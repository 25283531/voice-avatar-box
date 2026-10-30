"""提醒规则 CRUD + 排期实例 + 设备回执。

⚠️ 软删除（2026-10-30）之后，**每一个读 Reminder 的查询都必须自己加
``deleted_at IS NULL``** —— SQLAlchemy 不会自动加。本文件统一走
:func:`_get_alive` / ``_alive_filter()``，不要在别处再手写一遍
（漏一处 = "删了还在响"，见 models.Reminder 的注释）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import geo, models, reminder_audit
from ..db import get_db
from ..deps import get_current_user, require_device_owner
from ..models import EventState
from ..schedule import (TZ_MARGIN, clear_pending_events, event_due_utc,
                        event_zone, materialize_events, next_occurrence,
                        occurrences_on, reminder_zone)
from ..schemas import EventOut, ReminderAuditOut, ReminderIn, ReminderOut

router = APIRouter(prefix="/api/reminders", tags=["reminders"])


# ---------------------------------------------------------------- 软删除
def _alive_filter():
    """「未删除」的 SQL 条件。**所有**读 Reminder 的查询都要带上它。"""
    return models.Reminder.deleted_at.is_(None)


def _get_alive(db: Session, reminder_pk: int) -> models.Reminder:
    """按 id 取一条**未删除**的提醒；不存在或已删都按"不存在"处理（404）。

    为什么统一用带 where 的 select 而不是 ``db.get()``
    ------------------------------------------------
    ``db.get()`` 只能按主键取，加不了条件；先 get 再判 ``is_alive()`` 的写法
    每处都要记得判，漏一处就是「已删除的提醒还能被 PATCH / 还能被确认」。
    而且它把"已删"与"不存在"分成两条代码路径，容易一边 404、一边 403
    （后者会泄露"这个 id 曾经存在"）。
    """
    r = db.scalar(select(models.Reminder).where(
        models.Reminder.id == reminder_pk, _alive_filter()))
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提醒不存在")
    return r


def _get_deleted(db: Session, reminder_pk: int) -> models.Reminder:
    """按 id 取一条**已删除**的提醒（只有 restore 用）。"""
    r = db.scalar(select(models.Reminder).where(
        models.Reminder.id == reminder_pk,
        models.Reminder.deleted_at.is_not(None)))
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有找到已删除的提醒")
    return r


def client_via(request: Request) -> str:
    """客户端自报来源端。前端在请求头里带 ``X-Client: web|app``。

    为什么要客户端自报而不是服务端猜：网页端与 APP 端打的是同一套 API，
    User-Agent 里既有浏览器也有 WebView，靠 UA 判端必然误判（APP 内嵌页
    与浏览器页面 UA 几乎一样）。而"从哪个端删的"是家属之间互相追责时
    最有用的一列，宁可让前端显式声明。

    非法值一律回落 ``web``：这是个**展示**字段，不该因为它拼错就 400
    —— 那会让一次正常的删除失败。
    """
    raw = (request.headers.get("x-client") or "").strip().lower()
    return raw if raw in reminder_audit.VIA_VALUES else "web"


def _with_next(db: Session, r: models.Reminder) -> ReminderOut:
    out = ReminderOut.model_validate(r)
    out.next_fire_at = next_occurrence(r)
    # is_deleted 是派生值（不是列）：统一在这里推导，避免每个接口各算一遍。
    out.is_deleted = r.deleted_at is not None
    return out


def _clear_pending_events(db: Session, reminder_pk: int) -> int:
    """删掉这条提醒**尚未触发**的排期实例（实现见 ``schedule.clear_pending_events``）。

    为什么实现在 schedule 而不是这里：语音链路（``voice/tools.py``）的
    「语音删除提醒」也要做同一件事，而 voice 模块不该 import routers
    （分层反了，且会引入一串无关的 FastAPI 依赖）。
    """
    return clear_pending_events(db, reminder_pk)


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


def _reject_once_without_date(payload: ReminderIn) -> None:
    """一次性提醒必须带日期，否则 422。

    为什么这条校验在 **router 层**，而不是 ``ReminderIn`` 的
    ``model_validator``
    ---------------------------------------------------------------
    这是一个**只该拦入参、不该拦出参**的约束。而 ``ReminderOut(ReminderIn)``
    继承了 ``ReminderIn`` 的全部校验器，``_with_next()`` 里的
    ``ReminderOut.model_validate(r)`` 会把库里的行**再校验一遍**。
    于是同一个校验器会在两条完全不同的路径上生效：

    * 入参（POST/PATCH）：拦的是「用户刚提交的、还没落库的数据」—— 正确；
    * 出参（任何读接口 + 设备下发同步）：拦的是「**已经躺在库里**的行」——
      一条历史坏行会让 ``GET /api/reminders`` 和 ``/api/device/sync`` 直接
      抛异常 → 500 → **设备一条提醒都同步不到**，故障面从"一条规则不响"
      扩大到"整台设备静音"。

    换句话说：把校验挂在 model 上，等于让「修复坏数据」的手段（读取、编辑）
    本身也被坏数据卡死 —— 这是典型的"校验器选错层"。

    ⚠️ 所以**不要**把这段逻辑搬进 ``schemas.ReminderIn``。库里的坏行由
    :func:`app.schema_patch.backfill_once_without_date` 一次性修掉，固件侧另有
    ``elder_rmd_sched.c`` 的兜底 —— 三方各管一个面，见那两处注释。

    判据与回填、固件保持一致：``schedule_kind == ONCE`` 且 ``once_date``
    去空白后为空。``once_date`` 允许 ``None`` / ``''`` 两种写法，都要拦。
    """
    if int(payload.schedule_kind) != int(models.ScheduleKind.ONCE):
        return
    if (payload.once_date or "").strip():
        return
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "一次性提醒必须填写日期（once_date）",
    )


@router.post("", response_model=ReminderOut, status_code=status.HTTP_201_CREATED)
def create_reminder(device_pk: int, payload: ReminderIn, request: Request,
                    db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    require_device_owner(device_pk, user, db)
    _reject_once_without_date(payload)
    dev = db.get(models.Device, device_pk)
    data = payload.model_dump()
    data["tz"] = _reminder_tz(request, dev, data.get("tz", ""))
    r = models.Reminder(device_id=device_pk, creator_id=user.id, **data)
    db.add(r)
    # flush 而不是 commit：审计行要 reminder_id，而流水必须与业务变更
    # **同一个事务**提交（分开提交就可能出现"提醒建了、流水丢了"）。
    db.flush()
    reminder_audit.log(db, reminder=r, action="create", actor=user,
                       via=client_via(request),
                       after=reminder_audit.snapshot(r))
    db.commit()
    db.refresh(r)
    # 立即为该规则排期，保证设备下次心跳就能拉到
    materialize_events(db, device_pk)
    return _with_next(db, r)


@router.get("", response_model=list[ReminderOut])
def list_reminders(device_pk: int | None = None, include_deleted: bool = False,
                   db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    """提醒列表。默认**只返回未删除的**。

    ``include_deleted=true`` 时把已删除的一并带出（``is_deleted=true``）：
    这是用户要的「删了也能看到历史」在列表侧的落点，家属可以据此发现
    "老爷子把降压药提醒删了"并一键恢复。
    """
    q = select(models.Reminder)
    if not include_deleted:
        q = q.where(_alive_filter())
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


@router.get("/audits", response_model=list[ReminderAuditOut])
def list_audits(device_pk: int,
                reminder_id: int | None = None,
                limit: int = Query(100, ge=1, le=500),
                offset: int = Query(0, ge=0),
                db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    """某台设备的提醒操作流水（新→旧）。

    ⚠️ **路由顺序**：本函数必须定义在 ``/{reminder_pk}`` **之前**。
    FastAPI 按声明顺序匹配，放后面时 ``GET /api/reminders/audits`` 会先命中
    ``/{reminder_pk}``，把 ``"audits"`` 当成 int 解析 → 422，且报错信息
    （"value is not a valid integer"）完全指不到真正的原因。
    ``/upcoming`` 也是同样的道理，所以两者都放在前面。

    ``can_restore`` 是**当前**状态：同一条 delete 流水在恢复之后会变成 false
    （按钮要反映现状，而不是"当时能不能恢复"）。
    """
    require_device_owner(device_pk, user, db)
    q = select(models.ReminderAudit).where(
        models.ReminderAudit.device_id == device_pk)
    if reminder_id is not None:
        q = q.where(models.ReminderAudit.reminder_id == reminder_id)
    rows = db.scalars(
        q.order_by(models.ReminderAudit.created_at.desc(),
                   models.ReminderAudit.id.desc())
        .limit(limit).offset(offset)
    ).all()
    if not rows:
        return []

    # 一次查询算出"这些流水涉及的提醒里，哪些现在是已删除状态"。
    # 逐行查会变成 N+1（一页 100 条 = 100 次查询）。
    ids = {int(r.reminder_id) for r in rows}
    deleted_ids = set(db.scalars(
        select(models.Reminder.id).where(
            models.Reminder.id.in_(ids),
            models.Reminder.deleted_at.is_not(None))
    ).all())
    return [reminder_audit.to_out(r, can_restore=int(r.reminder_id) in deleted_ids)
            for r in rows]


@router.get("/{reminder_pk}", response_model=ReminderOut)
def get_reminder(reminder_pk: int, db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    r = _get_alive(db, reminder_pk)
    require_device_owner(r.device_id, user, db)
    return _with_next(db, r)


@router.patch("/{reminder_pk}", response_model=ReminderOut)
def update_reminder(reminder_pk: int, payload: ReminderIn, request: Request,
                    db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    r = _get_alive(db, reminder_pk)
    require_device_owner(r.device_id, user, db)
    # 与 create 同一条约束：把一条规则改成「一次性」却不给日期，等于让它
    # 永远不触发（见 _reject_once_without_date 的说明）。
    _reject_once_without_date(payload)

    # 快照必须在**改动之前**取：改完之后再 snapshot 拿到的是新值，
    # before/after 会变成两份一样的 JSON（流水看起来"什么都没改"）。
    before = reminder_audit.snapshot(r)
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
    # 与业务变更同一个事务（见 reminder_audit 模块头部）。
    reminder_audit.log(db, reminder=r, action="update", actor=user,
                       via=client_via(request),
                       before=before, after=reminder_audit.snapshot(r))
    db.commit()
    db.refresh(r)

    # 规则变了，清掉尚未发生的排期实例，按新规则重排
    _clear_pending_events(db, r.id)
    db.commit()
    materialize_events(db, r.device_id)
    return _with_next(db, r)


@router.post("/{reminder_pk}/toggle", response_model=ReminderOut)
def toggle_reminder(reminder_pk: int, request: Request,
                    db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    """暂停 / 恢复（老人住院、外出时常用）。"""
    r = _get_alive(db, reminder_pk)
    require_device_owner(r.device_id, user, db)
    was_enabled = bool(r.enabled)
    r.enabled = not r.enabled
    reminder_audit.log(
        db, reminder=r,
        action="toggle_on" if r.enabled else "toggle_off",
        actor=user, via=client_via(request),
        before={"enabled": was_enabled}, after={"enabled": bool(r.enabled)},
    )
    db.commit()
    db.refresh(r)
    return _with_next(db, r)


@router.delete("/{reminder_pk}", status_code=status.HTTP_204_NO_CONTENT)
def delete_reminder(reminder_pk: int, request: Request,
                    db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    """软删除：行留在库里（家属要看历史），但对外表现成"不存在"。

    幂等口径：对已删除的再删一次返回 **404** —— 因为 ``_get_alive`` 已经
    把它当成不存在了。这比返回 204 更好：前端拿到 404 会刷新列表，
    而不是以为自己又删了一遍。

    ⚠️ 软删除**必须同时置 ``enabled = False``**，但**不要**把它理解成"老固件
    的兜底" —— 这个说法一度写在这里，是错的，2026-10-30 已更正：

    下发查询是 ``Reminder.enabled.is_(True)``，所以「置 ``enabled=False``」和
    「查询时过滤 ``deleted_at``」对**下发 payload 的效果完全相同**，都只是让
    这条规则从数组里消失。而老固件 ``sync.c`` 的 ``do_sync()`` 短路条件是
    **数组为空**（``count > 0`` 才整表替换）：

      * 还有别的启用规则 → 数组非空 → 老固件整表替换 → 被删那条消失 ✅
      * **删掉的是最后一条** → 数组为空 → 老固件短路、保留本地旧表 → 继续
        每天响 ❌

    第二种情况下服务端**无论怎么改都救不了**，只能等设备 OTA 到含 ``a407119``
    的固件（那一版把判据从"解析出几条"改成 ``cJSON_IsArray``，空数组也整表
    替换）。别以为置了 false 就万事大吉。

    那为什么还要置 false —— **第二道防线**，两个理由：
      ① ``schedule.materialize_events`` 等查询按 ``enabled`` 过滤，与
         ``deleted_at`` 互为备份，以后谁改错一个，另一个还挡得住；
      ② 一条已删除的提醒本来就不该是"启用"状态，留 ``True`` 是自相矛盾的
         数据，读库的人会困惑。
    """
    r = _get_alive(db, reminder_pk)
    require_device_owner(r.device_id, user, db)
    # 快照必须在**改任何字段之前**取：before_json 要保留删除前的 enabled
    # 原值，restore 靠它还原（先置 false 再取快照 ⇒ 恢复后永远是暂停态）。
    before = reminder_audit.snapshot(r)
    r.deleted_at = models.utcnow()
    r.deleted_by = user.id
    r.deleted_via = client_via(request)
    r.enabled = False            # 第二道防线，**不是**老固件兜底，理由见 docstring
    reminder_audit.log(db, reminder=r, action="delete", actor=user,
                       via=r.deleted_via, before=before)
    # 必须与软删**同一个事务**：如果先提交删除再清实例，中途失败就会留下
    # "提醒已删、实例还在"的状态 ⇒ 设备继续按 PENDING 播报（删了还在响）。
    _clear_pending_events(db, r.id)
    db.commit()


@router.post("/{reminder_pk}/restore", response_model=ReminderOut)
def restore_reminder(reminder_pk: int, request: Request,
                     db: Session = Depends(get_db),
                     user: models.User = Depends(get_current_user)):
    """把一条软删除的提醒恢复（家属发现"老爷子把降压药提醒删了"时用）。

    ``enabled`` 还原成**删除前**的值（读最近一条 delete 流水的 before_json）：
    删除时把 enabled 置了 false（第二道防线，见 ``delete_reminder``），无条件
    恢复成 True 会把「先暂停、再删除」的意图弄丢
    （见 ``reminder_audit.enabled_before_delete``）。
    """
    r = _get_deleted(db, reminder_pk)
    require_device_owner(r.device_id, user, db)
    prev_enabled = reminder_audit.enabled_before_delete(db, r.id)
    r.enabled = True if prev_enabled is None else prev_enabled
    r.deleted_at = None
    r.deleted_by = None
    r.deleted_via = ""
    r.updated_at = models.utcnow()
    reminder_audit.log(db, reminder=r, action="restore", actor=user,
                       via=client_via(request),
                       before={"enabled": False},
                       after=reminder_audit.snapshot(r))
    db.commit()
    db.refresh(r)
    # 重新排期：删除时把 PENDING 实例清光了，不重排的话这条规则"活着但不响"。
    materialize_events(db, r.device_id)
    return _with_next(db, r)


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
def confirm_now(reminder_pk: int, request: Request,
                db: Session = Depends(get_db),
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

    r = _get_alive(db, reminder_pk)
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
    reminder_audit.log(db, reminder=r, action="confirm", actor=user,
                       via=client_via(request),
                       after={"event_id": int(ev.id) if ev.id else None,
                              "due_at": ev.due_at, "state": int(ev.state)})
    db.commit()
    db.refresh(ev)
    return ev
