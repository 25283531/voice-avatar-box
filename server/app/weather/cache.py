"""天气缓存（SQLite 单表，按需预热）。

计费模型决定了这里的每个设计
--------------------------
和风按**调用次数**计费。老人一天问十次「今天天气」，如果每次都打上游，
十次就是十次计费、十次 1~2 秒等待。缓存要同时解决两件事：**省钱**和**别让老人等**。

所以策略是「按需预热」而不是「拉全国」：

* 只有**真被问过**的城市才会进表 —— 冷门城市没人问，就永远不产生调用；
* 命中未过期的行 → 直接返回，**不碰网络**；
* 快过期时由定时任务提前续（见 ``service.warm_recent``），而不是等老人
  正好问到那一下才去拉 —— 那一下必然慢，且赶上上游抖动就直接答不上来。

为什么过期行**不删**
------------------
``expires_at`` 只是「不再被命中」，行本身是**需求记录**：它记录了「这个城市
最近被问过、问了几次」。预热任务正是靠 ``last_hit_at`` + ``hits`` 决定该续谁。
一旦到点就 DELETE，「按需」就退化成「每次都是冷启动」，缓存等于没有。

真正的清理交给 ``purge_stale``（默认保留 7 天），由预热任务顺带执行。

时区陷阱（本项目踩过）
--------------------
``DateTime(timezone=True)`` 在 SQLite 下**不保存时区**，读回来是 naive。
拿它和 ``datetime.now(timezone.utc)`` 直接比较会抛
``TypeError: can't compare offset-naive and offset-aware datetimes``。
所有比较一律先过 ``_aware()``（与 ``scheduler.py`` 里 due_at 的处理同一取向）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import WeatherCache

log = logging.getLogger("elder.weather.cache")

#: 过期多久之后彻底删行（天）。留一周足够预热任务判断"最近还有人问"
PURGE_AFTER_DAYS = 7


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite 读回来的 naive 时间按 UTC 解释（写入时用的就是 utcnow）。"""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def get(db: Session, key: str) -> dict[str, Any] | None:
    """取缓存。未过期返回上游原始 JSON（dict），过期或不存在返回 None。

    命中时顺手累计 ``hits`` / ``last_hit_at`` —— 这两个字段是预热任务的排序依据，
    **只有真实请求会写**，预热刷新走 ``put(..., count_hit=False)``，否则
    「预热自己把热度刷上去」会让冷门城市永远沉不下去。
    """
    row = db.scalars(select(WeatherCache).where(WeatherCache.cache_key == key)).first()
    if row is None:
        return None

    now = datetime.now(timezone.utc)
    if _aware(row.expires_at) is not None and _aware(row.expires_at) <= now:
        # 过期但保留：让预热任务知道"这个城市曾经被问过"
        return None

    try:
        payload = json.loads(row.payload or "{}")
    except (ValueError, TypeError):
        # 存进去的是上游原始 JSON，理论上不会坏。真坏了就当没有，
        # 让它重新拉一次 —— 比把坏数据喂给口语化层安全得多。
        log.warning("缓存 %s 的 payload 不是合法 JSON，按未命中处理", key)
        return None

    row.hits = int(row.hits or 0) + 1
    row.last_hit_at = now
    db.commit()
    return payload


def put(db: Session, key: str, city_name: str, payload: dict[str, Any],
        ttl_minutes: int, *, count_hit: bool = True) -> None:
    """写入/覆盖一条缓存。

    ``count_hit=False`` 用于**预热刷新**：刷新不是「有人问了」，不该计入热度。
    """
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=max(1, ttl_minutes))
    row = db.scalars(select(WeatherCache).where(WeatherCache.cache_key == key)).first()
    if row is None:
        row = WeatherCache(cache_key=key, city_name=city_name, hits=0)
        db.add(row)
    row.city_name = city_name
    row.payload = json.dumps(payload, ensure_ascii=False)
    row.fetched_at = now
    row.expires_at = expires
    if count_hit:
        row.hits = int(row.hits or 0) + 1
        row.last_hit_at = now
    db.commit()


def peek(db: Session, key: str) -> WeatherCache | None:
    """只读地看一行（不累计 hits、不判过期）。给预热任务与调试用。"""
    return db.scalars(select(WeatherCache).where(WeatherCache.cache_key == key)).first()


def warm_candidates(db: Session, *, recent_hours: int, ahead_minutes: int,
                    now: datetime | None = None) -> list[WeatherCache]:
    """挑出「该续了」的缓存行。

    条件两条，缺一不可：
      1. ``last_hit_at`` 在最近 N 小时内 —— 说明**真的有人还在问**这个城市；
         没人问的城市即使快过期也不续，这正是「冷门城市永不调用」的实现；
      2. ``expires_at`` 在 ahead_minutes 内到期（或已过期）—— 还没到期的没必要
         提前打上游，那纯属浪费计费次数。

    排序按 ``hits`` 降序：额度有限时先保热门城市。
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=max(1, recent_hours))
    until = now + timedelta(minutes=max(1, ahead_minutes))

    rows = db.scalars(
        select(WeatherCache)
        .where(WeatherCache.last_hit_at >= since)
        .where(WeatherCache.expires_at <= until)
        .order_by(WeatherCache.hits.desc())
    ).all()
    return list(rows)


def purge_stale(db: Session, *, keep_days: int = PURGE_AFTER_DAYS,
                now: datetime | None = None) -> int:
    """删掉「过期超过 keep_days」的行，返回删除条数。

    只删真的很久没人管的，避免表无限长大（一天几十个城市、一年也就几万行，
    其实不大，但无界增长的表迟早会变成某次排查里的噪音）。
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, keep_days))
    result = db.execute(
        delete(WeatherCache).where(WeatherCache.expires_at < cutoff))
    db.commit()
    return int(result.rowcount or 0)
