"""生成固件内置的**提醒提示音乐**（一次生成、永久入库，运行时不再合成）。

产物
----
``firmware/xiaozhi/main/assets/common/reminder_chime.ogg``

它由 ``firmware/xiaozhi/main/CMakeLists.txt`` 的
``file(GLOB COMMON_SOUNDS .../assets/common/*.ogg)`` 自动收进 ``EMBED_FILES``，
链接期变成 ``_binary_reminder_chime_ogg_start`` / ``_end`` 两个符号。
固件侧 ``elder_bridge.cc::OnPlayTone`` 的 ``ELDER_TONE_REMINDER`` 直接
``app.PlaySound(OGG_REMINDER_CHIME)`` —— **设备本地播放，不走网络、不重新生成**。

为什么是"现在生成一次、编进固件"
--------------------------------
1. 提醒的第一声必须**不依赖网络**：断网/服务端不可达时也要有声音把老人叫过来，
   语音内容可以没有，但"有事"这个信号必须有；
2. 固定资产才能保证所有设备听到的是**同一段音乐**（用户明确要求），
   也让音频行为可复现 —— 同一份固件在任何时候响的都一样。

为什么本脚本在 **Docker 容器里**跑
----------------------------------
Opus 编码需要 libopus。本机没装（``ctypes`` 找不到），生产镜像里有
（``server/app/voice/opus_codec.py`` 用的就是它）。所以：

    docker cp tools/make_reminder_chime.py elder-server:/tmp/
    docker exec elder-server python /tmp/make_reminder_chime.py --out /tmp/reminder_chime.ogg
    docker cp elder-server:/tmp/reminder_chime.ogg firmware/xiaozhi/main/assets/common/

音色与乐句
----------
- **五声音阶**（C D E G A）：没有半音冲突，怎么排都不会"走调"；
- **铃声包络**：8ms 起音 + 指数衰减 + 2/3 次谐波，接近八音盒，比纯正弦柔和；
- 第一小节（E-G-A）是"注意"，第二小节下行（A-G-E-D-C）是"请听我说"，
  末尾长音收住，给后面的语音留一个干净的起点；
- 总长约 4.9 秒（用户要求 3~10 秒）。
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
from array import array
from pathlib import Path

#: 编码采样率。与 popup.ogg 等既有音效同族（那是 16k），但 24k 与全平台
#: TTS 契约一致，且 Opus 解码后统一上采到设备输出率，两者无差别。
SAMPLE_RATE = 24000

#: 每帧 60ms（与固件解码器的帧长一致，避免它按 60ms 切包时最后一段对不齐）
FRAME_SEC = 0.06

_NOTE = {
    "C4": 261.63, "D4": 293.66, "E4": 329.63, "G4": 392.00, "A4": 440.00,
    "C5": 523.25, "D5": 587.33, "E5": 659.25, "G5": 783.99, "A5": 880.00,
}

#: (音名 或 "rest", 时长秒)
MELODY = (
    ("E4", 0.40), ("G4", 0.40), ("A4", 0.65),
    ("rest", 0.22),
    ("A4", 0.40), ("G4", 0.40), ("E4", 0.40), ("D4", 0.45),
    ("C4", 1.25),
)

# 2026-10-22 用户反馈「太刺耳、太突然」，四处一起改（只改音色不改乐句）：
#   1. 整体降一个八度：E5/G5/A5(659~880Hz) → E4/G4/A4(330~440Hz)。
#      高音区 + 泛音是"刺耳"的主因，降八度比调音量有效得多；
#   2. 泛音大幅削弱：2 次 0.32→0.18、3 次 0.10→0.04。八音盒的"亮"
#      来自高次谐波，对老人（高频听力普遍衰退、对 2~4kHz 更敏感）反而更吵；
#   3. 起音 8ms → 60ms 且改成 sin^2 曲线：8ms 是"啪"一下，60ms 是"渐入"，
#      这是"太突然"的直接原因；
#   4. 峰值 0.45 → 0.30、衰减 3.0 → 2.2：整体更轻、余音更长，不那么"凶"。
_HARMONICS = ((1, 1.00), (2, 0.18), (3, 0.04))
_ATTACK_SEC = 0.060
_DECAY_RATE = 2.2
_PEAK = 0.30          # 各音符之间的相对电平（绝对电平由 _TARGET_PEAK 统一收）
#: 归一化后的目标峰值（占满刻度的比例）。0.55 ≈ -5dBFS：
#: 既留足余量不削波，又不至于小到老人听不见。
_TARGET_PEAK = 0.55


def _add_note(buf: list[float], freq: float, dur: float) -> None:
    """把一个音**叠加**进缓冲（不是覆盖）—— 尾部衰减与下一个音的起音自然交叠。

    性能口径：朴素写法每样本 3 次 ``sin`` + 1 次 ``exp``（5 秒音频约 250 万次
    三角运算），实测 2.7 秒。这里换成**单周期波表**（音是稳态的，一个周期的
    波形可以整段平铺）+ **递推衰减**（每样本乘固定系数），等价且快 50 倍。
    """
    n = int(SAMPLE_RATE * dur)
    if n <= 0:
        return
    period = max(2, int(round(SAMPLE_RATE / freq)))
    two_pi = 2.0 * math.pi
    wave = [0.0] * period
    for i in range(period):
        v = 0.0
        for mult, amp in _HARMONICS:
            v += amp * math.sin(two_pi * mult * i / period)
        wave[i] = v

    attack = max(1, int(SAMPLE_RATE * _ATTACK_SEC))
    decay = math.exp(-_DECAY_RATE / SAMPLE_RATE)
    limit = len(buf)
    env = 0.0
    half_pi = math.pi / 2.0
    for i in range(n):
        if i < attack:
            # sin^2 渐入：一阶导在起点为 0，听感上没有"起点那一下"
            r = math.sin(half_pi * i / attack)
            env = r * r
        else:
            env = env * decay
        if i >= limit:
            break
        buf[i] += wave[i % period] * env * _PEAK


def synth_pcm() -> bytes:
    """合成整段音乐的 PCM（S16LE / mono / SAMPLE_RATE）。"""
    total = sum(d for _n, d in MELODY) + 0.35   # 留尾巴让最后一个音衰减完
    buf = [0.0] * int(SAMPLE_RATE * total)
    for name, dur in MELODY:
        if name != "rest":
            _add_note(buf, _NOTE[name], dur + 0.25)
    # ---- 归一化 + 首尾淡入淡出 ----
    # 2026-10-22：衰减从 3.0 放到 2.2 之后音符重叠变多，固定 _PEAK 会叠到
    # 削波（实测 peak 打满 32768）—— 削波是方波，比原来的高音更刺耳，
    # 正好和"柔和一点"的目标相反。所以改成**先合成、再按实际峰值归一化**：
    # 固定 _PEAK 只负责各音符之间的相对比例，绝对电平由这里统一收。
    peak = max((abs(v) for v in buf), default=0.0)
    if peak > 1e-6:
        scale = _TARGET_PEAK / peak
        buf = [v * scale for v in buf]
    # 首尾各 40ms 淡入淡出：缓冲区两端直接跳变会有"咔"声（尤其播放开始时）
    edge = max(1, int(SAMPLE_RATE * 0.04))
    for i in range(min(edge, len(buf))):
        g = i / edge
        buf[i] *= g
        buf[-1 - i] *= g

    # ⚠️ 不要用 struct.pack("<%dh" % n, *buf)：11 万个样本当可变参数展开，
    # 光打包就要 2.6 秒（合成本身只占 0.1 秒）。array 批量构造快两个数量级。
    out = array("h", (max(-32768, min(32767, int(v * 32767.0))) for v in buf))
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


# ---------------------------------------------------------------- OGG 封装
def _crc32_ogg(data: bytes) -> int:
    """Ogg 用的 CRC32：多项式 0x04C11DB7，**非反射**、初值 0、不取反。

    与 zlib.crc32 完全不同（那是反射型），不能替换 —— 写错的直接症状是
    固件 demux 报 "invalid page"，整段音乐不响。
    """
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 \
                else (crc << 1) & 0xFFFFFFFF
    return crc


def _page(serial: int, seq: int, granule: int, packets: list[bytes],
          *, bos: bool = False, eos: bool = False) -> bytes:
    """打一个 Ogg 页（27 字节头 + 段表 + 数据）。"""
    segments: list[int] = []
    body = bytearray()
    for pkt in packets:
        n = len(pkt)
        while n >= 255:
            segments.append(255)
            n -= 255
        segments.append(n)
        body += pkt
    if len(segments) > 255:
        raise ValueError("一页放不下这么多段（>255），把 packets 切小一点")

    header_type = (0x02 if bos else 0) | (0x04 if eos else 0)
    head = bytearray(b"OggS")
    head.append(0)                                   # version
    head.append(header_type)
    head += struct.pack("<q", granule)
    head += struct.pack("<I", serial)
    head += struct.pack("<I", seq)
    head += b"\x00" * 4                              # CRC 占位
    head.append(len(segments))
    head += bytes(segments)
    page = bytes(head) + bytes(body)
    crc = _crc32_ogg(page)
    return page[:22] + struct.pack("<I", crc) + page[26:]


def mux_ogg(packets: list[bytes], *, serial: int = 0x1234ABCD) -> bytes:
    """把一串 Opus 包封成 OGG Opus（OpusHead + OpusTags + 音频页）。

    颗粒度（granulepos）按 Opus 规范以 **48kHz 采样**计：本文件是 24kHz、
    每包 60ms ⇒ 每包推进 2880。写错只会影响 seek/时长，不影响解码，
    但既然要给固件用就按标准写对。
    """
    pre_skip = 312        # 6.5ms，与既有 popup.ogg 一致
    opus_head = (b"OpusHead" + bytes([1, 1])
                 + struct.pack("<H", pre_skip)
                 + struct.pack("<I", SAMPLE_RATE)
                 + struct.pack("<h", 0) + bytes([0]))
    vendor = b"voice-avatar-box reminder chime"
    opus_tags = (b"OpusTags" + struct.pack("<I", len(vendor)) + vendor
                 + struct.pack("<I", 0))

    pages = [_page(serial, 0, 0, [opus_head], bos=True),
             _page(serial, 1, 0, [opus_tags])]
    step = int(48 * FRAME_SEC * 1000)     # 60ms @48k = 2880
    granule = 0
    seq = 2
    batch: list[bytes] = []
    for i, pkt in enumerate(packets):
        batch.append(pkt)
        granule += step
        last = i == len(packets) - 1
        # 每 10 包（600ms）一页；末页必须带 EOS 且 granule 收在总长
        if len(batch) >= 10 or last:
            pages.append(_page(serial, seq, granule, batch, eos=last))
            seq += 1
            batch = []
    return b"".join(pages)


def encode_packets(pcm: bytes) -> list[bytes]:
    """逐帧调 libopus 编码（用服务端同一套 ctypes 封装，行为与线上一致）。"""
    root = Path(__file__).resolve().parent.parent
    server_dir = root / "server"
    if server_dir.is_dir():
        # 本机布局：脚本在 tools/，包在 server/app
        sys.path.insert(0, str(server_dir))
    # 容器里 app 包已装进 site-packages，不需要也不能加路径（server/ 不存在）
    from app.voice import opus_codec   # noqa: E402  必须在 sys.path 之后导入

    frame_bytes = opus_codec.frame_bytes(SAMPLE_RATE)
    packets: list[bytes] = []
    for off in range(0, len(pcm), frame_bytes):
        frame = pcm[off:off + frame_bytes]
        if len(frame) < frame_bytes:
            frame = frame + b"\x00" * (frame_bytes - len(frame))
        pkt = opus_codec.encode_opus(frame, SAMPLE_RATE)
        if not pkt:
            raise RuntimeError(f"第 {off // frame_bytes} 帧编码失败（libopus 不可用？）")
        packets.append(pkt)
    return packets


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成固件内置的提醒提示音乐")
    ap.add_argument("--out", default=str(
        Path(__file__).resolve().parent.parent
        / "firmware/xiaozhi/main/assets/common/reminder_chime.ogg"))
    args = ap.parse_args(argv)

    pcm = synth_pcm()
    packets = encode_packets(pcm)
    ogg = mux_ogg(packets)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(ogg)

    dur = len(pcm) / 2 / SAMPLE_RATE
    peak = max(abs(v) for v in struct.unpack(f"<{len(pcm) // 2}h", pcm))
    print(f"PCM : {len(pcm)} 字节 / {dur:.2f}s / peak={peak}")
    print(f"OGG : {len(ogg)} 字节 / {len(packets)} 个 Opus 包 / 压缩比 "
          f"{len(pcm) / max(1, len(ogg)):.1f}x")
    print(f"输出: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
