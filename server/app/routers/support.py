"""工单（用户通过邮箱提交问题）。

分两组：
  * ``router``        /api/support/tickets     —— 用户端：提交问题（可匿名）、看自己提交过的
  * ``admin_router``  /api/admin/support/tickets —— 管理端：列表 / 改状态 / 写回复

用户全程在 APP 内操作：提交 -> 服务端落库 + 给平台邮箱发通知 -> 管理员在管理端
回复 -> 回复邮件通知提交者。没有任何网页端表单参与。

当前**刻意不做**的三件事（现阶段一问一答够用，别为了「完整」提前上）：
  * 附件上传（要引入文件存储与类型/大小校验）；
  * 分页游标（只有 limit，工单量在这个量级下翻页足够）；
  * 多轮追问（需要把 admin_reply 拆成回复流表，当前是单条回复覆盖式）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import mailer
from .. import models
from ..db import get_db
from ..deps import bearer, get_current_admin, get_current_user
from ..schemas import TicketCreateIn, TicketOut, TicketPatchIn
from ..security import decode_token

log = logging.getLogger("elder.support")

# ============================================================ 用户端
router = APIRouter(prefix="/api/support/tickets", tags=["support"])

# ============================================================ 管理端
admin_router = APIRouter(
    prefix="/api/admin/support/tickets", tags=["admin-support"],
    dependencies=[Depends(get_current_admin)],
)

# 与会员订单列表一致的默认条数上限
_LIST_LIMIT = Query(100, ge=1, le=500)


# ---------------------------------------------------------------- 可选鉴权
def optional_user(
    cred=Depends(bearer),
    db: Session = Depends(get_db),
) -> models.User | None:
    """「登录了就用登录身份，没登录就当匿名」的可选鉴权。

    工单必须允许匿名提交：忘记密码的用户根本登不进 APP，如果强制带凭证，
    「找回密码」这条路断了就再也没有兜底入口 —— 而找回密码恰恰是最容易
    需要求助的场景。这里刻意不复用 get_current_user（它缺凭证会直接 401）。
    凭证无效时按匿名处理而不是报错：APP 的登录态过期后仍应能把问题发出去。
    """
    if cred is None:
        return None
    payload = decode_token(cred.credentials)
    if not payload or payload.get("typ") != "user":
        return None
    return db.get(models.User, int(payload["sub"]))


# ---------------------------------------------------------------- 序列化
def _ticket_out(db: Session, t: models.SupportTicket) -> TicketOut:
    user = db.get(models.User, t.user_id) if t.user_id else None
    return TicketOut(
        id=t.id,
        user_id=t.user_id,
        email=t.email,
        title=t.title,
        content=t.content,
        category=t.category,
        category_text=models.TicketCategory.TEXTS.get(t.category, models.TicketCategory.OTHER),
        device_id=t.device_id,
        status=t.status,
        status_text=models.TicketStatus.TEXTS.get(t.status, t.status),
        admin_reply=t.admin_reply or "",
        admin_id=t.admin_id,
        replied_at=t.replied_at,
        created_at=t.created_at,
        updated_at=t.updated_at,
        username=(user.username if user else ""),
    )


# ---------------------------------------------------------------- 用户端接口
@router.post("", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket(payload: TicketCreateIn, db: Session = Depends(get_db),
                  user: models.User | None = Depends(optional_user)):
    """提交问题。

    - 匿名可提交（不带头即可），但**必须留邮箱**，否则管理员无从回复；
    - 登录状态下提交会自动带上 user_id，管理端列表因此能看到是谁提的；
    - 落库后立刻给平台通知邮箱发一封邮件 —— 否则管理员只能靠时不时打开
      管理端才知道有新工单，小问题往往就拖成了差评。
    - ⚠️ 发信失败**不影响**工单创建：问题本身已经记下来了，最坏情况只是
      管理员晚一点看到，不能因为邮件挂了就让用户「提交失败」。
    """
    category = payload.category if payload.category in models.TicketCategory.TEXTS \
        else models.TicketCategory.OTHER
    ticket = models.SupportTicket(
        user_id=(user.id if user else None),
        email=payload.email.strip(),
        title=payload.title.strip(),
        content=payload.content.strip(),
        category=category,
        device_id=payload.device_id.strip(),
        status=models.TicketStatus.OPEN,
        created_at=mailer.now_utc(),
        updated_at=mailer.now_utc(),
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)

    # 邮件正文需要提交者用户名，先取出来（可能为空 -> 匿名）
    username = user.username if user else ""
    out = _ticket_out(db, ticket)

    cfg = mailer.get_smtp_config(db)
    to = mailer.notify_addr(cfg)
    if not to:
        log.warning("工单 #%s 已落库，但平台未配置通知邮箱，跳过邮件通知", ticket.id)
        return out
    subject, body = mailer.build_ticket_notify_mail(ticket, username)
    result = mailer.send_mail(db, to, subject, body)
    if not result.ok:
        log.warning("工单 #%s 通知邮件发送失败：%s", ticket.id, result.error)
    return out


@router.get("/mine", response_model=list[TicketOut])
def my_tickets(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    """我提交过的问题（按提交时间倒序）。

    只认 user_id，不按 email 兜底：邮箱可以被别人填成同一个，按邮箱查会
    把陌生人的工单显示出来。
    """
    rows = db.scalars(
        select(models.SupportTicket)
        .where(models.SupportTicket.user_id == user.id)
        .order_by(models.SupportTicket.created_at.desc())
        .limit(limit)
    ).all()
    return [_ticket_out(db, t) for t in rows]


# ---------------------------------------------------------------- 管理端接口
@admin_router.get("", response_model=list[TicketOut])
def list_tickets(status_: str | None = Query(None, alias="status"),
                 category: str | None = None,
                 limit: int = _LIST_LIMIT, db: Session = Depends(get_db)):
    """工单列表（按提交时间倒序）。``?status=open`` 即待处理列表。"""
    q = select(models.SupportTicket)
    if status_:
        q = q.where(models.SupportTicket.status == status_)
    if category:
        q = q.where(models.SupportTicket.category == category)
    rows = db.scalars(q.order_by(models.SupportTicket.created_at.desc()).limit(limit)).all()
    return [_ticket_out(db, t) for t in rows]


@admin_router.get("/{ticket_id}", response_model=TicketOut)
def get_ticket(ticket_id: int, db: Session = Depends(get_db)):
    t = db.get(models.SupportTicket, ticket_id)
    if not t:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工单不存在")
    return _ticket_out(db, t)


@admin_router.patch("/{ticket_id}", response_model=TicketOut)
def patch_ticket(ticket_id: int, payload: TicketPatchIn, db: Session = Depends(get_db),
                 admin: models.User = Depends(get_current_admin)):
    """改状态 / 写回复。

    写了 admin_reply 且 notify=True 时给提交者发一封邮件（默认通知）：
    用户提完问题不会一直守在 APP 里看状态，不通知的话回复等于没回复。
    发信失败不影响回复落库 —— 同理，回复已经写进去了，不能因为邮件挂了
    就把管理员的操作回滚掉。
    """
    t = db.get(models.SupportTicket, ticket_id)
    if not t:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工单不存在")

    if payload.status is not None:
        if payload.status not in models.TicketStatus.TEXTS:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"状态非法，可选：{','.join(models.TicketStatus.TEXTS)}")
        t.status = payload.status

    reply = (payload.admin_reply or "").strip()
    if reply:
        t.admin_reply = reply
        t.admin_id = admin.id
        t.replied_at = mailer.now_utc()
        # 写了回复说明已经在处理，未手动指定状态时自动推进到「处理中」
        if payload.status is None and t.status == models.TicketStatus.OPEN:
            t.status = models.TicketStatus.PROCESSING

    t.updated_at = mailer.now_utc()
    db.commit()
    db.refresh(t)
    out = _ticket_out(db, t)

    if reply and payload.notify:
        subject, body = mailer.build_ticket_reply_mail(t, reply)
        result = mailer.send_mail(db, t.email, subject, body)
        if not result.ok:
            log.warning("工单 #%s 回复邮件发送失败：%s", t.id, result.error)
    return out
