"""一次 WebSocket 连接 = 一个 :class:`VoiceSession`（会话编排）。

设计要点
--------
1. **一个连接一个会话对象**，状态机 idle→listening→thinking→speaking→idle，
   ``abort`` 可从任意态打断（固件按唤醒词随时发它）。
2. **阻塞型调用一律包线程池**：ASR / LLM / TTS / 全部 DB 操作都是同步
   blocking 的，直接 await 会卡住事件循环 —— 卡住的表现是"这一台设备
   在推理时，同进程其它设备的 WS 心跳全停"，比超时更难查。所以一律
   ``run_in_threadpool``。
3. **任何异常都不许把 WS 连接搞崩**。设备侧重连成本高（要走一次 OTA
   才拿得到新 token），一轮对话失败最多是"这次没答上来"。
4. **音频上行不进 asyncio 队列**：直接进 ``list[bytes]``（60ms 一帧，
   一轮最多几十帧，内存可忽略）。**下行反过来，必须进节流队列**：
   边合成边交给 :class:`~app.voice.audio_pacing.AudioPacer`，
   由它按虚拟播放时钟 60ms 一帧匀速发 —— 自己直接 await socket 的话
   帧间隔不可控（断续）且反压会堵住 LLM 流读取（响应慢）。

⚠️ 关于 MCP（``{"type":"mcp"}``）
--------------------------------
设备发的 ``{"type":"mcp", ...}`` 是**设备本地工具**通道（JSON-RPC 2.0，
固件 mcp_server.cc 校验），语义是"设备当 MCP server"，暴露的是音量/电量
这类**设备自己的**能力。

我们的提醒数据在**服务端**（reminders 表），设备本地没有。所以工具调用
走的是**服务端路线**：LLM function calling → 服务端执行 → 写 reminders 表
→ 结果喂回 LLM → 口语回复。设备发的 mcp 消息与本路线无关，仍然只记
debug 日志并忽略（不返回响应，行为与未发送完全相同）。完整理由见
``tools.py`` 的模块 docstring。
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import time
from typing import Any, Final

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocket, WebSocketDisconnect

from .. import config
from ..db import SessionLocal
from ..llm_gateway import ConfigMissingError, chat_stream
from ..macs import norm_mac
from ..chat_quota import local_date_str, quota_snapshot, record_chat_usage
from ..memory import bridge as memory_bridge
from ..memory.persona import tone_profile
from ..memory.textutil import sanitize_for_prompt
from ..models import InteractionKind
from ..security import decode_token
from .. import models as _m
from .. import usage as usage_svc
from . import asr_tts, opus_codec, voiceprint
from .audio_pacing import AudioPacer
from .prompts import (
    BASE_VOICE_PROMPT,
    DEVICE_UNBOUND_MESSAGE,
    EMPTY_UTTERANCE_REPLY,
    LLM_NOT_CONFIGURED_MESSAGE,
    TOOL_LOOP_FALLBACK,
    build_system_prompt,
    now_context,
    reminder_context,
)
from .tools import (TOOL_DEFS, clock_mentions, execute_tool,
                    is_whole_event_yes, medicine_names, pending_medicine_events)

log = logging.getLogger(__name__)

# WS 关闭码。设备侧 websocket_protocol.cc 只按"是否断开"处理，
# 不解析具体码；但语义上分开对日志与后续排障极有价值。
CLOSE_UNAUTHORIZED: Final[int] = 4401      # token 缺失/无效/设备不存在
CLOSE_DEVICE_MISMATCH: Final[int] = 4001   # token 有效但 Device-Id 不符

# 下行二进制帧头（v2 口径）。type=0 音频 / 1 JSON。
# 固件 protocol.h 的 BinaryProtocol2 是 packed struct，字段顺序即字节顺序，
# 顺序写错的表现是"payload_size 读到 version 上"→ 音频全是噪声。
_BIN_V2_HEADER: Final[tuple[str, ...]] = (">H", ">H", ">I", ">I", ">I")

# 分句正则：中英文句末标点 + 换行。lookbehind 保证标点本身留在前一句
# （否则"您先吃药。"会被切成"您先吃药" + "。"，念出来多一次停顿）。
_SENTENCE_SPLIT: Final[re.Pattern[str]] = re.compile(r"(?<=[。！？!?；;…\n])")

# 少于这个字数的碎片并回前一句。TTS 对 1-3 字的句子会念出明显的
# "起头-收尾"爆破音，老人听到的是"…的药。"这种断裂。
_MIN_SENTENCE_CHARS: Final[int] = 4

# ---- 服务端 VAD（老人语速慢 / 轻声 / 远场）----
# 2026-10-17 重构。旧实现只有一个静态门限（RMS 0.01）+ 一个静态阈值
# （连续静音 1500ms），对老人有三个失效模式：
#   ① 句中思考停顿 > 1.5s ⇒ 被判定"说完了"，后半句直接丢失；
#   ② 轻声/远场整句 RMS < 0.01 ⇒ 一句都不算语音 ⇒ 整句被丢弃；
#   ③ 门限一刀切低又会把房间底噪当语音 ⇒ 静音永远攒不够 ⇒ 会话挂死。
# 新实现三件套（默认值见 config.py，全部可配）：
#   * **底噪跟随**：每帧按能量估计房间底噪（快降慢升，语音几乎不抬它），
#     门限 = max(绝对下限, 底噪×系数) —— 安静房间救轻声老人，
#     嘈杂房间自动抬高，诚实降级（那种房间里的耳语分辨不出）；
#   * **迟滞（Schmitt）**：enter 门限判"开口了"，keep 门限判"还在说"，
#     句中轻声音节/换气不掉出语音态；keep 只保留 VOICE_SOFT_GRACE_MS
#     （锚定最后一次高能量帧，软帧不刷新）—— 否则持续底噪会无限续期；
#   * **停顿自适应**：本轮出现过"停顿后又继续"的长停顿 P，则阈值放大到
#     P×系数+500ms（封顶 VOICE_SILENCE_MAX_MS）—— 老人已经用行动证明
#     "我说话慢、停顿长"，服务端就多等一会。
_NOISE_FLOOR_INIT: Final[float] = 0.002     # 底噪初值：实测空调房 0.002-0.004 的下限
_NOISE_FLOOR_DOWN: Final[float] = 0.02      # 低于底噪时每帧的吸收系数（快降）
_NOISE_FLOOR_UP: Final[float] = 0.001       # 高于底噪时每帧的吸收系数（慢升）
_ENTER_FACTOR: Final[float] = 2.0           # enter 门限 = 底噪 × 2
_KEEP_FACTOR: Final[float] = 1.3            # keep  门限 = 底噪 × 1.3
_PAUSE_MARGIN_MS: Final[int] = 500          # 自适应阈值的固定余量

# 短句豁免上限（见 `_has_real_speech`）。
# 累计语音不超过这个值时**不要求**有连续语音段 —— 老人说「好」「嗯」
# 「退下吧」整句只有 250~600ms，一刀切会把它们判成噪声。
# 现场实测：噪声爆发平均 204ms、最长 180~200ms；真语音平均 522ms。
_SHORT_UTTERANCE_MS: Final[int] = 1500

# ---- 全双工打断（F2，2026-10-21）----
# 播报中检测到人声就停嘴改听 —— 这是"AEC 全双工"的体验核心：老人说话时
# 音箱不该自顾自讲完。上游小智同款机制（core/handle/receiveAudioHandle.py：
# ``if conn.client_aec and have_voice: if conn.client_is_speaking ...:
# handleAbortMessage``），且同样有唤醒后宽限期（上游 2s）。
#
# 为什么敢在播报期信 VAD：设备侧 AEC（CONFIG_USE_DEVICE_AEC）已把播放声
# 从麦克风信号里抵掉，残留下的能量远低于人声门限。**没有 AEC 的会话**
# （auto/manual 模式）不走这条：那种模式下设备播报时根本不推流，
# _continuous() 为假自然被挡住 —— 但真正的原因是没 AEC 时这会把音箱
# 自己的声音当成打断，那比不能打断糟得多。
_BARGE_GRACE_SEC: Final[float] = 2.0        # 开播后 2s 内不判打断
                                              # （AEC 残余 + 唤醒词尾巴）

# ---- 反馈信号识别（2026-10-31）----
# 打断是"老人不满意/没说到点上"的**强**信号；重复提问是"上一句没说清"的
# 弱信号。两者都必须落进人格演化，否则画像里永远只有正例（生产库实测
# 21 正例 / 0 反例，是自我强化而不是越用越懂老人）。
_RETRY_LOOKBACK: Final[int] = 3        # 与最近几轮 user 文本比对
_RETRY_SIMILARITY: Final[float] = 0.8  # 归一化后的字符级相似度阈值
#: 两句长度比的下限。为什么需要它：``SequenceMatcher`` 对"短句是长句子串"
#: 给分很高（「今天天气怎么样」vs「今天天气怎么样啊孩子」= 0.82），而那
#: 多半是**接着说**而不是重问。误判成 retry 的代价不只是统计脏了 ——
#: judge_feedback 会真的写进一对正/反例（"说成B比A更容易被接受"），
#: 那条假偏好会一直留在 APP 的偏好列表里。真正逐字重复的两句长度比接近 1。
_RETRY_MIN_LEN_RATIO: Final[float] = 0.85

# ---- 会话建立时叫设备同步配置（2026-11-01）----
#: 设备侧 MCP 工具名（固件 main/mcp_server.cc 注册，与 voice/tools.py 同名）。
#: 两边是两份手写字符串，改一处必须改另一处 —— 用同一个常量名提醒后来人。
_DEVICE_SYNC_TOOL: Final[str] = "self.elder.sync_now"
#: 等它回包的上限。固件的回调只往事件组置一个位就返回，2 秒已经宽得离谱。
_DEVICE_SYNC_TIMEOUT: Final[float] = 2.0
# ⚠️ 刻意**没有**时间节流（"同一台设备 60 秒内只叫一次"那种）。
# 理由是节流会让行为依赖墙上时钟：同一段代码在"刚连过"和"隔了很久"两种
# 情况下走不同分支，用例就会按时序变红/变绿，而按钟点变红的用例比没有
# 用例更坏。重连风暴的代价也远没有想象中大 —— 设备侧同步任务是单线程
# 串行的，重复叫只会让它多跑一轮（一次 HTTPS，约 200ms），且语音链路
# 走的是另一条连接，不排队。


def _norm_utterance(text: str) -> str:
    """归一化一句话再比相似度：只留字/词，去标点、空白、语气符号。

    不归一化的话「明天早上八点吃药。」与「明天早上八点吃药」相似度不到
    0.9 —— 老人复述时标点几乎必然不一样，而我们要判的是"是不是同一件事"。
    """
    return re.sub(r"[\W_]+", "", str(text or ""))


# ---- 送 ASR 前裁掉头尾静音（P1：首句响应时间，2026-10-21）----
# 实测（生产主机容器内 → 宿主 FunASR 8082）：ASR 延迟 ≈ 280ms 基座
# + 230ms/秒音频，**音频长度线性进耗时**。而 _pcm 里装的是从 listen start
# 到 finalize 的全部音频：老人从唤醒到开口常有 1~2s，尾部还要等够静音
# 阈值（VOICE_SILENCE_MS 起）—— 每轮 1~3s 是纯静音，白等 0.2~0.7s。
# VAD 每帧能量本来就在 _feed_vad 里算过，这里只按记录到的语音帧区间切片，
# 不引入第二次能量计算（同一事实两份实现必然分叉）。
_TRIM_HEAD_PAD_FRAMES: Final[int] = 6        # 词头留 6 帧 = 360ms
_TRIM_TAIL_PAD_FRAMES: Final[int] = 2        # 词尾留 2 帧 = 120ms
#: 兼容名：句中断点两侧外扩时两头都按**词头**值算（断点后面是新词的词头，
#: 那里才是会丢字的地方；尾部多留只是白等 ASR）。
_TRIM_PAD_FRAMES: Final[int] = _TRIM_HEAD_PAD_FRAMES
#: ⚠️ 2026-11-03 用户实报「如果我在10秒左右再次说话，前面两个字没有识别到」。
#:
#: 头尾原先都是 2 帧（120ms），词头这一侧**太窄**：``_first_speech_frame``
#: 是 VAD 判"能量过 enter 门限"的第一帧，而老人开口的头一两个字常常是
#: 轻声（"我""这""嗯"），能量在门限之下 ⇒ 那两帧不被记进语音区间 ⇒
#: 按 ``first-2`` 切就把这两个字整段切掉，送进 ASR 的音频直接从第三个字
#: 开始。症状正是"前面两个字没识别到"，而且**每轮都发生、只丢开头**。
#:
#: 为什么加长是安全的：多留 240ms 只是多送 240ms 静音，按 230ms/秒的
#: 实测关系只多 ~55ms 延迟；而切掉词头是**不可逆**的内容损失。
#: 词尾不加长：那里没有"丢字"风险（静音多留反而让 ASR 更容易断句）。
_TRIM_MIN_SAVE_MS: Final[int] = 300          # 裁掉总量不足 300ms 就原样发：切片有风险
                                              # （VAD 误判会切掉轻声的词），没收益不担风险

# ---- 送 ASR 前另裁「句中长静音」（2026-11-01 真机：唤醒词只认到两次）----
# 生产日志：838 帧（50.3s）→ 只裁头尾 → 259 帧（15.5s），识别为空。
# 259 帧里只有 540ms（9 帧）真语音，其余是十几秒静音 —— 只裁头尾等于
# 把"一句话 + 十几秒静音 + 另一句"整坨喂给 ASR，短句被静音淹掉。
# 所以按语音帧下标表把窗口切成若干段拼起来。三条闸都是"宁可不动"：
_TRIM_GAP_KEEP_FRAMES: Final[int] = 8        # 相邻语音帧间隔 ≤ 8 帧（480ms）算同一段。
                                              # 句中换气/轻声音节就几百毫秒，切太密
                                              # 会把一个词劈两半，ASR 直接丢字。
_TRIM_GAP_MIN_SAVE_MS: Final[int] = 2000      # 句中静音至少省下 2s 才拼接。
                                              # 正常一轮（连着说一两句）省不到 2s ⇒
                                              # 走连续窗口，送 ASR 的字节与改动前
                                              # 完全一致，行为零变化；只有"长静音"
                                              # 这种形态才动手。
_TRIM_MIN_KEEP_MS: Final[int] = 300           # 拼完不足 300ms 就回退连续窗口：
                                              # 一小片碎片送 ASR 只会返回空。

# 每轮对话记 Interaction 的 kind。用 DIALOG（=1）而不是新值：
# schemas.InteractionReportIn.kind 是 int，取值集合由
# models.InteractionKind 定义，新增枚举值要连 APP 统计口径一起改。
_INTERACTION_KIND: Final[int] = int(InteractionKind.DIALOG)


# ============================================================ 送 TTS 前的可播性过滤
#
# 为什么必须有这一层（2026-10-21 真机事故）
# ----------------------------------------
# 老人问天气，听到的第一句是「老人查天气，我要调用……」——模型把**它自己的
# 行动规划**当成回复念了出来，真正的天气答案反而在后面。
#
# 这不是偶发。根因是链路把「每一轮 LLM 的文本增量」都当回复播报，而
# function calling 的一轮里，模型常常会先写一句"我打算做什么"再发
# tool_calls。那一轮**不是给老人听的**，它是模型的思考草稿。
#
# 三层防御，这里是最外层（兜底）：
#   ① prompts.py 的硬约束（让模型别这么写）
#   ② 这一层的逐句安检（拦住漏网的）
#   ③ 全部被拦时的话术兜底（见 UNSPEAKABLE_REPLY_FALLBACK）
#
# 为什么是「逐句」而不是「整轮扣住不播」
# ------------------------------------
# 整轮扣住要等模型把整段说完才知道有没有 tool_calls，首句延迟会多出
# 整整一段生成时间 —— 老人本来就在抱怨"很久才开口"。逐句安检是 O(1) 的，
# **不增加任何延迟**，只在命中特征时丢弃该句。

#: 模型爱把工具名（`get_weather`、`createReminder`）写进正文。
#: 只认**代码形状**的标识符：含下划线的，或小写开头接大写的驼峰。
#: 刻意不拦所有拉丁字母 —— "PM2.5"、"3.5 度" 这类是要念出来的。
_CODE_IDENT: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+"      # snake_case：get_weather
    r"|[a-z]{2,}[A-Z][a-z]+"                    # camelCase：createReminder
)

#: 「我要用工具」语义。命中即不播 —— 这类句子对老人零信息量。
#: 注意 "调用" 后面要跟工具类名词才算：老人自己说"您调用一下电视"不该被拦，
#: 但那是**上行**文本，根本不经过这里；这里只处理模型的下行回复。
_TOOL_TALK: Final[re.Pattern[str]] = re.compile(
    r"(?:调用|使用|执行)[^，。！？；]{0,10}(?:工具|函数|接口|参数)"
    r"|(?:工具|函数)调用"
    r"|tool_call"
    r"|\bJSON\b"
    r"|\bAPI\b"
    r"|\btoken\b",
    re.IGNORECASE,     # 中文字符不受影响；这里只为兜住 JSON/json、API/api 的大小写
)

#: 第三人称叙述（"老人查天气…"）。老人是听的人，不是被描述的第三方。
#: 只认「老人 + 一个动作动词」这种主语结构，不拦"老人家用着方便"。
_THIRD_PERSON: Final[re.Pattern[str]] = re.compile(
    r"^\s*老人[^，。！？；]{0,10}(?:问|想|要|说|查|需要|希望)"
)

#: 「老人…我要…」同句出现 = 自我规划独白。
_SELF_PLAN: Final[re.Pattern[str]] = re.compile(
    r"老人[^。！？；]{0,24}(?:我要|我需要|我先|我来|让我)"
)

#: 模型口吻的**第一人称思考**（2026-11 用户实报「思考过程被念出来」）。
#:
#: 为什么必须单独一条：``_THIRD_PERSON`` 只认行首「老人」，``_SELF_PLAN``
#: 要求「老人」与「我要」同句 —— 而思考体的典型开头是「嗯，用户问的是…」
#: 「让我想想」「我需要先查一下」，一个「老人」都没有，两条都拦不住。
#:
#: 为什么可以偏严：这里判的是**模型要念给老人听的话**，不是老人说的话；
#: 老人自己的第一人称根本不经过这个函数。但仍然只锚行首、且要求是
#: 「陈述自己在思考」的句式 —— "让我帮您看看"这类客服话术必须放过去。
_REASONING_TRACE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:"
    r"嗯[，,]\s*用户"                                   # 嗯，用户问的是…
    r"|用户(?:问|说|想|要|需要|希望)"                   # 用户问的是… / 用户说…
    r"|让我(?:先)?(?:想想|想一想|看看|看一下|查|确认|分析|梳理|理一理)"
    r"|我需要(?:先|再)?(?:查|确认|了解|分析|看看)"
    r"|我先(?:查|确认|看看|看一下|了解|分析)"
    r"|先(?:确认|检查|了解)一下"
    r"|分析一下"
    r"|(?:思考|推理|分析|reasoning|thinking)\s*[:：]"
    r")"
    # ---- 2026-11-03 补：**不锚行首**的两条铁证 ----
    #
    # 用户实报「有一句思考过程也被念出来了」，生产日志（会话 981fcf70）那一轮
    # 8 句拦下 7 句，漏的就是这一句：
    #     [sad] 这个情况，我得先看看
    # 它既不以「用户」开头，也没有「让我/我需要/我先」，上面那条**锚行首**的
    # 正则一个都命中不了 —— 而它明明是模型在自言自语。
    #
    # ⚠️ 这两条必须写在上面的 `)` **之后**：写进组里就仍然被 `^\s*` 锚住，
    #    等于没改（这次就是这么踩了一次，T8 立刻红）。
    #
    # 为什么它们可以不锚行首：这些词在"对老人说的话"里根本不会出现 ——
    #   · 「用户」是模型眼里的第三人称，正常回答会叫「您」；
    #   · 「我得先/我需要先 + 看/查/确认」是**规划动作**，正常回答是
    #     「我帮您看看」「我这就查」，中间一定有"帮您/给您/这就"。
    r"|用户(?:问|说|想|要|需要|希望|可能|应该)"
    r"|我(?:得|需要)(?:先|再)(?:看看|看一下|查|确认|了解|分析|想想|想一想)",
    re.IGNORECASE,     # 只为兜住 reasoning:/Reasoning: 的大小写
)

#: Markdown 强调符与行内代码。TTS 会把 ``**`` 念成"星号星号"。
_MD_NOISE: Final[re.Pattern[str]] = re.compile(r"\*\*|__|`")
_MD_HEADING: Final[re.Pattern[str]] = re.compile(r"^\s*#{1,6}\s*")
#: 列表前缀（"- "、"1. "、"• "）。念出来是"横杠""一"这种噪音。
_MD_BULLET: Final[re.Pattern[str]] = re.compile(r"^\s*(?:[-•·]|\d+[.、)])\s*")

#: 整段回复都不可播时报给老人的话。
#: 措辞必须"承认没办好"，不能假装答了 —— 老人会以为设备坏了。
UNSPEAKABLE_REPLY_FALLBACK: Final[str] = "这个我一时没说明白，您再问一遍，我重新查"

#: 工具轮「缓发」门槛（字符数）。一轮生成的文字先攒着，攒到这个长度才认为
#: 「这是一段正经回答」并开始边生成边念；一轮结束时若发现是工具轮，攒下的
#: 直接丢弃不念。见 _chat_round 里那段长注释（2026-10-29 用户实报
#: 「一句话回答两次」）。
#:
#: 取值依据：实测模型抢跑的那句「好的，我给您记着，十二点四十五提醒您下楼。」
#: 是 21 字，稳稳落在门槛内；而正常回答攒到 40 字才开始念，多等的只有
#: 小几百毫秒（模型吐 40 字的时间），首句时延基本不变。
#: ⚠️ 调小会让抢跑的话漏出来（门槛低于 21 就等于没有闸门），
#:    调大只是让首句更晚开口 —— 所以只许往大调，不许往小调。
_TOOL_ROUND_HOLD_CHARS: Final[int] = 40

#: 一轮 LLM 从发请求到**第一个事件**最多等多久（秒）。超过就放弃这一轮，
#: 改说一句兜底话术并重新武装监听。
#:
#: 为什么必须有（2026-11-04 生产日志实测）：那天的 9 轮对话里
#:   最小 723ms / 中位 1023ms / 最大 **94007ms**，其中 **3 轮 > 20 秒**。
#: 也就是说三分之一的轮次会让老人干等半分钟以上，而这段时间里：
#:   · 设备一直在推上行音频，服务端在忙 ⇒ 整段被丢弃
#:     （同一份日志：`思考期丢弃上行音频 1160 帧（69600ms）`）；
#:   · 老人以为设备坏了，自己长按按键把提醒确认掉；
#:   · 等他终于等到回应，服务端才去确认 —— 而那条已经是 CONFIRMED 了，
#:     于是回一句「请让老人按一下按键」（他 6 秒前刚按完）。
#: 这条因果链就是用户实报「按键按了不能确认」的真正来源。
#:
#: 取值 20 秒：中位 1 秒、P 值上看 20 秒已经远超正常波动，再等只会让老人
#: 更慌；同时比"厂商偶发慢一档"（3~5 秒）留足余量，不会误杀正常轮次。
_FIRST_EVENT_TIMEOUT_SEC: Final[float] = 20.0

#: 首字超时后念给老人的话。**必须说人话、必须短、必须把球抛回去**：
#: 老人此刻已经等了 20 秒，任何解释（"模型"、"网络"、"服务"）他都听不懂，
#: 也不关心；他只需要知道"现在该再说一遍"。
LLM_SLOW_FALLBACK: Final[str] = "我这边有点慢，您再说一遍好吗？"

#: ``_chat_round`` 的首字超时哨兵。
#:
#: 为什么不用 ``None``：``None`` 的语义已经被"被 abort 叫停"占用了（调用方
#: 靠它区分"老人抢话"和"正常收尾"）。两者混成一个，会让超时被当成打断
#: 处理 —— 既不清 `_dismiss_candidate` 也不播任何话，老人听到的是静音，
#: 而且日志里一个字都不会有。
_FIRST_EVENT_TIMEOUT: Final[object] = object()

#: 只有**像要调工具**的话才值得为了防抢跑多等那几个字（见 _chat_round 的
#: ``hold_chars``）。这道闸门对每一句话都生效的话，等于给所有闲聊都加上
#: "攒够 40 字才开口"的代价 —— smoke 第 13 组直接量出来首帧 400ms → 500ms。
#: 而"模型抢先说一句、再调工具"只发生在要调工具的话上，所以先花一次正则判。
#:
#: 判错的代价是**单向**的：漏判 = 这一轮少了保护网（prompt 里那条硬约束
#: 还在，只是没有代码兜底）；多判 = 这一轮多等几十毫秒。所以宁可写宽。
_TOOL_HINT_RE: Final[re.Pattern[str]] = re.compile(
    r"提醒|叫我|喊我|叫一下|记得|记一下|记着|别忘|日程|闹钟|定时|待办|"
    r"音量|声音|大声|小声|静音|亮度|屏幕|亮一点|暗一点|"
    r"电量|电池|充电|天气|气温|温度|下雨|刮风|预报|"
    r"查一下|帮我查|几点|星期几|几号|日期|明天|后天|早上|中午|晚上|下午")

#: 纯标点（"。" / "……" / 只有标点和空白）。这种片段**不算事故**：
#: 它是切句的副产物（整句已播完，缓冲区里只剩那个句号），
#: 送去 TTS 会念出一声怪响。必须与"不可播报"分开计数 ——
#: 混在一起会让"整段回复都被拦下"的兜底话术被误触发。
_PUNCT_ONLY: Final[re.Pattern[str]] = re.compile(r"[。！？!?；;…、，,\s]+")

#: 「假成功」措辞识别。命中 = 模型在对老人宣称某件事已经办好了。
#:
#: 为什么需要它（2026-10-21 生产事故）
#: ----------------------------------
#: 老人说"两分钟后提醒我吃药"，``create_reminder`` 因时间解析失败返回
#: ``ok=false``，模型第二轮却回"记好了，两分钟后提醒您"。生产库
#: ``reminders`` 表里**一条都没建成**（interactions id=18/19 的 tool_calls
#: 都是 create_reminder ok=false），老人以为建好了，到点什么都没发生。
#:
#: prompt 里那条硬约束是**软**的（模型照样违反），所以这里加一道代码闸门。
#: 口径：动词 + 完成态（好了/上了/下了/住了/妥了），或"已经+动词"。
#: 刻意**不**匹配"查到了/看到了/想好了"这类非操作词 —— 那些不是承诺。
_SUCCESS_CLAIM_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:记|建|创建|安排|设|设置|定|加|写|添加|办|办妥|搞定)"
    r"(?:好|上|下|住|妥|了|定)|"
    r"已经(?:帮您)?(?:记|建|创建|安排|设|设置|定|加|写|添加|提醒)")

#: 会改写老人提醒状态的工具。只有这些工具失败时才启动上面的闸门 ——
#: 把"任何工具失败"都算上会让同轮里成功的天气查询连带被拦，那是误伤。
_REMINDER_TOOLS: Final[frozenset[str]] = frozenset(
    {"create_reminder", "delete_reminder", "toggle_reminder", "confirm_reminder"})

#: 「结束对话」的关键词，**分两档**（2026-10-22 用户需求：既要多认、又不能误认）。
#:
#: 为什么必须有（2026-10-21 真机反馈）：设备处于 realtime 连续收听模式，
#: 这一态**不跑唤醒词**，老人喊"退下吧"之后麦克风仍在推流。服务端原先
#: 没有"结束对话"这个概念，于是每 60s 拿环境噪声送一次 ASR、念一次
#: "抱歉，我没听清"—— 老人听到的是音箱永远在自言自语。
#:
#: 为什么分两档（2026-10-22 用户原话）：「对待机的词义识别添加多个词汇，
#: 尽量使用大模型来识别待机意图，语句中出现关键词的时候，提交给大模型答复时，
#: 让大模型根据上下文来分析用户是不是让进入待机状态……这样可以避免因关键词
#: 出现而误进入待机，也能尽量多的识别出待机意图。」
#:
#:   * ``_DISMISS_STRONG_RE``：**只对设备说才成立**的祈使句（"退下""待机"
#:     "闭嘴""别出声"）。这类话在正常陪聊里几乎不会出现，命中即待机，
#:     不必等一次 LLM 往返（老人说完"退下吧"，多等 2 秒才安静是折磨）。
#:   * ``_DISMISS_WEAK_RE``：**必须看上下文**才成立的词（"走了""出去"
#:     "休息吧""不用了""安静"）。它们既可能是让设备停，也可能是
#:     "他走了""我明天要出去""孩子睡了，安静点""不用了，我不吃"。
#:     命中后**不直接待机**，而是照常走一轮 LLM，并在 system 里加一段
#:     复核提示、把 ``enter_standby`` 工具放开给它 —— 由模型结合上下文判。
#:
#: 分档模式由 ``config.VOICE_DISMISS_LLM_VERIFY`` 控制（weak/all/off）。
#:
#: ⚠️ 2026-11-03 用户实报「他已经说了先不聊了，还是频繁在说抱歉我没听清」：
#: 「不聊了」「晚安」「我睡了」原先都在**弱档** ⇒ 只置 ``_dismiss_candidate``、
#: 交模型复核；模型若不调 ``enter_standby``（默认 ``VOICE_DISMISS_LLM_VERIFY
#: = "weak"`` 时它本来就没被要求调），本轮什么都不会发生 ⇒ 老人被反复念
#: 「我没听清」，而他刚刚明确说过不想聊了。**这是打脸，不是没听清。**
#: ⇒ 这些语义上**只可能是"结束会话"**的说法升到强档直判：说完立刻待机，
#:   不再赌一次模型往返。
#:
#: 判据怎么划：强档 = 离开当前对话语境就不成立的话（"退下""不聊了""晚安"
#: "我睡了"）；弱档 = 在正常陪聊里也常出现的话（"不用了""没事了""停一下"
#: "再见""安静""小点声"）—— 那些继续交模型看上下文。
#: 同一句**只能出现在一档**：``_dismiss_tier`` 先强后弱，重复虽不出错，
#: 但会让人误以为两档都在管它，日后改一档忘了另一档。
_DISMISS_STRONG_RE: Final[re.Pattern[str]] = re.compile(
    r"退下|下去吧|一边去|待机|待命|进入待机|"
    r"闭嘴|静音|别出声|不出声|"
    r"让我安静|让我清静|我想静静")
#: 弱判据。刻意**不收**「好了/行了/知道了/好/嗯」这类通用应答 ——
#: 它们几乎每轮都会出现，命中即调一次复核，纯属白烧 token 和首句时延。
#:
#: ⚠️ 2026-11-03 用户明确否决了「把结束语从弱档升到强档」这个改法，原话：
#:    「弱档中待机词不能直接判定为强待机，这些词在普通对话中也会夹杂，
#:      如果直接判定为待机会造成很严重的后果。这些词需要大模型进行判断
#:      是否是进入待机的意图，确认是进入待机意图才能让设备待机。」
#:    「不聊了」「我睡了」「晚安」「先这样吧」在陪聊里**经常**只是叙述
#:    （「今天太累了，我睡了」是在讲自己的状态，「昨天跟人聊到这儿就散了」
#:    是在回忆），直判待机会把正在进行的对话直接掐掉 —— 而老人不会知道
#:    是自己哪句话把设备关了，只会以为设备又坏了。
#:    所以这一档**只能**交大模型按上下文判意图（config.VOICE_DISMISS_LLM_VERIFY
#:    默认就是 "weak"），模型调 enter_standby 才真的待机。
#:    真要让某句话直判，只有「退下/待机/闭嘴」这类**只能对机器说**的祈使句
#:    才够格进强档 —— 它们的字面意思在正常对话里不成立。
_DISMISS_WEAK_RE: Final[re.Pattern[str]] = re.compile(
    r"走开|走吧|你走|出去|走了|我走了|要出门|"
    r"休息吧|去休息|你休息|歇着吧|歇会儿|歇一会|"
    r"睡吧|睡觉吧|我睡了|我要睡了|要睡了|该睡了|去睡了|困了|"
    r"不聊|别聊|不用聊|聊到这儿|聊到这|先这样吧|就这样吧|到此为止|"
    r"不说了|别说了|不用说了|不要再说了|别讲了|别念了|别吵了|别嚷嚷|"
    # ⚠️ 2026-11-03 用户实报「设备已经应答『有事随时叫我』，但是没有进入待机」。
    #
    # 现场（生产日志 22:07:32）：老人说「嗯，我不想说了。」→ 模型**正确**地
    # 调了 enter_standby → 被 _run_tool 的闸门拒了，日志写着
    # 「本轮没有待机关键词，拒绝模型发起的待机」，于是设备念完告别语还醒着。
    #
    # 为什么「不想说了」漏网：上面只有 ``不说了``（字面三连），而老人说的是
    # 「不**想说**了」—— 中间多一个"想"就匹配不上。同类的还有"不想聊了"
    # "不想讲了""没话说""说够了"。
    #
    # 放进**弱档**而不是强档，与上面 2026-11-03 那条用户裁决一致：这一档只
    # 负责"把 enter_standby 工具放开给模型"，是否真待机仍由模型按上下文判
    # —— "我不想说了"在陪聊里也可能只是"今天不想说这件事"，模型不调工具
    # 就什么都不会发生。强档只管「退下/待机/闭嘴」这类只能对机器说的祈使句。
    r"不想说|不想聊|不想讲|不想再聊|不想再说|"
    r"没什么好说|没啥好说|没话说|说够了|不问了|先不聊|"
    r"小点声|安静|再见|拜拜|晚安|"
    # ⚠️ 2026-11-04 用户实报「日志中可以看到识别到了我让他关机（待机），
    # 但是意图识别没有进入待机」。
    #
    # 现场（生产日志 00:42:50）：
    #   识别：你先关机吧。
    #   模型**正确**调了 enter_standby
    #   → 被 _run_tool 的闸门拒：「本轮没有待机关键词，拒绝模型发起的待机」
    #   → 设备念完话还醒着，老人以为它不听话。
    #
    # 为什么漏：两档都没有「关机」。「退下/待机/闭嘴」在强档，而「关机」在
    # 老人的口语里既可能是对设备说的（"你先关机吧"），也可能是问别的东西
    # （"电视怎么关机""手机咋关机"）—— 按 2026-11-03 那条裁决，这种词只能
    # 进弱档：**只负责把 enter_standby 放开给模型**，是否真待机仍由模型按
    # 上下文判。本次模型本来就判对了，缺的只是这道闸门的钥匙。
    #
    # 「关掉/关闭」同理收进来：它们常出现在"把电视关掉""关闭提醒"里，
    # 弱档不直判待机，模型不会因为这两个字就关掉设备。
    r"关机|关掉|关闭|"
    r"你忙|不用管我|别管我|"
    r"不用了|没事了|别放了|停一下|停止播放")

#: 兼容名：旧代码/旧测试里只关心"是不是命中"，不关心档位。
_DISMISS_RE: Final[re.Pattern[str]] = re.compile(
    f"(?:{_DISMISS_STRONG_RE.pattern})|(?:{_DISMISS_WEAK_RE.pattern})")

#: 服务端侧待机工具名。与设备 MCP 的 ``self.elder.standby`` 是两回事：
#: 这个是给**大模型**调的（判意图），那个是**下发给固件**的（真停麦）。
_STANDBY_TOOL_NAME: Final[str] = "enter_standby"

#: 连续空识别的待机阈值（2026-11-03 用户实报「说了先不聊了还在频繁说抱歉
#: 我没听清」）。
#:
#: 为什么是 3，而不是 1 或 2：
#:   * 1 次就待机太狠 —— 老人**确实**可能只说了一半、被环境声盖掉、或者
#:     说话前先清了清嗓子。第一次照旧念 ``EMPTY_UTTERANCE_REPLY`` 给他一个
#:     "我在这儿，你再说一遍"的机会，这是产品该有的礼貌；
#:   * 2 次仍不待机：realtime 模式下设备播报完就回 listening，中间那点
#:     回声残留很容易再送一次空 ASR，2 次就待机等于"偶尔一次真没听清就被
#:     关掉"；
#:   * 3 次时，"连续三次都没认出任何字"已经不是 ASR 的偶然失手，而是
#:     麦克风前面**没有人想说话**（老人走了 / 已经不想聊了 / 只是房间噪声）。
#:     此时继续念"我没听清"就是对着空气自言自语 —— 老人听到的是"设备坏了"。
#:     待机是唯一能真正结束这件事的动作（设备回 Idle 并重新打开唤醒词）。
#:
#: 计数只在**有效识别**时清零（见 ``_run_turn`` 里 ``_last_active_at`` 那行），
#: 重新武装监听时也清零（见 ``_rearm_listening``）—— 否则一次待机冷却之后
#: 计数还停在 3，下一句话刚开口就被判待机。
_EMPTY_ASR_STANDBY_STREAK: Final[int] = 3


def _dismiss_tier(utterance: str) -> str:
    """返回命中档位：``"strong"`` / ``"weak"`` / ``""``（未命中）。"""
    if _DISMISS_STRONG_RE.search(utterance):
        return "strong"
    if _DISMISS_WEAK_RE.search(utterance):
        return "weak"
    return ""


def _looks_toolish(messages: list[dict[str, Any]]) -> bool:
    """这条用户话像不像"要调工具"的（决定要不要为防抢跑多等几个字）。

    只看**最后一条 user 消息**：历史里的"提醒我吃药"不该让本轮闲聊也背上
    缓发代价。``content`` 不是字符串（多模态分块）时按"不像"处理。
    """
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if not isinstance(content, str):
            return False
        return _TOOL_HINT_RE.search(content) is not None
    return False


# ---------------------------------------------------------------- 提醒确认兜底
#
# 2026-10-22 真机事故：老人说「我已经喝完水了」，服务端时延日志里
# ``tool=0ms`` —— **大模型压根没调 confirm_reminder**，于是设备侧一直
# 不知道"办完了"，全屏提醒挂着不退，老人对着一屏白字等了 20 分钟。
#
# 工具描述里已经写了"必须调用"，但把这条生杀权完全交给模型是不可接受的：
# 漏调的后果是**屏上一直白、老人以为设备坏了**，而且不会自愈（4 小时才收）。
#
# 所以加一层服务端兜底，与待机关键词同一套思路：
#   * 只认**强确认**措辞 —— 「喝完了/吃过了/药吃了」这种离开提醒语境就不成立的；
#   * **不认**「好了/知道了/行」—— 那在正常聊天里到处都是，
#     误判的后果是"没吃药却被记成吃了"，比漏记危险得多；
#   * 模型自己调过就不再调第二次（看 tool_trace）；
#   * 调了但设备说没有待确认的提醒（返回 false）也无害，只是白跑一次。
#
# ⚠️ 措辞要按**老人实际怎么说话**写，不能按书面语写。用户 2026-10-22 原话是
# 「我已经喝完水了」—— 里面是「喝完水了」，第一版只写了「喝完了」，
# 结果这句**最典型的确认语反而匹配不上**。所以这里取词根「喝完/喝过」，
# 而不是「喝完了」这种整齐的书面形态。
_CONFIRM_RE: Final[re.Pattern[str]] = re.compile(
    r"喝完|喝过|水喝了|把水喝了|喝掉了|"
    r"吃完药|吃过药|吃了药|把药吃了|药吃了|药吃完|药吃过|药喝过|"
    r"吃完了|吃好了|吃过了|吃掉了|"
    r"做完了|做好了|办完了|办好了|弄完了|弄好了|搞定了|搞好了|完成了")

#: 否定/未完成措辞：命中就**不算**确认，哪怕里面同时出现「喝完」。
#:
#: 「我还没喝完呢」里明明白白带着"喝完"，不挡的话会**把没做的事记成做完了**
#: —— 对服药提醒来说这是最危险的一种误判：屏幕收了、子女端显示"已服药"，
#: 而药其实还在盒子里。宁可漏判（让模型或下一句去纠正），也不能误判。
_NEG_RE: Final[re.Pattern[str]] = re.compile(
    r"还没|还没有|没喝|没吃|没弄|没做|没办|没完成|"
    r"不喝|不吃|不想|稍后|待会|等会|等一|一会|马上|等下|回头")


#: 有未确认提醒时追加到 system 的状态提示。
#:
#: 为什么必须由服务端"告诉"模型，而不是靠工具描述自证：
#: 用户 2026-10-22 反馈「语音创建提醒时工具调用完全正常」，说明工具链没问题，
#: 差别在**上下文** —— 创建时老人说「提醒我八点吃药」，意图自明；而
#: 「我已经喝完水了」在模型眼里就是一句闲聊，它不知道屏上正挂着一条提醒。
#: 把状态摆到 system 里，模型才有依据去调 confirm_reminder。
#:
#: 2026-11-03 改：从「一个标题」升级成「清单 + 追问纪律」。固件 a85f21f 之后
#: 长按确认键只负责进对话、把话说清（「您吃的是哪种药？」），"哪条日程被确认了"
#: 完全落在服务端 + 模型身上。清单里同时给出逐味进度，模型才知道还剩哪味要问。
_PENDING_REMINDER_HINT: Final[str] = (
    "【当前状态】设备上有**已到点、尚未确认**的服药提醒，清单如下"
    "（括号里是逐味确认进度）：\n{block}\n"
    "屏幕正在（或马上）全屏显示这些内容，老人不确认就不会消失。下面是确认纪律，"
    "必须照做 —— 你要写进的是服药记录，**认错药比多问一句危险得多**：\n"
    "· 老人点了名（「降压药吃了」「氨氯地平吃了」）→ 调用 confirm_reminder，"
    "把他说出的那个词**原样**填进 medicine 参数。**不要**自己把「降压药」换成"
    "具体药名：服务端有一张专门的匹配表，你猜的药名很可能猜错。\n"
    "· 清单里**不止一条**、老人只说「吃了」「确认」却没说是哪一条 → **必须先问清"
    "是哪一种**，不许把几条一起确认，更不许只凭「吃了」两个字替他挑一条。\n"
    "· 清单里**只有一条**、老人说「吃了 / 确认 / 都吃了」→ **直接调用"
    "confirm_reminder，medicine 留空**，不要追问「是哪一味」。服务端会整条确认，"
    "并在记录里注明「未逐味点名」。"
    "反复追问是本功能最大的投诉点（2026-11-04）：设备长按 3 秒后问的就是一个是非"
    "问句，老人答「确认」却听到「您吃的是降压药还是降脂药」，再答「确认」还是同一"
    "句 —— 问三轮一次都没落库，屏幕和指示灯一直挂着。\n"
    "· 工具返回 ambiguous（候选药名）→ 把候选药名一个一个念给他听，让他挑一个，"
    "再带他挑的那个词重新调用。\n"
    "· 工具返回 matched=false → 复述一遍候选药名再问一次；**最多再问两轮**，"
    "仍然问不出来就如实说「我先记着，您方便时看一眼药盒」，然后结束这个话题，"
    "不要无限追问。\n"
    "· 确认完一条后，如果清单里还有别的待确认提醒，主动追问下一条"
    "（「那降糖药吃了吗？」），直到每条都有确切答案。\n"
    "· 老人含糊其辞（「好像吃了」「应该吃了吧」）**不算确认**，继续问，"
    "绝不许替他确认。\n"
    "· 他如果问「怎么关掉 / 怎么消掉」，回答就是：说清吃了哪种药，记下之后"
    "屏幕就会退。\n"
    "· 只陈述「记下了」；**不许报剂量、不许建议加量减量或停药**。"
)


def _pending_reminder_label(device_pk: int) -> str:
    """当前待确认服药提醒的**清单**（渲染成多行文本）；没有则返回空串。

    数据源直接复用 ``tools.pending_medicine_events`` —— 这份清单的用途是让模型
    知道"还有哪几条没确认"，它必须与 ``confirm_reminder`` 眼里的候选集**是同一
    批**，否则模型会追问一条工具根本确认不了的提醒（典型症状：老人回答了，
    工具却答「没有等着确认的提醒」）。所以这里不再另写一份 SQL。

    性能：那个函数是一次 ``join`` 把 ``Reminder`` 一起捞出来的（不是逐条
    ``db.get``）。本函数每轮对话开头都会调一次（见 ``_run_turn``），N+1 会让
    每一轮多出几条查询。

    ⚠️ 换行：模板里的换行是我们自己拼的，不会被 ``sanitize_for_prompt`` 压平；
    但**每个字段都要单独过 sanitize**。整段过一遍会把多行压成一行
    「- 「早上吃药」：氨氯地平（0/1 味） - 「中午吃药」：…」，模型分不清哪句
    属于哪条 —— 而且不会报错，只会答得含糊。药名/标题是家属可控输入，也必须
    防它靠换行伪造出角色行（见 ``textutil.has_role_marker``）。

    任何异常都吞掉返回空串：这条提示是**加分项**，查不到就不注入，
    绝不能因为它把整轮对话打挂。
    """
    try:
        with SessionLocal() as db:
            rows = pending_medicine_events(db, device_pk)
            if not rows:
                return ""
            lines: list[str] = []
            for ev, rule in rows:
                names = medicine_names(rule)
                done = [n for n in (ev.confirmed_medicines or []) if n in names]
                title = sanitize_for_prompt(str(rule.title or ""), limit=40)
                if names:
                    meds = "、".join(sanitize_for_prompt(n, limit=32)
                                    for n in names)
                    progress = f"（已确认 {len(done)}/{len(names)} 味）"
                else:
                    meds, progress = "未登记药名", ""
                lines.append(f"- 「{title}」：{meds}{progress}")
            return "\n".join(lines)
    except Exception:  # noqa: BLE001
        log.debug("查询待确认提醒失败（不影响本轮对话）", exc_info=True)
        return ""


#: 今天已提醒的日程清单最多列几条。
#:
#: 这段**每轮**都进 system prompt，而 system 是 prompt 缓存的前缀 —— 太长会
#: 挤掉记忆、提醒清单等其他上下文，也会让模型把注意力放在"念清单"上。
#: 日程提醒本来就不多（一天通常 1-3 条），4 条足够覆盖；超出的直接省略，
#: 宁可少提一句，也不要每轮拖着一长串旧事。
_REMINDED_SCHEDULE_MAX: Final[int] = 4

_REMINDED_SCHEDULE_HINT: Final[str] = (
    "【今天已经提醒过的日程】下面这些日程提醒**已经播报过、流程已走完**"
    "（日程类不需要老人确认，这是与服药提醒的区别）：\n{block}\n"
    "你可以在聊天里自然地问一句他做完了没有，例如"
    "「您让我提醒您的今天要还信用卡，您已经完成了吗？」。四条纪律：\n"
    "· 这只是关心，**不要调用任何工具去改状态** —— 记录已经算完成了，"
    "再确认一次只会制造假的第二条记录。\n"
    "· **一次对话里最多提一条**，别把清单整段念出来：老人记不住，"
    "而且连着念几件事听起来像在催账。\n"
    "· 老人说还没做完 → 可以问「要不要过一会儿再提醒您一次」，"
    "但**不要**承诺你去改提醒、**不要**自己新建提醒（那是另一个功能）。\n"
    "· 老人明显在说别的事（问天气、说不舒服、聊家常）时**不要**插这个话题；"
    "等他这轮说完了、有空档再顺口问。"
)


def _reminded_schedule_label(device_pk: int) -> str:
    """今天已经播报过的日程提醒（渲染成多行文本）；没有则返回空串。

    为什么需要它：日程类提醒「播报即完成」（2026-11-03 用户定义），状态一旦
    落成 CONFIRMED 就不再出现在 ``_pending_reminder_label`` 的待确认清单里，
    模型也就无从知道"刚才提醒过他还信用卡"。而用户明确要求模型能主动问
    「您已经完成了吗？」，所以必须另开一份"已完成、可闲聊"的清单。

    判据三条，缺一不可：
      * ``confirm_method == AUTO`` —— 只认"系统自动完成"这一种。子女在手机上
        代确认（APP）或老人自己确认过的，不该被当成"我刚提醒过你"来追问。
      * ``rtype != MEDICINE`` —— 服药提醒**不许**进这份清单。它的完成与否是
        依从率数据，模型随口一句"您吃完了吗"若被老人答"没有"，会诱导模型去
        改服药记录（另一个更严重的错）。
      * ``due`` 落在**今天**（按服务端时区，与 ``local_now`` 同源）—— 不按
        "最近 N 条"取，否则昨天没做完的日程会一直在今天的对话里冒出来。

    取 ``due_at`` 倒序 + limit：同一天里越晚的越"新鲜"，先提它。

    任何异常都吞掉返回空串 —— 与 ``_pending_reminder_label`` 同一个理由：
    这是加分项，查不到就不注入，绝不能因为它把整轮对话打挂。
    """
    try:
        # 局部导入：``..schedule`` 与 ``..voice`` 之间存在间接依赖，
        # 放到模块顶层容易绕成循环导入。这两个函数调用开销可忽略。
        from ..schedule import event_due_utc, local_now

        now = local_now()
        with SessionLocal() as db:
            rows = db.execute(
                select(_m.ReminderEvent, _m.Reminder)
                .join(_m.Reminder,
                      _m.Reminder.id == _m.ReminderEvent.reminder_id)
                .where(
                    _m.ReminderEvent.device_id == device_pk,
                    _m.ReminderEvent.state == int(_m.EventState.CONFIRMED),
                    _m.ReminderEvent.confirm_method == int(_m.ConfirmMethod.AUTO),
                    _m.Reminder.deleted_at.is_(None),
                    _m.Reminder.rtype != int(_m.ReminderType.MEDICINE),
                )
                .order_by(_m.ReminderEvent.due_at.desc())
                .limit(_REMINDED_SCHEDULE_MAX * 2)   # 多取一点，过滤"今天"后再截断
            ).all()

            lines: list[str] = []
            for ev, rule in rows:
                try:
                    # ``event_due_utc`` 给的是绝对时刻（UTC）；必须换到本地再看
                    # "是不是今天"，否则跨零点那几条会归错日子。
                    due = event_due_utc(ev).astimezone(now.tzinfo)
                except Exception:  # noqa: BLE001  脏 tz 不该让整段清单消失
                    continue
                if due.date() != now.date():
                    continue
                title = sanitize_for_prompt(str(rule.title or ""), limit=40)
                if not title:
                    continue
                lines.append(f"- {due:%H:%M} 「{title}」")
                if len(lines) >= _REMINDED_SCHEDULE_MAX:
                    break
            return "\n".join(lines)
    except Exception:  # noqa: BLE001
        log.debug("查询今天已提醒的日程失败（不影响本轮对话）", exc_info=True)
        return ""


def _fallback_confirm_verdict(device_pk: int,
                              whole_yes: bool = False) -> tuple[bool, str]:
    """服务端兜底确认（模型漏调 confirm_reminder）能否下发；返回 (可否, 跳过原因)。

    判据必须与 ``tools._confirm_reminder`` 的候选集**完全一致**：直接复用
    ``pending_medicine_events`` / ``medicine_names``，不另写一份 SQL。两套判据
    迟早漂移，漂移的症状是「兜底确认了一条工具根本不会确认的提醒」。

    ``whole_yes``（2026-11-04 加）：老人说的是「确认 / 都吃了」这类不点名某一味
    的短应答。为 True 时**允许唯一候选挂多味药**，与 ``_confirm_reminder`` 的
    新口径保持一致（那边的长注释解释了为什么）。

    为什么原先收紧到「恰好 1 条候选 + 该条只有 1 味药」（2026-11-03）：兜底下发
    的是**空参数**设备确认 —— 设备挑的是"当前正在响铃的那条"，服务端无从核对。
    多条候选时它可能确认错的那条。这一条**现在仍然成立，一个字没松**：多条候选
    永远拒绝，因为那才是"确认错提醒"的真实风险。

    多味药那条松了，理由是它已经不再是"确认错对象"，只剩"粒度"问题，而粒度
    问题在固件侧早就用同一个取舍解决了（``elder_bridge.cc`` 的
    ``!CanStartDialog()`` 兜底直接确认整条，注释：「信息缺失不能让老人确认
    不了」）。服务端比设备更严 = 按键的可靠性取决于网络与设备状态，而用户的
    要求是「按键一定能确认」。
    """
    try:
        with SessionLocal() as db:
            rows = pending_medicine_events(db, device_pk)
            if not rows:
                return False, "没有待确认的服药提醒"
            if len(rows) > 1:
                return False, (f"有 {len(rows)} 条候选待确认，"
                               "设备无法确定老人说的是哪条")
            names = medicine_names(rows[0][1])
            if len(names) > 1 and not whole_yes:
                return False, (f"唯一候选挂着 {len(names)} 味药"
                               f"（{'、'.join(names)}），且老人没说是哪一味")
            return True, ""
    except Exception as exc:  # noqa: BLE001  查库失败就不兜底，绝不瞎确认
        return False, f"查询待确认提醒失败 {type(exc).__name__}: {exc}"


#: 命中弱判据时追加到 system 的复核提示。**只在命中那一轮注入** ——
#: 每轮都塞的话，模型会变得爱管闲事，正常聊天也想让设备待机。
_STANDBY_VERIFY_HINT: Final[str] = (
    "【本轮特别提示】老人这句话里出现了可能表示「结束对话」的说法。"
    "请你**结合上面的对话上下文**判断他的真实意图：\n"
    "  · 如果他确实是在让你停下 / 待机 / 别再说话了 → 调用 enter_standby，"
    "然后只说一句很短的道别（10 个字以内），不要再提问、不要再展开；\n"
    "  · 如果这句话不是对你说的（在跟别人讲话、在说电视里的人），"
    "或者只是在叙述事实（「他走了」「我明天要出去」）、只是随口应答"
    "（「不用了」「没事了」），→ **绝对不要**调用 enter_standby，"
    "照常自然地回答他，也不要提这件事。")

#: 告别话术。**只有一句**：老人已经不想聊了，多说一个字都是骚扰。
_DISMISS_REPLY: Final[str] = "好的，您先忙，有需要再叫我。"

#: 闸门拦住假成功句之后，改念给老人的话。工具返回里通常自带一句人话
#: （"没听出具体时间…请老人像「明天早上八点」那样再说一遍"），优先用它；
#: 拿不到时才用这句兜底。
_CLAIM_FALLBACK: Final[str] = "我没太听清，您再说一遍具体时间好吗？"

#: 「复述时间」对账闸门（2026-10-29 真机事故后加）
#: ---------------------------------------------------------------
#: 事故：老人说「今天中午十二点四十五提醒我下楼」。模型把 ``title`` 填成
#: 「提醒」、把 ``when`` 填成「12:55」，**两个参数都错**，可它复述给老人的是
#: 「好的，我给您记着，十二点四十五提醒您下楼」—— 复述正确，落库错误。
#: 结果 12:45 什么都没响，12:55 响了一条叫「提醒」的提醒。老人对着一条不知道
#: 是什么的提醒按了确认，然后认定"设备不准"。
#:
#: 上面那道 ``_SUCCESS_CLAIM_RE`` **拦不住它**：那道闸门只在工具**失败**时启动，
#: 而这次工具是"成功"的 —— 它忠实地存了模型给的错参数。所以需要第二道判据：
#: 把模型嘴里说的钟点，和工具真正落库的钟点对账。
#:
#: 判据刻意做成「**只要有一个钟点对上就放行**」，不是"必须全对上"：老人常会
#: 听到「现在是十二点四十三，我给您定十二点四十五」这种一句话里带两个钟点的
#: 播报，全都要对上会把正确播报也拦掉。宁可漏，不可误拦 —— 误拦会让老人听到
#: 一句莫名其妙的反问，比漏掉更难解释。
_CLOCK_MISMATCH_FALLBACK: Final[str] = (
    "我记的是{clock}，您听对不对？要是不对，您再跟我说一遍。")


# ============================================================ 情绪表情（F4）
#
# 为什么用 [情绪词] 标记而不是上游的 emoji 字符
# --------------------------------------------
# 上游小智（core/utils/textUtils.py）让模型在回复里夹带一个 emoji，服务端
# 取第一个 emoji 映射成情绪名发给设备。我们不照抄，有两个具体原因：
#   ① Edge TTS 对 emoji 字符的处理不可预期（有的跳过、有的念"表情"），
#      而我们的 TTS 链是先合成再分帧，出问题整句都要重来；
#   ② emoji 在 UTF-8 里是多字节，切片/正则稍不留神就切出乱码，而
#      「老人听到乱码」是本项目最贵的故障形态之一。
# 改用行首 [情绪词] 标记：ASCII 方括号 + 固定词表，正则简单、TTS 无歧义。
#
# 词表与设备端内置表情**逐一对应**（firmware 的 Twemoji32/64 各 21 个，
# 见 display/lvgl_display/emoji_collection.cc 的 AddEmoji 列表）—— 两边
# 任何一处增删都会让对方收到不存在的情绪名，设备侧查不到图就静默回落
# FontAwesome，表现为"换了 prompt 但表情不变"。改词表必须同时改固件。
_EMOTION_NAMES: Final[frozenset[str]] = frozenset({
    "neutral", "happy", "laughing", "funny", "sad", "angry", "crying",
    "loving", "embarrassed", "surprised", "shocked", "thinking", "winking",
    "cool", "relaxed", "delicious", "kissy", "confident", "sleepy", "silly",
    "confused",
})

#: 行首情绪标记：[情绪词]，只认词表内的（大小写不敏感）。**不在词表内的
#: 方括号内容一律不剥**—— 那可能是模型写的正常文字（如"[笔记]"），剥了
#: 就是往老人嘴里塞内容。宁可多留一个标记，不能少说一个字。
_EMOTION_TAG: Final[re.Pattern[str]] = re.compile(r"^\[([a-zA-Z]+)\]")


def _extract_emotion(sentence: str) -> tuple[str, str]:
    """从一句话开头剥出情绪标记。返回 ``(emotion, 剩余文本)``。

    ``emotion`` 是词表内的名字；没有标记或不在词表内时返回 ``"neutral"``
    **且原文照返**（不剥）。调用方把 emotion 发给设备、把剩余文本送 TTS。
    """
    m = _EMOTION_TAG.match(sentence)
    if m is None:
        return "neutral", sentence
    name = m.group(1).lower()
    if name not in _EMOTION_NAMES:
        return "neutral", sentence
    return name, sentence[m.end():]


def _speech_clean(sentence: str) -> str:
    """去掉 Markdown 噪音，返回**可以直接念**的文本。"""
    s = _MD_HEADING.sub("", sentence)
    s = _MD_BULLET.sub("", s)
    s = _MD_NOISE.sub("", s)
    return s.strip()


def _speech_block_reason(sentence: str) -> str:
    """判断一句话能不能念给老人听。返回 "" 表示可播，否则返回原因标签。

    标签会进日志与埋点（``dropped_sentences``），用来回答
    "这轮为什么没声音"——只有布尔值的埋点在排查时等于没有。
    """
    if _CODE_IDENT.search(sentence):
        return "code_ident"
    if _TOOL_TALK.search(sentence):
        return "tool_talk"
    if _THIRD_PERSON.search(sentence):
        return "third_person"
    if _SELF_PLAN.search(sentence):
        return "self_plan"
    if _REASONING_TRACE.search(sentence):
        # 用户就是靠日志定位"思考被念出来"这件事的：只记一个标签，下次还得
        # 让模型再跑一遍才知道它当时想了什么。截到 120 字，够定位又不刷屏。
        log.warning("拦下疑似思考过程（reasoning），不播报：%r", sentence[:120])
        return "reasoning"
    return ""


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------- 语音侧用量记账
# 为什么 ASR / TTS 的记账单独抽一个函数
# ------------------------------------
# 1. **tokens 恒为 0**：这两家按次 / 按秒计费，没有 token 口径。用户端看到的
#    是「语音识别 · N 次」「语音合成 · N 次」，次数才是唯一有意义的口径，
#    所以 ok / elapsed_ms 必须准（失败也要落一行，否则"调用过但失败"与
#    "从没调用过"无法区分）。
# 2. **必须在音频通路之外写库**：SQLite 的 commit 在极端情况下会等到
#    busy_timeout（15s），直接写在 `_stream_sentence` 里就是"说话说到一半
#    卡住"——用户对这块的硬要求是「声音现在正常，千万不要再修出破音、卡顿」。
#    所以 TTS 的记账是「先收集、后落库」（见 `_ReplySpeaker._tts_calls`
#    与 `_record_usage` 里的 flush），ASR 的记账落在**已有的线程池**里。
# 3. 失败只 log.warning：记账是旁路，绝不能影响发声。
def _record_voice_call(db: Any, *, usage: str, device_id: int | None,
                       user_id: int | None, agent_id: int | None,
                       session_id: str = "", ok: bool = True,
                       error: str = "", elapsed_ms: int = 0) -> None:
    """落一行 ASR/TTS 的调用流水（source="voice"，tokens 记 0）。"""
    try:
        provider_name = ""
        model = ""
        # 厂商名只是**展示**用：真正落到哪条配置由 asr_tts 内部按序兜底决定，
        # 而它没有把命中的 row 返回出来。这里取优先级最高的那条（正常情况
        # 就是实际使用的那条），不为了一行记账去改音频模块的返回值签名 ——
        # 改签名的风险远大于"回落时厂商名不准"这一点点收益。
        try:
            from ..llm_gateway import resolve_row
            row = resolve_row(db, usage)
            if row is not None:
                provider_name = str(getattr(row, "name", "") or "")
                model = str(getattr(row, "model", "") or "")
        except Exception:  # noqa: BLE001  取不到厂商名不影响记账本身
            pass
        usage_svc.record_model_usage(
            db, source="voice", usage=usage,
            device_id=device_id, user_id=user_id, agent_id=agent_id,
            provider_name=provider_name, model=model,
            request_id=usage_svc.new_request_id(),
            session_id=session_id, ok=ok, error=error[:255],
            elapsed_ms=elapsed_ms,
        )
    except Exception as exc:  # noqa: BLE001  记账失败绝不能影响发声
        log.warning("语音侧用量记账失败（不影响音频）：%s: %s",
                    type(exc).__name__, exc)


def _flush_tts_usage(db: Any, *, speaker: Any, dev: Any,
                     session_id: str = "") -> None:
    """把 speaker 收集的 TTS 调用清单落库，并清空清单（**同步**，勿在音频通路上调）。

    TTS 是流式的：一句一次 ``tts_stream``、一次合成几十个分片。所以
    "整句一条"的粒度在这里（``speaker._tts_calls`` 每句 append 一项），
    而不是每个分片一条 —— 分片级记账既没有意义，也会把音频通路拖垮。
    """
    calls = getattr(speaker, "_tts_calls", None)
    if not calls:
        return
    for call in list(calls):
        _record_voice_call(
            db, usage="tts", device_id=dev.id, user_id=dev.owner_id,
            agent_id=dev.agent_id, session_id=session_id,
            ok=bool(call.get("ok", True)),
            error=str(call.get("error", "")),
            elapsed_ms=int(call.get("elapsed_ms", 0)),
        )
    calls.clear()


def _pcm_stats(pcm: bytes) -> tuple[float, float]:
    """粗略统计一段 16kHz/S16LE PCM 的 (RMS, 峰值)，都归一到 0~1。

    **抽样子采样**（最多 4000 个点）：这段音频可能有 1MB，逐样本求和会在
    事件循环上占几十毫秒；而这里只是打一行日志，够看量级就行。

    为什么需要这两个数（2026-11-03 深夜）：ASR 返回空时日志只有
    「累计语音 4680ms」，分不清是"音频太小"（rms 0.001 级）、"全是噪声"
    （rms 0.02 但峰值很低）、还是"削波失真"（峰值贴着 1.0）。
    """
    import array

    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return 0.0, 0.0
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    if not samples:
        return 0.0, 0.0
    step = max(1, len(samples) // 4000)
    total = 0.0
    count = 0
    peak = 0
    for i in range(0, len(samples), step):
        v = samples[i]
        total += v * v
        count += 1
        av = v if v >= 0 else -v
        if av > peak:
            peak = av
    if count == 0:
        return 0.0, 0.0
    return (total / count) ** 0.5 / 32768.0, peak / 32768.0


def _asr_dump_dir() -> "Path | None":
    """落盘目录：库文件所在目录（容器里就是 /data，与 elder.db 同一个卷）。

    取不到（非 sqlite / 目录不存在）就返回 None，调用方跳过落盘 ——
    诊断手段失效绝不能影响一轮对话。
    """
    from pathlib import Path

    url = str(getattr(config, "DATABASE_URL", "") or "")
    if not url.startswith("sqlite"):
        return None
    raw = url.split("///", 1)[-1]
    if not raw or raw == ":memory:":
        return None
    parent = Path(raw).parent
    return parent if parent.is_dir() else None


def _join_pcm(pcm: "bytes | list[bytes]") -> bytes:
    """统一成 bytes。两处调用方天然给不同型：``_run_turn`` 给的是已拼好的
    bytes，看门狗分支手里只有 ``_pcm`` 帧列表。**不能只在一边做转换** ——
    两处各写一份必然分叉（本仓踩过：函数返回 list 却被当 bytes 用）。
    """
    if isinstance(pcm, (bytes, bytearray)):
        return bytes(pcm)
    return b"".join(pcm)


def _pcm_fingerprint(pcm: "bytes | list[bytes]", speech_ms: int) -> tuple[str, float]:
    """一行音频指纹：(可读摘要, 峰值)。峰值单独返回给调用方做闸门判断，
    免得为了取一个数把 1MB 音频再统计一遍。"""
    raw = _join_pcm(pcm)
    rms, peak = _pcm_stats(raw)
    summary = ("rms=%.4f peak=%.3f 时长=%.1fs 语音=%dms"
               % (rms, peak, len(raw) / 32000.0, speech_ms))
    return summary, peak


# 未裁剪音频至少要是裁剪后的这么多倍，重试才有意义。
#
# 1.5 这个口径来自 2026-11-04 12:43 的生产现场（三次连续空识别）：
#   366 帧 → 30 帧（12.2×）、244 帧 → 62 帧（3.9×）、114 帧 → 27 帧（4.2×）
# 都远超 1.5。反过来，只裁掉头尾几十毫秒时比值≈1.0，重试等于把**同一段**
# 音频再送一遍 —— 白花一次调用、白等一个来回，还一定还是空。
_ASR_RETRY_MIN_RATIO: Final[float] = 1.5

# 重试的时长上限：超过这个长度就不再重试。
# 理由不是"太长会错"，而是"太长会慢"——老人等 60 秒音频跑完 ASR 才听到
# 「我没听清」，比立刻听到更糟；而且多数厂商对单段时长本来就有硬上限。
_ASR_RETRY_MAX_SEC: Final[float] = 60.0


def _should_retry_asr_untrimmed(untrimmed: "bytes | None",
                                trimmed_len: int) -> bool:
    """ASR 空识别后，要不要拿**未裁剪**的原始音频再试一次。

    只判"有没有更多东西可听"，不判业务状态：冷却期 / 已待机 / 连续空识别
    阈值全部由 ``_handle_empty_asr`` 自己管（那是唯一出口，见那里的说明）。
    """
    if not untrimmed:
        return False
    if len(untrimmed) > int(_ASR_RETRY_MAX_SEC * 32000):
        return False
    if trimmed_len <= 0:
        # 裁剪把整段都判成静音了 —— 恰恰是"最该拿原音频再试一次"的情形。
        return True
    return len(untrimmed) >= trimmed_len * _ASR_RETRY_MIN_RATIO


def _save_dump(pcm: "bytes | list[bytes]", tag: str, session_id: str) -> str:
    """落一个 ``asr_<tag>_<时间>_<会话8位>.wav``，返回文件名；失败返回空串。

    轮换用 ``asr_*.wav`` 而不是 ``asr_empty_*.wav``：两种 tag 共用一个配额，
    否则"空识别"和"窗口超时"各留 3 个，磁盘上翻倍且更难找。
    """
    raw = _join_pcm(pcm)
    try:
        folder = _asr_dump_dir()
        if folder is None:
            return ""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = folder / ("asr_%s_%s_%s.wav" % (tag, stamp, session_id[:8]))
        with open(path, "wb") as fh:
            fh.write(asr_tts.wav_header_bytes(len(raw), 16000))
            fh.write(raw)
        # 只留最近 N 个：这是排障证据，不是录音档案。
        olds = sorted(folder.glob("asr_*.wav"))
        keep = max(1, int(getattr(config, "ASR_DUMP_KEEP", 3)))
        for stale in olds[:-keep]:
            try:
                stale.unlink()
            except OSError:
                pass
        return path.name
    except Exception as exc:  # noqa: BLE001  落盘失败不影响对话
        log.warning("ASR 音频留证落盘失败（%s）：%s: %s",
                    tag, type(exc).__name__, exc)
        return ""


def _dump_empty_asr(pcm: "bytes | list[bytes]", speech_ms: int,
                    session_id: str) -> str:
    """ASR 返回空时留证：落一个 wav + 返回一行可读的统计摘要。

    只在「VAD 认为说了话（≥ ASR_DUMP_MIN_SPEECH_MS）且确实为空」时调用，
    正常对话零开销。落盘失败一律吞掉（诊断不能影响对话）。
    """
    summary, _ = _pcm_fingerprint(pcm, speech_ms)
    if not getattr(config, "ASR_DUMP", False):
        return summary
    if speech_ms < getattr(config, "ASR_DUMP_MIN_SPEECH_MS", 800):
        return summary
    name = _save_dump(pcm, "empty", session_id)
    return summary + (" 已存 %s" % name if name else "")


#: 窗口超时留证的峰值闸门（约 -40dBFS，相当于底噪 0.002 的 5 倍）。
_DUMP_TIMEOUT_PEAK: Final[float] = 0.01


def _dump_listen_timeout(pcm: "bytes | list[bytes]", speech_ms: int,
                         session_id: str, window_ms: int) -> str:
    """收听了整个窗口、VAD 却一帧都没判成语音 —— 但老人说他明明说了话。

    这是 2026-11-03 深夜现场里**最要命的一种**：60 秒窗口累计语音 0ms，
    服务端按设计"重新武装而不送 ASR"（见看门狗注释里 2026-10-21 那个
    "音箱永远自言自语"的事故），日志里只有一行数字，**一点证据都不留**。
    如果下次复现还是这一种，`_dump_empty_asr` 那套完全抓不到 —— 它的闸门
    是"语音量 ≥ 800ms"，这里恒为 0。

    所以闸门换成**峰值**：房间真安静时 peak 只有 0.001 级，落了也是白占
    磁盘（60 秒 wav 约 1.9MB，且每分钟一次）；一旦 peak ≥ 0.01 就说明这段
    音频里确实有可听的东西，VAD 却判成了静音 —— 那正是要抓的两种可能：
    门限定得太高，或者设备送上来的音频被衰减过。
    """
    summary, peak = _pcm_fingerprint(pcm, speech_ms)
    summary = "窗口=%dms %s" % (window_ms, summary)
    if not getattr(config, "ASR_DUMP", False):
        return summary
    if peak < _DUMP_TIMEOUT_PEAK:
        return summary + "（峰值过低，未落盘）"
    name = _save_dump(pcm, "timeout", session_id)
    return summary + (" 已存 %s" % name if name else "")


class VoiceSession:
    """一次 WS 连接的会话。**所有公开方法都是 async**，内部自行切线程池。"""

    def __init__(self, websocket: WebSocket, protocol_version: int = 1) -> None:
        self.ws: WebSocket = websocket
        self.protocol_version: int = protocol_version
        self.session_id: str = ""
        self.dev: Any = None
        self.state: str = "idle"

        # 上行音频缓存（Opus 裸包，未解码）
        self._frames: list[bytes] = []
        # 已解码 PCM（16kHz），按帧追加，finalize 时整段交给 ASR
        self._pcm: list[bytes] = []
        self._speech_ms: int = 0            # 累计有语音的时长
        self._silence_ms: int = 0           # 连续静音时长
        # 「连续语音段」的两个值（见 config.VOICE_MIN_SPEECH_RUN_MS 的说明）。
        # _speech_run_ms 是**当前**这一段有多长，静音帧一到就清零；
        # _max_speech_run_ms 记本轮最长的那一段。
        # 光看 _speech_ms（累计）挡不住环境噪声：60 秒里零星几下关门、
        # 电视声加起来就过 300ms，于是"一句都没说"照样送 ASR → 念"没听清"。
        self._speech_run_ms: int = 0
        self._max_speech_run_ms: int = 0

        # ---- 服务端 VAD 状态（老人语速慢 / 轻声 / 远场，见常量区注释）----
        self._noise_floor: float = _NOISE_FLOOR_INIT   # 房间底噪跟随值
        self._ms_since_loud: int = 10 ** 9             # 距最后一个 enter 级帧多久
        self._max_closed_pause_ms: int = 0             # 本轮"停顿后又继续"的最长停顿
        # 本轮首个/末个被判为语音的帧序号（_pcm 下标，-1 = 还没说过话）。
        # 送 ASR 前按这个区间裁头尾静音，见 _TRIM_PAD_FRAMES 注释。
        self._first_speech_frame: int = -1
        self._last_speech_frame: int = -1
        # 本轮所有被判为语音的帧下标（升序，_pcm 下标）。first/last 只够
        # 裁头尾，句中夹的十几秒静音全留在里面（2026-11-01 真机：838 帧
        # 只裁到 259 帧，其中真语音 540ms，ASR 返回空）。这张表由
        # _mark_speech_frame 顺手记，供 _trim_silence_edges 分段拼接。
        self._speech_idx: list[int] = []

        # 会话内对话历史（[{"role":..,"content":..}]），仅内存，不落库；
        # 跨连接的历史由记忆模块管（recent_turns），这里不重复存一份。
        self._history: list[dict[str, str]] = []

        # 打断标志。asyncio 单线程事件循环里 bool 读写是原子的，
        # 不需要锁；但所有 await 点都要重新读它（见 _speak_reply 的循环）。
        self._abort: bool = False

        # 本轮是不是被**老人抢话**打断的（2026-10-31）。
        # 与 ``_abort`` 分开的理由：``_abort`` 还会被"连接断开"置位
        # （run() 的 finally），那种情况不是反馈信号，记成"老人纠正"
        # 就是往画像里灌假数据。只有真正的人声打断才置这一位。
        self._turn_barged_in: bool = False

        # 当前这一轮对话的 task。**必须与收包主循环解耦**，否则播报期间
        # 读不到上行消息、abort 失效（见 run() 里的注释）。
        self._turn_task: asyncio.Task | None = None

        # ---- 唤醒应答（2026-10-25）----
        # 唤醒词到达后起一个延时任务，补一句「你好，<称呼>，要聊点什么？」。
        # 为什么是 task 而不是在 _on_listen 里直接 await：await 会卡住收包
        # 主循环，那几百毫秒里 abort / listen stop 全都读不到。
        self._greet_task: asyncio.Task | None = None

        # ---- 全双工打断（F2）状态 ----
        # 播报期的人声累计/静音累计。**独立于 _speech_ms/_silence_ms**：
        # 那两个是"收音轮"的状态，混用会让下一轮的门限和停顿自适应被
        # 播报期的能量污染（表现为轮询结束判据错乱）。
        self._barge_speech_ms: int = 0
        self._barge_silence_ms: int = 0
        # 本轮播报开始时刻（开播 grace 期由此算起）。0 = 还没开始播。
        self._speaking_started_at: float = 0.0
        # 打断交接保护：barge 触发后置位，_finalize 消费交接音频时清除。
        # 为真的是防止随后那次 _rearm_listening 把 _pcm 里老人刚说的话
        # 当"新一轮"清掉 —— 链条见 _rearm_listening 里的注释。
        self._barge_handover: bool = False

        # 「裁剪把话切没了」的兜底素材：本轮的**未裁剪** PCM（bytes）。
        # 只有真的裁剪过才非 None（没裁就等于重试同一段音频，纯浪费一次调用）。
        # ASR 返回空时用它重试一次，见 _resolve_empty_asr 的说明。
        self._pcm_untrimmed: bytes | None = None

        # 设备上报的收听模式（realtime/auto/manual）。连续对话模式下
        # 设备播完后继续听、不再喊唤醒词，服务端必须能自己重新武装监听
        # （见 _rearm_listening 的死锁说明）。
        self._listen_mode: str = ""

        # ---- 声纹说话人（F3）状态 ----
        # 已向 LLM "介绍过"的说话人（user 消息包成 {"speaker","content"}
        # 的那一轮记一个）。复刻上游 xiaozhi-esp32-server 的
        # introduced_speakers / system_introduced_speakers 语义：
        # 首轮让模型自然称呼一次，之后降为纯文本 —— 否则每轮上下文里都
        # 出现名字，模型会反复称呼，老人听着像复读机。
        self._introduced_speakers: set[str] = set()
        self._system_speakers: set[str] = set()
        # 本轮认出的说话人显示名（None = 没开声纹 / 没认出 / 没登记）。
        # 只为日志与埋点存在，不参与任何判据。
        self._current_speaker: str | None = None

        # 静音看门狗 task 的单实例引用。重新武装前先取消旧的，
        # 否则会出现两个看门狗同时等静音（重复触发一轮对话）。
        self._watchdog_task: asyncio.Task | None = None

        self._t0: float = 0.0               # 本轮 listen start 时刻

        # 最后一次"有效内容"的时刻（monotonic 秒）。锚点是 **ASR 认出非空
        # 文本** 那一刻（``_run_turn`` 里刷新），唤醒词也刷新一次（老人刚
        # 喊完唤醒词，那 120s 当然要从现在算起）。
        #
        # ⚠️ 与 ``_t0`` 的区别是这条设计的全部要点：``_t0`` 是"本轮收听
        # 窗口起点"，``_rearm_listening()`` 会重置它 ⇒ 用它做空闲判据的话，
        # 每 60s 自己清零一次，永远等不到超时（真机表现就是"一直在监听"）。
        # 本字段**只由有效内容刷新**，重新武装不碰它，所以能跨窗口累计。
        self._last_active_at: float = time.monotonic()

        # 「退下吧」冷却截止时刻（monotonic 秒）。非零且未到期时，
        # _on_binary 丢弃上行音频、_on_listen 不重新武装 ——
        # 老人在连续收听模式下喊"退下吧"，要真的能让他清静一会儿。
        # 到期后由 idle 态收到音频的那条兜底自动恢复。
        self._dismissed_until: float = 0.0

        # ---- "服务端在忙、设备还在说"的丢帧计数（2026-11-03）----
        # 每轮清零，轮次结束时若非 0 打一行日志。用途只有一个：把
        # 「老人说没反应」拆成两半 —— 有数 = 设备听到了、服务端没接住
        # （thinking 态）；没数 = 设备根本没送上来（麦克风/聆听态问题）。
        # 这一区分靠日志之外的任何手段都做不到，而两种原因的修法完全不同。
        self._drop_thinking_frames: int = 0

        # ---- 连续空识别计数（2026-11-03 用户实报）----
        # "老人已经说了不想聊，设备还在反复念『抱歉，我没听清』"的根因就是
        # 这个数字不存在：每一次空 ASR 都被当成**第一次**，于是永远念同一句。
        # 语义与清零时机见 ``_EMPTY_ASR_STANDBY_STREAK`` 的注释。
        self._empty_asr_streak: int = 0

        # ---- 「结束对话」意图复核（2026-10-22 用户需求）----
        # 本轮命中了待机关键词、且走"交大模型复核"那条路时为非空（档位名）。
        # 生命周期只在**一轮**之内：
        #   * ``_run_turn`` 开头清空 → 命中时置位；
        #   * 模型正常答完（含被 abort）→ 清空，表示"模型判了不是待机"；
        #   * 一轮失败（LLM 未配置/异常/空回复）→ **保持置位**，由
        #     ``_finalize`` 的 finally 兜底待机。老人说了"退下吧"，最差也
        #     得让音箱安静下来，不能因为模型挂了就继续念"我没听清"。
        self._dismiss_candidate: str = ""
        # 模型本轮调用了 enter_standby。**不能当场下发设备命令** —— 那一轮
        # 的告别语还没念（工具调用发生在生成回复之前），先切 idle 的话
        # 告别语会把设备顶回 speaking，最后又落回 listening（就是
        # _enter_standby 注释里那个坑）。所以只置标志，念完再切。
        self._standby_requested: bool = False

        # 本轮命中了"强确认"措辞（喝完了/吃过了…）。模型若没自己调
        # confirm_reminder，由服务端补一刀 —— 理由见 _CONFIRM_RE 上方注释。
        self._confirm_candidate: bool = False
        # 设备上「已响铃未确认」的服药提醒**清单**（每轮开头查一次，多行文本）。
        # 非空时 system 里会多一段状态说明 + 追问纪律，模型才知道该调
        # confirm_reminder、以及还剩哪一味没问。空串 = 不注入（行为与旧版一致）。
        self._pending_reminder: str = ""

        # 今天**已经播报过**的日程提醒清单（每轮开头查一次，多行文本）。
        # 日程类「播报即完成」，所以它不会出现在 _pending_reminder 里；但用户
        # 要求模型能主动问一句「您已经完成了吗？」，这份清单就是那个话头。
        # 空串 = 不注入（行为与旧版一致）。
        self._reminded_schedule: str = ""

        # 当前这一轮的播报器（_ReplySpeaker）。abort 时要能立刻丢掉它
        # 队列里还没发出去的音频帧 —— 否则老人已经喊停了，音箱还在
        # 把剩下的半句话说完。由 _ReplySpeaker.__init__ 写入、
        # finish()/close() 清空。
        self._speaker: _ReplySpeaker | None = None

        # ---- 服务端 → 设备 MCP 工具通道（JSON-RPC 2.0）----
        # 键是 JSON-RPC id（**必须是整数**：固件 mcp_server.cc 的
        # ParseMessage 用 cJSON_IsNumber 校验 id，字符串 id 会被直接丢弃），
        # 值是等回包的 Future。见 call_device_tool()。
        self._mcp_pending: dict[int, asyncio.Future] = {}
        self._mcp_seq: int = 0
        # 会话建立时「叫设备立刻同步一次配置」的后台任务（见 _kick_sync）。
        self._sync_task: asyncio.Task | None = None

    # ================================================================ 生命周期
    async def run(self) -> None:
        """主循环：鉴权 → hello → 收帧/收 JSON，直到连接断开。

        鉴权失败时在这里 close，**不抛异常**：WS 握手已经完成，
        抛出去只会变成 uvicorn 的 500 日志，设备侧看到的仍是断开，
        但服务端日志会误导排障。
        """
        try:
            if not await self._authenticate():
                return
        except Exception as exc:  # noqa: BLE001  鉴权期的异常同样不能崩进程
            log.warning("WS 鉴权异常：%s: %s", type(exc).__name__, exc)
            await self._safe_close(CLOSE_UNAUTHORIZED)
            return

        hello = {
            "type": "hello",
            "transport": "websocket",
            "session_id": self.session_id,
            "audio_params": {
                "sample_rate": config.VOICE_OUTPUT_SAMPLE_RATE,
                "frame_duration": config.VOICE_FRAME_DURATION_MS,
            },
        }
        # 传输层可改写自己的 hello（MQTT+UDP 传输要把 transport 改 "udp"
        # 并补 udp:{server,port,key,nonce} 段 —— 固件 mqtt_protocol.cc 的
        # ParseServerHello 只认 "udp"，udp 段缺一个字段都会让设备停在
        # 「等待服务器 hello」，10s 后报 SERVER_TIMEOUT）。
        # 用 getattr 探测而不是 isinstance：WebSocket 没有这个方法 ⇒
        # WS 路径行为零变化，且新传输不必改这里一行代码。
        rewrite = getattr(self.ws, "rewrite_hello", None)
        if callable(rewrite):
            hello = rewrite(hello) or hello
        await self._send_json(hello)
        log.info("[%s] 语音会话开始（设备 %s，协议 v%d）",
                 self.session_id, getattr(self.dev, "device_id", "?"),
                 self.protocol_version)

        # 会话建立 ⇒ 叫设备立刻同步一次配置（2026-11-01 用户要求）。
        #
        # 为什么是这里：灯效 / 形象 / 提醒写的是**设备侧**数据，只能由
        # `POST /api/device/sync` 下发，而设备空闲时靠 300 秒心跳拉取
        # （schemas.DeviceSyncOut.sync_interval_sec）。设备在**对话中**才连着
        # WS —— 这就是"唤醒设备可立即生效"唯一能兑现的时刻。不在这里叫一声，
        # 三端界面上那句"唤醒设备时立即生效"就是假的。
        #
        # ⚠️ 必须**后台跑**，绝不 await：hello 之后设备立刻开始推流，在这里
        # 挡住 2 秒 = 2 秒音频积压（用户红线：不许卡顿/破音）。
        self._sync_task = asyncio.create_task(self._kick_sync())

        try:
            while True:
                message = await self.ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if "bytes" in message and message["bytes"] is not None:
                    self._on_binary(message["bytes"])
                elif "text" in message and message["text"] is not None:
                    # ⚠️ 只 await 消息的**解析与分发**，绝不在这里 await 整轮对话。
                    # 整轮由 _start_turn 起成独立 task（本行的关键设计）。
                    # 早期版本是 `await self._on_json(...)` 直落到底，结果是
                    # 播报期间收不到任何上行消息 —— 而固件恰恰在**播报中**
                    # 发 abort（用户喊唤醒词要打断），于是打断功能整体失效
                    # 且不报任何错：abort 静静躺在内核缓冲区里，等整段播完
                    # 才被读到。断言「第 7 条 · 打断后不再收到后续句」是它的
                    # 唯一防线。
                    await self._on_json(message["text"])
        except WebSocketDisconnect:
            log.info("[%s] 设备断开", self.session_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] 会话异常：%s: %s", self.session_id,
                          type(exc).__name__, exc)
        finally:
            self.state = "closed"
            if self._sync_task is not None and not self._sync_task.done():
                # 会话没撑到同步请求发出就断了：留着它会在已断开的连接上
                # 发一条 JSON-RPC，等 2 秒超时后记一条误导性的 WARNING。
                self._sync_task.cancel()
            if self._watchdog_task is not None and not self._watchdog_task.done():
                # 会话结束后看门狗必须退：它 while state == "listening" 会
                # 自然退出，但显式取消更及时（少一个 0.2s 轮询的空转 task）。
                self._watchdog_task.cancel()
            if self._turn_task is not None and not self._turn_task.done():
                # 断开时把本轮标成打断：否则它还会继续往下发音频帧，
                # 对已断开的连接每帧抛一次异常（刷满日志且掩盖真因）。
                self._abort = True
                self._turn_task.cancel()
            # 节流协程挂在 _ReplySpeaker 上，由 finish()/close() 停。
            # 断开的那一轮可能正卡在 finish() 的等待里被连带取消 ⇒ 没人停它，
            # 它会带着 WebSocket 引用一直挂在进程里。这里补一刀
            # （close() 不发协议消息，对已断开的连接也无害）。
            sp = self._speaker
            if sp is not None:
                try:
                    await sp.close()
                except Exception:  # noqa: BLE001  清理路径不能反抛
                    log.debug("[%s] 断开时清理播报器失败", self.session_id,
                              exc_info=True)

    async def _authenticate(self) -> bool:
        """握手鉴权。返回 False 表示已 close，调用方应直接退出。"""
        header = self.ws.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token:
            await self._safe_close(CLOSE_UNAUTHORIZED)
            return False
        payload = await run_in_threadpool(decode_token, token)
        if not payload or payload.get("typ") != "device":
            # token 缺失/过期/被篡改/是用户 token —— 一律同一个码，
            # 不区分"格式错"与"过期"，避免把信息泄露给试探方。
            await self._safe_close(CLOSE_UNAUTHORIZED)
            return False
        try:
            dev_pk = int(payload.get("sub", ""))
        except (TypeError, ValueError):
            await self._safe_close(CLOSE_UNAUTHORIZED)
            return False

        def _load():
            with SessionLocal() as db:
                return db.get(_m.Device, dev_pk)

        dev = await run_in_threadpool(_load)
        if dev is None:
            await self._safe_close(CLOSE_UNAUTHORIZED)
            return False

        # 代次校验（与 deps.get_current_device 同一判据）：管理员解绑时
        # `token_epoch += 1`，旧 token 必须**连 WS 也开不了**。
        # 只在 HTTP 侧校验是不够的 —— WS 是真正读数据的通道，
        # 漏了这里等于「解绑后还能听老人说话」。
        if int(payload.get("ep", 0)) != int(getattr(dev, "token_epoch", 0) or 0):
            log.warning("设备 token 代次过期（已解绑/换绑）：dev_pk=%s", dev_pk)
            await self._safe_close(CLOSE_UNAUTHORIZED)
            return False

        # 设备身份校验（pk 复用，2026-10-20 P0）：**必须先查 token 自己声明的
        # 设备标识**，不能只查下面那段 header 比较。
        #
        # 为什么下面那段不够：`devices.id` 是裸 Integer 主键（无 AUTOINCREMENT），
        # 删掉当前最大行后 SQLite 会复用同一个 id；新设备的 header MAC 与它自己
        # 那一行的 device_id 天然一致 ⇒ **header 比较照样通过**，而 token 是
        # 上一家的旧 token。也就是说 header 比较只能挡住"拿 A 的 token 冒充 B 的
        # MAC"，挡不住"pk 被复用"。
        #
        # ⚠️ 与 deps.get_current_device 同一判据、同一坑：必须**双边 norm_mac**
        # 后比较（token 里是签发当时的列值，历史 token 可能是 `AA:BB:..`）。
        # 缺声明一律拒，不做兼容放行（该声明自初始提交起就有）。
        did = str(payload.get("device_id") or "")
        if not did or norm_mac(did) != norm_mac(str(getattr(dev, "device_id", "") or "")):
            log.warning("token 的 device_id 声明与设备行不符（pk 被复用？）："
                        "dev_pk=%s claim=%r db=%r",
                        dev_pk, did, getattr(dev, "device_id", None))
            await self._safe_close(CLOSE_DEVICE_MISMATCH)
            return False

        # 跨设备盗用 token：token 本身有效，但 Header 里的 Device-Id 与
        # 签发的不是同一台。**必须比对** —— 否则偷到一台设备的 token
        # 就能冒充任意 MAC（OTA 只认 Device-Id，token 与 MAC 分离时
        # 身份链就断了）。
        #
        # ⚠️⚠️ 必须按**归一化**后比较（2026-10-20）：`Device.device_id` 入库
        # 已统一成不带冒号的小写形式（`aabbccddeeff`，见 app/macs.py），
        # 而固件的 `Device-Id` 头是 `aa:bb:cc:dd:ee:ff`。这里若继续用裸串比，
        # **每一台设备一开口就被 4001 关掉** —— 而且现象是「设备能连上、
        # 一说就断」，与鉴权失败长得一模一样，极难定位。变异实证：
        # 把下面这行改回 `header_mac != dev.device_id`，`smoke_voice` 必须红。
        header_mac = (self.ws.headers.get("device-id") or "").strip()
        if header_mac and norm_mac(header_mac) != norm_mac(dev.device_id):
            log.warning("Device-Id 与 token 不匹配：header=%r token=%r",
                        header_mac, dev.device_id)
            await self._safe_close(CLOSE_DEVICE_MISMATCH)
            return False

        self.dev = dev
        import uuid
        self.session_id = uuid.uuid4().hex
        return True

    async def _safe_close(self, code: int) -> None:
        try:
            await self.ws.close(code=code)
        except Exception:  # noqa: BLE001  对端已断开时 close 会抛，无所谓
            pass

    # ================================================================ 发送
    async def _send_json(self, obj: dict[str, Any]) -> None:
        import json
        await self.ws.send_text(json.dumps(obj, ensure_ascii=False))

    async def _kick_sync(self) -> None:
        """会话建立时叫设备**立刻**同步一次配置。失败一律静默。

        与 ``voice/tools.py`` 的 ``_request_device_sync`` 是同一个工具、同一套
        降级口径（失败只记 INFO，周期心跳兜底），区别只在触发时机：那边是
        "提醒表刚变了"，这里是"设备刚连上来，趁它连着把积压的配置取走"。

        **不抛异常**：这是 fire-and-forget task，抛出去只会变成
        "Task exception was never retrieved" 的噪声日志，而失败本身
        完全可接受（老固件没注册这个工具 / 设备没答上来）。
        """
        try:
            await self.call_device_tool(_DEVICE_SYNC_TOOL, {}, _DEVICE_SYNC_TIMEOUT)
            log.info("[%s] 会话建立，已叫设备立刻同步配置", self.session_id)
        except Exception as exc:  # noqa: BLE001  老固件没这个工具 / 设备没答
            log.info("[%s] 会话建立时叫设备同步没成功（%s: %s），等心跳兜底",
                     self.session_id, type(exc).__name__, exc)

    async def call_device_tool(self, name: str, arguments: dict[str, Any] | None = None,
                               timeout: float = 3.0) -> Any:
        """调用设备侧的一个 MCP 工具，等它回包。

        设备是 MCP server（`initialize` / `tools/list` / `tools/call` 都由
        ``main/mcp_server.cc`` 实现），服务端只是 client。这条通道原先
        完全没建：``_on_json`` 收到设备的 mcp 消息只打 debug 就丢掉，
        所以「老人说吃过了 → 设备确认服药」这个闭环是断的。

        ⚠️ id 必须是**整数**：固件 ``McpServer::ParseMessage`` 对 id 做
        ``cJSON_IsNumber`` 校验，字符串 id 会打一条 "Invalid id" 然后
        直接 return，服务端只会看到超时 —— 这类失败在日志里长得像
        「设备没响应」，极难查。回包时固件用 ``std::to_string(id_int)``
        原样带回，所以按 int 建索引是安全的。

        超时抛 ``TimeoutError``（而不是返回 None）：调用方必须能区分
        「设备明确答了 false」和「设备没答上来」，前者是业务结果，
        后者要提示老人改用按键。
        """
        self._mcp_seq += 1
        rid = self._mcp_seq
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._mcp_pending[rid] = fut
        try:
            await self._send_json({
                "type": "mcp",
                "payload": {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                },
            })
            # 设备侧工具回调是同步跑的（取药提醒确认只改内存 + 落 NVS），
            # 3 秒足够；再长说明设备掉线或 WS 卡住，早点失败好让 LLM
            # 换一条话术（「你按一下设备上的按键」）。
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._mcp_pending.pop(rid, None)

    async def _send_audio(self, payload: bytes) -> None:
        """发一帧下行音频，按协商的 Protocol-Version 封帧。

        帧长由调用方保证（encode_opus 逐帧出）。timestamp 填 0：
        v2 的 timestamp 注释说"用于服务端 AEC"，我们**没有 AEC**
        （没有参考信号），填真实时间反而可能让设备侧的 AEC 误判为
        有参考信号可用。0 = "无时间戳"是明确语义。
        """
        if self.protocol_version == 2:
            import struct
            head = struct.pack(">HHIII", 2, 0, 0, 0, len(payload))
            await self.ws.send_bytes(head + payload)
        elif self.protocol_version == 3:
            import struct
            head = struct.pack(">BBH", 0, 0, len(payload))
            await self.ws.send_bytes(head + payload)
        else:
            await self.ws.send_bytes(payload)

    async def _send_alert(self, message: str) -> None:
        """发一条 alert：设备会震动 + 显示 + 朗读。

        用于**不走 TTS 也要告知**的场景（配额用尽 / LLM 未配置）。
        刻意不做"先 TTS 再 alert"：TTS 不可用时 alert 是唯一的告知通道，
        而它本身就能出声（固件 Alert() 走 sounds）。
        """
        await self._send_json({"type": "alert", "status": "info",
                               "message": message, "emotion": "neutral"})

    # ================================================================ 上行
    def _on_binary(self, data: bytes) -> None:
        """收一个二进制帧。type==1（JSON over binary）按 JSON 解析。"""
        if self._dismissed_until and time.monotonic() < self._dismissed_until:
            # 「退下吧」冷却期：老人已经明确不想聊了，realtime 模式下
            # 设备仍在推流，这里直接丢弃，既不送 ASR 也不重新武装。
            # 到期后由下面 idle 态那条兜底自动恢复。
            return
        if self.state == "idle" and self._continuous():
            # 连续对话会话里，设备还在推流但服务端已回 idle —— 就是
            # _rearm_listening 注释里那个死锁。设备以为我们在听，我们
            # 以为会话结束了。⇒ 把上行音频当作设备重新宣布 listening。
            #
            # ⚠️ 2026-11-03 **刻意保留**这条自动恢复（没有改成"只认 listen
            # start / 真实人声才重新武装"）：待机那次 ``self.elder.standby``
            # 是 3 秒超时的 best-effort，失败只 log（见 _enter_standby）——
            # 那时设备仍停在 realtime listening 且**不会重发 listen start**。
            # 若这里也不自动恢复，老人怎么喊都没人应（曾实测聋 164 秒），
            # 那个代价远大于"冷却过后多念一句没听清"。
            # 所以取折中：冷却刚过时清零连续空识别计数并打日志，让下一次
            # 空识别从"第 1 次"重新开始，而不是直接跳到第 3 次待机。
            if self._dismissed_until:
                log.info("[%s] 待机冷却已过，上行音频重新武装监听"
                         "（state=%s，连续空识别计数清零）",
                         self.session_id, self.state)
                # 归零 = 标记"这次冷却已经消费掉"，后面的帧不再重复打日志。
                self._dismissed_until = 0.0
            else:
                log.info("[%s] idle 态仍收到上行音频，按连续对话重新武装监听",
                         self.session_id)
            self._rearm_listening()
        if self.state == "speaking" and self._continuous():
            # 全双工打断（F2）：realtime 模式设备播报时仍在推流（AEC 已把
            # 播放声抵掉）。收下来只为判"有没有人想插话"—— 有就停嘴改听。
            # 这些帧同时留在 _pcm 里：打断后老人刚说的就是下一轮的输入。
            # ⚠️ 必须先剥头再进这个分支：payload 的赋值在任何使用之前。
            # 曾经写在分支后面 ⇒ speaking 期每帧一个 UnboundLocalError，
            # 会话整段死掉，而客户端症状只是"没有 tts stop"（连接被悄悄
            # 掐断），极难定位。剥头失败（畸形帧）时本帧整个丢弃，与
            # listening 路径同口径。
            payload = self._strip_binary_header(data)
            if payload is not None:
                self._feed_barge_in(payload)
            return
        if self.state != "listening":
            # 非 listening 态收到的音频：设备在播报时也可能继续推流
            # （固件不停止麦克风）。丢弃即可，不回错误 —— 回错误会让
            # 设备以为协议出错而重连。
            #
            # ⚠️ 2026-11-03 用户实报「偶发性的我说话设备完全没有识别到」：
            # 这条**静默丢弃**正是最可能的现场之一 —— ``thinking``（ASR +
            # LLM + TTS 首帧，实测 3~8 秒）期间老人说的话全被扔掉，且日志里
            # 一个字都没有，事后只能靠猜。所以现在计数，并在轮次结束时打一行
            # 汇总：有数就是"设备听到了但服务端在忙"，没数才轮到查麦克风。
            if self.state == "thinking":
                self._drop_thinking_frames += 1
            return
        payload = self._strip_binary_header(data)
        if payload is None:
            return
        # 防御性：固件不会发 type==1，但实现要按协议来
        if self.protocol_version == 2 and len(data) >= 4:
            import struct
            _ver, ftype = struct.unpack(">HH", data[:4])
            if ftype == 1:
                log.debug("收到 type==1 的二进制帧（按 JSON 处理）：%r", payload[:120])
                return
        self._frames.append(payload)
        # 立刻解码并做静音检测：等 finalize 再一次解会丢掉时间信息，
        # 而服务端静音兜底需要"每帧的能量"才能判断连续静音。
        try:
            pcm = opus_codec.decode_opus(payload, 16000, 1)
        except opus_codec.OpusUnavailableError:
            # 无 libopus 时静音兜底失效，但协议链路仍可用（帧照收），
            # 由设备端 listen stop 来 finalize。冒烟脚本据此判定 SKIP。
            log.debug("无 libopus，跳过本帧解码（静音兜底失效）")
            return
        self._pcm.append(pcm)
        self._feed_vad(pcm, config.VOICE_FRAME_DURATION_MS)

    # ---------------------------------------------------------------- VAD
    def _feed_vad(self, pcm: bytes, frame_ms: int) -> None:
        """喂一帧解码后的 PCM，更新语音/静音累计（老人向的三件套）。

        判定顺序（每帧）：
          1. 更新底噪跟随值（快降慢升，语音能量几乎不抬它）；
          2. RMS >= enter 门限 ⇒ 高能量帧：算语音，静音清零，记录"刚说过话"；
          3. 否则 RMS >= keep 门限 且距最后一个高能量帧不超过软语音期限
             ⇒ 软语音帧：算语音（句中轻声音节不掉出语音态）；
          4. 否则 ⇒ 静音帧；若这帧结束了一段静音（后面又说话了），
             且长度够长，记入"最长句中停顿"供自适应用。
        """
        rms = opus_codec.pcm_rms(pcm)
        # 本帧在 _pcm 里的下标（调用方先 append 再喂进来）。语音帧区间用于
        # ASR 前的头尾静音裁剪，见 _TRIM_PAD_FRAMES。
        frame_idx = len(self._pcm) - 1
        # 1) 底噪跟随。慢升（0.1%/帧）保证 5 秒的正常说话也只把底噪抬一点点，
        #    不会把门限顶到语音能量之上；快降（2%/帧）保证安静下来后迅速回落。
        if rms < self._noise_floor:
            self._noise_floor += (rms - self._noise_floor) * _NOISE_FLOOR_DOWN
        else:
            self._noise_floor += (rms - self._noise_floor) * _NOISE_FLOOR_UP

        enter = max(config.VOICE_SPEECH_RMS, self._noise_floor * _ENTER_FACTOR)
        keep = max(config.VOICE_SPEECH_RMS_KEEP, self._noise_floor * _KEEP_FACTOR)

        if rms >= enter:
            # 2) 高能量：闭合并记录刚才那段静音（若够长，算一次"句中停顿"）
            if self._silence_ms >= config.VOICE_PAUSE_MIN_MS:
                self._max_closed_pause_ms = max(self._max_closed_pause_ms,
                                                self._silence_ms)
            self._speech_ms += frame_ms
            self._speech_run_ms += frame_ms
            self._max_speech_run_ms = max(self._max_speech_run_ms,
                                          self._speech_run_ms)
            self._silence_ms = 0
            self._ms_since_loud = 0
            self._mark_speech_frame(frame_idx)
        elif rms >= keep and self._ms_since_loud <= config.VOICE_SOFT_GRACE_MS:
            # 3) 软语音：开口后的轻声延续。**不刷新 _ms_since_loud** ——
            #    刷新它等于让持续底噪无限续期，会话永远不结束。
            self._speech_ms += frame_ms
            self._speech_run_ms += frame_ms
            self._max_speech_run_ms = max(self._max_speech_run_ms,
                                          self._speech_run_ms)
            self._silence_ms = 0
            self._ms_since_loud += frame_ms
            self._mark_speech_frame(frame_idx)
        else:
            # 4) 静音
            self._silence_ms += frame_ms
            self._ms_since_loud += frame_ms
            # 连续语音段到此为止。**累计值不清**（_speech_ms 仍代表本轮
            # 一共说了多久），只清"这一段连续说了多久"。
            self._speech_run_ms = 0

    def _silence_threshold_ms(self) -> int:
        """本轮的自适应静音阈值（见常量区注释的第三件套）。"""
        adaptive = (self._max_closed_pause_ms * config.VOICE_PAUSE_FACTOR
                    + _PAUSE_MARGIN_MS)
        return int(max(config.VOICE_SILENCE_MS,
                       min(config.VOICE_SILENCE_MAX_MS, adaptive)))

    def _mark_speech_frame(self, idx: int) -> None:
        """把一帧记进"语音帧区间"（ASR 前裁静音用，见 _trim_silence_edges）。

        抽成方法是因为有三个调用方：_feed_vad 的高能量/软语音分支、
        _feed_barge_in 的人声分支。同一事实多处实现必然分叉 —— 区间
        口径一歪，裁掉的就不是静音而是话。
        """
        if self._first_speech_frame < 0:
            self._first_speech_frame = idx
        self._last_speech_frame = idx
        # 顺手记下标表（升序）。句中静音的分段拼接靠它，见 _trim_silence_edges。
        self._speech_idx.append(idx)

    def _reset_speech_window(self) -> None:
        """清空"语音帧区间"。每轮用完即清（_finalize / 重新武装时）。

        不清的后果（2026-10-22 F2 实测推演）：播报期之后的下一轮
        _feed_vad 看到的是上一轮的下标（``< 0`` 判不成立 ⇒ 不更新），
        下一轮裁剪会把老人刚开口的话当静音切掉。
        """
        self._first_speech_frame = -1
        self._last_speech_frame = -1
        # 下标表同理：上一轮的下标留在里面，下一轮会被当成"句中语音"拼进
        # 送 ASR 的音频，把不该保留的静音又拼回去。
        self._speech_idx.clear()

    # ---------------------------------------------------------------- 全双工
    def _feed_barge_in(self, payload: bytes) -> None:
        """播报期喂一帧：判"有没有人想插话"，顺手把帧留进 _pcm。

        与 _feed_vad 的三条区别（都是故意的）：
        1. **用听前的底噪做门限**：播报期的"底噪"含 AEC 残余，跟着它涨
           门限会把真正的人声挡在门外；
        2. **计数器独立**（_barge_*）：不碰收音轮的 _speech_ms/_silence_ms；
        3. **开播 2s 宽限期**：AEC 残余集中在刚开播的时刻，唤醒词尾巴
           也可能这时候到（上游同款 2s，见常量区注释）。
        """
        try:
            pcm = opus_codec.decode_opus(payload, 16000, 1)
        except opus_codec.OpusUnavailableError:
            # 无 libopus：打断检测失效，但帧照收（abort 与协议不受影响）。
            log.debug("无 libopus，跳过本帧打断检测")
            return
        self._pcm.append(pcm)
        if self._speaking_started_at and \
                time.monotonic() - self._speaking_started_at < _BARGE_GRACE_SEC:
            return
        rms = opus_codec.pcm_rms(pcm)
        frame_ms = config.VOICE_FRAME_DURATION_MS or 60
        enter = max(config.VOICE_SPEECH_RMS, self._noise_floor * _ENTER_FACTOR)
        if rms >= enter:
            # 这一帧是人声：记进裁剪窗口。没有这一步，老人"抢过来的"前
            # 两三秒全在 _pcm 里却不被区间覆盖，下一轮 _trim_silence_edges
            # 会把它们当静音切掉 —— 老人费劲插的话，服务端当没听见。
            # （区间起点由 _finalize 在每轮结束时清零，不会串轮。）
            self._mark_speech_frame(len(self._pcm) - 1)
            self._barge_speech_ms += frame_ms
            self._barge_silence_ms = 0
            if self._barge_speech_ms >= config.VOICE_MIN_SPEECH_MS:
                self._trigger_barge_in()
        else:
            self._barge_silence_ms += frame_ms
            # 长静音才清零累计：句中换气/停顿不该把"想打断"这件事忘掉。
            if self._barge_silence_ms >= config.VOICE_PAUSE_MIN_MS:
                self._barge_speech_ms = 0

    def _trigger_barge_in(self) -> None:
        """停嘴改听：作废未发出的音频、把已收到的人声交接给收音轮。

        幂等由调用方保证（_barge_speech_ms 累积到阈值才进来一次，
        进来立刻清零）。
        """
        log.info("[%s] 全双工打断：播报中检测到人声（%dms），停止播报改听",
                 self.session_id, self._barge_speech_ms)
        # ⚠️ 先取数再清零：反过来的话下面转移给 _speech_ms 的是 0，
        # 看门狗的 _speech_ms 判据不满足，老人打断后反而说不上话。
        detected_ms = self._barge_speech_ms
        self._barge_speech_ms = 0
        self._barge_silence_ms = 0
        # 1) 停嘴：abort 让 LLM 流与 TTS 逐帧检查后退出，pacer.reset()
        #    把**还没走下 socket** 的队列帧全部作废 —— 只置 _abort 的话
        #    老人还会听到已经排队的十几帧（"喊停了还在说"）。
        self._abort = True
        # 记下"这一轮是被抢话掐断的"：反馈沉淀要靠它判 correction
        # （见 _feedback_signal）。断开连接也会置 _abort，但不会置这一位。
        self._turn_barged_in = True
        speaker = self._speaker
        if speaker is not None:
            speaker._pacer.reset()
        # 2) 交接：播报期收到的人声就是下一轮的输入。把打断检测攒到的
        #    语音量并进收音轮，否则看门狗的 _speech_ms 判据不满足，
        #    会一路挂到 VOICE_MAX_LISTEN_MS 再被当"一句没说"重新武装
        #    —— 老人打断之后再也说不上话，比不能打断更糟。
        self._speech_ms = max(self._speech_ms, detected_ms)
        self._silence_ms = 0
        self._ms_since_loud = 0          # 刚才是人声，软语音期限从头算
        self._t0 = time.monotonic()      # 60s 收听上限为这一句重新起算
        self.state = "listening"
        # 交接保护：紧接着轮次任务会收尾发 tts stop，设备据此回 listening
        # 并重发 listen start（realtime 固件行为）⇒ 触发一次
        # _rearm_listening。没有这个标志，那次 rearm 会把 _pcm 里老人
        # 刚说的话当新一轮清掉。见 _rearm_listening 的保留分支。
        self._barge_handover = True
        # 3) 看门狗：_run_turn 的 finally 只把 "thinking" 改回 idle，
        #    不会碰 listening；这里若没有活着的看门狗就补一个。
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._silence_watchdog())

    def _strip_binary_header(self, data: bytes) -> bytes | None:
        """按协议版本剥下行/上行的二进制头。v1 无头。"""
        if self.protocol_version == 2:
            if len(data) < 16:
                return None
            import struct
            _ver, _type, _res, _ts, size = struct.unpack(">HHIII", data[:16])
            if size > len(data) - 16:
                log.debug("v2 帧 payload_size=%d 超出实际长度 %d", size, len(data) - 16)
                return None
            return data[16:16 + size]
        if self.protocol_version == 3:
            if len(data) < 4:
                return None
            import struct
            _type, _res, size = struct.unpack(">BBH", data[:4])
            if size > len(data) - 4:
                return None
            return data[4:4 + size]
        return data

    async def _on_json(self, text: str) -> None:
        import json
        try:
            obj = json.loads(text)
        except Exception:  # noqa: BLE001
            log.warning("收到无法解析的 JSON：%r", text[:200])
            return
        if not isinstance(obj, dict):
            return
        mtype = obj.get("type")
        if mtype == "listen":
            await self._on_listen(obj)
        elif mtype == "abort":
            self._on_abort(obj)
        elif mtype == "mcp":
            # 设备本地工具通道（JSON-RPC 2.0，设备当 MCP server）。
            # 三种来源要分清，不能一律丢弃：
            #  0. 设备请求「念一句话」（提醒播报）→ **必须真的合成下发**，
            #     见 _on_device_announce。这条不是 MCP，只是借了 mcp 这个信封。
            #  1. 我们主动下发 tools/call 的**回包** → 按 id 唤醒等待的
            #     Future（见 call_device_tool）。丢掉它会让服药确认永远超时。
            #  2. 设备主动发的通知（如 tools/list_changed）→ 目前没有消费者，
            #     记 debug 即可。
            payload = obj.get("payload")
            if isinstance(payload, dict):
                if payload.get("type") == "tts":
                    await self._on_device_announce(payload)
                    return
                rid = payload.get("id")
                fut = self._mcp_pending.get(rid) if isinstance(rid, int) else None
                if fut is not None and not fut.done():
                    fut.set_result(payload)
                    return
            log.debug("忽略无人等待的 mcp 消息：%r", str(payload)[:200])
        else:
            log.debug("忽略未知消息类型 %r", mtype)

    async def _on_device_announce(self, payload: dict[str, Any]) -> None:
        """设备请求「念一句话」（``{"type":"tts","text":"喝水时间到了"}``）。

        为什么必须有这条（2026-10-22 真机：定时提醒只有屏幕变化、没有声音）
        ------------------------------------------------------------------
        固件 ``elder_bridge.cc::OnSpeak`` 在提醒触发时只做两件事：把设备状态切到
        「提醒」形象，然后把要念的话**塞进 ``SendMcpMessage``** 发给服务端。
        它假定「在线走 TTS」，但本服务端从来只把 ``type:mcp`` 当「设备本地工具
        回包」处理 —— 这条 ``{"type":"tts"}`` 落进 ``_mcp_pending`` 查不到 id，
        被当成「无人等待的消息」记 debug 丢掉。结果：提醒**到点了、屏也变了、
        回执也回了，就是不出声**，管理后台看起来一切正常。

        修在这里而不是改固件：设备早就在发这句话，缺的是服务端这一侧的实现；
        改服务端不用重新烧录，存量设备立刻生效。

        与正常对话共用 :meth:`_speak_reply` 的下发路径，所以提醒会**同时**有
        声音和 ``sentence_start`` 字幕 —— 老人低头能看到刚才说了什么。
        """
        text = str(payload.get("text") or "").strip()
        if not text:
            log.warning("设备请求播报但 text 为空，忽略")
            return
        # 正在一轮对话里（设备多半也判了 idle，但两边不同步）：不打断。
        # 提醒的价值在于「说一句」，打断老人正在说的话是更大的伤害。
        if self._turn_task is not None and not self._turn_task.done():
            log.warning("设备请求播报「%s」，但正在对话中 → 跳过（不打断）", text)
            return
        log.info("设备请求播报：%s", text)
        # ---- 定时提醒 / 设备主动关心 = "唤醒"的一种 ----
        # 待机（退下吧 or 空闲超时）期间 ``_dismissed_until`` 有效，此时服务端
        # 会丢弃所有上行音频、并挡住设备重发的 listen start。提醒念完之后
        # 老人想回一句"知道了"，那句话会被静默吃掉，设备只能干等冷却结束
        # （默认 180s）—— 表现是"提醒响了，我说话它不理我"。
        # 所以主动播报前先解待机，并把空闲锚点续到现在。
        if self._dismissed_until:
            log.info("设备主动播报，解除待机冷却")
        self._dismissed_until = 0.0
        self._last_active_at = time.monotonic()
        try:
            # 提示音乐**不在这里发**：它是固件内置的一段固定 OGG
            # （firmware/xiaozhi/main/assets/common/reminder_chime.ogg，
            #  由 tools/make_reminder_chime.py 一次性生成后编进固件），
            # 设备自己先响音乐、约 5 秒后再发这条 mcp/tts 请求。
            # 服务端只负责「把事项念出来」——这样音乐不依赖网络，
            # 断网时提醒仍然有声音（只是没有语音内容）。
            await self._speak_reply(text, time.monotonic())
        except Exception as exc:  # noqa: BLE001  播报失败绝不能带崩 WS 连接
            log.warning("设备请求播报失败（提醒可能只在屏幕上显示）：%s: %s",
                        type(exc).__name__, exc)

    async def _on_listen(self, obj: dict[str, Any]) -> None:
        state = obj.get("state")
        if state == "start":
            mode = str(obj.get("mode") or "auto")
            self._listen_mode = mode
            log.info("[%s] listen start（mode=%s）", self.session_id, mode)
            if self._dismissed_until and time.monotonic() < self._dismissed_until:
                # 冷却期内设备每次进入 listening 都会重发 start
                # （realtime 模式的固件行为）。重新武装等于把"退下吧"
                # 撤销掉，这里必须挡住。
                log.info("[%s] 退下吧冷却中（还剩 %.0fs），忽略 listen start",
                         self.session_id,
                         self._dismissed_until - time.monotonic())
                return
            self._rearm_listening()
        elif state == "stop":
            log.info("[%s] listen stop", self.session_id)
            # 起 task 而非 await：见 run() 里的注释（await 会卡住收包
            # 主循环，abort 就再也读不到）。
            self._start_turn()
        elif state == "detect":
            # 唤醒词通知：固件在检测到唤醒词时发。只需记录 + 重置缓冲
            # （设备随后会发 listen start，这里不清也行，但清了更稳：
            # 唤醒前若有残余帧，会被当成这一句的开头）。
            #
            # ⚠️ 2026-11-03 起固件还会在 **Idle → Listening**（按键唤醒 /
            # 提醒追问）补发一条 detect —— 那是解除 180 秒待机冷却的唯一
            # 出口（老人说完「不聊了」之后按键说话，原先服务端全丢）。
            # 但按键唤醒**不能**接唤醒应答：老人按下键就是要说话，回一句
            # 「你好，要聊点什么？」会让设备转成 Speaking、上行音频被丢，
            # 他刚开口的几个字就没了 —— 正是同一批反馈里的「前面两个字
            # 没识别到」。所以固件在那条消息里带 ``"greet":false``。
            #
            # 判据用 ``is False`` 而不是 ``not obj.get("greet")``：老固件
            # 根本不带这个字段，缺省必须**照旧应答**（缺省 ≠ 不要）。
            text = str(obj.get("text") or "")
            no_greet = obj.get("greet") is False
            log.info("[%s] 唤醒词：%s%s", self.session_id, text,
                     "（按键唤醒，不应答）" if no_greet else "")
            # 需求 7：唤醒词是「待机」的**唯一出口**。老人说了"待机吧"之后
            # 一直不响应，直到他自己再喊一次唤醒词 —— 那正是本分支。
            # 放在最前面清：下面那些缓冲重置与此事无关，别让顺序成为隐患。
            if self._dismissed_until:
                log.info("[%s] 唤醒词到达，解除待机", self.session_id)
            self._dismissed_until = 0.0
            # 唤醒词也是"有效内容"：老人刚喊完，那 120s 空闲当然从现在算起。
            # 不刷的话会出现"刚唤醒就立刻被判空闲待机"（上一场对话的锚点
            # 还停在几分钟前）。
            self._last_active_at = time.monotonic()
            self._frames.clear()
            self._pcm.clear()
            self._speech_ms = 0
            self._silence_ms = 0
            self._noise_floor = _NOISE_FLOOR_INIT
            self._ms_since_loud = 10 ** 9
            self._max_closed_pause_ms = 0
            # ⚠️ 2026-11-04：连续语音段也必须一起清。它现在是**判据**
            # （见 _silence_watchdog），不清就会把上一轮的长语音段带进来，
            # 让这一轮的噪声窗口被误判成"有人说话"。
            self._speech_run_ms = 0
            self._max_speech_run_ms = 0
            self._reset_speech_window()
            self._barge_speech_ms = 0
            self._barge_silence_ms = 0
            # 需求（2026-10-25）：唤醒后要有一句语音应答，否则老人以为没
            # 唤醒、反复喊唤醒词（本板固件那句「叮」在 #else 分支里，不会播）。
            # 起 task 而不是 await：见 _wake_greet 的注释。
            #
            # 按键唤醒不发（见本分支开头的说明）：老人按完键就在等自己说话
            # 被听见，任何一句应答都会占住下行音频、把他刚开口的字吃掉。
            if not no_greet:
                self._schedule_wake_greet()

    # ---------------------------------------------------------------- 唤醒应答
    #: 这些"称呼"是代词，套进「你好，<称呼>，要聊点什么？」会变成病句
    #: （「你好，您，要聊点什么？」）。识别到就走无称呼版。
    _ADDRESS_PRONOUNS: Final[frozenset[str]] = frozenset(
        {"您", "你", "您们", "你们"})

    def _schedule_wake_greet(self) -> None:
        """唤醒词到达后，排一次延时应答（可被下一次唤醒取消）。"""
        if not config.VOICE_WAKE_GREET:
            return
        old = self._greet_task
        if old is not None and not old.done():
            old.cancel()
        self._greet_task = asyncio.create_task(self._wake_greet())

    async def _wake_greet(self) -> None:
        """喊完唤醒词后补一句「你好，<称呼>，要聊点什么？」。

        为什么需要它（2026-10-25 用户反馈）
        ----------------------------------
        老人喊完唤醒词，**设备一点声音都没有**：本板 ``CONFIG_SEND_WAKE_WORD_DATA=y``
        让固件走了 ``SendWakeWordDetected`` 分支，而那句「叮」（OGG_POPUP）在
        ``#else`` 里，压根不会播。于是老人的判断是「没唤醒」→ 反复喊唤醒词。

        三个刻意保守的取舍（都是"宁可少说一句"）
        --------------------------------------
        1. **延时** ``VOICE_WAKE_GREET_DELAY_MS`` 再开口：老人常常一口气说
           「你好小智，今天天气怎么样」，唤醒词只是这句话的开头。立刻应答
           会把后半句吃掉，逼他重说 —— 那比不回应更烦人。
        2. **开口前再确认一次没人说话**（``_ms_since_loud``，口径是"距最后
           一个高能量帧多久"）。他正在说就整个跳过。用累计语音量判不行：
           唤醒词自己的尾巴也算累计，那样永远跳过。
        3. **任何异常都吞掉**：一句寒暄失败绝不能影响这一轮对话。
        """
        try:
            await asyncio.sleep(config.VOICE_WAKE_GREET_DELAY_MS / 1000.0)
            reason = self._wake_greet_skip_reason()
            if reason:
                log.info("[%s] 跳过唤醒应答：%s", self.session_id, reason)
                return
            text = await self._wake_greet_text()
            # ⚠️ 必须**再判一次**：上面读称呼是一次真实的让出点，期间可能有
            # listen stop 进来把 state 推成 thinking。这时候再开口就会和这一
            # 轮的 TTS 抢同一条下行音频通道 —— 两路帧交错，老人听到的是破音。
            # 这里到 _speak_reply 之间**不许再出现 await**（_speak_reply 内部
            # 会在第一个 await 之前就把 state 置成 speaking，因此是原子的）。
            reason = self._wake_greet_skip_reason()
            if reason:
                log.info("[%s] 跳过唤醒应答：%s", self.session_id, reason)
                return
            log.info("[%s] 唤醒应答：%s", self.session_id, text)
            await self._speak_reply(text, time.monotonic())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001  寒暄失败不影响对话
            log.warning("[%s] 唤醒应答失败（不影响对话）：%s: %s",
                        self.session_id, type(exc).__name__, exc)

    def _wake_greet_skip_reason(self) -> str:
        """返回跳过唤醒应答的原因；空串表示可以开口。"""
        if self.state != "listening":
            return f"会话状态是 {self.state}（不是 listening）"
        if self._turn_task is not None and not self._turn_task.done():
            return "本轮对话已经在跑"
        if self._abort:
            return "本轮已被打断"
        if self._dismissed_until and time.monotonic() < self._dismissed_until:
            return "退下吧冷却中"
        if self._ms_since_loud < config.VOICE_WAKE_GREET_QUIET_MS:
            return (f"老人已经在说话（距最后一个高能量帧 "
                    f"{self._ms_since_loud}ms < "
                    f"{config.VOICE_WAKE_GREET_QUIET_MS}ms）")
        return ""

    async def _wake_greet_text(self) -> str:
        """组装要念的那句话。称呼取不到就退化成无称呼版，**绝不编一个**。"""
        # 长度在这里**再夹一次**（_wake_address 里已有 [:16]）：这句是要送
        # TTS 的，而 address 的来源是 LLM 抽取出来的文本，理论上可被污染。
        # 两处都夹不是冗余 —— 一处管"数据进来时"，一处管"话出去时"。
        addr = (await run_in_threadpool(self._wake_address)).strip()[:16]
        if addr and addr not in self._ADDRESS_PRONOUNS:
            return f"你好，{addr}，要聊点什么？"
        return "你好，要聊点什么？"

    def _wake_address(self) -> str:
        """老人习惯的称呼（家属手动填的优先，其次是自动学出来的）。

        唯一来源是 ``memory.persona.tone_profile`` 的 ``effective.address``
        —— 它内部已经处理了「人工覆盖 > 统计值」的优先级，这里不许再算一遍
        （同一事实两处实现必然分叉）。读不到就返回空串。
        """
        owner_id = getattr(self.dev, "owner_id", None)
        if owner_id is None:
            return ""
        try:
            with SessionLocal() as db:
                eff = (tone_profile(
                    db, owner_id, getattr(self.dev, "agent_id", None)
                ).get("effective") or {})
            return str(eff.get("address") or "").strip()[:16]
        except Exception as exc:  # noqa: BLE001  读不到称呼不算错误
            log.info("[%s] 读称呼失败，用无称呼版：%s: %s",
                     self.session_id, type(exc).__name__, exc)
            return ""

    def _on_abort(self, obj: dict[str, Any]) -> None:
        reason = obj.get("reason", "")
        log.info("[%s] abort（reason=%s）", self.session_id, reason)
        self._abort = True
        # 设备侧 abort 是**老人自己**按停/喊唤醒词（固件在播报期检测到
        # 唤醒词就发它）。语义与全双工打断相同：这一轮没被接受。
        # 若它发生在没有轮次在跑的空档，下一次 _run_turn 开头会清掉，
        # 不会污染下一轮。
        self._turn_barged_in = True
        # 打断只影响"正在播的音频"，不影响已经结束的 ASR/LLM：
        # 那两个已经拿到结果，重来一次只是多花一次 token。
        self._frames.clear()
        if self.state == "speaking":
            self.state = "idle"
        # 把节流器队列里**还没走下 socket** 的音频帧全部作废。
        # 只置 _abort 是不够的：_stream_sentence 要等下一个 run_in_threadpool
        # 才看得到它，而队列里可能还压着十几帧 —— 那段时间里老人会继续
        # 听到话音（表现是"喊停了还在说"）。reset() 是同步的，立刻生效。
        sp = self._speaker
        if sp is not None:
            try:
                sp._pacer.reset()
            except Exception:  # noqa: BLE001  打断路径不能反抛
                log.debug("[%s] abort 时复位节流器失败", self.session_id,
                          exc_info=True)

    # ---------------------------------------------------------------- 重新武装
    def _continuous(self) -> bool:
        """本轮是否是连续对话（设备播完后继续听，不需要再喊唤醒词）。

        manual 模式设备播完回 idle 等新唤醒词，服务端必须保持 idle ——
        此时若擅自回到 listening，设备并不在推流，看门狗空转 60s 后还会
        拿一段静音去识别，纯属浪费。
        """
        return self._listen_mode in ("realtime", "auto")

    def _rearm_listening(self) -> None:
        """把会话状态重置回 listening 并（重）起静音看门狗。

        死锁背景（2026-10-20 实测，一次 ASR 失败后设备聋了 164 秒）：
        连续对话模式下，固件播报完后回到 listening 态 —— 关闭 WakeNet、
        继续向上推流。而服务端一轮对话结束（_finalize 的 finally）后停在
        idle：_on_binary 因 state != "listening" **静默丢弃**所有上行音频，
        _silence_watchdog 也早已退出。两侧互相等：设备以为服务端在听，
        服务端以为会话结束了。用户再喊唤醒词毫无反应 —— 固件在 listening
        态根本不跑 WakeNet，只能等连接自己断（实测要几分钟以上）。
        ⇒ 两条恢复通道：
          1. 固件 realtime 模式每次进入 listening 都重发 listen start
             （固件侧修复，见 application.cc SetDeviceState）；
          2. 服务端在 idle 态收到上行音频时，自动按连续对话重新武装
             （本函数，覆盖固件不发消息的路径，如 ASR 失败后的 alert）。
        """
        # ⚠️ 2026-11-03 修正：重新武装**不再**清零连续空识别计数。
        #
        # 原注释的理由是"一次待机冷却之后计数还停在阈值上，老人刚开口说第一句、
        # 恰好被 ASR 判空，就立刻再待机一次"。这条理由**不成立**：
        #   · 真正进入待机（standby）时计数已经没有意义了 —— 下一轮真人开口
        #     会在 _finalize 的"有效内容"分支里把它清零（见 _last_active_at
        #     那几行），不需要 rearm 代劳；
        #   · 而 rearm 在**每一轮 TTS 之后都会发生**（realtime 模式播完自动回
        #     listening 并重发 listen start）。在这里清零，等于让计数永远到不了
        #     阈值 —— 用户实报「已经说了先不聊了，还是频繁在说抱歉我没听清」
        #     就是这么来的：每轮都被清零，于是每次都按"第 1 次"念
        #     EMPTY_UTTERANCE_REPLY，永远走不到 silent / standby。
        # ⇒ 计数的语义是"连续 N 次一轮话都没听清"，判据必须跨轮累计，
        #    清零点只能有一个：认出真内容（_finalize 里的有效内容分支）。
        if self._barge_handover:
            # 全双工打断（F2）的交接保护：这次 rearm 不是"新的一轮"，
            # 而是被我们自己的 abort 掐断的那一轮的延续。链条：
            # barge 触发 → 轮次任务收尾发 tts stop → 设备回 listening
            # 重发 listen start → 本函数。此时 _pcm 里正是老人抢话
            # 说的内容，_speech_ms 是交接过来的语音量 —— 一律清掉的
            # 话，老人打断了服务端、服务端却把他刚说的话当没说过，
            # 他得再重复一遍（产品上就是"打断了但没用"）。
            # ⇒ 保留全部 VAD 状态与音频，只重置轮次控制项。
            # 标志**不清**：由 _finalize 把交接音频送进 ASR 时清；
            # 中途设备若重发 listen start，仍按交接保护走。
            self._frames.clear()
            self._abort = False
            self._t0 = time.monotonic()
            self.state = "listening"
            # 看门狗单实例：旧的先取消，避免两个看门狗同时等静音。
            if self._watchdog_task is not None and not self._watchdog_task.done():
                self._watchdog_task.cancel()
            self._watchdog_task = asyncio.create_task(self._silence_watchdog())
            return
        self._frames.clear()
        self._pcm.clear()
        self._speech_ms = 0
        self._silence_ms = 0
        self._noise_floor = _NOISE_FLOOR_INIT
        self._ms_since_loud = 10 ** 9
        self._max_closed_pause_ms = 0
        # 同 _start_listen 那处：连续语音段是判据，必须跟着清零。
        self._speech_run_ms = 0
        self._max_speech_run_ms = 0
        self._reset_speech_window()
        self._barge_speech_ms = 0
        self._barge_silence_ms = 0
        self._abort = False
        self._t0 = time.monotonic()
        self.state = "listening"
        # 看门狗单实例：旧的先取消，避免两个看门狗同时等静音。
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = asyncio.create_task(self._silence_watchdog())

    # ------------------------------------------------ 空识别（2026-11-03）
    def _empty_asr_action(self) -> str:
        """决定这次空识别该做什么，并推进计数。

        返回四种动作之一（空串 = 什么都不做）：

          * ``""``        —— **不出声、不计数**。用于"老人已经表示不聊了"的
            两种状态：冷却期内（``_dismissed_until`` 未过期）或服务端已
            ``idle``。此刻任何"我没听清"都是在打脸：他刚说过不想聊。
            也正因为不计数，冷却结束后重新开口不会被前账拖累。
          * ``"speak"``   —— 连续第 1 次：照旧念 ``EMPTY_UTTERANCE_REPLY``。
            老人确实可能只说了一半、或者被环境声盖掉了。
          * ``"silent"``  —— 连续第 2 次：只记日志，静默等他自己再说。
            再念一遍"您能再说一遍吗"就开始有"自言自语"的观感了。
          * ``"standby"`` —— 连续第 N 次（``_EMPTY_ASR_STANDBY_STREAK``）：
            会话真的结束（设备回 Idle、重新打开唤醒词）。阈值为什么是 3
            见该常量的注释。

        单独抽成**纯判定**（不 await、不发包）是为了能被冒烟脚本直接断言：
        真机验证这条链路要说十几句话，成本高到没人会去做。
        """
        if self.state == "idle":
            # 已待机（可能是 _enter_standby 之后设备仍在推流）：一律不出声。
            log.info("[%s] 已待机态收到空识别，保持安静", self.session_id)
            return ""
        if self._dismissed_until and time.monotonic() < self._dismissed_until:
            # 冷却期内的空识别理论上走不到这里（_on_binary 已丢弃上行音频），
            # 但 listen stop / 看门狗兜底仍可能起一轮 —— 这里必须再挡一次，
            # 否则「说了不聊 → 冷却中又念一句没听清」照样会发生。
            log.info("[%s] 待机冷却中（还剩 %.0fs）收到空识别，保持安静",
                     self.session_id, self._dismissed_until - time.monotonic())
            return ""
        self._empty_asr_streak += 1
        if self._empty_asr_streak >= _EMPTY_ASR_STANDBY_STREAK:
            return "standby"
        if self._empty_asr_streak == 1:
            return "speak"
        return "silent"

    async def _handle_empty_asr(self, started: float) -> None:
        """执行 :meth:`_empty_asr_action` 的决定（唯一出口，便于断言）。"""
        action = self._empty_asr_action()
        if not action:
            return
        if action == "standby":
            log.info("[%s] 连续 %d 次识别为空（阈值 %d），会话到此为止 → 待机",
                     self.session_id, self._empty_asr_streak,
                     _EMPTY_ASR_STANDBY_STREAK)
            # farewell 留空：老人没在等告别，静默回 Idle 比"我先休息了"更不打扰。
            await self._enter_standby("连续多次识别为空", farewell="")
            return
        if action == "speak":
            log.info("[%s] 识别结果为空（连续第 %d 次），念固定回复",
                     self.session_id, self._empty_asr_streak)
            await self._speak_reply(EMPTY_UTTERANCE_REPLY, started)
            return
        log.info("[%s] 识别结果为空（连续第 %d 次），保持安静等老人再开口",
                 self.session_id, self._empty_asr_streak)

    async def _enter_standby(self, reason: str, farewell: str = "") -> None:
        """让设备真正进入待机：念完（可选的）告别语 → 固件切 idle。

        **两条入口共用这一个实现**，别各写一份（同一事实两处实现必然分叉）：

          * 「退下吧 / 你去休息吧 / 不聊了」——``farewell`` 非空，先念一句
            告别语再待机；
          * 会话空闲超时（``VOICE_IDLE_STANDBY_SEC``）——``farewell`` 为空，
            静默待机：老人只是没说话，突然冒一句"我先待机了"反而是打扰。

        为什么必须动**固件**而不只是服务端闭嘴（2026-10-21 真机实测）：
        设备侧 ``CONFIG_USE_DEVICE_AEC=y`` ⇒ ``aec_mode_ = kAecOnDeviceSide``
        ⇒ ``SetListeningMode(kListeningModeRealtime)``。realtime 模式下设备
        播报完自动回 listening，而那一态 ``EnableWakeWordDetection(false)``
        （application.cc:809）—— 唤醒词是关的。只靠服务端"不应答"的话，
        老人再喊唤醒词设备根本听不见，实测聋过 164 秒。
        只有 ``self.elder.standby`` 会把设备切到 ``kDeviceStateIdle``，
        停掉上行推流并**重新打开 WakeNet**，下一次唤醒词才有效。

        顺序不能反：先念告别语、后调固件。反过来的话设备已经 idle，
        告别语会把它顶回 speaking，最后又落回 listening。
        """
        log.info("[%s] 进入待机：%s", self.session_id, reason)
        # 服务端这一层的静默（固件没切成功时的兜底）：冷却期内 _on_binary
        # 直接丢弃上行音频、_on_listen 不重新武装。
        self._dismissed_until = (time.monotonic()
                                 + config.VOICE_DISMISS_COOLDOWN_SEC)
        self._frames.clear()
        self._pcm.clear()
        self._speech_ms = 0
        self._silence_ms = 0
        # 同 _start_listen 那处：连续语音段是判据，必须跟着清零。
        self._speech_run_ms = 0
        self._max_speech_run_ms = 0
        self._reset_speech_window()
        # 先把 state 落下，待机期间后续到达的上行帧一律按"非 listening"丢弃。
        self.state = "idle"
        # 看门狗自停：本函数会从**看门狗内部**被调用（空闲超时那条入口），
        # 那时 cancel(self._watchdog_task) 取消的就是当前 task —— 会立刻在
        # 下一处 await 抛 CancelledError，把告别语和设备命令一起带走。
        # 症状是"说了退下吧，设备既没待机也没吭声"，极难定位。
        task = self._watchdog_task
        if task is not None and not task.done() \
                and task is not asyncio.current_task():
            task.cancel()
        self._watchdog_task = None
        if farewell:
            await self._speak_reply(farewell, time.monotonic())
        try:
            await self.call_device_tool("self.elder.standby", timeout=3.0)
            log.info("[%s] 设备已切换待机（等下一次唤醒词）", self.session_id)
        except Exception as exc:  # noqa: BLE001
            # 待机是"少做一件事"，绝不能因为它失败而影响老人已经听到的话。
            log.info("[%s] 设备侧待机命令未成功（%s: %s）→ 仅服务端静默，"
                     "%d 秒后兜底恢复",
                     self.session_id, type(exc).__name__, exc,
                     config.VOICE_DISMISS_COOLDOWN_SEC)

    def _listen_timeout_fingerprint(self, window_ms: int) -> str:
        """看门狗超时分支的音频指纹（必要时落 wav）。**绝不抛异常**。

        为什么单独包一层而不是直接调 ``_dump_listen_timeout``：这段代码跑在
        :meth:`_silence_watchdog` 的循环体里，外层只有一个 ``except Exception``
        —— 诊断代码抛出去就被它吞掉，看门狗**直接退出**，表现是设备永远卡在
        聆听态、再也不会有任何兜底。拿不到证据是小事，把会话搞死是大事，
        所以这里的任何失败都只能降级成一行字符串。

        （这条不是假想：2026-11-03 加埋点时真的因为取 ``self._pcm`` 抛了
        AttributeError，``smoke_voice`` 12i 立刻红了 —— 那个测试用的假会话
        对象没有 ``_pcm``。测试替身恰好暴露了真实风险。）
        """
        try:
            return _dump_listen_timeout(list(self._pcm), self._speech_ms,
                                        self.session_id, window_ms)
        except Exception as exc:  # noqa: BLE001  诊断失败不许影响会话
            log.warning("[%s] 窗口超时音频留证失败：%s: %s",
                        self.session_id, type(exc).__name__, exc)
            return "留证失败：%s" % type(exc).__name__

    def _has_real_speech(self) -> bool:
        """这一轮里到底有没有"人说话"，而不是"房间噪声够响"。

        两个判据是**并且**关系（`config.VOICE_MIN_SPEECH_RUN_MS` 的注释里
        写的就是这个意思，只是从来没接上）：

          * 累计语音够长（``VOICE_MIN_SPEECH_MS``，300ms）—— 防"一个字没说"；
          * **有一段连续**够长（``VOICE_MIN_SPEECH_RUN_MS``，400ms）—— 防噪声。

        为什么必须有第二条（2026-11-04 现场实测）：噪声是零星的孤立尖峰，
        平均 204ms、最长 180~200ms；人说话是连续能量，平均 522ms。只判
        累计值时，噪声每 1~3 秒打一次就能把累计值堆到 7~13 秒 ⇒ 服务端拿
        一段**纯噪声**送 ASR ⇒ 五家 ASR 一致返回空（那份 wav 的 HNR 只有
        0.22，真语音 ≥0.7）⇒ 老人听到「抱歉，我没听清，您能再说一遍吗？」。

        ⚠️⚠️ 2026-11-04 教训：这个判据 2026-10-22 就写进 config 了，
        `_max_speech_run_ms` 也一直在 `_feed_vad` 里维护，但
        **全仓没有任何一处读它做判断**（grep 只有定义 + 写入）。
        也就是说 10-22 那次「没有唤醒也说抱歉我没听清」的修复
        从来没有生效过 —— 写了判据、没接线，CI 全绿。
        以后新增判据必须同时给"读它"的地方和"它不成立时"的断言。

        为什么还要有 `_SHORT_UTTERANCE_MS` 这条豁免（**很重要**）：
        「连续段够长」这条判据对**短句**是致命的。老人说一声「好」、
        「嗯」、「退下吧」，整句只有 250~600ms，本来就不可能凑出
        400ms 以上的连续段 —— 一刀切会把它们全判成噪声，表现是
        「说了『好』设备没反应」，比原来更糟。
        所以规则是：**短句（累计 ≤1.5s）直接放行**，只有"累计很长"时
        才要求"必须有一段连续"—— 因为真人连续说 1.5 秒以上，里面
        必然有 ≥400ms 的连续能量；而噪声堆到 7.8 秒也凑不出 400ms。
        """
        return (self._speech_ms >= config.VOICE_MIN_SPEECH_MS
                and (self._max_speech_run_ms >= config.VOICE_MIN_SPEECH_RUN_MS
                     or self._speech_ms <= _SHORT_UTTERANCE_MS))

    async def _silence_watchdog(self) -> None:
        """连续静音达到阈值就自动 finalize（等价于设备端 VAD 断句）。

        阈值是**自适应**的（:meth:`_silence_threshold_ms`）：本轮出现过长
        停顿就多等。另有三道安全阀：
          * ``VOICE_MAX_LISTEN_MS``：老人说着离开/卡住时兜底，不让会话
            永远挂在 listening（realtime 模式下设备不自己发 stop）；
          * ``VOICE_MIN_SPEECH_MS``：一句都没说就不触发，防止底噪被当成
            一整句送 ASR；
          * ``VOICE_IDLE_STANDBY_SEC``（2026-10-22 新增）：整场会话连续
            这么久没有有效内容 → 进待机，而不是无限重新武装。
        """
        try:
            while self.state == "listening":
                await asyncio.sleep(0.2)
                if self.state != "listening":
                    return
                # ---- 会话级空闲 → 待机（用户 2026-10-22 需求）----
                #
                # 为什么必须放在最前面、而且每 0.2s 判一次：
                # 下面那个 ``elapsed_ms >= VOICE_MAX_LISTEN_MS`` 分支要等
                # 满 60s 才进得来，若把它当空闲判据，待机最早也要 60s 后
                # 才可能触发，且每次重新武装都会把窗口清零 —— 就是"一直
                # 监听、从没进过待机"的真机现象。
                #
                # 两个附加条件都是必需的：
                #   * ``_speech_ms < VOICE_MIN_SPEECH_MS``：老人正在说话
                #     （或刚开口）时不能待机，否则说到一半被掐掉；
                #   * ``_last_active_at`` 只在有效识别时刷新，重新武装不碰。
                if (self._speech_ms < config.VOICE_MIN_SPEECH_MS
                        and time.monotonic() - self._last_active_at
                        >= config.VOICE_IDLE_STANDBY_SEC):
                    await self._enter_standby(
                        f"连续 {config.VOICE_IDLE_STANDBY_SEC}s 没有有效内容")
                    return
                threshold = self._silence_threshold_ms()
                if self._silence_ms >= threshold and self._has_real_speech():
                    if not self._start_turn():
                        # 旧轮还在收尾（barge 打断后尤其常见）：等它完了
                        # 再起，**不能 return** —— 把看门狗一起带走的话，
                        # 这一轮就再没人收尾了。下个 0.2s 轮次自然重试。
                        continue
                    log.info("[%s] 服务端静音兜底触发（连续静音 %dms ≥ 阈值 %dms，"
                             "累计语音 %dms，底噪 %.4f）",
                             self.session_id, self._silence_ms, threshold,
                             self._speech_ms, self._noise_floor)
                    return
                # 收听超时兜底：阈值再自适应也有上限，而这一个管的是
                # "一直不满足条件"的极端情形（老人离开、设备持续送接近
                # 门限的能量）。
                #
                # ⚠️⚠️ 2026-10-21 真机事故：这个分支**必须**先问一句
                # "这 60 秒里到底有没有人说话"。原先它无条件
                # ``_start_turn()``，于是连续对话模式下每 60s 就拿一段
                # 环境噪声送 ASR → 识别为空 → 念 EMPTY_UTTERANCE_REPLY
                # （"抱歉，我没听清，您能再说一遍吗？"）→ 设备回到
                # listening → 60s 后再来一遍。老人听到的是音箱**永远在
                # 自言自语**，而服务端日志一切正常（每一轮都有始有终）。
                #
                # 判据与上面那个静音分支保持一致：累计语音要达到
                # VOICE_MIN_SPEECH_MS。达不到就只重新武装（清计数器 +
                # 重启看门狗），**不**起一轮 —— 一句都没说却送 ASR，
                # 得到的必然是"我没听清"。
                elapsed_ms = int((time.monotonic() - self._t0) * 1000)
                if elapsed_ms >= config.VOICE_MAX_LISTEN_MS:
                    if not self._has_real_speech():
                        # ⚠️ 这个分支原先**只打一行数字**（"累计语音 0ms"），
                        # 2026-11-03 深夜现场就撞上了：老人说他明明说了话，
                        # 服务端却认定 60 秒里一句没说。没有音频指纹就无从
                        # 判断是"门限太高"还是"送上来的音频被衰减"，所以
                        # 这里也留指纹（峰值够高时连 wav 一起落）。
                        # 必须在 _rearm_listening() **之前**取，它会清 _pcm。
                        #
                        # 2026-11-04：判据从"累计语音"升级成 _has_real_speech()
                        # （累计 + **最长连续段**）。现场 09:13:57 那轮累计
                        # 7800ms 全是房间噪声（HNR 0.22、最长连续段仅 180ms），
                        # 旧判据放行 ⇒ 纯噪声送 ASR ⇒ 返回空 ⇒ 老人听
                        # 「抱歉，我没听清」。日志里把最长连续段一起打出来，
                        # 下次不用再猜是"没人说话"还是"噪声太响"。
                        log.info("[%s] 收听超 %ds 但没有人说话（累计语音 %dms "
                                 "< %dms，最长连续段 %dms < %dms），重新武装而"
                                 "不送 ASR；音频指纹：%s",
                                 self.session_id,
                                 config.VOICE_MAX_LISTEN_MS // 1000,
                                 self._speech_ms, config.VOICE_MIN_SPEECH_MS,
                                 self._max_speech_run_ms,
                                 config.VOICE_MIN_SPEECH_RUN_MS,
                                 self._listen_timeout_fingerprint(elapsed_ms))
                        self._rearm_listening()
                        return
                    log.info("[%s] 收听超 %ds 兜底 finalize（累计语音 %dms）",
                             self.session_id, config.VOICE_MAX_LISTEN_MS // 1000,
                             self._speech_ms)
                    if not self._start_turn():
                        continue   # 旧轮收尾中：等它完了再起（同上）
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] 静音兜底异常：%s: %s", self.session_id,
                        type(exc).__name__, exc)

    def _start_turn(self) -> bool:
        """起一个 task 跑本轮对话；已有在跑的那轮就直接返回 False。

        **幂等的两道闸**（缺一不可）：
        1. 这里：``_turn_task`` 还没跑完就再起一轮，等于同一段音频
           识别两遍、答两遍、记两份 token；
        2. :meth:`_finalize` 里的 ``state != "listening"`` —— 因为
           ``create_task`` 不会立刻执行，第二个 stop 消息可能在第一个
           task 真正跑起来之前就到了，那时 state 还是 listening。

        返回值语义（2026-10-22 加）：False = 本轮仍在进行、没有起新轮。
        看门狗据此**继续等**而不是 return —— barge 打断后旧轮还在收尾
        （TTS 分片在飞）时静音条件已满足，直接 return 会把看门狗一起
        带走，那一轮再没人收尾，老人的话永远送不进 ASR。

        为什么用 ``create_task`` 而不是直接 await：见 :meth:`run` 的注释
        （await 会卡住收包主循环 ⇒ abort 失效）。
        """
        if self._turn_task is not None and not self._turn_task.done():
            log.debug("[%s] 本轮仍在进行，忽略重复的 finalize 触发", self.session_id)
            return False
        self._turn_task = asyncio.create_task(self._finalize())
        return True

    async def _finalize(self) -> None:
        """结束本轮收音并跑完整链路：ASR → 配额 → LLM → TTS。

        **幂等**：设备 stop 与静音兜底可能同时到（甚至 stop 先到、兜底
        后到），后到的那次必须直接返回，否则同一段音频会识别两遍、
        答两遍、记两份用量。用 ``_pcm`` 是否已消费来判。
        """
        if self.state != "listening":
            return
        self.state = "thinking"
        # 本轮"服务端在忙"的丢帧从 0 起算（见 __init__ 里 _drop_thinking_frames）
        self._drop_thinking_frames = 0
        # ``_frames``（Opus 裸包）保留在成员上不清：abort 时 _on_abort 要清它，
        # 而这里只用 ``_pcm``（已解码）送 ASR。故意不把 frames 传下去 ——
        # 下游只需要 PCM，多传一个参数就多一处"该清哪个"的歧义。
        # ⚠️ 裁剪必须作用于**上面这份局部拷贝**：self._pcm 紧接着就被清空，
        # 写成 self._trim_silence_edges(list(self._pcm)) 会把空列表送给 ASR
        # （静默故障：识别为空 → 念「我没听清」，没有任何报错）。
        pcm = list(self._pcm)
        self._frames.clear()
        self._pcm.clear()

        started = time.monotonic()
        pcm_trimmed = self._trim_silence_edges(pcm)
        # ---- 留一份**未裁剪**的音频给「ASR 返回空」时的重试 ----
        #
        # 2026-11-04 12:43 现场（生产日志三次连续空识别，随后被判定待机）：
        #   366 帧（22.0s）→ 裁成 30 帧（1.8s）→ ASR 空，rms=0.0030
        #   244 帧 → 裁成 62 帧 → ASR 空，rms=0.0036
        #   114 帧 → 裁成 27 帧（1.6s）→ ASR 空，rms=0.0110 peak=0.163
        # 三条的共同点不是"老人没说话"，而是**VAD 只认出了几百毫秒**，
        # 于是 90% 的音频被当成静音扔掉，剩下的碎片当然认不出来。
        # 老人听到的是「说了话设备没反应」。
        #
        # 所以：裁剪照旧（它省的是首句延迟），但把原音频留一份；一旦 ASR
        # 返回空，就用原音频**再试一次**。重试只发生在失败路径上，正常轮次
        # 一次调用都不多花。
        self._pcm_untrimmed = (b"".join(pcm)
                               if len(pcm_trimmed) != len(pcm) else None)
        pcm = pcm_trimmed
        # 交接音频已被本轮消费（或本就没有交接）：标志必须在这里清。
        # barge 后正常路径是 rearm 保留交接、看门狗再来一次 finalize；
        # 若旧轮走的是 close() 路径（LLM 失败，不发 tts stop、没有
        # rearm），标志会一直挂到这次 finalize 才清 —— 两处合计保证
        # 它不会残留到下一轮，把下一轮的 _pcm 错当交接保护起来。
        self._barge_handover = False
        # 裁剪窗口本轮已用完，立刻清零。不清的话播报期（F2）之后，
        # 下一轮 _feed_vad 看到的是上一轮的下标（`< 0` 判不成立 ⇒
        # 不更新），下一轮裁剪会把老人刚开口的话当静音切掉。
        self._reset_speech_window()
        try:
            await self._run_turn(pcm, started)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] 本轮异常：%s: %s", self.session_id,
                          type(exc).__name__, exc)
            # 同上：命中待机关键词时留给 finally 的兜底念告别语，
            # 这里再说一句"没处理好"就是两句互相打架。
            if not self._dismiss_candidate:
                await self._send_alert("刚才没处理好，请您再说一遍")
        finally:
            if self.state == "thinking":
                self.state = "idle"
            # ---- 诊断：思考期丢帧汇总（2026-11-03）----
            # 放在 state 回落**之后**：这行日志的语义是"这一轮忙的时候，
            # 设备一共送上来多少帧被我扔掉了"，与后面的待机兜底无关。
            if self._drop_thinking_frames:
                frame_ms = config.VOICE_FRAME_DURATION_MS or 60
                log.info("[%s] 思考期丢弃上行音频 %d 帧（%dms）：老人这期间"
                         "说的话没进 ASR（设备听到了、服务端在忙）",
                         self.session_id, self._drop_thinking_frames,
                         self._drop_thinking_frames * frame_ms)
                self._drop_thinking_frames = 0
            # ---- 兜底：命中待机关键词，但这一轮没能走到"模型判定"----
            # （LLM 未配置 / 调用异常 / 回复为空 / 播报前就崩了）
            #
            # 老人说了"退下吧"，最差也得让音箱安静下来。不兜的话，LLM 一挂
            # 关键词就永远不生效 —— 又回到"音箱永远在自言自语"那个老毛病。
            # 判据用 ``_dismiss_candidate`` 是否**还**置位：_run_turn 在
            # "模型正常答完"和"被老人抢话打断"两条路上都会清掉它，
            # 所以走到这里还置位 = 这一轮确实没结论。
            if self._dismiss_candidate and not self._standby_requested:
                self._dismiss_candidate = ""
                log.info("[%s] 待机复核未完成（本轮失败），按关键词直接待机",
                         self.session_id)
                try:
                    await self._enter_standby("待机复核未完成（本轮失败）",
                                              farewell=_DISMISS_REPLY)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[%s] 兜底待机失败：%s: %s", self.session_id,
                                type(exc).__name__, exc)

    def _trim_silence_edges(self, pcm: list[bytes]) -> list[bytes]:
        """裁掉头尾静音再送 ASR（P1：首句响应时间）。

        实测（生产主机）：本地 FunASR 延迟 ≈ 280ms + 230ms/秒音频，
        **音频长度线性进 ASR 耗时**。而 ``_pcm`` 装的是从 listen start 到
        finalize 的全部音频：老人从唤醒到开口常有 1~2s，尾部还要等够静音
        阈值（``VOICE_SILENCE_MS`` 起）—— 每轮 1~3s 是纯静音，白等
        0.2~0.7s。语音帧区间由 ``_feed_vad`` 顺手记录（不引入第二次
        能量计算，同一事实两份实现必然分叉）。

        三条保守性设计：
        1. 词头留 ``_TRIM_HEAD_PAD_FRAMES`` 帧（360ms）、词尾留
           ``_TRIM_TAIL_PAD_FRAMES`` 帧（120ms）—— 老人语速慢，词头能量
           建立需要时间，贴边裁会把第一个字（常常还是轻声）切掉一半；
           头比尾长得多，因为只有"词头"会丢内容（见常量处 2026-11-03 的说明）；
        2. 裁掉总量不足 ``_TRIM_MIN_SAVE_MS`` 就原样发 —— 切片有风险
           （VAD 误判会切掉轻声的词），没收益不担风险；
        3. 一轮里没记录到任何语音帧 ⇒ 原样返回。此时 ``_speech_ms``
           通常也不够，看门狗根本不会走到这里；设备主动 stop 的
           空轮则保持原行为（送全段 → ASR 返回空 → 念「我没听清」），
           不因本改动改变语义。

        第二段（2026-11-01 真机）：只裁头尾挡不住"一句话 + 十几秒静音 +
        另一句"——838 帧裁到 259 帧，其中真语音只有 540ms，ASR 返回空。
        这里再按 ``_speech_idx`` 把窗口切成若干语音段拼起来，三道闸
        （``_TRIM_GAP_KEEP_FRAMES`` / ``_TRIM_GAP_MIN_SAVE_MS`` /
        ``_TRIM_MIN_KEEP_MS``）保证正常一轮仍走连续窗口、字节不变。
        """
        if self._first_speech_frame < 0 or self._last_speech_frame < 0:
            return pcm
        n = len(pcm)
        if n == 0:
            return pcm
        start = max(0, self._first_speech_frame - _TRIM_HEAD_PAD_FRAMES)
        end = min(n - 1, self._last_speech_frame + _TRIM_TAIL_PAD_FRAMES)
        if end < start:
            return pcm
        saved_frames = start + (n - 1 - end)
        frame_ms = config.VOICE_FRAME_DURATION_MS or 60
        if saved_frames * frame_ms < _TRIM_MIN_SAVE_MS:
            return pcm
        window = pcm[start:end + 1]

        # ---- 句中长静音：按语音帧下标表分段，拼成一条连续音频 ----
        # 相邻语音帧间隔 ≤ _TRIM_GAP_KEEP_FRAMES 归同一段（保留句中换气），
        # 超过就断开；每段两端各外扩 _TRIM_PAD_FRAMES 保护词头/词尾。
        idxs = [i for i in self._speech_idx if start <= i <= end]
        segs: list[list[int]] = []
        for i in idxs:
            if segs and i - segs[-1][1] <= _TRIM_GAP_KEEP_FRAMES:
                segs[-1][1] = i
            else:
                segs.append([i, i])
        merged: list[list[int]] = []
        for a, b in segs:
            a = max(start, a - _TRIM_HEAD_PAD_FRAMES)
            b = min(end, b + _TRIM_TAIL_PAD_FRAMES)
            if merged and a <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        # 返回的必须是**帧列表**（与入参同型）：调用方 _run_turn 用
        # b"".join(pcm) 拼整段音频，给它 bytes 会按 int 迭代 ⇒ TypeError，
        # 整轮被兜底成「刚才没处理好」，ASR 一次都没发。
        spliced = [f for a, b in merged for f in pcm[a:b + 1]]
        spliced_frames = len(spliced)
        saved_gap_frames = len(window) - spliced_frames
        # 硬闸：没省下足够长的静音 / 拼完只剩碎片 ⇒ 原样发连续窗口。
        # 正常一轮在这里必然回退，送 ASR 的音频与改动前逐字节一致。
        if (saved_gap_frames * frame_ms >= _TRIM_GAP_MIN_SAVE_MS
                and spliced_frames * frame_ms >= _TRIM_MIN_KEEP_MS):
            log.info("[%s] ASR 前另裁掉句中静音 %d 帧（%dms）：%d 帧 → %d 帧（%d 段）",
                     self.session_id, saved_gap_frames,
                     saved_gap_frames * frame_ms, n, spliced_frames,
                     len(merged))
            return spliced

        log.info("[%s] ASR 前裁掉头尾静音 %d 帧（%dms）：%d 帧 → %d 帧",
                 self.session_id, saved_frames, saved_frames * frame_ms,
                 n, len(window))
        return window

    def _asr_with_usage(self, audio: bytes) -> str:
        """跑一次 ASR 并记一次用量（**同步**，由线程池调起来）。

        记账必须在**线程池里**做（本函数就是被 run_in_threadpool 调起来的），
        不在事件循环上；失败也要落一行（ok=False），否则"识别过但失败"
        与"从没识别过"在用量页上无法区分。

        抽成方法而不是留在 ``_run_turn`` 里的闭包：空识别之后要用**另一段**
        音频再调一次，而闭包只认自己捕获的那一段 —— 重试就得把记账再抄
        一份，两份记账必然分叉（本仓踩过：同一事实两处实现）。
        """
        started = time.monotonic()
        with SessionLocal() as db:
            try:
                text = asr_tts.asr(db, audio)
            except Exception as exc:
                _record_voice_call(
                    db, usage="asr", device_id=self.dev.id,
                    user_id=self.dev.owner_id, agent_id=self.dev.agent_id,
                    session_id=self.session_id, ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=int((time.monotonic() - started) * 1000))
                raise
            _record_voice_call(
                db, usage="asr", device_id=self.dev.id,
                user_id=self.dev.owner_id, agent_id=self.dev.agent_id,
                session_id=self.session_id,
                elapsed_ms=int((time.monotonic() - started) * 1000))
            return text

    async def _resolve_empty_asr(self, pcm_all: bytes,
                                 untrimmed: "bytes | None",
                                 started: float) -> str:
        """ASR 返回空后的处置：先拿**未裁剪**音频重试一次，仍空才走退避。

        为什么值得多花一次调用（2026-11-04 12:43 生产现场，用户原话
        「好像又听不到我说的话了」）：
        VAD 只认出几百毫秒就把整段当成静音裁掉（22.0s → 1.8s），剩下的碎片
        ASR 当然认不出来；老人听到的是「说了话设备没反应」，接着连空三次被
        判待机 —— 而设备那边屏幕还挂着待办事项。重试只发生在**失败路径**
        上，正常轮次一次调用都不多花。

        返回重试识别出的文本（空串 = 重试也没认出来，退避已经执行过）。
        """
        if untrimmed and _should_retry_asr_untrimmed(untrimmed, len(pcm_all)):
            log.info("[%s] 裁剪切得太狠（%.1fs → %.1fs），改用未裁剪音频"
                     "重试一次", self.session_id,
                     len(untrimmed) / 32000.0, len(pcm_all) / 32000.0)
            try:
                retry_text = await run_in_threadpool(
                    self._asr_with_usage, untrimmed)
            except asr_tts.VoiceConfigMissingError:
                log.warning("[%s] 重试时发现未配置 ASR", self.session_id)
                retry_text = ""
            except Exception as exc:  # noqa: BLE001
                # 重试失败不额外打扰老人（下面照旧走"没听清"的退避），
                # 但**必须留一行**：否则"重试没救回来"与"根本没重试"
                # 在日志里长得一模一样（本仓踩过：静默吞异常 = 无从排查）。
                log.warning("[%s] 未裁剪音频重试失败：%s: %s",
                            self.session_id, type(exc).__name__, exc)
                retry_text = ""
            if retry_text:
                log.info("[%s] 未裁剪音频重试成功：%s",
                         self.session_id, retry_text)
                return retry_text
        await self._handle_empty_asr(started)
        return ""

    async def _maybe_confirm_fallback(self, tool_trace: list[dict[str, Any]],
                                      utterance: str = "") -> None:
        """模型漏调 ``confirm_reminder`` 时，服务端补一刀设备确认。

        抽成方法是为了能单独跑自测（build-out 里的临时脚本）：这段是"替老人
        写服药记录"的最后一环，只靠读代码保证正确是不够的。

        只在「恰好 1 条候选」时补，判据见 ``_fallback_confirm_verdict``；
        不满足就只打一条 log，交给模型按 ``_PENDING_REMINDER_HINT`` 的追问
        纪律继续问。

        ``utterance``（2026-11-04 加在末尾）：用来判"这句是不是整条确认
        （确认 / 都吃了）"。为真时允许唯一候选挂多味药 —— 与
        ``_confirm_reminder`` 同一个口径，理由见那边的长注释。
        """
        if not (self._pending_reminder and self._confirm_candidate):
            return
        if any(t.get("name") == "confirm_reminder" for t in tool_trace):
            return
        allowed, why = _fallback_confirm_verdict(
            self.dev.id, whole_yes=is_whole_event_yes(utterance))
        if not allowed:
            log.info("[%s] 兜底确认跳过：%s（交给模型按追问纪律继续问）",
                     self.session_id, why)
            return
        try:
            ok = await self.call_device_tool(
                "self.elder.confirm_reminder", timeout=3.0)
            log.info("[%s] 模型未调用 confirm_reminder，服务端兜底补调：%s",
                     self.session_id, ok)
        except Exception:  # noqa: BLE001  设备没响应不该影响对话
            log.warning("[%s] 兜底确认失败（设备无响应）", self.session_id,
                        exc_info=True)

    async def _run_turn(self, pcm: list[bytes], started: float) -> str:
        # 每轮重置"待机复核"状态（见 __init__ 里这两个字段的说明）。
        self._dismiss_candidate = ""
        self._standby_requested = False
        # 打断标记同理：上一轮被抢话不代表这一轮也被抢话。置位只有两处
        # （_trigger_barge_in / _on_abort），消费在 _feedback_signal。
        self._turn_barged_in = False
        # 提醒确认状态同理：上一轮命中过"喝完了"不代表这一轮也算。
        self._confirm_candidate = False
        self._pending_reminder = ""
        t_asr = _now_ms()
        pcm_all = b"".join(pcm)
        # 未裁剪音频由 ``_finalize`` 备好，**在这里取走并清空**：全仓只有
        # 这一个消费者，清空保证它不会残留到下一轮（下一轮若没裁剪，
        # 重试就等于把同一段音频再送一遍，纯浪费一次调用）。
        untrimmed = self._pcm_untrimmed
        self._pcm_untrimmed = None

        # ---- F3 声纹：与 ASR **并行**发起（复刻上游 connection.py 的并行触发）
        # 为什么并行而不是串在 ASR 后面：声纹服务按设计在局域网，通常比 ASR
        # 厂商的 HTTP 往返快得多，并行约等于零成本；串行则每轮多等一个来回，
        # 老人直接感觉得到。超时/失败一律按"没认出"处理（见 voiceprint 模块
        # docstring 第 5 条）—— 认不出人只是这轮不个性化，绝不能卡住陪聊。
        def _identify() -> tuple[tuple[str, str, str] | None, float]:
            if self.dev.owner_id is None:
                return None, 0.0        # 未绑定设备：没有"家人"可认
            with SessionLocal() as db:
                rows = list(db.scalars(
                    select(_m.VoiceSpeaker).where(
                        _m.VoiceSpeaker.owner_id == self.dev.owner_id,
                        _m.VoiceSpeaker.enabled.is_(True),
                        _m.VoiceSpeaker.deleted_at.is_(None))))
            if not rows:
                return None, 0.0        # 一个说话人都没登记 = 没配 = 不识别
            speakers = [(r.speaker_key, r.name, r.description or "")
                        for r in rows]
            return voiceprint.identify_speaker(pcm_all, speakers)

        try:
            if voiceprint.enabled():
                # return_exceptions=True：声纹那边的异常不外溢，ASR 的异常
                # 照旧走下面的分支（拿不到第一个结果就直接 raise 它）。
                asr_res, vp_res = await asyncio.gather(
                    run_in_threadpool(self._asr_with_usage, pcm_all),
                    run_in_threadpool(_identify),
                    return_exceptions=True)
                if isinstance(asr_res, BaseException):
                    raise asr_res
                utterance = asr_res
                # 声纹异常不外溢（认不出人不是事故），但**必须留一行日志**：
                # 静默吞掉的表现和「没认出」一模一样（identify 计数 0、
                # 日志空白），2026-10-29 因此连红 4 次才找到根因。
                if isinstance(vp_res, BaseException):
                    log.warning("[%s] 声纹识别异常，本轮按未认出处理：%s: %s",
                                self.session_id, type(vp_res).__name__, vp_res)
                    speaker_hit = (None, 0.0)
                else:
                    speaker_hit = vp_res
            else:
                utterance = await run_in_threadpool(
                    self._asr_with_usage, pcm_all)
                speaker_hit = (None, 0.0)
        except asr_tts.VoiceConfigMissingError:
            log.warning("[%s] 未配置 ASR", self.session_id)
            await self._send_alert("我现在听不见，请让家人在管理后台配置好语音识别")
            return ""
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] ASR 失败：%s: %s", self.session_id,
                        type(exc).__name__, exc)
            await self._send_alert("刚才没听清，请您再说一遍")
            return ""

        speaker_row, speaker_score = speaker_hit
        speaker_name = speaker_row[1] if speaker_row else None
        if speaker_name:
            self._current_speaker = speaker_name
            log.info("[%s] 声纹认出：%s（相似度 %.3f）", self.session_id,
                     speaker_name, speaker_score)

        if not utterance:
            # ASR 没认出东西：不调 LLM（让模型根据空文本编一句只会产出
            # 更空的客套，且白花一次配额与 token）。
            #
            # ⚠️ 2026-11-03 用户实报「他已经说了先不聊了，还是频繁在说
            # 抱歉我没听清」—— 这里原先**无条件**念一句固定回复，于是
            # 「服务端念没听清 → 设备回 Listening → 再空 ASR → 再念」这个
            # 环一旦启动就永不停止（设备端 listening 态没有超时，也不跑
            # 唤醒词）。处置全部收进 _handle_empty_asr，判据与理由见那里。
            #
            # 先留一行**音频指纹**（rms/峰值/时长）并按需落 wav：ASR 返回空
            # 有三种完全不同的原因（音频太小 / 全是噪声 / 被切碎），而日志
            # 里原先只有"累计语音 4680ms"这一个数字，分不出来。
            # 2026-11-03 深夜就是靠这条才不用猜：三次 ASR 全是 ok=True，
            # 说明服务本身好的，问题在送进去的音频。
            log.info("[%s] ASR 返回空，音频指纹：%s", self.session_id,
                     _dump_empty_asr(pcm_all, self._speech_ms, self.session_id))
            # 先拿**未裁剪**的原始音频重试一次（裁剪切掉的话都在里面）；
            # 重试也空才进退避 —— 念一句/保持安静/待机三档全在
            # _handle_empty_asr 里判，这里不复制第二份判据。
            utterance = await self._resolve_empty_asr(pcm_all, untrimmed,
                                                      started)
            if not utterance:
                return ""

        # ---- 「结束对话」意图（2026-10-22 改：关键词 → 分档 → 必要时大模型复核）----
        # 不调 LLM 直接待机的老路只留给**强判据**：模型会在告别上客套三四句，
        # 老人更烦；而 realtime 模式下设备不跑唤醒词，不主动闭嘴的话老人
        # 没有任何办法让音箱安静下来。
        #
        # 弱判据命中**不在这里动手**，而是置 ``_dismiss_candidate``，让这一轮
        # 照常走 LLM（system 里追加 _STANDBY_VERIFY_HINT，工具表里放开
        # enter_standby）。这样「他走了」「我明天要出去」「孩子睡了，安静点」
        # 这类话不会再把设备误送进待机 —— 模型看得到上下文，判得比正则准。
        tier = _dismiss_tier(utterance)
        if tier:
            mode = config.VOICE_DISMISS_LLM_VERIFY
            if mode == "off" or (mode == "weak" and tier == "strong"):
                log.info("[%s] 老人要求结束对话（%s 档直判，模式 %s）：%s",
                         self.session_id, tier, mode, utterance)
                await self._enter_standby("老人要求结束对话",
                                          farewell=_DISMISS_REPLY)
                return ""
            self._dismiss_candidate = tier
            log.info("[%s] 命中待机关键词（%s 档），交大模型复核意图：%s",
                     self.session_id, tier, utterance)

        # ---- 有效内容：刷新会话空闲锚点 ----
        # 放在 dismiss 判据**之后**：老人说"退下吧"不该把空闲计时器续期，
        # 那一刻会话已经结束了。放在 ASR 空结果之后同理（空识别不是内容）。
        self._last_active_at = time.monotonic()
        # 认出了真内容 ⇒ 连续空识别归零。不清零的话计数会跨轮累计：
        # 老人早上空过两次、下午随便说一句，第三次空识别就把他直接待机了。
        self._empty_asr_streak = 0

        # ---- 屏上是否正挂着待确认的服药提醒（清单，可能多条）----
        # 两个用途：① 注入 system 让模型知道该调 confirm_reminder、还剩哪味要问；
        # ② 模型漏调时由服务端补一刀（见下面 _confirm_candidate 的收尾）。
        self._pending_reminder = _pending_reminder_label(self.dev.id)
        # ---- 今天已提醒的日程（播报即完成，用来"顺口问一句做完了没"）----
        # 与上面的待确认清单互斥：同一条不可能既是"待确认"又是"已完成"。
        self._reminded_schedule = _reminded_schedule_label(self.dev.id)
        # 有确认措辞 **且** 没有否定措辞才算。见 _NEG_RE 上方注释：
        # 「还没喝完」带着"喝完"，不挡就会把没做的事记成做完了。
        #
        # 三条来源取并集（2026-11-04 加第三条）：
        #   ① ``_CONFIRM_RE``：老人主动汇报「我吃完药了 / 水喝完了」；
        #   ② ``is_whole_event_yes``：短应答「确认 / 对 / 都吃了」。
        #      设备长按 3 秒后念的就是一个是非问句，老人答「确认」——
        #      而 ① 里没有「确认」两个字，生产日志里 ASR 认出的正是「确认。」，
        #      于是整条确认链一次都没启动（详见 ``_CONFIRM_RE`` 上方注释）；
        #   ③ 两者都必须过 ``_NEG_RE``：短应答里也可能带否定（「没吃」）。
        self._confirm_candidate = (
            bool(_CONFIRM_RE.search(utterance)) or is_whole_event_yes(utterance)
        ) and not _NEG_RE.search(utterance)
        if self._pending_reminder:
            # 清单是多行的，日志里压成一行，免得一条日志刷十几行。
            log.info("[%s] 待确认提醒清单：%s%s",
                     self.session_id,
                     self._pending_reminder.replace("\n", " | "),
                     "；本轮命中强确认措辞" if self._confirm_candidate else "")

        log.info("[%s] 识别：%s", self.session_id, utterance)
        await self._send_json({"type": "stt", "text": utterance})
        t_asr = _now_ms() - t_asr

        # ---- 授权校验：**在调 LLM 之前**。
        # 取消会员制后不再有「每日时长配额」这道闸门：只要这台设备是我们授权的
        # （已绑定到某个用户），就无限量陪聊。判据与 /api/device/ota 下发
        # WS 通道的条件一致（见 app/chat_quota.py 模块头部的说明）。
        #
        # ⚠️ 这里**不能把整段删掉**：`unlimited` 为 False 只在「设备未绑定」时成立，
        # 而未绑定设备本不该出现在语音会话里（拿不到 device_token）。
        # 留着它是为了纵深防御 —— 万一将来出现别的入口能拿到 device_token，
        # 未授权设备就不该白烧 token。原有的降级播报与埋点也一并保留。
        def _quota() -> dict:
            with SessionLocal() as db:
                return quota_snapshot(db, device_ids=[self.dev.id],
                                      authorized=self.dev.owner_id is not None)

        try:
            quota = await run_in_threadpool(_quota)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] 授权状态查询失败，按已授权放行：%s: %s",
                        self.session_id, type(exc).__name__, exc)
            quota = {"unlimited": True}
        if not quota.get("unlimited"):
            log.info("[%s] 设备未绑定用户，拒绝调用 LLM（未授权设备）",
                     self.session_id)
            await self._send_alert(DEVICE_UNBOUND_MESSAGE)
            # ⚠️ 这里原来多传了一个 0（7 个位置参数对 6 个形参），
            # 调用即 TypeError。而它正好在「设备未绑定」这条降级路径上 ——
            # 这条路径平时没人走，测试也覆盖不到，于是错误一直没暴露。
            # 一旦真走到，异常会打断整轮收尾（alert 已发、埋点没写）。
            await self._record_turn(utterance, "", started, t_asr, 0, 0,
                                    reply_ok=False)
            return utterance

        # ---- 记忆 + 人格召回
        #
        # 2026-10-31：这里原来只拼 ``BASE_VOICE_PROMPT + recall_for_reply()``
        # ⇒ 家属在 APP 里建的角色（名字/交代/性格）一句也到不了老人耳朵里
        # —— ``PromptCompiler`` 只被 APP 预览接口调用过，语音链路一次都没调。
        # 现在交给 ``build_system_prompt``：它用编译器产出的身份段**替换**掉
        # BASE 里硬编码的「你是小智」，再拼上说话要求与记忆；没有角色时原样
        # 回退到 ``BASE_VOICE_PROMPT + memory_ctx``（与接入前同一份文案，
        # 唯一差别是「当前时间」那一行现在排在末尾 —— 位置变了，内容没变）。
        #
        # 为什么要和召回放在**同一个线程池任务**里：两者都要一个 DB 会话，
        # 分两次 run_in_threadpool 就多一次线程切换 + 多一个会话；
        # 而 memory_ctx 又正是 prompt 的输入，分开拿反而多一层拼装。
        def _recall() -> tuple[str, str]:
            with SessionLocal() as db:
                try:
                    ctx = memory_bridge.recall_for_reply(
                        db, self.dev.owner_id, self.dev.agent_id, utterance,
                        session_id=self.session_id)
                except Exception as exc:  # noqa: BLE001  记忆是增益
                    log.warning("[%s] 记忆召回失败，按无记忆回答：%s: %s",
                                self.session_id, type(exc).__name__, exc)
                    ctx = ""
                # examples_prompt 传空串：偏好示例已由 recall_for_reply
                # （内部 build_persona_prompt）注入在 ctx 里，再传一次就是
                # 同一段话在 prompt 里出现两遍（白烧 token + 诱导复读）。
                system = build_system_prompt(
                    db, self.dev.owner_id, self.dev.agent_id,
                    memory_ctx=ctx, examples_prompt="")
                # ---- 这位老人的全部启用提醒（每轮确定性全量注入）----
                # 为什么走全量注入而不是记忆召回：记忆是每轮按**这句话**检索
                # top-6，而设备提醒上限 16 条 ⇒ 第 7 条以后永远召不回来。
                # 「不许说错药名用量」要的是完备性，不是相关性（详见
                # prompts.reminder_context 的模块注释）。
                #
                # 为什么和召回共用这个 DB 会话：两者都只要一个 Session，
                # 分开跑就多一次线程切换 + 多一个连接（与上面同一条理由）。
                # 单独 try：提醒渲染失败只该"这一段没有"，不该把整份 prompt
                # 打回 BASE（那会连带丢掉人格与记忆）。
                try:
                    reminder_ctx = reminder_context(db, self.dev.id)
                except Exception as exc:  # noqa: BLE001  提醒是增益
                    log.warning("[%s] 提醒注入失败，本轮按无提醒回答：%s: %s",
                                self.session_id, type(exc).__name__, exc)
                    reminder_ctx = ""
                return system, reminder_ctx

        try:
            system, reminder_ctx = await run_in_threadpool(_recall)
        except Exception as exc:  # noqa: BLE001  连兜底都失败就退回基础口令
            log.warning("[%s] 组装口令失败，用基础口令回答：%s: %s",
                        self.session_id, type(exc).__name__, exc)
            system = BASE_VOICE_PROMPT
            reminder_ctx = ""

        system = system + "\n\n" + now_context()
        # ---- 提醒块：**放在 now_context 之后**（"下次：今天下午三点"要靠
        # 上面那行的时间才对得上），且**非空才追加** —— 没有启用提醒时
        # 追加一段只有标题的空块，模型会开始编提醒。
        #
        # 为什么是"build_system_prompt 返回后再追加"而不是塞进它的 join：
        # 它内部有红线首尾不变式（persona/guardrails.assert_present），塞进去
        # 要重新验证；而"返回后追加"已有四个先例（now_context / 说话人身份 /
        # 待机复核 / 待确认提醒），风险最低，异常回退路径也自动覆盖到。
        if reminder_ctx:
            system = system + "\n\n" + reminder_ctx
        # ---- F3：说话人身份注入 system（仅该说话人在本会话首次出现时）
        # 与 user 消息的 JSON 包装是同一思路的两半：system 给"他是谁、
        # 该怎么介绍他"（description 在这里才派上用场），JSON 让模型自然
        # 称呼一次。两边都只来一次 —— 每轮都注入的后果是模型每轮都称呼，
        # 上游为此专门加了两个 set，我们照抄这个判据。
        if speaker_name and speaker_name not in self._system_speakers:
            self._system_speakers.add(speaker_name)
            desc = (speaker_row[2] or "").strip() if speaker_row else ""
            line = f"当前说话人：{speaker_name}。"
            if desc:
                line += desc
            system = system + "\n\n" + line
        # ⚠️ 这里**不再**追加 memory_ctx：语气画像/正反例/事实记忆已经由
        # build_system_prompt 拼进 system 了。再拼一次就是同一段话出现两遍。
        # ---- 待机意图复核提示（只在命中弱判据的那一轮注入）----
        if self._dismiss_candidate:
            system = system + "\n\n" + _STANDBY_VERIFY_HINT
        # ---- 有未确认提醒时告诉模型"屏上还挂着一条" ----
        # 不注入的话模型把「我已经喝完了」当闲聊，不调 confirm_reminder，
        # 屏幕就永远不退（2026-10-22 真机：白屏挂了 20 分钟）。
        if self._pending_reminder:
            system = system + "\n\n" + _PENDING_REMINDER_HINT.format(
                block=self._pending_reminder)
        # ---- 今天已提醒的日程：给模型一个"顺口关心一句"的话头 ----
        # 不注入的话模型根本不知道刚才提醒过什么，用户明确要求的
        # 「您让我提醒您的今天要还信用卡，您已经完成了吗？」就问不出来。
        # 注意这段**只用于闲聊**，纪律里已经写明不许调工具改状态。
        if self._reminded_schedule:
            system = system + "\n\n" + _REMINDED_SCHEDULE_HINT.format(
                block=self._reminded_schedule)

        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        messages.extend(self._history[-config.VOICE_HISTORY_TURNS * 2:])
        # ---- F3：首轮把 user 内容包成 {"speaker","content"}
        # 之后轮次用纯文本。降级是必须的：JSON 每轮都在上下文里出现名字，
        # 会诱导模型反复称呼（上游 introduced_speakers 的同款语义）。
        user_content = utterance
        if speaker_name and speaker_name not in self._introduced_speakers:
            self._introduced_speakers.add(speaker_name)
            user_content = json.dumps(
                {"speaker": speaker_name, "content": utterance},
                ensure_ascii=False)
        messages.append({"role": "user", "content": user_content})

        # ---- LLM（流式 + 工具调用循环）
        # 循环上限见 config.VOICE_TOOL_MAX_ROWS：LLM 反复调用工具时每轮都是
        # 真金白银的 token，不设上限等于把账单交给模型的坏心情。
        t_llm = _now_ms()
        speaker = _ReplySpeaker(self)
        results: list[Any] = []          # 每轮一个 ChatResult（逐轮记账）
        tool_trace: list[dict[str, Any]] = []
        timing: dict[str, Any] = {"round_ms": [], "ttft_ms": [], "tool_ms": 0}
        reply = ""
        try:
            for round_no in range(1, config.VOICE_TOOL_MAX_ROUNDS + 1):
                if self._abort:
                    log.info("[%s] 思考/播报期间收到 abort，停止后续轮次",
                             self.session_id)
                    break
                result = await self._chat_round(messages, speaker, round_no,
                                                timing)
                if result is _FIRST_EVENT_TIMEOUT:
                    # 厂商卡住：这一轮一个事件都没来。别让老人继续干等 ——
                    # 说一句短话把球抛回去，并让 speaker 正常收尾（发配对的
                    # tts start/stop），否则设备侧会一直停在"设备在说话"。
                    reply = LLM_SLOW_FALLBACK
                    await speaker.push_text(reply)
                    await speaker.flush_rest()
                    break
                if result is not None:
                    results.append(result)
                    # 每轮真实耗时：来自 llm_gateway 的 ChatResult.elapsed_ms，
                    # 是**厂商 HTTP 往返**的时间，不含我们的工具执行与播报。
                    timing["round_ms"].append(
                        int(getattr(result, "elapsed_ms", 0) or 0))
                if result is None or not getattr(result, "tool_calls", None):
                    reply = (result.text or "").strip() if result is not None else ""
                    break
                # 有工具调用：助理消息（含 tool_calls）进上下文，逐个执行后
                # 以 tool 角色消息喂回，再请求 LLM 生成最终口语回复。
                messages.append({"role": "assistant",
                                 "content": result.text or "",
                                 "tool_calls": result.tool_calls})
                for call in result.tool_calls:
                    if self._abort:
                        break
                    t_tool = _now_ms()
                    outcome = await self._run_tool(call)
                    timing["tool_ms"] += _now_ms() - t_tool
                    tool_trace.append({
                        "round": round_no,
                        "name": str((call.get("function") or {}).get("name") or ""),
                        "ok": bool(outcome.get("ok")),
                        "message": str(outcome.get("message") or "")[:160],
                    })
                    # ---- 假成功闸门的燃料 ----
                    # 提醒类工具失败 ⇒ 记下名字与人话，_speak_sentence 靠它
                    # 拦下"记好了/已经建好"这类措辞。只记**提醒类**：
                    # 同轮里天气查询失败不该顺带拦掉正常的天气播报。
                    _tname = str((call.get("function") or {}).get("name") or "")
                    if not outcome.get("ok"):
                        # 失败原因必须进日志（2026-11-03）。
                        #
                        # "老人说了时间、设备一直回没听懂"这类投诉，只看
                        # failed_tools=create_reminder 是查不动的：
                        #   · 没解析出时间（parse_when 返回 ok=False）
                        #   · 标题为空 / 是占位词（_is_placeholder_title）
                        #   · 落库出错
                        # 三种对老人是**同一句**"没听懂"，对排查是三条完全不同
                        # 的路。args 也要打 —— 模型把 when 填成什么，决定了该改
                        # 提示词还是改解析器。
                        log.warning(
                            "工具失败 name=%s args=%s reason=%s",
                            _tname,
                            str((call.get("function") or {}).get("arguments") or "")[:200],
                            str(outcome.get("message") or "")[:200])
                        if _tname in _REMINDER_TOOLS:
                            speaker.failed_tools.add(_tname)
                            speaker._fail_message = str(
                                outcome.get("message") or "") or speaker._fail_message
                    # ---- 复述时间对账的燃料（2026-10-29）----
                    # 建提醒**成功**时记下真正落库的钟点。_speak_sentence 拿它
                    # 比模型嘴里的钟点，一个都对不上就拦下、改念真实时间。
                    elif _tname == "create_reminder":
                        # ⚠️ 变量名**不许用 _m**：本函数的嵌套闭包 ``_identify()``
                        # 引用模块别名 ``_m``（= app.models）。Python 的规则是
                        # 「本函数给谁赋过值，谁就是本函数的局部变量」—— 于是闭包
                        # 看到的是还没赋值的局部，抛
                        # ``NameError: cannot access free variable '_m'``。
                        # 2026-10-29 声纹冒烟连红 4 次就是这个（a9518f5 把 _m
                        # 当分钟用了），而异常被 gather(return_exceptions) 吞成
                        # 「没认出」，日志里一个字都没有。
                        _hour, _minute = outcome.get("hour"), outcome.get("minute")
                        if isinstance(_hour, int) and isinstance(_minute, int):
                            speaker._reminder_clocks.add((_hour % 12, _minute))
                            speaker._reminder_clock_speak = str(
                                outcome.get("clock") or "") \
                                or speaker._reminder_clock_speak
                    messages.append({
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "name": _tname,
                        "content": json.dumps(outcome, ensure_ascii=False),
                    })
            else:
                # 轮数用完还在调工具：给老人一句实话，别再要一轮 token。
                # ⚠️ 必须**推给 speaker 念出来**：这些轮次一条文本增量都没有
                # （模型全在调工具），speaker 缓冲区是空的，只把话术赋给
                # reply 的话老人听到的是静音（finish 只会发空 start/stop）。
                log.warning("[%s] 工具调用达到 %d 轮上限，停止循环",
                            self.session_id, config.VOICE_TOOL_MAX_ROUNDS)
                reply = TOOL_LOOP_FALLBACK
                await speaker.push_text(reply)
                await speaker.flush_rest()
        except ConfigMissingError:
            log.warning("[%s] 未配置 LLM", self.session_id)
            await speaker.close()   # 只收协程，不发协议消息（原行为不变）
            # 命中待机关键词时不念这句：_finalize 的兜底会紧接着念告别语，
            # 老人会听到"请让家人配置好语音识别"+"好的您先忙"两句互相打架。
            if not self._dismiss_candidate:
                await self._send_alert(LLM_NOT_CONFIGURED_MESSAGE)
            return utterance
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] LLM 调用失败：%s: %s", self.session_id,
                        type(exc).__name__, exc)
            await speaker.close()
            if not self._dismiss_candidate:
                await self._send_alert("我刚才走神了，请您再说一遍")
            return utterance

        t_llm = _now_ms() - t_llm
        if not reply:
            if self._abort:
                # 打断轮：已播的由 speaker 收尾，不进历史不记账。
                # 老人是抢话打断的 ⇒ 他还在聊，绝不是要待机：清掉候选，
                # 否则 _finalize 的兜底会在他抢话之后把设备送进待机。
                self._dismiss_candidate = ""
                await speaker.finish()
                # ⚠️ 打断是**最强的负反馈**，而这条 return 原来把它整个吞掉了：
                # _record_usage（记忆沉淀 + 反馈）在下面，走不到 ⇒ 老人每次
                # 抢话都白抢，人格演化永远收不到反例。
                # 这里补一次，信号由 _feedback_signal 给（抢话 ⇒ correction）；
                # 断开连接导致的 abort 不带 correction，只把老人这句话记下来。
                await self._sediment_feedback(
                    utterance, "",
                    signal=self._feedback_signal(utterance),
                    prev_bot_text=self._last_bot_text())
                return utterance
            log.warning("[%s] LLM 返回空文本", self.session_id)
            await speaker.close()
            if not self._dismiss_candidate:
                await self._send_alert("我没想好怎么回答，请您再说一遍")
            return utterance

        # ---- 补播缓冲区尾巴（**这是 2026-10-21 修掉的一个静音 bug**）----
        # push_text 只切"带句末标点的完整句"，最后一段没有 。！？ 的文本会
        # 一直留在 speaker._buf 里，而 finish() 只发 tts stop —— 那段话
        # **永远不会被念出来**。模型偶尔不给结尾标点（"今天晴，14 度"），
        # 老人听到的就是一片安静，日志里却写着"播报完成"。必须在这里 flush。
        await speaker.flush_rest()

        # ---- 整段回复都被安检拦下：不能就这么静音 ----
        # 走到这里说明模型这一轮全在讲"它要做什么"（规划语/工具名），
        # 一句能念的都没有。宁可给一句实话，也不能让老人对着沉默的音箱。
        if speaker.sentence_count == 0 and speaker.dropped > 0:
            log.warning("[%s] 本轮回复全部不可播报（拦截 %d 句，原因 %s），"
                        "改说兜底话术。原文：%r",
                        self.session_id, speaker.dropped,
                        ",".join(sorted(set(speaker.dropped_reasons))),
                        reply[:200])
            await speaker.push_text(UNSPEAKABLE_REPLY_FALLBACK)
            await speaker.flush_rest()
            # 历史里也换成兜底话术：把规划语留在上下文里，模型下一轮会照着学。
            reply = UNSPEAKABLE_REPLY_FALLBACK

        # ---- 反馈信号：必须在追加历史**之前**算 ----
        # 重复提问的对照是"之前那几轮"，本轮一旦进了 _history，任何一句话
        # 都和自己 100% 相似 ⇒ 每轮都会被判成 retry。
        prev_bot_text = self._last_bot_text()
        signal = self._feedback_signal(utterance)

        # 会话内历史：**只在本轮成功拿到回复后**才追加。失败轮不进历史，
        # 否则下一轮的上下文里会有一条没有回复的 user 消息，
        # 模型会以为用户的话被忽略了。工具轮的中间文本不进历史 ——
        # 4 轮窗口很紧，留给"老人问/小智答"更有价值。
        self._history.append({"role": "user", "content": utterance})
        self._history.append({"role": "assistant", "content": reply})

        # ---- 埋点 + 记忆沉淀（都不阻塞播报）
        await self._record_usage(results, utterance, reply, started, t_asr, t_llm,
                                 tool_trace, timing=timing, speaker=speaker,
                                 signal=signal, prev_bot_text=prev_bot_text)

        # ---- 收尾播报：流式过程中已播的句子不管，这里发 tts stop
        #    （或空回复的 start/stop 配对，设备靠它回落 idle）。
        await speaker.finish()

        # ---- 确认兜底：模型漏调 confirm_reminder 时由服务端补一刀 ----
        #
        # 2026-10-22 真机：老人说「我已经喝完水了」，时延日志里 tool=0ms，
        # 模型一个字都没调，全屏提醒在白屏上挂了 20 分钟没退。工具链本身是好的
        # （用户反馈"语音建提醒时工具调用正常"），坏的是**模型没意识到屏上
        # 还挂着东西**。system 里已经注入了状态，这里再兜一层：
        #   * 只在"有待确认提醒 + 本轮命中强确认措辞"时补；
        #   * 模型自己调过就不重复（看 tool_trace）；
        #   * 放在 speaker.finish() 之后 —— 先让老人听到"好的"，
        #     再让屏幕收起来，顺序反了会出现"屏先空、话后到"。
        # 2026-11-03 收紧过触发条件；2026-11-04 又松回"唯一候选即可"，
        # 见 ``_fallback_confirm_verdict`` 的 docstring（多条候选仍然一律不补）。
        await self._maybe_confirm_fallback(tool_trace, utterance)

        # ---- 模型判定待机：**念完告别语之后**再切固件 ----
        # 顺序不能反（理由见 _enter_standby 的 docstring）：先切 idle 的话，
        # 刚念的告别语会把设备顶回 speaking，最后又落回 listening。
        if self._standby_requested:
            await self._enter_standby("大模型判定老人要结束对话")
            return utterance

        # 正常答完 ⇒ 模型判了"这句不是要待机"，清掉候选。
        # 不清的话 _finalize 的 finally 会把它当"本轮没完成"再兜底待机一次，
        # 那正好把"大模型复核"这件事整个作废。
        self._dismiss_candidate = ""
        self._confirm_candidate = False
        return utterance

    async def _chat_round(self, messages: list[dict[str, Any]],
                          speaker: _ReplySpeaker, round_no: int,
                          timing: dict[str, Any] | None = None) -> Any:
        """跑一轮 LLM（流式）。文本增量实时交给 speaker 逐句播放。

        阻塞生成器（SSE 读取）不能直接在事件循环里跑，按项目既有口径
        ``run_in_threadpool`` 逐事件拉取 —— 拉事件的间隙事件循环是自由的，
        设备的上行消息（abort）不会被这轮生成挡住。

        返回 None 表示"中途被 abort 叫停"（没有完整的 ChatResult）。
        """
        db = SessionLocal()   # 本回合专用：生成器只在首个 next() 里碰 DB
        gen = None
        # 首 token 时延（TTFT）：从发起请求到**第一个文本增量**到达。
        # 这是"模型慢不慢"的唯一直接度量 —— 之前的 t_llm 把工具执行和
        # 播报阻塞都算进去了，看那个数永远分不清是谁慢。
        t_round = _now_ms()
        got_first = False
        # ---- 工具轮「缓发」闸门（2026-10-29 用户实报「一句话回答两次」后加）----
        #
        # 事故现场（设备串口 turn-diag2.log，一轮之内、中间没有 listening）：
        #     6026743 << " 好的，我给您记着，十二点四十五提醒您下楼。"
        #     6034003 << " 您说的是十二点四十五分，还是十二点十五分？我没听太准…"
        # 第 1 段说"记着了"，第 2 段却问"哪个时间"——**互相矛盾**，一次生成
        # 不可能这样。真相是同一轮里跑了两次 LLM：
        #   第 1 轮模型还没拿到工具结果就抢先说"记着了"（提示词明令禁止，
        #   但它看不到结果，只能猜），工具返回"需要澄清"，第 2 轮才问。
        # 而 text 增量是**无条件**推给 speaker 念的 ⇒ 老人听到两段完整答复。
        #
        # 为什么不能在 done 事件里再判断：tool_calls 只在 done 里给，那时
        # 文字早念出去了，撤不回来。所以只能**先攒着**：
        #   * 攒够 _TOOL_ROUND_HOLD_CHARS 才认为"这是一段正经回答"并开始
        #     边生成边念 —— 保住正常对话的首句时延；
        #   * 一轮结束时若发现这轮是工具轮，攒下的文字**直接丢弃**（照旧
        #     进上下文，只是不念）。
        # 40 字这个门槛的依据：实测那句抢跑的"好的，我给您记着，十二点
        # 四十五提醒您下楼。"是 21 字，稳稳落在门槛内；而正常回答的第一句
        # 很少短于 40 字才会开始念，多出来的等待只有小几百毫秒。
        #
        # ⚠️ 但这道闸门**不能对所有话都生效**：smoke 第 13 组量出来，一句
        # 15 字的普通回复会被整段攒到 done 才念，首帧 400ms → 500ms，
        # 流式白做了。所以先用 _looks_toolish 判一次：不像要调工具的话
        # （讲笑话、聊家常）hold_chars=0，一个字都不等。
        hold_chars = _TOOL_ROUND_HOLD_CHARS if _looks_toolish(messages) else 0
        held: list[str] = []
        held_chars = 0
        committed = False
        try:
            def _open() -> Any:
                return chat_stream(db, "chat", messages, tools=TOOL_DEFS,
                                   tool_choice="auto")

            gen = await run_in_threadpool(_open)
            while True:
                if self._abort:
                    break
                # ⚠️ 用哨兵而不是捕获 StopIteration：StopIteration 穿过协程
                # 边界会被 Python 转成 RuntimeError（见 llm_gateway.chat_stream
                # 的 docstring）。chat_stream 恒以 ("done", ChatResult) 收尾，
                # None 只可能是生成器被异常关闭。
                #
                # 首事件加超时（见 _FIRST_EVENT_TIMEOUT_SEC 的实测数据）：
                # 只给**第一个**事件设限。中途卡住时 speaker 可能已经在念了，
                # 那时掐断比继续等更糟（老人听到半句就断）。第一个事件之前
                # 什么都还没出声，放弃的代价只有"这句话没答"。
                try:
                    event = await asyncio.wait_for(
                        run_in_threadpool(next, gen, None),
                        None if got_first else _FIRST_EVENT_TIMEOUT_SEC)
                except (asyncio.TimeoutError, TimeoutError):
                    if timing is not None:
                        timing["ttft_ms"].append(_now_ms() - t_round)
                    log.warning("[%s] 第 %d 轮 %g 秒内没有收到任何事件"
                                "（厂商卡住）⇒ 放弃本轮，改说兜底话术",
                                self.session_id, round_no,
                                _FIRST_EVENT_TIMEOUT_SEC)
                    # 不调 gen.close()：那个生成器还被卡在线程池的
                    # next() 里，从别的线程关它会和读线程打架。直接放手，
                    # 线程自己会在底层读超时后结束（有界泄漏，不是无界的）。
                    return _FIRST_EVENT_TIMEOUT
                if event is None:
                    return None
                if not got_first:
                    # 首事件时延（TTFT）：**不分事件类型**。
                    # 纯工具调用轮一个 text 增量都没有（tool_calls 只在
                    # done 事件里给），只按 text 记的话这类轮的时延整段消失
                    # —— 而"天气问句是不是慢在工具那一轮"恰恰是最常要查的
                    # 问题（真机事故里老人等了几秒，日志里 ttft 是空的）。
                    got_first = True
                    if timing is not None:
                        timing["ttft_ms"].append(_now_ms() - t_round)
                kind, payload = event
                if kind == "done":
                    calls = (getattr(payload, "tool_calls", None)
                             if payload is not None else None)
                    if calls:
                        # 工具轮：这一轮生成的文字**一个字都不念**。
                        # 它照旧由调用方 append 进 messages（模型下一轮要看），
                        # 只是不进 TTS —— 老人只会听到最后一轮那份正式答复。
                        if held_chars:
                            log.warning("[%s] 第 %d 轮是工具轮，丢弃它已生成的"
                                        " %d 字（不念给老人）：%r",
                                        self.session_id, round_no, held_chars,
                                        "".join(held)[:120])
                            held.clear()
                            held_chars = 0
                        if committed and hold_chars:
                            # 已经越过门槛念出去了，撤不回来 —— 至少留证据。
                            # hold_chars=0 的轮次是**主动**不设防（不像要调
                            # 工具），它调工具属于判错，不算"漏网"，不报。
                            log.warning("[%s] 第 %d 轮是工具轮，但文字已超过 %d 字"
                                        "门槛、前段已念出（漏网）：老人可能听到"
                                        "两段答复", self.session_id, round_no,
                                        hold_chars)
                        elif committed:
                            log.info("[%s] 第 %d 轮不像要调工具却调了工具，"
                                     "文字已念出（未设防，属预期内）",
                                     self.session_id, round_no)
                        return payload
                    # 收尾轮（没有工具调用）：把攒下的文字念掉。
                    if held_chars:
                        await speaker.push_text("".join(held))
                        held.clear()
                        held_chars = 0
                    # ⚠️ 非流式兜底：厂商没理会 stream:true 时（网关 documented
                    # 的降级路径），整段文本只在 done 事件里，一个 text 增量都
                    # 没有。不补推的话 speaker 是空的 —— 老人什么都听不到，
                    # 而协议层照样有配对的空 start/stop，**没有任何断言会红**。
                    if (payload is not None and getattr(payload, "text", "")
                            and not speaker.pushed_chars):
                        await speaker.push_text(payload.text)
                        await speaker.flush_rest()
                    return payload
                if kind == "text" and payload:
                    if committed or not hold_chars:
                        # hold_chars=0（不像要调工具）：一个字都不等，照旧
                        # 边生成边念 —— 首句时延与加闸门之前逐字一致。
                        committed = True
                        await speaker.push_text(payload)
                    else:
                        held.append(payload)
                        held_chars += len(payload)
                        if held_chars >= hold_chars:
                            committed = True
                            await speaker.push_text("".join(held))
                            held.clear()
                            held_chars = 0
            # abort 提前退出：关生成器（finally 关连接），没有 ChatResult
            return None
        finally:
            if gen is not None:
                try:
                    await run_in_threadpool(gen.close)
                except Exception:  # noqa: BLE001  关闭失败没有补救手段
                    pass
            db.close()

    async def _run_tool(self, call: dict[str, Any]) -> dict[str, Any]:
        """执行一个工具调用。任何失败都转成"老人听得懂"的结果字典。"""
        fn = call.get("function") or {}
        name = str(fn.get("name") or "")
        raw_args = fn.get("arguments") or "{}"
        log.info("[%s] 工具调用：%s(%s)", self.session_id, name,
                 str(raw_args)[:200])

        # ---- enter_standby：模型判定「老人要结束对话」（2026-10-22 需求）----
        #
        # **只在命中待机关键词的那一轮放行**。理由：误判的代价不对称 ——
        # 该停没停只是多听一会儿，不该停却停了是"老人怎么喊都没反应"
        # （realtime 模式下设备停了推流、只剩 WakeNet）。模型再准，也不
        # 把这条生杀权完全交给它。
        if name == _STANDBY_TOOL_NAME:
            if not self._dismiss_candidate:
                log.info("[%s] 本轮没有待机关键词，拒绝模型发起的待机",
                         self.session_id)
                return {"ok": False,
                        "message": "现在不需要待机。请照常自然地回答老人，"
                                   "不要提这件事。"}
            self._standby_requested = True
            log.info("[%s] 模型判定为待机意图（%s 档关键词），"
                     "本轮念完即下发待机命令",
                     self.session_id, self._dismiss_candidate)
            return {"ok": True,
                    "message": "已确认，本轮结束后设备就会待机。"
                               "现在只说一句很短的道别（10 个字以内），"
                               "不要再提问，也不要再展开任何内容。"}

        def _run() -> dict[str, Any]:
            with SessionLocal() as db:
                return execute_tool(db, self.dev.id, name, raw_args,
                                    device_call=_device_call)

        # execute_tool 是同步函数、跑在线程池里（见下）。要它发起「服务端→
        # 设备」的 MCP 往返，只能从这个**工作线程**把协程丢回事件循环：
        #  - 直接 asyncio.run() 会新建一个 loop，而 WS 连接绑在原来的 loop 上，
        #    send_text 必然抛 "attached to a different loop"；
        #  - 不能用 asyncio.run_coroutine_threadsafe 之外的「伪同步」写法，
        #    因为回包要靠原 loop 上的收包任务（run() 里那个 while 循环）
        #    才会被处理，loop 一旦被这个线程占住就是死锁。
        # run_coroutine_threadsafe 只是投递 + 阻塞本线程，事件循环照常转，
        # 收包任务能把回包塞进 Future。
        loop = asyncio.get_running_loop()

        def _device_call(tool_name: str,
                         arguments: dict[str, Any] | None = None,
                         timeout: float = 3.0) -> Any:
            fut = asyncio.run_coroutine_threadsafe(
                self.call_device_tool(tool_name, arguments, timeout), loop)
            # 多等 1 秒：让 call_device_tool 内部的 wait_for 先超时，
            # 这样日志里报的是「设备没响应」而不是「future 取结果超时」。
            return fut.result(timeout + 1.0)

        try:
            return await run_in_threadpool(_run)
        except Exception as exc:  # noqa: BLE001  工具失败不崩会话
            log.warning("[%s] 工具 %s 执行异常：%s: %s", self.session_id,
                        name, type(exc).__name__, exc)
            return {"ok": False,
                    "message": "操作没成功，请用一句话告诉老人稍后再试"}

    async def _speak_reply(self, reply: str, started: float) -> None:
        """（兼容入口）一次性说完一整段。

        保留给「不经过 LLM 的固定回复」用（空识别等）—— 那些场景没有流式
        增量，直接整段走 speaker 的句子队列。LLM 回复的逐句流式播放见
        :class:`_ReplySpeaker`（由 ``_run_turn`` 直接驱动）。
        """
        speaker = _ReplySpeaker(self)
        await speaker.push_text(reply)
        # 固定话术常常不带句末标点，而 push_text 只切"完整句"——
        # 不 flush 的话这句话永远留在缓冲区里，老人听到的是静音。
        await speaker.flush_rest()
        await speaker.finish()
        # 这条路不经过 _record_usage（没有 LLM 调用可挂），但 TTS 确实用了：
        # 单独补一次记账。**放在 finish() 之后**——音频已播完，不占音频通路。
        try:
            def _flush() -> None:
                with SessionLocal() as db:
                    _flush_tts_usage(db, speaker=speaker, dev=self.dev,
                                     session_id=self.session_id)

            await run_in_threadpool(_flush)
        except Exception as exc:  # noqa: BLE001  记账失败不影响已经播完的话
            log.warning("[%s] 固定话术 TTS 记账失败：%s: %s", self.session_id,
                        type(exc).__name__, exc)


    # ================================================================ 埋点
    # ---------------------------------------------------------------- 反馈
    def _last_bot_text(self) -> str:
        """上一轮机器人说过的话（会话内历史里最后一条 assistant）。

        ``judge_feedback`` 判 retry 时需要它（"换说法后被接受"要成对比较），
        不传的话 retry 一律被判无效 —— 信号传了也等于没传。
        """
        for item in reversed(self._history):
            if item.get("role") == "assistant":
                return str(item.get("content") or "")
        return ""

    def _is_repeated_question(self, utterance: str) -> bool:
        """本轮是不是在重复最近几轮问过的同一件事（字符级相似度 > 0.8）。

        为什么用 ``difflib`` 而不是向量：这里要判的是"几乎同一句话"，
        字符级序列匹配对老人这种**逐字重复**的提问最准，而且零依赖、零
        网络往返 —— 它在每一轮对话的关键路径上，不能引入任何 IO。

        ASR 会把同一句话识别成略有差异的版本（"几点了"/"几点了？"），
        所以先归一化（去标点空白）再比。
        """
        cur = _norm_utterance(utterance)
        if not cur:
            return False
        prevs = [str(i.get("content") or "") for i in self._history
                 if i.get("role") == "user"][-_RETRY_LOOKBACK:]
        for prev in prevs:
            other = _norm_utterance(prev)
            if not other:
                continue
            short, long_ = sorted((len(cur), len(other)))
            if long_ and short / long_ < _RETRY_MIN_LEN_RATIO:
                # 长度差太多 ⇒ 是接着往下说，不是重问（见常量注释）
                continue
            if difflib.SequenceMatcher(None, cur, other).ratio() >= _RETRY_SIMILARITY:
                return True
        return False

    def _feedback_signal(self, utterance: str) -> str:
        """本轮该记哪种反馈信号（空串 = 交给 ``judge_feedback`` 自动识别）。

        优先级：抢话打断 > 重复提问。两者同时成立时记打断 —— 打断是老人
        主动做出的动作，比"又问了同样的话"更明确。
        """
        if self._turn_barged_in:
            return _m.PersonaSignal.CORRECTION
        if self._is_repeated_question(utterance):
            return _m.PersonaSignal.RETRY
        return ""

    async def _sediment_feedback(self, utterance: str, reply: str, *,
                                 signal: str = "",
                                 prev_bot_text: str = "") -> None:
        """把这一轮的反馈 + 事实记忆交给记忆模块（异步、失败自吞）。

        抽成方法是为了**打断路径也能调**：打断轮没有完整回复、不走
        ``_record_usage``，而它恰恰是最该被学到的那一轮。
        """
        turns = [{"role": "user", "content": utterance}]
        if reply:
            # 打断轮没有完整回复：不写空的 assistant 行（``as_turns`` 虽然
            # 也会滤掉空内容，但下游不该看到一条"机器人什么都没说"的轮次）。
            turns.append({"role": "assistant", "content": reply})
        try:
            memory_bridge.remember_after_reply(
                SessionLocal, self.dev.owner_id, self.dev.agent_id, turns,
                session_id=self.session_id,
                user_text=utterance, bot_text=reply,
                prev_bot_text=prev_bot_text, signal=signal,
                # 用量归属：记忆抽取（light）与向量化（embed）两条流水要靠它
                # 才能被用户端按设备过滤看到 —— 后台线程里 contextvars 不传播，
                # 必须显式传。
                device_id=self.dev.id,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] 记忆沉淀启动失败：%s: %s", self.session_id,
                        type(exc).__name__, exc)

    async def _record_usage(self, results: list[Any], utterance: str, reply: str,
                            started: float, t_asr: int, t_llm: int,
                            tool_trace: list[dict[str, Any]] | None = None,
                            *, timing: dict[str, Any] | None = None,
                            speaker: _ReplySpeaker | None = None,
                            signal: str = "",
                            prev_bot_text: str = "") -> None:
        """记账：token 用量 + 陪聊时长 + Interaction + 记忆沉淀。

        工具循环下**每轮 LLM 调用各记一行** model_usage（token 是每轮真实
        花的，合成一行就对不上账）；工具轨迹进 Interaction.meta，便于事后
        对账"这轮为什么多花了两倍 token"。

        **四条记录分两个事务**（token/时长/交互 一次 commit；记忆异步）。
        为什么不用一个大事务：``remember_after_reply`` 内部自己起线程
        与会话，塞进来会让主事务等一个与返回无关的写；而 token 与
        陪聊时长必须同生共死（漏一个统计就对不上账）。
        """
        elapsed_ms = int((time.monotonic() - started) * 1000)
        total_tokens = sum(int(getattr(r, "total_tokens", 0) or 0)
                           for r in results)
        # 提醒相关交互与普通陪聊分开累计（chat_quota 既有口径），
        # 只累计到 reminder_seconds 便于排查。
        is_reminder = any(str(t.get("name", "")).endswith("reminder")
                          for t in (tool_trace or []))

        # ---- 时延拆解（2026-10-21）----
        # 老人反馈"很久很久才说话"，但原来 meta 里只有 asr_ms / llm_ms /
        # tts_ms 三个数，其中 llm_ms 把工具执行和**播报阻塞**都算在内，
        # 看它永远分不清是模型慢、工具慢还是 TTS 慢。这里按阶段拆开：
        #   asr_ms      识别整段上传往返
        #   ttft_ms     每轮"发出请求 → 第一个事件到达"（不分类型：纯工具
        #               调用轮没有文本增量，只按 text 记会让它整段消失）
        #   round_ms    每轮厂商 HTTP 往返总耗时（ChatResult.elapsed_ms）
        #   tool_ms     工具执行累计（含设备 MCP 往返）
        #   tts_first   从 LLM 开始到**第一帧音频发出**（老人实际等待时长）
        #   tts_ms      TTS 合成累计（串行，直接累加进总时延）
        #   帧间隔峰值   相邻两帧实际间隔的最大值；超过 3 帧才算供给断档
        #   积压峰值    节流队列里待发帧数的最大值；越大说明整段音频越靠后，
        #               **最后一句话延后越久**（N 帧 ≈ N×60ms）
        #   speak_ms    整轮总耗时
        timing = timing or {}
        tts_first_ms = int(getattr(speaker, "tts_first_ms", 0) or 0)
        tts_total_ms = int(getattr(speaker, "tts_ms", 0) or 0)
        tts_max_gap = int(getattr(speaker, "max_gap_ms", 0) or 0)
        tts_max_pending = int(getattr(speaker, "max_pending", 0) or 0)
        dropped = int(getattr(speaker, "dropped", 0) or 0)
        dropped_reasons = list(getattr(speaker, "dropped_reasons", []) or [])
        claim_blocked = int(getattr(speaker, "claim_blocked", 0) or 0)
        clock_blocked = int(getattr(speaker, "clock_blocked", 0) or 0)
        failed_tools = sorted(getattr(speaker, "failed_tools", set()) or set())
        ttft = list(timing.get("ttft_ms") or [])
        rounds = list(timing.get("round_ms") or [])
        frame_ms = int(config.VOICE_FRAME_DURATION_MS or 60)
        gap_warn_ms = 3 * frame_ms          # 见下面那段 WARNING 的门限说明
        backlog_warn_frames = int(2000 / max(1, frame_ms))   # ≈2s 音频

        # 一行日志把"慢在哪"说清楚。这行是给运维直接 grep 的，
        # 不需要去翻 Interaction 表算减法。
        log.info("[%s] 时延 asr=%dms ttft=%s round=%s tool=%dms "
                 "首帧=%dms tts=%dms 帧间隔峰值=%dms 积压峰值=%d帧 "
                 "总=%dms 轮数=%d 丢弃句=%d 假成功拦下=%d 时间不符拦下=%d",
                 self.session_id, t_asr, ttft or "-", rounds or "-",
                 int(timing.get("tool_ms") or 0), tts_first_ms, tts_total_ms,
                 tts_max_gap, tts_max_pending,
                 elapsed_ms, len(results), dropped, claim_blocked,
                 clock_blocked)
        if claim_blocked:
            # 单独一行 WARNING：这是"老人以为建好了其实没建"的事故痕迹，
            # 混在 INFO 里没人看。出现一次就该查一次模型为什么谎报成功。
            log.warning("[%s] 本轮拦下 %d 句假成功播报（失败工具 %s）",
                        self.session_id, claim_blocked, failed_tools)
        if clock_blocked:
            # 同上，且更贵：提醒**建成了**、但时间/内容和老人说的不一致。
            # 出现一次就说明模型填错了工具参数，去查本轮 create_reminder 的
            # 入参与老人原话差在哪。
            log.warning("[%s] 本轮拦下 %d 句时间不符的复述 —— 模型说的时间和"
                        "实际落库的不一致，老人差点记住错时间",
                        self.session_id, clock_blocked)
        if tts_max_gap > gap_warn_ms:
            # 每帧 frame_ms 音频，间隔超过 3 帧就意味着设备在这一大段里
            # **没有声音可播**。这条 WARNING 是"断断续续"的判决书：它在
            # 服务端，说明是我们供给不足；它正常而老人仍觉得断续，
            # 问题就在设备侧（I2S 欠载）。
            # ⚠️ 门限必须是 3 帧而不是 1 帧：asyncio 的 sleep 粒度（Windows
            # 上约 15ms）与 TTS 分片的到达节奏合起来，1~2 帧的抖动**几乎
            # 每轮都有**（实测稳态 70~90ms）。拿 60ms 当门限只会每轮都报警，
            # 真事故反而被埋掉 —— 那正是本项目最反对的日志噪音。
            log.warning("[%s] 音频帧间隔峰值 %dms > %dms（一帧只承载 %dms）"
                        "—— 供给断过档，老人会听到一顿一顿。"
                        "多半是 TTS 分片慢、上游卡顿或设备侧 socket 读不动。",
                        self.session_id, tts_max_gap, gap_warn_ms, frame_ms)
        if tts_max_pending > backlog_warn_frames:
            # 这不是断续，是**迟到**：整段音频从生产到播放整体后移，
            # 最后一句话会明显晚半拍到几秒。TTS 出帧比播放快时正常，
            # 持续偏高说明设备侧 socket 读得慢（反压）。
            log.info("[%s] 音频积压峰值 %d 帧（约 %.1fs）——话音整体后移，"
                     "最后一句会晚一些。TTS 出帧比播放快时正常，持续偏高"
                     "说明设备侧读得慢。",
                     self.session_id, tts_max_pending,
                     tts_max_pending * frame_ms / 1000.0)

        def _write() -> None:
            with SessionLocal() as db:
                for result in results:
                    usage_svc.record_from_result(
                        db, result, source="voice", usage="chat",
                        device_id=self.dev.id, user_id=self.dev.owner_id,
                        agent_id=self.dev.agent_id,
                        request_id=usage_svc.new_request_id(),
                        session_id=self.session_id,
                    )
                # ---- TTS 用量：在这里落库，**不在音频通路上** ----
                # speaker 在流式播报期间只把每句的 (ok, error, elapsed_ms)
                # append 进列表（纯内存），真正的 INSERT 攒到这里一次做完。
                # 为什么不能写在 _stream_sentence 里：那是在按 60ms 一帧推
                # 音频的同一个事件循环上，SQLite 一次 commit 若撞上锁就要等
                # busy_timeout（15s）—— 老人听到的是"说着说着卡住"。
                _flush_tts_usage(db, speaker=speaker, dev=self.dev,
                                 session_id=self.session_id)
                record_chat_usage(db, self.dev.id, local_date_str(),
                                  max(1, elapsed_ms // 1000),
                                  is_reminder=is_reminder)
                meta: dict[str, Any] = {
                    "session_id": self.session_id,
                    "asr_ms": t_asr,
                    "llm_ms": t_llm,
                    "tts_ms": tts_total_ms,
                    "stt_chars": len(utterance),
                    "reply_chars": len(reply),
                    "total_tokens": total_tokens,
                    "llm_rounds": len(results),
                    # ---- 2026-10-21 时延拆解 ----
                    "ttft_ms": ttft,
                    "round_ms": rounds,
                    "tool_ms": int(timing.get("tool_ms") or 0),
                    "tts_first_ms": tts_first_ms,
                    "tts_max_gap_ms": tts_max_gap,
                    "tts_max_pending": tts_max_pending,
                    "total_ms": elapsed_ms,
                    "dropped_sentences": dropped,
                    "claim_blocked": claim_blocked,
                    "clock_blocked": clock_blocked,
                }
                if dropped_reasons:
                    meta["dropped_reasons"] = sorted(set(dropped_reasons))
                if failed_tools:
                    meta["failed_tools"] = failed_tools
                if tool_trace:
                    meta["tool_calls"] = tool_trace
                if self._current_speaker:
                    # F3：认出了谁。只记显示名，不记分数与音频 —— 埋点是给
                    # 家属/运维看"个性化有没有生效"的，不是第二个声纹库。
                    meta["speaker"] = self._current_speaker
                db.add(_m.Interaction(
                    device_id=self.dev.id,
                    kind=_INTERACTION_KIND,
                    duration_ms=elapsed_ms,
                    meta=meta,
                ))
                db.commit()

        try:
            await run_in_threadpool(_write)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] 埋点写入失败（不影响本轮对话）：%s: %s",
                        self.session_id, type(exc).__name__, exc)

        # 记忆沉淀：异步、异常自吞，绝不阻塞也不能让本轮失败。
        # signal / prev_bot_text 由 _run_turn 在追加历史**之前**算好传进来：
        #   * signal=correction ⇒ 老人抢话打断（走 judge_feedback 的纠正闸门）；
        #   * signal=retry      ⇒ 老人在重复问同一件事（需要 prev_bot_text 成对）；
        #   * 空串              ⇒ 交给 judge_feedback 自己识别。
        # 之前这里两个都不传，等于每轮都只有"机器人的原话"这一个输入，
        # 判定结果恒为无效 —— 反馈闭环是断的。
        await self._sediment_feedback(utterance, reply, signal=signal,
                                      prev_bot_text=prev_bot_text)

    async def _record_turn(self, utterance: str, reply: str, started: float,
                           t_asr: int, t_llm: int, t_tts: int,
                           *, reply_ok: bool = True) -> None:
        """失败/降级轮的埋点（不调 LLM 或 LLM 失败时）。"""
        elapsed_ms = int((time.monotonic() - started) * 1000)

        def _write() -> None:
            with SessionLocal() as db:
                db.add(_m.Interaction(
                    device_id=self.dev.id,
                    kind=_INTERACTION_KIND,
                    duration_ms=elapsed_ms,
                    meta={
                        "session_id": self.session_id,
                        "asr_ms": t_asr,
                        "llm_ms": t_llm,
                        "tts_ms": t_tts,
                        "stt_chars": len(utterance),
                        "reply_chars": len(reply),
                        "total_tokens": 0,
                        "degraded": not reply_ok,
                    },
                ))
                db.commit()

        try:
            await run_in_threadpool(_write)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] 降级轮埋点失败：%s: %s", self.session_id,
                        type(exc).__name__, exc)


class _ReplySpeaker:
    """把回复**逐句**送 TTS、**边收音频边切 60ms 帧**下发。

    与旧实现的区别（2026-10-17 流式 TTS）：
      * 旧：LLM 整段返回 → 逐句合成（首句延迟 = 整段 LLM + 第一句 TTS）；
      * 新：LLM **流式**增量进来（:meth:`push_text`）→ 完整句子立即送 TTS
        → ``asr_tts.tts_stream`` 的分片边到边编码下发（首句延迟 =
        LLM 第一句 + TTS 第一个分片）。

    ⚠️ 下行协议时序与旧实现**逐字一致**（设备靠它们回落 idle，改坏的表现
    是设备卡在 speaking 态）：首句前 ``tts start`` → 每句
    ``sentence_start`` + ``llm emotion`` + 音频帧 → 收尾 ``tts stop``；
    一句都没播时也要发**成对的空 start/stop**。

    打断语义与旧实现一致：每句之间、每个分片、每一帧都检查 ``_abort``；
    **不重置** ``_abort``（清空时机只有新的 listen start 一处）。

    下行音频一律经过 :class:`~app.voice.audio_pacing.AudioPacer`
    --------------------------------------------------------
    2026-10-22 起，音频帧不再"能发多快发多快"地直接 await socket，而是
    先进节流器、由独立协程按 60ms 一帧的虚拟播放时钟匀速下发。三件事
    因此同时成立（见 audio_pacing 模块 docstring 的事故背景）：

    * **不再断续**：帧间隔由时钟决定，不再取决于 TCP 反压；
    * **不再拖慢 LLM**：``push_text`` 只入队就返回，socket 堵住不再
      反压到流式读取协程上；
    * **控制消息顺序不坏**：``tts start`` / ``sentence_start`` /
      ``llm emotion`` / ``tts stop`` 与音频帧共用同一条 FIFO，
      ``tts stop`` 因此不可能插到音频前面（那个 bug 的表现是每句话
      最后几个字被吃掉，而服务端日志一切正常）。
    """

    #: ``finish()`` 等音频排空的上限（秒）。超时就 WARNING 并发 stop ——
    #: 宁可截断也不能把会话挂死在这个等待上。正常一句语音几秒内必然排空。
    _DRAIN_TIMEOUT_SEC: Final[float] = 60.0

    def __init__(self, session: VoiceSession) -> None:
        self._s = session
        self._buf = ""            # 未成句的尾巴（含 <4 字待合并碎片）
        self._started = False     # tts start 是否已发
        self._tts_ok = True       # TTS 是否可用（禁用/连续失败 → False）
        self._voice = ""          # 音色（首句时惰性解析）
        self._voice_loaded = False
        self.sentence_count = 0   # 已播句数（日志/埋点用）
        self.pushed_chars = 0     # 累计推入的文本字符数（非流式兜底判重用）
        # ---- 2026-10-21 新增：可播性过滤与下行耗时埋点 ----
        self.dropped = 0          # 被安检拦下的句数（不可播的规划语/工具名）
        self.dropped_reasons: list[str] = []   # 拦截原因标签，进 meta
        # ---- 假成功闸门（2026-10-21）----
        # 本轮哪些提醒类工具失败了。非空 ⇒ _speak_sentence 会拦下
        # "记好了/已经建好"这类措辞，改念工具给的澄清原因。
        self.failed_tools: set[str] = set()
        self.claim_blocked = 0    # 被闸门拦下的句数（进 meta 与日志）
        self._claim_fallback_done = False
        #: 工具失败时给老人的人话（取最近一次失败工具的 message）。
        #: 假成功闸门改念的就是这一句。
        self._fail_message = ""
        # ---- 复述时间对账（2026-10-29）----
        #: 本轮 create_reminder **真正落库**的钟点 ``(hour12, minute)``。
        #: 空集 = 本轮没建提醒 ⇒ 闸门不启动（闲聊里提到钟点不该被拦）。
        #: 用集合而不是单值：一轮里可能建了两条（早一次晚一次）。
        self._reminder_clocks: set[tuple[int, int]] = set()
        #: 工具返回的口语时间（"今天中午12点55分"），拦下错误复述时念给老人听，
        #: 让他自己判断存的对不对。
        self._reminder_clock_speak = ""
        self.clock_blocked = 0    # 被时间对账闸门拦下的句数
        self._clock_fallback_done = False
        self.tts_first_ms = 0     # 首帧音频发出耗时（从 speaker 创建算起）
        self.tts_ms = 0           # 全部合成下发累计耗时
        self.max_gap_ms = 0       # 相邻两帧的最大间隔（>60ms 即供给不足）
        self._last_frame_at = 0.0
        self._t0 = time.monotonic()
        # ---- 用量流水（2026-10-29）----
        #: 本轮的 TTS 调用清单 ``[{ok, error, elapsed_ms}]``。
        #: **只收集、不落库**：TTS 是流式的，每句一次 ``tts_stream``，一次合成
        #: 又产出几十个分片 —— 在分片里写库就是每句几十次 commit 打在音频
        #: 通路上（SQLite 一旦等锁就是秒级卡顿）。收集是纯内存 append，
        #: 真正的 INSERT 交给 `_record_usage`（本来就在线程池里）。
        self._tts_calls: list[dict[str, Any]] = []

        # ---- 下行节流器（2026-10-22）----
        # 发送回调用 _send_frame：统计埋点全部留在这里，节流器只是
        # 决定"什么时候调它"。start() 幂等，协程随后台常驻。
        self._pacer = AudioPacer(
            self._send_frame,
            frame_ms=config.VOICE_FRAME_DURATION_MS,
            send_json=session._send_json,
        )
        self._pacer.start()
        # 队列积压峰值（帧）。节流器把帧间隔钉死之后，"TTS 供帧快不快"
        # 改看这个数：积压越深，整段音频越靠后，**最后一句延后越久**。
        self.max_pending = 0

        # abort 要能立刻作废队列里的帧（见 VoiceSession._on_abort）
        session._speaker = self

    # ---------------------------------------------------------------- 文本
    async def push_text(self, text: str) -> None:
        """喂一段 LLM 增量文本；其中完整的句子立即合成播放。"""
        if not text:
            return
        self.pushed_chars += len(text)
        self._buf += text
        while True:
            sentence, rest = _split_first_sentence(self._buf)
            if not sentence:
                break
            self._buf = rest
            await self._speak_sentence(sentence)

    async def flush_rest(self) -> None:
        """把缓冲区里**没有句末标点的尾巴**作为一句播出去。

        给固定话术用（兜底话术、空识别回复）：它们常常不带「。」，而
        :meth:`push_text` 只切完整句 —— 不 flush 的话这句话永远留在缓冲区，
        老人听到的是静音，而协议层只有一个空的 start/stop 配对，
        **没有任何断言会为此变红**（这正是它危险的地方）。
        """
        tail = self._buf.strip()
        self._buf = ""
        # 只剩标点（典型：整句已播完，缓冲区里留着那个"。"）时不要合成 ——
        # TTS 对单个标点会念出一声奇怪的停顿或杂音。判断口径与
        # _speak_sentence 里的一致，统一走 _PUNCT_ONLY。
        if tail and not _PUNCT_ONLY.fullmatch(tail):
            await self._speak_sentence(tail)

    # ---------------------------------------------------------------- 播放
    async def _speak_sentence(self, sentence: str, *,
                              _allow_claim: bool = False) -> None:
        s = self._s
        if s._abort:
            log.info("[%s] 播报被打断，丢弃句子 %r", s.session_id, sentence[:20])
            return

        # ---- 可播性安检（见模块顶部 _speech_block_reason 的说明）----
        # 必须在发 ``sentence_start`` **之前**判：那个消息带 text，设备会把它
        # 显示在屏幕上。拦下来的句子若已经发过 sentence_start，老人会在屏幕上
        # 看到"老人查天气，我要调用 get_weather"——比念出来更糟。
        text = _speech_clean(sentence)

        # ---- 情绪标记（F4）：行首 [情绪词]，剥掉再送后面的安检/闸门/TTS ----
        # 必须在 block 检查与假成功闸门**之前**剥：那两个判据看的是要念的
        # 文本，标记不该参与；字幕（sentence_start 的 text）也必须是剥过的。
        emotion, text = _extract_emotion(text)
        if not text or _PUNCT_ONLY.fullmatch(text):
            # 剥完标记只剩标点/空串：这一句整个是个情绪壳，没有可念内容。
            log.debug("[%s] 跳过只有情绪标记的片段 %r", s.session_id, sentence[:20])
            return
        reason = _speech_block_reason(text)
        if reason:
            self.dropped += 1
            self.dropped_reasons.append(reason)
            log.warning("[%s] 丢弃不可播报的句子（reason=%s）：%r",
                        s.session_id, reason, sentence[:80])
            return

        # ---- 假成功闸门 ----
        # 本轮有提醒类工具失败，而模型这句话在宣称"已经办好了"。
        # prompt 里的硬约束拦不住（模型照样违反，见文件头部事故记录），
        # 所以在这里拦：宁可少说一句，也不能让老人以为提醒建好了。
        # 注意递归调用时传 _allow_claim=True —— 兜底话术本身是反问句，
        # 不含成功措辞，但显式放开比"赌它不命中"更诚实。这个开关同时
        # 关掉下面的**时间对账**闸门（两个兜底话术都不该被二次拦截）。
        if (not _allow_claim and self.failed_tools
                and _SUCCESS_CLAIM_RE.search(text)):
            self.claim_blocked += 1
            log.warning("[%s] 拦住假成功播报（本轮失败工具 %s）：%r",
                        s.session_id, sorted(self.failed_tools), text[:80])
            if not self._claim_fallback_done:
                self._claim_fallback_done = True
                await self._speak_sentence(
                    self._fail_message or _CLAIM_FALLBACK, _allow_claim=True)
            return

        # ---- 复述时间对账闸门（2026-10-29）----
        # 本轮建过提醒（_reminder_clocks 非空），这句话里报了钟点，而报的钟点
        # **一个都对不上**真正落库的时间 ⇒ 模型说的和存的不是一回事。
        # 念出去老人会记住错时间，比不念危险得多。
        #
        # 只在这一轮建过提醒时判：闲聊里老人说「今天下午三点孙子来」不该被拦。
        if not _allow_claim and self._reminder_clocks:
            mentions = clock_mentions(text)
            if mentions and not (set(mentions) & self._reminder_clocks):
                self.clock_blocked += 1
                log.warning("[%s] 拦住时间不符的复述（模型说 %s，实际存 %s）：%r",
                            s.session_id, mentions,
                            sorted(self._reminder_clocks), text[:80])
                if not self._clock_fallback_done:
                    self._clock_fallback_done = True
                    await self._speak_sentence(
                        _CLOCK_MISMATCH_FALLBACK.format(
                            clock=self._reminder_clock_speak
                            or "刚才说的那个时间"),
                        _allow_claim=True)
                return

        if not self._started:
            # 控制消息走节流器的插队通道：立即发、不占播放时间，且与音频帧
            # 共用同一条 FIFO —— 顺序因此有保证（旧实现直接 await _send_json，
            # 在音频开始节流之后会插到队列前头去）。
            self._pacer.add_json({"type": "tts", "state": "start"})
            self._started = True
            s.state = "speaking"
            # 全双工打断的 grace 期从**第一句话开口**算起（F2）。
            s._speaking_started_at = time.monotonic()
            s._barge_speech_ms = 0
            s._barge_silence_ms = 0
        self.sentence_count += 1

        self._pacer.add_json({"type": "tts", "state": "sentence_start",
                              "text": text})
        # 情绪随句走：设备端 application.cc 的 llm 分支据此调 SetEmotion，
        # 词表与固件内置 21 个表情一一对应（见 _EMOTION_NAMES 注释）。
        # 以前这里写死 "neutral"—— 设备有整套表情却永远只显示一张脸。
        self._pacer.add_json({"type": "llm", "emotion": emotion})

        # 单句 TTS 失败：跳过该句音频、后续句继续。
        # 不能整轮失败 —— 第一句合成失败而后面的句子都成功是常见的
        # （厂商对长句偶发 500），老人听到的是少一句，不是 silence。
        t_one = time.monotonic()
        try:
            await self._stream_sentence(text)
        except asr_tts.VoiceConfigMissingError:
            log.info("[%s] 未配置 TTS，只发文本", s.session_id)
            self._tts_ok = False
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 必须同时把 _tts_ok 置 False：这一轮**没有任何音频**发出去，
            # 而 finish() 的收尾日志本来只看这个标志。2026-10-20 的事故就是
            # 「TTS 抛 TypeError 两次、日志仍写『播报完成（2 句，音频已发）』」，
            # 排查时被这行日志带偏了两轮。
            self._tts_ok = False
            log.warning("[%s] TTS 失败，跳过音频：%s: %s",
                        s.session_id, type(exc).__name__, exc)
        finally:
            # 逐句计时：这是判断"慢在 TTS 还是慢在 LLM"的唯一依据。
            # 串行合成下这个数会直接累加进总时延，所以必须单独可见。
            self.tts_ms += int((time.monotonic() - t_one) * 1000)

    async def _send_frame(self, packet: bytes) -> None:
        """下发一帧音频，并记下**首帧**时刻与**帧间隔**。

        ``tts_first_ms`` 是"老人到底等了多久才听到第一个字"的直接度量：
        它包含 LLM 出首句 + TTS 首分片 + 编码，与 ``tts_ms``（纯合成耗时）
        分开记，才能区分"模型慢"和"TTS 慢"。0 表示一帧都没发出去。

        ``max_gap_ms`` 回答另一个问题：「断断续续」到底怪谁。
        每帧承载 60ms 音频，所以**服务端相邻两帧的间隔明显超过 60ms 就是
        供给不足**（TTS 分片慢/上游卡顿，或设备侧 socket 读不动），
        老人耳朵里就是一顿一顿；反之若间隔都贴近 60ms，说明音频是按播放
        节奏走的，断续只可能发生在设备侧（解码任务被抢 CPU 导致 I2S 欠载，
        听感是咔哒声 + 掉字）。

        2026-10-22 起这个回调由**节流器**按 60ms 一帧调用（见
        :class:`~app.voice.audio_pacing.AudioPacer`），所以正常情况下它
        测到的是"≈60ms"而不是"远小于 60ms"—— 突发灌帧的老路已经没了。
        于是这个数的含义变回它本该有的样子：**显著大于 60ms = 有一次
        供给断档**（设备至少 60ms 没有声音可播）。
        """
        now = time.monotonic()
        if not self.tts_first_ms:
            self.tts_first_ms = int((now - self._t0) * 1000)
        elif self._last_frame_at:
            gap = int((now - self._last_frame_at) * 1000)
            if gap > self.max_gap_ms:
                self.max_gap_ms = gap
        self._last_frame_at = now
        await self._s._send_audio(packet)

    async def _stream_sentence(self, sentence: str) -> None:
        """流式合成一句并逐帧**入队**下发。**帧间/分片间都检查 abort**。

        帧是交给节流器的（``_enqueue``），不是直接 await socket：把 socket
        等待留在这里，一来帧间隔不可控（断续），二来反压会顺着
        ``push_text`` 一路堵住 LLM 的流式读取（响应慢）。见
        ``audio_pacing`` 模块 docstring。
        """
        s = self._s
        if not self._voice_loaded:
            self._voice_loaded = True

            def _voice() -> str:
                with SessionLocal() as db:
                    return asr_tts.pick_voice(db, s.dev)

            try:
                self._voice = await run_in_threadpool(_voice)
            except Exception as exc:  # noqa: BLE001
                log.debug("[%s] 音色解析失败，用厂商默认：%s", s.session_id, exc)

        db = SessionLocal()   # 本句专用：生成器只在 next() 里碰 DB
        gen = None
        carry = b""            # 跨分片的不足一帧残留
        frame_bytes = opus_codec.frame_bytes(config.VOICE_OUTPUT_SAMPLE_RATE)
        # 用量流水：**整句记一条**（不是每个分片一条）。时间在这里掐表，
        # 落库在 _record_usage 里 —— 见 self._tts_calls 的注释。
        t_tts = time.monotonic()
        tts_ok = True
        tts_error = ""
        try:
            def _open() -> Any:
                return asr_tts.tts_stream(db, sentence, voice=self._voice)

            gen = await run_in_threadpool(_open)
            while True:
                if s._abort:
                    log.info("[%s] 帧级打断", s.session_id)
                    self._pacer.reset()   # 作废已入队未发出的帧
                    return
                # ⚠️ 同 _chat_round：用哨兵而不是捕获 StopIteration，
                # StopIteration 穿过协程边界会被转成 RuntimeError。
                # tts_stream 只会 yield 字节，None 只可能是生成器被关闭。
                chunk = await run_in_threadpool(next, gen, None)
                if chunk is None:
                    break
                if not chunk:
                    continue
                carry += chunk
                while len(carry) >= frame_bytes:
                    if s._abort:
                        log.info("[%s] 帧级打断", s.session_id)
                        self._pacer.reset()
                        return
                    frame = carry[:frame_bytes]
                    carry = carry[frame_bytes:]
                    try:
                        packet = opus_codec.encode_opus(
                            frame, config.VOICE_OUTPUT_SAMPLE_RATE)
                    except opus_codec.OpusUnavailableError:
                        # 无 libopus：音频发不了，但文本已经发了。
                        log.debug("[%s] 无 libopus，跳过音频帧", s.session_id)
                        self._pacer.reset()
                        return
                    if packet:
                        self._enqueue(packet)
            # 收尾：不足一帧的补零截齐（与 encode_opus 的口径一致）
            if carry and not s._abort:
                try:
                    packet = opus_codec.encode_opus(
                        carry, config.VOICE_OUTPUT_SAMPLE_RATE)
                except opus_codec.OpusUnavailableError:
                    return
                if packet:
                    self._enqueue(packet)
        except Exception as exc:
            # 只**记录**失败原因，异常照旧上抛（_speak_sentence 有兜底）。
            # 用量页要能区分「合成过但失败」与「根本没调用过」。
            tts_ok = False
            tts_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            # 整句结束后记一条（纯内存 append，**不碰数据库**）。
            # 放在 finally 里是为了让打断 / 无 libopus 提前 return 的路径
            # 也有记录 —— 那些路径确实调用过 TTS。
            self._tts_calls.append({
                "ok": tts_ok,
                "error": tts_error,
                "elapsed_ms": int((time.monotonic() - t_tts) * 1000),
            })
            if gen is not None:
                try:
                    await run_in_threadpool(gen.close)
                except Exception:  # noqa: BLE001
                    pass
            db.close()

    def _enqueue(self, packet: bytes) -> None:
        """把一帧交给节流器，并记录队列积压峰值。

        **不 await socket**：等待是节流器协程的事。这就是"断续"与
        "响应慢"的共同修法 —— 详见 ``audio_pacing`` 模块 docstring。
        """
        self._pacer.add_audio(packet)
        pending = self._pacer.pending
        if pending > self.max_pending:
            self.max_pending = pending

    async def finish(self) -> None:
        """收尾：等音频排空 → 发 tts stop（或成对的空 start/stop）。

        设备侧 application.cc 只在收到 stop 时回落 listening/idle ——
        **一对都不能少**，否则老人看到的是一直"说话中"的音箱。

        ⚠️ 排空等待是 2026-10-22 加的：音频帧现在由节流器按 60ms 一帧
        慢慢发，``finish()`` 被调用时队列里通常还压着十几帧。不等就发
        stop，设备会**在话音中途停下**（每句话最后几个字被吃掉），
        而服务端日志一片正常 —— 正是最难查的那类故障。
        """
        s = self._s
        try:
            if self._started:
                if self._pacer.broken:
                    # 音频通道已死（连续发送失败）：这一轮其实没有声音，
                    # 必须把结论写进日志和 meta，不能假装"音频已发"。
                    self._tts_ok = False
                    log.error("[%s] 音频下行通道已死，本轮音频并未真正发出",
                              s.session_id)
                elif not await self._pacer.wait_idle(
                        timeout=self._DRAIN_TIMEOUT_SEC):
                    log.warning("[%s] 音频队列 %.0fs 未排空就发 tts stop"
                                "（仍有 %d 帧待发），话音尾部可能被截断",
                                s.session_id, self._DRAIN_TIMEOUT_SEC,
                                self._pacer.pending)
                await s._send_json({"type": "tts", "state": "stop"})
            else:
                # 一句都没发（空回复 / 全被 abort 或 TTS 失败吞掉）：
                # 仍然要 start/stop 成对。
                await s._send_json({"type": "tts", "state": "start"})
                await s._send_json({"type": "tts", "state": "stop"})
            s.state = "idle"
            log.info("[%s] 播报完成（%d 句，音频%s）",
                     s.session_id, self.sentence_count,
                     "不可用" if not self._tts_ok else "已发")
            if self.dropped:
                # 单独一行、WARNING 级：被拦下的句子是**真实发生过的事故痕迹**，
                # 藏在 INFO 里就没人看，而它正是"老人说设备没回答"的答案。
                log.warning("[%s] 本轮有 %d 句因不可播报被拦下（原因：%s）",
                            s.session_id, self.dropped,
                            ",".join(sorted(set(self.dropped_reasons))))
        finally:
            # 节流协程必须停：它持有 WebSocket 引用，每轮泄漏一个的话，
            # 长时间运行的服务会慢慢吃掉内存，且没有任何报错。
            await self._pacer.stop(drain=False)
            self._detach()

    async def drain(self) -> None:
        """等已入队的音频帧真的走下 socket（**不停发送协程**）。

        给"这句话到底播完了没"的判定用（测试、以及任何想在下一次
        协议动作前确认音频已送达的调用方）。不加超时：队列只会在
        节流协程活着的时候排空，协程死掉时 ``wait_idle`` 永远不返回 ——
        那本身就是异常，宁可挂住也不要靜靜地少播。
        """
        await self._pacer.wait_idle()

    async def close(self) -> None:
        """停掉节流协程，**不发任何协议消息**。

        给"本轮提前返回"的路径用（LLM 未配置 / LLM 抛异常 / 空回复）：
        那些路径原本就不发 ``tts stop``，这里保持原样，只把协程收掉。
        """
        await self._pacer.stop(drain=False)
        self._detach()

    def _detach(self) -> None:
        """把自己从会话上摘下来（``_on_abort`` 会摸 ``_speaker``）。"""
        if getattr(self._s, "_speaker", None) is self:
            self._s._speaker = None


def _split_first_sentence(buf: str) -> tuple[str, str]:
    """从缓冲区头部切出第一个完整句（**增量版**）。

    流式场景下文本是一点点到的，不能等整段再切（那就退回非流式了）。
    规则与 :func:`_split_sentences` 一致：句末标点留在前一句、<4 字的
    碎片不切（等后面的文本拼上）—— 不合并的话，TTS 对 1-3 字的句子会念出
    明显的"起头-收尾"爆破音，老人听到的是"…的药。"这种断裂。

    返回 ("", buf) 表示还没有完整句（调用方应继续攒文本）。
    """
    m = _SENTENCE_SPLIT.search(buf)
    if not m:
        return "", buf
    head = buf[:m.start()]
    rest = buf[m.start():]
    if len(head.strip()) < _MIN_SENTENCE_CHARS and rest.strip():
        # 太短：并回后面。缓冲不消耗，等更多文本到了再切。
        return "", buf
    return head.strip(), rest


def _split_sentences(reply: str) -> list[str]:
    """按句末标点切句，<4 字的碎片并回前一句。"""
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(reply) if p.strip()]
    if not parts:
        return [reply.strip()] if reply.strip() else []
    merged: list[str] = []
    for part in parts:
        if merged and len(part) < _MIN_SENTENCE_CHARS:
            merged[-1] = merged[-1] + part
        else:
            merged.append(part)
    return merged
