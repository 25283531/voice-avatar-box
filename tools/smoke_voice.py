"""自研语音链路冒烟：WS 协议 + Opus 编解码 + 会话编排 + OTA + 埋点
+ 工具调用（提醒）+ 流式 TTS 首句延迟 + 老人语速向 VAD。

覆盖（与任务书断言清单一一对应）
------------------------------
1. **OTA 分流**：未绑定 MAC 只拿激活码、**不带 websocket 段**；绑定后才拿
   ws url + token（且 token 本地 decode 校验 typ/sub）；
   **firmware 段**：未配 ``ELDER_FIRMWARE_*`` 时两条路径都不返回（默认行为
   不变），配齐后两条路径都返回且值正确（版本比较由固件自己做）；
2. **WS 鉴权**：无 token / 垃圾 token → 被关（4401）；token 有效但
   Device-Id 不符 → 4001；
3. **握手**：服务端 hello 的 transport / session_id / sample_rate=24000；
4. **完整一轮**：listen start → 二进制帧 → listen stop → stt / tts start /
   tts stop；有 libopus 时做**编解码 round-trip**（发出去的帧解回来非全静音）；
5. **埋点**：model_usage（source=voice / usage=chat / total_tokens=155 /
   device_id）+ chat_usage + Interaction 三条都落库；
6. **取消会员制**：已绑定设备把时长上限压到 0 仍**照常调 LLM 并播报**
   （反向不变量，见 :func:`t_quota` —— 原来测的是「配额用尽不调 LLM」）;
7. **打断**：播报中发 abort → 后续 sentence_start / 二进制帧不再来；
8. **降级**：禁用 TTS → 仍收到 stt + tts start/stop + sentence_start，
   无二进制帧；禁用全部 LLM → 收到"未配置大模型" alert；
9. **空识别**：假 ASR 返回空 → 不调 LLM（chat 计数不变）。
10. **二进制帧封装** v1/v2/v3 + 服务端静音兜底 + 畸形帧不打挂会话；
11. **libopus 缺失契约**：抛 OpusUnavailableError 而不是静默返回空；
12. **工具调用（2026-10-17 新增）**：老人说"明天早上八点提醒我吃药" →
    LLM function calling → 服务端写 reminders 表 → 口语确认；歧义时间
    （"三点"）**不建提醒**而是反问；delete/toggle；工具循环轮数上限；
    OpenAI / Ollama / Anthropic / Gemini 四协议各自跑通建提醒；
13. **流式 TTS 首句延迟（新增）**：LLM 分片流式 + TTS 分片流式时，
    第一帧音频必须早于"LLM 整段生成完"与"第一句 TTS 整段合成完"，
    并打印实测毫秒数；
14. **老人语速向 VAD（新增）**：句中停顿 1.8s 不提前断句（自适应阈值）、
    轻声（RMS 0.007，旧门限 0.01 会整句丢失）可正常 finalize、
    迟滞（软语音期限内不掉出语音态）+ 期限（底噪不会让会话挂死）。
15. **本地 ASR 协议（2026-10-22 新增）**：管理页下拉选「本地 ASR
    （FunASR/SenseVoice）」保存后那条配置（``api_style=local_asr``，
    priority 压过 OpenAI 兼容行）→ 请求真打 ``POST {endpoint}/asr``
    （multipart ``audio``、无 model 无密钥），stt 采用其返回值，
    且 OpenAI 兼容那条一次都没被调用。

⚠️ 本机（Windows）没有 libopus 时怎么算
--------------------------------------
**第 4 条的音频 round-trip 断言会打印 SKIP 并跳过**，协议级断言照常跑。
这不是"假装全绿"：SKIP 的断言会计入 skipped 计数并在结尾单独列出，
CI（ubuntu，team-lead 已装 libopus0）里它必须真跑过。判定门限是
``failed == 0``，不是 ``passed == total``。

同理，第 13 条（首句延迟）与第 14 条（老人 VAD）用**可注入的假帧/替身
函数**在本机跑：opus 的 decode 换成按脚本返回指定 RMS 的 PCM、encode 换成
定长假包。替身只替换两个**叶子函数**，封帧/剥帧/VAD/断句/播报时序这些
真正的协议代码全都真跑。断言名与 CI 的 round-trip 断言分开命名，
输出里能一眼看出哪些覆盖了替身。

三个基础设施决策
----------------
1. **进程内起服务**（uvicorn 后台线程）：voice 路由还没接进 main.py
   （集成由 team-lead 做），本脚本自己 ``attach(app)``；
2. **假上游是真 HTTP**（threading + http.server，端口 bind 0 自选）：
   要验的正是"从厂商响应提取文本/音频"这条链路，mock 掉客户端等于
   把被测点 mock 了。2026-10-17 起它会按请求体/路径区分
   OpenAI / Ollama / Anthropic / Gemini 四种报文，并按脚本返回
   **流式（SSE/NDJSON）**或整段响应、文本或 tool_calls；
3. **websockets 同步客户端**：``websockets.sync.client.connect``，
   不必为测试再引入 asyncio 的复杂度。

用法::

    python tools/smoke_voice.py [--port 8119]
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import struct
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"

# 端口统一由 tools/smoke_ports.py 托管（唯一真源）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_ports import (  # 见 smoke_ports.py 模块 docstring
    guard_or_die,
    guard_port,
    release_port,
    resolve_port,
)

# ⚠️ **本地请求绝不走代理**（与全仓冒烟一致）：本机设了 http_proxy，
# urllib 默认会把 127.0.0.1 的请求绕去代理，表现为莫名的超时且零断言失败。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
DB_PATH = DB_DIR / f"elder_smoke_voice_{os.getpid()}.db"
SERVER_LOG = DB_PATH.with_name(DB_PATH.stem + "_server.log")
if DB_PATH.exists():
    DB_PATH.unlink()

PORT = resolve_port("smoke_voice")
BASE = f"http://127.0.0.1:{PORT}"
WS_URL = f"ws://127.0.0.1:{PORT}/api/voice/v1/ws"

MAC_BOUND = "AA:BB:CC:DD:EE:01"        # 播种时置 owner，用来拿 ws 通道
MAC_UNBOUND = "AA:BB:CC:DD:EE:02"      # 从未出现过，用来拿激活码

# ---------------------------------------------------------------- WS 兼容层
#
# 为什么需要这一层（本文件有 12 处连 WS，全部要带认证头）：
#   1. `websockets.connect` 的**请求头参数名在新版改名**了：
#      旧名 `extra_headers` → 新名 `additional_headers`。
#   2. 更隐蔽的一层：即便用对了新名，底层 `loop.create_connection()`
#      在 **Python 3.13 被移除了 extra_headers**。于是本地（elder venv =
#      Python 3.13 + websockets 17.x）会炸：
#          TypeError: create_connection() got an unexpected keyword
#                     argument 'extra_headers'
#      而 CI 是 Python 3.11 + 旧 websockets，一切正常。
#      ⇒ 症状是「本地永远红、CI 永远绿」，且错误信息完全指不到本文件。
#
# ⚠️ 判据必须用**签名探测**而不是版本号比较：实测 websockets 17.2 仍然
#    会把头传成 `extra_headers` 给已移除该参数的 asyncio ⇒ 「>=14 就用新名」
#    这条规则被证伪过一次。
#
# ⚠️ 探测必须**惰性**（函数内 import）且每次调用都重探：模块级 `websockets`
#    是下面若干处按需 import 的，模块顶部并没有它 ⇒ 在模块作用域探测会
#    NameError。顺带也不缓存：探测一次几微秒，换来「换库版本后无需重启」的
#    健壮性。
#
# 用法：把 `websockets.connect(..., extra_headers=h)` 换成
#      `ws_connect(..., headers=h)`，其余参数原样透传。
def ws_connect(url: str, headers: dict[str, str] | None = None, **kwargs):
    """`websockets.connect` 的薄包装：按运行时签名挑请求头参数名。

    刻意**不吞异常** —— 参数名选错的症状（TypeError）必须在调用处原样暴露，
    不做二次包装，否则会把「参数名对不上」伪装成「服务端拒连」。
    """
    import inspect

    import websockets

    params = inspect.signature(websockets.connect).parameters
    kw = "additional_headers" if "additional_headers" in params else "extra_headers"
    if headers is not None:
        kwargs[kw] = headers
    return websockets.connect(url, **kwargs)

FAKE_ASR_TEXT = "小暖小暖，明天早上八点提醒我吃药"
# 本地 FunASR/SenseVoice 桩的返回值（第 15 组）。与 FAKE_ASR_TEXT **故意不同**：
# 两路 ASR 回同一句话，就无法从 transcript 分辨请求到底走了哪条协议。
FAKE_LOCAL_ASR_TEXT = "本地识别，明天早上八点提醒我吃药"
FAKE_REPLY = "好的，明天早上八点我提醒您吃药。您也要记得多喝水。我陪您聊聊。"
FAKE_TOTAL_TOKENS = 155

# 假 TTS 的说话速度控制：默认每句 sleep 这么久才返回。
# 第 7 条（打断）要把它调大，才能在播报中插进 abort。
TTS_SENTENCE_DELAY_SEC = 0.0

# ---- 第 13 条（流式首句延迟）的流式参数 ----
# LLM 每个分片前 sleep 这么久：模拟"边想边吐"。三句回复 ⇒ 整段 3×delay，
# 而首句只需要第 1 个分片 —— 差距就是流式省下来的首句延迟。
LLM_CHUNK_DELAY_SEC = 0.10
# TTS 每个分片前 sleep 这么久：模拟厂商的分片合成节奏。
TTS_CHUNK_DELAY_SEC = 0.04
# 每个 TTS 分片的字节数（≥4096：客户端的 resp.read(4096) 要能立刻返回，
# 否则测到的就不是"分片延迟"而是"读满 4096 才返回"的假象）。
TTS_CHUNK_BYTES = 8192
# 一句合成分几个分片（>1 才走流式分支）。
TTS_CHUNKS_PER_SENTENCE = 5

# ---- 第 14 条（老人 VAD）的帧脚本参数 ----
# 帧时长以 config.VOICE_FRAME_DURATION_MS（60ms）计，服务端按**帧数**累计
# 静音/语音时长，与客户端发送节奏无关 —— 所以脚本用"帧数"表达时长：
#   10 帧 = 600ms，30 帧 = 1800ms，34 帧 ≈ 2040ms（> 2000ms 阈值）
LOUD_RMS = 0.2            # 正常说话（远高于任何门限）
SOFT_RMS = 0.0045         # 软语音：高于 keep、低于 enter（迟滞测试用）
QUIET_RMS = 0.007         # 轻声/远场：高于新门限 0.005、低于旧门限 0.01
NOISE_RMS = 0.0045        # 类底噪能量（grace 期限测试用）

results: list[tuple[str, bool, str]] = []
skipped: list[tuple[str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def skip(name: str, why: str) -> None:
    """记一条 SKIP。**必须带原因**，否则结尾的清单没有可读性。"""
    skipped.append((name, why))
    print(f"  [SKIP] {name}  -> {why}")


def _read_log_tail(max_bytes: int = 262144) -> str:
    try:
        return SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-max_bytes:]
    except OSError:
        return ""


def _log_has(needle: str) -> bool:
    """服务端日志里是否出现过这句话（用来断言"实际生效的阈值/计时"）。"""
    return needle in _read_log_tail()


# ================================================================ 假上游
class _UpstreamState:
    """假上游的共享可变状态（计数 + 行为开关 + 观测记录）。

    2026-10-17 起兼担"可编程厂商"的职责：``plan`` 是**按调用顺序消费**的
    脚本队列，每条描述"这一轮 LLM 该回什么"（文本 / tool_calls / 流式与否）。
    队列空时回落默认行为（整段 JSON 回 UP.reply）—— 老断言因此不受影响。
    """

    def __init__(self) -> None:
        # RLock：同一线程里会嵌套取（_serve_chat 持有锁时读 reply）
        self.lock = threading.RLock()
        self.chat_calls = 0
        self.asr_calls = 0
        self.tts_calls = 0
        self.asr_text = FAKE_ASR_TEXT
        # ---- 本地 FunASR / SenseVoice 协议（第 15 组）----
        self.asr_local_calls = 0
        self.local_asr_text = FAKE_LOCAL_ASR_TEXT
        self.last_asr_path = ""
        self.reply = FAKE_REPLY
        self.tts_enabled = True
        self.llm_enabled = True
        self.tts_delay = TTS_SENTENCE_DELAY_SEC
        # 每次 /audio/speech 返回的 PCM 时长（秒），默认 0.5
        self.tts_seconds = 0.5
        # ---- 流式 / 工具脚本 ----
        self.plan: list[dict] = []
        self.tts_chunk_delay = 0.0
        self.tts_chunk_bytes = TTS_CHUNK_BYTES
        self.tts_chunks = 1
        # ---- 观测记录（冒烟断言直接读；同一进程，时间戳可直接相比）----
        self.chat_bodies: list[dict] = []     # 请求体（留最后 20 条）
        self.chat_events: list[dict] = []     # 每次 chat 的时间线
        self.tts_events: list[dict] = []      # 每次 tts 的时间线

    def bump(self, name: str) -> int:
        with self.lock:
            setattr(self, name, getattr(self, name) + 1)
            return getattr(self, name)

    def get(self, name: str):
        with self.lock:
            return getattr(self, name)

    def pop_plan(self) -> dict:
        with self.lock:
            return self.plan.pop(0) if self.plan else {}

    def note_body(self, body: dict) -> None:
        with self.lock:
            self.chat_bodies.append(body)
            del self.chat_bodies[:-20]

    def note_event(self, kind: str, ev: dict) -> None:
        with self.lock:
            getattr(self, f"{kind}_events").append(ev)


UP = _UpstreamState()


def _pcm_bytes(n_bytes: int, freq: float = 440.0) -> bytes:
    """合成 n_bytes 字节的正弦 PCM（24kHz/mono/S16LE）。

    为什么用正弦而不是静音：静音的 RMS 是 0，而"编解码 round-trip"
    断言要的正是"解回来的东西不是全零"。正弦有稳定的可测能量。
    """
    n = n_bytes // 2
    out = bytearray()
    for i in range(n):
        v = int(12000 * math.sin(2 * math.pi * freq * i / 24000))
        out += struct.pack("<h", v)
    return bytes(out)


def _pcm_rms_bytes(n_bytes: int, rms: float, freq: float = 300.0) -> bytes:
    """合成**指定 RMS** 的 PCM（16kHz/mono/S16LE，设备上行格式）。

    正弦的 RMS = 幅度/√2，所以幅度 = rms×√2×32768。第 14 条的老人场景
    （轻声 / 软语音 / 底噪）全靠它按脚本注入能量，**不依赖任何真实音频**。
    """
    n = n_bytes // 2
    amp = max(0, min(32767, int(rms * math.sqrt(2) * 32768)))
    out = bytearray()
    for i in range(n):
        out += struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / 16000)))
    return bytes(out)


def _step_of(step: dict) -> tuple[dict | None, str, list[str] | None, float, tuple[int, int]]:
    """把一条脚本规范成 (tool, text, chunks, delay, (prompt, completion))。"""
    tool = step.get("tool") if isinstance(step.get("tool"), dict) else None
    text = str(step.get("text") or "")
    chunks = step.get("chunks") if isinstance(step.get("chunks"), list) else None
    delay = float(step.get("delay") or 0.0)
    usage = step.get("usage") if isinstance(step.get("usage"), dict) else {}
    return (tool, text, chunks, delay,
            (int(usage.get("prompt", 120)), int(usage.get("completion", 35))))


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """流式写入时客户端（语音会话）可能已经断开：那是常态，别刷 traceback。"""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class _FakeUpstream(BaseHTTPRequestHandler):
    """假上游：按路径区分四种厂商协议，按脚本返回流式/整段、文本/tool_calls。

    刻意用 ``ThreadingHTTPServer`` + ``bind 0``：端口让内核自选，
    免得又一个硬编码端口和别人撞车；多线程是因为语音链路的
    TTS/ASR 可能在后台线程里并发调（``run_in_threadpool``）。

    流式靠 **HTTP/1.0 + 连接即 EOF**（不写 Content-Length，写完关连接）：
    客户端 ``readline()/read(n)`` 因此能"到一片读一片"，首分片时间才测得准。
    """

    def log_message(self, *args):  # 静默：别往 stdout 刷访问日志
        pass

    # ---------------------------------------------------------------- 基础
    def _json(self, obj: dict, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _begin_stream(self, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()

    def _note_first(self) -> None:
        """记录"第一个正文字节写出"的时刻 —— 首 token / 首分片延迟的锚点。"""
        ev = getattr(self, "_ev", None)
        if ev is not None and ev.get("first_at") is None:
            ev["first_at"] = time.monotonic()

    def _sse(self, obj: dict) -> None:
        self._note_first()
        self.wfile.write(b"data: "
                         + json.dumps(obj, ensure_ascii=False).encode("utf-8")
                         + b"\n\n")

    def _ndjson(self, obj: dict) -> None:
        self._note_first()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")

    # ---------------------------------------------------------------- 路由
    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            body = {}

        if path.endswith("/chat/completions"):
            self._serve_chat("openai", body)
            return
        if path.endswith("/api/chat"):
            self._serve_chat("ollama", body)
            return
        if path.endswith("/messages"):
            self._serve_chat("anthropic", body)
            return
        if "/models/" in path:
            self._serve_chat("gemini", body)
            return

        if path.endswith("/audio/transcriptions"):
            UP.bump("asr_calls")
            self._json({"text": UP.get("asr_text")})
            return

        if path.endswith("/asr"):
            # 本地 FunASR/SenseVoice 协议（app/voice/asr_tts.py 的 local_asr 分支）：
            # POST {endpoint}/asr + multipart「audio」字段，**没有** model / language /
            # response_format，也无 Authorization。这里刻意**不解析 multipart**：
            # 要证明的是「请求真的走了这条路」（路径 + 计数 + 返回文本被上层采用），
            # 报文细节由 tools/test_speech_guard 的直接调用断言覆盖。
            UP.bump("asr_local_calls")
            UP.last_asr_path = path
            self._json({"text": UP.get("local_asr_text"), "raw": {"stub": True}})
            return

        if path.endswith("/audio/speech"):
            self._serve_tts()
            return

        self.send_error(404)

    # ---------------------------------------------------------------- chat
    def _serve_chat(self, proto: str, body: dict) -> None:
        UP.bump("chat_calls")
        UP.note_body(body)
        step = UP.pop_plan()
        if not step:
            # 队列空：默认行为（整段 JSON）—— 与加流式能力之前逐字一致
            step = {"text": UP.get("reply")}
        # 变异验证开关：模拟"不理会 stream:true 的厂商"（网关 documented 的
        # 降级路径）。开着它跑，第 13 条的流式断言必须全部变红 —— 用来证明
        # 那些断言不是恒绿。
        if os.environ.get("SMOKE_FORCE_NON_STREAM") and step.get("chunks"):
            step = {"text": step.get("text") or "".join(step["chunks"])}
        if not UP.get("llm_enabled"):
            self._json({"error": {"message": "上游被测试禁用"}}, 500)
            return
        ev = {"proto": proto, "req_at": time.monotonic(), "first_at": None,
              "done_at": None, "streamed": False, "tool": None}
        self._ev = ev
        UP.note_event("chat", ev)
        try:
            {"openai": self._send_openai,
             "ollama": self._send_ollama,
             "anthropic": self._send_anthropic,
             "gemini": self._send_gemini}[proto](step)
        finally:
            if ev["done_at"] is None:
                ev["done_at"] = time.monotonic()

    def _send_openai(self, step: dict) -> None:
        tool, text, chunks, delay, (p, c) = _step_of(step)
        usage = {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}
        if tool is None and chunks is None:
            # 厂商没理会 stream:true：整体读回来当普通响应（网关的降级路径）
            self._json({
                "choices": [{"message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": usage})
            return
        self._ev["streamed"] = True
        self._begin_stream("text/event-stream")
        if tool is not None:
            self._ev["tool"] = tool.get("name")
            self._sse({"choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call_smoke_1", "type": "function",
                "function": {
                    "name": str(tool.get("name") or ""),
                    "arguments": json.dumps(tool.get("arguments") or {},
                                            ensure_ascii=False)}}]}}]})
            self._sse({"choices": [{"index": 0, "delta": {},
                                    "finish_reason": "tool_calls"}]})
        else:
            for piece in (chunks or [text]):
                if delay:
                    time.sleep(delay)
                self._sse({"choices": [{"index": 0, "delta": {"content": piece}}]})
            self._sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self._sse({"choices": [], "usage": usage})
        self._note_first()
        self.wfile.write(b"data: [DONE]\n\n")

    def _send_ollama(self, step: dict) -> None:
        tool, text, chunks, delay, (p, c) = _step_of(step)
        if tool is None and chunks is None:
            self._json({"message": {"role": "assistant", "content": text},
                        "done": True, "prompt_eval_count": p, "eval_count": c})
            return
        self._ev["streamed"] = True
        self._begin_stream("application/x-ndjson")
        if tool is not None:
            self._ev["tool"] = tool.get("name")
            # Ollama 的 tool_calls 是整条给的（不分片），arguments 是对象
            self._ndjson({"message": {"role": "assistant", "tool_calls": [
                {"function": {"name": str(tool.get("name") or ""),
                              "arguments": tool.get("arguments") or {}}}]},
                "done": False})
        else:
            for piece in (chunks or [text]):
                if delay:
                    time.sleep(delay)
                self._ndjson({"message": {"role": "assistant", "content": piece},
                              "done": False})
        self._ndjson({"message": {"role": "assistant", "content": ""},
                      "done": True, "prompt_eval_count": p, "eval_count": c})

    def _send_anthropic(self, step: dict) -> None:
        tool, text, chunks, delay, (p, c) = _step_of(step)
        if tool is None and chunks is None:
            self._json({"content": [{"type": "text", "text": text}],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": p, "output_tokens": c}})
            return
        self._ev["streamed"] = True
        self._begin_stream("text/event-stream")
        self._sse({"type": "message_start",
                   "message": {"usage": {"input_tokens": p, "output_tokens": 1}}})
        if tool is not None:
            self._ev["tool"] = tool.get("name")
            self._sse({"type": "content_block_start", "index": 0,
                       "content_block": {"type": "tool_use", "id": "toolu_smoke_1",
                                         "name": str(tool.get("name") or "")}})
            self._sse({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "input_json_delta",
                                 "partial_json": json.dumps(
                                     tool.get("arguments") or {},
                                     ensure_ascii=False)}})
            self._sse({"type": "content_block_stop", "index": 0})
            self._sse({"type": "message_delta",
                       "delta": {"stop_reason": "tool_use"},
                       "usage": {"output_tokens": c}})
        else:
            self._sse({"type": "content_block_start", "index": 0,
                       "content_block": {"type": "text", "text": ""}})
            for piece in (chunks or [text]):
                if delay:
                    time.sleep(delay)
                self._sse({"type": "content_block_delta", "index": 0,
                           "delta": {"type": "text_delta", "text": piece}})
            self._sse({"type": "content_block_stop", "index": 0})
            self._sse({"type": "message_delta",
                       "delta": {"stop_reason": "end_turn"},
                       "usage": {"output_tokens": c}})
        self._sse({"type": "message_stop"})

    def _send_gemini(self, step: dict) -> None:
        tool, text, chunks, delay, (p, c) = _step_of(step)
        meta = {"promptTokenCount": p, "candidatesTokenCount": c,
                "totalTokenCount": p + c}
        if tool is None and chunks is None:
            self._json({"candidates": [{"content": {"role": "model",
                                                    "parts": [{"text": text}]},
                                        "finishReason": "STOP"}],
                        "usageMetadata": meta})
            return
        self._ev["streamed"] = True
        self._begin_stream("text/event-stream")
        if tool is not None:
            self._ev["tool"] = tool.get("name")
            self._sse({"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": str(tool.get("name") or ""),
                                  "args": tool.get("arguments") or {}}}]}}],
                "usageMetadata": meta})
        else:
            for piece in (chunks or [text]):
                if delay:
                    time.sleep(delay)
                self._sse({"candidates": [{"content": {"role": "model",
                                                       "parts": [{"text": piece}]}}]})
            self._sse({"candidates": [{"content": {"role": "model",
                                                   "parts": [{"text": ""}]},
                                       "finishReason": "STOP"}],
                       "usageMetadata": meta})

    # ---------------------------------------------------------------- tts
    def _serve_tts(self) -> None:
        UP.bump("tts_calls")
        if not UP.get("tts_enabled"):
            self._json({"error": {"message": "上游 TTS 被测试禁用"}}, 500)
            return
        ev = {"req_at": time.monotonic(), "first_at": None, "done_at": None}
        self._ev = ev
        UP.note_event("tts", ev)
        try:
            delay = UP.get("tts_delay")
            if delay:
                time.sleep(delay)
            chunk_delay = float(UP.get("tts_chunk_delay") or 0.0)
            n_chunks = max(1, int(UP.get("tts_chunks") or 1))
            if chunk_delay and n_chunks > 1:
                # 真流式：分片 + 片间延迟，让会话层"边收边切帧边下发"
                size = max(4096, int(UP.get("tts_chunk_bytes") or TTS_CHUNK_BYTES))
                data = _pcm_bytes(size * n_chunks)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                for i in range(0, len(data), size):
                    time.sleep(chunk_delay)   # 含首分片：time-to-first-audio
                    self._note_first()
                    self.wfile.write(data[i:i + size])
            else:
                data = _pcm_bytes(int(24000 * 2 * float(UP.get("tts_seconds") or 0.5)))
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self._note_first()
                self.wfile.write(data)
        finally:
            ev["done_at"] = time.monotonic()


def start_upstream() -> tuple[ThreadingHTTPServer, int]:
    srv = _QuietThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    port = int(srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


# ================================================================ 起服务
def start_app_server() -> object:
    """在后台线程里跑 app。环境变量必须在 import app.* **之前**设好。"""
    os.environ["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
    os.environ["ELDER_JWT_SECRET"] = "smoke-voice-secret-0123456789abcdef"
    os.environ["ELDER_SCHEDULER_INTERVAL"] = "3600"   # 不起后台调度
    os.environ["PYTHONIOENCODING"] = "utf-8"
    # 假上游就在本机，确保绕过代理
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("HTTPS_PROXY", None)
    # 固件分发默认不启用：显式清掉，保证 t_ota_firmware 的「未配置」断言
    # 测的是**默认行为**而不是开发机 ambient env 里残留的变量（那种残留会让
    # 默认断言诡异地红，且查不到本文件头上）。
    os.environ.pop("ELDER_FIRMWARE_VERSION", None)
    os.environ.pop("ELDER_FIRMWARE_URL", None)

    SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[logging.FileHandler(SERVER_LOG, encoding="utf-8")],
    )

    sys.path.insert(0, str(SERVER))
    import uvicorn
    from app.main import app
    from app.voice import attach

    attach(app)   # voice 路由尚未接进 main.py（集成由 team-lead 做）

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    return server


def wait_ready(timeout: float = 120.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with _LOCAL_OPENER.open(
                    urllib.request.Request(BASE + "/api/health"), timeout=2):
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


# ================================================================ 播种
def seed(upstream_port: int) -> dict:
    """进程内播种：管理员 + 两台设备 + llm/asr/tts 配置。

    LLM  deliberately 播**四条**（OpenAI 兼容 / Ollama / Anthropic / Gemini，
    priority 1-4，都指向同一个假上游）：第 12e 组要逐条禁用其它三条来验证
    四种协议的 tools 报文；其余组只用到优先级最高的 OpenAI 兼容那条
    （resolve_plan 按序兜底，第一条成功就不再往下走）。

    刻意**不走 HTTP**：建设备要激活码四步流程（那是 smoke_server 的领地），
    本测试关心的是语音链路本身。设备 token 用 security.issue_device_token
    现铸，与 OTA 下发的token 完全同源。
    """
    from app.db import SessionLocal
    from app.security import issue_device_token
    from sqlalchemy import select

    from app import models

    db = SessionLocal()
    try:
        admin = db.scalar(select(models.User).where(
            models.User.username == "admin"))
        if admin is None:
            admin = models.User(username="admin", nickname="管理员",
                                is_admin=True, password_hash="x")
            db.add(admin)
            db.flush()

        bound = db.scalar(select(models.Device).where(
            models.Device.device_id == MAC_BOUND))
        if bound is None:
            bound = models.Device(device_id=MAC_BOUND, owner_id=admin.id,
                                  name="已绑定设备")
            db.add(bound)
            db.flush()

        def _ensure(kind: str, name: str, **kw) -> models.AIProvider:
            row = db.scalar(select(models.AIProvider).where(
                models.AIProvider.kind == kind,
                models.AIProvider.name == name))
            if row is None:
                row = models.AIProvider(name=name, kind=kind, **kw)
                db.add(row)
                db.flush()
            return row

        llm_pks: dict[str, int] = {}
        for style, label, prio in (
                ("openai_compatible", "假 LLM-OpenAI（冒烟）", 1),
                ("ollama", "假 LLM-Ollama（冒烟）", 2),
                ("anthropic", "假 LLM-Anthropic（冒烟）", 3),
                ("gemini", "假 LLM-Gemini（冒烟）", 4)):
            row = _ensure(
                "llm_main", label,
                api_style=style,
                endpoint=f"http://127.0.0.1:{upstream_port}/v1",
                api_key="sk-fake", model="fake-chat-1", enabled=True,
                priority=prio)
            llm_pks[style] = int(row.id)
        asr = _ensure(
            "asr", "假 ASR（冒烟）",
            api_style="openai_compatible",
            endpoint=f"http://127.0.0.1:{upstream_port}/v1",
            api_key="sk-fake", model="whisper-1", enabled=True, priority=1)
        # 本地 FunASR/SenseVoice（2026-10-22 接入）。协议与 OpenAI 兼容完全不同：
        # POST {endpoint}/asr（**没有** /v1 后缀）+ multipart audio 字段、无密钥无 model。
        # priority=0 让它压过上面那条 OpenAI 兼容行；但**播种时停用**——
        # 否则第 4/9 组这些走语音的组会全被它截走（它们的断言写的是
        # FAKE_ASR_TEXT 的转写结果），症状是一片与本组无关的假失败。
        # 第 15 组自己启用、组末自己停用，前后状态干净。
        asr_local = _ensure(
            "asr", "本地 ASR（FunASR 冒烟）",
            api_style="local_asr", provider="funasr_local",
            endpoint=f"http://127.0.0.1:{upstream_port}",
            api_key="", model="sensevoice-small", enabled=False, priority=0)
        tts = _ensure(
            "tts", "假 TTS（冒烟）",
            api_style="openai_compatible",
            endpoint=f"http://127.0.0.1:{upstream_port}/v1",
            api_key="sk-fake", model="tts-1", enabled=True, priority=1,
            params={"voice": "zh-CN-XiaoxiaoNeural"})
        db.commit()
        return {
            "user_id": int(admin.id),
            "device_pk": int(bound.id),
            "llm_pk": llm_pks["openai_compatible"],
            "llm_pks": llm_pks,
            "asr_pk": int(asr.id), "tts_pk": int(tts.id),
            "asr_local_pk": int(asr_local.id),
            "token": issue_device_token(int(bound.id), MAC_BOUND),
        }
    finally:
        db.close()


# ================================================================ 工具
def _pcm_frame(seconds: float, freq: float = 440.0) -> bytes:
    """16kHz/mono/S16LE 的 PCM（设备上行就是这个格式）。"""
    n = int(16000 * seconds)
    out = bytearray()
    for i in range(n):
        out += struct.pack("<h", int(10000 * math.sin(2 * math.pi * freq * i / 16000)))
    return bytes(out)


def _rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    n = len(pcm) // 2
    vals = struct.unpack(f"<{n}h", pcm[: n * 2])
    return math.sqrt(sum(v * v for v in vals) / n) / 32768.0


def _db_count(sql: str, params: dict | None = None) -> int:
    from sqlalchemy import text
    from app.db import SessionLocal

    with SessionLocal() as db:
        return int(db.execute(text(sql), params or {}).scalar() or 0)


def _reminders(device_pk: int) -> list[dict]:
    """这台设备当前的提醒（断言"真的建进/删掉了没有"的唯一真源）。"""
    from app.db import SessionLocal
    from app.models import Reminder

    with SessionLocal() as db:
        rows = db.query(Reminder).filter(
            Reminder.device_id == device_pk).order_by(Reminder.id).all()
        return [{"id": int(r.id), "title": r.title,
                 "times": list(r.times_of_day or []),
                 "once_date": r.once_date,
                 "kind": int(r.schedule_kind),
                 "weekdays": list(r.weekdays or []),
                 "enabled": bool(r.enabled)} for r in rows]


def _last_interaction_meta(device_pk: int) -> dict:
    """最近一条 Interaction 的 meta（工具轨迹 / 轮数埋点在这里）。"""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Interaction

    with SessionLocal() as db:
        row = db.scalar(select(Interaction).where(
            Interaction.device_id == device_pk
        ).order_by(Interaction.id.desc()).limit(1))
        if row is None:
            return {}
        meta = row.meta
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:  # noqa: BLE001
                meta = {}
        return meta if isinstance(meta, dict) else {}


def _tomorrow() -> str:
    """明天（用服务端同一个 local_now，避免测试机时区与 ELDER_TZ 不一致）。"""
    from app.schedule import local_now

    return (local_now().date() + timedelta(days=1)).isoformat()


def _set_quota_limit(seconds: int) -> None:
    """把每日时长上限压到 ``seconds``（用于验证**它不再拦已绑定设备**）。"""
    from app.db import SessionLocal
    from app.models import ChatQuotaConfig

    with SessionLocal() as db:
        cfg = db.get(ChatQuotaConfig, 1)
        if cfg is None:
            cfg = ChatQuotaConfig(id=1, free_daily_seconds=seconds)
            db.add(cfg)
        else:
            cfg.free_daily_seconds = seconds
        db.commit()


def _disable_provider(pk: int) -> None:
    from app.db import SessionLocal
    from app.models import AIProvider

    with SessionLocal() as db:
        row = db.get(AIProvider, pk)
        row.enabled = False
        db.commit()


def _enable_provider(pk: int) -> None:
    from app.db import SessionLocal
    from app.models import AIProvider

    with SessionLocal() as db:
        row = db.get(AIProvider, pk)
        row.enabled = True
        db.commit()


def _set_llm_enabled(pks, enabled: bool) -> None:
    """批量切 llm 配置的启用位（四协议测试 / 禁用全部 LLM 用）。"""
    from app.db import SessionLocal
    from app.models import AIProvider

    with SessionLocal() as db:
        for pk in pks:
            row = db.get(AIProvider, int(pk))
            if row is not None:
                row.enabled = enabled
        db.commit()


def _set_asr_local_enabled(pk: int, enabled: bool) -> None:
    """切「本地 ASR」配置行的启用位（第 15 组前后各用一次）。

    播种时该行就是停用的（见 seed 的注释），本组开启、组末关闭 ——
    priority=0 的配置一旦常开，会静默改变**其余所有组**的观测对象，
    那种假失败与本组代码毫无关系，最难归因。
    """
    _set_llm_enabled([pk], enabled)


def _ota(mac: str, method: str = "POST") -> tuple[int, dict]:
    body = b"{}" if method == "POST" else None
    req = urllib.request.Request(
        BASE + "/api/voice/ota", data=body, method=method,
        headers={"Content-Type": "application/json", "Device-Id": mac,
                 "Client-Id": "smoke-client", "Activation-Version": "1"})
    try:
        with _LOCAL_OPENER.open(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"_raw": raw}


# ================================================================ 断言组
def t_ota(ids: dict) -> None:
    print("── 1. OTA：未绑定只给激活码，绑定才给 WS 通道 ──")
    code, unbound = _ota(MAC_UNBOUND)
    check("OTA 未绑定 MAC -> 200", code == 200, f"code={code}")
    check("OTA 未绑定返回 activation.code",
          bool(unbound.get("activation", {}).get("code")),
          str(unbound.get("activation", {}).get("code")))
    check("OTA 未绑定**不带** websocket 段"
          "（给未绑定设备发 token = 开一个无主账户）",
          "websocket" not in unbound, str(list(unbound.keys())))
    check("OTA 未绑定的激活码是 7 位（6 位随机 + 1 位校验，全数字）",
          len(str(unbound.get("activation", {}).get("code", ""))) == 7
          and str(unbound.get("activation", {}).get("code", "")).isdigit(),
          str(unbound.get("activation", {}).get("code")))
    check("OTA 响应带 server_time",
          isinstance(unbound.get("server_time", {}).get("timestamp"), int),
          str(unbound.get("server_time")))

    code, body = _ota(MAC_BOUND)
    ws = body.get("websocket", {})
    check("OTA 已绑定 -> 200 且带 websocket 段", code == 200 and bool(ws),
          f"code={code} keys={list(body.keys())}")
    check("OTA ws.url 以 /api/voice/v1/ws 结尾",
          str(ws.get("url", "")).endswith("/api/voice/v1/ws"), str(ws.get("url")))
    check("OTA ws.url 是 ws:// 或 wss://（不是 http）",
          str(ws.get("url", "")).startswith(("ws://", "wss://")),
          str(ws.get("url")))

    # 本地校验 token：与 OTA 下发的完全同源（都是 issue_device_token）
    from app.security import decode_token

    payload = decode_token(str(ws.get("token", ""))) or {}
    check("OTA 下发的 token 可解出 typ=device",
          payload.get("typ") == "device", str(payload.get("typ")))
    check("OTA 下发的 token 的 sub == 设备主键",
          payload.get("sub") == str(ids["device_pk"]),
          f"{payload.get('sub')} vs {ids['device_pk']}")
    check("OTA 未绑定的响应里没有 token 字段",
          "token" not in json.dumps(body.get("activation", {})), "")

    # 激活码复用：再要一次应该拿到**同一个**码（防刷，不重复建）。
    # 比对对象是上面那份**未绑定**响应 —— 不能用已绑定的那份，
    # 它压根没有 activation 段（曾在这里写错，断言拿到 None -> 恒绿）。
    code, again = _ota(MAC_UNBOUND)
    check("OTA 同一 MAC 二次申领复用同一个激活码（不刷库）",
          again.get("activation", {}).get("code")
          == unbound.get("activation", {}).get("code"),
          f"{unbound.get('activation', {}).get('code')} -> "
          f"{again.get('activation', {}).get('code')}")

    code, _no_mac = _ota("", "POST")
    check("OTA 无 Device-Id -> 400", code == 400, f"code={code}")

    code, via_get = _ota(MAC_UNBOUND, "GET")
    check("OTA 也接受 GET（固件某些版本用 GET）",
          code == 200 and bool(via_get.get("activation", {}).get("code")),
          f"code={code}")


def t_activate_v2() -> None:
    print("── 1b. 激活 v2 明确不支持（不假装成功）──")
    req = urllib.request.Request(
        BASE + "/api/voice/ota/activate", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "Device-Id": MAC_UNBOUND})
    try:
        with _LOCAL_OPENER.open(req, timeout=10) as resp:
            code = resp.status
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        code = e.code
        body = json.loads(e.read().decode("utf-8", "replace"))
    check("激活 v2 -> 400", code == 400, f"code={code}")
    check("激活 v2 的 error 明确说 unsupported",
          body.get("error") == "activation-v2-unsupported", str(body.get("error")))


def t_ota_firmware(ids: dict) -> None:
    print("── 1d. OTA firmware 段：默认不返回，两个变量都配了才返回 ──")
    import importlib

    from app import config as _config

    version = "2.0.5"
    url = "https://152.136.230.132/firmware/xiaozhi-2.0.5.bin"
    keys = ("ELDER_FIRMWARE_VERSION", "ELDER_FIRMWARE_URL")

    def _reload_and_probe(env: dict[str, str]) -> tuple[int, dict, int, dict]:
        """按 env 重载 config，返回 ``(未绑定码, 未绑定响应, 已绑定码, 已绑定响应)``。

        先把两个 key 全部 pop 再按 env 设置 —— 每个场景的输入**只有 env
        显式列出的那几项**，绝不让上一个场景的残留渗进来。reload 后同进程内
        的服务立即换行为（ota_check 经 config 模块属性在调用时读值，
        见 voice._firmware_section 的 docstring），DB / 种子 / 端口全部复用。
        """
        for key in keys:
            os.environ.pop(key, None)
        for key, value in env.items():
            os.environ[key] = value
        importlib.reload(_config)
        code_unbound, unbound = _ota(MAC_UNBOUND)
        code_bound, bound = _ota(MAC_BOUND)
        return code_unbound, unbound, code_bound, bound

    # ---- 默认（两个变量都未配）：两条路径都不带 firmware 段 ----
    # 这是「默认行为不许变」的回归网：该特性上线前固件永远拿不到 firmware 段，
    # 任何把默认改成"总返回"的改动都必须在这里红。
    code, unbound, _code, bound = _reload_and_probe({})
    check("1d · 未配置时未绑定响应无 firmware 段（默认行为不变）",
          code == 200 and "firmware" not in unbound,
          f"code={code} keys={list(unbound.keys())}")
    check("1d · 未配置时已绑定响应无 firmware 段（默认行为不变）",
          _code == 200 and "firmware" not in bound,
          f"code={_code} keys={list(bound.keys())}")

    # ---- 半配（只设一个）：两条路径都不得返回 firmware 段 ----
    # QA 曾把 _firmware_section 的条件改成「任一非空」，全套冒烟仍 140 过 0 败
    # ——「两个都非空才返回」当时只靠代码注释维持。这四条就是钉死该契约的
    # 断言：只配 version 或只配 url 都是配置不完整，宁可不返回让设备保持
    # 「不 OTA」的现状，也不下一个缺字段的段让固件白做比较。
    code, unbound, _code, bound = _reload_and_probe(
        {"ELDER_FIRMWARE_VERSION": version})
    check("1d · 半配（只有 VERSION）时未绑定响应无 firmware 段",
          code == 200 and "firmware" not in unbound,
          f"code={code} keys={list(unbound.keys())}")
    check("1d · 半配（只有 VERSION）时已绑定响应无 firmware 段",
          _code == 200 and "firmware" not in bound,
          f"code={_code} keys={list(bound.keys())}")
    code, unbound, _code, bound = _reload_and_probe(
        {"ELDER_FIRMWARE_URL": url})
    check("1d · 半配（只有 URL）时未绑定响应无 firmware 段",
          code == 200 and "firmware" not in unbound,
          f"code={code} keys={list(unbound.keys())}")
    check("1d · 半配（只有 URL）时已绑定响应无 firmware 段",
          _code == 200 and "firmware" not in bound,
          f"code={_code} keys={list(bound.keys())}")

    # ---- 配齐（两个都设）：两条路径都返回，且值正确 ----
    try:
        code, unbound, code_bound, bound = _reload_and_probe(
            {"ELDER_FIRMWARE_VERSION": version, "ELDER_FIRMWARE_URL": url})
        fw = unbound.get("firmware") or {}
        check("1d · 配齐后未绑定响应带 firmware 段",
              code == 200 and bool(fw), str(list(unbound.keys())))
        check("1d · 未绑定 firmware.version == ELDER_FIRMWARE_VERSION",
              fw.get("version") == version, str(fw.get("version")))
        check("1d · 未绑定 firmware.url == ELDER_FIRMWARE_URL",
              fw.get("url") == url, str(fw.get("url")))
        fw2 = bound.get("firmware") or {}
        check("1d · 配齐后已绑定响应也带 firmware 段",
              code_bound == 200 and bool(fw2), str(list(bound.keys())))
        check("1d · 已绑定 firmware.version/url == 配置值",
              fw2.get("version") == version and fw2.get("url") == url,
              str(fw2))
        # 新段不能挤掉既有段：已绑定路径的 websocket 是设备的命脉
        check("1d · firmware 段不挤掉 websocket 段",
              bool(bound.get("websocket")), str(list(bound.keys())))
    finally:
        # 还原默认环境并重载：不能把配置留给后续断言组
        for key in keys:
            os.environ.pop(key, None)
        importlib.reload(_config)


async def t_ws_auth(ids: dict) -> None:
    print("── 2. WS 鉴权 ──")
    import websockets

    # 无 token
    try:
        async with ws_connect(WS_URL, open_timeout=10) as ws:
            await ws.recv()
        code = None
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "rcvd", None)
        code = getattr(code, "code", None) if code is not None else None
    check("无 token 连接被关闭（拿不到 server hello）", code is not None,
          f"close_code={code}")

    # 垃圾 token（请求头参数名的版本差异由 ws_connect 统一处理，见其 docstring）
    try:
        async with ws_connect(
                WS_URL, open_timeout=10,
                headers={"Authorization": "Bearer garbage"}) as ws:
            await ws.recv()
        code = None
    except Exception as exc:  # noqa: BLE001
        r = getattr(exc, "rcvd", None)
        code = getattr(r, "code", None) if r is not None else None
    check("垃圾 token 连接被关闭", code is not None, f"close_code={code}")

    # 有效 token 但 Device-Id 不符
    try:
        async with ws_connect(
                WS_URL, open_timeout=10,
                headers={
                    "Authorization": f"Bearer {ids['token']}",
                    "Device-Id": "FF:FF:FF:FF:FF:FF",
                    "Protocol-Version": "1"}) as ws:
            await ws.recv()
        code = None
    except Exception as exc:  # noqa: BLE001
        r = getattr(exc, "rcvd", None)
        code = getattr(r, "code", None) if r is not None else None
    check("Device-Id 与 token 不符 -> close 4001（跨设备盗用 token 挡住）",
          code == 4001, f"close_code={code}")


async def _handshake(ws: object, ids: dict, version: int = 1) -> dict:
    await ws.send(json.dumps({
        "type": "hello", "version": version,
        "features": {"mcp": True}, "transport": "websocket",
        "audio_params": {"format": "opus", "sample_rate": 16000,
                         "channels": 1, "frame_duration": 60},
    }))
    raw = await asyncio_wait_recv(ws)
    return json.loads(raw)


async def asyncio_wait_recv(ws: object) -> str:
    import asyncio
    return await asyncio.wait_for(ws.recv(), timeout=15)


async def _settle(seconds: float = 2.5) -> None:
    """等上一轮对话彻底结束再进下一组断言。

    为什么需要：服务端把整轮跑在独立 task 里（为了不卡住收包主循环，
    否则 abort 失效），所以**客户端收到 tts stop 时那一轮可能还在收尾**
    （记 token / 写 Interaction / 沉淀记忆）。上一组的"最后一轮"若仍在飞，
    下一组捕获的 ``chat_calls`` 基线就会偏 1，表现为"配额/空识别竟然调了 LLM"
    这种**看着像产品 bug、其实是测试自己没等干净**的红。

    只 sleep 不带 ws：这些组是在**自己新建连接**之前就要等，
    手里还没有 ws 可 drain。
    """
    await asyncio.sleep(seconds)


async def _drain(ws: object, seconds: float = 0.2) -> None:
    """把已经到达的消息读干，避免上一条测试的残留影响下一条计数。"""
    import asyncio
    end = time.time() + seconds
    while time.time() < end:
        try:
            await asyncio.wait_for(ws.recv(), timeout=max(0.05, end - time.time()))
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            break


async def _connect(ids: dict, version: int = 1):
    """建连 + 握手 + drain，返回 (ws, session_id)。"""
    import websockets

    headers = {"Authorization": f"Bearer {ids['token']}",
               "Device-Id": MAC_BOUND,
               "Protocol-Version": str(version),
               "Client-Id": "smoke-client"}
    ws = await ws_connect(WS_URL, open_timeout=10,
                                  headers=headers)
    hello = await _handshake(ws, ids, version)
    await _drain(ws)
    return ws, str(hello.get("session_id", ""))


async def _one_turn(ws, sid: str, *, frames: int = 6, wait: float = 30.0):
    """跑一轮"收音 -> 播报"。

    返回 (句子列表, 事件列表, 二进制帧数)。事件列表元素：
    ``("json", type, state, text)`` 或 ``("bin", nbytes)`` —— 保序，
    第 13 条的协议时序断言靠它。
    """
    await ws.send(json.dumps({"session_id": sid, "type": "listen",
                              "state": "start", "mode": "auto"}))
    await asyncio.sleep(0.25)
    for _ in range(frames):
        await ws.send(FAKE_OPUS_PACKET)
        await asyncio.sleep(0.02)
    await ws.send(json.dumps({"session_id": sid, "type": "listen",
                              "state": "stop"}))
    sentences: list[str] = []
    events: list[tuple] = []
    bins = 0
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
        except asyncio.TimeoutError:
            break
        if isinstance(msg, bytes):
            bins += 1
            events.append(("bin", len(msg)))
            continue
        obj = json.loads(msg)
        events.append(("json", str(obj.get("type")), str(obj.get("state")),
                       str(obj.get("text") or "")))
        if obj.get("type") == "tts" and obj.get("state") == "sentence_start":
            sentences.append(str(obj.get("text") or ""))
        if obj.get("type") == "tts" and obj.get("state") == "stop":
            break
    return sentences, events, bins


async def t_handshake_and_turn(ids: dict, libopus) -> None:
    print("── 3-4. 握手 + 完整一轮 ──")
    import websockets

    headers = {"Authorization": f"Bearer {ids['token']}",
               "Device-Id": MAC_BOUND, "Protocol-Version": "1",
               "Client-Id": "smoke-client"}
    async with ws_connect(WS_URL, open_timeout=10,
                                  headers=headers) as ws:
        hello = await _handshake(ws, ids, 1)
        check("握手：收到服务端 hello", hello.get("type") == "hello",
              str(hello)[:120])
        check("握手：transport == websocket",
              hello.get("transport") == "websocket", str(hello.get("transport")))
        check("握手：session_id 非空", bool(hello.get("session_id")),
              str(hello.get("session_id")))
        check("握手：audio_params.sample_rate == 24000",
              hello.get("audio_params", {}).get("sample_rate") == 24000,
              str(hello.get("audio_params")))
        check("握手：audio_params.frame_duration == 60",
              hello.get("audio_params", {}).get("frame_duration") == 60,
              str(hello.get("audio_params")))
        sid = hello.get("session_id", "")

        await _drain(ws)

        # ---- listen start → 二进制帧 → listen stop
        chat_before = UP.get("chat_calls")
        await ws.send(json.dumps({"session_id": sid, "type": "listen",
                                  "state": "start", "mode": "auto"}))
        await asyncio.sleep(0.3)

        if libopus:
            # 有 libopus：用**真的 Opus 编码**发一段正弦。
            # 这是 round-trip 的关键：发出去的必须是设备会发的格式，
            # 而不是随机字节（随机字节 opus_decode 解出来是噪声，虽也能
            # 证明"没崩"，但证不了"编解码真的对"）。
            from app.voice import opus_codec

            pcm = _pcm_frame(0.12)
            packet = opus_codec.encode_opus(pcm, 16000)
            for _ in range(6):
                await ws.send(packet)
                await asyncio.sleep(0.02)
            check("libopus 可用，已发真 Opus 帧", bool(packet),
                  f"packet={len(packet)}B")
        else:
            skip("第 4 条 · 音频 round-trip（本机无 libopus）",
                 "ctypes 找不到 libopus（find_library=None，所有候选名均失败）；"
                 "协议级断言照常跑。CI（ubuntu，已装 libopus0）必须真跑过")
            # 无 libopus 也发点字节，验证协议层不因内容崩
            for _ in range(6):
                await ws.send(os.urandom(40))

        await ws.send(json.dumps({"session_id": sid, "type": "listen",
                                  "state": "stop"}))

        # ---- 依次收 stt / tts start / sentence_start / 二进制 / tts stop
        seen: dict[str, object] = {}
        binary_frames: list[bytes] = []
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                break
            if isinstance(msg, bytes):
                binary_frames.append(msg)
                continue
            obj = json.loads(msg)
            mtype = obj.get("type")
            if mtype == "stt":
                seen["stt"] = obj.get("text")
            elif mtype == "tts":
                seen[f"tts_{obj.get('state')}"] = True
                if obj.get("state") == "sentence_start":
                    seen.setdefault("sentences", []).append(obj.get("text"))
            elif mtype == "alert":
                seen["alert"] = obj.get("message")
            if "tts_stop" in seen:
                break

        check("第 4 条 · 收到 stt 且文本 == 假 ASR 返回值",
              seen.get("stt") == FAKE_ASR_TEXT, str(seen.get("stt")))
        check("第 4 条 · 收到 tts start", "tts_start" in seen, str(list(seen)))
        check("第 4 条 · 收到 tts stop（设备靠它回落 listening/idle）",
              "tts_stop" in seen, str(list(seen)))
        check("第 4 条 · 至少一句 sentence_start",
              bool(seen.get("sentences")), str(seen.get("sentences")))
        check("第 4 条 · 假上游 chat 被调了一次",
              UP.get("chat_calls") == chat_before + 1,
              f"{chat_before} -> {UP.get('chat_calls')}")

        if libopus:
            check("第 4 条 · 收到二进制音频帧（下行 TTS）",
                  len(binary_frames) > 0, f"{len(binary_frames)} 帧")
            if binary_frames:
                from app.voice import opus_codec

                pcm_out = opus_codec.decode_opus(
                    binary_frames[0], opus_codec_frame_rate())
                rms = _rms(pcm_out)
                check("第 4 条 ★ 编解码 round-trip：下行帧解回来非全静音",
                      rms > 0.001, f"rms={rms:.4f}, pcm={len(pcm_out)}B")
        else:
            skip("第 4 条 ★ 编解码 round-trip",
                 "本机无 libopus，服务端发不出 Opus 帧（_send_audio 被跳过）")


def opus_codec_frame_rate() -> int:
    """下行音频的采样率。**从 config 读而不是写死**：改了配置而这里写死，
    断言就会去用另一个速率解帧，表现为"解出来有声音但音调不对"，
    而这种错不会让任何断言红（RMS 仍然 > 0）。
    """
    from app import config
    return config.VOICE_OUTPUT_SAMPLE_RATE


async def t_interrupt(ids: dict) -> None:
    print("── 7. 打断：播报中 abort 立刻停发 ──")
    import websockets

    UP.tts_delay = 0.6
    UP.reply = "第一句话。第二句话。第三句话。第四句话。"
    headers = {"Authorization": f"Bearer {ids['token']}",
               "Device-Id": MAC_BOUND, "Protocol-Version": "1"}
    try:
        async with ws_connect(WS_URL, open_timeout=10,
                                      headers=headers) as ws:
            await _handshake(ws, ids, 1)
            await _drain(ws)
            sid = ""
            # hello 的 session_id 已经在 _handshake 里丢掉，重取一次
            await ws.send(json.dumps({"type": "listen", "state": "start",
                                      "mode": "auto"}))
            await asyncio.sleep(0.2)
            for _ in range(6):
                await ws.send(os.urandom(40))
            await ws.send(json.dumps({"type": "listen", "state": "stop"}))
            sid = "x"

            sentences: list[str] = []
            frames = 0
            deadline = time.time() + 30
            sent_abort = False
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, bytes):
                    frames += 1
                    if not sent_abort and frames >= 1:
                        await ws.send(json.dumps({"session_id": sid,
                                                  "type": "abort",
                                                  "reason": "wake_word_detected"}))
                        sent_abort = True
                    continue
                obj = json.loads(msg)
                if obj.get("type") == "tts" and obj.get("state") == "sentence_start":
                    sentences.append(obj.get("text", ""))
                    if not sent_abort and len(sentences) >= 1:
                        await ws.send(json.dumps({"session_id": sid,
                                                  "type": "abort",
                                                  "reason": "wake_word_detected"}))
                        sent_abort = True
                if obj.get("type") == "tts" and obj.get("state") == "stop":
                    break

            check("第 7 条 · 打断后不再收到后续 sentence_start"
                  "（4 句里最多 1-2 句）",
                  len(sentences) < 4, f"实际 {len(sentences)} 句：{sentences}")
            check("第 7 条 · 确实收到过 abort 前的句子（不是压根没播）",
                  len(sentences) >= 1, f"{sentences}")

            # 打断后会话仍可再次对话
            UP.reply = "我还在呢，您说。"
            UP.tts_delay = 0.0
            await ws.send(json.dumps({"type": "listen", "state": "start",
                                      "mode": "auto"}))
            await asyncio.sleep(0.2)
            for _ in range(6):
                await ws.send(os.urandom(40))
            await ws.send(json.dumps({"type": "listen", "state": "stop"}))
            got = ""
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, bytes):
                    continue
                obj = json.loads(msg)
                if obj.get("type") == "stt":
                    got = obj.get("text", "")
                if obj.get("type") == "tts" and obj.get("state") == "stop":
                    break
            check("第 7 条 · 打断后会话回到可再次对话（又收到一轮 stt）",
                  bool(got), f"stt={got!r}")
    finally:
        UP.tts_delay = TTS_SENTENCE_DELAY_SEC
        UP.reply = FAKE_REPLY


async def t_degraded_tts(ids: dict) -> None:
    print("── 8a. 降级：禁用 TTS → 只发文本，不崩 ──")
    import websockets

    _disable_provider(ids["tts_pk"])
    headers = {"Authorization": f"Bearer {ids['token']}",
               "Device-Id": MAC_BOUND, "Protocol-Version": "1"}
    try:
        async with ws_connect(WS_URL, open_timeout=10,
                                      headers=headers) as ws:
            await _handshake(ws, ids, 1)
            await _drain(ws)
            await ws.send(json.dumps({"type": "listen", "state": "start",
                                      "mode": "auto"}))
            await asyncio.sleep(0.2)
            for _ in range(6):
                await ws.send(os.urandom(40))
            await ws.send(json.dumps({"type": "listen", "state": "stop"}))

            sentences: list[str] = []
            frames = 0
            tts_stop = False
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, bytes):
                    frames += 1
                    continue
                obj = json.loads(msg)
                if obj.get("type") == "stt":
                    check("第 8 条 · 禁用 TTS 仍收到 stt",
                          bool(obj.get("text")), str(obj.get("text")))
                if obj.get("type") == "tts" and obj.get("state") == "sentence_start":
                    sentences.append(obj.get("text", ""))
                if obj.get("type") == "tts" and obj.get("state") == "stop":
                    tts_stop = True
                    break
            check("第 8 条 · 禁用 TTS 仍收到 tts start/stop（设备能回落 idle）",
                  tts_stop, f"stop={tts_stop}")
            check("第 8 条 · 禁用 TTS 仍收到 sentence_start 文本（看得见话）",
                  bool(sentences), str(sentences))
            check("第 8 条 · 禁用 TTS 时**不发**二进制音频帧", frames == 0,
                  f"{frames} 帧")
    finally:
        _enable_provider(ids["tts_pk"])


async def t_degraded_llm(ids: dict) -> None:
    print("── 8b. 降级：禁用全部 LLM → alert，不崩 ──")
    import websockets

    # 四条 llm 配置**全部**禁用：只禁优先级最高的那条的话，
    # resolve_plan 会兜底到下一条，alert 永远不来（而断言看着是通过）。
    _set_llm_enabled(ids["llm_pks"].values(), False)
    headers = {"Authorization": f"Bearer {ids['token']}",
               "Device-Id": MAC_BOUND, "Protocol-Version": "1"}
    try:
        async with ws_connect(WS_URL, open_timeout=10,
                                      headers=headers) as ws:
            await _handshake(ws, ids, 1)
            await _drain(ws)
            await ws.send(json.dumps({"type": "listen", "state": "start",
                                      "mode": "auto"}))
            await asyncio.sleep(0.2)
            for _ in range(6):
                await ws.send(os.urandom(40))
            await ws.send(json.dumps({"type": "listen", "state": "stop"}))

            alert = ""
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=8)
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, bytes):
                    continue
                obj = json.loads(msg)
                if obj.get("type") == "alert":
                    alert = obj.get("message", "")
                    break
            check("第 8 条 · 禁用 LLM 收到 alert 且提到管理后台",
                  bool(alert) and "管理后台" in alert, f"alert={alert!r}")

            # WS 仍存活：再发一轮应仍有响应（哪怕还是 alert）
            await ws.send(json.dumps({"type": "listen", "state": "start",
                                      "mode": "auto"}))
            await asyncio.sleep(0.2)
            for _ in range(4):
                await ws.send(os.urandom(40))
            await ws.send(json.dumps({"type": "listen", "state": "stop"}))
            alive = False
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=8)
                except asyncio.TimeoutError:
                    break
                if not isinstance(msg, bytes):
                    obj = json.loads(msg)
                    if obj.get("type") in ("alert", "stt", "tts"):
                        alive = True
                        break
            check("第 8 条 · WS 未崩，仍能收发", alive, "")
    finally:
        _set_llm_enabled(ids["llm_pks"].values(), True)


async def t_empty_asr(ids: dict) -> None:
    print("── 9. 空识别：不调 LLM，回「没听清」 ──")
    import websockets

    await _settle()
    UP.asr_text = ""
    chat_before = UP.get("chat_calls")
    headers = {"Authorization": f"Bearer {ids['token']}",
               "Device-Id": MAC_BOUND, "Protocol-Version": "1"}
    try:
        async with ws_connect(WS_URL, open_timeout=10,
                                      headers=headers) as ws:
            await _handshake(ws, ids, 1)
            await _drain(ws)
            await ws.send(json.dumps({"type": "listen", "state": "start",
                                      "mode": "auto"}))
            await asyncio.sleep(0.2)
            for _ in range(6):
                await ws.send(os.urandom(40))
            await ws.send(json.dumps({"type": "listen", "state": "stop"}))

            reply_text = ""
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, bytes):
                    continue
                obj = json.loads(msg)
                if obj.get("type") == "tts" and obj.get("state") == "sentence_start":
                    reply_text = obj.get("text", "")
                if obj.get("type") == "tts" and obj.get("state") == "stop":
                    break
            check("第 9 条 · 空识别时**不调 LLM**（chat 计数不变）",
                  UP.get("chat_calls") == chat_before,
                  f"{chat_before} -> {UP.get('chat_calls')}")
            check("第 9 条 · 回的是「没听清」那类话",
                  bool(reply_text) and "没说" in reply_text or "听清" in reply_text,
                  f"reply={reply_text!r}")
    finally:
        UP.asr_text = FAKE_ASR_TEXT


async def t_local_asr(ids: dict) -> None:
    """第 15 组：本地 FunASR/SenseVoice 协议（``api_style=local_asr``）。

    验的是**真机部署形态**：管理页下拉选「本地 ASR（FunASR/SenseVoice）」保存后
    的那条配置 —— ``POST {endpoint}/asr`` + multipart ``audio`` 字段、无 model
    无密钥。这条路与 OpenAI 兼容路径除了 endpoint 之外毫无共同点，写错任何一处
    的症状都是「唤醒成功但一个字都识别不出」，而服务端日志只会说 404/400。

    断言三条，各自守一个失效方向：
      1. 请求真打到 ``/asr``（不是 ``/audio/transcriptions``）——路径拼错；
      2. 识别文本被上层采用（stt 事件 == 桩返回值）——报文解析错；
      3. OpenAI 兼容那条**一次都没被调用**——resolve 选错了行（priority 失效）。
    """
    print("── 15. 本地 ASR（FunASR/SenseVoice）协议 ──")
    import websockets

    await _settle()
    _set_asr_local_enabled(ids["asr_local_pk"], True)
    local_before = UP.get("asr_local_calls")
    openai_before = UP.get("asr_calls")
    headers = {"Authorization": f"Bearer {ids['token']}",
               "Device-Id": MAC_BOUND, "Protocol-Version": "1"}
    seen = {}
    try:
        async with ws_connect(WS_URL, open_timeout=10,
                                      headers=headers) as ws:
            await _handshake(ws, ids, 1)
            await _drain(ws)
            await ws.send(json.dumps({"type": "listen", "state": "start",
                                      "mode": "auto"}))
            await asyncio.sleep(0.2)
            for _ in range(6):
                await ws.send(os.urandom(40))
            await ws.send(json.dumps({"type": "listen", "state": "stop"}))

            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, bytes):
                    continue
                obj = json.loads(msg)
                if obj.get("type") in ("stt", "tts", "alert"):
                    seen[obj["type"]] = obj.get("text") or obj.get("state")
                if obj.get("type") == "tts" and obj.get("state") == "stop":
                    break
    finally:
        _set_asr_local_enabled(ids["asr_local_pk"], False)

    check("第 15 条 · 请求真打到本地协议的 /asr",
          UP.last_asr_path.endswith("/asr") and UP.get("asr_local_calls") > local_before,
          f"path={UP.last_asr_path} 调用 {local_before} -> {UP.get('asr_local_calls')}")
    check("第 15 条 · stt 采用本地 ASR 的识别结果",
          seen.get("stt") == FAKE_LOCAL_ASR_TEXT, f"stt={seen.get('stt')!r}")
    check("第 15 条 · OpenAI 兼容 ASR 一次都没被调用（resolve 选对了行）",
          UP.get("asr_calls") == openai_before,
          f"{openai_before} -> {UP.get('asr_calls')}")


async def t_quota(ids: dict) -> None:
    """第 6 条：**取消会员制后，已绑定设备不再被时长配额拦**。

    ⚠️ 这条断言的**方向**在取消会员制时翻转了，原来测的是
    「配额用尽 -> 不调 LLM + 收到 alert」。删功能不能把断言整条删掉 ——
    那正是「为了让测试变绿而丢掉真正有价值的断言」。改为测**反向不变量**：

        把 free_daily_seconds 压到 0（比任何真实用量都极端），
        已绑定设备**仍然要调 LLM、仍然要拿到正常回复**。

    这条守的是本次改动的核心：任何人把「按用户判会员/按用量拦人」的逻辑
    加回来，这里会立刻红。
    """
    print("── 6. 取消会员制：已绑定设备不被时长配额拦 ──")
    import websockets

    await _settle()
    _set_quota_limit(0)          # 上限压到 0：闸门若还在必拦
    chat_before = UP.get("chat_calls")
    headers = {"Authorization": f"Bearer {ids['token']}",
               "Device-Id": MAC_BOUND, "Protocol-Version": "1"}
    try:
        async with ws_connect(WS_URL, open_timeout=10,
                                      headers=headers) as ws:
            await _handshake(ws, ids, 1)
            await _drain(ws)
            await ws.send(json.dumps({"type": "listen", "state": "start",
                                      "mode": "auto"}))
            await asyncio.sleep(0.2)
            for _ in range(6):
                await ws.send(os.urandom(40))
            await ws.send(json.dumps({"type": "listen", "state": "stop"}))

            alert = ""
            spoke = False
            deadline = time.time() + 40
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=8)
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, bytes):
                    continue
                obj = json.loads(msg)
                if obj.get("type") == "alert":
                    alert = obj.get("message", "")
                    continue
                if obj.get("type") == "tts":
                    # 收到 tts 段（start/stop）说明真的走到播报了
                    if obj.get("state") in ("start", "stop"):
                        spoke = True
                    if spoke and obj.get("state") == "stop":
                        break
            check("第 6 条 · 上限压到 0 时**仍调 LLM**（chat 计数 +1）",
                  UP.get("chat_calls") == chat_before + 1,
                  f"{chat_before} -> {UP.get('chat_calls')}")
            check("第 6 条 · 上限压到 0 时仍拿到正常播报（未走降级）",
                  spoke, f"tts={spoke}")
            check("第 6 条 · 未收到「配额/时长用尽」类 alert",
                  not ("聊天时间" in alert or "明天" in alert),
                  f"alert={alert!r}")
    finally:
        _set_quota_limit(3600)   # 恢复，别影响后续断言


def t_usage_rows(ids: dict) -> None:
    print("── 5. 埋点：model_usage / chat_usage / Interaction ──")
    usage_rows = _db_count(
        "SELECT COUNT(*) FROM model_usage WHERE source='voice' AND usage='chat'")
    check("第 5 条 · model_usage 有 source='voice' + usage='chat' 的行",
          usage_rows >= 1, f"{usage_rows} 行")

    total = _db_count(
        "SELECT COUNT(*) FROM model_usage WHERE source='voice'"
        " AND total_tokens = :t", {"t": FAKE_TOTAL_TOKENS})
    check(f"第 5 条 · total_tokens == {FAKE_TOTAL_TOKENS}（网关真实提取，"
          f"不是估算）", total >= 1, f"{total} 行")

    dev_rows = _db_count(
        "SELECT COUNT(*) FROM model_usage WHERE source='voice' AND device_id = :d",
        {"d": ids["device_pk"]})
    check("第 5 条 · device_id 是这台设备", dev_rows >= 1, f"{dev_rows} 行")

    chat_rows = _db_count(
        "SELECT COUNT(*) FROM chat_usage WHERE device_id = :d",
        {"d": ids["device_pk"]})
    check("第 5 条 · chat_usage 有记录（陪聊时长入账）", chat_rows >= 1,
          f"{chat_rows} 行")

    inter = _db_count(
        "SELECT COUNT(*) FROM interactions WHERE device_id = :d AND kind = 1",
        {"d": ids["device_pk"]})
    check("第 5 条 · Interaction 有记录（kind=DIALOG）", inter >= 1,
          f"{inter} 行")

    meta_ok = _db_count(
        "SELECT COUNT(*) FROM interactions WHERE device_id = :d AND kind = 1"
        " AND meta LIKE '%session_id%' AND meta LIKE '%asr_ms%'",
        {"d": ids["device_pk"]})
    check("第 5 条 · Interaction.meta 带 session_id / asr_ms 等耗时埋点",
          meta_ok >= 1, f"{meta_ok} 行")


def t_activation_table() -> None:
    print("── 1c. 激活码表：防刷不重复建 ──")
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import ActivationCode
    from app.macs import mac_variants, norm_mac

    with SessionLocal() as db:
        # ⚠️ 必须按**归一化键**计数，不能用 MAC_UNBOUND 原值。
        # MAC_UNBOUND 是 "AA:BB:CC:DD:EE:02"（大写带冒号），而入库一律走
        # `_ensure_activation_code` 里的 `norm_mac()`（→ "aabbccddee02"）。
        # 按原值等值查永远是 0 条，会让这条断言**假红**——它红的时候
        # 代码其实是对的，而真正的「重复建码」反而可能被它漏掉。
        # 用 mac_variants 覆盖全部历史写法，无论库里存成哪种都能数到。
        n = db.scalar(
            select(func.count()).select_from(ActivationCode).where(
                ActivationCode.device_id.in_(mac_variants(MAC_UNBOUND)))) or 0
    check("1c · 未绑定 MAC 的激活码只建了一条（多次申领复用）", n == 1,
          f"{n} 条（查键 {norm_mac(MAC_UNBOUND)}）")


# ================================================================ 12. 工具调用
async def t_tools_reminder(ids: dict) -> None:
    print("── 12a. 工具调用：老人说「明天早上八点提醒我吃药」→ 真建提醒 ──")
    await _settle()
    tomorrow = _tomorrow()
    before = _reminders(ids["device_pk"])
    chat_before = UP.get("chat_calls")
    UP.asr_text = "小暖小暖，明天早上八点提醒我吃药"
    UP.plan = [
        {"tool": {"name": "create_reminder",
                  "arguments": {"title": "吃药", "when": "明天早上八点"}},
         "usage": {"prompt": 100, "completion": 20}},
        {"text": "记好了，明天早上八点提醒您吃药。",
         "usage": {"prompt": 110, "completion": 25}},
    ]
    ws, sid = await _connect(ids)
    try:
        sentences, _events, _bins = await _one_turn(ws, sid)
    finally:
        await ws.close()
    rows = _reminders(ids["device_pk"])
    new = [r for r in rows if r["title"] == "吃药"]
    check("12a ★ 语音建提醒：reminders 表真多了一行「吃药」"
          "（不是只在口头答应）",
          len(new) == 1 and len(rows) == len(before) + 1,
          f"{len(before)} -> {len(rows)} 行")
    if new:
        r = new[0]
        check("12a · 时间是口语解析出来的：times_of_day == ['08:00']",
              r["times"] == ["08:00"], str(r["times"]))
        check("12a · once_date == 明天（一次性提醒，不是每天）",
              r["once_date"] == tomorrow, f"{r['once_date']} vs {tomorrow}")
        check("12a · 默认启用", r["enabled"] is True, str(r["enabled"]))
    check("12a · 两轮 LLM（工具轮 + 口语确认轮）",
          UP.get("chat_calls") == chat_before + 2,
          f"{chat_before} -> {UP.get('chat_calls')}")
    check("12a · 回复是把时间念清楚的确认句",
          any("八点" in s for s in sentences), str(sentences))
    meta = _last_interaction_meta(ids["device_pk"])
    calls = meta.get("tool_calls") or []
    check("12a · Interaction.meta 记录工具轨迹（name/ok/round）",
          len(calls) == 1 and calls[0].get("name") == "create_reminder"
          and calls[0].get("ok") is True and calls[0].get("round") == 1,
          str(calls))
    check("12a · meta.llm_rounds == 2（逐轮记账的口径）",
          meta.get("llm_rounds") == 2, str(meta.get("llm_rounds")))
    n120 = _db_count("SELECT COUNT(*) FROM model_usage WHERE source='voice'"
                     " AND total_tokens = 120")
    n135 = _db_count("SELECT COUNT(*) FROM model_usage WHERE source='voice'"
                     " AND total_tokens = 135")
    check("12a · 两轮 token 各记一行（120 / 135，不是合成一行）",
          n120 >= 1 and n135 >= 1, f"120:{n120} 135:{n135}")
    UP.plan = []
    UP.asr_text = FAKE_ASR_TEXT


async def t_tools_clarify(ids: dict) -> None:
    print("── 12b. 歧义时间：「三点」必须反问，绝不静默建错提醒 ──")
    await _settle()
    before = _reminders(ids["device_pk"])
    chat_before = UP.get("chat_calls")
    UP.asr_text = "小暖小暖，提醒我三点吃药"
    UP.plan = [
        # LLM 按老人原话把 when 填成「三点」——歧义，服务端必须 need_clarify
        {"tool": {"name": "create_reminder",
                  "arguments": {"title": "吃药", "when": "三点"}},
         "usage": {"prompt": 100, "completion": 20}},
        {"text": "您说的是下午三点还是凌晨三点？我得问清楚才好记。",
         "usage": {"prompt": 110, "completion": 25}},
    ]
    ws, sid = await _connect(ids)
    try:
        sentences, _events, _bins = await _one_turn(ws, sid)
    finally:
        await ws.close()
    rows = _reminders(ids["device_pk"])
    check("12b ★ 「三点」歧义：**没有**建任何提醒"
          "（时间错的服药提醒比没有提醒更糟）",
          len(rows) == len(before), f"{len(before)} -> {len(rows)} 行")
    check("12b · 澄清结果照常喂回 LLM 并拿到回复（两轮）",
          UP.get("chat_calls") == chat_before + 2,
          f"{chat_before} -> {UP.get('chat_calls')}")
    check("12b · 回复是反问澄清（不是确认句）",
          any("三点" in s and ("？" in s or "还是" in s) for s in sentences),
          str(sentences))
    meta = _last_interaction_meta(ids["device_pk"])
    calls = meta.get("tool_calls") or []
    check("12b · meta 里这次工具调用 ok=False（走了 need_clarify 分支）",
          len(calls) == 1 and calls[0].get("name") == "create_reminder"
          and calls[0].get("ok") is False, str(calls))
    UP.plan = []
    UP.asr_text = FAKE_ASR_TEXT


async def t_tools_delete_toggle(ids: dict) -> None:
    print("── 12c. 暂停 / 删除提醒 ──")
    await _settle()
    UP.asr_text = "小暖小暖，明天早上八点提醒我喝水"
    UP.plan = [
        {"tool": {"name": "create_reminder",
                  "arguments": {"title": "喝水", "when": "明天早上八点"}}},
        {"text": "记好了，明天早上八点提醒您喝水。"},
    ]
    ws, sid = await _connect(ids)
    try:
        await _one_turn(ws, sid)
    finally:
        await ws.close()
    row = next((r for r in _reminders(ids["device_pk"])
                if r["title"] == "喝水"), None)
    check("12c · 先建一条「喝水」", row is not None, str(row))

    UP.plan = [
        {"tool": {"name": "toggle_reminder",
                  "arguments": {"title": "喝水", "enabled": False}}},
        {"text": "好，喝水的提醒先暂停，需要时再说一声。"},
    ]
    ws, sid = await _connect(ids)
    try:
        sentences, _events, _bins = await _one_turn(ws, sid)
    finally:
        await ws.close()
    row = next((r for r in _reminders(ids["device_pk"])
                if r["title"] == "喝水"), None)
    check("12c · toggle_reminder 暂停成功（enabled=False）",
          row is not None and row["enabled"] is False, str(row))
    check("12c · 暂停后有口语反馈", any("暂停" in s for s in sentences),
          str(sentences))

    UP.plan = [
        {"tool": {"name": "delete_reminder",
                  "arguments": {"title": "喝水"}}},
        {"text": "已经删掉了喝水的提醒。"},
    ]
    ws, sid = await _connect(ids)
    try:
        sentences, _events, _bins = await _one_turn(ws, sid)
    finally:
        await ws.close()
    rows = _reminders(ids["device_pk"])
    check("12c · delete_reminder 删除成功（表里真没有了）",
          not any(r["title"] == "喝水" for r in rows),
          str([r["title"] for r in rows]))
    UP.plan = []
    UP.asr_text = FAKE_ASR_TEXT


async def t_tools_loop_cap(ids: dict) -> None:
    print("── 12d. 工具循环轮数上限（防 token 失控）──")
    await _settle()
    from app import config as cfg

    chat_before = UP.get("chat_calls")
    # 脚本比上限多给几条：全被消费掉就说明没设上限
    UP.plan = [{"tool": {"name": "list_reminders", "arguments": {}}}
               for _ in range(cfg.VOICE_TOOL_MAX_ROUNDS + 3)]
    ws, sid = await _connect(ids)
    try:
        sentences, events, _bins = await _one_turn(ws, sid)
    finally:
        await ws.close()
    check(f"12d · 模型反复要工具时最多 {cfg.VOICE_TOOL_MAX_ROUNDS} 轮就停"
          f"（不多花 token）",
          UP.get("chat_calls") == chat_before + cfg.VOICE_TOOL_MAX_ROUNDS,
          f"{chat_before} -> {UP.get('chat_calls')}")
    check("12d ★ 轮数用尽时兜底话术**真的被念出来**"
          "（只赋给 reply 不推给 speaker 的话老人听到静音）",
          any("没办好" in s for s in sentences), str(sentences))
    kinds = [(e[1], e[2]) for e in events if e[0] == "json"]
    check("12d · 兜底轮协议仍完整（tts start/stop 配对，设备能回落 idle）",
          ("tts", "start") in kinds and ("tts", "stop") in kinds,
          str(kinds))
    meta = _last_interaction_meta(ids["device_pk"])
    check("12d · meta.tool_calls 记满每一轮（便于对账为什么多花了 token）",
          len(meta.get("tool_calls") or []) == cfg.VOICE_TOOL_MAX_ROUNDS,
          str(meta.get("tool_calls")))

    # 会话还活着：兜底之后还能正常对话
    UP.plan = []
    ws, sid = await _connect(ids)
    try:
        sentences2, _events2, _bins2 = await _one_turn(ws, sid)
    finally:
        await ws.close()
    check("12d · 兜底后会话仍可正常对话（又拿到一句回复）",
          bool(sentences2), str(sentences2))
    UP.plan = []


async def t_tools_protocols(ids: dict) -> None:
    print("── 12e. 四协议 tools 报文：OpenAI / Ollama / Anthropic / Gemini ──")
    await _settle()
    tomorrow = _tomorrow()
    labels = {"openai_compatible": "OpenAI 兼容", "ollama": "Ollama",
              "anthropic": "Anthropic", "gemini": "Gemini"}
    pks = ids["llm_pks"]
    try:
        for style, label in labels.items():
            # 只留这一条 llm 配置：resolve_plan 按序兜底，
            # 不禁用其它三条的话测的永远只是第一条（OpenAI）。
            _set_llm_enabled(pks.values(), False)
            _set_llm_enabled([pks[style]], True)
            await _settle(0.3)
            title = f"测血压-{label}"
            chat_before = UP.get("chat_calls")
            UP.asr_text = f"小暖小暖，明天早上八点提醒我{title}"
            UP.plan = [
                {"tool": {"name": "create_reminder",
                          "arguments": {"title": title,
                                        "when": "明天早上八点"}},
                 "usage": {"prompt": 100, "completion": 20}},
                {"text": f"记好了，明天早上八点提醒您{title}。",
                 "usage": {"prompt": 110, "completion": 25}},
            ]
            ws, sid = await _connect(ids)
            try:
                sentences, _events, _bins = await _one_turn(ws, sid)
            finally:
                await ws.close()
            row = next((r for r in _reminders(ids["device_pk"])
                        if r["title"] == title), None)
            check(f"12e · {label}：tool_calls 真建成提醒"
                  f"（times=08:00 / once=明天）",
                  row is not None and row["times"] == ["08:00"]
                  and row["once_date"] == tomorrow, str(row))
            check(f"12e · {label}：工具结果喂回后拿到口语确认",
                  any(title in s for s in sentences), str(sentences))
            check(f"12e · {label}：两轮 LLM（工具轮 + 回复轮）",
                  UP.get("chat_calls") == chat_before + 2,
                  f"{chat_before} -> {UP.get('chat_calls')}")

            # 第二轮请求体里必须带着**翻译成该协议形状**的工具消息 ——
            # 这是四协议差异最大的一处，漏翻译的表现是第二轮 400 或
            # 模型"失忆"（它看不到自己刚调用过工具）。
            body = UP.chat_bodies[-1] if UP.chat_bodies else {}
            if style == "openai_compatible":
                ok = any(m.get("role") == "tool"
                         for m in (body.get("messages") or []))
            elif style == "ollama":
                ok = any(m.get("role") == "tool"
                         and m.get("tool_name") == "create_reminder"
                         for m in (body.get("messages") or []))
            elif style == "anthropic":
                ok = any(
                    isinstance(m.get("content"), list)
                    and any(isinstance(b, dict)
                            and b.get("type") == "tool_result"
                            for b in m["content"])
                    for m in (body.get("messages") or []))
            else:
                ok = any(
                    any(isinstance(p, dict) and "functionResponse" in p
                        for p in (m.get("parts") or []))
                    for m in (body.get("contents") or []))
            check(f"12e · {label}：第二轮请求带该协议形状的工具结果消息",
                  ok, json.dumps(body, ensure_ascii=False)[:200])
    finally:
        _set_llm_enabled(pks.values(), True)
        UP.plan = []
        UP.asr_text = FAKE_ASR_TEXT


# ================================================================ 13. 流式首句延迟
async def t_stream_latency(ids: dict, libopus) -> None:
    print("── 13. 流式 TTS：首句延迟必须早于整段完成 ──")
    await _settle()
    reply_chunks = ["第一句话。", "第二句话。", "第三句话。"]
    n_chat_before = len(UP.chat_events)
    n_tts_before = len(UP.tts_events)
    UP.plan = [{"text": "".join(reply_chunks), "chunks": reply_chunks,
                "delay": LLM_CHUNK_DELAY_SEC,
                "usage": {"prompt": 100, "completion": 30}}]
    UP.tts_chunk_delay = TTS_CHUNK_DELAY_SEC
    UP.tts_chunks = TTS_CHUNKS_PER_SENTENCE

    backup = None
    if not libopus:
        # 无 libopus 时下行发不出真 Opus 帧：把 encode 换成定长假包。
        # 替身只替换"编码"这一个叶子，时序/协议全部真跑；断言名里注明。
        backup = _patch_opus()
    try:
        ws, sid = await _connect(ids)
        try:
            await ws.send(json.dumps({"session_id": sid, "type": "listen",
                                      "state": "start", "mode": "auto"}))
            await asyncio.sleep(0.25)
            for _ in range(6):
                await ws.send(FAKE_OPUS_PACKET)
                await asyncio.sleep(0.02)
            t_stop = time.monotonic()
            await ws.send(json.dumps({"session_id": sid, "type": "listen",
                                      "state": "stop"}))

            sentences: list[str] = []
            events: list[tuple] = []
            first_frame_at: float | None = None
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, bytes):
                    if first_frame_at is None:
                        first_frame_at = time.monotonic()
                    events.append(("bin", len(msg)))
                    continue
                obj = json.loads(msg)
                events.append(("json", str(obj.get("type")),
                               str(obj.get("state")), str(obj.get("text") or "")))
                if obj.get("type") == "tts" and obj.get("state") == "sentence_start":
                    sentences.append(str(obj.get("text") or ""))
                if obj.get("type") == "tts" and obj.get("state") == "stop":
                    break
        finally:
            await ws.close()
    finally:
        if backup is not None:
            _restore_opus(backup)
        UP.plan = []
        UP.tts_chunk_delay = 0.0
        UP.tts_chunks = 1

    chat_evs = UP.chat_events[n_chat_before:]
    tts_evs = UP.tts_events[n_tts_before:]
    check("13 · 三句回复逐句到达（增量分句真的在工作）",
          sentences == reply_chunks, str(sentences))
    kinds = [(e[1], e[2]) for e in events if e[0] == "json"]
    check("13 · 下行时序与旧实现逐字一致"
          "（stt → tts start → 每句 sentence_start+llm emotion → tts stop）",
          kinds == [("stt", "None"),
                    ("tts", "start"),
                    ("tts", "sentence_start"), ("llm", "None"),
                    ("tts", "sentence_start"), ("llm", "None"),
                    ("tts", "sentence_start"), ("llm", "None"),
                    ("tts", "stop")], str(kinds))
    first_bin = next((i for i, e in enumerate(events) if e[0] == "bin"), -1)
    first_ss = next((i for i, e in enumerate(events)
                     if e[0] == "json" and e[1] == "tts"
                     and e[2] == "sentence_start"), -1)
    check("13 · 第一帧音频出现在第一句 sentence_start 之后"
          "（先文本后音频，设备才能显示字幕）",
          first_bin > first_ss >= 0,
          f"first_bin={first_bin} first_sentence_start={first_ss}")

    streamed = bool(chat_evs) and chat_evs[0].get("streamed")
    check("13 · 网关确实走了流式分支（假上游回了 SSE，不是整段降级）",
          streamed and len(chat_evs) == 1, f"streamed={streamed}")
    last_body = UP.chat_bodies[-1] if UP.chat_bodies else {}
    check("13 · 请求体带 stream=true + tools（流式与工具同时生效）",
          last_body.get("stream") is True
          and isinstance(last_body.get("tools"), list)
          and last_body.get("stream_options", {}).get("include_usage") is True,
          f"stream={last_body.get('stream')} "
          f"tools={bool(last_body.get('tools'))}")

    if first_frame_at is None or not chat_evs or not tts_evs:
        check("13 ★ 首句延迟：收到了第一帧音频（流式链路通）", False,
              f"first_frame={first_frame_at} chat={len(chat_evs)} "
              f"tts={len(tts_evs)}")
        return
    llm_done = chat_evs[0]["done_at"]
    tts0 = tts_evs[0]
    first_ms = int((first_frame_at - t_stop) * 1000)
    llm_total_ms = int((llm_done - t_stop) * 1000)
    tts_first_ms = int((tts0["first_at"] - tts0["req_at"]) * 1000)
    # 非流式的首帧至少要等：整段 LLM + 第一句 TTS 的首分片
    nonstream_first_ms = llm_total_ms + tts_first_ms
    print(f"        · 首句延迟（listen stop → 第一帧）= {first_ms}ms")
    print(f"        · 整段 LLM 生成完 = {llm_total_ms}ms；"
          f"TTS 首分片 = {tts_first_ms}ms ⇒ 非流式至少 {nonstream_first_ms}ms")
    check("13 ★ 首句延迟：第一帧早于 LLM 整段生成完成"
          "（流式 LLM 生效，不等整段回复）",
          first_frame_at < llm_done,
          f"首帧 {first_ms}ms < 整段 LLM {llm_total_ms}ms")
    check("13 ★ 首句延迟：第一帧早于第一句 TTS 整段合成完成"
          "（流式 TTS 生效，边收边发）",
          first_frame_at < tts0["done_at"],
          f"首帧 {first_ms}ms < 第一句合成完 "
          f"{int((tts0['done_at'] - t_stop) * 1000)}ms")
    check("13 ★ 首句延迟实测值严格小于非流式口径"
          f"（{first_ms}ms < {nonstream_first_ms}ms）",
          first_ms < nonstream_first_ms,
          f"流式 {first_ms}ms vs 非流式 {nonstream_first_ms}ms")


# ================================================================ 14. 老人 VAD
async def _vad_round(ws, sid: str, script: list[float], *, version: int = 1):
    """按 RMS 脚本逐帧发送（**不发 listen stop**），观察服务端何时断句。

    返回 (stt_frame_idx, sentences, events)。stt_frame_idx 是"收到 stt 时
    已经发了多少帧"—— None 表示发送全部结束后才到（没提前断句）。
    """
    sent = 0
    stt_idx: int | None = None
    sentences: list[str] = []
    events: list[tuple] = []
    for rms in script:
        if version == 1:
            blob = FAKE_OPUS_PACKET
        elif version == 2:
            blob = _frame_v2(FAKE_OPUS_PACKET)
        else:
            blob = _frame_v3(FAKE_OPUS_PACKET)
        await ws.send(blob)
        sent += 1
        await asyncio.sleep(0.02)
        # 非阻塞扫一眼：stt 在发送途中到达 == 提前断句
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=0.001)
            except asyncio.TimeoutError:
                break
            except Exception:
                raise
            if isinstance(msg, bytes):
                events.append(("bin", len(msg)))
                continue
            obj = json.loads(msg)
            events.append(("json", str(obj.get("type")), str(obj.get("state")),
                           str(obj.get("text") or "")))
            if obj.get("type") == "stt" and stt_idx is None:
                stt_idx = sent
            if obj.get("type") == "tts" and obj.get("state") == "sentence_start":
                sentences.append(str(obj.get("text") or ""))
            if obj.get("type") == "tts" and obj.get("state") == "stop":
                return stt_idx, sentences, events, sent
    # 发送完了：继续等到 tts stop（断句 + 整轮播报结束）
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
        except asyncio.TimeoutError:
            break
        if isinstance(msg, bytes):
            events.append(("bin", len(msg)))
            continue
        obj = json.loads(msg)
        events.append(("json", str(obj.get("type")), str(obj.get("state")),
                       str(obj.get("text") or "")))
        if obj.get("type") == "tts" and obj.get("state") == "sentence_start":
            sentences.append(str(obj.get("text") or ""))
        if obj.get("type") == "tts" and obj.get("state") == "stop":
            break
    return stt_idx, sentences, events, sent


async def t_elder_pause(ids: dict) -> None:
    print("── 14a. 句中停顿：老人说着想一下，不许被截断 ──")

    # 300ms 说话 → 1800ms 停顿（想一下）→ 继续 300ms → 说完整段
    # 旧实现静态阈值 1500ms 会在第一个停顿处断句，后半句直接丢失；
    # 新实现：1800ms 停顿记入"最长句中停顿"，阈值自适应放大到
    # 1800×1.5+500 = 3200ms，第二段静音要 3200ms（54 帧）才断，
    # 所以尾部给 60 帧静音（留 6 帧余量）。
    script = ([LOUD_RMS] * 5 + [0.0] * 30 + [LOUD_RMS] * 5 + [0.0] * 60)
    backup = _patch_opus(rms_script=script)
    try:
        ws, sid = await _connect(ids)
        try:
            await ws.send(json.dumps({"session_id": sid, "type": "listen",
                                      "state": "start", "mode": "auto"}))
            stt_idx, sentences, _events, sent = await _vad_round(ws, sid, script)
        finally:
            await ws.close()
    finally:
        _restore_opus(backup)
    idx = stt_idx if stt_idx is not None else sent
    check("14a ★ 1800ms 句中停顿没有提前断句"
          "（旧 1500ms 静态阈值会在这里把后半句丢掉）",
          idx > 45, f"stt 在第 {idx}/{sent} 帧到达")
    check("14a · 停顿后继续说的内容仍在同一轮里（只识别一轮）",
          bool(sentences), str(sentences))
    check("14a · 阈值确实被自适应放大（日志里的 阈值 3200ms）",
          _log_has("阈值 3200ms"), "")


async def t_elder_soft_voice(ids: dict) -> None:
    print("── 14b. 轻声/远场：整句 RMS 0.007（旧门限 0.01 会整句丢失）──")
    # 0.007 > 新 enter 门限 0.005（-46dBFS 附近），< 旧静态门限 0.01。
    # 旧实现下这句一个帧都不算语音 ⇒ _speech_ms=0 ⇒ 永远不 finalize，
    # 只能等 VOICE_MAX_LISTEN_MS（60s）兜底 —— 老人干等一分钟。
    script = [QUIET_RMS] * 10 + [0.0] * 40
    backup = _patch_opus(rms_script=script)
    try:
        ws, sid = await _connect(ids)
        try:
            await ws.send(json.dumps({"session_id": sid, "type": "listen",
                                      "state": "start", "mode": "auto"}))
            t0 = time.time()
            _idx, _sentences, events, _sent = await _vad_round(ws, sid, script)
        finally:
            await ws.close()
    finally:
        _restore_opus(backup)
    got_stt = any(e[0] == "json" and e[1] == "stt" for e in events)
    check("14b ★ 轻声整句被当成语音并正常断句（10s 内走完整轮，"
          "不是干等 60s 兜底）",
          got_stt and (time.time() - t0) < 10,
          f"stt={got_stt} 用时 {time.time() - t0:.1f}s")
    check("14b · 累计语音时长记到 600ms（日志里的 累计语音 600ms）",
          _log_has("累计语音 600ms"), "")
    check("14b · config 门限确实低于旧值 0.01（改回去这条就该红）",
          _cfg_less("VOICE_SPEECH_RMS", 0.01),
          f"VOICE_SPEECH_RMS={_cfg_get('VOICE_SPEECH_RMS')}")


async def t_elder_hysteresis(ids: dict) -> None:
    print("── 14c. 迟滞：句中轻声音节不掉出语音态 ──")
    # 300ms 正常说话 → 1980ms 软语音（高于 keep 0.0026、低于 enter）→ 静音。
    # 软语音期限 2000ms 内不掉出语音态 ⇒ 累计语音 300+1980=2280ms；
    # 若没有迟滞（软帧算静音），累计语音只有 300ms。
    script = [LOUD_RMS] * 5 + [SOFT_RMS] * 33 + [0.0] * 40
    backup = _patch_opus(rms_script=script)
    try:
        ws, sid = await _connect(ids)
        try:
            await ws.send(json.dumps({"session_id": sid, "type": "listen",
                                      "state": "start", "mode": "auto"}))
            _idx, sentences, events, _sent = await _vad_round(ws, sid, script)
        finally:
            await ws.close()
    finally:
        _restore_opus(backup)
    got_stt = any(e[0] == "json" and e[1] == "stt" for e in events)
    check("14c · 软语音段后续仍能正常断句播报", got_stt and bool(sentences),
          str(sentences))
    check("14c ★ 软语音帧计入了累计语音（日志 累计语音 2280ms；"
          "没有迟滞的话只有 300ms）",
          _log_has("累计语音 2280ms"), "")


async def t_elder_noise(ids: dict) -> None:
    print("── 14d. 底噪不挂死会话：软语音期限到期后必须能断句 ──")
    # 说过话之后来一段持续"类底噪"能量（高于 keep）：软语音期限
    # （VOICE_SOFT_GRACE_MS=2000ms）内算语音，到期后必须落回静音并断句。
    # 若期限失效（软帧永远算语音），会话只能等 60s 收听上限 —— 本断言 12s
    # 判死，正是为了抓住那种"看着没崩、其实挂死"的回归。
    script = [LOUD_RMS] * 5 + [NOISE_RMS] * 250
    backup = _patch_opus(rms_script=script)
    try:
        ws, sid = await _connect(ids)
        try:
            await ws.send(json.dumps({"session_id": sid, "type": "listen",
                                      "state": "start", "mode": "auto"}))
            t0 = time.time()
            _idx, _sentences, events, _sent = await _vad_round(ws, sid, script)
        finally:
            await ws.close()
    finally:
        _restore_opus(backup)
    got_stt = any(e[0] == "json" and e[1] == "stt" for e in events)
    check("14d ★ 持续底噪能量不会让会话挂死（12s 内断句并播报）",
          got_stt and (time.time() - t0) < 12,
          f"stt={got_stt} 用时 {time.time() - t0:.1f}s")


def _cfg_get(name: str):
    from app import config
    return getattr(config, name)


def _cfg_less(name: str, bound: float) -> bool:
    try:
        return float(_cfg_get(name)) < bound
    except (TypeError, ValueError):
        return False


def t_parse_when_units() -> None:
    print("── 12f. 口语时间解析单元断言（歧义必须 ok=False）──")
    from app.schedule import local_now

    from app.voice.tools import parse_when

    tomorrow = _tomorrow()
    day_after = (local_now().date() + timedelta(days=3)).isoformat()

    p = parse_when("明天早上八点")
    check("12f · 「明天早上八点」→ 08:00 / 明天 / 一次性",
          p.ok and p.times_of_day == ["08:00"] and p.once_date == tomorrow,
          f"ok={p.ok} times={p.times_of_day} date={p.once_date}")

    p = parse_when("晚上八点半")
    check("12f · 「晚上八点半」→ 20:30（时段词定小时）",
          p.ok and p.times_of_day == ["20:30"], f"times={p.times_of_day}")

    p = parse_when("大后天早上八点")
    check("12f · 「大后天」→ +3 天（顺序 bug 会算成 +2 天）",
          p.ok and p.once_date == day_after, f"date={p.once_date}")

    p = parse_when("每周三晚上八点半")
    check("12f · 「每周三晚上八点半」→ 每周三 20:30",
          p.ok and p.weekdays == [3] and p.times_of_day == ["20:30"],
          f"weekdays={p.weekdays} times={p.times_of_day}")

    p = parse_when("早饭后")
    check("12f · 「早饭后」→ 约定 07:30", p.ok and p.times_of_day == ["07:30"],
          f"times={p.times_of_day}")

    p = parse_when("三点")
    check("12f ★ 「三点」歧义（下午 vs 凌晨）→ ok=False 必须反问",
          (not p.ok) and p.reason, f"ok={p.ok} reason={p.reason}")

    p = parse_when("每天")
    check("12f ★ 「每天」缺时间 → ok=False 必须反问",
          (not p.ok) and p.reason, f"ok={p.ok} reason={p.reason}")

    p = parse_when("昨天早上八点")
    check("12f · 「昨天」是过去时间 → ok=False",
          not p.ok, f"ok={p.ok}")

    p = parse_when("待会提醒我")
    check("12f · 「待会」相对时间 → ok=False 反问具体几点",
          not p.ok, f"ok={p.ok}")


# ================================================================ main
# ================================================================= 10. 二进制帧封装
# 无 libopus 的机器上，用 **替身** 跑通整条二进制链路。
# 为什么这不算"假装全绿"：
#   ① 替身只替换 decode/encode 两个**叶子函数**，封帧/剥帧/静音判定/
#      播报时序这些**真正的协议代码**全都真跑；
#   ② 断言名与 CI 的 round-trip 断言**分开命名**，输出里能一眼看出哪些是
#      真编解码、哪些是替身覆盖；
#   ③ 替身跑通 + CI 真跑 = 两条互补的证据，不是一条假证据。
FAKE_OPUS_PACKET = b"\xaa" * 60


def _patch_opus(rms_script=None, silent_after: int = 10**9) -> object:
    """把 opus_codec 的两个叶子函数换成替身，返回可恢复的备份。

    :param rms_script: None=旧行为（前 silent_after 帧有声，之后静音）；
        float=全程一个 RMS；list=按帧取（用尽重复最后一个）—— 第 14 条
        的老人场景（停顿/轻声/软语音/底噪）靠它注入能量，**零真实音频**。
    """
    from app.voice import opus_codec

    backup = (opus_codec.encode_opus, opus_codec.decode_opus)
    counter = {"n": 0}

    def fake_encode(pcm: bytes, sr: int, channels: int = 1) -> bytes:
        return FAKE_OPUS_PACKET

    def fake_decode(data: bytes, sr: int, channels: int = 1) -> bytes:
        i = counter["n"]
        counter["n"] += 1
        if rms_script is None:
            if i < silent_after:
                return _pcm_frame(0.06)          # 有声
            return b"\x00" * int(sr * 0.06) * 2  # 静音
        if isinstance(rms_script, (int, float)):
            rms = float(rms_script)
        else:
            rms = float(rms_script[min(i, len(rms_script) - 1)])
        return _pcm_rms_bytes(int(sr * 0.06) * 2, rms)

    opus_codec.encode_opus = fake_encode
    opus_codec.decode_opus = fake_decode
    return backup


def _restore_opus(backup) -> None:
    from app.voice import opus_codec

    opus_codec.encode_opus, opus_codec.decode_opus = backup


def _frame_v2(payload: bytes) -> bytes:
    return struct.pack(">HHIII", 2, 0, 0, 0, len(payload)) + payload


def _frame_v3(payload: bytes) -> bytes:
    return struct.pack(">BBH", 0, 0, len(payload)) + payload


async def t_binary_framing(ids: dict) -> None:
    print("── 10. 二进制帧封装 v1/v2/v3 + 服务端静音兜底（编解码用替身）──")
    import websockets

    backup = _patch_opus()
    try:
        for version in (1, 2, 3):
            headers = {"Authorization": f"Bearer {ids['token']}",
                       "Device-Id": MAC_BOUND,
                       "Protocol-Version": str(version)}
            async with ws_connect(WS_URL, open_timeout=10,
                                          headers=headers) as ws:
                hello = await _handshake(ws, ids, version)
                await _drain(ws)
                sid = hello.get("session_id", "")
                got_bin, got_json = await _one_round(
                    ws, sid, version, [FAKE_OPUS_PACKET] * 6)

                label = f"v{version}"
                check(f"第 10 条 · {label} 收到二进制帧",
                      len(got_bin) > 0, f"{len(got_bin)} 帧")
                if not got_bin:
                    continue
                blob = got_bin[0]
                if version == 1:
                    check(f"第 10 条 · {label} 帧就是裸 Opus 包（无头）",
                          blob == FAKE_OPUS_PACKET, f"{len(blob)}B")
                elif version == 2:
                    ok = len(blob) >= 16
                    ver, ftype, _res, _ts, size = struct.unpack(
                        ">HHIII", blob[:16]) if ok else (0, 0, 0, 0, 0)
                    check(f"第 10 条 ★ {label} 头 16 字节：version=2 type=0 "
                          f"且 payload_size 与实际长度一致",
                          ok and ver == 2 and ftype == 0 and size == len(blob) - 16,
                          f"ver={ver} type={ftype} size={size} len={len(blob)}")
                    check(f"第 10 条 · {label} 负载逐字等于我们发的包",
                          blob[16:16 + size] == FAKE_OPUS_PACKET, "")
                else:
                    ok = len(blob) >= 4
                    ftype, _res, size = struct.unpack(">BBH", blob[:4]) if ok                         else (0, 0, 0)
                    check(f"第 10 条 ★ {label} 头 4 字节：type=0 "
                          f"且 payload_size 与实际长度一致",
                          ok and ftype == 0 and size == len(blob) - 4,
                          f"type={ftype} size={size} len={len(blob)}")
                    check(f"第 10 条 · {label} 负载逐字等于我们发的包",
                          blob[4:4 + size] == FAKE_OPUS_PACKET, "")
                check(f"第 10 条 · {label} 这一轮仍收到 stt/tts（协议没走歪）",
                      any(o.get("type") == "stt" for o in got_json)
                      and any(o.get("type") == "tts" for o in got_json),
                      str([o.get("type") for o in got_json]))
    finally:
        _restore_opus(backup)

    # ---- 服务端静音兜底：只发有声帧然后发静音，**不发 listen stop**
    print("── 10b. 服务端静音兜底（丢 listen stop 也能 finalize）──")
    backup = _patch_opus(silent_after=6)
    try:
        from app import config as cfg

        headers = {"Authorization": f"Bearer {ids['token']}",
                   "Device-Id": MAC_BOUND, "Protocol-Version": "1"}
        async with ws_connect(WS_URL, open_timeout=10,
                                      headers=headers) as ws:
            hello = await _handshake(ws, ids, 1)
            await _drain(ws)
            sid = hello.get("session_id", "")
            # 50 帧 = 6 有声 + 44 静音（2640ms > 阈值 2000ms）。
            # 帧数随阈值走：阈值调到 >2640ms 时这里必须同步加帧，
            # 否则"兜底"断言会因为静音不够而失败 —— 那正是想要的红。
            got_bin, got_json = await _one_round(
                ws, sid, 1, [FAKE_OPUS_PACKET] * 50, send_stop=False)
            check(f"第 10b 条 ★ 丢失 listen stop 时服务端静音兜底仍 finalize"
                  f"（阈值 {cfg.VOICE_SILENCE_MS}ms）",
                  any(o.get("type") == "stt" for o in got_json),
                  str([o.get("type") for o in got_json]))
            check("第 10b 条 · 兜底那一轮也照常播报（tts start/start 配对）",
                  any(o.get("type") == "tts" and o.get("state") == "stop"
                      for o in got_json), "")
    finally:
        _restore_opus(backup)


async def _one_round(ws, session_id, version, frames, *, send_stop=True):
    """跑一轮"收音 -> 播报"，返回 (收到的二进制帧, 收到的 JSON 列表)。"""
    await ws.send(json.dumps({"session_id": session_id, "type": "listen",
                              "state": "start", "mode": "auto"}))
    await asyncio.sleep(0.25)
    for payload in frames:
        if version == 2:
            blob = _frame_v2(payload)
        elif version == 3:
            blob = _frame_v3(payload)
        else:
            blob = payload
        await ws.send(blob)
        await asyncio.sleep(0.02)
    if send_stop:
        await ws.send(json.dumps({"session_id": session_id, "type": "listen",
                                  "state": "stop"}))
    got_bin: list[bytes] = []
    got_json: list[dict] = []
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
        except asyncio.TimeoutError:
            break
        if isinstance(msg, bytes):
            got_bin.append(msg)
            continue
        obj = json.loads(msg)
        got_json.append(obj)
        if obj.get("type") == "tts" and obj.get("state") == "stop":
            break
    return got_bin, got_json


def t_codec_contract() -> None:
    print("── 11. libopus 缺失时的契约（文案可执行 / 抛错而非静默）──")
    from app.voice import opus_codec

    if opus_codec.libopus_available():
        skip("第 11 条 · OpusUnavailableError 文案断言",
             "本机有 libopus，缺失分支走不到（CI 上同理）")
    else:
        raised = ""
        try:
            opus_codec.encode_opus(b"\x00" * 100, 16000)
        except opus_codec.OpusUnavailableError as exc:
            raised = str(exc)
        except Exception as exc:  # noqa: BLE001
            raised = f"{type(exc).__name__}: {exc}"
        check("第 11 条 ★ 无 libopus 时 encode 抛 OpusUnavailableError"
              "（不是静默返回空 —— 静默会让音频悄悄消失）",
              "OpusUnavailableError" not in raised
              and "libopus 不可用" in raised, raised.splitlines()[0] if raised else "")
        check("第 11 条 · 异常文案给出 apt 安装命令（可执行，不是'请检查配置'）",
              "apt-get install" in raised and "libopus0" in raised, "")
        check("第 11 条 · 异常文案给出 ELDER_LIBOPUS_PATH 逃生舱",
              "ELDER_LIBOPUS_PATH" in raised, "")

        # decode 在缺库时**同样抛**（而不是静默返回静音）—— 这是刻意的：
        # 静默会让服务端"永远听不见"却看起来一切正常，没有任何断言会红。
        # 断言名写死这一点，防止将来有人"顺手"把它改成静默。
        decode_raised = ""
        try:
            opus_codec.decode_opus(b"\x01\x02", 16000)
            decode_raised = ""
        except opus_codec.OpusUnavailableError as exc:
            decode_raised = str(exc)
        except Exception as exc:  # noqa: BLE001
            decode_raised = f"{type(exc).__name__}"
        check("第 11 条 ★ 无 libopus 时 decode 也抛 OpusUnavailableError"
              "（静默返回静音 = 服务端永远听不见却看起来正常）",
              "libopus 不可用" in decode_raised, decode_raised[:60])
        return

    # 有 libopus 的机器（CI）：反向确认真编解码可用
    check("第 11 条 · 本机有 libopus，encode 能出包",
          bool(opus_codec.encode_opus(_pcm_frame(0.06), 16000)), "")


async def t_malformed_frame(ids: dict) -> None:
    print("── 10c. 畸形帧不能打挂会话（payload_size 超出实际长度）──")
    import websockets

    headers = {"Authorization": f"Bearer {ids['token']}",
               "Device-Id": MAC_BOUND, "Protocol-Version": "2"}
    async with ws_connect(WS_URL, open_timeout=10,
                                  headers=headers) as ws:
        hello = await _handshake(ws, ids, 2)
        await _drain(ws)
        sid = hello.get("session_id", "")
        await ws.send(json.dumps({"session_id": sid, "type": "listen",
                                  "state": "start", "mode": "auto"}))
        await asyncio.sleep(0.2)
        # payload_size 谎报成 9999，实际只有 3 字节负载
        await ws.send(struct.pack(">HHIII", 2, 0, 0, 0, 9999) + b"\x01\x02\x03")
        # 再发一个正常帧，确认会话还活着
        await ws.send(_frame_v2(FAKE_OPUS_PACKET))
        await ws.send(json.dumps({"session_id": sid, "type": "listen",
                                  "state": "stop"}))
        got = []
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                break
            if isinstance(msg, bytes):
                continue
            obj = json.loads(msg)
            got.append(obj.get("type"))
            if obj.get("type") == "tts" and obj.get("state") == "stop":
                break
        check("第 10c 条 · 畸形帧被丢弃且会话仍能跑完整轮",
              "stt" in got and "tts" in got, str(got))


async def _amain(ids: dict, libopus: bool) -> None:
    await t_ws_auth(ids)
    await t_handshake_and_turn(ids, libopus)
    await t_binary_framing(ids)
    await t_malformed_frame(ids)
    await t_interrupt(ids)
    await t_degraded_tts(ids)
    await t_degraded_llm(ids)
    await t_empty_asr(ids)
    await t_quota(ids)
    # ---- 2026-10-17 新增三组 ----
    await t_tools_reminder(ids)
    await t_tools_clarify(ids)
    await t_tools_delete_toggle(ids)
    await t_tools_loop_cap(ids)
    await t_tools_protocols(ids)
    await t_stream_latency(ids, libopus)
    await t_elder_pause(ids)
    await t_elder_soft_voice(ids)
    await t_elder_hysteresis(ids)
    await t_elder_noise(ids)
    # ---- 2026-10-22 新增：本地自建 ASR 协议（组末自会停用该配置行）----
    await t_local_asr(ids)


def main() -> int:
    print(f"[boot] 语音链路冒烟，端口 {PORT}")
    # 端口三段式守卫（与全仓冒烟一致）：先 guard_or_die 挡住"别人在用"，
    # 再 guard_port 拿一个占位 socket，起服务前 release_port 释放 ——
    # 少最后一步就是自己挡住自己（uvicorn bind 失败，报错还很难懂）。
    guard_or_die(PORT, "smoke_voice.py")
    hold, hold_hint = guard_port(PORT)
    if hold is None:            # 理论上 guard_or_die 已挡住，第二道保险
        print(hold_hint)
        return 1

    upstream, up_port = start_upstream()
    print(f"[boot] 假上游 :{up_port}")

    release_port(hold, PORT)    # 必须释放，否则自己挡住自己

    server = start_app_server()
    if not wait_ready():
        print("[fatal] 服务未就绪。日志尾部：")
        print(_read_log_tail())
        return 1
    print("[boot] ready\n")

    try:
        ids = seed(up_port)
        print(f"[boot] 播种完成：device_pk={ids['device_pk']}，"
              f"llm 配置 {len(ids['llm_pks'])} 条\n")

        from app.voice import opus_codec
        libopus = opus_codec.libopus_available()
        if libopus:
            print("[boot] libopus 可用：音频 round-trip 断言将真跑\n")
        else:
            print("[boot] ⚠️ libopus 不可用：音频 round-trip 断言将 SKIP"
                  "（CI 的 ubuntu runner 已装 libopus0，那里必须真跑）\n")

        import asyncio

        t_ota(ids)
        t_ota_firmware(ids)
        t_activate_v2()
        asyncio.run(_amain(ids, libopus))
        t_usage_rows(ids)
        t_activation_table()
        t_codec_contract()
        t_parse_when_units()
    finally:
        try:
            server.should_exit = True
        except Exception:  # noqa: BLE001
            pass
        upstream.shutdown()
        release_port(hold, PORT)

    passed = sum(1 for _n, ok, _d in results if ok)
    failed = [n for n, ok, _d in results if not ok]
    print("\n" + "=" * 64)
    print(f"通过 {passed} 项，失败 {len(failed)} 项，跳过 {len(skipped)} 项")
    if skipped:
        print("\n跳过的断言（原因，供人工核对 / CI 对照）：")
        for name, why in skipped:
            print(f"  - {name}\n      {why}")
    if failed:
        print("\n失败的断言：")
        for name in failed:
            print(f"  - {name}")
        print("\n结论：**红**")
        return 1
    print("结论：**绿**（判定门限是 failed==0，不是 passed==total）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
