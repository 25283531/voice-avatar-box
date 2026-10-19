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
import time
from datetime import timedelta
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

#: 单个 MAC 下最多保留多少个**未使用**激活码。
#:
#: ⚠️ 2026-10-20 起这个上限**不再由「新建」路径消耗**：``_reusable_activation_code``
#: 改成「一台设备一辈子一枚码」（复用任意未用尽的行并顺延有效期），所以正常
#: 情况下每个 MAC 只会有 1 行。保留这个常量是为了给**历史数据**兜底：老库里
#: 可能已经攒了一摞（旧逻辑每 10 分钟新建一行且从不清理），运维排查时用得上。
_MAX_ACTIVE_CODES_PER_MAC: Final[int] = 10

#: 一枚激活码的**寿命上限**（自 created_at 起算）。设备每 15s 轮询一次，
#: 若每次都无条件顺延有效期，这枚码就永远不会过期 —— 一旦泄漏即永久凭据。
#: 24 小时足够覆盖「老人今天没空绑、明天再绑」，又让泄漏有确定的止损点。
_ACT_CODE_MAX_LIFETIME: Final = timedelta(hours=24)

#: 上次清理僵尸激活码的时间（time.monotonic）。清理是幂等 DELETE，
#: 挂在取码路径上按小时节流，不引入新的后台任务。
_last_purge_at: float = 0.0


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
    """找该 MAC 可复用的激活码；没有则 None。

    ⚠️ 四个判据，每个都对应一次真实的失败模式（2026-10-20 改 6+1 位时重写）：

    1. **不看 ``expires_at``**：原先只复用「未过期」的行，于是设备每过 10 分钟
       就新建一行，而 ``activation_codes`` **没有任何清理任务** —— 一台长期
       未绑定的设备能在一周内攒出上千行。7 位码空间只有 10^6，这些僵尸行会
       把可用空间直接吃掉。现在「一台设备长期一枚码」，复用时把有效期顺延。
    2. **但有效期不能无限顺延**：设备每 15s 轮询一次，若每次都无条件续期，
       这枚码永远不过期 ⇒ 一旦泄漏就是永久凭据。所以续期以
       ``created_at + _ACT_CODE_MAX_LIFETIME`` 封顶，到顶即退役换新码。
    3. **跳过已用尽的码**：``attempts >= max_attempts`` 的码已经被撞到作废，
       不能再发给用户（他照着屏幕输只会一直失败）。
    4. **按长度过滤**：老库里的 13 位行（1 字母 + 12 数字）不能发给新固件 ——
       新固件按 7 位校验，收到 13 位会拒绝上屏。
    """
    from datetime import timedelta

    from sqlalchemy import func, select

    row = db.scalar(
        select(ActivationCode).where(
            ActivationCode.device_id == mac,
            ActivationCode.used.is_(False),
            ActivationCode.attempts < ActivationCode.max_attempts,
            func.length(ActivationCode.code) == config.ACT_CODE_LEN,
        ).order_by(ActivationCode.id.desc())
    )
    if row is None:
        return None

    now = _now()
    created = row.created_at or now
    if created.tzinfo is None:
        created = created.replace(tzinfo=now.tzinfo)
    cap = created + _ACT_CODE_MAX_LIFETIME
    if cap <= now:
        # 到寿命上限：退役这枚码（used=True 让它再也不会被复用），下一轮新建
        row.used = True
        db.commit()
        log.info("MAC %s 的激活码到达寿命上限，退役换新", mac)
        return None

    # 顺延有效期（设备屏上正显示着这枚码，用户随时可能来绑），但不超过寿命上限
    fresh = code_expires_at()
    row.expires_at = fresh if fresh < cap else cap
    db.commit()
    db.refresh(row)
    return row


def _purge_stale_codes(db: Any) -> int:
    """删掉早已过期且从未被使用的激活码行，返回删除条数。

    ⚠️ 为什么必须有：``activation_codes`` 原先**没有任何清理**。改 7 位后空间
    只有 10^6，而 OTA 端点无鉴权、按任意 ``Device-Id`` 都会建行 —— 攻击者
    拿一批伪造 MAC 打几小时就能把表撑满，之后**所有真实设备都取不到码**
    （唯一索引撞满 → 生成失败），这是不可逆的功能性瘫痪。

    为什么「过期 1 天以上」而不是立刻删：设备刚过期就删，会让正在输入的用户
    拿到 404；留一天足够覆盖「看完屏、回屋拿手机、慢慢输」的真实节奏。
    幂等：DELETE 条件只依赖数据本身，重复执行结果相同（第 2 次删 0 行）。
    """
    from datetime import timedelta

    from sqlalchemy import delete

    cut = _now() - timedelta(days=1)
    res = db.execute(
        delete(ActivationCode).where(
            ActivationCode.used.is_(False),
            ActivationCode.expires_at < cut,
        )
    )
    db.commit()
    return int(res.rowcount or 0)


def _now():
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc)


def _firmware_section() -> dict[str, Any] | None:
    """OTA 响应的 ``firmware`` 段；未配置（任一变量为空）时返回 None。

    为什么在**调用时**读 ``config`` 模块属性而不是 import 期固化：
    ``config`` 是 import 期读环境变量的模块，冒烟脚本用
    ``importlib.reload(app.config)`` 切换场景，只有动态读才拿得到
     reload 后的值（与 ``device_api._voice_endpoints`` 的读点同一形态）。

    为什么两个都非空才返回：只配一半 = 配置不完整，下一个缺字段的段
    只会让固件白做一次版本比较，不如不返回让设备保持「不 OTA」的现状。
    版本比较由固件自己做（``ota.cc`` 的 ``IsNewVersionAvailable``），
    服务端只报告最新可用版本，不判断设备当前版本。
    """
    version = (config.FIRMWARE_VERSION or "").strip()
    url = (config.FIRMWARE_URL or "").strip()
    if not version or not url:
        return None
    return {"version": version, "url": url}


def _ensure_activation_code(db: Any, mac: str) -> ActivationCode:
    """给未绑定设备一个激活码：优先复用，没有再建。

    ⚠️ 新建必须带**撞码重试**：7 位码空间只有 10^6，``code`` 上有唯一索引，
    撞了会抛 IntegrityError。这条路径是**无鉴权端点**（未绑定设备都能打），
    一个未捕获的 500 会让设备每 15s 撞一次 —— 必须自己兜住。
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    global _last_purge_at
    now_mono = time.monotonic()
    if now_mono - _last_purge_at > 3600:
        _last_purge_at = now_mono
        try:
            purged = _purge_stale_codes(db)
            if purged:
                log.info("清理过期激活码 %d 行（释放码空间）", purged)
        except Exception:      # noqa: BLE001  清理失败绝不影响取码
            db.rollback()
            log.warning("清理过期激活码失败，本轮跳过", exc_info=True)

    row = _reusable_activation_code(db, mac)
    if row is not None:
        return row

    for _ in range(8):
        code = generate_activation_code()
        clash = db.scalar(
            select(ActivationCode).where(ActivationCode.code == code).limit(1)
        )
        if clash is not None:
            continue
        row = ActivationCode(
            code=code,
            device_id=mac,
            used_by=None,
            used=False,
            expires_at=code_expires_at(),
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # 并发撞码：另一个请求刚插了同一个 code，回滚后重试
            db.rollback()
            continue
        db.refresh(row)
        return row

    raise RuntimeError(f"激活码生成失败（连续 8 次撞码），MAC={mac}")


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

    固件段（可选）：``ELDER_FIRMWARE_VERSION`` 与 ``ELDER_FIRMWARE_URL``
    **都配置了**才在两条路径的响应里带上 ``firmware`` 段（版本比较由固件
    自己做，服务端只报告最新可用版本）；未配置时不返回该段，行为与
    配置该特性之前逐字一致。见 :func:`_firmware_section` 与
    docs/DEPLOY.md §1.10。

    为什么这个端点无鉴权却不能乱给东西
    ---------------------------------
    设备此时**还没有 token**（token 要从这个响应里拿），所以无法要求
    鉴权。但正因如此，**只有已绑定设备才给 token** —— 给未绑定设备发
    token 等于开一个无主账户：任何人都能拿一个 MAC 换一个能调 LLM、
    能读该 owner 记忆的凭证。

    激活码为什么复用而不每次新建：见 :func:`_reusable_activation_code`
    （7 位码空间只有 10^6，一台设备反复新建会把可用空间吃光）。
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
            firmware = _firmware_section()
            if firmware is not None:
                payload["firmware"] = firmware
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
        firmware = _firmware_section()
        if firmware is not None:
            payload["firmware"] = firmware
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
