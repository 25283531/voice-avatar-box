#!/usr/bin/env python3
"""长按即时应答 + 字幕累积 + 多条提醒同屏 —— 静态接线冒烟。

为什么是"静态"断言而不是跑起来：
  这三件事全部发生在**真机上**（按键回调、LVGL 布局、提醒引擎任务），
  本机没有屏也没有按键。可验证的部分只有"接线是否正确"：
  片段有没有被调用、追加分支还在不在、旧文案有没有被改回去。

⚠️ 一律**先剥注释再匹配**：本仓踩过"防回归注释自己踩红"的坑（见 MEMORY.md
   第 3 条）。这里剥 // 与 /* */，并且只在**代码位**上找调用点。

对应需求（2026-11-03 用户原话）：
  1. 长按屏幕显示日程 → 播「日程已确认完成，我就不在屏幕显示了」并退回对话态；
     长按屏幕显示服药提醒 → 播「您确认已经完成这次服药了吗？」并进入聆听。
  2. 累积多条提醒 → 同时显示在一屏上。
  3. 字幕显示不完整 → 要能翻页/滚动（根因是每句覆盖，见第 3 组）。

2026-11-04 新增第 5 组：#1 按键指示灯的长按灯效改成
「天青 #00E5FF 由暗变亮 → 3 秒达标天蓝 #00BFFF 全亮闪 3 次 / 中途松开粉红
 #FF69B4 全亮闪 5 次」。灯效只能在真机上看，本机唯一能守的是**代码位**：
渐变是不是还走同一色相（scale_color）、是不是还用墙上时钟、追问路径有没有
真的闪天蓝、灯珠宏有没有被顺手改掉。

2026-11-04 新增第 6 组（同日第二个需求：「确认窗口在 4 个小时内只要屏幕显示
的是待办事项就可以按键进行确认」）：日程类播报即 auto_done，引擎里没有待确认
项，但屏幕还挂着 4 小时 —— 闸门必须改成「引擎有待确认项 ∨ 屏上还挂着」，
并且 ui.c 的 show/hide 要跟着同步 #1 灯（hide 必须在清标志之后）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "firmware" / "components" / "elder" / "src" / "elder_bridge.cc"
CLIP_C = ROOT / "firmware" / "components" / "elder" / "src" / "elder_clip.c"
CLIP_H = ROOT / "firmware" / "components" / "elder" / "include" / "elder_clip.h"
REMINDER_C = ROOT / "firmware" / "components" / "elder" / "src" / "reminder.c"
#: 全屏提醒（4 小时窗口）与「屏显 / #1 灯同源」的真源
UI_C = ROOT / "firmware" / "components" / "elder" / "src" / "ui.c"
LCD = ROOT / "firmware" / "xiaozhi" / "main" / "display" / "lcd_display.cc"
#: #1 按键灯的真源（`firmware/xiaozhi/components/elder` 是 merge 生成的副本，不读它）
RGB_C = ROOT / "firmware" / "components" / "elder" / "src" / "rgb.c"
#: 按键消抖的真源（第 7 组：按下与松开的消抖阈值必须不同）
INPUT_C = ROOT / "firmware" / "components" / "elder" / "src" / "input.c"
BOARD_H = ROOT / "firmware" / "components" / "elder" / "include" / "elder_board.h"
#: 设备状态机 / listen start 的真源（第 9 组）
APP_CC = ROOT / "firmware" / "xiaozhi" / "main" / "application.cc"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))


def strip_comments(src: str) -> str:
    """去掉 // 与 /* */ 注释，保留字符串字面量。

    顺序很重要：先块注释再行注释，且**行注释不能吃掉字符串里的 //**
    （本文件里有 "http://…" 这类字面量，虽然现在没有，但别留坑）。
    """
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


def t1_clip_ids() -> None:
    print("\n── 1. 两条新片段必须在枚举里，且时长表/名字表同步 ──")
    h = CLIP_H.read_text(encoding="utf-8")
    for name in ("ELDER_CLIP_SCHED_DONE", "ELDER_CLIP_MED_CONFIRM"):
        check(name in h, f"枚举里有 {name}", "elder_clip.h 找不到")
    c = strip_comments(CLIP_C.read_text(encoding="utf-8"))
    # 时长表必须比原来多两项（7 → 9）。少一项 = 下标错位，症状是"服药播了日程的话"。
    m = re.search(r"kClipMs\[ELDER_CLIP_COUNT\]\s*=\s*\{(.*?)\};", c, re.S)
    nums = re.findall(r"\b(\d{3,5})\b", m.group(1)) if m else []
    check(len(nums) == 9, "kClipMs 有 9 项（7 旧 + 2 新）", f"实际 {len(nums)} 项")
    check("3780" in nums and "2700" in nums,
          "新片段时长 3780 / 2700 在表里（sched_done / med_confirm）",
          f"nums={nums}")
    check('"sched_done"' in c and '"med_confirm"' in c,
          "kClipName 同步加了 sched_done / med_confirm",
          "日志名没加，现场排查时分不清是哪条片段")


def t2_long_press_wiring() -> None:
    print("\n── 2. 长按分支：日程播第一句、服药播第二句并进聆听 ──")
    b = strip_comments(BRIDGE.read_text(encoding="utf-8"))

    check("elder_clip_play(ELDER_CLIP_SCHED_DONE)" in b,
          "日程分支播 ELDER_CLIP_SCHED_DONE",
          "没接片段 ⇒ 老人按完只有一声哔")
    check("elder_clip_play(ELDER_CLIP_MED_CONFIRM)" in b,
          "服药分支播 ELDER_CLIP_MED_CONFIRM",
          "没接片段 ⇒ 又要等 1~3 秒 TTS，正是用户投诉的点")
    check("ListenAfterClip(ELDER_CLIP_MED_CONFIRM)" in b,
          "服药分支播完进聆听（ListenAfterClip）",
          "只播不问 ⇒ 老人答了也没人听")

    # 顺序：先确认（收屏）再播片段。反了会让老人听完 3.8 秒才看到屏收起来。
    sched = b.find("elder_clip_play(ELDER_CLIP_SCHED_DONE)")
    confirm = b.find("ConfirmMedicine(idx, ELDER_CFM_BUTTON);\n                if (!elder_clip_play")
    check(confirm >= 0 and confirm < sched,
          "日程分支：先 ConfirmMedicine（收屏）再播片段",
          "顺序反了 ⇒ 按完 3.8 秒内屏幕还挂着提醒")

    # 服药分支的片段调用必须在 TTS 兜底之前（否则先发 TTS 就失去"零延迟"的意义）。
    med = b.find("elder_clip_play(ELDER_CLIP_MED_CONFIRM)")
    tts = b.find("new std::string(prompt)")
    check(med >= 0 and tts >= 0 and med < tts,
          "服药分支：片段在 TTS 兜底之前（零延迟）",
          "顺序反了 ⇒ 仍然先走 TTS")

    # 片段不可用 / 开不了麦时必须还能退回原路径，不能变成"按了没反应"。
    #
    # 判据 2026-11-03 晚从 CanStartDialog() 换成 CanPlayClip()：前者要求
    # kDeviceStateIdle，而 realtime 对话期间设备一直停在 kDeviceStateListening
    # ⇒ 长按 3 秒既播不出片段、也进不了这条 fail-safe，现场就是**一片安静**。
    # CanPlayClip() 放行 Idle 与 Listening，只排除 Speaking/Connecting。
    check("if (med_n == 0 && !CanPlayClip())" in b,
          "药数未知 + 播不了片段 ⇒ 退回直接确认（fail-safe）",
          "删掉这条会让离线老人按了没反应")
    check("if (med_n == 0 && !CanStartDialog())" not in b,
          "fail-safe 不再用 CanStartDialog()（Listening 态会被判成开不了麦）",
          "回到 CanStartDialog ⇒ 对话中长按服药键静默无响应")


def t3_subtitle_accumulate() -> None:
    print("\n── 3. 字幕必须累积（每句覆盖 = 永远只有最后一句）──")
    src = strip_comments(LCD.read_text(encoding="utf-8"))
    check("merged += text;" in src,
          "assistant 字幕走追加（merged += text）",
          "回到 lv_label_set_text(target, text) 就是覆盖，分页永远不会触发")
    check("chat_assistant_tick_" in src,
          "有跨轮兜底（20 秒没新句 ⇒ 视为新一轮，覆盖不追加）",
          "没有它，无用户话的轮次会把两轮回答拼在一起")
    check("const bool dup =" in src,
          "重复句保护（同一句重发不追加两遍）",
          "工具轮重发同一句时老人会看到两遍")
    check("ChatPageLayout(anchor_px)" in src,
          "写完文字后仍然调 ChatPageLayout(anchor_px)（分页入口）",
          "删了就没分页；不带 anchor_px 就等于每句都弹回第 1 页")


def t4_reminder_list() -> None:
    """多条提醒逐条列出 + 多味药一味一行 + 明细换行不裁字。

    2026-11-04 追加（用户第 3 条）：「两种药是按照一行显示的，所以第二种药只
    显示了前几个字。」
      根因在上游：sync.c 把同一时刻的多味药拼成一行「降压药1片、降脂药1片」，
      而 183px / 30px 字号一行只放 6 个汉字 —— 第二味药必然只剩前几个字。
      修法分两层，两层都要守：
        · reminder.c（文案层）：输出给屏幕的那一段把全角顿号还原成换行；
        · lcd_display.cc（显示层）：明细用 LV_LABEL_LONG_WRAP（折行不裁字），
          放不下时先压 text_line_space，压到底再纵向滚动，**不截断行**。

    本组新增的每条断言都做过「改坏 → 变红 → 还原」验证（2026-11-04，逐条跑、
    每次只改一处、跑完还原；括号里是变红的那一条）：
      · `med[used++] = '\\n';` 改成 `med[used++] = ' ';`（拼成一行）
            ⇒ 「多味药之间用 \\n 分隔」
      · `sep_mark = "、"` 改成 `","`（不再按真分隔符切）
            ⇒ 「按 UTF-8 子串切分」
      · 脏数据三元式改回 `med[used++] = c;`
            ⇒ 「脏数据仍换成空格」
      · ShowReminder 的明细改回 LV_LABEL_LONG_DOT
            ⇒ 「明细 label 是 LV_LABEL_LONG_WRAP」
      · 加回 `if (item_lines > fit_lines) item_lines = fit_lines;`
            ⇒ 「明细不再按可用行数截断」
      · 压行距循环的条件改成 `ls >= 0`（等于不压）
            ⇒ 「放不下先压 text_line_space」
      · 删掉 `lv_obj_add_flag(reminder_items_, LV_OBJ_FLAG_SCROLLABLE);`
            ⇒ 「压到底仍放不下 ⇒ 纵向滚动」
    结果：7 条改坏点全部按预期变红，每次只红预期的那一条。
    """
    print("\n── 4. 多条提醒逐条列出 + 多味药一味一行 ──")
    src = strip_comments(REMINDER_C.read_text(encoding="utf-8"))
    check("（还有 %u 条）" not in src,
          "旧的「（还有 N 条）」已删除",
          "还在 ⇒ 老人知道不止一条却不知道是哪几条")
    check('"\\n等 %u 件"' in src or "\\n等 %u 件" in src,
          "超屏时有「等 N 件」兜底",
          "没有兜底 ⇒ 第 4 条起无声消失")
    check("pending_same_minute(item->fired_due_at)" in src,
          "明细与条数用**同一个**判据（pending_same_minute）",
          "两套判据会漂移：屏幕写 3 条、实际只列 2 条")

    # ---- 多味药：一味一行（2026-11-04 用户第 3 条）----
    check("med[used++] = '\\n';" in src,
          "多味药之间用 \\n 分隔（一味药一行），不再用「、」拼成一句",
          "拼成一句 ⇒ 183px 一行 6 个汉字，第二味药只剩前几个字")
    check('const char *sep_mark = "、";' in src and "strstr(q, sep_mark)" in src,
          "按 UTF-8 子串切分（「、」是 3 字节，不能按字节替换）",
          "按字节替换会把一个汉字切成两半，屏上是乱码方块")
    check("(c == '\\r' || c == '\\n' || c == '\\t') ? ' ' : c" in src,
          "脏数据（家属输入的 \\r\\n\\t）仍换成空格 —— 「一行一件事」契约不被破坏",
          "删掉它 ⇒ 药名里的换行会被显示层当成新的明细项")
    check('snprintf(med, sizeof(med), "%s", med_line_of(item));' not in src,
          "不再整串拷进 med（那正是「拼成一行」的写法）",
          "整串拷贝 ⇒ 顿号原样留在屏上")

    # ---- 显示层：换行而不是裁字 ----
    lcd = strip_comments(LCD.read_text(encoding="utf-8"))
    check("lv_label_set_long_mode(reminder_items_, LV_LABEL_LONG_WRAP);" in lcd,
          "ShowReminder 的明细 label 是 LV_LABEL_LONG_WRAP（折行，不打点裁字）",
          "LONG_DOT ⇒ 一行写不下就在末尾打点，正是用户报的「只显示前几个字」")
    check("item_lines = fit_lines" not in lcd,
          "明细不再按可用行数截断（item_lines 保持真实行数）",
          "截断 = 后面的药直接不显示，老人漏服")
    check("lv_obj_set_style_text_line_space(reminder_items_, item_line_space, 0);" in lcd
          and "ls >= -6" in lcd,
          "放不下时先压 text_line_space（最多 -6px）",
          "不压 ⇒ 3 行以上直接进滚动，老人要多滚一次才看得全")
    check("lv_obj_add_flag(reminder_items_, LV_OBJ_FLAG_SCROLLABLE);" in lcd,
          "压到底仍放不下 ⇒ 该区域纵向滚动（一行都不裁）",
          "不滚 ⇒ 第 4 行起被容器裁掉，屏上还是「只显示前几个字」")


def _define_value(src: str, name: str) -> str:
    """取 `#define NAME value` 的 value（src 必须已经剥过注释）。找不到返回空串。"""
    m = re.search(rf"^[ \t]*#define[ \t]+{re.escape(name)}[ \t]+(\S+)", src, re.M)
    return m.group(1) if m else ""


def _brace_body(src: str, start: int) -> str:
    """从 start 往后找第一个 `{`，按花括号配平返回其内部文本。

    只用于**已剥注释**的源码；这几个函数体里没有带花括号的字符串字面量。
    """
    i = src.find("{", start)
    if i < 0:
        return ""
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i + 1:j]
    return ""


def t5_button_led() -> None:
    """#1 按键灯新语义（2026-11-04）。

    本组每条断言都做过「临时改坏真源 → 跑本脚本 → 确认对应断言变红 → 还原」
    的验证（2026-11-04，改坏点记录如下，避免以后有人以为是装饰性断言）：
      · 渐变改回 lerp_color            ⇒ 5.1 的 scale_color / lerp_color 两条红
      · 渐变 el 偷换成 g.btn_anim_ms   ⇒ 5.1 的墙上时钟 / 渲染时钟两条红
      · GRAD_COLOR 0x00E5FF→0x00E6FF   ⇒ 天青常量红
      · OK_COLOR 0x00BFFF→0x00CED1     ⇒ 天蓝常量红
      · FAIL_COLOR 0xFF69B4→0xFF1493   ⇒ 粉红常量红
      · GRAD_MIN_LEVEL 20→0            ⇒ 起点亮度红
      · OK_TIMES 3→4 / FAIL_TIMES 5→4  ⇒ 次数两条分别红
      · acknowledged 退回 BTN_ATTRACT  ⇒ 5.3 两条都红
      · ELDER_RGB_CHAIN 20→9 / LEDS 9→8⇒ 灯珠宏防回归红
    """
    print("\n── 5. #1 按键灯：天青变亮 → 天蓝闪 3 次 / 粉红闪 5 次 ──")
    src = strip_comments(RGB_C.read_text(encoding="utf-8"))

    # 5.1 渐变分支（只切 `case BTN_GRADIENT:` 到 `case BTN_FLASH:` 这一小段）。
    #     不能整文件匹配：同一个 btn_step 里 FLASH 分支本来就该用
    #     g.btn_anim_ms（渲染时钟，防止系统忙时少闪），整文件断言会误杀。
    m = re.search(r"case BTN_GRADIENT\s*:(.*?)case BTN_FLASH\s*:", src, re.S)
    grad = m.group(1) if m else ""
    check(bool(grad), "能定位 BTN_GRADIENT 分支",
          "找不到 case BTN_GRADIENT … case BTN_FLASH，状态机被重构过？")
    check("scale_color(ELDER_RGB_BTN_GRAD_COLOR" in grad,
          "渐变用 scale_color(ELDER_RGB_BTN_GRAD_COLOR)（同一色相只改亮度）",
          "换成 lerp/dim ⇒ 要么变「换颜色」，要么暗端死黑看不出渐变")
    check("lerp_color" not in grad,
          "渐变分支不再用 lerp_color（它会把天青插值成别的色相）",
          "回到 lerp_color ⇒ 老人看到的是「换了个颜色」而不是进度")
    check("g.btn_phase_ms" in grad,
          "渐变进度用墙上时钟 g.btn_phase_ms（与 input.c 的 3 秒判定同源）",
          "不用墙上时钟 ⇒ 系统忙时灯还没亮满、按键已经算确认，灯在骗人")
    check("g.btn_anim_ms" not in grad,
          "渐变分支没有偷换成渲染时钟 g.btn_anim_ms",
          "渲染时钟会被 ELDER_RGB_ANIM_MAX_STEP_MS 拖慢，和 3 秒判定错位")

    # 5.2 颜色 / 次数常量（对应需求原词：天青、天蓝、粉红、3 次、5 次）
    for name, want, why in (
        ("ELDER_RGB_BTN_GRAD_COLOR", "0x00e5ffu", "天青色"),
        ("ELDER_RGB_BTN_OK_COLOR", "0x00bfffu", "天蓝色"),
        ("ELDER_RGB_BTN_FAIL_COLOR", "0xff69b4u", "粉红色"),
        ("ELDER_RGB_BTN_GRAD_MIN_LEVEL", "20u", "渐变起点亮度（不从全黑起）"),
    ):
        got = _define_value(src, name)
        check(got.lower() == want, f"{name} == {want}（{why}）",
              f"实际 {got or '缺失'}")
    for name, want in (("ELDER_RGB_BTN_OK_TIMES", "3"),
                       ("ELDER_RGB_BTN_FAIL_TIMES", "5")):
        got = _define_value(src, name)
        check(got == want, f"{name} == {want}（用户要求闪 {want} 次）",
              f"实际 {got or '缺失'}")

    # 5.3 追问路径：3 秒达标即闪天蓝，不能再退回橙色呼吸。
    pos = src.find("void elder_rgb_button_acknowledged(void)")
    ack = _brace_body(src, pos) if pos >= 0 else ""
    check("btn_enter_flash(ELDER_RGB_BTN_OK_COLOR, ELDER_RGB_BTN_OK_TIMES)" in ack,
          "elder_rgb_button_acknowledged() 调 btn_enter_flash(OK, 3)",
          "退回「只回橙色呼吸」⇒ 老人按满 3 秒却看不到任何回执")
    check("BTN_ATTRACT" not in ack,
          "acknowledged() 不再自己切 BTN_ATTRACT（迁移交给 btn_enter_flash）",
          "又写回 BTN_ATTRACT ⇒ 与 confirmed() 不一致，追问路径不闪结果色")

    # 5.4 灯珠宏防回归：CHAIN != LEDS 是本项目硬约束。
    #     后 11 颗必须每帧写 0，否则它们停在开机随机态常亮（2026-10-27 实报）。
    b = strip_comments(BOARD_H.read_text(encoding="utf-8"))
    for name, want in (("ELDER_RGB_LEDS", "9"), ("ELDER_RGB_CHAIN", "20")):
        got = _define_value(b, name)
        check(got == want, f"{name} 仍是 {want}",
              f"实际 {got or '缺失'} —— 改了它后 11 颗会停在开机随机态常亮")


def t6_actionable_gate() -> None:
    """「屏上显示着待办 ⇒ 按键就能确认、#1 灯亮」（2026-11-04）。

    需求原话：「确认窗口在 4 个小时内只要屏幕显示的是待办事项就可以按键进行确认」。

    守的是这条链：日程/待办类提醒**播报即 auto_done**（用户 2026-11-03 定的），
    DONE 不在 has_pending_confirm() 的集合里 ⇒ 引擎里"没有待确认项"，可屏幕
    还挂着 4 小时。此时 #1 的闸门若只判引擎状态，老人长按 3 秒灯不亮、没渐变
    （rgb.c 里 g.btn_pending 为假时按下去什么都不亮），他会以为按键坏了。
    所以闸门改成 elder_reminder_button_actionable() = 引擎有待确认项 ∨ 屏上
    还挂着；屏显的 show/hide 两处都要跟着同步灯，hide 必须在清标志**之后**。

    本组每条断言都做过「临时改坏真源 → 跑本脚本 → 确认对应断言变红 → 还原」
    的验证（2026-11-04，改坏点与结果记录如下，避免以后有人以为是装饰性断言）：
      · actionable 的 `|| elder_ui_reminder_showing()` 删掉
            ⇒ 6.1 两条红（缺屏显项 / 缺 || 连接）
      · reminder.c 里 490 行实参改回裸 has_pending_confirm()
            ⇒ 6.2 两条红（仍有裸实参 / actionable 调用点少一处）
      · ui.c 的 show_reminder 里删掉同步调用
            ⇒ 6.3 的 show 一条红
      · ui.c 的 hide_reminder 里删掉同步调用
            ⇒ 6.3 的 hide 一条红 + 6.4 顺序断言红（调用没了，find 返回 -1）
      · hide_reminder 里把同步调用挪到 `reminder_showing = false;` 之前
            ⇒ 6.4 顺序断言红（6.3 仍绿 —— 调用还在，正是顺序断言存在的理由）
      · has_pending_confirm() 集合里加 `st == ELDER_RMD_DONE`
            ⇒ 6.5 红
      · ELDER_UI_REMINDER_HOLD_US 改成 2 小时
            ⇒ 6.6 红
    """
    print("\n── 6. 屏上有待办 ⇒ 按键可确认、#1 灯亮（屏显与灯同源）──")
    r = strip_comments(REMINDER_C.read_text(encoding="utf-8"))
    u = strip_comments(UI_C.read_text(encoding="utf-8"))

    # 6.1 单一真源函数：两个判据都要在，且用 || 连接。
    #     不能只判引擎：日程类播报即 DONE，屏还挂着 4 小时（见函数 docstring）。
    pos = r.find("bool elder_reminder_button_actionable(void)")
    act = _brace_body(r, pos) if pos >= 0 else ""
    check(bool(act), "能定位 elder_reminder_button_actionable() 函数体",
          "找不到定义，函数被改名/删掉了？")
    check("has_pending_confirm()" in act,
          "actionable 判据包含 has_pending_confirm()（引擎里还有待确认项）",
          "删了它 ⇒ 服药等待确认时按键也没反应")
    check("elder_ui_reminder_showing()" in act,
          "actionable 判据包含 elder_ui_reminder_showing()（屏上还挂着提醒）",
          "删了它 ⇒ 日程类 auto_done 后屏挂 4 小时，按键依旧没灯")
    check(bool(re.search(r"has_pending_confirm\(\)\s*\|\|\s*elder_ui_reminder_showing\(\)",
                         act)),
          "两个判据用 || 连接（任一成立即可按键）",
          "写成 && ⇒ 只有「引擎有待确认项 且 屏还挂着」才亮，"
          "正是要修的「屏上有待办却按不动」")

    # 6.2 reminder.c 的闸门实参：一处都不许还是裸的 has_pending_confirm()。
    #     按行正则（已剥注释），不做整文件子串匹配 —— 本仓踩过注释自己踩红的坑。
    bare = re.findall(
        r"elder_rgb_button_pending\s*\(\s*has_pending_confirm\s*\(\s*\)\s*\)", r)
    check(not bare,
          "reminder.c 没有一处 elder_rgb_button_pending(has_pending_confirm())",
          f"仍有 {len(bare)} 处裸判据 ⇒ 那几条路径上屏挂着灯也不亮")
    n_act = len(re.findall(
        r"elder_rgb_button_pending\s*\(\s*elder_reminder_button_actionable\s*\(\s*\)\s*\)", r))
    check(n_act >= 4,
          f"reminder.c 的闸门调用点都走 actionable（≥4 处，实际 {n_act}）",
          "set_state / 跨天 / 合并 / auto_done 四处漏了任何一处，"
          "灯就会与屏不同步")

    # 6.3 ui.c 的 show / hide 都要同步灯。
    sp = u.find("esp_err_t elder_ui_show_reminder(const char *text)")
    show = _brace_body(u, sp) if sp >= 0 else ""
    hp = u.find("void elder_ui_hide_reminder(void)")
    hide = _brace_body(u, hp) if hp >= 0 else ""
    check(bool(show) and bool(hide), "能定位 ui.c 的 show/hide 函数体",
          "函数签名被改过？")
    call = "elder_rgb_button_pending(elder_reminder_button_actionable())"
    check(call in show,
          "elder_ui_show_reminder() 里同步 #1 灯（屏一亮就可按键）",
          "缺这一句 ⇒ 屏上待办挂 4 小时，灯全程不亮")
    check(call in hide,
          "elder_ui_hide_reminder() 里同步 #1 灯（屏收了灯也要收）",
          "缺这一句 ⇒ 确认后灯橙色呼吸到 4 小时上限才灭")

    # 6.4 hide 里同步调用必须在清掉 reminder_showing **之后**。
    #     这是「灯灭不掉」那个坑的防线：ConfirmMedicine() 的 set_state(DONE)
    #     → 灯同步，发生在 OnReminder(DONE) → hide 之前，那一刻屏还挂着。
    if hide:
        i_flag = hide.find("reminder_showing = false")
        i_call = hide.find(call)
        check(i_flag >= 0 and i_call > i_flag,
              "hide 里同步灯在 reminder_showing = false 之后",
              "顺序反了 ⇒ 同步时屏仍算「挂着」，灯被判成可按键、"
              "橙色呼吸到 4 小时上限")
    else:
        check(False, "hide 里同步灯在 reminder_showing = false 之后",
              "函数体没定位到")

    # 6.5 防回归：DONE 绝不许进 has_pending_confirm() 的状态集合。
    #     加进去会重复回调 OnReminder(DONE)、重复 enqueue_event(CONFIRMED)，
    #     家属端看到重复的确认记录。
    hc = r.find("static bool has_pending_confirm(void)")
    hb = _brace_body(r, hc) if hc >= 0 else ""
    check(bool(hb), "能定位 has_pending_confirm() 函数体",
          "函数被改名/挪走了？")
    check("ELDER_RMD_DONE" not in hb,
          "has_pending_confirm() 的状态集合里没有 ELDER_RMD_DONE",
          "加进去 ⇒ auto_done 的条目被当成待确认，重复回调 + 重复上报")

    # 6.6 4 小时窗口的唯一真源仍在 ui.c，没被顺手改短/改长。
    m = re.search(r"^[ \t]*#define[ \t]+ELDER_UI_REMINDER_HOLD_US[ \t]+(.+)$", u, re.M)
    raw = m.group(1).strip() if m else ""
    expr = re.sub(r"(?i)(?<=\d)(?:ull|ll|ul|u|l)\b", "", raw)
    hours = None
    if expr and re.fullmatch(r"[0-9*+() \t]+", expr):
        try:
            hours = eval(expr, {"__builtins__": {}}, {}) / (60 * 60 * 1000 * 1000)
        except Exception:
            hours = None
    check(hours == 4,
          "ELDER_UI_REMINDER_HOLD_US 仍是 4 小时",
          f"实际 {raw or '缺失'} ⇒ 用户明确要求 4 小时")


def t7_press_debounce_and_listen_after_clip() -> None:
    """长按 1 秒就闪粉红 + 播完问句不进聆听（2026-11-04 用户第二条的后半）。

    用户原话：「长按服药确认按键依然不能触发确认…按键超过1秒就开始进行粉红色
    闪烁。但是2分钟之后，再按确认键颜色就正常了…也触发了『您确认已经完成这次
    服药了吗』的语音提醒，但是没有进入聆听状态，而是进入了待机状态。」

    两件事都只能在真机上复现，本机守的是**代码位**：
      · 松开的消抖阈值必须**不等于**按下（同一个值 = 长按过程中的接触抖动会被
        采信成"松手" ⇒ rgb.c 把渐变掐掉、改闪粉红 5 次 = "按对了却报错"）；
      · elder_rgb_button_release() 里必须有时长兜底（<300ms 不闪粉红），而且
        那条分支**不能**调 btn_enter_flash —— 否则兜底等于没做；
      · ListenAfterClipTask 必须是"等 + 轮询"，不能再是一次 vTaskDelay 之后
        一次性 if/else —— 那正是"播完回待机、没人开麦"的结构性原因；
      · 长按分支必须有 idx/type/med_n/CanPlayClip 的决定性日志（用户两次投诉
        都只能靠猜，日志是下次还能查得动的唯一保证）。

    「改坏 → 变红」验证记录（2026-11-04，每次只改一处、跑完还原）：
      · RELEASE_DEBOUNCE_MS 30→80 改回 30（与 DEBOUNCE_MS 同值）
            ⇒ 7.1「松开阈值 ≠ 按下阈值」红
      · input_task 里 `raw ? DEBOUNCE_MS : RELEASE_DEBOUNCE_MS` 改回裸
        DEBOUNCE_MS ⇒ 7.1「按目标电平选阈值」红
      · release 里把 `held_ms < …` 判据改成 `false`
            ⇒ 7.2「有按压时长判据」红，连带「阈值」「兜底不闪粉红」共 3 条红
      · 兜底阈值 300→0 ⇒ 7.2「兜底阈值 == 300ms」红
      · 把 btn_enter_flash 补进 <300ms 兜底分支
            ⇒ 7.2「兜底分支不闪粉红」红
      · ListenAfterClipTask 的 `for (;;)` 改成 `if (true)`
            ⇒ 7.3「是循环」「循环里 StartListening」2 条红
      · extra_max_ms 4000→0 ⇒ 7.3「4000ms 上限」红
      · 长按日志里删掉 `CanPlayClip=%d` ⇒ 7.4 红
      · 所有「长按确认结论：」改回「长按：」 ⇒ 7.4 结论行数红（实际 0 行）
    """
    print("\n── 7. 松手消抖 / 抖动兜底 / 播完轮询开麦 / 长按决定性日志 ──")
    inp = strip_comments(INPUT_C.read_text(encoding="utf-8"))
    rgb = strip_comments(RGB_C.read_text(encoding="utf-8"))
    b = strip_comments(BRIDGE.read_text(encoding="utf-8"))

    # 7.1 按下与松开用**不同**的消抖阈值。
    #     同一个值就等于没修：长按中的几十毫秒接触抖动照样会被当成松手。
    press_deb = _define_value(inp, "DEBOUNCE_MS")
    rel_deb = _define_value(inp, "RELEASE_DEBOUNCE_MS")
    check(rel_deb != "", "input.c 定义了 RELEASE_DEBOUNCE_MS",
          "缺失 ⇒ 松开仍与按下共用 30ms，长按中抖动一次就闪粉红")
    check(rel_deb != "" and press_deb != "" and rel_deb != press_deb,
          f"松开消抖({rel_deb or '缺失'}) ≠ 按下消抖({press_deb or '缺失'})",
          "两个阈值相等 ⇒ 这次要修的 bug 原样装回去了")
    ipos = inp.find("static void input_task(void *arg)")
    itask = _brace_body(inp, ipos) if ipos >= 0 else ""
    check(bool(itask), "能定位 input_task() 函数体", "函数签名被改过？")
    check(bool(re.search(r"raw\s*\?\s*DEBOUNCE_MS\s*:\s*RELEASE_DEBOUNCE_MS",
                         itask)),
          "input_task 按目标电平选阈值（raw ? DEBOUNCE_MS : RELEASE_DEBOUNCE_MS）",
          "只定义不引用 ⇒ 松开还是 30ms；改回裸 DEBOUNCE_MS 也一样")
    check("按键松开（消抖 %d ms 采信" in itask and
          "按键按下（消抖 %d ms 采信）" in itask,
          "按下/松开采信各有一行日志（带消抖值，松开带按压时长）",
          "没日志 ⇒ 下次还是只能猜有没有采信到")

    # 7.2 release 的时长兜底：<300ms 的"松开"不闪粉红。
    rpos = rgb.find("void elder_rgb_button_release(void)")
    rel = _brace_body(rgb, rpos) if rpos >= 0 else ""
    check(bool(rel), "能定位 elder_rgb_button_release() 函数体",
          "函数被改名/挪走了？")
    m = re.search(r"held_ms\s*<\s*([A-Za-z_][A-Za-z_0-9]*|\d+u?)", rel)
    check(bool(m), "release 里有「本轮按压时长 < 阈值」的判据",
          "删了它 ⇒ 一次抖动就闪粉红 5 次，老人看到的是「按对了却报错」")
    if m:
        tok = m.group(1)
        raw_val = tok if tok[0].isdigit() else _define_value(rgb, tok)
        val = re.sub(r"(?i)(?<=\d)(?:ull|ll|ul|u|l)\b", "", raw_val or "")
        check(val == "300", f"兜底阈值 == 300ms（实际 {raw_val or '缺失'}）",
              "用户要求：300ms 以内的「松开」只可能是抖动，不是老人的真实意图")
        guard = _brace_body(rel, m.start())
        check("btn_enter_flash" not in guard,
              "兜底分支**不调用** btn_enter_flash（不闪粉红）",
              "兜底里还闪粉红 ⇒ 等于什么都没改，老人照样看到「失败」")
    else:
        check(False, "兜底阈值 == 300ms", "判据没定位到")
        check(False, "兜底分支**不调用** btn_enter_flash（不闪粉红）", "判据没定位到")

    # 7.3 播完问句：轮询到能开麦为止，不再是"到点看一眼"。
    lpos = b.find("static void ListenAfterClipTask(void *arg)")
    ltask = _brace_body(b, lpos) if lpos >= 0 else ""
    check(bool(ltask), "能定位 ListenAfterClipTask() 函数体", "函数被改名/挪走了？")
    i_delay = ltask.find("vTaskDelay")
    i_del = ltask.find("vTaskDelete")
    loop = -1
    if i_delay >= 0:
        for kw in ("for (", "while ("):
            j = ltask.find(kw, i_delay + 1)
            if j >= 0 and (loop < 0 or j < loop):
                loop = j
    check(0 <= i_delay < loop < i_del,
          "vTaskDelay 之后到 vTaskDelete 之间是循环（不再一次性 if/else）",
          "回到一次性判定 ⇒ 片段起播被队列推后时只打一条 WARN 就结束，"
          "老人听完问句设备却回待机")
    check(loop >= 0 and "StartListening" in ltask[loop:i_del],
          "循环体里会 StartListening（等不到就继续等，等到了就开麦）",
          "循环里没有开麦 ⇒ 轮询没有意义")
    check(re.search(r"\b4000\b", ltask) is not None,
          "有 4000ms 的等待上限（不能无限等）",
          "没有上限 ⇒ 状态卡在配网/连接时这个任务永远不退出")

    # 7.4 长按分支的决定性日志。
    norm = re.sub(r"\s+", " ", b)
    check("长按确认：idx=%d type=%d med_n=%u CanPlayClip=%d" in norm,
          "长按分支打 idx/type/med_n/CanPlayClip 决定性日志",
          "缺这一行 ⇒ 下次「长按不能确认」还是只能靠猜")
    n_concl = len(re.findall(r"长按确认结论：", norm))
    check(n_concl >= 4,
          f"每个分支走完都有结论日志（≥4 行，实际 {n_concl}）",
          "只有入口日志、没有结论 ⇒ 分不清走到哪条分支")


def t8_longpress_always_lands() -> None:
    """长按 3 秒必须**一定有结果**（2026-11-04 用户实报）。

    用户原话：「我按了按键，没有语音提示，没有屏幕内容切换，粉色灯闪烁了 5 次」，
    并确认自己按了 3 秒以上、灯在由暗变亮。

    根因不是抖动、不是灯效、不是固件没烧进去，而是 **2026-11-03 01:21 的
    a334cca**：服药类长按从「无条件 ConfirmMedicine」改成「播问句 + 进对话」，
    从此要过 CanPlayClip / elder_clip_play / CanStartDialog 三道闸。任何一道
    不过就只打一行日志 —— 不确认、不出声、灯还闪粉红（"你没按够"）。

    本组守两条底线，都是"行为契约"，不是实现细节：
      · **8.1 3 秒达标先给回执**：acknowledge 必须排在 `if (idx >= 0)` 之前，
        否则下游任何一条静默 break 都会把老人按对的动作判成失败；
      · **8.2 追问进不去必须兜底确认**：不许再出现"只打日志然后 break"。
    """
    print("\n── 8. 长按 3 秒一定有结果（回执 + 兜底确认）──")
    b = strip_comments(BRIDGE.read_text(encoding="utf-8"))

    lpos = b.find("case ELDER_KEY_EV_LONG_HOLD: {")
    check(lpos >= 0, "能定位 LONG_HOLD 分支", "case 标签被改过？")
    lbranch = _brace_body(b, lpos) if lpos >= 0 else ""
    check(bool(lbranch), "能取到 LONG_HOLD 分支体", "分支结构被改过？")

    # 8.1 回执必须排在第一层 `if (idx >= 0)` 之前。
    i_ack = lbranch.find("elder_rgb_button_acknowledged()")
    i_idx = lbranch.find("if (idx >= 0) {")
    check(i_ack >= 0, "LONG_HOLD 分支里调用了 elder_rgb_button_acknowledged()",
          "只有服药片段那条分支才调 ⇒ 其他分支静默 break 时老人看到粉红「失败」")
    check(0 <= i_ack < i_idx,
          "acknowledge 排在 `if (idx >= 0)` **之前**（3 秒达标就先给回执）",
          "排在分支里 ⇒ 分支静默 break 时回执丢失，粉红闪 5 次复现")

    # 8.2 兜底确认：CanStartDialog 过不去时必须直接确认，不能只有 break。
    i_csd = lbranch.find("if (!CanStartDialog())")
    check(i_csd >= 0, "能定位 `if (!CanStartDialog())` 分支", "判据被改名/挪走？")
    if i_csd >= 0:
        blk = _brace_body(lbranch, i_csd)
        check(bool(blk), "能取到 CanStartDialog 失败分支体", "结构被改过？")
        check("ConfirmMedicine(" in blk,
              "CanStartDialog 过不去 ⇒ **兜底直接确认**（不再静默失败）",
              "只打日志 + break ⇒ 老人按满 3 秒既没声音也不确认，"
              "正是用户这次报的现象")
        check("elder_rgb_button_confirmed()" in blk,
              "兜底路径也闪成功灯（不让老人看到「失败」）",
              "兜底确认了却不给成功反馈 ⇒ 老人不知道成没成")
        check("ELDER_CLIP_DONE" in blk,
              "兜底路径播**本地**预录片段（不依赖网络）",
              "用 TTS 兜底 ⇒ 离线时还是没声音")

    # 8.3 「问不了就确认」这条取舍必须写在**兜底分支自己**的注释里，别让下一个
    #     人按 a334cca 的思路又改回静默 break。查带注释的原文，且必须落在
    #     这个分支体内 —— 全仓别处也有同样的措辞，只查全文等于没查。
    raw_b = BRIDGE.read_text(encoding="utf-8")
    i_csd_raw = raw_b.find("if (!CanStartDialog())")
    blk_raw = _brace_body(raw_b, i_csd_raw) if i_csd_raw >= 0 else ""
    check("按了就记上" in blk_raw or "按了没反应" in blk_raw,
          "兜底确认的取舍理由写在该分支体内（可追溯）",
          "没有理由 ⇒ 下次有人按 a334cca 的思路再删掉它")


def t9_listen_start_always_sent() -> None:
    """进入 listening 必须**一定**给服务端发 listen start（2026-11-04 用户实报）。

    用户原话：「语音播报了『您确认完成……』的完整语句后，屏幕显示设备从待机
    转换为连接中，然后转换为聆听中，但是我说话他一直识别不到，我说了十几次
    确认，他都听不到，状态灯也一直是聆听状态的蓝色，很久很久。」

    服务端证据：WebSocket 建立后**一条消息都没有** —— 没有 listen start、
    没有音频帧、没有会话结束。设备在"假装聆听"。

    根因：`SetDeviceState(kDeviceStateListening)` 里发 listen start 的条件是
    `!IsAudioProcessorRunning() || listening_mode_ == kListeningModeRealtime`。
    老人长按追问走的是 **ManualStop**（StartListening() 里写死），刚播完本地
    预录片段时音频处理器仍在跑 ⇒ 条件恒假 ⇒ 不发。ManualStop 从来没被豁免。

    这条判据守的是**行为契约**：不许再出现"只在某个 listening 模式下才宣布"。
    """
    print("\n── 9. 进入 listening 必须发 listen start（不许按模式收窄）──")
    a = strip_comments(APP_CC.read_text(encoding="utf-8"))

    # 9.1 找到 kDeviceStateListening 分支。
    i_listen = a.find("case kDeviceStateListening:")
    check(i_listen >= 0, "能定位 SetDeviceState 的 listening 分支",
          "case 标签被改过？")
    seg = a[i_listen: i_listen + 2600] if i_listen >= 0 else ""
    i_send = seg.find("SendStartListening")
    check(i_send >= 0, "listening 分支里调用了 SendStartListening",
          "没有这条消息 ⇒ 服务端永远收不到上行音频，设备在假装聆听")

    # 9.2 不许用 IsAudioProcessorRunning() / kListeningModeRealtime 收窄。
    #     只查**紧邻的那一个 if 条件**：分支前面还有一段
    #     `if (!IsAudioProcessorRunning()) EnableVoiceProcessing(true)`，
    #     它是启动音频处理器用的，与 listen start 无关 —— 拿整段窗口匹配会
    #     误伤（这条断言自己踩过一次假红）。
    pre = seg[:i_send] if i_send >= 0 else ""
    j = pre.rfind("if (")
    cond = pre[j:j + 200] if j >= 0 else ""
    check("IsAudioChannelOpened" in cond,
          "SendStartListening 的紧邻判据是「通道是否打开」",
          f"实际条件：{cond[:80]!r}")
    check("IsAudioProcessorRunning" not in cond,
          "SendStartListening **不被** IsAudioProcessorRunning() 收窄",
          "音频处理器在跑时就不发 ⇒ ManualStop（长按追问）永远聋")
    check("kListeningModeRealtime" not in cond,
          "SendStartListening **不被** listening_mode 收窄",
          "只豁免 realtime ⇒ ManualStop 落回同一个坑")

    # 9.3 通道未打开时要留痕，不能静默。
    after = seg[i_send: i_send + 900] if i_send >= 0 else ""
    check("IsAudioChannelOpened" in seg,
          "发之前判通道是否打开（IsAudioChannelOpened）",
          "通道没开就发没有意义")
    check("ESP_LOGE" in after,
          "通道没开时留 ERROR（不静默）",
          "静默 ⇒ 又是屏幕上在听、服务端一无所知")

    # 9.4 文档锚点：StartListening 确实走 ManualStop（这正是旧条件漏掉它的原因）。
    i_sl = a.find("void Application::StartListening()")
    sl = _brace_body(a, i_sl) if i_sl >= 0 else ""
    check("kListeningModeManualStop" in sl,
          "StartListening() 走 ManualStop（旧条件漏掉它的原因，别被改掉）",
          "改成 Realtime 会牵动 AEC 与播报打断，不能为了绕开判据而改它")


def main() -> int:
    t1_clip_ids()
    t2_long_press_wiring()
    t3_subtitle_accumulate()
    t4_reminder_list()
    t5_button_led()
    t6_actionable_gate()
    t7_press_debounce_and_listen_after_clip()
    t8_longpress_always_lands()
    t9_listen_start_always_sent()

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
