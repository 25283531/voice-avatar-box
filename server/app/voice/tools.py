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
from typing import Any, Final

from sqlalchemy import select

from .. import models
from .. import reminder_audit
from ..memory.textutil import sanitize_for_prompt
from ..persona.med_categories import match_medicine
from ..schedule import (ScheduleKind, clear_pending_events, event_due_utc,
                        event_instant, local_now, materialize_events,
                        next_occurrence)

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
                        "description": (
                            "提醒**做什么**，十几个字以内，如「吃药」「喝水」「下楼走走」。"
                            "⚠️ 必须是老人说的那件具体的事。**绝对不许**填「提醒」"
                            "「提醒事项」「事项」「任务」「日程」这类分类词 —— "
                            "填了服务端会退回让你重问。老人没说清内容就反问，别猜。"
                        ),
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
                    "dose": {
                        "type": "string",
                        "description": (
                            "老人提到的剂量，如「一片」「半片」「两粒」，没说就留空。"
                            "**只填剂量本身**，不要把药名塞进来 —— 药名属于 title。"
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
                "确认老人刚刚服用了**哪一味药**（服药提醒的逐味确认）。"
                "老人说「我吃过了」「降压药吃了」「氨氯地平吃了」时"
                "**必须**调用本工具，不能只口头答应 —— 不调用的话服务端不会"
                "记下确认，超时后这条提醒会被判成「漏服」并通知家属。\n"
                "medicine：把老人说的那个词**原样**填进来（「降压药」「二甲双胍」"
                "都行，服务端有一张专门的匹配表，你不要自己把类别词换成药名）。"
                "老人只说「吃了」、没点名具体药时留空 —— 留空时只有屏上恰好"
                "只有一条单味药的提醒才会被确认，否则工具会让你先问清是哪一种。\n"
                "返回里可能带 ambiguous（候选药名）/ matched=false（没对上）/ "
                "need_detail（要问清是哪一味）。按返回的 message 追问即可，"
                "这些都不是失败，不要说「失败了」。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "medicine": {
                        "type": "string",
                        "description": (
                            "老人这次确认的是哪一味药，原样传他说出的词"
                            "（如「降压药」「二甲双胍」）；"
                            "老人没点名具体药（只说「吃了」）时留空"
                        ),
                    },
                },
            },
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
    {
        "type": "function",
        "function": {
            "name": "get_wifi_info",
            # 追加在末尾（同上）：前面工具的 JSON 顺序不变，prompt 前缀缓存不被打掉。
            "description": (
                "查询设备现在连的是哪个 WiFi。\n"
                "老人问「连的哪个网」「WiFi 是哪个」「网络好不好」时调用，"
                "**不要凭印象编**。\n"
                "返回 wifi_name（WiFi 名字）和 signal（信号强弱，已经是"
                "「很好/一般/较弱」这种口语词）。\n"
                "用一句话回答，**不要念信号数字、不要提 rssi、不要说英文**。\n"
                "如果设备用的是网线或没连 WiFi，如实说「这台没连 WiFi」。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_headlines",
            "description": (
                "播报今天的新闻头条。老人问「今天有什么新闻」「说说明天的大事」"
                "「念念头条」时调用。\n"
                "返回一个标题列表（已经清洗过、可直接念）。\n"
                "**只念标题，不要补充背景、不要评论、不要发表看法** —— "
                "你只是把新闻念给他听。\n"
                "一次最多念三条，每条之间停顿一下；老人说「还有吗」再念剩下的。\n"
                "如果返回 ok=false，就说「今天没取到新闻，回头再试试」，"
                "不要编造任何新闻内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "念几条，默认 3，最多 5。老人说「多念几条」时才加大。",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_status_light",
            "description": (
                "开关设备上的**状态灯**（待机呼吸、聆听、说话、表情这几种）。\n"
                "老人说「把灯关了」「太亮了」「晚上别亮灯」时传 on=false；"
                "说「把灯打开」「开灯」时传 on=true。\n"
                "⚠️ 只影响状态灯。**提醒闪灯、服药未确认、求助告警、按键指示灯"
                "永远不受影响** —— 这一点必须如实告诉老人："
                "「灯关了，但有提醒还是会亮」，不要笼统说「灯全关了」。\n"
                "老人问「灯是不是关着」时传 on=null（或不传）只查状态，不要改。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "on": {
                        "type": "boolean",
                        "description": "true=打开状态灯，false=关闭状态灯；"
                                       "不传则只查询当前状态，不改变。",
                    },
                },
            },
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
    """把分钟数说成老人习惯的说法：八点 / 八点半 / 八点一刻。

    ⚠️ 45 分**不说"三刻"**（2026-10-29 改）：老人对"三刻"的把握远不如
    "45 分"，而且这三个字在 TTS 里容易被连读成"三点"。这一刻钟的差别不值得
    冒"听错时间"的风险 —— 服药提醒错半小时是事故。
    """
    h, m = divmod(minutes, 60)
    hh = h if h <= 12 else h - 12
    if m == 0:
        return f"{hh}点"
    if m == 30:
        return f"{hh}点半"
    if m == 15:
        return f"{hh}点一刻"
    return f"{hh}点{m:02d}分"


# ---------------------------------------------------------------- 钟点对账
#
# 2026-10-29 真机事故：老人说「今天中午十二点四十五提醒我下楼」。模型把
# ``title`` 填成「提醒」、把 ``when`` 填成「12:55」——**两个参数都错**，
# 但它复述给老人的是「好的，我给您记着，十二点四十五提醒您下楼」。
# 复述完全正确，落库完全错误：12:45 什么都没响，12:55 响了一条叫「提醒」的
# 提醒。老人对着一条不知道是什么的提醒按了确认，然后认定"设备不准"。
#
# 这里只提供"从一段话里把钟点抽出来"的能力，**对账逻辑在 session.py**
# （只有那边同时拿得到模型的复述和工具落库的结果）。
_CLOCK_MENTION_RE = re.compile(
    r"(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*点\s*"
    r"(?:(半)|(一刻)|(三刻)|(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*分?)?"
)

#: 光杆「一点」几乎都是「慢一点 / 早一点 / 差一点 / 快一点」，不是钟点。
#: 只有紧跟这几个字时才当时间看（「一点整」「一点提醒我」）。
_CLOCK_ONE_OK_NEXT = ("整", "提", "吃", "该", "要", "去", "我", "叫", "半", "钟")


def clock_mentions(text: str) -> list[tuple[int, int]]:
    """把一段话里出现的所有钟点抽出来，返回 ``[(hour12, minute), ...]``。

    ``hour12`` 是**取模 12 后**的小时（下午一点和凌晨一点都返回 1），因为
    复述里带不带「下午」完全取决于模型的措辞，不能拿它当判据。

    刻意做得**宽进严出**：
    * 抽不全没关系（漏了只是少一条对账依据）；
    * 抽错会误拦正确播报 —— 所以「一点」这种高频歧义词单独加了白名单，
      宁可漏掉一个真钟点，也不要把「您慢一点」当成 1 点钟。
    """
    s = _normalize(text)
    out: list[tuple[int, int]] = []
    for m in _CLOCK_MENTION_RE.finditer(s):
        h = _cn_number(m.group(1))
        if h is None or not 0 <= h <= 24:
            continue
        if m.group(2):
            mm = 30
        elif m.group(3):
            mm = 15
        elif m.group(4):
            mm = 45
        elif m.group(5):
            mm = _cn_number(m.group(5))
            if mm is None or not 0 <= mm < 60:
                continue
        else:
            mm = 0
            if h == 1 and s[m.end():m.end() + 1] not in _CLOCK_ONE_OK_NEXT:
                continue
        out.append((h % 12, mm))
    return out


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
    elif re.search(r"每周|每星期|星期[一二三四五六天末日]|礼拜[一二三四五六天末日]|周[一二三四五六天末日]", s):
        kind, repeat_word = ScheduleKind.WEEKLY, "每周"
        for m in re.finditer(r"(?:星期|礼拜|周)([一二三四五六天末日])", s):
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
            wd = re.search(r"(?:星期|礼拜|周)([一二三四五六天末日])", s)
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


#: 这些词当标题 = 模型没听清内容，拿**分类词**凑数（2026-10-29 真机事故：
#: 老人说「十二点四十五提醒我下楼」，落库的标题就是「提醒」两个字）。
#: 一条叫「提醒」的提醒对老人毫无意义 —— 到点响了，他不知道要做什么，
#: 只能按确认把它消掉。**这比不建还糟**：他会以为事情已经安排好了。
_TITLE_PLACEHOLDERS = frozenset({
    "提醒", "提醒事项", "提醒我", "提醒一下", "提醒您", "提醒他",
    "事项", "任务", "日程", "日程提醒", "待办", "待办事项",
    "闹钟", "闹铃", "定时", "事情", "内容", "未命名", "新建提醒", "新提醒",
})

#: 占位词 + 语气词/代词（「提醒一下吧」「事项提醒」「提醒我」）。带**内容**的
#: 标题（「提醒我下楼」「提醒吃药」）不会命中 —— 后半段不在可选集合里。
_PLACEHOLDER_TITLE_RE = re.compile(
    r"(?:请)?(?:提醒|提醒我|提醒您|事项|任务|日程|闹钟|待办|定时)"
    r"(?:我|您|一下|我一下|您一下|一下我|事项|提醒|吧|了|啊)*"
)


#: 语音链路的审计 actor 显示名。老人没有账号（设备语音不走登录态），
#: 所以 ``actor_id`` 恒为 None，用这个名字说明"是老人自己说的"。
_VOICE_ACTOR = "老人（语音）"


def _is_placeholder_title(title: str) -> bool:
    """标题是不是「提醒 / 事项」这类占位词（= 模型没听清内容就拿分类词凑数）。"""
    t = _normalize(title)
    if not t:
        return True
    if t in _TITLE_PLACEHOLDERS:
        return True
    return bool(_PLACEHOLDER_TITLE_RE.fullmatch(t))


def _find_reminders(db: Any, device_pk: int, keyword: str) -> list[models.Reminder]:
    # 软删除（2026-10-30）：已删除的提醒对老人来说"不存在"。
    # 漏了这个过滤 ⇒ 老人说「删掉吃药提醒」时，_resolve_one 可能选中一条
    # 早已删除的行，回一句"已删除「吃药」"（他其实没删成任何东西），
    # 或者更糟：把一条已删的重新暂停/恢复，让它在设备上又活过来。
    rows = list(db.query(models.Reminder)
                .filter(models.Reminder.device_id == device_pk,
                        models.Reminder.deleted_at.is_(None))
                .order_by(models.Reminder.id.desc()).all())
    if not keyword:
        return rows
    kw = keyword.strip()
    exact = [r for r in rows if (r.title or "").strip() == kw]
    if exact:
        return exact
    return [r for r in rows if kw in (r.title or "")]


#: 提醒类别的中文名（``models.ReminderType``）。给 LLM 看**中文**而不是 0/1/2：
#: 只丢一个整数过去，模型既不会念给老人听，也分不清「服药」和「喝水」。
_RTYPE_LABELS = {
    int(models.ReminderType.MEDICINE): "服药",
    int(models.ReminderType.SCHEDULE): "日程",
    int(models.ReminderType.WATER): "喝水",
    int(models.ReminderType.EXERCISE): "运动",
    int(models.ReminderType.CUSTOM): "自定义",
}


def _medicines_of(r: models.Reminder) -> list[dict[str, str]]:
    """这条提醒的药 + 剂量（**含老数据派生**），与 ``schemas.ReminderOut`` 同口径。

    为什么这里再实现一遍派生而不是复用 Pydantic：语音链路直接吃 ORM 行
    （``_list_reminders`` / ``_describe_reminder`` 都不构造 ReminderOut），
    为了拿这个字段去 ``model_validate`` 一整份 schema 是舍近求远。
    ⚠️ 口径必须与 ``schemas.ReminderOut._derive_medicines`` **逐字一致**
    （同样是"medicines 为空且 dose 非空 ⇒ [{title, dose}]"、同样截断 32/64），
    否则老人从语音里听到的药和网页上看到的药会不一样。

    ⚠️ 服务端另有一份归一 ``schemas.derive_medicines()``（``ReminderOut`` 与
    首页接口共用），两处语义不同是**刻意的**，不是漏改：
      · 这边：**原样每味都念**，不做任何省略 —— 老人要靠耳朵听全；
      · 那边：给界面排版用，只剩一味且药名 === title 时省掉重复药名。
    改这里时去确认那边是否也要改 —— 只改一处就以为改全了，正是这类
    "两份归一"最典型的翻车方式。
    """
    items = [{"name": str(m.get("name") or "")[:32],
              "dose": str(m.get("dose") or "")[:64]}
             for m in (r.medicines or []) if isinstance(m, dict)]
    items = [m for m in items if m["name"]]
    if items:
        return items
    dose = (r.dose or "").strip()
    name = (r.title or "").strip()[:32]
    if dose and name:
        return [{"name": name, "dose": dose[:64]}]
    return []


def _medicine_phrase(r: models.Reminder) -> str:
    """把药 + 剂量拼成一句话（给 LLM 念）。没有剂量时返回空串。

    * 一味药 → ``剂量 1片``（药名已经在 title 里，重复念一遍老人会觉得啰嗦）
    * 多味药 → ``药 降压药 1片、二甲双胍 0.5g``（**必须逐味列出**：老人要听的
      就是"今天吃哪几种、各吃多少"，只说"两种药"等于没说）
    """
    items = _medicines_of(r)
    if not items:
        return ""
    if len(items) == 1:
        dose = items[0]["dose"]
        return f"剂量 {dose}" if dose else ""
    return "药 " + "、".join(
        f"{m['name']} {m['dose']}".strip() for m in items)


#: 服药确认的时间窗（分钟）：提醒到点前 2 分钟起就算「可以确认」。
#:
#: 为什么必须有这个窗口，而不是把所有 PENDING 都当候选：实例是**提前若干
#: 小时**预排的（``config.SCHEDULE_AHEAD_HOURS``），不过滤的话老人早上说一句
#: 「吃了降压药」，会把晚上八点那条也一并确认掉 —— 一条真实存在、却还没到点
#: 的提醒被写成"已服用"，家属看到的就是假记录。
_CONFIRM_WINDOW_BEFORE_MIN = 2

#: 服药确认的**过期后**窗口（分钟）：4 小时。与固件 ``ui.c:31`` 的
#: ``ELDER_UI_REMINDER_HOLD_US``（4 小时）必须是同一个数。
#:
#: ---- 这个常量是 2026-11-04 用户实报事故的根因 ----
#: 固件那边的语义是「待办卡片在屏幕上挂多久」：到点触发后 4 小时内，卡片一直
#: 全屏显示、#1 呼吸灯一直亮，老人随时可以长按 3 秒确认（用户 2026-10-31 定的：
#: 「确认窗口在4个小时内只要屏幕显示的是待办事项就可以按键进行确认」）。
#:
#: 但 ``scheduler._tick`` 在 ``due + escalate_after_min``（默认 30 分钟）就把
#: 实例落成 ``MISSED(3)``，而本函数原来只认 ``PENDING/FIRED`` ⇒
#: **第 30 分钟起服务端眼里就没有这条提醒了，屏幕上却还挂着 3.5 小时**。
#:
#: 生产库 ev=125 是这条的完整样本：due=12:43、设备 16:19:26 才响（之前离线）、
#: state=3、note='timeout' ⇒ ``pending_medicine_events`` 返回空 ⇒
#: ① ``_pending_reminder_label`` 空 ⇒ system 里没有待确认清单 ⇒ 模型不知道有
#:    提醒，一次 ``confirm_reminder`` 都没调（日志 ``tool=0ms``）；
#: ② ``_maybe_confirm_fallback`` 在 ``if not (self._pending_reminder and ...)``
#:    第一道闸就返回，连「兜底确认跳过」那行都不打。
#: 老人看到的就是「喊了半天确认，他什么都没听进去，提醒一直挂在屏上」。
#:
#: 所以这里必须补上「过期后窗口」，而且判据要与**屏幕**同源：屏还挂着就还能
#: 确认。屏幕的计时起点是"卡片真正显示出来的那一刻"（设备迟响时晚于 due），
#: 所以下面用 ``max(due, fired_at)`` 作锚点，见 ``_confirm_deadline``。
_CONFIRM_WINDOW_AFTER_MIN = 240


def _confirm_anchor(ev: Any) -> datetime:
    """这一实例"可确认窗口"的起点（aware UTC）。

    正常情况下就是 ``due_at``；但设备离线 / 重启之后可能**迟响**（生产库
    ev=125 迟了 3 小时 36 分），而屏幕的 4 小时计时是从卡片显示那一刻才开始的
    （``ui.c`` 在 ``OnReminder`` 里设 ``reminder_expire_us``）。锚点不跟着迟响
    走的话，会出现「屏上还挂着、服务端说早就过期了」—— 正是本次事故。

    ``fired_at`` 由设备上报，``schemas.LOCAL_WALL_FIELDS`` 把它归一成**北京
    墙上时间**（与 ``due_at`` 同一口径），所以这里和 ``event_due_utc`` 一样走
    ``event_instant`` 还原绝对时刻。不用设备时钟做减法：那需要设备时区，而
    固件目前不上报自己的时区（``report_event`` 的 docstring 有同样说明）。
    """
    due = event_due_utc(ev)
    fired_at = getattr(ev, "fired_at", None)
    if fired_at is None:
        return due
    try:
        fired = event_instant(fired_at, getattr(ev, "tz", "") or "",
                              int(getattr(ev, "tz_offset_min", 0) or 0))
    except Exception:  # noqa: BLE001  脏时间戳只该让它退回 due，不该整条消失
        return due
    return fired if fired > due else due


def medicine_names(r: models.Reminder) -> list[str]:
    """这条提醒的药名清单（与语音播报**同口径**，见 ``_medicines_of``）。

    为什么复用 ``_medicines_of`` 而不是 ``schemas.derive_medicines``：后者是给
    界面排版用的（只剩一味且药名 === title 时会省掉重复药名），而确认判据必须
    按"老人实际要吃几味"来算 —— 少算一味就会让「全部确认」提前成立，把只吃了
    一味的记录写成吃全了。``session._pending_reminder_label`` 也调这里，
    两条链路共用一份口径，不再各自拼。
    """
    return [m["name"] for m in _medicines_of(r)]


#: 标点/空白归一用：判断"这句是不是一个光秃秃的应答"之前必须先去掉它们。
#: ASR 输出稳定地带着句号（生产日志里是「确认。」），不去掉就永远比不中。
_YES_STRIP_RE: Final[re.Pattern[str]] = re.compile(r"[\s。，、！？；：,.!?;:~～…·\"'“”‘’]+")

#: 「整条确认」的短应答白名单。**故意不含「好 / 好的 / 行 / 知道了」** ——
#: 理由与 ``session._CONFIRM_RE`` 上方那段一致：那几句在正常聊天里到处都是，
#: 误判的后果是"没吃药却被记成吃了"。
#:
#: 这份清单存在的直接原因（2026-11-04 用户实报）：
#: 设备长按 3 秒后念的是「我帮您确认一下，您刚才已经服用过降压药了吗？」，
#: 老人最自然的回答就是「确认」。而 ``session._CONFIRM_RE`` 里**没有「确认」
#: 两个字**，``match_medicine("确认", [...])`` 也返回空 ⇒ 服务端把这句话当成
#: 一次普通闲聊，既不写记录也不追问，提醒永远挂在屏上。
_WHOLE_YES: Final[frozenset[str]] = frozenset({
    "确认", "确定", "对", "对的", "是", "是的", "嗯", "嗯嗯", "没错",
    "吃了", "吃过了", "吃完了", "吃过", "喝过了", "已经吃了", "我已经吃了",
    "都吃了", "都吃过了", "全都吃了", "全部吃了", "两样都吃了", "两种都吃了",
    "两味都吃了", "一起吃了", "都喝了", "全都喝了", "全部喝了",
})

#: 「不点名某一味、而是说'都/全部'」的形态。白名单之外还要认这种自由组合：
#: 「降压药和降脂药都吃了」里的"都吃了"是结论，但整句不在白名单里。
_WHOLE_ALL_RE: Final[re.Pattern[str]] = re.compile(
    r"(都|全部|全都|两样|两种|两味|一起)[^，。]{0,6}(吃|喝|服)")

#: ``is_whole_event_yes`` 的长度上限（归一后字符数）。
#: 12 个汉字足够覆盖「两样都吃了」「我已经全部吃过了」这类回答，又短到不会
#: 把一长串叙述截胡成"整条确认"。
_WHOLE_YES_MAX_LEN: Final[int] = 12


def is_whole_event_yes(text: str) -> bool:
    """这句话是不是"整条确认"（不点名某一味药，只说'确认 / 都吃了'）。

    用途只有一个：``_confirm_reminder`` 与 ``session._fallback_confirm_verdict``
    在「只有一条待确认提醒」时，据此决定"整条标完成"还是"继续追问哪一味"。

    ⚠️ 只判措辞，**不判上下文**。调用方必须自己先确认"候选恰好 1 条" ——
    这条函数对"两味药吃哪味"一无所知，单靠它确认就是替老人瞎确认。

    为什么必须限制长度（``_WHOLE_YES_MAX_LEN``）：老人会连着说一长串
    「我刚才吃完饭把降压药吃了还有那个降脂药也吃了」，那里面点名了药，
    该走 ``match_medicine`` 的逐味路径，不能被这里截胡。
    """
    norm = _YES_STRIP_RE.sub("", text or "")
    if not norm or len(norm) > _WHOLE_YES_MAX_LEN:
        return False
    if norm in _WHOLE_YES:
        return True
    return bool(_WHOLE_ALL_RE.search(norm))


def _mark_whole_event_confirm(ev: Any, names: list[str]) -> None:
    """把"整条确认（未逐味点名）"这件事**留痕**，不假装是逐味确认。

    为什么要单独留痕：``confirmed_medicines`` 填满之后 ``state`` 会落
    ``CONFIRMED``，与"一味一味点过名"的记录在库里长得**一模一样**。家属端
    和依从率统计因此分不出"他说了两味都吃了"和"他点了名说降压药吃了"。
    这不是靠加一列解决的（加列要迁移），``note`` 已经会展示给家属，改它够用。

    ⚠️ 不能覆盖已有的 ``note``：``'timeout'`` / ``'confirmed'`` 这些是审计
    线索，抹掉就查不出"这条曾经超时升级过"。
    """
    tag = models.WHOLE_EVENT_CONFIRM_TAG
    if len(names) > 1:
        tag += f"：{'、'.join(names)}"
    old = str(getattr(ev, "note", "") or "").strip()
    ev.note = f"{old}；{tag}" if old else tag


def pending_medicine_events(
    db: Any, device_pk: int,
) -> list[tuple[models.ReminderEvent, models.Reminder]]:
    """当前**待确认**的服药提醒实例（已到点、未确认），连同规则一起返回。

    规则侧判据与 ``scheduler._tick`` 的超时扫描逐条对齐（规则未软删 +
    ``require_confirm`` + ``rtype == MEDICINE``）。时间侧判据是**屏幕口径**，
    不是 ``state`` 口径 —— 见 ``_CONFIRM_WINDOW_AFTER_MIN`` 的长注释：

      * 已到点：``anchor <= now + _CONFIRM_WINDOW_BEFORE_MIN``；
      * 屏还挂着：``anchor >= now - _CONFIRM_WINDOW_AFTER_MIN``（4 小时）。

    ``state`` 仍然要过一遍白名单，但白名单里**必须有 MISSED(3)**：
    ``scheduler`` 在 ``due + escalate_after_min``（默认 30 分钟）就把实例落成
    MISSED，而固件屏幕还挂 4 小时。少了它，老人对着屏上的待办卡按满 3 秒、
    在对话里说清吃了哪味药，服务端会答「没有等着确认的提醒」——2026-11-04
    生产库 ev=125 就是这么挂死的（详见 ``_CONFIRM_WINDOW_AFTER_MIN``）。

    ``CONFIRMED(2)`` 与 ``SKIPPED(4)`` **不在**白名单里：那两条是"已经办完了"，
    再确认一次只会制造第二条假记录。

    一次 join 把 ``Reminder`` 一起捞出来：调用方每轮都要读每条的 ``medicines``，
    逐条 ``db.get`` 就是 N+1（这个函数在每轮对话开头都会被调一次）。
    """
    now = local_now()
    rows = db.execute(
        select(models.ReminderEvent, models.Reminder)
        .join(models.Reminder,
              models.Reminder.id == models.ReminderEvent.reminder_id)
        .where(
            models.ReminderEvent.device_id == device_pk,
            models.ReminderEvent.state.in_([int(models.EventState.PENDING),
                                            int(models.EventState.FIRED),
                                            int(models.EventState.MISSED)]),
            models.Reminder.deleted_at.is_(None),
            models.Reminder.require_confirm.is_(True),
            models.Reminder.rtype == int(models.ReminderType.MEDICINE),
        )
        .order_by(models.ReminderEvent.due_at)
    ).all()

    out: list[tuple[models.ReminderEvent, models.Reminder]] = []
    for ev, rule in rows:
        try:
            anchor = _confirm_anchor(ev)
        except Exception:  # noqa: BLE001  脏 tz 不该让整条确认链断掉
            log.debug("待确认提醒 due_at 还原失败（跳过）：ev=%s", ev.id)
            continue
        if anchor > now + timedelta(minutes=_CONFIRM_WINDOW_BEFORE_MIN):
            continue    # 还没到点：提前几小时预排的实例不算候选
        if anchor < now - timedelta(minutes=_CONFIRM_WINDOW_AFTER_MIN):
            continue    # 屏幕的 4 小时窗口已经过了，卡片早就收了
        out.append((ev, rule))
    return out


def _describe_reminder(r: models.Reminder) -> str:
    """给 LLM 看的一行描述（含下次触发时间，让它能念给老人）。"""
    nxt = next_occurrence(r)
    parts = [f"id={r.id}", f"「{r.title}」"]
    meds = _medicine_phrase(r)
    if meds:
        parts.append(meds)
    if not r.enabled:
        parts.append("已暂停")
    if nxt is not None:
        parts.append(f"下次 {_day_word(nxt.date(), local_now().date())}"
                     f"{_period_phrase(nxt.hour * 60 + nxt.minute)}"
                     f"{_clock_phrase(nxt.hour * 60 + nxt.minute)}")
    else:
        parts.append("没有未来的触发时间")
    return "，".join(parts)


#: 设备侧「立刻同步一次」的 MCP 工具名（固件 main/mcp_server.cc 注册）。
_DEVICE_SYNC_TOOL = "self.elder.sync_now"
#: 等它回包的上限。它只是往事件组里置一个位、立刻返回 true，
#: 给 2 秒已经宽得离谱 —— 真超时说明设备卡死，不该拖住老人的对话。
_DEVICE_SYNC_TIMEOUT = 2.0


def _request_device_sync(device_call: Any) -> None:
    """提醒表刚变了 ⇒ 叫设备**立刻**同步一次，别等下一个心跳周期。

    为什么必须有（2026-10-29 真机事故的第二层原因）：提醒由**设备本地**响
    （断网也能响，刻意设计），但服务端新加的提醒要等设备下次心跳才拿到，
    心跳周期由服务端下发的 ``sync_interval_sec`` 决定，默认 300 秒。
    老人 12:43:41 说「十二点四十五提醒我下楼」，12:45 到点只有 79 秒
    ⇒ **即使参数全对，设备手里也还没有这条提醒，必然漏**。

    固件早有现成钩子 ``elder_sync_request_now()``，但全仓没有任何外部
    调用者 —— 缺的就是这一下。它不像"把心跳调成 60 秒"那样要付 5 倍 TLS
    握手代价（那会拖累音频），只在真的改了提醒表时才叫一次。

    **失败一律静默**：设备离线 / 老固件没注册这个工具时，周期心跳照旧
    兜底。绝不能因为这个"锦上添花"的动作把"提醒建成了"变成"提醒建失败了"
    —— 调用方拿到的结果字典一个字都不改。
    """
    if device_call is None:
        return
    try:
        device_call(_DEVICE_SYNC_TOOL, {}, _DEVICE_SYNC_TIMEOUT)
        log.info("提醒表已变，已叫设备立刻同步（不必等下一个心跳）")
    except Exception as exc:  # noqa: BLE001  设备离线 / 老固件无此工具
        log.info("叫设备立刻同步没成功（%s: %s），等下一个心跳周期兜底",
                 type(exc).__name__, exc)


def _after_change(out: dict[str, Any], device_call: Any) -> dict[str, Any]:
    """提醒表变更的收尾：**成功才**叫设备立刻同步，结果字典原样返回。"""
    if out.get("ok"):
        _request_device_sync(device_call)
    return out


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
            return _after_change(_create_reminder(db, device_pk, args),
                                 device_call)
        if name == "list_reminders":
            return _list_reminders(db, device_pk)
        if name == "delete_reminder":
            return _after_change(_delete_reminder(db, device_pk, args),
                                 device_call)
        if name == "toggle_reminder":
            return _after_change(_toggle_reminder(db, device_pk, args),
                                 device_call)
        if name == "confirm_reminder":
            return _confirm_reminder(db, device_pk, device_call, args)
        if name == "get_weather":
            return _get_weather(db, device_pk, args)
        if name == "set_volume":
            return _set_device_level("volume", args, device_call)
        if name == "set_brightness":
            return _set_device_level("brightness", args, device_call)
        if name == "get_device_status":
            return _get_device_status(device_call)
        if name == "get_wifi_info":
            return _get_wifi_info(device_call)
        if name == "get_headlines":
            return _get_headlines(args)
        if name == "set_status_light":
            return _set_status_light(args, device_call)
        return {"ok": False, "message": f"没有叫「{name}」的工具"}
    except Exception as exc:  # noqa: BLE001  兜底：工具失败不崩会话
        log.warning("工具 %s 执行失败：%s: %s", name, type(exc).__name__, exc)
        return {"ok": False, "message": "操作没成功，请老人稍后再试"}


#: 「正在响」的实例往回看多久。超过这个窗口还挂在 PENDING/FIRED 的，是
#: 历史残留（设备离线、回执丢了），不该参与"到底在确认哪一条"的判定。
_CONFIRM_LOOKBACK_HOURS = 4


def _sole_confirmable_event(db: Any, device_pk: int) -> Any:
    """该设备**恰好一条**「已到点、未确认」的实例；0 条或多条都返回 None。

    这条路径拿不到 ``server_id``，只能按"此刻正在响的那条"反推。而反推的
    代价是**猜错就把 A 的确认记到 B 头上**（见 ``_confirm_reminder`` 里
    "设备时区不一致"那段），所以判据必须是"恰好一条"：两条同时挂着时宁可
    退回改造前的行为（只记语音），也不替老人编造一次确认。

    为什么不分 rtype：服药路径已经在 ``pending_medicine_events`` 里判过了，
    走到这里说明**服药候选是空的**，此刻还挂着的多半是喝水/运动/日程。
    日程类已经"播报即完成"（``device_api.report_event`` 落 AUTO），所以
    真正落到这里的通常只剩喝水/运动 —— 它们同样适用"按键 + 语音"的定义。
    """
    now = local_now()
    floor = now - timedelta(hours=_CONFIRM_LOOKBACK_HOURS)
    ceil = now + timedelta(minutes=_CONFIRM_WINDOW_BEFORE_MIN)
    rows = db.execute(
        select(models.ReminderEvent)
        .where(
            models.ReminderEvent.device_id == device_pk,
            models.ReminderEvent.state.in_([int(models.EventState.PENDING),
                                            int(models.EventState.FIRED)]),
        )
        .order_by(models.ReminderEvent.due_at.desc())
    ).scalars().all()

    alive = []
    for ev in rows:
        try:
            due = event_due_utc(ev)
        except Exception:  # noqa: BLE001  脏 tz 不该让整条确认链断掉
            continue
        if floor <= due <= ceil:
            alive.append(ev)
            if len(alive) > 1:
                return None      # 多于一条就不猜，直接放弃
    return alive[0] if alive else None


#: 「刚刚已经确认过」的回看窗口（分钟）。
#:
#: 存在的唯一理由 —— 2026-11-04 09:14/09:15 的现场（生产日志逐行核对过）：
#:   09:14:07  老人说「确认。」，服务端注入的待确认清单里**有**「新的服药提醒」
#:   09:14:08  大模型开始生成 —— 然后首字卡了 **65.4 秒**（ttft=65393ms）
#:   09:15:12  这 65 秒里老人自己长按了按键，设备把那条写成 CONFIRMED
#:   09:15:13  服务端终于执行 confirm_reminder，待确认清单当然是空的
#:   09:15:16  退回设备旧路径，设备正忙着同步 POST 回执 ⇒ 超时
#:   09:15:18  模型照工具失败话术说「您按一下设备上的按键」—— 他刚刚按过
#: 老人看到的结论是「按键不能确认」，其实**已经确认成功了**。
#:
#: 窗口不能长：确认过 20 分钟以上还拿它当"刚刚"，会让真正需要再确认一次
#: 的场景（老人又没吃药）被静默吞掉。15 分钟足够覆盖一次首字卡顿 + 追问。
_RECENT_CONFIRM_MIN = 15


def _confirm_method_word(method: Any) -> str:
    """确认方式 → 说给大模型听的中文（它只用来组织话术，不上屏）。"""
    try:
        m = int(method or 0)
    except (TypeError, ValueError):
        return "未知"
    return {
        int(models.ConfirmMethod.VOICE): "语音",
        int(models.ConfirmMethod.BUTTON): "按键",
        int(models.ConfirmMethod.APP): "手机端",
        int(models.ConfirmMethod.VOICE_AND_BUTTON): "语音 + 按键",
    }.get(m, "未知")


def _recently_confirmed_medicine(
    db: Any, device_pk: int,
) -> tuple[Any, Any, int] | None:
    """该设备最近 ``_RECENT_CONFIRM_MIN`` 分钟内刚确认过的**服药**提醒。

    返回 ``(event, rule, 几分钟前)``；没有就 ``None``。

    为什么按 ``confirmed_at`` 判、不按 ``state`` 判：这两列是同一次回执写下去
    的，分不出先后；而这里要回答的问题是"是不是**刚刚**"，只有时间戳能答。

    为什么要给 ``+2 分钟`` 的上界：设备回执里的 ``confirmed_at`` 用的是设备自己
    的时钟，会略微超前服务端。超前太多（脏时间）就跳过，不能拿它冒充"刚刚"。
    """
    now = local_now()
    floor = now - timedelta(minutes=_RECENT_CONFIRM_MIN)
    ceil = now + timedelta(minutes=2)
    try:
        rows = db.execute(
            select(models.ReminderEvent, models.Reminder)
            .join(models.Reminder,
                  models.Reminder.id == models.ReminderEvent.reminder_id)
            .where(
                models.ReminderEvent.device_id == device_pk,
                models.ReminderEvent.state == int(models.EventState.CONFIRMED),
                models.ReminderEvent.confirmed_at.isnot(None),
                models.Reminder.rtype == int(models.ReminderType.MEDICINE),
            )
            .order_by(models.ReminderEvent.confirmed_at.desc())
            .limit(5)
        ).all()
    except Exception as exc:  # noqa: BLE001  查库失败只该让"回查"失效，不该崩对话
        log.warning("回查最近确认失败 %s: %s", type(exc).__name__, exc)
        return None

    for ev, rule in rows:
        try:
            at = event_instant(ev.confirmed_at, getattr(ev, "tz", ""),
                               int(getattr(ev, "tz_offset_min", 0) or 0))
        except Exception:  # noqa: BLE001  脏 tz，跳过这一行
            continue
        if floor <= at <= ceil:
            mins = max(0, int((now - at).total_seconds() // 60))
            return ev, rule, mins
    return None


def _already_confirmed_reply(ev: Any, rule: Any, mins: int) -> dict[str, Any]:
    """「已经确认过了」的统一话术。

    两个调用点（服药清单为空 / 设备超时）必须说同一句话，否则老人听到的
    结论取决于服务端走了哪条分支 —— 而他只关心"到底记上了没有"。
    """
    title = getattr(rule, "title", "") or "这条提醒"
    return {
        "ok": True,
        "confirmed": True,
        "already": True,
        "message": (
            f"「{title}」{mins} 分钟前就已经确认成功了"
            f"（确认方式：{_confirm_method_word(getattr(ev, 'confirm_method', 0))}）。"
            "请用一句话告诉老人已经记上了，"
            "**不要**再让他去按设备上的按键，也不要再问一遍吃没吃药。"
        ),
    }


def _arm_voice(db: Any, ev: Any) -> int | None:
    """把这条实例的确认方式**先**写成 VOICE，返回原值（供回滚）。

    这是「旧路径也能算全置信」的**唯一**前提，与服药路径同一个机制：
    ``device_api.report_event`` 只在「库里的 VOICE + 回执的 BUTTON」同时成立时
    才合并成 ``VOICE_AND_BUTTON(5)``。旧路径原来不写库，所以即使老人按了键
    （设备回执 BUTTON），库里还是 NONE ⇒ 永远合并不成，家属端只看到"按键确认"。

    必须**在 device_call 之前**写：设备的回执是异步的，可能在工具返回之前
    就到了；写到后面等于永远读不到 VOICE。

    已经是更高置信的方式（BUTTON / VOICE_AND_BUTTON / APP）就不覆盖 ——
    按键确认本身就是"人在场"的证据，不该被一次语音降级。
    """
    prev = int(getattr(ev, "confirm_method", 0) or 0)
    if prev in (int(models.ConfirmMethod.BUTTON),
                int(models.ConfirmMethod.VOICE_AND_BUTTON),
                int(models.ConfirmMethod.APP)):
        return None
    ev.confirm_method = int(models.ConfirmMethod.VOICE)
    db.commit()
    return prev


def _disarm_voice(db: Any, ev: Any, prev: int | None) -> None:
    """设备答 false / 出错时把 ``_arm_voice`` 的预写恢复回去。

    为什么必须回滚：预写发生在"还不知道设备认不认"的时候。若设备说"没有
    待确认的提醒"，这次对话其实什么也没确认，留着 VOICE 会让家属端看到
    「语音确认」而老人根本没确认过 —— 这是替老人编造依从记录。
    """
    if ev is None or prev is None:
        return
    try:
        ev.confirm_method = int(prev)
        db.commit()
    except Exception as exc:  # noqa: BLE001  回滚失败不该盖掉真正的错误
        db.rollback()
        log.warning("confirm_reminder：恢复确认方式失败 %s: %s",
                    type(exc).__name__, exc)


# 调设备 MCP 工具时等多久（秒）。
#
# ⚠️⚠️ 2026-11-04 现场事故：原来是 **3.0**，而设备侧处理
# `elder.confirm_reminder` 是**同步阻塞**的 ——
#   elder_bridge.cc:1576 → reminder.c:1484 → sync.c:943 里
#   `if (online) flush_queue();` → net.c:206 `esp_http_client_perform()`
# 也就是说"按一下键"要等设备把回执 POST 回服务端才算完，而那条 HTTP
# 请求 `keep_alive_enable=false`（每次重做 TCP+TLS 握手），
# 实测 **3.5 秒**。服务端只等 3.0 秒 ⇒ **每次都稳定输 0.5 秒**：
#   09:15:13.740 下发 → 09:15:16.745 判超时 → 09:15:17.210 回执才落库
#   09:18:53.400 下发 → 09:18:56.403 判超时 → 09:18:56.901 回执才落库
# 后果有两层：老人听到「请让老人按一下设备上的按键确认」（他刚按过）；
# 而且 `_disarm_voice()` 已经把预写的 VOICE 回滚，0.5 秒后到的 BUTTON
# 合并不上 ⇒ `confirm_method` 落成 2（仅按键）而不是 5（语音+按键全置信）。
#
# 与设备侧 `CONFIG_ELDER_HTTP_TIMEOUT_MS=8000`（sdkconfig.defaults:185）
# 对齐：设备最坏情况 8 秒才放弃，服务端就不该比它早。
_DEVICE_CALL_TIMEOUT_SEC = 8.0


def _device_pending_call(device_call: Any) -> dict[str, Any]:
    """不带 ``server_id`` 让设备确认**它自己挂着的那条** —— 真正的下发与解析。

    单独一层是为了让 ``_confirm_device_pending`` 能在**下发之前**预写确认方式、
    在下发之后按结果回滚，而这段"怎么判成功"的逻辑一个字都不用动。
    """
    if device_call is None:
        log.warning("confirm_reminder：没有设备通道（device_call=None）")
        return {"ok": False, "message": "现在连不上设备，请让老人按一下设备上的按键确认"}

    try:
        payload = device_call("self.elder.confirm_reminder", {},
                              _DEVICE_CALL_TIMEOUT_SEC)
    except TimeoutError:
        log.warning("confirm_reminder：设备 %d 秒内没回包",
                    int(_DEVICE_CALL_TIMEOUT_SEC))
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

    log.info("confirm_reminder：设备已确认（非服药提醒，走旧路径）")
    # ⚠️ 话术必须**带条件**，不能像改造前那样一律让老人"再长按 3 秒"：
    # 这条路径现在有两种结局 ——
    #   · 老人按过键（命中按键发起标记）⇒ 设备直落 DONE，屏幕已经收了。
    #     此时再说"请长按按钮 3 秒钟"，老人会对着黑屏按半天；
    #   · 老人只是说了句"吃过了" ⇒ 设备落 HALF_CONFIRMED，屏幕还亮着，
    #     确实需要补一次按键（2026-10-27 用户定义：语音确认只算半置信）。
    # 服务端拿不到"到底是哪种"，所以把判据交给老人自己的眼睛。
    return {"ok": True, "confirmed": True,
            "message": (
                "已记下。请用一句话告诉老人，只说这一遍、不要解释原因、"
                "不要多说别的："
                "「记好了。要是屏幕上那条提醒还亮着，就长按按钮 3 秒钟，"
                "等按钮闪蓝色三次再松手。」"
            )}


def _confirm_device_pending(db: Any, device_pk: int,
                            device_call: Any) -> dict[str, Any]:
    """改造前的行为：不带 ``server_id``，让设备确认**它自己挂着的那条**。

    现在只剩一个用途：没有待确认的**服药**提醒时，把喝水 / 日程 / 运动这类
    提醒交还给设备处理 —— 它们没有"哪一味药"的问题，按药名逐味确认那一套
    套不上去。固件 a85f21f 起 ``server_id`` 缺省（=0）就是"确认当前正在响铃
    的那条"，与本函数逐字等价，所以这段老代码继续有效。

    **2026-11-03 起也吃「按键发起」标记**（用户明确要求）：老人长按了确认键，
    但服务端没找到服药候选（规则被停用/已删、时间窗对不上）时会退到这里；
    固件认标记、回执报 BUTTON，服务端这边则要先**预写 VOICE**，两者凑齐才
    合得成 ``VOICE_AND_BUTTON(5)``。见 ``_arm_voice`` 的说明。

    失败语义（LLM 拿到的话术不同）：
      - 设备明确答 false → 没有待确认的提醒，正常回应，**不要说失败**；
      - 超时 / 通道不可用 → 让老人改按设备上的按键。
    """
    ev = None
    prev: int | None = None
    if db is not None:
        try:
            ev = _sole_confirmable_event(db, device_pk)
            if ev is not None:
                prev = _arm_voice(db, ev)
                if prev is not None:
                    log.info("confirm_reminder：旧路径预写 VOICE（ev=%s，原值=%s）",
                             getattr(ev, "id", "?"), prev)
        except Exception as exc:  # noqa: BLE001  预写失败不该让确认本身失败
            log.warning("confirm_reminder：预写确认方式失败 %s: %s",
                        type(exc).__name__, exc)
            ev, prev = None, None

    out = _device_pending_call(device_call)

    # 设备侧"失败"要**回查一次库**再下结论。
    #
    # 为什么不能信 ``ok=False``：设备处理 ``elder.confirm_reminder`` 是同步
    # 阻塞的 —— elder_bridge.cc:1576 → reminder.c:1484 → sync.c:943 里
    # ``if (online) flush_queue();`` → net.c:206 ``esp_http_client_perform()``
    # （keep_alive 关着，每次重做 TLS 握手，实测 3.5 秒）。也就是说"它到底
    # 办没办成"取决于它自己那条 POST，而**那条 POST 会自己把库里的 state
    # 改成 CONFIRMED**。所以"超时"完全可能是"它已经办成了、只是回包慢"。
    # 09:15:13 那一次就是这样：设备 09:15:12 已经确认，服务端 09:15:16 判超时，
    # 然后对老人说「请按一下按键」—— 他 4 秒前刚按完。
    if not out.get("confirmed") and not out.get("ok") and db is not None:
        recent = _recently_confirmed_medicine(db, device_pk)
        if recent is not None:
            ev0, rule0, mins = recent
            log.info("confirm_reminder：设备没回包，但 %d 分钟前刚确认过「%s」"
                     "（ev=%s）⇒ 按成功处理，不再让老人重按",
                     mins, getattr(rule0, "title", ""), getattr(ev0, "id", "?"))
            out = _already_confirmed_reply(ev0, rule0, mins)

    # 只有设备**明确说确认了**才留着那个 VOICE；其余一切结局（答 false、
    # 超时、报错、看不懂）都说明这次对话没有落地任何确认，必须恢复原值 ——
    # 否则家属端会看到一条老人根本没做过的"语音确认"。
    if not out.get("confirmed"):
        _disarm_voice(db, ev, prev)

    return out



def _confirm_reminder(db: Any, device_pk: int, device_call: Any,
                      args: dict[str, Any] | None = None) -> dict[str, Any]:
    """按「老人说的那一味药」确认一条**服药提醒**。

    与 2026-10 那版的分工不同（固件 a85f21f 之后）：长按服药确认键只负责
    **进对话、把话说清**（「您吃的是哪种药？」），"哪条日程被确认了"由服务端
    结合 ``persona.med_categories.match_medicine`` 判断。所以这里**先写库、
    再通知设备**：记录是老人确实吃了的事实，设备只是显示 —— 通知失败也绝不
    回滚数据库（那会让家属永远看不到这次服药）。

    三档结果（都算 ``ok``，只是话术不同）：
      * 确认：整条吃完 → ``state=CONFIRMED``；只吃了一部分 → 仍是 FIRED，
        由 ``scheduler`` 的超时升级把进度写进 note 给家属看；
      * 追问：``ambiguous``（同一类别有多味，绝不猜）/ ``matched=false``
        （一个都没对上）/ ``need_detail``（多条待确认、或多味药却没点名）；
      * 没有待确认的服药提醒：退回 ``_confirm_device_pending``（喝水/日程提醒
        走那条路，行为与改造前逐字一致）。
    """
    said = str((args or {}).get("medicine") or "").strip()[:32]

    # 没有 db 会话就查不出候选（只有单测会传 None，生产路径永远有 session）。
    # 退回改造前的行为：让设备确认它自己挂着的那条。**不要**让这条分支去猜药名
    # —— 没有库就没有药名清单，任何"匹配"都是瞎编。
    if db is None:
        log.warning("confirm_reminder：没有 db 会话，退回设备侧整条确认")
        return _confirm_device_pending(db, device_pk, device_call)

    try:
        cands = pending_medicine_events(db, device_pk)
    except Exception as exc:  # noqa: BLE001  查库失败不该崩掉整轮对话
        log.warning("confirm_reminder：查待确认提醒失败 %s: %s",
                    type(exc).__name__, exc)
        return {"ok": False, "message": "记录没查出来，请老人稍后再说一次"}

    if not cands:
        # 先问一句"是不是刚刚已经确认过了"。这一条是 2026-11-04 用户实报
        # 「服药提醒按键按了又是不能确认」的真正来源：大模型首字卡了 65 秒，
        # 这期间老人自己按了键，等他听到回应时提醒已经 CONFIRMED，清单当然是
        # 空的 —— 此时再说「请让老人按一下按键」就是让他对着已经办完的事再按一遍。
        recent = _recently_confirmed_medicine(db, device_pk)
        if recent is not None:
            ev0, rule0, mins = recent
            log.info("confirm_reminder：清单为空，但 %d 分钟前刚确认过「%s」"
                     "（ev=%s，方式=%s）⇒ 直接回「已经记上了」，不再调设备",
                     mins, getattr(rule0, "title", ""), getattr(ev0, "id", "?"),
                     _confirm_method_word(getattr(ev0, "confirm_method", 0)))
            return _already_confirmed_reply(ev0, rule0, mins)

        # 没有待确认的**服药**提醒 ⇒ 交还给设备（喝水/日程/运动提醒走这里，
        # 它们没有"哪一味药"的问题）。见 ``_confirm_device_pending`` 的注释。
        log.info("confirm_reminder：没有待确认的服药提醒（device_pk=%s），走设备旧路径",
                 device_pk)
        return _confirm_device_pending(db, device_pk, device_call)

    # 药名清单算一次就够：下面既要拿它做匹配，又要拿它判「是不是全确认了」。
    items = [(ev, rule, medicine_names(rule)) for ev, rule in cands]
    all_names: list[str] = []
    for _, _, ns in items:
        for n in ns:
            if n not in all_names:
                all_names.append(n)

    target: tuple[models.ReminderEvent, models.Reminder, list[str]] | None = None
    hits: list[str] = []
    # 这条记录是不是"整条确认"来的（没逐味点名）。只影响留痕，不影响 state。
    whole_event_confirm = False
    # 这一轮是不是"整条确认"（老人没点名某一味，只说「确认 / 都吃了」）。
    # 判据只看措辞，长度上限见 ``is_whole_event_yes``。
    whole_yes = is_whole_event_yes(said)
    if whole_yes:
        log.info("confirm_reminder：%r 判为整条确认（不点名某一味）", said)

    # ⚠️ ``and all_names``：一条药名都没登记（老数据只有 title）时没有可匹配
    # 的对象，硬走匹配表会永远返回 matched=false，把老人卡在「问不出药名」的
    # 死循环里。此时按"没有逐味信息"处理，落到下面的整条确认分支。
    #
    # ⚠️ ``not whole_yes``：不加这一条的后果是**死循环**（2026-11-04 用户实报
    # 「老子喊了半天确认，他听到啥了？」的最后一环）：老人答「确认」，
    # ``match_medicine("确认", ["降压药","降脂药"])`` 返回空 ⇒ 走 matched=False
    # ⇒ 让模型"复述药名再问一次" ⇒ 老人再说「确认」…… 每轮都问、永远不落库，
    # 屏幕和灯就一直挂着。而「确认」根本不是在点名药，它是在回答
    # 「您刚才已经服用过降压药了吗？」这个是非问 —— 它不该进药名匹配表。
    if said and not whole_yes and all_names:
        # 老人点了名（药名或类别词）—— 走匹配表，**不在这里猜**。
        hits = match_medicine(said, all_names)
        if len(hits) > 1:
            # 一个类别词对上多味药（「降压药」→ 氨氯地平 + 缬沙坦）：
            # 宁可多问一句，也不能认错药。
            log.info("confirm_reminder：%r 命中多味 %s，转追问", said, hits)
            return {"ok": True, "confirmed": False, "ambiguous": hits,
                    "message": (
                        f"老人说的「{said}」在待确认的药里对应不止一味："
                        f"{'、'.join(hits)}。请把候选药名一个一个念给他听，"
                        "让他挑一个，再带他挑的那个词重新调用本工具；不要替他选。"
                    )}
        if hits:
            target = next((it for it in items if hits[0] in it[2]), None)
        if target is None:
            log.info("confirm_reminder：%r 没对上任何一味（候选 %s）", said, all_names)
            cand_text = "、".join(all_names) or "（还没登记药名）"
            return {"ok": True, "confirmed": False, "matched": False,
                    "candidates": all_names,
                    "message": (
                        f"老人说的「{said}」对不上任何一条待确认的药"
                        f"（待确认的有：{cand_text}）。请复述这些药名再问一次是哪一味；"
                        "最多再问两轮，问不出来就如实说「我先记着，您方便时看一眼药盒」，"
                        "不要无限追问。"
                    )}
    elif len(items) == 1:
        # ---- 只有一条待确认 ⇒ **整条确认**，不再追问是哪一味 ----
        #
        # ⚠️ 2026-11-04 改口径（原逻辑：「这条提醒挂着 N 味药，老人没说清是哪
        # 一味 ⇒ need_detail，让模型问『您吃的是A还是B』」）。改的直接原因是
        # 用户第三次为同一件事发火，而现场证据说明"追问"这条路根本走不通：
        #
        #   ① 设备长按 3 秒后问的是「您吃的是哪种药？」，老人答「确认」——
        #      这不是在点名药，是在回答一个是非问。追问纪律把它当成"没点名"
        #      再问一遍，老人再说「确认」，每轮都问、一次都不落库，屏幕和 #1
        #      呼吸灯就一直挂着（用户原话：「一个提醒确认到现在还显示在屏幕上，
        #      事件指示灯还一直亮着」）。
        #   ② 固件自己在"开不了麦 / 播不了片段"时**已经**这么干了：见
        #      ``elder_bridge.cc`` 的 ``!CanStartDialog()`` 分支 ——
        #      ``ConfirmMedicine(idx, ELDER_CFM_BUTTON)`` 不管几味药，
        #      注释写的取舍是「信息缺失不能让老人确认不了，'按了就记上'比
        #      '按了没反应'安全得多」。服务端比设备更严 = 按键的可靠性取决于
        #      网络和设备状态，而用户的要求是「按键一定能确认」。
        #   ③ 候选**恰好 1 条**：不存在"确认错哪条提醒"的风险，剩下的只是
        #      "哪几味"的粒度问题。
        #
        # 代价说清楚（不要装作没有）：老人只吃了两味里的一味、却只说「吃了」，
        # 会把两味都记成已服。所以这里**必须留痕**：``_mark_whole_event_confirm``
        # 往 ``note`` 写「整条确认（未逐味点名）：降压药、降脂药」，家属端能看见
        # 这条记录不是逐味确认来的。宁可留一条粒度粗但真实的记录，也不要为了
        # 粒度把记录弄丢 —— 没有记录 = 家属以为老人没吃。
        target = items[0]
        if len(items[0][2]) > 1:
            whole_event_confirm = True
            log.info("confirm_reminder：唯一候选挂 %d 味药（%s），老人未点名"
                     "（said=%r，整条确认措辞=%s）⇒ 整条确认并留痕",
                     len(items[0][2]), "、".join(items[0][2]), said, whole_yes)
    else:
        # 多条待确认 + 没点名：不许让「吃了」三个字把几条提醒一起确认掉。
        brief = "；".join(
            f"「{sanitize_for_prompt(str(rule.title or ''), limit=40)}」"
            f"({len(ns) or 1}味)" for _, rule, ns in items)
        log.info("confirm_reminder：只说「吃了」但有 %d 条待确认，转追问", len(items))
        return {"ok": True, "confirmed": False, "need_detail": True,
                "candidates": all_names,
                "message": (
                    f"现在有 {len(items)} 条待确认的服药提醒：{brief}。"
                    "老人只说「吃了」、没说清是哪一种，请先问清是哪一味再调用本工具，"
                    "不许把几条一起确认。"
                )}

    ev, rule, names = target
    recorded = list(ev.confirmed_medicines or [])
    add = [hits[0]] if hits else list(names)
    for n in add:
        if n not in recorded:
            recorded.append(n)
    # ⚠️ 必须**整体重新赋值**：``confirmed_medicines`` 是普通 JSON 列，没有
    # 可变性追踪，就地 append 之后脏检查认为没变化 ⇒ 不写库（详见
    # ``routers/device_api.report_event`` 里同一条注释）。
    ev.confirmed_medicines = recorded
    # 整条确认（没逐味点名）必须留痕 —— 否则它在库里与"逐味点过名"完全一样。
    # 放在 commit 之前（下面 ``db.commit()``），放后面就丢。
    if whole_event_confirm:
        _mark_whole_event_confirm(ev, names)

    # 「全确认才算 CONFIRMED」—— 依从率、漏服告警全部只看 state，部分确认
    # 提前置 CONFIRMED 等于把"只吃了 2/4 味"记成"全吃了"，是医疗事故级的错。
    # names 为空（老数据没登记药名）时没有逐味信息，整条确认即可。
    complete = (not names) or all(n in recorded for n in names)
    now = local_now()
    if complete:
        ev.state = int(models.EventState.CONFIRMED)
        ev.confirmed_at = now
        # ---------------------------------------------------------------
        # 下面这一行是「按键发起 + 对话确认 = 全置信(5)」能算出来的**唯一前提**。
        #
        # ⚠️ 顺序依赖（动它之前务必读完）：
        #   ① 必须在下面 ``db.commit()`` **之前**赋值；
        #   ② 必须在 ``device_call("self.elder.confirm_reminder", ...)``
        #      **之前**赋值（commit 在前、通知设备在其后，见本函数尾部）。
        # 设备回执到达时 ``routers/device_api.report_event`` 拿**库里的值**判断
        # 是否合并：``payload.method == BUTTON and ev.method == VOICE``
        # ⇒ 落 ``VOICE_AND_BUTTON(5)``。赋值晚一步（挪到 commit 之后、或挪到
        # device_call 之后），设备那条 CONFIRMED+BUTTON 回执就先到、读到的还是
        # NONE/BUTTON ⇒ 合并永不触发，全置信永远算不出来。而且它**不报错、
        # 不告警**，只是家属端一直显示「按键确认」。
        #
        # 为什么是**无条件覆盖**（即使库里已经是 BUTTON）：新流程里老人先按键
        # 发起，固件在"按键发起"那次回执里报的就是 BUTTON(2)，库里因此可能已经
        # 是 BUTTON。这里必须改回 VOICE，否则上面的合并条件不成立。按用户
        # 2026-11-03 的定义，那次按键只负责"发起"，"吃的是哪味药"是在对话里
        # 说清的 ⇒ 属于「按键 + 语音」，不是「纯按键」。
        #
        # ⚠️ 第二个前提：``ev`` 必须是设备回执会命中的**同一条实例**。
        # report_event 按 ``(reminder_id, due_at)`` 定位，定位不到就补建一条新
        # 实例（device_api.py:568-577），补建那条的 method 是 NONE ⇒ 合并同样
        # 不会发生，全置信丢失且静默。所以 ``ev.due_at`` 必须与设备上报的
        # due_at 逐字相等（前提：设备时区 == 规则时区，见 report_event 的
        # docstring）。时区不一致属于固件侧待补（设备上报自己的时区），这里
        # 不做兜底 —— 兜底就得猜，猜错的代价是把 A 的确认记到 B 头上。
        #
        # ---- 纯对话确认（VOICE=1）的口径，别再"顺手升级" ----
        # 老人没按过任何键、只跟小智说「我吃过了 / 降压药吃了」时，落到这里就是
        # **最终结果** VOICE：固件走 ``force_done`` 直接落 DONE（不再进半置信的
        # HALF_CONFIRMED，也不需要老人再按一次键），回执 method=VOICE，库里是 1。
        # 依从率统计里 VOICE 与 VOICE_AND_BUTTON **都算已确认**（home.py 的
        # ``confirmed_count`` 只数 ``state == CONFIRMED``，不看 method），两者
        # 的区别只在审计留痕与家属端的展示口径。
        # 将来若要按置信度加权算依从率，入口就是这一行 —— 但**现在不要改统计
        # 口径**：存量数据会被整体重算，历史依从率一夜之间变化，家属会以为老人
        # "突然不听话了"。
        # ---------------------------------------------------------------
        ev.confirm_method = int(models.ConfirmMethod.VOICE)
    elif ev.state == int(models.EventState.PENDING):
        # 已到点却还是 PENDING：按"响过了"记。否则它留在 PENDING 里，
        # 超时升级照样能扫到，但设备侧看到的仍是"还没响"，两边对不上。
        ev.state = int(models.EventState.FIRED)

    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        log.warning("confirm_reminder：写库失败 %s: %s", type(exc).__name__, exc)
        return {"ok": False, "message": "记录没写进去，请老人稍后再说一次"}

    # ---- 通知设备：``server_id`` 是 ``reminders.id``（不是实例 id）----
    # 设备侧按这个 id 找到本地那条提醒并收屏 / 停铃。通知失败**不回滚**上面的
    # 记录，只在话术里带一句，让老人知道屏幕可能还亮着。
    #
    # ⚠️ **只在 ``complete`` 时才通知设备**（这条提醒挂的药全确认完）。
    # 设备侧收到 server_id 会直落 DONE（``elder_reminder_confirm_server`` 的
    # ``force_done`` 分支），连带收屏、停闪灯、清 ``pending_today``。一条挂
    # 2 味药时若在确认第 1 味就通知，屏幕会提前收起来，而这边还在追问第 2 味；
    # 第 2 味确认回来时设备已不是"待确认"，会答 false，话术里就会出现
    # 「设备说没办成…屏幕可能还亮着」这种自相矛盾的话。
    # 半置信时代这个问题是自愈的（后续确认还能把 HALF 推进 DONE），改成直落
    # DONE 之后不自愈，所以必须在这里挡住。
    dev_err = ""
    if complete:
        if device_call is None:
            dev_err = "现在连不上设备"
        else:
            try:
                payload = device_call("self.elder.confirm_reminder",
                                      {"server_id": ev.reminder_id},
                                      _DEVICE_CALL_TIMEOUT_SEC)
                if not isinstance(payload, dict):
                    dev_err = "设备返回的内容看不懂"
                elif payload.get("error"):
                    err = payload["error"]
                    dev_err = "设备说没办成"
                    log.warning("confirm_reminder：设备报错 %s",
                                err.get("message") if isinstance(err, dict) else err)
                else:
                    result = payload.get("result")
                    if isinstance(result, dict) and result.get("isError"):
                        dev_err = "设备说没办成"
            except TimeoutError:
                dev_err = f"设备 {int(_DEVICE_CALL_TIMEOUT_SEC)} 秒内没回包"
                log.warning("confirm_reminder：设备 %d 秒内没回包",
                            int(_DEVICE_CALL_TIMEOUT_SEC))
            except Exception as exc:  # noqa: BLE001  设备离线 / WS 已断
                dev_err = "现在连不上设备"
                log.warning("confirm_reminder：下发失败 %s: %s", type(exc).__name__, exc)

    dev_note = (f"（{dev_err}，但记录已经写进去了 —— 可以顺口说一句"
                "「记录好了，屏幕可能还亮着」。）" if dev_err else "")

    if complete:
        log.info("confirm_reminder：已确认（device_pk=%s reminder=%s med=%s）",
                 device_pk, ev.reminder_id, add or "-")
        said_part = f"「{add[0]}」" if add else "这次服药"
        return {"ok": True, "confirmed": True, "complete": True,
                "medicine": add[0] if add else "",
                "message": (
                    f"已记下{said_part}。用一句话告诉老人「记下了」即可；"
                    "**不许报剂量、不许建议加量减量或停药**。" + dev_note
                )}

    remaining = [n for n in names if n not in recorded]
    log.info("confirm_reminder：部分确认（reminder=%s 已确认 %s，还差 %s）",
             ev.reminder_id, recorded, remaining)
    return {"ok": True, "confirmed": True, "complete": False,
            "medicine": add[0] if add else "",
            "remaining": remaining,
            "message": (
                f"已记下「{add[0]}」，这条提醒还有 {'、'.join(remaining)} 没确认。"
                f"请马上主动追问老人「{'、'.join(remaining)}吃了吗」，"
                "得到确切答案后再调用本工具。" + dev_note
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


def _get_wifi_info(device_call: Any) -> dict[str, Any]:
    """查设备连的是哪个 WiFi。

    数据源是 ``self.get_device_status`` 的 ``network`` 段，**不新增固件工具** ——
    设备侧能拿到的信息就这些，再开一个 MCP 工具只是把同一份数据换个入口，
    却要全量刷机才能用。

    ``network`` 段有两种形状（见 :data:`_SIGNAL_WORDS` 的说明）：
    WiFi 板 ``{type:"wifi", ssid, signal:"strong|medium|weak"}``，
    4G 板 ``{type:"cellular", carrier, csq}``。**不要照着 wifi_board.cc
    顶上的注释块写**——那块注释写的是 ``"rssi": -60``，与代码不符。

    ⚠️ 信号在这里就地翻译成口语，**绝不把数字/英文交给模型**：
    ``_get_device_status`` 的注释已经踩过这个坑（"模型很可能把 rssi -60
    直接念出来"）。老人听到「信号负六十分贝」只会以为设备坏了。
    """
    if device_call is None:
        return {"ok": False, "message": "现在连不上设备，查不到网络"}

    status = _device_status(device_call)
    if status is None:
        return {"ok": False, "message": "设备没有回应，查不到网络"}

    net = _dig(status, ("network",))
    if not isinstance(net, dict):
        return {"ok": False, "message": "设备没上报网络信息"}

    kind = str(net.get("type") or "").strip().lower()
    ssid = str(net.get("ssid") or "").strip()

    if kind and kind != "wifi":
        # 4G / 有线：如实说，不要硬套"WiFi 名字"。
        # 类型与运营商都翻成中文：`cellular` / `CHINA MOBILE` 交给模型，
        # 它会直接念英文 —— 老人听不懂，还以为设备出了毛病。
        human = {"cellular": "移动网络（4G）", "4g": "移动网络（4G）",
                 "ethernet": "网线"}.get(kind, kind)
        carrier = _carrier_cn(net.get("carrier"))
        msg = f"这台设备不是用 WiFi 联网的，用的是{human}"
        if carrier:
            msg += f"，运营商是{carrier}"
        # ⚠️ 不把 ``kind``（``cellular`` / ``ethernet``）放进返回值：
        # 整个 dict 都会被喂给模型，它看到英文就会念英文。这里只留
        # 已经翻好的中文，模型没有别的可念。
        out: dict[str, Any] = {"ok": True}
        if carrier:
            out["carrier"] = carrier
        return {**out, "message": msg + "。如实告诉老人即可，不要提 WiFi 名字"}
    if not ssid:
        return {"ok": True, "wifi_name": "",
                "message": "设备连着 WiFi 但没报出名字（可能是隐藏网络）。"
                           "如实说「连着一个没名字的网」即可，不要猜名字"}

    out: dict[str, Any] = {"ok": True, "wifi_name": ssid}
    sig = _wifi_signal_word(net)
    if sig:
        # 只给三个口语词，**不给数字** —— 见上面的说明。
        out["signal"] = sig
    return {**out,
            "message": "用一句话回答老人（如「连着家里的 WiFi，信号挺好的」），"
                       "不要念信号数字、不要说英文、不要说 rssi"}


#: 固件 ``WifiBoard::GetDeviceStatusJson()`` 实际发的是**字符串** ``signal``
#: （strong / medium / weak），不是数字 rssi。
#:
#: ⚠️ 这里踩过一次：``wifi_board.cc`` 的**注释块**写的是 ``"rssi": -60``，
#: 而代码实际 ``cJSON_AddStringToObject(network, "signal", ...)``。照着注释
#: 写实现 ⇒ 生产上永远拿不到信号，老人问「网好不好」只能答"不知道"。
#: 所以两种形状都要认：**有 signal 就信固件那个词**（单一真源，门限由固件定），
#: 没有才拿 rssi 自己算。
_SIGNAL_WORDS = {
    "strong": "很好", "good": "很好", "excellent": "很好", "很好": "很好", "强": "很好",
    "medium": "一般", "fair": "一般", "normal": "一般", "一般": "一般", "中": "一般",
    "weak": "较弱", "poor": "较弱", "bad": "较弱", "较弱": "较弱", "弱": "较弱",
}


def _wifi_signal_word(net: dict[str, Any]) -> str:
    """把网络段里的信号强弱翻成一个口语词；取不到返回空串。"""
    raw = net.get("signal")
    if isinstance(raw, str) and raw.strip():
        word = _SIGNAL_WORDS.get(raw.strip().lower())
        if word:
            return word
    rssi = _coerce_int(net.get("rssi"))
    if rssi is None:
        return ""
    # 门限与固件保持一致（>= -60 strong / >= -70 medium / 其余 weak），
    # 否则同一台设备在「有 signal」和「只有 rssi」两条路上会给出不同的词。
    return "很好" if rssi >= -60 else "一般" if rssi >= -70 else "较弱"


#: 运营商英文名 → 中文。ML307 上报的是 ASCII（``CHINA MOBILE``），
#: 直接交给模型它会念英文。**认不出来的就不给** —— 宁可不说运营商，
#: 也不能让模型念一串老人听不懂的字母。
_CARRIER_CN = {
    "china mobile": "中国移动", "cmcc": "中国移动", "中国移动": "中国移动",
    "china unicom": "中国联通", "cucc": "中国联通", "中国联通": "中国联通",
    "china telecom": "中国电信", "ctcc": "中国电信", "中国电信": "中国电信",
    "china broadnet": "中国广电", "中国广电": "中国广电",
}


def _carrier_cn(raw: Any) -> str:
    """运营商名翻成中文；认不出来（或本身就是一串英文）返回空串。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    hit = _CARRIER_CN.get(text.lower())
    if hit:
        return hit
    # 未知名字：只有确实是中文才放行，英文一律不给（见上面的说明）。
    return text if any("\u4e00" <= ch <= "\u9fff" for ch in text) else ""


def _get_headlines(args: dict[str, Any]) -> dict[str, Any]:
    """播报今日头条。

    真正的抓取在 ``app/news/``（可选子系统，未配置时优雅降级）。
    ``import`` 放函数内，理由同 ``_get_weather``：新闻模块要拉 urllib/XML，
    让它拖垮整个 tools 模块的 import 不值得 —— 那会连带提醒、确认服药一起瘫痪。
    """
    try:
        from ..news import get_headlines, speak_lines
    except Exception as exc:  # noqa: BLE001  可选子系统：导入失败就当没这功能
        log.warning("新闻模块不可用：%s: %s", type(exc).__name__, exc)
        return {"ok": False, "message": "今天没取到新闻，回头再试试"}

    count = _coerce_int(args.get("count"))
    count = 3 if count is None else max(1, min(5, count))

    items, reason = get_headlines(limit=count)
    lines = speak_lines(items)
    if not lines:
        return {"ok": False, "message": "今天没取到新闻，回头再试试"}
    return {"ok": True, "headlines": lines,
            "message": "把这些标题念给老人听：只念标题，不要补充背景、"
                       "不要评论、不要发表看法。每条之间停一下。"
                       "念完问他还要不要听别的"}


def _set_status_light(args: dict[str, Any], device_call: Any) -> dict[str, Any]:
    """开关设备的状态灯。

    ⚠️ 只关**状态灯**（待机/聆听/说话/表情）。提醒闪灯、服药未确认、
    求助告警、按键指示灯由固件按**优先级**放行，服务端无法关掉它们 ——
    这是刻意的：家属说「晚上别亮灯」时，最不该被一起关掉的恰恰是
    「该吃药了」那盏灯。固件侧判据见 ``rgb.c`` 的 ``ELDER_RGB_PRIO_REMINDER``。

    ``on`` 不传 = 只查状态。用 ``in args`` 判而不是 ``args.get(...) is None``：
    模型可能显式传 ``null``，那与"没传"同义，两种都当查询。
    """
    want: bool | None = None
    if "on" in args and args.get("on") is not None:
        raw = args.get("on")
        if isinstance(raw, bool):
            want = raw
        else:
            text = str(raw).strip().lower()
            if text in ("true", "1", "on", "yes", "开"):
                want = True
            elif text in ("false", "0", "off", "no", "关"):
                want = False

    if device_call is None:
        return {"ok": False, "message": "现在连不上设备，灯没法调"}

    # 查状态与改状态走**两个**固件工具。为什么不合成一个带可选参数的：
    # MCP 的"可选"是用默认值表达的，"不传"和"传 false"就分不开了 ——
    # 老人问「灯关了吗」被当成"关闭"，是最坏的一种误操作。
    if want is None:
        try:
            payload = device_call("self.elder.get_status_light", {},
                                  _DEVICE_OP_TIMEOUT)
        except TimeoutError:
            return {"ok": False, "message": "设备没有响应，查不到灯的状态"}
        except Exception as exc:  # noqa: BLE001
            log.warning("get_status_light 下发失败：%s: %s", type(exc).__name__, exc)
            return {"ok": False, "message": "现在连不上设备，查不到灯的状态"}
        ok, text = _device_reply_text(payload)
        current = _light_state_of(text) if ok else None
        if current is None:
            return {"ok": False, "message": "设备没报出灯的状态"}
        return {"ok": True, "on": current,
                "message": ("现在状态灯是**开着**的" if current
                            else "现在状态灯是**关着**的")
                           + "。用一句话告诉老人即可（提醒灯一直不受影响，不用特意说）"}

    try:
        payload = device_call("self.elder.set_status_light", {"on": want},
                              _DEVICE_OP_TIMEOUT)
    except TimeoutError:
        return {"ok": False, "message": "设备没有响应，灯没调成"}
    except Exception as exc:  # noqa: BLE001  设备离线/WS 已断
        log.warning("set_status_light 下发失败：%s: %s", type(exc).__name__, exc)
        return {"ok": False, "message": "现在连不上设备，灯没法调"}

    ok, text = _device_reply_text(payload)
    if not ok:
        return {"ok": False, "message": "设备说没办成，灯没调成"}

    current = _light_state_of(text)
    if current is None:
        current = want      # 固件没回状态就回显意图，别让这轮变成"没办成"

    if current:
        return {"ok": True, "on": True,
                "message": "状态灯已经打开。用一句话回应老人"}
    return {"ok": True, "on": False,
            "message": "状态灯已经关了。**必须补一句**「提醒、吃药的灯还是照常亮」，"
                       "不要说成「灯全关了」—— 老人会以为该吃药时也不亮"}


def _light_state_of(text: str) -> bool | None:
    """从固件回包 ``{"on":true}`` 里取灯状态；取不到返回 None。"""
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001  固件版本差异：解析不了当没回
        return None
    if isinstance(data, dict) and isinstance(data.get("on"), bool):
        return data["on"]
    return None


def _create_reminder(db: Any, device_pk: int, args: dict[str, Any]) -> dict[str, Any]:
    title = str(args.get("title") or "").strip()
    when = str(args.get("when") or "").strip()
    repeat = str(args.get("repeat") or "").strip()
    if not title:
        return {"ok": False, "need_clarify": True,
                "message": "没听清要提醒什么，请反问老人提醒的内容"}
    # ---- 占位标题闸门（2026-10-29 真机事故后加）----
    # 模型听不清内容时会把 title 填成「提醒」「事项」这种分类词凑数。落库后
    # 到点响的是一条没有内容的提醒，老人只能按确认消掉 —— 他会以为事情
    # 已经安排好了。**宁可多问一句，不许存一条空壳提醒。**
    if _is_placeholder_title(title):
        log.warning("拒绝创建占位标题的提醒：title=%r when=%r", title, when)
        return {"ok": False, "need_clarify": True,
                "message": "老人没说清要提醒什么（title 是「提醒」这类空词）。"
                           "请反问一句「提醒您做什么？」，把老人说的**那件事**"
                           "填进 title，不要填分类词"}
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
        # 剂量走**老字段** ``dose``（一条提醒一味药），不塞 medicines：
        # 老人一句「提醒我八点吃降压药一片」只有一味药，写 medicines 反而
        # 让"老字段 vs 新字段"出现两份真源。出参由 schemas.ReminderOut 派生，
        # 网页 / APP / 设备看到的 medicines 与这里逐字一致。
        # 截到 64：与 schemas.MedicineItem.dose 的上限对齐，避免一条超长的
        # 模型输出让派生出来的 MedicineItem 校验失败（那是读路径，会 500）。
        dose=str(args.get("dose") or "").strip()[:64],
        require_confirm=True,
        enabled=True,
    )
    db.add(r)
    # flush 拿 id，然后与业务变更**同一个事务**写审计（见 reminder_audit 模块）。
    db.flush()
    reminder_audit.log(db, reminder=r, action="create", actor_type="voice",
                       actor_name=_VOICE_ACTOR, via="voice",
                       after=reminder_audit.snapshot(r))
    db.commit()
    db.refresh(r)
    # 立即排期，保证设备下次心跳就能拉到（与 routers/reminders.create 同口径）
    materialize_events(db, device_pk)
    nxt = next_occurrence(r)
    # 结构化时间供**服务端复述对账**用（session._ReplySpeaker 拿它比模型嘴里
    # 说的钟点）。不给结构化字段的话，复述错了没有任何办法发现 —— 事故里
    # summary 本身就写着错的 12:55，从文本里根本看不出问题。
    hour = minute = None
    if parsed.times_of_day:
        try:
            hh, mm = parsed.times_of_day[0].split(":")
            hour, minute = int(hh), int(mm)
        except (ValueError, TypeError):
            hour = minute = None
    return {
        "ok": True,
        "id": int(r.id),
        "title": r.title,
        "summary": f"「{r.title}」，{parsed.speak}",
        "hour": hour,
        "minute": minute,
        "clock": parsed.speak,
        "next_fire_at": nxt.isoformat() if nxt else None,
        "hint": "请用一句话跟老人复述确认。时间**照抄 clock 里的说法**，"
                "不要自己换算成几点几分；内容照抄 title，不要换成别的词",
    }


def _list_reminders(db: Any, device_pk: int) -> dict[str, Any]:
    # 软删除（2026-10-30）：列给老人听的只能是"还活着"的提醒，
    # 否则他会听到一条已经删掉的服药时间并按它吃药。
    rows = list(db.query(models.Reminder)
                .filter(models.Reminder.device_id == device_pk,
                        models.Reminder.deleted_at.is_(None))
                .order_by(models.Reminder.id.desc()).limit(20).all())
    if not rows:
        return {"ok": True, "count": 0, "reminders": [],
                "message": "老人当前没有任何提醒，如实告知"}
    # 每条都带上 dose / rtype / medicines：以前只给 {id,title,enabled,next_fire_at}，
    # 模型**完全看不到**剂量和类别 —— 老人问「我早上吃几片药」，它只能瞎猜或者
    # 说"不知道"，而这恰恰是服药提醒里最要命的那部分信息。
    return {"ok": True, "count": len(rows),
            "reminders": [{"id": int(r.id), "title": r.title,
                           "enabled": bool(r.enabled),
                           "rtype": int(r.rtype),
                           "rtype_label": _RTYPE_LABELS.get(int(r.rtype), ""),
                           "dose": r.dose or "",
                           "medicines": _medicines_of(r),
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
            # 软删除（2026-10-30）：用带 where 的 select 而不是 db.get()。
            # db.get() 拿不到 deleted_at 条件，会把已删的提醒当成活的，
            # 于是「删掉八点的药」可能删到一条早就删过的行。
            r = db.scalar(select(models.Reminder).where(
                models.Reminder.id == int(rid),
                models.Reminder.deleted_at.is_(None)))
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
    # 软删除（2026-10-30）：与 web/app 的 DELETE 同口径 —— 行留在库里（家属
    # 要能看到"是老人自己删的"），对外表现为不存在。
    before = reminder_audit.snapshot(r)
    r.deleted_at = models.utcnow()
    r.deleted_by = None          # 老人没有账号，删除人是"语音"，由流水记录
    r.deleted_via = "voice"
    # 与 web/app 的 DELETE 同口径：enabled 一并置 false 是**第二道防线**
    # （materialize_events 等查询按 enabled 过滤，与 deleted_at 互为备份），
    # **不是**老固件兜底 —— 下发 payload 本来就看 enabled，置 false 与过滤
    # deleted_at 对设备是同一个效果；老固件在"数组为空"时仍会保留本地旧表。
    # 完整说明见 routers/reminders.delete_reminder。
    r.enabled = False
    reminder_audit.log(db, reminder=r, action="delete", actor_type="voice",
                       actor_name=_VOICE_ACTOR, via="voice", before=before)
    # 必须与软删同一个事务：否则会留下"已删但 PENDING 还在"的状态，
    # 设备按实例继续播报 ⇒ 删了还在响。
    clear_pending_events(db, r.id)
    db.commit()
    return {"ok": True, "deleted": title,
            "message": f"已删除「{title}」，请用一句话告知老人"}


def _toggle_reminder(db: Any, device_pk: int, args: dict[str, Any]) -> dict[str, Any]:
    r, err = _resolve_one(db, device_pk, args, "暂停或恢复")
    if err:
        return err
    want = args.get("enabled")
    was_enabled = bool(r.enabled)
    r.enabled = (bool(want) if isinstance(want, bool) else not r.enabled)
    reminder_audit.log(
        db, reminder=r,
        action="toggle_on" if r.enabled else "toggle_off",
        actor_type="voice", actor_name=_VOICE_ACTOR, via="voice",
        before={"enabled": was_enabled}, after={"enabled": bool(r.enabled)},
    )
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
