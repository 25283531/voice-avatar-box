"""交互次数统计：子女端查看老人今天和设备聊了多少次、吃药是否按时。"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import get_current_user
from ..models import EventState, InteractionKind
from ..schedule import local_now
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
    now = local_now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # --- 交互次数按天聚合 ---
    since = today_start - timedelta(days=days - 1)
    rows = db.execute(
        select(
            func.date(models.Interaction.ts),
            models.Interaction.kind,
            func.count(models.Interaction.id),
        )
        .where(models.Interaction.device_id == device_pk,
               models.Interaction.ts >= since)
        .group_by(func.date(models.Interaction.ts), models.Interaction.kind)
    ).all()

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
    pending = db.scalar(
        select(func.count(models.ReminderEvent.id)).where(
            models.ReminderEvent.device_id == device_pk,
            models.ReminderEvent.state.in_([int(EventState.PENDING), int(EventState.FIRED)]),
        )
    ) or 0
    missed_today = db.scalar(
        select(func.count(models.ReminderEvent.id)).where(
            models.ReminderEvent.device_id == device_pk,
            models.ReminderEvent.state == int(EventState.MISSED),
            models.ReminderEvent.due_at >= today_start,
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
            "ts": r.ts.isoformat(),
            "duration_ms": r.duration_ms,
            "meta": r.meta or {},
        }
        for r in rows
    ]
