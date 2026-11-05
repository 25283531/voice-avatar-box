#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kotlin 结构检查：抓「括号配平但层次错」的错误。

本机没有 Android SDK、编译不了 Kotlin，而 CI 跑一轮要 1.5 分钟。
第一次推送时我只查了「括号总数配平」就放行，结果：
  · CI run 41972423369 → `Thread.join` 返回 void ⇒ `!joined` 编译不过
  · CI run 41972940902 → synchronized 块的返回结构 ⇒ 编译不过
  · CI run 41973478969 → 我改 decode 时多留了一个 `}`，把 companion
    object 挤出了类体外 ⇒ TAG/encoder/decoder 全"未解析"
第三次那个多余的 `}`，**括号总数是配平的** —— 所以"数括号"这种检查
根本抓不住。

这个脚本做的是**层次**检查：按大括号深度判断每个顶层声明是否真的
落在类体（depth 1）里。

用法：python ktlint_struct.py [文件...]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

DEFAULT_FILES = [
    "lx04/app/src/main/java/com/elder/lx04/MainActivity.kt",
    "lx04/app/src/main/java/com/elder/lx04/audio/OpusCodec.kt",
    "lx04/app/src/main/java/com/elder/lx04/audio/AudioCapture.kt",
    "lx04/app/src/main/java/com/elder/lx04/audio/AudioPlayer.kt",
    "lx04/app/src/main/java/com/elder/lx04/audio/AudioPreProcess.kt",
    "lx04/app/src/main/java/com/elder/lx04/net/VoiceClient.kt",
    "lx04/app/src/test/java/com/elder/lx04/audio/OpusConcurrencyTest.kt",
    "lx04/app/src/test/java/com/elder/lx04/audio/OpusEncodeTest.kt",
]

# 顶层声明：类 / 对象 / 函数 / 属性
TOP_DECL = re.compile(
    r"^\s{0,4}(?:@\w+\s+)*(?:public\s+|private\s+|internal\s+|open\s+|abstract\s+)*"
    r"(?:class|object|interface|fun|val|var|companion\s+object)\b"
)


def strip_noise(src: str) -> list[tuple[int, str]]:
    """剥掉注释与字符串，返回 (行号, 剥离后的行)。"""
    out: list[tuple[int, str]] = []
    in_block = False
    in_str = False
    in_raw = False
    for i, line in enumerate(src.splitlines(), 1):
        buf = []
        j = 0
        while j < len(line):
            if in_block:
                if line.startswith("*/", j):
                    in_block = False
                    j += 2
                else:
                    j += 1
                continue
            if in_raw:
                buf.append(" ")
                if line.startswith('"""', j):
                    in_raw = False
                    j += 3
                else:
                    j += 1
                continue
            if in_str:
                buf.append(" ")
                if line[j] == "\\":
                    j += 2
                elif line[j] == '"':
                    in_str = False
                    j += 1
                else:
                    j += 1
                continue
            if line.startswith('"""', j):
                in_raw = True
                buf.append(" ")
                j += 3
                continue
            if line.startswith("//", j):
                break
            if line.startswith("/*", j):
                in_block = True
                j += 2
                continue
            if line[j] == '"':
                in_str = True
                buf.append(" ")
                j += 1
                continue
            buf.append(line[j])
            j += 1
        out.append((i, "".join(buf)))
    return out


def check(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    lines = strip_noise(src)
    problems: list[str] = []

    depth = 0
    max_depth = 0
    # 记录每个顶层声明的起始深度
    decl_depths: list[tuple[int, str, int]] = []
    for lineno, text in lines:
        stripped = text.strip()
        if depth == 0 and TOP_DECL.match(text):
            decl_depths.append((lineno, stripped[:50], depth))
        # 先算这一行的深度变化
        opens = text.count("{")
        closes = text.count("}")
        depth += opens - closes
        max_depth = max(max_depth, depth)
        if depth < 0:
            problems.append(f"{lineno}: 多余的右花括号（深度变成负数）")
            depth = 0
            break

    if depth != 0:
        problems.append(f"文件结束时深度为 {depth}（应为 0）—— 有未闭合的左花括号")

    # companion object 必须在类体里（depth 1），不能在类外（depth 0）
    for lineno, text, d in decl_depths:
        if text.startswith("companion object") and d != 1:
            problems.append(
                f"{lineno}: 'companion object' 在深度 {d} —— "
                f"必须在类体内（深度 1）。多半是前面多了一个 '}}'，"
                f"把 companion object 挤出了类体"
            )

    # 每个文件至少要有一个 class 在深度 0
    if not any(t.startswith(("class ", "object ")) and d == 0 for _, t, d in decl_depths):
        problems.append("顶层没有找到 class/object 声明 —— 文件结构可能坏了")

    return problems


def main() -> int:
    files = [Path(a) for a in sys.argv[1:]] or [Path(f) for f in DEFAULT_FILES]
    bad = 0
    for f in files:
        if not f.is_file():
            print(f"[SKIP] {f} 不存在")
            continue
        probs = check(f)
        if probs:
            bad += 1
            print(f"[BAD] {f}")
            for p in probs:
                print(f"      {p}")
        else:
            print(f"[ OK ] {f}")
    print()
    if bad:
        print(f"{bad} 个文件结构有问题")
        return 1
    print("Kotlin 结构检查通过（层次层面）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
