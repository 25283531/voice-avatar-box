"""`tts_stream` 分片契约守卫（2026-10-20「连上但一点声音都没有」事故的永久回归）。

用法：
    cd server && python ../tools/smoke_tts_stream.py

事故是什么
----------
``server/app/voice/asr_tts.py::tts_stream()`` 的 edge / volcengine 两个分支曾写成::

    for chunk in tts(db, sentence, ...):
        yield chunk

而 ``tts()`` 返回的是**整段 bytes**：``for-in`` 逐字节迭代 ⇒ ``chunk`` 是 ``int``
⇒ 上层 ``session.py::_stream_sentence`` 的 ``carry += chunk`` 抛
``TypeError: can't concat int to bytes``。设备**连得上、收得到 LLM 文本、一个音都没有**。

配套的「假绿」缺陷：``_ReplySpeaker`` 的通用 except 分支忘了 ``_tts_ok = False``，
``finish()`` 照样打印「播报完成（2 句，音频已发）」，把事故藏了两周。

为什么必须进程内直调
--------------------
历史教训：所有涉及 ``tts_stream`` 的用例都挂在 edge / volcengine / libopus 这些
**可选依赖**上，CI 与本机各自 SKIP ⇒ 事故从缝隙里穿过去。所以本脚本：

* 不打 uvicorn、不连网络、不碰真实 DB；
* monkeypatch ``app.llm_gateway.resolve_row`` 返回假配置行（edge / volcengine 各一条）；
* monkeypatch ``asr_tts.tts`` 返回定长 bytes（模拟"整段合成完成"）；
* monkeypatch ``session.opus_codec.encode_opus``，彻底摆脱 libopus。

**任何环境下都不许 SKIP** —— SKIP 就是这次事故的成因本身。

断言为何有鉴别力
----------------
不是"源码里有没有写 yield tts(...)"这种静态匹配，而是复现**上层真实消费方式**：
逐分片 ``isinstance(c, bytes)`` + ``carry += c`` 拼接 + 总长等于输入长度。把
``tts_stream`` 改回 ``for chunk in tts(...)`` 后，第一条断言就红（``int`` 不是
``bytes``），第二条直接抛 ``TypeError``。会话路径则断言 edge/volcengine 两条
配置下**真的发出了音频包**且 ``_tts_ok is True``，以及 TTS 抛异常后
``_tts_ok is False`` + ``finish()`` 日志写「音频不可用」（双向，避免恒 False 的假绿）。

变异实证（改完请自己跑一遍，确认脚本会红；还原用 cp + cmp，别用 git checkout）
--------------------------------------------------------------------------
1. ``asr_tts.tts_stream`` 的 edge 分支改回 ``for chunk in tts(...): yield chunk``
   → 必须红（「每个分片都是 bytes」/「carry 拼接」/「edge 会话路径发出音频」）。
2. 删掉 ``session._speak_sentence`` 通用 except 里的 ``self._tts_ok = False``
   → 必须红（「TTS 抛异常后 _tts_ok 为 False」/「finish 日志写音频不可用」）。
**只跑绿的那一半等于没验证。**
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from app import llm_gateway                                    # noqa: E402
from app.voice import asr_tts                                  # noqa: E402
from app.voice import session as session_mod                   # noqa: E402
from app.voice.session import VoiceSession, _ReplySpeaker      # noqa: E402

#: 假 tts() 的返回值：定长、**不含 0x00**（逐字节迭代时不会有"假值被跳过"的干扰）、
#: 长度刻意不是一帧的整数倍（顺带覆盖收尾补零那条路径）。
FRAME_BYTES = 2880                      # 24kHz/60ms/mono S16LE
FAKE_PCM = bytes((i % 251) + 1 for i in range(FRAME_BYTES * 2 + 7))

#: 两条**同源缺陷**的配置行（只测 edge 会漏掉火山那一半）。
CASES = (
    ("edge", {"provider": "edge", "api_style": "edge"}),
    ("volcengine", {"provider": "volcengine", "api_style": "volcengine"}),
)

#: 兜底用例用的单条配置（整段式风格，掉一条不影响另一条）
SPEC_EDGE = {"provider": "edge", "api_style": "edge"}

_passed = 0
_failed = 0
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passed, _failed
    if ok:
        _passed += 1
        mark = "PASS"
    else:
        _failed += 1
        _failures.append(name)
        mark = "FAIL"
    print(f"  [{mark}] {name}" + (f" —— {detail}" if detail else ""))


def make_row(kind: str, spec: dict[str, str], *, name: str = "") -> Any:
    return SimpleNamespace(
        name=name or f"smoke-{kind}", provider=spec["provider"],
        api_style=spec["api_style"],
        endpoint="", api_key="", model="", timeout_sec=5.0, params={},
    )


class _WS:
    """最小 WebSocket 桩：只收集下行，不做任何 IO。"""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.audio: list[bytes] = []

    async def send_text(self, text: str) -> None:
        self.texts.append(text)

    async def send_bytes(self, payload: bytes) -> None:
        self.audio.append(payload)


class _DB:
    """SessionLocal() 的替身：既支持 ``with`` 也支持 ``.close()``。"""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return False

    def close(self) -> None:
        return None


class _LogSink(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def build_speaker() -> tuple[VoiceSession, _ReplySpeaker]:
    """绕过构造器造一个会话桩，但 **_speak_sentence / _stream_sentence 走真实代码**。"""
    sess = VoiceSession.__new__(VoiceSession)
    sess.ws = _WS()
    sess.protocol_version = 1
    sess.session_id = "smoke-tts-stream"
    sess.dev = None
    sess.state = "idle"
    sess._abort = False
    sess._turn_task = None

    spk = _ReplySpeaker(sess)
    spk._voice_loaded = True     # 跳过 pick_voice 的 DB 依赖
    spk._voice = ""
    return sess, spk


class _Patch:
    """一次性的属性替换，退出时**必定**还原（避免用例之间串味）。"""

    def __init__(self) -> None:
        self._saved: list[tuple[Any, str, Any]] = []

    def set(self, obj: Any, attr: str, value: Any) -> None:
        self._saved.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def restore(self) -> None:
        for obj, attr, old in reversed(self._saved):
            setattr(obj, attr, old)
        self._saved.clear()


def _consume_like_session(gen) -> tuple[list[Any], bytes, str]:
    """完全照抄 session.py::_stream_sentence 的消费方式：``carry += chunk``。"""
    chunks: list[Any] = []
    carry = b""
    err = ""
    try:
        for chunk in gen:
            chunks.append(chunk)
            carry += chunk       # ← 事故现场：chunk 是 int 时这里抛 TypeError
    except Exception as exc:      # noqa: BLE001  故意把异常带回去做断言
        err = f"{type(exc).__name__}: {exc}"
    return chunks, carry, err


def test_direct_contract(kind: str, spec: dict[str, str], holder: dict) -> None:
    """第 1 层：直接消费 tts_stream 生成器（不经过会话，纯分片契约）。"""
    holder["row"] = make_row(kind, spec)
    holder["plan"] = [("tts", holder["row"])]
    calls: list[str] = []

    def fake_tts(db: Any, sentence: str, *, voice: str = "", speed: float = 1.0,
                 row: Any = None) -> bytes:
        # ⚠️ 必须收 ``row=``：整段式分支现在走 _tts_atomic_with_fallback，
        # 它会明确指定"这次试哪一行"（否则兜底循环永远被 resolve_row 拉回第一条）。
        calls.append(sentence)
        return FAKE_PCM

    patch = _Patch()
    patch.set(asr_tts, "tts", fake_tts)
    try:
        chunks, carry, err = _consume_like_session(
            asr_tts.tts_stream(None, "今天天气不错。", voice=""))
    finally:
        patch.restore()

    check(f"[{kind}] 分发生效（走了 tts() 而不是 OpenAI 那条路）",
          calls == ["今天天气不错。"], f"calls={calls!r}")
    check(f"[{kind}] 生成器不抛异常", err == "", err)
    check(f"[{kind}] 至少吐出一个分片", len(chunks) >= 1, f"n={len(chunks)}")

    bad = [i for i, c in enumerate(chunks) if not isinstance(c, bytes)]
    check(f"[{kind}] 每个分片都是 bytes（不是 int）",
          not bad, f"非 bytes 分片下标={bad[:5]} 类型={[type(chunks[i]).__name__ for i in bad[:3]]}")
    # ↑ 变异 1（改回 for chunk in tts(...)）时：下标 0 就是 int，这条立刻红。

    total = sum(len(c) for c in chunks if isinstance(c, bytes))
    check(f"[{kind}] 分片总长等于假 tts() 返回值长度",
          total == len(FAKE_PCM), f"sum={total} 期望={len(FAKE_PCM)}")
    check(f"[{kind}] 按 session.py 方式拼接后字节完全一致",
          carry == FAKE_PCM, f"len(carry)={len(carry)}")


async def _speak_once(spec: dict[str, str], holder: dict, tts_impl) -> tuple[VoiceSession, _ReplySpeaker, _LogSink]:
    """跑一次真实 ``_speak_sentence``（含 finish 收尾日志），返回现场供断言。"""
    sess, spk = build_speaker()
    sink = _LogSink()
    logger = logging.getLogger("app.voice.session")
    old_level = logger.level
    logger.addHandler(sink)
    logger.setLevel(logging.INFO)
    holder["row"] = make_row(spec["provider"], spec)
    holder["plan"] = [("tts", holder["row"])]
    patch = _Patch()
    patch.set(asr_tts, "tts", tts_impl)
    try:
        try:
            await spk._speak_sentence("今天天气不错。")
        finally:
            await spk.finish()
    finally:
        patch.restore()
        logger.removeHandler(sink)
        logger.setLevel(old_level)
    return sess, spk, sink


def test_session_path(kind: str, spec: dict[str, str], holder: dict) -> None:
    """第 2 层：走真实 ``VoiceSession._speak_sentence``（老人听到声音的那条路）。"""
    def good_tts(db: Any, sentence: str, *, voice: str = "", speed: float = 1.0,
                 row: Any = None) -> bytes:
        return FAKE_PCM

    def bad_tts(db: Any, sentence: str, *, voice: str = "", speed: float = 1.0,
                row: Any = None) -> bytes:
        raise RuntimeError("模拟上游 TTS 炸掉（smoke）")

    # ---- 正常：必须真的有音频包发出去，_tts_ok 必须是 True ----
    patch = _Patch()
    patch.set(session_mod, "SessionLocal", _DB)
    patch.set(session_mod.opus_codec, "encode_opus", lambda frame, rate: b"PKT")
    try:
        sess, spk, sink = asyncio.run(_speak_once(spec, holder, good_tts))
    finally:
        patch.restore()

    # 变异 1 时：carry += int 抛 TypeError → 走 except → _tts_ok False、音频 0 包。
    check(f"[{kind}] 会话路径：正常 TTS 后 _tts_ok 为 True",
          spk._tts_ok is True, f"_tts_ok={spk._tts_ok!r}")
    check(f"[{kind}] 会话路径：真的发出了音频包（不是静音）",
          len(sess.ws.audio) >= 2, f"audio 包数={len(sess.ws.audio)}")
    check(f"[{kind}] 会话路径：句数计到 1", spk.sentence_count == 1,
          f"sentence_count={spk.sentence_count}")
    check(f"[{kind}] 会话路径：finish 日志写「音频已发」",
          any("音频已发" in line for line in sink.lines),
          f"日志尾部={sink.lines[-1] if sink.lines else '(空)'}")

    # ---- 异常：_tts_ok 必须 False，且 finish 不许再谎报「音频已发」----
    patch2 = _Patch()
    patch2.set(session_mod, "SessionLocal", _DB)
    patch2.set(session_mod.opus_codec, "encode_opus", lambda frame, rate: b"PKT")
    try:
        sess2, spk2, sink2 = asyncio.run(_speak_once(spec, holder, bad_tts))
    finally:
        patch2.restore()

    # 变异 2（删掉 except 里的 _tts_ok = False）时：这两条一起红。
    check(f"[{kind}] 会话路径：TTS 抛异常后 _tts_ok 为 False",
          spk2._tts_ok is False, f"_tts_ok={spk2._tts_ok!r}")
    check(f"[{kind}] 会话路径：异常时没有音频包", len(sess2.ws.audio) == 0,
          f"audio 包数={len(sess2.ws.audio)}")
    check(f"[{kind}] 会话路径：finish 日志写「音频不可用」（不再假绿）",
          any("音频不可用" in line for line in sink2.lines)
          and not any("音频已发" in line for line in sink2.lines),
          f"日志尾部={sink2.lines[-1] if sink2.lines else '(空)'}")


def test_atomic_fallback(holder: dict) -> None:
    """第 3 层：第一条配置炸掉时必须换下一条，而不是让整条链路静默。

    回归对象（2026-10-21 真机事故）：管理员按预置加了一条「腾讯云 TTS」
    （priority 6，抢在 Edge 的 10 前面）但密钥/地址没配全。``tts()`` 只取
    resolve_row 的第一条、**没有按序兜底** ⇒ 库里那条好好的 Edge 永远轮不到，
    老人听到彻底静默，而 finish() 照打「音频已发」。

    这套用例有鉴别力：把 ``_tts_atomic_with_fallback`` 改回
    ``return tts(db, ...)``（单条），下面三条一起红。
    """
    good = make_row("edge", SPEC_EDGE, name="好的 Edge")
    bad = make_row("edge", SPEC_EDGE, name="坏掉的那条")

    def picky_tts(db: Any, sentence: str, *, voice: str = "", speed: float = 1.0,
                  row: Any = None) -> bytes:
        # ``row is None`` 也算坏：这正好对应"变异版直接调 tts(db, ...) 而不指定行"
        # —— 兜底逻辑若被删掉，这里必抛，第一条断言立刻红（不留假绿口子）。
        if row is None or getattr(row, "name", "") == "坏掉的那条":
            raise asr_tts.VoiceUpstreamError("模拟上游 401（smoke）")
        return FAKE_PCM

    sink = _LogSink()
    logger = logging.getLogger("app.voice.asr_tts")
    old_level = logger.level
    logger.addHandler(sink)
    logger.setLevel(logging.INFO)
    patch = _Patch()
    patch.set(asr_tts, "tts", picky_tts)
    holder["row"] = bad
    holder["plan"] = [("tts", bad), ("tts", good)]
    try:
        audio = asr_tts._tts_atomic_with_fallback(None, "今天天气不错。")
        err = ""
    except Exception as exc:  # noqa: BLE001
        audio, err = b"", f"{type(exc).__name__}: {exc}"
    finally:
        patch.restore()
        logger.removeHandler(sink)
        logger.setLevel(old_level)

    check("[兜底] 第一条失败时仍拿到了音频（换了下一条）",
          err == "" and audio == FAKE_PCM, f"err={err} len={len(audio)}")
    check("[兜底] 失败的那条留下了告警日志",
          any("按序换下一条" in line for line in sink.lines),
          f"日志={sink.lines}")
    check("[兜底] 真正换了行之后写了「回落到」",
          any("回落到" in line for line in sink.lines),
          f"日志={sink.lines}")

    # ---- 反向：全部失败必须把错误抛出去（不许静默返回空音频）----
    patch2 = _Patch()
    patch2.set(asr_tts, "tts", picky_tts)
    holder["plan"] = [("tts", bad), ("tts", bad)]
    try:
        asr_tts._tts_atomic_with_fallback(None, "今天天气不错。")
        raised = ""
    except Exception as exc:  # noqa: BLE001
        raised = type(exc).__name__
    finally:
        patch2.restore()

    check("[兜底] 全部候选都失败时抛 VoiceUpstreamError（不返回空音频）",
          raised == "VoiceUpstreamError", f"raised={raised!r}")


def main() -> int:
    # 全局打桩：配置行由用例通过 holder 决定，永不触碰真实 DB / 网络。
    holder: dict[str, Any] = {"row": None, "plan": []}
    patch = _Patch()
    patch.set(llm_gateway, "resolve_row", lambda db, usage: holder["row"])
    # 整段式分支走的是 resolve_plan（按序兜底需要**全部**候选，不是第一条）
    patch.set(llm_gateway, "resolve_plan", lambda db, usage: list(holder["plan"]))

    print("── 1. tts_stream 分片契约（进程内直调，零可选依赖）──")
    for kind, spec in CASES:
        test_direct_contract(kind, spec, holder)

    print("── 2. 会话真实代码路径：有声音 / 失败不假绿 ──")
    for kind, spec in CASES:
        test_session_path(kind, spec, holder)

    print("── 3. 整段式 TTS 按序兜底（一条坏配置不许打死整条链路）──")
    test_atomic_fallback(holder)

    patch.restore()

    print(f"通过 {_passed} / 失败 {_failed}")
    if _failed:
        print("失败项：" + ", ".join(_failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
