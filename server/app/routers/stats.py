"""交互次数统计：子女端查看老人今天和设备聊了多少次、吃药是否按时。"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import get_current_user
from ..models import EventState, InteractionKind
from ..schedule import device_zone
from ..schemas import StatsOverview, StatsPoint

router = APIRouter(prefix="/api/stats", tags=["stats"])

_KIND_KEYS = {
    int(InteractionKind.WAKE): "wake",
    int(InteractionKind.DIALOG): "dialog",
    int(InteractionKind.REMINDER_CONFIRM): "confirm",
    int(InteractionKind.REMINDER_MISS): "miss",
    int(InteractionKind.SOS): "sos",
}


@router.get("/overview", response_model=StatsOverview)
def overview(device_pk: int, days: int = Query(7, ge=1, le=60),
             db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    from ..deps import require_device_owner

    dev = require_device_owner(device_pk, user, db)
    # 「今天」按**设备所在时区**算（2026-10-21 跨时区改造）：老人早上 7 点吃
    # 了药，在美西那就是北京时间前一天 —— 用服务端时区分桶会让"今日已确认"
    # 在半个地球的用户那里永远是 0。存量设备（规则 tz 为空）行为不变。
    zone = device_zone(db, device_pk)
    now = datetime.now(zone)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # --- 交互次数按天聚合 ---
    # ⚠️ **分桶必须按服务端本地时区，而不是 UTC**。这条曾是个真实缺陷：
    # ``Interaction.ts`` 存的是 UTC，而 ``today_start`` 是本地时区的零点，
    # 两者在 SQL 里被直接比较；而 ``func.date(ts)`` 在 SQLite 上取的是 ts 字面量
    # 的日期部分，也就是 **UTC 日期**。于是本地 00:00~08:00 之间
    # （UTC 还是前一天）刚上报的交互会被归到"昨天"的桶里，
    # 而查找用的 key 是本地日期 —— 两者对不上，today_total 恒为 0。
    # 症状是"早上八点前看统计永远是 0，八点后自己就好了"，极难定位。
    #
    # 修法：把 ts 先换算到本地时区再取日期。SQLite 没有原生时区函数，
    # 统一在 Python 侧做—— 这里数据量是单设备的交互条数（每天几十条），
    # 全量取回再分桶的开销可以忽略，换来的是 SQLite/MySQL 行为一致。
    since = today_start.astimezone(models.utcnow().tzinfo) - timedelta(days=days - 1)
    raw_rows = db.execute(
        select(models.Interaction.ts, models.Interaction.kind)
        .where(models.Interaction.device_id == device_pk,
               models.Interaction.ts >= since)
    ).all()

    rows: list[tuple[str, int, int]] = []
    for ts, kind in raw_rows:
        if ts is None:
            continue
        # ⚠️ SQLite 的 DateTime 列读回来是 **naive** 的（时区信息被丢掉），
        # 而它存的其实是 UTC。所以这里必须先补回 UTC 时区再换算 ——
        # 直接对 naive 值调 astimezone()，Python 会拿**进程本地时区**去理解它，
        # 于是同一个值在 UTC 机器与 UTC+8 机器上会算出不同的"本地日期"。
        aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
        local_day = aware.astimezone(now.tzinfo).date()
        rows.append((str(local_day), int(kind), 1))

    per_day: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "wake": 0, "dialog": 0, "confirm": 0, "miss": 0, "sos": 0}
    )
    for day, kind, cnt in rows:
        key = str(day)
        bucket = per_day[key]
        bucket["total"] += cnt
        bucket[_KIND_KEYS.get(int(kind), "dialog")] += cnt

    series: list[StatsPoint] = []
    for i in range(days):
        d = (today_start - timedelta(days=days - 1 - i)).date()
        b = per_day.get(str(d), {"total": 0, "wake": 0, "dialog": 0, "confirm": 0, "miss": 0, "sos": 0})
        series.append(StatsPoint(date=str(d), **b))

    today_bucket = per_day.get(str(today_start.date()),
                               {"total": 0, "wake": 0, "dialog": 0, "confirm": 0, "miss": 0, "sos": 0})

    total_7d = sum(p.total for p in series)

    # --- 提醒对账 ---
    # 软删除（2026-10-30）：待处理数**必须**排除已删除的提醒。
    # 删除只清了 PENDING 实例，已经 FIRED 的那条还留着（"那天确实响过"），
    # 不过滤的话家属会看到一个永远清不掉的「待处理 1 条」——而列表里根本
    # 没有这条提醒可处理，是个死胡同。
    pending = db.scalar(
        select(func.count(models.ReminderEvent.id))
        .join(models.Reminder, models.Reminder.id == models.ReminderEvent.reminder_id)
        .where(
            models.ReminderEvent.device_id == device_pk,
            models.ReminderEvent.state.in_([int(EventState.PENDING), int(EventState.FIRED)]),
            models.Reminder.deleted_at.is_(None),
        )
    ) or 0
    # ⚠️ ``missed_today`` **刻意不过滤**已删除的提醒：它是"今天真的错过了一次"
    # 的历史事实，删掉提醒不该把已经发生过的漏服从统计里抹掉 —— 那等于
    # 让家属永远发现不了"老爷子删了提醒、也真的漏了药"。
    missed_today = db.scalar(
        select(func.count(models.ReminderEvent.id)).where(
            models.ReminderEvent.device_id == device_pk,
            models.ReminderEvent.state == int(EventState.MISSED),
            # due_at 是**设备时区的裸钟点**，所以比较对象也要取同一个基准的
            # 钟点（去掉 tzinfo），否则 SQL 里是拿"纽约钟点"比"纽约零点的
            # aware 值"，SQLite 绑定时只取字面量 —— 看起来能跑，其实靠巧合。
            models.ReminderEvent.due_at >= today_start.replace(tzinfo=None),
        )
    ) or 0

    return StatsOverview(
        device_id=dev.device_id,
        device_name=dev.name,
        online=dev.online,
        last_seen_at=dev.last_seen_at,
        today_total=today_bucket["total"],
        today_dialog=today_bucket["dialog"],
        today_confirm=today_bucket["confirm"],
        today_miss=today_bucket["miss"],
        total_7d=total_7d,
        pending_reminders=pending,
        missed_today=missed_today,
        series=series,
    )


@router.get("/recent", response_model=list[dict])
def recent(device_pk: int, limit: int = Query(20, ge=1, le=200),
           db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """最近若干条交互明细（含 ASR 文本等 meta）。"""
    from ..deps import require_device_owner

    require_device_owner(device_pk, user, db)
    rows = db.scalars(
        select(models.Interaction)
        .where(models.Interaction.device_id == device_pk)
        .order_by(models.Interaction.ts.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": r.id,
            "kind": _KIND_KEYS.get(r.kind, "dialog"),
            # 同 home.py：手写 isoformat 必须补 UTC 偏移，否则前端按本地
            # 时区猜，非东八区手机整体差 8 小时。
            "ts": models.as_utc(r.ts).isoformat(),
            "duration_ms": r.duration_ms,
            "meta": r.meta or {},
        }
        for r in rows
    ]
