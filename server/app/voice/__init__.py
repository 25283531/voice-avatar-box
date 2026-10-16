"""自研语音链路（V1）：WebSocket 音频会话 + 设备 OTA 端点。

模块划分
--------
=====================  ====================================================
``__init__.py``        本文件：暴露 :func:`attach`，注册 WS 端点与 OTA 路由
``opus_codec.py``      ctypes 直连 libopus，模块级 ``decode_opus`` / ``encode_opus``
``asr_tts.py``        OpenAI 兼容的 ASR / TTS 客户端 + 音色回落链
``session.py``         一次连接一个 :class:`VoiceSession`（状态机 + 全链路编排）
``prompts.py``         语音专用基础口令与固定话术
=====================  ====================================================

接线约定
--------
``main.py`` 里只写一行 ``voice.attach(app)``。**``attach`` 幂等**：
重复调用不会注册出两份路由（uvicorn 会把同一路径的第二个路由也加进去，
表现为"偶发打到另一份 handler"，极难排查）。冒烟脚本直接调它，
所以在 ``attach`` 内部做去重，而不是指望调用方自觉。

⚠️ V1 已知边界（**不假装支持**）
-------------------------------
1. **流式 TTS 未实现**：拿完整句音频再切帧发，首句延迟 = 完整推理时间。
2. **MCP 忽略**：设备发 ``{"type":"mcp"}`` 只记日志，不响应。
3. **realtime 模式按 auto 处理**：我们没有 AEC，服务端 VAD 只做能量判断。
4. **激活 v2（HMAC）不支持**：``POST /api/voice/ota/activate`` 直接 400。
5. **音频只在 24kHz/mono/60ms 一档**，不协商（固件按我们声明的解）。
"""
from __future__ import annotations

import logging
from typing import Any, Final

from fastapi import APIRouter, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocket

from .. import config
from ..models import ActivationCode
from ..security import (
    code_expires_at,
    format_code,
    generate_activation_code,
    issue_device_token,
)
# 关闭码与 VoiceSession 从本包**再导出**：调用方（main.py 的接线、冒烟）
# 只需要 `from app.voice import attach`，不该再去摸 session 的内部名字。
# 用 `as` 形式是显式再导出（ruff 的 F401 对 `X as X` 视为有意导出）。
from .session import (
    CLOSE_DEVICE_MISMATCH as CLOSE_DEVICE_MISMATCH,
    CLOSE_UNAUTHORIZED as CLOSE_UNAUTHORIZED,
    VoiceSession as VoiceSession,
)

log = logging.getLogger(__name__)

#: 单个 MAC 下最多保留多少个未过期激活码。**无鉴权端点的防刷措施**：
# 设备反复重启会反复来要码，若每次都新建，activation_codes 表会被
# 一台设备刷满，而真正要绑定的用户反而可能拿到已过期的码。
_MAX_ACTIVE_CODES_PER_MAC: Final[int] = 10


def _ws_endpoint(request: Request) -> str:
    """推导下发给设备的 WS 地址。

    **口径与 ``device_api._voice_endpoints`` 完全一致**（含
    ``VOICE_PUBLIC_BASE`` 逃生舱）。刻意不复刻一份：两处推导逻辑一旦
    漂移，会出现"心跳里给的地址能连、OTA 给的连不上"这种只在特定
    部署拓扑下出现的故障，而本地开发（同源）永远复现不了。

    ``device_api._voice_endpoints`` 返回 (ws_url, ota_url)，这里只取前者。
    """
    from ..routers.device_api import _voice_endpoints

    ws_url, _ota_url = _voice_endpoints(request)
    return ws_url


def _server_time() -> dict[str, Any]:
    """服务端时间段。timezone_offset 固定 Asia/Shanghai = +480 分钟。

    为什么写死而不是从 ``config.TZ`` 反推：固件 ota.cc 只做
    ``ts += offset * 60 * 1000`` 的算术，拿不到负偏移时的夏令时规则；
    而项目面向国内部署，TZ 事实上恒为 Asia/Shanghai。
    """
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    return {"timestamp": int(now.timestamp() * 1000), "timezone_offset": 480}


def _reusable_activation_code(db: Any, mac: str) -> ActivationCode | None:
    """找该 MAC 未过期的可复用激活码；没有则 None。"""
    from sqlalchemy import func, select

    now = _utcnow()
    total = db.scalar(
        select(func.count()).select_from(ActivationCode).where(
            ActivationCode.device_id == mac,
            ActivationCode.used.is_(False),
            ActivationCode.expires_at > now,
        )
    ) or 0
    if total >= _MAX_ACTIVE_CODES_PER_MAC:
        # 已有一摞未过期的码：只复用最新的，绝不新建。
        log.info("MAC %s 已有 %d 个未过期激活码，只复用不新建", mac, total)
    row = db.scalar(
        select(ActivationCode).where(
            ActivationCode.device_id == mac,
            ActivationCode.used.is_(False),
            ActivationCode.expires_at > now,
        ).order_by(ActivationCode.id.desc())
    )
    return row


def _utcnow():
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc)


def _ensure_activation_code(db: Any, mac: str) -> ActivationCode:
    """给未绑定设备一个激活码：优先复用，没有再建。"""
    row = _reusable_activation_code(db, mac)
    if row is not None:
        return row
    code = generate_activation_code()
    row = ActivationCode(
        code=code,
        device_id=mac,
        used_by=None,
        used=False,
        expires_at=code_expires_at(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


router = APIRouter(prefix=config.VOICE_OTA_PATH, tags=["voice"])


# 只注册**一条**路由（不带尾斜杠）。Starlette 的 redirect_slashes 默认开启，
# 会自己把 /api/voice/ota/ 307 到 /api/voice/ota —— 固件 ota.cc 的
# Http 客户端跟随重定向。刻意不手动注册两条：两条会让 OpenAPI 出现
# Duplicate Operation ID 警告，且将来改一处忘另一处就是两份行为。
@router.api_route("", methods=["GET", "POST"], include_in_schema=False)
async def ota_check(
    request: Request,
    response: Response,
    device_id: str = Header(default="", alias="Device-Id"),
    client_id: str = Header(default="", alias="Client-Id"),
    activation_version: str = Header(default="", alias="Activation-Version"),
    body: dict | None = None,
) -> Any:
    """设备开机 OTA 检查（**无鉴权**，身份靠 ``Device-Id`` = MAC）。

    固件侧：``ota.cc`` 开机 POST 到这里（也接受 GET），把
    ``server_time`` 与 ``websocket`` / ``activation`` 段写进 NVS。

    ``include_in_schema=False``（见装饰器处注释）：消费者是固件而非前端。

    为什么这个端点无鉴权却不能乱给东西
    ---------------------------------
    设备此时**还没有 token**（token 要从这个响应里拿），所以无法要求
    鉴权。但正因如此，**只有已绑定设备才给 token** —— 给未绑定设备发
    token 等于开一个无主账户：任何人都能拿一个 MAC 换一个能调 LLM、
    能读该 owner 记忆的凭证。

    激活码为什么复用而不每次新建：见 :data:`_MAX_ACTIVE_CODES_PER_MAC`。
    """
    mac = (device_id or "").strip()
    if not mac:
        # 没有 MAC 就无法判断绑定状态，也无法把激活码关联到设备
        return JSONResponse(
            status_code=400,
            content={"error": "missing-device-id",
                     "message": "缺少 Device-Id 请求头（应为设备 MAC）"},
        )

    from ..db import SessionLocal

    with SessionLocal() as db:
        dev = _find_device(db, mac)

        if dev is not None and dev.owner_id is not None:
            # ---- 已绑定：发时间 + WS 通道（含现铸的设备 token）
            token = issue_device_token(int(dev.id), str(dev.device_id or mac))
            payload = {
                "server_time": _server_time(),
                "websocket": {
                    "url": _ws_endpoint(request),
                    "token": token,
                },
            }
            log.info("OTA：设备 %s 已绑定（owner=%s），下发 WS 通道",
                     mac, dev.owner_id)
            return JSONResponse(content=payload)

        # ---- 未绑定 / 未知设备：只给激活码，**不带 websocket 段**
        row = _ensure_activation_code(db, mac)
        payload = {
            "server_time": _server_time(),
            "activation": {
                "code": row.code,
                "message": "请在手机 APP 输入激活码，把设备绑定到您的账户",
            },
        }
        log.info("OTA：设备 %s 未绑定，下发激活码 %s（无 websocket 段）",
                 mac, format_code(row.code))
        return JSONResponse(content=payload)


@router.post("/activate")
async def ota_activate(request: Request) -> Any:
    """激活 v2（固件带 Serial-Number + HMAC challenge）。

    **V1 不支持**：开发板走 v1 激活码流程（见 ``ota_check``），v2 需要
    固件与云端共享一份 HMAC 密钥，那是明确的后续增量。这里直接 400，
    而不是返回一个"看起来成功"的空响应 —— 假装成功会让设备以为
    绑定完成却拿不到 token，排障时先怀疑的是网络。
    """
    return JSONResponse(
        status_code=400,
        content={"error": "activation-v2-unsupported",
                 "message": "V1 仅支持激活码绑定（激活流程 v1）"})


def _find_device(db: Any, mac: str):
    """按 MAC 查设备。独立成函数而不是内联 select：调用点要的是"有/没有"，
    而 MAC 大小写在不同固件版本间不一致（有的全大写有的全小写），
    统一在这里做一次归一，避免两处各写一种口径。
    """
    from sqlalchemy import func, select

    from .. import models

    return db.scalar(
        select(models.Device).where(
            func.lower(models.Device.device_id) == mac.lower())
    )


# ================================================================ WebSocket
async def _voice_ws(websocket: WebSocket) -> None:
    """WS 端点 handler。**先 accept 再鉴权**（否则拿不到 Header 也没法 close 带码）。

    为什么不是握手期 401：Starlette 的 WebSocket 拒绝只能在 accept 之后
    send close 帧；握手期直接返回 HTTP 401 会让设备侧看到
    "handshake failed" 而不是可识别的关闭码，日志里两件事长得一样。

    ⚠️⚠️ 形参注解**必须是** ``WebSocket``，不能写 ``Any``（踩过）
    ----------------------------------------
    FastAPI 的 ``@app.websocket()`` 会对端点签名做依赖解析：只有注解为
    ``WebSocket``（starlette/fastapi 的那个类）的形参才会被认成 WS 对象，
    其余形参一律按 query / header 参数处理。写成 ``websocket: Any`` 时
    FastAPI 认为调用方要传一个**名为 websocket 的 query 参数**，
    于是每个连接都抛 ``WebSocketRequestValidationError``，而 uvicorn 对
    "accept 之前抛异常"的统一表现是 **HTTP 403 + connection rejected**
    —— 看起来像鉴权失败，实际是签名写错。**这个坑没有任何 import 期
    报错，只有真连一次才暴露**，所以冒烟第 2、3 组断言是它的唯一防线。
    """
    await websocket.accept()
    version = 1
    raw = (websocket.headers.get("protocol-version") or "").strip()
    if raw.isdigit():
        version = int(raw)
    sess = VoiceSession(websocket, protocol_version=version)
    await sess.run()


_ATTACHED: bool = False


def attach(app: FastAPI) -> None:
    """把语音链路注册到 FastAPI app 上。**幂等**（重复调用不重复注册）。

    ``main.py`` 只调一次；冒烟脚本在 voice 路由尚未接进 main 时自行调用，
    所以这里必须自己防重 —— 否则"先 attach 后 main 也 attach"会注册出
    两份 WS handler，请求随机落到其中一份，问题只在重连时才暴露。
    """
    global _ATTACHED
    if _ATTACHED:
        return

    # 先摘掉可能存在的旧路由：极端情况下（reload / 测试反复建 app）
    # 也要保证路径唯一。
    app.include_router(router)

    @app.websocket(config.VOICE_WS_PATH)
    async def _ws_endpoint(websocket: WebSocket) -> None:  # noqa: F811
        # 注解同样必须是 WebSocket，理由见 _voice_ws 的 docstring。
        await _voice_ws(websocket)

    _ATTACHED = True
    log.info("语音链路已注册：WS %s，OTA %s",
             config.VOICE_WS_PATH, config.VOICE_OTA_PATH)
