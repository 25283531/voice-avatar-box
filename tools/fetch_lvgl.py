#!/usr/bin/env python3
"""把锁定版本的 LVGL 取到 third_party/lvgl，供 idf_component.yml 的 override_path 使用。

## 为什么需要这个脚本（2026-10-23 白屏事故）

`main/idf_component.yml` 里 lvgl 的约束写的是 `==9.3.0`，语义上只匹配 9.3.0
（本机 idf-component-manager 2.5.2 实测：==9.3.0 / ~9.3.0 / 9.3.0 都不匹配
9.6.0~1，只有 ^9.3.0 和 >=8,<10 才匹配）。但 CI 容器里的组件管理器在
"Manifest files have changed, solving dependencies." 触发的**重新求解**路径上
把这条约束丢掉了，只剩 `espressif/esp_lvgl_port` 要求的 `>=8,<10`，于是每次都
解析到注册表最新版 9.6.0~1 并覆盖已落盘的 9.3.0（CI run 39910735989 /
39911711545 两次复现，`==9.3.0` 也没能拦住）。

9.6 的 lv_font_t 是 44 字节且字段重排，而 78/xiaozhi-fonts 的 cbin 字库是按
9.3 布局（36 字节）生成的二进制镜像，cbin_font_create 直接 malloc_cpy 盲拷
⇒ dsc 读成空指针、line_height 读成 36 ⇒ 字形零命中 ⇒ 白屏/字幕消失/图片不显示。
而且 lv_font_fmt_txt_dsc_t 在 9.3/9.6 之间也变了，glyph_dsc/cmaps 数组是 LVGL
自己按编译期结构体读的 —— 从我们这边"兼容 9.6"做不到，**编译期必须就是 9.3.0**。

## 做法

用官方支持的 `override_path`：依赖指向本地目录时，组件管理器走 LocalSource，
**根本不去注册表**，也就不存在被求解器抬版本的可能。

本脚本负责把那个本地目录准备好，并且用**内容**（不是压缩包哈希）校验：
`idf_component.yml` 的 version、`lv_version.h` 的三个宏、以及"没有
include/lvgl/ 这一层"。三者都对才算取成功。

用法:
    python tools/fetch_lvgl.py           # 缺了或不对才下载，幂等
    python tools/fetch_lvgl.py --force   # 强制重下
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# 唯一真源：改版本必须同时改 main/idf_component.yml 的 override 注释与本文件
LVGL_VERSION = "9.3.0"
LVGL_MAJOR, LVGL_MINOR, LVGL_PATCH = 9, 3, 0

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "third_party" / "lvgl"
URL = ("https://components-file.espressif.com/components/lvgl/lvgl/"
       f"{LVGL_VERSION}/lvgl__lvgl-v{LVGL_VERSION}.zip")


def log(msg: str) -> None:
    print(f"[lvgl] {msg}", flush=True)


def read_version_ok(d: Path) -> tuple[bool, str]:
    """校验目录里的 LVGL 是不是我们要的那个版本。返回 (是否合格, 说明)。"""
    yml = d / "idf_component.yml"
    ver_h = d / "lv_version.h"
    if not yml.is_file() or not ver_h.is_file():
        return False, "缺 idf_component.yml 或 lv_version.h"
    if not (d / "CMakeLists.txt").is_file():
        return False, "缺 CMakeLists.txt（不是组件目录）"

    declared = ""
    for line in yml.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("version:"):
            declared = line.split(":", 1)[1].strip().strip("'\"")
            break

    macros: dict[str, str] = {}
    for line in ver_h.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#define LVGL_VERSION_"):
            parts = line.split()
            if len(parts) >= 3:
                macros[parts[1]] = parts[2]
    want = {"LVGL_VERSION_MAJOR": str(LVGL_MAJOR),
            "LVGL_VERSION_MINOR": str(LVGL_MINOR),
            "LVGL_VERSION_PATCH": str(LVGL_PATCH)}
    for k, v in want.items():
        if macros.get(k) != v:
            return False, f"{k}={macros.get(k)!r}，期望 {v!r}"

    if (d / "include" / "lvgl" / "lvgl.h").exists():
        return False, "存在 include/lvgl/lvgl.h（9.6+ 的目录形态）"

    if declared != LVGL_VERSION:
        return False, f"idf_component.yml version={declared!r}，期望 {LVGL_VERSION!r}"

    return True, f"version={declared}, lv_version.h={LVGL_MAJOR}.{LVGL_MINOR}.{LVGL_PATCH}"


def download(url: str, dst: Path) -> None:
    log(f"下载 {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "voice-avatar-box/fetch_lvgl"})
    with urllib.request.urlopen(req, timeout=180) as r, dst.open("wb") as f:
        total = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
    log(f"已下载 {total:,} 字节")


def extract_flat(zip_path: Path, out: Path) -> None:
    """注册表的包是平铺的（文件直接在根），也兼容有单层根目录的包。"""
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(out)
    entries = list(out.iterdir())
    if len(entries) == 1 and entries[0].is_dir() and (entries[0] / "CMakeLists.txt").is_file():
        inner = entries[0]
        log(f"包内有单层根目录 {inner.name}/，上移一层")
        for item in inner.iterdir():
            shutil.move(str(item), str(out / item.name))
        inner.rmdir()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="即使已就绪也重下")
    args = ap.parse_args()

    if DEST.is_dir() and not args.force:
        ok, why = read_version_ok(DEST)
        if ok:
            log(f"{DEST.relative_to(ROOT)} 已就绪（{why}），跳过下载")
            return 0
        log(f"现有目录不合格（{why}），重新获取")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        zip_path = tmp / "lvgl.zip"
        stage = tmp / "stage"
        stage.mkdir()
        download(URL, zip_path)
        extract_flat(zip_path, stage)
        ok, why = read_version_ok(stage)
        if not ok:
            log(f"!! 下载到的内容不合格：{why}")
            log("!! 不覆盖现有目录，请检查注册表或 URL")
            return 2
        log(f"内容校验通过（{why}）")
        if DEST.exists():
            shutil.rmtree(DEST)
        DEST.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stage), str(DEST))

    ok, why = read_version_ok(DEST)
    if not ok:
        log(f"!! 落盘后校验失败：{why}")
        return 3
    n = sum(1 for _ in DEST.rglob("*") if _.is_file())
    log(f"完成：{DEST.relative_to(ROOT)}（{n:,} 个文件，{why}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
