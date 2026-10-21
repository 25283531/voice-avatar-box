"""校验固件内置的 reminder_chime.ogg：容器结构 + Opus 包能否真解出音频。

为什么需要一个独立校验
----------------------
这份 OGG 是**手写的封装器**（`tools/make_reminder_chime.py` 里的 Ogg 页 +
CRC32），而它最终由固件的 demux 读取。任何一处写错（CRC 多项式、段表、
granulepos）在服务端都不会报错 —— 只有真机上"点了提醒却一片安静"。
所以生成之后必须立刻做两件事：

1. **结构自洽**：重新解析每一页，逐页复算 CRC，核对 BOS/EOS/段表；
2. **Opus 包真的可解**：用与线上同一套 libopus 封装逐包解码，
   核对总时长与能量 —— 只验"文件头是 OpusHead"是没用的，
   包体全坏它照样通过。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path


def _crc32_ogg(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 \
                else (crc << 1) & 0xFFFFFFFF
    return crc


def parse_pages(blob: bytes) -> list[dict]:
    pages, i = [], 0
    while i < len(blob):
        if blob[i:i + 4] != b"OggS":
            raise ValueError(f"偏移 {i} 不是 OggS（文件被截断或封装错）")
        n_seg = blob[i + 26]
        seg = blob[i + 27:i + 27 + n_seg]
        body_len = sum(seg)
        end = i + 27 + n_seg + body_len
        raw = bytearray(blob[i:end])
        crc_stored = struct.unpack("<I", raw[22:26])[0]
        raw[22:26] = b"\x00\x00\x00\x00"
        crc_calc = _crc32_ogg(bytes(raw))
        pages.append({
            "header_type": blob[i + 5],
            "granule": struct.unpack("<q", blob[i + 6:i + 14])[0],
            "seq": struct.unpack("<I", blob[i + 18:i + 22])[0],
            "crc_ok": crc_stored == crc_calc,
            "segments": list(seg),
            "body": bytes(blob[i + 27 + n_seg:end]),
        })
        i = end
    return pages


def split_packets(pages: list[dict]) -> list[bytes]:
    """按段表还原包（值为 255 的段表示"本包未完，续到下一段"）。"""
    packets, cur = [], bytearray()
    for pg in pages:
        off = 0
        for n in pg["segments"]:
            cur += pg["body"][off:off + n]
            off += n
            if n < 255:          # 小于 255 = 这个包到此结束
                packets.append(bytes(cur))
                cur = bytearray()
    return packets


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else
                "firmware/xiaozhi/main/assets/common/reminder_chime.ogg")
    blob = path.read_bytes()
    pages = parse_pages(blob)

    print(f"文件   : {path}  {len(blob)} 字节  {len(pages)} 页")
    bad = [p["seq"] for p in pages if not p["crc_ok"]]
    print(f"CRC    : {'全部通过' if not bad else '页 ' + str(bad) + ' 校验失败'}")

    packets = split_packets(pages)
    head, tags = packets[0], packets[1]
    print(f"包     : {len(packets)} 个（head/tags + {len(packets) - 2} 个音频）")
    print(f"OpusHead: {head[:8]!r} channels={head[9]} pre_skip="
          f"{struct.unpack('<H', head[10:12])[0]} rate="
          f"{struct.unpack('<I', head[12:16])[0]}")
    print(f"OpusTags: {tags[:8]!r}")

    audio = [p for p in packets[2:] if p]
    total = 0
    energy = 0
    peak = 0
    try:
        from app.voice import opus_codec  # noqa: E402
    except ImportError:
        # 本机布局：脚本在 tools/，包在 server/app（容器里 app 已可直接导入）
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
        from app.voice import opus_codec  # noqa: E402

    for pkt in audio:
        pcm = opus_codec.decode_opus(pkt, 24000)
        total += len(pcm)
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        energy += sum(s * s for s in samples)
        peak = max(peak, max(abs(s) for s in samples))

    n = total // 2
    dur = n / 24000
    rms = (energy / max(1, n)) ** 0.5
    print(f"解码   : {total} 字节 / {dur:.2f}s / rms={rms:.0f} / peak={peak}")
    print(f"末页granule: {pages[-1]['granule']} (= {pages[-1]['granule'] / 48000:.2f}s @48k)")
    print(f"EOS    : {'有' if pages[-1]['header_type'] & 0x04 else '缺失'}")

    ok = (not bad
          and head[:8] == b"OpusHead"
          and tags[:8] == b"OpusTags"
          and 3.0 <= dur <= 10.0
          and rms > 300          # 有实际声音，不是一片静音
          and pages[-1]["header_type"] & 0x04)
    print("\n结论：", "通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
