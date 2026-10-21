"""声纹认人冒烟（F3 · 3D Speaker）：提供者契约 + 登记 CRUD + 端到端个性化。

测什么
------
1. **提供者单元**（不起服务）：URL/key 解析、multipart 报文字节（含 WAV 头）、
   Bearer 头、阈值判定、未登记 ID 拒认、健康检查与缓存、超时/500/坏 JSON
   一律按"没认出"返回且**绝不外抛**。
2. **登记 CRUD**（真实 HTTP + 库）：建/查/改/软删、同 key 409、删后可重建、
   他人行 404（读与写同码）、admin 看全部、health 端点不吐 URL/key。
3. **端到端**（真实服务 + 假声纹服务 + WS 假设备）：
   * 认出来 ⇒ 首轮 user 消息是 ``{"speaker","content"}`` JSON、system 含
     「当前说话人：名字 + 描述」；第二轮降为纯文本、system 不再注入；
   * 分数低于阈值 ⇒ 不包 JSON、不注入，对话照常；
   * 一个说话人都没登记 ⇒ 声纹服务**零请求**（不白跑一趟）；
   * 服务不可达 / 不健康 ⇒ 对话照常完成（认不出人不是降级事故）。

反向变异（防假绿，本脚本自己跑）
--------------------------------
正常跑完后，父进程对每个变异各起一个子进程（import hook 在内存里改写源文件，
不落盘），要求指定断言**变红**：

=====  =====================================  ==================================
M1     阈值判定反过来（``>=`` 当通过）        低于阈值不认人
M2     拆掉"没登记说话人就不识别"的闸门      未登记时声纹服务零请求
M3     user 消息每轮都包 JSON（不止首轮）     第二轮 user 内容是纯文本
M4     拆掉 system 的说话人注入              system 含「当前说话人」行
=====  =====================================  ==================================

变异跑**不红** ⇒ 对应断言没有鉴别力 ⇒ 本脚本 exit 1。跳过变异：``--no-mutation``。
每个变异子进程用自己的 HTTP 口与假声纹服务口（父口 + 序号），不抢父进程的。

用法::

    python tools/smoke_voiceprint.py [8133] [--no-mutation]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_ports import (  # 见 smoke_ports.py 模块 docstring
    guard_or_die,
    guard_port,
    release_port,
    resolve_port,
)

# 复用 smoke_voice 的假上游 / 播种 / 编解码替身 / 断言账本。
# 只 import 不跑它的 main（模块级无副作用，除了删自己那份 smoke DB）。
sys.path.insert(0, str(ROOT / "tools"))
import smoke_voice as SV

# 假声纹服务口。刻意选高位冷门段：它不是上游默认值，也不会撞常见服务。
# 可用环境变量覆盖 —— 反向变异子进程靠它拿自己的口。
VP_PORT = int(os.environ.get("SMOKE_VOICEPRINT_PORT", "18099"))

MAC_BOUND = SV.MAC_BOUND          # aabbccddee01 归一化后（seed 时置 owner）

#: 本进程起的 HTTP 服务端口（main() 里赋值）。**不能图省事用 SV.PORT** ——
#: 那是 smoke_voice 的端口，本脚本起的是自己的服务（同 smoke_mqtt_udp 的教训）。
HTTP_PORT: int = 0

#: 假声纹服务返回的"命中"配置
VP_SPEAKER_KEY = "son"
VP_SPEAKER_NAME = "儿子"
VP_SPEAKER_DESC = "您的儿子，在北京工作，周末常打电话"
VP_GOOD_SCORE = 0.91
VP_LOW_SCORE = 0.12


# ================================================================ 变异定义
#: 每条变异：(锚点原文, 替换成, 要求变红的断言名列表)。
#: 锚点必须**语义不同**（不是"关掉功能"）：M1 把阈值判据反过来（永真），
#: M2 拆掉闸门而不是改闸门条件，M3 改成"每轮都包"（上游专门修掉的那个 bug），
#: M4 整块删注入。
MUTATIONS: dict[str, tuple[str, str, list[str]]] = {
    "M1": (
        "        if score < self.threshold:\n",
        "        if score >= self.threshold:\n",
        ["提供者 ★ 低于阈值的分数按未认出处理（score < threshold）"],
    ),
    "M2": (
        "    if p is None or not speakers:\n",
        "    if p is None:\n",
        ["提供者 ★ 一个说话人都没登记时不发起识别（零请求）"],
    ),
    "M3": (
        "        if speaker_name and speaker_name not in self._introduced_speakers:\n",
        "        if speaker_name:\n",
        ["端到端 ★ 第二轮 user 消息降为纯文本（不再带 speaker JSON）"],
    ),
    "M4": (
        "        if speaker_name and speaker_name not in self._system_speakers:\n",
        "        if False and speaker_name:\n",
        ["端到端 ★ system 首轮注入「当前说话人」行"],
    ),
}

#: 每条变异改的是哪个文件（M1/M2 → 提供者，M3/M4 → 会话）
_MUTATION_TARGET = {
    "M1": "app/voice/voiceprint.py",
    "M2": "app/voice/voiceprint.py",
    "M3": "app/voice/session.py",
    "M4": "app/voice/session.py",
}


def _install_mutation_hook(mid: str) -> None:
    """在内存里改写目标模块（**不落盘**）。机制同 smoke_mqtt_udp：
    meta path finder 拦截 import，子进程在 import 任何 app.* 之前装好钩子。
    """
    import importlib.abc
    import importlib.util

    rel = _MUTATION_TARGET[mid]
    src = SERVER / rel
    module_name = "app." + rel[len("app/"):-len(".py")].replace("/", ".")
    original = src.read_text(encoding="utf-8")
    old, new, _required = MUTATIONS[mid]
    if original.count(old) != 1:
        raise SystemExit(
            f"变异 {mid} 锚点没找到或不唯一（{original.count(old)} 处）：{old!r}\n"
            f"护栏可能已被改名/重构，变异自检无从谈起 —— 请更新 MUTATIONS。")
    mutated = original.replace(old, new, 1)

    class _Loader(importlib.abc.Loader):
        def create_module(self, spec):  # noqa: D102
            return None

        def exec_module(self, module) -> None:
            exec(compile(mutated, str(src), "exec"), module.__dict__)

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):  # noqa: D102
            if fullname != module_name:
                return None
            return importlib.util.spec_from_loader(fullname, _Loader())

    sys.meta_path.insert(0, _Finder())
    print(f"[mutate] {mid} 已注入（仅本进程内存）：{old.strip()!r} -> {new.strip()!r}")


# ================================================================ 假声纹服务
class _VpState:
    """假声纹服务的共享可变状态（计数 + 行为脚本 + 观测记录）。

    与 smoke_voice 的 ``UP`` 同一定位：断言直接读它，同一进程无需加锁
    （ThreadingHTTPServer 的每个请求一个线程，但测试是单线程驱动，
    且 GIL 下简单属性赋值本来就是原子的）。
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        # 行为脚本：match / low_score / unknown_id / http500 / badjson /
        # hang / unhealthy / down
        self.mode = "match"
        self.healthy = True
        # 观测
        self.health_requests = 0
        self.identify_requests = 0
        self.last_auth = ""
        self.last_speaker_ids = ""
        self.last_content_type = ""
        self.last_wav_head = b""
        self.last_wav_len = 0
        self.last_wav_rate = 0
        self.last_file_field_present = False

    def bump(self, name: str) -> None:
        with self.lock:
            setattr(self, name, getattr(self, name) + 1)


VP = _VpState()


def _parse_multipart(body: bytes, content_type: str) -> dict[str, bytes]:
    """解析 multipart/form-data。**只支持本假服务用到的扁平字段形态**。

    不做通用 MIME 解析：那是把测试代码写成第二个被测系统的老路。
    这里要验的只有"字段在不在、内容对不对"。
    """
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if not m:
        return {}
    delim = ("--" + m.group(1)).encode()
    out: dict[str, bytes] = {}
    for chunk in body.split(delim):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        name = re.search(rb'name="([^"]+)"', head)
        if not name:
            continue
        # 每个 part 的数据末尾是边界前的那个 \r\n，只剥这一个
        if data.endswith(b"\r\n"):
            data = data[:-2]
        out[name.group(1).decode()] = data
    return out


class _FakeVoiceprint(BaseHTTPRequestHandler):
    """兼容上游契约的假声纹服务。

    GET  /voiceprint/health?key=  -> {"status":"healthy"}（unhealthy 模式除外）
    POST /voiceprint/identify      -> {"speaker_id","score"}（按 mode 脚本）
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

    def do_GET(self) -> None:
        if not self.path.startswith("/voiceprint/health"):
            self.send_error(404)
            return
        VP.bump("health_requests")
        VP.last_auth = ""      # health 走 query key，不带头
        if not VP.healthy:
            self._json({"status": "degraded"})
            return
        self._json({"status": "healthy"})

    def do_POST(self) -> None:
        if not self.path.startswith("/voiceprint/identify"):
            self.send_error(404)
            return
        VP.bump("identify_requests")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        VP.last_auth = str(self.headers.get("Authorization") or "")
        VP.last_content_type = str(self.headers.get("Content-Type") or "")
        fields = _parse_multipart(body, VP.last_content_type)
        VP.last_speaker_ids = fields.get("speaker_ids", b"").decode("utf-8", "replace")
        wav = fields.get("file", b"")
        VP.last_file_field_present = "file" in fields
        VP.last_wav_head = wav[:4]
        VP.last_wav_len = len(wav)
        # WAV 头里第 23-24 字节是采样率（小端），第 23 字节起 4 字节
        VP.last_wav_rate = int.from_bytes(wav[24:28], "little") if len(wav) >= 28 else 0

        if VP.mode == "hang":
            time.sleep(30)      # 远超提供者的超时，用来验"不卡轮"
            return
        if VP.mode == "http500":
            self._json({"error": "boom"}, 500)
            return
        if VP.mode == "badjson":
            data = b"not-json-at-all"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if VP.mode == "low_score":
            self._json({"speaker_id": VP_SPEAKER_KEY, "score": VP_LOW_SCORE})
            return
        if VP.mode == "unknown_id":
            self._json({"speaker_id": "someone-else", "score": VP_GOOD_SCORE})
            return
        self._json({"speaker_id": VP_SPEAKER_KEY, "score": VP_GOOD_SCORE})


def start_voiceprint(port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), _FakeVoiceprint)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ================================================================ 起服务
def start_server(port: int, vp_port: int) -> object:
    """后台线程里跑 app。环境变量必须在 import app.* **之前**设好。"""
    db = SV.DB_DIR / f"elder_smoke_vp_{os.getpid()}.db"
    os.environ["ELDER_DB"] = f"sqlite:///{db.as_posix()}"
    os.environ["ELDER_JWT_SECRET"] = "smoke-voiceprint-secret-0123456789"
    os.environ["ELDER_SCHEDULER_INTERVAL"] = "3600"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    # 假上游与假声纹都在本机，确保绕过代理
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        os.environ.pop(k, None)
    # ---- F3 开关：本脚本的全部意义就在于**开着**它 ----
    os.environ["ELDER_VOICEPRINT_URL"] = (
        f"http://127.0.0.1:{vp_port}/voiceprint/health?key=smoke-vp-key")
    os.environ["ELDER_VOICEPRINT_TIMEOUT_SEC"] = "2"
    os.environ.pop("ELDER_FIRMWARE_VERSION", None)
    os.environ.pop("ELDER_FIRMWARE_URL", None)

    SV.SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[logging.FileHandler(SV.SERVER_LOG, encoding="utf-8")],
    )

    sys.path.insert(0, str(SERVER))
    import threading

    import uvicorn
    from app.main import app
    from app.voice import attach

    attach(app)   # 幂等；main 的 import 期已经 attach 过

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    return server


def wait_ready(port: int, timeout: float = 120.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with SV._LOCAL_OPENER.open(urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/health"), timeout=2):
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


# ================================================================ HTTP 小助手
class _Req:
    """带 Bearer 头的本地 HTTP 客户端（不走代理，同 smoke 纪律）。"""

    def __init__(self, token: str) -> None:
        self.token = token

    def _open(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{HTTP_PORT}{path}", data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        return SV._LOCAL_OPENER.open(req, timeout=15)

    def call(self, method: str, path: str,
             body: dict | None = None) -> tuple[int, object]:
        try:
            with self._open(method, path, body) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw.decode("utf-8"))
                                     if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001
                return exc.code, raw.decode("utf-8", "replace")


# ================================================================ 1 提供者
def t_provider() -> None:
    print("── 1. 提供者：契约 / 阈值 / 失败语义（不起服务）──")
    import app.voice.voiceprint as VPMod
    from app.voice.asr_tts import wav_header_bytes

    # ---- 配置解析 ----
    p = VPMod.VoiceprintProvider(
        f"http://127.0.0.1:{VP_PORT}/voiceprint/health?key=abc123",
        0.4, 2.0, 30.0, 3.0)
    SV.check("提供者 ★ 配好 URL+key 即 configured", p.configured, "")
    SV.check("提供者 ★ identify 地址 = base + /voiceprint/identify",
             p.identify_url == f"http://127.0.0.1:{VP_PORT}/voiceprint/identify",
             p.identify_url)
    SV.check("提供者 ★ key 从 query 解析", p.api_key == "abc123", p.api_key)

    no_key = VPMod.VoiceprintProvider(
        f"http://127.0.0.1:{VP_PORT}/voiceprint/health", 0.4, 2.0, 30.0, 3.0)
    SV.check("提供者 ★ URL 里没有 key ⇒ 不 configured（功能关闭）",
             not no_key.configured, "")

    bad_th = VPMod.VoiceprintProvider(
        f"http://127.0.0.1:{VP_PORT}/voiceprint/health?key=k", 1.5, 2.0, 30.0, 3.0)
    SV.check("提供者 ★ 阈值越界（>1）⇒ 不 configured", not bad_th.configured, "")
    zero_th = VPMod.VoiceprintProvider(
        f"http://127.0.0.1:{VP_PORT}/voiceprint/health?key=k", 0.0, 2.0, 30.0, 3.0)
    SV.check("提供者 ★ 阈值为 0 ⇒ 不 configured（永真阈值比没有更坏）",
             not zero_th.configured, "")

    empty = VPMod.VoiceprintProvider("", 0.4, 2.0, 30.0, 3.0)
    SV.check("提供者 ★ URL 为空 ⇒ 不 configured", not empty.configured, "")

    # ---- multipart 报文字节 ----
    pcm = b"\x11\x22" * 800        # 1600 字节 = 50ms @16k
    body, ctype = VPMod._multipart_body(["a", "b"], wav_header_bytes(len(pcm), 16000) + pcm)
    SV.check("提供者 ★ Content-Type 带 boundary",
             ctype.startswith("multipart/form-data; boundary="), ctype)
    SV.check("提供者 ★ speaker_ids 字段在报文里", b'name="speaker_ids"' in body, "")
    SV.check("提供者 ★ speaker_ids 值是逗号分隔", b"\r\n\r\na,b\r\n" in body, "")
    SV.check("提供者 ★ file 字段在报文里且文件名 audio.wav",
             b'name="file"; filename="audio.wav"' in body, "")
    SV.check("提供者 ★ file 段声明 audio/wav", b"Content-Type: audio/wav" in body, "")
    SV.check("提供者 ★ 报文里的音频以 RIFF 开头（真 WAV，不是裸 PCM）",
             b"RIFF" in body, "")

    # ---- 健康检查与缓存 ----
    VP.mode, VP.healthy = "match", True
    VP.health_requests = 0
    SV.check("提供者 ★ 健康检查通过（healthy）", p.health() is True, "")
    n_after_first = VP.health_requests
    p.health()
    SV.check("提供者 ★ 健康检查结果被缓存（第二次不再发请求）",
             VP.health_requests == n_after_first,
             f"{n_after_first} -> {VP.health_requests}")

    # ---- 识别：命中 ----
    VP.identify_requests = 0
    sid, score = p.identify(pcm, [VP_SPEAKER_KEY])
    SV.check("提供者 ★ 命中返回 speaker_id 与分数",
             sid == VP_SPEAKER_KEY and score == VP_GOOD_SCORE, f"{sid} {score}")
    SV.check("提供者 ★ 请求带 Authorization: Bearer <key>",
             VP.last_auth == "Bearer abc123", VP.last_auth)
    SV.check("提供者 ★ 请求带上了全部 speaker_ids",
             VP.last_speaker_ids == VP_SPEAKER_KEY, VP.last_speaker_ids)
    SV.check("提供者 ★ 收到的音频是 WAV（RIFF 头）",
             VP.last_wav_head == b"RIFF" and VP.last_file_field_present, "")
    SV.check("提供者 ★ 音频采样率 16000（与 ASR 同口径）",
             VP.last_wav_rate == 16000, str(VP.last_wav_rate))

    # ---- 识别：低于阈值 ----
    VP.mode = "low_score"
    sid, score = p.identify(pcm, [VP_SPEAKER_KEY])
    SV.check("提供者 ★ 低于阈值的分数按未认出处理（score < threshold）",
             sid is None and score == VP_LOW_SCORE, f"{sid} {score}")
    VP.mode = "match"

    # ---- 识别：服务端报了没问过的 ID ----
    VP.mode = "unknown_id"
    sid, score = p.identify(pcm, [VP_SPEAKER_KEY])
    SV.check("提供者 ★ 服务端报未登记的 ID ⇒ 未认出（不替它认人）",
             sid is None, f"{sid}")
    VP.mode = "match"

    # ---- 识别：各种失败都按未认出，且不外抛 ----
    VP.mode = "http500"
    sid, score = p.identify(pcm, [VP_SPEAKER_KEY])
    SV.check("提供者 ★ HTTP 500 ⇒ 未认出（不外抛）",
             sid is None and score == 0.0, f"{sid} {score}")
    VP.mode = "badjson"
    sid, score = p.identify(pcm, [VP_SPEAKER_KEY])
    SV.check("提供者 ★ 畸形 JSON ⇒ 未认出（不外抛）",
             sid is None and score == 0.0, f"{sid} {score}")
    VP.mode = "match"

    # ---- 识别：超时（服务睡 30s，提供者 2s 超时）----
    VP.mode = "hang"
    t0 = time.monotonic()
    sid, score = p.identify(pcm, [VP_SPEAKER_KEY])
    elapsed = time.monotonic() - t0
    SV.check("提供者 ★ 超时按未认出处理且不挂死（<5s）",
             sid is None and elapsed < 5.0, f"{sid} {elapsed:.1f}s")
    VP.mode = "match"

    # ---- identify_speaker 的三道闸门 ----
    VPMod.reset_provider()
    old_url = _app_config().VOICEPRINT_URL
    try:
        # 闸门 1：没配 URL ⇒ 不识别、零请求
        _app_config().VOICEPRINT_URL = ""
        VPMod.reset_provider()
        VP.identify_requests = 0
        VP.health_requests = 0
        hit, sc = VPMod.identify_speaker(pcm, [(VP_SPEAKER_KEY, "x", "")])
        SV.check("提供者 ★ 未配置 URL ⇒ 不识别且零请求",
                 hit is None and VP.identify_requests == 0
                 and VP.health_requests == 0, f"{hit} {VP.identify_requests}")

        # 闸门 2：一个说话人都没登记 ⇒ 不识别、零请求
        _app_config().VOICEPRINT_URL = old_url
        VPMod.reset_provider()
        VP.identify_requests = 0
        VP.health_requests = 0
        hit, sc = VPMod.identify_speaker(pcm, [])
        SV.check("提供者 ★ 一个说话人都没登记时不发起识别（零请求）",
                 hit is None and VP.identify_requests == 0
                 and VP.health_requests == 0, f"{hit} {VP.identify_requests}")

        # 闸门 3：服务不健康 ⇒ 这一轮不识别
        VP.healthy = False
        VPMod.reset_provider()
        VP.health_requests = 0
        hit, sc = VPMod.identify_speaker(pcm, [(VP_SPEAKER_KEY, "x", "")])
        SV.check("提供者 ★ 服务不健康 ⇒ 本轮不识别（不发 identify）",
                 hit is None and VP.identify_requests == 0, f"{hit}")
    finally:
        VP.healthy = True
        VP.mode = "match"
        _app_config().VOICEPRINT_URL = old_url
        VPMod.reset_provider()


def _app_config():
    """拿到**服务进程内**的 app.config（服务就在本进程的线程里）。"""
    import app.config as appconfig
    return appconfig


# ================================================================ 2 登记 CRUD
def t_router(ids: dict) -> None:
    print("── 2. 登记 CRUD + 归属隔离 + health 端点 ──")
    from app.db import SessionLocal
    from app import models
    from app.security import issue_user_token
    from sqlalchemy import select

    admin = _Req(issue_user_token(int(ids["user_id"]), "admin", True))

    # 第二个用户（非管理员）：归属隔离的被测对象
    with SessionLocal() as db:
        other = db.scalar(select(models.User).where(
            models.User.username == "vp_other"))
        if other is None:
            other = models.User(username="vp_other", nickname="另一位家属",
                                is_admin=False, password_hash="x")
            db.add(other)
            db.commit()
            db.refresh(other)
        other_id = int(other.id)
    other = _Req(issue_user_token(other_id, "vp_other", False))

    # ---- 建 ----
    code, row = admin.call("POST", "/api/voice/speakers", {
        "speaker_key": VP_SPEAKER_KEY, "name": VP_SPEAKER_NAME,
        "description": VP_SPEAKER_DESC})
    SV.check("登记 ★ 管理员可建（201）", code == 201, f"{code} {row}")
    pk = int(row["id"]) if isinstance(row, dict) else 0
    SV.check("登记 ★ owner 是当前用户",
             isinstance(row, dict) and row.get("owner_id") == int(ids["user_id"]),
             str(row)[:120])

    # ---- 同 key 重复 ⇒ 409 ----
    code, row = admin.call("POST", "/api/voice/speakers", {
        "speaker_key": VP_SPEAKER_KEY, "name": "重名"})
    SV.check("登记 ★ 同 owner 同 key 未删行 ⇒ 409", code == 409, f"{code} {row}")

    # ---- 校验 ----
    code, _ = admin.call("POST", "/api/voice/speakers",
                         {"speaker_key": "a,b", "name": "逗号"})
    SV.check("登记 ★ speaker_key 含逗号 ⇒ 422（会把一个说话人裂成两个）",
             code == 422, str(code))
    code, _ = admin.call("POST", "/api/voice/speakers",
                         {"speaker_key": "blank", "name": "   "})
    SV.check("登记 ★ 空白 name ⇒ 422", code == 422, str(code))

    # ---- 他人不可见 / 不可写 ----
    code, _ = other.call("GET", f"/api/voice/speakers/{pk}")
    SV.check("登记 ★ 别人的行 GET ⇒ 404（与不存在同码）", code == 404, str(code))
    code, _ = other.call("PATCH", f"/api/voice/speakers/{pk}",
                         {"speaker_key": VP_SPEAKER_KEY, "name": "抢"})
    SV.check("登记 ★ 别人的行 PATCH ⇒ 404", code == 404, str(code))
    code, _ = other.call("DELETE", f"/api/voice/speakers/{pk}")
    SV.check("登记 ★ 别人的行 DELETE ⇒ 404", code == 404, str(code))
    code, rows = other.call("GET", "/api/voice/speakers")
    SV.check("登记 ★ 别人的列表里看不到它",
             code == 200 and all(int(r["id"]) != pk for r in rows), str(code))

    # ---- 本人可改 ----
    code, row = admin.call("PATCH", f"/api/voice/speakers/{pk}",
                           {"speaker_key": VP_SPEAKER_KEY, "name": "儿子",
                            "description": "改了介绍"})
    SV.check("登记 ★ 本人可改（200 + 新值生效）",
             code == 200 and row.get("description") == "改了介绍",
             f"{code} {str(row)[:100]}")
    code, row = admin.call("PATCH", f"/api/voice/speakers/{pk}",
                           {"speaker_key": VP_SPEAKER_KEY, "name": "儿子",
                            "description": VP_SPEAKER_DESC})
    SV.check("登记 ★ 改回来（恢复端到端要用的描述）",
             code == 200 and row.get("description") == VP_SPEAKER_DESC, str(code))

    # ---- admin 看全部 ----
    code, _ = other.call("POST", "/api/voice/speakers",
                         {"speaker_key": "daughter", "name": "女儿"})
    SV.check("登记 ★ 第二个用户也能建自己的", code == 201, str(code))
    code, rows = admin.call("GET", "/api/voice/speakers")
    keys = sorted(r["speaker_key"] for r in rows)
    SV.check("登记 ★ 管理员看到全部用户的",
             code == 200 and keys == ["daughter", VP_SPEAKER_KEY], str(keys))

    # ---- 软删 + 删后可重建同 key ----
    code, _ = admin.call("DELETE", f"/api/voice/speakers/{pk}")
    SV.check("登记 ★ 软删（204）", code == 204, str(code))
    code, _ = admin.call("GET", f"/api/voice/speakers/{pk}")
    SV.check("登记 ★ 删后 GET ⇒ 404（不再参与任何列表）", code == 404, str(code))
    code, row = admin.call("POST", "/api/voice/speakers", {
        "speaker_key": VP_SPEAKER_KEY, "name": VP_SPEAKER_NAME,
        "description": VP_SPEAKER_DESC})
    SV.check("登记 ★ 删掉之后同 key 能重建（409 只数未删行）",
             code == 201, f"{code} {str(row)[:100]}")
    pk = int(row["id"]) if isinstance(row, dict) else pk

    # ---- health 端点 ----
    code, obj = admin.call("GET", "/api/voice/voiceprint/health")
    SV.check("登记 ★ health：enabled + healthy（假服务在线）",
             code == 200 and obj.get("enabled") is True and obj.get("healthy") is True,
             str(obj))
    SV.check("登记 ★ health 不吐 URL 与 key（任何登录用户都能问这个接口）",
             isinstance(obj, dict) and "url" not in obj and "key" not in obj,
             str(sorted(obj.keys())))
    SV.check("登记 ★ health 报阈值（0.4 默认）",
             isinstance(obj, dict) and obj.get("threshold") == 0.4, str(obj))


# ================================================================ 3 端到端
async def _connect(ids: dict):
    """建连 + 握手 + drain，返回 (ws, session_id)。

    与 smoke_voice 的 ``_connect`` 同构，但**WS_URL 用本脚本的端口** ——
    直接复用 SV._connect 会连到 smoke_voice 那个根本没起的服务上
    （smoke_mqtt_udp 同款教训：不能图省事用别人的端口常量）。
    """
    headers = {"Authorization": f"Bearer {ids['token']}",
               "Device-Id": MAC_BOUND,
               "Protocol-Version": "1",
               "Client-Id": "smoke-vp-client"}
    ws = await SV.ws_connect(f"ws://127.0.0.1:{HTTP_PORT}/api/voice/v1/ws",
                             open_timeout=10, headers=headers)
    await ws.send(json.dumps({
        "type": "hello", "version": 1,
        "features": {"mcp": True}, "transport": "websocket",
        "audio_params": {"format": "opus", "sample_rate": 16000,
                         "channels": 1, "frame_duration": 60},
    }))
    hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
    await _drain(ws)
    return ws, str(hello.get("session_id", ""))


async def _drain(ws: object, seconds: float = 0.2) -> None:
    end = time.time() + seconds
    while time.time() < end:
        try:
            await asyncio.wait_for(ws.recv(), timeout=max(0.05, end - time.time()))
        except Exception:  # noqa: BLE001
            break


async def _one_turn(ws, sid: str, *, frames: int = 6, wait: float = 30.0):
    """跑一轮"收音 -> 播报"，返回 (句子列表, 事件列表, 二进制帧数)。"""
    await ws.send(json.dumps({"session_id": sid, "type": "listen",
                              "state": "start", "mode": "auto"}))
    await asyncio.sleep(0.25)
    for _ in range(frames):
        await ws.send(SV.FAKE_OPUS_PACKET)
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


def _last_chat() -> dict:
    bodies = SV.UP.get("chat_bodies") or []
    return bodies[-1] if bodies else {}


def _msgs(body: dict) -> list[dict]:
    return list(body.get("messages") or [])


async def t_e2e(ids: dict) -> None:
    print("── 3. 端到端：认人 / 不认人 / 没登记 / 服务挂 ──")
    backup = SV._patch_opus(rms_script=0.5)   # 恒定 RMS：每轮都有声
    try:
        # ---- 3.1 一个说话人都没登记：声纹服务零请求 ----
        # （此时库里还没有任何 VoiceSpeaker 行 —— router 组建的那条属于
        #   另一个用户的设备主人？不，同一个 admin。所以这里先删掉再跑。）
        from app.db import SessionLocal
        from app import models
        from sqlalchemy import select
        with SessionLocal() as db:
            for row in db.scalars(select(models.VoiceSpeaker)).all():
                db.delete(row)
            db.commit()

        VP.identify_requests = 0
        VP.health_requests = 0
        ws, sid = await _connect(ids)
        try:
            sentences, events, bins = await _one_turn(ws, sid)
        finally:
            await ws.close()
        SV.check("端到端 ★ 没登记说话人时对话照常完成（有 stt 有播报）",
                 any(e[1] == "stt" for e in events) and bool(sentences),
                 str([e[1] for e in events])[:120])
        SV.check("端到端 ★ 没登记说话人 ⇒ 声纹服务零请求（不白跑一趟）",
                 VP.identify_requests == 0 and VP.health_requests == 0,
                 f"identify={VP.identify_requests} health={VP.health_requests}")
        body = _last_chat()
        msgs = _msgs(body)
        SV.check("端到端 ★ 没登记说话人 ⇒ user 消息是纯文本（无 speaker 键）",
                 bool(msgs) and '"speaker"' not in str(msgs[-1].get("content")),
                 str(msgs[-1])[:120] if msgs else "no messages")

        # ---- 3.2/3.3 同一条连接跑两轮：首轮 JSON + system 注入，第二轮降级 ----
        # 为什么必须同连接：引入状态（_introduced_speakers）是**按会话**记的
        # （复刻上游 per-connection 的 introduced_speakers）。设备重连 = 新会话
        # = 重新介绍一次，这是设计决策不是缺陷 —— 换连接测"第二轮"测到的
        # 只是"每个新会话都会重新介绍"，那条断言会恒红。
        with SessionLocal() as db:
            db.add(models.VoiceSpeaker(
                owner_id=int(ids["user_id"]), speaker_key=VP_SPEAKER_KEY,
                name=VP_SPEAKER_NAME, description=VP_SPEAKER_DESC))
            db.commit()

        VP.mode = "match"
        ws, sid = await _connect(ids)
        try:
            sentences, events, bins = await _one_turn(ws, sid)
            SV.check("端到端 ★ 认出来时对话照常完成", bool(sentences),
                     str(sentences)[:120])
            body = _last_chat()
            msgs = _msgs(body)
            user_content = str(msgs[-1].get("content")) if msgs else ""
            want = json.dumps({"speaker": VP_SPEAKER_NAME,
                               "content": SV.FAKE_ASR_TEXT}, ensure_ascii=False)
            SV.check("端到端 ★ 首轮 user 消息是 {\"speaker\",\"content\"} JSON",
                     user_content == want, user_content[:160])
            system = str(msgs[0].get("content")) if msgs else ""
            SV.check("端到端 ★ system 首轮注入「当前说话人」行",
                     f"当前说话人：{VP_SPEAKER_NAME}。" in system
                     and VP_SPEAKER_DESC in system, system[-200:])
            stt_texts = [e[3] for e in events if e[1] == "stt"]
            SV.check("端到端 ★ 下发给设备的 stt 是纯文本（不是 JSON）",
                     stt_texts == [SV.FAKE_ASR_TEXT], str(stt_texts))
            SV.check("端到端 ★ 这一轮真的调了声纹服务（identify 一次）",
                     VP.identify_requests >= 1, str(VP.identify_requests))

            # ---- 第二轮（同连接）：降级 ----
            await _drain(ws, 0.3)
            sentences, events, bins = await _one_turn(ws, sid)
            body = _last_chat()
            msgs = _msgs(body)
            user_content = str(msgs[-1].get("content")) if msgs else ""
            SV.check("端到端 ★ 第二轮 user 消息降为纯文本（不再带 speaker JSON）",
                     user_content == SV.FAKE_ASR_TEXT, user_content[:160])
            system = str(msgs[0].get("content")) if msgs else ""
            SV.check("端到端 ★ 第二轮 system 不再重复注入（防模型反复称呼）",
                     "当前说话人" not in system, system[-160:])
            SV.check("端到端 ★ 第二轮对话照常完成", bool(sentences),
                     str(sentences)[:100])
        finally:
            await ws.close()

        # ---- 3.4 分数低于阈值：不包 JSON、不注入，对话照常 ----
        VP.mode = "low_score"
        ws, sid = await _connect(ids)
        try:
            sentences, events, bins = await _one_turn(ws, sid)
        finally:
            await ws.close()
        body = _last_chat()
        msgs = _msgs(body)
        user_content = str(msgs[-1].get("content")) if msgs else ""
        system = str(msgs[0].get("content")) if msgs else ""
        SV.check("端到端 ★ 低于阈值 ⇒ user 纯文本",
                 user_content == SV.FAKE_ASR_TEXT, user_content[:160])
        SV.check("端到端 ★ 低于阈值 ⇒ system 无说话人行（行为与没开声纹一致）",
                 "当前说话人" not in system, "")
        SV.check("端到端 ★ 低于阈值时对话照常完成", bool(sentences), str(sentences)[:100])
        VP.mode = "match"

        # ---- 3.5 服务端报了没问过的 ID：不认 ----
        VP.mode = "unknown_id"
        ws, sid = await _connect(ids)
        try:
            sentences, events, bins = await _one_turn(ws, sid)
        finally:
            await ws.close()
        body = _last_chat()
        msgs = _msgs(body)
        user_content = str(msgs[-1].get("content")) if msgs else ""
        SV.check("端到端 ★ 服务端报未登记 ID ⇒ 不认（user 纯文本）",
                 user_content == SV.FAKE_ASR_TEXT, user_content[:160])
        VP.mode = "match"

        # ---- 3.6 服务不可达（identify 撞连接拒绝）：对话照常 ----
        # 健康缓存还记着 healthy，所以这一轮真的会发 identify 然后失败 ——
        # 正是要验的"失败不外溢"。
        vp_srv = _e2e_vp_server
        vp_srv.shutdown()
        vp_srv.server_close()
        ws, sid = await _connect(ids)
        try:
            sentences, events, bins = await _one_turn(ws, sid)
        finally:
            await ws.close()
        SV.check("端到端 ★ 声纹服务不可达时对话照常完成（认不出不是事故）",
                 bool(sentences) and any(e[1] == "stt" for e in events),
                 str([e[1] for e in events])[:120])
        body = _last_chat()
        msgs = _msgs(body)
        user_content = str(msgs[-1].get("content")) if msgs else ""
        SV.check("端到端 ★ 服务不可达 ⇒ user 纯文本", user_content == SV.FAKE_ASR_TEXT,
                 user_content[:160])

        # ---- 3.7 服务不健康（连都连不上）：跳过识别，对话照常 ----
        import app.voice.voiceprint as VPMod
        old_url = _app_config().VOICEPRINT_URL
        try:
            _app_config().VOICEPRINT_URL = "http://127.0.0.1:1/voiceprint/health?key=x"
            VPMod.reset_provider()
            ws, sid = await _connect(ids)
            try:
                sentences, events, bins = await _one_turn(ws, sid)
            finally:
                await ws.close()
            SV.check("端到端 ★ 服务不健康时对话照常完成",
                     bool(sentences) and any(e[1] == "stt" for e in events),
                     str([e[1] for e in events])[:120])
            body = _last_chat()
            msgs = _msgs(body)
            user_content = str(msgs[-1].get("content")) if msgs else ""
            SV.check("端到端 ★ 服务不健康 ⇒ user 纯文本",
                     user_content == SV.FAKE_ASR_TEXT, user_content[:160])
        finally:
            _app_config().VOICEPRINT_URL = old_url
            VPMod.reset_provider()
    finally:
        SV._restore_opus(backup)


# ================================================================ main
def _run_groups(ids: dict) -> None:
    t_provider()
    t_router(ids)
    asyncio.run(t_e2e(ids))


def _dump_results(path: str) -> None:
    payload = {"passed": [n for n, ok, _d in SV.results if ok],
               "failed": [n for n, ok, _d in SV.results if not ok]}
    Path(path).write_text(json.dumps(payload, ensure_ascii=False),
                          encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    global HTTP_PORT, VP_PORT, _e2e_vp_server
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    mutate = args.mutate

    if mutate:
        # 变异子进程：装钩子 → 起假声纹服务 → 起服务 → 跑全部组 → 落盘结果。
        _install_mutation_hook(mutate)
        # 子进程用自己的口（父进程此刻还占着父口），否则会连到父进程的
        # 服务上 —— 那是"连到别人的服务却全绿"的假绿形态。
        VP_PORT = int(os.environ.get("SMOKE_VOICEPRINT_PORT", str(VP_PORT)))
        port = args.port or (resolve_port("smoke_voiceprint") + 1)
        HTTP_PORT = port
        _e2e_vp_server = start_voiceprint(VP_PORT)
        upstream, up_port = SV.start_upstream()
        server = start_server(port, VP_PORT)
        if not wait_ready(port):
            print("[fatal] 变异子进程服务未就绪")
            return 1
        ids = SV.seed(up_port)
        _run_groups(ids)
        try:
            server.should_exit = True
        except Exception:  # noqa: BLE001
            pass
        upstream.shutdown()
        if args.out:
            _dump_results(args.out)
        failed = [n for n, ok, _d in SV.results if not ok]
        print(f"\n[mutate {mutate}] 失败 {len(failed)} 项：")
        for n in failed:
            print(f"  - {n}")
        return 1 if failed else 0

    port = args.port or resolve_port("smoke_voiceprint")
    HTTP_PORT = port
    print(f"[boot] 声纹认人冒烟，HTTP {port}，假声纹服务 {VP_PORT}")
    guard_or_die(port, "smoke_voiceprint.py")
    if not _udp_like_free(VP_PORT):
        print(f"[boot] ✗ 假声纹服务口 {VP_PORT} 已被占用。\n"
              f"  换口重跑：SMOKE_VOICEPRINT_PORT=<其它口> "
              f"python tools/smoke_voiceprint.py")
        return 1
    hold, hold_hint = guard_port(port)
    if hold is None:
        print(hold_hint)
        return 1

    _e2e_vp_server = start_voiceprint(VP_PORT)
    upstream, up_port = SV.start_upstream()
    print(f"[boot] 假上游 :{up_port}，假声纹服务 :{VP_PORT}")
    release_port(hold, port)

    server = start_server(port, VP_PORT)
    if not wait_ready(port):
        print("[fatal] 服务未就绪")
        return 1
    ids = SV.seed(up_port)
    _run_groups(ids)

    try:
        server.should_exit = True
    except Exception:  # noqa: BLE001
        pass
    upstream.shutdown()

    passed = sum(1 for _n, ok, _d in SV.results if ok)
    failed = [n for n, ok, _d in SV.results if not ok]
    print("\n" + "=" * 64)
    print(f"通过 {passed} 项，失败 {len(failed)} 项，跳过 {len(SV.skipped)} 项")
    if failed:
        print("\n失败的断言：")
        for name in failed:
            print(f"  - {name}")
        print("\n结论：**红**")
        return 1

    # ---- 反向变异自检 ----
    if args.no_mutation:
        print("\n结论：**绿**（--no-mutation：未做变异自检）")
        return 0

    print("\n── 反向变异自检（每条都必须让指定断言变红）──")
    mutation_failures: list[str] = []
    for i, (mid, (_old, _new, required)) in enumerate(MUTATIONS.items()):
        mport = port + 1 + i
        vport = VP_PORT + 1 + i
        out = SV.DB_DIR / f"elder_smoke_vp_mut_{mid}_{os.getpid()}.json"
        child_env = dict(os.environ)
        # 每个子进程拿自己的 HTTP 口与假声纹口：父进程此刻还占着这两个口，
        # 共用会让子进程连到**父进程的服务**上 —— 那正是"连到别人的服务
        # 却全绿"的假绿形态（smoke_ports.py 的教训）。
        child_env["SMOKE_VOICEPRINT_PORT"] = str(vport)
        try:
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()),
                 "--mutate", mid, "--port", str(mport), "--out", str(out)],
                capture_output=True, text=True, timeout=900,
                cwd=str(ROOT), env=child_env)
        except subprocess.TimeoutExpired:
            mutation_failures.append(f"{mid}: 变异子进程超时（900s）")
            continue
        try:
            summary = json.loads(out.read_text(encoding="utf-8"))
            child_failed = summary.get("failed", [])
        except Exception:  # noqa: BLE001
            child_failed = []
        missing = [r for r in required if r not in child_failed]
        if proc.returncode != 1:
            mutation_failures.append(
                f"{mid}: 变异子进程退出码 {proc.returncode}（期望 1 = 有断言变红）")
        elif missing:
            mutation_failures.append(
                f"{mid}: 这些断言本该变红却没有 —— {missing}")
        else:
            print(f"  {mid} ✓ 指定断言变红：{required[0][:44]}")
        if proc.returncode != 1 or missing:
            tail = (proc.stdout or "")[-1500:] + "\n" + (proc.stderr or "")[-1500:]
            print(f"    [子进程输出尾部]\n{tail}")
    if mutation_failures:
        print("\n变异自检失败（对应断言没有鉴别力 = 假绿）：")
        for line in mutation_failures:
            print(f"  - {line}")
        print("\n结论：**红**")
        return 1
    print(f"\n结论：**绿**（含 {len(MUTATIONS)} 条反向变异自检）")
    return 0


def _udp_like_free(port: int) -> bool:
    """TCP 口空闲检查（假声纹服务是 TCP）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="声纹认人冒烟")
    # 位置参数端口是**仓库约定**（CI 一律 `... smoke_xxx.py <port>`），
    # 只声明 --port 会让 CI 上那一步直接 argparse rc=2（F1 实测踩过）。
    ap.add_argument("port", type=int, nargs="?", default=None,
                    help="HTTP 端口（与 --port 等价；不给则查 smoke_ports 表）")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-mutation", action="store_true",
                    help="跳过反向变异自检")
    ap.add_argument("--mutate", choices=sorted(MUTATIONS), default=None,
                    help=argparse.SUPPRESS)   # 内部用：变异子进程
    ap.add_argument("--out", default=None, help=argparse.SUPPRESS)
    return ap.parse_args(argv)


#: 端到端组起的假声纹服务实例（3.6 要停掉它模拟"服务消失"）
_e2e_vp_server: ThreadingHTTPServer | None = None


if __name__ == "__main__":
    raise SystemExit(main())
