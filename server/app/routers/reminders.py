"""提醒规则 CRUD + 排期实例 + 设备回执。"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import get_current_user, require_device_owner
from ..models import EventState
from ..schedule import local_now, materialize_events, next_occurrence, occurrences_on
from ..schemas import EventOut, ReminderIn, ReminderOut

router = APIRouter(prefix="/api/reminders", tags=["reminders"])


def _with_next(db: Session, r: models.Reminder) -> ReminderOut:
    out = ReminderOut.model_validate(r)
    out.next_fire_at = next_occurrence(r)
    return out


@router.post("", response_model=ReminderOut, status_code=status.HTTP_201_CREATED)
def create_reminder(device_pk: int, payload: ReminderIn, db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    require_device_owner(device_pk, user, db)
    r = models.Reminder(device_id=device_pk, creator_id=user.id, **payload.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    # 立即为该规则排期，保证设备下次心跳就能拉到
    materialize_events(db, device_pk)
    return _with_next(db, r)


@router.get("", response_model=list[ReminderOut])
def list_reminders(device_pk: int | None = None, db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    q = select(models.Reminder)
    if device_pk is not None:
        require_device_owner(device_pk, user, db)
        q = q.where(models.Reminder.device_id == device_pk)
    elif not user.is_admin:
        # 非管理员只看自己名下设备的规则
        q = q.join(models.Device).where(models.Device.owner_id == user.id)
    rows = db.scalars(q.order_by(models.Reminder.id.desc())).all()
    return [_with_next(db, r) for r in rows]


@router.get("/upcoming", response_model=list[EventOut])
def upcoming(device_pk: int, hours: int = 24, db: Session = Depends(get_db),
             user: models.User = Depends(get_current_user)):
    """未来 N 小时内待触发的实例（APP 与设备共用）。"""
    require_device_owner(device_pk, user, db)
    materialize_events(db, device_pk, hours=hours)
    until = local_now() + timedelta(hours=hours)
    rows = db.scalars(
        select(models.ReminderEvent)
        .where(models.ReminderEvent.device_id == device_pk,
               models.ReminderEvent.due_at >= local_now() - timedelta(minutes=5),
               models.ReminderEvent.due_at <= until)
        .order_by(models.ReminderEvent.due_at)
    ).all()
    return list(rows)


@router.get("/{reminder_pk}", response_model=ReminderOut)
def get_reminder(reminder_pk: int, db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    r = db.get(models.Reminder, reminder_pk)
    if not r:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提醒不存在")
    require_device_owner(r.device_id, user, db)
    return _with_next(db, r)


@router.patch("/{reminder_pk}", response_model=ReminderOut)
def update_reminder(reminder_pk: int, payload: ReminderIn, db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    r = db.get(models.Reminder, reminder_pk)
    if not r:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提醒不存在")
    require_device_owner(r.device_id, user, db)

    for k, v in payload.model_dump().items():
        setattr(r, k, v)
    r.updated_at = local_now()
    db.commit()
    db.refresh(r)

    # 规则变了，清掉尚未发生的排期实例，按新规则重排
    pending = db.scalars(
        select(models.ReminderEvent).where(
            models.ReminderEvent.reminder_id == r.id,
            models.ReminderEvent.state == int(EventState.PENDING),
        )
    ).all()
    for e in pending:
        db.delete(e)
    db.commit()
    materialize_events(db, r.device_id)
    return _with_next(db, r)


@router.post("/{reminder_pk}/toggle", response_model=ReminderOut)
def toggle_reminder(reminder_pk: int, db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    """暂停 / 恢复（老人住院、外出时常用）。"""
    r = db.get(models.Reminder, reminder_pk)
    if not r:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提醒不存在")
    require_device_owner(r.device_id, user, db)
    r.enabled = not r.enabled
    db.commit()
    db.refresh(r)
    return _with_next(db, r)


@router.delete("/{reminder_pk}", status_code=status.HTTP_204_NO_CONTENT)
def delete_reminder(reminder_pk: int, db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    r = db.get(models.Reminder, reminder_pk)
    if not r:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提醒不存在")
    require_device_owner(r.device_id, user, db)
    db.delete(r)
    db.commit()


def _get_or_create_event(db: Session, r: models.Reminder, due: datetime) -> models.ReminderEvent:
    """取（或补建）某条规则在某个时刻的实例。

    实例表只预生成未来 SCHEDULE_AHEAD_HOURS 小时，当天早些时候的服药点
    可能压根没落库；而 (reminder_id, due_at) 上有唯一约束 uq_reminder_due，
    materialize_events() 也是先查再插，所以这里按同样的键去重补建一条，
    不会和调度器后面生成出来的实例撞成两条。
    """
    from ..models import ReminderEvent

    ev = db.scalar(
        select(ReminderEvent).where(
            ReminderEvent.reminder_id == r.id, ReminderEvent.due_at == due
        )
    )
    if ev is None:
        ev = ReminderEvent(
            reminder_id=r.id,
            device_id=r.device_id,
            due_at=due,
            state=int(EventState.PENDING),
        )
        db.add(ev)
        db.flush()
    return ev


@router.post("/{reminder_pk}/confirm", response_model=EventOut)
def confirm_now(reminder_pk: int, db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    """子女在 APP 上代老人确认当前待办（ConfirmMethod.APP）。

    实例表没预排到当天早些时候的服药点时，这里按规则**现场补一条**再确认，
    而不是直接 404 —— 老人吃过药了只是服务端还没排到，家属在 APP 上却点不动，
    是最容易被投诉的一类问题。补建用的时刻按服务端时区 TZ 推算（见 schedule.occurrences_on）。

    选中哪一次的顺序：
      1. 当天「已经到点」的服药点里最晚的那一次（老人通常就是刚吃完这一次）；
      2. 当天还没到点的服药点里最早的那一次（提前代确认）；
      3. 以上都不成立（规则今天不触发 / 今天都确认完了）才退回原来的
         「最早一条待确认实例」，再没有就 404。
    为什么不能直接取「最早一条待确认实例」：排期是按未来 36 小时生成的，
    当天早上的那次没排到时，最早的实例其实是**明天**的，确认了明天那次之后
    首页的「已确认次数」纹丝不动，看起来就像点了没反应。
    """
    from ..models import ConfirmMethod

    r = db.get(models.Reminder, reminder_pk)
    if not r:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提醒不存在")
    require_device_owner(r.device_id, user, db)

    now = local_now()
    today_dues = occurrences_on(r, now.date())
    ev = None
    if today_dues:
        past = [ts for ts in today_dues if ts <= now]
        # 已到点的按倒序（最近一次优先），未到点的按正序（最早一次优先）
        for due in past[::-1] + [ts for ts in today_dues if ts > now]:
            row = db.scalar(
                select(models.ReminderEvent).where(
                    models.ReminderEvent.reminder_id == r.id,
                    models.ReminderEvent.due_at == due,
                )
            )
            if row is None:
                # 排期还没生成到这一次：现场补一条（去重键与调度器一致）
                ev = _get_or_create_event(db, r, due)
                break
            if row.state not in (int(EventState.CONFIRMED), int(EventState.SKIPPED)):
                ev = row
                break
            # 这一次已经确认 / 跳过过了，继续看下一个服药点

    if ev is None:
        # 规则今天不触发，或今天都确认完了：退回原来的行为，确认最早的一条例外实例
        ev = db.scalars(
            select(models.ReminderEvent)
            .where(models.ReminderEvent.reminder_id == r.id,
                   models.ReminderEvent.state.in_([int(EventState.PENDING), int(EventState.FIRED)]))
            .order_by(models.ReminderEvent.due_at)
            .limit(1)
        ).first()
    if ev is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "当前没有待确认的提醒")

    ev.state = int(EventState.CONFIRMED)
    ev.confirm_method = int(ConfirmMethod.APP)
    ev.confirmed_at = local_now()
    ev.note = "子女端代确认"
    db.commit()
    db.refresh(ev)
    return ev
