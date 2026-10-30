"""提醒规则展开：把「复杂重复逻辑」翻译成具体的时间点列表。

这是整个提醒系统的核心。设备端拿到的是**已经展开好的时间点**，
因此端侧逻辑可以做得极薄（只需要在 RTC 到点时响铃），
复杂语义变更不需要改固件。

## 时区（2026-10-21 用户定调）

``times_of_day`` 里的 ``"08:00"`` **不是绝对时刻**，必须挂在一个时区上。
以前这里全用 ``config.TZ``（北京），于是海外的家庭设"早上 8 点吃药"会在
北京时间 8 点响 —— 那里可能正是半夜。

现在**每条规则自带时区**（``Reminder.tz``），展开时以它为准：

* ``r.tz`` 非空       → 用它的钟点；
* ``r.tz`` 空（历史数据）→ ``config.TZ``。**不做数据迁移**：
  存量规则当初就是按北京时间建的，标成北京才是如实记录，也才不会让
  升级本身变成一次"所有提醒平移 8 小时"的事故。

⚠️ 这里有一个**必须集中**的地方：``TZ`` 只允许出现在 :func:`reminder_zone`
一处。散着写（像以前那样 6 处 ``astimezone(TZ)``）时，谁少改一处就变成
"日期边界用北京、钟点用当地"，那种 bug 表现为"某些提醒在月初/月末漏一天"，
几乎不可能靠读日志发现。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy.orm import Session

from .config import TZ
from .geo import valid_timezone
from .models import Reminder, ScheduleKind

from zoneinfo import ZoneInfo


def reminder_zone(r: Reminder) -> ZoneInfo:
    """这条规则该按哪个时区解释钟点。

    ``r.tz`` 是**客户端可控输入**（创建时由请求推导），所以必须过
    :func:`geo.valid_timezone` 白名单校验 —— 否则一个 ``tz="Bad/Zone"``
    就能让排期线程在后台抛 ``ZoneInfoNotFoundError``，而那个异常在
    ``run_forever()`` 里被吞掉，表现是"这条规则永远不排期"。
    非法值一律退回 ``config.TZ``，并且**不报错**：一条坏规则不该拖垮
    这台设备其它正常规则的排期。
    """
    name = valid_timezone(getattr(r, "tz", ""))
    return ZoneInfo(name) if name else TZ


def zone_of(tz: str) -> ZoneInfo:
    """给"还没有规则行"的场合用（新建前算 next_fire_at）。"""
    name = valid_timezone(tz)
    return ZoneInfo(name) if name else TZ


#: 全球时区相对 UTC 的最大跨度（UTC-12 ~ UTC+14 = 26 小时）。
#:
#: 用途：``reminder_events.due_at`` 存的是**规则时区的裸钟点**，而 SQL 里的范围
#: 查询拿它跟"服务端此刻的钟点"比 —— 只有两者同一时区时这个比较才成立。
#: 混时区之后不能再让 SQL 做最终裁决，改为「SQL 先按 ±14h 放宽粗筛，Python
#: 精确过滤」。放宽 14h 而不是 26h：往两侧各放宽 14h 已覆盖任何单个时区与
#: 服务端时区的最大差（+14 与 -12 之间最坏 26h，但单侧最多 14h）。
TZ_MARGIN = timedelta(hours=14)


def event_zone(ev: object) -> ZoneInfo:
    """一条实例该按哪个时区解读 ``due_at``。"""
    name = valid_timezone(getattr(ev, "tz", ""))
    return ZoneInfo(name) if name else TZ


def event_instant(due_at: datetime, tz: str = "") -> datetime:
    """把「规则时区的裸钟点」还原成**绝对时刻**（aware，UTC）。

    这是全仓唯一该做这件事的地方。散着写必然出现「某处补了 TZ、某处没补」，
    而症状是「提醒到点不响 / 提前几小时响」—— 对服药提醒是安全问题，
    却没有任何异常可看（比较只是悄悄地错了）。
    """
    zone = zone_of(tz)
    if due_at.tzinfo is not None:
        return due_at.astimezone(timezone.utc)
    return due_at.replace(tzinfo=zone).astimezone(timezone.utc)


def event_due_utc(ev: object) -> datetime:
    """同 :func:`event_instant`，入参是整行对象（读它的 ``due_at`` 与 ``tz``）。"""
    return event_instant(getattr(ev, "due_at"), getattr(ev, "tz", ""))


def device_zone(db: Session, device_pk: int) -> ZoneInfo:
    """这台设备"生活在"哪个时区（用于按自然日聚合的统计）。

    判据是它**名下规则**的时区：规则由 ``_reminder_tz`` 按设备 IP 判定后落库，
    是这台设备地理位置最可靠的代理。没有规则时退回 ``config.TZ`` ——
    一台刚绑定、还没建过提醒的设备，除了服务端默认值也确实没有别的线索。

    为什么不直接在 ``devices`` 表上加一列时区：那会变成**两处真源**
    （心跳写的设备时区 vs 规则里的时区），两者漂移时没有任何症状，
    只是"统计的今天"和"提醒的今天"悄悄差了几天。
    """
    row = db.query(Reminder.tz).filter(
        Reminder.device_id == device_pk,
        Reminder.tz.isnot(None),
        Reminder.tz != "",
    ).first()
    name = valid_timezone(row[0]) if row and row[0] else ""
    return ZoneInfo(name) if name else TZ


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
        # 基准日必须是**规则所在时区**的今天：跨时区时"今天"可能差一天，
        # 用服务端时区算会让隔天类规则整体错一天。
        base = start or datetime.now(reminder_zone(r)).date()
        step = max(1, int(r.interval_days or 1))
        return ((d - base).days % step) == 0 and d >= base

    return False


def _last_day_of_month(d: date) -> int:
    nxt = d.replace(day=28) + timedelta(days=4)
    return (nxt - timedelta(days=nxt.day)).day


def occurrences_on(r: Reminder, d: date) -> list[datetime]:
    """返回规则 r 在日期 d 上的全部触发时刻（**规则时区的** aware 时刻）。"""
    if not r.enabled:
        return []
    if not rule_matches_date(r, d):
        return []

    zone = reminder_zone(r)
    out: list[datetime] = []
    for t in r.times_of_day or []:
        parsed = parse_hhmm(t)
        if not parsed:
            continue
        out.append(datetime.combine(d, time(parsed[0], parsed[1]), tzinfo=zone))
    return sorted(out)


def expand_occurrences(r: Reminder, since: datetime, until: datetime) -> list[datetime]:
    """展开 [since, until) 区间内规则 r 的全部触发时刻。"""
    if not r.enabled or since >= until:
        return []

    zone = reminder_zone(r)
    start_day = _parse_date(r.start_date)
    end_day = _parse_date(r.end_date)
    first_day = max(since.astimezone(zone).date(), start_day) if start_day else since.astimezone(zone).date()
    last_day = min(until.astimezone(zone).date(), end_day) if end_day else until.astimezone(zone).date()

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
    """返回规则 r 在 after 之后的第一个触发时刻（**规则时区的** aware 时刻）。"""
    zone = reminder_zone(r)
    base = (after or datetime.now(zone)).astimezone(zone)
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
    # 起点用**规则自己时区**的"现在"没有意义（要覆盖所有规则、可能跨时区），
    # 所以这里仍以服务端时区的现在为轴，往前多留 1 小时吸收时区差 ——
    # 否则一条 +14 区的规则可能刚好落在窗口左边界外，表现为"刚建的规则没排上"。
    start = since or datetime.now(TZ) - timedelta(hours=1)
    end = start + timedelta(hours=hours)

    rules = (
        db.query(Reminder)
        .filter(Reminder.device_id == device_pk, Reminder.enabled.is_(True),
                # 软删除（2026-10-30）：已删除的规则**不许**再排期。
                # 这是"删了还在响"最根本的一道闸 —— 只要这里漏了，删除接口
                # 之后的任何一次心跳都会把实例重新排出来并下发给设备。
                Reminder.deleted_at.is_(None))
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
                # 时区跟着规则走（不 JOIN）：这一列是 due_at 的**解读方式**，
                # 漏了它 due_at 就是个无法还原的裸钟点。
                tz=r.tz or "",
                state=int(EventState.PENDING),
            ))
            created += 1
    if created:
        db.commit()
    return created


def clear_pending_events(db: Session, reminder_pk: int) -> int:
    """删掉某条提醒**尚未触发**的排期实例，返回删除条数（不 commit）。

    三个调用点都必须用它，别各写一遍：删除提醒（web/app）、语音删除、
    修改规则（重排前清空）。漏一处的后果不是"统计不准"，而是：

    1. ``reminder_events`` 上有 ``UniqueConstraint(reminder_id, due_at)``，
       残留的 PENDING 会占住唯一键 —— 以后「恢复」时 ``materialize_events``
       插不进去，表现为"恢复了但永远不响"；
    2. ``/api/device/sync`` 会把 PENDING 实例下发给设备 ⇒ **删了还在响**。
       服药提醒重复播报 = 让老人重复服药，这是安全级问题。

    只删 PENDING：已触发/已确认的实例是**历史事实**，删了家属就再也看不到
    "那天到底吃没吃药"。
    """
    from .models import EventState, ReminderEvent

    pending = db.query(ReminderEvent).filter(
        ReminderEvent.reminder_id == reminder_pk,
        ReminderEvent.state == int(EventState.PENDING)).all()
    for e in pending:
        db.delete(e)
    return len(pending)
