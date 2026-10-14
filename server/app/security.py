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
