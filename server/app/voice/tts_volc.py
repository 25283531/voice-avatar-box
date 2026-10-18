"""火山引擎（豆包语音）大模型 TTS 适配 —— 私有协议，签名 + 自定义报文。

为什么要单独一个模块
--------------------
火山引擎的 TTS **不是 OpenAI 兼容**：报文形状、鉴权方式、音频封装三者都与
``/audio/speech`` 不同：

| 维度 | OpenAI 兼容（阿里云百炼等） | 火山引擎 |
| --- | --- | --- |
| 端点 | ``{endpoint}/audio/speech`` | ``{endpoint}/api/v1/tts`` |
| 鉴权 | ``Authorization: Bearer <key>`` | ``X-Api-App-Key`` + HMAC-SHA256 签名 |
| 正文 | ``{model, input, voice, response_format}`` | ``{app:{appid,token,cluster}, user:{uid}, audio:{voice_type, encoding, sample_rate}, request:{text}}`` |
| 音频 | 直接是音频字节 | **JSON 包着 base64** |

不写这个适配层，管理员即使把 endpoint/key 都填对，火山这条配置行也会失败；
而错误信息会是「返回不是合法 JSON」，完全指不到真因上。

⚠️ 关于版本（2026-10-18 实测与查证）
------------------------------------
本模块按 **V3 大模型语音合成**（``/api/v3/tts``）实现，因为它支持
**24kHz + pcm**（正是本项目客户端要的格式，见 ``asr_tts.TTS_SAMPLE_RATE``）。
旧版 ``/api/v1/tts`` 也可用但音色与编码选项少一档。

鉴权用 **V4 派生签名**（火山 OpenAPI 通用规范，docs/6638/173358）：
``kDate → kRegion → kService → kSigning`` 四级派生，再对 StringToSign 取 HMAC。
这是最容易写错的一步 —— 直接用 Secret 对 StringToSign 取一次 HMAC 会被拒。

⚠️ 无法本地实测的部分（请如实看待）
------------------------------------
签名算法与报文结构依据官方文档实现，但**签名正确性没有真实 AK/SK 无法验证**。
因此本模块把签名拆成纯函数 :func:`derive_signing_key` / :func:`build_authorization`，
让冒烟能用**官方文档里的推导过程**做确定性断言（见
``tools/smoke_voice_catalog.py`` 的 F 组），至少保证算法结构不会退化。

⚠️ 音色 ID 与「按年付费」
------------------------
火山大模型音色的 ``voice_type`` 是形如 ``zh_female_qingxin-essence`` 的字符串，
且**音色槽位单独计费**（约 150 元/年/音色）。所以这里预置的音色是
**公版免费音色**，付费专属音色需要管理员在控制台开通后自行填写 ID。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Final

log = logging.getLogger("elder.voice.volc")

# 火山 TTS 的默认接入点。可被 ai_providers 的 endpoint 覆盖。
VOLC_DEFAULT_ENDPOINT: Final[str] = "https://openspeech.bytedance.com"
VOLC_TTS_PATH: Final[str] = "/api/v3/tts"
VOLC_RESOURCE_ID: Final[str] = "volc.tts_async"  # 同步单次合成

VOLC_REGION: Final[str] = "cn-beijing"
VOLC_SERVICE: Final[str] = "openspeech"
VOLC_ALGORITHM: Final[str] = "HMAC-SHA256"

# 公版免费音色（不占音色槽位）。付费音色需管理员自行填 ID。
# 口径见火山官方「传统语音合成音色列表」docs/6561/97465。
VOLC_VOICES: Final[tuple[dict[str, str], ...]] = (
    {"id": "BV001_streaming", "name": "小艺（女声· 亲和）",
     "note": "公版免费音色，日常陪伴首选"},
    {"id": "BV002_streaming", "name": "小艺（女声· 温柔）",
     "note": "语速偏慢，适合老人"},
    {"id": "BV005_streaming", "name": "小艺（男声· 沉稳）",
     "note": "男声播报，咬字清楚"},
    {"id": "BV700_streaming", "name": "小艺（男声· 新闻播报）",
     "note": "播报腔，适合必须听清的提醒"},
    {"id": "BV701_streaming", "name": "小艺（女声· 播报）",
     "note": "播报腔女声"},
)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def derive_signing_key(secret_key: str, short_date: str,
                       region: str = VOLC_REGION,
                       service: str = VOLC_SERVICE) -> bytes:
    """从 SK 派生签名密钥（四级派生，火山 V4 规范）。

    ``kSecret → kDate → kRegion → kService → kSigning``

    ⚠️ 这一步**不能省**：火山的签名不是「用 SK 直接对 StringToSign 取一次
    HMAC」，而是先派生再取。少派生一级，签名长度/内容都会不对，服务端返回
    403 且错误文案不提示「签名算法不对」——极难自查。
    """
    k_date = _hmac(secret_key.encode("utf-8"), short_date)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "request")


def build_authorization(access_key: str, secret_key: str,
                        short_date: str, request_date: str,
                        signed_headers: str, canonical_request: str,
                        region: str = VOLC_REGION,
                        service: str = VOLC_SERVICE) -> str:
    """构造 ``Authorization`` 头（AWS SigV4 风格）。

    :param short_date:   ``YYYYMMDD``（UTC）
    :param request_date: ``YYYYMMDDTHHMMSSZ``（UTC）
    :param signed_headers: 参与签名的 header 名，**分号分隔且必须小写**且
        与实际发送的 header 一致，否则服务端按不同输入算签名，必然不匹配。
    """
    signing_key = derive_signing_key(secret_key, short_date, region, service)
    credential_scope = f"{short_date}/{region}/{service}/request"
    canonical_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = "\n".join([
        VOLC_ALGORITHM, request_date, credential_scope, canonical_hash,
    ])
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return (
        f"{VOLC_ALGORITHM} Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def utc_stamps(now: datetime | None = None) -> tuple[str, str]:
    """返回 ``(short_date, request_date)``，均为 UTC。

    ⚠️ **必须是 UTC**：本机时区是 Asia/Shanghai。用本地时间算出的
    short_date 与服务端差 8 小时，签名的 CredentialScope 直接对不上，
    报 403 —— 而错误文案不会说「你时区用错了」。
    """
    dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return dt.strftime("%Y%m%d"), dt.strftime("%Y%m%dT%H%M%SZ")


def build_body(text: str, voice_type: str, *,
               encoding: str = "pcm", sample_rate: int = 24000,
               speed_ratio: float = 1.0,
               app_id: str = "", token: str = "", cluster: str = "",
               uid: str = "elder-voice") -> dict[str, Any]:
    """构造 V3 请求正文。

    ⚠️ ``encoding`` 用 ``pcm`` 而不是 ``mp3``：本项目客户端要裸 PCM
    （见 ``asr_tts.TTS_RESPONSE_FORMAT``）。火山支持 24k pcm 直出，
    所以**不需要**任何转码 —— 这与 Edge 的 mp3 必须解码形成对比。
    """
    audio: dict[str, Any] = {
        "voice_type": voice_type,
        "encoding": encoding,
        "sample_rate": sample_rate,
    }
    if speed_ratio and speed_ratio != 1.0:
        audio["speed_ratio"] = float(speed_ratio)
    body: dict[str, Any] = {
        "user": {"uid": uid},
        "audio": audio,
        "request": {"text": text, "text_type": "plain"},
    }
    # app 段只在给了 AppID 时才带 —— 火山同时存在「AK/SK 签名」与
    # 「AppID/Token」两套鉴权。带空的 app 段会让签名与实际请求不一致。
    if app_id:
        body["app"] = {"appid": app_id, "token": token or "", "cluster": cluster}
    return body


def _decode_response(raw: bytes, endpoint: str) -> bytes:
    """从 JSON 响应里取出音频字节，并做分诊。

    火山把音频放在 ``data`` 字段，**base64 编码**。直接 return raw 会让
    mp3/JSON 字节流被当成 PCM 编码 —— 老人听到的是噪音，而协议层无报错。
    """
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"火山 TTS 返回不是合法 JSON（endpoint 或鉴权可能不对）："
            f"{raw[:200]!r} @ {endpoint}") from exc
    if not isinstance(obj, dict):
        raise RuntimeError(f"火山 TTS 返回体不是对象：{raw[:200]!r}")

    code = obj.get("code")
    # 成功时 code 常见为 3000；也有实现成功不返回 code。
    if code is not None and str(code) not in ("3000", "0", ""):
        msg = obj.get("message") or obj.get("msg") or ""
        raise RuntimeError(
            f"火山 TTS 返回错误 code={code} message={msg!r}。"
            f"常见原因：AK/SK 不匹配、签名日期时区错（必须 UTC）、"
            f"资源 ID 未开通、音色 ID 无权限（付费音色需单独开通）")

    data = obj.get("data")
    if not data:
        raise RuntimeError(f"火山 TTS 响应里没有 data 字段：{obj!r}")
    try:
        return base64.b64decode(data)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"火山 TTS 的 data 不是合法 base64：{str(data)[:80]!r}") from exc


def tts(text: str, voice_type: str, *, access_key: str, secret_key: str,
        endpoint: str = VOLC_DEFAULT_ENDPOINT,
        resource_id: str = VOLC_RESOURCE_ID,
        speed_ratio: float = 1.0, timeout: float = 30.0,
        proxy_opener: Any = None) -> bytes:
    """一句话 → 24kHz/mono/S16LE 裸 PCM。

    ⚠️ 本函数**无法在本机实测**（需真实 AK/SK），报文与签名依据官方文档实现，
    算法结构由 ``tools/smoke_voice_catalog.py`` 的确定性断言看守。
    真机首次使用请先用后台的「测试连接」按钮验证。
    """
    if not (access_key or "").strip() or not (secret_key or "").strip():
        raise RuntimeError(
            "火山引擎需要 AK 与 SK 两者（在本平台「密钥」字段填 "
            "AK:SK，用英文冒号分隔）")

    short_date, request_date = utc_stamps()
    body = build_body(text, voice_type, encoding="pcm", sample_rate=24000,
                      speed_ratio=speed_ratio)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

    # CanonicalRequest 只需覆盖方法、路径、参与签名的 header、正文哈希。
    # ⚠️ signed_headers 必须**全小写**且与下面实际发送的 header 名对应。
    signed_headers = "content-type;host;x-api-access-key;x-api-resource-id;x-date"
    canonical_request = "\n".join([
        "POST", VOLC_TTS_PATH, "",
        signed_headers,
        ";".join([
            "content-type:application/json",
            f"host:{urllib.parse.urlsplit(endpoint).netloc}",
            f"x-api-access-key:{access_key}",
            f"x-api-resource-id:{resource_id}",
            f"x-date:{request_date}",
        ]),
        hashlib.sha256(payload).hexdigest(),
    ])
    auth = build_authorization(access_key, secret_key, short_date,
                                request_date, signed_headers, canonical_request)

    url = endpoint.rstrip("/") + VOLC_TTS_PATH
    headers = {
        "Content-Type": "application/json",
        "X-Date": request_date,
        "X-Api-Access-Key": access_key,
        "X-Api-Resource-Id": resource_id,
        "Authorization": auth,
    }
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    opener = proxy_opener or urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        if exc.code in (401, 403):
            raise RuntimeError(
                f"火山 TTS 鉴权失败（HTTP {exc.code}）：{detail or '无返回体'}。"
                f"自查：① AK/SK 是否配错位② 签名日期是否用 UTC"
                f"（本机时区 Asia/Shanghai 直接算会差 8 小时）"
                f"③ 资源 {resource_id} 是否已开通") from None
        raise RuntimeError(f"火山 TTS 请求被拒（HTTP {exc.code}）：{detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"火山 TTS 网络异常：{exc}") from None

    audio = _decode_response(raw, endpoint)
    if not audio:
        raise RuntimeError(f"火山 TTS 返回空音频：{endpoint}")
    # 万一编码被忽略回了 wav 容器，剥掉 44 字节头（与 asr_tts 的防御一致）
    if audio[:4] == b"RIFF":
        audio = audio[44:]
    if len(audio) % 2:
        audio = audio[:-1]
    return audio