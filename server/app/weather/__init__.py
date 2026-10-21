"""和风天气子系统（按需缓存 + 定时预热）。

对外只用这几个入口::

    from app.weather import load_config, get_now, get_daily, warm_recent

设计说明散在各文件头部，这里只放一张索引：

* ``config.py``   —— 环境变量、私钥定位（在项目目录**之外**）、TTL
* ``jwt_auth.py`` —— Ed25519 JWT 签发与 15 分钟轮换
* ``client.py``   —— 标准库 urllib 调用和风（gzip、401 重签、错误码）
* ``cache.py``    —— SQLite 单表缓存（过期行保留，供预热判断需求）
* ``city.py``     —— 城市名归一化 + GeoAPI（缓存 30 天）
* ``speak.py``    —— 原始 JSON → 老人听得懂的一句话
* ``service.py``  —— 编排：缓存优先 → 上游 → 过期兜底 → 预热
"""
from __future__ import annotations

from .config import WeatherConfig, load as load_config
from .service import get_daily, get_now, warm_recent

__all__ = ["WeatherConfig", "load_config", "get_now", "get_daily", "warm_recent"]
