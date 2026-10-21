"""天气 HTTP 接口（给家属 APP / 后台看，也给运维直接验证缓存是否命中）。

为什么在语音工具之外**还要**一个 HTTP 接口
----------------------------------------
语音链路（LLM 调 ``get_weather``）是老人用的主路径，但它不可观测：想知道
「缓存到底有没有命中」「现在配置对不对」，只能翻日志。这个接口把同一份
``service.get_now`` 暴露出来，带 ``source``（api / cache / stale）与
``hits``，于是"按需缓存到底省了多少次调用"变成一条可以直接 curl 的事实。

⚠️ 语音工具与这个接口**共用同一个 service 与同一张缓存表**，不存在两套逻辑 ——
否则"网页上看到的是新的、老人听到的是旧的"这种分裂迟早会发生。

鉴权：与首页一致，走 ``get_current_user``（家属登录态）。天气不涉及隐私，
但它是付费 API 的入口，**不能匿名**，否则会被人拿去当免费天气源刷额度。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import get_current_user
from ..weather import get_daily, get_now, load_config

log = logging.getLogger("elder.weather.api")

router = APIRouter(prefix="/api/weather", tags=["weather"])


class WeatherOut(BaseModel):
    """一条天气结果。``source`` 是这套缓存策略的"透明度开关"。"""

    ok: bool
    message: str
    city: str = ""
    #: ``api``=本次真的调了上游；``cache``=命中缓存；``stale``=上游挂了用的旧数据
    source: str = ""
    stale: bool = False
    reason: str = ""
    need_city: bool = False


class WeatherStatusOut(BaseModel):
    """配置与缓存概况（给运维排查"为什么老人问天气没反应"）。"""

    configured: bool
    missing: list[str]
    ttl_minutes: int
    default_city: str
    cached_cities: int
    cached_rows: int
    total_hits: int


def _device_city(db: Session, user_id: int) -> str:
    """取该用户名下第一台设备的常驻城市（设备设置里的 weather_city）。"""
    dev = db.scalars(
        select(models.Device).where(models.Device.owner_id == user_id)
        .order_by(models.Device.id)
    ).first()
    if dev and isinstance(dev.settings, dict):
        return str(dev.settings.get("weather_city") or "")
    return ""


@router.get("/now", response_model=WeatherOut)
def weather_now(
    city: str = Query("", description="城市名，留空则用设备常驻城市"),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> Any:
    """实时天气。命中缓存时不产生任何上游调用。"""
    res = get_now(db, city or _device_city(db, int(user.id)))
    return WeatherOut(**{k: v for k, v in res.items()
                         if k in WeatherOut.model_fields})


@router.get("/daily", response_model=WeatherOut)
def weather_daily(
    city: str = Query("", description="城市名，留空则用设备常驻城市"),
    days: int = Query(3, ge=1, le=3, description="预报天数，最多 3 天"),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> Any:
    """今明后三天预报。"""
    res = get_daily(db, city or _device_city(db, int(user.id)), days=days)
    return WeatherOut(**{k: v for k, v in res.items()
                         if k in WeatherOut.model_fields})


@router.get("/status", response_model=WeatherStatusOut)
def weather_status(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> Any:
    """配置状态 + 缓存规模。不返回任何城市明细，只给计数。"""
    cfg = load_config()
    rows = db.scalars(select(models.WeatherCache)).all()
    return WeatherStatusOut(
        configured=cfg.configured,
        missing=cfg.missing,
        ttl_minutes=cfg.ttl_minutes,
        default_city=cfg.default_city,
        cached_cities=len({r.city_name for r in rows if r.city_name}),
        cached_rows=len(rows),
        total_hits=sum(int(r.hits or 0) for r in rows),
    )
