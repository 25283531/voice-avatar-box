"""把固件写进开发板（ESP32-S3），默认只写 app 分区、保留 NVS。

## 为什么默认走 COM5 而不是 COM11

这块板子同时挂了两个串口：

| 口   | VID:PID     | 是什么                          | 适合做什么 |
|------|-------------|---------------------------------|-----------|
| COM5 | 303A:1001   | 芯片**原生** USB-Serial-JTAG    | 烧录、复位（快、稳） |
| COM11| 1A86:7522   | CH340K 桥片，接 UART0           | 看启动日志（能看到最早期 ROM 日志） |

历史包袱：`build-out/flash_app.sh` 走的是 COM11，实测这条路很脆 ——
- stub + 压缩模式连续发包会让 CH340K 的 `WriteFile` **卡死到写超时**（实测 60s 不返回，
  已确认不是 CTS 流控：`fOutxCtsFlow=False`）；
- 退回 ROM 模式（1KB 块、逐块等应答）则会在擦除阶段 `Serial data stream stopped`。

同一条命令换成 COM5（原生 USB）**一次就过**：`Wrote 2995488 bytes (compressed 1663990)
at 0x00020000 in 19.8s`，`Hash of data verified.`。所以**烧录默认 COM5**，
COM11 只用来抓日志。

## 为什么 app 半写后连 COM5 也进不了下载模式（这条更值钱）

CH340K 那套自动下载电路是**经典两管交叉耦合**，真值表：

    DTR=0 RTS=0 -> EN=H IO0=H   常态
    DTR=0 RTS=1 -> EN=L IO0=H   复位
    DTR=1 RTS=0 -> EN=H IO0=L   下载模式
    DTR=1 RTS=1 -> EN=H IO0=H   **两条线都不动作**（不能拿它复位！）

两条实测结论：
1. **RTS 才是 EN、DTR 才是 IO0**（按住 RTS 串口立刻静默，释放必出 `rst:0x1 (POWERON)`；
   按住 DTR 毫无反应）。
2. **DTR 和 RTS 必须同一瞬间切换（gap=0）**。`DTR=0,RTS=1` 保持后**同时**写
   `DTR=1,RTS=0` ⇒ `boot:0x3 (DOWNLOAD(USB/UART0))`、输出仅 143B；中间插 20ms 或
   100ms 延时 ⇒ 退回 `boot:0xb (SPI_FAST_FLASH_BOOT)` 继续崩。有间隙时芯片会在中间态
   被放出来一次，strapping 就采到了高电平。

所以真要走 CH340K 那条路，`esptool --before default_reset` 是**不可靠**的（它默认时序
在这块板上会把芯片放出来一次）。本脚本因此只做 COM5 这条路；CH340K 的手动时序留在
`build-out/probe_dl.py`（未入库，属临时排障脚本）。

## 崩循环的识别

`RTC_SW_SYS_RST` 每 ~28ms 一次（2s 能抓 775 次）、`Saved PC` 落在二级 bootloader 段内
⇒ app/bootloader 分区半写。此时 UART0 被启动日志灌满，esptool 的 `sync` 读到的第一个
字节是日志文本（'P'/'E'/'M' = 0x50/0x45/0x4D）而不是 SLIP 头 0xC0 ⇒
`Invalid head of packet`。**换成 COM5 就绕开了整条被灌满的链路。**

用法：
    python tools/flash_app.py build-out/fw/xiaozhi.bin
    python tools/flash_app.py build-out/fw/elder-firmware-esp32s3-merged-flash.bin --full
    python tools/flash_app.py --port COM5 --baud 460800 build-out/fw/xiaozhi.bin
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 与 tools/boot_capture.py 保持一致：优先用 Espressif 自带的 python 环境
# （里面有和 IDF 匹配的 esptool），本机 python 只作兜底。
PY_CANDIDATES = [
    r"d:/Espressif/python_env/Scripts/python.exe",
    sys.executable,
]

APP_OFFSET = "0x20000"  # ota_0，分区表红线 C14：必须 0x20000


def pick_python() -> str:
    for p in PY_CANDIDATES:
        if Path(p).exists():
            return p
    return sys.executable


def port_exists(port: str) -> bool:
    try:
        from serial.tools import list_ports
    except ImportError:
        return True  # 装不了 pyserial 就不拦，让 esptool 自己报错
    return any(p.device.lower() == port.lower() for p in list_ports.comports())


def main() -> int:
    ap = argparse.ArgumentParser(description="把固件写进 ESP32-S3 开发板")
    ap.add_argument("bin", help="要写的 .bin")
    ap.add_argument("--port", default="COM5", help="默认 COM5（原生 USB，稳）")
    ap.add_argument("--baud", default="460800")
    ap.add_argument("--full", action="store_true",
                    help="写整片镜像（merged bin）到 0x0；默认只写 app 到 0x20000")
    ap.add_argument("--offset", default=None, help="手动指定偏移，覆盖默认值")
    args = ap.parse_args()

    bin_path = Path(args.bin)
    if not bin_path.is_file():
        print(f"[错误] 找不到固件：{bin_path}")
        return 2
    size = bin_path.stat().st_size
    offset = args.offset or ("0x0" if args.full else APP_OFFSET)

    if not port_exists(args.port):
        print(f"[错误] 串口 {args.port} 不存在。当前可用：")
        try:
            from serial.tools import list_ports
            for p in list_ports.comports():
                vid = f"{p.vid:04X}:{p.pid:04X}" if p.vid else "----:----"
                print(f"    {p.device:8s} {vid}  {p.description}")
        except ImportError:
            print("    （未安装 pyserial，无法枚举）")
        print("\n提示：烧录用原生 USB 口（303A:1001）；CH340K（1A86:7522）只适合抓日志。")
        return 2

    print(f"== 写入 {bin_path.name}（{size} 字节）-> {offset} @ {args.port} {args.baud} ==")
    cmd = [
        pick_python(), "-m", "esptool",
        "--chip", "esp32s3",
        "-p", args.port,
        "-b", args.baud,
        "--connect-attempts", "15",
        "--before", "default_reset",
        "--after", "hard_reset",
        "write_flash",
        "--flash_mode", "dio",
        "--flash_freq", "80m",
        "--flash_size", "16MB",
        offset, str(bin_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if any(k in line for k in ("Wrote", "Hash of", "Writing at 0x0002", "Writing at 0x0000",
                                   "Error", "error", "Failed", "failed", "A fatal")):
            print("   ", line.strip())

    if r.returncode != 0 or "Hash of data verified" not in out:
        print("\n[失败] 完整输出：")
        print(out[-4000:])
        return 1

    print("[OK] 写入并校验通过；设备已硬复位")
    if not args.full:
        print("     NVS（WiFi 凭据 / 提醒表 / 启动计数）未动")
    return 0


if __name__ == "__main__":
    sys.exit(main())
