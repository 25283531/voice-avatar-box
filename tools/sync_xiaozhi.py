#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_xiaozhi.py —— 把 xiaozhi-esp32 上游源码「瘦身」同步进本项目仓库。

⚠️⚠️ 2026-10-19 起：**默认冻结，不做上游同步**（用户拍板）
---------------------------------------------------------------
本项目在 `firmware/xiaozhi/` 上做了**直接改上游文件**的定制（不是全部走
overlay 注入），例如：

  * `main/display/lcd_display.cc` —— 对话界面 70/30 布局（6 处几何改造）
  * `main/main.cc` —— elder_boot / 绑定引导任务
  * `main/boards/{bread-compact-esp32,bread-compact-esp32-lcd,esp32-cgc-144,esp32-cgc}/*.cc`
                     —— 唤醒词改宏引用
  * `main/boards/common/wifi_board.cc` —— 唤醒词模型注释
  * `sdkconfig.defaults` —— 唤醒词段落去重

一次 `sync --force` / `sync --prune` 就会把上面全部**静默覆盖**（内容比对
只认「与上游一致」，不认识「本地已定制」）。而这些覆盖**不会报错**，只会
表现为「布局悄悄回退 / 唤醒词又变回你好小智」，排查成本极高。

所以本脚本的默认行为现在是**拒绝同步**并打印版本锁。要同步必须显式
`--unlock`（且需同时给出目标 commit），每次都会打印告警。

**这不代表放弃上游**：将来真要升级，流程见本文档「解冻流程」——
先 `--unlock`，同步后重跑 `merge_to_xiaozhi.py`，再按 README
「上游定制补丁」节把直接改的文件重新贴回去（`merge --check` 会红着提醒）。

设计目标
--------
上游整仓 567MB（含 .git 与 509MB 的 managed_components），直接搬进自己的
Git 仓库既不现实也没必要。本脚本只搬运**编译所需源码**，产物约 10MB，
可由 IDF 组件管理器根据 dependencies.lock 自动补齐第三方依赖。

用法
----
    # 0) 默认：一跑就拒绝同步，只报当前版本锁状态
    python tools/sync_xiaozhi.py

    # 1) 预览：统计文件数/体积、列出被排除的超大文件 Top20（不写盘）
    python tools/sync_xiaozhi.py --dry-run

    # 2) 正式同步（**需先 --unlock**）：幂等，内容相同则跳过拷贝
    python tools/sync_xiaozhi.py --unlock

    # 3) 强制覆盖（忽略内容比对，全部重写）
    python tools/sync_xiaozhi.py --unlock --force

    # 指定源/目标
    python tools/sync_xiaozhi.py --src D:/espdev/src/xiaozhi-esp32 --dst firmware/xiaozhi

过滤规则见 EXCLUDE_DIRS / EXCLUDE_SUFFIXES / MAX_FILE_SIZE 常量。
"""
from __future__ import annotations

import argparse
import filecmp
import hashlib
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------- 配置
DEFAULT_SRC = Path(r"D:\espdev\src\xiaozhi-esp32")
DEFAULT_DST = Path(r"G:\workbuddy\语音助手\firmware\xiaozhi")

UPSTREAM_URL = "https://github.com/78/xiaozhi-esp32"

# ---------------------------------------------------------------- 版本锁
#: 冻结的上游版本（2026-10-19 用户拍板）。
#:
#: 改成别的值**不会**自动解冻 —— 解冻必须走 ``--unlock``，避免有人为了
#: 「让 CI 过」随手改常量，等于把保护机制自己拆了。
PINNED_TAG = "v2.0.5"
PINNED_COMMIT = "860d12a12c7df9e933ff710b6f9061bf58d61ab2"
PINNED_ON = "2026-10-19"
PINNED_BY = "用户拍板（老人陪伴助手：直接改上游文件的定制不可再被覆盖）"

#: 解冻时必须显式确认的短语。看到这行就说明脚本在要求人明确表态，
#: 而不是让一个手滑的 ``--force`` 悄悄覆盖掉一周的定制工作。
UNLOCK_PHRASE = "I-ACCEPT-OVERWRITE-CUSTOM-PATCHES"

# 整目录排除：这些目录不需要进仓库
#   .git               上游版本库（567MB 的绝大部分）
#   build              构建产物
#   managed_components IDF 组件管理器自动拉取的第三方依赖（509MB）
#   docs               文档
#   .github            CI 工作流
EXCLUDE_DIRS = {
    ".git",
    "build",
    "managed_components",
    "docs",
    ".github",
    "__pycache__",
    ".vscode",
    ".idea",
}

# scripts/ 下与固件编译无关的「工具仓库」子目录（内含二进制/示例工程）
EXCLUDE_SCRIPT_SUBDIRS = {
    "Image_Converter",   # 图片转 EAF 的 PC 端工具（含可执行示例）
    "ogg_converter",     # 音频转换工具（含二进制）
    "acoustic_check",    # 声学测试工程
    "p3_tools",          # P3 音频格式工具
}

# 按后缀排除的大体积资源。
# 注意：main/assets/**/*.ogg 是开机提示音 / 激活提示音，被 assets.cc 通过
# EMBED_FILES 编进固件，**必须保留**。这里只排除真正与编译无关的素材。
EXCLUDE_SUFFIXES = {
    ".mp3", ".wav", ".flac", ".aac",
    ".png", ".jpg", ".jpeg", ".bmp", ".gif",
    ".mp4", ".avi", ".mov",
    ".zip", ".7z", ".rar", ".gz", ".tar",
    ".exe", ".dll", ".so", ".dylib",
    ".pdf",
    ".psd", ".ai",
}

# 单文件超过该体积直接排除（并进入 dry-run 报告）
MAX_FILE_SIZE = 8 * 1024 * 1024  # 8MB

# 顶层明确需要的文件（其余顶层文件如 sdkconfig 属构建产物，跳过）
ROOT_FILES = {
    "CMakeLists.txt",
    "LICENSE",
    "README.md",
    "README_zh.md",
    "README_ja.md",
    ".gitignore",
    "dependencies.lock",
    "sdkconfig.defaults",
    "sdkconfig.defaults.esp32",
    "sdkconfig.defaults.esp32c3",
    "sdkconfig.defaults.esp32c5",
    "sdkconfig.defaults.esp32c6",
    "sdkconfig.defaults.esp32p4",
    "sdkconfig.defaults.esp32s3",
}

# 顶层需要整棵搬运的目录
ROOT_DIRS = {
    "main",
    "partitions",
    "scripts",
}


# ---------------------------------------------------------------- 版本锁
def frozen_banner(src: Path) -> None:
    """打印当前冻结状态 + 被保护的自定义清单。"""
    print("=" * 70)
    print(f"⛔ 上游同步已冻结（{PINNED_ON} 由{PINNED_BY}）")
    print("=" * 70)
    print(f"  固定版本 : {PINNED_TAG}  commit={PINNED_COMMIT[:12]}")
    print(f"  本地源   : {src}")
    try:
        import subprocess

        out = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            cwd=str(src), capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(src), capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        if out:
            same = "一致" if sha.startswith(PINNED_COMMIT[:12]) else "⚠️ 与锁定版本不一致"
            print(f"  上游仓库 : {out}  {sha[:12]}  （{same}）")
            if not same.startswith("一致"):
                print("            ⚠️ 即使解锁，同步的也是**另一个版本**，"
                      "务必先 git checkout 到锁定 tag 再同步。")
    except Exception:
        pass

    print("\n  受保护的自定义改动（直接改上游文件，sync 会静默覆盖）：")
    for f in CUSTOM_PATCH_FILES:
        print(f"    - {f}")
    print("\n  要同步必须显式解锁：")
    print(f"    python tools/sync_xiaozhi.py --unlock --confirm '{UNLOCK_PHRASE}'")
    print("=" * 70)


#: 直接改上游文件的清单（与 README「上游定制补丁」节、决策记录 D24 对齐）。
#: sync 会覆盖它们，所以冻结保护的就是这一组。
CUSTOM_PATCH_FILES = (
    "main/display/lcd_display.cc            （70/30 对话布局，标记 elder-layout-70-30）",
    "main/main.cc                            （elder_boot + 绑定引导任务）",
    "main/boards/bread-compact-esp32/esp32_bread_board.cc",
    "main/boards/bread-compact-esp32-lcd/esp32_bread_board_lcd.cc",
    "main/boards/esp32-cgc-144/esp32_cgc_144_board.cc",
    "main/boards/esp32-cgc/esp32_cgc_board.cc",
    "main/boards/common/wifi_board.cc",
    "sdkconfig.defaults                      （唤醒词段去重）",
)


def require_unlock(args: argparse.Namespace, src: Path) -> bool:
    """同步前的冻结门禁。返回 True 表示可以继续同步。

    三道门，缺一不可：
      1. ``--unlock`` 开关存在；
      2. ``--confirm`` 短语逐字匹配（防止 ``--unlock`` 被 CI 模板顺手带上）；
      3. 二次确认（交互式），非交互环境（CI/管道）直接拒绝。
    """
    if not getattr(args, "unlock", False):
        frozen_banner(src)
        print("\n[sync] 已拒绝：加 --unlock 才会真正写盘。")
        return False

    if args.confirm != UNLOCK_PHRASE:
        print(f"[sync] --unlock 需要配 --confirm '{UNLOCK_PHRASE}'（逐字匹配）。",
              file=sys.stderr)
        return False

    if not sys.stdin.isatty():
        print("[sync] 非交互环境不做二次确认：解冻必须由人在终端里亲手敲。",
              file=sys.stderr)
        return False

    print("\n⚠️⚠️ 即将用上游源码覆盖 firmware/xiaozhi/ 下的自定义改动。")
    print("   下列文件会被重写，随后必须按 README「上游定制补丁」节重贴：")
    for f in CUSTOM_PATCH_FILES:
        print(f"     - {f}")
    print("\n   输入 sync-xiaozhi 确认（其它任何输入都取消）：", end="", flush=True)
    try:
        answer = input().strip()
    except EOFError:
        answer = ""
    if answer != "sync-xiaozhi":
        print("\n[sync] 已取消。")
        return False
    print("[sync] 已确认解锁，继续同步。\n")
    return True


# ---------------------------------------------------------------- 扫描
def rel_posix(p: Path, base: Path) -> str:
    return p.relative_to(base).as_posix()


def is_excluded(path: Path, src: Path) -> tuple[bool, str]:
    """返回 (是否排除, 原因)。"""
    rel = rel_posix(path, src)
    parts = rel.split("/")

    for d in EXCLUDE_DIRS:
        if d in parts:
            return True, f"目录 {d}"

    # scripts 下的特定子目录
    if parts[0] == "scripts" and len(parts) > 1 and parts[1] in EXCLUDE_SCRIPT_SUBDIRS:
        return True, f"scripts/{parts[1]} 工具子目录"

    if path.is_file():
        try:
            size = path.stat().st_size
        except OSError:
            return True, "无法读取"
        if size > MAX_FILE_SIZE:
            return True, f"超大文件 {size / 1048576:.1f}MB"
        if path.suffix.lower() in EXCLUDE_SUFFIXES:
            return True, f"后缀 {path.suffix}"

    return False, ""


def scan(src: Path) -> tuple[list[Path], list[tuple[Path, int, str]]]:
    """扫描源目录，返回 (保留文件列表, 排除文件列表[(路径, 大小, 原因)])。"""
    keep: list[Path] = []
    drop: list[tuple[Path, int, str]] = []

    for entry in sorted(src.iterdir()):
        if entry.is_dir():
            if entry.name in EXCLUDE_DIRS:
                drop.append((entry, dir_size(entry), f"目录 {entry.name}"))
                continue
            if entry.name not in ROOT_DIRS:
                drop.append((entry, dir_size(entry), "未列入同步清单的顶层目录"))
                continue
            for f in sorted(entry.rglob("*")):
                if f.is_dir():
                    continue
                ok, why = is_excluded(f, src)
                (keep if ok is False else drop).append(
                    f if ok is False else (f, f.stat().st_size, why)
                )
        elif entry.is_file():
            if entry.name in ROOT_FILES:
                keep.append(entry)
            else:
                drop.append((entry, entry.stat().st_size, "未列入同步清单的顶层文件"))

    return keep, drop


def dir_size(d: Path) -> int:
    total = 0
    for f in d.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.1f}GB"


# ---------------------------------------------------------------- 同步
def sync(keep: list[Path], src: Path, dst: Path, force: bool) -> dict:
    stats = {"copied": 0, "skipped": 0, "bytes": 0}
    for f in keep:
        rel = rel_posix(f, src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not force:
            # 内容比对：大小一致 + SHA256 一致则跳过，保证幂等
            if target.stat().st_size == f.stat().st_size and sha256_of(target) == sha256_of(f):
                stats["skipped"] += 1
                continue
        shutil.copy2(f, target)
        stats["copied"] += 1
        stats["bytes"] += f.stat().st_size
    return stats


def prune(keep: list[Path], src: Path, dst: Path) -> int:
    """删除目标目录中已不在同步清单内的文件（保持与上游一致）。"""
    keep_set = {rel_posix(f, src) for f in keep}
    removed = 0
    if not dst.is_dir():
        return 0
    for f in sorted(dst.rglob("*")):
        if not f.is_file():
            continue
        rel = rel_posix(f, dst)
        if rel == "VENDOR.md":
            continue
        if rel not in keep_set:
            f.unlink()
            removed += 1
    # 清理空目录
    for d in sorted(dst.rglob("*"), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    return removed


# ---------------------------------------------------------------- 报告
def write_vendor_md(dst: Path, keep: list[Path], total: int, src: Path) -> None:
    import datetime
    import subprocess

    def git(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", *args], cwd=str(src), capture_output=True, text=True, timeout=20
            )
            return out.stdout.strip()
        except Exception:
            return ""

    tag = git("describe", "--tags", "--always")
    commit = git("rev-parse", "HEAD")
    date = datetime.date.today().isoformat()

    content = f"""# VENDOR — xiaozhi-esp32 上游源码同步说明

本目录是 [xiaozhi-esp32]({UPSTREAM_URL}) 上游源码的**瘦身镜像**（vendor 方式，非 submodule），
用于「老人陪伴 & 提醒助手」项目的 ESP32-S3 固件基线。

| 项目 | 值 |
| --- | --- |
| 上游仓库 | {UPSTREAM_URL} |
| 上游 tag | **{tag or 'v2.0.5'}** |
| 上游 commit | `{commit or '860d12a'}` |
| 同步日期 | {date} |
| 同步文件数 | {len(keep)} |
| 同步体积 | {human(total)} |

## 为什么不是整棵搬
上游整仓约 567MB，其中：
- `.git/` 上游历史
- `managed_components/` 约 509MB，是 IDF 组件管理器按 `dependencies.lock`
  自动拉取的第三方依赖（LVGL / esp_emote_gfx / esp-sr / codec 等）
- `build/` 约 43MB，构建产物

这些都**不进入本仓库**：`managed_components` 在首次 `idf.py build` 时由组件管理器
依据 `main/idf_component.yml` + 根目录 `dependencies.lock` 自动下载还原。

## 过滤规则
同步脚本：`tools/sync_xiaozhi.py`

保留：
- 根目录：`CMakeLists.txt`、`LICENSE`、`README*.md`、`.gitignore`、
  `dependencies.lock`、`sdkconfig.defaults*`
- `main/`（固件源码、板卡目录、assets 提示音）
- `partitions/`（分区表 CSV）
- `scripts/`（构建相关脚本，剔除其中的 PC 端工具子目录）

排除：
- 目录：`.git/`、`build/`、`managed_components/`、`docs/`、`.github/`
- `scripts/` 子目录：`Image_Converter/`、`ogg_converter/`、`acoustic_check/`、`p3_tools/`
- 后缀：`.mp3 .wav .png .jpg .gif .mp4 .zip .exe .pdf …`（大体积素材）
- 单文件 > {MAX_FILE_SIZE // 1048576}MB

### 关于音频资源
`main/assets/**/*.ogg` **全部保留**：它们是开机提示音、激活提示音、配网提示音，
由 `main/CMakeLists.txt` 的 `EMBED_FILES`/`build_default_assets.py` 编进固件。
按语言分目录存放，编译时只取 `CONFIG_LANGUAGE_DIR` 指定的一种，单文件均 < 20KB。

## 如何更新
```bash
# 1) 在上游仓库切到目标 tag
cd D:/espdev/src/xiaozhi-esp32 && git checkout v2.0.5

# 2) 预览差异（不写盘）
cd G:/workbuddy/语音助手 && python tools/sync_xiaozhi.py --dry-run

# 3) 同步（幂等，重复执行只做增量）
cd G:/workbuddy/语音助手 && python tools/sync_xiaozhi.py

# 4) 更新本文件的 tag / commit / 日期字段后提交
```

若上游升级了大版本（更换 `main/idf_component.yml` 依赖），需清空
`firmware/xiaozhi/build/managed_components` 让组件管理器重新解析。

## 与本项目自研代码的关系
- 自研组件在 `firmware/components/elder/`，通过 `tools/merge_to_xiaozhi.py`
  拷贝进 `firmware/xiaozhi/components/elder/` 后一起编译。
- 上游代码**只读**，任何业务修改都应放在 `components/elder/` 或 `main/` 的
  独立文件中，不要直接改上游文件。
"""
    (dst / "VENDOR.md").write_text(content, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="同步 xiaozhi-esp32 源码到本项目（⚠️ 默认冻结，需 --unlock）")
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--dst", type=Path, default=DEFAULT_DST)
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写盘")
    ap.add_argument("--force", action="store_true", help="忽略内容比对强制覆盖")
    ap.add_argument("--prune", action="store_true", help="删除目标中已不在清单的文件")
    ap.add_argument("--unlock", action="store_true",
                    help=f"解除版本冻结（还需 --confirm '{UNLOCK_PHRASE}' 与交互确认）")
    ap.add_argument("--confirm", default="", help="解冻确认短语（逐字匹配）")
    args = ap.parse_args()

    src, dst = args.src, args.dst
    if not src.is_dir():
        print(f"[sync] 源目录不存在: {src}", file=sys.stderr)
        return 1

    print(f"[sync] 源 : {src}")
    print(f"[sync] 目标: {dst}")
    sys.stdout.flush()

    # ---- 冻结门禁：--dry-run 只读，放行；任何写盘动作都要先解锁 ----
    if not args.dry_run and not require_unlock(args, src):
        return 3

    keep, drop = scan(src)

    total = sum(f.stat().st_size for f in keep)
    print(f"\n=== 保留 {len(keep)} 个文件, 共 {human(total)} ===")

    files = [d for d in drop if d[0].is_file()]
    dirs = [d for d in drop if d[0].is_dir()]
    drop_bytes = sum(s for _, s, _ in files) + sum(s for _, s, _ in dirs)
    print(f"=== 排除 {len(files)} 个文件 + {len(dirs)} 个目录, 共 {human(drop_bytes)} ===")

    print("\n--- 被排除的超 1MB 文件 Top 20 ---")
    big = sorted(files, key=lambda x: x[1], reverse=True)[:20]
    if not big:
        print("(无)")
    for i, (p, s, why) in enumerate(big, 1):
        print(f"{i:2d}. {human(s):>8}  {rel_posix(p, src)}   [{why}]")

    print("\n--- 被排除的目录 ---")
    for d, s, why in dirs:
        print(f"    {human(s):>8}  {rel_posix(d, src)}/   [{why}]")

    print("\n--- 保留文件按顶层目录汇总 ---")
    agg: dict[str, list[int]] = {}
    for f in keep:
        top = rel_posix(f, src).split("/")[0]
        a = agg.setdefault(top, [0, 0])
        a[0] += 1
        a[1] += f.stat().st_size
    for k in sorted(agg, key=lambda k: -agg[k][1]):
        n, b = agg[k]
        print(f"    {human(b):>8}  {n:5d} 文件  {k}")

    if args.dry_run:
        print("\n[dry-run] 未写入任何文件。")
        return 0

    print("\n--- 同步中 ---")
    stats = sync(keep, src, dst, args.force)
    print(f"    新拷贝/更新 : {stats['copied']} ({human(stats['bytes'])})")
    print(f"    内容相同跳过: {stats['skipped']}")

    if args.prune:
        n = prune(keep, src, dst)
        print(f"    清理失效文件: {n}")

    write_vendor_md(dst, keep, total, src)
    print(f"\n[sync] 完成 → {dst}")
    print(f"[sync] VENDOR.md 已写入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
