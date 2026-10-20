"""复位设备（脱离 bootloader）并立刻抓取串口，捕捉 BootDiag 复位原因。

为什么要一条脚本完成：esptool run 之后设备立即重启，原生 USB 会重枚举，
分两步会丢掉开头；而 BootDiag 虽然每 10 秒重打，但要拿到「已运行时间」
和最早的堆数字，越早接上越好。
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = r"d:/Espressif/python_env/Scripts/python.exe"
PORT = "COM5"
SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 100
OUT = ROOT / "out" / sys.argv[2] if len(sys.argv) > 2 else ROOT / "out" / "uart_boot.log"


def run_reset() -> None:
    print("== 复位设备 ==", flush=True)
    r = subprocess.run(
        [PY, "-m", "esptool", "--chip", "esp32s3", "-p", PORT, "-b", "460800",
         "--connect-attempts", "30", "--before", "default_reset", "--after", "hard_reset", "run"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    tail = (r.stdout or "").strip().splitlines()[-3:]
    for t in tail:
        print("   ", t, flush=True)


def main() -> int:
    run_reset()

    import serial  # 延迟导入：复位阶段不占用串口

    ser = None
    for i in range(40):
        try:
            ser = serial.Serial(PORT, 115200, timeout=0.2)
            print(f"== 串口已连接（第 {i + 1} 次尝试）==", flush=True)
            break
        except Exception:
            time.sleep(0.5)
    if ser is None:
        print("!! 串口打不开")
        return 4

    buf = bytearray()
    end = time.time() + SECONDS
    while time.time() < end:
        d = ser.read(8192)
        if d:
            buf += d
    ser.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(bytes(buf))
    txt = buf.decode("utf-8", "replace")
    print(f"== 抓取 {len(buf):,} 字节 → {OUT.name} ==", flush=True)

    pat = re.compile(r"(rst:0x[0-9a-f]+[^\r\n]*|BootDiag[^\r\n]*|Guru Meditation[^\r\n]*"
                     r"|abort\(\)[^\r\n]*|assert[^\r\n]*|Brownout[^\r\n]*"
                     r"|interrupt wdt[^\r\n]*|task_wdt[^\r\n]*|invalid header[^\r\n]*"
                     r"|E \([0-9]+\) [^\r\n]*)", re.I)
    hits: list[str] = []
    for ln in txt.splitlines():
        m = pat.search(ln)
        if m and m.group(0) not in hits:
            hits.append(m.group(0).strip())
    print(f"== 关键行 {len(hits)} 条 ==")
    for h in hits[:40]:
        print("   ", h[:190])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
