"""多平台 AI API 接入配置（需求 15）：本服务做「配置的托管、下发与调用」。

两层职责，别混在一起看
----------------------
1. **托管**：CRUD + 列表过滤 + 导出成小智的 config.yaml 片段；
2. **调用**：server/app/llm_gateway.py 按「用途（usage）」把请求派到某条配置上。

本文件只暴露两个让上层"不必知道配了哪家"的入口：

    GET  /api/providers/resolve?usage=chat   这条用途最终会落到哪条配置（脱敏）
    POST /api/providers/test                 按用途真打一次，验证配置可用与否

为什么要「按用途解析」而不是「按 kind 查」
------------------------------------------
kind 是**储值/能力分类**，会随业务演进（今天 llm 一个够，明天要分 llm_main /
llm_light）；usage 是**调用方的意图**（"我要聊天"、"我要抽意图"、"我要向量"），
比 kind 稳定得多。上层只认 usage，我们把旧部署历史上只有 kind=llm 的老配置
通过候选链（llm_main -> llm）兜住，两边都不用改代码。详见 model_catalog.py。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import get_current_admin, get_current_user, require_config
from ..llm_gateway import GatewayError, chat, embed, rerank, resolve_plan
from ..model_catalog import (
    API_STYLES,
    EXPORT_KINDS,
    KIND_LABELS,
    KIND_LIST,
    USAGE_LABELS,
    USAGE_LIST,
    USAGE_EMBED,
    USAGE_ROUTES,
    describe_kinds,
)
from ..schemas import (
    ModelTestIn,
    ModelTestOut,
    ProviderIn,
    ProviderOut,
    ProviderSafeOut,
    ResolvedModelOut,
)

router = APIRouter(prefix="/api/providers", tags=["providers"])

# 兼容历史写法：别处可能 `from ..routers.providers import VALID_KINDS`。
# 语义上它现在是「真实平台配置的全部 kind」，不再等于下发/导出的那四种。
VALID_KINDS = KIND_LIST

# usage -> 走哪三个入口
_CHAT_USAGES = frozenset({"chat", "light", "vision"})
_EMBED_USAGES = frozenset({"embed"})
_RERANK_USAGES = frozenset({"rerank"})


def _clear_query_cache_if_embedding(kind: str | None) -> None:
    """改动了向量模型配置时，清掉记忆检索的**查询向量缓存**。

    为什么必须联动清：``memory/retrieve.py`` 的 ``_query_cache`` 只按 LRU 淘汰，
    里面存的是**旧模型**算出来的查询向量。换模型后若不清，新查询会拿新模型的
    向量去和库里旧模型写入的记忆向量算余弦 —— 等于拿两把不同的尺子量长度，
    而且**不报错**，只是静默给出错误排序（表现为「换了模型之后召回突然变差」，
    极难定位）。函数名里就写明了这个调用点，避免以后新增改配置的入口时漏掉。

    只在**向量相关**的 kind 上做：改 ASR/TTS 与记忆检索无关，不必连坐。
    """
    if (kind or "") not in USAGE_ROUTES.get(USAGE_EMBED, ()):
        return
    try:
        # 局部导入：memory 包会 import models/db，反向依赖 routers 层会成环
        from ..memory.retrieve import clear_query_cache

        clear_query_cache()
    except Exception:  # noqa: BLE001  清缓存失败不该让配置保存失败
        pass


def _require_usage(usage: str) -> str:
    """校验 usage；未知值明确列出可选清单（拼错是最常见的一类调用方 bug）。"""
    usage = (usage or "").strip()
    if usage not in USAGE_ROUTES:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"未知用途 {usage!r}，可选：{'、'.join(f'{k}（{USAGE_LABELS[k]}）' for k in USAGE_LIST)}",
        )
    return usage


@router.post("", response_model=ProviderOut, status_code=status.HTTP_201_CREATED)
def create_provider(payload: ProviderIn, db: Session = Depends(get_db),
                    admin: models.User = Depends(get_current_admin)):
    """新建一条**平台配置**（仅管理员）。

    ⚠️ 禁止 ``kind == ""``：空 kind 的行是**角色**，而角色已由 /api/roles 独立
    承载（人格、traits、guardrails、派生关系全在那里）。在这里放行空 kind
    等于给"角色改造波及平台配置"留了一条后门 —— APP 建角色时少传一个字段就
    会静默落成一条没有厂商的"平台配置"，然后被设备下发当成真配置用。
    """
    if not (payload.kind or "").strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "创建角色请用 POST /api/roles；本接口只接受带 kind 的平台 AI 配置",
        )
    p = models.AIProvider(**payload.model_dump())
    # 显式写死 record_type：不依赖模型默认值，避免将来默认值改动波及这里
    p.record_type = models.RECORD_TYPE_CONFIG
    db.add(p)
    db.commit()
    db.refresh(p)
    # 新增向量模型配置 -> 旧查询向量缓存全部作废（见 _clear_query_cache_if_embedding）
    _clear_query_cache_if_embedding(p.kind)
    return p


@router.get("", response_model=list[ProviderOut])
def list_providers(kind: str | None = None, db: Session = Depends(get_db),
                   admin: models.User = Depends(get_current_admin)):
    """配置列表（**仅管理员**）。?kind=embedding 这类过滤对新加的 kind 同样生效。

    ⚠️ 收紧为 admin only（架构 §12 U2）：这个接口返回带 api_key 的完整行，
    现状是**任何登录用户**都能读到全平台的密钥。即使 /safe 加了过滤，
    不收紧这里就等于留了个后门 —— 密钥必须只在管理端可见。

    同时加 ``record_type == 'config'`` 过滤：角色行不再混进平台配置列表。
    """
    q = select(models.AIProvider).where(models.AIProvider.record_type == models.RECORD_TYPE_CONFIG)
    if kind:
        q = q.where(models.AIProvider.kind == kind)
    return list(db.scalars(q.order_by(models.AIProvider.kind, models.AIProvider.priority)).all())


@router.get("/safe", response_model=list[ProviderSafeOut])
def list_providers_safe(db: Session = Depends(get_db),
                        user: models.User = Depends(get_current_user)):
    """脱敏列表（不含 api_key），供设备与 APP 拉取。

    ⚠️ 两处收紧（均为 P0-2 的止血点）：
      1. **加鉴权**。现状这个接口连 ``Depends(get_current_user)`` 都没有，
         任何人（包括完全未登录的调用方）都能拉到全量配置与角色；
      2. **加 record_type == 'config' 过滤**。角色改由 /api/roles 提供，
         平台配置不该再从"脱敏列表"里流出去 —— 这条链路正是 P0-1 事故的
         起点（拿到配置 id -> DELETE -> 设备失去对话能力）。
    """
    rows = db.scalars(
        select(models.AIProvider)
        .where(models.AIProvider.enabled.is_(True),
               models.AIProvider.record_type == models.RECORD_TYPE_CONFIG)
        .order_by(models.AIProvider.kind, models.AIProvider.priority)
    ).all()
    return list(rows)


@router.get("/catalog")
def provider_catalog(user: models.User = Depends(get_current_user)):
    """把「能力分类 / 用途 / 厂商风格」三张枚举表一起给出，便于管理页渲染下拉框。

    枚举值会随业务增长（比如以后加 kind=tts_stream），硬编码在前端的话每次都要
    改两个地方；这里做成接口，前端跟着它走就行。
    """
    return {
        "kinds": [{"value": k, "label": KIND_LABELS.get(k, k)} for k in KIND_LIST],
        "usages": [
            {"value": u, "label": USAGE_LABELS.get(u, u), "kinds": list(USAGE_ROUTES[u])}
            for u in USAGE_LIST
        ],
        "api_styles": [{"value": v, "label": t} for v, t in API_STYLES.items()],
    }


@router.get("/resolve", response_model=ResolvedModelOut)
def resolve_provider(usage: str, db: Session = Depends(get_db),
                     user: models.User = Depends(get_current_user)):
    """按用途解析出最终生效的那条配置（脱敏）。

    这是给上层用的视图：调用方只需要说「我要 chat」，不必知道平台配的是
    DeepSeek 还是通义、是 llm_main 还是退回的旧 llm。
    一条都没配时返回 503 + 「请配置 XXX」，而不是让上层拿到 None 然后炸在别处。
    """
    usage = _require_usage(usage)
    plan = resolve_plan(db, usage)          # 未知 usage 已在 _require_usage 挡掉
    if not plan:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"尚未配置「{USAGE_LABELS.get(usage, usage)}」用途的模型"
            f"（需要 {describe_kinds(usage)} 中的一种），请先在「AI 接入」里添加并启用",
        )
    kind, row = plan[0]
    return ResolvedModelOut(
        usage=usage,
        kind=kind,
        id=row.id,
        name=row.name,
        provider=row.provider,
        model=row.model,
        endpoint=row.endpoint,
        api_style=(row.api_style or "").strip() or "openai_compatible",
        supports_stream=bool(row.supports_stream),
        context_window=int(row.context_window or 0),
        timeout_sec=int(row.timeout_sec or 0) or 30,
        priority=int(row.priority or 0),
        # 首选 kind（候选链第一个）没命中才是兜底，常见的就是「退回旧 llm」
        fallback=(kind != USAGE_ROUTES[usage][0]),
    )


@router.post("/test", response_model=ModelTestOut)
def test_provider(payload: ModelTestIn, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    """按用途真打一次请求，验证配置链路是否可用（管理页的「测试连接」）。

    **不要**用这里的返回值做业务推断：它是一次性的探针。
    成功与失败都返回 200，看 ok 与 error_code。
    """
    usage = _require_usage(payload.usage)
    try:
        if usage in _EMBED_USAGES:
            res = embed(db, payload.text or "老人陪伴助手", usage=usage)
            return ModelTestOut(
                ok=True, usage=usage, kind=res.kind, provider_pk=res.provider_pk,
                provider_name=res.provider_name, model=res.model,
                vectors=res.vectors, elapsed_ms=res.elapsed_ms, attempts=res.attempts,
            )
        if usage in _RERANK_USAGES:
            docs = payload.documents or ["今天吃了降压药", "今天天气不错"]
            res2 = rerank(db, payload.text or "吃药了吗", docs, usage=usage)
            return ModelTestOut(
                ok=True, usage=usage, kind=res2.kind, provider_pk=res2.provider_pk,
                provider_name=res2.provider_name, model=res2.model,
                scores=res2.scores, elapsed_ms=res2.elapsed_ms, attempts=res2.attempts,
            )
        msgs = payload.messages or [{"role": "user", "content": "你好，请用一句话介绍自己"}]
        r = chat(db, usage, msgs)
        return ModelTestOut(
            ok=True, usage=usage, kind=r.kind, provider_pk=r.provider_pk,
            provider_name=r.provider_name, model=r.model, text=r.text,
            elapsed_ms=r.elapsed_ms, attempts=r.attempts,
        )
    except GatewayError as exc:
        # 把调用层的错误类型原样透出：no_config / auth_failed / rate_limited /
        # timeout / upstream_error 对运维的含义完全不同，不能合并成一个 500
        return ModelTestOut(
            ok=False, usage=usage, error_code=exc.code, message=exc.message,
            attempts=exc.attempts,
        )


@router.patch("/{provider_pk}", response_model=ProviderOut)
def update_provider(provider_pk: int, payload: ProviderIn, db: Session = Depends(get_db),
                    admin: models.User = Depends(get_current_admin)):
    """改一条平台配置（P0-1 修复：加 admin 校验 + record_type 断言）。

    非管理员一律 403，且**在取行之前**就拒 —— 否则 403/404 的差异会成为
    主键存在性的探测信道。管理员若传了一个非 config 的 id（角色行），返回
    404：这既防住了"把角色改成配置"，也让角色接口的语义保持唯一。
    """
    p = require_config(db, provider_pk, admin)
    if not (payload.kind or "").strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "kind 不允许置空；平台配置与角色是两类记录，角色请用 /api/roles",
        )
    before_kind = p.kind
    for k, v in payload.model_dump().items():
        setattr(p, k, v)
    # record_type / owner_id 不接受来自请求体的覆写：它们是**归属**信息，
    # 一旦被 PATCH 改成 'role'，这条配置就会从设备下发里消失。
    p.record_type = models.RECORD_TYPE_CONFIG
    p.owner_id = None
    db.commit()
    db.refresh(p)
    # kind 可能被改过（embedding -> llm），所以改前改后两个 kind 都要判
    _clear_query_cache_if_embedding(before_kind)
    _clear_query_cache_if_embedding(p.kind)
    return p


@router.delete("/{provider_pk}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider(provider_pk: int, db: Session = Depends(get_db),
                    admin: models.User = Depends(get_current_admin)):
    """删除一条平台配置（P0-1 修复：加 admin 校验 + record_type 断言 + 硬删）。

    ⚠️ 这条接口在修复前是**当前生产代码里最严重的一处越权**：任何登录用户
    都能删掉平台 LLM 配置，而 /api/device/sync 只按
    ``enabled=True AND kind.in_(DEVICE_KINDS)`` 取配置，取不到就直接让设备
    **彻底无法对话**。现在守卫在 deps.require_config 里，与其余写接口共用。

    为什么平台配置维持**硬删**而角色是软删（架构 §1.4）：
      * 平台配置没有归属者，也不被任何 agent_id 外键引用，硬删不产生孤儿；
      * 它的价值全在 api_key / endpoint，软删留着等于在库里留一份"曾经能用过"
        的凭证，是负资产；
      * 硬删意味着设备下发那条敏感查询**一个字都不用改** —— 改成软删就得给它
        加 deleted_at IS NULL，而那正是事故链路的敏感点，任何改动都是新增风险。
    """
    p = require_config(db, provider_pk, admin)
    kind = p.kind
    db.delete(p)
    db.commit()
    # 删掉向量模型后，下一次 embed 会走另一条配置或直接失败；
    # 留着旧查询向量就是「新旧向量混算」，必须清
    _clear_query_cache_if_embedding(kind)


@router.get("/export/xiaozhi")
def export_for_xiaozhi(db: Session = Depends(get_db),
                       user: models.User = Depends(get_current_user)):
    """导出成小智 server 的 selected_module 片段，便于把配置同步过去。

    只导出真正的**设备侧**平台配置（asr/llm/tts/vad）：ai_providers 里还存着
    APP 自建的 agent 角色（kind 为空），它们没有厂商和模型，混进导出片段里
    会把真实的 LLM/ASR 配置顶掉。

    同理，新的 llm_main / llm_light / embedding / rerank / vision 只由本后端
    自调用，小智那边不认识这些 key，导过去只会变成无用条目 ——
    但它们承担的「对话」角色会通过下面的 LLM 兜底折算成一条 llm 导出，
    保证只配了 llm_main 的新部署也能导出一份能用的片段。
    """
    out: dict[str, dict] = {}
    rows = db.scalars(
        select(models.AIProvider)
        .where(models.AIProvider.enabled.is_(True), models.AIProvider.kind.in_(EXPORT_KINDS))
        .order_by(models.AIProvider.kind, models.AIProvider.priority)
    ).all()
    for r in rows:
        out.setdefault(r.kind, {})
        if r.kind not in out[r.kind] or "name" not in out[r.kind]:
            out[r.kind] = {
                "name": r.provider,
                "model": r.model,
                "endpoint": r.endpoint,
                "params": r.params or {},
            }

    # LLM 兜底：一个 llm 都没配时，用解析出来的默认对话模型补位
    if "llm" not in out:
        from ..llm_gateway import resolve_row  # 局部导入避免模块级循环依赖

        row = resolve_row(db, "chat")
        if row is not None:
            out["llm"] = {
                "name": row.provider,
                "model": row.model,
                "endpoint": row.endpoint,
                "params": row.params or {},
                "note": f"由 {row.kind} 折算（未显式配置 llm）",
            }
    return {"selected_module": out}
