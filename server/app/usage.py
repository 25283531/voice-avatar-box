"""模型调用用量计量：归一化、落库、聚合查询。

为什么调用方不直接写 ``models.ModelUsage``
------------------------------------------
1. **归一化只做一次**：各厂商的 usage 字段名完全不同（OpenAI 的
   ``prompt_tokens`` / Anthropic 的 ``input_tokens`` / Gemini 的
   ``prompt_token_count`` / Ollama 的 ``prompt_eval_count``），漏值、
   负值、字符串数字都要在这里归零，不能让每个调用方各写一遍适配；
2. **记账失败绝不拖垮主流程**：老人正在等一句话回答，用量写库失败
   正确的表现是「这次没记上，照常回答」，所以内部吞异常只记日志；
3. **聚合口径必须单一真源**：「设备 token 用量」要在管理端、用户端、
   后续可能的设备配额三处展示，各写各的 SUM 必然漂移。

与 chat_usage 的分工：那边是「陪聊秒数」管配额，这边是「token 数」
管对账，见 models.ModelUsage 的类注释。
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models
from .models import utcnow

log = logging.getLogger("elder.usage")


# ---------------------------------------------------------------- 归一化
# 各协议 usage 字段名的候选表。顺序即优先级：前面的键存在就用前面的值。
# 刻意用「候选键列表」而不是 if/elif 链 —— 加一家厂商只许加一行数据，
# 不许加一段控制流（控制流会漂移，数据表不会）。
_PROMPT_KEYS: tuple[str, ...] = ("prompt_tokens", "input_tokens", "prompt_token_count",
                                 "prompt_eval_count", "promptTokenCount")
_COMPLETION_KEYS: tuple[str, ...] = ("completion_tokens", "output_tokens",
                                     "candidates_token_count", "eval_count",
                                     "candidatesTokenCount")
_TOTAL_KEYS: tuple[str, ...] = ("total_tokens", "totalTokenCount")


def _as_int(value: Any) -> int:
    """把厂商可能返回的各种形态收敛成非负整数。

    None / 缺失 / 布尔 / 不可解析 -> 0；负数 -> 0（用量不可能是负的，
    出现负数说明厂商语义变了，按 0 记比按 -1 记安全）。
    """
    if value is None or isinstance(value, bool):
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _first_int(d: dict, keys: tuple[str, ...]) -> int:
    for k in keys:
        if k in d:
            got = _as_int(d[k])
            if got:
                return got
    return 0


def normalize_usage(raw: dict | None) -> tuple[int, int, int]:
    """从厂商响应的 usage 段提取 (prompt, completion, total)。

    total 缺失时按 prompt + completion 折算；三者全缺返回 (0, 0, 0)
    （调用方据此判断「这家厂商没返用量」，可选择记 0 或记 estimated）。
    """
    if not isinstance(raw, dict):
        return (0, 0, 0)
    # OpenAI/Anthropic/Ollama 把用量放在 usage 段（Ollama 在顶层），
    # Gemini 放在 usageMetadata 段。两个容器名都认，免得加一家改一段控制流。
    meta: dict | None = None
    for key in ("usage", "usageMetadata"):
        v = raw.get(key)
        if isinstance(v, dict):
            meta = v
            break
    if meta is None:
        meta = raw
    prompt = _first_int(meta, _PROMPT_KEYS)
    completion = _first_int(meta, _COMPLETION_KEYS)
    total = _first_int(meta, _TOTAL_KEYS) or (prompt + completion)
    return (prompt, completion, total)


def new_request_id() -> str:
    """一次模型调用的追踪 id（足够短，能进日志与 URL）。"""
    return secrets.token_hex(8)


# ---------------------------------------------------------------- 落库
def record_model_usage(
    db: Session,
    *,
    source: str,
    usage: str,
    device_id: int | None = None,
    user_id: int | None = None,
    agent_id: int | None = None,
    provider_id: int | None = None,
    provider_name: str = "",
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    request_id: str = "",
    session_id: str = "",
    ok: bool = True,
    error: str = "",
    elapsed_ms: int = 0,
    commit: bool = True,
) -> models.ModelUsage | None:
    """落一行用量流水。任何异常都只记日志并返回 None —— 记账是旁路，不是主流程。

    ``commit=True`` 时自行提交（调用方通常没有别的事务要一起提交）；
    传 False 时由调用方控制事务边界（例如要和业务写同一个事务）。
    """
    try:
        row = models.ModelUsage(
            source=(source or "")[:16],
            usage=(usage or "")[:16],
            device_id=device_id,
            user_id=user_id,
            agent_id=agent_id,
            provider_id=provider_id,
            provider_name=(provider_name or "")[:64],
            model=(model or "")[:128],
            prompt_tokens=_as_int(prompt_tokens),
            completion_tokens=_as_int(completion_tokens),
            total_tokens=_as_int(total_tokens),
            request_id=(request_id or "")[:40],
            session_id=(session_id or "")[:40],
            ok=bool(ok),
            error=(error or "")[:255],
            elapsed_ms=_as_int(elapsed_ms),
        )
        db.add(row)
        if commit:
            db.commit()
            db.refresh(row)
        else:
            db.flush()
        return row
    except Exception as exc:  # noqa: BLE001  记账失败绝不能影响主流程
        log.warning("用量记账失败（不影响业务）：%s: %s", type(exc).__name__, exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001  rollback 失败只能吞掉
            pass
        return None


def record_from_result(
    db: Session,
    result: Any,
    *,
    source: str,
    usage: str,
    device_id: int | None = None,
    user_id: int | None = None,
    agent_id: int | None = None,
    request_id: str = "",
    session_id: str = "",
    ok: bool = True,
    error: str = "",
    commit: bool = True,
) -> models.ModelUsage | None:
    """从 ``llm_gateway.ChatResult``（或形状相同的对象）落一行。

    token 数优先读结果对象上的 ``prompt_tokens / completion_tokens /
    total_tokens`` 字段；对象没有这些字段时（旧版 ChatResult）回落解析
    ``raw`` 里的 usage 段。两种形态都容错，语音链路因此不依赖网关的改动顺序。
    """
    prompt = _as_int(getattr(result, "prompt_tokens", 0))
    completion = _as_int(getattr(result, "completion_tokens", 0))
    total = _as_int(getattr(result, "total_tokens", 0))
    if not (prompt or completion or total):
        raw = getattr(result, "raw", None)
        prompt, completion, total = normalize_usage(raw if isinstance(raw, dict) else None)
    if not total:
        total = prompt + completion
    return record_model_usage(
        db,
        source=source,
        usage=usage,
        device_id=device_id,
        user_id=user_id,
        agent_id=agent_id,
        provider_id=getattr(result, "provider_pk", None),
        provider_name=str(getattr(result, "provider_name", "") or ""),
        model=str(getattr(result, "model", "") or ""),
        prompt_tokens=prompt,
        completion_tokens=completion_tokens,
        total_tokens=total,
        request_id=request_id,
        session_id=session_id,
        ok=ok,
        error=error,
        elapsed_ms=_as_int(getattr(result, "elapsed_ms", 0)),
        commit=commit,
    )


# ---------------------------------------------------------------- 聚合
def usage_summary(
    db: Session,
    *,
    device_ids: list[int] | None = None,
    user_ids: list[int] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    usage: str | None = None,
    by_day: bool = False,
) -> dict:
    """聚合用量。三个界面（管理端 / 用户端 / 后续设备端）共用这一个口径。

    过滤条件全部可选且是 AND 关系；都不传 = 全量（仅管理端允许）。
    返回：
        calls / prompt_tokens / completion_tokens / total_tokens
        by_usage: {usage: total_tokens}
        by_day:   [{"date": "YYYY-MM-DD", "total_tokens": int}]（by_day=True 时）
        failed_calls
    """
    def _scope(q):
        if device_ids is not None:
            q = q.where(models.ModelUsage.device_id.in_(device_ids))
        if user_ids is not None:
            q = q.where(models.ModelUsage.user_id.in_(user_ids))
        if since is not None:
            q = q.where(models.ModelUsage.created_at >= since)
        if until is not None:
            q = q.where(models.ModelUsage.created_at <= until)
        if usage:
            q = q.where(models.ModelUsage.usage == usage)
        return q

    q = _scope(select(models.ModelUsage))
    rows = db.scalars(q).all()
    summary: dict[str, Any] = {
        "calls": 0,
        "failed_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "by_usage": {},
    }
    if by_day:
        summary["by_day"] = {}
    for r in rows:
        summary["calls"] += 1
        if not r.ok:
            summary["failed_calls"] += 1
        summary["prompt_tokens"] += int(r.prompt_tokens or 0)
        summary["completion_tokens"] += int(r.completion_tokens or 0)
        summary["total_tokens"] += int(r.total_tokens or 0)
        bucket = summary["by_usage"].setdefault(r.usage or "", 0)
        summary["by_usage"][r.usage or ""] = bucket + int(r.total_tokens or 0)
        if by_day:
            day = r.created_at.date().isoformat() if r.created_at else ""
            summary["by_day"][day] = summary["by_day"].get(day, 0) + int(r.total_tokens or 0)
    if by_day:
        summary["by_day"] = [{"date": d, "total_tokens": t}
                             for d, t in sorted(summary["by_day"].items())]
    return summary


def recent_usage_rows(
    db: Session,
    *,
    device_ids: list[int] | None = None,
    user_ids: list[int] | None = None,
    since: datetime | None = None,
    usage: str | None = None,
    limit: int = 100,
) -> list[models.ModelUsage]:
    """最近 N 条流水（列表展示用）。同样由调用方先算好可见范围。"""
    q = select(models.ModelUsage)
    if device_ids is not None:
        q = q.where(models.ModelUsage.device_id.in_(device_ids))
    if user_ids is not None:
        q = q.where(models.ModelUsage.user_id.in_(user_ids))
    if since is not None:
        q = q.where(models.ModelUsage.created_at >= since)
    if usage:
        q = q.where(models.ModelUsage.usage == usage)
    q = q.order_by(models.ModelUsage.id.desc()).limit(max(1, min(500, int(limit))))
    return list(db.scalars(q).all())


def count_calls_today(db: Session, device_id: int) -> int:
    """某设备今天的调用次数（设备端「今天聊了多少句」的轻量口径）。"""
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(db.scalar(
        select(func.count(models.ModelUsage.id))
        .where(models.ModelUsage.device_id == device_id,
               models.ModelUsage.created_at >= start,
               models.ModelUsage.usage == "chat")
    ) or 0)
