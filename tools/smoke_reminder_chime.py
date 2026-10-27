"""提醒提示音乐（固件内置资源）的静态门禁。

守的是什么
----------
「提醒先响一段音乐，再念事项」这件事，音乐**不是运行时生成的**，而是
一次性生成后编进固件的一段固定 OGG：

    firmware/xiaozhi/main/assets/common/reminder_chime.ogg
    （由 tools/make_reminder_chime.py 生成，CMake 的 glob 自动 EMBED_FILES）

它一旦被删掉/换坏/忘了接进 OnPlayTone，服务端和运行时**全都不会报错**：
提醒照样到点、屏照样变、回执照样回，只是没有声音。所以必须有一条静态断言
把「文件在、格式对、固件真的在放它」钉住。

为什么还要验固件源码里的**顺序**
-------------------------------
``play_tone(REMINDER)`` 与 ``ScheduleAnnounce`` 的顺序是这个产品需求本身
（音乐先、语音后）。把它俩调换，一切依然能编译、能出声，只是音乐盖住第一
句话 —— 老人只听得清开头，被盖住等于没说。顺序只能靠读源码确认。

用法：
    python tools/smoke_reminder_chime.py
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
ASSET = ROOT / "firmware/xiaozhi/main/assets/common/reminder_chime.ogg"
BRIDGE = ROOT / "firmware/components/elder/src/elder_bridge.cc"
UI_HDR = ROOT / "firmware/components/elder/include/elder_ui.h"
GEN = ROOT / "tools/make_reminder_chime.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(SERVER))
from verify_reminder_chime import parse_pages, split_packets  # noqa: E402

_PASS = 0
_FAIL = 0
_SKIP = 0
_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"  ✓ {name}")
    else:
        _FAIL += 1
        _FAILED.append(name)
        print(f"  ✗ {name}" + (f"  [{str(detail)[:200]}]" if detail else ""))


def skip(name: str, why: str) -> None:
    """第三态：判据对象不存在（如本机没 libopus），既不算通过也不算失败。"""
    global _SKIP
    _SKIP += 1
    print(f"  - SKIP {name}：{why}")


def main() -> int:
    print("提醒音乐（固件内置 OGG）静态门禁")

    # ---- A. 资源文件本身 ----
    print("\n── A. 资源文件 ──")
    if not ASSET.exists():
        check("A1 reminder_chime.ogg 存在于 assets/common", False, str(ASSET))
        _report()
        return 1
    blob = ASSET.read_bytes()
    check("A1 reminder_chime.ogg 存在于 assets/common", True)
    check("A2 是 OGG 容器（首 4 字节 OggS）", blob[:4] == b"OggS", repr(blob[:8]))

    pages = parse_pages(blob)
    bad = [p["seq"] for p in pages if not p["crc_ok"]]
    check("A3 每一页 CRC 都自洽（手写封装最容易错的地方）", not bad, f"坏页={bad}")

    packets = split_packets(pages)
    check("A4 头部是 Opus（OpusHead + OpusTags）",
          len(packets) >= 2 and packets[0][:8] == b"OpusHead"
          and packets[1][:8] == b"OpusTags",
          f"{packets[0][:8]!r} {packets[1][:8]!r}" if len(packets) >= 2 else "包数不足")
    check("A5 末页带 EOS（缺了固件可能播完不回落）",
          bool(pages[-1]["header_type"] & 0x04), str(pages[-1]["header_type"]))

    dur = pages[-1]["granule"] / 48000.0   # granule 以 48kHz 计
    check("A6 时长落在需求区间 3~10 秒", 3.0 <= dur <= 10.0, f"{dur:.2f}s")
    check("A7 单声道（提醒是老人听的人声场景，立体声没意义还费带宽）",
          packets[0][9] == 1, f"channels={packets[0][9]}")

    # 真解码一遍：只验文件头的话，包体全坏也能通过
    try:
        from app.voice import opus_codec  # noqa: F401
        have_opus = opus_codec.libopus_available()
    except Exception:  # noqa: BLE001
        have_opus = False
    if not have_opus:
        skip("A8 逐包解码有实际声音（rms>300）", "本机无 libopus；CI 已装 libopus0 必真跑")
    else:
        from app.voice import opus_codec  # noqa: E402
        total = 0
        energy = 0
        for pkt in [p for p in packets[2:] if p]:
            pcm = opus_codec.decode_opus(pkt, 24000)
            total += len(pcm)
            energy += sum(s * s for s in struct.unpack(f"<{len(pcm) // 2}h", pcm))
        n = max(1, total // 2)
        rms = (energy / n) ** 0.5
        check("A8 逐包解码有实际声音（rms>300）", rms > 300, f"rms={rms:.0f}")

    # ---- B. 固件侧真的在放它 ----
    print("\n── B. 固件接入 ──")
    bridge = BRIDGE.read_text(encoding="utf-8")
    ui_hdr = UI_HDR.read_text(encoding="utf-8")

    def func_body(src: str, signature: str) -> str:
        """截出某个函数的函数体（从签名到下一个顶格的 ``}``）。

        ⚠️ 断言必须落在**函数体内**，不能用「短标记串 in 全文」：
        ``case ELDER_TONE_REMINDER:`` 在 ``ToneName()`` 里也出现一次，
        用全文匹配的话，把 ``OnPlayTone`` 里的分支删掉照样绿 —— 那是假绿。
        """
        i = src.find(signature)
        if i < 0:
            return ""
        j = src.find("\n}\n", i)
        return src[i:j if j > 0 else len(src)]

    on_play_tone = func_body(bridge, "void OnPlayTone(")
    check("B1 固件声明了嵌入符号 _binary_reminder_chime_ogg_start",
          "_binary_reminder_chime_ogg_start" in bridge
          and "_binary_reminder_chime_ogg_end" in bridge)
    check("B2 音效枚举里有 ELDER_TONE_REMINDER", "ELDER_TONE_REMINDER" in ui_hdr)
    # B3 的判据跟着实现走（2026-10-27：OnPlayTone 改成"先选 sound、再在
    # Application::Schedule 里 PlaySound"，因为就地 PlaySound 会同步解 OGG +
    # codec->EnableOutput(true)，把 elder_input / esp_event 两条小栈路径压穿）。
    # 保护意图一点没变：ELDER_TONE_REMINDER 这一支必须真的指向内置 chime，
    # 且函数体里必须真的调 PlaySound —— 落到 default 就是"提醒到点但没声音"，
    # 运行时零报错，是本项目最坏的一类静默失效。
    check("B3 OnPlayTone **函数体内**为该枚举接了 PlaySound（落到 default 就静默无声）",
          bool(on_play_tone)
          and "case ELDER_TONE_REMINDER:" in on_play_tone
          and "kReminderChime" in on_play_tone
          and "PlaySound(" in on_play_tone,
          f"func_len={len(on_play_tone)}")

    # B9（2026-10-27）：PlaySound 必须**延后**到主事件循环，不许就地同步播。
    # AudioService::PlaySound() 是同步的（遍历 OGG 页 + 打开 codec + 推 I2S），
    # 而 OnPlayTone 会被 elder_input（按键确认 → OnReminder(DONE) → 提示音）
    # 和 esp_event 默认任务同步调到 —— 就地调就是"长按确认偶发重启"。
    # 判据用位置关系：Schedule( 必须出现在 PlaySound( 之前。
    _i_sched = on_play_tone.find("Schedule(")
    _i_play = on_play_tone.find("PlaySound(")
    check("B9 PlaySound 经 Schedule 延后，不就地同步播（避免压穿小栈任务）",
          -1 < _i_sched < _i_play, f"Schedule@{_i_sched} PlaySound@{_i_play}")

    # 顺序：play_tone(REMINDER) 必须出现在 ScheduleAnnounce 之前
    i_tone = bridge.find("elder_ui_play_tone(ELDER_TONE_REMINDER)")
    i_sched = bridge.find("ScheduleAnnounce(text)")
    check("B4 提醒触发时**先响音乐、再排播语音**（顺序即产品语义）",
          -1 < i_tone < i_sched, f"play_tone@{i_tone} Schedule@{i_sched}")
    check("B5 语音是延后播的（专用任务等音乐放完），不是同时下发",
          "xTaskCreate(AnnounceTask" in bridge
          and "vTaskDelay(pdMS_TO_TICKS(kChimeMs))" in bridge)
    # 反向要求：不许用 esp_timer 回调做这件事（默认 3.5KB 栈里跑 LVGL 会溢出，
    # 症状是随机 LoadProhibited）。只在**可执行代码**里查，注释里提到不算。
    code_only = "\n".join(line for line in bridge.split("\n")
                          if not line.lstrip().startswith(("*", "//", "/*")))
    check("B6 播报不用 esp_timer（3.5KB 栈里跑 LVGL 会随机崩）",
          "esp_timer_start_once" not in code_only
          and "esp_timer_create" not in code_only)
    check("B7 生成脚本仍在库（可复现同一段音乐）", GEN.exists(), str(GEN))

    # xiaozhi 侧是生成副本：改了 components/elder 必须同步，否则 CI 的
    # diff -rq 闸门会红。这里提前给一条可读的定位信息。
    twin = ROOT / "firmware/xiaozhi/components/elder/src/elder_bridge.cc"
    if twin.exists():
        check("B8 生成副本已同步（firmware/xiaozhi/components/elder）",
              "ELDER_TONE_REMINDER" in twin.read_text(encoding="utf-8"),
              "跑 tools/merge_to_xiaozhi.py")

    return _report()


def _report() -> int:
    print(f"\n通过 {_PASS} / 失败 {_FAIL} / 跳过 {_SKIP}")
    if _FAILED:
        print("失败的断言：")
        for name in _FAILED:
            print(f"  - {name}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
