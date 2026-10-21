"""设备 token 用量查询（双端）。

* 用户端 ``/api/usage``：**只看本人设备**（``chat_quota.user_device_ids``
  算出来的范围），汇总 + 最近流水；
* 管理端 ``/api/admin/usage``：可按 device_id / user_id / usage / 时间过滤，
  都不传 = 全量（仅管理员允许全量）。

三条设计约束
------------
1. **聚合口径单一真源在 ``app.usage``**（``usage_summary`` /
   ``recent_usage_rows``），本文件不二次加工：SUM 各家写一遍必然漂移，
   而这三处展示（管理端/用户端/后续设备配额）必须是同一个数；
2. **用户端可见范围只由 ``user.id`` 推导，不接受查询参数指定**：
   传了 ``device_id`` 也忽略（不是报错）—— 报 403/404 会泄露"这个 id
   存在吗"，而忽略参数让越权查询与"查了一个自己不拥有的设备"无法区分；
3. **响应里永远没有 prompt / 回复正文**：表里本来就不存（见
   ``UsageRowOut`` 的注释），本层也不加。

时间参数
--------
``since`` / ``until`` 收 ISO 日期（``2026-10-01``，按**北京**当天 00:00）或
日期时间（``2026-10-01T12:00:00``，无偏移时同样按北京时间解释）；
pydantic 两种都解析，非法值 422。解析后由 :func:`_local_bound` 转成 UTC
aware 再进查询 —— 库里 ``created_at`` 是 aware 的，naive 直比会静默错位。

⚠️ 2026-10-21 起这里**不再**用 ``chat_quota.aware`` 的「naive 按 UTC」约定：
配额按 UTC 自然日结算（与厂商账单一致）是对的，但**查询筛选**的日期是
用户视角的北京日期，两者语义不同，见 :func:`_local_bound` 的说明。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import usage as usage_svc
from ..db import get_db
from ..deps import get_current_admin, get_current_user
from ..chat_quota import user_device_ids
from ..config import TZ
from ..models import User
from ..schemas import UsageRowOut, UsageSummaryOut

#: 时间参数口径（2026-10-21 时区统一）
#:
#: 前端传 ``2026-10-01``（裸日期）时，用户心里想的是「**北京**的那一天」，
#: 不是 UTC 的那一天。若按 UTC 补齐，查询区间会从北京 08:00 才开始，
#: 凌晨 0–8 点的调用被丢掉（曾经的真实症状：家属看「昨天用量」总是偏少）。
#:
#: ⚠️ 不复用 ``chat_quota.aware``：那个函数的约定是「naive 一律按 UTC」，
#: 在**配额计算**里是对的（配额按 UTC 自然日结算，与厂商账单口径一致），
#: 但用在**查询筛选**上就是错的。两个语义必须分开，否则改一处会坏另一处。
def _local_bound(dt: datetime | None) -> datetime | None:
    """把查询边界按**北京时间**解释，返回 UTC aware（供与库列比较）。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ).astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)


# ============================================================ 用户端
router = APIRouter(prefix="/api/usage", tags=["usage"])

# ============================================================ 管理端
# 与 chat_quota 同款的分路由写法：router 级依赖一次到位，端点函数里
# 不再各自判管理员（漏一处就是一个越权面）。
admin_router = APIRouter(
    prefix="/api/admin/usage", tags=["admin-usage"],
    dependencies=[Depends(get_current_admin)],
)


def _scope_device_ids(device_id: int | None) -> list[int] | None:
    """管理端的设备过滤：不传 = None（全量），传了 = 单元素列表。

    返回 None 而不是 [] 是有意的：usage_svc 的判据是 ``is not None``，
    [] 会查出一个空结果，None 才是不限制。
    """
    return [device_id] if device_id else None


@router.get("/summary", response_model=UsageSummaryOut)
def my_summary(
        since: datetime | None = Query(None),
        until: datetime | None = Query(None),
        usage: str | None = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user)):
    """我的设备的 token 用量汇总。

    没有设备时看到全 0（不是报错）："还没绑定设备"与"绑了没打过字"
    在界面上是同一件事，不需要两个状态。
    """
    return usage_svc.usage_summary(
        db,
        device_ids=user_device_ids(db, user.id),
        since=_local_bound(since), until=_local_bound(until), usage=usage,
        by_day=True,
    )


@router.get("/rows", response_model=list[UsageRowOut])
def my_rows(
        limit: int = Query(50, ge=1, le=200),
        since: datetime | None = Query(None),
        usage: str | None = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user)):
    """我的设备的最近调用流水（新→旧）。"""
    return usage_svc.recent_usage_rows(
        db,
        device_ids=user_device_ids(db, user.id),
        since=_local_bound(since), usage=usage, limit=limit,
    )


@admin_router.get("/summary", response_model=UsageSummaryOut)
def admin_summary(
        since: datetime | None = Query(None),
        until: datetime | None = Query(None),
        usage: str | None = Query(None),
        device_id: int | None = Query(None),
        user_id: int | None = Query(None),
        db: Session = Depends(get_db),
        _admin: User = Depends(get_current_admin)):
    """全平台 token 用量汇总；过滤条件都不传 = 全量。"""
    return usage_svc.usage_summary(
        db,
        device_ids=_scope_device_ids(device_id),
        user_ids=[user_id] if user_id else None,
        since=_local_bound(since), until=_local_bound(until), usage=usage,
        by_day=True,
    )


@admin_router.get("/rows", response_model=list[UsageRowOut])
def admin_rows(
        limit: int = Query(50, ge=1, le=200),
        since: datetime | None = Query(None),
        usage: str | None = Query(None),
        device_id: int | None = Query(None),
        user_id: int | None = Query(None),
        db: Session = Depends(get_db),
        _admin: User = Depends(get_current_admin)):
    """全平台最近调用流水（新→旧）；过滤条件都不传 = 全量。"""
    return usage_svc.recent_usage_rows(
        db,
        device_ids=_scope_device_ids(device_id),
        user_ids=[user_id] if user_id else None,
        since=_local_bound(since), usage=usage, limit=limit,
    )
