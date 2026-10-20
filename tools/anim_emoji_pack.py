#!/usr/bin/env python3
"""把 Noto Animated Emoji 的 GIF 打包成 LVGL 可用的动画帧序列。

背景
----
设备上的表情一直是**静态**的：`78/xiaozhi-fonts` 里的 twemoji_64 是一堆
64x64 PNG 编译成 `lv_image_dsc_t` 数组，屏上永远只有一张不动的脸。

要让它动起来有三条路，选型理由写在下面（别改成另外两条）：

  1. Lottie / lottiefiles.com 的 .json —— **不可行**
     需要 rlottie 或 thorvg 运行时：flash +1MB 上下，每帧软件光栅化，
     PSRAM 大块缓冲。本机 8MB PSRAM 虽然够，但 CPU 会和 AFE 唤醒管线抢核，
     而唤醒本来就在丢帧（见 afe_wake_word.cc 里优先级的注释）。
  2. GIF 原地解码（LVGL gifdec）—— **不划算**
     每帧都要 LZW 解压 + 调色板转 RGB565，CPU 峰值高；LVGL 9 默认还没开。
  3. 帧序列 + lv_animimg —— **选它**
     播放只是「换一张已经解好的位图」，CPU 几乎为零。
     代价是 flash，但恰好是这台设备最富余的资源（见下面的预算）。

素材
----
`fonts.gstatic.com/s/e/notoemoji/latest/<codepoint>/512.gif`：Google 官方
Noto Animated Emoji，真实可达（实测 200，512x512，30ms/帧）。
许可 CC BY 4.0，可商用，署名写在 anim_emoji_index.h 里。
lottiefiles.com 上的动图要登录才能下载、且导出的是 .json，只能走方案 1，放弃。

体积预算（实测，见 --report）
---------------------------
64x64 RGB565A8 = 12,288 B/帧。8 帧 = 98,304 B/动画。
app 分区 4.1MB，当前固件 2.87MB ⇒ 余约 1.2MB，够放 10 个左右。
⚠️ 别直接塞进 .rodata 的 C 源码：同一个二进制写成 "0x12," 文本会膨胀 6 倍，
   git 里躺着 3MB 源码、每次编译都要重解析。这里出 .bin，由 CMake 的
   target_add_binary_data 嵌进固件。

用法
----
  # 只出体积报表，不生成文件（先看预算）
  python tools/anim_emoji_pack.py --report

  # 生成打包产物
  python tools/anim_emoji_pack.py --out firmware/xiaozhi/main/display/lvgl_display/anim
"""

import argparse
import io
import json
import os
import sys
import urllib.request

CDN = "https://fonts.gstatic.com/s/e/notoemoji/latest/{cp}/512.gif"

# 缺省表情集：偏「老人陪伴」语义 —— 招呼、赞许、关心、困倦、开心、想念。
# 名字要能在 LCD 的 emoji 轮播里当 key 用，所以用英文小写、无空格。
#
# 为什么是这 6 个而不是 Noto 全量：app 分区只有 4.1MB，当前固件 2.87MB，
# 64x64 RGB565A8 每个动画 96KB（8 帧）。6 个 = 576KB ⇒ 3.45MB，余 650KB。
# 再往上加会把 OTA 余量压到 300KB 以下，以后任何功能都塞不进去。
# 想扩充就改这张表，然后重跑本脚本 + 重编（索引表是编译期常量）。
DEFAULT_SET = [
    ("1f44b", "wave"),        # 👋 挥手打招呼
    ("1f642", "smile"),       # 🙂 微笑
    ("1f60d", "heart_eyes"),  # 😍 爱心眼 / 高兴
    ("1f44d", "thumbs_up"),   # 👍 点赞 / 好的
    ("2764", "heart"),        # ❤️ 爱心 / 想念
    ("1f634", "sleepy"),      # 😴 困了
]

# 备选（需要时挪进 DEFAULT_SET 即可，均已实测 CDN 可达）：
#   1f31e sun 🌞 早上好 / 天气      1f431 cat 🐱 猫
#   1f43b bear 🐻 熊               1f914 thinking 🤔 思考
#   1f602 funny 😂 笑              1f614 sad 😔 难过
#   1f436 与 1f430 在 CDN 上是 404（Noto 没做这两个的动画版）


def fetch_gif(cp, cache_dir):
    """下载并缓存原始 GIF（512x512，几百 KB）。离线时用缓存。"""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{cp}.gif")
    if os.path.exists(path) and os.path.getsize(path) > 1024:
        return path
    url = CDN.format(cp=cp)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
    except Exception as e:
        raise SystemExit(f"下载失败 {url}: {e}")
    if len(data) < 1024:
        raise SystemExit(f"下载内容过小，疑似 404：{url}（{len(data)} B）")
    with open(path, "wb") as f:
        f.write(data)
    return path


def load_frames(path, want, size):
    """均匀抽 want 帧，缩放到 size x size 的 RGBA。

    均匀取样而不是取前 N 帧：Noto 的 GIF 常常开头 10 帧都是同一个姿态，
    只取前 N 帧会得到一个几乎不动的动画。
    """
    from PIL import Image, ImageSequence

    im = Image.open(path)
    frames = []
    for f in ImageSequence.Iterator(im):
        frames.append(f.convert("RGBA"))
    if not frames:
        raise SystemExit(f"{path} 里没有帧")

    total = len(frames)
    if want >= total:
        picked = frames
    else:
        # 端点包含在内，首尾帧都保留，循环才接得上
        step = (total - 1) / (want - 1) if want > 1 else 0
        picked = [frames[round(i * step)] for i in range(want)]

    out = []
    for f in picked:
        if f.size != (size, size):
            f = f.resize((size, size), Image.LANCZOS)
        out.append(f)
    return out


def to_rgb565a8(img):
    """LVGL LV_COLOR_FORMAT_RGB565A8：先 w*h*2 的 RGB565 行优先，再 w*h 的 alpha。

    注意 alpha 平面**紧跟**在 color 平面后、按 w 字节一行（不是按 stride）。
    写错这个偏移的症状是：图像上半截正常、下半截错位成彩色雪花。
    """
    w, h = img.size
    px = img.load()
    color = bytearray(w * h * 2)
    alpha = bytearray(w * h)
    o = 0
    ai = 0
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < 8:
                # 全透明像素的 RGB 置 0，省得被预乘/插值污染边缘
                r = g = b = 0
            v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            color[o] = v & 0xFF          # little-endian
            color[o + 1] = (v >> 8) & 0xFF
            o += 2
            alpha[ai] = a
            ai += 1
    return bytes(color) + bytes(alpha)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="输出目录（生成 frames.bin + anim_emoji_index.h）")
    ap.add_argument("--cache", default="out/anim_emoji/gif", help="GIF 缓存目录")
    ap.add_argument("--size", type=int, default=64, help="帧边长，默认 64（与现有 twemoji 一致）")
    ap.add_argument("--frames", type=int, default=8, help="每个动画抽帧数，默认 8")
    ap.add_argument("--report", action="store_true", help="只打印体积预算，不写文件")
    args = ap.parse_args()

    if not args.out and not args.report:
        raise SystemExit("要么给 --out，要么给 --report")

    plan = []
    total_bytes = 0
    per_frame = args.size * args.size * 3  # RGB565A8

    for cp, name in DEFAULT_SET:
        try:
            gif = fetch_gif(cp, args.cache)
        except SystemExit as e:
            print(f"  [SKIP] {name} ({cp}): {e}", file=sys.stderr)
            continue
        size_b = os.path.getsize(gif)
        anim_b = per_frame * args.frames
        total_bytes += anim_b
        plan.append((cp, name, gif, size_b, anim_b))
        print(f"  {name:<11} {cp:<7} gif={size_b/1024:>7.1f}KB  "
              f"anim={anim_b/1024:>6.1f}KB ({args.frames}帧 x {per_frame}B)")

    print(f"\n合计 {len(plan)} 个动画，{args.frames} 帧，{args.size}x{args.size} RGB565A8")
    print(f"固件增量约 {total_bytes/1024:.1f} KB ({total_bytes} B)")

    if args.report:
        return

    # ── 生成 frames.bin + 索引头 ─────────────────────────────────
    os.makedirs(args.out, exist_ok=True)
    blob = bytearray()
    entries = []
    for cp, name, gif, _, _ in plan:
        frames = load_frames(gif, args.frames, args.size)
        base = len(blob)
        for f in frames:
            blob += to_rgb565a8(f)
        entries.append({
            "name": name,
            "codepoint": cp,
            "offset": base,
            "frames": len(frames),
            "frame_bytes": per_frame,
        })
        print(f"  packed {name}: offset={base} frames={len(frames)}")

    bin_path = os.path.join(args.out, "anim_frames.bin")
    with open(bin_path, "wb") as f:
        f.write(blob)

    hdr_path = os.path.join(args.out, "anim_emoji_index.h")
    with open(hdr_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"""// 自动生成，勿手改。由 tools/anim_emoji_pack.py 产出。
//
// 素材：Noto Animated Emoji (https://fonts.google.com/noto/specimen/Noto+Emoji)
//       版权所有 Google，许可 CC BY 4.0。
//       512x512 GIF 均匀抽 {args.frames} 帧，缩放到 {args.size}x{args.size}，
//       转 LVGL LV_COLOR_FORMAT_RGB565A8（前 w*h*2 的 RGB565，后 w*h 的 alpha）。
//
// 二进制由 CMake 的 target_add_binary_data 嵌入，符号名见 .cc 里的 extern 声明。
#ifndef ANIM_EMOJI_INDEX_H
#define ANIM_EMOJI_INDEX_H

#include <cstdint>
#include <cstddef>

struct AnimEmojiEntry {{
    const char* name;      // 轮播用的 key
    uint32_t offset;       // 在 anim_frames_bin 里的字节偏移
    uint16_t frames;       // 帧数
    uint16_t frame_bytes;  // 单帧字节数（w*h*3）
    uint16_t size;         // 边长
}};

static constexpr uint16_t kAnimEmojiFrameSize = {args.size};

static const AnimEmojiEntry kAnimEmojiTable[] = {{
""")
        for e in entries:
            f.write(f'    {{"{e["name"]}", {e["offset"]}u, {e["frames"]}u, '
                    f'{e["frame_bytes"]}u, {args.size}u}},\n')
        f.write("};\n\n")
        f.write("static constexpr size_t kAnimEmojiCount = "
                "sizeof(kAnimEmojiTable) / sizeof(kAnimEmojiTable[0]);\n\n")
        f.write(f"static constexpr size_t kAnimEmojiBlobSize = {len(blob)}u;\n\n")
        f.write("#endif  // ANIM_EMOJI_INDEX_H\n")

    print(f"\n写入 {bin_path} ({len(blob)} B)")
    print(f"写入 {hdr_path} ({len(entries)} 条)")


if __name__ == "__main__":
    main()
