"""把和风的原始 JSON 翻成**老人听得懂**的一句话。

为什么要有这一层（而不是把 JSON 直接喂给 LLM）
--------------------------------------------
LLM 完全能读 JSON，但它会**忠实复述字段**：「当前温度 31.71 摄氏度，体感温度
33.64 摄氏度，相对湿度百分之六十九，西南风 3 级」。对老人来说这是噪音 ——
他要的是「今天热，三十二度，多云」。

所以这里做三件事：
1. **取整**：31.71 → 32 度。小数点后一位对穿衣决策毫无信息量；
2. **翻译成人话**：湿度 0.69 不提数字，只在极端时说「有点潮」「太干」；
3. **给一条可执行建议**：带伞 / 加衣 / 别晒。且**最多一条** —— 老人记不住
   三条建议，等于没有建议。

LLM 拿到这段文字后基本可以照念，也可以自己调整语气（它有人格设定）。

⚠️ 一个刻意的取舍：**不编造**。上游没给的字段（比如没有降水概率）就不提，
不用「可能会下雨」这种模糊话凑长度。老人据此决定带不带伞，模糊话是害人。
"""
from __future__ import annotations

from typing import Any

from .city import CityInfo

#: 风向方位码 → 中文。和风返回的是 n/nne/ne… 这些小写缩写
_COMPASS = {
    "n": "北", "nne": "东北偏北", "ne": "东北", "ene": "东北偏东",
    "e": "东", "ese": "东南偏东", "se": "东南", "sse": "东南偏南",
    "s": "南", "ssw": "西南偏南", "sw": "西南", "wsw": "西南偏西",
    "w": "西", "wnw": "西北偏西", "nw": "西北", "nnw": "西北偏北",
    "vrb": "风向不定", "none": "",
}


def _num(obj: Any, default: float | None = None) -> float | None:
    """从 ``{"value": 31.71, "unit": "°C"}`` 里取值；取不到返回 default。"""
    if isinstance(obj, dict):
        v = obj.get("value")
    else:
        v = obj
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _temp_phrase(t: float | None) -> str:
    return f"{round(t)} 度" if t is not None else ""


def _umbrella(cond: str, pop: float | None, ptype: str = "") -> bool:
    """要不要提醒带伞。

    判据用「天气现象文字命中雨雪词」**或**「降水概率 ≥ 50%」——
    单看概率会漏掉「阴转小雨」这种概率不高但确实要下的，单看文字会漏掉
    「雷阵雨概率 80% 但文字写多云」这种。两者取并。
    """
    if any(w in cond for w in ("雨", "雪", "雹")):
        return True
    if ptype in ("rain", "snow", "ice", "mixed"):
        return True
    return pop is not None and pop >= 0.5


def now_speech(city: CityInfo, data: dict[str, Any]) -> str:
    """实时天气 → 一句口语。``data`` 是 ``/weather/v1/current`` 的原始响应。"""
    cond_obj = data.get("condition") or {}
    cond = str(cond_obj.get("text") or "").strip()
    temp = _num(data.get("temperature"))
    feels = _num(data.get("feelsLike"))
    humidity = data.get("humidity")
    wind = data.get("wind") or {}
    scale = wind.get("scale")
    compass = _COMPASS.get(str((wind.get("direction") or {}).get("compass") or ""), "")
    precip = data.get("precipitation") or {}

    parts: list[str] = []
    if city.full_name:
        parts.append(f"{city.full_name}现在")
    if cond:
        parts.append(cond)
    if temp is not None:
        parts.append(f"{round(temp)} 度")
    head = "，".join(parts) if parts else "现在天气"

    extras: list[str] = []
    # 体感与实温差 3 度以上才提 —— 差 1 度对老人没有决策价值
    if feels is not None and temp is not None and abs(feels - temp) >= 3:
        extras.append(f"体感像 {round(feels)} 度")
    # 风力 5 级以上才提（4 级以下老人没感觉）
    try:
        if scale is not None and int(scale) >= 5:
            extras.append(f"{compass or ''}{int(scale)} 级风")
    except (TypeError, ValueError):
        pass
    # 湿度只在极端时提，且不给数字
    try:
        h = float(humidity) if humidity is not None else None
        if h is not None:
            if h >= 0.85:
                extras.append("有点潮")
            elif h <= 0.25:
                extras.append("很干燥")
    except (TypeError, ValueError):
        pass

    advice = ""
    ptype = str(precip.get("type") or "")
    if _umbrella(cond, None, ptype):
        advice = "出门记得带伞。"
    elif temp is not None and temp >= 33:
        advice = "天热，尽量别在中午出门。"
    elif temp is not None and temp <= 5:
        advice = "天冷，出门多穿点。"

    tail = ("，".join(extras) + "。") if extras else "。"
    return f"{head}{tail}{advice}"


def daily_speech(city: CityInfo, data: dict[str, Any], *, days: int = 3) -> str:
    """每日预报 → 一段口语（今天/明天/后天各一句，最多 ``days`` 天）。

    ⚠️ 刻意**按数组下标**贴「今天/明天/后天」，不解析 ``forecastStartTime``：
    该字段是 UTC 且表示当地日期的起点，跨时区换算极易错一天（把明天说成今天，
    老人会按错的天气穿衣）。和风保证 ``days[0]`` 是查询地当天，下标是可靠契约，
    自己换算不是。
    """
    items = list(data.get("days") or [])[:max(1, days)]
    if not items:
        return ""

    labels = ("今天", "明天", "后天")
    sentences: list[str] = []
    for i, day in enumerate(items):
        label = labels[i] if i < len(labels) else f"第 {i + 1} 天"
        daypart = day.get("daytime") or {}
        nightpart = day.get("nighttime") or {}
        cond = str((daypart.get("condition") or {}).get("text")
                   or (nightpart.get("condition") or {}).get("text") or "").strip()
        tmax = _num(day.get("temperatureMax"))
        tmin = _num(day.get("temperatureMin"))

        piece = label
        if cond:
            piece += cond
        if tmax is not None and tmin is not None:
            piece += f"，{round(tmin)} 到 {round(tmax)} 度"
        elif tmax is not None:
            piece += f"，最高 {round(tmax)} 度"

        # 降水概率取白天/夜间较大者：老人白天出门，但夜里下雨也要收衣服
        pops = []
        for part in (daypart, nightpart):
            p = (part.get("precipitation") or {}).get("probability")
            if p is not None:
                try:
                    pops.append(float(p))
                except (TypeError, ValueError):
                    pass
        pop = max(pops) if pops else None

        ptype = str((daypart.get("precipitation") or {}).get("type") or "")
        if _umbrella(cond, pop, ptype):
            piece += "，有雨雪，记得带伞" if ("雪" in cond or ptype == "snow") else "，可能有雨，带把伞"
        sentences.append(piece)

    head = f"{city.full_name}的天气：" if city.full_name else "天气："
    return head + "；".join(sentences) + "。"
