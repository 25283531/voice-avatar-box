"""陪聊时长计量与「设备授权即无限量」判定（取消会员制后的唯一闸门）。

## 背景：为什么这个模块曾经叫 membership

早期版本里这里同时住着两套东西：

1. **会员订单/支付/核销**（订单号 + 随机金额 + 管理员肉眼核对）；
2. **陪聊时长计量**（``chat_usage`` 逐日聚合 + ``chat_quota_config`` 单行配置）。

产品决策取消会员制后，第1 套整体删除，本模块只保留第 2 套 —— 但它必须留下，
因为 token 用量对账、交互统计、提醒关键词下发都依赖它。模块因此从
``membership.py`` 更名为 ``chat_quota.py``：名字要反映它现在**是什么**，
而不是它曾经**是什么**。

## 判定语义（本次改动的核心不变量）
------------------------------------------------
早期判定是「**按用户**查会员」：``quota_snapshot(db, user_id)`` 内部
``is_vip()`` 决定 ``unlimited``。这意味着一个买了会员的用户，他名下**所有**
设备都无限量 —— 而一个没买会员的用户，哪怕设备是官方授权的，也每天 3600 秒封顶。

取消会员制后判定改为「**按设备授权**」：

* ``authorized=True``（该设备已绑定到某个用户，即 ``devices.owner_id`` 非空）
  -> ``unlimited=True``，``limit_seconds=None``，永不拦；
* ``authorized=False``（未绑定的设备）-> 走 ``free_daily_seconds`` 上限。

⚠️ **为什么以「绑定到用户」为授权判据，而不是别的标记**
------------------------------------------------------
这个判据不是新造的，它就是 ``/api/device/ota`` 下发 WS 通道的那个既有条件
（见 ``voice/__init__.py``：``dev.owner_id is not None`` 才签发 device_token）。
也就是说：**拿不到 WS token 的设备本来就进不了语音会话**，
而能进来的必然是已绑定设备。所以这条判定在语音链路上恒为真 ——
保留它是为了纵深防御（万一将来出现别的入口拿到 device_token），
以及让 ``/api/device/quota`` 这类对外接口有明确的降级语义。

⚠️ **记账与闸门必须分开看**
--------------------------
删掉会员制**不等于**删掉计量。``record_chat_usage`` 照旧逐日累加
``used_seconds``，``used_seconds_today`` 照旧可查 —— 用户还要看用量，
运营也要对账。区别只在于：这个数字**不再用于拦人**，只用于展示与统计。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models
from .models import ChatQuotaConfig, ChatUsage, ReminderKeyword

log = logging.getLogger("elder.chat_quota")

# ---------------------------------------------------------------- 常量
DEFAULT_FREE_DAILY_SECONDS = 3600
DEFAULT_IDLE_TIMEOUT_SECONDS = 90

# 默认服药/提醒关键词：设备端本地命中即放行 AI 调用（不计入陪聊时长统计口径）
DEFAULT_REMINDER_KEYWORDS: list[str] = [
    "药", "吃药", "吃了", "服了", "服药", "用药", "降压药", "胶囊", "片",
    "提醒", "日程", "确认", "好了", "知道了", "已完成", "取消", "稍后",
]


# ---------------------------------------------------------------- 工具
def aware(dt: datetime | None) -> datetime | None:
    """把可能为 naive 的时间（SQLite 读回的）统一补成 UTC aware，避免比较时报错。

    这个函数被 ``device_state`` / ``routers/usage`` / ``routers/mail`` 共用，
    是全仓「时间比较」的唯一入口 —— 各处自己写一遍补时区必然漂移。
    """
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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
def get_quota_config(db: Session) -> ChatQuotaConfig:
    """取陪聊配额配置（单行，不存在则按默认值创建）。

    ``free_daily_seconds`` 现在只对**未授权设备**有意义；已授权设备一律无限量。
    """
    row = db.scalar(select(ChatQuotaConfig).limit(1))
    if row is None:
        row = ChatQuotaConfig(free_daily_seconds=DEFAULT_FREE_DAILY_SECONDS,
                              idle_timeout_seconds=DEFAULT_IDLE_TIMEOUT_SECONDS)
        db.add(row)
        db.flush()
    return row


# ---------------------------------------------------------------- 计量
def used_seconds_today(db: Session, device_ids: list[int],
                       date: str | None = None) -> int:
    """统计这些设备当日已用的陪聊秒数。

    ⚠️ 取消会员制后这个数**不再用于拦人**，只用于展示与对账（见模块头部）。
    """
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


def quota_snapshot(db: Session, device_ids: list[int] | None = None,
                   date: str | None = None, *,
                   authorized: bool = True) -> dict:
    """构造一个统一的配额视图（设备端与用户端共用）。

    返回字段：
        used_seconds / limit_seconds(不限时为 None) / unlimited /
        idle_timeout_seconds / reminder_keywords / date

    ``authorized`` 由调用方按**设备**给出（判据见模块头部）。默认 ``True``
    是因为除「设备未绑定」这一条降级路径外，其余调用方拿到的都是已授权设备。
    """
    cfg = get_quota_config(db)
    date = date or local_date_str()
    ids = device_ids if device_ids is not None else []
    used = used_seconds_today(db, ids, date)
    unlimited = bool(authorized)
    return {
        "date": date,
        "used_seconds": used,
        "limit_seconds": None if unlimited else int(cfg.free_daily_seconds),
        "unlimited": unlimited,
        "idle_timeout_seconds": int(cfg.idle_timeout_seconds),
        "reminder_keywords": list_reminder_keywords(db),
    }


def record_chat_usage(db: Session, device_pk: int, date: str, seconds: int,
                      is_reminder: bool = False) -> ChatUsage:
    """上报一次陪聊会话时长（幂等累加，按设备 + 日期聚合）。

    is_reminder=True 表示这次是服药/提醒相关交互，与普通陪聊分开累计便于排查。

    ⚠️ 这个函数是时长对账的落点，**取消会员制不影响它**：
    删的只是「拿它去拦人」，记账本身照旧。
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
    """确保陪聊计量相关的默认配置与基础数据存在（幂等）。"""
    if db.scalar(select(ChatQuotaConfig).limit(1)) is None:
        db.add(ChatQuotaConfig(free_daily_seconds=DEFAULT_FREE_DAILY_SECONDS,
                               idle_timeout_seconds=DEFAULT_IDLE_TIMEOUT_SECONDS))
        log.info("seed: 默认陪聊配额 %ds / 空闲切断 %ds",
                 DEFAULT_FREE_DAILY_SECONDS, DEFAULT_IDLE_TIMEOUT_SECONDS)

    added = 0
    for idx, word in enumerate(DEFAULT_REMINDER_KEYWORDS):
        row = db.scalar(select(ReminderKeyword).where(ReminderKeyword.word == word))
        if row is None:
            db.add(ReminderKeyword(word=word, enabled=True, sort=idx))
            added += 1
    if added:
        log.info("seed: 默认服药关键词 %d 个", added)

    db.commit()
