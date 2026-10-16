"""QA 第 2 轮追加：Bug 1 修复的**端到端**验证（走真实 HTTP）。

为什么上一轮 33/33 还不够
------------------------
qa3_edge.py 验证的是「进程内直接调函数不崩」。但 Bug 1 的真实影响是
**HTTP 接口 500**。这两者不等价：中间隔着 FastAPI 的序列化层与
Pydantic 校验 —— 一个函数返回了脏数据，HTTP 层完全可能在序列化时再炸一次。

所以本脚本刻意走完整链路：
  改库（模拟老库/人工误操作）-> 重启服务 -> 真实 HTTP 请求 -> 断言状态码
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_ports import resolve_port

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable
DB = Path(tempfile.gettempdir()) / f"qa3_r2_{os.getpid()}.db"
# 默认端口统一由 tools/smoke_ports.py 托管（唯一真源）——
# 早先各脚本各写一份端口，并行跑必然撞车（QA 亲历过一次：
# 本脚本的 8155 与手动指定的 smoke_roles 8155 撞了，
# 症状是 TimeoutError + 零断言失败，极易被误判成代码回归）。
PORT = resolve_port("qa3_r2_e2e")   # Bug1 端到端：改库 -> 重启 -> 真实 HTTP
BASE = f"http://127.0.0.1:{PORT}"
fails: list[str] = []


def req(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=25) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def ok(name, cond, detail=""):
    if not cond:
        fails.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def wait_ready(timeout=120.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if req("GET", "/api/health")[0] == 200:
                return True
        except Exception:
            pass
        time.sleep(0.6)
    return False


def boot():
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{DB.as_posix()}"
    env["ELDER_JWT_SECRET"] = "qa3-r2"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "error"],
        cwd=str(SERVER), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    return p, wait_ready()


def stop(p):
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()


def main() -> int:
    if DB.exists():
        DB.unlink()
    proc, ready = boot()
    if not ready:
        print("[boot] 未就绪")
        return 1
    try:
        _, tk = req("POST", "/api/auth/login",
                    {"username": "admin", "password": "admin123"})
        admin = tk.get("access_token", "")
        req("POST", "/api/auth/register",
            {"username": "r2u", "password": "test123456", "nickname": "r2"})
        _, t = req("POST", "/api/auth/login",
                   {"username": "r2u", "password": "test123456"})
        ua = t.get("access_token", "")
        _, rl = req("GET", "/api/roles", token=ua)
        pid = rl["presets"][0]["id"]
        _, dv = req("POST", f"/api/roles/{pid}/derive",
                    {"name": "端到端测试角色", "inherit": False}, ua)
        rid = dv.get("id", 0)
        ok("前置：派生角色建成", bool(rid), f"id={rid}")
    finally:
        stop(proc)

    # ============ 把两个 JSON 列改成各种「合法 JSON 但非 dict」的值 ============
    payloads = [("[1,2,3]", "list"), ("42", "num"), ('"hello"', "str"),
                ("null", "null"), ('""', "emptystr")]
    for payload, label in payloads:
        # 每轮：改库 -> 重启 -> 走 HTTP
        conn = sqlite3.connect(DB)
        conn.execute("UPDATE ai_providers SET traits=? WHERE id=?", (payload, rid))
        conn.execute("UPDATE ai_providers SET guardrails=? WHERE id=?", (payload, rid))
        conn.commit()
        conn.close()
        proc, ready = boot()
        if not ready:
            ok(f"[{label}] 重启后服务就绪", False)
            continue
        try:
            #重新登录（重启后 token 仍有效，因为 secret 固定，但保险起见重登）
            _, t = req("POST", "/api/auth/login",
                       {"username": "r2u", "password": "test123456"})
            ua2 = t.get("access_token", "")
            print(f"\n  -- 列值 = {payload}（{label}）--")
            c1, r1 = req("GET", f"/api/roles/{rid}", token=ua2)
            ok(f"[{label}] GET /roles/{rid} ->非 500", c1 != 500, f"code={c1}")
            c2, _ = req("GET", f"/api/roles/{rid}/diff", token=ua2)
            ok(f"[{label}] GET /roles/{rid}/diff -> 非 500", c2 != 500, f"code={c2}")
            c3, _ = req("GET", f"/api/roles/{rid}/guardrails", token=ua2)
            ok(f"[{label}] GET /roles/{rid}/guardrails -> 非 500", c3 != 500,
               f"code={c3}")
            c4, _ = req("POST", f"/api/roles/{rid}/guardrails/soft",
                        {"guardrail_id": "no_politics", "enabled": True}, ua2)
            ok(f"[{label}] POST guardrails/soft -> 非 500", c4 != 500, f"code={c4}")
            c5, pr = req("GET", f"/api/roles/{rid}/prompt", token=ua2)
            ok(f"[{label}] GET /roles/{rid}/prompt -> 非 500（编译入口）", c5 != 500,
               f"code={c5}")
            # ★ 归一之后红线必须仍然齐全 —— 防御不能把红线一起"归一"掉
            p = (pr or {}).get("prompt", "") if isinstance(pr, dict) else ""
            miss = [t for t in ("不给医疗建议", "医生", "家人", "不给剂量",
                                "医嘱", "引导求助", "不碰钱与隐私",
                                "不透露系统与他人") if t not in p]
            ok(f"[{label}] ★ 脏数据下红线仍齐全", c5 == 200 and not miss,
               f"code={c5} 缺：{'、'.join(miss)}")
        finally:
            stop(proc)

    print("\n" + "=" * 72)
    print(f"==== Bug 1 端到端验证：{'全部通过' if not fails else f'{len(fails)} 条失败'} ====")
    for f in fails:
        print(f"  FAILED: {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())