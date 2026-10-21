"""按客户端 IP 所在国家/地区判定**展示时区**。

为什么需要这个模块（2026-10-21 用户纠偏）
----------------------------------------
一度把"北京时间"硬编码到展示层，理由是"几个节点时区不统一"。那是错的：

* 家属在海外用手机看老人的服药记录，显示北京时间等于要他先做心算；
* 但反过来直接用 ``new Date(s).getHours()``（设备时区）同样不行 ——
  老人机/家属手机的时区**可能是错的**：出厂默认 UTC、被手动改过、
  英文系统没设对。那时显示出来的"本地时间"也不是他眼前的钟点。

用户的定调是：**设备时区判定依赖 IP 所在国家/地区**。
所以这里的顺序是「先定国家，再定该国的时区」：

================  ================================================  ==========
层               判据                                              source
================  ================================================  ==========
1 可信反代头      ``CF-IPCountry`` 之类，**仅当对端在可信代理名单**    ``proxy``
2 本地 mmdb       ``ELDER_GEOIP_DB`` + 装了 ``maxminddb``            ``geoip``
3 网络查询        ``ELDER_GEO_API``（默认 ipwho.is，可设为空关闭）   ``api``
4 设备自报        ``X-Client-Timezone``，**校验是合法 IANA 名**      ``device``
5 兜底            ``config.TZ``（默认 Asia/Shanghai）                ``default``
================  ================================================  ==========

层 1-3 只要拿到国家，就**不信设备自报**了 —— 那正是"手机时区设错"要纠正的
情形。唯一的例外是**跨多时区的国家**（美国/加拿大/澳大利亚/俄罗斯/巴西/
墨西哥/印尼等）：那种情况下国家定不了时区，而设备自报的时区如果**落在该国
的合法时区集合里**，就用它（``source`` 变成 ``api+device``）。这不是向设备
妥协，而是"IP 定国家、设备定经度"的合理分工 —— 一个 IP 在洛杉矶的人，
``America/Los_Angeles`` 比"美国东部时间"对他有用得多。

``source`` 字段**必须如实反映真实来源**，并且对外可见（``GET /api/geo/timezone``
会返回它）。理由：这个链路一旦静默退化，表现是"所有人都是北京时间"，
而它跟"配置全部正确"长得一模一样 —— 没有 ``source`` 就只能靠猜。

⚠️ 已知边界（写在这里而不是靠读代码发现）
----------------------------------------
1. **多时区国家只取人口最多的那一个**作为主时区：US→纽约、CA→多伦多、
   AU→悉尼、RU→莫斯科、BR→圣保罗、MX→墨西哥城、ID→雅加达。真实的
   洛杉矶/温哥华/珀斯用户会被判成东部时间，靠设备自报的细化层救回来；
   设备时区也错的话，只能吃到这个近似。
2. **内网/回环地址没有国家**：``192.168.*`` / ``10.*`` / ``127.0.0.1``
   / ``::1`` 一律直接跳到层 4，**不发网络查询**（查了也没用，还把内网
   地址泄露给第三方）。本地开发时拿到的是开发机自己的时区，这是对的。
3. **IP 归属地不等于人所在时区**：出差、VPN、企业专线出口都会让它偏。
   本模块的定位是"在设备自己不可靠时给一个更可靠的默认值"，不是精确定位。
4. 网络查询**结果为空也算缓存**（短 TTL），否则一个查不到的网段会让每次
   启动都白等 800ms。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config
from .ratelimit import is_trusted_proxy

log = logging.getLogger("elder.geo")

# 网络查询走自己的空代理 handler：本机装有系统代理时，urllib 会把它带上，
# 而这里要连的是一个明确的、按 IP 拼接的地址，走代理只会多一层失败点。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ============================================================ 国家 → 时区
# 多时区国家：**元组第一个元素是主时区**（判定不出来时用它）。
# 顺序不是随便排的 —— 第一个位置直接决定"设备时区也查不到时"给谁。
_MULTI: dict[str, tuple[str, ...]] = {
    "US": ("America/New_York", "America/Chicago", "America/Denver",
           "America/Phoenix", "America/Los_Angeles", "America/Anchorage",
           "Pacific/Honolulu"),
    "CA": ("America/Toronto", "America/Vancouver", "America/Edmonton",
           "America/Winnipeg", "America/Halifax", "America/St_Johns"),
    "AU": ("Australia/Sydney", "Australia/Melbourne", "Australia/Brisbane",
           "Australia/Adelaide", "Australia/Perth", "Australia/Darwin",
           "Australia/Hobart"),
    "RU": ("Europe/Moscow", "Asia/Yekaterinburg", "Asia/Novosibirsk",
           "Asia/Krasnoyarsk", "Asia/Irkutsk", "Asia/Yakutsk",
           "Asia/Vladivostok", "Europe/Kaliningrad", "Europe/Samara"),
    "BR": ("America/Sao_Paulo", "America/Manaus", "America/Fortaleza",
           "America/Recife", "America/Cuiaba", "America/Belem"),
    "MX": ("America/Mexico_City", "America/Tijuana", "America/Monterrey",
           "America/Cancun", "America/Chihuahua", "America/Hermosillo",
           "America/Mazatlan", "America/Merida"),
    "ID": ("Asia/Jakarta", "Asia/Makassar", "Asia/Jayapura"),
    "KZ": ("Asia/Almaty", "Asia/Aqtobe", "Asia/Atyrau", "Asia/Oral",
           "Asia/Qostanay", "Asia/Qyzylorda"),
    "MN": ("Asia/Ulaanbaatar", "Asia/Hovd"),
    "CL": ("America/Santiago", "Pacific/Easter"),
    "EC": ("America/Guayaquil", "Pacific/Galapagos"),
    "ES": ("Europe/Madrid", "Atlantic/Canary"),
    "PT": ("Europe/Lisbon", "Atlantic/Azores", "Atlantic/Madeira"),
    "DK": ("Europe/Copenhagen", "Atlantic/Faroe", "America/Nuuk"),
    "NZ": ("Pacific/Auckland", "Pacific/Chatham"),
    "PS": ("Asia/Gaza", "Asia/Hebron"),
    "CD": ("Africa/Kinshasa", "Africa/Lubumbashi"),
    "AR": ("America/Argentina/Buenos_Aires", "America/Argentina/Cordoba",
           "America/Argentina/Mendoza", "America/Argentina/Salta"),
    "UA": ("Europe/Kyiv", "Europe/Simferopol"),
}

# 单时区国家（含本产品的主要用户群 CN/HK/MO/TW）。
# 这张表的价值在于"CN 只有一个时区"这件事本身就否掉了设备自报 ——
# 一个把时区设成 UTC 的国产老人机，IP 在 CN，就该显示北京时间。
_SINGLE: dict[str, str] = {
    # ---- 中国（一个中国原则：港澳台单列，均属中国）----
    "CN": "Asia/Shanghai", "HK": "Asia/Hong_Kong", "MO": "Asia/Macau",
    "TW": "Asia/Taipei",
    # ---- 亚洲 ----
    "JP": "Asia/Tokyo", "KR": "Asia/Seoul", "KP": "Asia/Pyongyang",
    "SG": "Asia/Singapore", "MY": "Asia/Kuala_Lumpur", "TH": "Asia/Bangkok",
    "VN": "Asia/Ho_Chi_Minh", "PH": "Asia/Manila", "IN": "Asia/Kolkata",
    "PK": "Asia/Karachi", "BD": "Asia/Dhaka", "LK": "Asia/Colombo",
    "NP": "Asia/Kathmandu", "MM": "Asia/Yangon", "KH": "Asia/Phnom_Penh",
    "LA": "Asia/Vientiane", "BN": "Asia/Brunei",
    "KG": "Asia/Bishkek", "TJ": "Asia/Dushanbe", "TM": "Asia/Ashgabat",
    "UZ": "Asia/Tashkent", "AF": "Asia/Kabul", "IR": "Asia/Tehran",
    "IQ": "Asia/Baghdad", "SA": "Asia/Riyadh", "AE": "Asia/Dubai",
    "QA": "Asia/Qatar", "KW": "Asia/Kuwait", "BH": "Asia/Bahrain",
    "OM": "Asia/Muscat", "YE": "Asia/Aden", "JO": "Asia/Amman",
    "LB": "Asia/Beirut", "SY": "Asia/Damascus", "IL": "Asia/Jerusalem",
    "TR": "Europe/Istanbul", "CY": "Asia/Nicosia", "GE": "Asia/Tbilisi",
    "AM": "Asia/Yerevan", "AZ": "Asia/Baku", "BT": "Asia/Thimphu",
    "MV": "Indian/Maldives",
    # ---- 欧洲 ----
    "GB": "Europe/London", "IE": "Europe/Dublin", "FR": "Europe/Paris",
    "DE": "Europe/Berlin", "IT": "Europe/Rome", "NL": "Europe/Amsterdam",
    "BE": "Europe/Brussels", "LU": "Europe/Luxembourg", "CH": "Europe/Zurich",
    "AT": "Europe/Vienna", "SE": "Europe/Stockholm", "NO": "Europe/Oslo",
    "FI": "Europe/Helsinki", "IS": "Atlantic/Reykjavik", "PL": "Europe/Warsaw",
    "CZ": "Europe/Prague", "SK": "Europe/Bratislava", "HU": "Europe/Budapest",
    "RO": "Europe/Bucharest", "BG": "Europe/Sofia", "GR": "Europe/Athens",
    "HR": "Europe/Zagreb", "SI": "Europe/Ljubljana", "RS": "Europe/Belgrade",
    "BA": "Europe/Sarajevo", "ME": "Europe/Podgorica", "MK": "Europe/Skopje",
    "AL": "Europe/Tirane", "EE": "Europe/Tallinn", "LV": "Europe/Riga",
    "LT": "Europe/Vilnius", "BY": "Europe/Minsk", "MD": "Europe/Chisinau",
    "MT": "Europe/Malta", "MC": "Europe/Monaco", "SM": "Europe/San_Marino",
    "AD": "Europe/Andorra", "LI": "Europe/Vaduz", "VA": "Europe/Vatican",
    # ---- 非洲 ----
    "ZA": "Africa/Johannesburg", "EG": "Africa/Cairo", "NG": "Africa/Lagos",
    "KE": "Africa/Nairobi", "ET": "Africa/Addis_Ababa", "GH": "Africa/Accra",
    "TZ": "Africa/Dar_es_Salaam", "UG": "Africa/Kampala", "MA": "Africa/Casablanca",
    "DZ": "Africa/Algiers", "TN": "Africa/Tunis", "LY": "Africa/Tripoli",
    "SD": "Africa/Khartoum", "SS": "Africa/Juba", "SN": "Africa/Dakar",
    "CI": "Africa/Abidjan", "CM": "Africa/Douala", "AO": "Africa/Luanda",
    "MZ": "Africa/Maputo", "ZW": "Africa/Harare", "ZM": "Africa/Lusaka",
    "BW": "Africa/Gaborone", "NA": "Africa/Windhoek", "MU": "Indian/Mauritius",
    "MG": "Indian/Antananarivo", "RW": "Africa/Kigali", "SO": "Africa/Mogadishu",
    "ML": "Africa/Bamako", "BF": "Africa/Ouagadougou", "NE": "Africa/Niamey",
    "TD": "Africa/Ndjamena", "CF": "Africa/Bangui", "CG": "Africa/Brazzaville",
    "GA": "Africa/Libreville", "GQ": "Africa/Malabo", "BJ": "Africa/Porto-Novo",
    "TG": "Africa/Lome", "GN": "Africa/Conakry", "SL": "Africa/Freetown",
    "LR": "Africa/Monrovia", "GM": "Africa/Banjul", "GW": "Africa/Bissau",
    "MR": "Africa/Nouakchott", "ER": "Africa/Asmara", "DJ": "Africa/Djibouti",
    "KM": "Indian/Comoro", "SC": "Indian/Mahe", "CV": "Atlantic/Cape_Verde",
    "ST": "Africa/Sao_Tome", "LS": "Africa/Maseru", "SZ": "Africa/Mbabane",
    "MW": "Africa/Blantyre", "BI": "Africa/Bujumbura",
    # ---- 美洲 ----
    "CO": "America/Bogota", "PE": "America/Lima", "VE": "America/Caracas",
    "UY": "America/Montevideo", "PY": "America/Asuncion",
    "BO": "America/La_Paz", "CR": "America/Costa_Rica",
    "PA": "America/Panama", "GT": "America/Guatemala",
    "SV": "America/El_Salvador", "HN": "America/Tegucigalpa",
    "NI": "America/Managua", "BZ": "America/Belize", "CU": "America/Havana",
    "DO": "America/Santo_Domingo", "HT": "America/Port-au-Prince",
    "JM": "America/Jamaica", "TT": "America/Port_of_Spain",
    "BS": "America/Nassau", "BB": "America/Barbados", "PR": "America/Puerto_Rico",
    "GY": "America/Guyana", "SR": "America/Paramaribo", "AG": "America/Antigua",
    "DM": "America/Dominica", "GD": "America/Grenada", "LC": "America/St_Lucia",
    "VC": "America/St_Vincent", "KN": "America/St_Kitts",
    # ---- 大洋洲 ----
    "FJ": "Pacific/Fiji", "PG": "Pacific/Port_Moresby", "SB": "Pacific/Guadalcanal",
    "VU": "Pacific/Efate", "WS": "Pacific/Apia", "TO": "Pacific/Tongatapu",
    "KI": "Pacific/Tarawa", "MH": "Pacific/Majuro", "FM": "Pacific/Chuuk",
    "PW": "Pacific/Palau", "NR": "Pacific/Nauru", "TV": "Pacific/Funafuti",
}

#: 国家代码（ISO 3166-1 alpha-2 大写）→ 该国可用时区，**主时区在第一位**。
#: 多时区国家在 :data:`_MULTI` 里，其余都是一国一区。
COUNTRY_ZONES: dict[str, tuple[str, ...]] = {
    **{k: (v,) for k, v in _SINGLE.items()},
    **_MULTI,
}

#: 英文国名 → 代码。有些服务（以及 ``CF-IPCountry`` 的少数实现）给的是国名
#: 而不是 2 位码；只收常见的那批，查不到就走下一层，不假装认识。
COUNTRY_NAMES: dict[str, str] = {
    "china": "CN", "hong kong": "HK", "macao": "MO", "macau": "MO",
    "taiwan": "TW", "japan": "JP", "korea": "KR", "south korea": "KR",
    "singapore": "SG", "malaysia": "MY", "thailand": "TH", "vietnam": "VN",
    "philippines": "PH", "india": "IN", "indonesia": "ID", "australia": "AU",
    "new zealand": "NZ", "united states": "US", "usa": "US",
    "united kingdom": "GB", "uk": "GB", "canada": "CA", "germany": "DE",
    "france": "FR", "italy": "IT", "spain": "ES", "portugal": "PT",
    "netherlands": "NL", "belgium": "BE", "switzerland": "CH",
    "austria": "AT", "sweden": "SE", "norway": "NO", "denmark": "DK",
    "finland": "FI", "ireland": "IE", "poland": "PL", "russia": "RU",
    "brazil": "BR", "mexico": "MX", "argentina": "AR", "chile": "CL",
    "south africa": "ZA", "egypt": "EG", "turkey": "TR",
    "united arab emirates": "AE", "saudi arabia": "SA", "israel": "IL",
}

# 「查过了，没有」的**国家码哨兵**。缓存里用它区分两类空结果：
#   * ``""``     未缓存 —— 下次还要查
#   * ``"-"``    查过但无结果 —— 命中它就直接走下一层，别重复付超时
_NO_COUNTRY = "-"


def normalize_country(raw: Any) -> str:
    """把各种形状的国家标识收敛成 2 位大写代码；认不出返回空串。

    接受：``"CN"`` / ``"cn"`` / ``" China "`` / ``{"country_code": "CN"}``。
    **不认识就返回空串**，绝不做"猜一个相近的"这种推断 —— 猜错的时区比
    "没查到"更难排查，因为界面上看起来一切正常。
    """
    if raw is None:
        return ""
    if isinstance(raw, Mapping):
        for key in ("country_code", "countryCode", "country_code2", "code"):
            got = normalize_country(raw.get(key))
            if got:
                return got
        return normalize_country(raw.get("country"))
    text = str(raw).strip().strip('"').strip()
    if not text:
        return ""
    if len(text) == 2 and text.isalpha():
        code = text.upper()
        # XX / T1 是 Cloudflare 表达"未知"和"Tor"的取值，不是国家
        return "" if code in ("XX", "T1", "ZZ") else code
    return COUNTRY_NAMES.get(text.lower(), "")


# ============================================================ IP 归一
def normalize_ip(raw: Any) -> str:
    """把 IP 收敛成规范字符串；非法返回空串。

    ``::ffff:1.2.3.4``（IPv4-mapped，反代常见）要还原成 ``1.2.3.4``，
    否则 :func:`is_private` 会用 IPv6 的规则去看一个 IPv4 地址。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    # 去掉端口（"1.2.3.4:5678" / "[::1]:443"）
    if text.startswith("["):
        text = text[1:text.find("]")] if "]" in text else text[1:]
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return ""
    mapped = getattr(addr, "ipv4_mapped", None)
    return str(mapped) if mapped else str(addr)


def is_private(ip: str) -> bool:
    """内网 / 回环 / 链路本地 / 保留地址 —— 这些**没有国家**，也别拿去查。"""
    if not ip:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def cache_key(ip: str) -> str:
    """缓存键：IPv4 取 /24、IPv6 取 /48（同一出口网段归属地几乎总是一样）。"""
    if not ip:
        return ""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    prefix = 24 if addr.version == 4 else 48
    try:
        return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))
    except ValueError:
        return ip


# ============================================================ 时区
_TZ_CACHE: dict[str, bool] = {}


def valid_timezone(name: Any) -> str:
    """合法 IANA 名则原样返回，否则返回空串。

    为什么要校验而不是直接用：``X-Client-Timezone`` 是**客户端可控输入**。
    不校验的话，``Intl.DateTimeFormat({timeZone})`` 在前端会抛
    ``RangeError``，而後端的 ``ZoneInfo()`` 会抛 ``ZoneInfoNotFoundError``
    —— 一个请求头就能把接口打成 500。
    """
    text = str(name or "").strip()
    if not text or len(text) > 64:
        return ""
    # 只允许 A-Za-z0-9_+-/ ，挡住 "Asia/Shanghai\n" 这类注入与超长垃圾
    if not all(c.isalnum() or c in "_-+/" for c in text):
        return ""
    if text in _TZ_CACHE:
        return text if _TZ_CACHE[text] else ""
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        _TZ_CACHE[text] = False
        return ""
    _TZ_CACHE[text] = True
    return text


def offset_minutes(name: str, at: datetime | None = None) -> int:
    """某时区**此刻**相对 UTC 的偏移（分钟，东为正）。

    取"此刻"而不是固定值：南半球与欧美有夏令时，写死 +480 会在澳洲/美国
    半年里差一小时，而那正是这个模块存在的理由。

    ⚠️ ``at`` 必须**先换算到目标时区**再取 ``utcoffset()``。直接在传入的
    datetime 上取（它可能带着 UTC 的 tzinfo）得到的是**那个时区**的偏移，
    不是目标时区的 —— 症状是该国永远是 0 分钟偏移，而看起来毫无异常。
    """
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return 0
    if at is None:
        moment = datetime.now(zone)
    elif at.tzinfo is None:
        moment = at.replace(tzinfo=zone)
    else:
        moment = at.astimezone(zone)
    delta = moment.utcoffset()
    return int(delta.total_seconds() // 60) if delta else 0


# ============================================================ 三层取国家
def country_from_proxy(peer_ip: str, headers: Mapping[str, str]) -> str:
    """层 1：可信反代声明的国家头。

    **只有 TCP 对端本身在 ``ELDER_TRUSTED_PROXY_IPS`` 里才采信**。这个头是
    请求方可以随便写的（跟 ``X-Forwarded-For`` 完全同类），盲信等于让任何人
    把头一改就把时区改成任意值 —— 那会让"按国家判定"变成"按用户心情判定"。
    """
    name = config.GEO_COUNTRY_HEADER
    if not name or not headers:
        return ""
    if not is_trusted_proxy(peer_ip):
        return ""
    return normalize_country(headers.get(name) or headers.get(name.replace("-", "_")))


def country_from_mmdb(ip: str) -> str:
    """层 2：本地 MaxMind 库。**可选依赖**，缺了就返回空串（不报错）。"""
    path = config.GEOIP_DB
    if not path or not ip:
        return ""
    reader = _mmdb_reader(path)
    if reader is None:
        return ""
    try:
        rec = reader.get(ip) or {}
    except Exception:  # noqa: BLE001  库文件损坏不该让接口 500
        log.warning("geo: 读取 mmdb 失败（%s），本次跳过该层", path, exc_info=True)
        return ""
    return normalize_country(rec.get("country") or rec.get("registered_country")
                             or rec.get("represented_country")) or ""


_MMDB: dict[str, Any] = {}


def _mmdb_reader(path: str):
    """按路径缓存 reader。导入/打开失败只记一次 WARNING，之后静默跳过。"""
    if path in _MMDB:
        return _MMDB[path]
    _MMDB[path] = None  # 先占位，避免每次请求都重试导入
    try:
        import maxminddb  # noqa: PLC0415  可选依赖，故意延迟导入
    except ImportError:
        log.warning(
            "geo: 配了 ELDER_GEOIP_DB 但没装 maxminddb，本地库这一层不可用。"
            "装它（pip install maxminddb）或改用 ELDER_GEO_API。")
        return None
    if not path_exists(path):
        log.warning("geo: ELDER_GEOIP_DB 指向的文件不存在：%s", path)
        return None
    try:
        _MMDB[path] = maxminddb.open_database(path)
    except Exception:  # noqa: BLE001
        log.warning("geo: 打不开 mmdb 库：%s", path, exc_info=True)
        return None
    log.info("geo: 已加载本地 IP 库 %s", path)
    return _MMDB[path]


def path_exists(path: str) -> bool:
    """``os.path.exists`` 的薄封装（单独拎出来便于测试打桩）。"""
    import os  # noqa: PLC0415
    return os.path.exists(path)


class _Cache:
    """网段 → (过期时刻, 国家码或哨兵)。**有上限**，超限整体清空。

    为什么不清空就是 OOM 路径：键来自请求（客户端 IP 的网段），不做上限
    就意味着"发一堆不同网段的请求"能把内存顶爆，而这条路径**不需要任何凭证**。
    """

    def __init__(self, ttl: float, max_entries: int) -> None:
        self.ttl = ttl
        self.max = max(1, max_entries)
        self._data: dict[str, tuple[float, str]] = {}

    def get(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        expire, value = item
        if expire < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def put(self, key: str, value: str, ttl: float | None = None) -> None:
        if not key:
            return
        if len(self._data) >= self.max:
            self._data.clear()
        self._data[key] = (time.monotonic() + (self.ttl if ttl is None else ttl), value)

    def clear(self) -> None:
        self._data.clear()


_API_CACHE = _Cache(config.GEO_CACHE_TTL_SEC, config.GEO_CACHE_MAX)


def country_from_api(ip: str, *, opener=None) -> str:
    """层 3：网络查询。**空 ``ELDER_GEO_API`` = 关闭**，内网地址不查。"""
    template = config.GEO_API_URL
    if not template or not ip or is_private(ip):
        return ""
    key = cache_key(ip)
    hit = _API_CACHE.get(key)
    if hit is not None:
        return "" if hit == _NO_COUNTRY else hit

    # 占位符两种写法等价：``{ip}`` 与 ``@IP@``。
    # 为什么容忍两种：docker-compose 的 ``${VAR:-默认值}`` 插值遇到 ``{ip}``
    # 里的 ``}`` 会**提前截断**，模板被切成半截 URL，而截断后的值看起来仍像
    # 一个地址 —— 症状是"永远查不到国家"。所以 compose 里用 ``@IP@``。
    url = template.replace("{ip}", ip).replace("@IP@", ip)
    country = ""
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "elder-geo/1",
        })
        with (opener or _OPENER).open(req, timeout=config.GEO_API_TIMEOUT_SEC) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
        country = _parse_country_body(body)
    except Exception as exc:  # noqa: BLE001  任何失败都只是"没查到"
        # 用 debug 而不是 warning：这个接口在 App 每次启动时都会被调，
        # 网络不通的部署会每秒刷一条告警，而它对业务毫无影响。
        log.debug("geo: 查询 %s 失败：%s: %s", url, type(exc).__name__, exc)

    # 失败用**短 TTL**：一次抖动不该让整片网段连续 6 小时吃兜底值，
    # 那会让"今天所有人的时区都不对"变成一个看不出原因的现象。
    _API_CACHE.put(key, country or _NO_COUNTRY,
                   ttl=None if country else config.GEO_FAIL_TTL_SEC)
    return country


def _parse_country_body(body: str) -> str:
    """从响应体里抠国家码。

    支持三种形状（覆盖 ipwho.is / ipapi.co / ip-api.com / api.country.is）：
      * ``{"country_code": "CN"}`` / ``{"countryCode": "CN"}`` / ``{"country": "China"}``
      * 纯文本 ``"CN"``
    **``success: false`` 一律当没查到** —— 那几家失败时也返回 200，
    并把错误塞在 body 里，不判这个就会把错误响应当成"国家未知"缓存下来。
    """
    text = (body or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except ValueError:
        return normalize_country(text[:32])
    if not isinstance(data, Mapping):
        return ""
    if data.get("success") is False or data.get("error"):
        return ""
    return normalize_country(data)


# ============================================================ 结论
@dataclass(frozen=True)
class Guess:
    """一次判定的结果。``source`` 是**排障的主要抓手**，必须如实填。"""

    timezone: str
    offset_minutes: int
    country: str
    source: str
    ip: str
    #: 逐层的尝试记录（``{"proxy": "", "geoip": "", "api": "CN"}``），
    #: 只在 ``verbose`` 请求时返回 —— 排查"为什么是北京时间"时，
    #: 光看 ``source`` 只能知道最终用了谁，不知道前几层为什么空。
    detail: dict[str, str] = field(default_factory=dict)

    def as_dict(self, *, verbose: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "timezone": self.timezone,
            "offset_minutes": self.offset_minutes,
            "country": self.country,
            "source": self.source,
            "ip": self.ip,
        }
        if verbose:
            out["detail"] = dict(self.detail)
        return out


def zones_for(country: str) -> tuple[str, ...]:
    return COUNTRY_ZONES.get(country, ())


def pick_timezone(country: str, device_tz: str) -> tuple[str, str]:
    """国家 + 设备自报 → ``(时区名, 细化来源)``，细化来源为空表示"没用设备"。

    规则（注意方向，写反了就是"设备说了算"，那正是要消灭的失效形态）：

    * 国家已知但**只有一个时区** → 一律用国家的，**无视设备自报**；
    * 国家已知且跨多时区     → 设备自报的时区若在该国合法集合里就用它；
    * 国家未知               → 用设备自报（若有），否则兜底。
    """
    zones = zones_for(country)
    if zones:
        if len(zones) > 1 and device_tz and device_tz in zones:
            return device_tz, "device"
        return zones[0], ""
    if device_tz:
        return device_tz, "device"
    return "", ""


def resolve(ip: Any, headers: Mapping[str, str] | None = None,
            peer_ip: Any = None, device_tz: Any = None) -> Guess:
    """把「IP + 请求头 + 设备自报」合成一个结论。

    :param ip:        真实客户端 IP（调用方负责用 ``ratelimit.client_ip`` 解析 XFF）
    :param headers:   请求头（用于可信反代的 ``CF-IPCountry``）
    :param peer_ip:   TCP 对端地址。**不给就等于 ``ip``** —— 直连部署下两者相同，
                      反代后面则不同，而"能不能信国家头"看的是对端。
    :param device_tz: 设备自报的 IANA 时区（``X-Client-Timezone``）。
    """
    client = normalize_ip(ip)
    peer = normalize_ip(peer_ip) or client
    tz_hint = valid_timezone(device_tz)

    country = ""
    source = ""
    detail: dict[str, Any] = {"ip": client, "private_ip": is_private(client)}
    if not is_private(client):
        # 逐层记账：前三层都试，detail 里留每层的原始结论。
        # 排查"为什么是北京时间"时，只看 source 只能知道最终用了谁，
        # 不知道前几层为什么是空的（是没配、查不到、还是被拦了）。
        country = country_from_proxy(peer, headers or {})
        detail["proxy"] = country
        if country:
            source = "proxy"
        if not country:
            country = country_from_mmdb(client)
            detail["geoip"] = country
            if country:
                source = "geoip"
        if not country:
            country = country_from_api(client)
            detail["api"] = country
            if country:
                source = "api"
    else:
        # 内网 / 回环：不发网络查询（查了也没用，还把内网地址送给第三方）
        detail["skip"] = "private"

    name, refine = pick_timezone(country, tz_hint)
    if not name:
        name = config.TZ.key
        source = "default"
    elif refine and not source:
        source = "device"
    elif refine:
        source = f"{source}+device"

    return Guess(
        timezone=name,
        offset_minutes=offset_minutes(name),
        country=country,
        source=source or "default",
        ip=client,
        detail=detail,
    )


def reset_cache() -> None:
    """清空网络查询缓存（测试用；生产没有任何调用点）。"""
    _API_CACHE.clear()
    _TZ_CACHE.clear()


# ============================================================ 请求级便捷入口
def device_timezone(dev: Any) -> str:
    """一台设备"生活在"哪个时区 —— 按它上一次心跳的 IP 判定。

    为什么用 ``Device.ip`` 而不是新增一列：那一列本来就在、由心跳维护，
    而"设备时区"是它的**派生结论**。再存一份就会变成两处真源，两者漂移时
    没有任何症状，只是某天开始提醒集体偏移。

    返回空串表示**判不出来**（没上报过 IP / IP 是内网 / 查询没结果）——
    调用方必须据此退回下一级兜底，不要拿空串去 ``ZoneInfo()``。
    """
    ip = str(getattr(dev, "ip", "") or "").strip()
    if not ip:
        return ""
    try:
        guess = resolve(ip)
    except Exception:  # noqa: BLE001  判不出设备时区不该让建提醒失败
        return ""
    return guess.timezone if guess.country else ""


def resolve_request(request: Any, device_tz: Any = None) -> Guess:
    """从 FastAPI/Starlette 的 ``Request`` 直接出结论。

    ``client_ip`` 已经处理了「只信任可信反代的 XFF」；``peer`` 单独取，因为
    「能不能采信反代声明的国家头」看的是 **TCP 对端**，不是解析出来的客户端。

    :param device_tz: 设备自报时区；缺省读 ``X-Client-Timezone``。
    """
    from .ratelimit import client_ip  # 延迟导入：避免 config ← ratelimit ← geo 的环形

    peer = ""
    try:
        peer = (request.client.host if request.client else "") or ""
    except Exception:  # noqa: BLE001
        peer = ""
    hint = device_tz
    if hint is None:
        try:
            hint = request.headers.get("x-client-timezone", "")
        except Exception:  # noqa: BLE001
            hint = ""
    return resolve(client_ip(request), getattr(request, "headers", {}) or {},
                   peer, hint)
