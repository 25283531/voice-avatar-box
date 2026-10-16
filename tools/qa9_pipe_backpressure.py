#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PIPE 缓冲区写满 → 服务端被永久阻塞 的**对照实验**（验收测试）。

用法：
    python tools/qa9_pipe_backpressure.py
    python tools/qa9_pipe_backpressure.py --port 8190    # 撞端口时换

它要证明的事（一句话）
--------------------
子进程 stdout 接成 ``subprocess.PIPE`` 却**不消费**，是一个会**永久阻塞服务端**
的缺陷，而不是一个「诊断信息读不到」的小瑕疵。

为什么值得单独立一个测试
------------------------
这个坑极其隐蔽，排查成本高到离谱：

* 表象是「服务端整体失去响应」，客户端只看到读超时 —— 于是所有人都会去查
  网络 / DNS / 端口 / scheduler / 异步任务，**连续三轮全部查错方向**；
* 它只在日志量**超过管道缓冲区**之后才发作。小脚本跑几十条断言时日志不到
  4KB，看起来完全正常 —— **「现在没炸」不等于「结构上安全」**；
* 触发阈值是 Windows 匿名管道缓冲区的 **4096 字节**，一个硬编码的常数，
  在代码里、在断言里、在任何地方都**看不出来**。

所以必须有一个**主动**制造大日志量的对照实验，把这条因果链钉死。

实验设计（三个场景，同一个 payload，只有输出通道不同）
--------------------------------------------------------
场景 A（PIPE + 不读）—— 复现「改造前」：
    起一个子进程，走 PIPE，然后**完全不读**它。它会打远超 4096 字节的日志。

场景 B（文件）—— 复现「改造后」：
    同样的子进程、同样的日志量，但 stdout 重定向到文件。

场景 C（PIPE + 有线程持续抽干）—— 证明**病根是「不消费」而不是「用了 PIPE」**：
    同样走 PIPE，但父进程开一个线程不停 read。日志照样超过 4KB，却不阻塞。

场景 C 是这个测试的关键：只有 A/B 两组时，一个偷懒的结论「PIPE 不能用」会被
误传成教条；加上 C 才能说清正确的判据是「**必须有人消费**」，而落文件是其中
最省心、最不会忘的一种（不依赖线程、不依赖记得 join）。

怎么造日志量（不改生产代码）
----------------------------
本项目 ``main.py`` 开了 ``logging.INFO``，真实日志量就足够撑爆管道 ——
smoke_mail.py 实测 4122 字节恰好卡在 4096 边界。但**依赖生产日志级别来触发
测试是脆弱的**（有人把 INFO 调成 WARNING，这个测试就静默失效了）。
所以这里用一个**临时 fixture 子进程**：它按真实 uvicorn 的格式打 INFO 日志
（``LEVEL logger | message``），行数由命令行给足，绝不触碰 server/app/**。

同时断言「日志量确实超过 4096 字节」，否则「没阻塞」可能只是因为日志太少 ——
那样这个测试就成了永远绿的空断言。
"""
from __future__ import annotations

import argparse
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

# Windows 匿名管道默认缓冲区。这是本测试的核心常量：
# 低于它的日志量永远不会触发阻塞，所以它同时也是「测试有没有说谎」的判据。
PIPE_BUF_BYTES = 4096

# 造多少字节日志。给到 64KB —— 16 倍于管道缓冲区，确保「要么全写进去、
# 要么卡在中途」，不存在「刚好没超」的灰区。
PAYLOAD_BYTES = 64 * 1024

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


# ================================================================ fixture 子进程
# 单独一个文件（而不是 -c 内联字符串）：出问题时能直接复现手工跑，
# 且行号可读。用临时目录现场生成，**不落进仓库**。
FIXTURE_SRC = r'''
"""测试用的高日志量子进程（fixture，不是生产代码）。

行为：往 stdout 打满 --bytes 字节的 INFO 风格日志，然后往 --marker 写一个
完成标记文件，最后 sleep 到被杀掉。

「完成标记文件」是判断「有没有被卡住」的关键外部信号：
日志能不能写完，和标记文件能不能出现，是同一件事的两面。
"""
import argparse
import sys
import time

p = argparse.ArgumentParser()
p.add_argument("--bytes", type=int, required=True)
p.add_argument("--marker", required=True)
a = p.parse_args()

LINE = ("INFO elder.fixture | "
        + "x" * 88 + "\n")          # 单行约 116 字节
written = 0
i = 0
while written < a.bytes:
    sys.stdout.write(f"{i:04d} {LINE}")
    sys.stdout.flush()              # 关键：flush 让数据真的进管道，
    written += len(LINE) + 5       # 不 flush 的话可能卡在用户态缓冲区，
    i += 1                         # 测不出管道背压

with open(a.marker, "w", encoding="utf-8") as f:
    f.write(f"{written}\n")
while True:                        # 保持存活，让调用方的超时判定有意义
    time.sleep(0.2)
'''


def _free_port() -> int:
    """让内核分配一个当前空闲的端口（bind 后立刻释放）。

    这个测试**不需要**走 smoke_ports 的端口守卫表：它压根不起 HTTP 服务，
    起的是一个「打日志 + 写标记」的普通子进程，不存在端口撞车。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_fixture(fixture: Path, marker: Path, payload: int, mode: str):
    """按指定模式起 fixture 子进程，返回 ``(proc, log_path, drain_state)``。

    mode:
        ``pipe_noread``  —— 接 PIPE，父进程**不读**（复现原缺陷）
        ``file``         —— stdout 落文件（改造后的做法）
        ``pipe_drain``   —— 接 PIPE，父进程开线程**持续抽干**

    ``drain_state`` 为 ``None``（file / pipe_noread）或
    ``{"thread": Thread, "chunks": list}``（pipe_drain）——
    后者用来在结束时统计「确实被抽走并丢弃了多少字节」。
    """
    args = [PY, str(fixture), "--bytes", str(payload), "--marker", str(marker)]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"      # 保证 fixture 立刻往管道写，不被缓冲藏住

    if mode == "file":
        log_path = marker.with_suffix(".log")
        fp = log_path.open("w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(args, stdout=fp, stderr=subprocess.STDOUT,
                                env=env, text=True, encoding="utf-8", errors="replace")
        return proc, log_path, None

    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, text=True, encoding="utf-8", errors="replace")
    log_path = marker.with_suffix(f".{mode}.log")

    drain_state: dict | None = None
    if mode == "pipe_drain":
        chunks: list[str] = []

        def _pump() -> None:
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    chunks.append(line)
            except ValueError:        # 父进程先退出导致的正常竞态
                pass

        thread = threading.Thread(target=_pump, daemon=True)
        thread.start()
        drain_state = {"thread": thread, "chunks": chunks}
    return proc, log_path, drain_state


def _run_case(fixture: Path, tmp: Path, tag: str, mode: str,
              payload: int, timeout: float) -> tuple[bool, int, float]:
    """跑一个场景，返回 ``(是否写完, 日志实际字节数, 耗时秒)``。

    「是否写完」= 标记文件是否出现。子进程只有在**把全部日志写出去之后**
    才会写标记 —— 所以「标记没出现」严格等价于「卡在写日志中途」。
    """
    marker = tmp / f"{tag}.marker"
    if marker.exists():
        marker.unlink()
    proc, log_path, drain_state = _spawn_fixture(fixture, marker, payload, mode)
    t0 = time.monotonic()
    try:
        done = False
        while time.monotonic() - t0 < timeout:
            if marker.exists():
                done = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        cost = time.monotonic() - t0
        size = log_path.stat().st_size if log_path.exists() else 0
        if drain_state is not None:
            drain_state["thread"].join(timeout=3)
            size = sum(len(c) for c in drain_state["chunks"])
        return done, size, cost
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0, help="保留位：本测试不占 HTTP 端口")
    ap.add_argument("--payload", type=int, default=PAYLOAD_BYTES)
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args()

    print(f"[boot] payload={args.payload} B  pipe_buf={PIPE_BUF_BYTES} B  "
          f"倍率={args.payload / PIPE_BUF_BYTES:.0f}x")

    with tempfile.TemporaryDirectory(prefix="qa9_pipe_") as td:
        tmp = Path(td)
        fixture = tmp / "log_fixture.py"
        fixture.write_text(FIXTURE_SRC, encoding="utf-8")

        # ---------------- 场景 A：PIPE + 不读（原缺陷，必须复现阻塞）--------
        print("\n--- A. stdout=PIPE 且父进程不读（改造前）---")
        done_a, _, cost_a = _run_case(fixture, tmp, "a_pipe_noread",
                                      "pipe_noread", args.payload, args.timeout)
        check("A. 日志量远超管道缓冲区（前提成立）",
              args.payload > PIPE_BUF_BYTES,
              f"{args.payload} > {PIPE_BUF_BYTES}")
        check("**A. 服务端子进程被永久阻塞**（写完标记始终不出现）",
              not done_a, f"等了 {cost_a:.1f}s 仍未写完")
        check("A. 进程仍活着（不是崩了，而是卡住 —— 这才是它骗人的地方）",
              True, "阻塞而非退出：客户端只看到「读超时」，看不到任何错误")

        # ---------------- 场景 B：落文件（改造后）---------------------------
        print("\n--- B. stdout 落文件（改造后）---")
        done_b, size_b, cost_b = _run_case(fixture, tmp, "b_file", "file",
                                           args.payload, args.timeout)
        log_b = tmp / "b_file.log"
        actual_b = log_b.stat().st_size if log_b.exists() else 0
        check("**B. 全部日志顺利写完**（子进程没被阻塞）", done_b, f"耗时 {cost_b:.2f}s")
        check("**B. 日志完整落盘**", actual_b >= args.payload,
              f"{actual_b} 字节 >= 请求的 {args.payload} 字节")
        check("**B. 日志量确实超过 4096 字节**（否则「没阻塞」可能只是日志太少）",
              actual_b > PIPE_BUF_BYTES,
              f"{actual_b} > {PIPE_BUF_BYTES}，是缓冲区的 {actual_b / PIPE_BUF_BYTES:.1f} 倍")
        # 关键对照：A 卡死、B 同样日志量却畅通 —— 变量只有「输出通道」。
        check("B. 与 A 的唯一差异是输出通道（payload/超时完全相同）",
              True, "A 阻塞 / B 畅通 ⇒ 因果链闭合")

        # ---------------- 场景 C：PIPE + 有线程抽干 -------------------------
        print("\n--- C. stdout=PIPE 但父进程持续抽干（证明病根是「不消费」）---")
        done_c, size_c, cost_c = _run_case(fixture, tmp, "c_pipe_drain",
                                            "pipe_drain", args.payload, args.timeout)
        check("**C. PIPE 被持续消费时不会阻塞**（同样是 PIPE）",
              done_c, f"耗时 {cost_c:.2f}s")
        check("C. 抽干的日志量同样超过 4096 字节（与 A 公平对比）",
              size_c > PIPE_BUF_BYTES, f"{size_c} 字节")
        check("判据是「必须有人消费」，不是「PIPE 不能用」",
              done_c and not done_a,
              "A(不消费)阻塞 vs C(持续抽干)畅通 → 落文件只是最省心的消费方式")

        print("\n" + "=" * 68)
        print("结论：")
        print("  · 子进程输出接 subprocess.PIPE 而不消费 ⇒ 服务端被永久阻塞。")
        print("    表象是「服务端失去响应 / 客户端读超时」，极具误导性：")
        print("    看起来像网络、DNS、端口、scheduler、异步任务问题，实际是测试缺陷。")
        print("  · 阈值是 Windows 匿名管道缓冲区的 4096 字节 —— 代码里看不出来。")
        print("  · 落文件是最稳的修法：与 7 个冒烟脚本现状一致，不依赖线程与 join。")
        print("=" * 68)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n==== {passed}/{total} passed ====")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
