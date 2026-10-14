"""后台定时任务：排期补全 + 超时升级。

- 排期补全：为所有设备预生成未来 N 小时的提醒实例；
- 超时升级：到点后超过 escalate_after_min 仍未确认 -> 标记 MISSED 并置 escalated，
  设备下次心跳会看到该状态，从而保持红灯常亮（需求 11）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import SCHEDULER_INTERVAL_SEC, SCHEDULE_AHEAD_HOURS
from .db import SessionLocal
from .models import Device, EventState, Reminder, ReminderEvent
from .schedule import local_now, materialize_events

log = logging.getLogger("elder.scheduler")


def _tick(session: Session) -> None:
    now = local_now()

    # 1) 排期补全
    devices = session.scalars(select(Device.id)).all()
    for dev_id in devices:
        try:
            materialize_events(session, dev_id, hours=SCHEDULE_AHEAD_HOURS)
        except Exception as exc:  # noqa: BLE001
            log.warning("materialize failed dev=%s: %s", dev_id, exc)
            session.rollback()

    # 2) 超时升级
    overdue = session.scalars(
        select(ReminderEvent).where(
            ReminderEvent.state.in_([int(EventState.PENDING), int(EventState.FIRED)])
        )
    ).all()
    for ev in overdue:
        rule = session.get(Reminder, ev.reminder_id)
        if not rule or not rule.require_confirm:
            continue
        deadline = ev.due_at + timedelta(minutes=max(1, rule.escalate_after_min))
        if now > deadline:
            ev.state = int(EventState.MISSED)
            ev.escalated = True
            ev.note = ev.note or "超时未确认"
    session.commit()


async def run_forever() -> None:
    """每 SCHEDULER_INTERVAL_SEC 秒跑一轮，异常不中断。"""
    log.info("scheduler started, interval=%ss", SCHEDULER_INTERVAL_SEC)
    while True:
        await asyncio.sleep(SCHEDULER_INTERVAL_SEC)
        try:
            with SessionLocal() as s:
                await asyncio.to_thread(_tick, s)
        except Exception as exc:  # noqa: BLE001
            log.exception("scheduler tick failed: %s", exc)
