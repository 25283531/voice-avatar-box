"""多信号检索：语义 + 关键词(BM25) + 实体，三路融合 + 时序衰减。

为什么必须三路融合，不能只用向量
----------------------------------
这是本模块最关键的一条判断，用本产品的真实场景说明：

  老人说："那个白色的降压的药叫什么来着？"

  * **语义路**（embedding 余弦）能懂"降压的药"这个意思，但真实 embedding 对
    "苯磺酸氨氯地平片"这种低频专有名词的表征非常弱（训练语料里没见过几次），
    语义上它和"阿司匹林""二甲双胍"挤在一起，分不出来；
  * **关键词路**（BM25）恰好相反：只要老人哪天照着药盒念出"苯磺酸氨氯地平片"，
    那一串字就是一次精确命中，一字不差；
  * **实体路**解决的是"跨记忆的同一个人/同一个东西"：老人说"张阿姨上次说啥"，
    "张阿姨"在库里可能出现在五六条不同记忆里，语义路只能捞到最像的一两条，
    实体路能一次性把它们全关联起来。

反过来也成立：只用 BM25 时，老人用完全不同的措辞（"家里那只小动物"vs 记忆里的"猫"）
一个字都重叠不了，直接召回失败。**两路必须融合，谁也替不了谁。**

为什么选「加权求和」而不是 RRF（倒数排名融合）
------------------------------------------------
RRF 只保留**排名**、丢掉绝对分数，只有一个超参 k，好处是不用做分数归一化。
这里不用它，有三个具体理由：

  1. **可比性已经对齐了**：三路信号在融合前都归一化到 [0,1]（余弦取正部、
     BM25 除以本批最大值、实体命中数除以本批最大值），量纲一致，加权求和是安全的；
  2. **加权求和能表达"谁更重要"**：本产品里精确药名/人名的命中是**一票否决级**
     的，需要给关键词路更高的权重；RRF 只有 k，做不到这种差异；
  3. **可解释**：融合后仍能打印每路得分（MemoryHit 里就带着），
     "为什么这条记忆被召回"能说清楚 —— 这对**给家属看的审计界面**是刚需，
     RRF 给不出这个解释。

时序：衰减 + 改口压制
----------------------
* **衰减**：长期指数衰减（默认半衰期 90 天），让陈年旧事慢慢靠后，
  近期记忆自然加权。
* **改口压制**：用户改口（"我以前养猫，现在养了条狗"）时，
  旧的陈述**不会被删除**（ADD-only），而是被较新的、语义上高度相似的
  陈述**压制**（分数打折）。这两条相乘，于是"最近说的"自动压过"以前说的"。

  ⚠️ 为什么单靠衰减不够：两句话可能只隔几秒（同一轮对话里改口），
  指数衰减在这两个时间点上算出来的权重几乎一模一样（1.0 vs 0.9999），
  根本分不出先后。所以必须补一条**只较先后、不看间隔**的压制规则。
"""
from __future__ import annotations

import logging
import math
import time
from collections import Counter, OrderedDict
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .. import config
from .client import ModelClient, get_default_client
from .store import SOURCE_LLM, SOURCE_MANUAL, MemoryRow, MemoryStore
from .textutil import extract_entities, tokenize
from .vectors import cosine, l2_normalize

log = logging.getLogger("elder.memory.retrieve")

# ---------------------------------------------------------------- 融合权重
# 三路相加，融合前会按「实际启用的信号」重新归一化，
# 所以关掉某一路（调试 / 消融对比）时剩下的权重会自动补齐到 1。
W_SEMANTIC = 0.50   # 语义：抓"意思相近"，是本产品的主力信号
W_KEYWORD = 0.30    # 关键词：抓药名/人名/专有名词，必须精确匹配的那部分
W_ENTITY = 0.20     # 实体：把同一个人的多条记忆串起来

# ---------------------------------------------------------------- 时序
HALF_LIFE_DAYS = 90.0     # 衰减半衰期：三个月前的记忆权重减半
CONTRADICTION_SIM = 0.80  # 语义相似度超过这个值，视为「同一件事的两种说法」
SUPPRESS_FACTOR = 0.55    # 被较新陈述压制时打的折（0.55 = 让位明显但不消失）
SUPPRESS_WINDOW = 24      # 只在 Top-N 候选里做压制（见 _suppress 的注释）

# BM25 经典参数
BM25_K1 = 1.5
BM25_B = 0.75

# 低于这个分数的结果不返回：宁可"没记住"，也不要把八竿子打不着的东西塞进 prompt
MIN_SCORE = 0.05

# ---------------------------------------------------------------- 来源可信度
# 为什么可信度要乘进分数，而不是只写在审计页给人看：
# 防污染闭环是「抽取 -> 打分/召回 -> 家属审计 -> 丢弃」。家属按下丢弃键时，
# 系统得先能分辨「这条是模型猜的」还是「这条是人亲口确认的」，
# 否则排序会把机器猜测和人确认摆在同一权重上，让人误删掉正确的记忆、
# 也让错误的记忆被当成事实继续用。产品承诺的「家属确认过的记忆更可信」
# 只有落进打分里，才真的成立。
#
# 数值刻意保守（0.90~1.12，跨度约 20%）：它只用来在**得分接近**的候选之间
# 决定先后，不该让一条低相关的确认记忆盖过强相关的抽取记忆
# ——那会把召回准确率换成溯源偏好，得不偿失。
TRUST_MANUAL = 1.12      # 家属/用户亲眼确认
TRUST_LLM = 0.90         # LLM 自动归纳，可能抽错
TRUST_UNKNOWN = 1.0      # 没标来源（旧数据 / 迁移来的）：不奖不罚
# 命中次数的轻量加成：反复被召回的事实，说明它在实践中确实有用。
# 同样是±小幅修正，理由与上面的来源权重一致。
TRUST_REPEAT_MAX = 1.06
TRUST_REPEAT_HALF = 20   # 命中次数达到一半就拿满加成


def trust_weight(row: MemoryRow) -> float:
    """按**来源**给这条记忆算可信度系数（乘在融合分上）。

    直接读 ``MemoryRow.source_quote`` —— 也就是入库时写下的溯源标记
    （store.SOURCE_MANUAL / SOURCE_LLM）。这就是 Bug#6 要求的
    「别只改文案，逻辑也得读这个字段」：手写与抽取的区别体现在分数上，
    而不只是审计页上的一行字。

    ⚠️ 旧数据可能存着 Bug#6 时期写错的标记（如 "家属手动补写"），
    这里对历史字面量做兼容归一，改一次就够，不必回刷全表。
    """
    src = (row.source_quote or "").strip()
    if not src:
        # 没写来源的按未知处理，但 ``source_session == "manual"``
        # 仍然是可靠的人工信号（source_session 一直是对的）。
        return TRUST_MANUAL if row.source_session == "manual" else TRUST_UNKNOWN
    if src in (SOURCE_MANUAL, "家属手动补写"):
        return TRUST_MANUAL
    if src == SOURCE_LLM:
        return TRUST_LLM
    return TRUST_UNKNOWN


def _repeat_weight(access_count: int) -> float:
    """命中次数带来的轻量加成，线性饱和到 TRUST_REPEAT_MAX。"""
    n = max(0, int(access_count or 0))
    if n <= 0:
        return 1.0
    return 1.0 + (TRUST_REPEAT_MAX - 1.0) * min(1.0, n / TRUST_REPEAT_HALF)

# 查询向量缓存上限。老人翻来覆去就那几句高频问法（"我该吃药了吗"），
# 缓存命中率其实很高。
QUERY_CACHE_MAX = 512


@dataclass
class MemoryHit:
    """一条被召回的记忆，附带**完整的打分明细**。

    带明细不是啰嗦：DEBUG 端点与家属端"为什么机器人这么说"都要用到，
    而且它能直接证明"三路融合确实在起作用"（每路各贡献了多少一目了然）。
    """

    memory_id: int
    content: str
    category: str
    score: float = 0.0
    semantic: float = 0.0
    keyword: float = 0.0
    entity: float = 0.0
    recency: float = 1.0
    suppressed: bool = False
    salience: float = 1.0
    # 来源可信度系数（见 trust_weight）：家属确认 >1.0，LLM 抽取 <1.0。
    # 随命中一起返回，是为了让「为什么这条排前面」在家属端可解释。
    trust: float = 1.0
    created_ts: float = 0.0
    entities: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.memory_id,
            "content": self.content,
            "category": self.category,
            "score": round(self.score, 4),
            "signals": {
                "semantic": round(self.semantic, 4),
                "keyword": round(self.keyword, 4),
                "entity": round(self.entity, 4),
            },
            "recency": round(self.recency, 4),
            "suppressed": self.suppressed,
            "salience": round(self.salience, 3),
            # 可信度系数一并暴露：家属端排序/审计要能看出
            # 「这条是机器猜的还是人确认的」，而不只是看到一个总分。
            "trust": round(self.trust, 3),
            "entities": list(self.entities),
            "created_at": _iso(self.created_ts),
        }


def _iso(ts: float) -> str:
    """epoch 秒 → 「2026-10-21 14:00:00」（**北京时间**）。

    ⚠️ 原来用 ``time.strftime(..., time.localtime(ts))``：依赖进程 TZ
    环境变量（容器设了、本机可能没设），同一份数据在两处显示差 8 小时。
    这个串会进调试输出与工具结果，错的时间会直接误导排查
    （本项目真机日志已经因此绕过一圈）。改用显式 ``config.TZ``。
    """
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, config.TZ).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 查询向量缓存
# 「每次搜索都把库里的记忆重新 embed 一遍」是灾难级设计，所以：
#   1. **记忆的向量在写入时算一次并落库**（见 service.memorize），之后永远不再算；
#   2. **查询的向量**在这里缓存，同一句话第二次问不再调模型。
# 于是 embed 的调用次数只和「不同的问法数量」有关，**与库里有多少条记忆无关**。
_query_cache: "OrderedDict[str, tuple[float, ...]]" = OrderedDict()


def clear_query_cache() -> None:
    """清空查询向量缓存（换了 embedding 模型后必须调用，否则新旧向量混算）。"""
    _query_cache.clear()


def _query_vector(query: str, client: ModelClient) -> tuple[float, ...]:
    text = (query or "").strip()
    if not text:
        return ()
    cached = _query_cache.get(text)
    if cached is not None:
        _query_cache.move_to_end(text)
        return cached
    try:
        vectors = client.embed([text]) or []
    except Exception as exc:  # noqa: BLE001  没配向量模型不该让检索 500
        log.warning("查询向量化失败，本次退化为「关键词 + 实体」两路检索：%s: %s",
                    type(exc).__name__, exc)
        return ()
    if not vectors or not vectors[0]:
        return ()
    vec = tuple(l2_normalize(list(vectors[0])))
    _query_cache[text] = vec
    if len(_query_cache) > QUERY_CACHE_MAX:
        _query_cache.popitem(last=False)
    return vec


# ---------------------------------------------------------------- BM25
def bm25_scores(query_tokens: Sequence[str],
                docs_tokens: Sequence[Sequence[str]]) -> list[float]:
    """BM25 打分。

    用 BM25 而不是简单的"共有词计数"，是因为后者会被**常见词**绑架：
    "的降""老人"这种 bigram 在每条记忆里都出现，共现计数高但毫无信息量。
    BM25 的 idf 项自动把它们压下去，tf 饱和项则防止长记忆靠堆词刷分。
    """
    n = len(docs_tokens)
    if n == 0:
        return []

    df: Counter[str] = Counter()
    tfs: list[Counter[str]] = []
    lens: list[int] = []
    for toks in docs_tokens:
        tf = Counter(toks)
        tfs.append(tf)
        lens.append(len(toks))
        for t in tf:
            df[t] += 1

    avgdl = (sum(lens) / n) or 1.0
    qset = {t for t in query_tokens if t}
    scores: list[float] = []
    for tf, dl in zip(tfs, lens):
        s = 0.0
        for t in qset:
            f = tf.get(t, 0)
            if not f:
                continue
            idf = math.log(1.0 + (n - df[t] + 0.5) / (df[t] + 0.5))
            denom = f + BM25_K1 * (1.0 - BM25_B + BM25_B * dl / avgdl)
            s += idf * (f * (BM25_K1 + 1.0)) / denom
        scores.append(s)
    return scores


def _normalize(values: Sequence[float]) -> list[float]:
    """把一路信号归一化到 [0,1]（除以本批最大值），让三路可比。"""
    top = max(values) if values else 0.0
    if top <= 0:
        return [0.0] * len(values)
    return [max(0.0, v) / top for v in values]


# ---------------------------------------------------------------- 时序
def _decay(created_ts: float) -> float:
    """长期指数衰减：半衰期 HALF_LIFE_DAYS，最老也不会到 0（保底 0.05）。

    保底是为了避免"三年前的一条重要用药信息"被彻底归零 ——
    老人的用药史恰恰是越老越有参考价值。
    """
    if created_ts <= 0:
        return 1.0
    age_days = max(0.0, (time.time() - created_ts) / 86400.0)
    return max(0.05, 0.5 ** (age_days / HALF_LIFE_DAYS))


def _suppress(hits: list[MemoryHit], rows: dict[int, MemoryRow]) -> None:
    """改口压制：同一件事，较新的陈述压过较旧的陈述。

    ⚠️ 只在 Top-N 候选里做（SUPPRESS_WINDOW）：两两算相似度是 O(n²)，
    全库做在 1000 条时就是 50 万次余弦，谁也扛不住；而"改口"只可能发生在
    **都被这次查询命中**的少数记忆之间，超出窗口的记忆本来也进不了 prompt。
    """
    n = min(len(hits), SUPPRESS_WINDOW)
    for i in range(n):
        newer = rows.get(hits[i].memory_id)
        if newer is None or not newer.embedding:
            continue
        for j in range(i + 1, n):
            older = rows.get(hits[j].memory_id)
            if older is None or not older.embedding:
                continue
            # hits 已按 (分数, 时间) 排好，但仍显式比较时间戳：
            # 排序依据是分数，不是时间，"谁更新"必须看 created_ts
            if older.created_ts > newer.created_ts:
                continue
            if cosine(newer.embedding, older.embedding) >= CONTRADICTION_SIM:
                hits[j].score *= SUPPRESS_FACTOR
                hits[j].suppressed = True


# ---------------------------------------------------------------- 主入口
def search(
    store: MemoryStore,
    query: str,
    user_id: int,
    agent_id: int | None,
    *,
    top_k: int = 8,
    client: ModelClient | None = None,
    signals: Iterable[str] = ("semantic", "keyword", "entity"),
    apply_time_decay: bool = True,
) -> list[MemoryHit]:
    """召回与 ``query`` 最相关的记忆。

    ``signals`` 可以只传一路（如 ``("keyword",)``），用于调试与效果对比 ——
    权重会自动在启用的信号间归一化，所以单路结果仍在同一量纲上可比。
    """
    use = set(signals or ())
    rows = list(store.corpus(user_id, agent_id))
    if not rows:
        return []

    # ---- 关键词路 ----
    q_tokens = tokenize(query)
    kw_raw = bm25_scores(q_tokens, [r.tokens for r in rows]) if ("keyword" in use and q_tokens) \
        else [0.0] * len(rows)
    kw_norm = _normalize(kw_raw)

    # ---- 语义路 ----
    sem_raw: list[float] = [0.0] * len(rows)
    q_vec: tuple[float, ...] = ()
    if "semantic" in use:
        cli = client or get_default_client()
        q_vec = _query_vector(query, cli)
        if q_vec:
            for idx, r in enumerate(rows):
                if not r.embedding:
                    continue
                # 维度不一致 = 换过 embedding 模型，旧向量不能和新向量一起算余弦
                # （等于拿两把不同的尺子量长度），直接判定为 0，等重建。
                if r.embedding_dim and r.embedding_dim != len(q_vec):
                    continue
                sem_raw[idx] = max(0.0, cosine(q_vec, r.embedding))
    # 余弦本身就是 [-1,1]，语义路不需要再归一化（保留绝对强度更有信息量）
    sem_norm = [max(0.0, min(1.0, v)) for v in sem_raw]

    # ---- 实体路 ----
    ent_raw: list[float] = [0.0] * len(rows)
    q_entities: list[str] = []
    if "entity" in use:
        known = store.entity_types(user_id, agent_id)
        q_entities = [name for name, _ in extract_entities(query, known)]
        if q_entities:
            qset = {e.lower() for e in q_entities}
            for idx, r in enumerate(rows):
                hit = sum(1 for e in r.entities if e.lower() in qset)
                ent_raw[idx] = float(hit)
    ent_norm = _normalize(ent_raw)

    # ---- 加权融合 ----
    weights = {
        "semantic": W_SEMANTIC if "semantic" in use else 0.0,
        "keyword": W_KEYWORD if "keyword" in use else 0.0,
        "entity": W_ENTITY if "entity" in use else 0.0,
    }
    total_w = sum(weights.values()) or 1.0

    hits: list[MemoryHit] = []
    for idx, r in enumerate(rows):
        base = (weights["semantic"] * sem_norm[idx]
                + weights["keyword"] * kw_norm[idx]
                + weights["entity"] * ent_norm[idx]) / total_w
        if base <= 0:
            continue
        recency = _decay(r.created_ts) if apply_time_decay else 1.0
        # salience 只做小幅加权（0.75~1.15）：重要度是"锦上添花"，
        # 不该让一条高 salience 的无关记忆盖过低 salience 的强命中
        sal = 0.75 + 0.4 * max(0.0, min(1.5, r.salience))
        # 来源可信度：家属确认过的记忆在得分接近时排在抽取记忆之前。
        # 这是 Bug#6 的落地点 —— 可信度必须真的进分数，而不只是审计页上的一行文案。
        trust = trust_weight(r) * _repeat_weight(r.access_count)
        hits.append(MemoryHit(
            memory_id=r.id,
            content=r.content,
            category=r.category,
            score=base * recency * sal * trust,
            semantic=sem_norm[idx],
            keyword=kw_norm[idx],
            entity=ent_norm[idx],
            recency=recency,
            salience=r.salience,
            trust=trust,
            created_ts=r.created_ts,
            entities=r.entities,
        ))

    # 先按融合分排序，再在头部窗口内做改口压制，最后重新排序
    hits.sort(key=lambda h: (-h.score, -h.created_ts))
    rows_by_id = {r.id: r for r in rows}
    _suppress(hits, rows_by_id)
    hits.sort(key=lambda h: (-h.score, -h.created_ts))

    out = [h for h in hits if h.score >= MIN_SCORE][:top_k]
    if out:
        try:
            store.mark_accessed([h.memory_id for h in out])
        except Exception as exc:  # noqa: BLE001  计数失败不影响本次召回
            log.debug("记忆命中计数失败：%s", exc)
    return out


def debug_scores(store: MemoryStore, query: str, user_id: int,
                 agent_id: int | None, client: ModelClient | None = None,
                 top_k: int = 5) -> dict[str, Any]:
    """把「每路信号单独检索」和「融合检索」的结果一起返回。

    只用于 DEBUG 端点与冒烟测试：它要跑四次打分（三路 + 融合），
    代价是正常检索的四倍，不能进主链路。有了它才能直接回答
    「只用向量会漏掉什么」这类问题。
    """
    single = {s: [h.as_dict() for h in
                  search(store, query, user_id, agent_id, top_k=top_k,
                         client=client, signals=(s,))]
              for s in ("semantic", "keyword", "entity")}
    fused = [h.as_dict() for h in
             search(store, query, user_id, agent_id, top_k=top_k, client=client)]
    return {"query": query, "single_signals": single, "fused": fused}
