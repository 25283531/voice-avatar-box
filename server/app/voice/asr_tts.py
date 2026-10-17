"""ASR / TTS 客户端：**只实现 OpenAI 兼容风格**（V1 边界）。

为什么只做一种风格
----------------
本项目把第三方服务全部收敛到自己的镜像里（见 config.py 的「历史沿革」注释），
云端 ASR/TTS 厂商的私有报文形状一个都不接。OpenAI 兼容是事实标准：
vLLM / Xinference / Ollama(兼容层) / one-api / new-api / 各种国产网关都能
提供 ``/audio/transcriptions`` 与 ``/audio/speech``。要接新厂商时优先
在**网关层**做风格适配，而不是在这里加分支 —— 那会把 llm_gateway
已经踩过的坑（四种报文形状的翻译）再踩一遍。

⚠️ V1 已知边界：**流式 TTS 未实现**（一次性拿完整段音频再切帧发）。
实时逐句 TTS 是明显的下一轮增量；当前实现首句延迟 = 完整推理时间，
实测可接受（见 smoke_voice 的 timing 输出），但不是最优。

HTTP 约定为什么必须照抄 llm_gateway
---------------------------------
本机部署 Ollama / vLLM 在同一台机器上是常态，而这些环境往往全局设了
HTTP_PROXY。urllib 默认把**所有**请求（含 127.0.0.1）丢给代理，
表现为莫名其妙的 502/超时。`_bypass_proxy` / `_open` 与 llm_gateway
逐行同源（含 RFC1918 私网放行），**不要在这里另写一套** —— 两套一旦
漂移，就会出现"LLM 调得通、TTS 调不通"这种极难归因的故障。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
import struct
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Final

from .. import config
from ..model_catalog import usage_kinds

log = logging.getLogger(__name__)

# 默认超时。语音链路是**实时交互**，等 60 秒比失败更糟（老人已经走了），
# 所以刻意比 LLM 的默认值短。
ASR_TIMEOUT_SEC: Final[int] = 30
TTS_TIMEOUT_SEC: Final[int] = 30

# TTS 请求的音频格式。要裸 PCM 而不是 wav/mp3：
# 固件按"服务端声明的 24kHz Opus 包"逐帧解码，中间任何一层容器格式
# 都要先剥壳再重采样，而剥壳逻辑我们不想在设备上实现（audio_service
# 只认 Opus）。
TTS_RESPONSE_FORMAT: Final[str] = "pcm"
# TTS 返回 24kHz 单声道 S16LE（裸 PCM）。与 config.VOICE_OUTPUT_SAMPLE_RATE
# 一致 —— 不一致时 encode_opus 会按错误的采样率编码，老人听到的是
# 音调不对但"确实有声音"，很难排查。
TTS_SAMPLE_RATE: Final[int] = 24000

# OpenAI 兼容的 TTS 端点返回的 WAV 头长度（44 字节标准头）。
# 防御性处理：部分厂商无视 response_format=pcm 仍返回 wav。
_WAV_HEADER_LEN: Final[int] = 44

_LOCAL_NETS: Final[tuple[Any, ...]] = (
    ipaddress.ip_network("127.0.0.0/8"),      # 本机 loopback
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),       # 家庭/内网部署的另一台推理机
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


class VoiceUpstreamError(RuntimeError):
    """上游不可用/返回异常。文案给运维看，要能定位到是哪个配置。"""


class VoiceConfigMissingError(RuntimeError):
    """某能力（ASR/TTS）一条可用配置都没有。"""

    def __init__(self, usage: str) -> None:
        kinds = " 或 ".join(usage_kinds(usage)) or usage
        super().__init__(f"未配置可用的{usage}能力（需要 {kinds} 配置行）")


def _bypass_proxy(url: str) -> bool:
    """判断该地址是否应该绕过系统代理。与 llm_gateway._bypass_proxy 同源。"""
    host = (urllib.parse.urlsplit(url).hostname or "").strip("[]")
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False      # 域名保持走代理（云厂商 API 可能确实需要它）
    return any(ip in net for net in _LOCAL_NETS)


def _open(url: str, req: urllib.request.Request, timeout: float):
    """按是否需要代理打开连接。与 llm_gateway._open 同源。"""
    if _bypass_proxy(url):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = urllib.request.build_opener()
    return opener.open(req, timeout=timeout)


def _wrap_urlerror(exc: urllib.error.URLError, endpoint: str, timeout: float) -> VoiceUpstreamError:
    """URLError 里塞了所有传输期异常，按 reason 精确分诊（照抄网关的措辞）。"""
    reason = getattr(exc, "reason", None)
    msg = str(reason or exc)
    low = msg.lower()

    if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in low:
        return VoiceUpstreamError(
            f"调用超时（{timeout:g}s 未返回）：{endpoint}。可把该配置的「超时秒数」调大")
    if isinstance(reason, ConnectionRefusedError) or "refused" in low:
        return VoiceUpstreamError(
            f"服务地址不可达（连接被拒绝）：{endpoint}，请检查 endpoint 与服务是否存活")
    if "name or service not known" in low or "getaddrinfo" in low:
        return VoiceUpstreamError(f"服务地址无法解析（DNS 失败）：{endpoint}")
    return VoiceUpstreamError(f"网络异常：{msg}")


def _http_error(status: int, detail: str, endpoint: str) -> VoiceUpstreamError:
    brief = (detail or "").strip().splitlines()[0][:200] if detail else ""
    short = f"（HTTP {status}）" + (f"：{brief}" if brief else "，服务未给出原因")
    if status in (401, 403):
        return VoiceUpstreamError(
            f"密钥无效或无访问权{short}；也可能是账号余额/额度已用尽：{endpoint}")
    if status == 429:
        return VoiceUpstreamError(f"触发上游限流{short}：{endpoint}")
    if status >= 500:
        return VoiceUpstreamError(f"服务侧异常{short}：{endpoint}")
    return VoiceUpstreamError(f"请求被上游拒绝{short}；常见原因是 endpoint/模型名填错：{endpoint}")


def _row_endpoint(row: Any) -> str:
    endpoint = (getattr(row, "endpoint", "") or "").strip().rstrip("/")
    if not endpoint:
        raise VoiceConfigMissingError(
            f"配置「{getattr(row, 'name', '')}」没有填写服务地址（endpoint）")
    return endpoint


def _row_key(row: Any) -> str:
    return (getattr(row, "api_key", "") or "").strip()


def _row_timeout(row: Any, default: float) -> float:
    try:
        return float(getattr(row, "timeout_sec", None) or default)
    except (TypeError, ValueError):
        return default


def _row_params(row: Any) -> dict[str, Any]:
    p = getattr(row, "params", None)
    return p if isinstance(p, dict) else {}


def _load_json(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001  厂商可能用 HTML 报错页冒充响应
        raise VoiceUpstreamError(
            f"返回不是合法 JSON（多半是 endpoint 填错到了网页）：{raw[:200]}") from None
    if not isinstance(obj, dict):
        raise VoiceUpstreamError(f"返回体不是对象：{raw[:200]}")
    return obj


def _post_multipart(row: Any, path: str, fields: dict[str, str],
                    file_field: str, filename: str, blob: bytes,
                    timeout: float) -> dict[str, Any]:
    """发 multipart/form-data（ASR 的 file= 必须用这个）。

    为什么不用 aiohttp/httpx：项目全程 urllib，多一个异步 HTTP 客户端
    就多一份依赖与一份超时/代理语义。multipart 手写边界即可，格式固定。
    """
    endpoint = _row_endpoint(row)
    url = endpoint + path
    key = _row_key(row)
    boundary = "----eldervoice7d91f4c2a6b34c5e"
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    lines: list[bytes] = []
    for name, value in fields.items():
        lines.append(f'--{boundary}\r\nContent-Disposition: form-data; '
                     f'name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8"))
    lines.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        .encode("utf-8"))
    lines.append(blob)
    lines.append(b"\r\n")
    lines.append(f"--{boundary}--\r\n".encode("utf-8"))
    payload = b"".join(lines)

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with _open(url, req, timeout) as resp:
            return _load_json(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise _http_error(exc.code, detail, endpoint) from None
    except urllib.error.URLError as exc:
        raise _wrap_urlerror(exc, endpoint, timeout) from None
    except (socket.timeout, TimeoutError):
        raise VoiceUpstreamError(
            f"调用超时（{timeout:g}s 未返回）：{endpoint}，可调大该配置的「超时秒数」") from None


# ---------------------------------------------------------------- ASR
def asr(db: Any, pcm_16k: bytes) -> str:
    """16kHz/mono/S16LE PCM -> 识别文本。

    :param db: SQLAlchemy Session（取 asr 配置行）。
    :raises VoiceConfigMissingError: 没有可用 ASR 配置。
    :raises VoiceUpstreamError: 上游不可用/返回异常。

    为什么 wav 头在这里手写而不是调 wave 模块：标准库的 wave 会写出
    带 LIST/INFO 块的变体，且接口是文件而非字节；这里是 44 字节固定头，
    写死更可预测（固件/厂商都能解）。
    """
    from ..llm_gateway import resolve_row

    row = resolve_row(db, "asr")
    if row is None:
        raise VoiceConfigMissingError("asr")
    timeout = _row_timeout(row, ASR_TIMEOUT_SEC)
    model = (getattr(row, "model", "") or "").strip() or "whisper-1"

    wav = wav_header_bytes(len(pcm_16k), 16000) + pcm_16k

    fields = {
        "model": model,
        "language": config.VOICE_ASR_LANGUAGE,
        "response_format": "json",
    }
    payload = _post_multipart(row, "/audio/transcriptions", fields,
                              "file", "utterance.wav", wav, timeout)
    text = str(payload.get("text", "") or "").strip()
    return text


# ---------------------------------------------------------------- TTS
def tts(db: Any, sentence: str, *, voice: str = "", speed: float = 1.0) -> bytes:
    """一句话 -> 24kHz/mono/S16LE 裸 PCM。

    :param voice: 音色。空串表示**不带 voice 字段**，让厂商用默认
        （见 _pick_voice 的回落链）。
    :raises VoiceConfigMissingError / VoiceUpstreamError
    """
    from ..llm_gateway import resolve_row

    row = resolve_row(db, "tts")
    if row is None:
        raise VoiceConfigMissingError("tts")
    timeout = _row_timeout(row, TTS_TIMEOUT_SEC)
    model = (getattr(row, "model", "") or "").strip() or "tts-1"

    body: dict[str, Any] = {
        "model": model,
        "input": sentence,
        "response_format": TTS_RESPONSE_FORMAT,
    }
    if voice:
        body["voice"] = voice
    if speed and speed != 1.0:
        body["speed"] = speed

    endpoint = _row_endpoint(row)
    url = endpoint + "/audio/speech"
    key = _row_key(row)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    try:
        with _open(url, req, timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise _http_error(exc.code, detail, endpoint) from None
    except urllib.error.URLError as exc:
        raise _wrap_urlerror(exc, endpoint, timeout) from None
    except (socket.timeout, TimeoutError):
        raise VoiceUpstreamError(
            f"调用超时（{timeout:g}s 未返回）：{endpoint}，可调大该配置的「超时秒数」") from None

    if not raw:
        raise VoiceUpstreamError(f"TTS 返回空音频体：{endpoint}")

    # 防御 1：厂商无视 response_format=pcm 仍回 wav。剥 44 字节头即可，
    # 但必须先确认那确实是 RIFF 头 —— 否则会把合法音频的头 4 字节误判。
    if raw[:4] == b"RIFF":
        log.info("TTS 返回了 wav 容器（无视了 response_format=pcm），剥 44 字节头")
        raw = raw[_WAV_HEADER_LEN:]

    # 防御 2：错误体伪装成音频。厂商偶发把 JSON 错误塞进 200 响应里
    # （部分网关的错误处理就是这样），不查会把 `{"error":...}` 当音频发出去，
    # 老人听到的是一次爆音。
    if raw[:1] in (b"{", b"["):
        raise VoiceUpstreamError(
            f"TTS 返回体像 JSON 错误而非音频：{raw[:200]!r}：{endpoint}")

    if len(raw) % 2:
        raw = raw[:-1]        # 丢掉半个采样，保证 S16LE 对齐
    return raw


def tts_stream(db: Any, sentence: str, *, voice: str = "",
               speed: float = 1.0):
    """一句话 -> **增量**吐出 24kHz/mono/S16LE 裸 PCM（阻塞生成器）。

    与 :func:`tts` 的差异只有"读取方式"：本函数不等整个响应体到齐，
    而是 ``resp.read(amt)`` 拿到多少吐多少，让上层（会话编排）边收边切
    60ms 帧下发 —— 首句延迟从"整段合成时间"降到"第一个分片时间"。

    **流式与否由厂商决定，我们不假装**：
      * 厂商用 chunked transfer 分片发送（真流式 TTS）→ 每个分片到达就 yield；
      * 厂商一次性发完整段（Content-Length，多数 OpenAI 兼容网关的默认行为）
        → 生成器只 yield 一次（整段），行为与 :func:`tts` 等价，**不报错也不
        假报流式**。上层按"第一帧早于整段合成完成"的实测值判断真实收益。

    防御与非流式版逐字一致：RIFF 头剥 44 字节（可跨分片）、JSON 错误体
    识别（读到就抛，不把错误文本当音频发出去）、奇数长度按采样对齐
    （分片边界可能劈开一个采样，用 1 字节进位缓冲兜）。
    """
    from ..llm_gateway import resolve_row

    row = resolve_row(db, "tts")
    if row is None:
        raise VoiceConfigMissingError("tts")
    timeout = _row_timeout(row, TTS_TIMEOUT_SEC)
    model = (getattr(row, "model", "") or "").strip() or "tts-1"

    body: dict[str, Any] = {
        "model": model,
        "input": sentence,
        "response_format": TTS_RESPONSE_FORMAT,
    }
    if voice:
        body["voice"] = voice
    if speed and speed != 1.0:
        body["speed"] = speed

    endpoint = _row_endpoint(row)
    url = endpoint + "/audio/speech"
    key = _row_key(row)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    try:
        resp = _open(url, req, timeout)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise _http_error(exc.code, detail, endpoint) from None
    except urllib.error.URLError as exc:
        raise _wrap_urlerror(exc, endpoint, timeout) from None
    except (socket.timeout, TimeoutError):
        raise VoiceUpstreamError(
            f"调用超时（{timeout:g}s 未返回）：{endpoint}，可调大该配置的「超时秒数」") from None

    try:
        head = b""          # 尚未判别的头几个字节（RIFF / JSON / PCM）
        carry = b""         # 奇数采样进位
        seen_any = False
        while True:
            try:
                chunk = resp.read(4096)
            except (socket.timeout, TimeoutError):
                raise VoiceUpstreamError(
                    f"读取音频流超时：{endpoint}，可调大该配置的「超时秒数」") from None
            if not chunk:
                break
            if not seen_any and not head:
                head = chunk[:8]
                # 防御：错误体伪装成音频（与非流式版同一条判据）
                if head[:1] in (b"{", b"["):
                    rest = chunk + resp.read()
                    raise VoiceUpstreamError(
                        f"TTS 返回体像 JSON 错误而非音频：{(chunk + rest)[:200]!r}：{endpoint}")
            if not seen_any:
                seen_any = True
                if head[:4] == b"RIFF":
                    # 剥 44 字节 WAV 头；头可能跨分片，先把前 44 字节攒齐
                    need = _WAV_HEADER_LEN
                    buf = chunk
                    while len(buf) < need:
                        more = resp.read(need - len(buf))
                        if not more:
                            break
                        buf += more
                    chunk = buf[need:]
                    if not chunk:
                        continue
            if carry:
                chunk = carry + chunk
                carry = b""
            if len(chunk) % 2:
                carry = chunk[-1:]
                chunk = chunk[:-1]
            if chunk:
                yield chunk
        if carry:
            # 末尾剩半个采样：补零对齐（S16LE 必须是整采样）
            yield carry + b"\x00"
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def pick_voice(db: Any, device: Any) -> str:
    """音色回落链：**角色优先，形象兜底**（与 device_api.sync 口径一致）。

    dev.agent_id → 角色 tts_voice → dev.avatar_id → 形象 tts_voice
    → TTS 配置行 params.voice → 都不填则返回空串（请求里不带 voice
    字段，让厂商用自己的默认）。

    为什么和设备端 sync 同一条链：两边算出不同音色时，老人会在
    "心跳里报的音色" 与 "实际听到的声音" 之间发现不一致，而那是最
    难归因的一类问题（听起来像设备缓存了旧配置）。
    """
    from ..llm_gateway import resolve_row

    from .. import models as _m

    def _val(obj: Any, attr: str) -> str:
        v = getattr(obj, attr, None)
        return (v or "").strip() if isinstance(v, str) else ""

    # 1) 角色
    agent_id = getattr(device, "agent_id", None)
    if agent_id:
        role = db.get(_m.AIProvider, int(agent_id))
        if role is not None:
            v = _val(role, "tts_voice")
            if v:
                return v
    # 2) 形象
    avatar_id = getattr(device, "avatar_id", None)
    if avatar_id:
        avatar = db.get(_m.Avatar, int(avatar_id))
        if avatar is not None:
            v = _val(avatar, "tts_voice")
            if v:
                return v
    # 3) TTS 配置行 params.voice
    row = resolve_row(db, "tts")
    if row is not None:
        v = str(_row_params(row).get("voice", "") or "").strip()
        if v:
            return v
    return ""


def wav_header_bytes(pcm_len: int, sample_rate: int = 16000) -> bytes:
    """导出手写 RIFF 头（给 asr() 用，单独导出便于冒烟断言帧长口径）。"""
    byte_rate = sample_rate * 2
    return b"RIFF" + struct.pack("<I", 36 + pcm_len) + b"WAVEfmt " \
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16) \
        + b"data" + struct.pack("<I", pcm_len)
