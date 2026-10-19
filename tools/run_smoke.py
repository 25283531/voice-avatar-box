#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_smoke.py —— 跑一个冒烟脚本，把完整输出落到 logs/<name>.log。

为什么需要它（2026-10-19 实际踩到的）
------------------------------------
server-ci 红灯时，GitHub 网页上的日志正文是**异步加载**的：web 页面拿到的是
空壳，API 要 token，日志下载端点未认证返回 Not Found。
⇒ 三条路都断时，唯一能拿到的只有 job summary 里那一句
「后端冒烟测试 / Process completed with exit code 1」，
**完全不知道是哪个脚本、哪条断言红的**。

本地却复现不出来（干净 clone 也绿）时，就只能猜——这是纯浪费。

所以把输出**先落盘**：红灯时随 artifact 上传、绿灯时忽略，
排查时先下载 artifact 看结论行，而不是在网页里翻十几步的折叠面板。

三个必须做对的地方
------------------
1. **退出码必须透传**：本脚本失败时不能让 job 绿。
   忘了 `sys.exit(rc)` 就等于把整个后端 CI 变成永远绿灯。
2. **流式落盘**：子进程输出直接重定向到文件，不能等它跑完再写——
   脚本若卡死/被 kill，落盘里至少还有崩溃前的部分。
   （早先用 `capture_output=True` 会丢掉「卡在哪一步」这个最关键的信息。）
3. **stdout 与 stderr 同时落盘**：Python 的 traceback 走 stderr，
   只收 stdout 会把真正的报错丢掉。

用法：
    python tools/run_smoke.py logs/smoke_roles.log python tools/smoke_roles.py 8101
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        print("用法: python tools/run_smoke.py <日志路径> <命令> [参数...]",
              file=sys.stderr)
        return 2

    log_path = Path(sys.argv[1])
    if not log_path.is_absolute():
        log_path = ROOT / log_path
    cmd = sys.argv[2:]

    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[run_smoke] {' '.join(cmd)}")
    print(f"[run_smoke] 日志 -> {log_path}")
    # 失败诊断步骤按 logs/*.log 摘结论行，所以这里先写头部占位，
    # 让「脚本没跑起来」与「脚本跑了但没输出」在 artifact 里可区分。
    log_path.write_text(
        f"# command: {' '.join(cmd)}\n"
        f"# cwd: {ROOT}\n"
        f"# python: {sys.version.split()[0]}\n\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    # PYTHONUNBUFFERED：否则子进程的 print 被缓冲，
    # 崩了会丢掉缓冲区里尚未 flush 的输出——而那往往正是崩溃前的最后线索。
    env["PYTHONUNBUFFERED"] = "1"

    with log_path.open("a", encoding="utf-8", errors="replace") as fp:
        rc = subprocess.call(cmd, cwd=str(ROOT), stdout=fp, stderr=subprocess.STDOUT,
                             env=env)

    # 退出码一定透传，否则这个包装器就是把 CI 变绿的bug。
    print(f"[run_smoke] rc={rc}  日志已落盘: {log_path}")
    return rc


if __name__ == "__main__":
    sys.exit(main())