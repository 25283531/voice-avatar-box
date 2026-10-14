"""给仓库写入 Actions Secrets（本机 HTTPS 到 github.com 被拦截，只能走 API）。

GitHub 要求 secret 值用仓库公钥做 libsodium sealed box 加密后再 PUT，
所以本机需要 PyNaCl（托管 Python 与 venv 都已装 1.6.2）。

用法（推荐用环境变量传值，避免明文出现在命令行历史里）：
    DCLOUD_USERNAME=xxx DCLOUD_PASSWORD=yyy python tools/gh_secrets.py
    python tools/gh_secrets.py NAME=value NAME2=value2

需要 GH_TOKEN（或 %TEMP%\\gh_token.txt）。
"""
from __future__ import annotations

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gh_api import api  # noqa: E402

REPO = "25283531/voice-avatar-box"
API = f"/repos/{REPO}"

# 默认从环境变量搬这几个（与 app-pack 工作流里用到的名字一致）
DEFAULT_NAMES = ["DCLOUD_USERNAME", "DCLOUD_PASSWORD"]


def encrypt(public_key_b64: str, value: str) -> str:
    from nacl import encoding, public

    pub = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(pub)
    return base64.b64encode(sealed.encrypt(value.encode("utf-8"))).decode("utf-8")


def put_secret(name: str, value: str) -> None:
    meta = api(f"{API}/actions/secrets/public-key")
    body = {
        "encrypted_value": encrypt(meta["key"], value),
        "key_id": meta["key_id"],
    }
    api(f"{API}/actions/secrets/{name}", "PUT", body)
    print(f"  ✓ {name} 已写入（{len(value)} 字符）")


def main() -> int:
    pairs: list[tuple[str, str]] = []
    for a in sys.argv[1:]:
        if "=" in a:
            k, v = a.split("=", 1)
            pairs.append((k, v))
    if not pairs:
        pairs = [(n, os.environ[n]) for n in DEFAULT_NAMES if os.environ.get(n)]
    if not pairs:
        raise SystemExit("没有可写入的 secret：用 NAME=value 传参，或设置 DCLOUD_USERNAME/DCLOUD_PASSWORD 环境变量")

    for name, value in pairs:
        put_secret(name, value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
