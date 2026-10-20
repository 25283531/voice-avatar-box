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
4. **音频帧的收与发都不经过 asyncio 队列做缓冲**：上行直接进
   ``list[bytes]``（60ms 一帧，一轮最多几十帧，内存可忽略）；
   下行边合成边发（流式 TTS，见 :class:`_ReplySpeaker`）。

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
import json
import logging
import re
import time
from typing import Any, Final

from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocket, WebSocketDisconnect

from .. import config
from ..db import SessionLocal
from ..llm_gateway import ConfigMissingError, chat_stream
from ..macs import norm_mac
from ..chat_quota import local_date_str, quota_snapshot, record_chat_usage
from ..memory import bridge as memory_bridge
from ..models import InteractionKind
from ..security import decode_token
from .. import models as _m
from .. import usage as usage_svc
from . import asr_tts, opus_codec
from .prompts import (
    BASE_VOICE_PROMPT,
    DEVICE_UNBOUND_MESSAGE,
    EMPTY_UTTERANCE_REPLY,
    LLM_NOT_CONFIGURED_MESSAGE,
    TOOL_LOOP_FALLBACK,
)
from .tools import TOOL_DEFS, execute_tool

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

# 每轮对话记 Interaction 的 kind。用 DIALOG（=1）而不是新值：
# schemas.InteractionReportIn.kind 是 int，取值集合由
# models.InteractionKind 定义，新增枚举值要连 APP 统计口径一起改。
_INTERACTION_KIND: Final[int] = int(InteractionKind.DIALOG)


def _now_ms() -> int:
    return int(time.time() * 1000)


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

        # ---- 服务端 VAD 状态（老人语速慢 / 轻声 / 远场，见常量区注释）----
        self._noise_floor: float = _NOISE_FLOOR_INIT   # 房间底噪跟随值
        self._ms_since_loud: int = 10 ** 9             # 距最后一个 enter 级帧多久
        self._max_closed_pause_ms: int = 0             # 本轮"停顿后又继续"的最长停顿

        # 会话内对话历史（[{"role":..,"content":..}]），仅内存，不落库；
        # 跨连接的历史由记忆模块管（recent_turns），这里不重复存一份。
        self._history: list[dict[str, str]] = []

        # 打断标志。asyncio 单线程事件循环里 bool 读写是原子的，
        # 不需要锁；但所有 await 点都要重新读它（见 _speak_reply 的循环）。
        self._abort: bool = False

        # 当前这一轮对话的 task。**必须与收包主循环解耦**，否则播报期间
        # 读不到上行消息、abort 失效（见 run() 里的注释）。
        self._turn_task: asyncio.Task | None = None

        self._t0: float = 0.0               # 本轮 listen start 时刻

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

        await self._send_json({
            "type": "hello",
            "transport": "websocket",
            "session_id": self.session_id,
            "audio_params": {
                "sample_rate": config.VOICE_OUTPUT_SAMPLE_RATE,
                "frame_duration": config.VOICE_FRAME_DURATION_MS,
            },
        })
        log.info("[%s] 语音会话开始（设备 %s，协议 v%d）",
                 self.session_id, getattr(self.dev, "device_id", "?"),
                 self.protocol_version)

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
            if self._turn_task is not None and not self._turn_task.done():
                # 断开时把本轮标成打断：否则它还会继续往下发音频帧，
                # 对已断开的连接每帧抛一次异常（刷满日志且掩盖真因）。
                self._abort = True
                self._turn_task.cancel()

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
        if self.state != "listening":
            # 非 listening 态收到的音频：设备在播报时也可能继续推流
            # （固件不停止麦克风）。丢弃即可，不回错误 —— 回错误会让
            # 设备以为协议出错而重连。
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
            self._silence_ms = 0
            self._ms_since_loud = 0
        elif rms >= keep and self._ms_since_loud <= config.VOICE_SOFT_GRACE_MS:
            # 3) 软语音：开口后的轻声延续。**不刷新 _ms_since_loud** ——
            #    刷新它等于让持续底噪无限续期，会话永远不结束。
            self._speech_ms += frame_ms
            self._silence_ms = 0
            self._ms_since_loud += frame_ms
        else:
            # 4) 静音
            self._silence_ms += frame_ms
            self._ms_since_loud += frame_ms

    def _silence_threshold_ms(self) -> int:
        """本轮的自适应静音阈值（见常量区注释的第三件套）。"""
        adaptive = (self._max_closed_pause_ms * config.VOICE_PAUSE_FACTOR
                    + _PAUSE_MARGIN_MS)
        return int(max(config.VOICE_SILENCE_MS,
                       min(config.VOICE_SILENCE_MAX_MS, adaptive)))

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
            # 设备本地工具通道（JSON-RPC 2.0，设备当 MCP server）。我们的
            # 提醒数据在服务端，工具调用走服务端 function calling 路线
            # （见模块 docstring 与 tools.py），与本消息无关：只记 debug
            # 日志并忽略，不返回任何响应。
            log.debug("忽略 mcp 消息（设备本地工具通道，与服务端工具调用无关）：%r",
                      str(obj.get("payload"))[:200])
        else:
            log.debug("忽略未知消息类型 %r", mtype)

    async def _on_listen(self, obj: dict[str, Any]) -> None:
        state = obj.get("state")
        if state == "start":
            mode = str(obj.get("mode") or "auto")
            self._frames.clear()
            self._pcm.clear()
            self._speech_ms = 0
            self._silence_ms = 0
            self._noise_floor = _NOISE_FLOOR_INIT
            self._ms_since_loud = 10 ** 9
            self._max_closed_pause_ms = 0
            self._abort = False
            self._t0 = time.monotonic()
            self.state = "listening"
            log.info("[%s] listen start（mode=%s）", self.session_id, mode)
            # 服务端静音兜底：单独起一个 task，不阻塞收包主循环。
            # realtime 模式固件不做 VAD、等我们断句；auto 模式设备先到
            # 就以设备为准（谁先到谁 finalize，见 _finalize 的幂等设计）。
            asyncio.create_task(self._silence_watchdog())
        elif state == "stop":
            log.info("[%s] listen stop", self.session_id)
            # 起 task 而非 await：见 run() 里的注释（await 会卡住收包
            # 主循环，abort 就再也读不到）。
            self._start_turn()
        elif state == "detect":
            # 唤醒词通知：固件在检测到唤醒词时发。只需记录 + 重置缓冲
            # （设备随后会发 listen start，这里不清也行，但清了更稳：
            # 唤醒前若有残余帧，会被当成这一句的开头）。
            log.info("[%s] 唤醒词：%s", self.session_id, obj.get("text"))
            self._frames.clear()
            self._pcm.clear()
            self._speech_ms = 0
            self._silence_ms = 0
            self._noise_floor = _NOISE_FLOOR_INIT
            self._ms_since_loud = 10 ** 9
            self._max_closed_pause_ms = 0

    def _on_abort(self, obj: dict[str, Any]) -> None:
        reason = obj.get("reason", "")
        log.info("[%s] abort（reason=%s）", self.session_id, reason)
        self._abort = True
        # 打断只影响"正在播的音频"，不影响已经结束的 ASR/LLM：
        # 那两个已经拿到结果，重来一次只是多花一次 token。
        self._frames.clear()
        if self.state == "speaking":
            self.state = "idle"

    async def _silence_watchdog(self) -> None:
        """连续静音达到阈值就自动 finalize（等价于设备端 VAD 断句）。

        阈值是**自适应**的（:meth:`_silence_threshold_ms`）：本轮出现过长
        停顿就多等。另有两道安全阀：
          * ``VOICE_MAX_LISTEN_MS``：老人说着离开/卡住时兜底，不让会话
            永远挂在 listening（realtime 模式下设备不自己发 stop）；
          * ``VOICE_MIN_SPEECH_MS``：一句都没说就不触发，防止底噪被当成
            一整句送 ASR。
        """
        try:
            while self.state == "listening":
                await asyncio.sleep(0.2)
                if self.state != "listening":
                    return
                threshold = self._silence_threshold_ms()
                if self._silence_ms >= threshold and \
                        self._speech_ms >= config.VOICE_MIN_SPEECH_MS:
                    log.info("[%s] 服务端静音兜底触发（连续静音 %dms ≥ 阈值 %dms，"
                             "累计语音 %dms，底噪 %.4f）",
                             self.session_id, self._silence_ms, threshold,
                             self._speech_ms, self._noise_floor)
                    self._start_turn()
                    return
                # 收听超时兜底：阈值再自适应也有上限，而这一个管的是
                # "一直不满足条件"的极端情形（老人离开、设备持续送接近
                # 门限的能量）。到了就直接 finalize，ASR 认出多少算多少。
                elapsed_ms = int((time.monotonic() - self._t0) * 1000)
                if elapsed_ms >= config.VOICE_MAX_LISTEN_MS:
                    log.info("[%s] 收听超 %ds 兜底 finalize（累计语音 %dms）",
                             self.session_id, config.VOICE_MAX_LISTEN_MS // 1000,
                             self._speech_ms)
                    self._start_turn()
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] 静音兜底异常：%s: %s", self.session_id,
                        type(exc).__name__, exc)

    def _start_turn(self) -> None:
        """起一个 task 跑本轮对话；已有在跑的那轮就直接返回。

        **幂等的两道闸**（缺一不可）：
        1. 这里：``_turn_task`` 还没跑完就再起一轮，等于同一段音频
           识别两遍、答两遍、记两份 token；
        2. :meth:`_finalize` 里的 ``state != "listening"`` —— 因为
           ``create_task`` 不会立刻执行，第二个 stop 消息可能在第一个
           task 真正跑起来之前就到了，那时 state 还是 listening。

        为什么用 ``create_task`` 而不是直接 await：见 :meth:`run` 的注释
        （await 会卡住收包主循环 ⇒ abort 失效）。
        """
        if self._turn_task is not None and not self._turn_task.done():
            log.debug("[%s] 本轮仍在进行，忽略重复的 finalize 触发", self.session_id)
            return
        self._turn_task = asyncio.create_task(self._finalize())

    async def _finalize(self) -> None:
        """结束本轮收音并跑完整链路：ASR → 配额 → LLM → TTS。

        **幂等**：设备 stop 与静音兜底可能同时到（甚至 stop 先到、兜底
        后到），后到的那次必须直接返回，否则同一段音频会识别两遍、
        答两遍、记两份用量。用 ``_pcm`` 是否已消费来判。
        """
        if self.state != "listening":
            return
        self.state = "thinking"
        # ``_frames``（Opus 裸包）保留在成员上不清：abort 时 _on_abort 要清它，
        # 而这里只用 ``_pcm``（已解码）送 ASR。故意不把 frames 传下去 ——
        # 下游只需要 PCM，多传一个参数就多一处"该清哪个"的歧义。
        pcm = list(self._pcm)
        self._frames.clear()
        self._pcm.clear()

        started = time.monotonic()
        try:
            await self._run_turn(pcm, started)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] 本轮异常：%s: %s", self.session_id,
                          type(exc).__name__, exc)
            await self._send_alert("刚才没处理好，请您再说一遍")
        finally:
            if self.state == "thinking":
                self.state = "idle"

    async def _run_turn(self, pcm: list[bytes], started: float) -> str:
        t_asr = _now_ms()

        def _asr() -> str:
            with SessionLocal() as db:
                return asr_tts.asr(db, b"".join(pcm))

        try:
            utterance = await run_in_threadpool(_asr)
        except asr_tts.VoiceConfigMissingError:
            log.warning("[%s] 未配置 ASR", self.session_id)
            await self._send_alert("我现在听不见，请让家人在管理后台配置好语音识别")
            return ""
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] ASR 失败：%s: %s", self.session_id,
                        type(exc).__name__, exc)
            await self._send_alert("刚才没听清，请您再说一遍")
            return ""

        if not utterance:
            # ASR 没认出东西：不调 LLM（让模型根据空文本编一句只会产出
            # 更空的客套，且白花一次配额与 token），直接念固定回复。
            log.info("[%s] 识别结果为空", self.session_id)
            await self._speak_reply(EMPTY_UTTERANCE_REPLY, started)
            return ""

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
            await self._record_turn(utterance, "", started, t_asr,
                                    0, 0, 0, reply_ok=False)
            return utterance

        # ---- 记忆 + 人格召回
        def _recall() -> str:
            with SessionLocal() as db:
                return memory_bridge.recall_for_reply(
                    db, self.dev.owner_id, self.dev.agent_id, utterance,
                    session_id=self.session_id)

        try:
            memory_ctx = await run_in_threadpool(_recall)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] 记忆召回失败，按无记忆回答：%s: %s",
                        self.session_id, type(exc).__name__, exc)
            memory_ctx = ""

        system = BASE_VOICE_PROMPT
        if memory_ctx:
            system = system + "\n\n" + memory_ctx

        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        messages.extend(self._history[-config.VOICE_HISTORY_TURNS * 2:])
        messages.append({"role": "user", "content": utterance})

        # ---- LLM（流式 + 工具调用循环）
        # 循环上限见 config.VOICE_TOOL_MAX_ROWS：LLM 反复调用工具时每轮都是
        # 真金白银的 token，不设上限等于把账单交给模型的坏心情。
        t_llm = _now_ms()
        speaker = _ReplySpeaker(self)
        results: list[Any] = []          # 每轮一个 ChatResult（逐轮记账）
        tool_trace: list[dict[str, Any]] = []
        reply = ""
        try:
            for round_no in range(1, config.VOICE_TOOL_MAX_ROUNDS + 1):
                if self._abort:
                    log.info("[%s] 思考/播报期间收到 abort，停止后续轮次",
                             self.session_id)
                    break
                result = await self._chat_round(messages, speaker, round_no)
                if result is not None:
                    results.append(result)
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
                    outcome = await self._run_tool(call)
                    tool_trace.append({
                        "round": round_no,
                        "name": str((call.get("function") or {}).get("name") or ""),
                        "ok": bool(outcome.get("ok")),
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "name": str((call.get("function") or {}).get("name") or ""),
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
            await self._send_alert(LLM_NOT_CONFIGURED_MESSAGE)
            return utterance
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] LLM 调用失败：%s: %s", self.session_id,
                        type(exc).__name__, exc)
            await self._send_alert("我刚才走神了，请您再说一遍")
            return utterance

        t_llm = _now_ms() - t_llm
        if not reply:
            if self._abort:
                # 打断轮：已播的由 speaker 收尾，不进历史不记账
                await speaker.finish()
                return utterance
            log.warning("[%s] LLM 返回空文本", self.session_id)
            await self._send_alert("我没想好怎么回答，请您再说一遍")
            return utterance

        # 会话内历史：**只在本轮成功拿到回复后**才追加。失败轮不进历史，
        # 否则下一轮的上下文里会有一条没有回复的 user 消息，
        # 模型会以为用户的话被忽略了。工具轮的中间文本不进历史 ——
        # 4 轮窗口很紧，留给"老人问/小暖答"更有价值。
        self._history.append({"role": "user", "content": utterance})
        self._history.append({"role": "assistant", "content": reply})

        # ---- 埋点 + 记忆沉淀（都不阻塞播报）
        await self._record_usage(results, utterance, reply, started, t_asr, t_llm,
                                 tool_trace)

        # ---- 收尾播报：流式过程中已播的句子不管，这里发 tts stop
        #    （或空回复的 start/stop 配对，设备靠它回落 idle）。
        await speaker.finish()
        return utterance

    async def _chat_round(self, messages: list[dict[str, Any]],
                          speaker: _ReplySpeaker, round_no: int) -> Any:
        """跑一轮 LLM（流式）。文本增量实时交给 speaker 逐句播放。

        阻塞生成器（SSE 读取）不能直接在事件循环里跑，按项目既有口径
        ``run_in_threadpool`` 逐事件拉取 —— 拉事件的间隙事件循环是自由的，
        设备的上行消息（abort）不会被这轮生成挡住。

        返回 None 表示"中途被 abort 叫停"（没有完整的 ChatResult）。
        """
        db = SessionLocal()   # 本回合专用：生成器只在首个 next() 里碰 DB
        gen = None
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
                event = await run_in_threadpool(next, gen, None)
                if event is None:
                    return None
                kind, payload = event
                if kind == "done":
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
                    await speaker.push_text(payload)
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

        def _run() -> dict[str, Any]:
            with SessionLocal() as db:
                return execute_tool(db, self.dev.id, name, raw_args)

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


    # ================================================================ 埋点
    async def _record_usage(self, results: list[Any], utterance: str, reply: str,
                            started: float, t_asr: int, t_llm: int,
                            tool_trace: list[dict[str, Any]] | None = None) -> None:
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
                record_chat_usage(db, self.dev.id, local_date_str(),
                                  max(1, elapsed_ms // 1000),
                                  is_reminder=is_reminder)
                meta: dict[str, Any] = {
                    "session_id": self.session_id,
                    "asr_ms": t_asr,
                    "llm_ms": t_llm,
                    "tts_ms": 0,
                    "stt_chars": len(utterance),
                    "reply_chars": len(reply),
                    "total_tokens": total_tokens,
                    "llm_rounds": len(results),
                }
                if tool_trace:
                    meta["tool_calls"] = tool_trace
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

        # 记忆沉淀：异步、异常自吞，绝不阻塞也不能让本轮失败
        try:
            memory_bridge.remember_after_reply(
                SessionLocal, self.dev.owner_id, self.dev.agent_id,
                [{"role": "user", "content": utterance},
                 {"role": "assistant", "content": reply}],
                session_id=self.session_id,
                user_text=utterance, bot_text=reply,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] 记忆沉淀启动失败：%s: %s", self.session_id,
                        type(exc).__name__, exc)

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
    """

    def __init__(self, session: VoiceSession) -> None:
        self._s = session
        self._buf = ""            # 未成句的尾巴（含 <4 字待合并碎片）
        self._started = False     # tts start 是否已发
        self._tts_ok = True       # TTS 是否可用（禁用/连续失败 → False）
        self._voice = ""          # 音色（首句时惰性解析）
        self._voice_loaded = False
        self.sentence_count = 0   # 已播句数（日志/埋点用）
        self.pushed_chars = 0     # 累计推入的文本字符数（非流式兜底判重用）

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
        if tail:
            await self._speak_sentence(tail)

    # ---------------------------------------------------------------- 播放
    async def _speak_sentence(self, sentence: str) -> None:
        s = self._s
        if s._abort:
            log.info("[%s] 播报被打断，丢弃句子 %r", s.session_id, sentence[:20])
            return
        if not self._started:
            await s._send_json({"type": "tts", "state": "start"})
            self._started = True
            s.state = "speaking"
        self.sentence_count += 1

        await s._send_json({"type": "tts", "state": "sentence_start",
                            "text": sentence})
        await s._send_json({"type": "llm", "emotion": "neutral"})

        # 单句 TTS 失败：跳过该句音频、后续句继续。
        # 不能整轮失败 —— 第一句合成失败而后面的句子都成功是常见的
        # （厂商对长句偶发 500），老人听到的是少一句，不是 silence。
        try:
            await self._stream_sentence(sentence)
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

    async def _stream_sentence(self, sentence: str) -> None:
        """流式合成一句并逐帧下发。**帧间/分片间都检查 abort**。"""
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
        try:
            def _open() -> Any:
                return asr_tts.tts_stream(db, sentence, voice=self._voice)

            gen = await run_in_threadpool(_open)
            while True:
                if s._abort:
                    log.info("[%s] 帧级打断", s.session_id)
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
                        return
                    frame = carry[:frame_bytes]
                    carry = carry[frame_bytes:]
                    try:
                        packet = opus_codec.encode_opus(
                            frame, config.VOICE_OUTPUT_SAMPLE_RATE)
                    except opus_codec.OpusUnavailableError:
                        # 无 libopus：音频发不了，但文本已经发了。
                        log.debug("[%s] 无 libopus，跳过音频帧", s.session_id)
                        return
                    if packet:
                        await s._send_audio(packet)
            # 收尾：不足一帧的补零截齐（与 encode_opus 的口径一致）
            if carry and not s._abort:
                try:
                    packet = opus_codec.encode_opus(
                        carry, config.VOICE_OUTPUT_SAMPLE_RATE)
                except opus_codec.OpusUnavailableError:
                    return
                if packet:
                    await s._send_audio(packet)
        finally:
            if gen is not None:
                try:
                    await run_in_threadpool(gen.close)
                except Exception:  # noqa: BLE001
                    pass
            db.close()

    async def finish(self) -> None:
        """收尾：发 tts stop（或成对的空 start/stop）。

        设备侧 application.cc 只在收到 stop 时回落 listening/idle ——
        **一对都不能少**，否则老人看到的是一直"说话中"的音箱。
        """
        s = self._s
        if self._started:
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
