"""事实提取：**单次 ADD-only 提取**。

这是借鉴 Mem0 2026 新算法的核心一条，也是本模块和「老式记忆系统」最大的分歧点。

为什么不做 UPDATE / DELETE（只 ADD）
-------------------------------------
老算法（Mem0 2025 及更早、以及大多数自研记忆）是**两遍式**的：

    第一遍：从对话里抽出候选事实
    第二遍：把候选事实和库里已有事实逐条比对，判断该 ADD / UPDATE / DELETE

这条路有三个绕不开的毛病：

  1. **慢**：每轮对话要两次 LLM 调用，而且第二遍要把库里相关记忆全塞进 prompt，
     老人说完一句话要等更久才听到回应；
  2. **会丢信息**：一旦判定为 UPDATE，旧值就被新值**覆盖**了。
     可"我以前养猫，现在养狗"里，养过猫这件事本身也是有效信息
     （哪天老人说"以前那只猫"，系统答不上来就很怪）；
  3. **判定本身会错**：让 LLM 在「同一件事的两种说法」之间做增删改的决策，
     是典型的易错任务，错了就是不可逆的数据丢失。

ADD-only 的做法：**只追加，永不覆盖**。矛盾的信息允许并存，
由检索时的**时序打分**决定当下该信哪一条（见 retrieve.py 的"改口压制"）。
于是：

  * 提取延迟直接砍掉一半（只剩一次 LLM）；
  * 数据只增不减，不存在"被 LLM 误判删掉"的可能；
  * 时间维度天然可回溯 —— 家属能看到"上个月他是这么说的"。

⚠️ 唯一保留的"非追加"操作是**完全相同的句子不重复写**（幂等去重）。
同一轮对话被重复提交时不该产生两条一模一样的记忆，这与 UPDATE 无关。

健壮性要求（硬性）
-------------------
``extract_facts`` **在任何情况下都不许抛异常**。它跑在对话结束后的收尾阶段，
一旦抛出去就是 500，而用户（老人）根本感知不到这次失败，只会觉得
"机器人怎么有时候记不住"。所以：模型没配、返回非法 JSON、返回空串、
返回一堆垃圾 —— 一律记日志 + 返回空列表。宁可这次没记住，也不能把对话打断。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..models import MemoryCategory
from .client import USAGE_LIGHT, ModelClient, get_default_client
from .textutil import entity_names, fingerprint

log = logging.getLogger("elder.memory.extract")

# 每轮最多抽多少条。LLM 偶尔会发疯把整段对话拆成二十条，
# 真正常用的长期事实一轮也就 1~3 条，设上限既省存储也防止噪声淹没检索。
MAX_FACTS_PER_TURN = 8

# 明显没有长期价值的套话：属于"对话礼貌用语"，写进记忆只会稀释检索结果
_SMALL_TALK = (
    "你好", "早上好", "晚上好", "谢谢", "好的", "嗯嗯", "知道了", "没事", "再见",
    "在吗", "你在吗", "哈哈", "哦哦",
)

_EXTRACT_SYSTEM = """你是一个**事实提取器**，服务于一位老年人的陪伴机器人。

任务：从下面的对话里提取**值得长期记住**的事实。

输出要求（必须严格遵守）：
1. 只输出一个 JSON 数组，不要任何解释、不要 Markdown 代码围栏、不要用 ``` 包裹；
2. 数组里每个元素形如：
   {"fact": "一句独立完整的话", "category": "medicine", "entities": ["苯磺酸氨氯地平片"], "salience": 0.9}
3. category 只能取这些值之一：
   profile(个人情况) / preference(喜好习惯) / event(发生的事) /
   relationship(人物关系) / medicine(用药健康) / schedule(日程安排) / other(其它)
4. entities 列出这句话里的人名/称谓、药名、地点、时间，没有就给空数组；
5. salience 是 0~1 的重要度。

什么该抽：
- **罕见但关键**的信息优先：药物名称与用法、过敏史、疾病、就诊与透析安排、
  家属/邻居的姓名与关系、重要日程、住址、生活习惯；
- 用户明确表达的好恶（爱听京剧、不吃香菜、讨厌被催）；
- 已经发生的重要事件（上周摔了一跤、昨天女儿来看他）。

什么不该抽：
- 寒暄与礼貌用语（你好、谢谢、再见）；
- 只在这一轮有用的临时信息（"现在几点"、"帮我放首歌"）；
- 你的猜测和推断 —— 只抽对话里**明确说出来**的；
- 重复的事实：同一条信息只输出一次。

每条 fact 必须**独立可理解**，不要出现"他""这个""上次"这类依赖上下文的指代，
要把指代替换成人名或事物名。"""

_EXTRACT_USER = """对话内容：
{conversation}

请输出 JSON 数组。"""


@dataclass
class Turn:
    """一轮对话里的一句话。"""

    role: str  # user / assistant / system
    content: str


@dataclass
class Fact:
    """一条被抽出来的事实（还没落库）。"""

    content: str
    category: str = MemoryCategory.OTHER
    entities: list[str] = field(default_factory=list)
    salience: float = 1.0
    source_quote: str = ""

    def is_valid(self) -> bool:
        return bool(self.content) and len(self.content.strip()) >= 2


def as_turns(conversation: Sequence[Any]) -> list[Turn]:
    """把各种输入形态统一成 Turn 列表。

    调用方可能是语音链路（自带 dataclass）、HTTP 接口（dict）、
    或者测试里随手写的一串字符串，这里都收下 —— 这种"输入宽容"放在边界上是值得的。
    """
    turns: list[Turn] = []
    for item in conversation or ():
        if isinstance(item, Turn):
            turns.append(item)
        elif isinstance(item, dict):
            turns.append(Turn(role=str(item.get("role", "user")),
                              content=str(item.get("content", ""))))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            turns.append(Turn(role=str(item[0]), content=str(item[1])))
        elif isinstance(item, str):
            turns.append(Turn(role="user", content=item))
    return [t for t in turns if (t.content or "").strip()]


def _render(conversation: Sequence[Any]) -> str:
    """把对话渲染成给 LLM 看的文本。"""
    labels = {"user": "老人", "assistant": "机器人", "system": "系统"}
    lines: list[str] = []
    for t in as_turns(conversation):
        who = labels.get((t.role or "").strip().lower(), t.role or "?")
        lines.append(f"{who}：{t.content.strip()}")
    return "\n".join(lines)


def _salience_of(category: str, raw: Any) -> float:
    """重要度：LLM 给的优先，没给就按分类兜底。

    用药与日程**默认更高**：对老人来说，吃错药、错过透析的代价远大于
    "记错了他喜欢听什么戏"。
    """
    try:
        val = float(raw)
    except (TypeError, ValueError):
        val = 0.0
    if val <= 0:
        val = 0.95 if category in MemoryCategory.IMPORTANT else 0.6
    return max(0.1, min(1.0, val))


def _normalize_category(raw: Any) -> str:
    cat = str(raw or "").strip().lower()
    return cat if cat in MemoryCategory.LIST else MemoryCategory.OTHER


def _parse_facts(raw: str) -> list[Fact]:
    """解析 LLM 输出。**任何异常都吞掉**，返回空列表（见模块头部的健壮性要求）。

    做三件事是为了对抗真实 LLM 的输出习惯：
      1. 剥掉 ```json 代码围栏（模型非常爱加，尤其是小模型）；
      2. 找不到合法 JSON 时，截取第一个 ``[`` 到最后一个 ``]`` 再试一次
         （模型经常在 JSON 前后各说一句废话）；
      3. 模型偶尔给 ``{"facts": [...]}`` 而不是数组，一并兼容。
    """
    if not raw or not raw.strip():
        log.info("事实提取：模型返回空内容，本次不写入记忆")
        return []

    text = raw.strip()
    fenced = re.search(r"```(?:json|JSON)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()

    data: Any = None
    # ① 先按整段解析。② 失败时用 raw_decode **只吃掉第一个 JSON 值**，
    #    忽略后面的废话。
    #
    # 2026-10-22 真机日志：`Extra data: line 1 column 3 (char 2)` —— 模型
    # 返回的是 `{}` 或 `[]` 后面又跟了一段说明文字。原实现第二道兜底只找
    # `[` 到 `]`，遇到 `{}` 打头的输出直接找不到方括号，于是**每轮都跳过**，
    # 记忆一条都没写进去（日志里永远是 "-> 0 条事实"）。
    # raw_decode 正是为"JSON 后面还有多余字符"设计的，比截取区间稳。
    decoder = json.JSONDecoder()
    try:
        data = decoder.raw_decode(text)[0]
    except (json.JSONDecodeError, ValueError):
        # ③ 最后再试一次：截取第一个 `[` 到最后一个 `]`（JSON 前面有废话时）
        start, end = text.find("["), text.rfind("]")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end + 1])
            except (json.JSONDecodeError, ValueError) as exc:
                log.warning("事实提取：返回内容不是合法 JSON，本次跳过（%s）", exc)
                return []
        else:
            log.warning("事实提取：返回内容里找不到 JSON，本次跳过（前 80 字：%s）",
                        text[:80])
            return []

    if isinstance(data, dict):
        for key in ("facts", "memories", "items", "data", "result"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = []
    if not isinstance(data, list):
        log.warning("事实提取：返回结构不是数组，本次跳过")
        return []

    facts: list[Fact] = []
    for item in data:
        if not isinstance(item, dict):
            # 也容忍 ["一句话", "另一句"] 这种退化输出
            if isinstance(item, str) and item.strip():
                facts.append(Fact(content=item.strip()))
            continue
        content = str(item.get("fact") or item.get("content")
                      or item.get("memory") or item.get("text") or "").strip()
        if not content:
            continue
        category = _normalize_category(item.get("category"))
        entities = item.get("entities")
        if not isinstance(entities, list):
            entities = []
        facts.append(Fact(
            content=content[:500],
            category=category,
            entities=[str(e).strip() for e in entities if str(e).strip()][:12],
            salience=_salience_of(category, item.get("salience")),
        ))
    return facts


def _is_small_talk(fact: Fact) -> bool:
    """过滤套话：这些写进记忆只会稀释检索结果。"""
    key = fingerprint(fact.content)
    if not key:
        return True
    if len(key) < 4 and fact.category == MemoryCategory.OTHER:
        # "好""嗯" 这一类：没有任何检索价值
        return True
    return any(key == fingerprint(w) for w in _SMALL_TALK)


def extract_facts(
    conversation: Sequence[Any],
    *,
    client: ModelClient | None = None,
    known_entities: dict[str, str] | None = None,
    max_facts: int = MAX_FACTS_PER_TURN,
) -> list[Fact]:
    """从一轮对话里抽事实。**只抽不比对、只追加不修改**，且绝不抛异常。

    参数
    ----
    conversation  一轮对话，支持 Turn / dict / (role, content) / 纯字符串
    client        模型客户端；不传就用 ``get_default_client()``
    known_entities 已有实体词典，用于补全实体（查询时的实体路要靠它）

    返回
    ----
    去重、过滤、补齐实体后的 Fact 列表。出错时返回空列表。
    """
    turns = as_turns(conversation)
    if not turns:
        return []

    cli = client or get_default_client()
    rendered = _render(turns)
    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user", "content": _EXTRACT_USER.format(conversation=rendered)},
    ]

    # ⚠️ 这里吞掉一切异常是**有意为之**，理由见模块头部"健壮性要求"。
    # 记忆提取是"锦上添花"，它挂了绝不能连累正在进行的对话。
    try:
        raw = cli.chat(USAGE_LIGHT, messages, temperature=0.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("事实提取调用模型失败，本次不写入记忆：%s: %s",
                    type(exc).__name__, exc)
        return []

    try:
        facts = _parse_facts(raw)
    except Exception as exc:  # noqa: BLE001  解析环节的兜底（正常不应触发）
        log.warning("事实提取解析异常，本次不写入记忆：%s: %s", type(exc).__name__, exc)
        return []

    out: list[Fact] = []
    seen: set[str] = set()
    for f in facts:
        if not f.is_valid() or _is_small_talk(f):
            continue
        key = fingerprint(f.content)
        if key in seen:
            continue
        seen.add(key)
        # 实体补全：LLM 漏给实体时，用规则补一遍。
        # 实体路的召回完全依赖这里，宁可多补也不要漏。
        if not f.entities:
            f.entities = entity_names(f.content, known_entities)
        elif known_entities:
            for name in entity_names(f.content, known_entities):
                if name not in f.entities:
                    f.entities.append(name)
        out.append(f)
        if len(out) >= max_facts:
            break

    log.info("事实提取：本轮 %d 句对话 -> %d 条事实", len(turns), len(out))
    return out
