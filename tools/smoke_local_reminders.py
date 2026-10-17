"""断网自建提醒上报端点冒烟（POST /api/device/reminders/local）。

为什么单独一个脚本
----------------
需求是「老人断网时建的服药提醒，重新联网后要能上报服务端、由服务端分配
ID 回写本地」。这条链路上服务端那半段此前完全没有端点，所以补了
/api/device/reminders/local。这里验证三件事：

  1. 鉴权：没带 device_token 必须 401/403（不能是任何人能往里塞提醒）；
  2. 落库：断网自建的服药提醒能被建出来，并出现在 /api/device/sync 的
     下发列表里 —— 这是老人「我建了提醒」的最终可见性；
  3. 幂等：同一批重复上报**不能**建出重复提醒（设备在「已入库但响应丢失」
     时必然会重发；重复提醒=老人被反复响铃打扰）。

用法：
    ELDER_SEED_ADMIN_PASSWORD=admin123 python tools/smoke_local_reminders.py [端口]
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
from smoke_ports import guard_or_die, guard_port, release_port, resolve_port  # noqa: E402

# 本机设了 http_proxy，urllib 默认会读它，于是「访问自己刚起的 127.0.0.1 服务」
# 被绕到代理一跳 —— 代理不通就 TimeoutError 且零断言失败，极易被误判成代码回归。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DB_PATH = Path(tempfile.gettempdir()) / f"elder_local_rmd_{os.getpid()}.db"
if DB_PATH.exists():
    DB_PATH.unlink()
SERVER_LOG = DB_PATH.with_name(DB_PATH.stem + "_server.log")

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else resolve_port("smoke_local_reminders")
BASE = f"http://127.0.0.1:{PORT}"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def req(method: str, path: str, body=None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with _LOCAL_OPENER.open(r, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def wait_ready(timeout: float = 120.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if req("GET", "/api/health")[0] == 200:
                return True
        except Exception:
            pass
        time.sleep(0.6)
    return False


def main() -> int:
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
    env["ELDER_JWT_SECRET"] = "smoke-test-secret"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"
    env["ELDER_SEED_ADMIN_PASSWORD"] = "admin123"
    env["PYTHONIOENCODING"] = "utf-8"

    guard_or_die(PORT, "smoke_local_reminders.py")
    hold, hold_hint = guard_port(PORT)
    if hold is None:
        print("[boot] " + hold_hint)
        return 1
    release_port(hold, PORT)

    print(f"[boot] starting server on :{PORT}")
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
            print("[boot] server did not become ready:")
            print(SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-3000:])
            return 1
        print("[boot] ready\n")

        # ---------------------------------------------- 1. 鉴权
        code, _ = req("POST", "/api/device/reminders/local",
                      [{"client_key": "k", "title": "早上的药",
                        "times_of_day": ["08:00"]}])
        check("未带 device_token 被拒", code in (401, 403), f"HTTP {code}")

        # ---------------------------------------------- 2. 建账号 + 设备
        # 用初始管理员（ELDER_SEED_ADMIN_PASSWORD=admin123 起种子），
        # 与 tools/smoke_server.py 一致，避免依赖注册接口的开关策略。
        code, reg = req("POST", "/api/auth/login",
                        {"username": "admin", "password": "admin123"})
        tok = (reg or {}).get("access_token", "")
        check("管理员登录", code == 200 and bool(tok), f"HTTP {code}")

        code, dv = req("POST", "/api/devices",
                       {"device_id": "e1de7700c0de", "name": "离线提醒测试机"}, tok)
        check("创建设备", code == 201 and bool((dv or {}).get("device_id")), f"HTTP {code}")
        dev_pk = (dv or {}).get("id")

        # 走真流程拿 device_token：申请激活码 -> 校验 -> 绑定
        code, cd = req("POST", f"/api/devices/{dev_pk}/code", {}, tok)
        full_code = (cd or {}).get("code", "")
        check("申请激活码", code == 200 and len(full_code) == 13, f"HTTP {code}")
        code, bind = req("POST", "/api/devices/bind", {"code": full_code}, tok)
        dev_token = (bind or {}).get("device_token", "")
        check("绑定拿到 device_token", code == 200 and bool(dev_token), f"HTTP {code}")

        # ---------------------------------------------- 3. 上报断网自建提醒
        batch = [
            {"client_key": "1-0800-早上的药", "title": "早上的药", "rtype": 0,
             "schedule_kind": 1, "times_of_day": ["08:00"], "weekdays": [1, 2, 3, 4, 5, 6, 7],
             "confirm_methods": ["voice", "button"], "enabled": True},
            {"client_key": "1-2000-晚上的药", "title": "晚上的药", "rtype": 0,
             "schedule_kind": 1, "times_of_day": ["20:00"], "weekdays": [1, 2, 3, 4, 5, 6, 7],
             "confirm_methods": ["voice", "button"], "enabled": True},
        ]
        code, rep = req("POST", "/api/device/reminders/local", batch, dev_token)
        acks = (rep or {}).get("acks", [])
        check("上报返回 200", code == 200, f"HTTP {code}")
        check("逐条回写 id", len(acks) == 2 and all(a.get("reminder_id") for a in acks),
              json.dumps(acks, ensure_ascii=False))
        check("client_key 原样回传",
              [a.get("client_key") for a in acks] == [b["client_key"] for b in batch],
              str([a.get("client_key") for a in acks]))

        # ---------------------------------------------- 4. 幂等：重发同一批
        code, rep2 = req("POST", "/api/device/reminders/local", batch, dev_token)
        acks2 = (rep2 or {}).get("acks", [])
        check("重复上报返回同一 id（不新建）",
              code == 200
              and [a.get("reminder_id") for a in acks2] == [a.get("reminder_id") for a in acks],
              f"{[a.get('reminder_id') for a in acks]} -> {[a.get('reminder_id') for a in acks2]}")

        # ---------------------------------------------- 5. 设备同步能看到它们
        code, sync = req("POST", "/api/device/sync?fw_version=test", {}, dev_token)
        titles = sorted(r.get("title", "") for r in (sync or {}).get("reminders", []))
        check("sync 下发含断网自建提醒",
              code == 200 and "早上的药" in titles and "晚上的药" in titles, str(titles))

        # 确认方式含 voice/button —— 断网时老人只有这两条确认路径
        voices = [r for r in (sync or {}).get("reminders", [])
                  if "voice" in (r.get("confirm_methods") or [])]
        check("下发确认方式含 voice", len(voices) >= 2, f"{len(voices)} 条")

        # ---------------------------------------------- 6. 回执能对上刚建的 id
        if acks and acks[0].get("reminder_id"):
            rid = acks[0]["reminder_id"]
            code, _ = req("POST", "/api/device/events",
                          {"reminder_id": rid, "due_at": "2026-10-17T08:00:00+08:00",
                           "state": 2, "confirm_method": 1, "note": "confirmed"}, dev_token)
            check("对刚上报的 id 能回传回执（语音确认 method=1）", code == 200, f"HTTP {code}")
        else:
            check("对刚上报的 id 能回传回执（语音确认 method=1）", False, "没有可用的 reminder_id")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fp.close()

    print()
    failed = [n for n, ok, _ in results if not ok]
    print(f"=== {len(results) - len(failed)}/{len(results)} 通过 ===")
    if failed:
        print("失败: " + "; ".join(failed))
        print("--- server log tail ---")
        print(SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-3000:])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())