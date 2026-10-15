"""记忆 / 人格的 HTTP 接口。

两个 router：
  * ``router``        /api/memory/**       —— 用户端（家属 APP 自己看自己家的）
  * ``admin_router``  /api/admin/memory/** —— 管理端

权限边界（本文件最重要的一条设计）
--------------------------------
**所有端点一律按当前登录用户的 user_id 过滤，管理员也不例外。**

这不是疏忽，是刻意的：记忆是这个家庭最私密的数据（用药、病情、子女地址、
家庭矛盾），运营人员因为「能进管理端」就顺手看到这些，是典型的越权。
管理端只提供一类不涉及内容的能力：

  * ``/api/admin/memory/stats`` —— 只给**聚合计数**（谁的记忆条数偏多），
    不返回任何一条记忆的正文，用于排查「某个账号是不是抽取失控了」。

需要看具体内容时（如申诉排障），由用户本人走用户端接口，
或按项目的数据合规流程单独走，不做成一个「管理员一键查看」的接口。

⚠️ 路由顺序
-----------
FastAPI 按**声明顺序**匹配，所以所有字面量路径（``/persona/profile``、
``/turns``、``/facts``）必须声明在 ``/{memory_id}`` 之前，
否则 ``GET /api/memory/persona/profile`` 会先命中 ``/{memory_id}`` 并因
类型不匹配报 422。下面刻意按「字面量在前、参数在后」排列，改动时别调换。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import get_current_admin, get_current_user
from . import service
from .client import get_default_client
from .persona import (build_persona_prompt, delete_example, discard_audit,
                      list_audits, list_examples, record_feedback, tone_profile,
                      update_manual)
from .schemas import (ChatTurnOut, FeedbackOut, MemoryContextIn, MemoryFactIn,
                      MemoryFeedbackIn, MemoryHitOut, MemoryOut, MemorySearchIn,
                      MemorySearchOut, MemorySignalsOut, MemoryTurnIn,
                      MemoryWriteOut, PersonaManualIn, SimpleOut, ToneProfileOut)
from .store import SOURCE_MANUAL, NewFact, SqliteMemoryStore, invalidate
from .textutil import extract_entities

log = logging.getLogger("elder.memory.router")

router = APIRouter(prefix="/api/memory", tags=["memory"])
admin_router = APIRouter(
    prefix="/api/admin/memory", tags=["admin-memory"],
    dependencies=[Depends(get_current_admin)],
)

_LIST_LIMIT = Query(200, ge=1, le=500)


# ---------------------------------------------------------------- 工具
def _agent_or_400(db: Session, agent_id: int | None) -> int | None:
    """校验 agent_id 指向的确实是**一个角色**。

    刻意校验而不是照单全收：一个不存在的 agent_id 会让写入的记忆**永远召不回来**
    （检索按 agent_id 精确匹配），而表里看不出任何异常，属于典型的静默数据丢失。

    ⚠️ 另一条同样重要的判据：必须是 ``is_role()`` 而不是"行存在即可"。
    ai_providers 里还有平台 LLM/TTS/ASR 配置（record_type='config'），
    把它们当agent_id 写进记忆/画像，会造出一批"挂在一个模型配置上"的记录：
    列表里看着正常，但永远不会有对话产生这些数据，且**没有任何报错**。
    400 而不是 404：这里是"你传的参数不对"，调用方需要知道原因。
    """
    if agent_id is None:
        return None
    row = db.get(models.AIProvider, int(agent_id))
    if row is None or not row.is_role():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "角色不存在")
    return int(agent_id)


def _agent_match(column, agent_id: int | None):
    """agent_id 的匹配条件。

    ⚠️ SQL 里 ``agent_id = NULL`` 永远不成立，必须用 ``IS NULL``。
    详见 memory/store.py 的 _agent_match 注释。
    """
    return column.is_(None) if agent_id is None else column == agent_id


def _own_memory(db: Session, user: models.User, memory_id: int) -> models.AgentMemory:
    """取一条**属于当前用户**的记忆，否则一律 404。

    刻意返回 404 而不是 403：403 等于告诉外人「这个 id 存在」，
    等于开了一个可枚举的探测接口（顺便泄漏别人有多少条记忆）。
    """
    row = db.get(models.AgentMemory, int(memory_id))
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "记忆不存在")
    return row


def _hit_out(hit) -> MemoryHitOut:
    return MemoryHitOut(
        id=hit.memory_id,
        content=hit.content,
        category=hit.category,
        category_text=models.MemoryCategory.TEXTS.get(hit.category, hit.category),
        score=round(float(hit.score), 4),
        signals=MemorySignalsOut(
            semantic=round(float(hit.semantic), 4),
            keyword=round(float(hit.keyword), 4),
            entity=round(float(hit.entity), 4),
        ),
        suppressed=bool(hit.suppressed),
        # 可信度系数出接口：家属端要能区分「机器猜的」与「人确认过的」。
        trust=round(float(hit.trust), 4),
        created_ts=float(hit.created_ts),
    )


def _memory_out(row: models.AgentMemory) -> MemoryOut:
    return MemoryOut(
        id=row.id,
        content=row.content,
        category=row.category,
        category_text=models.MemoryCategory.TEXTS.get(row.category, row.category),
        entities=list(row.entities or []),
        salience=float(row.salience or 0.0),
        source_session=row.source_session or "",
        source_quote=row.source_quote or "",
        created_at=row.created_at,
        superseded_at=row.superseded_at,
        supersede_reason=row.supersede_reason or "",
        access_count=int(row.access_count or 0),
    )


# ================================================================ 字面量路径（必须在参数路由之前）
@router.post("/search", response_model=MemorySearchOut)
def search_memories(payload: MemorySearchIn, db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    """搜索记忆。

    排序完全交给检索层（三路融合 + 时序），这里**不**做二次过滤：
    任何「看起来不相关」的判断都不该在接口层做 —— 接口层看不到三路得分，
    硬过滤只会把「关键词精确命中」的好结果误杀。
    """
    agent_id = _agent_or_400(db, payload.agent_id)
    hits = service.search(db, payload.q, user.id, agent_id, top_k=payload.top_k)
    return MemorySearchOut(
        query=payload.q,
        hits=[_hit_out(h) for h in hits],
        context=service.build_context(db, payload.q, user.id, agent_id,
                                      top_k=payload.top_k),
        total=len(hits),
    )


@router.post("/context", response_model=SimpleOut)
def memory_context(payload: MemoryContextIn, db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    """只要拼好的一段 prompt 文本（语音链路 / 调试页用）。

    单开一个端点而不是在 search 结果里带 context：APP 调试页要结构化命中列表，
    语音链路要一段文本，合成一个接口只会让两边都多拿一半无用字段。
    """
    agent_id = _agent_or_400(db, payload.agent_id)
    ctx = service.build_context(db, payload.q, user.id, agent_id, top_k=payload.top_k)
    return SimpleOut(ok=True, detail=ctx)


@router.get("/turns", response_model=list[ChatTurnOut])
def list_turns(limit: int = Query(30, ge=1, le=200),
               agent_id: int | None = Query(None),
               session_id: str | None = Query(None),
               db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    """最近说过的话（原话，不做提炼）。

    存在的意义是**可核对**：记忆是机器抽的，家属有权回到原话确认
    「它凭什么这么记我」。没有这一层，机器的概括就成了唯一可信的版本。
    """
    agent_id = _agent_or_400(db, agent_id)
    rows = service.recent_turns(db, user.id, agent_id, session_id, limit=limit)
    return [ChatTurnOut(**r) for r in rows]


@router.post("/facts", response_model=MemoryWriteOut,
             status_code=status.HTTP_201_CREATED)
def add_fact(payload: MemoryFactIn, db: Session = Depends(get_db),
             user: models.User = Depends(get_current_user)):
    """手动补一条记忆（不经过 LLM 抽取）。

    给「机器人漏记了」准备的兜底：家属明确知道「爸每周三去透析」，
    让他去教机器人怎么抽取既不现实也没必要。
    """
    agent_id = _agent_or_400(db, payload.agent_id)
    category = (payload.category if payload.category in models.MemoryCategory.LIST
                else models.MemoryCategory.OTHER)
    # 手动补写也要抽实体：否则这条记忆**永远不会被实体路召回**
    #（"张阿姨最近怎么样"这类指代提问全靠实体路），等于写进去却半残。
    # 抽取走textutil 的规则版，零成本、不需要模型。
    entities = [name for name, _ in extract_entities(payload.content)]
    store = SqliteMemoryStore(db)
    try:
        vectors = get_default_client().embed([payload.content])
    except Exception as exc:  # noqa: BLE001  没配向量模型也要能写进去
        log.warning("手动写记忆时向量化失败，按无向量写入：%s", exc)
        vectors = []
    mid = store.add_fact(
        user.id, agent_id,
        NewFact(content=payload.content.strip(), category=category,
                entities=entities,
                salience=(payload.salience if payload.salience is not None else 1.0),
                source_session="manual",
                # 手动补写没有「从哪句话抽来的」这个来源，记成 SOURCE_MANUAL：
                # 留空会让详情页的溯源一栏空白，运营排查时看不出是人工写的。
                # ⚠️ 用常量而不是字面量文案：这个值不只用于展示，
                #    retrieve.trust_weight() 按它给可信度加权（Bug#6：
                #    手动写入曾被记成 "LLM 抽取"，把亲眼确认过的事实
                #    标成了机器猜的，可信度恰好标反）。
                source_quote=SOURCE_MANUAL),
        embedding=(vectors[0] if vectors else None),
    )
    db.commit()
    # ⚠️ 必须在 commit **之后** 再失效一次（与 delete_memory 同理）。
    #    add_fact 内部那次失效发生在事务提交前，只能挡住「已提交数据的陈旧快照」；
    #    若不在此补一次，并发读方会把「缺这条新记忆」的快照写回缓存，
    #    这条记忆将在 TTL(300s) 内对检索完全不可见 —— 症状是
    #    「机器人刚听完的话几十秒内召不回」。详见 store.mark_pending 的注释。
    invalidate(user.id, agent_id)
    return MemoryWriteOut(ok=True, ids=[mid], count=1, reason="")


# ---------------------------------------------------------------- 人格画像
@router.get("/persona/profile", response_model=ToneProfileOut)
def get_tone(agent_id: int | None = Query(None), db: Session = Depends(get_db),
             user: models.User = Depends(get_current_user)):
    """语气画像（数值 + 人话描述）。

    路径放在 ``/persona/profile`` 而不是 ``/persona/{x}``：``/api/memory/{id}``
    已经是记忆详情的路由，字面量路径必须排在它前面才不会抢匹配。
    """
    agent_id = _agent_or_400(db, agent_id)
    return ToneProfileOut(**tone_profile(db, user.id, agent_id))


@router.patch("/persona/profile", response_model=ToneProfileOut)
def patch_tone(payload: PersonaManualIn, agent_id: int | None = Query(None),
               db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    """家属手动纠正语气画像。

    人永远压过机器：自动演化再准也会有偏差，而家属一句「别跟他开玩笑」
    比任何统计都权威。
    """
    agent_id = _agent_or_400(db, agent_id)
    out = update_manual(db, user.id, agent_id, payload.model_dump(exclude_unset=True))
    db.commit()
    return ToneProfileOut(**out)


@router.get("/persona/prompt", response_model=SimpleOut)
def get_persona_prompt(agent_id: int | None = Query(None),
                       db: Session = Depends(get_db),
                       user: models.User = Depends(get_current_user)):
    """当前会注入 system prompt 的那段人格提示词（只读预览）。

    给家属一个「所以机器人现在到底是按什么在说话」的透明窗口。
    """
    agent_id = _agent_or_400(db, agent_id)
    return SimpleOut(ok=True, detail=build_persona_prompt(db, user.id, agent_id))


# ---------------------------------------------------------------- 偏好示例
@router.get("/persona/examples", response_model=list[dict])
def get_examples(status_: str | None = Query(None, alias="status"),
                 limit: int = Query(100, ge=1, le=300),
                 agent_id: int | None = Query(None),
                 db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    """偏好示例池（正例 / 反例）。

    直接返回 dict 而不套 Pydantic：这一层的形状由 ``persona._example_out``
    决定（含 kind_text / source_text 等展示字段），再抄一份 schema 等于把
    同一份字段清单维护两遍，改一处忘一处是迟早的事。
    """
    agent_id = _agent_or_400(db, agent_id)
    return list_examples(db, user.id, agent_id, status=status_, limit=limit)


@router.delete("/persona/examples/{example_id}", response_model=SimpleOut)
def remove_example(example_id: int, db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user),
                   reason: str = Query("", max_length=255)):
    """作废一条学到的偏好。

    这是「防污染」闭环的最后一环：机器人学错了，家属能立刻让它忘掉，
    而且**软作废**（status=discarded），审计里查得到「谁在什么时候说这条不算数」。
    """
    agent_id = _example_agent(db, user, example_id)
    ok = delete_example(db, user.id, agent_id, example_id,
                        reason=reason or "家属判定为错误学习")
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "示例不存在")
    db.commit()
    return SimpleOut(ok=True, detail="已作废，机器人不会再按这条说话")


@router.post("/feedback", response_model=FeedbackOut, status_code=status.HTTP_201_CREATED)
def post_feedback(payload: MemoryFeedbackIn, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    """记录一次反馈信号。

    **先判定再入库**：判定为无效的反馈也会留一条审计（valid=False），
    家属因此能看到「机器人刚才差点被逗，还好没学」——
    这比事后无法解释要好得多。
    """
    agent_id = _agent_or_400(db, payload.agent_id)
    out = record_feedback(db, user.id, agent_id, payload.user_text,
                          bot_text=payload.bot_text,
                          prev_bot_text=payload.prev_bot_text,
                          hint=payload.signal, force=payload.force)
    db.commit()
    return FeedbackOut(
        ok=True,
        valid=bool(out["valid"]),
        signal=str(out["signal"] or ""),
        inferred=str(out["inferred"] or ""),
        reason=str(out["reason"] or ""),
        audit_id=int(out["audit_id"] or 0),
        example_id=out["example_id"],
    )


# ---------------------------------------------------------------- 审计（防污染）
@router.get("/persona/audits", response_model=list[dict])
def get_audits(valid: bool | None = Query(None),
                limit: int = Query(100, ge=1, le=300),
                agent_id: int | None = Query(None),
                db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    """审计列表：机器人「以为自己学到了什么」。

    ``?valid=false`` 专门看被挡下来的那些 —— 那才是防污染机制真正在干活的证据。
    """
    agent_id = _agent_or_400(db, agent_id)
    return list_audits(db, user.id, agent_id, only_valid=valid, limit=limit)


@router.delete("/persona/audits/{audit_id}", response_model=SimpleOut)
def remove_audit(audit_id: int, db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user),
                 reason: str = Query("", max_length=255)):
    """作废一条学习记录（连同它产出的示例）。

    留痕不删行：作废掉的学习也要能查，否则事后没人能解释
    「机器人上周为什么会那样说话」。
    """
    agent_id = _audit_agent(db, user, audit_id)
    ok = discard_audit(db, user.id, agent_id, audit_id,
                       reason=reason or "家属判定为错误学习")
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "审计记录不存在")
    db.commit()
    return SimpleOut(ok=True, detail="已作废")


# ================================================================ 参数路由（放最后）
@router.post("", response_model=MemoryWriteOut, status_code=status.HTTP_201_CREATED)
def memorize_turns(payload: MemoryTurnIn, db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    """把一轮对话沉淀成长期记忆。

    这里走**同步**实现（不是 memorize_background）：HTTP 调用方是 APP，
    它明确需要「写完了」的回执来刷新列表。记忆抽取要走一次 LLM，
    慢在几百毫秒量级，APP 可以接受；语音链路那种「不能等」的场景才用后台线程。
    """
    agent_id = _agent_or_400(db, payload.agent_id)
    ids = service.memorize(db, user.id, agent_id, payload.turns,
                           session_id=payload.session_id)
    reason = "" if ids else "本轮没有新增记忆（没有值得记的事实，或与已有记忆重复）"
    return MemoryWriteOut(ok=True, ids=ids, count=len(ids), reason=reason)


@router.get("", response_model=list[MemoryOut])
def list_memories(limit: int = _LIST_LIMIT,
                 agent_id: int | None = Query(None),
                 include_superseded: bool = Query(False),
                 db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    """我的记忆列表（按写入时间倒序）。

    ``include_superseded=true`` 才带出已作废的条目：家属偶尔需要确认
    「我删过什么」，但默认列表里堆一堆灰色划掉的记忆会让 APP 很难看懂。
    """
    agent_id = _agent_or_400(db, agent_id)
    q = select(models.AgentMemory).where(
        models.AgentMemory.user_id == user.id,
        _agent_match(models.AgentMemory.agent_id, agent_id),
    )
    if not include_superseded:
        q = q.where(models.AgentMemory.superseded_at.is_(None))
    rows = db.scalars(q.order_by(models.AgentMemory.id.desc()).limit(limit)).all()
    return [_memory_out(r) for r in rows]


@router.get("/{memory_id}", response_model=MemoryOut)
def get_memory(memory_id: int, db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    """看一条记忆的详情（含它是从哪句话抽出来的）。"""
    return _memory_out(_own_memory(db, user, memory_id))


@router.delete("/{memory_id}", response_model=SimpleOut)
def delete_memory(memory_id: int, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user),
                  reason: str = Query("", max_length=255)):
    """删掉一条记忆。

    **软失效**（superseded_at）而不是删行：记忆是机器人行为的唯一解释依据，
    真删掉之后「机器人为什么会提这件事」就再也查不出源头了。
    删除原因一并记下，家属在审计视图里能看到自己删过什么、为什么删。
    """
    row = _own_memory(db, user, memory_id)
    store = SqliteMemoryStore(db)
    # user_id 一并传下去做纵深防御：即使将来有人新增调用点绕过了 _own_memory，
    # store 层也不会让越权的删除生效。
    if not store.supersede(row.id, reason or "家属在 APP 里删除", user_id=user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "记忆不存在")
    db.commit()
    # ⚠️ 必须在 commit 之后。supersede 内部那次失效发生在提交前，
    #    只挡住「已提交数据的陈旧快照」；并发读方仍可能把「这条还在」的
    #    快照写回缓存，导致删除后仍能被检索到，直到 TTL 到期。
    #    详见 store.mark_pending 的注释。
    invalidate(row.user_id, row.agent_id)
    return SimpleOut(ok=True, detail="已删除，该记忆不会再被想起")


# ---------------------------------------------------------------- agent 定位
def _audit_agent(db: Session, user: models.User, audit_id: int) -> int | None:
    """定位一条审计的 agent_id，并确认它属于当前用户。

    为什么不从查询参数取 agent_id：``persona.discard_audit`` 内部按
    ``(user_id, agent_id)`` 做等值匹配，若让调用方自己传，传错（传成 NULL）
    会导致一条属于角色的审计**永远删不掉**。从库里读自己这行的 agent_id
    是唯一不会传错的做法。
    """
    row = db.get(models.AgentPersonaAudit, int(audit_id))
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "审计记录不存在")
    return row.agent_id


def _example_agent(db: Session, user: models.User, example_id: int) -> int | None:
    """定位一条偏好示例的 agent_id（理由同 _audit_agent）。"""
    row = db.get(models.AgentPreferenceExample, int(example_id))
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "示例不存在")
    return row.agent_id


# ================================================================ 管理端
@admin_router.get("/stats")
def admin_stats(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    """**只有聚合计数，不含任何一条记忆正文**。

    用途只有一个：排查「某个账号是不是记忆抽取失控了」（条数异常暴涨，
    通常意味着 LLM 在把闲聊当事实记）。要看具体内容必须由用户本人授权，
    这里刻意不提供按 id 查看的接口 —— 理由见模块头部注释。
    """
    rows = db.execute(
        select(
            models.AgentMemory.user_id,
            func.count(models.AgentMemory.id).label("memories"),
            # 用 case 而不是方言函数：func.iif 只有 SQLite 支持，
            # 换 MySQL 部署时这里会直接报函数不存在。
            func.sum(case((models.AgentMemory.superseded_at.is_not(None), 1),
                          else_=0)).label("superseded"),
        ).group_by(models.AgentMemory.user_id)
        .order_by(func.count(models.AgentMemory.id).desc())
        .limit(limit)
    ).all()
    return {
        "items": [{"user_id": r.user_id,
                   "memories": int(r.memories or 0),
                   "superseded": int(r.superseded or 0)} for r in rows],
        "note": "只含聚合计数；管理员不可查看任何用户的记忆正文",
    }
