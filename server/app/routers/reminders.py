"""提醒规则 CRUD + 排期实例 + 设备回执。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import geo, models
from ..db import get_db
from ..deps import get_current_user, require_device_owner
from ..models import EventState
from ..schedule import (TZ_MARGIN, event_due_utc, event_zone, materialize_events,
                        next_occurrence, occurrences_on, reminder_zone)
from ..schemas import EventOut, ReminderIn, ReminderOut

router = APIRouter(prefix="/api/reminders", tags=["reminders"])


def _with_next(db: Session, r: models.Reminder) -> ReminderOut:
    out = ReminderOut.model_validate(r)
    out.next_fire_at = next_occurrence(r)
    return out


def _reminder_tz(request, dev: models.Device, explicit: str = "") -> str:
    """这条规则该按哪个时区解释 ``times_of_day``。

    **优先级（顺序即答案，不要"综合判断"）**：

    1. 请求里显式给的 ``tz``（前端知道自己在哪个时区时最准）；
    2. **设备所在时区** —— 由设备上一次心跳的 IP 判定（``geo.resolve``）。
       为什么设备排在创建者前面：提醒是**从设备上响出来的**，"早上 8 点吃药"
       指的是老人眼前那个钟的 8 点。家属在纽约用手机给国内老人建提醒，
       按家属时区会算成国内的晚上 8 点 —— 这是服药提醒，错 12 小时是安全问题；
    3. 创建者所在时区（请求 IP 判定）—— 设备还没上报过 IP 时的兜底；
    4. ``config.TZ``。

    ⚠️ 返回的一定是**合法 IANA 名或空串**，空串表示"用 config.TZ"。
    不返回非法值：``Reminder.tz`` 是客户端可控输入，落一条坏值会让排期线程
    在后台抛 ``ZoneInfoNotFoundError``，而那个异常被 ``run_forever`` 吞掉，
    表现是"这条规则永远不排期"。
    """
    given = geo.valid_timezone(explicit)
    if given:
        return given
    dev_tz = geo.device_timezone(dev)
    if dev_tz:
        return dev_tz
    if request is not None:
        try:
            return geo.resolve_request(request).timezone
        except Exception:  # noqa: BLE001
            pass
    return ""


@router.post("", response_model=ReminderOut, status_code=status.HTTP_201_CREATED)
def create_reminder(device_pk: int, payload: ReminderIn, request: Request,
                    db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    require_device_owner(device_pk, user, db)
    dev = db.get(models.Device, device_pk)
    data = payload.model_dump()
    data["tz"] = _reminder_tz(request, dev, data.get("tz", ""))
    r = models.Reminder(device_id=device_pk, creator_id=user.id, **data)
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
    """未来 N 小时内待触发的实例（APP 与设备共用）。

    ⚠️ ``due_at`` 是**规则时区的裸钟点**，跨时区之后 SQL 里的范围条件不再
    是"绝对时刻的范围"。做法：SQL 按 ±:data:`schedule.TZ_MARGIN` 放宽粗筛
    （永远覆盖得住任何时区），再在 Python 里按绝对时刻精确过滤/排序。
    直接丢掉粗筛会让每条设备把全部历史实例拉进内存。
    """
    require_device_owner(device_pk, user, db)
    materialize_events(db, device_pk, hours=hours)

    now = datetime.now(timezone.utc)
    lo, hi = now - timedelta(minutes=5), now + timedelta(hours=hours)
    wall = now.replace(tzinfo=None)
    rows = db.scalars(
        select(models.ReminderEvent)
        .where(models.ReminderEvent.device_id == device_pk,
               models.ReminderEvent.due_at >= wall - TZ_MARGIN - timedelta(minutes=5),
               models.ReminderEvent.due_at <= wall + timedelta(hours=hours) + TZ_MARGIN)
        .order_by(models.ReminderEvent.due_at)
    ).all()
    keep = [e for e in rows if lo <= event_due_utc(e) <= hi]
    keep.sort(key=event_due_utc)
    return keep


@router.get("/{reminder_pk}", response_model=ReminderOut)
def get_reminder(reminder_pk: int, db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    r = db.get(models.Reminder, reminder_pk)
    if not r:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提醒不存在")
    require_device_owner(r.device_id, user, db)
    return _with_next(db, r)


@router.patch("/{reminder_pk}", response_model=ReminderOut)
def update_reminder(reminder_pk: int, payload: ReminderIn, request: Request,
                    db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    r = db.get(models.Reminder, reminder_pk)
    if not r:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提醒不存在")
    require_device_owner(r.device_id, user, db)

    data = payload.model_dump()
    # 未显式给 tz 时**保留原值**，不重新判定：一条规则建好之后的时区不该
    # 因为"家属这次在另一个国家改了个标题"而漂移 —— 那会把老人已经习惯的
    # 服药时间整体平移。要改时区必须显式传。
    data["tz"] = geo.valid_timezone(data.get("tz", "")) or r.tz or ""
    for k, v in data.items():
        setattr(r, k, v)
    # updated_at 是 **UTC** 类字段（不是 due_at 那种墙上时间）。
    # 以前这里写的是 local_now()（北京时间），落进 SQLite 后被当作 UTC 序列化，
    # 于是"最后修改时间"稳定地比真实时间晚 8 小时 —— 属于本轮要消灭的
    # 「几个节点时区不统一」的一部分。
    r.updated_at = models.utcnow()
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
            # 时区必须跟着规则一起落：due_at 是"规则时区的裸钟点"，
            # 少了 tz 这一列就再也分不清是北京的 8 点还是纽约的 8 点。
            tz=r.tz or "",
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
    是最容易被投诉的一类问题。补建用的时刻按**规则自己的时区**推算
    （见 schedule 模块头部；以前是"服务端时区"，跨时区之后就不成立了）。

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

    # 「当天」与「到点」都必须在规则的时区里判：拿北京时间的 now 去比一条
    # 纽约规则的 08:00，会把"已经吃过的那一次"判成"还没到点"。
    now = datetime.now(reminder_zone(r))
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
    # 与 due_at 同一个钟点基准（规则时区的墙上时间），否则同一条实例里
    # due_at 是纽约钟点、confirmed_at 是北京钟点，页面上会显示"确认时间晚 12 小时"。
    ev.confirmed_at = datetime.now(event_zone(ev))
    ev.note = "子女端代确认"
    db.commit()
    db.refresh(ev)
    return ev
