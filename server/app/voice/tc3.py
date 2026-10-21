"""腾讯云 API 3.0 签名（TC3-HMAC-SHA256）—— TTS 与 ASR 共用的唯一实现。

为什么要单独一个模块，而不是各自写一遍
--------------------------------------
腾讯云语音合成与语音识别是**两个不同域名、不同 Version** 的服务，但**签名算法
完全相同**（TC3-HMAC-SHA256，见官方「公共参数」文档 1073/37989 与 1093/35640）。
若照抄成两份，任何一次算法修正都得改两处 —— 而签名错了的表现是 HTTP 401 与
一句 ``AuthFailure.SignatureFailure``，既不说是哪一步错了、也不区分是哪家的
实现错了。本项目已经吃过「同一事实两份真源」的亏（见 MEMORY.md），所以这里
只留一份，两家服务通过 ``service`` 参数区分。

它长什么样（与火山 / AWS SigV4 的差异，是犯错高发区）
----------------------------------------------------
=====================  ==============================  ==========================
步骤                   腾讯云 TC3                       AWS SigV4（火山）
=====================  ==============================  ==========================
密钥派生第一级          ``"TC3" + SecretKey``（**字符串   ``"AWS4" + SecretKey``
                        拼接后当 HMAC 的 key**）
CredentialScope         ``{date}/{service}/tc3_request``  ``{date}/{region}/{svc}/aws4_request``
签署的 header            ``content-type;host``            含 x-date 等自定义头
=====================  ==============================  ==========================

⚠️ **最容易写错的一步**是密钥派生第一级：腾讯云是把 ``"TC3"`` **前缀拼进
SecretKey 字符串**再当 HMAC 的 key，而不是把 ``"TC3"`` 当消息去 HMAC。写成
后者（``hmac(secret_key, "TC3")``）签名长度对、结构对、错误信息也一样，只有
服务端会说签名不匹配。

⚠️ **date 必须是 UTC 日期**：本项目进程时区是 Asia/Shanghai，用本地日期算
CredentialScope 在 UTC 16:00 之后会整体差一天 —— 表现为「白天能用、晚上全是
401」这种最让人怀疑人生的故障。见 :func:`utc_stamps`。

无法本地实测的部分（如实交代）
------------------------------
签名算法依据官方文档实现，**没有真实 SecretId/SecretKey 无法验证端到端**。
因此本模块把每一步都拆成纯函数（:func:`canonical_request` /
:func:`string_to_sign` / :func:`derive_signing_key` / :func:`authorization`），
让冒烟能用文档给出的推导过程做**确定性断言**，保证算法结构不会静默退化。
真机首次使用请先用管理后台该配置行的「测试连接」。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Final

log = logging.getLogger("elder.voice.tc3")

ALGORITHM: Final[str] = "TC3-HMAC-SHA256"
#: 密钥派生第一级的前缀。⚠️ 是**拼到 SecretKey 前面**，不是拿它当消息。
SECRET_PREFIX: Final[str] = "TC3"
#: CredentialScope 末段固定值（腾讯云把这一步叫 tc3_request，不随服务变）。
CREDENTIAL_TERM: Final[str] = "tc3_request"
DEFAULT_REGION: Final[str] = "ap-guangzhou"
#: 两家服务的域名。endpoint 可被配置行覆盖（私有化/代理场景）。
SERVICE_HOSTS: Final[dict[str, str]] = {
    "tts": "tts.tencentcloudapi.com",
    "asr": "asr.tencentcloudapi.com",
}


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_stamps(now: datetime | None = None) -> tuple[int, str]:
    """返回 ``(unix_timestamp, date_str)``，**两者都基于 UTC**。

    :param now: 便于测试注入；无时区的 datetime 按 UTC 解释（与全库
        ``models.utcnow()`` 的存储口径一致）。

    ⚠️ ``date_str`` 必须是 UTC 日期，理由见模块 docstring。
    """
    if now is None:
        dt = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        dt = now.replace(tzinfo=timezone.utc)
    else:
        dt = now.astimezone(timezone.utc)
    return int(dt.timestamp()), dt.strftime("%Y-%m-%d")


def parse_secret_pair(raw: str) -> tuple[str, str]:
    """把配置行的密钥字段拆成 ``(secret_id, secret_key)``。

    接受 ``SecretId:SecretKey``，英文冒号与**全角冒号**都认 —— 从控制台
    页面复制时经常混进全角冒号，而管理员看不出来自己复制了什么（火山
    适配层踩过同一个坑，见 ``asr_tts._secret_pair``）。

    :returns: 拆不出来时返回 ``("", "")``，由调用方给出「怎么填」的报错。
    """
    text = (raw or "").strip()
    if not text:
        return "", ""
    for sep in (":", "："):
        if sep in text:
            head, _, tail = text.partition(sep)
            return head.strip(), tail.strip()
    # 只填了一段：当成 SecretId，SecretKey 留空 —— 让上层报「需要两段」，
    # 而不是拿半截密钥去签名（那种 401 完全指不到真因上）。
    return text, ""


def canonical_request(method: str, uri: str, query: str,
                      headers: dict[str, str],
                      signed_headers: str, payload: bytes) -> str:
    """拼接规范性请求串（CanonicalRequest）。

    :param uri: 路径，**必须以 / 开头**；腾讯云云 API 3.0 的路径恒为 ``/``。
    :param query: 已 URL 编码的查询串，无参数填 ``""``。
    :param headers: 参与签名的 header（名**小写**），值会被 strip。
        必须与 ``signed_headers`` 一一对应，否则服务端按不同输入算签名。
    :param signed_headers: 分号分隔的小写 header 名，**顺序**要与
        ``headers`` 里的排序一致（本函数内部按键名排序，调用方无需关心）。
    """
    keys = sorted(k.strip().lower() for k in headers)
    canonical_headers = "".join(
        f"{k}:{headers_item(headers, k).strip()}\n" for k in keys)
    return "\n".join([
        method.upper(),
        uri or "/",
        query or "",
        canonical_headers,
        ";".join(keys),
        sha256_hex(payload),
    ])


def headers_item(headers: dict[str, str], lower_key: str) -> str:
    """大小写不敏感地取 header 值（调用方可能传 ``Content-Type``）。"""
    for k, v in headers.items():
        if k.strip().lower() == lower_key:
            return v
    return ""


def string_to_sign(timestamp: int, credential_scope: str,
                   canonical: str) -> str:
    """拼接待签名字符串。

    ``{ALGORITHM}\\n{timestamp}\\n{scope}\\n{sha256(canonical)}``
    """
    return "\n".join([ALGORITHM, str(timestamp), credential_scope,
                      sha256_hex(canonical.encode("utf-8"))])


def derive_signing_key(secret_key: str, date: str, service: str) -> bytes:
    """派生签名密钥：``SecretDate → SecretService → SecretSigning``。

    ⚠️ 第一级的 key 是 ``"TC3" + SecretKey``（字符串拼接），不是
    ``hmac(secret_key, "TC3")``。写反了结构完全一样、签名也只是"不匹配"。
    """
    k_date = _hmac((SECRET_PREFIX + secret_key).encode("utf-8"), date)
    k_service = _hmac(k_date, service)
    return _hmac(k_service, CREDENTIAL_TERM)


def authorization(secret_id: str, secret_key: str, *, timestamp: int,
                  date: str, service: str, canonical: str,
                  signed: dict[str, str]) -> tuple[str, str]:
    """生成 ``(Authorization 头的值, SignedHeaders 串)``。

    :param signed: 参与签名的 header（名大小写不限）。**必须与实际发送的
        header 一一对应** —— 这一条由 :func:`call` 保证：它先算签名、再用
        同一个 dict 发请求，不给"签一套、发另一套"留机会。

    ⚠️ 签名链在这里**只走一遍**。`call` 不许内联一份自己的
    canonical→sign 流程：两份实现会在某次修正时只改一处，表现为
    「TTS 能通、ASR 全 401」，而且错误信息一模一样，查起来毫无抓手。
    """
    signed_headers = ";".join(sorted(k.strip().lower() for k in signed))
    scope = f"{date}/{service}/{CREDENTIAL_TERM}"
    to_sign = string_to_sign(timestamp, scope, canonical)
    signing_key = derive_signing_key(secret_key, date, service)
    signature = hmac.new(
        signing_key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return (
        f"{ALGORITHM} Credential={secret_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}",
        signed_headers,
    )


class TencentError(RuntimeError):
    """腾讯云调用失败（网络 / 鉴权 / 业务错误码）。

    刻意不继承 :class:`app.voice.asr_tts.VoiceUpstreamError`：那个类在
    ``asr_tts`` 里定义，反向 import 会形成环。上层用 ``except Exception``
    兜住并转成自己的错误类型（与火山适配层的做法一致）。
    """


def call(service: str, action: str, version: str, payload: dict[str, Any], *,
         secret_id: str, secret_key: str,
         endpoint: str = "", region: str = "",
         timeout: float = 30.0,
         opener: Any = None) -> dict[str, Any]:
    """发一次云 API 3.0 请求，返回 ``Response`` 段。

    :param service: ``tts`` / ``asr``，同时用于域名与 CredentialScope。
    :param endpoint: 完整 URL（可带协议与路径），空则取 :data:`SERVICE_HOSTS`。
    :param region: 空则用 :data:`DEFAULT_REGION`。⚠️ 一句话识别文档明确说
        "本接口不需要传递此参数"，传了也不会错，所以统一传、由各适配器决定
        是否写进 header。
    :raises TencentError: 任何失败（含业务错误码），文案里带自查提示。
    """
    if not secret_id or not secret_key:
        raise TencentError(
            "腾讯云需要 SecretId 与 SecretKey 两段（在本平台「密钥」字段填 "
            "SecretId:SecretKey，用英文冒号分隔）。"
            "只有一段会被拒 —— 别把 SecretId 当 API Key 用。")

    host = SERVICE_HOSTS.get(service, "")
    if endpoint:
        parts = urllib.parse.urlsplit(endpoint)
        # 容错：管理员可能只填域名（没有协议），也可能填到某个路径上。
        base = endpoint if parts.scheme else f"https://{endpoint}"
        parts = urllib.parse.urlsplit(base)
        host = parts.netloc or host
        uri = parts.path or "/"
    else:
        base = f"https://{host}"
        uri = "/"

    timestamp, date = utc_stamps()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    signed = {"content-type": "application/json; charset=utf-8", "host": host}
    canonical = canonical_request("POST", uri, "", signed, "", body)
    auth, _signed_headers = authorization(
        secret_id, secret_key, timestamp=timestamp, date=date,
        service=service, canonical=canonical, signed=signed)

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Host": host,
        "Authorization": auth,
        "X-TC-Action": action,
        "X-TC-Version": version,
        # ⚠️ X-TC-Timestamp 必须与签名里的 timestamp 同一个值。
        "X-TC-Timestamp": str(timestamp),
    }
    if region:
        headers["X-TC-Region"] = region

    url = base.rstrip("/") + ("" if uri == "/" else uri)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    send = opener or urllib.request.build_opener()

    try:
        with send.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        if exc.code in (401, 403):
            raise TencentError(
                f"腾讯云鉴权失败（HTTP {exc.code}）：{detail or '无返回体'}。"
                f"自查：① SecretId/SecretKey 是否两段都填对"
                f"② 签名日期必须用 UTC（本进程时区 Asia/Shanghai，"
                f"本地日期在 UTC 16:00 后会差一天）"
                f"③ 该服务是否已在控制台开通") from None
        raise TencentError(
            f"腾讯云请求被拒（HTTP {exc.code}）：{detail}") from None
    except urllib.error.URLError as exc:
        raise TencentError(f"腾讯云网络异常：{exc}") from None
    except (TimeoutError, OSError) as exc:
        raise TencentError(
            f"腾讯云调用超时（{timeout:g}s 未返回）：{host}") from exc

    return _unwrap(raw, action, host)


def _unwrap(raw: bytes, action: str, host: str) -> dict[str, Any]:
    """剥掉 ``{"Response": {...}}`` 外壳，并把 ``Error`` 段转成异常。

    腾讯云的**业务错误也是 HTTP 200**，错误信息在 ``Response.Error`` 里。
    不判这一段，上层会拿到一个没有关键字段的 dict，报出
    「响应里没有 Audio」这种完全指不到真因的消息（火山适配层踩过同款）。
    """
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise TencentError(
            f"腾讯云 {action} 返回不是合法 JSON（endpoint={host}）："
            f"{raw[:200]!r}") from exc
    if not isinstance(obj, dict):
        raise TencentError(f"腾讯云 {action} 返回体不是对象：{raw[:200]!r}")

    resp = obj.get("Response")
    if not isinstance(resp, dict):
        raise TencentError(f"腾讯云 {action} 响应缺少 Response 段：{raw[:200]!r}")

    err = resp.get("Error")
    if isinstance(err, dict):
        code = err.get("Code") or ""
        msg = err.get("Message") or ""
        raise TencentError(
            f"腾讯云 {action} 返回错误 {code}：{msg}。{_HINTS.get(code, '')}".strip())
    return resp


#: 常见业务错误码的定向提示。只收「看错误码猜不到真因」的那几个。
_HINTS: Final[dict[str, str]] = {
    "FailedOperation.ServiceIsolate": "账号欠费已停服，请先充值。",
    "FailedOperation.UserNotRegistered": "该服务未开通，请到控制台开通。",
    "FailedOperation.UserHasNoAmount": "资源包已耗尽，请购买资源包或开后付费。",
    "FailedOperation.UserHasNoFreeAmount": "免费额度已用尽。",
    "UnsupportedOperation.NoFreeAccount": "免费资源包已用尽。",
    "UnsupportedOperation.PkgExhausted": "资源包余量已用尽。",
    "UnsupportedOperation.AccountArrears": "账号欠费。",
    "UnsupportedOperation.ServerNotOpen": "服务未开通。",
    "AuthFailure.InvalidAuthorization": "签名无效：多为 SecretId/SecretKey 或 UTC 日期不对。",
    "InvalidParameterValue.VoiceType": "音色 ID 非法（腾讯云 VoiceType 是**整数**）。",
    "InvalidParameterValue.Codec": "Codec 非法，本平台固定用 pcm。",
    "InvalidParameterValue.SampleRate": "采样率非法；该音色可能不支持 24000（多数只到 16000）。",
    "InvalidParameterValue.TextEmpty": "待合成文本为空。",
    "UnsupportedOperation.TextTooLong": "文本超长：中文上限 150 字，本平台已自动分段，"
                                        "若仍报此错请检查分段参数。",
    "InvalidParameterValue.ErrorInvalidVoiceFormat": "音频格式不支持。",
    "InvalidParameterValue.ErrorVoicedataTooLong": "音频超过 60s 上限。",
}
