"""可播性过滤（规划语/工具名）+ 缓冲区尾巴补播的单元断言。

为什么单独一个文件而不是塞进 smoke_voice
--------------------------------------
``smoke_voice`` 要起真服务 + 假上游，跑一次几十秒，断言的是**协议时序**。
而这里的被测对象是一组纯函数加一个不含网络的 ``_ReplySpeaker``，
秒级可跑。改一行正则就想立刻知道"有没有把正常回复也拦掉"，
这个反馈回路必须短，所以单独成文件。

背景（2026-10-21 真机事故）
--------------------------
老人问天气，听到的第一句是「老人查天气，我要调用……」——模型把自己的
行动规划念了出来。同一次事故里还有第二个 bug：``_run_turn`` 从不
``flush_rest()``，于是**结尾没有句末标点的那段话永远不会被念**。
两个 bug 叠加，老人听到的就是"一句莫名其妙的自言自语，然后没声音"。
本文件把这两件事都钉住。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from app.voice import asr_tts, opus_codec                     # noqa: E402
from app.voice import session as vs                           # noqa: E402
from app.voice.session import (                               # noqa: E402
    _ReplySpeaker,
    _speech_block_reason,
    _speech_clean,
    _split_first_sentence,
)

_passed = 0
_failed = 0
_failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  [PASS] {label}" + (f"  -> {detail}" if detail else ""))
    else:
        _failed += 1
        _failures.append(label)
        print(f"  [FAIL] {label}" + (f"  -> {detail}" if detail else ""))


# ---------------------------------------------------------------- 假设备
class _FakeWS:
    def __init__(self) -> None:
        self.json: list[dict] = []
        self.audio: list[bytes] = []
        # 统一事件流：json 与 audio 按**真实到达顺序**记在一起。
        # 分成两个 list 就再也验不了"tts stop 是不是插到了音频前面"。
        self.events: list[tuple[str, Any]] = []

    async def send_json(self, obj: dict) -> None:
        self.json.append(obj)
        self.events.append(("json", obj))

    async def send_bytes(self, payload: bytes) -> None:
        self.audio.append(payload)
        self.events.append(("audio", payload))


class _FakeSession:
    """只实现 _ReplySpeaker 真正用到的那几个成员。"""

    def __init__(self) -> None:
        self.session_id = "test-guard"
        self.ws = _FakeWS()
        self._abort = False
        self.state = "thinking"
        self.protocol_version = 1
        self.dev = None

    async def _send_json(self, obj: dict) -> None:
        await self.ws.send_json(obj)

    async def _send_audio(self, payload: bytes) -> None:
        await self.ws.send_bytes(payload)


def _install_fakes(pcm_chunks: list[bytes]) -> None:
    """把 TTS / 音色 / Opus 编码换成可控的假实现（不碰网络、不碰 libopus）。"""
    asr_tts.pick_voice = lambda db, dev: "fake-voice"          # type: ignore[assignment]

    def _fake_tts_stream(db, sentence, *, voice="", speed=1.0):
        for chunk in pcm_chunks:
            yield chunk

    asr_tts.tts_stream = _fake_tts_stream                      # type: ignore[assignment]
    # 本机可能没有 libopus。这里测的是"该发的帧有没有发"，不是编码质量，
    # 所以直接返回定长假包 —— 用真编码器会让断言依赖 CI 装没装 libopus0。
    opus_codec.encode_opus = lambda pcm, sr, ch=1: b"\xAA" * 12  # type: ignore[assignment]


async def _speak(text: str, pcm_chunks: list[bytes] | None = None,
                 flush: bool = True) -> tuple[_FakeSession, _ReplySpeaker]:
    _install_fakes(pcm_chunks or [b"\x00" * 2880])
    s = _FakeSession()
    sp = _ReplySpeaker(s)
    await sp.push_text(text)
    if flush:
        await sp.flush_rest()
    # ⚠️ 2026-10-22：音频帧现在先进节流器、由独立协程按 60ms 一帧发，
    # 不再在 _stream_sentence 里 await socket。所以 push_text 返回时帧
    # **还没走下 socket** —— 不 drain 就断言 ws.audio 是假红（测的是
    # "队列里有帧"，不是"帧发出去了"）。真实调用链里承担这件事的是
    # finish()（它 wait_idle 之后才发 tts stop）。
    await sp.drain()
    return s, sp


# ================================================================ T1 过滤表
def t1_filter_table() -> None:
    print("\n── T1 可播性判定：该拦的拦住，该放的一个都不能误伤 ──")
    block = [
        ("老人查天气，我要调用 get_weather 工具。", "真机事故原句"),
        ("我要调用天气工具查一下。", "tool_talk"),
        ("老人问天气，我先查一下接口。", "third_person"),
        ("我先调用 API 获取数据。", "API"),
        ("让我先 createReminder 一下。", "camelCase"),
        ('{"name": "get_weather"}', "JSON 片段"),
        ("老人想知道天气，我需要先查询。", "自我规划"),
        ("我调用一下 json 接口", "json"),
        ("正在执行 工具调用 流程。", "工具调用"),
    ]
    for text, why in block:
        reason = _speech_block_reason(_speech_clean(text))
        check(bool(reason), f"T1 应拦（{why}）：{text!r}", reason or "未拦")

    allow = [
        "平顶山今天晴，最高十八度，出门不冷。",
        "我帮您记一下，明天早上八点提醒您吃药。",
        "您问的是平顶山吗？",
        "今天PM2.5有点高，出门戴个口罩。",     # 拉丁字母但非代码形状
        "我查查啊，您稍等。",                   # 面向老人的确认语，必须保留
        "老人家用这个挺方便。",                 # 有"老人"但不是第三人称叙述
        "现在是下午三点二十。",
        "外面风大，您把外套穿上。",
    ]
    for text in allow:
        reason = _speech_block_reason(_speech_clean(text))
        check(not reason, f"T1 应放：{text!r}", reason or "")

    # Markdown 噪音必须被剥掉，但内容要保住（不能连带把整句丢掉）
    check(_speech_clean("**今天晴**，`十四度`。") == "今天晴，十四度。",
          "T1 Markdown 强调符与反引号被剥掉",
          _speech_clean("**今天晴**，`十四度`。"))


# ================================================================ T2 播报
def t2_speaker() -> None:
    print("\n── T2 规划语不播：不发声、不发 sentence_start、计入 dropped ──")

    async def _run() -> None:
        s, sp = await _speak("老人查天气，我要调用 get_weather。")
        kinds = [o.get("state") for o in s.ws.json if o.get("type") == "tts"]
        check(not s.ws.audio, "T2 被拦句子不产生任何音频帧",
              f"{len(s.ws.audio)} 帧")
        check("sentence_start" not in kinds,
              "T2 被拦句子不发 sentence_start（设备屏幕也不会显示）", str(kinds))
        check(sp.dropped == 1, "T2 dropped 计数为 1", str(sp.dropped))
        check(sp.dropped_reasons == ["code_ident"],
              "T2 拦截原因被记录（供埋点归因）", str(sp.dropped_reasons))
        check(sp.sentence_count == 0, "T2 已播句数为 0", str(sp.sentence_count))

        # 正常回复必须照常播
        s2, sp2 = await _speak("平顶山今天晴，十八度。")
        starts = [o for o in s2.ws.json if o.get("state") == "sentence_start"]
        check(len(starts) == 1 and starts[0]["text"] == "平顶山今天晴，十八度。",
              "T2 正常回复照常播，text 原样下发",
              str(starts[:1]))
        check(bool(s2.ws.audio), "T2 正常回复产生音频帧", f"{len(s2.ws.audio)} 帧")
        check(sp2.tts_first_ms >= 0, "T2 首帧时刻被记录",
              f"{sp2.tts_first_ms}ms")

    asyncio.run(_run())


# ================================================================ T3 尾巴
def t3_tail() -> None:
    print("\n── T3 缓冲区尾巴：没有句末标点的那段也必须念出来 ──")

    async def _run() -> None:
        # 关键回归：整段没有 。！？ —— 旧实现里 push_text 一句都切不出来，
        # 而 _run_turn 不 flush，老人听到的是**彻底静音**。
        s, sp = await _speak("今天晴，十八度", pcm_chunks=[b"\x00" * 2880])
        starts = [o for o in s.ws.json if o.get("state") == "sentence_start"]
        check(len(starts) == 1, "T3 无句末标点时 flush_rest 把它念出来",
              str([o.get("text") for o in starts]))

        # 只剩标点不能合成（TTS 对单个"。"会念出怪声）
        s2, sp2 = await _speak("好。", pcm_chunks=[b"\x00" * 2880])
        s3, sp3 = await _speak("。", pcm_chunks=[b"\x00" * 2880])
        check(sp3.sentence_count == 0 and not s3.ws.audio,
              "T3 纯标点尾巴不合成", f"句数={sp3.sentence_count}")
        check(sp2.sentence_count == 1, "T3 「好。」仍会念（不是纯标点）",
              f"句数={sp2.sentence_count}")

    asyncio.run(_run())


# ================================================================ T4 切句
def t4_split() -> None:
    print("\n── T4 增量切句：标点留在前一句，短碎片不切 ──")
    head, rest = _split_first_sentence("您先吃药。然后休息。")
    check(head == "您先吃药。" and rest == "然后休息。",
          "T4 切出第一句，标点留在前句", f"{head!r} / {rest!r}")
    head2, rest2 = _split_first_sentence("好。然后")
    check(head2 == "" and rest2 == "好。然后",
          "T4 少于 4 字的碎片不切（避免 TTS 爆破音）", f"{head2!r} / {rest2!r}")
    head3, _ = _split_first_sentence("正在生成回复")
    check(head3 == "", "T4 无句末标点时返回空（交给 flush_rest）", repr(head3))


# ================================================================ T5 音频格式
def t5_audio_sniff() -> None:
    print("\n── T5 TTS 返回体格式判别：mp3 / float32 不能再被当 PCM 送出去 ──")
    import math
    import struct

    from app.voice.asr_tts import (
        _ensure_pcm,
        _float32_to_s16,
        _looks_like_float32,
        _sniff_audio,
        VoiceUpstreamError,
    )

    # 造一段"像语音"的 int16：正弦波 + 抖动，避免全零被当成静音特例
    n = 4096
    pcm16 = struct.pack(
        f"<{n}h", *[int(12000 * math.sin(i / 12.0)) for i in range(n)])

    check(_sniff_audio(pcm16) == "pcm", "T5 裸 PCM 判为 pcm", _sniff_audio(pcm16))
    check(_sniff_audio(b"RIFF" + b"\x00" * 40 + pcm16) == "wav",
          "T5 RIFF 头判为 wav")
    check(_sniff_audio(b"ID3\x04\x00" + b"\x00" * 32) == "mp3",
          "T5 ID3 判为 mp3")
    check(_sniff_audio(b"\xff\xfb\x90\x64" + b"\x00" * 32) == "mp3",
          "T5 MPEG 帧同步判为 mp3")
    check(_sniff_audio(b"\xff\xf1\x50\x80" + b"\x00" * 32) == "aac",
          "T5 ADTS 判为 aac（layer 位为 0）")
    check(_sniff_audio(b"OggS\x00\x02" + b"\x00" * 26) == "ogg", "T5 判为 ogg")
    check(_sniff_audio(b"fLaC\x00\x00" + b"\x00" * 26) == "flac", "T5 判为 flac")

    # ★ 核心断言：真实 int16 语音绝不能被误判成 float32
    check(not _looks_like_float32(pcm16),
          "T5 ★ 真实 int16 语音不会被误判为 float32（误判会直接毁掉正常音频）")

    # 造一段 float32（正弦，幅度 0.5）
    f32 = struct.pack(
        f"<{n}f", *[0.5 * math.sin(i / 12.0) for i in range(n)])
    check(_looks_like_float32(f32), "T5 float32 被正确识别")
    s16 = _float32_to_s16(f32)
    check(len(s16) == n * 2, "T5 float32→int16 长度减半", f"{len(s16)} 字节")
    peak = max(abs(v) for v in struct.unpack(f"<{n}h", s16))
    check(abs(peak - 16383) < 200, "T5 float32→int16 幅度正确（0.5→约 16383）",
          f"峰值 {peak}")

    # _ensure_pcm 端到端
    out = _ensure_pcm(b"RIFF" + b"\x00" * 40 + pcm16, "http://x", "测试")
    check(out == pcm16, "T5 _ensure_pcm 剥掉 wav 头且内容不变")
    out2 = _ensure_pcm(f32, "http://x", "测试")
    check(len(out2) == n * 2, "T5 _ensure_pcm 自动转换 float32", f"{len(out2)} 字节")

    raised = ""
    try:
        _ensure_pcm(b'{"error":"quota"}', "http://x", "测试")
    except VoiceUpstreamError as exc:
        raised = str(exc)[:40]
    check(bool(raised), "T5 JSON 错误体被拒（不当音频发出去）", raised)


# ================================================================ T7 节流接入
def t7_pacing_order() -> None:
    print("\n── T7 节流器接入：tts stop 不许插到音频前面 ──")

    async def _run() -> None:
        # 12 帧 > 预缓冲 5 帧 ⇒ finish() 被调用时队列里必然还有帧。
        # 旧实现"能发多快发多快"，调 finish() 时帧早已发完；接入节流器后
        # 队列是异步排空的，finish() 必须等它 —— 不等的话 stop 会插到音频
        # 前面，表现是每句话最后几个字被吃掉，而日志一切正常。
        _install_fakes([b"\x00" * 2880] * 6)
        s = _FakeSession()
        sp = _ReplySpeaker(s)
        await sp.push_text("今天晴，十八度。明天下雨，记得带伞。")
        await sp.flush_rest()
        await sp.finish()
        await sp.close()

        ev = s.ws.events
        kinds = [k for k, _ in ev]
        check(bool(ev) and kinds[-1] == "json"
              and ev[-1][1].get("state") == "stop",
              "T7 ★ tts stop 是最后一条消息（没有插到音频前面）",
              str(ev[-1] if ev else None))
        check(kinds.count("audio") == 12,
              "T7 十二帧全部发出（节流不丢帧、不重发）",
              str(kinds.count("audio")))
        starts = [i for i, (k, v) in enumerate(ev)
                  if k == "json" and v.get("state") == "sentence_start"]
        check(len(starts) == 2, "T7 两句各发一次 sentence_start", str(starts))
        for idx, pos in enumerate(starts):
            nxt = starts[idx + 1] if idx + 1 < len(starts) else len(ev)
            between = [k for k, _ in ev[pos + 1:nxt]]
            check("audio" in between,
                  f"T7 第 {idx + 1} 句的 sentence_start 之后有音频帧",
                  str(between[:3]))
        check(sp.tts_first_ms > 0, "T7 首帧时刻仍被记录",
              f"{sp.tts_first_ms}ms")

    asyncio.run(_run())


# ================================================================ T6 调用点
def t6_call_arity() -> None:
    print("\n── T6 内部方法调用点：位置参数个数必须对得上 ──")
    import ast

    src_path = Path(__file__).resolve().parents[1] / "server" / "app" / "voice" / "session.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))

    # 只收**定参**方法（有 *args 的不参与比较），且只关心私有方法。
    # ⚠️ 必须同时收 AsyncFunctionDef —— 这个文件里绝大多数方法是 async def，
    # 只查 FunctionDef 会得到一个空的签名表，然后"因为查不到所以全绿"。
    # 第一版就是只查了 FunctionDef，对着 HEAD 里那个真实 bug 报了"无错配"。
    # 这就是本项目最忌讳的假绿：断言跑了、绿了、但什么都没测。
    sigs: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name.startswith("_") and node.args.vararg is None:
            total = len(node.args.posonlyargs) + len(node.args.args)
            sigs.setdefault(node.name, []).append(total - 1)   # 去掉 self

    problems: list[str] = []
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute)
                and isinstance(fn.value, ast.Name) and fn.value.id == "self"):
            continue
        if fn.attr not in sigs:
            continue
        checked += 1
        n_pos = len(node.args)
        if not any(n_pos <= n for n in sigs[fn.attr]):
            problems.append(
                f"{fn.attr} 传了 {n_pos} 个位置参数，形参最多 {max(sigs[fn.attr])} 个"
                f"（第 {node.lineno} 行）")

    # 这两个门槛一起防"空跑"：签名表非空 + 真的扫到了调用点。
    check(len(sigs) >= 10, "T6 签名表非空（含 async def）", f"{len(sigs)} 个私有方法")
    check(checked >= 5, "T6 真的扫到了调用点（扫不到等于没测）", f"{checked} 处")
    check(not problems, "T6 所有 self.xxx() 位置参数个数与定义一致",
          "；".join(problems))


def main() -> int:
    print("=" * 64)
    print("可播性过滤 + 尾巴补播 + TTS 返回体格式 单元断言")
    print("=" * 64)
    t1_filter_table()
    t2_speaker()
    t3_tail()
    t4_split()
    t5_audio_sniff()
    t6_call_arity()
    t7_pacing_order()
    print("\n" + "=" * 64)
    print(f"通过 {_passed} 项，失败 {_failed} 项")
    if _failed:
        print("失败清单：")
        for name in _failures:
            print("  -", name)
    print("结论：", "**绿**" if _failed == 0 else "**红**")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
