"""天气业务编排：按需缓存 → 上游 → 过期兜底 → 预热。

三条不变量（改这个文件时先读这三条）
----------------------------------
1. **缓存优先，且缓存命中绝不触网**。老人问天气的延迟预算是「一句话的停顿」，
   超过 1.5 秒就开始像卡住。命中缓存是 0 次网络往返。
2. **上游失败时，用过期的旧数据兜底**，并在返回里标 ``stale=True``。
   对老人来说「一小时前的温度」远好于「查不到天气」——前者能穿衣，后者只能干着急。
   但**必须**让上层知道是旧数据，不能假装新鲜（子女端/后台要能看出降级）。
3. **预热只刷「最近真被问过」的城市**，绝不遍历全国。这是计费红线：
   和风按次计费，拉全国一次就是几千次调用，一天几轮足以把免费额度打光。

城市从哪来（优先级）
------------------
``参数 city`` > ``设备 settings["weather_city"]`` > ``ELDER_WEATHER_DEFAULT_CITY``。
设备级配置放在 ``Device.settings`` 这个 JSON 列里，不动表结构 —— 一个家庭一台
设备、一个常驻城市，没必要为它加一列再写一次 schema 补丁。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from . import cache, city as city_mod
from .client import QWeatherClient, WeatherError, get_client
from .config import WeatherConfig, load as load_config
from .speak import daily_speech, now_speech

log = logging.getLogger("elder.weather")

#: 设备 settings 里存常驻城市的键名
DEVICE_CITY_KEY = "weather_city"


def now_key(city_id: str) -> str:
    return f"now:{city_id}"


def daily_key(city_id: str, days: int) -> str:
    return f"daily:{city_id}:{days}"


def _device_city(device: Any) -> str:
    """从设备的 settings JSON 里取常驻城市（取不到返回空串）。"""
    if device is None:
        return ""
    settings = getattr(device, "settings", None)
    if isinstance(settings, dict):
        return str(settings.get(DEVICE_CITY_KEY) or "").strip()
    return ""


def _pick_city_name(raw_city: str, device: Any, cfg: WeatherConfig) -> str:
    return (raw_city or "").strip() or _device_city(device) or cfg.default_city


def _stale_fallback(db: Session, key: str) -> dict[str, Any] | None:
    """取一条**已过期**的缓存（兜底用）。没有则 None。"""
    import json as _json

    row = cache.peek(db, key)
    if row is None or not row.payload:
        return None
    try:
        return _json.loads(row.payload)
    except (ValueError, TypeError):
        return None


def _fail(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "message": message, **extra}


def get_now(db: Session, raw_city: str = "", *, device: Any = None,
            client: QWeatherClient | None = None,
            cfg: WeatherConfig | None = None) -> dict[str, Any]:
    """查实时天气。返回可直接喂给 LLM 的结构（``message`` 是给模型的话术）。"""
    cfg = cfg or load_config()
    if not cfg.configured:
        return _fail("天气服务还没开通，请让家里人先在后台配置和风天气的密钥。",
                     reason="not_configured")

    name = _pick_city_name(raw_city, device, cfg)
    if not name:
        return _fail("请问您想查哪个城市的天气？", reason="no_city", need_city=True)

    try:
        info = city_mod.resolve(db, name, client=client, cfg=cfg)
    except WeatherError as exc:
        log.warning("天气：城市解析失败 %s：%s", name, exc)
        return _fail("天气服务暂时连不上，稍后再问一次好吗？", reason=exc.code)

    if info is None:
        return _fail(f"没找到「{name}」这个地方，您说的是哪个城市？",
                     reason="city_not_found", need_city=True)

    key = now_key(info.id)
    hit = cache.get(db, key)
    if hit is not None:
        log.debug("天气命中缓存 city=%s", info.full_name)
        return {"ok": True, "message": now_speech(info, hit), "source": "cache",
                "stale": False, "city": info.full_name,
                "observed_at": hit.get("metadata", {}).get("tag", "")}

    cli = client or get_client()
    try:
        data = cli.weather_now(info.lat, info.lon)
    except WeatherError as exc:
        stale = _stale_fallback(db, key)
        if stale is not None:
            log.warning("天气上游失败（%s），用过期缓存兜底 city=%s", exc, info.full_name)
            return {"ok": True, "message": now_speech(info, stale) + "（这是稍早的数据）",
                    "source": "stale", "stale": True, "city": info.full_name,
                    "reason": exc.code}
        log.warning("天气上游失败且无缓存 city=%s：%s", info.full_name, exc)
        return _fail("天气服务暂时连不上，稍后再问一次好吗？", reason=exc.code)

    cache.put(db, key, info.name, data, ttl_minutes=cfg.ttl_minutes)
    return {"ok": True, "message": now_speech(info, data), "source": "api",
            "stale": False, "city": info.full_name}


def get_daily(db: Session, raw_city: str = "", *, days: int = 3,
              device: Any = None, client: QWeatherClient | None = None,
              cfg: WeatherConfig | None = None) -> dict[str, Any]:
    """查每日预报（今天/明天/后天）。语义与 ``get_now`` 完全对称。"""
    cfg = cfg or load_config()
    if not cfg.configured:
        return _fail("天气服务还没开通，请让家里人先在后台配置和风天气的密钥。",
                     reason="not_configured")

    days = max(1, min(3, days))  # 老人最多关心到后天；再远就是瞎猜
    name = _pick_city_name(raw_city, device, cfg)
    if not name:
        return _fail("请问您想查哪个城市的天气？", reason="no_city", need_city=True)

    try:
        info = city_mod.resolve(db, name, client=client, cfg=cfg)
    except WeatherError as exc:
        log.warning("天气：城市解析失败 %s：%s", name, exc)
        return _fail("天气服务暂时连不上，稍后再问一次好吗？", reason=exc.code)

    if info is None:
        return _fail(f"没找到「{name}」这个地方，您说的是哪个城市？",
                     reason="city_not_found", need_city=True)

    key = daily_key(info.id, days)
    hit = cache.get(db, key)
    if hit is not None:
        return {"ok": True, "message": daily_speech(info, hit, days=days),
                "source": "cache", "stale": False, "city": info.full_name}

    cli = client or get_client()
    try:
        data = cli.weather_daily(info.lat, info.lon, days=days)
    except WeatherError as exc:
        stale = _stale_fallback(db, key)
        if stale is not None:
            log.warning("天气上游失败（%s），用过期缓存兜底 city=%s", exc, info.full_name)
            return {"ok": True, "message": daily_speech(info, stale, days=days) + "（这是稍早的预报）",
                    "source": "stale", "stale": True, "city": info.full_name,
                    "reason": exc.code}
        return _fail("天气预报暂时连不上，稍后再问一次好吗？", reason=exc.code)

    cache.put(db, key, info.name, data, ttl_minutes=cfg.ttl_minutes)
    return {"ok": True, "message": daily_speech(info, data, days=days),
            "source": "api", "stale": False, "city": info.full_name}


# ================================================================ 预热
def _geo_from_cache(db: Session, city_name: str) -> dict[str, Any] | None:
    """从 geo 缓存里取坐标（**不打任何 API**）。

    预热为什么不用 ``city_mod.resolve``：那个函数在 geo 缓存过期时会真的去调
    GeoAPI。预热是后台行为，它的原则是「只续已经付过费的东西」，绝不为一次
    后台刷新新增调用。geo 缓存没了就跳过这个城市 —— 等老人下次真问到时自然
    会重建（那时多一次 geo 调用是可接受的，因为确实有人要）。
    """
    row = cache.peek(db, f"geo:{city_mod.normalize(city_name)}")
    if row is None or not row.payload:
        return None
    import json as _json
    try:
        data = _json.loads(row.payload)
    except (ValueError, TypeError):
        return None
    if not (data.get("lat") and data.get("lon")):
        return None
    return data


def warm_recent(db: Session, *, client: QWeatherClient | None = None,
                cfg: WeatherConfig | None = None,
                now: datetime | None = None) -> dict[str, int]:
    """刷新「最近被问过 + 快过期」的城市。返回统计，便于日志/健康端点。

    只刷 ``now:*`` 与 ``daily:*``；``geo:*`` 的 TTL 是 30 天，不在预热范围内
    （它到期说明这个城市一个月没人问了，该让它自然淘汰）。
    """
    cfg = cfg or load_config()
    if not cfg.configured:
        return {"checked": 0, "refreshed": 0, "failed": 0, "skipped": 0}

    now = now or datetime.now(timezone.utc)
    rows = cache.warm_candidates(db, recent_hours=cfg.warm_recent_hours,
                                 ahead_minutes=cfg.warm_ahead_minutes, now=now)
    stats = {"checked": len(rows), "refreshed": 0, "failed": 0, "skipped": 0}
    if not rows:
        return stats

    cli = client or get_client()
    for row in rows:
        key = row.cache_key
        if not (key.startswith("now:") or key.startswith("daily:")):
            stats["skipped"] += 1
            continue

        geo = _geo_from_cache(db, row.city_name)
        if geo is None:
            # 坐标没了：跳过而不是硬查。见 _geo_from_cache 的说明
            stats["skipped"] += 1
            continue

        try:
            if key.startswith("now:"):
                data = cli.weather_now(str(geo["lat"]), str(geo["lon"]))
            else:
                try:
                    days = int(key.rsplit(":", 1)[-1])
                except ValueError:
                    days = 3
                data = cli.weather_daily(str(geo["lat"]), str(geo["lon"]), days=days)
        except WeatherError as exc:
            stats["failed"] += 1
            # 额度类错误要显式告警：它意味着「缓存即将全部失效 + 无法续」
            if exc.is_quota:
                log.error("天气预热遇到额度/频率限制，缓存将无法续期：%s", exc)
            else:
                log.warning("天气预热失败 %s：%s", key, exc)
            continue

        cache.put(db, key, row.city_name, data, ttl_minutes=cfg.ttl_minutes,
                  count_hit=False)
        stats["refreshed"] += 1

    if stats["refreshed"] or stats["failed"]:
        log.info("天气预热：检查 %d，刷新 %d，失败 %d，跳过 %d",
                 stats["checked"], stats["refreshed"], stats["failed"], stats["skipped"])
    return stats
