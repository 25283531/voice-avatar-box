"""音频下行节流器 —— 照上游 xiaozhi-esp32-server 的 ``AudioRateController`` 实现。

为什么必须有这个东西（2026-10-21 真机事故）
-------------------------------------------
老人反馈"声音断断续续"。原先我们的实现是：TTS 一返回整段音频，就在
``await ws.send_bytes()`` 循环里**能发多快发多快**地把几十帧灌给设备。

这么做的两个后果：

1. **帧间隔不可控**。设备按 60ms 一帧播放，服务端却在 5ms 内灌完 50 帧，
   然后进入下一句合成（几百毫秒到数秒）。TCP 反压会让发送协程长时间挂起，
   帧之间的间隔完全不可预测 —— 表现出来就是一顿一顿。
2. **反压会堵住 LLM 流读取**。发送是在 ``push_text`` 里 await 的，而
   ``push_text`` 在 LLM 流式读取循环里被 await。socket 一发不出去，
   下一个 LLM 增量就读不到 —— 这就是"响应时间太长"的一半原因。

上游怎么做的（``core/utils/audioRateController.py``，已逐行核对）
---------------------------------------------------------------
- **虚拟播放时钟**：每帧对应 60ms 播放时间，``play_position`` 按帧递增，
  实际发送时刻 = ``start_timestamp + play_position``。某帧晚发了**不会追发**
  （不会 burst），下一帧仍按节奏走 —— 时间累积误差被消掉。
- **前几帧直接突发**（``PRE_BUFFER_COUNT``）：只为了把首句延迟压下来，
  之后的帧全部走时钟。
- **控制消息不占播放时间**：``sentence_start`` 之类立即发，不占时钟。
- **发送在独立协程里**：TTS 与音频下发互不阻塞，LLM 读取也不被拖住。

本模块只做「节流与排序」，不认识 WebSocket、不认识协议版本 —— 由调用方给
``send_audio`` / ``send_json`` 回调。这样它能被纯内存测试覆盖（不需要真连接、
不需要 libopus）。

时钟语义（一句话说清）
---------------------
第 n 帧（从 0 数）的计划发送时刻 = ``_t0 + n * interval``。``_t0`` 是**第一帧
真正发出的时刻**。预缓冲帧不等时钟，所以它们天然**早于**自己的计划时刻 ——
这正是预缓冲的意义（领先播放进度一小段）。之后的每一帧都等到自己的时刻才发。

三条"不许静音"的防线
--------------------
这个模块一旦失灵，症状是**老人什么都听不到而服务端一声不吭** —— 本项目里
最贵的那类故障。所以：

1. ``_run`` 里 **popleft 之前必须再查一次队列**：等待播放时钟时协程是睡着的，
   这期间 ``reset()``（abort）可能把队列清空。直接 popleft 会在空 deque 上抛
   ``IndexError``，被外层 ``except Exception`` 吃掉 ⇒ 发送协程**死掉**，
   之后每一帧都堆在队列里，而 ``wait_idle`` 因为 ``_drained`` 已置位立刻返回
   ⇒ ``tts stop`` 照发、日志照绿、音箱全程安静。
2. 发送回调抛异常（连接断开、socket 满）同样会打死 ``_run``：入队时
   ``_ensure_running()`` 自动把它拉起来（自愈）；连续失败
   ``_MAX_SEND_FAILURES`` 次才置 ``broken``（判死并 ERROR 日志），
   不再无限重启。
3. ``wait_idle(timeout=...)``：调用方（``finish()``）必须知道"音频到底发完没"，
   超时要 WARNING 而不是默默往下走。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable, Deque, Final

log = logging.getLogger(__name__)

#: 下发一个音频帧的秒数。与固件 ``audio_service.h`` 的
#: ``OPUS_FRAME_DURATION_MS`` 必须一致 —— 不一致时我们每帧发的音频长度
#: 和设备以为的长度不一样，表现是整段变快/变慢，且没有任何报错。
DEFAULT_FRAME_MS: Final[int] = 60

#: 前几帧不走时钟、直接发。目的是把"第一句话什么时候能听到"提前。
#: 5 帧 = 300ms 音频，正好盖住设备解码队列尚未建立起来的空窗。
PRE_BUFFER_COUNT: Final[int] = 5

#: 连续发送失败多少次才判定"音频通道已死"。给 3 次是让偶发的 socket
#: 抖动能自愈（重启即恢复），同时挡住"每帧重启一次协程"的风暴。
_MAX_SEND_FAILURES: Final[int] = 3


class AudioPacer:
    """按虚拟播放时钟匀速下发音频帧，控制消息插队立即发。"""

    def __init__(self,
                 send_audio: Callable[[bytes], Any],
                 *,
                 frame_ms: int = DEFAULT_FRAME_MS,
                 pre_buffer: int = PRE_BUFFER_COUNT,
                 send_json: Callable[[dict], Any] | None = None) -> None:
        """
        :param send_audio: 发送一个音频帧的回调（async）。
        :param frame_ms: 一帧音频承载的毫秒数。
        :param pre_buffer: 前几帧直接突发，不等时钟。
        :param send_json: 发送控制消息的回调（async）；为 None 表示不支持插队。
        """
        self._send = send_audio
        self._send_json = send_json
        self.interval = max(1, frame_ms) / 1000.0
        self.pre_buffer = max(0, pre_buffer)
        self._q: Deque[tuple[str, Any]] = deque()
        self._play_pos = 0.0        # 虚拟播放位置（秒），每发一帧 += interval
        self._t0 = 0.0              # 播放时钟原点（第一帧真正发出的时刻）
        self._pre_left = self.pre_buffer
        self._task: asyncio.Task | None = None
        self._drained = asyncio.Event()
        self._drained.set()         # 初始即为空
        self._empty_at = 0.0
        self._wake: asyncio.Event | None = None
        self._stopped = False       # stop() 之后不许再自愈重启
        self._broken = False        # 连续发送失败 → 通道判死
        self.fail_streak = 0
        self.frames_sent = 0        # **真正发出**的帧数（失败的不计）
        self.max_gap = 0.0          # 相邻两帧最大间隔（秒）
        self._last_send_at = 0.0
        self._first_frame_at = 0.0
        self._t_start = time.monotonic()

    # ---------------------------------------------------------------- 入队
    def add_audio(self, packet: bytes) -> None:
        """入队一个音频帧。

        队列从空恢复时把时钟原点回拨，让新音频从"当前虚拟播放位置"开始，
        而不是从现在开始 —— 否则设备会先播完积压的新音频、再被时钟追上，
        听感是跳段。只有停得比一帧还久才算"真的停过"（流式分片间隙不算）。

        这里还会顺带自愈：发送协程若已被异常打死，就地拉起来。
        """
        if not packet:
            return
        if self._broken:
            # 通道已死：帧没有去处，入队只会让 pending 虚高。
            # ⚠️ 更不能 clear _drained —— 那会让 wait_idle 去等一个
            # 永远不会再跑的协程，把整轮会话挂死在这里。
            return
        if not self._q and self._play_pos > 0:
            gap = time.monotonic() - self._empty_at
            if gap >= self.interval:
                self._t0 = time.monotonic() - self._play_pos
        self._q.append(("audio", packet))
        self._drained.clear()
        self._ensure_running()
        if self._wake is not None:
            self._wake.set()

    def add_json(self, obj: dict) -> None:
        """入队一条控制消息（``sentence_start`` / ``tts start`` …）。

        立即发、**不占播放时间**，与上游 ``add_message`` 同义。
        """
        if self._broken:
            # 同 add_audio：通道已死时不入队、不动 _drained。
            return
        self._q.append(("json", obj))
        self._drained.clear()
        self._ensure_running()
        if self._wake is not None:
            self._wake.set()

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        """启动发送协程。幂等：已在跑就什么都不做。"""
        self._stopped = False
        if self._task is None or self._task.done():
            self._t_start = time.monotonic()
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self, *, drain: bool = True) -> None:
        """停掉发送协程。

        :param drain: True 先等队列排空（正常收尾）；False 立刻丢弃（打断）。
        """
        task = self._task
        if task is None:
            return
        self._stopped = True
        if drain:
            # 队列可能还在被生产者填，等它自然排空。
            await self._drained.wait()
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if not drain:
            self._q.clear()
        self._task = None

    async def wait_idle(self, timeout: float | None = None) -> bool:
        """等队列排空（**不停发送协程**）。

        给"这一句发完了"用：句子结束时要等音频真的走下 socket 才能算完，
        否则 ``tts stop`` 会插到音频前面去（设备听到的是话音中途被打断）。

        :param timeout: 秒；None = 一直等。超时返回 False，调用方必须
            显式处理（宁可 WARNING + 可能截断，也不能假装发完了）。
        """
        if timeout is None:
            await self._drained.wait()
            return True
        try:
            await asyncio.wait_for(self._drained.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def reset(self) -> None:
        """清空一切并复位（打断 / 新一轮对话）。

        时钟必须一起复位：否则下一轮的 ``_play_pos`` 从旧值继续累加，
        音频会被发送协程"攒"住几十秒才动。

        不碰 ``_task`` / ``_stopped`` / ``_broken`` / ``fail_streak``：
        它描述的是"这一轮的内容作废"，不是"通道死了"。
        """
        self._q.clear()
        self._play_pos = 0.0
        self._t0 = 0.0
        self._pre_left = self.pre_buffer
        self._drained.set()
        self.frames_sent = 0
        self.max_gap = 0.0
        self._last_send_at = 0.0
        self._first_frame_at = 0.0
        self._t_start = time.monotonic()

    # ---------------------------------------------------------------- 统计
    @property
    def pending(self) -> int:
        """尚未发出去的条数（音频帧 + 控制消息）。

        节流器把帧间隔钉在 60ms 之后，"TTS 供帧快不快"改看这个数：
        积压越深，说明整段音频被压在后头，**最后一句会跟着延后**
        （积压 N 帧 ≈ N×60ms）。反之它长期为 0 而回复还没结束，
        就是设备侧读不动（socket 反压）。
        """
        return len(self._q)

    @property
    def broken(self) -> bool:
        """发送通道是否已判死（连续失败达上限）。

        判死时 ``wait_idle`` 仍会返回（不挂住会话），但调用方必须
        据此把"音频已发"改成"音频不可用"—— 否则又是一次静默事故。
        """
        return self._broken

    @property
    def first_frame_sec(self) -> float:
        """从构造到第一帧发出的秒数；一帧没发返回 0。"""
        if not self.frames_sent:
            return 0.0
        return self._first_frame_at - self._t_start

    # ---------------------------------------------------------------- 内部
    def _ensure_running(self) -> None:
        """发送协程死了就拉起来（自愈）。

        场景：``_send`` 抛异常（连接抖动/ socket 满）把 ``_run`` 打死了。
        不自愈的话，之后每一帧都堆在 ``_q`` 里，而 ``_drained`` 已被
        except 分支置位 ⇒ ``wait_idle`` 立刻返回 ⇒ 一切照常、全程静音。
        """
        if self._stopped or self._broken:
            return
        task = self._task
        if task is None or task.done():
            self.start()

    async def _run(self) -> None:
        try:
            while True:
                while self._q:
                    kind, payload = self._q[0]
                    if kind == "json":
                        self._q.popleft()
                        if self._send_json is not None:
                            await self._send_json(payload)
                        continue
                    if self._pre_left > 0:
                        # 预缓冲期：直接发，不等时钟（压首句延迟）
                        self._pre_left -= 1
                    else:
                        await self._wait_my_turn()
                    if not self._q:
                        # ⚠️ 等播放时钟期间 reset() 把队列清空了（abort）：
                        # 本帧作废，回外层重等。少了这一句就会在空 deque 上
                        # popleft 抛 IndexError，而外层 except 会把整个发送
                        # 协程吃掉 —— 之后本轮永久静音且无任何报错。
                        break
                    item = self._q.popleft()
                    await self._send_frame(item[1])
                self._drained.set()
                self._empty_at = time.monotonic()
                # 队列空了但协程还在：等新数据。**必须让出事件循环**，
                # 否则 TTS 合成协程永远拿不到执行机会（症状：帧间隔忽长忽短）。
                self._wake = asyncio.Event()
                if self._q:
                    continue
                await self._wake.wait()
        except asyncio.CancelledError:
            log.debug("音频节流发送协程已取消")
            raise
        except Exception as exc:  # noqa: BLE001  发送失败不能让会话崩
            # 队列作废：这些帧已经不可能按节奏发出去（时钟原点都未必立得起来），
            # 留着只会让 pending 虚高、让下一轮误判积压。
            self._q.clear()
            self._drained.set()
            self.fail_streak += 1
            if self.fail_streak >= _MAX_SEND_FAILURES:
                # 判死：等调用方（finish）据此把"音频已发"改成"音频不可用"。
                # 不自愈了 —— 连接已经没有了，重启只是每帧再炸一次。
                self._broken = True
                log.error("音频下行连续失败 %d 次，判定通道已死：%s: %s",
                          self.fail_streak, type(exc).__name__, exc)
            else:
                log.warning("音频节流发送异常（第 %d 次，将自愈）：%s: %s",
                            self.fail_streak, type(exc).__name__, exc)

    async def _wait_my_turn(self) -> None:
        """等到播放时钟走到本帧的时刻，然后推进一帧。"""
        if self._t0 <= 0:
            # 时钟还没立（reset 后 / 第一帧就走到这里）：以此刻为原点，
            # 本帧立即发。少了这一句，_t0=0 会让 elapsed 恒为巨大值，
            # 节奏彻底失效（那一轮会退化成"能发多快发多快"）。
            self._t0 = time.monotonic()
        while True:
            elapsed = time.monotonic() - self._t0
            if elapsed >= self._play_pos:
                break
            # 睡到剩余时间：别睡过头（节奏会乱），也别空转（CPU 会被吃满）。
            await asyncio.sleep(self._play_pos - elapsed)
        self._play_pos += self.interval

    async def _send_frame(self, packet: bytes) -> None:
        """发一帧 + 记账。第一帧确立播放时钟原点。

        记账放在 ``await self._send`` **之后**：发送失败的帧不算
        "已发"（``frames_sent`` / ``max_gap`` 都只反映真的走下去的帧），
        否则一次 socket 抖动就会把首帧时刻和帧间隔全部带偏。
        """
        await self._send(packet)
        now = time.monotonic()
        if not self.frames_sent:
            self._t0 = now                 # 时钟原点 = 第一帧真正发出的时刻
            self._first_frame_at = now
        else:
            gap = now - self._last_send_at
            if gap > self.max_gap:
                self.max_gap = gap
        self._last_send_at = now
        self.frames_sent += 1
        self.fail_streak = 0
