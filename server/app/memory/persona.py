"""人格与语气演化：偏好示例池 + 语气画像 + **防污染与审计**。

明确不做模型微调
----------------
微调成本极高（算力、数据、每次改都要重训），而且**不可逆**：一旦把某个老人的
说话习惯训进权重，想撤回来只能重训。对家庭场景来说这是不可接受的风险。
这里用三条低成本路径达到"越用越贴合"的效果：

  1. **偏好示例池**（主要手段）：把真实对话里的反馈沉淀成正/反例，
     作为 few-shot 动态注入 system prompt。改一条示例立刻生效，删掉立刻失效；
  2. **语气画像**（数值化 + 可解释）：持续统计平均句长 / 口头禅 / 是否爱开玩笑 /
     称呼习惯 / 语速偏好，生成**自然语言描述**，既注入 prompt 也给家属看；
  3. **人工可改**（兜底）：自动演化永远是**增益**，不能锁死手动修改
     （角色的 system_prompt 家属随时能改，画像的每一档也能手动覆盖）。

⚠️ 防污染（本模块最重要的部分）
--------------------------------
老人的表达可能含糊，也可能被旁人逗弄。**"哈哈哈"可能是觉得好笑，
也可能是觉得机器人蠢得可笑**。如果来者不拒地把所有反馈当学习信号，
机器人会被逗几次就学歪，而且学歪了家属还不知道为什么。

所以每条推断入库前都要过 ``judge_feedback()`` 这道闸门，并且：

  * 判定为无效的也**照样写审计**（valid=False），家属能看到
    "机器人刚刚差点被逗，还好没学"；
  * 生效的推断在审计里带人话描述（"机器人学到了什么"）和判定理由；
  * 家属一键作废，作废也留痕。

**这一层比自动学习本身更重要**：没有它，"越用越懂用户"就变成
"越用越不知道它在想什么"。

闸门为什么用规则而不用 LLM
--------------------------
用 LLM 判断"这算不算有效反馈"听起来更聪明，但有两个硬伤：

  1. **慢**：这活在每一轮对话后都要跑，多一次 LLM 就是几百毫秒；
  2. **它自己也会被污染**：让模型判断一句含糊的话，等于把不确定性
     从一个黑盒搬到另一个黑盒，而且**没法给家属解释**为什么这么判。

规则是确定的、零延迟的、每一条判定都能给出一句人话理由 ——
这恰好是审计最需要的东西。
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from sqlalchemy import select

from .. import models
from .textutil import normalize, sanitize_for_prompt

log = logging.getLogger("elder.memory.persona")

# ---------------------------------------------------------------- 噪声特征
# 只有笑声 / 语气词：文档里点名的典型歧义场景（"哈哈哈"可能是笑机器人蠢）
_LAUGH_ONLY = re.compile(r"^[哈呵嘿嘻嗨噢哦啊哎唉嗯\n\W\d]+$")

# 玩梗 / 逗弄 / 恶意纠正。命中即判无效：
# 这些话的真实意图是"逗机器人玩"，不是"教机器人说话"
_TEASE_KEYWORDS = (
    "你是机器人", "你是不是", "机器人吧", "你是傻", "你真笨", "真笨", "好蠢", "太蠢",
    "笨死了", "逗你玩", "逗你的", "骗你的", "骗你", "随便说说", "闹着玩", "跟你玩",
    "学我说话", "跟着我说", "重复我说", "说个笑话", "讲个笑话", "背个诗", "唱个歌",
    "没事", "没意思", "无聊", "滚", "闭嘴",
)

# 明确纠正的开头。命中后还要看后面有没有实质内容（见 judge_feedback）
_NEGATION_RE = re.compile(
    r"^(?:不是|不对|不行|不对的|错了|说错了|写错了|别|不要|不用|甭|不要这么|别说|"
    r"我是说|我的意思是|我说的是|意思是|应该说|你应该|你能不能|能不能|你最好|"
    r"换个说法|换个方式|重说|再说一遍)"
)

# 纠正之后剩下的部分要把这些引导词再剥一次（"不是，我的意思是X" -> "X"）
_LEADING_NOISE_RE = re.compile(r"^(?:我的意思是|我说的是|意思是|应该说|就是说|我是说|那个|这个|嗯|啊|哦)+")

_PUNCT_RE = re.compile(r"^[，。、！？,.\s:：;；~～-]+")


@dataclass
class FeedbackJudgement:
    """一次反馈信号的判定结果。"""

    valid: bool
    signal: str = ""        # models.PersonaSignal 短码
    reason: str = ""        # 判定理由（人话，给家属看）
    inferred: str = ""      # "机器人学到了什么"（人话）
    positive_text: str = ""  # 正例：应该这样说
    negative_text: str = ""  # 反例：不该这样说
    weight: float = 1.0


def _clean_remainder(text: str) -> str:
    """剥掉否定词与引导词，留下用户真正想表达的内容。"""
    s = (text or "").strip()
    for _ in range(3):
        s = _NEGATION_RE.sub("", s, count=1)
        s = _LEADING_NOISE_RE.sub("", s, count=1)
        s = _PUNCT_RE.sub("", s)
    return s.strip()


def judge_feedback(user_text: str, bot_text: str = "", prev_bot_text: str = "",
                   hint: str = "") -> FeedbackJudgement:
    """判断一句话是不是**有效的学习信号**，并产出正/反例。

    ``hint`` 是调用方已经知道的信号类型（correction / retry / positive / negative），
    不传就只做自动识别（目前能自动识别"明确纠正"这一类）。
    """
    sig = models.PersonaSignal
    u = (user_text or "").strip()
    b = (bot_text or "").strip()
    prev = (prev_bot_text or "").strip()
    hint = (hint or "").strip().lower()

    if not u:
        return FeedbackJudgement(False, reason="用户这句话是空的，没有可判断的内容")

    # ---- 闸门 1：太短 ----
    if len(re.sub(r"\W|\d", "", u)) < 4:
        return FeedbackJudgement(False, reason=f"这句话太短（{u!r}），无法判断是反馈还是随口应答")

    # ---- 闸门 2：只有笑声 / 语气词 ----
    if _LAUGH_ONLY.match(u):
        return FeedbackJudgement(
            False, signal=hint or sig.POSITIVE,
            reason="只有笑声或语气词：可能是觉得好笑，也可能是觉得机器人蠢得可笑，"
                   "两种意思完全相反，不作为学习信号")

    # ---- 闸门 3：玩梗 / 逗弄 / 恶意 ----
    for kw in _TEASE_KEYWORDS:
        if kw in u:
            return FeedbackJudgement(
                False, signal=hint or sig.CORRECTION,
                reason=f"疑似玩梗或逗弄机器人（命中「{kw}」），不是真的在教它说话")

    # ---- 明确纠正：否定词 + 实质内容 ----
    is_correction = (hint == sig.CORRECTION) or bool(_NEGATION_RE.match(u))
    if is_correction:
        rest = _clean_remainder(u)
        if len(re.sub(r"\W|\d", "", rest)) < 4:
            return FeedbackJudgement(
                False, signal=sig.CORRECTION,
                reason=f"像是在否定，但没说出该怎么做（{u!r}），缺少可学习的正例")
        neg = b or ""
        return FeedbackJudgement(
            True, signal=sig.CORRECTION,
            reason="用户明确否定了刚才的说法，并给出了自己的表述",
            inferred=f"用户希望这样表达：{rest}" + (f"（原来的说法「{_brief(neg)}」记为反例）" if neg else ""),
            positive_text=rest,
            negative_text=neg,
            weight=1.0,
        )

    # ---- 重试成功：机器人换说法后用户接受了 ----
    if hint == sig.RETRY:
        if not prev:
            return FeedbackJudgement(False, signal=sig.RETRY,
                                     reason="标记为「换说法后被接受」但缺少机器人的上一句说法")
        return FeedbackJudgement(
            True, signal=sig.RETRY,
            reason="机器人换了一种说法后用户接受了，前后两句构成一对偏好",
            inferred=f"同样的话，说成「{_brief(b)}」比「{_brief(prev)}」更容易被接受",
            positive_text=b,
            negative_text=prev,
            weight=0.8,
        )

    # ---- 积极反馈 ----
    if hint == sig.POSITIVE:
        if not b:
            return FeedbackJudgement(False, signal=sig.POSITIVE,
                                     reason="标记为积极反馈但没有机器人的原话，无从学习")
        return FeedbackJudgement(
            True, signal=sig.POSITIVE,
            reason="用户给出了正向反应（接话 / 继续聊 / 带内容的笑）",
            inferred=f"「{_brief(b)}」这种说法是受欢迎的",
            positive_text=b,
            weight=0.6,
        )

    # ---- 消极反馈 ----
    if hint == sig.NEGATIVE:
        return FeedbackJudgement(
            True, signal=sig.NEGATIVE,
            reason="用户给出了负向反应（打断 / 重复提问 / 说算了）",
            inferred=(f"「{_brief(b)}」这种说法没被接受" if b else "上一轮的表达方式没被接受"),
            negative_text=b,
            weight=0.5,
        )

    return FeedbackJudgement(False, reason="没有识别出明确的反馈信号（既不是纠正，也没有正/负向标记）")


def _brief(text: str, limit: int = 40) -> str:
    """截短一段话，供审计/画像/prompt 展示用。

    这里顺手做了 prompt 注入清洗（sanitize_for_prompt）：本函数的输出会进
    ``build_persona_prompt``，也就是 system prompt，而输入是**家属/机器人/
    LLM 产生的自由文本**。原来的 ``.replace("\\n", " ")`` 只压了换行，
    挡不住 "\\n\\nSystem: ..." 这类角色标记伪造。
    详见 textutil.sanitize_for_prompt。
    """
    s = sanitize_for_prompt(text or "")
    return s if len(s) <= limit else s[:limit] + "…"


# ---------------------------------------------------------------- 写入
def _match_agent(column: Any, agent_id: int | None):
    """agent_id 的匹配条件（空值用 IS NULL，见 store._agent_match 的注释）。"""
    return column.is_(None) if agent_id is None else column == agent_id


def record_feedback(db, user_id: int, agent_id: int | None, user_text: str,
                    bot_text: str = "", prev_bot_text: str = "",
                    hint: str = "", force: bool = False) -> dict[str, Any]:
    """记录一次反馈：**先判定，再入库，无论判定结果如何都留审计**。

    ``force=True`` 用于家属手动添加（人工纠偏），跳过自动闸门 ——
    人做的判断优先于机器的判断，这正是 7.3「人工 editable」的含义。
    """
    sig = models.PersonaSignal
    if force:
        judgement = FeedbackJudgement(
            True, signal=sig.MANUAL,
            reason="家属手动添加，跳过自动有效性判定",
            inferred=f"家属指定：这样说更好 —— {_brief(user_text)}",
            positive_text=(user_text or "").strip(),
            negative_text=(bot_text or "").strip(),
            weight=1.0,
        )
    else:
        judgement = judge_feedback(user_text, bot_text, prev_bot_text, hint)

    audit = models.AgentPersonaAudit(
        user_id=user_id,
        agent_id=agent_id,
        signal=judgement.signal or (hint or ""),
        user_text=(user_text or "")[:2000],
        bot_text=(bot_text or "")[:2000],
        prev_bot_text=(prev_bot_text or "")[:2000],
        inferred=judgement.inferred,
        valid=bool(judgement.valid),
        reason=judgement.reason,
        created_at=models.utcnow(),
    )
    db.add(audit)
    db.flush()

    example_id: int | None = None
    if judgement.valid:
        # 一个纠正同时产出正例和反例：正例注入 "应该这样说"，
        # 反例注入 "要避免" —— 两条都写，反例的效果通常比正例更直接。
        if judgement.positive_text:
            ex = models.AgentPreferenceExample(
                user_id=user_id, agent_id=agent_id,
                kind=models.PreferenceKind.POSITIVE,
                source=judgement.signal,
                bot_text=(bot_text or "")[:2000],
                user_text=judgement.positive_text[:2000],
                weight=judgement.weight,
                status=models.ExampleStatus.ACTIVE,
            )
            db.add(ex)
            db.flush()
            example_id = ex.id
        if judgement.negative_text:
            db.add(models.AgentPreferenceExample(
                user_id=user_id, agent_id=agent_id,
                kind=models.PreferenceKind.NEGATIVE,
                source=judgement.signal,
                bot_text=judgement.negative_text[:2000],
                user_text=(user_text or "")[:2000],
                weight=judgement.weight,
                status=models.ExampleStatus.ACTIVE,
            ))
    audit.example_id = example_id
    return {
        "valid": judgement.valid,
        "signal": judgement.signal,
        "reason": judgement.reason,
        "inferred": judgement.inferred,
        "audit_id": audit.id,
        "example_id": example_id,
    }


# ---------------------------------------------------------------- 语气画像
_ADDR_WORDS = ("爷爷", "奶奶", "外公", "外婆", "叔叔", "阿姨", "伯伯", "老爷子", "您", "你")
_HUMOR_MARKS = ("哈哈", "笑话", "玩笑", "逗", "幽默", "乐呵", "有意思", "逗你", "开玩笑")
_MAX_CATCHPHRASE = 20


def _agent_key(agent_id: int | None) -> int:
    """agent_id 的哨兵表示：None（L1 全局）-> 0，其余 -> agent_id。

    为什么需要它：``UniqueConstraint("user_id", "agent_id")`` 在 SQLite 与 MySQL
    下都**不约束 agent_id IS NULL 的行**（SQL 里 NULL != NULL），而 L1 全局画像
    恰恰就是那一行。于是理论上能插入多行 L1 画像，而取值用
    ``db.scalar(select(...))`` —— 读哪行是不确定的，且不报错。
    加一个普通列 agent_key 并对它建唯一索引，(user, 0) 才真正受约束保护。
    详见 models.AgentPersonaProfile 与 schema_patch.dedupe_persona_globals。
    """
    return 0 if agent_id is None else int(agent_id)


def _get_profile(db, user_id: int, agent_id: int | None, create: bool = True):
    row = db.scalar(select(models.AgentPersonaProfile).where(
        models.AgentPersonaProfile.user_id == user_id,
        # 同时按 agent_key 精确匹配：它有唯一索引，走索引比 IS NULL 快，
        # 且能顺带依赖数据库挡掉重复行。
        models.AgentPersonaProfile.agent_key == _agent_key(agent_id),
    ))
    if row is None and create:
        row = models.AgentPersonaProfile(user_id=user_id, agent_id=agent_id,
                                         agent_key=_agent_key(agent_id))
        db.add(row)
        db.flush()
    return row


#: 口头禅候选里不许出现的字。中文口头禅是"短语"，不是"含虚词的片段"——
#: 统计出来的"喝水的""是十二"就是没做这一步的产物（2026-10-29 家属端
#: 语气画像里真的显示了这两个词）。
_CATCH_STOP = frozenset("的了是在有我你他她它们这那和就也都不到一个吧呢啊呀嘛个把被给对向从与及或很太最不没")


def _catchphrases(text: str) -> list[str]:
    """从一句话里挑候选口头禅：出现 ≥2 次、且**不含虚词**的 3~4 字片段。

    为什么是 3~4 字：2 字片段（"的时""我们"）噪声太大，
    5 字以上又几乎不可能在一句话里重复出现。

    为什么还要过滤虚词：n-gram 会把跨词边界的碎片也切出来 ——
    "十二点四十五" 能切出 "是十二"（如果前面有个"是"），"喝水的" 能切出
    "喝水的"。这些片段在语言学上根本不是短语，只是相邻两三个字的巧合，
    攒够次数就进了画像，家属看到的是一串不知所云的东西。
    判据很粗（含虚词就丢），但方向对：**宁可一个口头禅都不显示，也不要显示假的**。
    """
    norm = "".join(normalize(text).split())
    if len(norm) < 6:
        return []
    counter: Counter[str] = Counter()
    for size in (3, 4):
        for i in range(len(norm) - size + 1):
            counter[norm[i:i + size]] += 1
    return [p for p, c in counter.items()
            if c >= 2 and not (set(p) & _CATCH_STOP)]


def observe_reply(db, user_id: int, agent_id: int | None, bot_text: str,
                  user_text: str = "", positive: bool = True) -> None:
    """把一次「机器人说话 + 用户反应」累积进语气画像（增量，不做全量重算）。

    只统计**被认可**的回复来定"该怎么说话"：被否掉的说法会进反例池，
    但不该参与"平均句长"这类正向统计，否则画像会被自己犯过的错带偏。
    """
    prof = _get_profile(db, user_id, agent_id)
    bot = (bot_text or "").strip()
    if user_text:
        n = prof.replies_count + 1
        prof.avg_user_len = _running_avg(prof.avg_user_len, n, len(user_text))
        prof.replies_count = n

    if not bot:
        return
    if positive:
        prof.positive_count += 1
        prof.avg_reply_len = _running_avg(prof.avg_reply_len, prof.positive_count, len(bot))
        # 爱开玩笑的程度：EMA，避免一次玩笑就把画像拉满
        hit = 1.0 if any(m in bot for m in _HUMOR_MARKS) else 0.0
        prof.humor_score = round(0.8 * float(prof.humor_score or 0.0) + 0.2 * hit, 4)
        counts = dict(prof.address_counts or {})
        for word in _ADDR_WORDS:
            if word in bot:
                counts[word] = int(counts.get(word, 0)) + 1
        if counts:
            keep = dict(sorted(counts.items(), key=lambda kv: -kv[1])[:8])
            prof.address_counts = keep
            prof.address = max(keep.items(), key=lambda kv: kv[1])[0]
        phrases = dict(prof.catchphrases or {})
        for p in _catchphrases(bot):
            phrases[p] = int(phrases.get(p, 0)) + 1
        if phrases:
            prof.catchphrases = dict(sorted(phrases.items(), key=lambda kv: -kv[1])[:_MAX_CATCHPHRASE])
    else:
        prof.negative_count += 1


def _running_avg(old: float, n: int, value: float) -> float:
    """增量均值：new = old + (value - old) / n。避免存总数再除的精度与溢出问题。"""
    old = float(old or 0.0)
    n = max(1, int(n))
    return round(old + (float(value) - old) / n, 3)


def tone_profile(db, user_id: int, agent_id: int | None) -> dict[str, Any]:
    """语气画像（统计值 + 人工覆盖 + 自然语言描述）。

    返回的 dict 直接给接口用：**数值给 APP 做滑块，描述给家属读**。
    人工覆盖（manual）优先级最高 —— 自动演化永远是增益，不能压过人的判断。
    """
    prof = _get_profile(db, user_id, agent_id, create=False)
    stats = {
        "replies_count": int(prof.replies_count or 0) if prof else 0,
        "positive_count": int(prof.positive_count or 0) if prof else 0,
        "negative_count": int(prof.negative_count or 0) if prof else 0,
        "avg_reply_len": float(prof.avg_reply_len or 0.0) if prof else 0.0,
        "avg_user_len": float(prof.avg_user_len or 0.0) if prof else 0.0,
        "humor_score": float(prof.humor_score or 0.0) if prof else 0.0,
        "address": (prof.address or "") if prof else "",
        "speed": (prof.speed or "normal") if prof else "normal",
        # 口头禅要**跨多轮**重复才算数：一次回复内部重复两遍多半是"好的好的"
        # 这种口误，累计到 3 轮还在说才是习惯。低于门槛的一律不显示 ——
        # 画像里写一句假口头禅，比空着更伤信任。
        "catchphrases": [p for p, c in sorted(
            (prof.catchphrases or {}).items(), key=lambda kv: -kv[1]
        ) if int(c) >= 3][:8] if prof else [],
    }
    manual = dict(prof.manual or {}) if prof else {}

    # 自动推语速：句长是最稳的代理指标（说短句 = 要听清 = 放慢）
    auto_speed = "slow" if 0 < stats["avg_reply_len"] <= 18 else (
        "fast" if stats["avg_reply_len"] >= 45 else "normal")

    merged = {
        "avg_reply_len": float(manual.get("avg_reply_len") or stats["avg_reply_len"] or 0.0),
        "humor": bool(manual["humor"]) if "humor" in manual else stats["humor_score"] >= 0.3,
        "address": str(manual.get("address") or stats["address"] or ""),
        "speed": str(manual.get("speed") or stats["speed"] or auto_speed or "normal"),
        "note": str(manual.get("note") or ""),
    }
    return {
        "stats": stats,
        "manual": manual,
        "effective": merged,
        "description": describe_tone(stats, merged),
    }


def describe_tone(stats: dict[str, Any], effective: dict[str, Any]) -> str:
    """把画像写成一句（段）人话：既注入 prompt，也直接展示给家属。"""
    parts: list[str] = []
    avg = float(effective.get("avg_reply_len") or 0.0)
    if avg <= 0:
        parts.append("回复长度：还没有足够数据，先按简短、清楚来说")
    elif avg <= 18:
        parts.append(f"回复要短，平均每句 {avg:.0f} 字左右，一次只说一件事")
    elif avg <= 45:
        parts.append(f"回复中等长度，平均每句 {avg:.0f} 字左右")
    else:
        parts.append(f"可以稍微多说一点，平均每句 {avg:.0f} 字左右")

    addr = str(effective.get("address") or "")
    if addr:
        parts.append(f"称呼对方用「{addr}」")

    parts.append("可以适度开开玩笑" if effective.get("humor") else "少开玩笑，说正事为主")

    speed_map = {"slow": "语速放慢、句子之间多停顿", "normal": "语速正常",
                 "fast": "语速可以稍快"}
    parts.append(speed_map.get(str(effective.get("speed") or "normal"), "语速正常"))

    phrases = [p for p in (stats.get("catchphrases") or []) if len(p) >= 3][:3]
    if phrases:
        parts.append("常用口头禅：" + "、".join(phrases))

    note = str(effective.get("note") or "").strip()
    if note:
        parts.append("家属特别交代：" + note)
    return "；".join(parts) + "。"


# ---------------------------------------------------------------- 示例池与审计
def list_examples(db, user_id: int, agent_id: int | None,
                  status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    q = select(models.AgentPreferenceExample).where(
        models.AgentPreferenceExample.user_id == user_id,
        _match_agent(models.AgentPreferenceExample.agent_id, agent_id),
    )
    if status:
        q = q.where(models.AgentPreferenceExample.status == status)
    rows = db.scalars(q.order_by(
        models.AgentPreferenceExample.weight.desc(),
        models.AgentPreferenceExample.id.desc(),
    ).limit(limit)).all()
    return [_example_out(r) for r in rows]


def _example_out(r: models.AgentPreferenceExample) -> dict[str, Any]:
    return {
        "id": r.id,
        "kind": r.kind,
        "kind_text": models.PreferenceKind.TEXTS.get(r.kind, r.kind),
        "source": r.source,
        "source_text": models.PersonaSignal.TEXTS.get(r.source, r.source),
        "bot_text": r.bot_text,
        "user_text": r.user_text,
        "weight": round(float(r.weight or 0.0), 3),
        "status": r.status,
        "status_text": models.ExampleStatus.TEXTS.get(r.status, r.status),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def delete_example(db, user_id: int, agent_id: int | None, example_id: int,
                   reason: str = "家属判定为错误学习") -> bool:
    """一键作废一条学习。

    **软作废**（status=discarded）而不是删行：删行之后"机器人曾经学过什么、
    什么时候被谁删掉"就再也查不到了，那还谈什么审计。
    """
    row = db.get(models.AgentPreferenceExample, int(example_id))
    if row is None or row.user_id != user_id or row.agent_id != agent_id:
        return False
    row.status = models.ExampleStatus.DISCARDED
    row.updated_at = models.utcnow()
    # 关联的审计一起标记作废，家属看审计列表时能直接看到"这条已被撤掉"
    for audit in db.scalars(select(models.AgentPersonaAudit).where(
            models.AgentPersonaAudit.example_id == row.id)).all():
        audit.discarded_at = models.utcnow()
        audit.discard_reason = (reason or "")[:255]
    return True


def list_audits(db, user_id: int, agent_id: int | None,
                only_valid: bool | None = None, limit: int = 100) -> list[dict[str, Any]]:
    q = select(models.AgentPersonaAudit).where(
        models.AgentPersonaAudit.user_id == user_id,
        _match_agent(models.AgentPersonaAudit.agent_id, agent_id),
    )
    if only_valid is not None:
        q = q.where(models.AgentPersonaAudit.valid.is_(bool(only_valid)))
    rows = db.scalars(q.order_by(models.AgentPersonaAudit.id.desc()).limit(limit)).all()
    return [_audit_out(r) for r in rows]


def _audit_out(r: models.AgentPersonaAudit) -> dict[str, Any]:
    return {
        "id": r.id,
        "signal": r.signal,
        "signal_text": models.PersonaSignal.TEXTS.get(r.signal, r.signal or "未识别"),
        "user_text": r.user_text,
        "bot_text": r.bot_text,
        "inferred": r.inferred,
        "valid": bool(r.valid),
        "reason": r.reason,
        "example_id": r.example_id,
        "discarded_at": r.discarded_at.isoformat() if r.discarded_at else None,
        "discard_reason": r.discard_reason,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def discard_audit(db, user_id: int, agent_id: int | None, audit_id: int,
                  reason: str = "家属判定为错误学习") -> bool:
    """作废一条学习记录（连同它产出的示例）。"""
    row = db.get(models.AgentPersonaAudit, int(audit_id))
    if row is None or row.user_id != user_id or row.agent_id != agent_id:
        return False
    row.discarded_at = models.utcnow()
    row.discard_reason = (reason or "")[:255]
    if row.example_id:
        ex = db.get(models.AgentPreferenceExample, int(row.example_id))
        if ex is not None:
            ex.status = models.ExampleStatus.DISCARDED
            ex.updated_at = models.utcnow()
    return True


def update_manual(db, user_id: int, agent_id: int | None, patch: dict[str, Any]) -> dict[str, Any]:
    """家属手动调整语气画像（人工可纠偏）。

    只接受白名单里的键，避免把整个画像写坏；传 None 表示"恢复自动推断"。
    """
    prof = _get_profile(db, user_id, agent_id)
    manual = dict(prof.manual or {})
    allowed = ("avg_reply_len", "humor", "address", "speed", "note")
    for key in allowed:
        if key not in patch:
            continue
        val = patch[key]
        if val is None:
            manual.pop(key, None)
        else:
            manual[key] = val
    prof.manual = manual
    if "speed" in manual and isinstance(manual["speed"], str):
        prof.speed = manual["speed"]
    return tone_profile(db, user_id, agent_id)


# ---------------------------------------------------------------- 组装 prompt
def build_persona_prompt(db, user_id: int, agent_id: int | None,
                         positive_limit: int = 3, negative_limit: int = 3) -> str:
    """把「语气画像 + 偏好示例」拼成注入 system prompt 的片段。

    三块内容的顺序是刻意的：**先说该怎么说话（画像），再给正例，最后给反例**。
    反例放最后 —— LLM 对末尾内容的注意力更高，而"别这么说"的约束力
    通常比"要这么说"更强。

    ⚠️ 这里只产出「画像 + 示例」两段；**红线不在这里**。红线由
    ``persona.guardrails`` 从代码常量无条件注入，不经过本函数 ——
    任何"用户内容参与红线生成"的路径都必须被排除。
    """
    profile = tone_profile(db, user_id, agent_id)
    # description 里含家属手填的 note，同样是不可信输入，进prompt 前清洗
    lines: list[str] = ["【这位老人习惯的说话方式】",
                        sanitize_for_prompt(profile["description"])]

    rows = db.scalars(select(models.AgentPreferenceExample).where(
        models.AgentPreferenceExample.user_id == user_id,
        _match_agent(models.AgentPreferenceExample.agent_id, agent_id),
        models.AgentPreferenceExample.status == models.ExampleStatus.ACTIVE,
    ).order_by(models.AgentPreferenceExample.weight.desc(),
               models.AgentPreferenceExample.id.desc()).limit(64)).all()

    pos = [r for r in rows if r.kind == models.PreferenceKind.POSITIVE][:positive_limit]
    neg = [r for r in rows if r.kind == models.PreferenceKind.NEGATIVE][:negative_limit]

    if pos:
        lines.append("")
        lines.append("【他喜欢这样的表达，照着学】")
        for i, r in enumerate(pos, 1):
            lines.append(f"{i}. {_brief(r.user_text or r.bot_text, 80)}")
    if neg:
        lines.append("")
        lines.append("【下面这些说法他明确不喜欢，一定避免】")
        for i, r in enumerate(neg, 1):
            lines.append(f"{i}. {_brief(r.bot_text or r.user_text, 80)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 两层画像合并
def merge_profiles(db, user_id: int, agent_id: int | None) -> dict[str, Any]:
    """合并 L1（全局）与 L2（角色级）两层画像，供人格编译器使用。

    为什么要两层：家属可能对「小智这个角色」有特殊交代（L2），也可能只是
    笼统地说"说话轻一点"（L1）。角色模型落地后这个区分才有意义 ——
    换角色就该换一份语气，而"这个人本身的说话习惯"应当跨角色共享。

    优先级 **L2 > L1**：越具体的越贴近当下这个角色的实际用法。
    traits 声明不参与这个函数（由 persona.compiler.ProfileMerger 统一处理优先级链
    L2.manual > L1 统计 > 声明），这里只负责把两层 DB 数据取出来。
    """
    l1 = tone_profile(db, user_id, None)
    l2 = tone_profile(db, user_id, agent_id) if agent_id is not None else None
    return {"l1": l1, "l2": l2}


def copy_l2_manual(db, user_id: int, from_agent_id: int | None,
                   to_agent_id: int) -> None:
    """派生角色时把「家属对母版的手动交代」复制为新角色的 L2。

    只复制 manual，**不复制统计值**（架构 §9.1 / PRD §4）：
    统计值是"这个人跟机器人互动出来的结果"，它天然跟着人走，
    跨角色复制会让新角色一上来就带着不属于它的历史。

    复制而非共享引用：派生是 copy-on-write 的物理复制（L2 画像同理），
    否则家属之后改母版的交代会连带改掉派生角色，与 traits overlay 的语义矛盾。
    """
    src = _get_profile(db, user_id, from_agent_id, create=False)
    manual = dict(getattr(src, "manual", None) or {})
    dst = _get_profile(db, user_id, to_agent_id, create=True)
    if manual:
        dst.manual = manual
        if isinstance(manual.get("speed"), str):
            dst.speed = manual["speed"]
