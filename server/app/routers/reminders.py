"""提醒规则 CRUD + 排期实例 + 设备回执。"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import get_current_user, require_device_owner
from ..models import EventState
from ..schedule import local_now, materialize_events, next_occurrence
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


@router.post("/{reminder_pk}/confirm", response_model=EventOut)
def confirm_now(reminder_pk: int, db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    """子女在 APP 上代老人确认当前待办（ConfirmMethod.APP）。"""
    from ..models import ConfirmMethod

    r = db.get(models.Reminder, reminder_pk)
    if not r:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提醒不存在")
    require_device_owner(r.device_id, user, db)

    ev = db.scalars(
        select(models.ReminderEvent)
        .where(models.ReminderEvent.reminder_id == r.id,
               models.ReminderEvent.state.in_([int(EventState.PENDING), int(EventState.FIRED)]))
        .order_by(models.ReminderEvent.due_at)
        .limit(1)
    ).first()
    if not ev:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "当前没有待确认的提醒")

    ev.state = int(EventState.CONFIRMED)
    ev.confirm_method = int(ConfirmMethod.APP)
    ev.confirmed_at = local_now()
    ev.note = "子女端代确认"
    db.commit()
    db.refresh(ev)
    return ev
