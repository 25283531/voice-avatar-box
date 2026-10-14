"""提醒规则展开：把「复杂重复逻辑」翻译成具体的时间点列表。

这是整个提醒系统的核心。设备端拿到的是**已经展开好的时间点**，
因此端侧逻辑可以做得极薄（只需要在 RTC 到点时响铃），
复杂语义变更不需要改固件。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from .config import TZ
from .models import Reminder, ScheduleKind, utcnow


def parse_hhmm(value: str) -> tuple[int, int] | None:
    """解析 "08:30" -> (8, 30)，非法返回 None。"""
    try:
        hh, mm = value.strip().split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:  # noqa: BLE001
        return None
    return None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        return None


def local_now() -> datetime:
    return datetime.now(TZ)


def rule_matches_date(r: Reminder, d: date) -> bool:
    """判断规则 r 在日期 d 是否需要触发。"""
    start = _parse_date(r.start_date)
    end = _parse_date(r.end_date)
    if start and d < start:
        return False
    if end and d > end:
        return False

    kind = r.schedule_kind
    if kind == ScheduleKind.ONCE:
        once = _parse_date(r.once_date)
        return once is not None and d == once

    if kind == ScheduleKind.DAILY:
        return True

    if kind == ScheduleKind.WEEKLY:
        weekdays = r.weekdays or []
        return d.isoweekday() in weekdays   # 1=周一 ... 7=周日

    if kind == ScheduleKind.MONTHLY:
        days = r.month_days or []
        # 处理 31 号在 30 天月份不存在的情形：落到当月最后一天
        return d.day in days or (31 in days and d.day == _last_day_of_month(d))

    if kind == ScheduleKind.INTERVAL_DAYS:
        base = start or (local_now().date())
        step = max(1, int(r.interval_days or 1))
        return ((d - base).days % step) == 0 and d >= base

    return False


def _last_day_of_month(d: date) -> int:
    nxt = d.replace(day=28) + timedelta(days=4)
    return (nxt - timedelta(days=nxt.day)).day


def occurrences_on(r: Reminder, d: date) -> list[datetime]:
    """返回规则 r 在日期 d 上的全部触发时刻（本地时区 aware）。"""
    if not r.enabled:
        return []
    if not rule_matches_date(r, d):
        return []

    out: list[datetime] = []
    for t in r.times_of_day or []:
        parsed = parse_hhmm(t)
        if not parsed:
            continue
        out.append(datetime.combine(d, time(parsed[0], parsed[1]), tzinfo=TZ))
    return sorted(out)


def expand_occurrences(r: Reminder, since: datetime, until: datetime) -> list[datetime]:
    """展开 [since, until) 区间内规则 r 的全部触发时刻。"""
    if not r.enabled or since >= until:
        return []

    start_day = _parse_date(r.start_date)
    end_day = _parse_date(r.end_date)
    first_day = max(since.astimezone(TZ).date(), start_day) if start_day else since.astimezone(TZ).date()
    last_day = min(until.astimezone(TZ).date(), end_day) if end_day else until.astimezone(TZ).date()

    out: list[datetime] = []
    d = first_day
    # 安全上限：避免错误规则导致死循环
    guard = 0
    while d <= last_day and guard < 400:
        for ts in occurrences_on(r, d):
            if since <= ts < until:
                out.append(ts)
        d += timedelta(days=1)
        guard += 1
    return out


def next_occurrence(r: Reminder, after: datetime | None = None) -> datetime | None:
    """返回规则 r 在 after 之后的第一个触发时刻。"""
    base = (after or local_now()).astimezone(TZ)
    horizon = base + timedelta(days=370)
    d = base.date()
    guard = 0
    while d <= horizon.date() and guard < 400:
        for ts in occurrences_on(r, d):
            if ts > base:
                return ts
        d += timedelta(days=1)
        guard += 1
    return None


def materialize_events(db: Session, device_pk: int, since: datetime | None = None,
                       hours: int | None = None) -> int:
    """为某台设备的所有启用规则预生成 ReminderEvent（幂等）。

    返回新增实例数量。设备离线时自身也会按规则响铃，联网后对账。
    """
    from .models import EventState, ReminderEvent

    hours = hours or 36
    start = since or local_now() - timedelta(hours=1)
    end = start + timedelta(hours=hours)

    rules = (
        db.query(Reminder)
        .filter(Reminder.device_id == device_pk, Reminder.enabled.is_(True))
        .all()
    )
    created = 0
    for r in rules:
        for ts in expand_occurrences(r, start, end):
            exists = (
                db.query(ReminderEvent)
                .filter(ReminderEvent.reminder_id == r.id, ReminderEvent.due_at == ts)
                .first()
            )
            if exists:
                continue
            db.add(ReminderEvent(
                reminder_id=r.id,
                device_id=device_pk,
                due_at=ts,
                state=int(EventState.PENDING),
            ))
            created += 1
    if created:
        db.commit()
    return created
