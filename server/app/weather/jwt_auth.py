"""和风天气的 JWT 签发（EdDSA / Ed25519）。

为什么自己管 token 的刷新节奏
---------------------------
和风允许有效期最长 24 小时，但**签一次用满 24 小时**并不划算：私钥一旦泄漏，
24 小时窗口太长；而每次都重签又要反复做 Ed25519 签名 + PEM 解析。折中取
15 分钟有效期、提前 60 秒换新 —— 这是和风官方 Python 示例的取值，也是
「够安全、签名开销可忽略」的常见平衡点。

线程安全
--------
``client.py`` 的调用发生在线程池（``asyncio.to_thread``）里，uvicorn 也可能
多 worker。这里用一把进程内锁包住「读缓存 → 判定是否要重签 → 写回」，
避免两个线程同时签出两份 token 又互相覆盖（功能上无害，但会让日志里出现
成对的「重新签发」记录，误导排查）。
"""
from __future__ import annotations

import logging
import threading
import time

import jwt

from .config import WeatherConfig

log = logging.getLogger("elder.weather.jwt")

#: token 有效期（秒）。和风上限 86400，这里刻意取短
TOKEN_TTL_SEC = 900
#: 提前多少秒换新。留足余量：设备/服务器时钟哪怕有几十秒漂移也不会踩到 401
REFRESH_AHEAD_SEC = 60
#: ``iat`` 往前挪多少秒。和风文档明确建议 -30 秒，用来抵消双方时钟误差
IAT_SKEW_SEC = 30


class JwtSigner:
    """按需签发并缓存 JWT。一个进程一个实例即可（见 ``client.get_client``）。"""

    def __init__(self, cfg: WeatherConfig) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._token: str = ""
        #: 到点就重签（wall clock，秒）
        self._renew_at: float = 0.0
        #: 私钥只在第一次真正要用时才读盘（未配置天气的部署不该碰这个文件）
        self._private_key: str | None = None

    def _key(self) -> str:
        if self._private_key is None:
            self._private_key = self._cfg.load_private_key()
        return self._private_key

    def token(self) -> str:
        """返回一个当前可用的 JWT（必要时重签）。"""
        now = time.time()
        with self._lock:
            if self._token and now < self._renew_at:
                return self._token
            iat = int(now) - IAT_SKEW_SEC
            payload = {
                # iss = 开发者 ID，sub = 项目 ID。**两者不能搞反**：
                # 反了以后和风返回 401，而错误信息不会告诉你是哪个字段错了。
                "iss": self._cfg.developer_id,
                "sub": self._cfg.project_id,
                "iat": iat,
                "exp": iat + TOKEN_TTL_SEC,
            }
            # header 只放 kid；alg 由 PyJWT 按 algorithm 自动写成 EdDSA。
            # PyJWT 还会补一个 typ:JWT —— 和风文档说「若包含 typ 必须为 JWT」，
            # 我们的值正是 JWT，合规。
            self._token = jwt.encode(
                payload, self._key(), algorithm="EdDSA",
                headers={"kid": self._cfg.key_id})
            self._renew_at = now + TOKEN_TTL_SEC - REFRESH_AHEAD_SEC
            log.debug("已签发和风 JWT，%ss 后自动换新", TOKEN_TTL_SEC - REFRESH_AHEAD_SEC)
            return self._token

    def invalidate(self) -> None:
        """作废当前 token（收到 401 时调用，强制下次重签）。"""
        with self._lock:
            self._token = ""
            self._renew_at = 0.0
