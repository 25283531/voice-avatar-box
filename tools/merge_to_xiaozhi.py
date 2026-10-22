#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_to_xiaozhi.py —— 把自研 elder 组件「合并」进 xiaozhi 源码树，使其可直接编译。

为什么需要它
------------
firmware/xiaozhi/ 是上游 xiaozhi-esp32 的只读镜像（由 sync_xiaozhi.py 生成，
每次同步都会覆盖）。自研代码必须放在 firmware/components/elder/，
再在**每次同步之后**由本脚本注入，否则会被下一次同步冲掉。

本脚本做三件事（全部幂等，可反复执行）：
  1. 拷贝 firmware/components/elder → firmware/xiaozhi/components/elder
  2. 在 firmware/xiaozhi/main/CMakeLists.txt **末尾追加**一段 overlay，
     把 elder 的 .c/.cc 直接编进 main 组件（从而能访问 Application / Display）
  3. 把板卡与 elder 相关的 Kconfig 默认值 **追加**进
     firmware/xiaozhi/sdkconfig.defaults

为什么是「追加」而不是改写
--------------------------
追加内容都带 `>>> BEGIN/END elder-companion overlay >>>` 标记，
脚本先剥离旧标记段再追加，因此重复执行不会叠加，也不会破坏上游原文。

用法
----
    python tools/merge_to_xiaozhi.py            # 执行合并
    python tools/merge_to_xiaozhi.py --check    # 只检查是否已合并
    python tools/merge_to_xiaozhi.py --clean    # 撤销合并（还原上游）

完整流程：
    python tools/sync_xiaozhi.py        # 拉上游源码
    python tools/merge_to_xiaozhi.py    # 注入自研代码
    python tools/idfwin.py -C ../firmware/xiaozhi set-target esp32s3
    python tools/idfwin.py -C ../firmware/xiaozhi build
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

# 以脚本自身位置推导仓库根目录，保证本地与 CI（Linux runner）行为一致。
# 本脚本在 <repo>/tools/ 下，故根目录是其上两级。
ROOT = Path(__file__).resolve().parent.parent
ELDER_SRC = ROOT / "firmware" / "components" / "elder"
XIAOZHI = ROOT / "firmware" / "xiaozhi"
ELDER_DST = XIAOZHI / "components" / "elder"
MAIN_CMAKE = XIAOZHI / "main" / "CMakeLists.txt"
MAIN_CC = XIAOZHI / "main" / "main.cc"
SDK_DEFAULTS = XIAOZHI / "sdkconfig.defaults"
LCD_DISPLAY_CC = XIAOZHI / "main" / "display" / "lcd_display.cc"
LCD_DISPLAY_H = XIAOZHI / "main" / "display" / "lcd_display.h"
MCP_SERVER_CC = XIAOZHI / "main" / "mcp_server.cc"

BEGIN = "# >>> BEGIN elder-companion overlay (由 tools/merge_to_xiaozhi.py 注入，勿手改) >>>"
END = "# <<< END elder-companion overlay <<<"

# 追加到 main/CMakeLists.txt 的内容。
# 关键点：用 target_sources()/target_include_directories() 把 elder 的源码
# 直接编进 main 组件 —— 这样 elder_bridge.cc 才能链接到 xiaozhi 的
# Application / Board / Display 等符号。若改成「独立的 elder 组件 + REQUIRES」，
# 组件是无法反向依赖 main 的。
CMAKE_OVERLAY = f"""{BEGIN}
# ---- 只把 C++ 适配层编进 main ----
# elder_bridge.cc 需要访问 xiaozhi 的 Application / Board / Display / AudioService，
# 这些符号只存在于 main 组件，而 ESP-IDF 的组件不能反向依赖 main，
# 所以它必须编进 main。
#
# 其余纯 C 源文件（rgb/input/reminder/activation/ui/net/sync）由
# components/elder 组件自己的 CMakeLists.txt 编译。
# ⚠️ 千万不要把它们也加进这里的 target_sources()：同一批 .c 会被编译两次，
#    链接期直接 duplicate symbol（本机增量编译不会暴露，全量构建才报）。
set(ELDER_DIR "${{PROJECT_DIR}}/components/elder")
target_sources(${{COMPONENT_LIB}} PRIVATE
    "${{ELDER_DIR}}/src/elder_bridge.cc"
    "${{ELDER_DIR}}/src/elder_bind_guide.cc"
)
target_include_directories(${{COMPONENT_LIB}} PRIVATE "${{ELDER_DIR}}/include")
# 链接 elder 组件：同时传递它的 REQUIRES（driver / json / esp_http_client 等）
target_link_libraries(${{COMPONENT_LIB}} PRIVATE idf::elder)

# ---- 在 app_main() 启动 xiaozhi **之前**拉起未绑定设备的绑定引导 ----
{END}
"""

# 追加到 main/main.cc 的内容：在 app.Start() 之后调用 elder_bridge_start()。
#
# ⚠️ 关键事实（2026-10-19 实测代码核实，推翻了早期的错误注释）：
#   app.Start() **不是**「创建主事件循环任务并立刻返回」。它在
#   CheckNewVersion(ota) 之后还有 protocol_ 初始化，而未绑定设备的
#   CheckNewVersion() 是 `while (true)` 永不返回：
#       ShowActivationCode(ota.GetActivationCode(), …)  // 念码播报
#       for (int i = 0; i < 10; ++i) { ota.Activate(); } // v1 无 challenge，必败
#       → 回到循环顶部再来一轮
#   因此 **elder_boot 这个放在 app.Start() 之后的任务，在未绑定设备上
#   永远不会被创建**（这正是原第 8 步「未绑定时屏显激活码」形同虚设的
#   根因，见 D24）。绑定引导因此必须放在 app.Start() 之前（MAIN_CC_CALL_PRE）。
#
# ⚠️ 踩过的坑（C++ 语言规则，不是风格问题）：
#   `extern "C"` **只能出现在命名空间作用域**，写在函数体里是语法错误：
#       main.cc:35:12: error: expected unqualified-id before string constant
#       main.cc:38:9: error: 'elder_bridge_start' was not declared in this scope
#   连带出第二个错误：声明没生效，后面的调用自然找不到符号。
#   所以这里用普通 C++ 声明（不套 extern "C"）—— elder_bridge.cc 那边自己
#   是 `extern "C"` 定义，而 main.cc 里 lambda 捕获的调用会按 C++ 规则去找
#   名字，两边对不上。所以正确做法是**在文件作用域加一个带 extern "C" 的
#   前置声明**（见 MAIN_CC_DECL），函数体里只留调用。
MAIN_CC_DECL = """
// >>> BEGIN elder-companion overlay (由 tools/merge_to_xiaozhi.py 注入，勿手改) >>>
// 老人陪伴 & 提醒助手：等 xiaozhi 的 Board / Display / AudioService 就绪后拉起
// 声明必须在**文件作用域**：extern "C" 写在函数体里是语法错误。
extern "C" void elder_bridge_start(void);
// 未绑定设备的绑定引导（必须在 app.Start() 之前启动，见 MAIN_CC_CALL_PRE 注释）
extern "C" esp_err_t elder_bind_guide_start(void);
// <<< END elder-companion overlay <<<
"""

# 插在 app.Start() **之前**的调用块。
#
# 为什么必须在这里：未绑定设备上 app.Start() 永不返回（CheckNewVersion 的
# while(true)），任何放在它之后的代码都执行不到 —— 包括原来的 elder_boot 任务。
# 引导任务只依赖 Board::GetInstance().GetDisplay()，Board 在 app.Start() 的
# 第一行就构造完，所以「提前起任务」不会拿到未初始化的 Display。
MAIN_CC_CALL_PRE = """
    // >>> BEGIN elder-companion overlay (由 tools/merge_to_xiaozhi.py 注入，勿手改) >>>
    // 未绑定设备的「请去 APP 绑定」引导。必须在 app.Start() **之前**起：
    // 未绑定设备上 app.Start() 会永久阻塞在 CheckNewVersion 的 while(true)，
    // 放在它之后的任何初始化都不会执行。详见 tools/merge_to_xiaozhi.py 的
    // MAIN_CC_CALL_PRE 注释与 docs/决策记录.md D24。
    elder_bind_guide_start();
    // <<< END elder-companion overlay <<<
"""

MAIN_CC_CALL = """
    // >>> BEGIN elder-companion overlay (由 tools/merge_to_xiaozhi.py 注入，勿手改) >>>
    xTaskCreatePinnedToCore([](void *arg) {
        vTaskDelay(pdMS_TO_TICKS(2000));
        elder_bridge_start();
        vTaskDelete(NULL);
    }, "elder_boot", 4096, NULL, 5, NULL, 1);
    // <<< END elder-companion overlay <<<
"""

# 追加到 sdkconfig.defaults：立创·实战派 + 显示风格 + elder 后端地址
SDKCONFIG_OVERLAY = f"""{BEGIN}
# 目标板卡：立创·实战派 ESP32-S3
CONFIG_BOARD_TYPE_LICHUANG_DEV_S3=y
#
# 显示风格：标准 LVGL 界面（SpiLcdDisplay），**不要**开 CONFIG_USE_EMOTE_MESSAGE_STYLE。
#
# 为什么放弃 emote（esp_emote_gfx + EAF 序列帧）形象：真机烧录后屏幕全黑，串口刷
#   gfx_label: FreeType font detected but support is not enabled
#   gfx_label: font context is NULL
#   LichuangDevBoard: Touch display is not initialized
# 根因链（已在本仓库源码上逐条核对）：
#   1) EmoteDisplay 走的是 esp_emote_gfx 自绘引擎（gfx_emote_init），**根本不创建
#      LVGL display**，也不调用 lv_init()。所以 lichuang_dev_board.cc:194 的
#      lv_display_get_default() 必然返回 NULL —— 触摸屏那条报错是 emote 路径的
#      固有产物，不是 LCD 初始化失败。
#   2) 文字渲染走 gfx_draw_label.c:137 的 gfx_is_lvgl_font() 判断，判断失败就把
#      font_ctx 置 NULL（:143-145），之后每次画字都报 "font context is NULL"。
#   3) 实测本仓字体**不是** FreeType 字体，开 CONFIG_GFX_FONT_FREETYPE_SUPPORT 无效：
#      · assets 里的 font_puhui_common_20_4.bin（1,240,332 B）头部就是 lv_font_t：
#        line_height=25 / base_line=6 / dsc=36 / cmap_num=27 / bpp=4，
#        与编译进固件的 font_puhui_basic_20_4.c（line_height=25, base_line=6,
#        cmap_num=7）完全同构 ⇒ gfx_is_lvgl_font() 返回 **true**，
#        压根不会进 #else 的 FreeType 分支；
#      · FreeType 分支只服务 gfx_label_new_font() 载入的内存 TTF，xiaozhi 从未调用。
#      开它只会白白把 FreeType 库编进固件（Flash 变大），症状不变。
# 改回 emote 只需把下面这行换成 CONFIG_USE_EMOTE_MESSAGE_STYLE=y（但要先把上面
# 第 1) 条的 lv_init/LVGL display 缺失和第 2) 条的字体判定一起修掉，否则依旧黑屏）。
CONFIG_USE_DEFAULT_MESSAGE_STYLE=y
# 摄像头已删除（2026-10-22 用户确认不用）——CONFIG_CAMERA_GC0308 及
# esp_video / esp_cam_sensor / esp_new_jpeg / esp_image_effects 依赖全部移除。
# 与后端 TZ=Asia/Shanghai 对齐的提示音语言
CONFIG_LANGUAGE_ZH_CN=y
# 16MB Flash + Octal PSRAM（立创·实战派 N16R8）
CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y
CONFIG_SPIRAM=y
CONFIG_SPIRAM_MODE_OCT=y
CONFIG_SPIRAM_SPEED_80M=y
# 分区表：v2/16m.csv（含 8MB assets 分区，存放 EAF 形象包 + 唤醒模型）
CONFIG_PARTITION_TABLE_CUSTOM=y
CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions/v2/16m.csv"
# --- 唤醒：回归上游小智（2026-10-20 用户拍板）---
# 放弃自研 dual_wake_word/MultiNet 离线命令词，唤醒/对话/发声全用上游原生。
# 唤醒词 = 「你好小智」（上游 WakeNet 默认 wn9_nihaoxiaozhi_tts）。
# 这里**不覆盖** CONFIG_USE_CUSTOM_WAKE_WORD / CONFIG_CUSTOM_WAKE_WORD /
# CONFIG_SR_WN_* / CONFIG_SR_MN_* —— 让 Kconfig 默认 USE_AFE_WAKE_WORD（S3+SPIRAM）
# 接管。上游 sdkconfig.defaults.esp32s3 自带 CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=y，
# 正是「你好小智」的模型，与本次回归一致，不要再删。
# 离线命令词（elder_cmd/elder_mn/custom_wake_word）已删除，NVS 离线提醒
# 引擎（reminder.c 到点播报）保留。
# 业务后端地址（2026-10-18 起 IP 直连：域名未备案被厂商按 SNI 阻断，
# 连 IP 不带 SNI 可绕过；服务端配 IP 证书后可开证书校验，见 Kconfig 帮助）
CONFIG_ELDER_SERVER_URL="https://152.136.230.132"
CONFIG_ELDER_SERVER_TOKEN=""
CONFIG_ELDER_SYNC_INTERVAL_SEC=300
CONFIG_ELDER_HTTP_TIMEOUT_MS=8000
# 立创板无板载 WS2812；外接灯珠时才打开
CONFIG_ELDER_ENABLE_RGB=n
# --- 引脚：产品级换板只改这里，不改代码（2026-10-20）---
# GH1.25 5P 扩展口只引出 GPIO10/11 两根信号线：彩灯 10、按键 11。
# 时钟走板载 I2C（立创板建在端口 1，地址 0x68 与音频器件不冲突）。
# ⚠️ 不要把 ELDER_KEY_GPIO 填回 0：GPIO0 是 BOOT 键，被自动下载电路接管，
#    与上游 xiaozhi 的按键语义双触发，USB 无主机时还会被持续拉低。
CONFIG_ELDER_KEY_GPIO=11
CONFIG_ELDER_RGB_GPIO=10
# 原型期无时钟芯片：先关。接上 DS3231 模块后改 y，开机即从 RTC 恢复时间。
CONFIG_ELDER_RTC_ENABLE=n
CONFIG_ELDER_RTC_I2C_PORT=1
# 无 RTC 时用 NVS 里的上次校时 epoch 兜底，避免掉电重启后当天提醒一条不响。
# 注意这是近似时间（关机时长未知），联网后立刻校正。
CONFIG_ELDER_TIME_NVS_FALLBACK=y
# 演示提醒数据（无后端联调时可临时打开）
CONFIG_ELDER_DEMO_DATA=n
# xiaozhi 的 OTA/WS 地址由后端 /api/device/sync 下发，此处保持默认即可
{END}
"""


# 打进 main/display/lcd_display.cc 的补丁：补上 LVGL 9.3.0 未暴露的内部声明。
#
# 背景（上游真实缺陷，不是我们引入的）：
#   lcd_display.cc 在 `#if CONFIG_SPIRAM` 分支里调用 lv_image_cache_resize()，
#   而该函数的声明位于 LVGL 的 src/misc/cache/instance/lv_image_cache.h，
#   这个头文件**没有被 lvgl.h 包含**（只有内部的 lv_cache.h 会包含它），
#   于是 `CONFIG_SPIRAM=y` 时必然编译失败：
#       error: 'lv_image_cache_resize' was not declared in this scope
#   上游 sdkconfig.defaults.esp32s3 本身就开了 CONFIG_SPIRAM，无法绕开。
#
# 做法：在文件顶部（#define TAG 之前，处于文件作用域）插入一段带 __has_include
# 兜底的补丁 —— 头文件在就直接包含，不在就退化为显式 extern "C" 声明，
# 这样即便将来 LVGL 换版本/挪动文件也不会把构建搞挂。
LCD_DISPLAY_PATCH = """
// >>> BEGIN elder-companion overlay (由 tools/merge_to_xiaozhi.py 注入，勿手改) >>>
// LVGL 9.3.0 把 lv_image_cache_resize() 藏在内部头文件里，lvgl.h 未暴露，
// 而本文件在 CONFIG_SPIRAM 分支调用了它 —— 补上声明，否则编译失败。
#if defined(__has_include)
#if __has_include("src/misc/cache/instance/lv_image_cache.h")
#include "src/misc/cache/instance/lv_image_cache.h"
#else
extern "C" void lv_image_cache_resize(uint32_t new_size, bool evict_now);
#endif
#else
#include "src/misc/cache/instance/lv_image_cache.h"
#endif
// <<< END elder-companion overlay <<<

"""


def strip_overlay(text: str, begin: str, end: str) -> tuple[str, bool]:
    """去掉已有的 overlay 段，返回 (新文本, 是否原本存在)。"""
    had = begin in text
    if not had:
        return text, False
    out = []
    skipping = False
    for line in text.splitlines(keepends=True):
        if line.strip() == begin:
            skipping = True
            continue
        if skipping and line.strip() == end:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "".join(out), True


def append_overlay(path: Path, overlay: str, begin: str = BEGIN, end: str = END) -> bool:
    """幂等地把 overlay 追加到文件末尾。返回是否改动。"""
    if not path.is_file():
        print(f"[merge] 文件不存在，跳过: {path}", file=sys.stderr)
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    text, _ = strip_overlay(text, begin, end)
    new = text.rstrip("\n") + "\n\n" + overlay
    if new == path.read_text(encoding="utf-8", errors="replace"):
        return False
    path.write_text(new, encoding="utf-8")
    return True


def remove_overlay(path: Path, begin: str = BEGIN, end: str = END) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    new, had = strip_overlay(text, begin, end)
    if had:
        path.write_text(new.rstrip("\n") + "\n", encoding="utf-8")
    return had


def insert_once(path: Path, anchor: str, patch: str, tag: str, before: bool = True) -> bool:
    """幂等地在 anchor 行前/后插入 patch。已存在 tag 则返回 False。"""
    if not path.is_file():
        print(f"[merge] 文件不存在，跳过: {path}", file=sys.stderr)
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    if tag in text:
        return False
    if anchor not in text:
        print(f"[merge] 警告：{path} 中未找到锚点 `{anchor.strip()}`，跳过该补丁",
              file=sys.stderr)
        return False
    if before:
        text = text.replace(anchor, patch + anchor, 1)
    else:
        text = text.replace(anchor, anchor + patch, 1)
    path.write_text(text, encoding="utf-8")
    return True


def merge() -> int:
    if not ELDER_SRC.is_dir():
        print(f"[merge] 源组件不存在: {ELDER_SRC}", file=sys.stderr)
        return 1
    if not XIAOZHI.is_dir():
        print(f"[merge] 请先运行 tools/sync_xiaozhi.py（缺 {XIAOZHI}）", file=sys.stderr)
        return 1

    # 1) 拷贝组件（先清后拷，保证与源完全一致）
    if ELDER_DST.exists():
        shutil.rmtree(ELDER_DST)
    ELDER_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ELDER_SRC, ELDER_DST)
    n_files = sum(1 for _ in ELDER_DST.rglob("*") if _.is_file())
    print(f"[merge] 组件已同步: {ELDER_DST} ({n_files} 个文件)")

    # 2) main/CMakeLists.txt overlay
    if append_overlay(MAIN_CMAKE, CMAKE_OVERLAY):
        print(f"[merge] 已注入: {MAIN_CMAKE}")
    else:
        print(f"[merge] 已是最新: {MAIN_CMAKE}")

    # 3) main/main.cc：
    #    - 文件作用域加 `extern "C" void elder_bridge_start(void);` 前置声明
    #      （必须在这里 —— extern "C" 写在函数体里是语法错误，见 MAIN_CC_DECL）
    #    - app.Start() 之后插入启动调用
    #
    # ⚠️ 不能沿用 "elder_bridge_start 是否出现过" 来判重：早期版本把
    #    extern "C" 声明写在了函数体里（语法错误，编译必挂），那处文本里
    #    同样含有 "elder_bridge_start" 字样，旧判重会误判成「已注入」而
    #    永远修不好。改为按 overlay 标记整段剥离后重建（幂等且能纠错）。
    if MAIN_CC.is_file():
        text = MAIN_CC.read_text(encoding="utf-8")
        # ⚠️ 标记必须传**不带缩进**的形态：strip_overlay() 是按 `line.strip()`
        #    比对行内容的，传带 4 空格的标记永远匹配不上 ⇒ 旧 overlay 剥不掉，
        #    每跑一次 merge 就多插一份 elder_boot 任务（HEAD 上曾累积到 3 份，
        #    等于 elder_bridge_start() 被调 3 次）。
        stripped, had_overlay = strip_overlay(
            text,
            "// >>> BEGIN elder-companion overlay (由 tools/merge_to_xiaozhi.py 注入，勿手改) >>>",
            "// <<< END elder-companion overlay <<<",
        )
        # 也剥掉可能残留在文件作用域的裸声明（MAIN_CC_DECL 的形态）
        stripped = stripped.replace(
            'extern "C" void elder_bridge_start(void);\n', "")
        stripped = stripped.replace(
            'extern "C" esp_err_t elder_bind_guide_start(void);\n', "")

        marker = "    app.Start();\n"
        if marker not in stripped:
            print("[merge] 警告：main.cc 中未找到 `app.Start();`，需手动接入",
                  file=sys.stderr)
        else:
            # ⚠️ 顺序要紧：先插 app.Start() **之前**的引导块，再插之后的
            #    elder_boot。两段都靠同一对 BEGIN/END 标记整段剥离，幂等。
            stripped = stripped.replace(marker, MAIN_CC_CALL_PRE + marker, 1)
            stripped = stripped.replace(
                marker, marker + MAIN_CC_CALL, 1)
            # 前置声明插在 app_main() 定义之前（文件作用域）
            anchor = 'extern "C" void app_main(void)'
            if anchor in stripped:
                stripped = stripped.replace(
                    anchor, MAIN_CC_DECL + "\n" + anchor, 1)
            else:
                print("[merge] 警告：main.cc 中未找到 app_main() 定义，"
                      "elder_bridge_start 的声明需手动补到文件作用域",
                      file=sys.stderr)

            # 空白规范化：strip_overlay 只删 BEGIN/END 两行，overlay 两侧的空行
            # 会残留；而 MAIN_CC_DECL / MAIN_CC_CALL 自带前导空行 ⇒ 每跑一次
            # merge 就多几行空行（HEAD 上曾累积到连续 6 行）。把 2 个及以上
            # 连续空行压成 1 个，保证反复 merge 后 main.cc 字节稳定。
            stripped = re.sub(r"\n{3,}", "\n\n", stripped)

            changed = stripped != text
            if changed:
                MAIN_CC.write_text(stripped, encoding="utf-8")
                print(f"[merge] 已注入启动调用: {MAIN_CC}"
                      + ("（含旧 overlay 纠错）" if had_overlay else ""))
            else:
                print(f"[merge] 已是最新: {MAIN_CC}")

    # 4) sdkconfig.defaults overlay
    if append_overlay(SDK_DEFAULTS, SDKCONFIG_OVERLAY):
        print(f"[merge] 已注入: {SDK_DEFAULTS}")
    else:
        print(f"[merge] 已是最新: {SDK_DEFAULTS}")

    # 5) main/display/lcd_display.cc 补丁（LVGL 9.3.0 + PSRAM 的 lv_image_cache_resize 声明）
    if insert_once(LCD_DISPLAY_CC, '#define TAG "LcdDisplay"\n', LCD_DISPLAY_PATCH,
                   "elder-companion overlay"):
        print(f"[merge] 已注入: {LCD_DISPLAY_CC}")
    else:
        print(f"[merge] 已是最新: {LCD_DISPLAY_CC}")

    print("\n[merge] 完成。下一步：")
    print("  python tools/idfwin.py set-target esp32s3   (需先 cd 到 firmware/xiaozhi)")
    print("  python tools/idfwin.py build")
    return 0


def clean() -> int:
    for p in (MAIN_CMAKE, SDK_DEFAULTS):
        if remove_overlay(p):
            print(f"[merge] 已还原: {p}")
    cpp_block = re.compile(
        r"\n *// >>> BEGIN elder-companion overlay.*?// <<< END elder-companion overlay <<<\n",
        flags=re.S)
    for p in (MAIN_CC, LCD_DISPLAY_CC):
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        new = cpp_block.sub("", text)
        if new != text:
            p.write_text(new, encoding="utf-8")
            print(f"[merge] 已还原: {p}")
    if ELDER_DST.exists():
        shutil.rmtree(ELDER_DST)
        print(f"[merge] 已删除: {ELDER_DST}")
    return 0


def check() -> int:
    ok = True
    ok &= ELDER_DST.is_dir()
    ok &= MAIN_CMAKE.is_file() and BEGIN in MAIN_CMAKE.read_text(encoding="utf-8")
    ok &= MAIN_CC.is_file() and "elder_bridge_start" in MAIN_CC.read_text(encoding="utf-8")
    ok &= SDK_DEFAULTS.is_file() and BEGIN in SDK_DEFAULTS.read_text(encoding="utf-8")
    lcd_ok = LCD_DISPLAY_CC.is_file() and "elder-companion overlay" in LCD_DISPLAY_CC.read_text(encoding="utf-8")
    ok &= lcd_ok
    # 70/30 对话布局是**直接改上游 lcd_display.cc** 的定制（不是 overlay 注入），
    # 上游 sync_xiaozhi.py --force 会把它冲掉 ⇒ 这里守住标记，CI 的 merge --check
    # 会红，避免「布局悄悄回退」还照样出包。重贴步骤见 README「上游定制补丁」节。
    layout_ok = LCD_DISPLAY_CC.is_file() and "elder-layout-70-30" in LCD_DISPLAY_CC.read_text(encoding="utf-8")
    ok &= layout_ok
    # 需求 8（2026-10-22）：全屏大字提醒。同样是**直接改上游**的两处
    # （lcd_display.h 加声明、lcd_display.cc 加实现），上游 sync 会一起冲掉。
    # 两个文件都要守：只冲掉头文件时，症状是链接期
    # "no member named 'ShowReminder'"；只冲掉实现则反过来 —— 两者都不该
    # 等到编译 8 分钟后才发现。
    remind_h_ok = LCD_DISPLAY_H.is_file() and "elder-reminder-fullscreen" in LCD_DISPLAY_H.read_text(encoding="utf-8")
    remind_cc_ok = LCD_DISPLAY_CC.is_file() and "elder-reminder-fullscreen" in LCD_DISPLAY_CC.read_text(encoding="utf-8")
    ok &= remind_h_ok and remind_cc_ok
    # 需求 7（2026-10-22）：待机。mcp_server.cc 里注册 self.elder.standby，
    # 也是直接改上游的定制。
    standby_ok = MCP_SERVER_CC.is_file() and "elder_enter_standby" in MCP_SERVER_CC.read_text(encoding="utf-8")
    ok &= standby_ok
    print("[merge] 合并状态:", "已合并" if ok else "未合并/不完整")
    print(f"  components/elder : {'OK' if ELDER_DST.is_dir() else 'MISSING'}")
    print(f"  main/CMakeLists  : {'OK' if MAIN_CMAKE.is_file() and BEGIN in MAIN_CMAKE.read_text(encoding='utf-8') else 'MISSING'}")
    print(f"  main/main.cc     : {'OK' if MAIN_CC.is_file() and 'elder_bridge_start' in MAIN_CC.read_text(encoding='utf-8') else 'MISSING'}")
    print(f"  sdkconfig.defaults: {'OK' if SDK_DEFAULTS.is_file() and BEGIN in SDK_DEFAULTS.read_text(encoding='utf-8') else 'MISSING'}")
    print(f"  display/lcd_display.cc: {'OK' if lcd_ok else 'MISSING'}")
    print(f"  lcd_display.cc 70/30 布局: {'OK' if layout_ok else 'MISSING（被上游同步冲掉？按 README「上游定制补丁」节重贴）'}")
    print(f"  lcd_display.h 全屏提醒: {'OK' if remind_h_ok else 'MISSING（需求 8）'}")
    print(f"  lcd_display.cc 全屏提醒: {'OK' if remind_cc_ok else 'MISSING（需求 8）'}")
    print(f"  mcp_server.cc 待机工具: {'OK' if standby_ok else 'MISSING（需求 7）'}")
    return 0 if ok else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="把 elder 组件合并进 xiaozhi 源码树")
    ap.add_argument("--check", action="store_true", help="只检查合并状态")
    ap.add_argument("--clean", action="store_true", help="撤销合并")
    args = ap.parse_args()
    if args.check:
        return check()
    if args.clean:
        return clean()
    return merge()


if __name__ == "__main__":
    raise SystemExit(main())
