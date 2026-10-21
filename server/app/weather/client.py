"""和风天气 HTTP 客户端（标准库 urllib，不引入 requests/httpx）。

为什么继续用 urllib
------------------
项目里所有出站 HTTP 都走标准库（``llm_gateway.py``、``voice/asr_tts.py`` 都
写明了这个取向）。天气只是多一个调用点，没有理由为它单独拉一个 HTTP 栈 ——
多一个依赖就多一份版本冲突、多一份容器体积、多一次安全审计。

两个必须显式处理的点
------------------
1. **gzip**：和风要求 ``--compressed``。urllib 不会自动解压，必须自己读
   ``Content-Encoding`` 再 ``gzip.decompress``。忘了这步的现象很怪：
   HTTP 200、body 是乱码、``json.loads`` 抛 UnicodeDecodeError。
2. **新版 API 成功响应里没有 ``code`` 字段**。旧版 ``/v7/*`` 有
   ``{"code":"200"}``，新版 ``/weather/v1/*`` 直接返回 ``metadata``/``condition``，
   **只有出错时才有 code**。所以判定成功不能找 code=="200"，要看 HTTP 状态码 +
   响应里有没有 ``error`` 结构。这一条写错会导致「天气永远查不到，但日志一切正常」。
"""
from __future__ import annotations

import gzip
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import WeatherConfig, load as load_config
from .jwt_auth import JwtSigner

log = logging.getLogger("elder.weather.client")


class WeatherError(Exception):
    """和风调用失败。``code`` 是和风的状态码（拿不到时为 "http_<status>"）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    @property
    def is_auth(self) -> bool:
        return self.code == "401"

    @property
    def is_quota(self) -> bool:
        """402 = 超限/余额不足，429 = 超 QPM。两者都该让运维知道，而不是当普通失败。"""
        return self.code in ("402", "429")


class QWeatherClient:
    """和风 API 的最小客户端。一个进程一个实例（token 缓存挂在实例上）。"""

    def __init__(self, cfg: WeatherConfig | None = None) -> None:
        self.cfg = cfg or load_config()
        self._signer = JwtSigner(self.cfg)

    # ------------------------------------------------------------ 底层请求
    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET 一个和风接口，返回解析后的 JSON（dict）。

        ``path`` 必须以 ``/`` 开头，例如 ``/weather/v1/current/33.77/113.19``。
        """
        if not self.cfg.configured:
            raise WeatherError("not_configured",
                               "天气服务未配置：" + "、".join(self.cfg.missing))

        url = f"https://{self.cfg.host}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        # 401 时重签一次再试：token 可能因为服务器时钟回拨/私钥刚轮换而失效。
        # 只重试一次 —— 重试两次以上说明是配置错了（比如 kid 填了 API KEY 的），
        # 反复签只会把 401 刷成日志噪音。
        for attempt in (1, 2):
            token = self._signer.token()
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {token}",
                # 必须声明 gzip：和风的网关会按这个头决定是否压缩
                "Accept-Encoding": "gzip",
                "User-Agent": "elder-companion/1.0 (+qweather)",
            })
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout_sec) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                        raw = gzip.decompress(raw)
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = b""
                try:
                    body = exc.read()
                    if exc.headers.get("Content-Encoding", "").lower() == "gzip":
                        body = gzip.decompress(body)
                except Exception:  # noqa: BLE001  读 body 失败不该盖住真实错误
                    body = b""

                code, message = self._parse_error(exc.code, body)
                if code == "401" and attempt == 1:
                    log.warning("和风返回 401，作废当前 JWT 后重签重试一次")
                    self._signer.invalidate()
                    continue
                raise WeatherError(code, message) from exc
            except urllib.error.URLError as exc:
                # 网络层失败（DNS/超时/连接被拒）。语音链路里这是常态，
                # 不该用 ERROR 级别刷屏，但必须带上是哪一步失败
                raise WeatherError("network", f"连不上和风：{exc.reason}") from exc
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise WeatherError("bad_json", f"和风返回的不是合法 JSON：{exc}") from exc
        raise WeatherError("401", "和风认证失败（重签后仍失败，请检查 kid/私钥）")

    @staticmethod
    def _parse_error(status: int, body: bytes) -> tuple[str, str]:
        """从错误响应里抠出和风的 code/message。拿不到就退回 HTTP 状态码。"""
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return f"http_{status}", f"HTTP {status}"
        # 新版错误形如 {"error":{"code":"401","message":"..."}}，
        # 旧版形如 {"code":"401","message":"..."}。两种都认。
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            return str(err.get("code") or f"http_{status}"), str(err.get("message") or "")
        if isinstance(data, dict):
            return str(data.get("code") or f"http_{status}"), str(data.get("message") or "")
        return f"http_{status}", f"HTTP {status}"

    # ------------------------------------------------------------ 业务接口
    def city_lookup(self, name: str, number: int = 5) -> list[dict[str, Any]]:
        """城市搜索。返回候选列表（每项含 name/id/lat/lon/adm1/adm2）。"""
        data = self._get("/geo/v2/city/lookup", {
            "location": name,
            "number": number,
            "lang": "zh",
            # 限定中国：本项目面向国内老人，同名城市（比如"山西"）不该跑到国外
            "range": "cn",
        })
        return list(data.get("location") or [])

    def weather_now(self, lat: str, lon: str) -> dict[str, Any]:
        """实时天气。"""
        return self._get(f"/weather/v1/current/{lat}/{lon}", {"lang": "zh"})

    def weather_daily(self, lat: str, lon: str, days: int = 3) -> dict[str, Any]:
        """每日预报（新版 ``days`` 取值 1-10，默认 7）。"""
        return self._get(f"/weather/v1/daily/{lat}/{lon}",
                         {"days": max(1, min(10, days)), "lang": "zh"})


_client: QWeatherClient | None = None


def get_client() -> QWeatherClient:
    """进程级单例（JWT 缓存挂在实例上，每次新建会让缓存失效）。"""
    global _client
    if _client is None:
        _client = QWeatherClient()
    return _client


def reset_client() -> None:
    """丢弃单例。测试与「配置改了想立刻生效」时用。"""
    global _client
    _client = None
