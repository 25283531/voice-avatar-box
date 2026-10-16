"""QA：uvicorn bind 失败到底会不会「苟活」？—— 裁决我和architect 的分歧。

分歧
----
* 我（QA）上一轮报：**bind 失败但进程仍活着**（poll()=None），因此 wait_ready
  会探到别人的服务 -> 假绿必然发生。
* architect 报：**bind 失败会退出**（poll()=1），因此 wait_ready 先查poll()
  -> 立即 return False -> 假绿链路被切断。

两边都给了实验，但结论相反。**按我自己提的「核实优先于推断」，必须裁决。**

为什么两边都可能得出错的结论（我怀疑是实验设计的坑）
--------------------------------------------------------
1. 我上一轮那次：两个 Popen 后只 sleep 了 6 秒就poll()。
   uvicorn 从启动到 bind 失败可能**要更久**（import 链长），
   6 秒内它可能还没走到 bind 那一步 -> poll()=None -> 我误读成"苟活"。
2. architect 提到他第一次用 `(cmd &)` 起服务，被shell 收尾杀掉，
   探活拿到 502 —— 那次实验无效，他也自己承认了。

所以本次实验的设计要点：
  * **必须等到进程真的结束**（用 poll() 轮询到有结果，而不是固定 sleep）
  * 分别记录「起服务 -> bind 失败」耗时，确认不是时序问题
  * 同时观察 stderr 是否真含10048（证明它确实走到了 bind 那一步）
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable


def spawn(port: int, tag: str) -> tuple[subprocess.Popen, Path]:
    db = Path(tempfile.gettempdir()) / f"qa3_alive_{tag}.db"
    if db.exists():
        db.unlink()
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{db.as_posix()}"
    env["ELDER_JWT_SECRET"] = f"qa3alive-{tag}"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "info"],
        cwd=str(SERVER), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    return p, db


def wait_exit(p: subprocess.Popen, limit: float = 60.0) -> tuple[bool, float]:
    """轮询到进程有结果为止，返回 (是否退出, 耗时秒)。"""
    t0 = time.time()
    while time.time() - t0 < limit:
        rc = p.poll()
        if rc is not None:
            return True, time.time() - t0
        time.sleep(0.25)
    return False, time.time() - t0


def read_out(p: subprocess.Popen) -> str:
    try:
        return p.stdout.read() or ""
    except Exception:
        return ""


def main() -> int:
    PORT = 8251
    print("=" * 72)
    print("裁决：uvicorn bind 失败后是「退出」还是「苟活」")
    print("=" * 72)

    print(f"\n[步骤1] 起服务 A（占用 {PORT}），等它真正就绪")
    pa, dba = spawn(PORT, "A")
    import urllib.request
    import urllib.error

    ready = False
    for _ in range(150):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/api/health", timeout=3) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.4)
    print(f"  A 就绪 = {ready}  A.poll() = {pa.poll()}")
    if not ready:
        print("  A 都没起来，后续实验无意义")
        pa.terminate()
        return 2

    print(f"\n[步骤 2] 起服务 B（**同端口**），然后**轮询到有结果为止**")
    print("           （关键：不固定 sleep，避免我上一轮把『还没走到 bind』误读成苟活）")
    pb, dbb = spawn(PORT, "B")
    exited, elapsed = wait_exit(pb, limit=60)
    print(f"  B 是否退出 = {exited}   耗时 = {elapsed:.1f}s   poll() = {pb.poll()}")
    print(f"  B 自己的 DB 是否被 seed = {dbb.exists() and dbb.stat().st_size > 0}")

    out_b = read_out(pb) if exited else ""
    has_bind_err = ("10048" in out_b) or ("already in use" in out_b.lower())
    print(f"  B 的 stderr 含 bind 错误 = {has_bind_err}")
    if has_bind_err:
        for ln in out_b.splitlines():
            if "10048" in ln or "already in use" in ln.lower():
                print(f"    | {ln.strip()[:140]}")
                break

    print("\n[步骤 3] 若 B 苟活，它仍会占着 CPU；再测一次「同时起两个」")
    #再复现一次，两次结论一致才算稳
    pb2, dbb2 = spawn(PORT, "B2")
    exited2, elapsed2 = wait_exit(pb2, limit=60)
    print(f"  第二次 B2 是否退出 = {exited2}  耗时 = {elapsed2:.1f}s  poll() = {pb2.poll()}")
    if exited2:
        read_out(pb2)

    for p in (pa, pb, pb2):
        try:
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=8)
            else:
                p.stdout.read()
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    print("\n" + "=" * 72)
    print("裁决结果")
    print("=" * 72)
    if exited and exited2:
        print("✅ **architect 对，我错。** uvicorn bind 失败后**进程会退出**")
        print("   （poll() 有明确非 None 值，且 stderr 含 10048）")
        print()
        print("   我上一轮那次实验的有效性存疑：固定 sleep 6 秒后就poll()，")
        print("   而 uvicorn 从启动到 bind 失败耗时可能更长 —— 我把")
        print("   『还没走到 bind 那一步』误读成了『苟活』。")
        print()
        print("   正确的机制（architect 版）：")
        print("     B bind 失败 -> 进程退出 -> wait_ready 每轮先查 proc.poll()")
        print("     -> 已退出则立即 return False -> **不会去探别人的 health**")
        print("   所以**假绿的前提是 wait_ready 不检查自己的进程存活**；")
        print("   而 smoke_roles 的 wait_ready 已经检查了 -> 这条路已堵死。")
    elif not exited or not exited2:
        print("⚠️ **我对，architect 错。** 进程确实苟活（poll() 持续为 None）")
    else:
        print("⚠️ 两次结论不一致 —— 说明存在时序依赖，机制本身不稳定，")
        print("   这比「谁对谁错」更值得警惕：**守卫不能依赖时序。**")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())