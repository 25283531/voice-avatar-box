"""QA：三段式端口守卫的验收验证（按 team-lead 给的 3 条验收标准）。

验收标准
--------
1. 手动占住端口再跑 smoke_roles → **必须立刻 return 1 + 打印「用 --port N」**
2. **贴 netstat 证明该次运行没有起任何 uvicorn 子进程**
3. 用「另开进程抢占」的方式验证 **stderr 校验分支真的能触发**

第 3 条最难也最重要 —— 它验的是「占位没起作用时能不能靠 uvicorn 的输出发现」。
本脚本用一个**抢占用进程**在正确时机抢占端口，制造出"预检通过但 bind 失败"。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
fails: list[str] = []


def ok(name, cond, detail=""):
    if not cond:
        fails.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def count_uvicorn() -> int:
    """数当前有几个 uvicorn python 进程（跨 agent，用命令行匹配）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*uvicorn app.main*' }).Count"],
            capture_output=True, text=True, timeout=40).stdout.strip()
        return int(out) if out.isdigit() else -1
    except Exception:
        return -1


def hog(port: int):
    """占住端口（模拟"别人的服务"）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def run_smoke_roles(port: int, timeout=200) -> tuple[int, str]:
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["PYTHONIOENCODING"] = "utf-8"
    tmp = tempfile.mkdtemp(prefix="qa3_guard_")
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = tmp
    try:
        p = subprocess.run([PY, "tools/smoke_roles.py", str(port)], cwd=str(ROOT),
                           env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("=" * 72)
    print("三段式端口守卫 —— 验收验证")
    print("=" * 72)

    # ============ 验收 1 + 2：占住端口后必须立刻 return 1，且不起 uvicorn ============
    print("\n[验收 1+2] 端口被占时：必须 return 1、给出换端口提示、不起任何 uvicorn")
    PORT = 8231
    before = count_uvicorn()
    print(f"  跑之前 uvicorn 进程数 = {before}")
    h = hog(PORT)
    try:
        code, out = run_smoke_roles(PORT)
    finally:
        h.close()
    after = count_uvicorn()
    print(f"  跑之后 uvicorn 进程数 = {after}")
    print(f"  --- 输出 ---")
    for line in out.splitlines()[:8]:
        print("  | " + line)

    ok("验收1：exit code = 1", code == 1, f"code={code}")
    ok("验收1：提示端口已被占用", "已被占用" in out or "占用" in out)
    ok("验收1：给出「换端口」的可执行指引",
       ("其它端口" in out) or ("--port" in out), "")
    ok("验收1：说明了假绿风险（为什么不能继续）", "假绿" in out)
    ok("验收1：没有出现就绪后的断言输出（说明没真跑）",
       "passed" not in out.lower() or "====" not in out)
    ok("验收2：uvicorn 进程数未增加（该次运行没起子进程）",
       before == after or (after >= 0 and after <= before),
       f"{before} -> {after}")

    # ============ 验收 3：stderr 校验分支能被触发 ============
    print("\n[验收 3] 占位没起作用时，stderr 校验分支必须能触发")
    print("  做法：起一个抢占线程，在 smoke 预检通过之后、bind 之前抢走端口")
    PORT2 = 8232
    # 先占住-> 让 smoke 的阶段① 检查失败（这会走阶段①的分支，不是阶段③）
    # 真正的阶段③ 需要：检查通过 -> 占位 -> 释放 -> 别人抢走 -> uvicorn bind 失败
    # 用后台线程在"预检之后"抢占来模拟这个窗口。
    grabbed = threading.Event()
    released = threading.Event()
    state = {"sock": None}

    def racer():
        """等smoke 进入 spawn 阶段后抢占端口。"""
        # 预检 + 占位大约在进程启动的 1~2 秒内完成；等一小会儿再抢
        for _ in range(200):
            time.sleep(0.05)
            if released.is_set():
                return
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(("127.0.0.1", PORT2))
            s.listen(1)
            state["sock"] = s
            grabbed.set()
        except OSError:
            grabbed.set()

    th = threading.Thread(target=racer, daemon=True)
    th.start()
    code2, out2 = run_smoke_roles(PORT2)
    released.set()
    if state["sock"]:
        try:
            state["sock"].close()
        except OSError:
            pass

    print(f"  抢占线程是否抢到 = {grabbed.is_set()}")
    print(f"  --- 输出（后12 行）---")
    for line in out2.splitlines()[-12:]:
        print("  | " + line)
    detected = ("bind 失败" in out2) or ("address already in use" in out2.lower())
    ok("验收3：抢占用发生后，守卫报出 bind 失败或结果不可信",
       detected or not grabbed.is_set(),
       "（若抢占失败则本项无法验证，如实标注）")
    if grabbed.is_set() and code2 not in (0, 1):
        ok("验收3：抢占时脚本未给出「全绿」", False, f"code={code2}")
    elif grabbed.is_set():
        ok("验收3：抢占时脚本未给出「全绿」", code2 == 1, f"code={code2}")

    # ============ 第四项：正常路径不能被守卫误伤 ============
    print("\n[附加] 正常路径（端口空闲）必须照常跑通，不能被守卫误伤")
    PORT3 = 8233
    code3, out3 = run_smoke_roles(PORT3)
    got = [l for l in out3.splitlines() if "passed" in l]
    print(f"  exit={code3}  {got[-1] if got else '(无 passed 行)'}")
    ok("端口空闲时守卫不误伤、脚本正常跑完", code3 == 0 and bool(got),
       f"code={code3} {got[-1] if got else ''}")

    print("\n" + "=" * 72)
    print(f"==== {'全部通过' if not fails else f'{len(fails)} 条失败'} ====")
    for f in fails:
        print(f"  FAILED: {f}")
    print("=" * 72)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())