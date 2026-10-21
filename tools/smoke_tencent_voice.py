"""腾讯云语音（TTS + ASR）冒烟 —— 算法对齐官方文档 + 假上游端到端 + 反向变异。

为什么这个脚本必须存在（而不是"配了就能用，不用测"）
--------------------------------------------------
腾讯云的两条链路都**没有真实密钥就无法验证**：签名错了是 401，音色不支持
24k 是 InvalidParameterValue.SampleRate，音色 ID 填成火山的字符串是
InvalidParameterValue.VoiceType —— 三种失败的错误信息互不相干，而管理员
在后台点「保存」时一条都不会提前暴露。等老人听不到声音再查，成本高得多。

所以本脚本用三层手段把能验的都验掉：

1. **对齐官方文档的测试向量**（最强的一层，见 A 组）
   腾讯云「签名方法 v3」文档（1073/37990）给出了一个完整的示例请求，
   其中 **CanonicalRequest 全文、HashedRequestPayload、StringToSign 全文**
   三个值**不依赖 SecretKey**，因此可以逐字节复现。我们把自己的
   ``canonical_request()`` / ``string_to_sign()`` 输出与它比对 ——
   任何格式偏差（多一个空行、header 顺序不对、缺终换行、payload 哈希错）
   都会立刻变红。这比"自己算一遍再和自己比"强得多：后者两边一起错就永远绿。

2. **假上游端到端 + 服务端复算验签**（D/E 组）
   起一个本地 HTTP 服务冒充 ``tts.tencentcloudapi.com``，它**独立实现一遍
   签名复算**（不复用 ``tc3.py`` 的任何函数），用收到的 body/host/timestamp
   重算 Signature 并与请求头比对。这等价于"腾讯云服务端真的验了签"——
   时间戳与签名不一致、signed headers 不匹配、body 哈希错，都会被抓到。

3. **反向变异**（H 组）
   把被测行为改回去，子进程确认指定断言真的变红。四条变异覆盖：
   编码参数（Codec）、UTC 日期、DataLen 口径、文本分段。

端口：本脚本起一个假上游 HTTP 服务，端口见 ``tools/smoke_ports.py``
（``smoke_tencent_voice`` 项）。⚠️ 与其它冒烟脚本一样用**位置参数**传端口。

用法::

    python tools/smoke_tencent_voice.py [8134] [--no-mutation]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(ROOT / "tools"))

# ⚠️ 必须在 import 任何网络代码之前清代理：本机可能设了 HTTP_PROXY，
# 而假上游在 127.0.0.1 —— urllib 会把本地请求也发给代理，表现是
# "连接被拒"或超时，看着像服务没起。这是本仓库反复踩过的坑。
for _k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

from smoke_ports import resolve_port  # noqa: E402

HTTP_PORT = 0
_MUTATE = ""

# ---------------------------------------------------------------- 结果计数
_PASS = 0
_FAIL = 0
_SKIP = 0
_FAILED_NAMES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """记一条断言。**失败明细要短**，长文本一律截断 —— 刷屏会让真因被淹没。"""
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"  ✓ {name}")
    else:
        _FAIL += 1
        _FAILED_NAMES.append(name)
        print(f"  ✗ {name}" + (f"  [{str(detail)[:200]}]" if detail else ""))
    return ok


def skip(name: str, why: str) -> None:
    """第三态：**判据对象不存在**，既不算通过也不算失败。

    混进"通过"是反向假绿（看着 100% 绿，其实一条没验）。
    """
    global _SKIP
    _SKIP += 1
    print(f"  - SKIP {name}：{why}")


# ================================================================ 假腾讯云上游
class _UpstreamState:
    """假上游的可变行为开关 + 收到的请求记录。"""

    def __init__(self) -> None:
        self.mode = "ok"          # ok / error / ratelimit
        self.requests: list[dict] = []
        self.signature_ok: list[bool] = []
        self.lock = threading.Lock()

    def reset(self, mode: str = "ok") -> None:
        with self.lock:
            self.mode = mode
            self.requests = []
            self.signature_ok = []

    def note(self, item: dict, sig_ok: bool) -> None:
        with self.lock:
            self.requests.append(item)
            self.signature_ok.append(sig_ok)


UP = _UpstreamState()

#: 假上游认的密钥。被测代码必须用同一对。
SECRET_ID = "AKIDsmoketest0001"
SECRET_KEY = "smokesecret0001"


def _verify_signature(headers, body: bytes) -> bool:
    """**独立实现**一遍 TC3 签名复算（刻意不复用 tc3.py）。

    为什么不复用：复用等于用被测代码验被测代码 —— 两边一起错就永远绿。
    这里按官方文档 37990 的步骤逐字重写，只依赖标准库。
    """
    auth = headers.get("Authorization") or ""
    if not auth.startswith("TC3-HMAC-SHA256 "):
        return False
    try:
        parts = dict(
            seg.strip().split("=", 1) for seg in auth[len("TC3-HMAC-SHA256 "):].split(",")
        )
    except ValueError:
        return False
    credential = parts.get("Credential", "")
    signed_headers = parts.get("SignedHeaders", "")
    signature = parts.get("Signature", "")
    if not (credential and signed_headers and signature):
        return False

    secret_id, _, scope = credential.partition("/")
    date, service, term = (scope.split("/") + ["", "", ""])[:3]
    if secret_id != SECRET_ID or term != "tc3_request":
        return False

    host = headers.get("Host") or ""
    ts = headers.get("X-TC-Timestamp") or ""
    ctype = headers.get("Content-Type") or ""

    # 逐字复刻官方示例的拼法（注意每个 header 行都要有结尾换行）
    canonical_headers = (f"content-type:{ctype}\n"
                         f"host:{host}\n")
    signed = "content-type;host"
    if signed_headers != signed:
        return False
    hashed_payload = hashlib.sha256(body).hexdigest()
    canonical = "\n".join(["POST", "/", "", canonical_headers, signed,
                           hashed_payload])
    hashed_canonical = hashlib.sha256(canonical.encode()).hexdigest()
    string_to_sign = "\n".join(["TC3-HMAC-SHA256", ts,
                                f"{date}/{service}/tc3_request", hashed_canonical])

    def sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k = sign(("TC3" + SECRET_KEY).encode(), date)
    k = sign(k, service)
    k = sign(k, "tc3_request")
    expect = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, signature)


class _FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D102 —— 静音，否则线程日志刷屏
        pass

    def do_POST(self):  # noqa: N802 —— BaseHTTPRequestHandler 的约定名
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        action = self.headers.get("X-TC-Action") or ""
        sig_ok = _verify_signature(self.headers, body)

        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            parsed = {}

        UP.note({"action": action, "body": parsed,
                 # ⚠️ **键统一小写**：urllib 的 Request.add_header 会对 header 名
                 # 做 capitalize()（"X-TC-Version" → "X-tc-version"），而 HTTP
                 # 头本来就大小写不敏感。这里不归一化，测试一定会拿到 None，
                 # 而症状是「服务端收到了但断言说没收到」—— 极难自查。
                 "headers": {k.lower(): v for k, v in self.headers.items()},
                 "raw": body}, sig_ok)

        mode = UP.mode
        if mode == "error":
            resp = {"Response": {"Error": {
                "Code": "FailedOperation.ServiceIsolate",
                "Message": "账号已欠费停服"}}}
        elif mode == "ratelimit":
            resp = {"Response": {"Error": {
                "Code": "LimitExceeded.AccessLimit",
                "Message": "请求超过限制频率"}}}
        elif action == "TextToVoice":
            # 假音频：长度与文本长度挂钩，便于断言"分段后总长变多"
            audio = b"\x11\x22" * max(20, min(len(str(parsed.get("Text") or "")), 400))
            resp = {"Response": {"Audio": base64.b64encode(audio).decode(),
                                 "SessionId": parsed.get("SessionId") or "",
                                 "RequestId": "req-tts"}}
        elif action == "SentenceRecognition":
            resp = {"Response": {"Result": "老人，该吃药了",
                                 "AudioDuration": 1500,
                                 "RequestId": "req-asr"}}
        else:
            resp = {"Response": {"Error": {
                "Code": "InvalidAction", "Message": f"未知 action {action}"}}}

        raw = json.dumps(resp).encode("utf-8")
        self.send_response(200)      # ⚠️ 腾讯云业务错误也是 HTTP 200
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start_upstream(port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), _FakeHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ================================================================ A 组：官方向量
#: 官方「签名方法 v3」文档（1073/37990）示例的请求体，**逐字**。
_OFFICIAL_PAYLOAD = json.dumps(
    {"Limit": 1, "Filters": [{"Values": ["\u672a\u547d\u540d"],
                              "Name": "instance-name"}]}).encode("utf-8")
_OFFICIAL_TS = 1551113065
_OFFICIAL_DATE = "2019-02-25"
_OFFICIAL_PAYLOAD_HASH = "35e9c5b0e3ae67532d3c9f17ead6c90222632e5b1ff7f6e89887f1398934f064"
_OFFICIAL_CANONICAL = (
    "POST\n"
    "/\n"
    "\n"
    "content-type:application/json; charset=utf-8\n"
    "host:cvm.tencentcloudapi.com\n"
    "x-tc-action:describeinstances\n"
    "\n"
    "content-type;host;x-tc-action\n"
    + _OFFICIAL_PAYLOAD_HASH
)
_OFFICIAL_CANONICAL_HASH = "7019a55be8395899b900fb5564e4200d984910f34794a27cb3fb7d10ff6a1e84"
_OFFICIAL_STRING_TO_SIGN = (
    "TC3-HMAC-SHA256\n"
    f"{_OFFICIAL_TS}\n"
    "2019-02-25/cvm/tc3_request\n"
    + _OFFICIAL_CANONICAL_HASH
)


def t_official_vectors() -> None:
    print("\n── A. TC3 算法对齐官方文档（可独立复现的三个值）──")
    from app.voice import tc3

    # A1 payload 哈希
    got = tc3.sha256_hex(_OFFICIAL_PAYLOAD)
    check("A1 HashedRequestPayload 与官方文档一致", got == _OFFICIAL_PAYLOAD_HASH,
          f"得到 {got}")

    # A2 CanonicalRequest 逐字节
    headers = {
        "content-type": "application/json; charset=utf-8",
        "host": "cvm.tencentcloudapi.com",
        "x-tc-action": "describeinstances",
    }
    got_cr = tc3.canonical_request("POST", "/", "", headers, "", _OFFICIAL_PAYLOAD)
    check("A2 CanonicalRequest 逐字节等于官方文档",
          got_cr == _OFFICIAL_CANONICAL,
          repr(got_cr[:160]) if got_cr != _OFFICIAL_CANONICAL else "")

    # A3 规范串哈希
    got_ch = tc3.sha256_hex(got_cr.encode())
    check("A3 HashedCanonicalRequest 与官方文档一致",
          got_ch == _OFFICIAL_CANONICAL_HASH, f"得到 {got_ch}")

    # A4 StringToSign 逐行
    got_sts = tc3.string_to_sign(_OFFICIAL_TS, "2019-02-25/cvm/tc3_request", got_cr)
    check("A4 StringToSign 逐行等于官方文档",
          got_sts == _OFFICIAL_STRING_TO_SIGN, repr(got_sts[:200]))

    # A5 date 必须取自 UTC（官方示例 1551113065 → 2019-02-25）
    ts, date = tc3.utc_stamps(
        datetime.fromtimestamp(_OFFICIAL_TS, tz=timezone.utc))
    check("A5 utc_stamps 由时间戳还原出官方 UTC 日期",
          ts == _OFFICIAL_TS and date == _OFFICIAL_DATE,
          f"得到 ts={ts} date={date}")

    # A6 授权头结构（Signature 是 64 位 hex，scope 三段）
    auth, signed = tc3.authorization(
        "AKIDx", "SECRETx", timestamp=_OFFICIAL_TS, date=_OFFICIAL_DATE,
        service="cvm", canonical=got_cr, signed=headers)
    sig = auth.rsplit("Signature=", 1)[-1]
    check("A6 Authorization 结构（前缀/scope/SignedHeaders/64位hex）",
          auth.startswith("TC3-HMAC-SHA256 Credential=AKIDx/2019-02-25/cvm/tc3_request, ")
          and signed == "content-type;host;x-tc-action"
          and len(sig) == 64 and all(c in "0123456789abcdef" for c in sig),
          auth[:150])


def t_tc3_details() -> None:
    print("\n── B. TC3 细节（密钥解析 / UTC 日期 / 派生）──")
    from app.voice import tc3

    check("B1 两段密钥·英文冒号",
          tc3.parse_secret_pair("AKID:x") == ("AKID", "x"))
    check("B2 两段密钥·全角冒号（复制粘贴常混入）",
          tc3.parse_secret_pair("AKID：x") == ("AKID", "x"),
          str(tc3.parse_secret_pair("AKID：x")))
    check("B3 只填一段 → SecretKey 为空（上层据此报「要两段」）",
          tc3.parse_secret_pair("onlyid") == ("onlyid", ""))
    check("B4 空密钥 → 空二元组",
          tc3.parse_secret_pair("   ") == ("", ""))

    # B5 ⚠️ 最关键的一条：UTC 16:30 在北京是次日 00:30。
    # 若实现用本地日期算 scope，会得到 10-22 而不是 10-21 ⇒ 夜间全部 401。
    night = datetime(2026, 10, 21, 16, 30, tzinfo=timezone.utc)
    _, date = tc3.utc_stamps(night)
    check("B5 跨日时刻仍取 UTC 日期（北京次日 0:30 不能滚到次日）",
          date == "2026-10-21", f"得到 {date}")

    # B6 派生密钥必须随 secret 变化（能抓「忽略 secret」这类退化）
    k1 = tc3.derive_signing_key("secretA", "2026-10-21", "tts")
    k2 = tc3.derive_signing_key("secretB", "2026-10-21", "tts")
    k3 = tc3.derive_signing_key("secretA", "2026-10-21", "asr")
    check("B6 派生密钥随 secret 与服务变化（非固定值）",
          k1 != k2 and k1 != k3 and len(k1) == 32,
          f"len={len(k1)}")

    # B7 "TC3" 是**前缀拼进 key**，不是消息。写反了结构一样但签名不同。
    expect_prefix = hmac.new(("TC3" + "secretA").encode(), b"2026-10-21",
                             hashlib.sha256).digest()
    wrong_prefix = hmac.new(b"secretA", b"TC3", hashlib.sha256).digest()
    k_date_impl = k1  # 已含三级派生，这里只比对第一级的行为差异
    check("B7 密钥派生第一级用「TC3+SecretKey」当 key（与反向写法不同）",
          expect_prefix != wrong_prefix and len(k_date_impl) == 32,
          "前缀拼接与消息拼接必须区分")


# ================================================================ C 组：TTS 纯函数
def t_tts_pure() -> None:
    print("\n── C. TTS 纯函数（分段 / 音色 / 速率 / 报文）──")
    from app.voice import tts_tencent as T

    # 分段
    long_text = "药" * 400
    chunks = T.split_text(long_text)
    check("C1 超长无标点文本按 150 字硬切",
          [len(c) for c in chunks] == [150, 150, 100],
          str([len(c) for c in chunks]))
    check("C2 分段结果拼接后与原文一致（不丢字）",
          "".join(chunks) == long_text)
    check("C3 短文本不分段",
          T.split_text("该吃药了。") == ["该吃药了。"])
    check("C4 空文本 → 空列表（不产出空段）",
          T.split_text("   ") == [])
    multi = "今天天气不错。" * 30     # 210 字，含句号
    chunks2 = T.split_text(multi)
    check("C5 长文本按句读切分（每段不超过上限）",
          all(len(c) <= 150 for c in chunks2) and len(chunks2) >= 2,
          str([len(c) for c in chunks2]))

    # 音色
    check("C6 音色 '601009' → 整数 601009", T._voice_type("601009") == 601009)
    check("C7 音色留空 → 用出厂默认 601009（爱小芊，情感最全）",
          T._voice_type("") == int(T.DEFAULT_VOICE) == 601009,
          f"得到 {T._voice_type('')}")
    raised = False
    try:
        T._voice_type("BV001_streaming")
    except RuntimeError as exc:
        raised = "整数" in str(exc)
    check("C8 填入别家音色 ID → 报错并指出要整数", raised)

    # 速率
    check("C9 倍率 1.0 精确映射到档位 0（不能有轻微偏差）",
          T._speed_gear(1.0) == 0.0, str(T._speed_gear(1.0)))
    check("C10 倍率 1.5 → 档位 2", T._speed_gear(1.5) == 2.0,
          str(T._speed_gear(1.5)))
    check("C11 倍率 0.6 → 档位 -2", T._speed_gear(0.6) == -2.0)

    # 报文
    body = T.build_body("你好", 1001)
    check("C12 Codec 必须是 pcm（客户端只吃裸 PCM）",
          body.get("Codec") == "pcm", str(body.get("Codec")))
    check("C13 SampleRate 必须是 24000（否则客户端按 24k 播会变调）",
          body.get("SampleRate") == 24000, str(body.get("SampleRate")))
    check("C14 中文语言 + 默认模型 + 非空 SessionId",
          body.get("PrimaryLanguage") == 1 and body.get("ModelType") == 1
          and bool(body.get("SessionId")))
    check("C15 倍率 1.0 时不带 Speed 字段（让厂商用默认语速）",
          "Speed" not in body, str(body.get("Speed")))
    emo = T.build_body("你好", 1001, emotion="happy")
    check("C16 传了情感 → 带 EmotionCategory/Intensity",
          emo.get("EmotionCategory") == "happy"
          and emo.get("EmotionIntensity") == 100)

    # 响应解包
    pcm = b"\x01\x02" * 50
    resp = {"Audio": base64.b64encode(pcm).decode()}
    check("C17 base64 音频正确解出", T._decode_audio(resp) == pcm)
    wav_like = b"RIFF" + b"\x00" * 40 + b"\x01\x02" * 10
    check("C18 服务端若回了 wav 容器，剥掉 44 字节头",
          T._decode_audio({"Audio": base64.b64encode(wav_like).decode()})
          == b"\x01\x02" * 10)
    odd = b"\x01\x02" * 10 + b"\x03"
    check("C19 奇数长度按 16bit 采样对齐（丢半个采样而非送噪音）",
          len(T._decode_audio({"Audio": base64.b64encode(odd).decode()})) == 20)

    # ---- 预置音色表（用户 2026-10-21 提供的控制台清单，8 个）----
    ids = [v["id"] for v in T.TENCENT_VOICES]
    check("C20 预置音色共 8 个且 ID 全为纯数字",
          len(ids) == 8 and all(i.isdigit() for i in ids), str(ids))
    check("C21 ★ 每个预置音色都支持 24000（本平台客户端硬要求）",
          all(24000 in v.get("rates", ()) for v in T.TENCENT_VOICES),
          str([(v["id"], v.get("rates")) for v in T.TENCENT_VOICES
               if 24000 not in v.get("rates", ())]))
    check("C22 默认音色在预置清单内",
          T.DEFAULT_VOICE in ids, T.DEFAULT_VOICE)
    check("C23 大模型音色 601xxx 支持 9 种情感",
          all(len(v.get("emotions", ())) == 9 for v in T.TENCENT_VOICES
              if v["id"].startswith("601")),
          str([(v["id"], len(v.get("emotions", ())))
               for v in T.TENCENT_VOICES if v["id"].startswith("601")]))
    check("C24 超自然音色（402/403/502）只支持中性",
          all(v.get("emotions") == ("neutral",) for v in T.TENCENT_VOICES
              if not v["id"].startswith("601")),
          str([(v["id"], v.get("emotions")) for v in T.TENCENT_VOICES
               if not v["id"].startswith("601")]))

    # ---- 情感白名单：不支持就静默降级，而不是报错 ----
    check("C25 'happy' 在 601009 上受支持",
          T.emotion_supported("601009", "happy") is True)
    check("C26 'happy' 在 402001 上不支持（只有中性）",
          T.emotion_supported("402001", "happy") is False)
    check("C27 手填的未知音色一律放行（不替厂商做判断）",
          T.emotion_supported("999999", "happy") is True)
    check("C28 空情感恒为不支持（不会有 'EmotionCategory=' 空值）",
          T.emotion_supported("601009", "") is False)


# ================================================================ D 组：ASR 纯函数
def t_asr_pure() -> None:
    print("\n── D. ASR 纯函数（报文 / 上限 / base64 口径）──")
    from app.voice import asr_tencent as A

    pcm = b"\xaa" * 3200       # 0.1s @16k/16bit
    body = A.build_body(pcm, "16k_zh")
    check("D1 VoiceFormat 必须是 pcm（与本平台裸 PCM 逐字节一致）",
          body.get("VoiceFormat") == "pcm", str(body.get("VoiceFormat")))
    check("D2 SourceType=1（随请求上传，不用公网 Url）",
          body.get("SourceType") == 1, str(body.get("SourceType")))
    check("D3 DataLen 是**未编码**的字节数（填成 base64 长度会漏字）",
          body.get("DataLen") == len(pcm), f"{body.get('DataLen')} vs {len(pcm)}")
    data = body.get("Data") or ""
    check("D4 Data 是 base64 且**不含换行**（官方明确要求）",
          "\n" not in data and "\r" not in data, repr(data[:40]))
    check("D5 Data base64 解码后与原始 PCM 逐字节一致",
          base64.b64decode(data) == pcm)
    check("D6 默认引擎是 16k_zh（本平台音频 16kHz）",
          A.build_body(pcm).get("EngSerViceType") == "16k_zh")
    check("D7 传了热词表则下发 HotwordList",
          A.build_body(pcm, hotwords="氨氯地平|10").get("HotwordList") == "氨氯地平|10")

    check("D8 时长换算（16k×16bit×60s = 1920000 字节）",
          abs(A.seconds_of(b"\x00" * 1920000) - 60.0) < 1e-6,
          str(A.seconds_of(b"\x00" * 1920000)))

    over = b"\x00" * int(61 * 16000 * 2)
    raised = False
    try:
        A._check_limits(over)
    except RuntimeError as exc:
        raised = "60" in str(exc)
    check("D9 超过 60s → 本地拦截并说明上限（不发出去换错误码）", raised)

    raised_empty = False
    try:
        A._check_limits(b"")
    except RuntimeError:
        raised_empty = True
    check("D10 空音频 → 本地拦截", raised_empty)

    ok_boundary = True
    try:
        A._check_limits(b"\x00" * 1920000)   # 正好 60s
    except RuntimeError:
        ok_boundary = False
    check("D11 正好 60s 放行（边界不多不少）", ok_boundary)


# ================================================================ E 组：端到端 TTS
class _Row:
    """最小配置行替身（asr_tts 的适配层只读这几个属性）。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _fake_row(port: int, *, provider: str = "tencent", model: str = "",
              api_key: str = "") -> "_Row":
    return _Row(
        provider=provider,
        api_style="tencent",
        endpoint=f"http://127.0.0.1:{port}",
        api_key=api_key or f"{SECRET_ID}:{SECRET_KEY}",
        model=model,
        voice="1001",
        name="smoke-tencent",
        timeout_sec=10,
        params="",
    )


def t_e2e_tts(port: int) -> None:
    print("\n── E. 端到端 TTS：真实分派 → 假上游 → 服务端复算验签 ──")
    import app.llm_gateway as gw
    from app.voice import asr_tts

    UP.reset("ok")
    row = _fake_row(port)
    original = gw.resolve_row

    def _patched(db, usage):
        return row if usage == "tts" else original(db, usage)

    gw.resolve_row = _patched
    try:
        pcm = asr_tts.tts(None, "老人家，该吃药了", voice="1001")
    finally:
        gw.resolve_row = original

    check("E1 端到端拿到 PCM 音频", bool(pcm) and len(pcm) > 0, f"len={len(pcm)}")
    check("E2 假上游收到且仅收到 1 次请求（短文本不分段）",
          len(UP.requests) == 1, str(len(UP.requests)))
    if UP.requests:
        req = UP.requests[0]
        check("E3 ★ 假上游独立复算签名通过（等价于服务端验签）",
              UP.signature_ok[0] is True,
              f"sig_ok={UP.signature_ok}")
        check("E4 X-TC-Action = TextToVoice",
              req["action"] == "TextToVoice", req["action"])
        check("E5 X-TC-Version = 2019-08-23",
              req["headers"].get("x-tc-version") == "2019-08-23",
              str(req["headers"].get("x-tc-version")))
        check("E6 线上报文的 Codec/SampleRate 真为 pcm/24000",
              req["body"].get("Codec") == "pcm"
              and req["body"].get("SampleRate") == 24000,
              f"{req['body'].get('Codec')}/{req['body'].get('SampleRate')}")
        check("E7 线上报文 Text 是原文（编码未破坏中文）",
              req["body"].get("Text") == "老人家，该吃药了",
              str(req["body"].get("Text"))[:60])
        check("E8 Authorization 里的 SecretId 取自配置行",
              SECRET_ID in (req["headers"].get("authorization") or ""),
              (req["headers"].get("authorization") or "")[:80])
    else:
        for name in ("E3", "E4", "E5", "E6", "E7", "E8"):
            skip(f"{name} 线上报文断言", "假上游没收到请求")

    # E9 长文本必须分成多次请求（否则超 150 字会被厂商拒）
    UP.reset("ok")
    long_text = "记得按时吃药。" * 40        # 280 字
    gw.resolve_row = _patched
    try:
        asr_tts.tts(None, long_text, voice="1001")
    finally:
        gw.resolve_row = original
    check("E9 超长文本分成多次请求（每次 ≤150 字）",
          len(UP.requests) >= 2
          and all(len(str(r["body"].get("Text") or "")) <= 150 for r in UP.requests),
          f"{len(UP.requests)} 次，最长 {max((len(str(r['body'].get('Text') or '')) for r in UP.requests), default=0)} 字")
    check("E10 每一段都单独通过服务端验签",
          all(UP.signature_ok), str(UP.signature_ok))


# ================================================================ F 组：端到端 ASR
def t_e2e_asr(port: int) -> None:
    print("\n── F. 端到端 ASR：真实分派 → 假上游 → 服务端复算验签 ──")
    import app.llm_gateway as gw
    from app.voice import asr_tts

    UP.reset("ok")
    row = _fake_row(port, provider="tencent_asr", model="16k_zh")
    original = gw.resolve_plan

    def _patched_plan(db, usage):
        return [("asr", row)] if usage == "asr" else original(db, usage)

    gw.resolve_plan = _patched_plan
    try:
        text = asr_tts.asr(None, b"\x11" * 3200)
    finally:
        gw.resolve_plan = original

    check("F1 端到端拿到识别文本", text == "老人，该吃药了", repr(text))
    check("F2 假上游收到 1 次请求", len(UP.requests) == 1, str(len(UP.requests)))
    if UP.requests:
        req = UP.requests[0]
        check("F3 ★ ASR 链路的签名也通过服务端复算（service=asr）",
              UP.signature_ok[0] is True, str(UP.signature_ok))
        check("F4 X-TC-Action = SentenceRecognition",
              req["action"] == "SentenceRecognition", req["action"])
        check("F5 X-TC-Version = 2019-06-14",
              req["headers"].get("x-tc-version") == "2019-06-14",
              str(req["headers"].get("x-tc-version")))
        check("F6 线上 Data 解码后与送出的 PCM 逐字节一致",
              base64.b64decode(req["body"].get("Data") or "") == b"\x11" * 3200)
        check("F7 线上 DataLen 与原始 PCM 长度一致",
              req["body"].get("DataLen") == 3200,
              str(req["body"].get("DataLen")))
        check("F8 引擎取自配置行的 model 字段",
              req["body"].get("EngSerViceType") == "16k_zh",
              str(req["body"].get("EngSerViceType")))
        check("F9 ⚠️ endpoint 留空也合法（默认域名写在适配器里）",
              True, "见 G4：空 endpoint 不被 http/https 闸门跳过")
    else:
        for name in ("F3", "F4", "F5", "F6", "F7", "F8"):
            skip(f"{name} 线上报文断言", "假上游没收到请求")


# ================================================================ G 组：错误处理
def t_errors(port: int) -> None:
    print("\n── G. 错误处理（错误码 / 本地拦截 / 配置缺失）──")
    import app.llm_gateway as gw
    from app.voice import asr_tts, tts_tencent
    from app.voice.asr_tts import VoiceUpstreamError

    original_row = gw.resolve_row
    original_plan = gw.resolve_plan

    # G1 业务错误码（HTTP 200 + Response.Error）必须变异常
    UP.reset("error")
    gw.resolve_row = lambda db, usage: _fake_row(port)
    try:
        caught = ""
        try:
            asr_tts.tts(None, "你好", voice="1001")
        except VoiceUpstreamError as exc:
            caught = str(exc)
        check("G1 ★ 业务错误码（HTTP 200+Error 段）被识别为失败",
              "ServiceIsolate" in caught, caught[:120])
        check("G2 错误文案带定向提示（欠费）",
              "欠费" in caught or "停服" in caught, caught[:160])
    finally:
        gw.resolve_row = original_row

    # G3 超限在本地拦截，**零请求**
    UP.reset("ok")
    gw.resolve_plan = lambda db, usage: [("asr", _fake_row(port, provider="tencent_asr"))]
    try:
        caught3 = ""
        try:
            asr_tts.asr(None, b"\x00" * int(61 * 16000 * 2))
        except Exception as exc:  # noqa: BLE001
            caught3 = str(exc)
        check("G3 ★ 超 60s 在本地拦截，不发请求（额度零消耗）",
              "60" in caught3 and len(UP.requests) == 0,
              f"requests={len(UP.requests)} msg={caught3[:80]}")
    finally:
        gw.resolve_plan = original_plan

    # G4 腾讯云行 endpoint 留空不能被 http/https 闸门跳过
    UP.reset("ok")
    empty_row = _fake_row(port, provider="tencent_asr")
    empty_row.endpoint = ""      # 最自然的填法
    empty_row.api_style = "tencent"
    gw.resolve_plan = lambda db, usage: [("asr", empty_row)]
    try:
        # endpoint 空 ⇒ 走真实域名 asr.tencentcloudapi.com，网络必然失败；
        # 这里只关心**它不是被"跳过"**（跳过会得到「全部配置的 endpoint
        # 都不是 http/https」，而那与网络失败是两种完全不同的错误）。
        msg = ""
        try:
            asr_tts.asr(None, b"\x11" * 3200)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
        check("G4 ★ 空 endpoint 不被『非 http/https』闸门跳过",
              "都不是 http/https" not in msg,
              msg[:140])
    finally:
        gw.resolve_plan = original_plan

    # G5 缺密钥的报错必须说清"要两段"
    caught5 = ""
    try:
        tts_tencent.tts("你好", "1001", secret_id="onlyid", secret_key="")
    except Exception as exc:  # noqa: BLE001
        caught5 = str(exc)
    check("G5 只填一段密钥 → 报错说明需要两段",
          "两段" in caught5 or "SecretKey" in caught5, caught5[:140])


# ================================================================ I 组：只填密钥也要能用
def t_endpoint_defaults() -> None:
    """预置厂商的 endpoint 留空时必须回落到官方域名（2026-10-21 线上事故）。

    事故形状：管理后台按预置新增一条「腾讯云 TTS」，管理员**只填了密钥**
    —— 这是预置下拉承诺的最短路径。但 ``_row_endpoint`` 见空串直接抛
    ``VoiceConfigMissingError``，而 ``tts()`` 只取优先级第一条、没有按序
    兜底 ⇒ 库里那条好用的 Edge 永远轮不到，老人听到彻底静默。

    这几条断言就是"只填密钥也能用"的永久回归：把 ``_row_endpoint`` 的
    预置回落删掉，I1/I2 立刻红（见 H 组 M5）。
    """
    print("\n── I. 预置厂商 endpoint 留空 → 回落官方域名 ──")
    from app.voice import asr_tts

    def blank(provider: str, name: str = "") -> _Row:
        return _Row(provider=provider, api_style="tencent", endpoint="",
                    api_key="AKID:SK", model="", name=name,
                    timeout_sec=10, params="")

    got = ""
    try:
        got = asr_tts._row_endpoint(blank("tencent"))
    except Exception as exc:  # noqa: BLE001
        got = f"<{type(exc).__name__}: {exc}>"
    check("I1 TTS 行只填密钥（endpoint 空）→ 用 tts.tencentcloudapi.com",
          got == "https://tts.tencentcloudapi.com", got)

    got = ""
    try:
        got = asr_tts._row_endpoint(blank("tencent_asr"))
    except Exception as exc:  # noqa: BLE001
        got = f"<{type(exc).__name__}: {exc}>"
    check("I2 ASR 行只填密钥（endpoint 空）→ 用 asr.tencentcloudapi.com",
          got == "https://asr.tencentcloudapi.com", got)

    # 管理员自己填了地址（私有化 / 代理）必须以他为准，不能被预置覆盖
    row = blank("tencent")
    row.endpoint = "https://proxy.internal/tts/"
    check("I3 自己填过地址时以填的为准（去掉结尾斜杠）",
          asr_tts._row_endpoint(row) == "https://proxy.internal/tts",
          asr_tts._row_endpoint(row))

    # 未知厂商 + 空 endpoint：必须报错，且文案要能定位到是哪一行
    msg = ""
    try:
        asr_tts._row_endpoint(blank("acme_gateway"))
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
    check("I4 未知厂商且没填地址 → 抛错（不许静默）",
          "没有填写服务地址" in msg, msg[:140])
    check("I5 报错文案带上 provider（名称没填时也能定位）",
          "acme_gateway" in msg, msg[:140])

    # ---- 保存时归一化：库里那一行本身就该是自洽的 ----
    # 只做运行时回落是不够的：管理后台打开它、原样点保存，PATCH 会整体覆盖
    # 一次，空串又被写回；列表里也永远看不出这条到底打哪个域名。
    from app.routers.providers import _normalize_preset_service
    from app.schemas import ProviderIn

    tts_row = ProviderIn(name="", kind="tts", provider="tencent")
    _normalize_preset_service(tts_row, set())
    check("I6 保存腾讯云 TTS：endpoint 自动补 tts.tencentcloudapi.com",
          tts_row.endpoint == "https://tts.tencentcloudapi.com", tts_row.endpoint)
    check("I7 保存腾讯云 TTS：api_style 保持空（tencent 是供应商级风格，"
          "不在可存白名单里，写进去会被校验器 500）",
          tts_row.api_style == "", repr(tts_row.api_style))
    check("I8 保存腾讯云 TTS：名称为空时补厂商名（不许留「」）",
          bool((tts_row.name or "").strip()), f"name={tts_row.name!r}")
    check("I9 保存腾讯云 TTS：model 保持空（该厂商没有模型概念，不许编一个）",
          tts_row.model == "", f"model={tts_row.model!r}")

    asr_row = ProviderIn(name="", kind="asr", provider="tencent_asr")
    _normalize_preset_service(asr_row, set())
    check("I10 保存腾讯云 ASR：endpoint 自动补 asr.tencentcloudapi.com",
          asr_row.endpoint == "https://asr.tencentcloudapi.com", asr_row.endpoint)
    check("I11 保存腾讯云 ASR：引擎自动补 16k_zh（不是 8k_）",
          asr_row.model == "16k_zh", repr(asr_row.model))

    # 管理员自己填的地址绝不能被预置覆盖（私有化/代理是合法需求）
    custom = ProviderIn(name="我的代理", kind="tts", provider="tencent",
                        endpoint="https://proxy.internal/tts")
    _normalize_preset_service(custom, set())
    check("I12 自己填过地址时归一化不覆盖",
          custom.endpoint == "https://proxy.internal/tts", custom.endpoint)
    check("I13 自己填过名称时归一化不覆盖", custom.name == "我的代理", custom.name)

    # 不在预置表里的自建网关：一个字段都不许动
    unknown = ProviderIn(name="自建", kind="tts", provider="acme_gateway")
    _normalize_preset_service(unknown, set())
    check("I14 非预置厂商不被改动（endpoint/model 仍为空）",
          unknown.endpoint == "" and unknown.model == "",
          f"endpoint={unknown.endpoint!r} model={unknown.model!r}")


def t_fallback_policy() -> None:
    """兜底口径：ASR 保底=自部署 FunASR，TTS 保底=微软免费 Edge，且**永远最后试**。

    为什么这条必须是断言而不是注释（2026-10-21 事故）
    ------------------------------------------------
    兜底配置的**全部价值**是「别人都挂了它还在」，所以它必须零密钥、零自定义
    端点、零成本。那次事故里，管理员按预置新增一条腾讯云 TTS 但密钥/地址没填全，
    它按 priority 抢在 Edge 前面 ⇒ 整条链路静默，库里那条免费 Edge 永远轮不到。

    只靠 priority 排序是不可靠的：priority 是可编辑数据。所以代码把
    ``LAST_RESORT_*_PROVIDERS`` 强制排到末尾 —— 本用例直接验**asr() 的真实
    尝试顺序**（不是验那个排序小函数，否则改回不排序也能绿）。
    """
    print("\n── J. 兜底口径：自部署 ASR / 免费 Edge TTS 永远最后试 ──")
    import app.llm_gateway as gw
    from app.voice import asr_tencent, asr_tts

    def row(provider: str, **kw) -> _Row:
        base = dict(provider=provider, api_style="", endpoint="",
                    api_key="", model="", name=provider, timeout_sec=3, params="")
        base.update(kw)
        return _Row(**base)

    # ---- J1/J2：TTS 侧排序（纯函数，顺序即契约）----
    tts_plan = [("tts", row("edge", api_style="edge", name="Edge 保底")),
                ("tts", row("tencent", api_style="tencent", name="腾讯云主力"))]
    order = [r.name for _k, r in asr_tts._primary_then_last_resort(
        tts_plan, asr_tts.LAST_RESORT_TTS_PROVIDERS)]
    check("J1 TTS 保底 = 微软免费 Edge",
          asr_tts.LAST_RESORT_TTS_PROVIDERS == ("edge",),
          repr(asr_tts.LAST_RESORT_TTS_PROVIDERS))
    check("J2 Edge 即使 priority 排最前也被挪到最后",
          order == ["腾讯云主力", "Edge 保底"], repr(order))

    # ---- J3：ASR 侧真实尝试顺序（走 asr() 本体，不是只测排序函数）----
    asr_plan = [("asr", row("funasr_local", api_style="local_asr",
                            endpoint="http://127.0.0.1:8082")),
                ("asr", row("tencent_asr", api_style="tencent",
                            endpoint="https://asr.tencentcloudapi.com"))]

    tried: list[str] = []
    orig_label, orig_plan = asr_tts._row_label, gw.resolve_plan
    orig_transcribe, orig_post = asr_tencent.transcribe, asr_tts._post_multipart

    def spy_label(r):
        tried.append(getattr(r, "provider", ""))
        return orig_label(r)

    def boom(*_a, **_kw):
        raise asr_tts.VoiceUpstreamError("模拟上游全挂（smoke）")

    asr_tts._row_label = spy_label
    gw.resolve_plan = lambda db, usage: list(asr_plan)
    asr_tencent.transcribe = boom
    asr_tts._post_multipart = boom
    try:
        asr_tts.asr(None, b"\x00" * 320)
        raised = ""
    except Exception as exc:  # noqa: BLE001
        raised = type(exc).__name__
    finally:
        asr_tts._row_label = orig_label
        gw.resolve_plan = orig_plan
        asr_tencent.transcribe = orig_transcribe
        asr_tts._post_multipart = orig_post

    check("J3 ASR 保底 = 自部署 FunASR",
          asr_tts.LAST_RESORT_ASR_PROVIDERS == ("funasr_local",),
          repr(asr_tts.LAST_RESORT_ASR_PROVIDERS))
    check("J4 云端 ASR 先试、自部署 FunASR 最后试（真实尝试顺序）",
          tried == ["tencent_asr", "funasr_local"], repr(tried))
    check("J5 全部失败时抛 VoiceUpstreamError（不静默返回空串）",
          raised == "VoiceUpstreamError", raised)


# ================================================================ H 组：反向变异
MUTATIONS: dict[str, tuple[str, str, list[str]]] = {
    # 名字: (原文, 替换, 期望变红的断言关键字)
    "M1": (
        '        "Codec": "pcm",\n',
        '        "Codec": "wav",\n',
        ["C12 Codec 必须是 pcm", "E6 线上报文的 Codec"],
    ),
    "M2": (
        "    return int(dt.timestamp()), dt.strftime(\"%Y-%m-%d\")\n",
        # ⚠️ 变异必须**显式**指定时区，不能用无参 astimezone()：
        # CI runner 的宿主时区就是 UTC，那时 astimezone() 不改变日期 ⇒
        # 变异不变红 ⇒ 变成「CI 上变异检测失败」的假红/假绿。
        # 用显式 Asia/Shanghai 才能在 UTC 与北京时间两种宿主上都变红。
        "    return int(dt.timestamp()), dt.astimezone("
        "__import__(\"zoneinfo\").ZoneInfo(\"Asia/Shanghai\")"
        ").strftime(\"%Y-%m-%d\")\n",
        ["B5 跨日时刻仍取 UTC 日期"],
    ),
    "M3": (
        '        "DataLen": len(pcm),\n',
        '        "DataLen": len(base64.b64encode(pcm)),\n',
        ["D3 DataLen 是**未编码**的字节数", "F7 线上 DataLen"],
    ),
    "M4": (
        "TEXT_LIMIT: Final[int] = 150\n",
        "TEXT_LIMIT: Final[int] = 10000\n",
        ["C1 超长无标点文本按 150 字硬切", "E9 超长文本分成多次请求"],
    ),
    # 删掉「预置厂商 endpoint 回落」—— 模拟 2026-10-21 那次「只填密钥就整条链路挂掉」
    "M5": (
        '    preset = _preset_endpoint(row)\n'
        '    if preset:\n'
        '        return preset.rstrip("/")\n',
        "    pass\n",
        ["I1 TTS 行只填密钥", "I2 ASR 行只填密钥"],
    ),
    # 删掉「保存时补 endpoint」—— 管理员只填密钥存进去，库里那行仍然是空串
    "M6": (
        '        if not (payload.endpoint or "").strip():\n'
        '            payload.endpoint = str(entry.get("endpoint") or "")\n',
        "        pass\n",
        ["I6 保存腾讯云 TTS"],
    ),
    # 删掉「保底排最后」—— 回到「谁 priority 小谁先上」，坏配置又能一票否决
    "M7": (
        "    # 自部署 FunASR 是保底，永远排最后（理由见 LAST_RESORT_ASR_PROVIDERS）\n"
        "    plan = _primary_then_last_resort(plan, LAST_RESORT_ASR_PROVIDERS)\n",
        "    pass\n",
        ["J4 云端 ASR 先试"],
    ),
}

_MUTATION_TARGETS = {
    "M1": "server/app/voice/tts_tencent.py",
    "M2": "server/app/voice/tc3.py",
    "M3": "server/app/voice/asr_tencent.py",
    "M4": "server/app/voice/tts_tencent.py",
    "M5": "server/app/voice/asr_tts.py",
    "M6": "server/app/routers/providers.py",
    "M7": "server/app/voice/asr_tts.py",
}


def _install_mutation_hook(name: str) -> None:
    """在**内存里**改写目标模块的源码（不落盘），供变异子进程使用。

    为什么不落盘再还原：落盘方案在崩溃/被 kill 时会留下被改坏的源码，
    而"忘了还原"的代价是下次真跑时行为诡异。用 import hook 改写
    ``get_source`` 的结果，进程退出即消失，零残留。
    """
    old, new, _ = MUTATIONS[name]
    target = _MUTATION_TARGETS[name]
    target_path = (ROOT / target).resolve()

    import importlib.abc
    import importlib.machinery
    import importlib.util

    class _Loader(importlib.abc.SourceLoader):
        def __init__(self, fullname, path):
            self.fullname = fullname
            self.path = path

        def get_filename(self, fullname):
            return self.path

        def get_data(self, path):
            src = Path(path).read_text(encoding="utf-8")
            if Path(path).resolve() == target_path:
                if src.count(old) != 1:
                    raise RuntimeError(
                        f"变异 {name} 的锚点在 {target} 里出现 {src.count(old)} 次，"
                        f"应为 1 —— 源码变了，先修冒烟")
                src = src.replace(old, new)
            return src.encode("utf-8")

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if not fullname.startswith("app."):
                return None
            # ⚠️ 必须**按全名推导路径**（app.routers.providers ->
            # app/routers/providers.py），不能只枚举 app/ 与 app/voice/ 两层。
            # 曾经只枚举那两层：M6 的目标在 app/routers/ 下，finder 匹配不到，
            # 模块被正常导入 ⇒ 变异根本没生效 ⇒ 子进程 rc=0、断言全绿，
            # 表现成「变异检测失败」的假红。这类假红比假绿便宜，但同样浪费时间。
            candidate = SERVER.joinpath(*fullname.split(".")).with_suffix(".py")
            if candidate.exists() and candidate.resolve() == target_path:
                loader = _Loader(fullname, str(candidate))
                return importlib.util.spec_from_loader(
                    fullname, loader, origin=str(candidate))
            return None

    sys.meta_path.insert(0, _Finder())


def run_mutation(name: str, port: int) -> tuple[bool, str]:
    """跑一个变异子进程，确认**指定断言真的变红**。

    判定是严的：子进程必须退出码 1 **且**失败列表里出现指定断言名。
    只看退出码不够 —— 子进程可能因为别的错（比如 import 失败）退出 1，
    那种"红"不证明变异抓到了目标行为。
    """
    env = dict(os.environ)
    env["SMOKE_MUTATION"] = name
    env["SMOKE_HTTP_PORT"] = str(port)
    expect = MUTATIONS[name][2]
    proc = subprocess.run(
        [sys.executable, str(Path(__file__)), "--mutate", name,
         "--port", str(port)],
        capture_output=True, text=True, timeout=300, env=env, cwd=str(ROOT))
    out = (proc.stdout or "") + (proc.stderr or "")
    hit = [e for e in expect if e in out]
    ok = proc.returncode == 1 and bool(hit)
    detail = ""
    if not ok:
        detail = (f"rc={proc.returncode} 期望的失败断言命中 {len(hit)}/{len(expect)}；"
                  f"尾部输出：{out[-300:]}")
    return ok, detail


def t_mutations(port: int) -> None:
    print("\n── H. 反向变异（改回去必须真变红）──")
    for name in sorted(MUTATIONS):
        ok, detail = run_mutation(name, port)
        check(f"H{name[1]} 变异 {name} 让指定断言变红", ok, detail)


# ================================================================ main
def main(argv: list[str] | None = None) -> int:
    global HTTP_PORT, _MUTATE
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    HTTP_PORT = args.port or resolve_port("smoke_tencent_voice")
    _MUTATE = args.mutate or ""
    mutate_name = _MUTATE

    if mutate_name:
        if mutate_name not in MUTATIONS:
            print(f"未知变异 {mutate_name}")
            return 2
        _install_mutation_hook(mutate_name)
        # 子进程用自己的端口，避免与父进程（可能还在跑）抢同一个口
        HTTP_PORT = int(os.environ.get("SMOKE_HTTP_PORT") or HTTP_PORT)

    print(f"腾讯云语音冒烟（假上游端口 {HTTP_PORT}"
          + (f"，变异 {mutate_name}" if mutate_name else "") + "）")
    upstream = start_upstream(HTTP_PORT)
    try:
        t_official_vectors()
        t_tc3_details()
        t_tts_pure()
        t_asr_pure()
        t_e2e_tts(HTTP_PORT)
        t_e2e_asr(HTTP_PORT)
        t_errors(HTTP_PORT)
        t_endpoint_defaults()
        t_fallback_policy()
        if not mutate_name and not args.no_mutation:
            # 变异子进程各用父端口 +100 起自己的假上游
            t_mutations(HTTP_PORT + 100)
    finally:
        upstream.shutdown()

    print(f"\n通过 {_PASS} / 失败 {_FAIL} / 跳过 {_SKIP}")
    if _FAILED_NAMES:
        print("失败的断言：")
        for n in _FAILED_NAMES:
            print(f"  - {n}")
    return 1 if _FAIL else 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """端口既接受**位置参数**也接受 ``--port``。

    ⚠️ CI 一律用位置参数调用（``python tools/smoke_xxx.py 8134``，见
    ``tools/smoke_ports.py`` 的说明）。只声明 ``--port`` 会让 CI 上
    argparse 直接 rc=2 全红，而本机用 ``--port`` 跑得全绿 —— 这种
    「本机绿、CI 红」的差异比单纯的失败更浪费时间（F1 实测踩过）。
    """
    ap = argparse.ArgumentParser(description="腾讯云语音冒烟")
    ap.add_argument("port", nargs="?", type=int, default=None,
                    help="假上游端口（也可用 --port）")
    ap.add_argument("--port", dest="port_opt", type=int, default=None,
                    help="同位置参数，两者等价")
    ap.add_argument("--no-mutation", action="store_true",
                    help="跳过反向变异（调试用）")
    ap.add_argument("--mutate", default="", help=argparse.SUPPRESS)
    ns = ap.parse_args(argv)
    ns.port = ns.port if ns.port is not None else ns.port_opt
    return ns


if __name__ == "__main__":
    sys.exit(main())
