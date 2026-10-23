"""从 xiaozhi.bin 里定位**内置** lv_font_t 常量，判定它是 9.3 还是 9.6 布局。

背景（2026-10-23 白屏根因）：
  9.3.0  lv_font_t（36 字节）：get_glyph_dsc@0 get_glyph_bitmap@4 release_glyph@8
                              line_height@12 base_line@16 位域@20 underline@21,22
                              dsc@24 fallback@28 user_data@32
  9.6.0  lv_font_t（44 字节）：dsc@0 fallback@4 user_data@8
                              get_glyph_dsc@12 get_glyph_bitmap@16 release_glyph@20
                              line_height@24 base_line@28 cap_height@32 x_height@36

cbin 字库按 9.3 生成。若编译期用的是 9.6 头，cbin_font_create 会把文件偏移 0 的
0 当成 dsc（空指针）、把偏移 24 的 dsc 字段值 36 当成 line_height => 白屏。

用法：python build-out/check_font_layout.py build-out/fw-XXXX/xiaozhi.bin
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

IROM = range(0x42000000, 0x44000000)
DROM = range(0x3C000000, 0x40000000)


def parse_segments(blob: bytes) -> list[tuple[int, int, int]]:
    """ESP32-S3 镜像：24 字节头，段表从 24 开始，每段 (load_addr, len) + 4 字节对齐。"""
    assert blob[0] == 0xE9, f"魔数不对: {blob[0]:#x}"
    n = blob[1]
    segs = []
    p = 24
    for _ in range(n):
        addr, ln = struct.unpack_from("<II", blob, p)
        p += 8
        segs.append((addr, ln, p))
        p += (ln + 3) & ~3
    return segs


def to_va(segs, va: int) -> int | None:
    """把虚拟地址映射成文件偏移。"""
    for addr, ln, off in segs:
        if addr <= va < addr + ln:
            return off + (va - addr)
    return None


def u32(blob: bytes, off: int) -> int:
    return struct.unpack_from("<I", blob, off)[0]


def find_fonts(blob: bytes, segs) -> list[dict]:
    """在 DROM 段里扫 lv_font_t 候选：两种布局都试，靠字段自洽度打分。"""
    hits = []
    for addr, ln, off in segs:
        if addr not in DROM:
            continue
        body = blob[off:off + ln]
        for i in range(0, len(body) - 44, 4):
            w0, w1, w2, w3 = (u32(body, i), u32(body, i + 4),
                              u32(body, i + 8), u32(body, i + 12))
            w4, w6, w7 = u32(body, i + 16), u32(body, i + 24), u32(body, i + 28)

            # ---- 9.3 形态：@0/@4 是 IROM 函数指针，@12 = line_height(合理 8..64)，
            #      @16 = base_line，@24 = DROM 里的 dsc 指针 ----
            if (w0 in IROM and w1 in IROM and w2 == 0
                    and 8 <= w3 <= 64 and 0 < w4 < w3 and w6 in DROM):
                hits.append(dict(va=addr + i, layout="9.3", size=36,
                                 fn_dsc=w0, fn_bmp=w1, lh=w3, bl=w4, dsc=w6,
                                 dsc_va=w6))

            # ---- 9.6 形态：@0 = dsc 指针，@12/@16 是 IROM 函数指针，
            #      @24 = line_height，@28 = base_line ----
            if (w0 in DROM and w3 in IROM and u32(body, i + 16) in IROM
                    and 8 <= w6 <= 64 and 0 < w7 < w6):
                hits.append(dict(va=addr + i, layout="9.6", size=44,
                                 fn_dsc=w3, fn_bmp=u32(body, i + 16),
                                 lh=w6, bl=w7, dsc=w0, dsc_va=w0))
    # 去重（同一地址可能两种判据都命中，保留自洽度高的）
    seen: dict[int, dict] = {}
    for h in hits:
        k = h["va"]
        if k not in seen or h["layout"] == "9.3":
            seen.setdefault(k, h)
    return sorted(seen.values(), key=lambda x: x["va"])


def check_dsc(blob: bytes, segs, va: int) -> dict | None:
    """读 lv_font_fmt_txt_dsc_t，两种布局各解一次，看哪个自洽。

    这个结构在 9.3 与 9.6 之间也变了，是很硬的旁证：
      9.3：kern_scale(u16)@16，然后 cmap_num:9 | bpp:4 | kern_classes:1 |
           bitmap_format:2 压在一个 u16 里 @18，stride(u8)@20  —— 共 21 字节
      9.6：kern_scale(u16)@16，cmap_num 变成**完整 u16**@18，
           bpp/kern_classes/bitmap_format 挤进一个 u8 @20，
           stride(u8)@21，are_glyphs_dynamic_loaded(bool)@22 —— 共 23 字节

    同一串字节在两种布局下解读结果完全不同，所以"哪个读出来 bpp 合法"
    本身就是版本判据。
    """
    off = to_va(segs, va)
    if off is None or off + 24 > len(blob):
        return None

    base = dict(
        glyph_bitmap=u32(blob, off + 0),
        glyph_dsc=u32(blob, off + 4),
        cmaps=u32(blob, off + 8),
        kern_dsc=u32(blob, off + 12),
        kern_scale=struct.unpack_from("<H", blob, off + 16)[0],
    )

    # 9.3 解
    w93 = struct.unpack_from("<H", blob, off + 18)[0]
    d93 = dict(base, layout="9.3",
               cmap_num=w93 & 0x1FF, bpp=(w93 >> 9) & 0xF,
               kern_classes=(w93 >> 13) & 1,
               bitmap_format=(w93 >> 14) & 3,
               stride=blob[off + 20])

    # 9.6 解
    packed = blob[off + 20]
    d96 = dict(base, layout="9.6",
               cmap_num=struct.unpack_from("<H", blob, off + 18)[0],
               bpp=packed & 0xF,
               kern_classes=(packed >> 4) & 1,
               bitmap_format=(packed >> 5) & 3,
               stride=blob[off + 21],
               dyn_loaded=blob[off + 22])

    def coherent(d):
        return (1 <= d["cmap_num"] <= 32 and d["bpp"] in (1, 2, 4, 8)
                and d["bitmap_format"] in (0, 1, 2))

    ok93, ok96 = coherent(d93), coherent(d96)
    if ok93 and not ok96:
        d93["coherent"] = "仅 9.3 自洽"
        return d93
    if ok96 and not ok93:
        d96["coherent"] = "仅 9.6 自洽"
        return d96
    if ok93 and ok96:
        d93["coherent"] = "两种都自洽(无法区分)"
        return d93
    d93["coherent"] = "两种都不自洽"
    return d93


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1
                else "build-out/fw-259265e/xiaozhi.bin")
    blob = path.read_bytes()
    segs = parse_segments(blob)

    print(f"== {path} ({len(blob):,} bytes) ==")
    for i, (addr, ln, off) in enumerate(segs):
        kind = ("DROM" if addr in DROM else "IROM" if addr in IROM else "RAM/其它")
        print(f"  seg{i}: load={addr:#010x} len={ln:>9,} fileoff={off:#x}  {kind}")

    fonts = find_fonts(blob, segs)

    # 过滤误报：候选必须"自己的 dsc 在自己的布局下自洽"。
    # 真实 lv_font_t 的 dsc 一定指向一个合法的 lv_font_fmt_txt_dsc_t；
    # 而错位命中（比如落在真实结构体前 12 字节）的 dsc 会指向字符串/代码，
    # 两种布局都解不出合法的 cmap_num/bpp。
    checked = []
    for f in fonts:
        d = check_dsc(blob, segs, f["dsc_va"])
        if d is None:
            continue
        if d["coherent"] == "两种都不自洽":
            continue
        if d["layout"] != f["layout"]:
            continue
        checked.append((f, d))

    # 再按地址去重：两个候选相距不到 sizeof 视为同一结构体的错位命中，
    # 保留先出现的（地址更小 = 结构体真正的起点）。
    kept: list[tuple[dict, dict]] = []
    for f, d in sorted(checked, key=lambda x: x[0]["va"]):
        if kept and f["va"] - kept[-1][0]["va"] < 44:
            continue
        kept.append((f, d))

    print(f"\n== 候选 lv_font_t 常量：{len(fonts)} 个，"
          f"经 dsc 自洽+去重后有效 {len(kept)} 个 ==")
    for f, d in kept:
        print(f"\n  VA {f['va']:#010x}  布局={f['layout']} (sizeof={f['size']})")
        print(f"    get_glyph_dsc    = {f['fn_dsc']:#010x}")
        print(f"    get_glyph_bitmap = {f['fn_bmp']:#010x}")
        print(f"    line_height      = {f['lh']}   base_line = {f['bl']}")
        print(f"    dsc              = {f['dsc_va']:#010x}")
        print(f"    dsc 按 {d['layout']} 解：cmap_num={d['cmap_num']} "
              f"bpp={d['bpp']} stride={d['stride']} "
              f"kern_classes={d['kern_classes']} "
              f"bitmap_format={d['bitmap_format']}  [{d['coherent']}]")
        print(f"    dsc: glyph_dsc={d['glyph_dsc']:#x} cmaps={d['cmaps']:#x} "
              f"kern_scale={d['kern_scale']}")

    good = [f for f, _ in kept if f["layout"] == "9.3" and f["lh"] == 25 and f["bl"] == 6]
    bad = [f for f, _ in kept if f["layout"] == "9.6"]
    n93 = sum(1 for f, _ in kept if f["layout"] == "9.3")

    print("\n" + "=" * 62)
    print(f"9.3 布局字体 {n93} 个 / 9.6 布局字体 {len(bad)} 个")
    if bad:
        print(f"结论：FAIL —— 仍有 {len(bad)} 个 9.6 布局的字体常量，"
              "编译期头文件仍是 9.6，白屏根因未消除。")
        return 1
    if not n93:
        print("结论：UNKNOWN —— 没扫到 9.3 布局的字体常量，需人工看上面候选。")
        return 2
    if good:
        f = good[0]
        print(f"结论：PASS —— 内置字体是 9.3 布局；20px 字体在 {f['va']:#010x}，"
              f"line_height@12={f['lh']}、base_line@16={f['bl']}、dsc@24={f['dsc_va']:#x}，"
              "与 cbin 字库的 9.3 布局一致。")
        return 0
    print("结论：PASS（无 9.6），但没找到 lh=25/bl=6 的那个字体，请人工确认。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
