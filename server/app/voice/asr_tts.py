"""ASR / TTS 客户端：默认只实现 OpenAI 兼容风格（V1 边界）。

为什么默认只做一种风格
----------------
本项目把第三方服务全部收敛到自己的镜像里（见 config.py 的「历史沿革」注释），
云端 ASR/TTS 厂商的私有报文形状一个都不接。OpenAI 兼容是事实标准：
vLLM / Xinference / Ollama(兼容层) / one-api / new-api / 各种国产网关都能
提供 ``/audio/transcriptions`` 与 ``/audio/speech``。要接新厂商时优先
在**网关层**做风格适配，而不是在这里加分支 —— 那会把 llm_gateway
已经踩过的坑（四种报文形状的翻译）再踩一遍。

例外：本机自建的 FunASR/SenseVoice（``api_style=local_asr``）
------------------------------------------------------------
它不是 OpenAI 兼容（``/asr`` + multipart ``audio`` 字段、无 model 无密钥），
硬套 OpenAI 路径只会得到 404/400 且报错指不到真因，所以在 :func:`asr`
里有一个显式分支。判据见 :func:`_asr_style`。

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
import io
import json
import logging
import math
import socket
import struct
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Final

from .. import config
from ..model_catalog import API_STYLE_LOCAL_ASR, usage_kinds

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

# ======================================================== TTS 返回体的格式判别
#
# 为什么必须做（2026-10-21 真机故障）
# ----------------------------------
# 老人听到的是"断断续续 + 杂音 + 破音"。而"杂音/破音"最经典的两个成因，
# 都是**把非 PCM 字节当成 S16LE 送进 Opus 编码器**：
#   · 厂商无视 ``response_format: "pcm"`` 回了 mp3/aac → 整段被当采样点，
#     出来的是持续沙沙声；
#   · 厂商回的是 **32-bit float PCM**（不少推理框架的默认输出格式）→
#     IEEE754 位模式被当 int16 读，听感是沙沙声 + 爆音，而且**音量还很响**。
# 原来的防御只认 ``RIFF`` 和 ``{``/``[``，这两种都漏。
#
# 这里的原则是「**先判别、再处理、并留下痕迹**」：不猜、不静默，
# 每种异常格式都要在日志里留一条，否则下次还会从"声音难听"开始查。

#: 每种异常格式在**本进程内只 INFO/WARNING 一次**（避免每句刷一条）。
_SNIFF_LOGGED: set[str] = set()


def _sniff_audio(raw: bytes) -> str:
    """按魔数判别 TTS 返回体到底是什么格式。

    返回 ``pcm`` / ``wav`` / ``mp3`` / ``aac`` / ``ogg`` / ``flac`` / ``json`` / ``empty``。
    判不出来时返回 ``pcm`` —— 这是**保守选择**：宁可按约定格式处理，
    也不要把正常音频误判成压缩包去解码（那会直接静音）。
    """
    if not raw:
        return "empty"
    head = raw[:12]
    if head[:4] == b"RIFF":
        return "wav"
    if head[:3] == b"ID3":
        return "mp3"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:1] in (b"{", b"["):
        return "json"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        # MPEG 帧同步字。ADTS(AAC) 与 MP3 都是 0xFFFx，靠 layer 位区分：
        # MPEG Audio 的 layer 位是 01/10/11，ADTS 恒为 00。
        layer = (head[1] >> 1) & 0x03
        return "aac" if layer == 0 else "mp3"
    return "pcm"


def _looks_like_float32(pcm: bytes) -> bool:
    """判断一段"按 S16LE 收到"的字节，其实是不是 32-bit float PCM。

    判据只有一条，但极强：**把每 4 字节当 float32 读，看是否几乎全部落在
    [-1.5, 1.5]**。真实语音的 int16 采样对拼成 4 字节后，float 解释的指数位
    是随机的，落在这个区间的比例约 1%；而真正的 float32 音频是 100%。
    两者相差两个数量级，不存在"边缘地带"。
    """
    n = len(pcm) // 4
    if n < 2048:            # 不足 8KB 统计不可靠，不做判断（保守）
        return False
    try:
        values = struct.unpack(f"<{n}f", pcm[: n * 4])
    except struct.error:    # pragma: no cover - 长度已保证，防御性保留
        return False
    in_range = 0
    total = 0.0
    for v in values:
        if not math.isfinite(v):
            return False     # int16 噪声里出现 NaN/Inf 才是常态，直接否掉
        if -1.5 <= v <= 1.5:
            in_range += 1
            total += v * v
    if in_range / n < 0.95:
        return False
    rms = math.sqrt(total / max(1, in_range))
    return 0.002 < rms < 0.9   # 全零（静音）不算，避免误报


def _float32_to_s16(data: bytes) -> bytes:
    """32-bit float PCM → 16-bit PCM（削顶到 [-1,1]，与 Opus 输入口径一致）。"""
    n = len(data) // 4
    if not n:
        return b""
    values = struct.unpack(f"<{n}f", data[: n * 4])
    out = []
    for v in values:
        if not math.isfinite(v):
            v = 0.0
        elif v > 1.0:
            v = 1.0
        elif v < -1.0:
            v = -1.0
        out.append(int(v * 32767))
    return struct.pack(f"<{n}h", *out)


def _decode_any_to_pcm24k(raw: bytes, endpoint: str) -> bytes:
    """mp3/aac/ogg/flac → 24kHz/mono/S16LE 裸 PCM（用 PyAV，自带 ffmpeg）。

    为什么不直接调 ``tts_edge.mp3_to_pcm_24k``
    ------------------------------------------
    那个函数是本模块需要的逻辑（且实现是对的：逐帧 resample + 末尾
    ``resample(None)`` 冲洗），但它住在 **Edge 专用**模块里。让
    ``asr_tts`` 依赖 ``tts_edge`` 会把「可选依赖 edge-tts」变成
    「OpenAI 路径的隐性前置」，将来有人清理 Edge 分支时会连带炸掉主链路。
    6 行代码的重复，换模块边界干净，这个交换是划算的。
    """
    try:
        import av
    except ImportError:
        raise VoiceUpstreamError(
            f"TTS 返回的是压缩音频（不是裸 PCM），需要 av 才能解码：{endpoint}。"
            f"修复：pip install av，或把该 TTS 配置的响应格式改成 pcm"
        ) from None
    try:
        container = av.open(io.BytesIO(raw))
        try:
            resampler = av.AudioResampler(format="s16", layout="mono",
                                          rate=TTS_SAMPLE_RATE)
            out = []
            for frame in container.decode(audio=0):
                out.extend(resampler.resample(frame))
            out.extend(resampler.resample(None))   # 冲洗，漏了会切掉句尾
            return b"".join(bytes(f.planes[0]) for f in out)
        finally:
            container.close()
    except Exception as exc:  # noqa: BLE001
        raise VoiceUpstreamError(
            f"TTS 返回的压缩音频解码失败（{type(exc).__name__}: {exc}）：{endpoint}"
        ) from None


def _ensure_pcm(raw: bytes, endpoint: str, where: str) -> bytes:
    """把 TTS 返回体**归一成** 24kHz/mono/S16LE 裸 PCM，并记下真实格式。

    ``where`` 只用于日志（"非流式" / "流式首片"），便于定位是哪条路径。
    """
    fmt = _sniff_audio(raw)
    if fmt not in ("pcm", "empty") and fmt not in _SNIFF_LOGGED:
        _SNIFF_LOGGED.add(fmt)
        log.warning("TTS 返回体真实格式是 %s（不是约定的裸 PCM）—— "
                    "已在服务端转换，但应改厂商配置避免额外开销与音质损失："
                    "%s（%s）", fmt, endpoint, where)

    if fmt == "json":
        raise VoiceUpstreamError(
            f"TTS 返回体像 JSON 错误而非音频：{raw[:200]!r}：{endpoint}")
    if fmt == "wav":
        raw = raw[_WAV_HEADER_LEN:]
        fmt = "pcm"
    if fmt in ("mp3", "aac", "ogg", "flac"):
        raw = _decode_any_to_pcm24k(raw, endpoint)
        fmt = "pcm"
    if fmt == "pcm" and _looks_like_float32(raw):
        if "float32" not in _SNIFF_LOGGED:
            _SNIFF_LOGGED.add("float32")
            # 这条必须是 WARNING：float32 被当 int16 送 Opus，老人听到的
            # 就是持续杂音 + 爆音，而服务端日志一切"正常"。
            log.warning("TTS 返回的是 32-bit float PCM（约定是 16-bit），"
                        "已自动转换。这是「杂音/破音」的典型成因：%s（%s）",
                        endpoint, where)
        raw = _float32_to_s16(raw)
    if len(raw) % 2:
        raw = raw[:-1]        # 丢掉半个采样，保证 S16LE 对齐
    return raw

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


def _preset_endpoint(row: Any) -> str:
    """预置厂商的**官方默认地址**（配置行 endpoint 留空时的唯一来源）。

    为什么必须有这一层（2026-10-21 线上事故）
    ----------------------------------------
    管理后台新增一条「腾讯云 TTS」配置，管理员只填了密钥 —— 这是需求明确
    允许的最短路径，也是预置下拉的卖点。但 ``_row_endpoint`` 见到空串直接
    抛 :class:`VoiceConfigMissingError`，而 ``tts()`` 只取 ``resolve_row``
    的第一行、**没有按序兜底**，于是这一行把整条 TTS 链路打死：
    服务端日志一行 ``TTS 失败，跳过音频``，设备侧 `speaking` 状态照走、
    老人听到的是彻底静默。表面看是「固件没声音」，实际是配置解析把自己
    锁死了。

    腾讯云 TTS/ASR、火山的域名都是写死在适配器里的常量，没有「必须手填」
    的道理；``endpoint`` 的真实语义是**可选覆盖**（私有化/代理）。

    ⚠️ 只查**厂商预置**，不查管理员自己填过的行 —— 这里绝不能猜。
    """
    provider = (getattr(row, "provider", "") or "").strip().lower()
    if not provider:
        return ""
    # TTS 厂商预置（腾讯云 / 火山 / 百炼 / Edge…）
    from .catalog import vendor_entry

    entry = vendor_entry(provider)
    if entry is not None:
        return str(entry.get("endpoint") or "").strip()
    # ASR / LLM / Embedding 的预置（如 tencent_asr → asr.tencentcloudapi.com）
    from ..provider_presets import preset_entry

    preset = preset_entry(provider)
    if preset is not None:
        return str(preset.get("endpoint") or "").strip()
    return ""


def _row_endpoint(row: Any) -> str:
    endpoint = (getattr(row, "endpoint", "") or "").strip().rstrip("/")
    if endpoint:
        return endpoint
    preset = _preset_endpoint(row)
    if preset:
        return preset.rstrip("/")
    # 报错文案里带上 provider：管理员常见的情况是「名称没填」（预置下拉
    # 允许只选厂商就保存），此时「配置「」」等于没说，查不出是哪一行。
    label = ((getattr(row, "name", "") or "").strip()
             or (getattr(row, "provider", "") or "").strip())
    raise VoiceConfigMissingError(f"配置「{label}」没有填写服务地址（endpoint）")


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
def _asr_endpoint_scheme(row: Any) -> str:
    """取这一行 endpoint 的协议（小写）。空 endpoint 返回空串。"""
    endpoint = (getattr(row, "endpoint", "") or "").strip()
    if not endpoint:
        return ""
    return (urllib.parse.urlsplit(endpoint).scheme or "").lower()


def _asr_style(row: Any) -> str:
    """这一行走哪条 ASR 实现（``local_asr`` / ``tencent`` / ``openai_compatible``）。

    判据与 ``_tts_dispatch_style`` 同构：先认 provider 预置值，再认显式
    ``api_style``。**本地 ASR 必须排在 api_style 之前判**的理由与 TTS 侧
    完全一样：预置会把 api_style 一并写好，但历史行可能是老默认值，
    让 api_style 先判会把本地行静默路由到 OpenAI 路径（表现为
    「配好了却报 endpoint 格式不对」）。

    回落 OpenAI 兼容而不是报错，与 TTS 侧同一理由：不破坏存量配置。
    """
    provider = (getattr(row, "provider", "") or "").strip().lower()
    if provider == "funasr_local":
        return API_STYLE_LOCAL_ASR
    # 腾讯云的 ASR 预置 provider 值是 tencent_asr（与 TTS 的 tencent 分开：
    # 两张预置表各管一摊，但走同一个适配器）。
    if provider in ("tencent", "tencent_asr"):
        return "tencent"
    style = (getattr(row, "api_style", "") or "").strip()
    if style == API_STYLE_LOCAL_ASR:
        return API_STYLE_LOCAL_ASR
    if style == "tencent":
        return "tencent"
    return "openai_compatible"


def _row_label(row: Any) -> str:
    return f"{getattr(row, 'name', '')}({getattr(row, 'model', '')})"


def asr(db: Any, pcm_16k: bytes) -> str:
    """16kHz/mono/S16LE PCM -> 识别文本。

    :param db: SQLAlchemy Session（取 asr 配置行）。
    :raises VoiceConfigMissingError: 没有可用 ASR 配置。
    :raises VoiceUpstreamError: 上游不可用/返回异常。

    为什么 wav 头在这里手写而不是调 wave 模块：标准库的 wave 会写出
    带 LIST/INFO 块的变体，且接口是文件而非字节；这里是 44 字节固定头，
    写死更可预测（固件/厂商都能解）。

    为什么按配置列表逐个兜底而不是只取第一条（2026-10-20 事故）：
    生产库里有三条 asr 行，优先级最高的一条 endpoint 是
    ``wss://api-maas.teleai.com.cn/...``（TeleAI 实时 ASR）。本函数只走
    HTTP multipart（``urllib`` 不认识 wss，报
    ``unknown url type: wss``），原先只调 ``resolve_row`` 取第一条 ⇒
    高优先级的 wss 行一票否决，后面两条好好的 https 行永远轮不到，
    整条语音链路「唤醒成功但一个字都识别不出」。
    ⇒ 改成与 LLM 网关同构的「按序兜底」：非 http(s) 端点跳过并告警，
    调用失败换下一条，全部失败才抛。

    风格分支（2026-10-22）：``api_style=local_asr`` 走本机 FunASR/SenseVoice
    的 ``/asr``，其余走 OpenAI 兼容的 ``/audio/transcriptions``。判据见
    :func:`_asr_style`。
    """
    from ..llm_gateway import resolve_plan

    plan = resolve_plan(db, "asr")
    if not plan:
        raise VoiceConfigMissingError("asr")
    # 自部署 FunASR 是保底，永远排最后（理由见 LAST_RESORT_ASR_PROVIDERS）
    plan = _primary_then_last_resort(plan, LAST_RESORT_ASR_PROVIDERS)

    wav = wav_header_bytes(len(pcm_16k), 16000) + pcm_16k

    last_error: VoiceUpstreamError | None = None
    skipped: list[str] = []

    for _kind, row in plan:
        style = _asr_style(row)
        scheme = _asr_endpoint_scheme(row)
        # 腾讯云这类「固定域名 + 请求体签名」的厂商**不需要 endpoint**：
        # 域名写在适配器里，endpoint 只是可选的覆盖（私有化/代理）。
        # 若沿用 http/https 闸门，管理员留空 endpoint（最自然的填法）会被
        # 静默跳过，表现为「配好了腾讯云却一直用回退的那条」。
        if style != "tencent" and scheme not in ("http", "https"):
            skipped.append(
                f"{_row_label(row)} endpoint={scheme + '://' if scheme else '（空）'}"
                f"{'（实时 ASR 等服务端未实现的协议）' if scheme in ('ws', 'wss') else ''}")
            continue

        timeout = _row_timeout(row, ASR_TIMEOUT_SEC)
        try:
            if style == API_STYLE_LOCAL_ASR:
                # 本机 FunASR/SenseVoice（llama.cpp GGUF 版）：
                # POST {endpoint}/asr，multipart 字段名 audio，**不需要**
                # model / language / response_format —— 服务端按加载的模型
                # 识别、语种自动判别。多传字段无害，但传 model 会让人误以为
                # 换个名字就能换模型（实际要改服务端加载的 gguf）。
                payload = _post_multipart(row, "/asr", {},
                                          "audio", "utterance.wav", wav, timeout)
            elif style == "tencent":
                from . import asr_tencent
                secret_id, secret_key = _secret_pair(row)
                # 腾讯云的「模型」字段存的是 EngSerViceType（如 16k_zh）。
                # 本平台音频固定 16kHz，所以只可能选 16k_* 系列。
                engine = ((getattr(row, "model", "") or "").strip()
                          or asr_tencent.DEFAULT_ENGINE)
                try:
                    return asr_tencent.transcribe(
                        pcm_16k, secret_id=secret_id, secret_key=secret_key,
                        engine=engine, endpoint=_row_endpoint(row),
                        timeout=timeout).strip()
                except Exception as exc:  # noqa: BLE001
                    # ⚠️ 必须转成 VoiceUpstreamError：外层只认这个类型才会
                    # 「按序换下一条」。腾讯云的 TencentError 是 RuntimeError
                    # 子类，直接冒泡会让第一优先级的腾讯云 key 填错时
                    # **整条 ASR 全挂**，而不是回落到后面的配置行 ——
                    # 那正是 2026-10-20 那次「唤醒成功但一个字都识别不出」
                    # 事故的同一个形状。
                    raise VoiceUpstreamError(f"腾讯云 ASR 失败：{exc}") from None
            else:
                model = (getattr(row, "model", "") or "").strip() or "whisper-1"
                fields = {
                    "model": model,
                    "language": config.VOICE_ASR_LANGUAGE,
                    "response_format": "json",
                }
                payload = _post_multipart(row, "/audio/transcriptions", fields,
                                          "file", "utterance.wav", wav, timeout)
        except VoiceUpstreamError as exc:
            log.warning("ASR 配置「%s」调用失败，按序换下一条：%s",
                        _row_label(row), exc)
            last_error = exc
            continue

        text = str(payload.get("text", "") or "").strip()
        if skipped:
            log.warning("ASR 跳过 %d 条不兼容配置：%s", len(skipped),
                        "；".join(skipped))
        return text

    if last_error is not None:
        raise last_error
    if skipped:
        raise VoiceUpstreamError(
            f"全部 ASR 配置的 endpoint 都不是 http/https（共 {len(skipped)} 条）："
            f"{'；'.join(skipped)}")
    raise VoiceConfigMissingError("asr")


# ---------------------------------------------------------------- TTS
def _tts_dispatch_style(row: Any) -> str:
    """判断这一行该走哪条 TTS 实现。

    判据优先级（顺序不能换）
    ------------------------
    1. ``provider`` 命中预置厂商 → 用厂商自带的 api_style。
       ⚠️ **这条必须排在 api_style 之前**：管理后台选「火山引擎」时会把
       ``api_style`` 预填成 ``openai_compatible``（因为火山列表里第一项是它，
       或是历史默认值残留），若让 api_style 优先，火山行会被静默路由到
       OpenAI 路径 —— 表现为「配好了火山，却报 endpoint 格式不对」。
       这正是「选了预置还要手改字段」会引发的坑，判据要防住它。
    2. ``provider == "edge"`` → 内置 Edge 桥（Edge 无 endpoint，
       不能靠 api_style 判）。
    3. 显式填了 ``api_style`` → 按它走（手填厂商/自建网关的正规入口）。
    4. 都认不出 → OpenAI 兼容（历史行为：绝大多数自建服务是兼容的）。

    ⚠️ 第 4 条回落到 OpenAI 兼容而不是报错，是为了**不破坏存量配置**：
    本项目历史上所有 TTS 行都走 OpenAI 兼容，改成「认不出就报错」会让全部
    旧部署在升级瞬间失效。
    """
    from .catalog import vendor_entry

    provider = (getattr(row, "provider", "") or "").strip().lower()
    preset = vendor_entry(provider)
    if preset is not None:
        return str(preset["api_style"])

    if provider == "edge":
        return "edge"
    style = (getattr(row, "api_style", "") or "").strip()
    if style in ("edge", "volcengine", "tencent"):
        return style
    return "openai_compatible"


def _secret_pair(row: Any) -> tuple[str, str]:
    """从密钥字段里拆出厂商要的 ``(id, secret)`` 两段。

    火山要 ``AK:SK``、腾讯云要 ``SecretId:SecretKey`` —— **同一个形状**，
    所以只留一份解析（历史上它叫 ``_volc_keys``，腾讯云接入时改名）。

    接受两种写法（都要支持，否则管理员会以为自己填错了）：
      * ``ID:SECRET``（推荐，冒号分隔）
      * 只有一段 → 当作 ID，SECRET 留空（此时厂商会 401/403，错误文案会指明）

    用中文冒号不行 —— 从网页上复制的文案里经常混进全角冒号，
    而用户**看不出**自己复制了什么，必须在这里兜住。
    """
    raw = (getattr(row, "api_key", "") or "").strip()
    for sep in (":", "："):     # 全角冒号一并处理
        if sep in raw:
            ak, _, sk = raw.partition(sep)
            return ak.strip(), sk.strip()
    return raw, ""


def tts(db: Any, sentence: str, *, voice: str = "", speed: float = 1.0,
        row: Any = None) -> bytes:
    """一句话 -> 24kHz/mono/S16LE 裸 PCM（**统一分发入口**）。

    按当前配置行的 ``provider``/``api_style`` 自动切换到对应实现：
      - ``edge``          → :mod:`app.voice.tts_edge`（免密钥，合成+解码）
      - ``volcengine``    → :mod:`app.voice.tts_volc`（私有协议 + 签名）
      - 其他/未填          → OpenAI 兼容 ``/audio/speech``（阿里云百炼等）

    :param voice: 音色。空串表示**不带 voice 字段**，让厂商用默认
        （见 pick_voice 的回落链）。
    :param row: 指定用哪一行配置。缺省（None）表示自己按优先级取第一条。
        给 ``tts_stream`` 的按序兜底用 —— 它必须能明确指定"这一次试哪一行"，
        否则兜底循环里每次调 tts() 都会被 resolve_row 拉回第一条，
        表现是「同一条坏配置被重试 N 次、后面好的永远轮不到」。
    :raises VoiceConfigMissingError / VoiceUpstreamError
    """
    from ..llm_gateway import resolve_row

    if row is None:
        row = resolve_row(db, "tts")
    if row is None:
        raise VoiceConfigMissingError("tts")

    style = _tts_dispatch_style(row)
    if style == "edge":
        from . import tts_edge
        try:
            return tts_edge.synth_pcm(sentence, voice)
        except tts_edge.MissingEdgeDeps as exc:
            raise VoiceConfigMissingError(str(exc)) from None
        except TimeoutError as exc:
            raise VoiceUpstreamError(
                f"Edge TTS 超时未返回（微软可能限流，本机连续调用会触发）：{exc}") from None
        except Exception as exc:  # noqa: BLE001
            raise VoiceUpstreamError(f"Edge TTS 调用失败：{exc}") from None

    if style == "volcengine":
        from . import tts_volc
        ak, sk = _secret_pair(row)
        endpoint = _row_endpoint(row)
        try:
            return tts_volc.tts(sentence, voice, access_key=ak, secret_key=sk,
                                 endpoint=endpoint,
                                 speed_ratio=speed,
                                 timeout=_row_timeout(row, TTS_TIMEOUT_SEC))
        except Exception as exc:  # noqa: BLE001
            raise VoiceUpstreamError(f"火山引擎 TTS 失败：{exc}") from None

    if style == "tencent":
        from . import tts_tencent
        secret_id, secret_key = _secret_pair(row)
        try:
            return tts_tencent.tts(
                sentence, voice, secret_id=secret_id, secret_key=secret_key,
                endpoint=_row_endpoint(row),
                # ⚠️ 腾讯云的 TTS 不需要 Region（与 ASR 一致，官方未要求），
                # 传了也不影响签名（它不在 signed headers 里）。
                speed=speed,
                timeout=_row_timeout(row, TTS_TIMEOUT_SEC))
        except Exception as exc:  # noqa: BLE001
            raise VoiceUpstreamError(f"腾讯云 TTS 失败：{exc}") from None

    return _tts_openai(row, sentence, voice=voice, speed=speed)


#: 「一次调用拿整段音频」的 TTS 风格（没有增量能力，必须等整包返回）。
#: 单独抽常量是因为它同时驱动两处判据：``tts_stream`` 的分支，
#: 以及按序兜底时「哪些行可以安全地整段重来」。
_ATOMIC_TTS_STYLES: Final[tuple[str, ...]] = ("edge", "volcengine", "tencent")

# ---------------------------------------------------------------- 兜底口径
#: **保底 ASR**：自部署的 FunASR/SenseVoice（provider=funasr_local）。
LAST_RESORT_ASR_PROVIDERS: Final[tuple[str, ...]] = ("funasr_local",)

#: **保底 TTS**：微软免费 Edge TTS（provider=edge）。
LAST_RESORT_TTS_PROVIDERS: Final[tuple[str, ...]] = ("edge",)

# 为什么保底要写死在代码里、而不是只靠 ai_providers.priority 排序
# ------------------------------------------------------------------
# 兜底配置的**全部价值**是「别人都挂了它还在」，所以它必须满足三条硬性质：
#   ① 零密钥 ② 零自定义端点 ③ 零调用成本。
# 只有这两条满足：自部署 FunASR（本机 8082，免密钥）与 Edge TTS（微软免费，
# 走 edge-tts 库）。云厂商的 key 会过期、额度会用完、endpoint 会被改错 ——
# 2026-10-21 的事故就是「按预置加了一条腾讯云 TTS，密钥/地址没填全，
# 它按 priority 抢在 Edge 前面，结果整条链路静默」。让这条坏配置靠排序
# "自然"排到最后是不可靠的：priority 是可编辑数据，谁都能改错。
# ⇒ 代码强制把这两个 provider 排到**最后**，无论库里 priority 写成多少。
# 管理员仍可用 enabled=0 彻底关掉它（那时就真的没有兜底了，属于明知故犯）。


def _primary_then_last_resort(plan: list[tuple[str, Any]],
                              last_resort: tuple[str, ...]
                              ) -> list[tuple[str, Any]]:
    """把保底 provider 的行挪到**末尾**，其余保持原有优先级顺序。

    为什么不是"把保底放在第一位"：保底是**最后手段**，不是首选。自部署 ASR
    的识别质量明显弱于云端（真机实测把「你好小智」听成「你好小志/小池」），
    Edge 的音色也不如腾讯云大模型音色自然 —— 它们在链路里的位置就是刹车，
    不是发动机。

    :param plan: ``resolve_plan`` 的返回值（已按 priority, id 排好序）。
    :param last_resort: 需要挪到末尾的 provider 集合（小写）。
    :return: 新列表；顺序：先非保底（原顺序），后保底（原顺序）。
    """
    primary: list[tuple[str, Any]] = []
    fallback: list[tuple[str, Any]] = []
    for item in plan:
        provider = (getattr(item[1], "provider", "") or "").strip().lower()
        (fallback if provider in last_resort else primary).append(item)
    return primary + fallback


def _tts_atomic_with_fallback(db: Any, sentence: str, *,
                              voice: str = "", speed: float = 1.0) -> bytes:
    """整段式 TTS 的**按序兜底**：第一条失败就换下一条，全失败才抛。

    为什么必须兜底（2026-10-21 事故，与 ASR 那次同形）
    ----------------------------------------------
    管理员按预置加了一条「腾讯云 TTS」（priority 6，抢在 Edge 的 10 前面），
    然后密钥还没配全/地址没填 —— ``tts()`` 只取 ``resolve_row`` 的第一条、
    **没有按序兜底**，于是这一行把整条语音合成打死：服务端刷
    ``TTS 失败，跳过音频``，设备侧 ``STATE: speaking`` 照走、`finish()`
    照打「音频已发」，老人听到的是彻底静默。而库里明明还躺着一条好用的 Edge。

    只在**整段式**风格上做：它们要么整段成功要么整段失败，重试不会产生
    "半句话已经播出去了"的错位。OpenAI 兼容那条是真流式，中途换行会把
    前后两个音色拼在一起，**不做**（要做得先缓冲，代价是首帧时延）。

    :return: 第一段成功的音频（24kHz/mono/S16LE）。
    :raises VoiceUpstreamError: 全部候选都失败时抛**最后一条**的错误。
    :raises VoiceConfigMissingError: 一条候选都没有。
    """
    from ..llm_gateway import resolve_plan

    candidates = [r for _kind, r in _primary_then_last_resort(
                      resolve_plan(db, "tts"), LAST_RESORT_TTS_PROVIDERS)
                  if _tts_dispatch_style(r) in _ATOMIC_TTS_STYLES]
    last_error: Exception | None = None
    for index, candidate in enumerate(candidates):
        try:
            audio = tts(db, sentence, voice=voice, speed=speed, row=candidate)
        except (VoiceUpstreamError, VoiceConfigMissingError) as exc:
            last_error = exc
            log.warning("TTS 配置「%s」失败，按序换下一条：%s",
                        _row_label(candidate), exc)
            continue
        if index:
            # 回落必须留痕：否则「换了音色/换了厂商」在听感上像灵异事件
            log.warning("TTS 已回落到配置「%s」（前 %d 条不可用）",
                        _row_label(candidate), index)
        return audio

    if last_error is not None:
        raise last_error
    raise VoiceConfigMissingError("tts")


def _tts_openai(row: Any, sentence: str, *, voice: str, speed: float) -> bytes:
    """OpenAI 兼容 ``/audio/speech`` 那一路（阿里云百炼、各家网关、自建服务）。

    抽成独立函数是为了让 :func:`tts` 只做「分发」，各家协议差异各自成文件。
    改动前这里是 ``tts`` 本体；现在 ``tts`` 是分发入口，**调用方不用改**。
    """
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

    # 归一成裸 PCM：剥 wav 头、解码 mp3/aac/ogg/flac、转换 float32、
    # 识别 JSON 错误体、按采样对齐。全部收在 _ensure_pcm 一处，
    # 流式/非流式共用同一套判据 —— 两条路径各写一份必然漂移，
    # 而漂移的表现是"非流式能出声、流式是噪音"，最难自查。
    return _ensure_pcm(raw, endpoint, "非流式")


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

    防御口径与非流式版**共用同一套判据**（:func:`_ensure_pcm`）：
    wav 剥 44 字节头、mp3/aac/ogg/flac 用 PyAV 解码、float32 转 int16、
    JSON 错误体识别（读到就抛，不把错误文本当音频发出去）、奇数长度按
    采样对齐（分片边界可能劈开一个采样，用 1 字节进位缓冲兜）。

    ⚠️ 判别期会先攒 8KB 再决定走流式还是整段：float32 判据需要 2048 个
    采样才有统计意义。代价是首帧晚约 170ms，换来的是"绝不把 mp3/float32
    当 PCM 送出去"。这两类错误格式正是「杂音 + 破音」的直接成因。
    """
    from ..llm_gateway import resolve_row

    row = resolve_row(db, "tts")
    if row is None:
        raise VoiceConfigMissingError("tts")

    # ---- 与 tts() 同样的分发（判据见该函数注释）----
    # ⚠️ 必须在这里也分一次，否则 Edge/火山 配置下行时会走 OpenAI 路径：
    # Edge 的 endpoint 是空串 → 拼出 "/audio/speech" → 连到本机自己 → 失败。
    # 症状是「非流式能出声、流式不出声」，属于最难自查的一类不一致。
    _style = _tts_dispatch_style(row)
    if _style in _ATOMIC_TTS_STYLES:
        # ⚠️ 这三个分支的 tts() 返回**整段 bytes**，不是分片迭代器。
        # 曾经写成 `for chunk in tts(...)`：bytes 被逐字节迭代 ⇒ chunk 是 int
        # ⇒ 上层 session.py 的 `carry += chunk` 抛
        # `TypeError: can't concat int to bytes`，老人一个字都听不到，
        # 而 finish() 照样打印「音频已发」（假绿日志把这起事故藏了两周）。
        # 这三条路径本身没有增量能力（Edge 要等整段解码完，火山/腾讯云都是
        # 整包 JSON 响应），所以这里**整段 yield 一次**：行为与 tts() 等价，
        # 也不假装流式。
        yield _tts_atomic_with_fallback(db, sentence, voice=voice, speed=speed)
        return

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
        carry = b""         # 奇数采样进位（分片边界可能劈开一个采样）
        head_buf = b""      # 判别格式前攒的字节
        decided = False     # 是否已确认"这就是真 PCM，可以放心流式吐"
        while True:
            try:
                chunk = resp.read(4096)
            except (socket.timeout, TimeoutError):
                raise VoiceUpstreamError(
                    f"读取音频流超时：{endpoint}，可调大该配置的「超时秒数」") from None
            if not chunk:
                break

            if not decided:
                # ---- 判别期：先攒够 8KB 再决定怎么处理 ----
                # 为什么要攒而不是看第一片：float32 判据需要 2048 个采样
                # （8KB）才有统计意义，而 4KB 一片看不出来。代价是首帧
                # 晚 170ms 左右（8KB @ 24k/16bit），换来的是"不会把 mp3
                # 或 float32 当 PCM 送出去"——后者正是杂音/破音的成因。
                head_buf += chunk
                if len(head_buf) < 8192:
                    continue
                fmt = _sniff_audio(head_buf)
                if fmt == "json":
                    rest = head_buf + resp.read()
                    raise VoiceUpstreamError(
                        f"TTS 返回体像 JSON 错误而非音频：{rest[:200]!r}：{endpoint}")
                if fmt in ("mp3", "aac", "ogg", "flac"):
                    # 压缩格式无法边解边切（解码器有状态），整段收完再解。
                    data = _ensure_pcm(head_buf + resp.read(), endpoint, "流式整段")
                    if data:
                        yield data
                    return
                if fmt == "wav":
                    head_buf = head_buf[_WAV_HEADER_LEN:]
                if _looks_like_float32(head_buf):
                    data = _ensure_pcm(head_buf + resp.read(), endpoint, "流式整段")
                    if data:
                        yield data
                    return
                # 真 PCM：把攒下的这批吐出去，之后恢复逐片流式
                decided = True
                if len(head_buf) % 2:
                    carry = head_buf[-1:]
                    head_buf = head_buf[:-1]
                if head_buf:
                    yield head_buf
                continue

            if carry:
                chunk = carry + chunk
                carry = b""
            if len(chunk) % 2:
                carry = chunk[-1:]
                chunk = chunk[:-1]
            if chunk:
                yield chunk

        if not decided:
            # 整个响应不足 8KB（短句很常见）：走一次完整归一。
            if not head_buf:
                raise VoiceUpstreamError(f"TTS 返回空音频体：{endpoint}")
            data = _ensure_pcm(head_buf, endpoint, "流式短句")
            if data:
                yield data
            return
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
