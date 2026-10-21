"""腾讯云语音合成（TTS）适配 —— 云 API 3.0 + TC3 签名。

为什么单独一个模块（而不是塞进 asr_tts）
----------------------------------------
与火山引擎同一个理由：腾讯云的 TTS **不是 OpenAI 兼容**。

| 维度 | OpenAI 兼容 | 腾讯云 |
| --- | --- | --- |
| 端点 | ``{endpoint}/audio/speech`` | ``https://tts.tencentcloudapi.com``（**固定域名**） |
| 鉴权 | ``Authorization: Bearer <key>`` | TC3-HMAC-SHA256（见 ``tc3.py``） |
| 正文 | ``{model, input, voice}`` | ``{Text, SessionId, VoiceType, Codec, SampleRate, ...}`` |
| 音频 | 原始字节 | **JSON 包着 base64** |

字段契约（依据官方 1073/37995，2026-10-21 查证）
-----------------------------------------------
* ``Codec`` 取 ``pcm`` ⇒ 直接是 16bit 裸 PCM，**不需要解码**（这是选它的
  主要理由：本平台客户端要的就是裸 PCM，见 ``asr_tts.TTS_SAMPLE_RATE``）；
* ``SampleRate`` 取 ``24000`` —— ⚠️ **只有部分音色支持 24k**，其余只到 16k
  （文档原文「24000：24k（部分音色支持，请参见音色列表）」）。
  我们**不做 16k→24k 的自行上采样**：那不是"补一个参数"而是自己写重采样器，
  插值出来的声音老人听着发闷，而且一旦出问题极难判断是厂商还是我们的锅。
  不支持 24k 的音色就明确报错、让管理员换音色（错误文案直接指明）。
* ``Text`` 中文上限 **150 字**（全角标点也算一个字）⇒ 本模块**内部分段**，
  逐段合成后按顺序拼接字节。上层拿到的仍是一整段 PCM，调用方无感。
* ``VoiceType`` 是**整数**（如 1001），而本平台的 ``voice`` 字段是字符串 ⇒
  在 :func:`_voice_type` 里做转换并给出可读报错。
* ``Speed`` 是**档位不是倍率**：-2→0.6x、-1→0.8x、0→1.0x、1→1.2x、
  2→1.5x、6→2.5x。本平台的 ``speed`` 是倍率 ⇒ 由 :func:`_speed_gear`
  映射到最近档位（1.0 直接传 0）。

⚠️ 实测结论（2026-10-21，用真实 SecretId/SecretKey 打过）
--------------------------------------------------------
**签名与报文已经端到端验证通过**，不再只是"按文档实现、未验证"：

* ``Codec=pcm`` + ``SampleRate=24000`` 真能拿到裸 PCM：16 字文本合成
  171360 字节（3.57s @24k，约 0.22s/字，与正常语速吻合）；
* 超自然音色 402001 也接受 24000（"8k/16k/24k"这一列是真的）；
* ``EmotionCategory=happy`` 产出的字节与不带情感**不同** ⇒ 情感真的生效；
* **用错 SecretKey 会返回 ``AuthFailure.SignatureFailure``** —— 这反过来
  证明 TC3 签名链路（``tc3.py``）是真的被服务端验了，不是形同虚设。

冒烟（``tools/smoke_tencent_voice.py``）仍然保留结构断言，因为它要在**没有
密钥的 CI 上**跑；但上述四条已经写进测试名与注释，避免以后有人以
"没验过"为由改坏它。
"""
from __future__ import annotations

import base64
import logging
import uuid
from typing import Any, Final

from . import tc3

log = logging.getLogger("elder.voice.tencent_tts")

TENCENT_TTS_HOST: Final[str] = tc3.SERVICE_HOSTS["tts"]
TENCENT_TTS_ACTION: Final[str] = "TextToVoice"
TENCENT_TTS_VERSION: Final[str] = "2019-08-23"

#: 中文文本单次上限（全角标点算一个字）。英文是 500 字母，但中文上限更严，
#: 统一按中口径切分 —— 混合文本按字符数算，不会超。
TEXT_LIMIT: Final[int] = 150

#: 客户端要求的采样率。⚠️ 请求 24000 但**不是所有音色都支持**，见模块说明。
TARGET_SAMPLE_RATE: Final[int] = 24000

#: 预置音色（2026-10-21 由用户提供的控制台音色表，文档 1073/92668）。
#:
#: ⚠️ **VoiceType 是整数**；ID 若在控制台变动会报 InvalidParameterValue.VoiceType。
#:
#: 每条都标了 ``rates``（该音色支持的采样率）与 ``emotions``（支持的情感）——
#: 这两项不是装饰：
#:   * 本项目客户端**硬要求 24kHz**（``asr_tts.TTS_SAMPLE_RATE``），
#:     请求了不支持的采样率会被厂商拒绝。下方 8 个音色**全部支持 24k**，
#:     所以这条约束在当前预置集合里不会触发；但管理员可以手填别的音色 ID，
#:     那时 ``rates`` 就是排查的第一手依据。
#:   * 情感只有大模型音色（601xxx）支持 9 种；超自然音色（402/403/502）
#:     **只支持中性**。给后者传 happy 不会让声音更有感情，反而可能报错，
#:     所以 :func:`tts` 里按音色白名单过滤，**不支持就静默不发**（不能让
#:     "情感"这种锦上添花的功能把主链路搞挂）。
TENCENT_VOICES: Final[tuple[dict[str, Any], ...]] = (
    # ---------------------------------------------------- 大模型音色（支持 9 种情感）
    {"id": "601008", "name": "爱小豪（聊天男声 · 大模型音色）",
     "gender": "男", "scene": "日常对话的男性角色",
     "note": "聊天男声，情感丰富，适合当「儿子/老伴」口吻",
     "rates": (8000, 16000, 24000),
     "emotions": ("neutral", "sad", "happy", "angry", "fear",
                  "sajiao", "amaze", "disgusted", "peaceful")},
    {"id": "601009", "name": "爱小芊（聊天女声 · 大模型音色）",
     "gender": "女", "scene": "日常陪伴首选",
     "note": "聊天女声，情感丰富，默认推荐给老人陪伴",
     "rates": (8000, 16000, 24000),
     "emotions": ("neutral", "sad", "happy", "angry", "fear",
                  "sajiao", "amaze", "disgusted", "peaceful")},
    {"id": "601010", "name": "爱小娇（聊天女声 · 大模型音色）",
     "gender": "女", "scene": "日常陪伴的备选",
     "note": "聊天女声，音色偏甜，适合亲切口吻",
     "rates": (8000, 16000, 24000),
     "emotions": ("neutral", "sad", "happy", "angry", "fear",
                  "sajiao", "amaze", "disgusted", "peaceful")},
    # ---------------------------------------------------- 超自然大模型音色（仅中性）
    {"id": "402001", "name": "云小团（温柔甜美女声 · 超自然）",
     "gender": "女", "scene": "播报与陪伴皆可",
     "note": "温柔甜美，咬字清楚，适合服药提醒等必须听清的播报",
     "rates": (8000, 16000, 24000), "emotions": ("neutral",)},
    {"id": "402002", "name": "云大围（磁性男声 · 超自然）",
     "gender": "男", "scene": "男声播报",
     "note": "磁性男声，稳重，适合家属口吻",
     "rates": (8000, 16000, 24000), "emotions": ("neutral",)},
    {"id": "403000", "name": "云小朵（特色女童声 · 超自然）",
     "gender": "女", "scene": "轻松互动（注意：童声对老人可能显突兀）",
     "note": "特色女童声，适合闲聊与讲故事",
     "rates": (8000, 16000, 24000), "emotions": ("neutral",)},
    {"id": "403008", "name": "云小洋（机灵男童 · 超自然）",
     "gender": "男", "scene": "轻松互动 / 讲故事",
     "note": "机灵男童声，活泼",
     "rates": (8000, 16000, 24000), "emotions": ("neutral",)},
    {"id": "502007", "name": "智小虎（聊天童声 · 超自然）",
     "gender": "男", "scene": "闲聊陪伴",
     "note": "聊天童声，语气轻松",
     "rates": (8000, 16000, 24000), "emotions": ("neutral",)},
)

#: 出厂默认音色：爱小芊。选它而不是男声/童声的理由 —— 老人陪伴场景里
#: 温和女声的接受度最高，且它是**情感最全**的一档（9 种），
#: 后续 F4 表情情绪打通后不需要再换音色。
DEFAULT_VOICE: Final[str] = "601009"

#: 多情感音色可用的情感标签（EmotionCategory）。当前未接入会话情绪层，
#: 先留常量 + 参数位：F4（表情情绪）打通后按情绪传入即可，不必改协议。
EMOTIONS: Final[tuple[str, ...]] = (
    "neutral", "happy", "sad", "angry", "fear", "news", "story", "radio",
    "poetry", "call", "sajiao", "disgusted", "amaze", "peaceful", "exciting",
    "aojiao", "jieshuo",
)

#: 速率倍率 → 腾讯云档位。腾讯云的 Speed 是"档"不是"倍率"（见模块说明）。
_SPEED_GEARS: Final[tuple[tuple[float, float], ...]] = (
    (0.6, -2.0), (0.8, -1.0), (1.0, 0.0), (1.2, 1.0), (1.5, 2.0), (2.5, 6.0),
)


def _speed_gear(ratio: float) -> float:
    """把倍率映射到最近的腾讯云档位。

    ``1.0`` 是绝大多数情况（不传 speed），必须精确映射到 ``0.0`` ——
    若映射成 -0.02 之类的近似值，语速会有轻微但持续的偏差。
    """
    try:
        value = float(ratio)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    return min(_SPEED_GEARS, key=lambda pair: abs(pair[0] - value))[1]


def _voice_type(voice: str, default: str = DEFAULT_VOICE) -> int:
    """``voice`` 字符串 → ``VoiceType`` 整数。

    允许管理员填 ``601009`` / ``"601009"`` / 带空格。填了非数字（如把火山那种
    ``BV001_streaming`` 粘过来）就报可读错误 —— 否则会拼出
    ``"VoiceType": NaN`` 或字符串，腾讯云回一个 InvalidParameterValue.VoiceType，
    完全指不到"你填的是别家的音色 ID"。
    """
    text = (voice or "").strip() or default
    try:
        return int(float(text))
    except (TypeError, ValueError):
        raise RuntimeError(
            f"腾讯云音色 ID 必须是**整数**（如 {DEFAULT_VOICE}），当前填的是 {text!r}。"
            f"如果是火山/百炼的音色 ID（如 BV001_streaming），请先在后台把"
            f"厂商切换回对应那一家 —— 各家的音色 ID 不通用。") from None


def emotion_supported(voice: str, emotion: str) -> bool:
    """该音色是否支持这个情感标签。

    依据 :data:`TENCENT_VOICES` 的 ``emotions`` 字段（来自控制台音色表）：
    大模型音色（601xxx）支持 9 种，超自然音色（402/403/502）**只支持中性**。

    **手填的未知音色一律放行**（返回 True）：我们不知道它支持什么，
    不能替厂商做判断 —— 让请求发出去由厂商回错误码，比我们猜错更诚实。
    """
    if not emotion:
        return False
    for entry in TENCENT_VOICES:
        if entry["id"] == (voice or "").strip():
            return emotion in entry.get("emotions", ())
    return True


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """按句读把文本切成每段 ≤ ``limit`` 字的片段。

    为什么按句读而不是硬切：硬切会把一个词劈成两半（"吃/药"），合成出来
    有断字感；按标点切则每段都是自然的语义单元。超长且无标点的整段
    （罕见：老人说的话不会被接口喂进来，喂进来的是 LLM 回复）才硬切。

    返回的片段**非空且顺序保持**，调用方按序拼接即可。
    """
    body = (text or "").strip()
    if not body:
        return []
    if len(body) <= limit:
        return [body]

    # 句末 + 句中标点都当作可切点；不切断英文小数点之类的情况极少，忽略。
    marks = "。！？；\n!?;\r"
    chunks: list[str] = []
    buf = ""
    for ch in body:
        buf += ch
        if len(buf) >= limit:
            # 超限：回退到 buf 里最后一个标点处切（若有）
            cut = max((buf.rfind(m) for m in marks), default=-1)
            if cut >= limit // 3:
                chunks.append(buf[:cut + 1].strip())
                buf = buf[cut + 1:]
            else:
                chunks.append(buf.strip())
                buf = ""
        elif ch in marks and len(buf) >= limit // 2:
            chunks.append(buf.strip())
            buf = ""
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if c]


def build_body(text: str, voice_type: int, *,
               sample_rate: int = TARGET_SAMPLE_RATE,
               speed_ratio: float = 1.0,
               volume: float = 0.0,
               emotion: str = "",
               emotion_intensity: int = 100,
               session_id: str = "") -> dict[str, Any]:
    """构造 TextToVoice 请求正文（纯函数，供冒烟断言）。

    ``Codec`` 固定 ``pcm``：本项目客户端只吃裸 PCM，选 wav 会多出 44 字节头
    需要剥、选 mp3 要解码 —— 两者都是可避免的中间环节。
    """
    body: dict[str, Any] = {
        "Text": text,
        "SessionId": session_id or str(uuid.uuid4()),
        "VoiceType": voice_type,
        "Codec": "pcm",
        "SampleRate": int(sample_rate),
        "PrimaryLanguage": 1,      # 1=中文
        "ModelType": 1,            # 1=默认模型
        "ProjectId": 0,
    }
    gear = _speed_gear(speed_ratio)
    if gear != 0.0:
        body["Speed"] = gear
    if volume:
        body["Volume"] = float(volume)
    if emotion in EMOTIONS:
        body["EmotionCategory"] = emotion
        body["EmotionIntensity"] = int(emotion_intensity)
    return body


def _decode_audio(resp: dict[str, Any]) -> bytes:
    """从 ``Response.Audio``（base64）取出 PCM 字节。"""
    audio = resp.get("Audio")
    if not audio:
        raise RuntimeError(
            f"腾讯云 TTS 响应里没有 Audio 字段：{str(resp)[:200]}。"
            f"常见原因：Text 为空、音色无权限、或该音色不支持请求的采样率")
    try:
        raw = base64.b64decode(audio)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"腾讯云 TTS 的 Audio 不是合法 base64：{str(audio)[:80]!r}") from exc
    if not raw:
        raise RuntimeError("腾讯云 TTS 返回空音频")
    # Codec=pcm 正常不带容器头；但若服务端忽略了 Codec 回了 wav，
    # 这里剥掉（与火山适配层的防御一致：多剥一次无害，少剥一次是噪音）。
    if raw[:4] == b"RIFF":
        raw = raw[44:]
    if len(raw) % 2:
        raw = raw[:-1]      # 16bit 采样按 2 字节对齐
    return raw


def tts(text: str, voice: str, *, secret_id: str, secret_key: str,
        endpoint: str = "", region: str = "",
        speed: float = 1.0, emotion: str = "",
        timeout: float = 30.0, opener: Any = None,
        sample_rate: int = TARGET_SAMPLE_RATE) -> bytes:
    """一段文字 → 24kHz/mono/S16LE 裸 PCM（多段自动拼接）。

    :param voice: 音色 ID（整数形态的字符串，如 ``"1001"``）。
    :param endpoint: 空则用 ``tts.tencentcloudapi.com``；私有化/代理可覆盖。
    :raises RuntimeError: 任何失败（上层 ``asr_tts`` 转成 VoiceUpstreamError）。
    """
    pieces = split_text(text)
    if not pieces:
        raise RuntimeError("腾讯云 TTS 的文本为空 —— 上层可能把空句子送下来了")

    voice_type = _voice_type(voice)
    # 情感白名单：音色不支持就**静默降级**为不带情感，并在日志里留痕。
    # 为什么不是报错：情感是锦上添花（F4 表情情绪的配套），而主链路是
    # "把话念出来"。因为管理员选了个不支持情感的播报音色就让老人听不到
    # 提醒，代价完全不成比例。
    if emotion and not emotion_supported(voice, emotion):
        log.info("音色 %s 不支持情感 %s，本次不带情感合成（仅大模型音色 "
                 "601xxx 支持多情感）", voice or DEFAULT_VOICE, emotion)
        emotion = ""
    out: list[bytes] = []
    for index, piece in enumerate(pieces):
        body = build_body(piece, voice_type, sample_rate=sample_rate,
                          speed_ratio=speed, emotion=emotion)
        try:
            resp = tc3.call("tts", TENCENT_TTS_ACTION, TENCENT_TTS_VERSION,
                            body, secret_id=secret_id, secret_key=secret_key,
                            endpoint=endpoint, region=region, timeout=timeout,
                            opener=opener)
        except tc3.TencentError as exc:
            # 分段时把"第几段"补进错误信息：否则报错说 Text 超长，
            # 管理员看着自己那句短话完全无法理解。
            extra = f"（第 {index + 1}/{len(pieces)} 段，{len(piece)} 字）" \
                if len(pieces) > 1 else ""
            raise RuntimeError(f"{exc}{extra}") from None
        out.append(_decode_audio(resp))

    if not out:
        raise RuntimeError("腾讯云 TTS 没有产出任何音频")
    return b"".join(out)
