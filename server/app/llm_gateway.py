"""统一的大模型调用层（Model Gateway）。

为什么要有这一层
----------------
需求是「平台要为不同类别的大模型各配一个接入点」。配出来容易，**用起来**麻烦：
上层模块（对话、记忆提取、意图识别、提醒解析）应该说"我要 chat / embed / light"，
而不是各自去查配了哪家厂商、再各自拼一遍那家的 HTTP 报文。

所以这里提供三个入口：

    chat(usage, messages)  按用途路由到某条配置的对话模型，返回文本
    embed(text)            调向量模型，返回 float 列表
    rerank(query, docs)    调重排模型，返回打分列表

它们背后的三重兜底
------------------
1. **用途 -> kind 候选链**（model_catalog.USAGE_ROUTES）。``chat`` 优先找
   ``llm_main``，没有就退回旧值 ``llm``（存量老配置不会被新版本抛弃）；
2. **同一 kind 多条配置按 priority 排序**，逐条尝试，一条挂了自动换下一条；
3. **同一条配置有限重试**（max_retries），超时 / 429 / 5xx 才重试，401 不重试。

错误处理为什么分那么多类
------------------------
笼统抛 500 是最省事也最没用的做法。「平台还没配模型」「密钥错了」「被限流」
「厂商 5xx」这四类在上层眼里完全是四件事：

    no_config     -> 运维去「AI 接入」页面加一条，重试没用
    auth_failed   -> 密钥失效 / 额度耗尽，换 key，重试没用
    rate_limited  -> 限流，等一会儿或换备用渠道，值得重试
    upstream_error-> 厂商侧故障，换备用配置，值得重试
    timeout       -> 多半是本地小模型慢，建议调大该配置的超时秒数，值得重试
    bad_response  -> 返回不是约定的 JSON 形状（填错 endpoint 最常见）
    bad_config    -> 配置本身缺字段（endpoint / model 没填）
    unsupported   -> 该厂商/协议不支持这个能力（如 Anthropic 没有 embeddings）
    bad_usage     -> usage 拼错了，纯调用方 bug

上层拿到 ``error_code`` 就能给出可执行的提示，而不是让用户对着一个 500 发呆。

依赖口径
--------
只用标准库 ``urllib.request``，**刻意不引入 requests / httpx / openai sdk**：
本项目的部署形态是单容器 SQLite/mariadb，少一个依赖少一份 CVE 面和一个构建体积，
而三家报文形状加起来不过几十行 JSON 拼装，犯不着拉一堆 SDK 进来。

每条配置的 endpoint 需要填到**带版本号的基址**，如
``https://api.deepseek.com/v1``、``http://127.0.0.1:11434/v1``（Ollama 也有
OpenAI 兼容端点）。这里不做「自动补 /v1」的猜测：补错了更难排查，宁可在报错里说清楚。
"""
from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from ipaddress import ip_address, ip_network
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .model_catalog import (
    API_STYLES,
    DEFAULT_API_STYLE,
    PROTOCOL_ANTHROPIC,
    PROTOCOL_GEMINI,
    PROTOCOL_OLLAMA,
    PROTOCOL_OPENAI,
    PROTOCOL_WITH_EMBEDDINGS,
    PROTOCOL_WITH_RERANK,
    USAGE_ROUTES,
    describe_kinds,
    protocol_of,
)

log = logging.getLogger("elder.llm_gateway")

# 配置里没填（或老库列是 NULL）时的兜底值。
# 这里刻意用 ``or 默认值`` 兜一层：本项目没有 Alembic，老库靠 schema_patch 的
# ALTER TABLE ADD COLUMN 补列，补出来的旧行取到的可能是 None 而不是 30，
# 不能假设「数据库一定有值」。
DEFAULT_TIMEOUT_SEC = 30
DEFAULT_MAX_RETRIES = 1
DEFAULT_TEMPERATURE = 0.7

# 换下一条配置之间的退避上限（秒），避免打爆本来就在喘的厂商。
_RETRY_BACKOFF_CAP = 2.0


# ================================================================ 错误类型
class GatewayError(Exception):
    """调用层异常基类。子类只需覆盖三个类属性与默认话术。"""

    code = "upstream_error"
    http_status = 502
    retryable = False

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.message = message
        if retryable is not None:
            self.retryable = retryable
        # 调用链路（每条配置每次尝试）由 _run 回填，方便排查「到底试了谁」
        self.attempts: list[dict[str, Any]] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.code,
            "message": self.message,
            "status": self.http_status,
            "attempts": self.attempts,
        }


class UsageError(GatewayError):
    """usage 拼错（纯调用方 bug）。"""

    code = "bad_usage"
    http_status = 400


class ConfigMissingError(GatewayError):
    """某个用途一条可用配置都没有：「请在『AI 接入』里配置 XXX」。"""

    code = "no_config"
    http_status = 503


class BadConfigError(GatewayError):
    """配置本身不完整：没填 endpoint / model / api_style 不认识。"""

    code = "bad_config"
    http_status = 400


class AuthError(GatewayError):
    """401/403：密钥错、无权访问、或账号余额耗尽（厂商常在 403 里给这个信号）。"""

    code = "auth_failed"
    http_status = 401


class RateLimitError(GatewayError):
    """429：限流，退避后可重试。"""

    code = "rate_limited"
    http_status = 429
    retryable = True


class UpstreamError(GatewayError):
    """厂商侧故障 5xx / 连接不上 / 被拒的 4xx。"""

    code = "upstream_error"
    http_status = 502
    retryable = True


class ModelTimeoutError(GatewayError):
    """超时：多半是本地小模型慢，建议调大该配置的「超时秒数」。"""

    code = "timeout"
    http_status = 504
    retryable = True


class BadResponseError(GatewayError):
    """返回体不是约定的 JSON 形状。"""

    code = "bad_response"
    http_status = 502


class UnsupportedCapabilityError(GatewayError):
    """该协议不支持这个能力（如 Anthropic 没有 embeddings 接口）。"""

    code = "unsupported"
    http_status = 400


# ================================================================ 返回结构
@dataclass
class Attempt:
    """一次尝试的记录（成功也记），用于排障与「测试连接」页展示。"""

    provider_pk: int
    name: str
    kind: str
    model: str
    endpoint: str
    attempt: int          # 第几次尝试（1 起）
    ok: bool
    elapsed_ms: int = 0
    error_code: str = ""
    message: str = ""


@dataclass
class ChatResult:
    text: str
    provider_pk: int
    provider_name: str
    kind: str
    model: str
    usage: str
    finish_reason: str = ""
    elapsed_ms: int = 0
    attempts: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class VectorResult:
    vectors: list[list[float]]
    provider_pk: int
    provider_name: str
    kind: str
    model: str
    usage: str
    dimensions: int = 0
    elapsed_ms: int = 0
    attempts: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RankResult:
    """按输入 docs 顺序排列的分数（越大越相关）。"""

    scores: list[float]
    provider_pk: int
    provider_name: str
    kind: str
    model: str
    usage: str
    elapsed_ms: int = 0
    attempts: list[dict[str, Any]] = field(default_factory=list)


# ================================================================ 配置解析
def _clean_kwargs(payload: Any) -> Any:
    """递归去掉 None 值：厂商 API 对显式 null 很敏感（有的直接 400）。"""
    if isinstance(payload, dict):
        return {k: _clean_kwargs(v) for k, v in payload.items() if v is not None}
    if isinstance(payload, list):
        return [_clean_kwargs(v) for v in payload]
    return payload


def resolve_plan(db: Session, usage: str) -> list[tuple[str, models.AIProvider]]:
    """列出某用途可用的全部配置，顺序即最终调用顺序。

    返回 [(kind, row)]：先按 usage 的 kind 候选链（如 llm_main -> llm）分组，
    组内按 priority 升序、id 升序。调用方按顺序逐个兜底即可。
    """
    if usage not in USAGE_ROUTES:
        raise UsageError(f"未知用途 {usage!r}，可选：{', '.join(USAGE_ROUTES)}")

    rows: list[tuple[str, models.AIProvider]] = []
    for kind in USAGE_ROUTES[usage]:
        found = db.scalars(
            select(models.AIProvider)
            .where(models.AIProvider.kind == kind, models.AIProvider.enabled.is_(True))
            .order_by(models.AIProvider.priority, models.AIProvider.id)
        ).all()
        rows.extend((kind, r) for r in found)
    return rows


def resolve_row(db: Session, usage: str) -> models.AIProvider | None:
    """某用途「最终会落到哪条配置」——用于 /api/providers/resolve 展示与设备下发。"""
    plan = resolve_plan(db, usage)
    return plan[0][1] if plan else None


def _cfg_params(row: models.AIProvider) -> dict[str, Any]:
    """取 params 字典。数据库里可能被写成字符串/列表，这里做一次防御。"""
    p = getattr(row, "params", None)
    return p if isinstance(p, dict) else {}


def _num(row: models.AIProvider, key: str, default: float) -> float:
    """从 params 里取数值，取不到或类型不对就回落 default。"""
    v = _cfg_params(row).get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _protocol(row: models.AIProvider) -> str:
    style = (getattr(row, "api_style", "") or "").strip() or DEFAULT_API_STYLE
    return protocol_of(style)


def _timeout_of(row: models.AIProvider, override: float | None) -> float:
    """超时：调用方临时覆盖优先，否则取配置值，都没有就用默认值。"""
    if override:
        return float(override)
    return float(getattr(row, "timeout_sec", None) or DEFAULT_TIMEOUT_SEC)


def _retries_of(row: models.AIProvider) -> int:
    v = getattr(row, "max_retries", None)
    if v is None:
        return DEFAULT_MAX_RETRIES
    try:
        return max(0, min(5, int(v)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_RETRIES


# ================================================================ 上下文裁剪
def _estimate_tokens(text: Any) -> int:
    """粗略估算 token 数。

    中文一个字通常 0.6~1 个 token，英文一个词 1~1.3 个 token；这里统一按
    「2 个字符约 1 个 token」保守估计。裁剪只需要「别超太多」，不需要精确
    tokenizer——真要精确得把 tiktoken 拖进来，为一个裁剪逻辑不值得。
    """
    if isinstance(text, str):
        n = len(text)
    elif isinstance(text, list):
        n = sum(len(str(p.get("text", ""))) for p in text if isinstance(p, dict)) + 4 * len(text)
    else:
        n = len(str(text or ""))
    return max(1, n // 2)


def trim_messages(messages: list[dict[str, Any]], context_window: int,
                  reserve_tokens: int = 0) -> list[dict[str, Any]]:
    """按上下文上限裁剪历史。

    裁剪策略（刻意保守）：
      * system 消息**永远保留**——人格丢了比少聊两句严重得多；
      * 其余从**最早**的开始丢，保证最近一轮对话完整；
      * 无论如何至少留下最后一条消息，宁可超限被厂商报上下文错，也不要发出空请求；
      * reserve_tokens 留给「回复 + 提示词模板」，通常按 max_tokens 传入。
    """
    window = int(context_window or 0)
    if window <= 0:
        return list(messages)

    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    budget = window - reserve_tokens - sum(_estimate_tokens(m.get("content")) for m in system)
    if budget <= 0:
        # 人格本身就超过上下文了，再裁也无济于事，原样返回让厂商给出明确报错
        return list(messages)

    kept: list[dict[str, Any]] = []
    used = 0
    for m in reversed(rest):
        cost = _estimate_tokens(m.get("content")) + 4
        if kept and used + cost > budget:
            break
        used += cost
        kept.append(m)
    if not kept and rest:      # 至少保住最后一条
        kept.append(rest[-1])
    kept.reverse()
    return system + kept


# ================================================================ HTTP 层
# 本机的 Ollama / vLLM / Xinference 部署在同一台机器上是很常见的形态，
# 而这些环境里往往又全局配了 HTTP_PROXY —— urllib 默认会把**所有**请求
# （含 127.0.0.1:11434）丢给代理，代理自然到不了内网，最终表现成莫名其妙的
# 502 / 连接失败。所以本地地址与 RFC1918 私网地址一律绕过代理；
# 公网域名仍然走代理，该绕境的那部分不受影响。
_LOCAL_NETS = (
    ip_network("127.0.0.0/8"),      # 本机 loopback
    ip_network("::1/128"),
    ip_network("10.0.0.0/8"),       # 家庭/内网部署的另一台推理机
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
)


def _bypass_proxy(url: str) -> bool:
    """判断该地址是否应该绕过系统代理。"""
    host = (urllib.parse.urlsplit(url).hostname or "").strip("[]")
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        ip = ip_address(host)
    except ValueError:
        return False      # 域名保持走代理（云厂商 API 可能确实需要它）
    return any(ip in net for net in _LOCAL_NETS)


def _open(url: str, req: urllib.request.Request, timeout: float):
    """按是否需要代理打开连接。"""
    if _bypass_proxy(url):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = urllib.request.build_opener()
    return opener.open(req, timeout=timeout)
def _wrap_urlerror(exc: urllib.error.URLError, endpoint: str, timeout: float) -> GatewayError:
    """URLError 里塞了所有传输期异常，这里按 reason 精确分诊。"""
    reason = getattr(exc, "reason", None)
    msg = str(reason or exc)
    low = msg.lower()

    if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in low:
        return ModelTimeoutError(
            f"调用超时（{timeout:g}s 未返回）：{endpoint}。"
            f"本地小模型首次推理往往较慢，可把该配置的「超时秒数」调大"
        )
    if isinstance(reason, ConnectionRefusedError) or "refused" in low:
        return UpstreamError(f"服务地址不可达（连接被拒绝）：{endpoint}，请检查 endpoint 与服务是否存活")
    if "name or service not known" in low or "nodename nor servname" in low or "getaddrinfo" in low:
        return UpstreamError(f"服务地址无法解析（DNS 失败）：{endpoint}")
    return UpstreamError(f"网络异常：{msg}")


def _http_error(status: int, detail: str, row: models.AIProvider) -> GatewayError:
    """把厂商的 HTTP 状态码翻译成调用层的错误类型。"""
    brief = (detail or "").strip().splitlines()[0][:200] if detail else ""
    short = f"（HTTP {status}）" + (f"：{brief}" if brief else "，厂商未给出原因")
    if status in (401, 403):
        return AuthError(f"密钥无效或无访问权{short}；若为云厂商，也可能是账号余额/额度已用尽")
    if status == 429:
        return RateLimitError(f"触发厂商限流{short}，已退避重试或切换备用配置")
    if status >= 500:
        return UpstreamError(f"厂商服务侧异常{short}", retryable=True)
    # 其余 4xx（400 参数错、404 地址错、422 ...）重试一万次也是同样的错，别浪费时间
    return UpstreamError(f"请求被厂商拒绝{short}；常见原因是 endpoint/模型名填错", retryable=False)


def _http_post(row: models.AIProvider, path: str, body: dict[str, Any],
               timeout: float) -> tuple[int, str]:
    """发一次 POST，返回 (状态码, 原始响应文本)。所有异常统一转成 GatewayError。"""
    protocol = _protocol(row)
    endpoint = (getattr(row, "endpoint", "") or "").strip().rstrip("/")
    if not endpoint:
        raise BadConfigError(f"配置「{row.name}」没有填写服务地址（endpoint）")

    key = (getattr(row, "api_key", "") or "").strip()
    headers = {"Content-Type": "application/json"}

    if protocol == PROTOCOL_ANTHROPIC:
        url = endpoint + path
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
    elif protocol == PROTOCOL_GEMINI:
        # Gemini 把密钥放在 query 参数里，走 header 反而会被拒
        sep = "&" if "?" in path else "?"
        url = f"{endpoint}{path}{sep}key={urllib.parse.quote(key)}"
    else:
        url = endpoint + path
        if key:
            headers["Authorization"] = f"Bearer {key}"

    payload = json.dumps(_clean_kwargs(body), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with _open(url, req, timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001  读错误体失败不该掩盖原始错误
            pass
        raise _http_error(exc.code, detail, row) from None
    except urllib.error.URLError as exc:
        raise _wrap_urlerror(exc, endpoint, timeout) from None
    except (socket.timeout, TimeoutError):
        raise ModelTimeoutError(
            f"调用超时（{timeout:g}s 未返回）：{endpoint}，可调大该配置的「超时秒数」"
        ) from None


def _load_json(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001  厂商可能用 HTML 报错页冒充响应
        raise BadResponseError(f"返回不是合法 JSON（多半是 endpoint 填错到了网页）：{raw[:200]}") from None
    if not isinstance(obj, dict):
        raise BadResponseError(f"返回体不是对象：{raw[:200]}")
    return obj


# ================================================================ 报文拼装 / 解析
def _chat_payload(protocol: str, row: models.AIProvider, messages: list[dict[str, Any]],
                  temperature: float | None, max_tokens: int) -> tuple[str, dict[str, Any]]:
    """拼出 (path, body)。各家报文形状差异全收敛在这里。"""
    model = (getattr(row, "model", "") or "").strip()
    if not model:
        raise BadConfigError(f"配置「{row.name}」没有填写模型名（model）")

    if protocol == PROTOCOL_OPENAI:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens:
            body["max_tokens"] = max_tokens
        return "/chat/completions", body

    if protocol == PROTOCOL_OLLAMA:
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens:
            options["num_predict"] = max_tokens
        return "/api/chat", {"model": model, "messages": messages, "stream": False,
                             "options": options or None}

    if protocol == PROTOCOL_ANTHROPIC:
        # Anthropic 不接受 messages 里的 system role，system 要单独一个字段；
        # 另外 max_tokens 是**必填**，不填直接 400。
        system = "\n\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        rest = [m for m in messages if m.get("role") != "system"] or messages
        return "/messages", {"model": model, "system": system, "messages": rest,
                             "max_tokens": max_tokens or 1024, "temperature": temperature}

    # Gemini：路径里带模型名，system 走 systemInstruction
    return (
        f"/models/{model}:generateContent",
        {
            "systemInstruction": _gemini_system(messages),
            "contents": _gemini_contents(messages),
            "generationConfig": {"temperature": temperature,
                                 "maxOutputTokens": max_tokens or None},
        },
    )


def _gemini_system(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    parts = [{"text": str(m.get("content", ""))} for m in messages if m.get("role") == "system"]
    return {"parts": parts} if parts else None


def _gemini_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI 的 [{role, content}] -> Gemini 的 [{role, parts}]。

    content 既可能是字符串，也可能是视觉场景下的 part 列表
    （[{"type":"text",...},{"type":"image_url",...}]），后者正好顺手把上传图的
    能力用在了 vision 用途上，不必再单独写一套 multimodal 报文。
    """
    contents: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "system":
            continue
        role = "user" if m.get("role") == "user" else "model"
        parts: list[dict[str, Any]] = []
        content = m.get("content")
        if isinstance(content, list):
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    parts.append({"text": str(p.get("text", ""))})
                elif p.get("type") == "image_url":
                    url = (p.get("image_url") or {}).get("url", "")
                    head, _, b64 = str(url).partition(",")
                    if head.startswith("data:"):
                        mime = head[5:].split(";")[0] or "image/jpeg"
                        parts.append({"inlineData": {"mimeType": mime, "data": b64}})
                    else:
                        parts.append({"fileData": {"mimeType": "image/jpeg", "fileUri": url}})
        else:
            parts.append({"text": str(content or "")})
        if parts:
            contents.append({"role": role, "parts": parts})
    return contents


def _extract_text(protocol: str, payload: dict[str, Any]) -> tuple[str, str]:
    """从各家响应里抠出文本。返回 (text, finish_reason)。"""
    if protocol == PROTOCOL_OPENAI:
        choices = payload.get("choices") or []
        if not choices:
            raise BadResponseError("响应里没有 choices 字段，请检查 endpoint 是否指向对话接口")
        msg = choices[0].get("message") or {}
        text = msg.get("content") or choices[0].get("text") or ""
        return str(text).strip(), str(choices[0].get("finish_reason") or "")

    if protocol == PROTOCOL_OLLAMA:
        msg = payload.get("message") or {}
        return str(msg.get("content") or payload.get("response") or "").strip(), \
            "stop" if payload.get("done") else ""

    if protocol == PROTOCOL_ANTHROPIC:
        blocks = payload.get("content") or []
        if not blocks:
            raise BadResponseError("Anthropic 响应里没有 content 块")
        return "".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict)).strip(), \
            str(payload.get("stop_reason") or "")

    candidates = payload.get("candidates") or []
    if not candidates:
        raise BadResponseError("Gemini 响应里没有 candidates，可能是被安全策略拦下了")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict)).strip(), \
        str(candidates[0].get("finishReason") or "")


# ================================================================ 兜底执行器
def _run(db: Session, usage: str, invoke: Callable[[models.AIProvider, str], Any]) -> tuple[Any, list[dict[str, Any]]]:
    """按「多条配置按序兜底 + 单条有限重试」执行 invoke，返回 (结果, 尝试记录)。

    invoke(row, kind) 抛 GatewayError 代表这条这次没成：
      * retryable 且还有重试次数 -> 退避后重试同一条；
      * 否则换下一条配置。
    全部失败时抛出最后一次的异常，并把完整尝试记录挂在 exception.attempts 上。
    """
    plan = resolve_plan(db, usage)
    if not plan:
        raise ConfigMissingError(
            f"尚未配置「{usage}」用途的模型（需要 {describe_kinds(usage)} 中的一种），"
            f"请到管理后台「AI 接入」里添加并启用"
        )

    attempts: list[Attempt] = []
    last: GatewayError | None = None

    for kind, row in plan:
        retries = _retries_of(row)
        for attempt in range(1, retries + 2):
            started = time.monotonic()
            try:
                result = invoke(row, kind)
            except GatewayError as exc:
                last = exc
                attempts.append(Attempt(
                    provider_pk=row.id, name=row.name, kind=kind, model=row.model,
                    endpoint=row.endpoint, attempt=attempt, ok=False,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error_code=exc.code, message=exc.message,
                ))
                if not exc.retryable or attempt > retries:
                    break
                backoff = min(0.4 * attempt, _RETRY_BACKOFF_CAP)
                log.warning("模型调用失败（%s/%s/%s 第 %d 次），%.1fs 后退避重试：%s",
                            usage, kind, row.model, attempt, backoff, exc.message)
                time.sleep(backoff)
                continue
            attempts.append(Attempt(
                provider_pk=row.id, name=row.name, kind=kind, model=row.model,
                endpoint=row.endpoint, attempt=attempt, ok=True,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            ))
            return result, [asdict(a) for a in attempts]

    assert last is not None  # plan 非空时走到这里必然至少失败过一次
    last.attempts = [asdict(a) for a in attempts]
    labels = " -> ".join(f"{a.name}({a.kind}/{a.model})" for a in attempts)
    log.error("用途 %s 全部配置调用失败：%s", usage, labels or "（无可用配置）")
    raise last


# ================================================================ 对外入口
def chat(db: Session, usage: str, messages: list[dict[str, Any]], *,
         temperature: float | None = None, max_tokens: int | None = None,
         timeout_sec: float | None = None, auto_trim: bool = True) -> ChatResult:
    """按用途发一次对话请求。

    ``messages`` 用 OpenAI 的 [{"role":"user"|"assistant"|"system","content":...}]
    形状，由调用层负责翻译成目标厂商的报文。

    temperature / max_tokens 的优先级：**调用方显式传值 > 该配置 params 里的值
    > 内置默认**。刻意不在代码里写死 0.7 / 2048 这类魔法数字——不同用途差别很大，
    意图识别要低温（0.1）保证结构化输出稳定，陪聊可以给到 0.8 更自然。

    auto_trim=True 且配置填了 context_window 时，会先按窗口裁剪历史
    （reserve 按 max_tokens 预留回复空间）。
    """
    if usage not in USAGE_ROUTES:
        raise UsageError(f"未知用途 {usage!r}，可选：{', '.join(USAGE_ROUTES)}")
    src = [dict(m) for m in messages]

    def invoke(row: models.AIProvider, kind: str) -> ChatResult:
        protocol = _protocol(row)
        temp = temperature if temperature is not None else _num(row, "temperature", DEFAULT_TEMPERATURE)
        mt = max_tokens if max_tokens is not None else int(_num(row, "max_tokens", 0) or 0)
        window = int(getattr(row, "context_window", 0) or 0)
        payload_msgs = trim_messages(src, window, mt) if (auto_trim and window) else src

        timeout = _timeout_of(row, timeout_sec)
        path, body = _chat_payload(protocol, row, payload_msgs, temp, mt)
        started = time.monotonic()
        _, raw = _http_post(row, path, body, timeout)
        payload = _load_json(raw)
        text, finish = _extract_text(protocol, payload)
        return ChatResult(
            text=text, provider_pk=row.id, provider_name=row.name, kind=kind,
            model=row.model, usage=usage, finish_reason=finish,
            elapsed_ms=int((time.monotonic() - started) * 1000), raw=payload,
        )

    result, attempts = _run(db, usage, invoke)
    result.attempts = attempts
    return result


def embed(db: Session, text: str | list[str], *, usage: str = "embed",
          timeout_sec: float | None = None) -> VectorResult:
    """文本向量化。text 可以是单条字符串或字符串列表（与 OpenAI 的 input 一致）。"""
    inputs = [text] if isinstance(text, str) else list(text)

    def invoke(row: models.AIProvider, kind: str) -> VectorResult:
        protocol = _protocol(row)
        if protocol not in PROTOCOL_WITH_EMBEDDINGS:
            style = (getattr(row, "api_style", "") or DEFAULT_API_STYLE).strip() or DEFAULT_API_STYLE
            raise UnsupportedCapabilityError(
                f"「{API_STYLES.get(style, style)}」风格暂不支持文本嵌入；"
                f"请改用 OpenAI 兼容的向量模型（如 bge-m3、text-embedding-v3）"
            )
        model = (getattr(row, "model", "") or "").strip()
        if not model:
            raise BadConfigError(f"配置「{row.name}」没有填写模型名（model）")

        timeout = _timeout_of(row, timeout_sec)
        if protocol == PROTOCOL_OLLAMA:
            path, body = "/api/embeddings", {"model": model, "input": inputs}
        else:
            path, body = "/embeddings", {"model": model, "input": inputs}
        started = time.monotonic()
        _, raw = _http_post(row, path, body, timeout)
        payload = _load_json(raw)

        vectors = _extract_vectors(protocol, payload)
        if not vectors:
            raise BadResponseError("响应里没有取到 embedding 数组，请检查模型是否为向量模型")
        return VectorResult(
            vectors=vectors, provider_pk=row.id, provider_name=row.name, kind=kind,
            model=row.model, usage=usage, dimensions=len(vectors[0]),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    result, attempts = _run(db, usage, invoke)
    result.attempts = attempts
    return result


def _extract_vectors(protocol: str, payload: dict[str, Any]) -> list[list[float]]:
    """各家 embeddings 响应取向量：OpenAI 是 data[].embedding，Ollama 是 embeddings[]。"""
    out: list[list[float]] = []
    if protocol == PROTOCOL_OLLAMA and isinstance(payload.get("embeddings"), list):
        rows_src = [{"embedding": v} for v in payload["embeddings"]]
    else:
        rows_src = payload.get("data") or []
        if isinstance(payload.get("embedding"), list):
            rows_src = [{"embedding": payload["embedding"]}]
    for item in rows_src:
        vec = item.get("embedding") if isinstance(item, dict) else None
        if isinstance(vec, list):
            out.append([float(x) for x in vec])
    return out


def rerank(db: Session, query: str, documents: list[str], *, usage: str = "rerank",
           top_n: int | None = None, timeout_sec: float | None = None) -> RankResult:
    """检索结果重排，返回与 documents 等长、同序的分数（越大越相关）。"""
    def invoke(row: models.AIProvider, kind: str) -> RankResult:
        protocol = _protocol(row)
        if protocol not in PROTOCOL_WITH_RERANK:
            style = (getattr(row, "api_style", "") or DEFAULT_API_STYLE).strip() or DEFAULT_API_STYLE
            raise UnsupportedCapabilityError(
                f"「{API_STYLES.get(style, style)}」风格暂不支持重排；"
                f"请改用提供 OpenAI 兼容 /rerank 接口的厂商"
            )
        model = (getattr(row, "model", "") or "").strip()
        if not model:
            raise BadConfigError(f"配置「{row.name}」没有填写模型名（model）")

        timeout = _timeout_of(row, timeout_sec)
        body: dict[str, Any] = {"model": model, "query": query, "documents": list(documents)}
        if top_n:
            body["top_n"] = top_n
        started = time.monotonic()
        _, raw = _http_post(row, "/rerank", body, timeout)
        payload = _load_json(raw)
        scores = _extract_scores(payload, len(documents))
        return RankResult(
            scores=scores, provider_pk=row.id, provider_name=row.name, kind=kind,
            model=row.model, usage=usage,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    result, attempts = _run(db, usage, invoke)
    result.attempts = attempts
    return result


def _extract_scores(payload: dict[str, Any], expect: int) -> list[float]:
    """把 data/results 里的 (index, score) 还原成与输入 documents 同序的数组。

    不同厂商字段名不一样：Cohere 系用 relevance_score，硅基流动/OpenAI 兼容多半
    也是这两个之一，vLLM 则可能是 score。三种都认，认不出来才算失败。
    """
    items = payload.get("results") or payload.get("data") or []
    if not isinstance(items, list) or not items:
        raise BadResponseError("响应里没有 results/data 列表，请确认该模型是重排模型")
    scores = [0.0] * expect
    for item in items:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if not isinstance(idx, int) or not 0 <= idx < expect:
            continue
        raw = item.get("relevance_score", item.get("score"))
        try:
            scores[idx] = float(raw)
        except (TypeError, ValueError):
            scores[idx] = 0.0
    return scores
