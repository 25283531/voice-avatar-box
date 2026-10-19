"""冒烟：``/api/voice/ota`` 的「新建激活码」配额。

为什么要单独一个脚本
--------------------
``/api/voice/ota`` **无鉴权**（设备此刻还没有 token），身份就是请求头里的
``Device-Id``（MAC）。未绑定分支会按 MAC **建一行**激活码 —— 于是「拿随机
MAC 刷这个端点」既是一条不需要凭证的写库放大路径，又能批量收割别人的
待绑定码。配额由此而来（``config.OTA_NEW_CODE_PER_HOUR``）。

本脚本要证明的**核心不是「能拦住」**，而是「**不会误伤正常设备**」：
设备端 ``GUIDE_POLL_MS = 15s``，单台设备一小时轮询 240 次。若「复用已有码」
也被计入配额，正常设备会在 25 分钟内被自己的轮询打满，表现为
「设备突然不显示激活码」—— 那正是这一轮在修的症状。所以断言里
**复用路径必须在配额耗尽后依然 200**。

用法：
    python tools/smoke_ota_ratelimit.py

阈值通过 ``ELDER_OTA_NEW_CODE_PER_HOUR`` 注入（本脚本用 3，便于快速耗尽）。
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
from smoke_ports import detect_addr_in_use, guard_or_die, guard_port, release_port, resolve_port  # noqa: E402

# 与其它冒烟同一条铁律：本地请求绝不走代理，否则「访问自己刚起的服务」
# 被绕到代理一跳，表现为 TimeoutError 而零断言失败。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
DB_PATH = Path(os.environ.get("SMOKE_DB", str(DB_DIR / f"elder_ota_rl_{os.getpid()}.db")))
if DB_PATH.exists():
    unlink_retry(DB_PATH)

PORT = resolve_port("smoke_ota_ratelimit")
BASE = f"http://127.0.0.1:{PORT}"
SERVER_LOG = DB_DIR / f"ota_rl_server_{os.getpid()}.log"

#: 本脚本注入的配额（新建 N 条之后第 N+1 条应 429）
QUOTA = 3

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        _failures.append(name)
    return ok


def ota(mac: str) -> tuple[int, dict]:
    """按设备的方式打一次 OTA 端点（带 Device-Id 头，GET）。"""
    r = urllib.request.Request(BASE + "/api/voice/ota", method="GET")
    r.add_header("Device-Id", mac)
    r.add_header("Content-Type", "application/json")
    try:
        with _LOCAL_OPENER.open(r, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw}


def wait_ready(timeout: float = 120.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = urllib.request.Request(BASE + "/api/health", method="GET")
            with _LOCAL_OPENER.open(r, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def _log_tail(max_bytes: int = 8192) -> str:
    try:
        data = SERVER_LOG.read_bytes()
        return data[-max_bytes:].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return "<no log>"


def main() -> int:
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
    env["ELDER_OTA_NEW_CODE_PER_HOUR"] = str(QUOTA)
    # 登录限流/绑定限流与本脚本无关，但保持与 CI 一致的最小注入
    env.setdefault("ELDER_SEED_ADMIN_PASSWORD", "admin123")

    print(f"[boot] starting server on :{PORT} (quota={QUOTA}/h, db={DB_PATH.name})")
    guard_or_die(PORT, "smoke_ota_ratelimit.py")
    hold, hold_hint = guard_port(PORT)
    if hold is None:
        print("[boot] " + hold_hint.replace("\n", "\n[boot] "))
        return 1
    release_port(hold, PORT)

    log_fp = SERVER_LOG.open("w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT),
         "--log-level", "warning"],
        cwd=str(SERVER), env=env,
        stdout=log_fp, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace",
    )
    try:
        if not wait_ready():
            print("[boot] server did not become ready. log tail:")
            print(_log_tail())
            return 1
        if detect_addr_in_use(_log_tail()):
            print(f"[boot] ✗ 端口 {PORT} bind 失败，结果不可信，直接退出")
            return 1
        print("[boot] ready\n")

        # ---------------------------------------------- 1. 前 QUOTA 个 MAC 正常发码
        codes: dict[str, str] = {}
        for i in range(QUOTA):
            mac = f"aa:bb:cc:00:00:{i:02x}"
            code, body = ota(mac)
            got = ((body or {}).get("activation") or {}).get("code")
            check(f"新 MAC #{i + 1} 拿到激活码", code == 200 and bool(got),
                  f"http={code} code={got}")
            if got:
                codes[mac] = got

        # 前 QUOTA 个码必须互不相同（否则「配额」统计的其实是同一枚码）
        check("前 %d 枚码互不相同" % QUOTA,
              len(set(codes.values())) == len(codes), str(sorted(codes.values())))

        # ---------------------------------------------- 2. 第 QUOTA+1 个 MAC 应被拦
        overflow_mac = "aa:bb:cc:00:00:ff"
        code, body = ota(overflow_mac)
        detail = (body or {}).get("detail") or (body or {}).get("message") or ""
        check("超出配额的新 MAC 被 429", code == 429, f"http={code} detail={detail!r}")
        check("429 文案命中限速前缀", "设备注册过于频繁" in str(detail), str(detail))

        # ---------------------------------------------- 3. ★ 复用路径不得被误伤
        # 这是本脚本存在的理由：设备每 15s 轮询一次，配额耗尽后**必须**还能拿到
        # 自己那枚码，否则就是「设备突然不显示激活码」。
        first_mac = "aa:bb:cc:00:00:00"
        for n in range(3):
            code, body = ota(first_mac)
            got = ((body or {}).get("activation") or {}).get("code")
            check(f"配额耗尽后复用第 {n + 1} 次仍 200 且码不变",
                  code == 200 and got == codes.get(first_mac),
                  f"http={code} code={got} 期望={codes.get(first_mac)}")

        # ---------------------------------------------- 4. 无 MAC 仍是 400（未被限速吞掉）
        r = urllib.request.Request(BASE + "/api/voice/ota", method="GET")
        try:
            with _LOCAL_OPENER.open(r, timeout=20) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        check("缺 Device-Id 仍返回 400（不被限速掩盖）", code == 400, f"http={code}")

        print()
        if _failures:
            print(f"✗ {len(_failures)} 项失败：{_failures}")
            print("--- server log tail ---")
            print(_log_tail())
            return 1
        print("✓ 全部通过")
        return 0
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            proc.kill()
        log_fp.close()
        for p in (DB_PATH, SERVER_LOG):
            try:
                if p.exists():
                    unlink_retry(p)
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
