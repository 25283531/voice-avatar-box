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

BEGIN = "# >>> BEGIN elder-companion overlay (由 tools/merge_to_xiaozhi.py 注入，勿手改) >>>"
END = "# <<< END elder-companion overlay <<<"

# 追加到 main/CMakeLists.txt 的内容。
# 关键点：用 target_sources()/target_include_directories() 把 elder 的源码
# 直接编进 main 组件 —— 这样 elder_bridge.cc 才能链接到 xiaozhi 的
# Application / Board / Display 等符号。若改成「独立的 elder 组件 + REQUIRES」，
# 组件是无法反向依赖 main 的。
CMAKE_OVERLAY = f"""{BEGIN}
# ---- elder 组件源码直接编入 main（bridge 需要访问 xiaozhi 的 C++ API）----
set(ELDER_DIR "${{PROJECT_DIR}}/components/elder")
target_sources(${{COMPONENT_LIB}} PRIVATE
    "${{ELDER_DIR}}/src/rgb.c"
    "${{ELDER_DIR}}/src/input.c"
    "${{ELDER_DIR}}/src/reminder.c"
    "${{ELDER_DIR}}/src/activation.c"
    "${{ELDER_DIR}}/src/ui.c"
    "${{ELDER_DIR}}/src/net.c"
    "${{ELDER_DIR}}/src/sync.c"
    "${{ELDER_DIR}}/src/elder_bridge.cc"
)
target_include_directories(${{COMPONENT_LIB}} PRIVATE "${{ELDER_DIR}}/include")

# ---- 在 app_main() 启动 xiaozhi 之后拉起 elder ----
{END}
"""

# 追加到 main/main.cc 的内容：在 app.Start() 之后调用 elder_bridge_start()。
# app.Start() 内部会创建主事件循环任务并立刻返回，因此这里延时 2s 再启动 elder，
# 等 Board/Display 初始化完成，避免拿到空指针。
MAIN_CC_CALL = """
    // >>> BEGIN elder-companion overlay (由 tools/merge_to_xiaozhi.py 注入，勿手改) >>>
    // 老人陪伴 & 提醒助手：等 xiaozhi 的 Board / Display / AudioService 就绪后拉起
    extern "C" void elder_bridge_start(void);
    xTaskCreatePinnedToCore([](void *arg) {
        vTaskDelay(pdMS_TO_TICKS(2000));
        elder_bridge_start();
        vTaskDelete(NULL);
    }, "elder_boot", 4096, NULL, 5, NULL, 1);
    // <<< END elder-companion overlay <<<
"""

# 追加到 sdkconfig.defaults：立创·实战派 + EAF 形象 + elder 后端地址
SDKCONFIG_OVERLAY = f"""{BEGIN}
# 目标板卡：立创·实战派 ESP32-S3
CONFIG_BOARD_TYPE_LICHUANG_DEV_S3=y
# 表情/形象走 EAF 序列帧（esp_emote_gfx），只有开启它 EmoteDisplay 才会被实例化
CONFIG_USE_EMOTE_MESSAGE_STYLE=y
# 与后端 TZ=Asia/Shanghai 对齐的提示音语言
CONFIG_LANGUAGE_ZH_CN=y
# 16MB Flash + Octal PSRAM（立创·实战派 N16R8）
CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y
CONFIG_SPIRAM=y
CONFIG_SPIRAM_MODE_OCT=y
CONFIG_SPIRAM_SPEED_80M=y
# 分区表：v2/16m.csv（含 8MB assets 分区，存放 EAF 形象包）
CONFIG_PARTITION_TABLE_CUSTOM=y
CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions/v2/16m.csv"
# 业务后端地址（按实际部署修改，也可 idf.py menuconfig 调整）
CONFIG_ELDER_SERVER_URL="http://192.168.1.100:8000"
CONFIG_ELDER_SERVER_TOKEN=""
CONFIG_ELDER_SYNC_INTERVAL_SEC=300
CONFIG_ELDER_HTTP_TIMEOUT_MS=8000
# 立创板无板载 WS2812；外接灯珠时才打开
CONFIG_ELDER_ENABLE_RGB=n
# 演示提醒数据（无后端联调时可临时打开）
CONFIG_ELDER_DEMO_DATA=n
# xiaozhi 的 OTA/WS 地址由后端 /api/device/sync 下发，此处保持默认即可
{END}
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

    # 3) main/main.cc 里插入 elder_bridge_start() 调用
    if MAIN_CC.is_file():
        text = MAIN_CC.read_text(encoding="utf-8")
        if "elder_bridge_start" not in text:
            marker = "    app.Start();\n"
            if marker in text:
                text = text.replace(marker, "    app.Start();\n" + MAIN_CC_CALL, 1)
                MAIN_CC.write_text(text, encoding="utf-8")
                print(f"[merge] 已注入启动调用: {MAIN_CC}")
            else:
                print(f"[merge] 警告：main.cc 中未找到 `app.Start();`，需手动接入",
                      file=sys.stderr)
        else:
            print(f"[merge] 已是最新: {MAIN_CC}")

    # 4) sdkconfig.defaults overlay
    if append_overlay(SDK_DEFAULTS, SDKCONFIG_OVERLAY):
        print(f"[merge] 已注入: {SDK_DEFAULTS}")
    else:
        print(f"[merge] 已是最新: {SDK_DEFAULTS}")

    print("\n[merge] 完成。下一步：")
    print("  python tools/idfwin.py set-target esp32s3   (需先 cd 到 firmware/xiaozhi)")
    print("  python tools/idfwin.py build")
    return 0


def clean() -> int:
    for p in (MAIN_CMAKE, SDK_DEFAULTS):
        if remove_overlay(p):
            print(f"[merge] 已还原: {p}")
    if MAIN_CC.is_file():
        text = MAIN_CC.read_text(encoding="utf-8")
        if "elder_bridge_start" in text:
            import re
            text = re.sub(r"\n *// >>> BEGIN elder-companion overlay.*?// <<< END elder-companion overlay <<<\n",
                          "", text, flags=re.S)
            MAIN_CC.write_text(text, encoding="utf-8")
            print(f"[merge] 已还原: {MAIN_CC}")
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
    print("[merge] 合并状态:", "已合并" if ok else "未合并/不完整")
    print(f"  components/elder : {'OK' if ELDER_DST.is_dir() else 'MISSING'}")
    print(f"  main/CMakeLists  : {'OK' if MAIN_CMAKE.is_file() and BEGIN in MAIN_CMAKE.read_text(encoding='utf-8') else 'MISSING'}")
    print(f"  main/main.cc     : {'OK' if MAIN_CC.is_file() and 'elder_bridge_start' in MAIN_CC.read_text(encoding='utf-8') else 'MISSING'}")
    print(f"  sdkconfig.defaults: {'OK' if SDK_DEFAULTS.is_file() and BEGIN in SDK_DEFAULTS.read_text(encoding='utf-8') else 'MISSING'}")
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
