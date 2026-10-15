"""长期记忆 + 人格演化（借鉴 Mem0 2026 的 ADD-only 算法，自行实现）。

为什么自己写而不用 Mem0 / Letta / Graphiti
------------------------------------------
这三个项目都很好，但它们的自托管形态分别要 PostgreSQL+Qdrant / PostgreSQL+pgvector
/ Neo4j，等于往镜像里再塞 2~3 个服务容器，直接违反本项目「单一 Docker 镜像」的
硬性约束（见 docs/架构设计.md 第 1、6 节）。**算法是公开的，实现是自己的**——
这里只借鉴它们公开的三条核心机制，存储层用内置 SQLite 自己落地：

  1. **ADD-only 提取**：每轮对话结束只调**一次** LLM 抽事实并**追加**，
     不做 UPDATE / DELETE（见 extract.py）；
  2. **多信号检索**：语义 + 关键词(BM25) + 实体 三路融合（见 retrieve.py）；
  3. **时序推理**：新陈述靠时间衰减与「改口压制」压过旧陈述，而不是删掉旧记忆。

包内分层
--------
  client.py    模型调用适配层（可注入，默认桥接 app/llm_gateway.py）
  textutil.py  中文二元组分词 + 规则实体抽取（零依赖）
  vectors.py   向量编解码 / 余弦（纯标准库 array，不引 numpy）
  store.py     MemoryStore 抽象 + SQLite 实现 + 语料缓存
  extract.py   事实提取（ADD-only）
  retrieve.py  三路融合检索 + 时序衰减
  persona.py   偏好示例池 + 语气画像 + 防污染 + 审计
  service.py   对上层的门面（语音链路 / HTTP 都调这里）
  schemas.py   记忆模块的 Pydantic 出入参（独立于 app/schemas.py）
  router.py    HTTP 接口（用户端 /api/memory + 管理端 /api/admin/memory）
  bridge.py    语音链路接入点（说话前召回 / 说话后异步沉淀）

对外只需要记住三个函数
----------------------
  memorize(db, user_id, agent_id, turns)   # 对话结束后写记忆
  search(db, query, user_id, agent_id)     # 说话前召回记忆
  build_context(...) / build_persona_prompt(...)  # 组装注入 prompt 的文本
"""
from __future__ import annotations

from .bridge import remember_after_reply, recall_for_reply
from .client import (ModelClient, ModelClientNotReady, get_default_client,
                     set_default_client, reset_default_client)
from .extract import Fact, Turn, extract_facts
from .persona import (FeedbackJudgement, judge_feedback, record_feedback,
                      delete_example, list_examples, list_audits, discard_audit,
                      tone_profile, describe_tone, build_persona_prompt)
from .retrieve import MemoryHit, search as retrieve_search
from .service import (build_context, memorize, memorize_background, search,
                      recent_turns)
from .store import MemoryRow, MemoryStore, SqliteMemoryStore
from .textutil import extract_entities, tokenize
from .vectors import cosine, decode_vector, encode_vector

__all__ = [
    # 模型适配
    "ModelClient", "ModelClientNotReady", "set_default_client", "get_default_client",
    "reset_default_client",
    # 提取
    "Turn", "Fact", "extract_facts",
    # 存储
    "MemoryStore", "SqliteMemoryStore", "MemoryRow",
    # 检索
    "retrieve_search", "MemoryHit", "search",
    # 门面
    "memorize", "memorize_background", "build_context", "recent_turns",
    # 语音链路接入点
    "recall_for_reply", "remember_after_reply",
    # 人格
    "FeedbackJudgement", "judge_feedback", "record_feedback", "delete_example",
    "list_examples", "list_audits", "discard_audit", "tone_profile",
    "describe_tone", "build_persona_prompt",
    # 文本与向量工具
    "tokenize", "extract_entities", "encode_vector", "decode_vector", "cosine",
]
