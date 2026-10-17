"""密码哈希、JWT 签发、激活码生成与校验。"""
from __future__ import annotations

import hashlib
import hmac
import os
import random
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from .config import (
    ACT_CODE_LEN,
    ACT_CODE_TTL_MINUTES,
    DEVICE_TOKEN_TTL,
    JWT_ALG,
    JWT_SECRET,
    LETTERS,
    USER_TOKEN_TTL,
)

# ============================================================ 密码
# 用标准库 PBKDF2-SHA256，避免引入 bcrypt / argon2 的编译依赖。
_PBKDF2_ROUNDS = 200_000


def hash_password(raw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(raw: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, dk_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", raw.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:  # noqa: BLE001
        return False


# ============================================================ 计时对齐（防用户名枚举）
#
# ⚠️ 为什么需要它（这是可利用的侧信道，不是理论风险）
# ------------------------------------------------------
# ``login()`` 里原本是 ``if user and verify_password(...)``：用户**不存在**时
# ``and`` 短路，口令哈希**根本不跑**，整个请求几毫秒就返回；而用户**存在**但口令
# 错时，PBKDF2-SHA256 要跑满 200000 轮（实测约 145ms）。两条路径差一个数量级，
# 攻击者拿同一个口令逐个试用户名、取几十次采样的中位数，就能把有效用户名筛出来。
# 对策：用户不存在时也付一次**完全等价**的 PBKDF2 代价。
#
#: dummy 明文。**进程启动时生成一次**、永不泄露。
#: ``secrets.token_urlsafe(16)`` 出 16 字节熵、base64 后约 22 字符，
#: 与本项目生成的随机种子口令（``main._seed()`` 用的是同一个函数）**完全同量级**。
#:
#: ⚠️ 必须"模块加载时生成一次"而**不能**"每次请求现场生成"：生成它本身就要跑一次
#: PBKDF2（约 145ms），放进请求里等于给**每个**请求额外加一次哈希 —— 那不是对齐，
#: 是把登录端点整体变慢一倍（成功登录也一样）。
#:
#: ⚠️ 熵必须足够（``secrets`` 走 CSPRNG）：若这个明文可预测，攻击者就能构造出与它
#: 逐字相等的口令，在真实校验路径上"命中"dummy 哈希，让两条路径重新分叉。
_DUMMY_PLAINTEXT = secrets.token_urlsafe(16)
_DUMMY_HASH = hash_password(_DUMMY_PLAINTEXT)


def burn_password_check_time() -> None:
    """跑一次与真实口令校验**完全等价**的 PBKDF2 计算，然后**丢弃结果**。

    供「用户不存在」的分支调用，把它的耗时对齐到「用户存在 + 口令错」那条路径上，
    消除登录接口的用户名枚举计时侧信道（背景见本节上方那段说明）。

    ⚠️ 为什么结果必须丢弃、且本函数**永远只返回 ``None``**：
    ``_DUMMY_PLAINTEXT`` 与 ``_DUMMY_HASH`` 是**配对**的，所以这里的
    ``verify_password`` 实际会返回 ``True``。这个 ``True`` 不代表任何一次真实校验
    的结果，它只是"把 PBKDF2 跑完"这件事的副产品。若日后有人把本函数改成
    ``return verify_password(...)``、或写成 ``if not burn_password_check_time()``，
    就会凭空多出一条"口令正确"的分支 —— 那就是认证绕过。所以返回值在函数内部就被
    丢掉，签名上也只承诺 ``None``。
    """
    verify_password(_DUMMY_PLAINTEXT, _DUMMY_HASH)


# ============================================================ JWT
def _issue(payload: dict, ttl: timedelta) -> str:
    now = datetime.now(timezone.utc)
    body = {**payload, "iat": now, "exp": now + ttl}
    return jwt.encode(body, JWT_SECRET, algorithm=JWT_ALG)


def issue_user_token(user_id: int, username: str, is_admin: bool = False) -> str:
    return _issue({"sub": str(user_id), "typ": "user", "username": username,
                   "admin": bool(is_admin)}, USER_TOKEN_TTL)


def issue_device_token(device_pk: int, device_id: str) -> str:
    return _issue({"sub": str(device_pk), "typ": "device", "device_id": device_id},
                  DEVICE_TOKEN_TTL)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.PyJWTError:
        return None


# ============================================================ 激活码
def luhn_check_digit(digits11: str) -> int:
    """与固件 elder_activation.c 中 luhn_check_digit() 完全一致。

    digits11: 11 位数字字符串（code[1..11]）
    """
    total = 0
    for i in range(11):
        d = int(digits11[i])
        if (11 - i) % 2 == 0:      # index 1,3,5,7,9 加倍
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - (total % 10)) % 10


def generate_activation_code(prefix: str | None = None) -> str:
    """生成 13 位激活码：1 位字母 + 11 位随机数字 + 1 位 Luhn 校验位。"""
    letter = prefix if prefix else secrets.choice(LETTERS)
    letter = letter.upper()
    if letter in ("I", "O"):
        letter = secrets.choice(LETTERS)
    digits = "".join(secrets.choice("0123456789") for _ in range(11))
    return letter + digits + str(luhn_check_digit(digits))


def verify_activation_code(code: str) -> bool:
    """校验激活码格式与 Luhn 校验位（不查库，纯算法）。"""
    if not code or len(code) != ACT_CODE_LEN:
        return False
    c0 = code[0].upper()
    if not c0.isalpha() or c0 in ("I", "O"):
        return False
    body = code[1:]
    if not body.isdigit():
        return False
    return int(body[11]) == luhn_check_digit(body[:11])


def normalize_code(code: str) -> str:
    """容错输入：去空格、去分隔符、首字母大写。"""
    cleaned = "".join(ch for ch in code.strip() if ch.isalnum())
    return cleaned.upper() if cleaned else ""


def format_code(code: str) -> str:
    """显示为 A-3829-4471-0056，降低 320x240 小屏上的抄写错误率。"""
    if len(code) != ACT_CODE_LEN:
        return code
    d = code[1:]
    return f"{code[0].upper()}-{d[0:4]}-{d[4:8]}-{d[8:12]}"


def code_expires_at(now: datetime | None = None) -> datetime:
    base = now or datetime.now(timezone.utc)
    return base + timedelta(minutes=ACT_CODE_TTL_MINUTES)
