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
  3. 帧序列 + 自写切帧定时器 —— **选它**
     播放只是「换一张已经解好的位图」，CPU 几乎为零。

产品形态（2026-10-20 用户拍板）
------------------------------
只留**一张可爱的动物动图**（熊猫 🐼）当「空闲屏保」：
  · 界面 60 秒没有变化 ⇒ 开始循环播放，一直播到有人说话为止
  · 有语境（在听 / 在想 / 在说 / 有提醒）⇒ 停掉动图，回到静态表情图标
最初做过 6 个动画轮流放，但那种轮播只在空闲时触发，实际看到的机会很少，
不如把预算全押在一张上，做成**完整时长、约 60 帧**的平滑循环 ——
帧数一少（比如 12 帧）整段就变成幻灯片，比不动更难看。
熊猫原片是 71 帧 / 2.4 秒（30ms/帧），抽到 60 帧后 2.4 秒不打折。

素材
----
`fonts.gstatic.com/s/e/notoemoji/latest/<codepoint>/512.gif`：Google 官方
Noto Animated Emoji，真实可达（实测 200，512x512，120ms/帧）。
许可 CC BY 4.0，可商用，署名写在 anim_emoji_index.h 里。
lottiefiles.com 上的动图要登录才能下载、且导出的是 .json，只能走方案 1，放弃。

自适应抽帧（为什么不是均匀抽帧）
------------------------------
Noto 的 GIF 里运动是不均匀的：熊猫从坐着到扑腾的那几帧位移很大，
而首尾各有十几帧几乎不动。均匀抽 60/71 帧会把这些「静止段」也占掉配额，
真正动的部分反而被抽稀 ⇒ 爆发动作一跳而过。
这里改成**按运动量取舍**：累积到画面变化超过阈值才落一帧，同时记下
这一帧该停多久（`hold_ms`），所以时间轴和原 GIF 完全一致，只是省掉了
「看起来没变」的帧。

体积预算（实测，见 --report）
---------------------------
64x64 RGB565A8 = 12,288 B/帧。60 帧 ≈ 720KB。
app 分区 0x3f0000 = 4,128,768 B，当前固件 2,927,472 B ⇒ 余 1.15MB，
放完熊猫还剩 ~450KB 给后续功能。
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
import os
import sys
import urllib.request

CDN = "https://fonts.gstatic.com/s/e/notoemoji/latest/{cp}/512.gif"

# 只留一张：熊猫。为什么是它 —— 圆脸、动作慢、辨识度高，
# 老人一眼能看懂「这是只小动物」，而且深色轮廓在浅底深底都不糊。
# 想换动物就改这张表里的一行，然后重跑本脚本 + 重编（索引表是编译期常量）。
DEFAULT_SET = [
    ("1f43c", "panda"),        # 🐼 熊猫
]

# 备选（需要时挪进 DEFAULT_SET 即可，均已实测 CDN 可达）：
#   1f431 cat 🐱 猫        1f43b bear 🐻 熊      1f98a fox 🦊 狐狸
#   1f427 penguin 🐧 企鹅   1f981 lion 🦁 狮子
#   1f436 与 1f430 在 CDN 上是 404（Noto 没做这两个的动画版）
#   1f986 鸭子、1f439 仓鼠、1f42f 老虎 同样是 404


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


def _mean_abs_diff(a, b):
    """两张同尺寸 RGBA 的平均绝对差（0~255）。

    只比 RGB：alpha 在 Noto 的表情里基本是 0/255 硬边，纳入比较会把
    「轮廓动了一点点」放大成整块区域的变化，反而抽不出关键帧。
    """
    from PIL import ImageChops

    d = ImageChops.difference(a.convert("RGB"), b.convert("RGB")).convert("L")
    return sum(d.getdata()) / float(a.size[0] * a.size[1])


def load_frames(path, size):
    """读出全部帧（RGBA，缩放到 size x size）+ 每帧原始时长（ms）。"""
    from PIL import Image, ImageSequence

    frames, durs = [], []
    with Image.open(path) as im:
        for f in ImageSequence.Iterator(im):
            # ⚠️ 时长必须在 convert/resize **之前**取：convert() 返回的是新
            # Image 对象，原 GIF 帧的 info（含 duration）不会带过来，之后
            # 读到的永远是默认值，时间轴被压成一半，播放快一倍。
            durs.append(int(f.info.get("duration", 0)) or 50)
            frames.append(f.convert("RGBA").resize((size, size), Image.LANCZOS))
    if not frames:
        raise SystemExit(f"{path} 里没有帧")
    return frames, durs


def sample_adaptive(frames, durs, thr, max_hold_ms):
    """按运动量抽帧，返回 (保留的帧下标, 每帧停留时长)。

    判据是「相对**上一张已落帧**」的累积变化，而不是相邻帧差：
    相邻差会把一段缓慢连续的移动切成几十个「都不到阈值」的小步，
    最后全被丢掉，动作就整段消失了。
    """
    picked = [0]
    holds = []
    acc = 0
    last = 0
    for i in range(1, len(frames)):
        acc += durs[i - 1]
        if _mean_abs_diff(frames[last], frames[i]) >= thr or acc >= max_hold_ms:
            holds.append(acc)
            picked.append(i)
            last = i
            acc = 0
    # 最后一帧要撑到本轮结束，循环才接得上（否则尾巴会突然加速）
    holds.append(acc + durs[-1] if acc else durs[-1])
    return picked, holds


def pick_frames(frames, durs, max_frames, max_hold_ms):
    """在帧数上限内找一组尽量平滑的抽帧方案。

    从低阈值往上试：阈值越低保留的帧越多、越平滑。取第一个不超过
    max_frames 的方案；全都不行就退到「均匀抽 max_frames 帧」，
    让时间轴至少还对得上。
    """
    best = None
    for thr in [0.6, 0.8, 1.0, 1.3, 1.6, 2.0, 2.5, 3.0, 3.6, 4.3, 5.2, 6.2]:
        picked, holds = sample_adaptive(frames, durs, thr, max_hold_ms)
        best = (picked, holds, thr)
        if len(picked) <= max_frames:
            return picked, holds, thr
    # 兜底：均匀抽
    total = len(frames)
    step = (total - 1) / (max_frames - 1) if max_frames > 1 else 0
    picked = [round(i * step) for i in range(max_frames)]
    holds = []
    for k in range(len(picked)):
        nxt = picked[k + 1] if k + 1 < len(picked) else total
        holds.append(sum(durs[picked[k]:nxt]))
    return picked, holds, float("nan")


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
    ap.add_argument("--frames", type=int, default=60,
                    help="每个动画的帧数上限，默认 60（超了会自动抬高运动阈值）")
    ap.add_argument("--max-hold-ms", type=int, default=600,
                    help="单帧最多停多久，默认 600ms（太长会显得卡住）")
    ap.add_argument("--report", action="store_true", help="只打印体积预算，不写文件")
    args = ap.parse_args()

    if not args.out and not args.report:
        raise SystemExit("要么给 --out，要么给 --report")

    per_frame = args.size * args.size * 3  # RGB565A8

    plan = []
    for cp, name in DEFAULT_SET:
        gif = fetch_gif(cp, args.cache)
        frames, durs = load_frames(gif, args.size)
        picked, holds, thr = pick_frames(frames, durs, args.frames, args.max_hold_ms)
        anim_b = per_frame * len(picked)
        total_ms = sum(holds)
        plan.append({
            "cp": cp, "name": name, "gif": gif,
            "picked": picked, "holds": holds,
            "bytes": anim_b, "total_ms": total_ms,
        })
        print(f"  {name:<9} {cp:<7} 源 {len(frames)} 帧/{sum(durs)}ms"
              f" → 保留 {len(picked)} 帧/{total_ms}ms"
              f"（阈值 {thr:.1f}，{len(picked)/ (total_ms/1000.0):.1f} fps）"
              f"  {anim_b/1024:.0f}KB")

    total_bytes = sum(p["bytes"] for p in plan)
    print(f"\n合计 {len(plan)} 个动画，{args.size}x{args.size} RGB565A8，"
          f"{per_frame} B/帧")
    print(f"固件增量约 {total_bytes/1024:.1f} KB ({total_bytes} B)")
    print("app 分区 4,128,768 B；当前固件 2,927,472 B "
          f"⇒ 放完还剩 {4128768 - 2927472 - total_bytes} B")

    if args.report:
        return

    # ── 生成 frames.bin + 索引头 ─────────────────────────────────
    os.makedirs(args.out, exist_ok=True)
    blob = bytearray()
    entries = []
    ms_table = []
    for p in plan:
        frames, _ = load_frames(p["gif"], args.size)
        base = len(blob)
        for i in p["picked"]:
            blob += to_rgb565a8(frames[i])
        entries.append({
            "name": p["name"],
            "offset": base,
            "frames": len(p["picked"]),
            "frame_bytes": per_frame,
            "ms_off": len(ms_table),
        })
        ms_table.extend(p["holds"])
        print(f"  packed {p['name']}: offset={base} frames={len(p['picked'])}")

    bin_path = os.path.join(args.out, "anim_frames.bin")
    with open(bin_path, "wb") as f:
        f.write(blob)

    hdr_path = os.path.join(args.out, "anim_emoji_index.h")
    with open(hdr_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"""// 自动生成，勿手改。由 tools/anim_emoji_pack.py 产出。
//
// 素材：Noto Animated Emoji (https://fonts.google.com/noto/specimen/Noto+Emoji)
//       版权所有 Google，许可 CC BY 4.0。
//       512x512 GIF 按运动量自适应抽帧，缩放到 {args.size}x{args.size}，
//       转 LVGL LV_COLOR_FORMAT_RGB565A8（前 w*h*2 的 RGB565，后 w*h 的 alpha）。
//
// frame_ms 是这个动画每一帧该停多久（ms），时间轴与原 GIF 一致。
// 二进制由 CMake 的 target_add_binary_data 嵌入，符号名见 .cc 里的 extern 声明。
#ifndef ANIM_EMOJI_INDEX_H
#define ANIM_EMOJI_INDEX_H

#include <cstdint>
#include <cstddef>

struct AnimEmojiEntry {{
    const char* name;      // 动画名，作为 key 用
    uint32_t offset;       // 在 anim_frames_bin 里的字节偏移
    uint16_t frames;       // 帧数
    uint16_t frame_bytes;  // 单帧字节数（w*h*3）
    uint16_t ms_off;       // 本动画第一帧的时长在 kAnimEmojiFrameMs 里的下标
    uint16_t size;         // 边长
}};

static constexpr uint16_t kAnimEmojiFrameSize = {args.size};

static const AnimEmojiEntry kAnimEmojiTable[] = {{
""")
        for e in entries:
            f.write(f'    {{"{e["name"]}", {e["offset"]}u, {e["frames"]}u, '
                    f'{e["frame_bytes"]}u, {e["ms_off"]}u, {args.size}u}},\n')
        f.write("};\n\n")
        f.write("static constexpr size_t kAnimEmojiCount = "
                "sizeof(kAnimEmojiTable) / sizeof(kAnimEmojiTable[0]);\n\n")
        f.write(f"static constexpr size_t kAnimEmojiBlobSize = {len(blob)}u;\n\n")
        f.write("// 各帧停留时长（ms），下标以 AnimEmojiEntry::ms_off 为基\n")
        f.write("static const uint16_t kAnimEmojiFrameMs[] = {\n")
        line = "   "
        for v in ms_table:
            piece = f" {v}u,"
            if len(line) + len(piece) > 96:
                f.write(line + "\n")
                line = "   "
            line += piece
        if line.strip():
            f.write(line + "\n")
        f.write("};\n\n")
        f.write("static constexpr size_t kAnimEmojiFrameMsCount = "
                "sizeof(kAnimEmojiFrameMs) / sizeof(kAnimEmojiFrameMs[0]);\n\n")
        f.write("#endif  // ANIM_EMOJI_INDEX_H\n")

    print(f"\n写入 {bin_path} ({len(blob)} B)")
    print(f"写入 {hdr_path} ({len(entries)} 条, {len(ms_table)} 个时长)")


if __name__ == "__main__":
    main()
