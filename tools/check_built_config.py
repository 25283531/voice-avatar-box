#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_built_config.py —— 编译**之后**断言 elder 的开关真的进了固件配置。

为什么需要它（2026-10-23 假绿事故）
------------------------------------
用户接上 DS1302 时钟芯片和 WS2812 彩灯后，CI 的 firmware-build 一路绿灯，
但把产物固件下下来搜字符串，`DS1302` 出现 **0 次** —— 说明

    #if CONFIG_ELDER_RTC_ENABLE && defined(CONFIG_ELDER_RTC_DRIVER_DS1302)

这一整块被预处理器跳过了，编译的是 rtc.c 里的空桩。**构建照样成功**：
桩同样提供 elder_rtc_init/read/write/present 四个符号，链接期不报错。
这种"功能静默编空、CI 全绿"的失败模式，只有拿产物去验才能发现。

根因是 `firmware/xiaozhi/sdkconfig.defaults` 的 elder 段由
tools/merge_to_xiaozhi.py 的 SDKCONFIG_OVERLAY **整块重写**，手改生成文件
在 CI 上会被覆盖回模板旧值。C10 已经拦这个来源，本脚本是第二道：
**不看配置源头，只看编译真正读到的结果**。

与 prebuild_check 的分工
------------------------
* prebuild_check.py 跑在编译**前**，读 sdkconfig（kconfig 文本）；
* 本脚本跑在编译**后**，读 build/config/sdkconfig.h（C 头文件）。
  后者才是 `#if` 真正展开的依据 —— 两者不是同一份东西：
  sdkconfig 里有 `CONFIG_X=y` 而 sdkconfig.h 里没有对应 `#define` 的情形
  （符号不存在 / 被 Kconfig 的 depends 判为不可见）只会在后者暴露。

判据
----
1. sdkconfig.defaults 的 elder 段里每个 ``=y`` 的 ``CONFIG_ELDER_*``，
   在 sdkconfig.h 里都必须是 ``#define ... 1``；
2. 若 ``CONFIG_ELDER_RTC_ENABLE`` 为 1，则 DS1302 / DS3231 驱动符号
   必须**恰好有一个**为 1（choice 语义，两个都没有 = 两个后端都没编）。

用法：
    python tools/check_built_config.py            # 默认读 firmware/xiaozhi
    python tools/check_built_config.py --header path/to/sdkconfig.h
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
XIAOZHI = ROOT / "firmware" / "xiaozhi"
SDK_DEFAULTS = XIAOZHI / "sdkconfig.defaults"
BUILD_H = XIAOZHI / "build" / "config" / "sdkconfig.h"

#: 必须与 tools/merge_to_xiaozhi.py / tools/prebuild_check.py 的标记逐字一致。
BEGIN_MARK = "# >>> BEGIN elder-companion overlay"
END_MARK = "# <<< END elder-companion overlay"

DRIVERS = ("CONFIG_ELDER_RTC_DRIVER_DS1302", "CONFIG_ELDER_RTC_DRIVER_DS3231")


def overlay_enabled(defaults_path: Path) -> dict[str, str]:
    """取 elder 段里 ``CONFIG_ELDER_*=y`` 的键（只关心开着的）。"""
    if not defaults_path.is_file():
        return {}
    inside = False
    out: dict[str, str] = {}
    for raw in defaults_path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        if s.startswith(BEGIN_MARK):
            inside = True
            continue
        if s.startswith(END_MARK):
            inside = False
            continue
        if not inside or not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip().strip('"')
        if k.startswith("CONFIG_ELDER_") and v == "y":
            out[k] = v
    return out


def defines(header: Path) -> dict[str, str]:
    """读 sdkconfig.h 的 ``#define CONFIG_X 1``（值为 1 的才算开）。"""
    out: dict[str, str] = {}
    if not header.is_file():
        return out
    for raw in header.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"#define\s+(CONFIG_\S+)\s+(.+)$", raw.strip())
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"')
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="断言 elder 开关真的编进了固件配置")
    ap.add_argument("--header", type=Path, default=BUILD_H,
                    help=f"要检查的 sdkconfig.h（默认 {BUILD_H}）")
    ap.add_argument("--defaults", type=Path, default=SDK_DEFAULTS,
                    help=f"取期望值的 sdkconfig.defaults（默认 {SDK_DEFAULTS}）")
    args = ap.parse_args()

    print("=" * 68)
    print("编译后配置断言（2026-10-23「CI 绿但功能没编进去」事故而加）")
    print("=" * 68)

    if not args.header.is_file():
        print(f"  [FAIL] 找不到 {args.header}")
        print("         没编译过就没有可断言的对象；本检查必须在 idf.py build 之后跑。")
        return 1

    want = overlay_enabled(args.defaults)
    have = defines(args.header)

    bad: list[str] = []
    for key in sorted(want):
        if have.get(key) != "1":
            bad.append(f"{key}: defaults 里是 y，但 sdkconfig.h 里"
                       f"{'缺失' if key not in have else '=' + repr(have[key])}"
                       f" —— 该功能会被 #if 整块跳过，构建却照样成功")

    # choice 语义：RTC 开着就必须恰好选了一个驱动后端。
    if have.get("CONFIG_ELDER_RTC_ENABLE") == "1":
        picked = [d for d in DRIVERS if have.get(d) == "1"]
        if len(picked) != 1:
            bad.append("CONFIG_ELDER_RTC_ENABLE=1 但选中的驱动后端是 "
                       f"{picked or '（一个都没有）'} —— 必须恰好一个")

    for key in sorted(want):
        mark = "OK  " if have.get(key) == "1" else "FAIL"
        print(f"  [{mark}] {key} = {have.get(key, '(缺失)')}")

    print("=" * 68)
    if bad:
        print(f"共 {len(want)} 个期望开关，发现 {len(bad)} 个问题")
        for b in bad:
            print(f"  ✗ {b}")
        print("\n这类失败**不会让构建变红**：被跳过的代码由空桩/另一后端提供同名符号，"
              "\n链接期一切正常，症状是「烧进去功能完全没有」。")
        print("处置：确认 firmware/xiaozhi/sdkconfig.defaults 的 elder 段来自 "
              "tools/merge_to_xiaozhi.py 的 SDKCONFIG_OVERLAY（C10 会拦手改），"
              "\n      再确认对应 Kconfig 符号存在且 depends 满足。")
        return 1

    print(f"共 {len(want)} 个期望开关，全部已生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
