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

2026-10-17 新增的两个能力
------------------------
* **工具调用**（function calling）：``chat()`` 加可选 ``tools`` / ``tool_choice``，
  ``ChatResult`` 带回规范化的 ``tool_calls``。默认不传 tools 时行为与此前逐字
  一致（共用模块，不许有回归）。四家协议的支持矩阵与降级行为见
  ``_chat_payload`` 上方的注释块。
* **流式对话**（``chat_stream()``）：阻塞生成器，逐条 yield 文本增量，
  语音链路用它把"整段生成完再合成"改成"按句合成"。厂商不理会 stream:true
  时自动退回非流式解析，不假装流式。

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
    PROTOCOL_LOCAL_EMBED,
    PROTOCOL_OLLAMA,
    PROTOCOL_OPENAI,
    PROTOCOL_WITH_EMBEDDINGS,
    PROTOCOL_WITH_RERANK,
    USAGE_ROUTES,
    describe_kinds,
    protocol_of,
)
# usage.py 只依赖 models/db，不 import 本模块 —— 这个方向不成环
# （已复查：models 不 import llm_gateway，usage 的 import 表里也没有它）。
# 若将来 usage 反向依赖网关，这里要改函数内局部 import 并把原因写进注释。
from .usage import normalize_usage

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
    # ---- 工具调用（function calling）----
    # 规范化成 OpenAI 形状，**四家厂商统一**，调用方不需要知道背后是谁::
    #
    #     [{"id": "call_xxx", "type": "function",
    #       "function": {"name": "create_reminder",
    #                    "arguments": "{\"title\": \"吃药\"}"}}]
    #
    # * ``arguments`` 恒为 **JSON 字符串**（Anthropic/Gemini/Ollama 原生给的是
    #   对象，这里统一 json.dumps 过来），解析失败时保留原串，由调用方兜。
    # * ``id`` 由厂商给；Gemini 不给 id，用 "gc-<序号>" 合成 —— 喂回 tool
    #   结果时 Gemini 只认 name，OpenAI/Ollama 认 id，两样都带上。
    # * ``text`` 与 ``tool_calls`` 可以同时非空（"我帮您记一下" + 工具调用），
    #   语音链路会把这段文本当成确认语先念出来。
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # ---- token 用量（2026-10 计量建设）----
    # ⚠️ 上面的 ``usage`` 是**用途字符串**（"chat"/"light"/…），与下面三个
    # token 数字不是一回事，别混淆。
    # 厂商没返 usage 段时三者全为 0（调用方据此判断"这家不返用量"，
    # 可选择记 0 或记 estimated）。归一化口径唯一真源是
    # ``app.usage.normalize_usage``，这里不自己解析。
    # ⚠️ 边界：``VectorResult`` / ``RankResult`` **本轮不接**（embed/rerank
    # 的 raw 里同样有 usage，但接一个能力要连它的记账方一起接，半接半不接
    # 比不接更危险）—— 留作后续增量，要接时连 ``record_from_result`` 的
    # 调用方一起改，并补对应的冒烟断言。
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


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


def endpoint_of(url: str) -> str:
    """取不含 query 的端点（Gemini 的 key 在 query 里，别进报错文案）。"""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _build_post_request(row: models.AIProvider, path: str,
                        body: dict[str, Any]) -> tuple[str, urllib.request.Request]:
    """拼出 (url, Request)。**非流式与流式共用这一个函数** —— 两套各写一份
    必然漂移，漂移的表现是"同一个 key，流式调用 401、非流式调用正常"。
    """
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
    return url, urllib.request.Request(url, data=payload, headers=headers, method="POST")


def _http_post(row: models.AIProvider, path: str, body: dict[str, Any],
               timeout: float) -> tuple[int, str]:
    """发一次 POST，返回 (状态码, 原始响应文本)。所有异常统一转成 GatewayError。"""
    url, req = _build_post_request(row, path, body)
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
        raise _wrap_urlerror(exc, endpoint_of(url), timeout) from None
    except (socket.timeout, TimeoutError):
        raise ModelTimeoutError(
            f"调用超时（{timeout:g}s 未返回）：{endpoint_of(url)}，可调大该配置的「超时秒数」"
        ) from None


def _open_stream(row: models.AIProvider, path: str, body: dict[str, Any],
                 timeout: float):
    """发一次 POST 并返回**未读完的响应对象**，供流式（SSE/NDJSON）逐行读。

    与非流式的差异只有"不把 body 一次读干"。异常分诊口径与 ``_http_post``
    逐字一致（同一家厂商的 401 不能因为流式就变成另一种错误）。
    """
    url, req = _build_post_request(row, path, body)
    try:
        return _open(url, req, timeout)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001  同上
            pass
        raise _http_error(exc.code, detail, row) from None
    except urllib.error.URLError as exc:
        raise _wrap_urlerror(exc, endpoint_of(url), timeout) from None
    except (socket.timeout, TimeoutError):
        raise ModelTimeoutError(
            f"调用超时（{timeout:g}s 未返回）：{endpoint_of(url)}，可调大该配置的「超时秒数」"
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
# ---------------------------------------------------------------- 工具（function calling）
# 各家 tools 支持矩阵（2026-10-17 实现时点，**只实现确认支持的**）：
#
#   | 协议      | tools 请求字段                        | tool_calls 响应位置            | tool 结果喂回方式 |
#   |-----------|---------------------------------------|--------------------------------|-------------------|
#   | OpenAI    | body["tools"] + body["tool_choice"]   | choices[].message.tool_calls   | {"role":"tool","tool_call_id":..} |
#   | Ollama    | body["tools"]（原生 /api/chat 支持）   | message.tool_calls             | {"role":"tool","tool_name":..}    |
#   | Anthropic | body["tools"]（name/description/input_schema） | content[].type=="tool_use" | user 消息里的 tool_result 块     |
#   | Gemini    | body["tools"][].function_declarations  | parts[].functionCall           | user 消息里的 functionResponse   |
#
# **降级行为（不假装支持）**：
#   1. 上游把带 tools 的请求以 4xx 拒掉时（部分 OpenAI 兼容网关/小模型不认识
#      这个字段），chat() 会自动**去掉 tools 重试一次**并打 WARNING ——
#      表现是"这次没调用工具、当普通对话回答"，而不是整轮失败；
#   2. 模型本身不支持 function calling（本地小模型常见）时，它就是不返回
#      tool_calls，调用方按普通文本回复处理 —— 这是模型的自由，不是错误；
#   3. 流式（chat_stream）下同样四家都实现了 SSE/NDJSON 解析。

def _tool_args_json(raw: Any) -> str:
    """把厂商给的 arguments 统一成 JSON **字符串**（内部规范形状）。

    OpenAI 给字符串；Ollama/Anthropic/Gemini 给对象。给字符串的原样返回，
    给对象的 dumps；给其它（None/坏类型）返回 "{}" —— 调用方 json.loads
    失败时有兜底，不会在这里抛。
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        try:
            return json.dumps(raw, ensure_ascii=False)
        except Exception:  # noqa: BLE001  不可序列化的参数不该让整轮崩
            return "{}"
    return "{}"


def _openai_tools_to(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """内部 OpenAI 形状 [{type:function, function:{...}}] -> Anthropic 形状。"""
    out: list[dict[str, Any]] = []
    for t in tools or []:
        fn = (t or {}).get("function") or {}
        if not fn.get("name"):
            continue
        out.append({"name": str(fn["name"]),
                    "description": str(fn.get("description") or ""),
                    "input_schema": fn.get("parameters") or {"type": "object",
                                                             "properties": {}}})
    return out


def _gemini_tools_to(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """内部 OpenAI 形状 -> Gemini 的 functionDeclarations。"""
    decls: list[dict[str, Any]] = []
    for t in tools or []:
        fn = (t or {}).get("function") or {}
        if not fn.get("name"):
            continue
        decls.append({"name": str(fn["name"]),
                      "description": str(fn.get("description") or ""),
                      "parameters": fn.get("parameters") or {"type": "object",
                                                             "properties": {}}})
    return [{"function_declarations": decls}] if decls else []


def _translate_messages(protocol: str,
                        messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把内部（OpenAI 形状）消息列表翻成目标协议形状。

    需要翻译的只有两类"普通对话里不存在"的消息：
      * assistant 带 tool_calls（模型要求调用工具的那条）；
      * role=="tool"（工具执行结果）。
    其余消息各家原有的翻译逻辑（system 提取 / parts 化）继续复用。
    """
    has_special = any(m.get("role") == "tool" or m.get("tool_calls") for m in messages)
    if not has_special:
        return messages

    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        calls = m.get("tool_calls") or []
        if role == "tool":
            content = str(m.get("content") or "")
            if protocol == PROTOCOL_ANTHROPIC:
                # Anthropic 的 tool_result 必须挂在 user 消息里
                out.append({"role": "user", "content": [
                    {"type": "tool_result",
                     "tool_use_id": str(m.get("tool_call_id") or ""),
                     "content": content}]})
            elif protocol == PROTOCOL_GEMINI:
                # Gemini 的 functionResponse 也挂 user；response 必须是对象
                out.append({"role": "user", "parts": [{"functionResponse": {
                    "name": str(m.get("name") or ""),
                    "response": {"result": content}}}]})
            elif protocol == PROTOCOL_OLLAMA:
                # Ollama 原生要 tool_name（OpenAI 形状认 id）
                out.append({"role": "tool",
                            "tool_call_id": str(m.get("tool_call_id") or ""),
                            "tool_name": str(m.get("name") or ""),
                            "content": content})
            else:
                out.append({"role": "tool",
                            "tool_call_id": str(m.get("tool_call_id") or ""),
                            "content": content})
            continue

        if role == "assistant" and calls:
            text = str(m.get("content") or "")
            if protocol == PROTOCOL_ANTHROPIC:
                blocks: list[dict[str, Any]] = []
                if text:
                    blocks.append({"type": "text", "text": text})
                for c in calls:
                    fn = (c or {}).get("function") or {}
                    try:
                        args = json.loads(_tool_args_json(fn.get("arguments")))
                    except Exception:  # noqa: BLE001  坏 arguments 传空对象
                        args = {}
                    blocks.append({"type": "tool_use",
                                   "id": str((c or {}).get("id") or ""),
                                   "name": str(fn.get("name") or ""),
                                   "input": args if isinstance(args, dict) else {}})
                out.append({"role": "assistant", "content": blocks})
            elif protocol == PROTOCOL_GEMINI:
                parts: list[dict[str, Any]] = []
                if text:
                    parts.append({"text": text})
                for c in calls:
                    fn = (c or {}).get("function") or {}
                    try:
                        args = json.loads(_tool_args_json(fn.get("arguments")))
                    except Exception:  # noqa: BLE001
                        args = {}
                    parts.append({"functionCall": {
                        "name": str(fn.get("name") or ""),
                        "args": args if isinstance(args, dict) else {}}})
                out.append({"role": "model", "parts": parts})
            else:
                # OpenAI / Ollama 原生就吃这个形状（ Ollama 的 arguments
                # 既接受字符串也接受对象，保持字符串最稳）
                out.append({"role": "assistant", "content": text, "tool_calls": calls})
            continue

        out.append(m)
    return out


def _chat_payload(protocol: str, row: models.AIProvider, messages: list[dict[str, Any]],
                  temperature: float | None, max_tokens: int,
                  tools: list[dict[str, Any]] | None = None,
                  tool_choice: str | None = None) -> tuple[str, dict[str, Any]]:
    """拼出 (path, body)。各家报文形状差异全收敛在这里。

    ``tools=None``（默认）时**不产出任何工具相关字段**，报文与加 tools
    能力之前逐字一致 —— 共用模块，不许有任何回归。
    """
    model = (getattr(row, "model", "") or "").strip()
    if not model:
        raise BadConfigError(f"配置「{row.name}」没有填写模型名（model）")

    # 先翻成目标协议形状。**无工具消息时原样返回**（_translate_messages 内部
    # 判定），普通对话的报文与加 tools 能力之前逐字一致。
    msgs = _translate_messages(protocol, messages)

    if protocol == PROTOCOL_OPENAI:
        body: dict[str, Any] = {"model": model, "messages": msgs}
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice
        return "/chat/completions", body

    if protocol == PROTOCOL_OLLAMA:
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens:
            options["num_predict"] = max_tokens
        body = {"model": model, "messages": msgs, "stream": False,
                "options": options or None}
        if tools:
            body["tools"] = tools
        return "/api/chat", body

    if protocol == PROTOCOL_ANTHROPIC:
        # Anthropic 不接受 messages 里的 system role，system 要单独一个字段；
        # 另外 max_tokens 是**必填**，不填直接 400。
        # 工具消息已被 _translate_messages 翻成 Anthropic 原生形状
        # （assistant 的 content 块 / user 的 tool_result 块），这里直接用。
        system = "\n\n".join(str(m.get("content", "")) for m in msgs if m.get("role") == "system")
        rest = [m for m in msgs if m.get("role") != "system"] or msgs
        body = {"model": model, "system": system, "messages": rest,
                "max_tokens": max_tokens or 1024, "temperature": temperature}
        if tools:
            body["tools"] = _openai_tools_to(tools)
            body["tool_choice"] = {"type": "auto"}
        return "/messages", body

    # Gemini：路径里带模型名，system 走 systemInstruction
    body = {
        "systemInstruction": _gemini_system(msgs),
        "contents": _gemini_contents(msgs),
        "generationConfig": {"temperature": temperature,
                             "maxOutputTokens": max_tokens or None},
    }
    if tools:
        body["tools"] = _gemini_tools_to(tools)
        # Gemini 的 function_calling_config 默认就是 AUTO，显式省略
    return (
        f"/models/{model}:generateContent",
        body,
    )


def _gemini_system(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    parts = [{"text": str(m.get("content", ""))} for m in messages if m.get("role") == "system"]
    return {"parts": parts} if parts else None


def _gemini_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI 的 [{role, content}] -> Gemini 的 [{role, parts}]。

    content 既可能是字符串，也可能是视觉场景下的 part 列表
    （[{"type":"text",...},{"type":"image_url",...}]），后者正好顺手把上传图的
    能力用在了 vision 用途上，不必再单独写一套 multimodal 报文。

    工具消息经 ``_translate_messages`` 翻过之后带的是**成品 parts**
    （functionCall / functionResponse），这里原样透传 —— 再解析一遍
    只会把没有 "type" 键的 part 悄悄丢掉。
    """
    contents: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "system":
            continue
        if isinstance(m.get("parts"), list):
            role = "user" if m.get("role") == "user" else "model"
            if m["parts"]:
                contents.append({"role": role, "parts": m["parts"]})
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


def _extract_result(protocol: str, payload: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    """从各家响应里抠出 (文本, finish_reason, tool_calls)。

    tool_calls 统一成内部 OpenAI 形状（见 ChatResult.tool_calls 注释）。
    """
    if protocol == PROTOCOL_OPENAI:
        choices = payload.get("choices") or []
        if not choices:
            raise BadResponseError("响应里没有 choices 字段，请检查 endpoint 是否指向对话接口")
        msg = choices[0].get("message") or {}
        text = msg.get("content") or choices[0].get("text") or ""
        return str(text).strip(), str(choices[0].get("finish_reason") or ""), \
            _norm_openai_tool_calls(msg.get("tool_calls"))

    if protocol == PROTOCOL_OLLAMA:
        msg = payload.get("message") or {}
        text = str(msg.get("content") or payload.get("response") or "").strip()
        finish = "stop" if payload.get("done") else ""
        return text, finish, _norm_openai_tool_calls(msg.get("tool_calls"))

    if protocol == PROTOCOL_ANTHROPIC:
        blocks = payload.get("content") or []
        if not blocks:
            raise BadResponseError("Anthropic 响应里没有 content 块")
        text = "".join(str(b.get("text", "")) for b in blocks
                       if isinstance(b, dict) and b.get("type") == "text").strip()
        calls = [{"id": str(b.get("id") or ""), "type": "function",
                  "function": {"name": str(b.get("name") or ""),
                               "arguments": _tool_args_json(b.get("input"))}}
                 for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        return text, str(payload.get("stop_reason") or ""), calls

    candidates = payload.get("candidates") or []
    if not candidates:
        raise BadResponseError("Gemini 响应里没有 candidates，可能是被安全策略拦下了")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict)).strip()
    calls = []
    for i, p in enumerate(parts):
        if isinstance(p, dict) and isinstance(p.get("functionCall"), dict):
            fc = p["functionCall"]
            # Gemini 不给 tool_call id，合成一个稳定的：喂回结果时它只认 name，
            # 但内部形状要齐全（OpenAI/Ollama 认 id）。
            calls.append({"id": f"gc-{i}", "type": "function",
                          "function": {"name": str(fc.get("name") or ""),
                                       "arguments": _tool_args_json(fc.get("args"))}})
    return text, str(candidates[0].get("finishReason") or ""), calls


def _norm_openai_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """OpenAI/Ollama 形状的 tool_calls -> 内部规范形状。

    Ollama 的 ``function.arguments`` 是**对象**而不是字符串，这里统一
    dumps 成字符串；缺字段的坏条目直接跳过（一个坏调用不该让整轮失败）。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            continue
        fn = c.get("function") or {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        out.append({"id": str(c.get("id") or f"call-{i}"), "type": "function",
                    "function": {"name": name,
                                 "arguments": _tool_args_json(fn.get("arguments"))}})
    return out


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
         timeout_sec: float | None = None, auto_trim: bool = True,
         tools: list[dict[str, Any]] | None = None,
         tool_choice: str | None = None) -> ChatResult:
    """按用途发一次对话请求。

    ``messages`` 用 OpenAI 的 [{"role":"user"|"assistant"|"system","content":...}]
    形状，由调用层负责翻译成目标厂商的报文。工具场景下还会出现两种消息：
    assistant 带 ``tool_calls``、以及 ``role=="tool"`` 的执行结果
    （见 :func:`_translate_messages`）。

    temperature / max_tokens 的优先级：**调用方显式传值 > 该配置 params 里的值
    > 内置默认**。刻意不在代码里写死 0.7 / 2048 这类魔法数字——不同用途差别很大，
    意图识别要低温（0.1）保证结构化输出稳定，陪聊可以给到 0.8 更自然。

    auto_trim=True 且配置填了 context_window 时，会先按窗口裁剪历史
    （reserve 按 max_tokens 预留回复空间）。

    ``tools`` 为 None（默认）时行为与本能力加入前**逐字一致**：报文里不会
    出现任何工具字段，各家解析路径完全相同。
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
        path, body = _chat_payload(protocol, row, payload_msgs, temp, mt,
                                   tools=tools, tool_choice=tool_choice)
        started = time.monotonic()
        try:
            _, raw = _http_post(row, path, body, timeout)
        except UpstreamError as exc:
            # 带 tools 被厂商 4xx 拒掉（网关不认识这个字段 / 小模型不支持）：
            # 去掉 tools 重试一次。**这是明确的降级**——这次就当普通对话，
            # 日志里留 WARNING，报告与运维文档都写明了这个行为。
            # 只兜 4xx（retryable=False）：429/5xx 是别的问题，换配置重试
            # 比摘 tools 更对症。
            if not (tools and not exc.retryable):
                raise
            log.warning("配置「%s」（%s）拒绝了带 tools 的请求（%s），"
                        "去掉 tools 降级重试一次", row.name, protocol, exc.message)
            path, body = _chat_payload(protocol, row, payload_msgs, temp, mt)
            _, raw = _http_post(row, path, body, timeout)
        payload = _load_json(raw)
        text, finish, calls = _extract_result(protocol, payload)
        # token 用量从响应体提取（OpenAI/Anthropic/Gemini/Ollama 四种形状
        # 由 normalize_usage 统一归一）。厂商不返 usage 段时是 (0, 0, 0)，
        # 记账方（usage.record_from_result）按 0 落库 —— "调用过但没用量"
        # 与 "没调用过"由 ModelUsage 行的存在性区分，不由 token 数区分。
        # ⚠️ tools 场景下各家的 usage 段形状与普通对话**相同**
        # （OpenAI/Anthropic/Gemini/Ollama 均如此，2026-10-17 核对），
        # 所以这里不需要为 tools 单开一条提取路径。
        prompt_tokens, completion_tokens, total_tokens = normalize_usage(payload)
        return ChatResult(
            text=text, provider_pk=row.id, provider_name=row.name, kind=kind,
            model=row.model, usage=usage, finish_reason=finish,
            elapsed_ms=int((time.monotonic() - started) * 1000), raw=payload,
            tool_calls=calls,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    result, attempts = _run(db, usage, invoke)
    result.attempts = attempts
    return result


# ================================================================ 流式对话
# 为什么语音链路要流式：老人问完一句话，首句延迟 = 整段 LLM 生成时间 +
# 第一句 TTS 时间。流式把 LLM 按句产出、按句送 TTS，首句延迟降到
# "LLM 第一句 + TTS 第一包"。对只能干等的老人，这是体验上最大的一笔。
#
# 事件协议（阻塞生成器，由调用方在线程池里逐条 next）::
#
#     ("text", "增量文本")
#     ... 任意条 ...
#     return ChatResult        # 生成器返回值（StopIteration.value）
#
# **四家协议的流式形状**（2026-10-17 实现时点）：
#   OpenAI    SSE（data: {json} / data: [DONE]），usage 靠 stream_options 末包
#   Ollama    NDJSON（一行一个 JSON，无 data: 前缀），usage 在 done:true 那条
#   Anthropic SSE（message_start/content_block_delta/message_delta）
#   Gemini    SSE（:streamGenerateContent?alt=sse，一行一个 candidates）
#
# 兜底：厂商不理会 stream:true、直接回完整 JSON 时（部分网关的默认行为），
# 按 Content-Type 识别后走非流式解析路径，产出与 chat() 完全相同的结果 ——
# 不假装流式，也不失败。

_STREAM_CHUNK = 65536


def _sse_lines(resp: Any):
    """从 SSE 响应里逐条 yield data 段（多行 data 已按 SSE 规范 join）。"""
    buf: list[str] = []
    while True:
        line = resp.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if not text:
            if buf:
                yield "\n".join(buf)
                buf = []
            continue
        if text.startswith(":"):
            continue                      # 注释 / 心跳
        if text.startswith("data:"):
            buf.append(text[5:].lstrip(" "))
        # event:/id:/retry: 行不需要 —— 各协议的 data 体内自带 type 字段
    if buf:
        yield "\n".join(buf)


def _ndjson_lines(resp: Any):
    """Ollama 的流式是裸 NDJSON（没有 data: 前缀），逐行 yield。"""
    while True:
        line = resp.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").strip()
        if text:
            yield text


def _result_of(row: models.AIProvider, kind: str, usage: str, payload: dict,
               text: str, finish: str, calls: list[dict[str, Any]],
               started: float, *, streamed: bool) -> ChatResult:
    """拼 ChatResult。token 一律过 normalize_usage（唯一真源不破例）。"""
    prompt_tokens, completion_tokens, total_tokens = normalize_usage(payload)
    return ChatResult(
        text=text, provider_pk=row.id, provider_name=row.name, kind=kind,
        model=row.model, usage=usage, finish_reason=finish,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        raw=payload if not streamed else {"streamed": True, "usage": payload},
        tool_calls=calls,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def _stream_openai(row: models.AIProvider, kind: str, usage: str, body: dict,
                   timeout: float, started: float):
    """OpenAI 兼容 SSE。yield ("text", delta)，return ChatResult。"""
    resp = _open_stream(row, "/chat/completions", body, timeout)
    try:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "event-stream" not in ctype:
            # 厂商没理会 stream:true：整体读回来当普通响应解析
            raw = resp.read().decode("utf-8", "replace")
            payload = _load_json(raw)
            text, finish, calls = _extract_result(PROTOCOL_OPENAI, payload)
            return _result_of(row, kind, usage, payload, text, finish, calls,
                              started, streamed=False)
        parts: list[str] = []
        acc: dict[int, dict[str, str]] = {}
        usage_payload: dict[str, Any] = {}
        finish = ""
        for data in _sse_lines(resp):
            if data.strip() == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:  # noqa: BLE001  心跳/坏行跳过，不让整轮炸掉
                continue
            if not isinstance(obj, dict):
                continue
            if isinstance(obj.get("usage"), dict) and obj["usage"]:
                usage_payload = obj["usage"]
            choices = obj.get("choices") or []
            if not choices:
                continue
            ch = choices[0]
            if ch.get("finish_reason"):
                finish = str(ch["finish_reason"])
            delta = ch.get("delta") or {}
            piece = delta.get("content")
            if piece:
                parts.append(str(piece))
                yield ("text", str(piece))
            for frag in delta.get("tool_calls") or []:
                if not isinstance(frag, dict):
                    continue
                idx = int(frag.get("index") or 0)
                slot = acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                if frag.get("id"):
                    slot["id"] = str(frag["id"])
                fn = frag.get("function") or {}
                # name 只在首个分片出现：首个非空即锁定，重复分片不再拼
                # （有的网关会把 name 重复发，拼第二次就坏了）。
                if fn.get("name") and not slot["name"]:
                    slot["name"] = str(fn["name"])
                if fn.get("arguments"):
                    slot["args"] += str(fn["arguments"])
        calls = _norm_openai_tool_calls(
            [{"id": acc[i]["id"], "type": "function",
              "function": {"name": acc[i]["name"], "arguments": acc[i]["args"]}}
             for i in sorted(acc)])
        return _result_of(row, kind, usage, {"usage": usage_payload},
                          "".join(parts), finish, calls, started, streamed=True)
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001  关闭失败没有补救手段
            pass


def _stream_ollama(row: models.AIProvider, kind: str, usage: str, body: dict,
                   timeout: float, started: float):
    """Ollama 原生 /api/chat 的 NDJSON 流。yield ("text", delta)，return ChatResult。"""
    resp = _open_stream(row, "/api/chat", body, timeout)
    try:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "ndjson" not in ctype and "json" in ctype:
            # 非 NDJSON（stream 没生效）：按完整响应解析
            raw = resp.read().decode("utf-8", "replace")
            payload = _load_json(raw)
            text, finish, calls = _extract_result(PROTOCOL_OLLAMA, payload)
            return _result_of(row, kind, usage, payload, text, finish, calls,
                              started, streamed=False)
        parts: list[str] = []
        calls: list[dict[str, Any]] = []
        done_obj: dict[str, Any] = {}
        finish = ""
        for line in _ndjson_lines(resp):
            try:
                obj = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(obj, dict):
                continue
            msg = obj.get("message") or {}
            piece = msg.get("content")
            if piece:
                parts.append(str(piece))
                yield ("text", str(piece))
            # Ollama 的 tool_calls 是**整条**给的（不分片），取最后一条非空
            got = _norm_openai_tool_calls(msg.get("tool_calls"))
            if got:
                calls = got
            if obj.get("done"):
                done_obj = obj
                finish = "stop"
        return _result_of(row, kind, usage, done_obj, "".join(parts),
                          finish, calls, started, streamed=True)
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def _stream_anthropic(row: models.AIProvider, kind: str, usage: str, body: dict,
                      timeout: float, started: float):
    """Anthropic Messages SSE。yield ("text", delta)，return ChatResult。"""
    resp = _open_stream(row, "/messages", body, timeout)
    try:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "event-stream" not in ctype:
            raw = resp.read().decode("utf-8", "replace")
            payload = _load_json(raw)
            text, finish, calls = _extract_result(PROTOCOL_ANTHROPIC, payload)
            return _result_of(row, kind, usage, payload, text, finish, calls,
                              started, streamed=False)
        parts: list[str] = []
        blocks: dict[int, dict[str, Any]] = {}
        input_tokens = 0
        output_tokens = 0
        finish = ""
        for data in _sse_lines(resp):
            try:
                obj = json.loads(data)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(obj, dict):
                continue
            etype = str(obj.get("type") or "")
            if etype == "message_start":
                msg = obj.get("message") or {}
                u = msg.get("usage") or {}
                input_tokens = int(u.get("input_tokens") or 0)
                output_tokens = int(u.get("output_tokens") or 0)
            elif etype == "content_block_start":
                idx = int(obj.get("index") or 0)
                blk = obj.get("content_block") or {}
                if blk.get("type") == "tool_use":
                    blocks[idx] = {"id": str(blk.get("id") or ""),
                                   "name": str(blk.get("name") or ""), "args": ""}
            elif etype == "content_block_delta":
                idx = int(obj.get("index") or 0)
                delta = obj.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    parts.append(str(delta["text"]))
                    yield ("text", str(delta["text"]))
                elif delta.get("type") == "input_json_delta":
                    slot = blocks.setdefault(idx, {"id": "", "name": "", "args": ""})
                    slot["args"] += str(delta.get("partial_json") or "")
            elif etype == "message_delta":
                delta = obj.get("delta") or {}
                if delta.get("stop_reason"):
                    finish = str(delta["stop_reason"])
                u = obj.get("usage") or {}
                if u.get("output_tokens"):
                    output_tokens = int(u["output_tokens"])
            elif etype == "error":
                raise UpstreamError(f"Anthropic 流中返回错误：{str(obj)[:200]}")
        calls = [{"id": blocks[i]["id"], "type": "function",
                  "function": {"name": blocks[i]["name"],
                               "arguments": blocks[i]["args"] or "{}"}}
                 for i in sorted(blocks) if blocks[i]["name"]]
        payload = {"usage": {"input_tokens": input_tokens,
                             "output_tokens": output_tokens}}
        return _result_of(row, kind, usage, payload, "".join(parts),
                          finish, calls, started, streamed=True)
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def _stream_gemini(row: models.AIProvider, kind: str, usage: str, body: dict,
                   timeout: float, started: float):
    """Gemini streamGenerateContent SSE。yield ("text", delta)，return ChatResult。"""
    model = str(body.get("model") or "")
    resp = _open_stream(row, f"/models/{model}:streamGenerateContent?alt=sse",
                        {k: v for k, v in body.items() if k != "model"}, timeout)
    try:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "event-stream" not in ctype:
            raw = resp.read().decode("utf-8", "replace")
            payload = _load_json(raw)
            text, finish, calls = _extract_result(PROTOCOL_GEMINI, payload)
            return _result_of(row, kind, usage, payload, text, finish, calls,
                              started, streamed=False)
        parts: list[str] = []
        calls: list[dict[str, Any]] = []
        usage_meta: dict[str, Any] = {}
        finish = ""
        for data in _sse_lines(resp):
            try:
                obj = json.loads(data)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(obj, dict):
                continue
            if isinstance(obj.get("usageMetadata"), dict):
                usage_meta = obj["usageMetadata"]     # 末块带累计值
            candidates = obj.get("candidates") or []
            if not candidates:
                continue
            cand = candidates[0]
            if cand.get("finishReason"):
                finish = str(cand["finishReason"])
            for i, p in enumerate((cand.get("content") or {}).get("parts") or []):
                if not isinstance(p, dict):
                    continue
                if p.get("text"):
                    parts.append(str(p["text"]))
                    yield ("text", str(p["text"]))
                if isinstance(p.get("functionCall"), dict):
                    fc = p["functionCall"]
                    calls.append({"id": f"gc-{i}", "type": "function",
                                  "function": {"name": str(fc.get("name") or ""),
                                               "arguments": _tool_args_json(fc.get("args"))}})
        return _result_of(row, kind, usage, {"usageMetadata": usage_meta},
                          "".join(parts), finish, calls, started, streamed=True)
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def _chat_stream_body(protocol: str, row: models.AIProvider,
                      messages: list[dict[str, Any]], temp: float | None,
                      mt: int, tools: list[dict[str, Any]] | None,
                      tool_choice: str | None) -> tuple[str, dict[str, Any]]:
    """流式请求体：在非流式拼装函数之上加 stream 开关。

    复用 :func:`_chat_payload` 的翻译逻辑（工具消息/system/parts 全一致），
    只补各家的 stream 标志 —— 另写一套拼装必然漂移。
    """
    path, body = _chat_payload(protocol, row, messages, temp, mt,
                               tools=tools, tool_choice=tool_choice)
    if protocol == PROTOCOL_OPENAI:
        body["stream"] = True
        # 不带 stream_options 时 OpenAI 末包不给 usage，token 会记 0
        body["stream_options"] = {"include_usage": True}
    elif protocol == PROTOCOL_OLLAMA:
        body["stream"] = True
    elif protocol == PROTOCOL_ANTHROPIC:
        body["stream"] = True
    elif protocol == PROTOCOL_GEMINI:
        path = path.replace(":generateContent", ":streamGenerateContent")
        if "?" in path:
            path += "&alt=sse"
        else:
            path += "?alt=sse"
    return path, body


def chat_stream(db: Session, usage: str, messages: list[dict[str, Any]], *,
                temperature: float | None = None, max_tokens: int | None = None,
                timeout_sec: float | None = None, auto_trim: bool = True,
                tools: list[dict[str, Any]] | None = None,
                tool_choice: str | None = None):
    """流式对话。**阻塞生成器**（在调用方线程里逐条 next，别在事件循环里跑）。

    事件流::

        ("text", "增量文本")   # 任意条
        ...
        ("done", ChatResult)   # 恒为最后一条

    ⚠️ 为什么用显式 ``("done", result)`` 事件而不是生成器 return 值：
    调用方（语音会话）是在**协程**里 ``await run_in_threadpool(next, gen)``
    消费它的，StopIteration 穿过协程边界会被 Python 转成 RuntimeError
    （PEP 479 一类的东西），排查起来非常绕。显式 done 事件让调用方永远
    只需要处理「普通返回值」，不看异常。

    多条配置的兜底口径与 :func:`chat` 一致：**建连/首发失败**才换下一条；
    流已经开始（yield 过事件）之后失败不换 —— 前半段可能已经念给老人了，
    换一家重来等于同一句话说两遍。
    """
    if usage not in USAGE_ROUTES:
        raise UsageError(f"未知用途 {usage!r}，可选：{', '.join(USAGE_ROUTES)}")
    src = [dict(m) for m in messages]
    plan = resolve_plan(db, usage)
    if not plan:
        raise ConfigMissingError(
            f"尚未配置「{usage}」用途的模型（需要 {describe_kinds(usage)} 中的一种），"
            f"请到管理后台「AI 接入」里添加并启用"
        )

    attempts: list[dict[str, Any]] = []
    last: GatewayError | None = None
    for kind, row in plan:
        protocol = _protocol(row)
        temp = temperature if temperature is not None else _num(row, "temperature", DEFAULT_TEMPERATURE)
        mt = max_tokens if max_tokens is not None else int(_num(row, "max_tokens", 0) or 0)
        window = int(getattr(row, "context_window", 0) or 0)
        payload_msgs = trim_messages(src, window, mt) if (auto_trim and window) else src
        timeout = _timeout_of(row, timeout_sec)
        started = time.monotonic()
        try:
            _path, body = _chat_stream_body(protocol, row, payload_msgs, temp, mt,
                                            tools, tool_choice)
            parser = {PROTOCOL_OPENAI: _stream_openai,
                      PROTOCOL_OLLAMA: _stream_ollama,
                      PROTOCOL_ANTHROPIC: _stream_anthropic,
                      PROTOCOL_GEMINI: _stream_gemini}[protocol]
            gen = parser(row, kind, usage, body, timeout, started)
            # 生成器是惰性的：**建连与第一个事件都在第一个 next() 里发生**。
            # 把它放进 try —— 这一步失败时一条事件都没 yield 出去，
            # 换下一条配置是安全的（老人什么都没听到）。
            try:
                first = next(gen)
            except StopIteration as stop:
                result: ChatResult = stop.value
                result.attempts = attempts + [{
                    "provider_pk": row.id, "name": row.name, "kind": kind,
                    "model": row.model, "ok": True,
                    "elapsed_ms": result.elapsed_ms}]
                yield ("done", result)
                return
        except GatewayError as exc:
            # 建连前失败（BadConfig / 连接被拒 / 4xx 等）：记一条尝试，换下一条
            last = exc
            attempts.append({"provider_pk": row.id, "name": row.name, "kind": kind,
                             "model": row.model, "ok": False, "error_code": exc.code,
                             "message": exc.message,
                             "elapsed_ms": int((time.monotonic() - started) * 1000)})
            continue

        # 流已建立：逐事件转发。中途失败直接抛（不换配置，理由见 docstring）。
        yield first
        while True:
            try:
                event = next(gen)
            except StopIteration as stop:
                result: ChatResult = stop.value
                result.attempts = attempts + [{
                    "provider_pk": row.id, "name": row.name, "kind": kind,
                    "model": row.model, "ok": True,
                    "elapsed_ms": result.elapsed_ms}]
                yield ("done", result)
                return
            yield event

    assert last is not None
    last.attempts = attempts
    log.error("流式用途 %s 全部配置建连失败：%s", usage,
              " -> ".join(a["name"] for a in attempts) or "（无可用配置）")
    raise last


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
        timeout = _timeout_of(row, timeout_sec)

        if protocol == PROTOCOL_LOCAL_EMBED:
            # 本机 FastEmbed（BAAI/bge-small-zh-v1.5，512 维，已 L2 归一化）。
            # 报文形状与 OpenAI 完全不同（/embed + input/texts，无 model 无密钥），
            # 所以这里是独立分支而不是拼 /embeddings 再撞 404。
            # 单条用 input、批量用 texts —— 与本地服务实现的判据一一对应
            # （服务端只认这两种报文形状，混用会 400）。
            body: dict[str, Any] = (
                {"input": inputs[0]} if len(inputs) == 1 else {"texts": inputs}
            )
            started = time.monotonic()
            _, raw = _http_post(row, "/embed", body, timeout)
            payload = _load_json(raw)
            vectors = _extract_vectors(protocol, payload)
            if not vectors:
                raise BadResponseError(
                    "本地向量化服务响应里没有 embedding 数组，"
                    "请检查服务是否已加载模型（GET /health 看 dim）"
                )
            # model 字段本地服务根本不看，只为落库对账留个可读名字。
            model = (getattr(row, "model", "") or "").strip() or "bge-small-zh-v1.5"
            return VectorResult(
                vectors=vectors, provider_pk=row.id, provider_name=row.name, kind=kind,
                model=model, usage=usage, dimensions=len(vectors[0]),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        model = (getattr(row, "model", "") or "").strip()
        if not model:
            raise BadConfigError(f"配置「{row.name}」没有填写模型名（model）")

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
    """各家 embeddings 响应取向量：
    OpenAI 是 data[].embedding；Ollama 是 embeddings[]；
    本地 FastEmbed 批量是 embeddings[]（键名与 Ollama 恰好一致，同上处理）。
    """
    out: list[list[float]] = []
    if protocol in (PROTOCOL_OLLAMA, PROTOCOL_LOCAL_EMBED) \
            and isinstance(payload.get("embeddings"), list):
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
