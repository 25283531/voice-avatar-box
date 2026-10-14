#!/usr/bin/env python3
"""
make_firmware.py —— 把 ESP-IDF 的多个烧录分区合并成「单个 bin」，便于手工烧录。

为什么：ESP-IDF 默认产出 bootloader / app / partition-table 三个 bin + 三个偏移地址，
        手工烧录时容易写错地址。合并成一个 0x0 起的整体镜像后，只需一条命令：

            esptool write_flash 0x0 firmware.bin

用法：
    python make_firmware.py                      # 自动查找 <工程>/build/flash_args
    python make_firmware.py <build_dir> <输出bin>
    python make_firmware.py --list               # 仅打印待合并的分区，不执行

依赖：esptool（已在 D:\\Espressif\\python_env 中）
"""

import os
import subprocess
import sys
from pathlib import Path

ESPTOOL = Path(r"D:\Espressif\python_env\Scripts\esptool.exe")
DEFAULT_BUILD = Path(r"D:\espdev\workspace\elder-build")
DEFAULT_OUT = Path(r"G:\workbuddy\语音助手\dist\elder-companion.bin")


def read_flash_args(build_dir: Path):
    """从 build/flash_args 解析出 (全局参数, [(偏移, 相对路径)])"""
    f = build_dir / "flash_args"
    if not f.is_file():
        raise SystemExit(f"找不到 {f}，请先完成 idf.py build")

    opts = []
    files = []
    for raw in f.read_text(encoding="utf-8").split():
        if raw.startswith("--"):
            opts.append(raw)
        elif raw.startswith("0x"):
            files.append([raw, None])
        elif files and files[-1][1] is None:
            files[-1][1] = raw
    return opts, [(a, b) for a, b in files if b]


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "--list"]
    list_only = "--list" in sys.argv[1:]

    if len(argv) >= 2:
        build_dir, out = Path(argv[0]), Path(argv[1])
    elif len(argv) == 1:
        build_dir, out = Path(argv[0]), DEFAULT_OUT
    else:
        build_dir, out = DEFAULT_BUILD, DEFAULT_OUT

    opts, files = read_flash_args(build_dir)

    print(f"[mkfw] 工程构建目录: {build_dir}")
    print(f"[mkfw] flash 参数   : {' '.join(opts) if opts else '(默认)'}")
    for off, rel in files:
        p = build_dir / rel
        size = p.stat().st_size if p.is_file() else -1
        print(f"[mkfw]   {off:<10} {rel:<45} {size:>8,} bytes")
    print(f"[mkfw] 输出镜像     : {out}")

    if list_only:
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [str(ESPTOOL), "--chip", "esp32s3"]
    if "--flash_mode" not in opts:
        cmd += ["--flash_mode", "dio"]
    if "--flash_freq" not in opts:
        cmd += ["--flash_freq", "80m"]
    if "--flash_size" not in opts:
        cmd += ["--flash_size", "detect"]
    cmd += ["merge_bin", "-o", str(out)]
    cmd += opts
    for off, rel in files:
        cmd += [off, str(build_dir / rel)]

    print("\n[mkfw] 执行:", " ".join(cmd[:6]), "...")
    rc = subprocess.call(cmd, env={**os.environ, "MSYSTEM": os.environ.get("MSYSTEM", "")})
    if rc != 0:
        print("[mkfw] 合并失败 rc =", rc)
        return rc

    size = out.stat().st_size
    print(f"\n[mkfw] 完成: {out}  ({size:,} bytes)")
    print("\n烧录命令（把 COMx 换成你的实际端口）：")
    print(f'  "{ESPTOOL}" --chip esp32s3 -p COMx -b 460800 write_flash 0x0 "{out}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
