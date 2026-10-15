"""会员系统核心逻辑（人工核销版）。

背景：当前拿不到支付接口，会员只能走「线下转账 + 管理员肉眼核对」。
每个订单会分配一个「4 位订单号 + 区间内随机的金额（整数分）」，两者一一绑定，
于是人工核销有**两个方向**：

- 订单号 → 金额：用户付款后报号，管理员查出这笔该收多少钱，去收款记录核对。
- 金额 → 订单号：管理员先在收款记录里看到一笔入账，用金额反查是谁下的单
  （见 find_orders_by_amount）。

占用规则（对齐 docs/会员功能规划.md 第 2/3 节）
------------------------------------------------
- 订单号：48 小时内**所有状态**的订单都占用（含 confirmed/rejected/cancelled），
  防止管理员刚核销完的号被另一个用户复用造成错账。
- 金额：**不做占用**。订单号才是唯一标识，重复金额完全能靠订单号区分开；
  早期版本在这里做「48 小时内不重复」，会把区间占满导致新订单下不了单，已废弃。
- pending / notified 一旦超过 48 小时由定时任务置 expired，订单号释放。

所有金额一律用**整数分**参与计算与存储，只在出参展示时转成元。
"""
from __future__ import annotations

import logging
import math
import secrets
import string
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import models
from .config import (
    AMOUNT_MAX_LIMIT_CENTS,
    AMOUNT_MIN_LIMIT_CENTS,
    MEMBERSHIP_ORDER_RATE_MAX,
    MEMBERSHIP_ORDER_RATE_WINDOW_SEC,
    MEMBERSHIP_ORDER_TTL_HOURS,
)
from .models import (
    ChatQuotaConfig,
    ChatUsage,
    Membership,
    MembershipAmountRange,
    MembershipOrder,
    MembershipPlan,
    MembershipSource,
    MembershipStatus,
    OrderStatus,
    PaymentConfig,
    ReminderKeyword,
    utcnow,
)

log = logging.getLogger("elder.membership")

# ---------------------------------------------------------------- 常量
ORDER_CODE_LEN = 4
ORDER_CODE_CHARS = string.ascii_uppercase + string.digits
# 只有这两个状态是「活跃」的：会占用订单号与金额，且会被定时任务清理
ACTIVE_ORDER_STATUS = (OrderStatus.PENDING, OrderStatus.NOTIFIED)
# 终态：不再活跃，但仍会在创建后 48 小时内占用号段（避免核销后立刻被复用）
CLOSED_ORDER_STATUS = (
    OrderStatus.CONFIRMED, OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED,
)

# 默认金额区间：10.00 ~ 12.00 元（1000~1200 分，共 201 个候选值）。
#
# ⚠️ 金额**不要求唯一**：订单号与金额是一一绑定的，管理员拿到订单号就能看到这笔的
# 专属金额，去收款记录里核对有没有这笔入账即可确认；反过来也能先看到收款记录里的
# 金额、再按金额反查订单号（见 find_orders_by_amount）。
# 因此金额只是「让每一笔转账可区分」的标记，不需要像订单号那样做独占 ——
# 早期版本要求「48 小时内金额不重复」，结果区间很快被占满、新订单直接下不了单，
# 而实际上同一金额对应两笔订单也完全能靠订单号区分开。
DEFAULT_MIN_CENTS = 1000
DEFAULT_MAX_CENTS = 1200
DEFAULT_FREE_DAILY_SECONDS = 3600
DEFAULT_IDLE_TIMEOUT_SECONDS = 90

# 默认套餐（天数为开通时长；price_cents 仅为展示价，实际收款以订单随机金额为准）
DEFAULT_PLANS: list[dict] = [
    {"code": "monthly", "name": "月卡", "days": 30, "price_cents": 1900, "sort": 10,
     "description": "开通 30 天会员，陪聊不限时长"},
    {"code": "quarterly", "name": "季卡", "days": 90, "price_cents": 4900, "sort": 20,
     "description": "开通 90 天会员，陪聊不限时长"},
    {"code": "yearly", "name": "年卡", "days": 365, "price_cents": 16800, "sort": 30,
     "description": "开通 365 天会员，陪聊不限时长"},
]

# 默认服药/提醒关键词：设备端本地命中即放行 AI 调用，不消耗陪聊配额
DEFAULT_REMINDER_KEYWORDS: list[str] = [
    "药", "吃药", "吃了", "服了", "服药", "用药", "降压药", "胶囊", "片",
    "提醒", "日程", "确认", "好了", "知道了", "已完成", "取消", "稍后",
]


# ---------------------------------------------------------------- 工具
def aware(dt: datetime | None) -> datetime | None:
    """把可能为 naive 的时间（SQLite 读回的）统一补成 UTC aware，避免比较时报错。"""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_order_code(raw: str) -> str:
    """容错输入：去掉空格与分隔符、统一转大写。"""
    if not raw:
        return ""
    return "".join(ch for ch in str(raw).strip() if ch.isalnum()).upper()


def validate_order_code(raw: str) -> str:
    """校验并规范化 4 位订单号（字符集 A-Z + 0-9），非法直接 422。"""
    code = normalize_order_code(raw)
    if len(code) != ORDER_CODE_LEN or any(ch not in ORDER_CODE_CHARS for ch in code):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"订单号应为 {ORDER_CODE_LEN} 位字母或数字（A-Z / 0-9）",
        )
    return code


def cents_to_yuan(cents: int) -> str:
    """整数分 -> 展示用元字符串（仅用于展示，不参与计算）。"""
    return f"{int(cents) / 100:.2f}"


def local_date_str(now: datetime | None = None) -> str:
    """当日本地日期 YYYY-MM-DD（配额按设备本地日期对账）。"""
    from .schedule import local_now

    base = now or local_now()
    return base.strftime("%Y-%m-%d")


def validate_date_str(value: str) -> str:
    """校验 YYYY-MM-DD，非法返回空串由调用方决定处理。"""
    try:
        datetime.strptime(value.strip(), "%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return ""
    return value.strip()


# ---------------------------------------------------------------- 单行配置
def get_amount_range(db: Session) -> MembershipAmountRange:
    """取金额区间配置（单行，不存在则按默认值创建）。"""
    row = db.scalar(select(MembershipAmountRange).limit(1))
    if row is None:
        row = MembershipAmountRange(min_cents=DEFAULT_MIN_CENTS, max_cents=DEFAULT_MAX_CENTS,
                                    enabled=True)
        db.add(row)
        db.flush()
    return row


def get_quota_config(db: Session) -> ChatQuotaConfig:
    """取陪聊配额配置（单行，不存在则按默认值创建）。"""
    row = db.scalar(select(ChatQuotaConfig).limit(1))
    if row is None:
        row = ChatQuotaConfig(free_daily_seconds=DEFAULT_FREE_DAILY_SECONDS,
                              idle_timeout_seconds=DEFAULT_IDLE_TIMEOUT_SECONDS,
                              vip_unlimited=True)
        db.add(row)
        db.flush()
    return row


def get_payment_config(db: Session) -> PaymentConfig:
    """取收款信息配置（单行，不存在则创建空配置）。"""
    row = db.scalar(select(PaymentConfig).limit(1))
    if row is None:
        row = PaymentConfig()
        db.add(row)
        db.flush()
    return row


# ---------------------------------------------------------------- 占用判定
def _occupied_filter(now: datetime | None = None):
    """生成「仍占用订单号/金额」的过滤条件。

    占用 = 处于活跃状态（pending / notified）
        OR 创建时间在 TTL 窗口内（所有状态，防止刚核销的号被复用）。
    """
    now = now or utcnow()
    since = now - timedelta(hours=MEMBERSHIP_ORDER_TTL_HOURS)
    return or_(
        MembershipOrder.status.in_(ACTIVE_ORDER_STATUS),
        MembershipOrder.created_at >= since,
    )


def is_code_occupied(db: Session, code: str, now: datetime | None = None) -> bool:
    """该订单号当前是否被占用。"""
    row = db.scalar(
        select(MembershipOrder.id)
        .where(MembershipOrder.code == code, _occupied_filter(now))
        .limit(1)
    )
    return row is not None


def pick_amount_cents(db: Session, now: datetime | None = None) -> int:
    """在区间内随机取一个整数分作为本笔订单的专属金额。

    金额**不做去重**：它与订单号一一绑定，管理员既可以用订单号查金额，
    也可以用金额反查订单号（find_orders_by_amount），重复金额不会造成混淆。
    早期版本在这里做「48 小时内不重复」，会把区间占满导致新订单下不了单，已废弃。
    """
    cfg = get_amount_range(db)
    lo = int(cfg.min_cents)
    hi = int(cfg.max_cents)
    if lo > hi:
        lo, hi = hi, lo

    if lo < AMOUNT_MIN_LIMIT_CENTS or hi > AMOUNT_MAX_LIMIT_CENTS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"金额区间配置非法，应在 {AMOUNT_MIN_LIMIT_CENTS}~{AMOUNT_MAX_LIMIT_CENTS} 分之间",
        )
    return secrets.randbelow(hi - lo + 1) + lo


# ---------------------------------------------------------------- 套餐
def get_plan(db: Session, plan_code: str) -> MembershipPlan | None:
    return db.scalar(select(MembershipPlan).where(MembershipPlan.code == plan_code))


def list_plans(db: Session, only_enabled: bool = True) -> list[MembershipPlan]:
    q = select(MembershipPlan)
    if only_enabled:
        q = q.where(MembershipPlan.enabled.is_(True))
    return list(db.scalars(q.order_by(MembershipPlan.sort, MembershipPlan.id)).all())


# ---------------------------------------------------------------- 下单
def create_order(db: Session, user: models.User, raw_code: str,
                 plan_code: str) -> MembershipOrder:
    """创建订单：校验订单号 -> 频率限制 -> 分配随机金额 -> 落库。

    - 订单号 48 小时内重复 -> 409（APP 重新生成再提交）
    - 金额区间耗尽 -> 409（提示管理员放宽区间）
    - 60 秒内下单超过 3 笔 -> 429
    """
    code = validate_order_code(raw_code)
    plan = get_plan(db, plan_code)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"套餐不存在: {plan_code}")

    now = utcnow()

    # 频率限制：避免刷号占满 4 位号段
    recent = db.scalar(
        select(func.count())
        .select_from(MembershipOrder)
        .where(MembershipOrder.user_id == user.id,
               MembershipOrder.created_at >= now - timedelta(
                   seconds=MEMBERSHIP_ORDER_RATE_WINDOW_SEC))
    ) or 0
    if recent >= MEMBERSHIP_ORDER_RATE_MAX:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"下单过于频繁，请 {MEMBERSHIP_ORDER_RATE_WINDOW_SEC} 秒后再试",
        )

    if is_code_occupied(db, code, now):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"订单号 {code} 在 {MEMBERSHIP_ORDER_TTL_HOURS} 小时内已被占用，请更换订单号重试",
        )

    amount = pick_amount_cents(db, now)
    order = MembershipOrder(
        code=code,
        user_id=user.id,
        plan_code=plan.code,
        amount_cents=amount,
        status=OrderStatus.PENDING,
        created_at=now,
        expires_at=now + timedelta(hours=MEMBERSHIP_ORDER_TTL_HOURS),
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    log.info("order created code=%s user=%s plan=%s amount=%s", code, user.id, plan.code, amount)
    return order


def get_order_by_code(db: Session, raw_code: str) -> MembershipOrder:
    """按订单号取订单（大小写不敏感），不存在 404。"""
    code = validate_order_code(raw_code)
    order = db.scalar(select(MembershipOrder).where(MembershipOrder.code == code)
                      .order_by(MembershipOrder.created_at.desc()))
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"订单不存在: {code}")
    return order


def find_orders_by_amount(db: Session, amount_cents: int,
                          include_inactive: bool = False,
                          limit: int = 50) -> list[MembershipOrder]:
    """按金额反查订单号（管理员的第二个核销入口）。

    场景：管理员先在收款记录里看到一笔「10.53 元」的入账，但不知道是谁下的单，
    就用这个金额反查订单号，找到订单后再确认到账。

    金额不做独占，所以同一个金额可能命中多笔订单（不同时间、不同用户），
    这里按创建时间倒序全部返回，由管理员结合时间/金额/用户名判断。
    默认只看「仍可能待收款」的订单（活跃状态或 48 小时窗口内），
    include_inactive=True 时才翻历史全量。
    """
    q = select(MembershipOrder).where(MembershipOrder.amount_cents == amount_cents)
    if not include_inactive:
        q = q.where(_occupied_filter())
    return list(db.scalars(
        q.order_by(MembershipOrder.created_at.desc()).limit(limit)
    ).all())


def notify_paid(db: Session, order: MembershipOrder) -> MembershipOrder:
    """用户点「我已支付」：pending -> notified。"""
    if order.status == OrderStatus.NOTIFIED:
        return order
    if order.status != OrderStatus.PENDING:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"订单当前状态为 {order.status}，无法标记已支付")
    order.status = OrderStatus.NOTIFIED
    order.notified_at = utcnow()
    db.commit()
    db.refresh(order)
    return order


def cancel_order(db: Session, order: MembershipOrder) -> MembershipOrder:
    """用户放弃订单：pending / notified -> cancelled。"""
    if order.status in CLOSED_ORDER_STATUS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"订单当前状态为 {order.status}，无法取消")
    order.status = OrderStatus.CANCELLED
    db.commit()
    db.refresh(order)
    return order


# ---------------------------------------------------------------- 会员开通
def activate_membership(db: Session, user_id: int, days: int, plan: str = "",
                        source: str = MembershipSource.ORDER,
                        note: str = "") -> Membership:
    """给指定用户开通/续费会员，返回会员记录。

    未过期时**叠加**时长（从原到期时间往后延），已过期或首次则从当前时间起算。
    """
    days = max(1, int(days))
    now = utcnow()
    m = db.scalar(select(Membership).where(Membership.user_id == user_id))
    if m is None:
        m = Membership(
            user_id=user_id,
            plan=plan,
            started_at=now,
            expires_at=now + timedelta(days=days),
            source=source,
            status=MembershipStatus.ACTIVE,
            note=note,
        )
        db.add(m)
    else:
        expires = aware(m.expires_at)
        base = expires if (expires and expires > now) else now
        m.expires_at = base + timedelta(days=days)
        m.plan = plan or m.plan
        m.source = source
        m.status = MembershipStatus.ACTIVE
        if note:
            m.note = note
        if m.started_at is None:
            m.started_at = now
    db.flush()
    return m


def confirm_order(db: Session, order: MembershipOrder, admin: models.User,
                  note: str = "") -> tuple[MembershipOrder, Membership, bool]:
    """管理员确认到账 -> 开通会员。

    幂等：订单已是 confirmed 时直接返回，不再重复加时长。
    返回 (订单, 会员记录, 本次是否真正开通)。
    """
    if order.status == OrderStatus.CONFIRMED:
        m = db.scalar(select(Membership).where(Membership.user_id == order.user_id))
        return order, m, False

    if order.status not in ACTIVE_ORDER_STATUS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"订单当前状态为 {order.status}，无法确认到账")

    plan = get_plan(db, order.plan_code)
    days = plan.days if plan else 30
    now = utcnow()

    order.status = OrderStatus.CONFIRMED
    order.confirmed_at = now
    order.admin_id = admin.id
    order.admin_note = note or order.admin_note or ""

    m = activate_membership(db, order.user_id, days, plan=order.plan_code,
                            source=MembershipSource.ORDER,
                            note=f"订单 {order.code} 核销")
    db.commit()
    db.refresh(order)
    db.refresh(m)
    log.info("order confirmed code=%s amount=%s user=%s by admin=%s days=%s",
             order.code, order.amount_cents, order.user_id, admin.id, days)
    return order, m, True


def reject_order(db: Session, order: MembershipOrder, admin: models.User,
                 reason: str = "") -> MembershipOrder:
    """管理员驳回：notified / pending -> rejected。"""
    if order.status == OrderStatus.REJECTED:
        order.admin_note = reason or order.admin_note
        db.commit()
        db.refresh(order)
        return order
    if order.status not in ACTIVE_ORDER_STATUS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"订单当前状态为 {order.status}，无法驳回")
    order.status = OrderStatus.REJECTED
    order.admin_id = admin.id
    order.admin_note = reason or ""
    db.commit()
    db.refresh(order)
    return order


def grant_membership(db: Session, user_id: int, days: int, admin: models.User,
                     note: str = "", plan: str = "admin") -> tuple[Membership, models.User]:
    """后台直接给某用户开通 N 天会员（不走订单）。"""
    target = db.get(models.User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"用户不存在: {user_id}")
    m = activate_membership(db, user_id, days, plan=plan,
                            source=MembershipSource.ADMIN, note=note)
    db.commit()
    db.refresh(m)
    log.info("membership granted user=%s days=%s by admin=%s", user_id, days, admin.id)
    return m, target


# ---------------------------------------------------------------- 过期清理
def expire_stale_orders(db: Session, now: datetime | None = None) -> int:
    """把超过 TTL 仍为 pending / notified 的订单置为 expired，返回清理数量。"""
    now = now or utcnow()
    cutoff = now - timedelta(hours=MEMBERSHIP_ORDER_TTL_HOURS)
    rows = db.scalars(
        select(MembershipOrder).where(
            MembershipOrder.status.in_(ACTIVE_ORDER_STATUS),
            or_(MembershipOrder.expires_at <= now, MembershipOrder.created_at < cutoff),
        )
    ).all()
    for order in rows:
        order.status = OrderStatus.EXPIRED
        order.admin_note = order.admin_note or "超过有效期未支付，自动过期"
    if rows:
        db.commit()
        log.info("expired %d stale orders", len(rows))
    return len(rows)


def expire_memberships(db: Session, now: datetime | None = None) -> int:
    """把已过期的会员记录标记为 expired（不影响 is_vip 判定，仅便于后台筛查）。"""
    now = now or utcnow()
    rows = db.scalars(
        select(Membership).where(
            Membership.status == MembershipStatus.ACTIVE,
            Membership.expires_at <= now,
        )
    ).all()
    for m in rows:
        m.status = MembershipStatus.EXPIRED
    if rows:
        db.commit()
    return len(rows)


# ---------------------------------------------------------------- 状态查询
def get_membership(db: Session, user_id: int) -> Membership | None:
    return db.scalar(select(Membership).where(Membership.user_id == user_id))


def is_vip(db: Session, user_id: int | None, now: datetime | None = None) -> bool:
    """该用户当前是否为有效会员。"""
    if not user_id:
        return False
    m = get_membership(db, user_id)
    if m is None or m.status != MembershipStatus.ACTIVE:
        return False
    expires = aware(m.expires_at)
    return bool(expires and expires > (now or utcnow()))


def days_left(m: Membership | None, now: datetime | None = None) -> int:
    """剩余天数（向上取整，不足一天算 1 天）。"""
    if m is None:
        return 0
    expires = aware(m.expires_at)
    if not expires:
        return 0
    delta = (expires - (now or utcnow())).total_seconds()
    return max(0, math.ceil(delta / 86400)) if delta > 0 else 0


# ---------------------------------------------------------------- 陪聊配额
def used_seconds_today(db: Session, device_ids: list[int],
                       date: str | None = None) -> int:
    """统计这些设备当日已用的陪聊秒数。"""
    if not device_ids:
        return 0
    date = date or local_date_str()
    total = db.scalar(
        select(func.coalesce(func.sum(ChatUsage.used_seconds), 0))
        .where(ChatUsage.device_id.in_(device_ids), ChatUsage.date == date)
    )
    return int(total or 0)


def user_device_ids(db: Session, user_id: int) -> list[int]:
    """用户名下全部设备主键。"""
    rows = db.scalars(
        select(models.Device.id).where(models.Device.owner_id == user_id)
    ).all()
    return [int(v) for v in rows]


def quota_snapshot(db: Session, user_id: int | None, device_ids: list[int] | None = None,
                   date: str | None = None) -> dict:
    """构造一个统一的配额视图（用户端与设备端共用）。

    返回字段：
        used_seconds / limit_seconds(不限时为 None) / unlimited /
        idle_timeout_seconds / reminder_keywords / date / is_vip
    """
    cfg = get_quota_config(db)
    vip = is_vip(db, user_id)
    date = date or local_date_str()
    ids = device_ids if device_ids is not None else (
        user_device_ids(db, user_id) if user_id else []
    )
    used = used_seconds_today(db, ids, date)
    unlimited = bool(vip and cfg.vip_unlimited)
    return {
        "date": date,
        "is_vip": vip,
        "used_seconds": used,
        "limit_seconds": None if unlimited else int(cfg.free_daily_seconds),
        "unlimited": unlimited,
        "idle_timeout_seconds": int(cfg.idle_timeout_seconds),
        "reminder_keywords": list_reminder_keywords(db),
    }


def record_chat_usage(db: Session, device_pk: int, date: str, seconds: int,
                      is_reminder: bool = False) -> ChatUsage:
    """上报一次陪聊会话时长（幂等累加，按设备 + 日期聚合）。

    is_reminder=True 表示这次是服药/提醒相关交互，按规划不计入配额，
    只累计到 reminder_seconds 便于排查。
    """
    seconds = max(0, int(seconds))
    row = db.scalar(
        select(ChatUsage).where(ChatUsage.device_id == device_pk, ChatUsage.date == date)
    )
    if row is None:
        row = ChatUsage(device_id=device_pk, date=date, used_seconds=0,
                        session_count=0, reminder_seconds=0)
        db.add(row)
        db.flush()

    row.session_count = int(row.session_count or 0) + 1
    if is_reminder:
        row.reminder_seconds = int(row.reminder_seconds or 0) + seconds
    else:
        row.used_seconds = int(row.used_seconds or 0) + seconds
    db.commit()
    db.refresh(row)
    return row


def list_reminder_keywords(db: Session, only_enabled: bool = True) -> list[str]:
    """下发给设备的服药/提醒关键词表。"""
    q = select(ReminderKeyword)
    if only_enabled:
        q = q.where(ReminderKeyword.enabled.is_(True))
    rows = db.scalars(q.order_by(ReminderKeyword.sort, ReminderKeyword.id)).all()
    return [r.word for r in rows]


# ---------------------------------------------------------------- 种子数据
def seed_defaults(db: Session) -> None:
    """确保会员相关的默认配置与基础数据存在（幂等）。"""
    # 单行配置
    if db.scalar(select(MembershipAmountRange).limit(1)) is None:
        db.add(MembershipAmountRange(min_cents=DEFAULT_MIN_CENTS,
                                     max_cents=DEFAULT_MAX_CENTS, enabled=True))
        log.info("seed: 默认金额区间 %d~%d 分", DEFAULT_MIN_CENTS, DEFAULT_MAX_CENTS)

    if db.scalar(select(ChatQuotaConfig).limit(1)) is None:
        db.add(ChatQuotaConfig(free_daily_seconds=DEFAULT_FREE_DAILY_SECONDS,
                               idle_timeout_seconds=DEFAULT_IDLE_TIMEOUT_SECONDS,
                               vip_unlimited=True))
        log.info("seed: 默认陪聊配额 %ds / 空闲切断 %ds",
                 DEFAULT_FREE_DAILY_SECONDS, DEFAULT_IDLE_TIMEOUT_SECONDS)

    if db.scalar(select(PaymentConfig).limit(1)) is None:
        db.add(PaymentConfig())
        log.info("seed: 空收款信息配置（请在管理端填写）")

    # 套餐
    changed = False
    for item in DEFAULT_PLANS:
        row = db.scalar(select(MembershipPlan).where(MembershipPlan.code == item["code"]))
        if row is None:
            db.add(MembershipPlan(**item))
            changed = True
    if changed:
        log.info("seed: 默认会员套餐 %d 个", len(DEFAULT_PLANS))

    # 服药关键词
    added = 0
    for idx, word in enumerate(DEFAULT_REMINDER_KEYWORDS):
        row = db.scalar(select(ReminderKeyword).where(ReminderKeyword.word == word))
        if row is None:
            db.add(ReminderKeyword(word=word, enabled=True, sort=idx))
            added += 1
    if added:
        log.info("seed: 默认服药关键词 %d 个", added)

    db.commit()
