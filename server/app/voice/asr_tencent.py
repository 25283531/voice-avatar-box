"""腾讯云语音识别（ASR · 一句话识别）适配 —— 云 API 3.0 + TC3 签名。

字段契约（依据官方 1093/35646，2026-10-21 查证）
-----------------------------------------------
* ``EngSerViceType``：引擎模型。本平台音频固定 **16kHz/mono/S16LE**（见
  ``asr_tts.asr`` 的入参约定），所以只能选 ``16k_*`` 系列 ——
  选 ``8k_*`` 会把 16k 音频按 8k 解，识别结果是一堆乱字，而接口**照样返回
  200**。默认 ``16k_zh``（中文通用）；老人说方言的场景用 ``16k_zh_dialect``
  （支持 23 种方言，含上海/四川/粤语片区）。
* ``SourceType``：``1`` = 语音数据随请求上传（我们不使用 ``Url`` 方式：
  那要求音频公网可下载，等于把家庭对话录音放到公网）。
* ``VoiceFormat``：``pcm`` —— 与本平台的裸 PCM **逐字节一致**，不需要
  包 wav 头。这是选它的主要理由（换 ``wav`` 只是多 44 字节，但多一处
  可以出错的地方）。
* ``Data``：**base64 编码，且不能带回车换行**（官方原文）。Python 的
  ``base64.b64encode`` 天然不带换行，``base64.encodebytes`` 会 —— 别混用。
* ``DataLen``：**未编码前**的字节数。填错会让服务端少读一段音频，
  表现为"最后几个字识别不出来"，极难自查。
* 硬限制：**时长 ≤ 60s、文件 ≤ 3MB（base64 后）**。超了由本模块提前拦截
  并给出可读报错，而不是把 3MB 的 JSON 发出去换一个
  ``ErrorVoicedataTooLong``。

⚠️ ``Region``：官方文档明确写「本接口不需要传递此参数」。适配器默认不传。

⚠️ 实测结论（2026-10-21，用真实 SecretId/SecretKey 打过）
--------------------------------------------------------
**端到端已验证**：把腾讯云 TTS 合成的音频降到 16kHz 后喂给本接口，
识别结果与原文逐字一致（"老人家，该吃药了。今天天气不错。" →
"老人家，该吃药了，今天天气不错。"，只有标点被规范化）。同时验证了：

* ``VoiceFormat=pcm`` + ``SourceType=1``（base64 直传）真的可用；
* ``DataLen`` 填原始字节数能被正确读取（填错会漏字，这正是 D3/F7
  两条断言要守住的）；
* 冒烟在没有密钥的 CI 上跑结构断言 + 假上游，真实验证留在这里作为记录。
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Final

from . import tc3

log = logging.getLogger("elder.voice.tencent_asr")

TENCENT_ASR_ACTION: Final[str] = "SentenceRecognition"
TENCENT_ASR_VERSION: Final[str] = "2019-06-14"

#: 一句话识别的硬上限（官方）。
MAX_DURATION_SEC: Final[float] = 60.0
MAX_BASE64_BYTES: Final[int] = 3 * 1024 * 1024
#: 本平台音频的采样率（16kHz/mono/S16LE）。用于算时长与校验入参。
SAMPLE_RATE: Final[int] = 16000
SAMPLE_WIDTH: Final[int] = 2

DEFAULT_ENGINE: Final[str] = "16k_zh"

#: 可选的引擎模型。只列 16k 系列 —— 8k 系列的音频格式与本平台不符，
#: 列出来只会让管理员选错（选错的症状是"能识别但全是乱码"）。
ENGINE_CHOICES: Final[tuple[dict[str, str], ...]] = (
    {"id": "16k_zh", "name": "中文普通话（默认）"},
    {"id": "16k_zh_dialect", "name": "中文多方言（23 种，含四川/上海/粤语）"},
    {"id": "16k_zh-PY", "name": "中英粤混合"},
    {"id": "16k_en", "name": "英语"},
    {"id": "16k_yue", "name": "粤语"},
)


def seconds_of(pcm: bytes) -> float:
    """裸 PCM 的时长（秒）。"""
    return len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH) if pcm else 0.0


def build_body(pcm: bytes, engine: str = DEFAULT_ENGINE, *,
               hotwords: str = "", filter_punc: int = 0,
               convert_num: int = 1) -> dict[str, Any]:
    """构造 SentenceRecognition 请求正文（纯函数，供冒烟断言）。

    :param hotwords: 临时热词表，形如 ``"氨氯地平|10,硝苯地平|10"``。
        老人场景里药名、家人称谓是识别率最大的短板 —— 这是官方提供的
        正解（``HotwordList``），比在服务端做后处理替换可靠得多。
    :param convert_num: ``1`` = 阿拉伯数字智能转换（"八点" → "8点"），
        下游 ``parse_when`` 两种都认，保持默认 1 与官方一致。
    """
    body: dict[str, Any] = {
        "EngSerViceType": (engine or DEFAULT_ENGINE).strip(),
        "SourceType": 1,
        "VoiceFormat": "pcm",
        "Data": base64.b64encode(pcm).decode("ascii"),
        "DataLen": len(pcm),
    }
    if hotwords:
        body["HotwordList"] = hotwords
    if filter_punc:
        body["FilterPunc"] = int(filter_punc)
    if convert_num != 1:
        body["ConvertNumMode"] = int(convert_num)
    return body


def _check_limits(pcm: bytes) -> None:
    """提前拦下超限请求，给出人话报错。

    不把超限请求发出去换 ``ErrorVoicedataTooLong``：那个错误码不会说
    "你这段有 78 秒"，而老人/家属更不会知道该说短一点。
    """
    if not pcm:
        raise RuntimeError("腾讯云 ASR 收到的音频为空 —— 上层可能没收到声音")
    duration = seconds_of(pcm)
    if duration > MAX_DURATION_SEC:
        raise RuntimeError(
            f"这一段音频 {duration:.1f}s 超过一句话识别的 {MAX_DURATION_SEC:g}s 上限。"
            f"一句话识别只适合短句；长录音需要走「录音文件识别」接口"
            f"（本平台未接入，会话链路里 VAD 会把话切成短句，正常不会触发）")
    # base64 膨胀 4/3，预算留一点余量
    if len(pcm) * 4 // 3 > MAX_BASE64_BYTES:
        raise RuntimeError(
            f"这一段音频编码后约 {len(pcm) * 4 // 3 / 1024 / 1024:.1f}MB，"
            f"超过一句话识别的 3MB 上限")


def transcribe(pcm: bytes, *, secret_id: str, secret_key: str,
               engine: str = DEFAULT_ENGINE, endpoint: str = "",
               region: str = "", hotwords: str = "",
               timeout: float = 30.0, opener: Any = None) -> str:
    """16kHz/mono/S16LE 裸 PCM → 识别文本。

    :returns: 识别结果（可能为空串 —— 静音/噪声时官方会返回空 Result，
        这与"调用失败"是两件事，调用方不应把它当异常）。
    """
    _check_limits(pcm)
    body = build_body(pcm, engine, hotwords=hotwords)
    resp = tc3.call("asr", TENCENT_ASR_ACTION, TENCENT_ASR_VERSION, body,
                    secret_id=secret_id, secret_key=secret_key,
                    endpoint=endpoint, region=region, timeout=timeout,
                    opener=opener)
    return str(resp.get("Result") or "").strip()
