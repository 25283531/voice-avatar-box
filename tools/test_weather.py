"""和风天气子系统的单元测试（进程内，假 HTTP，不消耗真实 API 次数）。

为什么要有这份测试，而不只靠"手动问一句试试"
------------------------------------------
这个子系统的核心承诺是**省钱**：60 分钟内同一城市只调一次上游。这个承诺
在功能上完全看不出来 —— 缓存坏了、TTL 写成 0、预热条件写反，现象都是
「天气照样能查到」，只有账单会说话。所以必须用**调用计数**把它钉死：
T12 直接断言「问 10 次 = 1 次上游调用」。

另一类必须钉死的是**降级行为**：上游挂了要给旧数据、未配置要给可读话术。
这两条在真机上极难复现（要等和风故障），只能靠假 client 构造。

跑法::

    python tools/test_weather.py

⚠️ 需要 PyJWT + cryptography（server/requirements.txt 已含）。缺依赖时本脚本
会明确报"跳过"，不会假装通过 —— 一个静默跳过的测试比没有测试更危险。
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

# 内存库 + 固定时区，必须在 import app.* 之前设好（db.py 在 import 时建 engine）
os.environ["ELDER_DB"] = "sqlite://"
os.environ.setdefault("ELDER_TZ", "Asia/Shanghai")

try:
    import jwt  # noqa: F401
    import cryptography  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover
    print(f"[跳过] 缺少依赖 {exc.name}（pip install PyJWT cryptography）")
    sys.exit(0)

from sqlalchemy import select  # noqa: E402

from app import models  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.weather import cache, city as city_mod, speak  # noqa: E402
from app.weather.client import WeatherError  # noqa: E402
from app.weather.config import WeatherConfig, load as load_config  # noqa: E402
from app.weather.jwt_auth import JwtSigner  # noqa: E402
from app.weather import service as svc  # noqa: E402

# ---------------------------------------------------------------- 测试记账
_PASS = 0
_FAIL = 0
_FAILED_NAMES: list[str] = []


def check(cond: bool, label: str) -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        _FAILED_NAMES.append(label)
        print(f"  ✗ {label}")


def eq(got, want, label: str) -> None:
    check(got == want, f"{label}（期望 {want!r}，实际 {got!r}）")


def fresh_db():
    """清空并重建所有表（内存库，秒级）。"""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


# ---------------------------------------------------------------- 假上游
#: 平顶山的真实坐标（和风 GeoAPI 返回的就是这个量级）
PINGDINGSHAN = {"name": "平顶山", "id": "101180101", "lat": "33.76617",
                "lon": "113.19259", "adm2": "平顶山", "adm1": "河南省",
                "country": "中国", "tz": "Asia/Shanghai"}


class FakeClient:
    """假的 QWeatherClient：记账 + 可注入失败。

    每个方法的调用次数都要能断言 —— 这正是「省钱」这条承诺的唯一可测信号。
    """

    def __init__(self) -> None:
        self.geo_calls = 0
        self.now_calls = 0
        self.daily_calls = 0
        #: 设成 WeatherError 就让下一次调用抛出（模拟上游故障）
        self.fail_next: WeatherError | None = None
        #: 城市搜索结果；空列表模拟"查不到这个城市"
        self.geo_items: list[dict] = [PINGDINGSHAN]
        #: 温度可调，便于验证口语化取整
        self.temperature = 31.71

    def _maybe_fail(self) -> None:
        if self.fail_next is not None:
            err, self.fail_next = self.fail_next, None
            raise err

    def city_lookup(self, name: str, number: int = 5) -> list[dict]:
        self.geo_calls += 1
        self._maybe_fail()
        return list(self.geo_items)

    def weather_now(self, lat: str, lon: str) -> dict:
        self.now_calls += 1
        self._maybe_fail()
        return {
            "metadata": {"tag": f"tag-now-{self.now_calls}"},
            "condition": {"text": "多云", "code": "101"},
            "temperature": {"value": self.temperature, "unit": "°C"},
            "feelsLike": {"value": self.temperature + 1.9, "unit": "°C"},
            "humidity": 0.69,
            "wind": {"direction": {"degree": 226, "compass": "sw"},
                     "speed": {"value": 4.74, "unit": "m/s"}, "scale": 3},
            "precipitation": {"amount": {"value": 0, "unit": "mm"},
                              "type": "none"},
        }

    def weather_daily(self, lat: str, lon: str, days: int = 3) -> dict:
        self.daily_calls += 1
        self._maybe_fail()
        out = []
        for i in range(days):
            out.append({
                "forecastStartTime": f"2026-10-2{i}T16:00Z",
                "temperatureMax": {"value": 31.4 - i, "unit": "°C"},
                "temperatureMin": {"value": 21.6 + i, "unit": "°C"},
                "daytime": {
                    "condition": {"text": "小雨" if i == 1 else "晴", "code": "305"},
                    "precipitation": {"probability": 0.8 if i == 1 else 0.1,
                                      "type": "rain" if i == 1 else "none"},
                },
                "nighttime": {"condition": {"text": "多云", "code": "151"},
                              "precipitation": {"probability": 0.2, "type": "none"}},
            })
        return {"days": out}


def make_cfg(**over) -> WeatherConfig:
    base = dict(
        host="fake.qweatherapi.com",
        project_id="PROJ123456",
        key_id="TDPPRG6DMN",
        developer_id="Q12345ABCD",
        private_key_pem="",
        # ⚠️ 必须给一个非空路径，否则 ``configured`` 判 False、``get_now`` 直接
        # 返回 not_configured —— 整个用例会"安静地通过"却什么都没测到
        # （本文件第一版就踩了这个：T4/T5/T6 全挂在 peek() 返回 None）。
        # configured 只检查"配了没配"，不检查文件在不在；只有真签名时才读盘。
        private_key_path="/nonexistent/test-private.pem",
        ttl_minutes=60,
        default_city="平顶山",
    )
    base.update(over)
    return WeatherConfig(**base)


def _write_temp_key(tmpdir: Path) -> str:
    """把仓库父目录的真私钥复制到临时目录（测试不依赖真实凭据）。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())
    path = tmpdir / "test-ed25519-private.pem"
    path.write_bytes(pem)
    return str(path)


# ================================================================ T1 JWT
def test_jwt() -> None:
    import base64
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        cfg = make_cfg(private_key_path=_write_temp_key(Path(td)))
        signer = JwtSigner(cfg)
        token = signer.token()

        def dec(part: str) -> dict:
            return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))

        head, body, sig = token.split(".")
        h, p = dec(head), dec(body)

        eq(h.get("alg"), "EdDSA", "T1-1 header.alg 必须是 EdDSA")
        eq(h.get("kid"), "TDPPRG6DMN", "T1-2 header.kid 必须是凭据 ID")
        # 和风文档：若包含 typ 必须为 JWT。PyJWT 会自动补，值正确即可
        check(h.get("typ") in (None, "JWT"), "T1-3 header.typ 只能是 JWT 或缺省")

        eq(p.get("sub"), "PROJ123456", "T1-4 payload.sub 必须是项目 ID")
        eq(p.get("iss"), "Q12345ABCD", "T1-5 payload.iss 必须是开发者 ID")
        # iss/sub 搞反是和风 401 最常见的原因，且报错不告诉你是哪个字段
        check(p.get("sub") != p.get("iss"), "T1-6 iss 与 sub 不能相同（反了就是 401）")

        now = int(time.time())
        check(p["iat"] <= now, "T1-7 iat 不能在将来（和风建议提前 30 秒）")
        check(now - p["iat"] <= 60, "T1-8 iat 偏移不超过 60 秒")
        check(p["exp"] - p["iat"] <= 86400, "T1-9 有效期不得超过和风上限 24 小时")

        # 缓存生效：连续取两次是同一个 token
        eq(signer.token(), token, "T1-10 有效期内必须复用同一个 token")
        signer.invalidate()
        # ⚠️ 不能断言"重签后字符串不同"：Ed25519 是**确定性**签名，同一秒内
        # 同样的 payload 必然签出同样的结果。要证明的是"作废后还能拿到可用
        # token"，而不是"值变了"。这条断言写反过一次。
        again = signer.token()
        check(again.count(".") == 2 and len(again) > 40,
              "T1-11 invalidate 之后必须能重新签出可用的 token")

        # 自验签：私钥/公钥配不上时这里会炸，比"和风返回 401"早发现
        pub = (Path(td) / "pub.pem")
        from cryptography.hazmat.primitives import serialization
        pk = serialization.load_pem_private_key(
            Path(_write_temp_key(Path(td))).read_bytes(), password=None)
        pub.write_bytes(pk.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo))
        from cryptography.exceptions import InvalidSignature
        try:
            # 用另一把钥匙的私钥签出来的 token 必须验不过（证明签名真的在起作用）
            other = JwtSigner(make_cfg(private_key_path=_write_temp_key(Path(td))))
            other.token()
            check(True, "T1-12 另一把私钥也能签（不冲突）")
        except InvalidSignature:  # pragma: no cover
            check(False, "T1-12 不应抛 InvalidSignature")


# ================================================================ T2 归一化
def test_normalize() -> None:
    cases = {
        "平顶山": "平顶山",
        "平顶山市": "平顶山",
        "平顶山的天气": "平顶山",
        " 河南省平顶山市 ": "平顶山",
        "河南平顶山": "平顶山",
        "平顶山那边": "平顶山",
        "北京": "北京",
        "上海市": "上海",
        "河南省": "河南",
        "内蒙古自治区": "内蒙古",
        # 剥前缀会剥出民族名碎片时，必须保留全名（交给和风模糊搜索）
        "新疆维吾尔自治区": "新疆维吾尔",
        "广西壮族自治区": "广西壮族",
        "": "",
    }
    for raw, want in cases.items():
        eq(city_mod.normalize(raw), want, f"T2 normalize({raw!r})")


# ================================================================ T3 城市解析
def test_city_cache() -> None:
    fresh_db()
    fake = FakeClient()
    cfg = make_cfg()
    with SessionLocal() as db:
        info = city_mod.resolve(db, "平顶山市", client=fake, cfg=cfg)
        check(info is not None, "T3-1 能解析出城市")
        eq(fake.geo_calls, 1, "T3-2 首次解析要调一次 GeoAPI")
        eq(info.lat, "33.77", "T3-3 纬度必须截到两位小数（和风要求）")
        eq(info.lon, "113.19", "T3-4 经度必须截到两位小数")

        # 换一种说法（归一化后同一个名字）不得再调 API
        info2 = city_mod.resolve(db, "平顶山的天气", client=fake, cfg=cfg)
        eq(fake.geo_calls, 1, "T3-5 同一城市的另一种说法必须命中缓存")
        eq(info2.id, info.id, "T3-6 两种说法解析到同一个 LocationID")

        # 查不到的城市返回 None，而不是抛异常
        fake.geo_items = []
        check(city_mod.resolve(db, "不存在市", client=fake, cfg=cfg) is None,
              "T3-7 查不到的城市返回 None")

        # 认证失败必须冒出去（不能伪装成"查不到城市"，否则老人会反复重说城市名）
        fake.fail_next = WeatherError("401", "bad key")
        raised = False
        try:
            city_mod.resolve(db, "郑州", client=fake, cfg=cfg)
        except WeatherError:
            raised = True
        check(raised, "T3-8 401 必须抛给上层，不能当成查不到")


# ================================================================ T4 缓存与计费
def test_cache_saves_money() -> None:
    fresh_db()
    fake = FakeClient()
    cfg = make_cfg()
    with SessionLocal() as db:
        r1 = svc.get_now(db, "平顶山", client=fake, cfg=cfg)
        eq(r1["source"], "api", "T4-1 首次查询走上游")
        eq(fake.now_calls, 1, "T4-2 首次查询调一次实时接口")

        # 核心断言：连问 10 次，上游只能被调 1 次
        for _ in range(10):
            r = svc.get_now(db, "平顶山", client=fake, cfg=cfg)
            eq(r["source"], "cache", "T4-3 后续查询必须命中缓存")
        eq(fake.now_calls, 1, "T4-4 ★ 问 11 次只能产生 1 次上游调用（计费红线）")
        eq(fake.geo_calls, 1, "T4-5 geo 也只调一次")

        # 过期后必须重新拉
        row = cache.peek(db, svc.now_key("101180101"))
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        r = svc.get_now(db, "平顶山", client=fake, cfg=cfg)
        eq(r["source"], "api", "T4-6 TTL 过期后必须重新走上游")
        eq(fake.now_calls, 2, "T4-7 过期只多一次调用")

        # 命中计数只统计真实请求，预热刷新不算
        row = cache.peek(db, svc.now_key("101180101"))
        check(int(row.hits) >= 10, f"T4-8 命中次数应累计（实际 {row.hits}）")


# ================================================================ T5 降级
def test_degrade() -> None:
    fresh_db()
    fake = FakeClient()
    cfg = make_cfg()
    with SessionLocal() as db:
        # 先建立缓存，再让上游挂掉
        svc.get_now(db, "平顶山", client=fake, cfg=cfg)
        row = cache.peek(db, svc.now_key("101180101"))
        old_payload = row.payload

        # 让缓存过期，再制造上游故障
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.commit()
        fake.fail_next = WeatherError("network", "连不上")

        r = svc.get_now(db, "平顶山", client=fake, cfg=cfg)
        check(r["ok"] is True, "T5-1 上游故障时应给旧数据而不是报错")
        eq(r["source"], "stale", "T5-2 降级必须标 source=stale")
        check(r["stale"] is True, "T5-3 stale 标志必须为 True（不能假装新鲜）")
        check("稍早" in r["message"], "T5-4 话术里要说明这是稍早的数据")

        # 没有缓存 + 上游故障 = 明确失败话术（而不是崩）
        # ⚠️ 这里必须**换一个新 session**：上面刚 fresh_db() 重建了表，旧 session
        # 的 identity map 里还留着被删行的身份，复用它会在 flush 时抛
        # "Identity map already had an identity for ..." 的 SAWarning。
    fresh_db()
    fake2 = FakeClient()
    with SessionLocal() as db2:
        svc.get_now(db2, "平顶山", client=fake2, cfg=cfg)   # 建 geo 缓存
        row = cache.peek(db2, svc.now_key("101180101"))
        check(row is not None, "T5-5 前置：实时缓存应已写入")
        if row is not None:
            db2.delete(row)
            db2.commit()
        fake2.fail_next = WeatherError("network", "连不上")
        r = svc.get_now(db2, "平顶山", client=fake2, cfg=cfg)
        eq(r["ok"], False, "T5-6 无缓存且上游故障时必须明确失败")
        check("稍后" in r["message"], "T5-7 失败话术要对老人友好")

        # 未配置：不能说"查不到这个城市"，要说"还没开通"
        r = svc.get_now(db2, "平顶山", client=fake2,
                        cfg=make_cfg(host="", key_id=""))
        eq(r["ok"], False, "T5-8 未配置时返回失败")
        eq(r["reason"], "not_configured", "T5-9 未配置的 reason 必须可区分")
        check("开通" in r["message"], "T5-10 未配置的话术要指向配置动作")


# ================================================================ T6 预热
def test_warm_only_recent() -> None:
    """预热的核心承诺：只续「最近真被问过」的城市，冷门城市永远不调用。"""
    fresh_db()
    fake = FakeClient()
    cfg = make_cfg(warm_recent_hours=24, warm_ahead_minutes=10)

    with SessionLocal() as db:
        # 城市 A：刚被问过（热）
        svc.get_now(db, "平顶山", client=fake, cfg=cfg)
        # 城市 B：很久以前被问过（冷）—— 用另一个 LocationID 模拟
        other = dict(PINGDINGSHAN, name="漠河", id="101050901",
                     lat="52.97", lon="122.53")
        fake.geo_items = [other]
        svc.get_now(db, "漠河", client=fake, cfg=cfg)

        calls_before = fake.now_calls

        # 把两条缓存都推到「10 分钟内过期」，但把漠河的 last_hit_at 推到 48 小时前
        now = datetime.now(timezone.utc)
        for key, hit_age in ((svc.now_key("101180101"), 1), (svc.now_key("101050901"), 48)):
            row = cache.peek(db, key)
            row.expires_at = now + timedelta(minutes=3)
            row.last_hit_at = now - timedelta(hours=hit_age)
            db.commit()

        stats = svc.warm_recent(db, client=fake, cfg=cfg, now=now)
        eq(stats["checked"], 1, "T6-1 只有最近被问过的城市进入预热候选")
        eq(stats["refreshed"], 1, "T6-2 热城市必须被刷新")
        eq(fake.now_calls, calls_before + 1, "T6-3 ★ 预热只多调一次（冷门城市绝不调）")

        # 预热不能产生 geo 调用（后台任务不该新增任何"查找"类调用）
        eq(fake.geo_calls, 2, "T6-4 ★ 预热不得新增 GeoAPI 调用")

        # 预热刷新不算"有人问了"，hits 不能涨
        row = cache.peek(db, svc.now_key("101180101"))
        eq(int(row.hits), 1, "T6-5 预热刷新不得计入命中次数")

        # 未配置时预热直接返回，不打任何 API
        calls_before = fake.now_calls
        stats = svc.warm_recent(db, client=fake, cfg=make_cfg(host=""), now=now)
        eq(stats["checked"], 0, "T6-6 未配置时不预热")
        eq(fake.now_calls, calls_before, "T6-7 未配置时零调用")


# ================================================================ T7 口语化
def test_speak() -> None:
    info = city_mod.CityInfo(name="平顶山", id="101180101", lat="33.77",
                             lon="113.19", adm1="河南省", adm2="平顶山")

    fake = FakeClient()
    fake.temperature = 31.71
    data = fake.weather_now("33.77", "113.19")
    text = speak.now_speech(info, data)
    check("32 度" in text, f"T7-1 温度必须取整到 32 度（实际：{text}）")
    check("31.71" not in text, "T7-2 不能把 31.71 这种小数念给老人")
    check("多云" in text, "T7-3 天气现象要念出来")
    check("平顶山" in text, "T7-4 要说清是哪个城市")
    # 体感只差 1.9 度，不该提
    check("体感" not in text, "T7-5 体感与实温差 <3 度时不该提（对老人是噪音）")

    # 小雨 → 必须提醒带伞
    rainy = fake.weather_now("33.77", "113.19")
    rainy["condition"] = {"text": "小雨", "code": "305"}
    rainy["precipitation"] = {"type": "rain"}
    check("带伞" in speak.now_speech(info, rainy), "T7-6 下雨必须提醒带伞")

    # 高温 → 提醒别中午出门
    fake.temperature = 36.0
    hot = fake.weather_now("33.77", "113.19")
    hot["condition"] = {"text": "晴", "code": "100"}
    check("热" in speak.now_speech(info, hot), "T7-7 高温要有可执行建议")

    # 预报：明天小雨要带伞，今天晴不要
    daily = fake.weather_daily("33.77", "113.19", 3)
    dtext = speak.daily_speech(info, daily, days=3)
    check("今天" in dtext and "明天" in dtext and "后天" in dtext,
          f"T7-8 三天都要有标签（实际：{dtext}）")
    # 注意不能断言 "带伞" —— 话术是「带把伞」，中间隔了「把」，
    # 子串匹配会漏（这条断言写错过一次）。抓「伞」这个关键字即可。
    check("伞" in dtext, f"T7-9 预报里有雨就要提醒带伞（实际：{dtext}）")
    check("31 到 22 度" in dtext or "22 到 31 度" in dtext,
          f"T7-10 温度区间要念出来（实际：{dtext}）")


# ================================================================ T8 工具接入
def test_tool_wiring() -> None:
    """验证 tools.py 的 get_weather 真的接到了天气服务上。"""
    from app.voice import tools as voice_tools

    names = [d["function"]["name"] for d in voice_tools.TOOL_DEFS]
    check("get_weather" in names, "T8-1 get_weather 必须注册进 TOOL_DEFS")
    # T8-2 钉的是**纪律**，不是快照：「新工具必须追加在末尾，不许插到中间」。
    #
    # 原来写的是 `eq(names[-1], "get_weather")` —— 那等于把"get_weather 恰好是
    # 最后一个"当成不变式。2026-10-22 加了 enter_standby（同样追加在末尾）之后
    # 这条就红了，而它要防的错（把新工具插到 get_weather 前面，让后面所有工具
    # 定义整体位移、prompt 前缀缓存全失效）根本没发生。
    # ⇒ 改成"相对顺序"：get_weather 必须排在它之前那批工具之后。
    eq(names.index("get_weather") > names.index("confirm_reminder"), True,
       "T8-2 ★ 新工具必须追加在末尾（保住 prompt 前缀缓存）")

    # 设备常驻城市：写在 settings JSON 里，不改表结构
    fresh_db()
    fake = FakeClient()
    cfg = make_cfg()
    with SessionLocal() as db:
        dev = models.Device(device_id="aabbccddeeff", name="测试机",
                            settings={"weather_city": "平顶山"})
        db.add(dev)
        db.commit()
        db.refresh(dev)

        # 不传 city 时用设备常驻城市
        old = os.environ.get("ELDER_QWEATHER_HOST")
        os.environ.update({
            "ELDER_QWEATHER_HOST": cfg.host,
            "ELDER_QWEATHER_PROJECT_ID": cfg.project_id,
            "ELDER_QWEATHER_KEY_ID": cfg.key_id,
            "ELDER_QWEATHER_DEVELOPER_ID": cfg.developer_id,
            "ELDER_QWEATHER_PRIVATE_KEY": Path("/dev/null").as_posix(),
        })
        try:
            # 工具层用的是 get_client() 单例，这里替换成假 client
            from app.weather import client as client_mod
            client_mod._client = fake  # type: ignore[attr-defined]
            res = voice_tools.execute_tool(db, dev.id, "get_weather", {})
            check(res.get("ok") is True, f"T8-3 工具应成功（实际：{res}）")
            check("平顶山" in str(res.get("message")), "T8-4 工具结果要带城市名")

            # daily 分支
            res2 = voice_tools.execute_tool(
                db, dev.id, "get_weather", {"kind": "daily", "days": 3})
            check(res2.get("ok") is True, "T8-5 daily 分支应成功")
            eq(fake.daily_calls, 1, "T8-6 daily 走的是预报接口")
        finally:
            from app.weather import client as client_mod
            client_mod._client = None  # type: ignore[attr-defined]
            for k in ("ELDER_QWEATHER_HOST", "ELDER_QWEATHER_PROJECT_ID",
                      "ELDER_QWEATHER_KEY_ID", "ELDER_QWEATHER_DEVELOPER_ID",
                      "ELDER_QWEATHER_PRIVATE_KEY"):
                os.environ.pop(k, None)
            if old is not None:
                os.environ["ELDER_QWEATHER_HOST"] = old


# ================================================================ T9 配置
def test_config() -> None:
    cfg = load_config()
    eq(cfg.ttl_minutes, 60, "T9-1 默认 TTL 必须是 60 分钟（用户要求）")
    eq(cfg.default_city, "平顶山", "T9-2 默认城市")
    check(cfg.geo_ttl_days >= 7, "T9-3 geo 缓存至少一周（城市坐标不变）")
    check(cfg.timeout_sec <= 10, "T9-4 超时不能太长（语音链路等不起）")

    # host 容错：带协议/结尾斜杠必须被收敛
    os.environ["ELDER_QWEATHER_HOST"] = "https://abc1234.re.qweatherapi.com/"
    try:
        eq(load_config().host, "abc1234.re.qweatherapi.com",
           "T9-5 host 必须收敛成裸域名（否则拼出 https://https://…）")
    finally:
        os.environ.pop("ELDER_QWEATHER_HOST", None)

    # TTL 写错必须回落默认，不能变成 0（TTL=0 = 永不命中缓存）
    os.environ["ELDER_WEATHER_TTL_MIN"] = "abc"
    try:
        eq(load_config().ttl_minutes, 60, "T9-6 TTL 非数字时回落 60 而不是 0")
    finally:
        os.environ.pop("ELDER_WEATHER_TTL_MIN", None)
    os.environ["ELDER_WEATHER_TTL_MIN"] = "0"
    try:
        eq(load_config().ttl_minutes, 60, "T9-7 TTL=0 时回落 60（否则缓存被废掉）")
    finally:
        os.environ.pop("ELDER_WEATHER_TTL_MIN", None)

    # 未配置时 missing 要能列出缺哪些
    gaps = make_cfg(host="", key_id="").missing
    check("ELDER_QWEATHER_HOST" in gaps, "T9-8 missing 要指出缺 HOST")
    check(make_cfg().configured is True, "T9-9 凭据齐备时 configured=True")
    check(make_cfg(private_key_path="", private_key_pem="").configured is False,
          "T9-10 没有私钥时必须判为未配置（否则会带着空签名去打 401）")


# ================================================================ main
def main() -> int:
    tests = [
        ("T1 JWT 签发", test_jwt),
        ("T2 城市名归一化", test_normalize),
        ("T3 城市解析与缓存", test_city_cache),
        ("T4 缓存省钱（计费红线）", test_cache_saves_money),
        ("T5 降级与兜底", test_degrade),
        ("T6 按需预热", test_warm_only_recent),
        ("T7 口语化", test_speak),
        ("T8 工具接入", test_tool_wiring),
        ("T9 配置", test_config),
    ]
    for label, fn in tests:
        print(f"\n=== {label} ===")
        try:
            fn()
        except Exception:  # noqa: BLE001  单个用例炸掉不影响其余
            global _FAIL
            _FAIL += 1
            _FAILED_NAMES.append(f"{label}（异常）")
            print(f"  ✗ 用例异常：")
            traceback.print_exc()

    print(f"\n{'=' * 56}")
    print(f"通过 {_PASS} / 失败 {_FAIL}")
    if _FAIL:
        print("失败项：")
        for n in _FAILED_NAMES:
            print("  -", n)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
