#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音输入 / 待机 / 服药卡片四条实报的回归冒烟（2026-11-03 晚）。

用户原话（五条里的第 1、2、4、5 条，第 3 条在 smoke_empty_asr.py 里）：
  1. 「如果我在10秒左右再次说话，前面两个字没有识别到。」
  2. 「偶发性的我说话设备完全没有识别到。」
  4. 「长按服药确认按键，没有提示音，松开之后也没有播放声音，我主动说我
     吃过药了，才进入服用了哪种药物的流程。」
  5. 「服药提醒页面上只显示了标题，没有显示需要服药的种类和剂量。」

为什么要单独一条冒烟
--------------------
这四条里有三条是**静默失败**：没有异常、没有报错、日志里连一行都没有。
   · 词头被裁掉 → ASR 少两个字，看起来像"识别不准"；
   · 服务端在忙时丢帧 → 老人说没反应，事后无从判断是麦克风还是服务端；
   · 片段被状态判据挡住 + TTS 兜底又被挡 → 按满 3 秒**完全安静**。
静默失败只能靠**判据本身**守，所以这里的断言都落在"判据"上，
而不是"某次调用的输出长什么样"。

判据口径
--------
· 第 1 条走**真函数**（``_trim_silence_edges``），断言"词头前的 pad 帧
  必须留在送 ASR 的音频里"，不写死帧数（常量改了不该假红）。
· 第 2 条走**真入口**（``_on_binary``），断言 thinking 态的帧被计数；
  同时断言唤醒/按键必须能解掉服务端冷却（那是"完全没识别到"的另一半）。
· 第 4、5 条是固件，只能静态验接线：判据函数是谁、调用顺序对不对、
  字段有没有一路接到显示层。
"""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import app.voice.session as S  # noqa: E402

FW = ROOT / "firmware"
ELDER = FW / "components" / "elder"          # 源组件（注入副本由 merge 生成）
MAIN = FW / "xiaozhi" / "main"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))


def strip_comments(src: str) -> str:
    """去掉 // 与 /* */ 注释，保留字符串字面量（与 smoke_subtitle_sync 同款）。"""
    out: list[str] = []
    i, n = 0, len(src)
    in_str = False
    quote = ""
    while i < n:
        ch = src[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if ch == quote:
                in_str = False
            i += 1
            continue
        if ch in ("'", '"'):
            in_str = True
            quote = ch
            out.append(ch)
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def func_body(src: str, signature: str) -> str:
    """取 `signature` 那个函数体（花括号配对）；找不到返回 ""。"""
    i = src.find(signature)
    if i < 0:
        return ""
    j = src.find("{", i)
    if j < 0:
        return ""
    depth = 0
    k = j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
        k += 1
    return ""


def py_func(src: str, signature: str) -> str:
    """取 Python 方法体（缩进 4 的方法）：从 signature 到下一个同级 def。

    ⚠️ 不能用 func_body：那个按花括号配对，只对 C/C++ 有效。这里踩过一次
    —— 拿 func_body 取 Python 函数永远返回 ""，断言全变成假红。
    """
    i = src.find(signature)
    if i < 0:
        return ""
    tail = src[i + len(signature):]
    ends = [m.start() for m in re.finditer(r"\n    (?:async )?def ", tail)]
    j = min(ends) if ends else len(tail)
    return tail[:j]


# ================================================== 1. 词头不再被切掉
def t1_head_pad() -> None:
    print("\n── 1. 送 ASR 前的词头 pad（用户：前两个字没识别到）──")
    head = S._TRIM_HEAD_PAD_FRAMES
    tail = S._TRIM_TAIL_PAD_FRAMES
    frame_ms = S.config.VOICE_FRAME_DURATION_MS or 60

    check(head > tail, "头 pad 比尾 pad 长（只有词头会丢内容）",
          f"head={head} tail={tail} —— 两头相等就是老行为")
    check(head * frame_ms >= 300, "头 pad ≥ 300ms（够盖住一两个轻声字）",
          f"实际 {head * frame_ms}ms")

    # ---- 真函数：语音帧前的 pad 必须留在输出里 ----
    n = 40
    first, last = 20, 29
    pcm = [bytes([i % 256, 0]) for i in range(n)]
    sess = types.SimpleNamespace()
    sess.session_id = "SMOKE-HEADPAD"
    sess._first_speech_frame = first
    sess._last_speech_frame = last
    sess._speech_idx = list(range(first, last + 1))
    sess._trim_silence_edges = types.MethodType(
        S.VoiceSession._trim_silence_edges, sess)
    out = sess._trim_silence_edges(list(pcm))

    check(isinstance(out, list) and all(isinstance(f, bytes) for f in out),
          "返回值仍是帧列表（与入参同型）",
          f"类型={type(out).__name__} —— 返回 bytes 会让下游 join 抛 TypeError")
    # 输出必须**从 first-head 开始**：这正是"词头前那几帧"的边界。
    want_start = max(0, first - head)
    check(len(out) == (last + tail) - want_start + 1,
          f"输出 = 语音帧 + 头 {head} + 尾 {tail} 帧",
          f"out={len(out)} want={(last + tail) - want_start + 1}")
    # 核心断言：first 之前的那 head 帧**逐帧**都在，且顺序不变。
    check(out[:head] == pcm[want_start:first],
          "第一个能量帧之前的 pad 帧逐帧保留（前两个字就藏在这里）",
          "少一帧就少 60ms 词头 —— 正是用户听到的「前面两个字没识别到」")
    check(out[head:head + (last - first + 1)] == pcm[first:last + 1],
          "语音帧本体逐帧未动", "裁静音不许碰语音帧")

    # ---- 变异：把 head pad 打回 2 帧，输出必须变短 ----
    real = S._TRIM_HEAD_PAD_FRAMES
    S._TRIM_HEAD_PAD_FRAMES = 2
    try:
        out2 = sess._trim_silence_edges(list(pcm))
    finally:
        S._TRIM_HEAD_PAD_FRAMES = real
    check(len(out2) == len(out) - (head - 2),
          "★ 变异可检出：head pad 退回 2 帧后输出正好短 (head-2) 帧",
          f"pad2_out={len(out2)} normal_out={len(out)}")


# ============================================ 2. 服务端在忙时丢帧要可诊断
def t2_drop_thinking() -> None:
    print("\n── 2. thinking 态丢帧计数（用户：偶发完全没识别到）──")
    sess = S.VoiceSession(websocket=object())  # type: ignore[arg-type]
    sess.session_id = "SMOKE-DROP"
    check(getattr(sess, "_drop_thinking_frames", None) == 0,
          "会话初始化时计数为 0", "字段不存在 ⇒ 后面所有断言都无从谈起")

    # 真入口：thinking 态的二进制帧应当被计数（而不是静默丢弃）
    sess.state = "thinking"
    for _ in range(3):
        sess._on_binary(b"\x00\x01\x00\x00" + b"\x00" * 20)
    check(sess._drop_thinking_frames == 3,
          "thinking 态的 3 帧被计入 _drop_thinking_frames",
          f"实际 {sess._drop_thinking_frames} —— 不计数就只能靠猜")

    # idle 态**不**计入（那条走重新武装，音频不丢）
    sess.state = "idle"
    before = sess._drop_thinking_frames
    sess._on_binary(b"\x00\x01\x00\x00" + b"\x00" * 20)
    check(sess._drop_thinking_frames == before,
          "idle 态的帧不计入丢帧（那条是重新武装，不是丢弃）",
          f"从 {before} 变成 {sess._drop_thinking_frames}")

    src = strip_comments((ROOT / "server" / "app" / "voice" / "session.py")
                         .read_text(encoding="utf-8"))
    fin = py_func(src, "async def _finalize")
    check("_drop_thinking_frames = 0" in fin,
          "_finalize 每轮把计数清零（否则会跨轮累加）", "")
    check("思考期丢弃上行音频" in src,
          "轮次结束会打一行汇总日志", "没有日志 = 这个数只活在内存里")

    # ---- 2026-11-05：服药确认追问（mode=manual）也必须能重新武装 ----
    # 现场：「对话时能识别到我说的话，服药确认时说什么都识别不了」。
    # 固件长按追问走 StartListening() → 写死 ManualStop → listen start 带
    # mode=manual；服务端 _finalize 每轮收尾把 state 置回 idle，而设备侧
    # 没有状态跃迁、不会重发 listen start。旧判据
    #   `if state == "idle" and _continuous()`
    # 在 manual 下恒假 ⇒ 上行音频被静默丢弃，日志里一个字都没有。
    #
    # 打桩 _rearm_listening：真函数要 asyncio.create_task（需要 running
    # loop），本脚本测的是"判据放不放行"，不是看门狗本身。
    sess.state = "idle"
    sess._listen_mode = "manual"
    sess._frames.clear()
    armed: list[int] = []

    def _rearm_stub() -> None:
        armed.append(1)
        sess.state = "listening"

    sess._rearm_listening = _rearm_stub  # type: ignore[method-assign]
    sess._on_binary(b"\x00\x01\x00\x00" + b"\x00" * 20)
    check(armed == [1] and len(sess._frames) == 1,
          "★ mode=manual：idle 态上行音频重新武装并收下（服药确认追问）",
          f"armed={armed} frames={len(sess._frames)} —— 不放行就是"
          "「对话能听、服药确认聋」")

    # 静态判据：防止有人把 manual 这个放行条件又收窄掉。
    onbin = py_func(src, "def _on_binary")
    check(bool(onbin) and '_listen_mode == "manual"' in onbin,
          "_on_binary 的 idle 兜底显式放行 manual",
          "改回只认 _continuous() 就原样装回这个 bug")

    # 反向：会话从没收到过 listen start 时不许凭一帧就武装。
    sess2 = S.VoiceSession(websocket=object())  # type: ignore[arg-type]
    sess2.session_id = "SMOKE-NOSTART"
    sess2.state = "idle"
    check(sess2._listen_mode == "" and not sess2._continuous(),
          "新会话 _listen_mode 为空、_continuous() 为假", "")
    armed2: list[int] = []
    sess2._rearm_listening = lambda: armed2.append(1)  # type: ignore[method-assign]
    sess2._on_binary(b"\x00\x01\x00\x00" + b"\x00" * 20)
    check(armed2 == [],
          "未宣布要听的会话不凭一帧自动武装（manual 放行不能泛化）",
          f"armed2={armed2}")


# ================================== 3. 长按服药确认必须有声音（固件）
def t3_med_clip() -> None:
    print("\n── 3. 长按服药确认必须有声音（用户：没有提示音、松开也没声音）──")
    bridge = strip_comments((ELDER / "src" / "elder_bridge.cc")
                            .read_text(encoding="utf-8"))

    can_play = func_body(bridge, "static bool CanPlayClip()")
    check(bool(can_play), "有 CanPlayClip()（片段专用判据）",
          "复用 CanStartDialog 就是老 bug：它要求设备在 Idle")
    check("kDeviceStateListening" in can_play,
          "CanPlayClip 允许 Listening 态",
          "连续对话下设备一直 Listening ⇒ 判据恒假 ⇒ 一点声音都没有")
    check("kDeviceStateSpeaking" not in can_play.split("return")[-1],
          "CanPlayClip 不放行 Speaking（会顶掉正在说的话）", "")

    check("CanPlayClip() && elder_clip_play(ELDER_CLIP_MED_CONFIRM)" in bridge,
          "服药问句的片段用 CanPlayClip 判，不再用 CanStartDialog",
          "用 CanStartDialog ⇒ Listening 态下片段被跳过")

    # TTS 兜底：必须在**建任务之前**判，否则任务自己静默 return。
    i_task = bridge.find('xTaskCreate(AskThenListenTask')
    i_gate = bridge.rfind("if (!CanStartDialog())", 0, i_task)
    check(i_task > 0 and i_gate > 0,
          "TTS 兜底前先判 CanStartDialog（建任务之前）",
          "建了任务再判 ⇒ AskThenListenTask 静默 return，老人听到的是安静")

    # 片段没播出去必须留日志
    check("预录问句未播" in bridge,
          "片段没播出去会打 WARNING（带 state）",
          "静默失败是这次排查最大的障碍")

    listen = func_body(bridge, "static void ListenAfterClipTask")
    check("kDeviceStateListening" in listen,
          "ListenAfterClipTask 认得「已经在聆听」",
          "不认就只打 WARNING 说'设备不在待机'，其实是它本来就在听")


# ================================ 4. 服药卡片要显示药名 + 剂量（固件）
def t4_med_card() -> None:
    print("\n── 4. 服药提醒卡片要有药名与剂量（用户：只显示了标题）──")
    hdr = strip_comments((ELDER / "include" / "elder_reminder.h")
                         .read_text(encoding="utf-8"))
    rmd = strip_comments((ELDER / "src" / "reminder.c").read_text(encoding="utf-8"))
    syn = strip_comments((ELDER / "src" / "sync.c").read_text(encoding="utf-8"))

    check("elder_med_line_t" in hdr, "头文件有 elder_med_line_t（显示行类型）", "")
    check("const elder_med_line_t *lines" in hdr,
          "set_med_info 带 lines 参数", "不带就永远拿不到剂量")
    check("elder_reminder_med_line" in hdr and "elder_reminder_med_line" in rmd,
          "有 elder_reminder_med_line() 取值接口", "")

    # 存：三块一起清零、一起写
    setfn = func_body(rmd, "void elder_reminder_set_med_info")
    check("memset(g.med_line, 0, sizeof(g.med_line));" in setfn,
          "换表时清掉旧显示行（缩表后不许串到别的提醒上）", "")
    check("memcpy(g.med_line[i], lines[i], ELDER_MED_LINE_MAX);" in setfn,
          "显示行按同下标写入", "")

    # 用：单条提醒时插一行明细
    screen = func_body(rmd, "void elder_reminder_screen_text")
    check("med_line_of(item)" in screen,
          "文案里取了该条提醒的显示行",
          "不取就还是「标题 + 请及时服用」两行")
    check('"%s\\n%s\\n%s", head, med, hint' in screen,
          "单条提醒输出「标题 / 药名剂量 / 提示语」三行",
          "少这一行就是用户看到的「只有标题」")
    check("'\\r' || *p == '\\n'" in screen or "\\r' || *p == '\\n'" in screen,
          "药名剂量里的换行被换成空格（一行一件事的契约）", "")

    # 拼：sync.c 从 medicines[].name + dose 拼
    check('cJSON_GetObjectItem(m, "dose")' in syn,
          "sync.c 读了 medicines[].dose", "不读 dose 就只有药名没有剂量")
    check("等%d味" in syn, "超过 2 味时用「等N味」收口", "")
    # 兜底：服务端 name 上限是 32 **字符** = 32 个汉字 = 96 字节，已经超过
    # 一行明细的 95 字节上限。直接 snprintf 截断会切出半个汉字（LVGL 画成
    # 乱码方块），整条丢弃又会退回"只有标题"—— 也就是用户报的那个问题。
    check("copy_utf8_head" in syn and "if (shown == 0)" in syn,
          "第一味都放不下时按 UTF-8 整字截断（不出半个汉字、也不丢整行）",
          "snprintf 直截 ⇒ 乱码方块；整条丢弃 ⇒ 「只有标题」又回来")
    check("n + clen + 1 > cap" in syn,
          "整字拷贝给自己留了结尾空字符的位置", "少留 1 字节就是 1 字节越界写")
    check("elder_med_line_t med_lines[ELDER_REMINDER_MAX];" in syn,
          "显示行缓冲在 sync_rmd_buf_t 里（堆上）",
          "放函数栈会压爆 elder_sync 任务（MEMORY 第 7 条）")
    check("elder_reminder_set_med_info(rb->med_counts, rb->med_names," in syn,
          "解析结果写回提醒引擎", "")


# ==================== 5. 按键唤醒要能解掉服务端待机冷却（固件 + 服务端）
def t5_wake_clears_cooldown() -> None:
    print("\n── 5. 按键唤醒必须能解掉服务端 180 秒待机冷却 ──")
    app = strip_comments((MAIN / "application.cc").read_text(encoding="utf-8"))
    body = func_body(app, "void Application::SetDeviceState")
    check("SendWakeWordDetected" in body,
          "Idle → Listening 时发 detect（服务端据此解冷却）",
          "不发 ⇒ 冷却期内老人按键说话，服务端全部丢弃，最长聋 3 分钟")
    check("previous_state == kDeviceStateIdle" in body,
          "判据是 Idle → Listening 这一次跃迁",
          "无条件发 ⇒ 播报完自动回 listening 也会解冷却，'退下吧'就不生效了")

    src = strip_comments((ROOT / "server" / "app" / "voice" / "session.py")
                         .read_text(encoding="utf-8"))
    listen = py_func(src, "async def _on_listen")
    check('obj.get("state")' in listen and "detect" in listen,
          "服务端 detect 分支存在（解冷却的唯一出口）", "")
    check("self._dismissed_until = 0.0" in listen,
          "detect 会清掉冷却", "")

    # ---- 但按键唤醒不许触发唤醒应答 ----
    #
    # 「你好，要聊点什么？」会让设备转成 Speaking、上行音频被丢弃 ——
    # 老人刚开口的字正好没了，就是用户报的「前面两个字没识别到」。
    check('obj.get("greet") is False' in listen,
          "服务端按 greet 字段决定是否应答",
          "判成 `not obj.get(...)` ⇒ 老固件不带该字段，所有唤醒都不应答且不报错")
    check("if not no_greet:" in listen,
          "greet=false 时跳过唤醒应答", "跳过这一判 ⇒ 按键唤醒也念一句寒暄，吃掉老人开口")
    check("SendWakeWordDetected(\"按键唤醒\", false)" in body,
          "固件按键唤醒带 greet=false",
          "不带 ⇒ 新服务端会念寒暄；这条链路的失败形态只有耳朵能发现")

    proto_h = strip_comments((MAIN / "protocols" / "protocol.h")
                             .read_text(encoding="utf-8"))
    proto_c = strip_comments((MAIN / "protocols" / "protocol.cc")
                             .read_text(encoding="utf-8"))
    check("bool greet = true" in proto_h,
          "SendWakeWordDetected 的 greet 默认 true（唤醒词那条路不变）",
          "默认 false ⇒ 真唤醒词也不应答了，老人以为没唤醒")
    check('"\\,\\"greet\\":false"' in proto_c or '\\"greet\\":false' in proto_c,
          "greet=false 时才在报文里加这个字段",
          "无条件加 ⇒ 默认报文与升级前不再逐字节一致，排查多一个变量")


def t6_workflow() -> None:
    print("\n── 6. 这个脚本必须在 CI 里真的被执行 ──")
    wf = (ROOT / ".github" / "workflows" / "server-ci.yml").read_text(encoding="utf-8")
    check("tools/smoke_voice_input_medcard.py" in wf,
          "server-ci.yml 里跑了 smoke_voice_input_medcard.py",
          "写了不跑 = 没门禁，而且不报错（MEMORY 第 1 条）")


def main() -> int:
    t1_head_pad()
    t2_drop_thinking()
    t3_med_clip()
    t4_med_card()
    t5_wake_clears_cooldown()
    t6_workflow()

    passed = sum(1 for ok, _, _ in results if ok)
    failed = len(results) - passed
    for ok, name, detail in results:
        if not ok:
            print(f"  [FAIL] {name}  {detail}")
    print("=" * 64)
    print(f"通过 {passed} 项，失败 {failed} 项")
    if failed:
        print("结论：**红**")
        return 1
    print("结论：**绿**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
