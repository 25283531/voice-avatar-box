"""冒烟：求助邮箱不许写死 —— env → 管理端 SMTP → 空串。

背景（用户 2026-10-20 明确要求）
------------------------------
SMTP 是在**生产 Docker 实例的管理端**配好的（存 DB ``smtp_config`` 表），代码里
不许写死任何邮箱。而 ``config.py`` 原来有一行

    SUPPORT_EMAIL = os.getenv("ELDER_SUPPORT_EMAIL", "support@voice.250886.xyz")

该域名当前**不可达**（生产是 IP 直连、没有 DNS 记录）⇒ 未设环境变量的部署会把
「忘记用户名 → 发邮件给管理员」这条**唯一**能解绑设备的通道指向死信箱，而页面上
照样渲染出一个可点的 mailto，谁都不会发现信没到。

断言
----
  S1 全新库、未设 env ⇒ ``/api/support-contact`` 返回 ``{{"email": ""}}``，
     且响应体里**不出现** ``voice.250886.xyz``（写死值不许以任何形式回来）
  S2 启动日志里有一条「未解析到管理员联系邮箱」的 WARNING（**启动期**打，
     不是每请求打 —— 这个端点会被登录页每次渲染调用）
  S3 ``from_addr`` 与 ``notify_to`` 填成**两个不同地址** ⇒ 返回 **from_addr**
     （管理员对外身份；notify_to 是工单内部收件箱，生产上是个私人 QQ 邮箱，
     渲染到公网登录页上就是泄漏）
  S3b ``from_addr`` 为空、只有 ``notify_to`` ⇒ 回落到 ``notify_to``（兜底仍工作）
  S4 设了 env ⇒ env 优先于 DB（同一个库上换一个进程，DB 里两列都还在）
  S5 设了 env 时启动日志打的是 INFO（不再误报"没有求助通道"）

⚠️ S3 的方向**曾经写反过**（断言返回 notify_to），后果是门禁绿着保护了错误行为：
公网登录页返回了私人 QQ 邮箱，而测试说"这是对的"。所以这一条的期望值本身
就是被测契约的一部分，改代码时**必须同时**看这里，别把红当成"测试过时"。

为什么 S4 要重启一个进程：``SUPPORT_EMAIL`` 在 import 期求值，运行中改
``os.environ`` 不会生效 —— 在同一进程里"改环境变量再看结果"测的是测试代码
自己，不是服务端。所以：**同一个库文件，两个进程**。

用法：
    python tools/smoke_support_email.py [--port 8131]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_common import unlink_retry  # noqa: E402
from smoke_ports import (  # noqa: E402
    DEFAULT_PORTS,
    detect_addr_in_use,
    guard_or_die,
    guard_port,
    is_port_free,
    release_port,
    resolve_port,
)

_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
#: **两个阶段共用同一个库文件**：S4 要证明「DB 里有 notify_to，env 仍然赢」，
#: 换库就把那个前提洗掉了。
DB_PATH = Path(os.environ.get("SMOKE_DB", str(DB_DIR / f"elder_support_{os.getpid()}.db")))

PORT = resolve_port("smoke_support_email")
#: 当前阶段的地址。由 ``Server.__enter__`` 按实际端口重写 —— 两个阶段**不能**
#: 复用同一个端口：上一段 terminate 之后端口上还挂着 TIME_WAIT 的连接，
#: Windows 下 ``SO_EXCLUSIVEADDRUSE`` 会让下一次 bind 直接失败（实测；
#: ``smoke_hw_model_truncation`` 也踩过同一个坑，同样用"换一个端口"解决）。
BASE = f"http://127.0.0.1:{PORT}"

LEGACY_PW = "admin123"
#: 写死的那个地址：任何响应/日志里出现它都算回归。
DEAD_DOMAIN = "voice.250886.xyz"
ENV_EMAIL = "help@elder-support.example.com"
NOTIFY_TO = "admin@elder.example.com"
FROM_ADDR = "noreply@elder.example.com"

_passed: list[str] = []
_failed: list[str] = []
_skipped: list[tuple[str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))
    (_passed if ok else _failed).append(name)
    return ok


def skip(name: str, why: str) -> None:
    print(f"  ⊘ SKIP {name}  [{why}]")
    _skipped.append((name, why))


def req(method: str, path: str, body: dict | None = None, token: str | None = None,
        ) -> tuple[int, str, dict | None]:
    """返回 (状态码, 原始响应文本, 解析后的 JSON)。

    原始文本是**必须**留的：断言「响应里不出现某个地址」只有看原文才能做。
    """
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with _LOCAL_OPENER.open(r, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            code = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        code = e.code
    try:
        return code, raw, (json.loads(raw) if raw.strip() else None)
    except Exception:  # noqa: BLE001
        return code, raw, None


def wait_ready(timeout: float = 120.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            code, _, _ = req("GET", "/api/health")
            if code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


class Server:
    """一次服务实例。``tag`` 只用于区分日志文件名。"""

    def __init__(self, tag: str, support_email: str | None, port: int | None = None) -> None:
        self.tag = tag
        self.support_email = support_email
        self.port = port or PORT
        self.log = DB_DIR / f"support_{tag}_{os.getpid()}.log"
        self.proc: subprocess.Popen | None = None
        self.fp = None

    def __enter__(self) -> "Server":
        global BASE
        BASE = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ)
        env.pop("MSYSTEM", None)
        env.pop("ELDER_SUPPORT_EMAIL", None)     # 阶段 A 必须完全没有它
        env["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
        env["ELDER_SEED_ADMIN_PASSWORD"] = LEGACY_PW
        env["ELDER_JWT_SECRET"] = "smoke-support-secret-0123456789abcdef"
        env["ELDER_MAIL_FAKE"] = "1"
        if self.support_email is not None:
            env["ELDER_SUPPORT_EMAIL"] = self.support_email

        print(f"[boot:{self.tag}] port={self.port} env={self.support_email!r} db={DB_PATH.name}")
        guard_or_die(self.port, "smoke_support_email.py")
        hold, hint = guard_port(self.port)
        if hold is None:
            print("[boot] " + hint.replace("\n", "\n[boot] "))
            raise SystemExit(1)
        release_port(hold, self.port)

        self.fp = self.log.open("w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--log-level", "info"],
            cwd=str(SERVER), env=env,
            stdout=self.fp, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace",
        )
        if not wait_ready():
            print("[boot] server did not become ready. log tail:")
            print(self.tail())
            raise SystemExit(1)
        if detect_addr_in_use(self.tail()):
            print(f"[boot] ✗ 端口 {self.port} bind 失败，结果不可信")
            raise SystemExit(1)
        # 启动自检的日志是在 lifespan 里打的，等它落盘再读
        time.sleep(0.5)
        print(f"[boot:{self.tag}] ready\n")
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


def admin_token() -> str | None:
    code, _, body = req("POST", "/api/auth/login",
                        {"username": "admin", "password": LEGACY_PW})
    return (body or {}).get("access_token") if code == 200 else None


def _pick_free_port(base_port: int) -> int:
    """给第二阶段挑一个空闲且**未登记**的端口（登记过的留给别的脚本）。"""
    taken = set(DEFAULT_PORTS.values())
    for candidate in range(base_port + 1, base_port + 60):
        if candidate in taken:
            continue
        if is_port_free(candidate):
            return candidate
    return base_port + 1          # 兜底：交给 guard_or_die 报错


def main() -> int:
    if DB_PATH.exists():
        unlink_retry(DB_PATH)
    try:
        # ============================================ 阶段 A：全新库、无 env
        print("--- 阶段 A：全新库 + 未设 ELDER_SUPPORT_EMAIL ---")
        with Server("a", None) as srv:
            code, raw, body = req("GET", "/api/support-contact")
            check("S1 未配置时端点返回 {'email': ''}（不是任何兜底地址）",
                  code == 200 and (body or {}).get("email") == "",
                  f"http={code} body={raw[:80]!r}")
            check(f"S1 响应体里不出现写死的 {DEAD_DOMAIN}（死信箱不许再回来）",
                  DEAD_DOMAIN not in raw, f"raw={raw[:120]!r}")
            log_a = srv.tail()
            check("S2 启动日志里有「未解析到管理员联系邮箱」的 WARNING（启动期打，不每请求打）",
                  "未解析到管理员联系邮箱" in log_a, "")

            tok = admin_token()
            check("阶段 A：管理员登录成功", bool(tok))
            if tok:
                code, raw, _ = req("PATCH", "/api/admin/mail/smtp",
                                   {"host": "127.0.0.1", "port": 9,
                                    "from_addr": FROM_ADDR, "notify_to": NOTIFY_TO,
                                    "enabled": False}, token=tok)
                check("阶段 A：管理端写入 SMTP（from_addr 与 notify_to 填成两个不同地址）",
                      code == 200, f"http={code} body={raw[:120]!r}")
                code, raw, body = req("GET", "/api/support-contact")
                check("S3 ★ 两列都配了时返回 from_addr（管理员对外身份），"
                      "**不是** notify_to（工单内部收件箱）",
                      code == 200 and (body or {}).get("email") == FROM_ADDR,
                      f"http={code} email={(body or {}).get('email')!r}")
                check(f"S3 此时响应里也不出现 {DEAD_DOMAIN}", DEAD_DOMAIN not in raw, "")
                check("S3 响应里不得出现 notify_to 那个内部收件箱地址",
                      NOTIFY_TO not in raw, f"raw={raw[:120]!r}")

                # S3b：from_addr 清空、只留 notify_to ⇒ 兜底必须仍然工作
                code, raw, _ = req("PATCH", "/api/admin/mail/smtp",
                                   {"from_addr": ""}, token=tok)
                check("阶段 A：把 from_addr 清空", code == 200, f"http={code}")
                code, raw, body = req("GET", "/api/support-contact")
                check("S3b from_addr 为空时回落到 notify_to（兜底没被写丢）",
                      code == 200 and (body or {}).get("email") == NOTIFY_TO,
                      f"http={code} email={(body or {}).get('email')!r}")

                # 还原成两列都有，供阶段 B 证明「DB 里有地址，env 仍然赢」
                code, _, _ = req("PATCH", "/api/admin/mail/smtp",
                                 {"from_addr": FROM_ADDR}, token=tok)
                check("阶段 A：恢复 from_addr（阶段 B 的前提）", code == 200,
                      f"http={code}")

        # ============================================ 阶段 B：同一库 + 设 env
        print("\n--- 阶段 B：同一个库 + 设 ELDER_SUPPORT_EMAIL（env 必须赢）---")
        with Server("b", ENV_EMAIL, port=_pick_free_port(PORT)) as srv:
            code, raw, body = req("GET", "/api/support-contact")
            check("S4 设了 env ⇒ 返回 env 的地址（优先于 DB 里的 from_addr / notify_to）",
                  code == 200 and (body or {}).get("email") == ENV_EMAIL,
                  f"http={code} email={(body or {}).get('email')!r}")
            log_b = srv.tail()
            check("S5 设了 env 时启动日志打的是 INFO，且不再误报「未解析到」",
                  "【求助通道】" in log_b and "未解析到管理员联系邮箱" not in log_b,
                  "")
    finally:
        for p in (DB_PATH,):
            try:
                if p.exists():
                    unlink_retry(p)
            except Exception:  # noqa: BLE001
                pass
        for p in DB_DIR.glob(f"support_[ab]_{os.getpid()}.log"):
            try:
                unlink_retry(p)
            except Exception:  # noqa: BLE001
                pass

    print()
    print(f"==== {len(_passed)} passed, {len(_skipped)} skipped, {len(_failed)} failed ====")
    for name, why in _skipped:
        print(f"  SKIP: {name} —— {why}")
    for name in _failed:
        print(f"  FAILED: {name}")
    return 0 if not _failed else 1


if __name__ == "__main__":
    sys.exit(main())
