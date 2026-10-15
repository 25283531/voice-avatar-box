"""会员系统接口（人工核销版）。

分三组：
- 用户端 ``/api/membership``：会员状态、套餐、下单、我的订单、已支付通知、放弃、收款信息
- 管理端 ``/api/admin/membership``：配置、订单列表/查询、确认到账、驳回、直接分配、过期清理
- 设备端 ``/api/device``：当日陪聊配额、陪聊时长上报

订单号与金额的核心规则见 docs/会员功能规划.md 与 app/membership.py 模块注释。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import (
    AMOUNT_MAX_LIMIT_CENTS,
    AMOUNT_MIN_LIMIT_CENTS,
    MEMBERSHIP_ORDER_TTL_HOURS,
)
from ..db import get_db
from ..deps import get_current_admin, get_current_device, get_current_user
from .. import membership as svc
from ..membership import (
    aware,
    cents_to_yuan,
    local_date_str,
    validate_date_str,
)
from ..models import utcnow
from ..schemas import (
    AmountRangeIn,
    ChatUsageIn,
    ChatUsageOut,
    ConfirmResultOut,
    DeviceQuotaOut,
    GrantIn,
    MembershipConfigOut,
    MembershipConfigPatch,
    MembershipOut,
    MembershipPlanOut,
    OrderCreateIn,
    OrderCreateOut,
    OrderOut,
    PaymentInfoOut,
    QuotaConfigIn,
    RejectIn,
)

# ============================================================ 用户端
router = APIRouter(prefix="/api/membership", tags=["membership"])

# ============================================================ 管理端
admin_router = APIRouter(
    prefix="/api/admin/membership", tags=["admin-membership"],
    dependencies=[Depends(get_current_admin)],
)

# ============================================================ 设备端
# 与 /api/device/sync 同前缀但独立成组，避免动到 device_api.py 的既有逻辑。
device_router = APIRouter(prefix="/api/device", tags=["device"])


# ---------------------------------------------------------------- 序列化辅助
def _plan_name(db: Session, plan_code: str) -> str:
    plan = svc.get_plan(db, plan_code)
    return plan.name if plan else ""


def _order_out(db: Session, order: models.MembershipOrder) -> OrderOut:
    user = db.get(models.User, order.user_id)
    return OrderOut(
        id=order.id,
        code=order.code,
        user_id=order.user_id,
        plan_code=order.plan_code,
        plan_name=_plan_name(db, order.plan_code),
        amount_cents=order.amount_cents,
        amount_yuan=cents_to_yuan(order.amount_cents),
        status=order.status,
        created_at=order.created_at,
        expires_at=order.expires_at,
        notified_at=order.notified_at,
        confirmed_at=order.confirmed_at,
        admin_id=order.admin_id,
        admin_note=order.admin_note,
        username=(user.username if user else ""),
        nickname=(user.nickname if user else ""),
    )


def _payment_out(db: Session) -> dict[str, Any]:
    cfg = svc.get_payment_config(db)
    return PaymentInfoOut(
        channel=cfg.channel, payee_name=cfg.payee_name, account=cfg.account,
        qr_url=cfg.qr_url, note=cfg.note,
    ).model_dump()


def _minutes_text(seconds: int) -> str:
    """秒数 -> 用于展示的分钟数文案（60 分钟而不是 1 小时，老人家属更好读）。"""
    seconds = int(seconds)
    if seconds <= 0:
        return "0 分钟"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    return f"{round(seconds / 60)} 分钟"


def _benefits_texts(vip: bool, quota: dict[str, Any]) -> list[str]:
    """会员权益 -> 中文短句数组。

    APP 的 MembershipInfo.benefits 是 string[]，页面直接 join 展示，
    所以这里必须给数组而不是字典。免费用户也给几条，避免展示成空白。
    """
    idle = int(quota.get("idle_timeout_seconds") or 90)
    if vip:
        chat = "陪聊不限时长"
    else:
        limit = quota.get("limit_seconds")
        chat = (f"每日陪聊 {_minutes_text(limit)}" if limit else "陪聊不限时长")
    return [chat, "服药 / 日程提醒不受限", f"空闲 {idle} 秒自动断线"]


def _plan_benefits(plan: models.MembershipPlan) -> list[str]:
    """套餐权益 -> 中文短句数组（APP 的 MembershipPlan.benefits 也是 string[]）。"""
    return [f"开通 {int(plan.days)} 天", "陪聊不限时长", "服药 / 日程提醒不受限"]


def _membership_out(db: Session, user: models.User) -> MembershipOut:
    m = svc.get_membership(db, user.id)
    now = utcnow()
    vip = svc.is_vip(db, user.id, now)
    quota = svc.quota_snapshot(db, user.id)
    return MembershipOut(
        is_vip=vip,
        plan=(m.plan if m else ""),
        started_at=(m.started_at if m else None),
        expires_at=(m.expires_at if m else None),
        days_left=(svc.days_left(m, now) if vip else 0),
        source=(m.source if m else ""),
        status=(m.status if m else "none"),
        benefits=_benefits_texts(vip, quota),
        # 结构化明细保留给管理端 / 排查，不因为改成数组就丢信息
        benefits_detail={
            "chat_unlimited": vip,
            "chat_daily_seconds": None if vip else quota["limit_seconds"],
            "reminder_always_free": True,
            "idle_timeout_seconds": quota["idle_timeout_seconds"],
        },
        quota=quota,
    )


# ---------------------------------------------------------------- 用户端接口
@router.get("", response_model=MembershipOut)
def my_membership(db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    """会员状态、到期时间、剩余天数、权益，以及当日陪聊已用/上限。"""
    return _membership_out(db, user)


@router.get("/plans", response_model=list[MembershipPlanOut])
def plans(db: Session = Depends(get_db), _: models.User = Depends(get_current_user)):
    """套餐列表（天数 + 展示价）。"""
    rows = svc.list_plans(db, only_enabled=True)
    return [
        MembershipPlanOut(
            id=p.id, code=p.code, name=p.name, description=p.description, days=p.days,
            # APP 套餐卡片写的是「{{ p.months }} 个月」，按 30 天一个月折算，至少 1 个月
            months=max(1, round(p.days / 30)),
            price_cents=p.price_cents, price_yuan=cents_to_yuan(p.price_cents),
            sort=p.sort, enabled=p.enabled, benefits=_plan_benefits(p),
        )
        for p in rows
    ]


@router.post("/orders", response_model=OrderCreateOut, status_code=status.HTTP_201_CREATED)
def create_order(payload: OrderCreateIn, db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    """创建订单。

    body 带 APP 生成的 4 位订单号；服务端在金额区间内分配一个 48 小时内
    未被占用的随机金额（整数分），并返回收款信息供用户转账。

    冲突（订单号 48h 内已占用 / 金额区间耗尽）返回 409，APP 应重新生成订单号再提交。
    """
    order = svc.create_order(db, user, payload.code, payload.plan_code)
    plan = svc.get_plan(db, order.plan_code)
    return OrderCreateOut(
        id=order.id,
        code=order.code,
        plan_code=order.plan_code,
        plan_name=plan.name if plan else "",
        days=plan.days if plan else 0,
        amount_cents=order.amount_cents,
        amount_yuan=cents_to_yuan(order.amount_cents),
        status=order.status,
        expires_at=order.expires_at,
        expires_in_sec=max(0, int((aware(order.expires_at) - utcnow()).total_seconds())),
        payment_info=_payment_out(db),
        tip=(f"请向收款账号转账 {cents_to_yuan(order.amount_cents)} 元，"
             f"转账后点击「我已支付」，管理员核对后自动开通。"
             f"订单号 {order.code} 有效期 {MEMBERSHIP_ORDER_TTL_HOURS} 小时。"),
    )


@router.get("/orders/mine", response_model=list[OrderOut])
def my_orders(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db),
              user: models.User = Depends(get_current_user)):
    """我的订单列表（按创建时间倒序）。"""
    rows = db.scalars(
        select(models.MembershipOrder)
        .where(models.MembershipOrder.user_id == user.id)
        .order_by(models.MembershipOrder.created_at.desc())
        .limit(limit)
    ).all()
    return [_order_out(db, o) for o in rows]


@router.post("/orders/{code}/notify", response_model=OrderOut)
def notify_paid(code: str, db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    """「我已支付」：订单置为 notified，进入管理端待核销列表。"""
    order = svc.get_order_by_code(db, code)
    if order.user_id != user.id and not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权操作该订单")
    svc.notify_paid(db, order)
    return _order_out(db, order)


@router.post("/orders/{code}/cancel", response_model=OrderOut)
def cancel_order(code: str, db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    """放弃订单（释放该订单对金额的占用从 48h 后才生效，号段同理）。"""
    order = svc.get_order_by_code(db, code)
    if order.user_id != user.id and not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权操作该订单")
    svc.cancel_order(db, order)
    return _order_out(db, order)


@router.get("/payment-info", response_model=PaymentInfoOut)
def payment_info(db: Session = Depends(get_db),
                 _: models.User = Depends(get_current_user)):
    """收款账号 / 收款二维码（管理端配置）。"""
    return _payment_out(db)


# ---------------------------------------------------------------- 管理端接口
def _config_out(db: Session) -> MembershipConfigOut:
    """组装管理端「会员配置」聚合视图。"""
    rng = svc.get_amount_range(db)
    quota = svc.get_quota_config(db)
    return MembershipConfigOut(
        amount_range=AmountRangeIn(min_cents=rng.min_cents, max_cents=rng.max_cents,
                                   enabled=rng.enabled),
        quota=QuotaConfigIn(free_daily_seconds=quota.free_daily_seconds,
                            idle_timeout_seconds=quota.idle_timeout_seconds,
                            vip_unlimited=quota.vip_unlimited),
        payment=_payment_out(db),
    )


@admin_router.get("/config", response_model=MembershipConfigOut)
def get_config(db: Session = Depends(get_db)):
    """读取会员配置：金额区间、陪聊配额、收款信息。"""
    return _config_out(db)


@admin_router.patch("/config", response_model=MembershipConfigOut)
def patch_config(payload: MembershipConfigPatch, db: Session = Depends(get_db),
                 admin: models.User = Depends(get_current_admin)):
    """局部更新会员配置。

    金额区间需满足 ``min >= 100``、``max <= 99999`` 且 ``min < max``。
    """
    if payload.amount_range is not None:
        lo = payload.amount_range.min_cents
        hi = payload.amount_range.max_cents
        if lo < AMOUNT_MIN_LIMIT_CENTS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"金额下限不得低于 {AMOUNT_MIN_LIMIT_CENTS} 分（1 元）")
        if hi > AMOUNT_MAX_LIMIT_CENTS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"金额上限不得高于 {AMOUNT_MAX_LIMIT_CENTS} 分")
        if lo >= hi:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "金额下限必须小于上限")
        rng = svc.get_amount_range(db)
        rng.min_cents = lo
        rng.max_cents = hi
        rng.enabled = payload.amount_range.enabled
        rng.updated_at = utcnow()

    if payload.quota is not None:
        quota = svc.get_quota_config(db)
        quota.free_daily_seconds = max(0, payload.quota.free_daily_seconds)
        quota.idle_timeout_seconds = max(1, payload.quota.idle_timeout_seconds)
        quota.vip_unlimited = payload.quota.vip_unlimited
        quota.updated_at = utcnow()

    if payload.payment is not None:
        pay = svc.get_payment_config(db)
        pay.channel = payload.payment.channel
        pay.payee_name = payload.payment.payee_name
        pay.account = payload.payment.account
        pay.qr_url = payload.payment.qr_url
        pay.note = payload.payment.note
        pay.updated_at = utcnow()

    db.commit()
    return _config_out(db)


@admin_router.get("/orders", response_model=list[OrderOut])
def list_orders(status_: str | None = Query(None, alias="status"),
                limit: int = Query(100, ge=1, le=500),
                db: Session = Depends(get_db)):
    """订单列表。``?status=notified`` 即待核销列表（按创建时间倒序）。"""
    q = select(models.MembershipOrder)
    if status_:
        q = q.where(models.MembershipOrder.status == status_)
    rows = db.scalars(q.order_by(models.MembershipOrder.created_at.desc()).limit(limit)).all()
    return [_order_out(db, o) for o in rows]


def _parse_amount_cents(amount_yuan: str | None, amount_cents: int | None) -> int:
    """把管理员输入的金额统一转成整数分。

    元 -> 分必须用 Decimal 而不是 float：10.53 * 100 在二进制浮点下是 1052.9999...，
    直接 int() 会变成 1052，和库里存的 1053 对不上，反查就查不到。
    """
    if amount_cents is not None:
        return int(amount_cents)
    if amount_yuan is None or not str(amount_yuan).strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "请提供 amount_yuan（如 10.53）或 amount_cents（如 1053）")
    try:
        value = Decimal(str(amount_yuan).strip())
    except InvalidOperation:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"金额格式非法: {amount_yuan}")
    if value < 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "金额不能为负")
    return int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))


# 注意：这个路由必须注册在 /orders/{code} 之前。
# FastAPI 按注册顺序匹配，否则 "by-amount" 会被当成 code 去查订单。
@admin_router.get("/orders/by-amount", response_model=list[OrderOut])
def find_orders_by_amount(
        amount_yuan: str | None = Query(None, description="金额（元），如 10.53"),
        amount_cents: int | None = Query(None, ge=0, description="金额（分），如 1053"),
        all_: bool = Query(False, alias="all",
                           description="翻历史全量；默认只看仍可能待收款的订单"),
        limit: int = Query(50, ge=1, le=200),
        db: Session = Depends(get_db),
        admin: models.User = Depends(get_current_admin)):
    """按金额反查订单号（人工核销的两个入口之一）。

    入口一：用户报订单号 -> `GET /orders/{code}` 查这笔该收多少钱。
    入口二：管理员先看收款记录里到了一笔钱 -> 用金额反查是哪笔订单。

    金额不独占，所以一个金额可能命中多笔；返回按创建时间倒序，由管理员结合
    时间 / 用户名 / 套餐来判断。全部查不到时返回空数组（不报 404）——
    「这个金额暂时没有对应订单」是正常结果，不是错误。
    """
    cents = _parse_amount_cents(amount_yuan, amount_cents)
    rows = svc.find_orders_by_amount(db, cents, include_inactive=all_, limit=limit)
    return [_order_out(db, o) for o in rows]


@admin_router.get("/orders/{code}", response_model=OrderOut)
def get_order(code: str, db: Session = Depends(get_db)):
    """按订单号查询：金额 / 用户 / 时间 / 套餐 / 状态（管理员核销入口）。"""
    return _order_out(db, svc.get_order_by_code(db, code))


@admin_router.post("/orders/{code}/confirm", response_model=ConfirmResultOut)
def confirm_order(code: str, db: Session = Depends(get_db),
                  admin: models.User = Depends(get_current_admin)):
    """确认到账 -> 开通会员（幂等：重复确认不会重复加时长）。"""
    order = svc.get_order_by_code(db, code)
    order, m, activated = svc.confirm_order(db, order, admin)
    target = db.get(models.User, order.user_id)
    return ConfirmResultOut(
        order=_order_out(db, order),
        membership=_membership_out(db, target) if target else MembershipOut(is_vip=False),
        activated=activated,
    )


@admin_router.post("/orders/{code}/reject", response_model=OrderOut)
def reject_order(code: str, payload: RejectIn | None = None,
                 db: Session = Depends(get_db),
                 admin: models.User = Depends(get_current_admin)):
    """驳回订单（可填备注，如「未查到该金额」）。"""
    order = svc.get_order_by_code(db, code)
    svc.reject_order(db, order, admin, (payload.reason if payload else "") or "")
    return _order_out(db, order)


@admin_router.post("/grant", response_model=MembershipOut)
def grant(payload: GrantIn, db: Session = Depends(get_db),
          admin: models.User = Depends(get_current_admin)):
    """后台直接给某用户开通 N 天会员（亲友 / 测试 / 补偿场景，不走订单）。"""
    _, target = svc.grant_membership(db, payload.user_id, payload.days, admin,
                                     note=payload.note, plan=payload.plan)
    return _membership_out(db, target)


@admin_router.post("/orders/expire")
def expire_orders(db: Session = Depends(get_db)):
    """手动触发过期清理（定时任务也会跑）：超时未支付的订单置 expired。"""
    n_orders = svc.expire_stale_orders(db)
    n_members = svc.expire_memberships(db)
    return {"ok": True, "expired_orders": n_orders, "expired_memberships": n_members,
            "ttl_hours": MEMBERSHIP_ORDER_TTL_HOURS}


# ---------------------------------------------------------------- 设备端接口
@device_router.get("/quota", response_model=DeviceQuotaOut)
def device_quota(db: Session = Depends(get_db),
                 dev: models.Device = Depends(get_current_device)):
    """当日陪聊配额：已用秒数 / 上限 / 空闲切断秒数 / 服药关键词表。"""
    snap = svc.quota_snapshot(db, dev.owner_id, device_ids=[dev.id])
    return DeviceQuotaOut(**snap)


@device_router.post("/chat-usage", response_model=ChatUsageOut)
def report_chat_usage(payload: ChatUsageIn, db: Session = Depends(get_db),
                      dev: models.Device = Depends(get_current_device)):
    """上报一次会话时长。``is_reminder=true`` 表示服药/提醒类交互，不计入配额。"""
    date = validate_date_str(payload.date) if payload.date else local_date_str()
    if not date:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "日期格式应为 YYYY-MM-DD")

    row = svc.record_chat_usage(db, dev.id, date, payload.seconds, payload.is_reminder)
    snap = svc.quota_snapshot(db, dev.owner_id, device_ids=[dev.id], date=date)
    remaining = None if snap["unlimited"] else max(
        0, int(snap["limit_seconds"] or 0) - int(snap["used_seconds"])
    )
    dev.last_seen_at = utcnow()
    dev.online = True
    db.commit()
    return ChatUsageOut(
        date=row.date,
        used_seconds=int(row.used_seconds or 0),
        session_count=int(row.session_count or 0),
        reminder_seconds=int(row.reminder_seconds or 0),
        limit_seconds=snap["limit_seconds"],
        unlimited=snap["unlimited"],
        remaining_seconds=remaining,
    )
