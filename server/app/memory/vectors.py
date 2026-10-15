"""向量的编解码与相似度（纯标准库，不引 numpy）。

为什么不用 numpy
----------------
项目 requirements.txt 里没有 numpy（虽然本机碰巧装了），为了「单一镜像 + 小体积」
不打算新增这个依赖。而本场景对它的需求极弱：

  * 单人记忆是**数百条**量级，1000 条 × 1024 维 float32 也只有 4MB；
  * 检索时对全库做一次余弦，1000 × 1024 ≈ 一百万次乘加，
    CPython 的 ``sum(map(mul, a, b))`` 在毫秒级就能跑完，
    **比一次网络往返（哪怕只去本地 Qdrant）还快**；
  * 存储用标准库 ``array`` 的 ``tobytes()``，比 pickle 更紧凑也更安全
    （pickle 反序列化是不可信输入的经典攻击面，虽然这里数据是自己写的，
    但能不用就不用）。

结论：为这点计算量引入 Qdrant 属于给自己加运行时负担，为这点计算量引入 numpy
属于给自己加镜像体积。真到了记忆上万条那一天，正确的做法也是换 MemoryStore 后端
（见 store.py），而不是在进程里做向量索引。

⚠️ 字节序：``array('f').tobytes()`` 用的是**本机字节序**。同一份库文件只在
同一台机器上读写，所以没问题；但库文件跨机器拷贝（x86 ↔ ARM 大端）后向量会读错。
因此每条记忆都记了 ``embedding_model``，将来做版本/平台迁移时据此判定要不要重算。
"""
from __future__ import annotations

import math
from array import array
from typing import Sequence

# float32 足够：embedding 的数值精度本来就在 1e-3 量级，
# 用 float64 只是把 BLOB 体积翻倍（每条 4KB -> 8KB）。
TYPE_CODE = "f"


def encode_vector(vec: Sequence[float]) -> bytes:
    """把浮点序列序列化成 BLOB（float32，本机字节序）。"""
    return array(TYPE_CODE, (float(x) for x in vec)).tobytes()


def decode_vector(blob: bytes | None) -> list[float]:
    """把 BLOB 反序列化回浮点列表；空/损坏返回空列表（不抛异常）。

    损坏静默返回空是刻意的：一条记忆的向量坏了，最坏的结果是它不参与语义打分，
    另外两路信号（关键词 / 实体）仍然能召回它；抛异常则会让整次检索 500。
    """
    if not blob:
        return []
    try:
        arr = array(TYPE_CODE)
        arr.frombytes(blob)
        return list(arr)
    except Exception:  # noqa: BLE001  向量损坏不应打断检索
        return []


def l2_normalize(vec: Sequence[float]) -> list[float]:
    """L2 归一化。归一化后余弦相似度 == 点积，检索时省一次开方。"""
    total = math.fsum(float(x) * float(x) for x in vec)
    if total <= 0.0:
        return [float(x) for x in vec]
    inv = 1.0 / math.sqrt(total)
    return [float(x) * inv for x in vec]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    """点积。长度不一致时按较短的那个算（向量维度漂移时降级而非报错）。"""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    if len(a) != len(b):
        # 维度不一致通常意味着换了 embedding 模型，这里只算公共前缀，
        # 结果会偏低但不会因为越界崩掉；store 层另有 embedding_model 字段做正经校验。
        return math.fsum(a[i] * b[i] for i in range(n))
    return math.fsum(x * y for x, y in zip(a, b))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度，返回 [-1, 1]；任一侧为空向量返回 0。"""
    if not a or not b:
        return 0.0
    norm_a = math.fsum(x * x for x in a)
    norm_b = math.fsum(x * x for x in b)
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot(a, b) / math.sqrt(norm_a * norm_b)


def dimensions(blob: bytes | None) -> int:
    """看 BLOB 里有多少维（不解码出列表，省一次拷贝）。"""
    if not blob:
        return 0
    return len(blob) // 4
