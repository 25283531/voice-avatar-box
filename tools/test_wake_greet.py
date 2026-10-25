"""唤醒应答（「你好，<称呼>，要聊点什么？」）的纯函数级单测。

为什么需要它
------------
老人喊完唤醒词，**设备一点声音都没有**：本板 ``sdkconfig`` 里
``CONFIG_SEND_WAKE_WORD_DATA=y``，固件那句「叮」（OGG_POPUP）在
``application.cc`` 的 ``#else`` 分支里，压根不会播。老人的判断于是变成
「没唤醒」→ 反复喊唤醒词。修法是在唤醒词到达后补一句语音应答。

这条功能有三类失效，**症状各不相同，但没有一类会让协议层或状态机报错**：

1. **没接上**：`_on_listen` 的 detect 分支忘了调 `_schedule_wake_greet`。
   现象是"代码写了但线上没反应"——本项目已经踩过（D53 的前端事故同款）。
   ⇒ 第 1 组用 spy 钉住"detect 必须排任务、listen start 必须不排"。
2. **该闭嘴的时候开口**：老人一口气说「你好小智，今天天气怎么样」，
   我们插一句寒暄，把他后半句吃掉 —— 比不回应更烦人。
   ⇒ 第 3 组把每条跳过判据单独钉一遍。
3. **两路 TTS 抢同一条下行通道**：读称呼是一次真实的 ``await`` 让出点，
   期间 listen stop 可能把 state 推成 thinking。此时再开口，两路音频帧
   交错，老人听到的是**破音**（用户原话："千万不要再修的破音、卡顿"）。
   ⇒ 第 4 组专门造这个竞态，要求第二次检查必须拦住。

跑法
----
    python tools/test_wake_greet.py
退出码 0 = 全绿。CI 的 server-ci job 会跑它（纯内存 + 临时库，不占端口）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

# 导入 app.* 需要的最小环境（不连真库：sqlite 落在临时目录）
_TMP = Path(tempfile.mkdtemp(prefix="wake-greet-"))
os.environ.setdefault("ELDER_DB", f"sqlite:///{(_TMP / 'test.db').as_posix()}")
os.environ.setdefault("ELDER_JWT_SECRET", "wake-greet-secret-0123456789abcdef")
os.environ.setdefault("ELDER_SCHEDULER_INTERVAL", "3600")   # 不起后台调度
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_k, None)

_g_pass = 0
_g_fail = 0
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global _g_pass, _g_fail
    if ok:
        _g_pass += 1
        print(f"  [OK]   {name}")
    else:
        _g_fail += 1
        _failures.append(name)
        print(f"  [FAIL] {name}" + (f"  —— {detail}" if detail else ""))


class FakeWS:
    """只够 VoiceSession.__init__ 用（本脚本从不真的发协议消息）。"""

    async def send_text(self, text: str) -> None:   # pragma: no cover
        pass

    async def close(self, code: int = 1000) -> None:  # pragma: no cover
        pass


def make_session(owner_id: int | None = None):
    from app.voice.session import VoiceSession

    s = VoiceSession(FakeWS(), protocol_version=1)
    s.session_id = "test-greet"
    s.dev = SimpleNamespace(id=1, owner_id=owner_id, agent_id=None,
                            device_id="aa:bb:cc:dd:ee:ff")
    # 默认进 listening：真实链路里 detect 之后紧跟 listen start
    s.state = "listening"
    return s


# ================================================================ 第 1 组
async def t_wiring() -> None:
    print("\n[1] 接线：detect 必须排任务，listen start 必须不排")
    s = make_session()
    calls: list[int] = []
    s._schedule_wake_greet = lambda: calls.append(1)   # spy

    await s._on_listen({"state": "detect", "text": "你好小智"})
    check("★ 唤醒词（detect）必须排一次唤醒应答", calls == [1], f"calls={calls}")

    calls.clear()
    await s._on_listen({"state": "start", "mode": "realtime"})
    check("listen start 不得排唤醒应答（那是每轮都发的，会变成复读机）",
          calls == [], f"calls={calls}")

    # 唤醒词必须解除待机（否则喊完还是"退下"状态，应答也发不出去）
    s2 = make_session()
    s2._dismissed_until = time.monotonic() + 60
    s2._schedule_wake_greet = lambda: None
    await s2._on_listen({"state": "detect", "text": "你好小智"})
    check("唤醒词必须清掉「退下吧」冷却", s2._dismissed_until == 0.0,
          f"_dismissed_until={s2._dismissed_until}")


# ================================================================ 第 2 组
async def t_text() -> None:
    print("\n[2] 文案：称呼来自 tone_profile，取不到就退化，绝不编")
    from app.voice import session as S

    async def run(addr):
        s = make_session(owner_id=1)
        s._wake_address = lambda: addr
        return await s._wake_greet_text()

    check("有称呼 → 「你好，爷爷，要聊点什么？」",
          await run("爷爷") == "你好，爷爷，要聊点什么？")
    check("无称呼 → 「你好，要聊点什么？」（不留空档「你好，，」）",
          await run("") == "你好，要聊点什么？")
    for p in ("您", "你"):
        check(f"「{p}」是代词，套进去成病句 ⇒ 走无称呼版",
              await run(p) == "你好，要聊点什么？", f"addr={p}")
    check("「您老」不是纯代词，保留",
          await run("您老") == "你好，您老，要聊点什么？")

    # 真库：owner 存在但没有画像行 → 空串，不抛
    from app.db import init_db
    init_db()
    s = make_session(owner_id=999999)
    check("库里有 owner 但没有画像 → 空串（不抛异常）",
          s._wake_address() == "", f"{s._wake_address()!r}")
    s = make_session(owner_id=None)
    check("没绑定 owner → 空串（不查库）", s._wake_address() == "")

    # 称呼必须限长：画像里的 address 是 LLM 产物，理论上可被污染
    s = make_session(owner_id=1)
    s._wake_address = lambda: "很长的称呼" * 10
    text = await s._wake_greet_text()
    check("称呼过长时不至于念出一长串", len(text) <= 40, f"len={len(text)}")


# ================================================================ 第 3 组
async def t_skip_reasons() -> None:
    print("\n[3] 该闭嘴时必须闭嘴（每条判据单独钉）")

    def reason(**kw):
        s = make_session()
        for k, v in kw.items():
            setattr(s, k, v)
        return s._wake_greet_skip_reason()

    check("正常 listening + 没人说话 → 可以开口",
          reason(_ms_since_loud=10 ** 9) == "")
    check("★ 老人已经在说话（距高能量帧 100ms）→ 跳过",
          reason(_ms_since_loud=100) != "")
    check("刚说完 400ms（< 500ms 门限）→ 仍算在说话，跳过",
          reason(_ms_since_loud=400) != "")
    check("静了 600ms（> 500ms 门限）→ 可以开口",
          reason(_ms_since_loud=600) == "")
    check("state=speaking（正在播别的）→ 跳过",
          reason(state="speaking", _ms_since_loud=10 ** 9) != "")
    check("state=thinking（本轮 ASR/LLM 在跑）→ 跳过",
          reason(state="thinking", _ms_since_loud=10 ** 9) != "")
    check("本轮已被打断 → 跳过",
          reason(_abort=True, _ms_since_loud=10 ** 9) != "")

    async def done_task():
        return None

    async def with_turn():
        s = make_session()
        s._ms_since_loud = 10 ** 9
        s._turn_task = asyncio.create_task(done_task())
        await asyncio.sleep(0)          # 让它跑完 → done()
        finished = s._wake_greet_skip_reason()
        s._turn_task = asyncio.create_task(asyncio.sleep(10))
        running = s._wake_greet_skip_reason()
        s._turn_task.cancel()
        return finished, running

    finished, running = await with_turn()
    check("轮次 task 已跑完 → 不挡（否则每轮之后都永远跳过）",
          finished == "", f"{finished!r}")
    check("★ 轮次 task 正在跑 → 跳过", running != "", f"{running!r}")

    s = make_session()
    s._ms_since_loud = 10 ** 9
    s._dismissed_until = time.monotonic() + 60
    check("「退下吧」冷却期内 → 跳过", s._wake_greet_skip_reason() != "")


# ================================================================ 第 4 组
async def t_race_guard() -> None:
    print("\n[4] 竞态：读称呼期间本轮开始了 → 第二次检查必须拦住（防两路 TTS 抢通道）")
    from app import config

    old_delay = config.VOICE_WAKE_GREET_DELAY_MS
    config.VOICE_WAKE_GREET_DELAY_MS = 1
    try:
        s = make_session(owner_id=1)
        s._ms_since_loud = 10 ** 9
        spoke: list[str] = []

        async def fake_speak(text, started):
            spoke.append(text)

        async def slow_text():
            # 模拟"读称呼"这个 await 期间 listen stop 到达
            await asyncio.sleep(0.02)
            s.state = "thinking"
            return "你好，要聊点什么？"

        s._speak_reply = fake_speak
        s._wake_greet_text = slow_text
        await s._wake_greet()
        check("★ 期间本轮已开始 ⇒ 一个字都不能播（否则两路音频交错 = 破音）",
              spoke == [], f"spoke={spoke}")

        # 反过来：没人打扰时必须真的开口
        s2 = make_session(owner_id=1)
        s2._ms_since_loud = 10 ** 9
        spoke2: list[str] = []

        async def fake_speak2(text, started):
            spoke2.append(text)

        async def text2():
            return "你好，要聊点什么？"

        s2._speak_reply = fake_speak2
        s2._wake_greet_text = text2
        await s2._wake_greet()
        check("没人打扰时必须真的开口（否则功能等于没上线）",
              spoke2 == ["你好，要聊点什么？"], f"spoke={spoke2}")

        # 老人正在说话时不得开口
        s3 = make_session(owner_id=1)
        s3._ms_since_loud = 120
        spoke3: list[str] = []

        async def fake_speak3(text, started):
            spoke3.append(text)

        s3._speak_reply = fake_speak3
        s3._wake_greet_text = text2
        await s3._wake_greet()
        check("老人正在说话 ⇒ 不播寒暄（不打断他）", spoke3 == [], f"spoke={spoke3}")

        # TTS 抛异常不能把会话带崩
        s4 = make_session(owner_id=1)
        s4._ms_since_loud = 10 ** 9

        async def boom(text, started):
            raise RuntimeError("TTS 挂了")

        s4._speak_reply = boom
        s4._wake_greet_text = text2
        raised = None
        try:
            await s4._wake_greet()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        check("寒暄 TTS 失败必须被吞掉（不影响这一轮对话）", raised is None,
              f"抛了 {type(raised).__name__}")
    finally:
        config.VOICE_WAKE_GREET_DELAY_MS = old_delay


# ================================================================ 第 5 组
async def t_switch() -> None:
    print("\n[5] 总开关：关掉即完全回到旧行为")
    from app import config

    s = make_session()
    s._greet_task = None
    old = config.VOICE_WAKE_GREET
    try:
        config.VOICE_WAKE_GREET = False
        s._schedule_wake_greet()
        check("关掉后不排任务（现场嫌吵可以一键关）", s._greet_task is None,
              f"_greet_task={s._greet_task!r}")

        config.VOICE_WAKE_GREET = True
        s._schedule_wake_greet()
        check("打开后真的排了任务", s._greet_task is not None)
        if s._greet_task is not None:
            s._greet_task.cancel()

        # 连续两次唤醒：旧任务必须被取消，不能两个一起开口
        config.VOICE_WAKE_GREET_DELAY_MS = 5000
        s._schedule_wake_greet()
        first = s._greet_task
        s._schedule_wake_greet()
        second = s._greet_task
        await asyncio.sleep(0)
        check("连续两次唤醒只留一个任务（旧的被取消）",
              first is not second and first is not None and first.cancelled(),
              f"first.cancelled={getattr(first, 'cancelled', None)}")
        if second is not None:
            second.cancel()
    finally:
        config.VOICE_WAKE_GREET = old
        config.VOICE_WAKE_GREET_DELAY_MS = 900


# ================================================================ 第 6 组
async def t_defaults() -> None:
    print("\n[6] 配置默认值（这几个数都是「现场调参」的旋钮，别被人随手改掉）")
    from app import config

    check("默认开启", config.VOICE_WAKE_GREET is True)
    check("延时默认 900ms（太短会吃掉老人的后半句）",
          config.VOICE_WAKE_GREET_DELAY_MS == 900,
          f"{config.VOICE_WAKE_GREET_DELAY_MS}")
    check("静默门限默认 500ms（小于它就算老人在说话）",
          config.VOICE_WAKE_GREET_QUIET_MS == 500,
          f"{config.VOICE_WAKE_GREET_QUIET_MS}")
    check("延时必须小于静默门限的两倍，否则老人早说完很久才开口",
          config.VOICE_WAKE_GREET_DELAY_MS <= 2 * config.VOICE_WAKE_GREET_QUIET_MS)


async def main() -> int:
    print("=== 唤醒应答（你好，<称呼>，要聊点什么？）单测 ===")
    await t_wiring()
    await t_text()
    await t_skip_reasons()
    await t_race_guard()
    await t_switch()
    await t_defaults()

    print(f"\n=== {_g_pass} 通过 / {_g_fail} 失败 ===")
    if _failures:
        print("失败项：")
        for f in _failures:
            print(f"  - {f}")
    return 1 if _g_fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
