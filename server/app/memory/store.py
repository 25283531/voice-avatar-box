"""记忆存储层：``MemoryStore`` 抽象 + SQLite 实现 + 语料缓存。

为什么能用 SQLite，不需要向量数据库
------------------------------------
这是本模块最重要的一条选型结论，理由量化如下：

  * **规模**：单人记忆是**数百条**量级（老人一天聊几十轮，能沉淀成事实的
    通常个位数，一年也就几百条）；
  * **体积**：1000 条 × 1024 维 float32 ≈ 4MB，一条才 4KB，SQLite 的 BLOB 完全够；
  * **速度**：对 1000 条做一次暴力余弦 ≈ 一百万次乘加，纯 Python 是毫秒级，
    **比一次网络往返（哪怕目标是本机的 Qdrant）还快**；
  * **运维**：引入 Qdrant / pgvector 等于多一个要部署、要备份、要排障的容器，
    直接违反「单一 Docker 镜像」的硬约束。

所以默认路径就是「SQLite + 进程内暴力算」。等哪天记忆量真的上万了，
正确的做法是**换一个 MemoryStore 后端**（下面这个 Protocol 就是为此留的缝），
而不是在进程里手搓 HNSW。

为什么一次搜索要把整个人的记忆读出来
--------------------------------------
不是偷懒：三路信号里的 BM25 需要**全语料的 df（文档频率）**，实体路需要全量
实体倒排，而全量也就几百行。一次 SELECT 全拿出来在进程里算，比按条件筛选
再多次往返更简单也更快。为了避免每次查询都读库，这里加了一个**按
(user_id, agent_id) 分片、写入即失效**的语料缓存 —— 一秒内可能连续有多次检索
（多设备 / 追问），命中缓存后连 SQL 都不用发。

⚠️ 缓存的是**脱离会话的快照**（MemoryRow 数据类，不是 ORM 对象）：
既避免缓存了 ORM 实例后 detached 报错，也避免一个请求改了对象污染另一个请求。

⚠️ 缓存失效的时机比看起来难做对，详见 ``mark_pending`` 与 ``_versions``：
「写入时失效」是不够的，必须**事务提交之后**再失效一次，否则并发下会出现
「刚写入的记忆 300 秒内完全不可见」。

🚨 而这三层防护（_cache / _query_cache / _versions）**全是进程内 dict**：
多进程部署会让它们同时失效、已修的 Bug#1 复活。
详见本文件「缓存」小节开头的硬约束警告框。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol, Sequence, runtime_checkable

from sqlalchemy import delete, event, select, update

from .. import models
from .textutil import tokenize
from .vectors import decode_vector, encode_vector, l2_normalize

log = logging.getLogger("elder.memory.store")

# 单个 (user_id, agent_id) 参与检索的记忆上限。
# 超过就只取最近的一批：暴力检索是 O(n)，而老人真正常用的记忆永远是最近那些。
# 设 2000 是给「几年不清理」留的余量，正常根本到不了。
MAX_CORPUS_ROWS = 2000

# 语料缓存的有效期（秒）。写操作会立刻失效对应分片，TTL 只是兜底
# （防止另一个进程改了库而本进程不知道）。
CORPUS_CACHE_TTL = 300.0

# 挂在会话 ``db.info`` 上的键名：待失效分片表 / 钩子是否已挂。
_PENDING_KEY = "elder_memory_pending_invalidate"
_HOOK_KEY = "elder_memory_invalidate_hook"


def _epoch(dt: datetime | None) -> float:
    """datetime -> epoch 秒。兼容 naive/aware（SQLite 读回来可能是 aware，
    老库或 MySQL 可能给 naive，统一按 UTC 兜底，别在比较时炸掉）。"""
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ---------------------------------------------------------------- 溯源标记
# ⚠️ 这些字面量是**跨模块契约**，不是随手写的文案：
#    retrieve.trust_weight() 按它们算可信度、router 的审计展示按它们显示来源。
#    Bug#6 就是因为「手动写入」与「LLM 抽取」在库里被记成同一个标记，
#    导致家属亲眼确认过的事实反而被当成机器猜的、可信度标反。
#    所以：**新增来源类型必须同时在这里登记**，否则打分与展示会各说各话。
SOURCE_MANUAL = "家属手动确认"   # 家属/用户亲眼确认，可信度最高
SOURCE_LLM = "LLM 抽取"          # 模型自动从对话里归纳，可能抽错


@dataclass(frozen=True)
class MemoryRow:
    """一条记忆的**只读快照**（检索时的最小工作集）。

    用 frozen dataclass 而不是 ORM 对象，是为了让语料缓存可以安全地跨请求复用：
    ORM 对象带着会话状态，缓存它就是给自己埋雷。
    """

    id: int
    user_id: int
    agent_id: int | None
    content: str
    category: str
    entities: tuple[str, ...] = ()
    tokens: tuple[str, ...] = ()
    embedding: tuple[float, ...] = ()
    embedding_model: str = ""
    embedding_dim: int = 0
    salience: float = 1.0
    source_session: str = ""
    # 溯源标记（见上面的 SOURCE_* 常量）。必须进快照：
    # 可信度打分要读它，而打分发生在检索层——不带到 MemoryRow 就读不到。
    source_quote: str = ""
    created_ts: float = 0.0
    superseded: bool = False
    access_count: int = 0

    @property
    def age_days(self) -> float:
        return max(0.0, (time.time() - self.created_ts) / 86400.0)


@dataclass
class NewFact:
    """写入一条记忆所需的全部输入（不含向量，向量由 store 自己算好后带上）。"""

    content: str
    category: str = "other"
    entities: Sequence[str] = ()
    salience: float = 1.0
    source_session: str = ""
    source_quote: str = ""


@dataclass
class _CorpusEntry:
    rows: list[MemoryRow] = field(default_factory=list)
    entities: dict[str, list[int]] = field(default_factory=dict)
    entity_types: dict[str, str] = field(default_factory=dict)
    fetched_at: float = 0.0
    # 取这份快照时分片的版本号；与当前版本不一致 = 快照可能过期，一律当未命中
    version: int = 0


@runtime_checkable
class MemoryStore(Protocol):
    """记忆存储抽象。

    只有存储语义，**不含检索算法**：检索需要 embedding 客户端、需要时间权重、
    需要 fusion 系数，那些都不该是存储层该知道的东西（见 retrieve.py）。
    换后端（SQLite -> pgvector -> Qdrant）只需要再实现这些方法，
    retrieve.py 一行都不用改。
    """

    def add_fact(self, user_id: int, agent_id: int | None, fact: NewFact,
                 embedding: Sequence[float] | None = None,
                 embedding_model: str = "") -> int:
        """写入一条记忆，返回主键。**

        **只追加**：不存在「更新已有事实」的路径，这是 ADD-only 算法的底线。
        """  # noqa: D400
        ...

    def corpus(self, user_id: int, agent_id: int | None) -> list[MemoryRow]:
        """取该 (用户, 角色) 下全部**未失效**记忆的快照（带缓存）。"""
        ...

    def entities(self, user_id: int, agent_id: int | None) -> dict[str, list[int]]:
        """实体 -> 记忆 id 列表（实体倒排表）。"""
        ...

    def entity_types(self, user_id: int, agent_id: int | None) -> dict[str, str]:
        """实体 -> 类型。供查询时的实体抽取做「已知实体词典」匹配。"""
        ...

    def mark_accessed(self, memory_ids: Iterable[int]) -> None:
        """记录命中（access_count + last_accessed_at）。失败不影响检索结果。"""
        ...

    def supersede(self, memory_id: int, reason: str = "",
                  user_id: int | None = None) -> bool:
        """软失效一条记忆（家属删掉 / 判定为错）。**不真删行**，保留审计痕迹。

        ``user_id`` 传了就做归属校验（纵深防御，见 SqliteMemoryStore.supersede）。
        """
        ...

    def count(self, user_id: int, agent_id: int | None) -> int:
        """未失效记忆条数。"""
        ...


class SqliteMemoryStore:
    """SQLite 实现（实际上走 SQLAlchemy，MySQL 下同样可用）。

    ``db`` 是调用方的会话：写操作由调用方 commit，这样记忆写入能和
    「记一条 chat_message」在同一个事务里。
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------ 写
    def add_fact(self, user_id: int, agent_id: int | None, fact: NewFact,
                 embedding: Sequence[float] | None = None,
                 embedding_model: str = "") -> int:
        vec = l2_normalize(list(embedding)) if embedding else []
        row = models.AgentMemory(
            user_id=user_id,
            agent_id=agent_id,
            content=fact.content,
            category=(fact.category or models.MemoryCategory.OTHER),
            # 预分词：写入时算一次，之后每次检索都省掉全库分词
            tokens=" ".join(tokenize(fact.content)),
            entities=list(fact.entities or []),
            embedding=(encode_vector(vec) if vec else None),
            embedding_model=embedding_model or "",
            embedding_dim=(len(vec) if vec else 0),
            salience=float(fact.salience or 1.0),
            source_session=fact.source_session or "",
            source_quote=fact.source_quote or "",
            created_at=models.utcnow(),
        )
        self.db.add(row)
        # 必须先 flush 拿到主键，实体表要引用它
        self.db.flush()

        for ent in _dedup(fact.entities or ()):
            self.db.add(models.AgentMemoryEntity(
                user_id=user_id,
                agent_id=agent_id,
                entity=ent,
                entity_type=_entity_type(ent),
                memory_id=row.id,
            ))
        # 写入后让本分片的语料缓存立刻失效，否则下一次检索会看不到刚写的东西。
        # ⚠️ 这一步是**提交前**的提前失效：它挡住的是「已提交数据的陈旧快照」，
        #    但挡不住「读方在本次commit 期间把缺新行的快照写回缓存」，
        #    所以还必须由 mark_pending 在 **commit 之后**再失效一次。
        invalidate(user_id, agent_id)
        mark_pending(self.db, user_id, agent_id)
        return int(row.id)

    # ------------------------------------------------------------ 读
    def corpus(self, user_id: int, agent_id: int | None) -> list[MemoryRow]:
        entry = _cache_get(user_id, agent_id)
        if entry is not None:
            return entry.rows

        key = (user_id, agent_id)
        # 取快照**之前**先记下版本号：查库期间若有人提交（版本 +1），
        # 下面 _cache_put 会拒绝写入这份可能过期的快照。
        with _cache_lock:
            snap_version = _versions.get(key, 0)

        q = select(models.AgentMemory).where(
            models.AgentMemory.user_id == user_id,
            _agent_match(agent_id),
            models.AgentMemory.superseded_at.is_(None),
        ).order_by(models.AgentMemory.id.desc()).limit(MAX_CORPUS_ROWS)

        rows: list[MemoryRow] = []
        for m in self.db.scalars(q).all():
            vec = decode_vector(m.embedding)
            rows.append(MemoryRow(
                id=m.id,
                user_id=m.user_id,
                agent_id=m.agent_id,
                content=m.content or "",
                category=m.category or models.MemoryCategory.OTHER,
                entities=tuple(m.entities or ()),
                tokens=tuple((m.tokens or "").split()),
                embedding=tuple(vec),
                embedding_model=m.embedding_model or "",
                embedding_dim=(m.embedding_dim or len(vec)),
                salience=float(m.salience or 1.0),
                source_session=m.source_session or "",
                source_quote=m.source_quote or "",
                created_ts=_epoch(m.created_at),
                superseded=m.superseded_at is not None,
                access_count=int(m.access_count or 0),
            ))

        # 查实体倒排（与语料同批缓存，避免每次检索多一次 SQL）
        entities_map, entity_types = self._load_entities(user_id, agent_id)
        # 版本对不上就不缓存（只是白查一次库，绝不返回/缓存过期快照）
        _cache_put(user_id, agent_id, _CorpusEntry(
            rows=rows, entities=entities_map, entity_types=entity_types,
            fetched_at=time.time(), version=snap_version,
        ))
        return rows

    def _load_entities(self, user_id: int, agent_id: int | None):
        q = select(models.AgentMemoryEntity).where(
            models.AgentMemoryEntity.user_id == user_id,
            _agent_match(models.AgentMemoryEntity.agent_id, agent_id),
        )
        entities_map: dict[str, list[int]] = {}
        entity_types: dict[str, str] = {}
        for e in self.db.scalars(q).all():
            if not e.entity:
                continue
            entities_map.setdefault(e.entity, []).append(int(e.memory_id))
            entity_types.setdefault(e.entity, e.entity_type or "other")
        return entities_map, entity_types

    def entities(self, user_id: int, agent_id: int | None) -> dict[str, list[int]]:
        # 📌 这里**不能**假设「只要调过 corpus()，缓存就一定填上了」。
        # ``corpus`` 内部是 seqlock 判定：版本号在查库期间被别人 +1 时，
        # 它会**主动跳过写缓存**（宁可白查一次库，也不缓存可能过期的快照）。
        # 所以下面必须先判空、拿不到就再回源一次，而不是直接 subscript。
        # 重复调 corpus() 是安全的：命中缓存时它只是一次 dict 查找。
        entry = _cache_get(user_id, agent_id)
        if entry is None:
            self.corpus(user_id, agent_id)
            entry = _cache_get(user_id, agent_id)
        return dict(entry.entities) if entry else {}

    def entity_types(self, user_id: int, agent_id: int | None) -> dict[str, str]:
        # 同entities：缓存可能已被填充，也可能因版本冲突被跳过；
        # 拿不到就回源，仍拿不到（并发持续冲突）就返回空 dict，由调用方兜底。
        entry = _cache_get(user_id, agent_id)
        if entry is None:
            self.corpus(user_id, agent_id)
            entry = _cache_get(user_id, agent_id)
        return dict(entry.entity_types) if entry else {}

    # ------------------------------------------------------------ 维护
    def mark_accessed(self, memory_ids: Iterable[int]) -> None:
        ids = [int(i) for i in memory_ids if i]
        if not ids:
            return
        try:
            now = models.utcnow()
            self.db.execute(
                update(models.AgentMemory)
                .where(models.AgentMemory.id.in_(ids))
                .values(access_count=models.AgentMemory.access_count + 1,
                        last_accessed_at=now)
            )
        except Exception as exc:  # noqa: BLE001  统计失败不该影响检索结果
            log.warning("记忆命中计数失败：%s", exc)

    def supersede(self, memory_id: int, reason: str = "",
                  user_id: int | None = None) -> bool:
        """软失效一条记忆。

        ``user_id`` 是**纵深防御**，不是可选装饰：router 层已经用 ``_own_memory``
        校验过归属，但store 层是public API，谁都能调。不校验的话，
        新增一个调用点忘了判归属就是越权 —— 这类bug 不会报错，只会静默
        删掉别人家的记忆，属于最难查的一类。传了就必须匹配。
        """
        row = self.db.get(models.AgentMemory, int(memory_id))
        if row is None:
            return False
        if user_id is not None and int(row.user_id) != int(user_id):
            log.warning("supersede 拒绝：记忆 %s 属于 user %s，非 %s",
                        memory_id, row.user_id, user_id)
            return False
        row.superseded_at = models.utcnow()
        row.supersede_reason = (reason or "")[:255]
        # 实体索引里的行一并清掉，否则失效的记忆还会通过实体路被召回
        try:
            self.db.execute(
                delete(models.AgentMemoryEntity)
                .where(models.AgentMemoryEntity.memory_id == row.id)
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("清理实体索引失败（记忆 %s 已失效）：%s", memory_id, exc)
        # 同 add_fact：这里失效 + 提交后再失效（详见 add_fact 的注释）
        invalidate(row.user_id, row.agent_id)
        mark_pending(self.db, row.user_id, row.agent_id)
        return True

    def count(self, user_id: int, agent_id: int | None) -> int:
        from sqlalchemy import func  # 局部导入：只有这条路径需要

        return int(self.db.scalar(
            select(func.count(models.AgentMemory.id)).where(
                models.AgentMemory.user_id == user_id,
                _agent_match(agent_id),
                models.AgentMemory.superseded_at.is_(None),
            )
        ) or 0)


# ---------------------------------------------------------------- 缓存
# ╔══════════════════════════════════════════════════════════════════════╗
# ║ 🚨 硬约束：这些缓存**全部是进程内 dict**（_cache / _query_cache /  ║
# ║    _versions），跨进程**不共享、不可见**。                                ║
# ║                                                                      ║
# ║ 后果（必须记住）：一旦部署改成**多进程**（例如 uvicorn --workers 4、  ║
# ║    gunicorn 多 worker、多个容器副本、K8s 横向扩容），下面三层防护会    ║
# ║    **同时失效**，且是静默失效：                                           ║
# ║      1) _cache      —— worker A 写入后只失效 A 自己的缓存，             ║
# ║                        worker B 仍持有陈旧快照；                         ║
# ║      2) _versions   —— 版本号互不可见，A 的 invalidate 对 B 无效，       ║
# ║                        seqlock 判定永远"版本没变"，反而把过期快照        ║
# ║                        合法地写回缓存；                                 ║
# ║      3) _query_cache—— 查询向量缓存同样按进程隔离。│
# ║    症状：已修好的 Bug#1「刚写入的记忆 300 秒内完全召不回」会**复活**，   ║
# ║    而且只在多进程下出现、单进程测试永远测不出来。                        ║
# ║                                                                      ║
# ║ 现状：**单进程部署，线上不受影响**（server/Dockerfile 里没有 --workers，║
# ║    单个 uvicorn 进程）。这个约束是当前"单一 Docker 镜像、无外部依赖"  ║
# ║    硬约束的直接推论 —— 不引入 Redis 之类来跨进程共享失效信号。          ║
# ║                                                                      ║
# ║ 若将来必须多进程，**先**做这件事，否则不要动部署参数：                     ║
# ║    把失效信号换成跨进程可见的载体（Redis pub/sub、或按分片写一张         ║
# ║    invalidate 标记表并让每个 worker 每次读库前先查它），                 ║
# ║    也就是把上面三层换成等价于分布式 seqlock 的实现。                      ║
# ╚══════════════════════════════════════════════════════════════════════╝
#
# 模块级缓存：所有 SqliteMemoryStore 实例共享。
# 这一点很关键 —— HTTP 请求每次 new 一个 store，如果缓存挂在实例上就完全不生效了。
_cache: dict[tuple[int, int | None], _CorpusEntry] = {}
_cache_lock = threading.Lock()

# 每个分片的**版本号**。任何失效动作（invalidate / mark_pending）都会 +1。
#
# 为什么需要它（这是本模块最隐蔽的一个 bug 的解法）
# --------------------------------------------------
# 只在「commit 之后失效一次」是不够的，还存在一个更窄的窗口：
#   读方在 t0 记下版本号 -> t1 查库（读到的是旧数据）-> 写方在 t2 提交并失效
#   -> t3 读方把「旧数据」写回缓存（失效发生在它之后，白失效）
# 于是又出现一条 300s 内不可见的记忆。
#
# 版本号把这个窗口也关掉：读方只有在「查库期间版本号没变」时才允许写缓存
# （经典的 seqlock / 版本戳写法）。版本一变就不缓存 —— 宁可不缓存，
# 也不能缓存一份「可能过期」的快照，因为后者的代价是静默丢记忆，
# 而前者只是多查一次库。
_versions: dict[tuple[int, int | None], int] = {}


def _agent_match(agent_id: int | None, column: Any = None):
    """构造 agent_id 的匹配条件。

    ⚠️ SQL 里 ``agent_id = NULL`` 永远不成立，必须用 ``IS NULL``。
    这里统一处理，避免「通用记忆（agent_id 为空）」和「某角色的记忆」互相串台
    —— 那正是需求里「换一个角色 = 另一份人格记忆」的反面。
    """
    col = column if column is not None else models.AgentMemory.agent_id
    return col.is_(None) if agent_id is None else col == agent_id


def _cache_get(user_id: int, agent_id: int | None) -> _CorpusEntry | None:
    key = (user_id, agent_id)
    with _cache_lock:
        entry = _cache.get(key)
        version = _versions.get(key, 0)
    if entry is None:
        return None
    if time.time() - entry.fetched_at > CORPUS_CACHE_TTL:
        with _cache_lock:
            _cache.pop(key, None)
        return None
    # 版本戳对不上 = 取快照期间有人写过，这一份随时可能是旧的，直接当未命中
    if entry.version != version:
        return None
    return entry


def _cache_put(user_id: int, agent_id: int | None, entry: _CorpusEntry) -> bool:
    """写入缓存。返回是否成功。

    写入前再比一次版本号：查库 -> 组装 -> 写缓存这一段里若版本变了
    （说明期间有写事务提交），这份快照就是**提交前**的世界，绝不能进缓存。
    """
    key = (user_id, agent_id)
    with _cache_lock:
        if entry.version != _versions.get(key, 0):
            return False
        _cache[key] = entry
    return True


def invalidate(user_id: int, agent_id: int | None) -> None:
    """让某个分片的缓存失效（写入 / 删除后调用），并把该分片版本号 +1。

    版本号 +1 是必须的：光 ``pop`` 掉缓存键挡不住「读方已经读库、正在写回」
    这一路（见 _versions 的注释）。
    """
    key = (user_id, agent_id)
    with _cache_lock:
        _versions[key] = _versions.get(key, 0) + 1
        _cache.pop(key, None)


def clear_cache() -> None:
    """清空全部缓存（测试用 / 跨进程改动后的兜底）。

    版本号也要一起 +1：否则一份在clear之前取的快照仍会被后续的
    ``_cache_get`` 判定为有效（键虽然被pop 了，但别人可能已经读到了引用）。
    """
    with _cache_lock:
        _cache.clear()
        for k in list(_versions):
            _versions[k] = _versions.get(k, 0) + 1


# ---------------------------------------------------------------- 提交后失效
def mark_pending(db: Any, user_id: int, agent_id: int | None) -> None:
    """登记「这个分片有**未提交**的写入」，并保证提交后一定再失效一次。

    为什么必须在 commit 之后再失效一次
    ----------------------------------
    ``add_fact`` / ``supersede`` 内部只做了 ``flush()``，事务还没提交。
    如果只在那里失效，会出现这样一条时间线：

        A 写入 -> add_fact 里失效（未提交）
                -> B 检索：缓存 miss -> 查库**看不到未提交的新行**
                -> B 把「缺这条新记忆」的快照写回缓存
                -> A commit：新行落地，但**没有人再失效**
                -> 这条记忆在 TTL(300s) 内对检索完全不可见

    症状是「机器人刚听完的话几十秒内召不回」，自愈要等满 300 秒 ——
    这是产品可感知的故障，不是理论问题。``memorize_background`` 走后台线程 +
    独立会话，窗口更宽，语音链路下几乎必然撞上。

    对照删除路径：``supersede`` 内部也提前失效了，但 router 在 ``db.commit()``
    **之后又补了一次** —— 写入路径缺的正是这一份。这里把它做成 store 层的机制
    而不是靠每个调用点自觉，避免以后新增调用点又踩同一个坑。

    实现：给会话挂一个 ``after_commit`` 钩子 + 一份待失效分片表。
    钩子只挂一次（按会话 ``info`` 打标记），提交时按分片逐个 invalidate。
    """
    if db is None:
        # 没有会话可挂钩子（理论上不该发生）：退化成提交前失效，行为同旧版
        invalidate(user_id, agent_id)
        return

    info = getattr(db, "info", None)
    if not isinstance(info, dict):  # 非 SQLAlchemy 会话（测试替身等）
        invalidate(user_id, agent_id)
        return

    pending: set[tuple[int, int | None]] = info.setdefault(_PENDING_KEY, set())
    pending.add((int(user_id), agent_id))
    if info.get(_HOOK_KEY):
        return
    info[_HOOK_KEY] = True

    def _after_commit(_session: Any = None) -> None:
        # commit 成功 -> 数据已对其他会话可见 -> 现在失效才是「有效失效」
        done = info.get(_PENDING_KEY) or set()
        info[_PENDING_KEY] = set()
        #监听器是 once=True（触发即摘除），所以必须把「已挂钩子」的标记也清掉：
        # 否则同一个会话里的**第二次**写入（写 -> commit -> 再写 -> 再commit）
        # 会因为标记还在而不再挂钩子，第二次提交后就没有失效了。
        info[_HOOK_KEY] = False
        for u, a in done:
            invalidate(u, a)

    def _after_rollback(_session: Any = None) -> None:
        # 回滚：数据没落地，但缓存**已经被提前失效过**了（这是安全的：
        # 多失效一次只会多查一次库，绝不会读到脏数据）。清掉登记即可。
        info[_PENDING_KEY] = set()
        info[_HOOK_KEY] = False

    try:
        event.listen(db, "after_commit", _after_commit, once=True)
        event.listen(db, "after_rollback", _after_rollback, once=True)
    except Exception as exc:  # noqa: BLE001  挂不上钩子也不能影响写入
        log.warning("挂载记忆缓存提交钩子失败，退化为提交前失效：%s", exc)
        info[_HOOK_KEY] = False
        invalidate(user_id, agent_id)


# ---------------------------------------------------------------- 小工具
def _dedup(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        s = (it or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _entity_type(name: str) -> str:
    """给实体打个类型（写实体表时用；textutil 有更完整的判定，这里够用即可）。"""
    from .textutil import (ENTITY_MEDICINE, ENTITY_PERSON, ENTITY_PLACE, ENTITY_TIME,
                           extract_entities)

    pairs = extract_entities(name)
    for _, t in pairs:
        if t in (ENTITY_PERSON, ENTITY_MEDICINE, ENTITY_TIME, ENTITY_PLACE):
            return t
    return "other"
