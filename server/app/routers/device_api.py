"""设备侧接口：心跳同步、提醒回执、交互上报。

固件每 N 分钟调一次 /api/device/sync，一次拿全：
服务器时间（校时）、提醒规则与排期、当前形象、AI 供应商、WS/OTA 地址。
离线期间设备按本地 RTC 与已下发规则自行响铃，联网后把回执补齐。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import SCHEDULE_AHEAD_HOURS, XIAOZHI_OTA_URL, XIAOZHI_WS_URL
from ..db import get_db
from ..deps import get_current_device
from ..membership import quota_snapshot
from ..models import EventState, Interaction, ReminderEvent, utcnow
from ..schedule import local_now, materialize_events
from ..schemas import (
    AvatarOut,
    DeviceQuotaOut,
    DeviceSyncOut,
    EventOut,
    EventReportIn,
    InteractionReportIn,
    ProviderSafeOut,
    ReminderOut,
)

router = APIRouter(prefix="/api/device", tags=["device"])


@router.post("/sync", response_model=DeviceSyncOut)
def sync(fw_version: str = "", db: Session = Depends(get_db),
         dev: models.Device = Depends(get_current_device)):
    """心跳 + 拉全量配置。"""
    dev.online = True
    dev.last_seen_at = utcnow()
    if fw_version:
        dev.fw_version = fw_version
    db.commit()

    # 为该设备排期（幂等）
    materialize_events(db, dev.id, hours=SCHEDULE_AHEAD_HOURS)

    rules = db.scalars(
        select(models.Reminder).where(models.Reminder.device_id == dev.id,
                                      models.Reminder.enabled.is_(True))
    ).all()
    now = local_now()
    upcoming = db.scalars(
        select(ReminderEvent)
        .where(ReminderEvent.device_id == dev.id,
               ReminderEvent.state == int(EventState.PENDING),
               ReminderEvent.due_at >= now - timedelta(minutes=10),
               ReminderEvent.due_at <= now + timedelta(hours=SCHEDULE_AHEAD_HOURS))
        .order_by(ReminderEvent.due_at)
        .limit(200)
    ).all()

    avatar = db.get(models.Avatar, dev.avatar_id) if dev.avatar_id else None
    # 只下发真正的平台配置：ai_providers 里的 agent 角色（kind 为空）没有厂商与模型，
    # 下发给设备会被当成一条可用的 LLM 配置，反而把真实配置顶掉。
    providers = db.scalars(
        select(models.AIProvider)
        .where(models.AIProvider.enabled.is_(True),
               models.AIProvider.kind.in_(("asr", "llm", "tts", "vad")))
        .order_by(models.AIProvider.kind, models.AIProvider.priority)
    ).all()

    db.refresh(dev)
    # 规划文档 7.3：心跳时顺带下发当日陪聊配额（已用/上限/空闲切断/服药关键词）
    quota = DeviceQuotaOut(**quota_snapshot(db, dev.owner_id, device_ids=[dev.id]))
    return DeviceSyncOut(
        server_time=now,
        device=dev,
        avatar=AvatarOut.model_validate(avatar) if avatar else None,
        reminders=[ReminderOut.model_validate(r) for r in rules],
        upcoming=[EventOut.model_validate(e) for e in upcoming],
        providers=[ProviderSafeOut.model_validate(p) for p in providers],
        ws_url=XIAOZHI_WS_URL,
        ota_url=XIAOZHI_OTA_URL,
        quota=quota,
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
