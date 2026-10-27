"""语音链路的**服务端工具调用**（LLM function calling → 服务端执行）。

架构路线：为什么是服务端工具，而不是设备 MCP
--------------------------------------------
固件侧的 "MCP" 是**设备本地工具**通道：``firmware/xiaozhi/main/mcp_server.cc``
的 ``ParseMessage`` 校验 JSON-RPC 2.0，``application.cc`` 把服务端发来的
``{"type":"mcp","payload":...}`` 交给它。小智的设计是"设备当 MCP server、
LLM 侧当 client"，暴露的是**设备能力**（音量、电量、摄像头）。

但我们的提醒数据在**服务端**（``reminders`` 表），设备本地根本没有这个数据。
所以：

* ❌ 不去实现"通过 MCP JSON-RPC 通道代理到设备"——设备上没有提醒数据，
  这条路走不通，而且会把简单问题复杂化；
* ✅ 正确路线：在服务端实现工具调用 —— LLM function calling → 服务端直接
  执行 → 写 ``reminders`` 表 → 把结果喂回 LLM → 生成口语回复。
  **设备完全不需要参与，固件零改动。**

用户原话说的是 "mcp 工具调用"，但真实意图是"尤其是提醒功能"——上面这条
路线用 LLM 标准的 function calling 满足了这个意图，且不依赖设备侧任何新能力。

例外：**设备自己的能力**必须代理到设备（2026-10-25）
----------------------------------------------------
提醒数据在服务端，但音量 / 屏幕亮度 / 电量这些状态在**设备**上，服务端没有
任何办法替它改。所以这一类工具走 ``device_call``（服务端 → 设备 ``tools/call``，
实现见 ``session.VoiceSession.call_device_tool``）：

* ``set_volume``        → 固件 ``self.audio_speaker.set_volume``
* ``set_brightness``    → 固件 ``self.screen.set_brightness``
* ``get_device_status`` → 固件 ``self.get_device_status``

这三个工具固件**早就注册好了**（``main/mcp_server.cc`` 的 ``AddCommonTools``），
缺的只是服务端这一侧的 ``TOOL_DEFS``。上线前的真实现象就是老人说「声音大一点」
模型只能道歉 —— LLM 根本不知道设备有这两个能力。

时间解析为什么自己写一层而不是只用 schedule.parse_hhmm
-----------------------------------------------------
``schedule.parse_hhmm`` 只认 "08:30" 这种机器格式。老人说的是
「早上八点」「晚上八点半」「礼拜一」「每天」「早饭后」——必须有一层
**口语 → 结构化**的翻译。但它产出的是 ``ScheduleKind`` / ``times_of_day``
这些既有结构（直接喂给 ``models.Reminder``），复用而不是另造一套排期逻辑；
展开/物化仍然全部走 ``schedule.py``（``next_occurrence`` /
``materialize_events``），这里一行排期代码都不重复写。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .. import models
from ..schedule import ScheduleKind, local_now, materialize_events, next_occurrence

log = logging.getLogger(__name__)

# ================================================================ 工具定义
# 形状用 OpenAI 的 [{type:function, function:{name, description, parameters}}]，
# 由 llm_gateway._chat_payload 翻译成 Anthropic / Gemini 的形状 —— 工具定义
# 只写一份，别在四个协议分支里各写一遍。
TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_reminder",
            "description": (
                "给老人创建一条提醒（吃药、喝水、测血压、活动等）。"
                "老人说「提醒我……」时调用本工具，不要只口头答应。"
                "时间按老人原话填到 when，由服务端解析。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "提醒内容，十几个字以内，如「吃药」「喝水」「测血压」",
                    },
                    "when": {
                        "type": "string",
                        "description": (
                            "时间，老人原话或 ISO 时间，如「明天早上八点」「晚上八点半」"
                            "「每周三早上八点」「每天」「早饭后」。"
                            "**原话照抄，不要自己换算成几点几分** —— 服务端认识"
                            "「两分钟后」「半小时后」「一个小时后」「一刻钟后」"
                            "这类相对时间。听不出具体时间时才填空。"
                        ),
                    },
                    "repeat": {
                        "type": "string",
                        "description": (
                            "重复规则，不填表示一次性。可选：每天、每周、每月、隔天。"
                            "老人原话里已带重复词时留空即可。"
                        ),
                    },
                },
                "required": ["title", "when"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_reminders",
            "description": "列出老人当前的所有提醒。老人问「我有哪些提醒」时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_reminder",
            "description": (
                "删除一条提醒。可以用 reminder_id 精确删，也可以用 title "
                "按内容找（老人说「把吃药的提醒删了」）。找到多条相似时不要猜，"
                "返回候选让老人自己选。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {"type": "integer", "description": "提醒 id（可选）"},
                    "title": {"type": "string", "description": "提醒内容关键词（可选）"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_reminder",
            "description": (
                "暂停或恢复一条提醒（老人住院、外出时常用）。"
                "同样支持 reminder_id 或 title 定位，多条相似时返回候选。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {"type": "integer", "description": "提醒 id（可选）"},
                    "title": {"type": "string", "description": "提醒内容关键词（可选）"},
                    "enabled": {
                        "type": "boolean",
                        "description": "true=恢复，false=暂停；不填则取反",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_reminder",
            "description": (
                "确认老人刚刚完成了当前提醒事项（最常见的是已吃服药提醒）。"
                "老人听到提醒后说「我吃过了」「吃完了」「已经吃了」「药吃了」时"
                "**必须**调用本工具，不能只口头答应 —— 不调用的话设备侧不会"
                "记下确认，15 分钟后会把这条提醒判成「漏服」并通知家属。"
                "无参数。返回 false 表示当前没有待确认的提醒，此时用一句话"
                "自然回应即可，不要说「失败了」。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            # ⚠️ 新增工具**一律追加到末尾**：TOOL_DEFS 会拼进每次请求的 tools
            # 字段，插在中间会让后面所有工具定义整体位移，服务端的 prompt
            # 前缀缓存全部失效（每次请求都全量计费）。这条与 D47 里
            # 「MCP 工具放工具表末尾」是同一条纪律。
            "description": (
                "查询真实天气。老人问「今天天气怎么样」「明天冷不冷」"
                "「用不用带伞」「外面下雨吗」时**必须**调用，不能凭印象回答 —— "
                "天气每天都在变，编一个温度会让老人穿错衣服出门。\n"
                "选 kind：只说现在/今天 → now；提到明天/后天/这几天 → daily。\n"
                "城市：老人没提就别问，留空即可（用这台设备的常驻城市）；"
                "只有他明确说了别的城市才填 city。\n"
                "返回里的 message 是可以直接念给老人听的一句话，不要改写数字。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名，如「平顶山」。老人没提城市就留空",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["now", "daily"],
                        "description": "now=实时天气；daily=今明后三天预报",
                    },
                    "days": {
                        "type": "integer",
                        "description": "kind=daily 时的天数，1-3，默认 3",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "enter_standby",
            # 追加在末尾（同上一条纪律：插在中间会让 prompt 前缀缓存全失效）。
            "description": (
                "让设备进入待机：停止麦克风监听，只等下一次唤醒词。\n"
                "**只有在老人明确表示要结束这次对话时**才调用 —— 例如他让你"
                "退下、待机、别说了、安静会儿、去休息、他要睡了、不聊了。\n"
                "**不要**在他只是随口提到这些字眼时调用：他可能在说别人"
                "（「他走了」「孩子睡了」）、在说自己的安排（「我明天要出去」）、"
                "在跟屋里的其他人说话，或者只是回答你的问题（「不用了」"
                "「没事了」）。这些情况照常回答他，**不要**调用本工具。\n"
                "调用后本轮只说一句很短的道别（10 个字以内），不要再提问、"
                "不要再展开任何内容。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_volume",
            # 追加在末尾（同上一条纪律：插在中间会让 prompt 前缀缓存全失效）。
            "description": (
                "调节设备扬声器的音量（0-100）。老人说「声音大一点」「太小了"
                "听不清」「小点声」「太吵了」「声音调到 50」「最大声」「静音」"
                "时**必须**调用，不能只口头答应。\n"
                "两种用法二选一：\n"
                "  · 老人给了具体数字 → 填 level（0-100）；\n"
                "  · 老人只说了大小 → 填 delta（一档 20，如 20 表示调大、-20 表示调小）。\n"
                "「最大 / 最响」填 level=100；「静音 / 别出声 / 关掉声音」填 level=0。\n"
                "**一次只调一档**（20），调完用一句话问老人现在合不合适，"
                "不要连着调好几次 —— 老人听不清时会一直说「再大点」，"
                "你一次调到底反而会吓到他。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {
                        "type": "integer",
                        "description": "目标音量，0-100（老人说了具体数值时填）",
                    },
                    "delta": {
                        "type": "integer",
                        "description": "相对变化量，20=调大一档，-20=调小一档",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_brightness",
            # 追加在末尾（同上）。
            "description": (
                "调节设备屏幕亮度（0-100）。老人说「屏幕太亮了」「刺眼」「调暗点」"
                "「看不清屏幕」「亮一点」「把屏幕关掉」时**必须**调用。\n"
                "两种用法二选一：\n"
                "  · 老人给了具体数字 → 填 level（0-100）；\n"
                "  · 老人只说了亮/暗 → 填 delta（一档 20，如 20 表示调亮、-20 表示调暗）。\n"
                "「太刺眼 / 关掉屏幕」填 level=0；「最亮」填 level=100。\n"
                "**只调屏幕背光，不要动音量** —— 老人说屏幕刺眼时把声音调小是错的。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {
                        "type": "integer",
                        "description": "目标亮度，0-100（老人说了具体数值时填）",
                    },
                    "delta": {
                        "type": "integer",
                        "description": "相对变化量，20=调亮一档，-20=调暗一档",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_device_status",
            # 追加在末尾（同上）。
            "description": (
                "查询设备当前的真实状态：音量、屏幕亮度、电池电量与是否在充电。\n"
                "老人问「现在声音多大」「还有多少电」「充上电了吗」「屏幕怎么这么暗」"
                "时调用，**不要凭印象编数字**。\n"
                "返回的是设备上报的字段（volume / brightness / battery_level / "
                "charging），要把它翻译成一句老人听得懂的话（如「电量还有八成，"
                "正插着电」），**不要念字段名，不要说英文**。\n"
                "没屏幕或没电池的设备可能缺字段，缺了就说「这台看不到」，不要猜。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

#: 工具执行出错时喂回 LLM 的 JSON 里，need_clarify=True 表示"必须让老人澄清"。
#: LLM 看到它就会用一句话反问，而不是瞎猜一个时间建一条错提醒 —— 对老人
#: 来说，一条时间错的服药提醒是最糟的结果（比没有提醒更糟）。

# ================================================================ 口语时间解析
_CN_DIGITS: dict[str, int] = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "俩": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

# 时段词 -> (小时修正, 是否强制上午)。None 表示不影响小时。
_PERIOD_AM = ("早上", "早晨", "清晨", "上午", "早间")
_PERIOD_NOON = ("中午", "正午", "白日")
_PERIOD_PM = ("下午", "午后")
_PERIOD_EVENING = ("傍晚", "黄昏", "晚上", "夜里", "晚间", "晚晌")
_PERIOD_NIGHT = ("半夜", "凌晨", "后半夜")

# 饭后时刻的约定值（分钟偏移自 00:00）。老人说"早饭后吃药"不会说几点，
# 用三餐的常规时刻兜底；确认回复里会把具体时间念出来，老人不满意可以改。
_MEAL_AFTER_MIN: dict[str, int] = {
    "早": 7 * 60 + 30, "午": 12 * 60, "晚": 18 * 60 + 30, "午饭": 12 * 60,
    "晚饭": 18 * 60 + 30, "早饭": 7 * 60 + 30,
}

_WEEKDAY_WORDS: dict[str, int] = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "天": 7, "日": 7,
    "末": 7,
}


@dataclass
class ParsedWhen:
    """口语时间的解析结果。ok=False 时 reason 是给 LLM 看的澄清提示。"""

    ok: bool = False
    reason: str = ""
    schedule_kind: int = int(ScheduleKind.ONCE)
    times_of_day: list[str] = field(default_factory=list)
    weekdays: list[int] = field(default_factory=list)
    month_days: list[int] = field(default_factory=list)
    interval_days: int = 1
    once_date: str | None = None
    #: 口语化描述（"明天早上八点"），进工具结果、供 LLM 复述确认
    speak: str = ""


def _normalize(text: str) -> str:
    """全角转半角、去空白与标点，只留解析需要的字符。"""
    out = []
    for ch in text or "":
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
            continue
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
            continue
        out.append(ch)
    s = "".join(out)
    return re.sub(r"[\s，。、；！？,.;:：!?\"'「」『』（）()【】\[\]]+", "", s)


def _cn_number(text: str) -> int | None:
    """解析中文/阿拉伯数字（0-99，含"十""二十三""三十"）。"""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    # "二十三" / "二十" / "十五" 形态
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        if left and left not in _CN_DIGITS:
            return None
        if right and right not in _CN_DIGITS:
            return None
        return tens * 10 + ones
    return None


def _fmt_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _day_word(d: date, now: date) -> str:
    delta = (d - now).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    if delta == 2:
        return "后天"
    if delta == 3:
        return "大后天"
    if delta == -1:
        return "昨天"
    return f"{d.month}月{d.day}号"


def _period_phrase(minutes: int) -> str:
    """按小时选时段词，供口语化描述用。"""
    h = minutes // 60
    if h < 5:
        return "半夜"
    if h < 12:
        return "早上" if h < 9 else "上午"
    if h < 14:
        return "中午"
    if h < 18:
        return "下午"
    return "晚上"


def _clock_phrase(minutes: int) -> str:
    """把分钟数说成老人习惯的说法：八点 / 八点半 / 八点一刻。"""
    h, m = divmod(minutes, 60)
    hh = h if h <= 12 else h - 12
    if m == 0:
        return f"{hh}点"
    if m == 30:
        return f"{hh}点半"
    if m == 15:
        return f"{hh}点一刻"
    if m == 45:
        return f"{hh}点三刻"
    return f"{hh}点{m:02d}分"


def parse_when(text: str, now: datetime | None = None) -> ParsedWhen:
    """解析老人口语时间。**歧义/解析失败一律 ok=False + reason** —— 让 LLM
    拿去反问老人，绝不静默建一条时间错的提醒。
    """
    now = now or local_now()
    s = _normalize(text)
    if not s:
        return ParsedWhen(ok=False, reason="没听出时间，请老人再说一遍具体时间")

    # ---- 1. 重复规则 ----
    kind = ScheduleKind.ONCE
    weekdays: list[int] = []
    month_days: list[int] = []
    interval_days = 1
    repeat_word = ""
    if re.search(r"每天|每日|天天", s):
        kind, repeat_word = ScheduleKind.DAILY, "每天"
    elif re.search(r"每周|每星期|星期[一二三四五六天末]|礼拜[一二三四五六天末]|周[一二三四五六天末]", s):
        kind, repeat_word = ScheduleKind.WEEKLY, "每周"
        for m in re.finditer(r"(?:星期|礼拜|周)([一二三四五六天末])", s):
            weekdays.append(_WEEKDAY_WORDS[m.group(1)])
        weekdays = sorted(set(weekdays))
        if not weekdays:
            return ParsedWhen(ok=False,
                              reason="老人说了每周重复但没说星期几，请反问「每周星期几」")
    elif re.search(r"每月|每个月|月月", s):
        kind, repeat_word = ScheduleKind.MONTHLY, "每月"
        m = re.search(r"每月?\s*([0-9]+|[一二三]?十?[一二三四五六七八九]?)\s*[号日]", s)
        if m:
            day = _cn_number(m.group(1))
            if day and 1 <= day <= 31:
                month_days = [day]
        else:
            return ParsedWhen(ok=False,
                              reason="老人说了每月重复但没说几号，请反问「每月几号」")
    elif re.search(r"隔天|每隔一天", s):
        kind, repeat_word = ScheduleKind.INTERVAL_DAYS, "隔天"
        interval_days = 2
    elif (m := re.search(r"隔(?:天|日)\s*([0-9]+|[一二三四五六七八九])\s*天", s)):
        kind, repeat_word = ScheduleKind.INTERVAL_DAYS, "隔天"
        interval_days = (_cn_number(m.group(1)) or 1) + 1

    # ---- 2. 一天之内的时刻 ----
    minutes: int | None = None

    # ---- 1.5 相对时间：「两分钟后」「半小时后」「一个小时后」「一刻钟后」----
    # 为什么必须有这一段（2026-10-21 生产事故）
    # ----------------------------------------
    # 老人不说"几点几分"，说的是"两分钟后提醒我吃药"。原先这个句式落到
    # 下面"没听出具体时间"→ 工具 ok=false → 模型却在第二轮回"记好了"，
    # 生产库 reminders 表里**一条都没建成**（interactions id=18/19 的
    # tool_calls 都是 create_reminder ok=false）。老人以为建好了，到点
    # 什么都没发生 —— 这是本项目最贵的一类故障（假成功）。
    #
    # 口径：
    #   * 「N分钟/N小时/半小时/一刻钟 + 后|以后」→ 按 now 折算成今天的时刻；
    #   * 跨过午夜由下面第 3 节的 `due > now` 判断自动滚到明天，不用特殊处理；
    #   * 超过 24 小时的"相对时间"（"三天后"）交给第 3 节的日期分支，
    #     这里只管一天以内 —— 混在一起会出现"三天后算成今天 15:00"这种错；
    #   * 歧义句式（待会/一会儿/马上/现在）**仍然反问，不猜**。
    rel = re.search(
        r"(?:(\d+|[零一二两俩三四五六七八九十]+)\s*个?\s*(分钟|小时|钟头)"
        r"|(半)\s*个?\s*小时"
        r"|(一刻钟))(?:之?后|以后)", s)
    if rel and minutes is None:
        unit = rel.group(2)
        qty = _cn_number(rel.group(1) or "") if rel.group(1) else None
        if unit == "分钟":
            delta_min = qty if qty else 1
        elif unit in ("小时", "钟头"):
            delta_min = (qty if qty else 1) * 60
        elif rel.group(3):          # "半小时后"
            delta_min = 30
        else:                       # "一刻钟后"
            delta_min = 15
        if delta_min > 24 * 60:
            return ParsedWhen(
                ok=False,
                reason=(f"老人说的「{text}」超过一天，请按「明天早上八点」"
                        f"那样说一个具体时间"))
        minutes = (now.hour * 60 + now.minute + delta_min) % (24 * 60)

    # 2a. 饭后约定（显式带早/午/晚的才认；光说"饭后"要反问）
    meal = re.search(r"(早|午|晚)饭(?:后|以后)", s)
    if meal:
        minutes = _MEAL_AFTER_MIN[meal.group(1)]
    elif "饭后" in s or "饭前" in s:
        return ParsedWhen(ok=False,
                          reason="老人只说了「饭后/饭前」没说早午晚，请反问"
                                 "「是早饭后、午饭后还是晚饭后」")

    # 2b. 钟点：「八点」「8点」「八点半」「八点三十分」「八点一刻」「三点十五」
    hour: int | None = None
    minute = 0
    clock = re.search(r"([0-9]+|[零一二两俩三四五六七八九十]+)\s*点\s*"
                      r"(半|一刻|三刻|[0-9]+|[零一二两俩三四五六七八九十]+)?\s*分?",
                      s)
    if clock:
        hour = _cn_number(clock.group(1))
        tail = clock.group(2)
        if tail:
            if tail == "半":
                minute = 30
            elif tail == "一刻":
                minute = 15
            elif tail == "三刻":
                minute = 45
            else:
                minute = _cn_number(tail) or 0
        if hour is None or not (0 <= hour <= 24) or not (0 <= minute <= 59):
            return ParsedWhen(ok=False, reason=f"没听懂时间「{text}」，请老人再说一遍")
        if hour == 24:
            hour = 0
    elif minutes is None:
        # 没有"点"也没有饭后词：看看是不是裸数字 + 时段词（"早上8"）
        bare = re.search(r"(早上|上午|中午|下午|傍晚|晚上|夜里|凌晨|半夜)?"
                         r"\s*([0-9]+|[零一二两俩三四五六七八九十]+)\s*$", s)
        if bare and bare.group(1) and bare.group(2):
            hour = _cn_number(bare.group(2))
            if hour is None or hour > 23:
                return ParsedWhen(ok=False, reason=f"没听懂时间「{text}」，请老人再说一遍")
            minute = 0
        elif re.search(r"现在|马上|待会|一会儿|过一会", s):
            return ParsedWhen(ok=False,
                              reason="老人说的是相对时间（现在/待会），请反问具体几点")

    if minutes is None:
        if hour is None:
            return ParsedWhen(ok=False,
                              reason=f"没听出具体时间（原话「{text}」），"
                                     f"请老人像「明天早上八点」那样再说一遍")
        # 时段词定小时（含歧义判定）
        if any(w in s for w in _PERIOD_NIGHT):
            hour = hour % 24 if hour <= 12 else hour
            if hour == 12:
                hour = 0
        elif any(w in s for w in _PERIOD_AM):
            hour = hour % 12 if hour != 12 else 12
            if hour == 12:
                hour = 0
        elif "中午" in s or "正午" in s:
            hour = min(12, hour)
        elif any(w in s for w in _PERIOD_PM + _PERIOD_EVENING):
            hour = hour + 12 if hour < 12 else hour
        else:
            # 没有时段词：6-11 视为上午，12 点整视为中午，13+ 原样，
            # 1-5 点**歧义**（下午三点 vs 凌晨三点）—— 必须反问。
            if 6 <= hour <= 11:
                pass
            elif hour == 12:
                pass
            elif hour >= 13:
                pass
            else:
                return ParsedWhen(
                    ok=False,
                    reason=(f"老人说的时间「{text}」不明确是下午{hour}点还是"
                            f"凌晨{hour}点，请用一句话反问确认"))
        minutes = hour * 60 + minute

    # ---- 3. 日期 / 星期 ----
    once_date: str | None = None
    if kind == ScheduleKind.ONCE:
        today = now.date()
        target: date | None = None
        # ⚠️ 顺序：「大后天」必须排在「后天」之前 —— 先匹配"后天"会把
        # "大后天"截成 +2 天，老人白等多一天才发现提醒没响。
        if re.search(r"今天|今日|今儿", s):
            target = today
        elif re.search(r"大后天", s):
            target = today + timedelta(days=3)
        elif re.search(r"后天", s):
            target = today + timedelta(days=2)
        elif re.search(r"明天|明日|明儿", s):
            target = today + timedelta(days=1)
        elif (m := re.search(r"(\d+|[零一二两俩三四五六七八九十]+)\s*天(?:之?后|以后)", s)):
            # 「三天后早上八点」。原先这里落到"没给日期"分支，静默排成
            # **明天** —— 老人白等两天才发现提醒没响。同一个"日期没说清
            # 就猜"的家族，一起修掉。
            n = _cn_number(m.group(1)) or 1
            target = today + timedelta(days=max(1, n))
        elif re.search(r"昨天|昨日", s):
            return ParsedWhen(ok=False, reason="老人说的是过去的时间，无法创建提醒，"
                                               "请确认是不是明天的同一个时间")
        else:
            wd = re.search(r"(?:星期|礼拜|周)([一二三四五六天末])", s)
            if wd:
                want = _WEEKDAY_WORDS[wd.group(1)]
                delta = (want - today.isoweekday()) % 7
                # 同一个星期几且今天还没到点 -> 今天，否则下周
                if delta == 0 and minutes is not None:
                    due = datetime.combine(today, datetime.min.time()).replace(
                        hour=minutes // 60, minute=minutes % 60, tzinfo=now.tzinfo)
                    if due <= now:
                        delta = 7
                target = today + timedelta(days=delta)
            else:
                # 没给日期：今天到了就往明天排（服药提醒说"八点"时常是明早）
                due = datetime.combine(today, datetime.min.time()).replace(
                    hour=minutes // 60, minute=minutes % 60, tzinfo=now.tzinfo)
                target = today if due > now else today + timedelta(days=1)
        once_date = target.isoformat()

    # ---- 4. 口语化描述 ----
    if kind == ScheduleKind.ONCE and once_date:
        speak = f"{_day_word(date.fromisoformat(once_date), now.date())}" \
                f"{_period_phrase(minutes)}{_clock_phrase(minutes)}"
    else:
        speak = f"{repeat_word or '每天'}{_period_phrase(minutes)}{_clock_phrase(minutes)}"
        if weekdays:
            speak = "每周" + "、".join(f"周{'一二三四五六日'[w - 1]}" for w in weekdays) \
                + _period_phrase(minutes) + _clock_phrase(minutes)
        elif month_days:
            speak = f"每月{month_days[0]}号{_period_phrase(minutes)}{_clock_phrase(minutes)}"

    return ParsedWhen(ok=True, schedule_kind=int(kind), times_of_day=[_fmt_time(minutes)],
                      weekdays=weekdays, month_days=month_days,
                      interval_days=interval_days, once_date=once_date, speak=speak)


# ================================================================ 工具执行
def _guess_rtype(title: str) -> int:
    if re.search(r"药|服药|吃药|打针|胰岛素", title):
        return int(models.ReminderType.MEDICINE)
    if re.search(r"喝水|饮水|喝药茶", title):
        return int(models.ReminderType.WATER)
    if re.search(r"运动|锻炼|散步|走路|太极|广场舞", title):
        return int(models.ReminderType.EXERCISE)
    return int(models.ReminderType.CUSTOM)


def _find_reminders(db: Any, device_pk: int, keyword: str) -> list[models.Reminder]:
    rows = list(db.query(models.Reminder)
                .filter(models.Reminder.device_id == device_pk)
                .order_by(models.Reminder.id.desc()).all())
    if not keyword:
        return rows
    kw = keyword.strip()
    exact = [r for r in rows if (r.title or "").strip() == kw]
    if exact:
        return exact
    return [r for r in rows if kw in (r.title or "")]


def _describe_reminder(r: models.Reminder) -> str:
    """给 LLM 看的一行描述（含下次触发时间，让它能念给老人）。"""
    nxt = next_occurrence(r)
    parts = [f"id={r.id}", f"「{r.title}」"]
    if not r.enabled:
        parts.append("已暂停")
    if nxt is not None:
        parts.append(f"下次 {_day_word(nxt.date(), local_now().date())}"
                     f"{_period_phrase(nxt.hour * 60 + nxt.minute)}"
                     f"{_clock_phrase(nxt.hour * 60 + nxt.minute)}")
    else:
        parts.append("没有未来的触发时间")
    return "，".join(parts)


def execute_tool(db: Any, device_pk: int, name: str,
                 arguments: str | dict[str, Any],
                 device_call: Any = None) -> dict[str, Any]:
    """执行一个工具，返回**可 JSON 序列化**的结果（喂回 LLM 的 tool 消息）。

    任何异常都不抛给调用方：工具失败的后果是"这次没办成 + 一句老人听得懂
    的话"，不是会话崩溃。

    ``device_call(name, arguments, timeout)`` 是可选的「服务端 → 设备」同步
    回调，由 session 层注入（它内部用 run_coroutine_threadsafe 桥回事件
    循环，因为本函数跑在线程池里）。只有需要设备配合的工具才用得上：
    ``confirm_reminder`` / ``set_volume`` / ``set_brightness`` /
    ``get_device_status``。传 None 时该类工具返回"通道不可用"，
    而不是抛异常 —— 这样单测可以不带设备直接跑。
    """
    try:
        args = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
        if not isinstance(args, dict):
            args = {}
    except Exception:  # noqa: BLE001  模型给了坏 JSON：当空参数处理
        log.warning("工具 %s 的参数不是合法 JSON：%r", name, str(arguments)[:200])
        args = {}

    try:
        if name == "create_reminder":
            return _create_reminder(db, device_pk, args)
        if name == "list_reminders":
            return _list_reminders(db, device_pk)
        if name == "delete_reminder":
            return _delete_reminder(db, device_pk, args)
        if name == "toggle_reminder":
            return _toggle_reminder(db, device_pk, args)
        if name == "confirm_reminder":
            return _confirm_reminder(db, device_pk, device_call)
        if name == "get_weather":
            return _get_weather(db, device_pk, args)
        if name == "set_volume":
            return _set_device_level("volume", args, device_call)
        if name == "set_brightness":
            return _set_device_level("brightness", args, device_call)
        if name == "get_device_status":
            return _get_device_status(device_call)
        return {"ok": False, "message": f"没有叫「{name}」的工具"}
    except Exception as exc:  # noqa: BLE001  兜底：工具失败不崩会话
        log.warning("工具 %s 执行失败：%s: %s", name, type(exc).__name__, exc)
        return {"ok": False, "message": "操作没成功，请老人稍后再试"}


def _confirm_reminder(db: Any, device_pk: int, device_call: Any) -> dict[str, Any]:
    """让设备确认「老人刚完成了当前提醒」。

    状态在**设备**上（提醒引擎跑在 NVS 里，断网也要能响），所以这里不能
    只改数据库 —— 必须经 MCP 通道下发 tools/call，由固件
    ``elder_confirm_pending_medicine()`` 改状态并回执，服务端再收到
    ``state=CONFIRMED`` 才落库。只改 DB 的写法会造成「服务端显示已确认、
    设备还在响铃」，是最坏的假象。

    失败语义要分清（LLM 拿到的话术不同）：
      - 设备明确答 false → 没有待确认的提醒，正常回应，**不要说失败**；
      - 超时 / 通道不可用 → 让老人改按设备上的按键。
    """
    if device_call is None:
        log.warning("confirm_reminder：没有设备通道（device_call=None）")
        return {"ok": False, "message": "现在连不上设备，请让老人按一下设备上的按键确认"}

    try:
        payload = device_call("self.elder.confirm_reminder", {}, 3.0)
    except TimeoutError:
        log.warning("confirm_reminder：设备 3 秒内没回包")
        return {"ok": False, "message": "设备没有响应，请让老人按一下设备上的按键确认"}
    except Exception as exc:  # noqa: BLE001  设备离线/WS 已断
        log.warning("confirm_reminder：下发失败 %s: %s", type(exc).__name__, exc)
        return {"ok": False, "message": "现在连不上设备，请让老人按一下设备上的按键确认"}

    if not isinstance(payload, dict):
        return {"ok": False, "message": "设备返回的内容看不懂，请让老人按一下设备上的按键确认"}

    err = payload.get("error")
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        log.warning("confirm_reminder：设备报错 %s", msg)
        return {"ok": False, "message": "设备说没办成，请让老人按一下设备上的按键确认"}

    result = payload.get("result")
    # 固件 tools/call 的成功返回形如
    # {"content":[{"type":"text","text":"true"}],"isError":false}；
    # 这里做宽松判定：只要不是明确的 false / isError，就算确认成功。
    if isinstance(result, dict) and result.get("isError"):
        return {"ok": False, "message": "设备说没办成，请让老人按一下设备上的按键确认"}

    confirmed = True
    if isinstance(result, dict):
        for item in (result.get("content") or []):
            if isinstance(item, dict) and str(item.get("text", "")).strip().lower() == "false":
                confirmed = False
                break
    elif result is False:
        confirmed = False

    if not confirmed:
        log.info("confirm_reminder：设备答 false（当前没有待确认提醒）")
        return {"ok": True, "confirmed": False,
                "message": "现在没有等着确认的提醒，用一句话自然回应老人即可，不要说失败"}

    log.info("confirm_reminder：设备已确认（device_pk=%s）", device_pk)
    # 语音确认只算**半置信**（2026-10-27 用户定义）：设备端进的是
    # HALF_CONFIRMED，闪灯不停、屏幕不退、#1 继续橙色呼吸 —— 必须再按一次
    # 按钮才算完成。所以这里不能只说"记下了"，要把按键动作**逐字**教给老人：
    # 说"按一下"他会在别的地方乱摸，说"长按 3 秒 + 闪蓝三次再松手"他才做得对。
    return {"ok": True, "confirmed": True,
            "message": (
                "已记下（语音确认只算半置信，还没算完成）。"
                "请用一句话让老人照着做，只说这一遍、不要解释原因、不要多说别的："
                "「为了防止识别错误，请长按亮着橙色指示灯的按钮 3 秒钟，"
                "等按钮闪蓝色三次再松手。」"
            )}


# ================================================================ 设备能力
# 音量 / 亮度 / 设备状态：状态在**设备**上，服务端只能经 device_call 代理。
#
# 超时为什么是 2.0 而不是 confirm_reminder 的 3.0
# ----------------------------------------------
# confirm_reminder 要等固件改状态 + 落 NVS，给宽一点有道理。而这两个 set
# 工具的回调在固件里只是一句 ``codec->SetOutputVolume()`` /
# ``backlight->SetBrightness()``，毫秒级；真机上局域网一个来回通常 50~150ms。
# 等超过 2 秒就是链路真出问题了，早点失败让 LLM 换话术，别让老人对着
# 一个"嗯……"干等 3 秒 —— 老人的耐心比链路预算更贵。
_DEVICE_OP_TIMEOUT = 2.0

#: kind -> 固件工具名 / 参数名 / 状态 JSON 里的取值路径。
#: 工具名**必须与 ``main/mcp_server.cc`` 的 AddCommonTools 逐字一致**，
#: 拼错的现象是「设备回 Unknown tool」，而日志里看起来像设备不支持。
_LEVEL_SPECS: dict[str, dict[str, Any]] = {
    "volume": {
        "label": "音量",
        "tool": "self.audio_speaker.set_volume",
        "arg": "volume",
        "path": ("audio_speaker", "volume"),
    },
    "brightness": {
        "label": "屏幕亮度",
        "tool": "self.screen.set_brightness",
        "arg": "brightness",
        "path": ("screen", "brightness"),
    },
}


def _coerce_int(value: Any) -> int | None:
    """把模型给的东西转成 int。

    LLM 经常不守规矩：``"50"`` / ``"50%"`` / ``50.0`` / ``"+20"`` 都出现过。
    ``bool`` 要单独挡掉（Python 里 True 是 int 的子类，会被转成 1 —— 那样
    老人说「调大点」结果音量变成 1，是最刺耳的那种 bug）。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        m = re.search(r"-?\d+", value)
        if m:
            return int(m.group())
    return None


def _dig(data: Any, path: tuple[str, ...]) -> Any:
    """按路径取嵌套字典里的值，中途不是字典就返回 None（不抛异常）。"""
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _device_reply_text(payload: Any) -> tuple[bool, str]:
    """把固件 ``tools/call`` 的回包拆成 ``(是否成功, 文本)``。

    固件 ``McpTool::Call`` 的形状（``main/mcp_server.h``）：
      成功 → ``{"result":{"content":[{"type":"text","text":"..."}],"isError":false}}``
      失败 → ``{"error":{"message":"..."}}``

    bool 结果被序列化成 ``"true"`` / ``"false"``，**cJSON 对象 / std::string
    结果被序列化成一段 JSON 字符串**（所以 get_device_status 拿到的 text
    还要再 json.loads 一次 —— 这是最容易写错的一步）。
    """
    if not isinstance(payload, dict):
        return False, ""
    err = payload.get("error")
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return False, str(msg or "")
    result = payload.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        return False, ""
    for item in (result.get("content") or []):
        if isinstance(item, dict) and item.get("type") == "text":
            return True, str(item.get("text") or "")
    return True, ""


def _device_status(device_call: Any) -> dict[str, Any] | None:
    """读设备状态，失败一律返回 None（不抛）。

    ``self.get_device_status`` 的 text 是 ``WifiBoard::GetDeviceStatusJson()``
    的返回值，即一段 JSON **字符串**：
    ``{"audio_speaker":{"volume":70},"screen":{"brightness":100,...},"battery":
    {"level":50,"charging":true},...}``
    """
    try:
        payload = device_call("self.get_device_status", {}, _DEVICE_OP_TIMEOUT)
    except Exception as exc:  # noqa: BLE001  超时/断链都归到"读不到"
        log.info("get_device_status 失败：%s: %s", type(exc).__name__, exc)
        return None
    ok, text = _device_reply_text(payload)
    if not ok or not text:
        log.info("get_device_status 回包不可用（ok=%s）", ok)
        return None
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001  固件版本差异：解析不了就当读不到
        log.info("get_device_status 的 text 不是 JSON：%r", text[:120])
        return None
    return data if isinstance(data, dict) else None


def _set_device_level(kind: str, args: dict[str, Any],
                      device_call: Any) -> dict[str, Any]:
    """调音量 / 亮度。``kind`` 取 ``_LEVEL_SPECS`` 的键。

    支持两种说法（老人不会说"音量 50"这种话，但子女在 App 里会）：
      * ``level``  绝对值 0-100 —— 一次往返；
      * ``delta``  相对量 —— **必须先读当前值**再算，两次往返。

    为什么相对量不能"猜一个当前值"：猜错方向的后果是老人喊「大点声」声音
    反而变小，他会以为设备坏了。读不到当前值就如实让老人说个数字。
    """
    spec = _LEVEL_SPECS[kind]
    label = str(spec["label"])

    if device_call is None:
        log.warning("%s：没有设备通道（device_call=None）", kind)
        return {"ok": False, "message": f"现在连不上设备，{label}没法调"}

    level = _coerce_int(args.get("level"))
    delta = _coerce_int(args.get("delta"))
    if level is None and delta is None:
        return {"ok": False, "need_clarify": True,
                "message": f"没听出要调大还是调小{label}，请反问老人"}

    if level is None:
        current = _coerce_int(_dig(_device_status(device_call) or {},
                                   tuple(spec["path"])))
        if current is None:
            log.info("%s：读不到当前值，无法按相对量调整", kind)
            return {"ok": False,
                    "message": f"没读到设备现在的{label}，请让老人说个具体数值"
                               f"（比如 30、50）"}
        level = current + int(delta or 0)

    level = max(0, min(100, int(level)))

    try:
        payload = device_call(str(spec["tool"]), {str(spec["arg"]): level},
                              _DEVICE_OP_TIMEOUT)
    except TimeoutError:
        log.warning("%s：设备 %.1f 秒内没回包", kind, _DEVICE_OP_TIMEOUT)
        return {"ok": False,
                "message": f"设备没有回应，{label}可能没调成，请老人再说一次"}
    except Exception as exc:  # noqa: BLE001  设备离线 / WS 已断
        log.warning("%s：下发失败 %s: %s", kind, type(exc).__name__, exc)
        return {"ok": False, "message": f"现在连不上设备，{label}没法调"}

    ok, text = _device_reply_text(payload)
    if not ok:
        log.warning("%s：设备回 error / isError（%s）", kind, text[:160])
        # "Unknown tool" 不是故障：这台设备没屏幕背光（或固件没编 LVGL），
        # 固件里压根没注册 self.screen.set_brightness。
        if "unknown tool" in text.lower():
            return {"ok": False, "unsupported": True,
                    "message": f"这台设备不支持调{label}，一句话说明白就好，"
                               f"不要让老人再试"}
        return {"ok": False,
                "message": f"设备说没调成，请老人再试一次"}

    log.info("%s：已设为 %s（device_pk 无关，设备侧自管）", kind, level)
    if kind == "volume" and level == 0:
        # 静音之后我们自己的 TTS 也听不见了 —— 还让模型"自然回应老人"
        # 就等于对着一台哑掉的设备说话，老人会以为设备坏了。
        return {"ok": True, "level": 0,
                "message": "已经静音（音量 0）。老人现在**听不到任何声音**，"
                           "所以本轮只说一句极短的话留给屏幕字幕（如「已静音」），"
                           "不要提问，也不要解释"}
    return {"ok": True, "level": level,
            "message": f"{label}已经调到 {level}（0-100）。用一句话自然回应老人"
                       f"（可以说「调好了，您听听合不合适」），不要解释数值范围"}


def _get_device_status(device_call: Any) -> dict[str, Any]:
    """把设备状态挑成老人关心的几个字段。

    刻意**不把原始 JSON 整段喂给模型**：里面还有 ssid / 芯片温度 / rssi，
    模型很可能把「rssi -60」直接念出来。只给需要的四个数。
    """
    if device_call is None:
        return {"ok": False, "message": "现在连不上设备，查不到状态"}

    status = _device_status(device_call)
    if status is None:
        return {"ok": False, "message": "设备没有回应，查不到当前状态"}

    out: dict[str, Any] = {}
    volume = _coerce_int(_dig(status, ("audio_speaker", "volume")))
    if volume is not None:
        out["volume"] = volume
    brightness = _coerce_int(_dig(status, ("screen", "brightness")))
    if brightness is not None:
        out["brightness"] = brightness
    battery = _coerce_int(_dig(status, ("battery", "level")))
    if battery is not None:
        out["battery_level"] = battery
    charging = _dig(status, ("battery", "charging"))
    if isinstance(charging, bool):
        out["charging"] = charging

    if not out:
        return {"ok": False, "message": "设备没有上报可用的状态"}

    return {"ok": True, "status": out,
            "message": "用一句话把上面的数字翻译成老人听得懂的话"
                       "（如「电量还有八成，正插着电」），"
                       "不要念字段名，不要说英文"}


def _create_reminder(db: Any, device_pk: int, args: dict[str, Any]) -> dict[str, Any]:
    title = str(args.get("title") or "").strip()
    when = str(args.get("when") or "").strip()
    repeat = str(args.get("repeat") or "").strip()
    if not title:
        return {"ok": False, "need_clarify": True,
                "message": "没听清要提醒什么，请反问老人提醒的内容"}
    if not when and not repeat:
        return {"ok": False, "need_clarify": True,
                "message": "没听出时间，请反问老人具体时间（如「明天早上八点」）"}
    if repeat and not when:
        when = repeat
    elif repeat:
        when = f"{when}{repeat}" if repeat not in when else when

    parsed = parse_when(when)
    if not parsed.ok:
        return {"ok": False, "need_clarify": True, "message": parsed.reason}

    # 这条提醒按**设备所在时区**落库（2026-10-21 跨时区改造）。
    # 老人说"明天早上八点"，指的是他眼前那个钟的八点 —— 服务端在哪个时区
    # 与这件事无关。判不出来（设备没上报过 IP）就留空串 = config.TZ，
    # 与改造前逐字一致，不会让存量设备的时间发生任何漂移。
    from .. import geo
    from ..schedule import zone_of

    dev = db.get(models.Device, device_pk)
    rule_tz = geo.device_timezone(dev)
    zone = zone_of(rule_tz)
    # "明天""后天"这类相对词必须按同一个时区解释：用北京时间的 now 去算
    # "明天"，在美西可能整体差一天。parse_when 支持显式 now，传进去即可。
    parsed = parse_when(when, now=datetime.now(zone))
    if not parsed.ok:
        return {"ok": False, "need_clarify": True, "message": parsed.reason}

    r = models.Reminder(
        device_id=device_pk,
        creator_id=None,
        tz=rule_tz,
        title=title[:120],
        speak_text="",
        rtype=_guess_rtype(title),
        schedule_kind=parsed.schedule_kind,
        times_of_day=parsed.times_of_day,
        weekdays=parsed.weekdays,
        month_days=parsed.month_days,
        interval_days=parsed.interval_days,
        once_date=parsed.once_date,
        require_confirm=True,
        enabled=True,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    # 立即排期，保证设备下次心跳就能拉到（与 routers/reminders.create 同口径）
    materialize_events(db, device_pk)
    nxt = next_occurrence(r)
    return {
        "ok": True,
        "id": int(r.id),
        "summary": f"「{r.title}」，{parsed.speak}",
        "next_fire_at": nxt.isoformat() if nxt else None,
        "hint": "请用一句话跟老人复述确认，把时间说清楚",
    }


def _list_reminders(db: Any, device_pk: int) -> dict[str, Any]:
    rows = list(db.query(models.Reminder)
                .filter(models.Reminder.device_id == device_pk)
                .order_by(models.Reminder.id.desc()).limit(20).all())
    if not rows:
        return {"ok": True, "count": 0, "reminders": [],
                "message": "老人当前没有任何提醒，如实告知"}
    return {"ok": True, "count": len(rows),
            "reminders": [{"id": int(r.id), "title": r.title,
                           "enabled": bool(r.enabled),
                           "next_fire_at": (next_occurrence(r).isoformat()
                                            if next_occurrence(r) else None)}
                          for r in rows],
            "descriptions": [_describe_reminder(r) for r in rows]}


def _resolve_one(db: Any, device_pk: int, args: dict[str, Any],
                 what: str) -> tuple[models.Reminder | None, dict[str, Any] | None]:
    """按 id 或关键词定位唯一一条提醒。返回 (提醒, 错误结果)。"""
    rid = args.get("reminder_id")
    keyword = str(args.get("title") or "").strip()
    if rid:
        try:
            r = db.get(models.Reminder, int(rid))
        except (TypeError, ValueError):
            r = None
        if r is None or int(r.device_id) != int(device_pk):
            return None, {"ok": False, "message": f"没有找到 id 为 {rid} 的提醒"}
        return r, None
    if not keyword:
        return None, {"ok": False, "need_clarify": True,
                      "message": f"没听清要{what}哪条提醒，请反问老人是哪一条"}
    rows = _find_reminders(db, device_pk, keyword)
    if not rows:
        return None, {"ok": False,
                      "message": f"没有找到和「{keyword}」有关的提醒，如实告知老人"}
    if len(rows) > 1:
        # 多条相似：**不猜**。把候选给 LLM，让它让老人自己选。
        return None, {"ok": False, "need_clarify": True,
                      "candidates": [_describe_reminder(r) for r in rows],
                      "message": f"找到 {len(rows)} 条和「{keyword}」有关的提醒，"
                                 f"请让老人确认是哪一条（把时间念出来给老人听）"}
    return rows[0], None


def _delete_reminder(db: Any, device_pk: int, args: dict[str, Any]) -> dict[str, Any]:
    r, err = _resolve_one(db, device_pk, args, "删除")
    if err:
        return err
    title = r.title
    db.delete(r)
    db.commit()
    return {"ok": True, "deleted": title,
            "message": f"已删除「{title}」，请用一句话告知老人"}


def _toggle_reminder(db: Any, device_pk: int, args: dict[str, Any]) -> dict[str, Any]:
    r, err = _resolve_one(db, device_pk, args, "暂停或恢复")
    if err:
        return err
    want = args.get("enabled")
    r.enabled = (bool(want) if isinstance(want, bool) else not r.enabled)
    db.commit()
    db.refresh(r)
    nxt = next_occurrence(r)
    state = "恢复了" if r.enabled else "暂停了"
    tail = ""
    if r.enabled and nxt is not None:
        tail = (f"，下次{_day_word(nxt.date(), local_now().date())}"
                f"{_period_phrase(nxt.hour * 60 + nxt.minute)}"
                f"{_clock_phrase(nxt.hour * 60 + nxt.minute)}")
    return {"ok": True, "id": int(r.id), "title": r.title, "enabled": bool(r.enabled),
            "message": f"已经{state}「{r.title}」{tail}，请用一句话告知老人"}


# ================================================================ 天气
def _get_weather(db: Any, device_pk: int, args: dict[str, Any]) -> dict[str, Any]:
    """查天气。真正的逻辑在 ``app/weather/service.py``，这里只做参数翻译。

    为什么 ``import`` 放在函数内（而不是模块顶部）
    --------------------------------------------
    天气是**可选子系统**：没配和风密钥时服务该正常跑，提醒照响。天气模块会
    拉 PyJWT + cryptography（约 10MB，且签名相关依赖出问题的方式通常很吵）。
    把它放在函数内，最坏情况只是「问天气时这一条工具报错」，**不会**连带
    整个语音链路的 tools 模块 import 失败 —— 那会让提醒、确认服药一起瘫痪。
    这是「可选功能不得拖垮主链路」的具体落实，不是随手写的延迟导入。
    """
    from ..weather import get_daily, get_now

    kind = str(args.get("kind") or "now").strip().lower()
    city = str(args.get("city") or "").strip()

    # 设备对象只用来读 settings["weather_city"]（常驻城市）。
    # 取不到就传 None，service 层会回落到 ELDER_WEATHER_DEFAULT_CITY。
    device = db.get(models.Device, device_pk)

    if kind == "daily":
        try:
            days = int(args.get("days") or 3)
        except (TypeError, ValueError):
            days = 3
        return get_daily(db, city, days=days, device=device)

    return get_now(db, city, device=device)
