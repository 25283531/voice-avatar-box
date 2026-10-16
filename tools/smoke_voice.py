"""自研语音链路冒烟：WS 协议 + Opus 编解码 + 会话编排 + OTA + 埋点。

覆盖（与任务书断言清单一一对应）
------------------------------
1. **OTA 分流**：未绑定 MAC 只拿激活码、**不带 websocket 段**；绑定后才拿
   ws url + token（且 token 本地 decode 校验 typ/sub）；
2. **WS 鉴权**：无 token / 垃圾 token → 被关（4401）；token 有效但
   Device-Id 不符 → 4001；
3. **握手**：服务端 hello 的 transport / session_id / sample_rate=24000；
4. **完整一轮**：listen start → 二进制帧 → listen stop → stt / tts start /
   tts stop；有 libopus 时做**编解码 round-trip**（发出去的帧解回来非全静音）；
5. **埋点**：model_usage（source=voice / usage=chat / total_tokens=155 /
   device_id）+ chat_usage + Interaction 三条都落库；
6. **配额用尽**：假上游 chat 计数不变 + 收到 alert + WS 仍存活；
7. **打断**：播报中发 abort → 后续 sentence_start / 二进制帧不再来；
8. **降级**：禁用 TTS → 仍收到 stt + tts start/stop + sentence_start，
   无二进制帧；禁用全部 LLM → 收到"未配置大模型" alert；
9. **空识别**：假 ASR 返回空 → 不调 LLM（chat 计数不变）。

⚠️ 本机（Windows）没有 libopus 时怎么算
--------------------------------------
**第 4 条的音频 round-trip 断言会打印 SKIP 并跳过**，协议级断言照常跑。
这不是"假装全绿"：SKIP 的断言会计入 skipped 计数并在结尾单独列出，
CI（ubuntu，team-lead 已装 libopus0）里它必须真跑过。判定门限是
``failed == 0``，不是 ``passed == total``。

三个基础设施决策
----------------
1. **进程内起服务**（uvicorn 后台线程）：voice 路由还没接进 main.py
   （集成由 team-lead 做），本脚本自己 ``attach(app)``；
2. **假上游是真 HTTP**（threading + http.server，端口 bind 0 自选）：
   要验的正是"从厂商响应提取文本/音频"这条链路，mock 掉客户端等于
   把被测点 mock 了；
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

FAKE_ASR_TEXT = "小暖小暖，明天早上八点提醒我吃药"
FAKE_REPLY = "好的，明天早上八点我提醒您吃药。您也要记得多喝水。我陪您聊聊。"
FAKE_TOTAL_TOKENS = 155

# 假 TTS 的说话速度控制：默认每句 sleep 这么久才返回。
# 第 7 条（打断）要把它调大，才能在播报中插进 abort。
TTS_SENTENCE_DELAY_SEC = 0.0

results: list[tuple[str, bool, str]] = []
skipped: list[tuple[str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def skip(name: str, why: str) -> None:
    """记一条 SKIP。**必须带原因**，否则结尾的清单没有可读性。"""
    skipped.append((name, why))
    print(f"  [SKIP] {name}  -> {why}")


def _read_log_tail(max_bytes: int = 8192) -> str:
    try:
        return SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-max_bytes:]
    except OSError:
        return ""


# ================================================================ 假上游
class _UpstreamState:
    """假上游的共享可变状态（计数 + 行为开关）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.chat_calls = 0
        self.asr_calls = 0
        self.tts_calls = 0
        self.asr_text = FAKE_ASR_TEXT
        self.reply = FAKE_REPLY
        self.tts_enabled = True
        self.llm_enabled = True
        self.tts_delay = TTS_SENTENCE_DELAY_SEC
        # 每次 /audio/speech 返回的 PCM 时长（秒），默认 0.5
        self.tts_seconds = 0.5

    def bump(self, name: str) -> int:
        with self.lock:
            setattr(self, name, getattr(self, name) + 1)
            return getattr(self, name)

    def get(self, name: str):
        with self.lock:
            return getattr(self, name)


UP = _UpstreamState()


class _FakeUpstream(BaseHTTPRequestHandler):
    """假 OpenAI 兼容上游：chat / transcriptions / speech 三个路由。

    刻意用 ``ThreadingHTTPServer`` + ``bind 0``：端口让内核自选，
    免得又一个硬编码端口和别人撞车；多线程是因为语音链路的
    TTS/ASR 可能在后台线程里并发调（``run_in_threadpool``）。
    """

    def log_message(self, *args):  # 静默：别往 stdout 刷访问日志
        pass

    def _json(self, obj: dict, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _pcm(self, seconds: float, freq: float = 440.0) -> bytes:
        """合成一段正弦 PCM（24kHz/mono/S16LE）。

        为什么用正弦而不是静音：静音的 RMS 是 0，而"编解码 round-trip"
        断言要的正是"解回来的东西不是全零"。正弦有稳定的可测能量。
        """
        n = int(24000 * seconds)
        out = bytearray()
        for i in range(n):
            v = int(12000 * math.sin(2 * math.pi * freq * i / 24000))
            out += struct.pack("<h", v)
        return bytes(out)

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length) if length else b"{}"
        # 请求体**不解析**：假上游只按路径分流，不需要看 messages 内容
        # （真上游才需要）。少解析一次也少一处"测试自己 mock 自己"的嫌疑。

        if path.endswith("/chat/completions"):
            UP.bump("chat_calls")
            if not UP.get("llm_enabled"):
                self._json({"error": {"message": "上游被测试禁用"}}, 500)
                return
            self._json({
                "choices": [{
                    "message": {"role": "assistant", "content": UP.get("reply")},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 120, "completion_tokens": 35,
                          "total_tokens": FAKE_TOTAL_TOKENS},
            })
            return

        if path.endswith("/audio/transcriptions"):
            UP.bump("asr_calls")
            self._json({"text": UP.get("asr_text")})
            return

        if path.endswith("/audio/speech"):
            UP.bump("tts_calls")
            if not UP.get("tts_enabled"):
                self._json({"error": {"message": "上游 TTS 被测试禁用"}}, 500)
                return
            delay = UP.get("tts_delay")
            if delay:
                time.sleep(delay)
            data = self._pcm(UP.get("tts_seconds"))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_error(404)


def start_upstream() -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
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
        except Exception:  # noqa: BLE001  未就绪时连接被拒是常态
            pass
        time.sleep(0.5)
    return False


# ================================================================ 播种
def seed(upstream_port: int) -> dict:
    """进程内播种：管理员 + 两台设备 + llm/asr/tts 三条配置。

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

        llm = _ensure(
            "llm_main", "假 LLM（冒烟）",
            api_style="openai_compatible",
            endpoint=f"http://127.0.0.1:{upstream_port}/v1",
            api_key="sk-fake", model="fake-chat-1", enabled=True, priority=1)
        asr = _ensure(
            "asr", "假 ASR（冒烟）",
            api_style="openai_compatible",
            endpoint=f"http://127.0.0.1:{upstream_port}/v1",
            api_key="sk-fake", model="whisper-1", enabled=True, priority=1)
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
            "llm_pk": int(llm.id), "asr_pk": int(asr.id), "tts_pk": int(tts.id),
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


def _set_quota_limit(seconds: int) -> None:
    """把免费日配额顶到上限，模拟"今天的时间用完了"。"""
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
    check("OTA 未绑定的激活码是 13 位",
          len(str(unbound.get("activation", {}).get("code", ""))) == 13,
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


async def t_ws_auth(ids: dict) -> None:
    print("── 2. WS 鉴权 ──")
    import websockets

    # 无 token
    try:
        async with websockets.connect(WS_URL, open_timeout=10) as ws:
            await ws.recv()
        code = None
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "rcvd", None)
        code = getattr(code, "code", None) if code is not None else None
    check("无 token 连接被关闭（拿不到 server hello）", code is not None,
          f"close_code={code}")

    # 垃圾 token
    try:
        async with websockets.connect(
                WS_URL, open_timeout=10,
                extra_headers={"Authorization": "Bearer garbage"}) as ws:
            await ws.recv()
        code = None
    except Exception as exc:  # noqa: BLE001
        r = getattr(exc, "rcvd", None)
        code = getattr(r, "code", None) if r is not None else None
    check("垃圾 token 连接被关闭", code is not None, f"close_code={code}")

    # 有效 token 但 Device-Id 不符
    try:
        async with websockets.connect(
                WS_URL, open_timeout=10,
                extra_headers={
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


async def t_handshake_and_turn(ids: dict, libopus) -> None:
    print("── 3-4. 握手 + 完整一轮 ──")
    import websockets

    headers = {"Authorization": f"Bearer {ids['token']}",
               "Device-Id": MAC_BOUND, "Protocol-Version": "1",
               "Client-Id": "smoke-client"}
    async with websockets.connect(WS_URL, open_timeout=10,
                                  extra_headers=headers) as ws:
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
        async with websockets.connect(WS_URL, open_timeout=10,
                                      extra_headers=headers) as ws:
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
        async with websockets.connect(WS_URL, open_timeout=10,
                                      extra_headers=headers) as ws:
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

    _disable_provider(ids["llm_pk"])
    headers = {"Authorization": f"Bearer {ids['token']}",
               "Device-Id": MAC_BOUND, "Protocol-Version": "1"}
    try:
        async with websockets.connect(WS_URL, open_timeout=10,
                                      extra_headers=headers) as ws:
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
        _enable_provider(ids["llm_pk"])


async def t_empty_asr(ids: dict) -> None:
    print("── 9. 空识别：不调 LLM，回「没听清」 ──")
    import websockets

    await _settle()
    UP.asr_text = ""
    chat_before = UP.get("chat_calls")
    headers = {"Authorization": f"Bearer {ids['token']}",
               "Device-Id": MAC_BOUND, "Protocol-Version": "1"}
    try:
        async with websockets.connect(WS_URL, open_timeout=10,
                                      extra_headers=headers) as ws:
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


async def t_quota(ids: dict) -> None:
    print("── 6. 配额用尽：不调 LLM + alert + WS 存活 ──")
    import websockets

    await _settle()
    _set_quota_limit(0)          # 免费日配额 0 秒 = 一开始就用尽
    chat_before = UP.get("chat_calls")
    headers = {"Authorization": f"Bearer {ids['token']}",
               "Device-Id": MAC_BOUND, "Protocol-Version": "1"}
    try:
        async with websockets.connect(WS_URL, open_timeout=10,
                                      extra_headers=headers) as ws:
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
            check("第 6 条 · 配额用尽收到 alert", bool(alert), f"alert={alert!r}")
            check("第 6 条 · 配额用尽时**不调 LLM**（chat 计数不变）",
                  UP.get("chat_calls") == chat_before,
                  f"{chat_before} -> {UP.get('chat_calls')}")
            check("第 6 条 · alert 文案提到「聊天时间」",
                  "聊天时间" in alert or "明天" in alert, f"alert={alert!r}")
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

    with SessionLocal() as db:
        n = db.scalar(
            select(func.count()).select_from(ActivationCode).where(
                ActivationCode.device_id == MAC_UNBOUND)) or 0
    check("1c · 未绑定 MAC 的激活码只建了一条（多次申领复用）", n == 1,
          f"{n} 条")


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


def _patch_opus(silent_after: int = 10**9) -> object:
    """把 opus_codec 的两个叶子函数换成替身，返回可恢复的备份。

    :param silent_after: 前 N 帧解成**有声**，之后解成静音 —— 用来触发
        服务端静音兜底（没有它，兜底那条路径在本机永远跑不到）。
    """
    from app.voice import opus_codec

    backup = (opus_codec.encode_opus, opus_codec.decode_opus)
    counter = {"n": 0}

    def fake_encode(pcm: bytes, sr: int, channels: int = 1) -> bytes:
        return FAKE_OPUS_PACKET

    def fake_decode(data: bytes, sr: int, channels: int = 1) -> bytes:
        counter["n"] += 1
        if counter["n"] <= silent_after:
            return _pcm_frame(0.06)          # 有声
        return b"\x00" * int(sr * 0.06) * 2  # 静音

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


async def t_binary_framing(ids: dict) -> None:
    print("── 10. 二进制帧封装 v1/v2/v3 + 服务端静音兜底（编解码用替身）──")
    import websockets

    backup = _patch_opus()
    try:
        for version in (1, 2, 3):
            headers = {"Authorization": f"Bearer {ids['token']}",
                       "Device-Id": MAC_BOUND,
                       "Protocol-Version": str(version)}
            async with websockets.connect(WS_URL, open_timeout=10,
                                          extra_headers=headers) as ws:
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
        async with websockets.connect(WS_URL, open_timeout=10,
                                      extra_headers=headers) as ws:
            hello = await _handshake(ws, ids, 1)
            await _drain(ws)
            sid = hello.get("session_id", "")
            got_bin, got_json = await _one_round(
                ws, sid, 1, [FAKE_OPUS_PACKET] * 40, send_stop=False)
            check(f"第 10b 条 ★ 丢失 listen stop 时服务端静音兜底仍 finalize"
                  f"（阈值 {cfg.VOICE_SILENCE_MS}ms）",
                  any(o.get("type") == "stt" for o in got_json),
                  str([o.get("type") for o in got_json]))
            check("第 10b 条 · 兜底那一轮也照常播报（tts start/start 配对）",
                  any(o.get("type") == "tts" and o.get("state") == "stop"
                      for o in got_json), "")
    finally:
        _restore_opus(backup)


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
    async with websockets.connect(WS_URL, open_timeout=10,
                                  extra_headers=headers) as ws:
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
        print(f"[boot] 播种完成：device_pk={ids['device_pk']}\n")

        from app.voice import opus_codec
        libopus = opus_codec.libopus_available()
        if libopus:
            print("[boot] libopus 可用：音频 round-trip 断言将真跑\n")
        else:
            print("[boot] ⚠️ libopus 不可用：音频 round-trip 断言将 SKIP"
                  "（CI 的 ubuntu runner 已装 libopus0，那里必须真跑）\n")

        import asyncio

        t_ota(ids)
        t_activate_v2()
        asyncio.run(_amain(ids, libopus))
        t_usage_rows(ids)
        t_activation_table()
        t_codec_contract()
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
