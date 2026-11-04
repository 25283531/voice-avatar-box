#!/usr/bin/env python3
"""生成 LX04 客户端的启动图标（各密度 mipmap PNG）。

为什么要有这个脚本
------------------
图标原本想在 CI 里用 heredoc 现写现跑，结果 heredoc 的结束标记 `PY`
在 YAML 的 `run: |` 块里被加上了缩进，bash 匹配不到，直接
`syntax error: unexpected end of file`（见 run 41804175633）。
把逻辑挪到这个文件里，CI 只需 `python3 lx04/scripts/gen_icons.py`，
既躲开缩进坑，也能在本地复现同一份图标。

为什么不用 Pillow
-----------------
CI 镜像和本机都不保证装 Pillow，而这件事实在不需要它 ——
纯 `zlib` + `struct` 手写 PNG 只有几十行，且结果确定（同一输入
永远产出同一字节），不会让每次构建的图标悄悄变样。

设计
----
深蓝竖向渐变底 + 白色圆环 + 中心实心点，取「提醒 / 播报」的意象。
尺寸按 Android 密度档位给全，避免系统放大导致发虚。

用法
----
    python3 lx04/scripts/gen_icons.py            # 生成到默认位置
    python3 lx04/scripts/gen_icons.py --out DIR  # 指定 res 目录
"""

from __future__ import annotations

import argparse
import math
import struct
import zlib
from pathlib import Path

# 各密度档位的边长（px）。数值取 Android 官方 launcher icon 基准尺寸。
DENSITIES: list[tuple[str, int]] = [
    ("mdpi", 48),
    ("hdpi", 72),
    ("xhdpi", 96),
    ("xxhdpi", 144),
    ("xxxhdpi", 192),
]

# 背景竖向渐变：上浅下深。蓝色系贴近「陪伴 / 信任」，也不用担心
# 在浅色桌面壁纸上糊成一片。
BG_TOP = (30, 136, 229)
BG_BOTTOM = (13, 71, 161)

WHITE = (255, 255, 255)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    """拼一个 PNG chunk：长度 + 类型 + 数据 + CRC32。"""
    body = tag + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def _render_pixels(size: int) -> bytes:
    """按行渲染成 PNG 的原始扫描线（每行前置 filter byte 0）。"""
    center = (size - 1) / 2.0
    r_outer = size * 0.30
    r_inner = size * 0.225
    r_dot = size * 0.085

    # 渐变按行算一次即可，不必逐像素重复插值。
    rows: list[bytes] = []
    denom = max(1, size - 1)
    for y in range(size):
        t = y / denom
        bg = tuple(
            int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)
        )
        line = bytearray([0])  # filter type 0 (None)
        dy = y - center
        for x in range(size):
            dx = x - center
            d = math.hypot(dx, dy)
            if r_inner <= d <= r_outer or d <= r_dot:
                line += bytes(WHITE)
            else:
                line += bytes(bg)
        rows.append(bytes(line))
    return b"".join(rows)


def write_icon(path: Path, size: int) -> None:
    """写一张 size×size 的 24 位真彩 PNG。"""
    raw = _render_pixels(size)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def main() -> int:
    here = Path(__file__).resolve()
    # 默认输出到 lx04/app/src/main/res（脚本在 lx04/scripts/ 下）
    default_res = here.parent.parent / "app" / "src" / "main" / "res"

    ap = argparse.ArgumentParser(description="生成 LX04 启动图标")
    ap.add_argument(
        "--out",
        type=Path,
        default=default_res,
        help=f"res 目录（默认 {default_res}）",
    )
    args = ap.parse_args()

    for density, size in DENSITIES:
        out = args.out / f"mipmap-{density}" / "ic_launcher.png"
        write_icon(out, size)
        print(f"  {out.relative_to(args.out.parent.parent.parent.parent)}  {size}x{size}")

    print(f"完成：{len(DENSITIES)} 个密度档位 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
