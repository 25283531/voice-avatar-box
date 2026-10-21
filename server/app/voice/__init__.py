"""自研语音链路（V1）：WebSocket 音频会话 + 设备 OTA 端点 + MQTT/UDP 传输。

模块划分
--------
=====================  ====================================================
``__init__.py``        本文件：暴露 :func:`attach`，注册 WS 端点与 OTA 路由
``opus_codec.py``      ctypes 直连 libopus，模块级 ``decode_opus`` / ``encode_opus``
``asr_tts.py``        OpenAI 兼容的 ASR / TTS 客户端 + 音色回落链
``session.py``         一次连接一个 :class:`VoiceSession`（状态机 + 全链路编排）
``prompts.py``         语音专用基础口令与固定话术
``mqtt_udp.py``        F1：MQTT broker + UDP 音频网关 + WS 形状传输适配器
                       （**默认关闭**，``ELDER_MQTT_PORT`` 门控，见该模块注释）
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
6. **MQTT+UDP 默认关闭且 Phase 1 只有明文**：``ELDER_MQTT_PORT=0``（默认）
   时 broker 不起、OTA 不下发 mqtt 段；开启后 1883 上凭据可被网络攻击者
   截获重放，TLS（8883 + IP 短期证书）是已声明的后续增量，见
   ``mqtt_udp.py`` 模块注释与 docs/DEPLOY.md。
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Final

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocket

from .. import config
from ..macs import mac_variants, norm_mac
from ..models import ActivationCode
from ..ratelimit import client_ip, ota_new_code_gate, record_ota_new_code
from ..security import (
    code_expires_at,
    format_code,
    generate_activation_code,
    issue_device_token,
)
# 关闭码与 VoiceSession 从本包**再导出**：调用方（main.py 的接线、冒烟）
# 只需要 `from app.voice import attach`，不该再去摸 session 的内部名字。
# 用 `as` 形式是显式再导出（ruff 的 F401 对 `X as X` 视为有意导出）。
from .mqtt_udp import mqtt_section
from .session import (
    CLOSE_DEVICE_MISMATCH as CLOSE_DEVICE_MISMATCH,
    CLOSE_UNAUTHORIZED as CLOSE_UNAUTHORIZED,
    VoiceSession as VoiceSession,
)

log = logging.getLogger(__name__)

#: 单个 MAC 下最多保留多少个**未使用**激活码。
#:
#: ⚠️ 2026-10-20 二次改版（用户拍板「10 分钟过期换新码」）后，正常路径下每个
#: MAC 同时**最多 1 枚有效码**（见 :func:`_ensure_activation_code` 的退役兄弟码
#: 逻辑）。这个常量只作为历史数据兜底与排查参考。
_MAX_ACTIVE_CODES_PER_MAC: Final[int] = 10

#: 已过期但**保留多久**才删（自 expires_at 起算）。
#:
#: 为什么留 1 小时而不是立刻删：设备刚过期就删，正在输入的用户会从
#: 「激活码已过期，请在设备上重新获取」退化成「激活码不存在」，后者更含糊。
#: 留 1 小时足够覆盖「看着屏输错两次、抬头重看」的节奏，同时把 10^6 码空间
#: 的占用压到「一小时内的活跃设备数 × 6」量级。
_ACT_CODE_PURGE_GRACE: Final = timedelta(hours=1)

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
    """找该 MAC **当前仍在有效期内**的激活码；没有则 None。

    ⚠️ 2026-10-20 用户拍板改版：**有效期严格 10 分钟，过期即换新码**。
    这条取代了此前「一台设备一辈子一枚码 + 顺延有效期 + 24h 封顶」的做法。

    为什么改成严格过期（用户给的理由是「尽量避免撞库」，工程上成立）：
    长期有效的码等于**长期有效的凭据** —— 一旦被看到（拍照、录屏、旁人瞄一眼），
    攻击者有的是时间去撞。10 分钟把「泄漏 → 可用」的窗口压到十分钟，
    并且每轮换新码意味着旧码即使被记下也已经作废。

    四个判据：
    1. ``expires_at > now`` —— 严格有效期，**不顺延**。过期后由
       :func:`_ensure_activation_code` 新建一枚；
    2. ``used is False`` —— 已被绑定用掉的码不能再发（否则用户照着输必然
       「已使用」）；
    3. ``attempts < max_attempts`` —— 已被撞到作废的码同理；
    4. ``length(code) == ACT_CODE_LEN`` —— 老库里 13 位行不能发给新固件
       （新固件按 7 位校验，收到 13 位会拒绝上屏）。
    """
    from sqlalchemy import func, select

    now = _now()
    return db.scalar(
        select(ActivationCode).where(
            # 两种历史写法都查：否则老行（带冒号）会被判成「没有可用码」，
            # 于是每次轮询都新建一枚，10 分钟 TTL 下几分钟就把表撑起来。
            ActivationCode.device_id.in_(mac_variants(mac)),
            ActivationCode.used.is_(False),
            ActivationCode.attempts < ActivationCode.max_attempts,
            func.length(ActivationCode.code) == config.ACT_CODE_LEN,
            ActivationCode.expires_at > now,
        ).order_by(ActivationCode.id.desc())
    )


def _retire_codes_for(db: Any, mac: str, keep_id: int | None = None) -> int:
    """把该 MAC 名下**所有仍然有效**的未用码立刻作废（``expires_at = now``）。

    这是「服务端锁定」的另一半：一枚码只属于申请它的那台设备（``device_id``
    在申领时就写死），而这里再保证**同一台设备同一时刻只有一枚有效码** ——
    换新码时旧码立即失效，绑定成功后该设备名下不再残留任何可用码。

    为什么用 ``expires_at = now`` 而不是 ``used = True``：``used`` 的语义是
    「已被用于绑定」，用它来标记「退役」会让审计口径失真（明明没人绑过，
    却显示已使用）。置过期是准确的说法，且 ``bind_device`` 对过期码的
    提示（「已过期，请在设备上重新获取」）正好是用户该看到的话。

    返回被作废的行数。
    """
    from sqlalchemy import select, update

    now = _now()
    variants = mac_variants(mac)

    # ---- 读先行：一枚残留码都没有就直接返回，**不进写事务**。
    #
    # 为什么必须有这一步（这是热路径，不是洁癖）：
    #   本函数被 `ota_check` 的**已绑定分支**每 15 秒调一次，作为「绑定成功后
    #   不该再有任何可用码」的第二道保险（正常路径下 `bind_device` 已经作废过
    #   一遍，所以这里**永远匹配 0 行**）。
    #   没有这道短路时，每次轮询都要发 2 条 UPDATE（按两种历史写法各一条）
    #   + 1 次 COMMIT，即每台设备每 15 秒抢一次**写锁**。1000 台 ≈ 576 万次
    #   空写事务/天，而且都在无鉴权的 OTA 路径上 —— 与「全表扫」叠在一起
    #   构成同一个 DoS 面（见 _find_device 与 schema_patch.NEW_INDEXES 注释）。
    #   SELECT 走 `ix_act_device_id`，代价比「UPDATE + COMMIT」低一个量级。
    #
    # ⚠️ 不要因此删掉整个调用：它防的是「绑定发生在旧版本服务端 / 管理员手工
    #    改过归属」这类真实场景，那时代码里没有别的路径会作废残留码。
    probe = select(ActivationCode.id).where(
        ActivationCode.device_id.in_(variants),
        ActivationCode.used.is_(False),
        ActivationCode.expires_at > now,
    )
    if keep_id is not None:
        probe = probe.where(ActivationCode.id != keep_id)
    if db.scalar(probe.limit(1)) is None:
        return 0

    total = 0
    # ⚠️ **单条 IN，不按变体循环**：``mac_variants`` 现在会返回最多 4~6 种写法
    # （大小写 × 分隔符，见 app/macs.py 的回归说明）。若按变体逐个发 UPDATE，
    # 已绑定分支每 15 秒一次调用就会变成 4~6 条空转写 —— 扩「容忍度」不该
    # 带来「写放大」，这两件事必须解耦。单条 IN 无论几种变体都只发一条语句，
    # 且每个值都是常量，``ix_act_device_id`` 照常可用。
    #
    # synchronize_session=False：这是一次纯 SQL 批量更新，不需要 ORM 去「同步」
    # 内存里已加载的对象。开着它会让 SQLAlchemy 用 Python 求值 WHERE 条件，
    # 而 SQLite 读回的 expires_at 是 naive、这里的 now 是 aware，
    # 直接抛 `can't compare offset-naive and offset-aware datetimes`。
    stmt = update(ActivationCode).where(
        ActivationCode.device_id.in_(variants),
        ActivationCode.used.is_(False),
        ActivationCode.expires_at > now,
    )
    if keep_id is not None:
        stmt = stmt.where(ActivationCode.id != keep_id)
    res = db.execute(
        stmt.values(expires_at=now).execution_options(synchronize_session=False))
    total = int(res.rowcount or 0)
    db.commit()
    return total


def _purge_stale_codes(db: Any) -> int:
    """删掉早已过期且从未被使用的激活码行，返回删除条数。

    ⚠️ 为什么必须有：``activation_codes`` 原先**没有任何清理**。7 位码空间
    只有 10^6，而 OTA 端点无鉴权、按任意 ``Device-Id`` 都会建行 —— 攻击者
    拿一批伪造 MAC 打几小时就能把表撑满，之后**所有真实设备都取不到码**
    （唯一索引撞满 → 生成失败），这是不可逆的功能性瘫痪。

    ⚠️ 改 10 分钟 TTL 之后这条清理**更重要了**：一台设备一天会换 144 枚码
    （24h × 6 次/小时）。保留期必须远短于「码空间 / 设备数」，否则 1000 台
    设备一周就能把 10^6 吃光。现取 1 小时宽限（见 ``_ACT_CODE_PURGE_GRACE``）。

    幂等：DELETE 条件只依赖数据本身，重复执行结果相同（第 2 次删 0 行）。
    """
    from sqlalchemy import delete

    cut = _now() - _ACT_CODE_PURGE_GRACE
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


def _mqtt_endpoint_host(request: Request) -> str:
    """MQTT broker 的**设备可达主机名/IP**。

    口径与 ``device_api._voice_endpoints`` 完全一致：显式配了
    ``ELDER_PUBLIC_BASE_URL`` 用它的主机名（部署在反代/隧道后面时唯一
    正确的值），否则按请求头现场推导。刻意不复刻推导逻辑，而是复用同一
    个来源：两处一旦漂移，会出现「WS 能连、MQTT 连不上」而这种只在特定
    拓扑下出现的故障，本地开发（同源）永远复现不了。
    """
    if config.PUBLIC_BASE_URL:
        from urllib.parse import urlparse
        return urlparse(config.PUBLIC_BASE_URL).hostname or ""
    return (request.headers.get("host") or "").split(":")[0]


def _mqtt_section(mac: str, request: Request) -> dict[str, Any] | None:
    """OTA 响应的 ``mqtt`` 段；**未启用（``ELDER_MQTT_PORT<=0``）时返回 None**。

    ⚠️ 一旦返回该段，固件 ``application.cc:478`` 会**优先选 MQTT**、
    放弃 WebSocket —— 所以这不是「可选增强」，是通道切换，默认必须关。
    只给**已绑定**设备（调用方保证）：凭据本身不需要绑定，但会话鉴权要，
    给未绑定设备发 mqtt 段等于发一条它用不了的通道。
    """
    if config.MQTT_PORT <= 0:
        return None
    host = _mqtt_endpoint_host(request)
    if not host:
        log.warning("MQTT 已启用但推导不出设备可达的 broker 主机，"
                    "本设备不下发 mqtt 段（继续走 WS）")
        return None
    return mqtt_section(mac, host)


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


def _ensure_activation_code(db: Any, mac: str, request: Any = None) -> ActivationCode:
    """给未绑定设备一个激活码：优先复用，没有再建。

    ⚠️ 新建必须带**撞码重试**：7 位码空间只有 10^6，``code`` 上有唯一索引，
    撞了会抛 IntegrityError。这条路径是**无鉴权端点**（未绑定设备都能打），
    一个未捕获的 500 会让设备每 15s 撞一次 —— 必须自己兜住。

    ``request`` 用于按来源限「新建」次数（``ratelimit.ota_new_code_gate``）。
    闸门**只在真正要新建时**判定 —— 复用路径（设备每 15s 轮询的主路径）
    一次都不碰它，否则正常设备会被自己的轮询打满配额。
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

    # ---- 走到这里就是要**新建一行**（= 这个 MAC 从没申请过码）----
    if request is not None:
        verdict = ota_new_code_gate(request)
        if verdict is not None:
            log.warning("OTA 新建激活码被限速：MAC=%s 来源=%s，需等 %ss",
                        mac, client_ip(request), verdict.retry_after)
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, verdict.detail,
                headers={"Retry-After": str(verdict.retry_after)})

    for _ in range(8):
        code = generate_activation_code()
        clash = db.scalar(
            select(ActivationCode).where(ActivationCode.code == code).limit(1)
        )
        if clash is not None:
            continue
        row = ActivationCode(
            code=code,
            # 入库一律存**归一化**后的键（不带冒号、小写）：这样
            # `ActivationCode.device_id` 与 `Device.device_id` 的历史格式一致，
            # 且「一 MAC 一枚有效码」的锁定才可能成立（见 app/macs.py）。
            device_id=norm_mac(mac),
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
        # 服务端锁定：同一台设备同一时刻只允许有一枚有效码。换新码的瞬间
        # 旧码立即作废 —— 否则 10 分钟换一次会同时存在多枚可用码，
        # 「过期换新」想压窄的撞库窗口又被打回去了。
        retired = _retire_codes_for(db, mac, keep_id=row.id)
        if retired:
            log.info("MAC %s 换新码，作废旧码 %d 枚", mac, retired)
        if request is not None:
            record_ota_new_code(request)   # 只记「新建」成功的那一笔
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
    mac_raw = (device_id or "").strip()
    # 归一化后才是**库键**（`ActivationCode.device_id` / `Device.device_id` 都用它）。
    # 原始值只用于日志，保留冒号便于和固件日志对照。
    mac = norm_mac(mac_raw)
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
            # ⚠️ 用户要求：「已经绑定用户的设备，在用户没有解绑之前，不再下发
            # 激活码，不允许重复绑定」。本分支**只回 websocket 段、绝不含
            # activation 段** —— 固件的 probe 正是靠「有 websocket、无 code」
            # 判定已绑定并停止上屏（elder_bind_guide.cc 的 Probe::kBound）。
            #
            # 同时把这台设备名下可能残留的未用码全部作废：绑定成功那一刻起，
            # 任何还活着的码都是「能把这台已绑定设备再绑一次」的隐患。
            # 正常路径下 bind_device 已经作废过一遍，这里是第二道保险
            # （例如绑定发生在旧版本服务端、或管理员手工改过归属）。
            stale = _retire_codes_for(db, str(dev.device_id or mac))
            if stale:
                log.info("OTA：设备 %s 已绑定，作废残留激活码 %d 枚", mac, stale)
            # ---- 来源 IP 变化 ⇒ 一条干净的可疑信号
            #
            # 为什么需要它：本分支对**已绑定设备无条件现铸** device_token（身份
            # 就是 MAC）。解绑 → 新家庭绑定之后，原主人若还留着这个 MAC，打一次
            # OTA 就能拿到一枚 ep 正确的新 token —— 也就是说 `token_epoch` 让
            # **旧 token** 失效了，但堵不住 **MAC 持有者**（见决策记录 D44 的
            # 残余风险段）。v1 协议内封不住这条路径（要设备与服务端共享密钥，
            # 即 activation v2 HMAC），所以至少要留下可观测的痕迹。
            #
            # 为什么必须**条件写**：这是 15s 轮询的热路径，无条件写 = 每设备
            # 每天 5760 次 UPDATE + COMMIT，把 OTA 变成写放大器（与 _find_device
            # 的全表扫是同一个 DoS 面）。
            #
            # ⚠️ **首轮必然静默**：`Device.ip` 在这一版之前从没被写入过，存量行
            # 全是空串 ⇒ `prev_ip` 为空 ⇒ 只记录、不告警。所以「部署后没看到任何
            # 告警」是**预期**的，不是告警坏了；真正有意义的信号出现在同一台设备
            # 第二次从**不同网络**来取 token 时。
            cur_ip = ""
            try:
                cur_ip = client_ip(request) if request is not None else ""
            except Exception:      # noqa: BLE001  取不到 IP 绝不能影响发 token
                cur_ip = ""
            if cur_ip and cur_ip != "unknown":
                prev_ip = str(getattr(dev, "ip", "") or "")
                if prev_ip and prev_ip != cur_ip:
                    log.warning(
                        "设备 token 异地重签：dev_pk=%s mac=%s prev=%s cur=%s "
                        "（MAC 即身份，持 MAC 者可无条件换 token，需人工确认是否本人）",
                        dev.id, dev.device_id, prev_ip, cur_ip)
                if cur_ip != prev_ip:
                    dev.ip = cur_ip
                    db.commit()
            token = issue_device_token(int(dev.id), str(dev.device_id or mac),
                                       int(getattr(dev, "token_epoch", 0) or 0))
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
            # MQTT+UDP 通道（F1）：**仅在 ELDER_MQTT_PORT>0 时**出现。
            # 它一出现，固件就优先走 MQTT（application.cc:478）——
            # 这是运维的显式选择，不是默认行为。
            mqtt = _mqtt_section(mac, request)
            if mqtt is not None:
                payload["mqtt"] = mqtt
                log.info("OTA：设备 %s 追加下发 mqtt 段（%s），设备将改走 MQTT+UDP",
                         mac, mqtt["endpoint"])
            log.info("OTA：设备 %s 已绑定（owner=%s），下发 WS 通道、不发激活码",
                     mac, dev.owner_id)
            return JSONResponse(content=payload)

        # ---- 未绑定 / 未知设备：只给激活码，**不带 websocket 段**
        row = _ensure_activation_code(db, mac, request)
        expires_at = row.expires_at
        if expires_at.tzinfo is None:          # SQLite 读回是 naive，按 UTC 解释
            from datetime import timezone as _tz
            expires_at = expires_at.replace(tzinfo=_tz.utc)
        left = int((expires_at - _now()).total_seconds())
        payload = {
            "server_time": _server_time(),
            "activation": {
                "code": row.code,
                # 固件屏显「10 分钟内有效」用；不解析也不影响旧固件
                "expires_in": max(0, left),
                "expires_at": expires_at.isoformat(),
                "message": "请在手机 APP 输入激活码，把设备绑定到您的账户",
            },
        }
        firmware = _firmware_section()
        if firmware is not None:
            payload["firmware"] = firmware
        log.info("OTA：设备 %s 未绑定，下发激活码 %s（%ds 后过期，无 websocket 段）",
                 mac, format_code(row.code), max(0, left))
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
    """按 MAC 查设备。独立成函数而不是内联 select：调用点要的是"有/没有"。

    ⚠️ 比较必须在**归一化之后**做，且要能对上两种历史写法：

        固件 `Device-Id` 头：``aa:bb:cc:dd:ee:ff``
        `Device.device_id` 存量：``aabbccddeeff``

    原先只做 ``lower()``，于是带冒号的请求永远匹配不到不带冒号的存量行 ——
    后果不是「查不到设备」这么轻：一台**已绑定**的设备会被判成未绑定，
    服务端于是给它下发激活码，直接违反「已绑定设备不再下发激活码」。

    ⚠️ 为什么用 ``IN (mac_variants(...))`` 而不是 ``lower(replace(...))``
    -------------------------------------------------------------------
    本函数在 ``GET /api/voice/ota`` 这条**无鉴权、设备每 15s 轮询**的路径上。
    之前那版写成::

        col = func.lower(func.replace(func.replace(Device.device_id, ":", ""), "-", ""))
        select(...).where(col == norm_mac(mac))

    **函数包住列，``device_id`` 上的索引就废了** ⇒ 每个请求全表扫 ``devices``。
    这是个 DoS 放大器：攻击者并发刷这个端点，正常设备就在这条路径上排队。

    现在改成「先算出全部历史写法，再用 ``IN`` 等值比较」：
      * ``IN`` 的每个值都是常量，**索引照常可用**；
      * 查询侧覆盖的是**统一分隔符**（无分隔 / 全 ``:`` / 全 ``-``）× 大小写的
        6 种形态 —— 也就是「MAC 标签印成全大写」与「网页表单手输全小写」
        这两种**真实形态**；
      * ⚠️ 这**不是**旧写法的等价替换，覆盖面**严格更窄**：被替换掉的
        ``lower(replace(replace(...)))`` 连**混合大小写**（``Aa:Bb:..``）、
        混合分隔符（``aa:bb-cc:..``）、重复分隔符（``aa::bb..``）都能命中，
        本函数都不覆盖。这三族由 ``schema_patch.normalize_device_macs()``
        收敛成单一写法，**不在查询侧解决** —— 分隔符是开放集合，
        塞进 ``IN`` 列表会无限膨胀。⇒ 部署前必须跑一次存量 MAC 检查，
        确认迁移能成功执行，见 docs/DEPLOY.md。

    ⚠️⚠️ **禁止**在「``IN`` 没命中」时回退一次 ``lower(replace(...))`` 查询
    ------------------------------------------------------------------
    这个"兜底"看起来最顺手，实际是本函数里**最坏**的一种改法：

      * ``IN`` 未命中的情形**完全由调用方控制** —— 随便报一个不存在的 MAC
        就能稳定触发回退分支；
      * 于是无鉴权端点变成「6 次索引 seek + 1 次全表扫」，比现状差一个量级；
      * 这一版花了整轮才把全表扫从这条热路径上拆掉，回退等于自毁。

    正确做法是让迁移把存量收敛掉（迁移遇冲突会 ``log.error`` 提示人工介入），
    而不是把成本转移给每一个请求。

    （注：本函数的**读先行探针** ``_retire_codes_for`` 里的那条 SELECT 不属于
    这个禁令 —— 它查的是 ``activation_codes`` 且走 ``ix_act_device_id``，
    不扫 ``devices``；这里禁止的是「按 ``devices`` 全表扫兜底」。）

    ⚠️ 为什么必须 ``order_by(id)`` + 多行告警（**变体越多越危险**）
    ----------------------------------------------------------
    变体从 2 种扩到 4~6 种之后，出现了一个两变体时代不存在的失败模式：
    库里若同时存在 ``AA:BB:..`` 与 ``aa:bb:..`` **两行**（唯一索引允许，
    因为字符串不同），``IN`` 会**同时命中两行**。

    而 :func:`db.scalar` 在无 ``ORDER BY`` 时返回哪一行**由执行计划决定、
    不保证稳定**。后果不是「查不到」（那至少有明确症状），而是：

      * 同一台设备在两次 15s 轮询之间**漂到不同的 ``Device`` 行**；
      * 激活码时有时无（这一次算未绑定、下一次算已绑定）；
      * ``reminders.device_id`` 只指向其中一行 ⇒ **老人可能收不到已排期的
        服药提醒**，而且没有任何报错。

    所以这里显式 ``order_by(Device.id)`` 固定取最早那行（可复现），
    并在命中多行时打 ``log.error`` —— 这是「存量写法未归一化」的确定性证据，
    应当人工合并（自动合并要迁一串外键，风险更高）。
    """
    from sqlalchemy import select

    from .. import models

    rows = db.scalars(
        select(models.Device)
        .where(models.Device.device_id.in_(mac_variants(mac)))
        .order_by(models.Device.id)
        .limit(2)
    ).all()
    if not rows:
        return None
    if len(rows) > 1:
        # 同一 MAC 的两种写法各占一行（唯一索引允许，字符串不同）。
        # 不在这里合并：要迁 reminders / events / interactions 一串外键，
        # 自动做的风险远大于收益。但必须**喊出来** —— 否则表现是
        # 「提醒时有时无」这类无从排查的故障。
        log.error(
            "同一 MAC 命中多台设备行（存量写法未归一化，请人工合并）：mac=%s ids=%s",
            norm_mac(mac), [r.id for r in rows])
    return rows[0]


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


# ================================================================ MQTT+UDP 传输（F1）
#: 运行中的 MQTT/UDP 服务端实例。None = 未启用或未启动。
_transports: Any = None


async def start_transports() -> None:
    """启动 MQTT broker + UDP 网关（``ELDER_MQTT_PORT>0`` 才真起）。

    由 ``main.py`` 的 lifespan 调用，**不在 import 期起**：import 时没有
    事件循环，且冒烟脚本要能显式控制启停。幂等：重复调用直接返回。
    """
    global _transports
    if config.MQTT_PORT <= 0:
        return
    if _transports is not None:
        return
    from .mqtt_udp import MqttUdpServer
    _transports = MqttUdpServer()
    await _transports.start()


async def stop_transports() -> None:
    """停止 MQTT/UDP 传输并清空实例。幂等。"""
    global _transports
    if _transports is None:
        return
    server, _transports = _transports, None
    try:
        await server.stop()
    except Exception:  # noqa: BLE001  停机路径不能反抛
        log.warning("MQTT+UDP 传输停止异常", exc_info=True)
