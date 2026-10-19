"""设备侧接口：心跳同步、提醒回执、交互上报。

固件每 N 分钟调一次 /api/device/sync，一次拿全：
服务器时间（校时）、提醒规则与排期、当前形象、AI 供应商、WS/OTA 地址。
离线期间设备按本地 RTC 与已下发规则自行响铃，联网后把回执补齐。
"""
from __future__ import annotations

from datetime import timedelta
from datetime import timezone as dt_timezone
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import (
    PUBLIC_BASE_URL,
    SCHEDULE_AHEAD_HOURS,
    VOICE_OTA_PATH,
    VOICE_WS_PATH,
    WS_PUBLIC_BASE_URL,
)
from ..db import get_db
from ..deps import get_current_device
from ..llm_gateway import resolve_row
from ..chat_quota import quota_snapshot
from ..model_catalog import DEVICE_KINDS, KIND_LLM
from ..models import EventState, Interaction, ReminderEvent, utcnow
from ..schedule import local_now, materialize_events, next_occurrence
from ..schemas import (
    AvatarOut,
    DeviceQuotaOut,
    DeviceSyncOut,
    EventOut,
    EventReportIn,
    InteractionReportIn,
    LocalReminderIn,
    LocalReminderReportOut,
    ProviderSafeOut,
    ReminderOut,
)

router = APIRouter(prefix="/api/device", tags=["device"])


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
                                      models.Reminder.enabled.is_(True))
    ).all()
    now = local_now()
    upcoming = list(db.scalars(
        select(ReminderEvent)
        .where(ReminderEvent.device_id == dev.id,
               ReminderEvent.state == int(EventState.PENDING),
               ReminderEvent.due_at >= now - timedelta(minutes=10),
               ReminderEvent.due_at <= now + timedelta(hours=SCHEDULE_AHEAD_HOURS))
        .order_by(ReminderEvent.due_at)
        .limit(200)
    ).all())

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
    # 统一按 UTC 排序键排：窗口查出来的行 due_at 是 SQLite 回读的「墙上时间」(naive)，
    # 刚 db.add 的对象还是 schedule 生成的 aware 值，直接排序会抛
    # TypeError: can't compare offset-naive and offset-aware datetimes。
    upcoming.sort(key=lambda e: e.due_at.replace(tzinfo=dt_timezone.utc))

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
    )


@router.post("/events", response_model=EventOut)
def report_event(payload: EventReportIn, db: Session = Depends(get_db),
                 dev: models.Device = Depends(get_current_device)):
    """设备回传一次提醒的触发 / 确认 / 超时结果。

    若该实例服务端未预排（设备离线期间自行排的），这里按
    (reminder_id, due_at) 补建，保证对账不丢。
    """
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
        )
        db.add(ev)
        db.flush()

    if ev.device_id != dev.id:
        ev.device_id = dev.id

    ev.state = payload.state
    ev.confirm_method = payload.confirm_method
    if payload.fired_at:
        ev.fired_at = payload.fired_at
        ev.fired_count = (ev.fired_count or 0) + 1
    if payload.confirmed_at:
        ev.confirmed_at = payload.confirmed_at
    if payload.note:
        ev.note = payload.note
    if payload.state == int(EventState.MISSED):
        ev.escalated = True
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
        existing = db.scalar(
            select(models.Reminder).where(
                models.Reminder.device_id == dev.id,
                models.Reminder.title == item.title,
                models.Reminder.times_of_day == item.times_of_day,
                models.Reminder.rtype == item.rtype,
                models.Reminder.schedule_kind == item.schedule_kind,
            )
        )
        if existing is not None:
            acks.append({"client_key": item.client_key, "reminder_id": existing.id})
            continue

        row = models.Reminder(
            device_id=dev.id,
            creator_id=None,
            title=item.title,
            speak_text=item.speak_text or item.title,
            rtype=item.rtype,
            schedule_kind=item.schedule_kind,
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
    """设备主动申请一枚激活码用于屏显（APP 未发起时也能自助绑定）。"""
    from ..security import code_expires_at, format_code, generate_activation_code

    for _ in range(8):
        code = generate_activation_code()
        clash = db.scalar(select(models.ActivationCode).where(models.ActivationCode.code == code))
        if not clash:
            break
    else:
        return {"ok": False, "reason": "generate-failed"}

    row = models.ActivationCode(code=code, device_id=dev.device_id, expires_at=code_expires_at())
    db.add(row)
    db.commit()
    return {
        "ok": True,
        "code": code,
        "display": format_code(code),
        "expires_in_sec": int((row.expires_at - utcnow()).total_seconds()),
        "payload": _qr_payload_hint(dev.device_id),
    }


def _qr_payload_hint(device_id: str) -> dict[str, Any]:
    """给 APP 扫码用的载荷提示（实际二维码由端侧生成）。"""
    return {"scheme": "elder-companion://bind", "device_id": device_id}
