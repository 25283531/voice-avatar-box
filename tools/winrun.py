#!/usr/bin/env python3
"""
winrun.py —— 在 Windows 上以「原生」方式运行外部程序。

为什么需要它
------------
本项目的 Bash 工具运行在 Git Bash / MSYS2 下（MSYSTEM=MINGW64）。
MSYS 的 POSIX→Windows 参数转换会在 CreateProcess 时改写命令行，
把 Windows 风格的参数全部破坏：

    ./installer.exe /LOG=D:/a.log /S /D="D:\\Espressif"

    实际收到 ↓

    installer.exe "D:/Program Files/WorkBuddy/.../LANG=zh_CN"
                  "D:\\Program Files\\...\\LOG=D;...\\install.log"
                  S:/
                  "D:\\Program Files\\...\\D=D;\\Espressif"

即：`/S` 静默开关失效、`/LOG=` 路径丢失、`/D=` 安装目录被改写。
Python 的 subprocess 是直接调用 CreateProcess，**不做路径转换**，
所以用它当跳板即可原样传递参数。

用法
----
    python winrun.py <程序> [参数...]
    python winrun.py --cd <工作目录> <程序> [参数...]

示例
----
    python winrun.py D:/Espressif/python_env/Scripts/esptool.exe chip_id -p COM5
    python winrun.py D:/espdev/installers/xxx.exe /S /D=D:\\Espressif
"""

import os
import subprocess
import sys

# 由 WorkBuddy / MSYS 注入、会污染子进程的环境变量。
DROP_ENV = {
    "MSYSTEM",
    "MSYSCON",
    "MSYS2_PATH_TYPE",
    "MSYS2_ARG_CONV_EXCL",
    "PYTHONHOME",
    "PYTHONNOUSERSITE",
    "_",
    "PWD",
    "OLDPWD",
    "SHLVL",
    "TMPDIR",
    "ORIGINAL_PATH",
    "EXEPATH",
}

# WorkBuddy 注入的 Python shim，会让子进程加载到错误的包。
workbuddy_pythonpath = os.path.join(
    os.environ.get("PROGRAMFILES", "C:\\Program Files"),
    "WorkBuddy", "resources", "app.asar.unpacked", "cli", "vendor", "shim",
)


def build_env() -> dict:
    env = dict(os.environ)
    for key in DROP_ENV:
        env.pop(key, None)

    # 剔除无效的单纯盘符（例如污染出来的 "D"）与 Python shim。
    cleaned = []
    for part in env.get("PATH", "").split(";"):
        part = part.strip()
        if not part or len(part) <= 2:
            continue
        if part.rstrip("\\") == workbuddy_pythonpath:
            continue
        cleaned.append(part)
    env["PATH"] = ";".join(cleaned)

    pp = [
        p for p in env.get("PYTHONPATH", "").split(";")
        if p and workbuddy_pythonpath not in p
    ]
    if pp:
        env["PYTHONPATH"] = ";".join(pp)
    else:
        env.pop("PYTHONPATH", None)

    return env


def main() -> int:
    argv = sys.argv[1:]
    if len(argv) < 2:
        print(__doc__)
        return 2

    cwd = None
    if argv[0] == "--cd":
        cwd = argv[1]
        argv = argv[2:]

    print(f"[winrun] exe   : {argv[0]}")
    print(f"[winrun] args  : {argv[1:]}")
    if cwd:
        print(f"[winrun] cwd   : {cwd}")
    sys.stdout.flush()

    return subprocess.call(argv, env=build_env(), cwd=cwd)


if __name__ == "__main__":
    raise SystemExit(main())
