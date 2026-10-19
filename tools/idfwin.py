"""
idfwin.py - 在 Windows 上以「原生环境」方式调用 ESP-IDF 工具。

背景：本机默认 shell 是 Git Bash，而 ESP-IDF 明确禁止在 MSys/Mingw 下运行
（idf_tools.py 硬报错、CMake 会误用 MSYS 的 gcc）。本脚本在启动子进程前：
  1. 清除 MSYSTEM / 相关 MSYS 变量
  2. 从 PATH 中剔除 Git-for-Windows / MSYS / Mingw 目录
  3. 注入 IDF_PATH / IDF_TOOLS_PATH

用法：
  python tools/idfwin.py --version
  python tools/idfwin.py install-python-env
  python tools/idfwin.py set-target esp32s3
  python tools/idfwin.py build
"""
import os
import subprocess
import sys
from pathlib import Path

IDF_TOOLS_PATH = Path(r"D:\Espressif")
IDF_PATH = IDF_TOOLS_PATH / "frameworks" / "esp-idf-v5.5.1"
BASE_PYTHON = IDF_TOOLS_PATH / "tools" / "idf-python" / "3.11.2" / "python.exe"

BAD_PATH_MARKERS = ("msys", "mingw", r"\git\usr\bin", r"\git\cmd", r"\git\bin")


def pick_python() -> Path:
    """优先使用 IDF 创建的独立虚拟环境，回退到内置 Python。

    实测 idf_tools.py install-python-env 会把环境直接建在 python_env 根下，
    但不同版本也可能建为 python_env/<name>_py<x>_env，两种情况都要覆盖。
    """
    env_dir = IDF_TOOLS_PATH / "python_env"
    if env_dir.is_dir():
        nested = sorted(env_dir.glob("*/Scripts/python.exe"))
        if nested:
            return nested[-1]
        direct = env_dir / "Scripts" / "python.exe"
        if direct.is_file():
            return direct
    return BASE_PYTHON


def build_env() -> dict:
    env = os.environ.copy()
    for key in ("MSYSTEM", "OSTYPE", "MSYSCON", "MINGW_PREFIX"):
        env.pop(key, None)

    kept = []
    for part in env.get("PATH", "").split(";"):
        low = part.lower()
        if part and not any(marker in low for marker in BAD_PATH_MARKERS):
            kept.append(part)

    # 补回 IDF 自带的 git（cmd/ 下的 git.exe 是原生 Windows 版本，
    # 不会引入 MSYS 的 sh.exe，因此不会让 CMake 误选 MSYS 工具链）
    for git_cmd in sorted((IDF_TOOLS_PATH / "tools" / "idf-git").glob("*/cmd")):
        if git_cmd.is_dir():
            kept.insert(0, str(git_cmd))

    env["PATH"] = ";".join(kept)

    env["IDF_TOOLS_PATH"] = str(IDF_TOOLS_PATH)
    env["IDF_PATH"] = str(IDF_PATH)
    env["IDF_PYTHON_ENV_PATH"] = str((IDF_TOOLS_PATH / "python_env"))
    # ⚠️ 禁用 ccache 的**环境层**（与 idf.py 层双保险）。
    #    仓库路径含中文，ccache 转换路径时抛
    #      "Cannot convert character sequence: Illegal byte sequence"
    #    ⇒ 表现为若干编译单元 FAILED，而报错完全指不到中文路径。
    #    两层都设的原因：export.ps1 会自行探测并启用 ccache，单设一处不可靠。
    env["IDF_CCACHE_ENABLE"] = "0"
    # 让 pip/urllib 走系统代理失效时不再卡住
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2

    if args[0] == "install-python-env":
        cmd = [str(BASE_PYTHON), str(IDF_PATH / "tools" / "idf_tools.py"), "install-python-env"]
        print(f"[idfwin] python : {cmd[0]}")
        sys.stdout.flush()
        return subprocess.call(cmd, env=build_env())

    # 正式构建走官方 export.ps1，由它负责把 cmake / ninja / xtensa 工具链装进 PATH，
    # 避免我们在脚本里硬编码工具版本号。
    quoted = ", ".join(f"'{a}'" for a in args)
    # ⚠️ 必须禁用 ccache：仓库路径含中文（`G:\workbuddy\语音助手\...`），
    #    而 ccache 走自己的路径转换，遇到无法映射的字节序列就抛
    #        what(): filesystem error: Cannot convert character sequence:
    #                 Illegal byte sequence
    #    表现为 12 个编译单元全部 FAILED，报错信息完全指不到「中文路径」。
    #    ⚠️ 必须在 export.ps1 **之前**设：export.ps1 自己会去探测并启用 ccache，
    #    之后再设就晚了（已验证：改前 12 FAILED / 改后全绿）。
    #    CI（ubuntu）路径无中文，不需要这个开关，但同一份脚本两边都能跑。
    script = (
        "$ErrorActionPreference='Continue'\n"
        f"$env:IDF_TOOLS_PATH='{IDF_TOOLS_PATH}'\n"
        f"$env:IDF_PATH='{IDF_PATH}'\n"
        "$env:IDF_CCACHE_ENABLE='0'\n"
        f"& '{IDF_PATH / 'export.ps1'}' | Out-Null\n"
        "$env:IDF_CCACHE_ENABLE='0'\n"   # export 之后再说一次，防它自己又打开
        f"& python '{IDF_PATH / 'tools' / 'idf.py'}' @({quoted})\n"
        "exit $LASTEXITCODE\n"
    )
    ps_path = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    cmd = [
        str(ps_path if ps_path.is_file() else "powershell.exe"),
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script,
    ]
    print(f"[idfwin] IDF_PATH : {IDF_PATH}")
    print(f"[idfwin] tools : {IDF_TOOLS_PATH}")
    sys.stdout.flush()
    return subprocess.call(cmd, env=build_env())


if __name__ == "__main__":
    raise SystemExit(main())
