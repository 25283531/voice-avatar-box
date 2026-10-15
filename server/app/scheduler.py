"""后台定时任务：排期补全 + 超时升级 + 会员订单/会员过期清理。

- 排期补全：为所有设备预生成未来 N 小时的提醒实例；
- 超时升级：到点后超过 escalate_after_min 仍未确认 -> 标记 MISSED 并置 escalated，
  设备下次心跳会看到该状态，从而保持红灯常亮（需求 11）；
- 订单过期：超过 48 小时仍为 pending / notified 的会员订单置 expired，
  释放其占用的 4 位订单号与随机金额（见 app/membership.py 的占用规则）；
- 会员过期：把已到期的会员记录标记为 expired，便于后台筛查。
- 离线清扫：超过在线窗口（5 分钟，见 device_state.ONLINE_WINDOW）没有心跳的设备置 offline，
  否则 devices.online 只增不减，设备断电后仍会显示在线。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import SCHEDULER_INTERVAL_SEC, SCHEDULE_AHEAD_HOURS, TZ
from .db import SessionLocal
from .device_state import mark_offline_devices
from .membership import expire_memberships, expire_stale_orders
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
        # due_at 是 schedule 按服务端时区 TZ 生成的，落库时带时区，
        # 但 SQLite 读回来会把时区抹掉变成 naive —— 直接和 now（aware）比较会抛
        # `TypeError: can't compare offset-naive and offset-aware datetimes`，
        # 异常会顺着冒到 run_forever，把这一轮后面的所有步骤（含订单过期、
        # 离线清扫）一起带走。这里按 TZ 把时区补回去再比较。
        due = ev.due_at if ev.due_at.tzinfo else ev.due_at.replace(tzinfo=TZ)
        deadline = due + timedelta(minutes=max(1, rule.escalate_after_min))
        if now > deadline:
            ev.state = int(EventState.MISSED)
            ev.escalated = True
            ev.note = ev.note or "超时未确认"
    session.commit()

    # 3) 会员订单过期：释放订单号与金额占用
    try:
        n = expire_stale_orders(session)
        if n:
            log.info("expired %d stale membership orders", n)
    except Exception as exc:  # noqa: BLE001
        log.warning("expire orders failed: %s", exc)
        session.rollback()

    # 4) 会员到期标记
    try:
        expire_memberships(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("expire memberships failed: %s", exc)
        session.rollback()

    # 5) 离线清扫：devices.online 只在设备上报时被置 True，
    #    设备断电 / 换 Wi-Fi 之后不会再上报，不清扫就会「永远在线」。
    #    阈值与首页展示共用 device_state.ONLINE_WINDOW（5 分钟，一个心跳周期 + 余量）。
    try:
        n = mark_offline_devices(session)
        if n:
            log.info("marked %d device(s) offline", n)
    except Exception as exc:  # noqa: BLE001
        log.warning("mark offline failed: %s", exc)
        session.rollback()


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
