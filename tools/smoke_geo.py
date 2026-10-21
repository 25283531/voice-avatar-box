"""冒烟：展示时区按 IP 所在国家/地区判定 + 提醒按规则自身时区展开。

为什么要专门守这个（2026-10-21 用户定调，两次纠偏）
--------------------------------------------------
第一版把展示层硬编码成北京时间（"统一几个节点的时间口径"），被用户纠正：

    用户的设备、手机app所在的手机，可能处于不同的国家，采用的时区不同。
    如果在其他国家创建日程，那么只能按照用户所在时区进行创建。
    但作为管理端，管理员看系统日志、看 token 调用日志，要统一为北京时间……

所以口径是**三份**，不是一份：

====================  ============================================
角色                   口径
====================  ============================================
手机 App（家属）       本机 IP 所在国家/地区判定的时区
老人端设备             设备 IP 判定的时区（提醒以它为准响铃）
管理后台               固定北京时间（可排查、可统计）
====================  ============================================

这三条里最容易错、也最难发现的是中间那条：**提醒的"早上 8 点"到底挂在
哪个时区上**。挂错了没有任何异常 —— 建规则返回 201、界面显示正常，
只是老人半夜被叫醒。因此这里的断言全部落在"绝对时刻"上（换算成 UTC 比），
不比对字符串。

断言分组
--------
  A  IP / 国家 / 时区名的**输入归一化**（脏输入不许进下游）
  B  国家 → 时区表：覆盖度 + 每一个时区名都真的存在
  C  `pick_timezone` 的三条规则（单区无视设备 / 多区用设备 / 未知用设备）
  D  可信反代头：**只有可信对端**才采信（否则任意人可改自己的时区）
  E  `resolve` 端到端（对着**本地假 IP 地理位置服务**，不依赖公网）
  F  提醒按规则时区展开（同一句"08:00"，北京与纽约差 12 小时）
  G  `GET /api/geo/timezone` 真实 HTTP
  H  反向变异：把关键行为改回去，指定断言必须真变红

用法：
    python tools/smoke_geo.py [--port 8135] [--no-mutation]

⚠️ CI 一律用**位置参数**传端口（见 tools/smoke_ports.py）；
``--port`` 只是本机排障的等价写法，两者都支持。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_ports import resolve_port  # noqa: E402

PORT = resolve_port("smoke_geo")
#: 假 IP 地理位置服务的端口。故意与主端口拉开 200，避开变异子进程用的 +100。
FAKE_PORT = PORT + 200
DB_DIR = ROOT / "out" / "smoke_geo"
DB_PATH = DB_DIR / "geo.db"

_PASS = 0
_FAIL = 0
_SKIP = 0
_FAILED_NAMES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✓ {name}")
    else:
        _FAIL += 1
        _FAILED_NAMES.append(name)
        print(f"  ✗ {name}" + (f"  —— {detail}" if detail else ""))


def skip(name: str, why: str) -> None:
    """第三态：判据对象不存在时**单列**，绝不混进"通过"。"""
    global _SKIP
    _SKIP += 1
    print(f"  ⊘ 跳过 {name}  —— {why}")


# ============================================================ 假 IP 地理位置服务
#: IP → 国家。刻意包含"能查到"与"查不到"两类。
FAKE_DB = {
    "8.8.8.8": "US",
    "208.80.152.2": "US",        # 同一 /24 之外的第二个美国 IP
    "114.114.114.114": "CN",
    "223.5.5.5": "CN",
    "203.104.153.1": "JP",
    "1.1.1.1": "AU",
}
_FAKE_HITS: list[str] = []


class _FakeGeo(BaseHTTPRequestHandler):
    """返回 ``{"success": true, "country_code": "XX"}``，形状对齐 ipwho.is。

    ⚠️ 也刻意实现了**失败形态**（``success: false`` 且 HTTP 200）——
    那几家服务出错时就是这样的，不判 ``success`` 就会把错误响应当成
    "这个网段没有国家"缓存下来。
    """

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        ip = (self.path or "/").lstrip("/").split("?")[0]
        _FAKE_HITS.append(ip)
        code = FAKE_DB.get(ip, "")
        body = json.dumps(
            {"ip": ip, "success": bool(code), "country_code": code} if code
            else {"ip": ip, "success": False, "message": "reserved range"}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a: object) -> None:  # 静音：否则每个请求一行到 stderr
        return


class _FakeGeoServer:
    def __init__(self, port: int) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), _FakeGeo)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ============================================================ 模块加载（必须在设 env 后）
geo = None      # type: ignore[assignment]
schedule = None  # type: ignore[assignment]
models = None   # type: ignore[assignment]
config = None   # type: ignore[assignment]
ratelimit = None  # type: ignore[assignment]
device_api = None  # type: ignore[assignment]


def boot_modules() -> None:
    """设好环境变量再 import app —— ``config`` 是 import 期求值的。

    这不是洁癖：``ELDER_GEO_API`` 在 ``config`` 的模块级被读一次，
    在 import 之后改 ``os.environ`` 只骗得了测试自己。
    """
    global geo, schedule, models, config, ratelimit, device_api
    os.environ["ELDER_GEO_API"] = f"http://127.0.0.1:{FAKE_PORT}/{{ip}}"
    os.environ["ELDER_GEO_TIMEOUT_SEC"] = "3"
    os.environ.pop("ELDER_GEOIP_DB", None)
    os.environ.pop("ELDER_TRUSTED_PROXY_IPS", None)
    os.environ.pop("ELDER_GEO_COUNTRY_HEADER", None)
    os.environ.setdefault("ELDER_DB", f"sqlite:///{DB_PATH.as_posix()}")
    sys.path.insert(0, str(SERVER))
    from app import config as _config
    from app import geo as _geo
    from app import models as _models
    from app import ratelimit as _ratelimit
    from app import schedule as _schedule
    # 设备时区下发（心跳响应里的 tz_offset_minutes）。放在这里而不是在用例里
    # import：路由模块的导入要 app.db / app.deps 都就绪，延迟到用例里会变成
    # "第一次导入失败导致整组跳过"，而跳过比红更危险。
    from app.routers import device_api as _device_api
    config, geo, models, ratelimit, schedule, device_api = (
        _config, _geo, _models, _ratelimit, _schedule, _device_api)


# ============================================================ A. 输入归一化
def t_normalize() -> None:
    print("\n── A. 输入归一化（脏输入不许进下游）──")
    n = geo.normalize_ip
    check("A1 '1.2.3.4:5678' 去掉端口", n("1.2.3.4:5678") == "1.2.3.4", n("1.2.3.4:5678"))
    check("A2 IPv4-mapped 还原成 v4", n("::ffff:1.2.3.4") == "1.2.3.4", n("::ffff:1.2.3.4"))
    check("A3 '[::1]:443' 去括号端口", n("[::1]:443") == "::1", n("[::1]:443"))
    check("A4 非 IP 返回空串", n("not-an-ip") == "", n("not-an-ip"))
    check("A5 空值返回空串", n("") == "" and n(None) == "", repr(n(None)))

    pr = geo.is_private
    for ip in ("10.1.2.3", "192.168.1.1", "127.0.0.1", "::1", "169.254.1.1"):
        check(f"A6a 内网/回环 {ip} 判为 private", pr(ip) is True, str(pr(ip)))
    check("A6b 公网 IP 不是 private", pr("8.8.8.8") is False)
    check("A6c 空串按 private 处理（不发查询）", pr("") is True)

    ck = geo.cache_key
    check("A7a IPv4 按 /24 聚合", ck("8.8.8.8") == "8.8.8.0/24", ck("8.8.8.8"))
    check("A7b 同 /24 的两个 IP 同键", ck("8.8.8.8") == ck("8.8.8.200"))
    check("A7c IPv6 按 /48 聚合", ck("2001:4860:4860::8888").endswith("/48"),
          ck("2001:4860:4860::8888"))

    nc = geo.normalize_country
    check("A8a 小写码归一", nc("cn") == "CN")
    check("A8b 国名归一", nc(" China ") == "CN", nc(" China "))
    check("A8c 字典里的 country_code 归一", nc({"country_code": "us"}) == "US")
    check("A8d XX/T1/ZZ 视为未知", nc("XX") == "" and nc("T1") == "" and nc("ZZ") == "",
          f"{nc('XX')!r} {nc('T1')!r} {nc('ZZ')!r}")
    check("A8e 认不出的国名不猜", nc("Narnia") == "", nc("Narnia"))

    vt = geo.valid_timezone
    check("A9a 合法 IANA 名通过", vt("Asia/Shanghai") == "Asia/Shanghai")
    check("A9b 编造的名字被拒", vt("Bad/Zone") == "", vt("Bad/Zone"))
    check("A9c 首尾空白被 strip（换个行尾不该让整条链路退化）",
          vt("Asia/Shanghai\n") == "Asia/Shanghai", repr(vt("Asia/Shanghai\n")))
    check("A9c2 **中间**夹换行的被拒（防注入 / 防多值拼接）",
          vt("Asia/\nShanghai") == "", repr(vt("Asia/\nShanghai")))
    check("A9d 带分号的被拒", vt("Asia/Shanghai; drop") == "", repr(vt("Asia/Shanghai; drop")))
    check("A9e 超长被拒", vt("A/" + "x" * 100) == "")


# ============================================================ B. 国家→时区表
def t_country_table() -> None:
    print("\n── B. 国家 → 时区表 ──")
    z = geo.COUNTRY_ZONES
    check("B1 中国只有一个时区", z.get("CN") == ("Asia/Shanghai",), str(z.get("CN")))
    check("B2 港澳台各自单列（一个中国原则）",
          z.get("HK") == ("Asia/Hong_Kong",) and z.get("MO") == ("Asia/Macau",)
          and z.get("TW") == ("Asia/Taipei",),
          f"HK={z.get('HK')} MO={z.get('MO')} TW={z.get('TW')}")
    check("B3 美国主时区是纽约且多于一个", z.get("US", ("",))[0] == "America/New_York"
          and len(z.get("US", ())) > 1, str(z.get("US")))
    check("B4 覆盖国家数 >= 190", len(z) >= 190, f"{len(z)} 个")

    # 最关键的一条：**表里每一个时区名都真的存在**。手写 200 行名字时，
    # 拼错一个（Asia/Ho_Chi_Minh → Asia/Hochiminh）不会在别处报错，
    # 只会在那个国家的用户身上退化成 config.TZ。
    bad = []
    for code, zones in z.items():
        for name in zones:
            if geo.valid_timezone(name) != name:
                bad.append(f"{code}:{name}")
    check("B5 表内每个时区名都真实存在", not bad, "非法：" + ", ".join(bad[:8]))

    # 偏移抽样：写死偏移（+480）会在夏令时国家半年差一小时，这里守住"按此刻算"
    check("B6a 北京 +480", geo.offset_minutes("Asia/Shanghai") == 480)
    summer = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    winter = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    check("B6b 纽约夏天 -240（夏令时生效）",
          geo.offset_minutes("America/New_York", summer) == -240,
          str(geo.offset_minutes("America/New_York", summer)))
    check("B6c 纽约冬天 -300", geo.offset_minutes("America/New_York", winter) == -300,
          str(geo.offset_minutes("America/New_York", winter)))


# ============================================================ C. pick_timezone
def t_pick_timezone() -> None:
    print("\n── C. 国家 + 设备自报 → 时区 ──")
    pick = geo.pick_timezone

    name, refine = pick("CN", "America/New_York")
    check("C1 单区国家**无视**设备自报（手机时区设错要被纠正）",
          name == "Asia/Shanghai" and refine == "",
          f"{name!r}/{refine!r}")

    name, refine = pick("US", "America/Los_Angeles")
    check("C2 多区国家采纳该国之内的设备自报", name == "America/Los_Angeles"
          and refine == "device", f"{name!r}/{refine!r}")

    name, refine = pick("US", "Asia/Shanghai")
    check("C3 设备自报**不在该国**时不采纳，退回主时区",
          name == "America/New_York" and refine == "", f"{name!r}/{refine!r}")

    name, refine = pick("", "Europe/Berlin")
    check("C4 国家未知时用设备自报", name == "Europe/Berlin" and refine == "device",
          f"{name!r}/{refine!r}")

    name, refine = pick("", "")
    check("C5 什么都不知道时返回空（由调用方兜底）", name == "" and refine == "",
          f"{name!r}/{refine!r}")

    name, refine = pick("CN", "")
    check("C6 单区国家 + 无设备自报 → 国家时区", name == "Asia/Shanghai", name)


# ============================================================ D. 可信反代头
def t_trusted_proxy() -> None:
    print("\n── D. 可信反代声明的国家头 ──")
    hdr = {config.GEO_COUNTRY_HEADER: "US"}
    saved = ratelimit._TRUSTED

    try:
        ratelimit._TRUSTED = ()
        got = geo.country_from_proxy("1.2.3.4", hdr)
        check("D1 未声明可信代理 ⇒ 国家头**一律不采信**（否则人人可改自己时区）",
              got == "", repr(got))

        import ipaddress
        ratelimit._TRUSTED = (ipaddress.ip_network("1.2.3.4/32"),)
        got = geo.country_from_proxy("1.2.3.4", hdr)
        check("D2 对端在可信名单里 ⇒ 采信", got == "US", repr(got))
        got = geo.country_from_proxy("9.9.9.9", hdr)
        check("D3 对端不在名单 ⇒ 仍不采信", got == "", repr(got))

        g = geo.resolve("8.8.8.8", hdr, peer_ip="9.9.9.9")
        check("D4 resolve 里 peer 与 client 是分开判的（不能用 client 判可信）",
              g.source == "api", f"source={g.source}")
        g = geo.resolve("8.8.8.8", hdr, peer_ip="1.2.3.4")
        check("D5 可信反代优先于网络查询", g.source == "proxy" and g.country == "US",
              f"source={g.source} country={g.country}")
    finally:
        ratelimit._TRUSTED = saved


# ============================================================ E. resolve 端到端
def t_resolve() -> None:
    print("\n── E. resolve 端到端（对着本地假 IP 库）──")
    geo.reset_cache()
    _FAKE_HITS.clear()

    g = geo.resolve("8.8.8.8")
    check("E1 美国 IP → 美国主时区", g.country == "US"
          and g.timezone == "America/New_York" and g.source == "api",
          f"{g.country}/{g.timezone}/{g.source}")

    g = geo.resolve("114.114.114.114")
    check("E2 中国 IP → 北京时间（唯一时区）",
          g.country == "CN" and g.timezone == "Asia/Shanghai" and g.source == "api",
          f"{g.country}/{g.timezone}/{g.source}")

    g = geo.resolve("8.8.8.8", device_tz="America/Los_Angeles")
    check("E3 美国 IP + 设备自报洛杉矶 → 洛杉矶，来源标明细化",
          g.timezone == "America/Los_Angeles" and g.source == "api+device",
          f"{g.timezone}/{g.source}")

    g = geo.resolve("114.114.114.114", device_tz="America/Los_Angeles")
    check("E4 中国 IP + 设备自报洛杉矶 → **仍是北京时间**（设备时区设错要被纠正）",
          g.timezone == "Asia/Shanghai" and g.source == "api",
          f"{g.timezone}/{g.source}")

    before = len(_FAKE_HITS)
    g = geo.resolve("10.0.0.5")
    check("E5 内网 IP 不发网络查询", len(_FAKE_HITS) == before,
          f"命中 {len(_FAKE_HITS) - before} 次")
    check("E6 内网 IP 无国家，来源如实写 default",
          g.country == "" and g.source == "default" and g.timezone == "Asia/Shanghai",
          f"{g.country}/{g.source}/{g.timezone}")

    g = geo.resolve("10.0.0.5", device_tz="Asia/Tokyo")
    check("E7 内网 IP + 设备自报 → 用设备，来源 device",
          g.timezone == "Asia/Tokyo" and g.source == "device", f"{g.timezone}/{g.source}")

    # 缓存：同一 /24 第二次不该再打假上游
    geo.reset_cache()
    _FAKE_HITS.clear()
    geo.resolve("8.8.8.8")
    geo.resolve("8.8.8.200")     # 同 /24，不在 FAKE_DB 里
    check("E8 同一 /24 命中缓存（第二次不发请求，也不会因为库里没有就变空）",
          len(_FAKE_HITS) == 1, f"实际请求 {len(_FAKE_HITS)} 次：{_FAKE_HITS}")

    # 查不到：既不能抛，也不能假装查到
    g = geo.resolve("198.51.100.7")
    check("E9 查不到的 IP → 兜底且 country 为空（不编造）",
          g.country == "" and g.timezone == "Asia/Shanghai", f"{g.country}/{g.timezone}")

    # 连不上任何服务：必须降级而不是抛
    saved = config.GEO_API_URL
    try:
        config.GEO_API_URL = "http://127.0.0.1:1/{ip}"
        geo.reset_cache()
        g = geo.resolve("203.0.113.9", device_tz="Europe/Paris")
        check("E10 上游连不上 ⇒ 不抛异常，退回设备时区",
              g.timezone == "Europe/Paris" and g.source == "device", f"{g.source}")
    finally:
        config.GEO_API_URL = saved
        geo.reset_cache()


# ============================================================ F. 提醒按规则时区展开
def _rule(**kw):
    base = dict(id=1, device_id=1, title="t", enabled=True, rtype=0,
                schedule_kind=1, times_of_day=["08:00"], weekdays=[],
                month_days=[], interval_days=1, once_date=None,
                start_date=None, end_date=None, tz="")
    base.update(kw)
    return models.Reminder(**base)


def t_schedule_tz() -> None:
    print("\n── F. 提醒按规则自身时区展开 ──")
    d = date(2026, 10, 22)

    bj = schedule.occurrences_on(_rule(tz="Asia/Shanghai"), d)
    ny = schedule.occurrences_on(_rule(tz="America/New_York"), d)
    la = schedule.occurrences_on(_rule(tz=""), d)   # 空串 = 历史数据 = config.TZ

    check("F1 北京规则 08:00 = UTC 00:00",
          bj and bj[0].astimezone(timezone.utc) == datetime(2026, 10, 22, 0, 0,
                                                            tzinfo=timezone.utc),
          str(bj[0] if bj else None))
    check("F2 纽约规则 08:00 = UTC 12:00（夏令时）",
          ny and ny[0].astimezone(timezone.utc) == datetime(2026, 10, 22, 12, 0,
                                                            tzinfo=timezone.utc),
          str(ny[0] if ny else None))
    check("F3 同一天同一句 08:00，两地相差 12 小时",
          (ny[0].astimezone(timezone.utc) - bj[0].astimezone(timezone.utc)).total_seconds()
          == 12 * 3600)
    check("F4 tz 为空（历史数据）行为**逐字不变**",
          la and la[0].astimezone(timezone.utc) == bj[0].astimezone(timezone.utc),
          str(la[0] if la else None))

    bad = schedule.occurrences_on(_rule(tz="Bad/Zone"), d)
    check("F5 非法 tz 不抛异常，退回 config.TZ",
          bad and bad[0].astimezone(timezone.utc) == bj[0].astimezone(timezone.utc),
          str(bad[:1]))

    # event_instant：把"规则时区的裸钟点"还原成绝对时刻（全仓唯一入口）
    naive = datetime(2026, 10, 22, 8, 0)          # SQLite 读回来的样子
    check("F6 event_instant(裸钟点, Asia/Shanghai) = UTC 00:00",
          schedule.event_instant(naive, "Asia/Shanghai")
          == datetime(2026, 10, 22, 0, 0, tzinfo=timezone.utc),
          str(schedule.event_instant(naive, "Asia/Shanghai")))
    check("F7 event_instant(裸钟点, America/New_York) = UTC 12:00",
          schedule.event_instant(naive, "America/New_York")
          == datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc),
          str(schedule.event_instant(naive, "America/New_York")))

    # 下一次触发：跨时区时"今天"的判定也要跟着走
    after = datetime(2026, 10, 22, 13, 0, tzinfo=timezone.utc)
    nxt_ny = schedule.next_occurrence(_rule(tz="America/New_York"), after)
    check("F8 纽约规则在 UTC 13:00 之后的下一次是**次日** 08:00（当地）",
          nxt_ny is not None and nxt_ny.date() == date(2026, 10, 23),
          str(nxt_ny))
    nxt_bj = schedule.next_occurrence(_rule(tz="Asia/Shanghai"), after)
    check("F9 北京规则在同一时刻之后的下一次是**当日** 08:00（当地次日 08:00 = UTC 00:00 次日）",
          nxt_bj is not None and nxt_bj.date() == date(2026, 10, 23),
          str(nxt_bj))


# ============================================================ G. 真实 HTTP
BASE = f"http://127.0.0.1:{PORT}"


def _get(path: str, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(BASE + path, headers=headers or {})
    # 必须绕开系统代理，否则本机代理会把 127.0.0.1 也拦掉
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return exc.code, {}


def wait_ready(timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, body = _get("/api/health")
            if code == 200 and body.get("ok"):
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


def t_http() -> None:
    print("\n── G. GET /api/geo/timezone（真实 HTTP）──")
    code, body = _get("/api/geo/timezone")
    check("G1 状态码 200", code == 200, str(code))
    check("G2 五个字段齐备",
          all(k in body for k in ("timezone", "offset_minutes", "country", "source", "ip")),
          str(body))
    check("G3 回环来源如实写 default，不是假装查到了",
          body.get("source") == "default" and body.get("country") == "",
          f"source={body.get('source')} country={body.get('country')}")
    check("G4 回环 IP 被识别为 127.0.0.1", body.get("ip") == "127.0.0.1", str(body.get("ip")))

    code, body = _get("/api/geo/timezone", {"X-Client-Timezone": "America/Chicago"})
    check("G5 回环无国家时，设备自报生效（source=device）",
          code == 200 and body.get("timezone") == "America/Chicago"
          and body.get("source") == "device", str(body))

    code, body = _get("/api/geo/timezone", {"X-Client-Timezone": "Not/AZone"})
    check("G6 非法 X-Client-Timezone 不 500，退回兜底",
          code == 200 and body.get("timezone") == "Asia/Shanghai", str(body))

    code, body = _get("/api/geo/timezone?verbose=1")
    check("G7 verbose=1 附带逐层记录", code == 200 and isinstance(body.get("detail"), dict),
          str(body.get("detail")))


class Server:
    """一个真实 uvicorn 实例（``/api/geo/timezone`` 无鉴权，不需要登录）。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.log = DB_DIR / f"server_{os.getpid()}.log"
        self.proc: subprocess.Popen | None = None
        self.fp = None

    def __enter__(self) -> "Server":
        DB_DIR.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-journal", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        env = dict(os.environ)
        env.pop("MSYSTEM", None)
        env["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
        env["ELDER_JWT_SECRET"] = "smoke-geo-secret-0123456789abcdef"
        env["ELDER_SEED_ADMIN_PASSWORD"] = "admin123"
        env["ELDER_GEO_API"] = f"http://127.0.0.1:{FAKE_PORT}/{{ip}}"
        env["ELDER_GEO_TIMEOUT_SEC"] = "3"
        env["ELDER_LOGIN_RATE_LIMIT"] = "0"

        self.fp = self.log.open("w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--log-level", "info"],
            cwd=str(SERVER), env=env, stdout=self.fp, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        if not wait_ready():
            print("[boot] 服务没起来，日志尾部：")
            print(self.tail())
            raise SystemExit(1)
        print(f"[boot] ready on :{self.port}")
        return self

    def tail(self, n: int = 65536) -> str:
        try:
            return self.log.read_bytes()[-n:].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return "<no log>"

    def __exit__(self, *exc: object) -> None:
        try:
            if self.proc is not None:
                self.proc.terminate()
                self.proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            if self.proc is not None:
                self.proc.kill()
        try:
            if self.fp is not None:
                self.fp.close()
        except Exception:  # noqa: BLE001
            pass


# ============================================================ G2. 设备时区下发
class _FakeState:
    pass


class _FakeApp:
    def __init__(self) -> None:
        self.state = _FakeState()


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """够 ``geo.resolve_request`` / ``ratelimit.client_ip`` 用的最小 Request 替身。"""

    def __init__(self, peer: str = "127.0.0.1",
                 headers: dict[str, str] | None = None) -> None:
        self.client = _FakeClient(peer)
        self.headers = headers or {}
        self.app = _FakeApp()


class _FakeDev:
    def __init__(self, ip: str = "") -> None:
        self.ip = ip


def t_device_tz() -> None:
    print("\n── G2. 心跳下发设备时区（固件据此 setenv TZ）──")
    # 固定两个时刻：一个在美东夏令时内（EDT，-240），一个在标准时内（EST，-300）。
    # **故意用两个日期**：只测一个的话，"把 -240 写死"也能过 —— 而写死正是
    # 这个功能最可能的退化形态（夏令时半年错一小时，用户只会说"有时候不准"）。
    summer = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
    winter = datetime(2026, 12, 21, 14, 0, tzinfo=timezone.utc)

    # G9：设备自报时区（私网 IP 判不出国家 ⇒ 采信设备自报，这是 pick_timezone 的规则）
    tz, off = device_api._device_tz_for(
        _FakeRequest(headers={"x-client-timezone": "America/New_York"}),
        _FakeDev(), summer)
    check("G9 私网 + 设备自报纽约 ⇒ 时区名取自设备", tz == "America/New_York", tz)
    check("G10 十月的美东偏移是 -240（夏令时）", off == -240, str(off))

    # G11：同一台设备换个日期，偏移必须跟着变 —— 这条是"偏移是算出来的"的证据
    _tz2, off2 = device_api._device_tz_for(
        _FakeRequest(headers={"x-client-timezone": "America/New_York"}),
        _FakeDev(), winter)
    check("G11 十二月的美东偏移是 -300（标准时）", off2 == -300, str(off2))

    # G12：设备什么都不说、IP 也是内网 ⇒ 兜底 ELDER_TZ，且给出 +480
    tz3, off3 = device_api._device_tz_for(_FakeRequest(), _FakeDev(), summer)
    check("G12 判不出时兜底 ELDER_TZ", tz3 == config.TZ.key, tz3)
    check("G13 兜底时偏移是 +480（不是 0/UTC）", off3 == 480, str(off3))

    # G14：第一层（请求 IP）判不出来时，必须真的去问第二层（Device.ip）。
    #      两层都打桩成"已知答案"，才能证明第二层**被调用过** —— 只断言
    #      "最后是 +480" 是测不出这一层的（兜底也是 +480，两者同形）。
    orig_req = geo.resolve_request
    orig_dev = geo.device_timezone

    def _no_guess(request, device_tz=None):      # noqa: ANN001
        raise RuntimeError("模拟请求 IP 判定链失败")

    def _dev_guess(dev):                         # noqa: ANN001
        return "America/New_York"

    geo.resolve_request = _no_guess                    # type: ignore[assignment]
    geo.device_timezone = _dev_guess                   # type: ignore[assignment]
    try:
        tz4, off4 = device_api._device_tz_for(_FakeRequest(), _FakeDev("8.8.8.8"), summer)
        check("G14 请求层失败时回落到 Device.ip 判定的时区",
              tz4 == "America/New_York", tz4)
        check("G14b 回落层的偏移也跟着算（-240，不是兜底的 +480）", off4 == -240, str(off4))
    except Exception as exc:                           # noqa: BLE001
        check("G14 请求层失败时回落到 Device.ip 判定的时区", False, repr(exc))
    finally:
        geo.resolve_request = orig_req                 # type: ignore[assignment]
        geo.device_timezone = orig_dev                 # type: ignore[assignment]

    # G14c：两层都判不出 ⇒ 兜底，且**不抛异常**（心跳不能因此失败）
    geo.resolve_request = _no_guess                    # type: ignore[assignment]
    try:
        tz5, _ = device_api._device_tz_for(_FakeRequest(), _FakeDev(), summer)
        check("G14c 两层都判不出时兜底而不抛异常", tz5 == config.TZ.key, tz5)
    except Exception as exc:                           # noqa: BLE001
        check("G14c 两层都判不出时兜底而不抛异常", False, repr(exc))
    finally:
        geo.resolve_request = orig_req                 # type: ignore[assignment]

    # G15：退化到兜底要在 app.state 里留痕（只一次），否则"配置全对"与
    #      "整条链断了"完全无法区分。
    req = _FakeRequest()
    device_api._device_tz_for(req, _FakeDev(), summer)
    check("G15 退化到兜底时在 app.state 留痕",
          getattr(req.app.state, device_api._TZ_WARN_ATTR, False) is True)


# ============================================================ H. 反向变异
MUTATIONS: dict[str, tuple[str, str, list[str]]] = {
    # 名字: (原文, 替换, 期望变红的断言关键字)
    "M1": (
        # 去掉「只有可信对端才采信国家头」的闸门
        "    if not is_trusted_proxy(peer_ip):\n        return \"\"\n",
        "    if False:\n        return \"\"\n",
        ["D1 未声明可信代理"],
    ),
    "M2": (
        # 让单区国家也听设备的（= 回到"设备说了算"，正是要消灭的形态）
        "        if len(zones) > 1 and device_tz and device_tz in zones:\n"
        "            return device_tz, \"device\"\n",
        "        if device_tz:\n            return device_tz, \"device\"\n",
        ["C1 单区国家"],
    ),
    "M3": (
        # 排期退回"永远用服务端时区"
        "    name = valid_timezone(getattr(r, \"tz\", \"\"))\n"
        "    return ZoneInfo(name) if name else TZ\n",
        "    return TZ\n",
        ["F2 纽约规则", "F3 同一天同一句"],
    ),
    "M4": (
        # 缓存写不进去（等价于每次请求都去查上游）
        "        self._data[key] = (time.monotonic() + "
        "(self.ttl if ttl is None else ttl), value)",
        "        return",
        ["E8 同一 /24 命中缓存"],
    ),
    "M5": (
        # 设备时区下发退化成"永远用服务端时区"（= 改造前的形态：
        # 固件 TZ 写死东八区，海外设备整体错小时数）
        "    moment = now or local_now()\n",
        "    return TZ.key, geo.offset_minutes(TZ.key, now or local_now())\n"
        "    moment = now or local_now()\n",
        ["G9 私网 + 设备自报纽约", "G10 十月的美东偏移"],
    ),
}

#: 每个变异改的是哪个文件（相对仓库根）。
_MUTATION_TARGETS = {
    "M1": "server/app/geo.py",
    "M2": "server/app/geo.py",
    "M3": "server/app/schedule.py",
    "M4": "server/app/geo.py",
    "M5": "server/app/routers/device_api.py",
}


def _install_mutation_hook(name: str) -> None:
    """用 import hook **在内存里**改写目标源码，不落盘。

    为什么不直接改文件再还原：中途被 kill / 断言崩掉就会留下变异后的源码，
    而"忘了还原"的代价是下一次真跑的结论不可信。import hook 进程退出即消失。
    """
    old, new, _ = MUTATIONS[name]
    target_path = (ROOT / _MUTATION_TARGETS[name]).resolve()

    import importlib.abc
    import importlib.machinery
    import importlib.util

    class _Loader(importlib.abc.SourceLoader):
        def __init__(self, fullname: str, path: str) -> None:
            self.fullname = fullname
            self.path = path

        def get_filename(self, fullname: str) -> str:
            return self.path

        def get_data(self, path: str) -> bytes:
            src = Path(path).read_text(encoding="utf-8")
            if Path(path).resolve() == target_path:
                if src.count(old) != 1:
                    raise RuntimeError(
                        f"变异 {name} 的锚点在 {_MUTATION_TARGETS[name]} 里出现 "
                        f"{src.count(old)} 次，应为 1 —— 源码变了，先修冒烟")
                src = src.replace(old, new)
            return src.encode("utf-8")

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if not fullname.startswith("app."):
                return None
            mod = fullname.rsplit(".", 1)[-1]
            for cand in (SERVER / "app" / f"{mod}.py",
                         SERVER / "app" / "routers" / f"{mod}.py"):
                if cand.exists():
                    return importlib.util.spec_from_file_location(
                        fullname, str(cand),
                        loader=_Loader(fullname, str(cand)))
            return None

    sys.meta_path.insert(0, _Finder())


def run_mutation(name: str, port: int) -> tuple[bool, str]:
    """子进程必须退出码 1 **且**失败列表里出现指定断言名。"""
    env = dict(os.environ)
    env["SMOKE_MUTATION"] = name
    proc = subprocess.run(
        [PY, str(Path(__file__)), "--mutate", name, "--port", str(port)],
        capture_output=True, text=True, timeout=420, env=env, cwd=str(ROOT))
    out = (proc.stdout or "") + (proc.stderr or "")
    expect = MUTATIONS[name][2]
    hit = [e for e in expect if e in out]
    ok = proc.returncode == 1 and bool(hit)
    detail = ""
    if not ok:
        detail = (f"rc={proc.returncode} 期望的失败断言命中 {len(hit)}/{len(expect)}；"
                  f"尾部输出：{out[-400:]}")
    return ok, detail


def t_mutations(port: int) -> None:
    print("\n── H. 反向变异（把行为改回去，指定断言必须真变红）──")
    for name in sorted(MUTATIONS):
        ok, detail = run_mutation(name, port)
        check(f"H{name[1]} 变异 {name} 让指定断言变红", ok, detail)


# ============================================================ main
def main(argv: list[str] | None = None) -> int:
    global PORT, FAKE_PORT
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    PORT = args.port or resolve_port("smoke_geo")
    FAKE_PORT = PORT + 200
    base_url = f"http://127.0.0.1:{PORT}"
    globals()["BASE"] = base_url

    mutate_name = args.mutate or ""
    if mutate_name:
        if mutate_name not in MUTATIONS:
            print(f"未知变异 {mutate_name}")
            return 2
        _install_mutation_hook(mutate_name)
        PORT = int(os.environ.get("SMOKE_MUTATION_PORT") or PORT)
        globals()["BASE"] = f"http://127.0.0.1:{PORT}"

    print(f"展示时区冒烟（HTTP :{PORT}，假 IP 库 :{FAKE_PORT}"
          + (f"，变异 {mutate_name}" if mutate_name else "") + "）")

    DB_DIR.mkdir(parents=True, exist_ok=True)
    fake = _FakeGeoServer(FAKE_PORT)
    fake.start()
    try:
        boot_modules()
        t_normalize()
        t_country_table()
        t_pick_timezone()
        t_trusted_proxy()
        t_resolve()
        t_schedule_tz()
        t_device_tz()
        with Server(PORT):
            t_http()
        if not mutate_name and not args.no_mutation:
            t_mutations(PORT + 100)
    finally:
        fake.stop()

    print(f"\n通过 {_PASS} / 失败 {_FAIL} / 跳过 {_SKIP}")
    if _FAILED_NAMES:
        print("失败的断言：")
        for n in _FAILED_NAMES:
            print(f"  - {n}")
    return 1 if _FAIL else 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """端口既接受**位置参数**也接受 ``--port``。

    ⚠️ CI 一律用位置参数（``python tools/smoke_geo.py 8135``）。只声明
    ``--port`` 会让 CI 那一步 argparse rc=2 全红，而本机用 ``--port``
    跑得全绿 —— 这种差异比单纯失败更浪费时间（F1 实测踩过）。
    """
    ap = argparse.ArgumentParser(description="展示时区判定冒烟")
    ap.add_argument("port", nargs="?", type=int, default=None)
    ap.add_argument("--port", dest="port_opt", type=int, default=None)
    ap.add_argument("--no-mutation", action="store_true",
                    help="跳过反向变异（调试用）")
    ap.add_argument("--mutate", default="", help=argparse.SUPPRESS)
    ns = ap.parse_args(argv)
    ns.port = ns.port if ns.port is not None else ns.port_opt
    return ns


if __name__ == "__main__":
    sys.exit(main())
