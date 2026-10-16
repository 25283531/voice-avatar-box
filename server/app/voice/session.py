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
   下行边合成边发（流式 TTS 是下一轮增量）。

⚠️ 关于 MCP（``{"type":"mcp"}``）
--------------------------------
**V1 只记 debug 日志并忽略**。MCP 工具集成（让老人说"提醒我三点吃药"
就走提醒解析链路）是明确的后续增量：它需要工具注册、参数抽取、与
reminder 模块的双写，塞进本轮会让"能说话"这个最基本的能力一起延期。
这里**不假装支持** —— 不返回 ``{"type":"mcp", ...}`` 的响应，设备侧
的 ``McpServer::ParseMessage`` 收不到东西，行为与未发送完全相同。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Final

from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocket, WebSocketDisconnect

from .. import config
from ..db import SessionLocal
from ..llm_gateway import ConfigMissingError, chat
from ..membership import local_date_str, quota_snapshot, record_chat_usage
from ..memory import bridge as memory_bridge
from ..models import InteractionKind
from ..security import decode_token
from .. import models as _m
from .. import usage as usage_svc
from . import asr_tts, opus_codec
from .prompts import (
    BASE_VOICE_PROMPT,
    EMPTY_UTTERANCE_REPLY,
    LLM_NOT_CONFIGURED_MESSAGE,
    QUOTA_EXHAUSTED_MESSAGE,
)

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

# 静音检测的 RMS 门限。0.01 约等于 -40dBFS：远低于正常说话（-20dBFS 左右），
# 高于大部分电路的底噪。实测空调房底噪约 0.002-0.004。
_SILENCE_RMS: Final[float] = 0.01

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

        # 跨设备盗用 token：token 本身有效，但 Header 里的 Device-Id 与
        # 签发的不是同一台。**必须比对** —— 否则偷到一台设备的 token
        # 就能冒充任意 MAC（OTA 只认 Device-Id，token 与 MAC 分离时
        # 身份链就断了）。
        header_mac = (self.ws.headers.get("device-id") or "").strip()
        if header_mac and header_mac != (dev.device_id or ""):
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
        rms = opus_codec.pcm_rms(pcm)
        frame_ms = config.VOICE_FRAME_DURATION_MS
        if rms >= _SILENCE_RMS:
            self._speech_ms += frame_ms
            self._silence_ms = 0
        else:
            self._silence_ms += frame_ms

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
            # V1 边界：只记 debug 日志并忽略，不返回任何 mcp 响应。
            # 详见模块 docstring。
            log.debug("忽略 mcp 消息（V1 不支持，后续增量）：%r",
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
        """连续静音达到阈值就自动 finalize（等价于设备端 VAD 断句）。"""
        try:
            while self.state == "listening":
                await asyncio.sleep(0.2)
                if self.state != "listening":
                    return
                if (self._silence_ms >= config.VOICE_SILENCE_MS
                        and self._speech_ms >= config.VOICE_MIN_SPEECH_MS):
                    log.info("[%s] 服务端静音兜底触发（连续静音 %dms，"
                             "累计语音 %dms）",
                             self.session_id, self._silence_ms, self._speech_ms)
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

        # ---- 配额校验：**在调 LLM 之前**。用尽就不调，省 token 也省时间。
        def _quota() -> dict:
            with SessionLocal() as db:
                return quota_snapshot(db, self.dev.owner_id,
                                      device_ids=[self.dev.id])

        try:
            quota = await run_in_threadpool(_quota)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] 配额查询失败，按不限制放行：%s: %s",
                        self.session_id, type(exc).__name__, exc)
            quota = {"unlimited": True}
        if not quota.get("unlimited"):
            used = int(quota.get("used_seconds") or 0)
            limit = quota.get("limit_seconds")
            if limit is not None and used >= int(limit):
                log.info("[%s] 配额用尽（%d/%d），不调 LLM",
                         self.session_id, used, int(limit))
                await self._send_alert(QUOTA_EXHAUSTED_MESSAGE)
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

        # ---- LLM
        t_llm = _now_ms()
        try:
            def _chat():
                with SessionLocal() as db:
                    return chat(db, "chat", messages)

            result = await run_in_threadpool(_chat)
        except ConfigMissingError:
            log.warning("[%s] 未配置 LLM", self.session_id)
            await self._send_alert(LLM_NOT_CONFIGURED_MESSAGE)
            return utterance
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] LLM 调用失败：%s: %s", self.session_id,
                        type(exc).__name__, exc)
            await self._send_alert("我刚才走神了，请您再说一遍")
            return utterance

        reply = (result.text or "").strip()
        t_llm = _now_ms() - t_llm
        if not reply:
            log.warning("[%s] LLM 返回空文本", self.session_id)
            await self._send_alert("我没想好怎么回答，请您再说一遍")
            return utterance

        # 会话内历史：**只在本轮成功拿到回复后**才追加。失败轮不进历史，
        # 否则下一轮的上下文里会有一条没有回复的 user 消息，
        # 模型会以为用户的话被忽略了。
        self._history.append({"role": "user", "content": utterance})
        self._history.append({"role": "assistant", "content": reply})

        # ---- 埋点 + 记忆沉淀（都不阻塞播报）
        await self._record_usage(result, utterance, reply, started, t_asr, t_llm)

        # ---- 分句播报
        await self._speak_reply(reply, started)
        return utterance

    async def _speak_reply(self, reply: str, started: float) -> None:
        """把回复按句切分，逐句合成并逐帧发二进制音频。

        顺序：首句前 tts start → 每句 sentence_start + 音频 → 全部发完
        tts stop。**每句之间、每帧之间检查 ``_abort``**：打断必须立刻
        停发剩余内容，否则老人喊"停"之后还会继续念完一整段。

        ⚠️ 这里**不重置** ``_abort``。早期版本在开头 ``self._abort = False``，
        于是"用户在 ASR/LLM 阶段喊了唤醒词"这个 abort 会在真正开播前被清掉，
        结果是老人明明喊了停，服务端还是把整段念完。语义上正确的清空时机
        只有一处：**新的 listen start**（用户重新开口 = 旧打断作废），
        那里已经做了。
        """
        if self._abort:
            # 播报前就被打断了（用户在 ASR/LLM 期间喊了唤醒词）。
            # 固件此刻正在 waiting for tts start，不发成对的 start/stop
            # 会让它停在上一态，所以发一个**空播报**把状态收干净。
            log.info("[%s] 播报前已收到 abort，丢弃本条回复", self.session_id)
            await self._send_json({"type": "tts", "state": "start"})
            await self._send_json({"type": "tts", "state": "stop"})
            self.state = "idle"
            return
        self.state = "speaking"
        t_tts = _now_ms()

        sentences = _split_sentences(reply)
        sent_any = False
        tts_ok = True
        voice = ""

        def _voice() -> str:
            with SessionLocal() as db:
                return asr_tts.pick_voice(db, self.dev)

        try:
            voice = await run_in_threadpool(_voice)
        except Exception as exc:  # noqa: BLE001
            log.debug("[%s] 音色解析失败，用厂商默认：%s", self.session_id, exc)

        for idx, sentence in enumerate(sentences):
            if self._abort:
                log.info("[%s] 播报被打断，剩余 %d 句不发",
                         self.session_id, len(sentences) - idx)
                break
            if not sent_any:
                await self._send_json({"type": "tts", "state": "start"})
                sent_any = True

            await self._send_json({"type": "tts", "state": "sentence_start",
                                   "text": sentence})
            await self._send_json({"type": "llm", "emotion": "neutral"})

            # 单句 TTS 失败：跳过该句音频、后续句继续。
            # 不能整轮失败 —— 第一句合成失败而后面的句子都成功是常见的
            # （厂商对长句偶发 500），老人听到的是少一句，不是 silence。
            try:
                def _tts(s: str = sentence) -> bytes:
                    with SessionLocal() as db:
                        return asr_tts.tts(db, s, voice=voice)

                pcm = await run_in_threadpool(_tts)
            except asr_tts.VoiceConfigMissingError:
                log.info("[%s] 未配置 TTS，只发文本", self.session_id)
                tts_ok = False
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] 第 %d 句 TTS 失败，跳过音频：%s: %s",
                            self.session_id, idx, type(exc).__name__, exc)
                continue

            if not pcm:
                continue
            # 逐帧编码并发送，帧间检查 abort
            frame = opus_codec.frame_bytes(config.VOICE_OUTPUT_SAMPLE_RATE)
            for off in range(0, len(pcm), frame):
                if self._abort:
                    log.info("[%s] 帧级打断", self.session_id)
                    break
                chunk = pcm[off:off + frame]
                try:
                    packet = opus_codec.encode_opus(
                        chunk, config.VOICE_OUTPUT_SAMPLE_RATE)
                except opus_codec.OpusUnavailableError:
                    # 无 libopus：音频发不了，但文本已经发了。
                    log.debug("[%s] 无 libopus，跳过音频帧", self.session_id)
                    break
                if not packet:
                    continue
                await self._send_audio(packet)

        if sent_any:
            await self._send_json({"type": "tts", "state": "stop"})
        else:
            # 一句都没发（空回复或全被 TTS 失败吞掉）：仍然要 start/stop
            # 成对，否则设备停在 speaking 态不回 idle（application.cc:454
            # 只在收到 stop 时回落 listening/idle）。
            await self._send_json({"type": "tts", "state": "start"})
            await self._send_json({"type": "tts", "state": "stop"})
        self.state = "idle"
        log.info("[%s] 播报完成（TTS %dms，音频%s）",
                 self.session_id, _now_ms() - t_tts, "不可用" if not tts_ok else "已发")

    # ================================================================ 埋点
    async def _record_usage(self, result: Any, utterance: str, reply: str,
                            started: float, t_asr: int, t_llm: int) -> None:
        """记账：token 用量 + 陪聊时长 + Interaction + 记忆沉淀。

        **四条记录分两个事务**（token/时长/交互 一次 commit；记忆异步）。
        为什么不用一个大事务：``remember_after_reply`` 内部自己起线程
        与会话，塞进来会让主事务等一个与返回无关的写；而 token 与
        陪聊时长必须同生共死（漏一个统计就对不上账）。
        """
        elapsed_ms = int((time.monotonic() - started) * 1000)
        total_tokens = int(getattr(result, "total_tokens", 0) or 0)

        def _write() -> None:
            with SessionLocal() as db:
                usage_svc.record_from_result(
                    db, result, source="voice", usage="chat",
                    device_id=self.dev.id, user_id=self.dev.owner_id,
                    agent_id=self.dev.agent_id,
                    request_id=usage_svc.new_request_id(),
                    session_id=self.session_id,
                )
                record_chat_usage(db, self.dev.id, local_date_str(),
                                  max(1, elapsed_ms // 1000), is_reminder=False)
                db.add(_m.Interaction(
                    device_id=self.dev.id,
                    kind=_INTERACTION_KIND,
                    duration_ms=elapsed_ms,
                    meta={
                        "session_id": self.session_id,
                        "asr_ms": t_asr,
                        "llm_ms": t_llm,
                        "tts_ms": 0,
                        "stt_chars": len(utterance),
                        "reply_chars": len(reply),
                        "total_tokens": total_tokens,
                    },
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
