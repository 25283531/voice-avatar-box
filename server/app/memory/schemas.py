"""记忆 / 人格模块的 Pydantic 出入参。

为什么不放进 app/schemas.py
---------------------------
app/schemas.py 是全项目共用的单一 schema 文件，记忆模块只是其中一个业务域。
把记忆的十几个模型塞进去会让那个文件继续膨胀，也会让「记忆模块」与「公共 schema」
之间形成不必要的耦合（记忆 router 一导入就把整个 schemas 拖进来）。
这里单独一个文件，记忆模块自成一体，将来要整体拆服务也不用动公共文件。

风格与 app/schemas.py 保持一致：ORMBase + Field 约束 + 中文文档字符串。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from ..schemas import ORMBase


# ============================================================ 出入参
class MemorySearchIn(ORMBase):
    """搜索记忆的查询体。

    刻意用 POST 而不是在 URL 上带 q=：老人说的话可能包含中文标点、空格甚至
    隐私内容，放路径里既容易超长也会进网关访问日志。
    """

    q: str = Field(min_length=1, max_length=500)
    # 记忆归属的角色；不传 = 通用记忆（agent_id 为 NULL）
    agent_id: int | None = None
    top_k: int = Field(8, ge=1, le=50)


class MemoryTurnIn(ORMBase):
    """写入记忆：把一轮对话沉淀成长期记忆。

    刻意暴露「原始对话」而不是「一条事实」：单条事实的抽取涉及分类、实体、
    重要度判断，让调用方自己拼这些字段等于把算法复制一份到 APP 端。
    """

    turns: list[dict[str, str]] = Field(min_length=1, max_length=40)
    agent_id: int | None = None
    session_id: str = Field("", max_length=64)

    @field_validator("turns")
    @classmethod
    def _check_turns(cls, v: list[dict[str, str]]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for item in v:
            content = str(item.get("content") or "").strip()
            if not content:
                # 空句直接丢掉而不是报错：一轮对话里混入空内容很正常，
                # 让调用方先自己清洗一遍纯属增加出错面。
                continue
            out.append({"role": str(item.get("role") or "user")[:16],
                        "content": content[:4000]})
        if not out:
            raise ValueError("对话内容不能为空")
        return out


class MemoryFactIn(ORMBase):
    """手动补一条记忆（家属纠偏 / 机器人漏记了重要的事）。

    与 MemoryTurnIn 并存：走 LLM 抽取一次要几百毫秒且可能抽错，
    「明确知道一件事」时家属直接补一条更可靠。
    """

    content: str = Field(min_length=2, max_length=1000)
    category: str = "other"
    agent_id: int | None = None
    # 重要度 0~1，留空按分类给默认值（用药/日程更高，见 memory/extract.py）
    salience: float | None = Field(None, ge=0.0, le=1.0)


class MemoryContextIn(ORMBase):
    """只要「拼好的一段话」而不是结构化命中列表。

    语音链路真正要的是能直接塞进 prompt 的文本；APP 调试页才需要结构化结果。
    两种需求合成一个接口只会让两边都拿到不合适的形状，所以分成两个端点。
    """

    q: str = Field(min_length=1, max_length=500)
    agent_id: int | None = None
    top_k: int = Field(6, ge=1, le=30)


class MemoryFeedbackIn(ORMBase):
    """记录一次反馈信号（会进偏好示例池 + 审计）。

    ``force=True`` 表示「家属人工判定有效」，跳过自动闸门。
    """

    user_text: str = Field(min_length=1, max_length=2000)
    bot_text: str = Field("", max_length=2000)
    prev_bot_text: str = Field("", max_length=2000)
    # correction / retry / positive / negative；留空则由规则自动识别
    signal: str = ""
    agent_id: int | None = None
    force: bool = False


class PersonaManualIn(ORMBase):
    """家属手动调整语气画像。传 None = 恢复自动推断。"""

    avg_reply_len: float | None = Field(None, ge=0.0, le=500.0)
    humor: bool | None = None
    address: str = ""
    speed: str | None = None
    note: str = Field("", max_length=500)

    @field_validator("speed")
    @classmethod
    def _check_speed(cls, v: str | None) -> str | None:
        if v in (None, "", "slow", "normal", "fast"):
            return v
        raise ValueError("speed 只能是 slow / normal / fast")


# ============================================================ 出参
class MemoryOut(ORMBase):
    """一条记忆的完整视图（含溯源与生命周期，APP「我的记忆」页直接展示）。"""

    id: int
    content: str
    category: str
    category_text: str = "其它"
    entities: list[str] = Field(default_factory=list)
    salience: float = 1.0
    source_session: str = ""
    source_quote: str = ""
    created_at: datetime | None = None
    superseded_at: datetime | None = None
    supersede_reason: str = ""
    access_count: int = 0


class MemorySignalsOut(ORMBase):
    """三路信号的得分明细。

    单独一个模型而不是三个平铺字段：与 ``retrieve.MemoryHit.as_dict()``
    的形状保持一致（那边就是嵌套的 signals），少一次形状转换，
    将来在别处复用同一个 dict 时也不会对不上。
    """

    semantic: float = 0.0
    keyword: float = 0.0
    entity: float = 0.0


class MemoryHitOut(ORMBase):
    """一条召回结果，**带每路信号的得分明细**。

    明细不是给 APP 用的装饰：家属问「机器人为什么突然提起孙女的生日」时，
    能拿这三路得分说清楚「因为名字精确命中了」，这在投诉场景里是刚需。
    """

    id: int
    content: str
    category: str
    category_text: str = "其它"
    score: float = 0.0
    signals: MemorySignalsOut = Field(default_factory=MemorySignalsOut)
    suppressed: bool = False
    # 来源可信度系数（>1 家属确认过，<1 模型自动抽取）。
    # 家属端要靠它区分「机器猜的」和「人确认的」，所以必须出接口。
    trust: float = 1.0
    created_ts: float = 0.0


class MemoryWriteOut(ORMBase):
    """写入结果。

    ``ids`` 为空是**正常情况**：一轮对话里没有值得记的事实（纯寒暄）、
    或抽出来的事实与已有记忆完全重复（ADD-only 的幂等去重）。
    所以额外给一句人话原因，APP 才不会显示成「写入失败」。
    """

    ok: bool = True
    ids: list[int] = Field(default_factory=list)
    count: int = 0
    reason: str = ""


class MemorySearchOut(ORMBase):
    query: str = ""
    hits: list[MemoryHitOut] = Field(default_factory=list)
    context: str = ""
    total: int = 0


class ToneProfileOut(ORMBase):
    """语气画像：数值给 APP 做滑块，描述给家属读。"""

    stats: dict[str, Any] = Field(default_factory=dict)
    manual: dict[str, Any] = Field(default_factory=dict)
    effective: dict[str, Any] = Field(default_factory=dict)
    description: str = ""


class FeedbackOut(ORMBase):
    """反馈判定结果。

    刻意把「判定理由」原样返回给调用方：被驳回时家属需要知道**为什么**
    （"这句话太短" vs "疑似玩梗"），否则只能干瞪眼地重复提交。
    """

    ok: bool = True
    valid: bool = False
    signal: str = ""
    inferred: str = ""
    reason: str = ""
    audit_id: int = 0
    example_id: int | None = None


class SimpleOut(ORMBase):
    ok: bool = True
    detail: str = ""


class ChatTurnOut(ORMBase):
    role: str
    content: str
    created_at: str | None = None
    session_id: str = ""
