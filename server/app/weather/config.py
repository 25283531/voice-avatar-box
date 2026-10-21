"""和风天气（QWeather）的配置。

为什么天气配置**不**并进 ``app/config.py``
------------------------------------------
``app/config.py`` 已经是「服务端能不能起来」级别的配置（数据库、时区、限速）。
天气是**可选子系统**：没配凭据时服务该正常启动，只是回答「天气服务还没开通」。
把一堆可选凭据混进主配置，会让「启动必需」和「功能可选」两件事看起来同级 ——
之后任何人都可能误以为缺 key 是启动故障。所以这里自洽地读自己的环境变量。

私钥放在哪里（用户 2026-10-21 的明确要求）
----------------------------------------
私钥**不能**进项目目录（仓库会推到 GitHub）。所以：

* 首选 ``ELDER_QWEATHER_PRIVATE_KEY``：直接给 PEM 全文，容器部署用这个
  （挂文件再读也行，但环境变量在 docker-compose / K8s Secret 里最省事）；
* 其次 ``ELDER_QWEATHER_PRIVATE_KEY_PATH``：给路径；
* 兜底：**仓库父目录**的 ``qweather-ed25519-private.pem``。
  本项目在 ``G:/workbuddy/语音助手``，父目录就是 ``G:/workbuddy`` —— 正是
  私钥的落地位置。用「相对仓库根往上找」而不是写死 ``G:\\workbuddy``：
  写死盘符的代码换台机器就废，而且把本机路径泄进仓库。

三条都不成立时 ``configured=False``，调用方据此走「未配置」话术，**不抛异常**。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("elder.weather")

#: 仓库根（``server/app/weather/config.py`` 往上四层）
_REPO_ROOT = Path(__file__).resolve().parents[3]
#: 仓库**父目录** —— 私钥的默认落地处（刻意在项目之外，不会被 git 收走）
_KEY_DIR = _REPO_ROOT.parent

DEFAULT_PRIVATE_KEY_NAME = "qweather-ed25519-private.pem"


def _int_env(name: str, default: int) -> int:
    """读整数环境变量；解析失败回落 default，且不接受非正数。

    与 ``app/config.py:_int_env`` 同一取向：TTL 写错时必须是「保持默认」，
    不能变成 0 —— TTL=0 意味着**永不命中缓存**，等于把「按需缓存」直接废掉，
    而日志上看起来一切正常，只有账单会说话。
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("环境变量 %s=%r 不是整数，回落默认值 %s", name, raw, default)
        return default
    if value <= 0:
        log.warning("环境变量 %s=%r 必须为正数，回落默认值 %s", name, raw, default)
        return default
    return value


def _default_key_path() -> Path:
    """默认私钥路径：仓库父目录下的 qweather-ed25519-private.pem。"""
    return _KEY_DIR / DEFAULT_PRIVATE_KEY_NAME


@dataclass(frozen=True)
class WeatherConfig:
    """一次进程生命周期内不变的天气配置（frozen：避免运行中被改写）。"""

    #: API Host，形如 ``abcd1234.re.qweatherapi.com``（**不含** https:// 与结尾斜杠）
    host: str = ""
    #: 项目 ID —— JWT 的 ``sub``
    project_id: str = ""
    #: 凭据 ID —— JWT header 的 ``kid``（**必须是标了 JWT 的那条凭据**，不是 API KEY 的）
    key_id: str = ""
    #: 开发者 ID —— JWT 的 ``iss``（控制台-设置里 Q 开头的 10 位）
    developer_id: str = ""
    #: 私钥 PEM 全文（优先于路径）
    private_key_pem: str = ""
    #: 私钥文件路径
    private_key_path: str = ""
    #: 缓存有效期（分钟）—— 用户要求 60
    ttl_minutes: int = 60
    #: 城市地理信息缓存有效期（天）。城市坐标几乎不变，给长一点，
    #: 否则「平顶山」每次冷启动都要多打一次 geo 接口
    geo_ttl_days: int = 30
    #: 默认城市（老人没说城市名时用）。空串 = 必须问老人
    default_city: str = ""
    #: 单次 HTTP 超时（秒）。语音链路里 6 秒已经很长，宁可答「查不到」也别让老人干等
    timeout_sec: float = 6.0
    #: 预热：只刷新「最近 N 小时内被真实问过」的城市
    warm_recent_hours: int = 24
    #: 预热提前量（分钟）：缓存还剩这么多就提前续，避免「刚好在临界点被问到」
    warm_ahead_minutes: int = 10
    #: 总开关（默认开；没有凭据时自然不可用）
    enabled: bool = True

    @property
    def missing(self) -> list[str]:
        """还缺哪些必填项（用于日志与后台提示，顺序即填写顺序）。"""
        gaps: list[str] = []
        if not self.host:
            gaps.append("ELDER_QWEATHER_HOST")
        if not self.project_id:
            gaps.append("ELDER_QWEATHER_PROJECT_ID")
        if not self.key_id:
            gaps.append("ELDER_QWEATHER_KEY_ID")
        if not self.developer_id:
            gaps.append("ELDER_QWEATHER_DEVELOPER_ID")
        if not (self.private_key_pem or self.private_key_path):
            gaps.append("ELDER_QWEATHER_PRIVATE_KEY(_PATH)")
        return gaps

    @property
    def configured(self) -> bool:
        """凭据是否齐备。``enabled=False`` 时一律视为未配置。"""
        return self.enabled and not self.missing

    def load_private_key(self) -> str:
        """取私钥 PEM 全文。优先环境变量，其次文件。

        读不到就抛 ``FileNotFoundError`` —— 这是**配置错误**（不是「暂时查不到
        天气」），必须让它显式失败：静默返回空串会让 JWT 签名变成一条
        「设备能连、天气永远 401」的哑故障，排查成本极高。
        """
        if self.private_key_pem.strip():
            return self.private_key_pem
        path = Path(self.private_key_path) if self.private_key_path else _default_key_path()
        if not path.is_file():
            raise FileNotFoundError(
                f"和风私钥不存在：{path}（请设置 ELDER_QWEATHER_PRIVATE_KEY 或 "
                f"ELDER_QWEATHER_PRIVATE_KEY_PATH）")
        return path.read_text(encoding="utf-8")


def _normalize_host(raw: str) -> str:
    """把用户可能写的 ``https://xxx/`` 收敛成裸 host。

    这层容错不是洁癖：和风控制台给的是一串裸 host，但人复制时常常连协议一起带上。
    后面拼 URL 用的是 ``https://{host}{path}``，不收敛就会出现
    ``https://https://xxx/...`` 这种一眼看不出问题的 404。
    """
    host = raw.strip()
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host.rstrip("/")


def load() -> WeatherConfig:
    """从环境变量读一次配置（每次调用都重新读，便于测试注入）。"""
    key_pem = os.getenv("ELDER_QWEATHER_PRIVATE_KEY", "").strip()
    key_path = os.getenv("ELDER_QWEATHER_PRIVATE_KEY_PATH", "").strip()

    # 两个都没设时，若默认位置真有私钥就自动用上（本地开发零配置）
    if not key_pem and not key_path and _default_key_path().is_file():
        key_path = str(_default_key_path())

    return WeatherConfig(
        host=_normalize_host(os.getenv("ELDER_QWEATHER_HOST", "")),
        project_id=os.getenv("ELDER_QWEATHER_PROJECT_ID", "").strip(),
        key_id=os.getenv("ELDER_QWEATHER_KEY_ID", "").strip(),
        developer_id=os.getenv("ELDER_QWEATHER_DEVELOPER_ID", "").strip(),
        private_key_pem=key_pem,
        private_key_path=key_path,
        ttl_minutes=_int_env("ELDER_WEATHER_TTL_MIN", 60),
        geo_ttl_days=_int_env("ELDER_WEATHER_GEO_TTL_DAYS", 30),
        default_city=os.getenv("ELDER_WEATHER_DEFAULT_CITY", "平顶山").strip(),
        timeout_sec=float(_int_env("ELDER_WEATHER_TIMEOUT_SEC", 6)),
        warm_recent_hours=_int_env("ELDER_WEATHER_WARM_HOURS", 24),
        warm_ahead_minutes=_int_env("ELDER_WEATHER_WARM_AHEAD_MIN", 10),
        enabled=os.getenv("ELDER_WEATHER_ENABLE", "1").strip() not in ("0", "false", "False", ""),
    )
