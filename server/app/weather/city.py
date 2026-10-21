"""城市名 → 经纬度/城市 ID 的解析（GeoAPI，带长缓存）。

为什么天气要两步（先查城市再查天气）
----------------------------------
新版天气接口只吃**经纬度**（``/weather/v1/current/{lat}/{lon}``），而老人说的是
「平顶山」。中间必须有一次 GeoAPI 查询。

但这不该每次都查：城市坐标几十年不变，而老人可能一天问十次天气。所以
``geo:{城市名}`` 单独缓存 30 天（``config.geo_ttl_days``）。代价是首次问一个新
城市要两次往返，之后每次只要一次 —— 且第二次开始就能被预热任务接管。

归一化做到什么程度
----------------
只做**机械**的收敛（空白、尾部行政区后缀、「的天气」这类尾巴），**不做**别名表。
理由：别名表（"帝都"→北京、"煤城"→平顶山）是无穷的，写几条就以为自己覆盖了，
反而给后来人「这里已经处理过了」的错觉。真正的口语理解交给 LLM ——
``get_weather`` 工具的 ``city`` 参数由模型填，模型本来就知道「俺们平顶山」指哪。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from . import cache
from .client import QWeatherClient, WeatherError, get_client
from .config import WeatherConfig, load as load_config

log = logging.getLogger("elder.weather.city")

#: 尾部的行政区后缀。**按长度降序**匹配，否则「自治区」会先被「区」切掉，
#: 剩下「内蒙古自治」这种查不到的怪名字。
_ADMIN_SUFFIXES = ("特别行政区", "自治区", "自治州", "地区", "省", "市", "县", "区", "盟", "旗")
#: 尾部的话术残留（老人说「平顶山的天气」「平顶山那边」）
_TAIL_NOISE = ("的天气", "天气", "那边", "这儿", "那里", "怎么样", "如何", "呢", "啊", "呀")

#: 省级行政区名，用于剥掉「河南省平顶山」这类前缀。
#:
#: 这是一个**封闭**集合（全国 34 个省级区划），不是「帝都→北京」那种开放别名表 ——
#: 后者永远写不全，写几条只会给后来人「已经处理过了」的错觉（见模块 docstring）。
#: 剥掉前缀的价值很实在：和风 geo 的模糊搜索对「河南省平顶山」虽然多半也能命中，
#: 但命中结果可能是「河南」这个省级条目（rank 更高），于是天气查到的是省会。
_PROVINCE_PREFIXES = (
    "内蒙古", "黑龙江", "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁",
    "吉林", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北",
    "湖南", "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海",
    "台湾", "广西", "西藏", "宁夏", "新疆", "香港", "澳门",
)


@dataclass(frozen=True)
class CityInfo:
    """一个可用的地点。``lat``/``lon`` 是和风要求的**字符串**（保留两位小数）。"""

    name: str      # 展示名，如「平顶山」
    id: str        # LocationID，如 101180101
    lat: str
    lon: str
    adm1: str = ""  # 省
    adm2: str = ""  # 市

    @property
    def full_name(self) -> str:
        """给老人念的名字：优先「省+市」避免重名，其次市名。"""
        if self.adm2 and self.adm2 != self.name:
            return f"{self.adm1}{self.adm2}" if self.adm1 else self.adm2
        return self.name


#: 剥前缀后若剩下这些**民族名**，说明剥过头了（"新疆维吾尔自治区" → "维吾尔"）。
#: 宁可保留全名交给和风模糊搜索，也不要切出一个查不到的词。
_ETHNIC_FRAGMENTS = (
    "维吾尔", "壮族", "回族", "藏族", "蒙古", "哈萨克", "柯尔克孜", "朝鲜",
    "土家", "苗族", "彝族", "侗族", "白族", "傣族", "黎族", "畲族", "纳西",
)


def normalize(raw: str) -> str:
    """把口语城市名收敛成查询用的短名。

    目标是「**别切坏**」，不是「完美收敛」—— 因为缓存键最终落在 GeoAPI 返回的
    ``location.id`` 上（见 ``resolve``），名字只是查 ID 的输入。不同说法各查一次
    geo 是可接受的（每个城市 30 天一次），切出一个查不到的词才是事故。

    >>> normalize("平顶山市")
    '平顶山'
    >>> normalize(" 河南省平顶山市 ")
    '平顶山'
    >>> normalize("内蒙古自治区")
    '内蒙古'
    """
    text = (raw or "").strip()
    for noise in _TAIL_NOISE:
        if text.endswith(noise):
            text = text[: -len(noise)]
    text = text.strip().rstrip("，,。.！!？? ")

    # ① 先剥尾部行政后缀。"自治区" 这类长后缀必须先于 "区" 匹配（见 _ADMIN_SUFFIXES
    #    的降序说明），否则 "内蒙古自治区" 会被切成 "内蒙古自治"。
    changed = True
    while changed and len(text) > 2:
        changed = False
        for suffix in _ADMIN_SUFFIXES:
            if text.endswith(suffix) and len(text) - len(suffix) >= 2:
                text = text[: -len(suffix)]
                changed = True
                break

    # ② 再剥省名前缀（"河南平顶山" → "平顶山"）
    for prefix in _PROVINCE_PREFIXES:
        if text.startswith(prefix) and len(text) - len(prefix) >= 2:
            rest = text[len(prefix):]
            if rest.startswith(_ETHNIC_FRAGMENTS):
                break  # 剥过头了，保留全名
            text = rest
            break

    # ③ 清理开头残留的行政区字（"河南省平顶山" 在 ② 之后会剩 "省平顶山"）
    for lead in ("省", "市", "自治区", "特别行政区", "地区"):
        if text.startswith(lead) and len(text) - len(lead) >= 2:
            text = text[len(lead):]
            break

    return text.strip()


def _to_info(item: dict[str, Any]) -> CityInfo:
    """把 GeoAPI 的一条结果转成 CityInfo，经纬度截到两位小数。

    和风要求「最多两位小数」，而 GeoAPI 返回的是 5 位（``39.91755``）。
    直接透传会被上游拒掉或按四舍五入到 1 公里格点 —— 显式 ``round(..., 2)``
    让行为可预期，也避免浮点格式化出 ``113.19000000000001`` 这种脏值。
    """
    def _2(v: Any) -> str:
        try:
            return f"{round(float(v), 2):.2f}"
        except (TypeError, ValueError):
            return ""

    return CityInfo(
        name=str(item.get("name") or ""),
        id=str(item.get("id") or ""),
        lat=_2(item.get("lat")),
        lon=_2(item.get("lon")),
        adm1=str(item.get("adm1") or ""),
        adm2=str(item.get("adm2") or ""),
    )


def resolve(db: Session, raw: str, *, client: QWeatherClient | None = None,
            cfg: WeatherConfig | None = None) -> CityInfo | None:
    """城市名 → CityInfo。查不到返回 None（调用方据此问老人「您说的是哪个城市」）。

    缓存键用**归一化后的名字**：这样「平顶山」「平顶山市」「平顶山的天气」
    共用同一条缓存，不会因为说法不同各存一份、各打一次 API。
    """
    cfg = cfg or load_config()
    name = normalize(raw)
    if not name:
        return None

    key = f"geo:{name}"
    hit = cache.get(db, key)
    if hit is not None:
        return _to_info(hit) if hit.get("id") else None

    cli = client or get_client()
    try:
        items = cli.city_lookup(name)
    except WeatherError as exc:
        # 认证/额度问题必须冒出去让上层说「天气服务暂时不可用」，
        # 不能当成「查不到这个城市」—— 那会让老人反复重说城市名。
        if exc.is_auth or exc.is_quota or exc.code == "not_configured":
            raise
        log.warning("城市搜索失败 city=%s：%s", name, exc)
        return None

    if not items:
        log.info("城市搜索无结果：%s", name)
        return None

    best = items[0]
    cache.put(db, key, name, best, ttl_minutes=cfg.geo_ttl_days * 24 * 60)
    return _to_info(best)
