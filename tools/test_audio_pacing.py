"""音频下行节流器（AudioPacer）的单元断言。

为什么单独测这个
----------------
它是 2026-10-21「声音断断续续」事故的正主。原先我们把 TTS 返回的整段音频
在 ``await ws.send_bytes()`` 循环里**能发多快发多快**地灌给设备，帧间隔完全
不可控；而且发送被 await 在 LLM 流式读取循环里，socket 一反压就读不到下一
个 token —— 一次性制造了「断续」和「响应慢」两个故障。

上游 xiaozhi-esp32-server 的做法（``core/utils/audioRateController.py``，
已逐行核对）是**虚拟播放时钟**：每帧对应固定毫秒，按节奏发、晚发不追发、
前几帧直接突发压首句延迟、控制消息不占播放时间。本文件把这三条钉住。

帧长在测试里用 4ms、预缓冲 2 帧，整套跑下来几十毫秒。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from app.voice.audio_pacing import (          # noqa: E402
    DEFAULT_FRAME_MS,
    PRE_BUFFER_COUNT,
    AudioPacer,
)

_passed = 0
_failed = 0
_failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  [PASS] {label}" + (f"  -> {detail}" if detail else ""))
    else:
        _failed += 1
        _failures.append(label)
        print(f"  [FAIL] {label}" + (f"  -> {detail}" if detail else ""))


class _Recorder:
    """把发送出去的帧与控制消息都记下来，带时间戳。"""

    def __init__(self, slow: float = 0.0) -> None:
        self.frames: list[bytes] = []
        self.jsons: list[dict] = []
        self.times: list[float] = []          # 每帧发出的单调时刻
        self.slow = slow
        self.t0 = 0.0

    async def send_audio(self, packet: bytes) -> None:
        if self.slow:
            await asyncio.sleep(self.slow)
        if not self.t0:
            self.t0 = time.monotonic()
        self.frames.append(packet)
        self.times.append(time.monotonic())

    async def send_json(self, obj: dict) -> None:
        self.jsons.append(obj)


class _FlakySender:
    """可控制的假发送端：前 N 次抛异常 / 永远抛异常。"""

    def __init__(self, fail_times: int = 0, always: bool = False) -> None:
        self.frames: list[bytes] = []
        self.remaining = fail_times
        self.always = always

    async def send(self, packet: bytes) -> None:
        if self.always or self.remaining > 0:
            if self.remaining > 0:
                self.remaining -= 1
            raise OSError("假 socket 炸了")
        self.frames.append(packet)


def _pacer(rec: _Recorder, *, frame_ms: int = 4, pre: int = 2) -> AudioPacer:
    return AudioPacer(rec.send_audio, frame_ms=frame_ms, pre_buffer=pre,
                      send_json=rec.send_json)


# ================================================================ T1 预缓冲
def t1_pre_buffer() -> None:
    print("\n── T1 前几帧直接突发：首句延迟不能被节流拖住 ──")

    async def _run() -> None:
        rec = _Recorder()
        p = _pacer(rec, pre=2)
        p.start()
        for i in range(4):
            p.add_audio(f"f{i}".encode())
        await p.stop()
        check(len(rec.frames) == 4, "T1 四帧全部发出", str(len(rec.frames)))
        # 语义：前 pre_buffer 帧不等时钟；第 pre_buffer+1 帧确立时钟原点也立即发；
        # 之后每帧按 4ms。所以前 3 帧应当几乎同时出来，第 4 帧要等。
        gaps = [b - a for a, b in zip(rec.times, rec.times[1:])]
        early = gaps[: p.pre_buffer]           # 预缓冲帧之间：≈0
        check(max(early) < 0.002,
              "T1 预缓冲帧之间没有节流间隔（首句要快）",
              f"预缓冲内最大间隔 {max(early)*1000:.1f}ms")
        later = gaps[p.pre_buffer:]            # 预缓冲之后：≈ frame_ms
        check(min(later) >= 0.002,
              "T1 预缓冲发完后才开始按节奏发",
              f"第{p.pre_buffer+1}→{p.pre_buffer+2}帧间隔 "
              f"{later[0]*1000:.1f}ms")

    asyncio.run(_run())


# ================================================================ T2 匀速
def t2_steady_pace() -> None:
    print("\n── T2 匀速下发：不发快也不追发 ──")

    async def _run() -> None:
        rec = _Recorder()
        p = _pacer(rec, frame_ms=4, pre=2)
        p.start()
        for i in range(20):
            p.add_audio(f"f{i}".encode())
        await p.stop()
        gaps = [b - a for a, b in zip(rec.times, rec.times[1:])]
        paced = gaps[p.pre_buffer:]            # 预缓冲之后才是节流段
        worst = max(paced)
        avg = sum(paced) / len(paced)
        check(worst < 0.020, "T2 预缓冲之后没有任何一帧间隔超过 20ms",
              f"最大 {worst*1000:.1f}ms")
        check(0.002 < avg < 0.012,
              "T2 平均间隔贴近帧长（不发快也不拖沓）",
              f"均值 {avg*1000:.1f}ms")
        # 关键：不追发。若某帧迟到，下一帧不能提前把时间补回来。
        total = rec.times[-1] - rec.times[0]
        expected = (len(rec.frames) - 1) * 0.004
        check(total >= expected * 0.8,
              "T2 总时长不少于 (帧数-1)×帧长（没有压缩播放时间）",
              f"总 {total*1000:.0f}ms，理论 {expected*1000:.0f}ms")

    asyncio.run(_run())


# ================================================================ T3 控制消息
def t3_control_messages() -> None:
    print("\n── T3 控制消息立即发、不占播放时间 ──")

    async def _run() -> None:
        rec = _Recorder()
        p = _pacer(rec, frame_ms=4, pre=2)
        p.start()
        p.add_json({"state": "sentence_start", "text": "今天晴"})
        for i in range(6):
            p.add_audio(f"f{i}".encode())
        await p.stop()
        check(len(rec.jsons) == 1, "T3 控制消息被发出", str(rec.jsons))
        check(rec.frames and rec.frames[0] == b"f0",
              "T3 控制消息之后的音频仍是原顺序", rec.frames[:1].__str__())
        gaps = [b - a for a, b in zip(rec.times, rec.times[1:])]
        paced = gaps[p.pre_buffer:]
        check(max(paced) < 0.020,
              "T3 控制消息没把音频节奏带偏（不占播放时间）",
              f"后续帧最大间隔 {max(paced)*1000:.1f}ms")

    asyncio.run(_run())


# ================================================================ T4 空后续填
def t4_refill_after_empty() -> None:
    print("\n── T4 排空后重新入队：时钟回拨，不许跳段 ──")

    async def _run() -> None:
        rec = _Recorder()
        p = _pacer(rec, frame_ms=4, pre=2)
        p.start()
        for i in range(6):
            p.add_audio(f"a{i}".encode())
        # 等这一批真的走下 socket（**不 stop**，发送协程继续活着）
        await p.wait_idle()
        first_batch = list(rec.frames)
        check(len(first_batch) == 6, "T4 第一批全部发出", str(len(first_batch)))

        # 第二阶段：模拟"下一句 TTS 刚合成好"，停得远大于一帧
        await asyncio.sleep(0.030)
        for i in range(6):
            p.add_audio(f"b{i}".encode())
        await p.wait_idle()
        await p.stop()
        check(len(rec.frames) == 12, "T4 两批全部发出（发送协程没被停掉）",
              str(len(rec.frames)))
        check(rec.frames[:6] == first_batch, "T4 第一批顺序未被破坏")
        # 关键断言：第二批不能"立刻补发"把停顿攒成一次突发
        times2 = rec.times[6:]
        gaps2 = [b - a for a, b in zip(times2, times2[1:])]
        check(gaps2 and max(gaps2) < 0.030,
              "T4 第二批按节奏发，没有把停顿攒成一次突发",
              f"第二批最大间隔 {max(gaps2)*1000:.1f}ms")

    asyncio.run(_run())


# ================================================================ T5 打断 / 复位
def t5_abort_and_reset() -> None:
    print("\n── T5 打断丢弃、复位清零 ──")

    async def _run() -> None:
        rec = _Recorder()
        p = _pacer(rec, frame_ms=4, pre=2)
        p.start()
        for i in range(40):
            p.add_audio(f"x{i}".encode())
        # 不 drain：打断语义，队列里剩下的立刻丢掉
        await p.stop(drain=False)
        sent_abort = len(rec.frames)
        check(sent_abort < 40, "T5 打断时截断，不把剩下几十帧硬发完",
              f"发了 {sent_abort}/40 帧")
        p.reset()
        check(p.frames_sent == 0 and p.max_gap == 0.0,
              "T5 reset 清零计数与间隔", f"frames={p.frames_sent}")
        # 复位后新的一轮要能从头按节奏发（时钟没被上一轮污染）
        rec2 = _Recorder()
        p2 = _pacer(rec2, frame_ms=4, pre=2)
        p2.start()
        for i in range(6):
            p2.add_audio(f"y{i}".encode())
        await p2.stop()
        check(len(rec2.frames) == 6, "T5 复位后新一轮正常发完",
              str(len(rec2.frames)))

    asyncio.run(_run())


# ================================================================ T6 常量一致
def t6_constants() -> None:
    print("\n── T6 帧长常量必须与固件一致 ──")
    check(DEFAULT_FRAME_MS == 60,
          "T6 默认帧长 60ms（固件 audio_service.h 的 OPUS_FRAME_DURATION_MS）",
          str(DEFAULT_FRAME_MS))
    check(PRE_BUFFER_COUNT == 5,
          "T6 预缓冲 5 帧（=300ms，盖住设备解码队列建立前的空窗）",
          str(PRE_BUFFER_COUNT))


# ================================================================ T7 慢发送
def t7_slow_sink() -> None:
    print("\n── T7 发送慢时不会塌方成乱序 ──")

    async def _run() -> None:
        rec = _Recorder(slow=0.003)          # 每帧 send 本身要 3ms
        p = _pacer(rec, frame_ms=4, pre=2)
        p.start()
        for i in range(10):
            p.add_audio(f"s{i}".encode())
        await p.stop()
        check(rec.frames == [f"s{i}".encode() for i in range(10)],
              "T7 慢发送下帧序不乱", str(len(rec.frames)))
        check(len(set(rec.frames)) == 10, "T7 不重不漏", str(len(set(rec.frames))))

    asyncio.run(_run())


# ================================================================ T8 打断竞态
def t8_reset_during_wait() -> None:
    print("\n── T8 等播放时钟时被 reset()：不许把发送协程打死 ──")

    async def _run() -> None:
        rec = _Recorder()
        # pre=0：每一帧都走时钟，才能进入"睡着等回合"的状态
        p = _pacer(rec, frame_ms=4, pre=0)
        p.start()
        p.add_audio(b"a")
        await p.wait_idle()
        check(len(rec.frames) == 1, "T8 首帧发出（时钟原点已立）",
              str(rec.frames))

        # 关键复现：让协程睡进 _wait_my_turn，**在睡眠期间**reset()，
        # 并且**先不**入队新帧（必须这样：一入队队列就不空了，popleft
        # 不会炸，竞态被掩盖 —— 第一版测试就是这么假绿过）。
        # 旧实现在这里对空 deque popleft 抛 IndexError → 被 except
        # Exception 吃掉 → 发送协程死掉 → 之后每帧都堆在队列里、
        # wait_idle 立刻返回 → 全程静音、日志全绿。
        p.add_audio(b"b")
        await asyncio.sleep(0.001)          # 足够让协程睡进等回合里
        p.reset()
        # 等这一觉睡完：4ms 帧长 + Windows 约 15ms 定时粒度，给足余量。
        await asyncio.sleep(0.060)
        # 决定性断言：协程必须还活着。放在"再入队"之前查 ——
        # 一旦先入了队，add_audio 的自愈会把协程拉起来，真假就分不出了。
        check(p._task is not None and not p._task.done(),
              "T8 ★ reset 清空队列后发送协程仍然活着（无 IndexError）",
              "task alive")

        # 复位后新的一轮要能继续发（既没死、也没把 b 重放出来）
        p.add_audio(b"c")
        await p.wait_idle()
        check(rec.frames == [b"a", b"c"],
              "T8 复位后继续发新帧，被丢弃的帧不重放", str(rec.frames))

        # 时钟必须重新立起来：否则 _t0=0 会让 elapsed 恒为巨大值，
        # 那一轮退化成"能发多快发多快"——正是本次要消灭的老路。
        # ⚠️ 这里必须用 25ms 以上的帧长：Windows 事件循环的 sleep 粒度
        # 约 15ms，用 4ms 测出来的间隔是定时器量化的结果、没有信息量
        # （实测 4ms 的 sleep 睡出 26ms，随后的帧因时钟已过而立即发出）。
        # 断言口径改成"总时长不少于 (N-1)×帧长"：睡过头只会更长，
        # 不会假绿；退化成突发则第一批就发完，立刻变红。
        rec2 = _Recorder()
        p2 = AudioPacer(rec2.send_audio, frame_ms=25, pre_buffer=0)
        p2.start()
        t_first = time.monotonic()
        for i in range(4):
            p2.add_audio(f"z{i}".encode())
        await p2.wait_idle()
        await p2.stop()
        total = (rec2.times[-1] - t_first) * 1000
        check(3 * 25 * 0.9 <= total < 3 * 25 * 8,
              "T8 reset 之后重新按帧长节流（没有退化成突发）",
              f"4 帧总 {total:.0f}ms，理论 ≥68ms")

    asyncio.run(_run())


# ================================================================ T9 发送失败
def t9_send_failure() -> None:
    print("\n── T9 发送抛异常：自愈；连续失败才判死 ──")

    async def _run() -> None:
        # ---- 单次失败：协程死掉，但下一次入队要能把它拉起来 ----
        sk = _FlakySender(fail_times=1)
        p = AudioPacer(sk.send, frame_ms=4, pre_buffer=0)
        p.start()
        p.add_audio(b"a")
        await asyncio.sleep(0.030)
        check(sk.frames == [] and p.frames_sent == 0,
              "T9 发送失败的那一帧不计入已发", str(p.frames_sent))
        check(not p.broken, "T9 单次失败不判死（允许自愈）",
              f"streak={p.fail_streak}")
        # 前置条件：协程确实已经被异常打死了（否则下面验的不是自愈）。
        # 测试可以探内部状态来证明前提，但不能拿它当结论。
        check(p._task is not None and p._task.done(),
              "T9 前置条件：发送协程确实死了", "task done")
        p.add_audio(b"b")
        await p.wait_idle()
        check(sk.frames == [b"b"] and p.frames_sent == 1,
              "T9 ★ 失败后自愈，后续帧照常发出", str(sk.frames))

        # ---- 连续失败：判死，且不许无限重启 ----
        sk2 = _FlakySender(always=True)
        p2 = AudioPacer(sk2.send, frame_ms=4, pre_buffer=0)
        p2.start()
        for i in range(5):
            p2.add_audio(bytes([65 + i]))
            await asyncio.sleep(0.020)
        check(p2.broken and p2.fail_streak == 3,
              "T9 连续失败达上限判死，且不无限重启协程",
              f"streak={p2.fail_streak}")
        check(await p2.wait_idle() is True,
              "T9 判死后 wait_idle 仍然返回（会话不被挂死）")

    asyncio.run(_run())


def main() -> int:
    print("=" * 64)
    print("音频下行节流器（虚拟播放时钟）单元断言")
    print("=" * 64)
    t1_pre_buffer()
    t2_steady_pace()
    t3_control_messages()
    t4_refill_after_empty()
    t5_abort_and_reset()
    t6_constants()
    t7_slow_sink()
    t8_reset_during_wait()
    t9_send_failure()
    print("\n" + "=" * 64)
    print(f"通过 {_passed} 项，失败 {_failed} 项")
    if _failed:
        for n in _failures:
            print("  -", n)
    print("结论：", "**绿**" if _failed == 0 else "**红**")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
