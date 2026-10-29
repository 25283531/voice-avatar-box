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
from .textutil import entity_names, fingerprint, normalize

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

什么不该抽（这一节最容易出错，**逐条对照**）：
- 寒暄与礼貌用语（你好、谢谢、再见、我知道了）；
- **给机器人下的指令与操作**：调音量/调亮度/建提醒/删东西/放歌/查天气。
  这些是"要做的事"，不是"关于老人的事实"。日程类只抽**老人自己的安排**
  （"每周三去透析""每天早上八点吃降压药"），不抽"帮我定个八点的闹钟"；
- **只发生一次的动作**："我喝完了""我起来了""我吃过了"。
  例外：本身是健康或安全事件时要抽（摔了一跤、去了医院、忘了吃药）；
- **纯时间或纯数字的碎片**："今天中午12:45""是今天中午十二点四十五"。
  一条事实必须带上"做什么"，只有时间不构成事实；
- **读不通的碎片**：语音识别有错字、句子残缺、前后不搭（如"31天要上晚都"）。
  这种情况**宁可不抽**，绝对不要猜、不要替老人补全成一句看似合理的话；
- 机器人自己说的话不算老人的事实（除非老人明确附和或确认）；
- 重复的事实：同一条信息只输出一次；同一件事有多种说法时，只输出**最完整**那句。

每条 fact 必须**独立可理解**，不要出现"他""这个""上次"这类依赖上下文的指代，
要把指代替换成人名或事物名。

⚠️ **必须改写成陈述句，不要照抄原话。**
原话「提醒我12:45下楼」本身不是事实；若他确实需要长期记住这件事，
改写成「老人 12:45 要下楼办事」才算。凡是以「我」「帮我」「提醒我」「把…」
开头的**照抄**，一律不要输出 —— 服务端会把这类原话直接丢掉（见 _is_low_value）。

每条 fact 必须是一句**通顺、有主谓、能独立看懂**的中文。写完自己读一遍：
读着别扭、缺主语、像是半句话 —— 说明抽错了，删掉。"""

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


# ================================================================ 低价值闸门
#
# 为什么提示词写了"不要抽"还要再来一道代码闸门
# ------------------------------------------------
# 2026-10-29 用户报「记忆是空的」，查库时顺带看到 21 条记忆里有 9 条是垃圾：
#
#     创建一个下午12点55分的提醒 / 提醒我12:45下楼 / 今天中午12:45
#     是今天中午十二点四十五 / 声音调到30 / 屏幕调暗一点儿
#     你的日常删除吧 / 31天要上晚都 / 我扔两天儿吧。我得。我的。
#
# 它们的共同点是**把老人原话照抄下来**，而不是写成"关于老人的事实"。
# 提示词已经把「不要照抄」写进去了，但模型（尤其是小模型）在这件事上极不稳定，
# 而记忆一旦写脏就再也洗不干净（ADD-only，只追加）。所以必须有一道
# **不依赖模型听话**的闸门兜底 —— 与 voice/tools.py 的 `_is_placeholder_title`
# 同一条设计：模型给的参数直接落库的工具，都要有服务端自己的合法性判据。
#
# 判据刻意保守：**只丢"明显不是事实"的**，可疑的一律放行。理由不对称 ——
# 误丢一条真实记忆（老人说了却没记住）比多留一条废话（检索时被 BM25 压下去）
# 代价大得多。
_STOP_CHARS = frozenset(
    # 虚词与系动词
    "的了是在有和就都也还要把被给对向从与及或吧呢啊吗呀哦嗯很太最不没别"
    # 方位/量词
    "上下里外个些点儿"
    # 时间单字（时间由 _TIME_WORDS 整体剥掉，剩下的单字时间词没有信息量）
    "天晚早号月年日时秒分"
    # 代词与指示
    "我你他她它们这那哪个么什怎"
)
_TIME_WORDS = (
    "今天", "明天", "昨天", "后天", "前天", "每天", "每月", "每周", "每年",
    "今年", "去年", "明年", "明早", "明晚", "今早", "今晚", "昨早", "昨晚",
    "早上", "上午", "中午", "下午", "晚上", "夜里", "凌晨", "半夜",
    "星期", "礼拜", "周末", "点钟", "点半", "分钟", "小时", "现在", "刚才",
)
#: 主语样板：说"老人怎么怎么样"时的套话，不算实义字（但也不丢弃）。
_BOILERPLATE = ("老人", "用户", "自己", "机器人", "对方")
_DIGIT_RE = re.compile(r"[0-9]+")

#: 命令式开头：这是"给机器人下指令"，不是关于老人的事实。
_IMPERATIVE_RE = re.compile(
    r"^(?:请|麻烦|帮我|给我|替我|帮忙|快|你|您)?"
    r"(?:创建|新建|建一个|设一个|定一个|加一个|设置|设定|添加|增加|"
    r"删|删除|删掉|去掉|取消|关掉|关闭|打开|开启|调|调整|调大|调小|"
    r"放|播放|唱|暂停|停止|提醒我|提醒一下|叫我|喊我|查一下|查查|告诉我)"
)
#: 设备操作对象开头：模型把「声音调到30」重写成名词开头时，同样不是事实。
_DEVICE_RE = re.compile(r"^(?:声音|音量|屏幕|亮度|灯光|彩灯|灯|麦克风|静音|字体)")
#: 一次性完成态动作（"我喝完了""我起来了"）——不是长期事实。
#: 只在**非**健康/事件分类时丢：摔了一跤、去了医院这类必须留。
_DONE_ACTION_RE = re.compile(r"^(?:我|老人)?(?:已经|刚刚|刚才|刚)?[^，。；]{1,10}(?:了|啦)$")
#: 片段切分（句读与空白）。用于识别 ASR 碎片。
_SEG_SPLIT_RE = re.compile(r"[，。；！？、,.;!?\s]+")

#: 时间表达：只做**抽取**不做解析。同一件事反复被说起时，时间说法几乎总一致，
#: 拿来当去重键足够；解析成绝对时间会把"每周三"和"明天"这种区别弄丢。
_TIME_TOKEN_RE = re.compile(
    r"每天|每月|每周|每年|工作日|周末|今天|明天|昨天|后天|前天|明早|明晚|今早|今晚|"
    r"早上|上午|中午|下午|晚上|夜里|凌晨|半夜|星期[一二三四五六日天]|"
    r"[0-9一二两三四五六七八九十]{1,2}\s*点"
    r"(?:\s*(?:半|[0-9一二两三四五六七八九十]{1,3}\s*分?))?"
)
#: 口语时间词先展开成标准写法，否则「明早八点」和「明天早上八点」会算成两件事。
_TIME_ALIAS = (
    ("明早", "明天早上"), ("明晚", "明天晚上"), ("今早", "今天早上"),
    ("今晚", "今天晚上"), ("昨早", "昨天早上"), ("昨晚", "昨天晚上"),
    ("明儿", "明天"), ("今儿", "今天"), ("昨儿", "昨天"),
)
#: 数字折叠：让「八点」和「8点」算同一个时间（只用于比较，不用于展示）。
_CN_DIGIT = str.maketrans("零〇一二两三四五六七八九十", "0012234567899")


def _expand_time(text: str) -> str:
    s = text
    for a, b in _TIME_ALIAS:
        s = s.replace(a, b)
    return s


def _meaningful_chars(text: str) -> str:
    """取出"实义字"：剥掉时间表达、主语样板、数字、虚词后剩下的字。

    不做真正的分词（见 textutil 的设计原则：零新增依赖），只按字表筛。
    对"这句话有没有信息量"这个判断来说足够了 —— 它不需要知道词边界。

    ⚠️ 必须**先**用 ``_TIME_TOKEN_RE`` 整段剥掉时间表达，再逐字筛。
    只按字表筛挡不住中文数字：「是今天中午十二点四十五」里的"十二""四十五"
    既不是虚词也不是阿拉伯数字，会被当成 5 个实义字而放行 —— 而这正是
    生产库里那条垃圾记忆（2026-10-29）。
    """
    s = normalize(text)
    s = _TIME_TOKEN_RE.sub(" ", _expand_time(s))
    for w in _BOILERPLATE + _TIME_WORDS:
        s = s.replace(w, " ")
    s = _DIGIT_RE.sub(" ", s)
    return "".join(ch for ch in s if ch not in _STOP_CHARS and not ch.isspace())


def _is_low_value(fact: Fact) -> bool:
    """明显不是"关于老人的长期事实"的，丢掉。**可疑的一律放行**（见上面注释）。"""
    text = (fact.content or "").strip()
    if not text:
        return True

    # ① 给机器人下的指令 / 设备操作
    if _IMPERATIVE_RE.match(text) or _DEVICE_RE.match(text):
        return True
    # 「你的日常删除吧」：第二人称开头 + 删除类动作 = 指令，不是事实
    if text[0] in "你您" and re.search(r"删|取消|去掉|关", text):
        return True

    # ② 实义字太少（纯时间、纯数字、残句）
    if len(_meaningful_chars(text)) < 3:
        return True

    # ③ ASR 碎片：被句读切出来的段落里有「一两个实义字」的
    #    （"我扔两天儿吧。我得。我的。" -> "我得""我的"）
    # 判据是「被切成三截以上、其中至少两截只有一两个字」：
    # 只看"存在一小截"会误杀正常的多分句（"老人每天早上六点起床，晚上九点睡"，
    # 后半截剥掉时间词就只剩一个"睡"字，但它是一句完整的话）。
    segs = [s for s in _SEG_SPLIT_RE.split(text) if s]
    tiny = sum(1 for s in segs if len(_meaningful_chars(s)) < 2)
    if len(segs) >= 3 and tiny >= 2:
        return True

    # ④ 一次性完成态动作（"我已经喝完水了"）。
    #    判据用 (用药, 事件) 而不是 MemoryCategory.IMPORTANT ——
    #    IMPORTANT 含 schedule，而"我已经喝完水了"恰恰被模型标成 schedule。
    if fact.category not in (MemoryCategory.MEDICINE, MemoryCategory.EVENT) \
            and _DONE_ACTION_RE.match(text):
        return True

    return False


# ---------------------------------------------------------------- 跨轮重复
def _time_key(text: str) -> str:
    """把句子里的时间表达抽出来做键（只抽取、不解析）。

    解析成绝对时间会把"每周三"和"明天"这种区别弄丢；而"明早"和"明天早上"
    必须先展开成同一种写法，否则同一件事的两种说法会算成两件。
    """
    norm = _expand_time(normalize(text))
    toks = [t.replace(" ", "").translate(_CN_DIGIT)
            for t in _TIME_TOKEN_RE.findall(norm)]
    return "|".join(sorted(set(toks)))


def dedup_key(fact: Fact) -> tuple[str, frozenset[str]] | None:
    """跨轮去重的键：``(时间键, 实体集合)``。返回 None 表示**不参与**去重。

    只对**用药与日程**两类生效 —— 这两类的重复最伤：同一条"明天早上八点吃鼻炎药"
    被反复说起时，库里会堆出四五条几乎一样的记忆，检索时同一条信息被放大四五倍，
    而家属在记忆页看到的就是一屏重复。

    ⚠️ 三个"宁可漏"的保守处：
      * 没有实体就返回 None（没有"这件事是什么"的锚点，比了也不可信）；
      * 时间键必须**完全相等**（"每天早上八点"和"明天早上八点"是不同的安排，
        绝不能合并）；
      * 分类不在 IMPORTANT 里就返回 None（喜好/画像这类反复说很正常）。
    """
    return dedup_key_of(fact.content, fact.category, fact.entities)


def dedup_key_of(content: str, category: str,
                 entities: Sequence[str] | None) -> tuple[str, frozenset[str]] | None:
    """同 :func:`dedup_key`，但直接吃原始字段。

    单独开一个是因为 ``service.memorize`` 要拿**库里已有的记忆行**（不是 Fact）
    来构造键集合 —— 让那边自己拼一份等价逻辑，早晚会和这里分叉。
    """
    if category not in MemoryCategory.IMPORTANT:
        return None
    ents = frozenset(e for e in (entities or ()) if e)
    if not ents:
        return None
    return _time_key(content), ents


def is_duplicate(fact: Fact, existing: set[tuple[str, frozenset[str]]]) -> bool:
    """这条事实是否与已有记忆**同时间、同实体**。"""
    key = dedup_key(fact)
    if key is None:
        return False
    tkey, ents = key
    if not tkey:
        return False
    return any(tkey == t and (ents & e) for t, e in existing)


def extract_facts(
    conversation: Sequence[Any],
    *,
    client: ModelClient | None = None,
    known_entities: dict[str, str] | None = None,
    max_facts: int = MAX_FACTS_PER_TURN,
    device_id: int | None = None,
    user_id: int | None = None,
) -> list[Fact]:
    """从一轮对话里抽事实。**只抽不比对、只追加不修改**，且绝不抛异常。

    参数
    ----
    conversation  一轮对话，支持 Turn / dict / (role, content) / 纯字符串
    client        模型客户端；不传就用 ``get_default_client()``
    known_entities 已有实体词典，用于补全实体（查询时的实体路要靠它）
    device_id / user_id
                  用量记账的归属。**必须显式传**：本函数跑在
                  ``memorize_background`` 的裸线程里，contextvars 不会
                  传播过去；行上没有 device_id，用户端按设备过滤就永远
                  看不到这条轻量模型的调用。

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
        raw = cli.chat(USAGE_LIGHT, messages, temperature=0.0,
                       device_id=device_id, user_id=user_id)
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
    # 同一轮里模型自己吐两条同义事实（"明天八点吃药" / "明早八点要吃药"）也要挡：
    # 跨轮去重在 service.memorize 里做（那里能查库），这里只管本轮。
    seen_keys: set[tuple[str, frozenset[str]]] = set()
    dropped = 0
    for f in facts:
        if not f.is_valid() or _is_small_talk(f) or _is_low_value(f):
            dropped += 1
            continue
        key = fingerprint(f.content)
        if key in seen:
            continue
        seen.add(key)
        # 实体补全：LLM 漏给实体时，用规则补一遍。
        # 实体路的召回完全依赖这里，宁可多补也不要漏。
        # ⚠️ 必须在去重键判定**之前**补 —— 否则 LLM 没给实体的那条
        #    会被 dedup_key 判成"不参与去重"而漏掉。
        if not f.entities:
            f.entities = entity_names(f.content, known_entities)
        elif known_entities:
            for name in entity_names(f.content, known_entities):
                if name not in f.entities:
                    f.entities.append(name)
        if is_duplicate(f, seen_keys):
            dropped += 1
            continue
        dk = dedup_key(f)
        if dk is not None:
            seen_keys.add(dk)
        out.append(f)
        if len(out) >= max_facts:
            break

    log.info("事实提取：本轮 %d 句对话 -> %d 条事实（低价值/重复丢弃 %d 条）",
             len(turns), len(out), dropped)
    return out
