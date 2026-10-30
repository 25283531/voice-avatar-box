"""设备侧接口：心跳同步、提醒回执、交互上报。

固件每 N 分钟调一次 /api/device/sync，一次拿全：
服务器时间（校时）、提醒规则与排期、当前形象、AI 供应商、WS/OTA 地址。
离线期间设备按本地 RTC 与已下发规则自行响铃，联网后把回执补齐。
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .. import geo
from .. import reminder_audit
from ..config import (
    PUBLIC_BASE_URL,
    SCHEDULE_AHEAD_HOURS,
    TZ,
    VOICE_OTA_PATH,
    VOICE_WS_PATH,
    WS_PUBLIC_BASE_URL,
)
from ..db import get_db
from ..deps import get_current_device
from ..llm_gateway import resolve_row
from ..chat_quota import quota_snapshot
from ..model_catalog import DEVICE_KINDS, KIND_LLM
from ..models import ConfirmMethod, EventState, Interaction, ReminderEvent, utcnow
from ..schedule import (TZ_MARGIN, event_due_utc, local_now, materialize_events,
                        next_occurrence)
from ..schemas import (
    AvatarOut,
    DeviceQuotaOut,
    DeviceSyncOut,
    DeviceWifiChangeOut,
    DeviceWifiResultIn,
    EventOut,
    EventReportIn,
    InteractionReportIn,
    LocalReminderIn,
    LocalReminderReportOut,
    ProviderSafeOut,
    ReminderOut,
)

router = APIRouter(prefix="/api/device", tags=["device"])

log = logging.getLogger("elder.device_api")


def _voice_endpoints(request: Request) -> tuple[str, str]:
    """推导下发给设备的语音通道与 OTA 地址。

    自研语音链路和本 API 是**同一个服务、同一个端口**，所以地址天然同源，
    只需要把请求里的 scheme（ws/wss）与 Host 组合出来，不必像以前那样
    配一个指向外部容器的内网地址。这么做的好处：

    - 部署时不用再解释「为什么还要填一个服务端的地址」，减少一类配置错误；
    - 设备走 HTTPS 域名进来时自动得到 `wss://`，不会出现 ws/wss 混用
      被拒的情况（放在反代后面尤其重要）；
    - 单一镜像，没有第二个服务需要编排。

    三种部署模式（``ELDER_DEPLOY_MODE``，启动期校验与摘要见
    ``config._deploy_startup_check``）：

    - ``direct`` / ``auto``（默认）：未配对外地址时**按请求现场推导** ——
      scheme 取 ``request.url.scheme``，Host 取请求头。注意镜像没有给
      uvicorn 传 ``--proxy-headers``，而 uvicorn 只信任来自 127.0.0.1 的
      ``X-Forwarded-Proto``：生产里对端是 Docker 网桥网关，scheme 恒为
      ``http``。所以这种推导**只在直暴露（公网 IP:端口）下天然正确**，
      TLS 反代 / 隧道后面必须显式配对外地址。
    - ``tunnel``：启动期强制要求显式对外地址（缺了直接启动失败，不静默
      降级），本函数优先用它下发，与请求无关。
      ⚠️ 判据是**拓扑**不是工具：**任何在前面做 TLS 终止的后端容器**
      （Cloudflare Tunnel / Nginx / Caddy / OpenResty……）拿到的都是明文
      HTTP，都属于这一类——**不用 Cloudflare Tunnel 也要设 tunnel**。

    显式对外地址的来源与优先级（``config.PUBLIC_BASE_URL`` 已合并好）：
    ``ELDER_PUBLIC_BASE_URL`` > ``ELDER_VOICE_PUBLIC_BASE``（旧逃生舱，
    语义逐字未变）；WS 侧 ``ELDER_WS_PUBLIC_BASE_URL``（单独域名）优先，
    未配则由 HTTP 地址换 scheme 推导。
    """
    if PUBLIC_BASE_URL:
        base = PUBLIC_BASE_URL
        # WS 地址：显式配了单独域名（wss://...）用它；否则由 HTTP 地址换
        # scheme 推导 —— 与旧变量时代的口径逐字一致。
        ws_base = WS_PUBLIC_BASE_URL or \
            base.replace("https://", "wss://").replace("http://", "ws://")
        return f"{ws_base}{VOICE_WS_PATH}", f"{base}{VOICE_OTA_PATH}"

    scheme = "wss" if request.url.scheme == "https" else "ws"
    http_scheme = "https" if request.url.scheme == "https" else "http"
    host = request.headers.get("host") or request.url.netloc
    return (
        f"{scheme}://{host}{VOICE_WS_PATH}",
        f"{http_scheme}://{host}{VOICE_OTA_PATH}",
    )


_TZ_WARN_ATTR = "_elder_tz_fallback_warned"


def _device_tz_for(request: Request, dev: models.Device,
                   now: Any = None) -> tuple[str, int]:
    """这台设备该用哪个时区 → ``(IANA 名, 偏移分钟)``。

    为什么抽成独立函数：它有三层回落，而**任何一层没生效都没有症状** ——
    只是时区悄悄退化成北京。写在 ``sync()`` 里就只能起整套设备鉴权流程才
    测得到，那种测试最后通常会退化成"接口 200 就算过"。

    取值顺序（与提醒创建时的 ``_reminder_tz`` 前三层一致 —— 不一致就会变成
    "创建按 A 时区、响铃按 B 时区"，而两边各自的日志都正常）：

      1. 本次请求的 IP（最鲜：设备可能刚换网络，``Device.ip`` 还没更新）
      2. ``Device.ip``（设备上次来取 token 时记下的）
      3. ``ELDER_TZ`` 兜底（与改造前逐字一致：判定不出来仍按北京）

    判不出时区**绝不能**让心跳失败 —— 心跳一失败设备就整台离线，代价远大于
    "时区退回兜底"，所以两条链路都包了 try。
    """
    moment = now or local_now()

    # ⚠️ 不能拿"时区名是不是空串"当"判定失败"的判据：``geo.resolve`` **自己**
    # 就会在最后把空串填成 ``config.TZ.key`` 并把 ``source`` 记成 ``default``。
    # 所以第一层永远会返回一个非空名字 —— 按空串判会得到一个**永不可达**的
    # 兜底分支（何况"退化到兜底"这件事恰恰是必须留痕的那类故障）。
    # 正确判据是 ``source``。
    tz_name = ""
    source = ""
    try:
        guess = geo.resolve_request(request)
        tz_name, source = guess.timezone, guess.source
    except Exception:  # noqa: BLE001  判定失败不阻断心跳
        tz_name, source = "", ""

    if not tz_name:
        try:
            tz_name = geo.device_timezone(dev)
            source = "device" if tz_name else ""
        except Exception:  # noqa: BLE001
            tz_name, source = "", ""
    if not tz_name:
        tz_name = TZ.key
        source = "default"

    if source == "default":
        # 整条链退化成兜底必须留痕：这是"配置全对"与"链断了"长得完全一样的
        # 那类故障。只告警一次 —— 心跳每几分钟一次，每次都打等于没有告警。
        state = getattr(request.app, "state", None)
        if state is not None and not getattr(state, _TZ_WARN_ATTR, False):
            setattr(state, _TZ_WARN_ATTR, True)
            log.warning("设备时区判定退化为 ELDER_TZ（%s）：IP 判定链没给出结论，"
                        "不在该时区的设备会按 %s 响铃", tz_name, tz_name)

    return tz_name, geo.offset_minutes(tz_name, moment)


@router.post("/sync", response_model=DeviceSyncOut)
def sync(request: Request, fw_version: str = "", hw_model: str = "",
         db: Session = Depends(get_db),
         dev: models.Device = Depends(get_current_device)):
    """心跳 + 拉全量配置。

    ``hw_model``：固件探测到的芯片型号（如 ``ESP32-S3``，见固件
    ``sync.c`` 的 ``chip_model_name()``），与 ``fw_version`` 同为 query
    param、同形态上报。非空即写入设备行（用户端/管理端设备表的「硬件型号」
    列读它）；空 = 老固件未升级，保留原值不清空 —— 心跳是周期动作，
    一次异常请求不该把已采到的型号抹掉。
    """
    dev.online = True
    dev.last_seen_at = utcnow()
    if fw_version:
        dev.fw_version = fw_version
    hw = (hw_model or "").strip()
    if hw:
        dev.hardware_model = hw[:64]     # 与 Device.hardware_model 列宽对齐
    db.commit()

    # 为该设备排期（幂等）
    materialize_events(db, dev.id, hours=SCHEDULE_AHEAD_HOURS)

    rules = db.scalars(
        select(models.Reminder).where(models.Reminder.device_id == dev.id,
                                      models.Reminder.enabled.is_(True),
                                      # 软删除（2026-10-30）：已删除的规则**绝不能**
                                      # 下发。这是「删了还在响」最直接的入口 ——
                                      # 设备按这份列表在本地排期，下发一次它就
                                      # 天天响，服务端这边看起来"已经删了"。
                                      models.Reminder.deleted_at.is_(None))
    ).all()
    now = local_now()

    # 「这台设备现在几点」—— 下发给固件 setenv("TZ")，见 DeviceSyncOut 的说明。
    # 判定顺序与不阻断心跳的理由都写在 _device_tz_for 里。
    tz_name, tz_offset = _device_tz_for(request, dev, now)
    wall = now.replace(tzinfo=None)
    # ⚠️ due_at 是**规则时区的裸钟点**，跨时区后 SQL 范围条件不再是绝对时刻。
    # 先按 ±TZ_MARGIN 放宽粗筛（覆盖任何时区），再按绝对时刻精确过滤。
    upcoming = list(db.scalars(
        select(ReminderEvent)
        .where(ReminderEvent.device_id == dev.id,
               ReminderEvent.state == int(EventState.PENDING),
               ReminderEvent.due_at >= wall - TZ_MARGIN - timedelta(minutes=10),
               ReminderEvent.due_at <= wall + timedelta(hours=SCHEDULE_AHEAD_HOURS) + TZ_MARGIN)
        .order_by(ReminderEvent.due_at)
        .limit(200)
    ).all())
    upcoming = [e for e in upcoming
                if now - timedelta(minutes=10) <= event_due_utc(e)
                <= now + timedelta(hours=SCHEDULE_AHEAD_HOURS)]

    # 补齐「视野内一条实例都没有」的规则，让每条启用规则至少有一次触发随心跳下发。
    #
    # 为什么需要：SCHEDULE_AHEAD_HOURS(36h) 对「每天」类规则总是够用（相邻两次触发
    # 最多隔 24h），但对周/月级规则可能一条都装不下 —— 「每周一三五」的规则在周六看，
    # 下一次触发（周一 08:00）远在 35h 之外，materialize_events 建不出任何实例，
    # upcoming 就是空数组。设备此时完全不知道下次该什么时候响铃，
    # 本模块开头承诺的「一次拿全提醒排期」落空（老人也看不到下次服药时间）。
    #
    # 为什么逐条补、而不是直接放大 hours：SCHEDULE_AHEAD_HOURS 一放大，
    # 「每天 3 次」的规则会一次性灌进几百条实例，而响应侧 .limit(200) 会截断，
    # 排期表和载荷都跟着膨胀。逐条补最多每条规则 +1 条，代价恒定。
    covered = {e.reminder_id for e in upcoming}
    created = False
    for r in rules:
        if r.id in covered:
            continue
        nxt = next_occurrence(r, now)
        if nxt is None:
            continue
        ev = db.scalar(
            select(ReminderEvent).where(
                ReminderEvent.reminder_id == r.id, ReminderEvent.due_at == nxt
            )
        )
        if ev is None:
            ev = ReminderEvent(reminder_id=r.id, device_id=dev.id, due_at=nxt,
                               tz=r.tz or "",
                               state=int(EventState.PENDING))
            db.add(ev)
            created = True
        elif ev.state != int(EventState.PENDING):
            # 已响铃/已确认/已超时的实例不重排：重复下发会让老人重复响铃。
            continue
        upcoming.append(ev)
        covered.add(r.id)
    if created:
        db.commit()
    # 统一按**绝对时刻**排序：窗口查出来的行 due_at 是 SQLite 回读的裸钟点
    # （基准逐规则不同），刚 db.add 的对象还是 schedule 生成的 aware 值，
    # 直接排序会抛 TypeError: can't compare offset-naive and offset-aware。
    # 注意**不能**简单地补一个 UTC 标记了事 —— 那样排的是"墙上钟点"，
    # 混时区时（一条北京规则 + 一条纽约规则）顺序会错。
    upcoming.sort(key=event_due_utc)

    avatar = db.get(models.Avatar, dev.avatar_id) if dev.avatar_id else None
    # ⚠️⚠️ 下面这条查询是本项目最敏感的一行，**绝对不要**加 owner 或 record_type 条件。
    #
    # 理由（架构 §6.2/§13.7）：owner_id 是**角色归属**，而这里的行是**平台配置**
    # —— 平台配置的 owner 恒为 NULL，设备不属于任何用户。若"顺手"加上
    # `record_type == 'config'` 之外的 owner 条件（例如误写成
    # `owner_id == dev.owner_id`），平台配置会被整体过滤掉，
    # 设备心跳拿不到任何 LLM 配置 —— **设备直接失去对话能力，且不报错**。
    #
    # 为什么 `kind.in_(DEVICE_KINDS)` 就够了：角色行的 kind 是空串，天然不在
    # DEVICE_KINDS 里。这是"恰好安全"而非"必然安全"，但 `kind` 列本身有
    # 非空约束语义（schema 层已拒绝空 kind 建配置），所以不必再加条件 ——
    # 每多一个条件就多一个能配错的机会。
    rows = db.scalars(
        select(models.AIProvider)
        .where(models.AIProvider.enabled.is_(True),
               models.AIProvider.kind.in_(DEVICE_KINDS))
        .order_by(models.AIProvider.kind, models.AIProvider.priority)
    ).all()
    providers = [ProviderSafeOut.model_validate(r) for r in rows]

    # 平滑过渡：新部署很可能只配了 llm_main / llm_light 而一条老式 llm 都没有。
    # 这时用「默认对话模型」折算成一条 kind=llm 补位，保证设备手上永远有一条
    # 可用的 LLM —— 升级前后固件逻辑不变，不会出现「昨天还能聊、今天没模型了」。
    if not any(p.kind == KIND_LLM for p in providers):
        row = resolve_row(db, "chat")
        if row is not None:
            out = ProviderSafeOut.model_validate(row)
            providers.append(out.model_copy(update={"kind": KIND_LLM}))

    # 角色与形象：只用于决定「外观 + 音色」，不参与上面那段平台配置查询。
    # 软删的形象/角色在这里按「未绑定」处理 —— 直接不下发，而不是报错：
    # 心跳失败会让设备退化成离线状态，代价远大于少一份外观配置。
    if avatar is not None and avatar.deleted_at is not None:
        avatar = None
    role = db.get(models.AIProvider, dev.agent_id) if dev.agent_id else None
    if role is not None and (not role.is_role() or role.deleted_at is not None
                            or role.archived_at is not None):
        role = None

    db.refresh(dev)
    # 规划文档 7.3：心跳时顺带下发当日陪聊统计（已用秒数 / 空闲切断 / 服药关键词）。
    # ⚠️ 取消会员制后这里**没有上限**了 —— 已绑定设备恒为 unlimited。
    quota = DeviceQuotaOut(**quota_snapshot(db, device_ids=[dev.id],
                                            authorized=dev.owner_id is not None))
    ws_url, ota_url = _voice_endpoints(request)
    # 音色回落：**角色优先，形象兜底**（架构 §4.2）。
    # 角色有自己选定的音色时用它；没选（或角色缺失/已删）才用形象的默认音色。
    # 放在这里而不是存储层，是为了让 avatar 段保持"这个形象长什么样"的语义，
    # 而音色是"这台设备现在该用谁的声音" —— 随角色变，不随形象变。
    voice = (role.tts_voice if role is not None else "") or \
            (avatar.tts_voice if avatar is not None else "")

    # ---- 改 WiFi 下发（契约见 build-out/_wifi-spec.md §一）----
    #
    # 三条不变量，任何一条写错都会让老人家里的设备反复断网：
    #   1. **只有 pending 才下发**：delivered 之后不再发 —— 设备拿到同一条指令
    #      会再试连一次（Stop → Start → 等 45 秒），重复下发等于让 WiFi 反复闪；
    #   2. **下发即置 delivered 并立刻 commit**：必须在构造响应之前落盘，
    #      否则这次响应丢包/进程重启后设备再来问，服务端会**再发一遍**；
    #   3. **提速只在 pending 这一次生效**：下发完下一跳就回 300 ——
    #      不能因为"这台设备改过 WiFi"就永久 15 秒轮询（那会让全平台的
    #      设备数 × 20 倍心跳量，而收益是零）。
    wifi_change: DeviceWifiChangeOut | None = None
    sync_interval = 300
    if (dev.wifi_state or "") == "pending":
        wifi_change = DeviceWifiChangeOut(
            req_id=int(dev.wifi_req_id or 0),
            ssid=dev.wifi_req_ssid or "",
            password=dev.wifi_req_pwd or "",
        )
        dev.wifi_state = "delivered"
        db.commit()
        sync_interval = 15

    return DeviceSyncOut(
        server_time=now,
        device=dev,
        avatar=AvatarOut.model_validate(avatar) if avatar else None,
        reminders=[ReminderOut.model_validate(r) for r in rules],
        upcoming=[EventOut.model_validate(e) for e in upcoming],
        providers=providers,
        ws_url=ws_url,
        ota_url=ota_url,
        quota=quota,
        tts_voice=voice,
        timezone=tz_name,
        tz_offset_minutes=tz_offset,
        wifi_change=wifi_change,
        sync_interval_sec=sync_interval,
    )


@router.post("/wifi-result", response_model=dict[str, Any])
def report_wifi_result(payload: DeviceWifiResultIn, db: Session = Depends(get_db),
                       dev: models.Device = Depends(get_current_device)):
    """设备回报一次改 WiFi 的终态（契约见 build-out/_wifi-spec.md §一）。

    鉴权与 ``/api/device/events`` 完全一致（device token，见 deps.get_current_device）：
    这是设备侧接口，家属的 JWT 不该能调，所以不用 get_current_user。

    终态映射只有两条：
      * ``ok=True``  -> ``applied``（新配置已生效）
      * ``ok=False`` -> ``rolled_back``（试连失败，设备已回到原配置，仍然在线）

    两种情况都**立刻清空 wifi_req_pwd**：明文密码留在库里只在这一个窗口内有
    意义（等设备来取），拿到终态后它就成了纯负资产 —— 而且下一次请求一定会
    覆盖它，不清也不会"更安全"，只会让泄露面拖得更长。
    ``wifi_req_ssid`` **不清**：APP 还要靠它显示「已经切换到 xxx」。

    ⚠️ **req_id 与库里不一致时返回 200 但什么都不改。**
    ------------------------------------------------------------------
    为什么不能直接按收到的 req_id 改状态：设备侧的回报是**至少一次**语义
    （断网时进 NVS 队列、联网后重发；sync 请求体里也会带 wifi_result，可能
    与单独 POST 重复）。于是会出现这种顺序：

        设备试连 A 失败 → 回报进队列（没发出去）
        家属看到没动静 → 又提交一次 B（req_id 2）
        队列 flush → 把 A 的"失败"回报补发上来

    若照旧按 ok=False 写 ``rolled_back``，B 这条**还没试过**的请求就被上一次的
    失败结果判死了：APP 显示"已回滚"，设备却可能已经连上 B。旧结果覆盖新请求
    是这类"设备端至少一次 + 服务端有状态"的经典错序，判等 req_id 是最小代价的
    修法。

    为什么返回 200 而不是 4xx：固件把非 2xx 当失败、留在队列里**无限重试**，
    那条过期回报会每轮心跳重发一次，永不收敛（与 /events 丢弃已删提醒回执
    同一处理原则，见那里的注释）。
    """
    if int(payload.req_id) != int(dev.wifi_req_id or 0):
        log.info("丢弃过期的改 WiFi 回报：device=%s 收到 req_id=%s 当前 req_id=%s",
                 dev.id, payload.req_id, dev.wifi_req_id)
        return {"ok": True, "ignored": "stale_req_id"}

    dev.wifi_state = "applied" if payload.ok else "rolled_back"
    # 明文密码到此为止：终态已拿到，不再需要它。
    dev.wifi_req_pwd = ""
    dev.wifi_result_at = utcnow()
    db.commit()

    # reason 只进日志、**不进库也不回给 APP**：它是固件内部错误码/英文，
    # 家属看到"ESP_ERR_WIFI_NOT_CONNECT"没有任何可行动的信息；真正给家属看的
    # 那句话由家属端按 state 派生（见 routers/devices._WIFI_MESSAGES）。
    if not payload.ok:
        log.warning("改 WiFi 失败已回滚：device=%s req_id=%s ssid=%s reason=%s",
                    dev.id, dev.wifi_req_id, payload.ssid or "-",
                    (payload.reason or "-")[:200])
    else:
        log.info("改 WiFi 已生效：device=%s req_id=%s ssid=%s",
                 dev.id, dev.wifi_req_id, payload.ssid or "-")
    return {"ok": True, "state": dev.wifi_state, "req_id": dev.wifi_req_id}


@router.post("/events", response_model=EventOut | dict[str, Any])
def report_event(payload: EventReportIn, db: Session = Depends(get_db),
                 dev: models.Device = Depends(get_current_device)):
    """设备回传一次提醒的触发 / 确认 / 超时结果。

    若该实例服务端未预排（设备离线期间自行排的），这里按
    (reminder_id, due_at) 补建，保证对账不丢。

    规则已被软删除时**整条回执丢弃**，返回 ``{"ok": true,
    "ignored": "reminder_deleted"}``（见下方注释）。

    ⚠️ ``due_at`` 在这里是**设备本地钟点**（固件 ``sync.c`` 用
    ``elder_time_iso_local`` 按自己的 TZ 格式化，不带偏移）。它与
    ``reminder_events.due_at`` 能对上，前提是**设备时区 == 规则时区**。
    两者不一致时的对账（设备在别国、固件 TZ 还是 CST-8）需要固件侧先上报
    自己的时区，属于单独的固件改动；在那之前，这里的行为与改造前完全一致
    （存量设备全部在北京时间下，条件成立）。
    """
    # 软删除（2026-10-30）：这里**故意用 db.get 而不是带 ``deleted_at IS NULL``
    # 的 select** —— "已删除"与"从不存在"必须分开处理，混成一个 None 就分不出来：
    #   * 已删除 ⇒ 静默忽略（见下面 return）；
    #   * 从不存在 ⇒ 保持旧行为，照常补建实例（设备断网时自建的提醒，服务端
    #     可能压根没这条规则，靠补建对账不丢）。
    # 后续只用到它的 ``tz`` 与 ``deleted_at``，不需要排期字段，所以不过滤。
    rule = db.get(models.Reminder, payload.reminder_id)
    if rule is not None and rule.deleted_at is not None:
        # 设备断网期间的回执会进 NVS 队列，联网后 flush_queue() 补发；这期间
        # 规则被软删了。若照旧补建 ReminderEvent，家属就会在历史里看到
        # 「删掉的提醒还显示吃过药」—— 一条假记录，比丢一条回执严重得多。
        #
        # ⚠️ 不能抛 4xx：固件把非 2xx 当失败留在队列里重试，会形成死循环
        # （每条回执每次心跳都重发，永不收敛）。返回 200 + 明确标记。
        log.info("丢弃已删除提醒的回执：device=%s reminder=%s due_at=%s state=%s",
                 dev.id, payload.reminder_id, payload.due_at, payload.state)
        return {"ok": True, "ignored": "reminder_deleted"}
    ev = db.scalar(
        select(ReminderEvent).where(
            ReminderEvent.reminder_id == payload.reminder_id,
            ReminderEvent.due_at == payload.due_at,
        )
    )
    if ev is None:
        ev = ReminderEvent(
            reminder_id=payload.reminder_id,
            device_id=dev.id,
            due_at=payload.due_at,
            # 补建时补上规则时区，否则这条实例的 due_at 就再也没法还原成绝对时刻
            tz=(rule.tz if rule is not None else "") or "",
        )
        db.add(ev)
        db.flush()

    if ev.device_id != dev.id:
        ev.device_id = dev.id

    # 审计要的是"**第一次**变成已确认"这个事件：设备对同一条实例会报两次
    # （先语音、后按键），两次都记的话家属会看到两条「确认提醒」，
    # 而实际只吃了一次药。
    prev_state = ev.state
    ev.state = payload.state
    # 语音确认 = 半置信，按键确认 = 全置信（2026-10-27 用户定义）。
    # 同一条提醒实例设备会报两次：先 method=VOICE（老人说「我吃过了」），
    # 再 method=BUTTON（长按 3 秒复核）。这里把"先语音、后按键"合并成
    # VOICE_AND_BUTTON(5)，而不是让后到的直接覆盖：
    #   * 覆盖 ⇒ 子女端只看到「按键确认」，丢掉"语音也确认过"这个事实，
    #     也看不出老人是"自己说的"还是"被催着按的"；
    #   * 反过来（先按键、后语音）保持 BUTTON：按键已是全置信，不该被降级。
    _method = payload.confirm_method
    if (
        payload.state == int(EventState.CONFIRMED)
        and _method == int(ConfirmMethod.BUTTON)
        and ev.confirm_method == int(ConfirmMethod.VOICE)
    ):
        _method = int(ConfirmMethod.VOICE_AND_BUTTON)
        if not ev.note:
            ev.note = "语音确认后半置信，按键复核"
    ev.confirm_method = _method
    if payload.fired_at:
        ev.fired_at = payload.fired_at
        ev.fired_count = (ev.fired_count or 0) + 1
    if payload.confirmed_at:
        ev.confirmed_at = payload.confirmed_at
    if payload.note:
        ev.note = payload.note
    if payload.state == int(EventState.MISSED):
        ev.escalated = True
    # 设备确认 = 一条审计流水（actor_type=device）。只在**首次**确认时记，
    # 理由见上面 prev_state 的注释。已删除的提醒不记：它的行还在，但
    # 家属已经从列表里删掉了，再往流水里塞"确认"只会让人以为删错了。
    if (payload.state == int(EventState.CONFIRMED)
            and prev_state != int(EventState.CONFIRMED)
            and rule is not None):
        reminder_audit.log(
            db, reminder=rule, action="confirm", actor_type="device",
            actor_name=(dev.name or "设备")[:64], via="device",
            after={"event_id": int(ev.id) if ev.id else None,
                   "due_at": ev.due_at, "confirm_method": _method},
        )
    db.commit()
    db.refresh(ev)
    return ev


@router.post("/reminders/local", response_model=LocalReminderReportOut)
def report_local_reminders(
    payload: list[LocalReminderIn],
    db: Session = Depends(get_db),
    dev: models.Device = Depends(get_current_device),
):
    """设备把断网期间本地自建的提醒补报上来，返回服务端分配的 id。

    背景（医疗事故级）：老人在断网时用离线命令词/按键建的服药提醒，
    只写在设备 NVS（server_id == 0），服务端完全不知道。如果不补报，
    这些提醒在子女端永远不可见 —— 老人以为子女能看到服药记录，实际看不到。

    幂等：按 (device_id, title, times_of_day) 查重。设备在「已入库但响应
    丢失」时会重发同一批，靠这个避免建出重复提醒（重复提醒=老人被反复打扰，
    同样不可接受）。这里刻意不用 client_key 落库，而是用业务字段比对，
    这样即使设备端 client_key 生成逻辑变了也不会造出重复数据。
    """
    acks: list[dict[str, Any]] = []

    for item in payload:
        # ⚠️ 本端点**没有** once_date 字段（协议里就没有，设备侧也只上报
        # schedule_kind/times_of_day/weekdays）。所以设备报一条
        # ``schedule_kind = 0``（ONCE）过来，落库必然是「一次性 + 无日期」
        # —— 那个组合服务端算不出任何触发时刻，这条规则**永远不会响**，
        # 而子女端看到的是一条正常的"指定日期一次"。
        #
        # 这里**改成每天**而不是拒绝：拒绝等于把老人断网时自建的提醒**丢掉**，
        # 正是本端点存在的意义（见 docstring「医疗事故级」）。口径与
        # routers/reminders._reject_once_without_date（入参 422）和固件
        # elder_rmd_sched.c（once_ymd==0 按每天）一致：能救的救，不静默丢。
        kind = int(item.schedule_kind)
        if kind == int(models.ScheduleKind.ONCE):
            log.warning("设备上报的本地提醒 %s 标成一次性但没有日期（协议无此字段），"
                        "按每天落库", item.client_key)
            kind = int(models.ScheduleKind.DAILY)

        # 幂等去重（设备在响应丢失时会重发同一批）。
        #
        # ⚠️ 软删除（2026-10-30）让这里多了一个必须明确的分支：
        #   * **只按活着的行去重**（``deleted_at IS NULL``）—— 已删的提醒
        #     不该再被当成"已经建过了"；
        #   * 但若命中的是一条**已删除**的行，必须**ack 它的 id 且不新建**
        #     （见下面第二个查询）。否则设备补报一次，就等于把用户刚删掉的
        #     提醒**复活成一条新行**，服务端还会为它 materialize 出 PENDING
        #     实例、随心跳下发 —— 那正是"删了还在响"。
        #     而 ack 一个已删除的 id 是安全的：``/sync`` 的规则列表已经过滤
        #     掉它，设备下次心跳就会把本地这条清掉（自愈，且不会新建实例）。
        _dedupe = (
            models.Reminder.device_id == dev.id,
            models.Reminder.title == item.title,
            models.Reminder.times_of_day == item.times_of_day,
            models.Reminder.rtype == item.rtype,
            models.Reminder.schedule_kind == kind,
        )
        existing = db.scalar(
            select(models.Reminder).where(*_dedupe, models.Reminder.deleted_at.is_(None)))
        if existing is not None:
            acks.append({"client_key": item.client_key, "reminder_id": existing.id})
            continue
        gone = db.scalar(
            select(models.Reminder).where(*_dedupe,
                                          models.Reminder.deleted_at.is_not(None)))
        if gone is not None:
            log.info("设备补报的本地提醒 %s 与服务端已删除的 #%s 相同，只 ack 不新建"
                     "（新建等于复活已删提醒）", item.client_key, gone.id)
            acks.append({"client_key": item.client_key, "reminder_id": gone.id})
            continue

        row = models.Reminder(
            device_id=dev.id,
            creator_id=None,
            title=item.title,
            speak_text=item.speak_text or item.title,
            rtype=item.rtype,
            schedule_kind=kind,
            times_of_day=list(item.times_of_day),
            weekdays=list(item.weekdays),
            require_confirm=True,
            # 允许语音 + 按键：断网时老人只有这两条确认路径
            confirm_methods=list(item.confirm_methods or ["voice", "button"]),
            enabled=item.enabled,
        )
        db.add(row)
        db.flush()   # 拿到自增 id 后回给设备
        acks.append({"client_key": item.client_key, "reminder_id": row.id})

    db.commit()
    dev.last_seen_at = utcnow()
    dev.online = True
    return {"acks": acks}


@router.post("/interactions", status_code=status.HTTP_201_CREATED)
def report_interaction(payload: InteractionReportIn, db: Session = Depends(get_db),
                       dev: models.Device = Depends(get_current_device)):
    """上报一次交互（唤醒 / 对话 / 确认 / 超时 / 求助），用于统计。"""
    row = Interaction(
        device_id=dev.id,
        kind=payload.kind,
        ts=payload.ts or utcnow(),
        duration_ms=payload.duration_ms,
        meta=payload.meta or {},
    )
    db.add(row)
    dev.last_seen_at = utcnow()
    dev.online = True
    db.commit()
    return {"ok": True}


@router.post("/activation-code")
def request_activation_code(db: Session = Depends(get_db),
                            dev: models.Device = Depends(get_current_device)):
    """设备主动申请一枚激活码用于屏显。

    ⚠️ 2026-10-20 语义变更：本接口要 ``device_token`` 才能调，而设备 token
    只在**绑定成功那一刻**签发（见 ``voice.ota_check`` 的已绑定分支）⇒ 能走到
    这里的设备**必然已经绑定**。按用户要求「已绑定设备在解绑前不再下发激活码」，
    这里直接如实拒绝，而不是照旧发一枚新码。

    未绑定设备的取码路径是 ``GET/POST /api/voice/ota``（不需要任何凭证，
    身份就是 ``Device-Id`` 头），固件每 15s 走的就是那条。

    为什么不做成「已绑定也发，只是不生效」：发出去就意味着有人能读到屏幕、
    抄下来、再撞一次绑定接口 —— 那正是 10 分钟 TTL 要压窄的窗口，不该自己开口子。
    """
    if dev.owner_id is not None:
        return {"ok": False, "reason": "already-bound",
                "message": "设备已绑定账户，不再下发激活码；如需换绑请先解绑"}
    if not dev.device_id:
        return {"ok": False, "reason": "missing-device-id",
                "message": "设备缺少 MAC，无法申领激活码"}

    # 延迟 import：voice 顶层会 import 本模块（_voice_endpoints），顶层互引会成环。
    from ..security import format_code
    from ..voice import _ensure_activation_code

    row = _ensure_activation_code(db, str(dev.device_id))
    expires_at = row.expires_at
    if expires_at.tzinfo is None:      # SQLite 读回 naive，按 UTC 解释
        from datetime import timezone as _tz
        expires_at = expires_at.replace(tzinfo=_tz.utc)
    return {
        "ok": True,
        "code": row.code,
        "display": format_code(row.code),
        "expires_in_sec": max(0, int((expires_at - utcnow()).total_seconds())),
        "payload": _qr_payload_hint(dev.device_id),
    }


def _qr_payload_hint(device_id: str) -> dict[str, Any]:
    """给 APP 扫码用的载荷提示（实际二维码由端侧生成）。"""
    return {"scheme": "elder-companion://bind", "device_id": device_id}
