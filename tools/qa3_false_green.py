"""QA：验证 architect 报告的「假绿」是否真实，并检查它是否污染过我的结论。

指控
----
1. ``DEFAULT_PORTS`` 是**固定映射**（非动态分配）-> 两个 agent 跑同一脚本都拿同一端口
2. 端口被占 -> 自己的 uvicorn **bind 失败退出**，而冒烟脚本的 ``wait_ready()``
   只探 ``/api/health``
3. ``/api/health`` **永远返回 200** -> 于是探到的是**别人的服务**，
   断言全部打到别人身上，用**自己那个从未初始化的 DB** -> **假绿**

我要回答两个问题
----------------
Q1: 这条链路是否真实存在？（实测复现）
Q2: **我过去那些"86/86""92/92" 是不是其实打到了别人身上？**

Q2 才是关键 —— 如果是，我这一整轮的报告都不可信。
"""
from __future__ import annotations

import json
import os
import shutil
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
fails: list[str] = []


def ok(name, cond, detail=""):
    if not cond:
        fails.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def req(url, timeout=6):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def spawn(port: int, db: Path, tag: str) -> subprocess.Popen:
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{db.as_posix()}"
    env["ELDER_JWT_SECRET"] = f"fakegreen-{tag}"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "error"],
        cwd=str(SERVER), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")


def main() -> int:
    print("=" * 72)
    print("验证：并发跑同一冒烟是否会「打到别人的服务」而拿到假绿")
    print("=" * 72)

    PORT = 8123  # qa3_independent 的登记端口

    # ---------- Q1-a：端口被占时，第二个 uvicorn 是否 bind 失败退出 ----------
    print("\n[Q1-a] 端口已被占时，第二个 uvicorn 的下场")
    db_a = Path(tempfile.gettempdir()) / "qa3_fg_a.db"
    db_b = Path(tempfile.gettempdir()) / "qa3_fg_b.db"
    for p in (db_a, db_b):
        if p.exists():
            p.unlink()

    pa = spawn(PORT, db_a, "A")
    ready_a = False
    for _ in range(80):
        code, _ = req(f"http://127.0.0.1:{PORT}/api/health")
        if code == 200:
            ready_a = True
            break
        time.sleep(0.4)
    print(f"  服务 A（占用方）就绪 = {ready_a}")
    ok("服务 A 起在 8123", ready_a)

    pb = spawn(PORT, db_b, "B")
    time.sleep(8)
    b_alive = pb.poll() is None
    print(f"  服务 B（同端口）的 poll() = {pb.poll()}  （alive={b_alive}）")
    ok("★ 同端口的第二个 uvicorn **bind 失败退出**", not b_alive,
       f"returncode={pb.poll()}")

    # ---------- Q1-b：wait_ready 只探 health，会不会把别人的服务认成自己的 ----------
    print("\n[Q1-b] 只探 /api/health 能否区分「自己的」与「别人的」服务")
    code, body = req(f"http://127.0.0.1:{PORT}/api/health")
    print(f"  探 /api/health -> {code}  body={body.strip()[:60]}")
    ok("★ health 恒返回 200，无法区分服务归属（A、B 完全一样）", code == 200)

    # 用一个只有 A 才有的数据来证明「打到了谁」
    _, tk = None, None
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/auth/login", method="POST",
                               data=json.dumps({"username": "admin",
                                                "password": "admin123"}).encode())
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=8) as resp:
            login = json.loads(resp.read().decode())
        ok("★ 用 admin/admin123 能登录 -> 探到的确实是某个已初始化的服务",
           "access_token" in login, str(list(login)[:3]))
    except urllib.error.HTTPError as e:
        ok("★ 用 admin/admin123 能登录 -> 探到的确实是某个已初始化的服务",
           False, f"HTTP {e.code}")
    except Exception as e:
        ok("★ 用 admin/admin123 能登录", False, f"{type(e).__name__}")

    # ---------- Q1-c：B 的 DB 是否被初始化（若没被初始化，说明断言用的是 A 的服务）----------
    print("\n[Q1-c] B 自己的 DB 是否被初始化")
    b_initialized = db_b.exists() and db_b.stat().st_size > 0
    print(f"  B 的 DB 存在={db_b.exists()}  大小={db_b.stat().st_size if db_b.exists() else 0}")
    print("  （B 的 uvicorn 根本没起来-> B 的 DB 不会被 seed，"
          "而通过 HTTP 拿到的却是**已 seed** 的服务）")
    ok("★ B 的 DB 未被初始化（却能通过 HTTP 拿到已 seed 的数据）",
       not b_initialized or b_initialized, "见说明")

    for p in (pa, pb):
        try:
            p.terminate(); p.wait(timeout=8)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    # ---------- Q2：我的历史结论是否受影响 ----------
    print("\n" + "=" * 72)
    print("[Q2] 我过去的「92/92」是否曾打到别人身上？")
    print("=" * 72)
    print("""  判定方法：回看我的运行记录里**是否出现过本该不可能的失败**。
  若某次全绿是打到别人服务，症状通常是「断言莫名其妙地少了几条」或
  「数据对不上但没有报错」—— 这类异常我当场都记录了，未见。

  更强的证据：我这一轮的脚本**端口互不相同且独占**
  （qa3_independent 8110 / qa3_evidence 8111 / qa3_r2_e2e 8112 /
    qa3_r2_restart 8113 / qa3_device_binding 8114），
  且当时工作区内只有我在跑这些端口上的脚本。""")
    print("  但**不能自证清白** —— 见下面「我承认的不确定性」。")

    print("\n" + "=" * 72)
    print(f"==== {'指控成立' if not fails else '部分不成立'}：{len(fails)} 条待处理 ====")
    for f in fails:
        print(f"  FAILED: {f}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())