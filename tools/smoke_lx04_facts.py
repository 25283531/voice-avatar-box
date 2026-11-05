#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LX04 实机事实防回归冒烟。

2026-11-04 的实机探测推翻了移植前调研的三条结论：屏幕是 800×480 横屏（不是
480×800 竖屏）、5mm 色带是 ≈47px（不是 24/46px）、中间键原厂上报 KEY_POWER
且应用层拦不住（必须 keylayout 重映射成 F1）。另外发现麦克风被 `mivpm` 常驻占用。

这些结论当时只写在文档里，代码、README、方案文档、代码注释里各留了一份旧数字，
于是"照文档改代码"会把熄屏/错位重新引回来。**写在注释里的教训不是判据** ——
本脚本把结论钉成可执行断言，由 `.github/workflows/lx04-build.yml` 每次构建都跑。

判据口径（本仓踩过的坑）：
  * 静态判据按**代码位**判，先剥注释再匹配。否则"防回归注释里提到 KEYCODE_POWER"
    自己就会踩红，或反过来让注释里的正确值掩盖代码里的错误值。
  * 与下游契约有关的属性（集合内容 / 朝向 / 单位口径）**逐条单独断言**，不靠
    "某段文本存在"顺带覆盖。
  * 用 `--root` 指向临时副本可做变异验证：每条断言都必须能被对应变异变红。

用法：python3 tools/smoke_lx04_facts.py [--root 仓库根]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TOPKEYS = "lx04/app/src/main/java/com/elder/lx04/input/TopKeys.kt"
MANIFEST = "lx04/app/src/main/AndroidManifest.xml"
BANDVIEW = "lx04/app/src/main/java/com/elder/lx04/light/LedBandView.kt"
DEPLOY = "lx04/scripts/deploy.sh"
FACTS = "docs/lx04/实机验证结果.md"
CHECKLIST = "docs/lx04/实机验证清单.md"
PLAN = "docs/lx04/LX04移植方案.md"
README = "lx04/README.md"
WORKFLOW = ".github/workflows/lx04-build.yml"
MAINACTIVITY = "lx04/app/src/main/java/com/elder/lx04/MainActivity.kt"
VOICECLIENT = "lx04/app/src/main/java/com/elder/lx04/net/VoiceClient.kt"
OPUSCODEC = "lx04/app/src/main/java/com/elder/lx04/audio/OpusCodec.kt"
AUDIOCAPTURE = "lx04/app/src/main/java/com/elder/lx04/audio/AudioCapture.kt"
KCOMPILE = "tools/kcompile.py"
KTLINTSTRUCT = "tools/ktlint_struct.py"
KTEST = "tools/ktest.py"

REQUIRED = [
    TOPKEYS, MANIFEST, BANDVIEW, DEPLOY, FACTS, CHECKLIST, PLAN, README, WORKFLOW,
    MAINACTIVITY, VOICECLIENT, OPUSCODEC, AUDIOCAPTURE,
    KCOMPILE, KTLINTSTRUCT, KTEST,
]

_FAILS: list[str] = []
_N = 0


def read(root: Path, rel: str) -> str:
    p = root / rel
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def kt_code(src: str) -> str:
    """Kotlin 代码位：去掉 KDoc/块注释与行注释。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())


def xml_code(src: str) -> str:
    """XML 代码位：去掉 <!-- --> 注释。"""
    return re.sub(r"<!--.*?-->", "", src, flags=re.S)


def sh_code(src: str) -> str:
    """Shell 代码位：去掉整行注释与行尾注释，保留命令字符串。"""
    out = []
    for line in src.splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(re.sub(r"(?<=\s)#.*$", "", line))
    return "\n".join(out)


def check(name: str, ok: bool, detail: str = "") -> None:
    global _N
    _N += 1
    if ok:
        print(f"  [OK]   {name}")
    else:
        msg = name + (f" —— {detail}" if detail else "")
        _FAILS.append(msg)
        print(f"  [FAIL] {msg}")


def run(root: Path) -> int:
    print(f"LX04 实机结论防回归  root={root}")

    print("\n[0] 被判文件齐全")
    for rel in REQUIRED:
        check(f"0 {rel} 存在", bool(read(root, rel)), "文件缺失，判据无法执行")

    # ------------------------------------------------------------------ 1 按键
    print("\n[1] 中间键映射（代码位，注释不参与）")
    topkeys = kt_code(read(root, TOPKEYS))
    decl = re.search(
        r"val\s+CONFIRM_KEY_CODES\s*:\s*Set<Int>\s*=\s*setOf\s*\((.*?)\)",
        topkeys,
        re.S,
    )
    check("1a 找得到 CONFIRM_KEY_CODES 声明", decl is not None, "声明被改了结构或文件缺失")
    codes = set(re.findall(r"KEYCODE_\w+", decl.group(1))) if decl else set()
    check("1b 集合恰好是 {KEYCODE_F1}", codes == {"KEYCODE_F1"}, f"实际={sorted(codes)}")
    check(
        "1c 不含 KEYCODE_POWER",
        "KEYCODE_POWER" not in codes,
        "放 POWER 会熄屏 + 长按弹关机菜单，且触摸屏 event2/3 也上报 POWER",
    )

    # ------------------------------------------------------- 2 系统侧重映射/占麦
    print("\n[2] keylayout 重映射与释放麦克风（deploy.sh 代码位）")
    sh = sh_code(read(root, DEPLOY))
    check(
        "2a key 116 映射成 F1",
        re.search(r"key\s+116\s+F1", sh) is not None,
        "中间键会退回 POWER ⇒ 按确认键熄屏",
    )
    check("2b 写的是 mtk-pmic-keys.kl", "mtk-pmic-keys.kl" in sh, "keylayout 文件名错了")
    check(
        "2c MIVPM_SERVICE 默认值是 mivpm",
        re.search(r'MIVPM_SERVICE="\$\{MIVPM_SERVICE:-mivpm\}"', sh) is not None,
        "占麦服务名被改，stop 会停在错误的进程上",
    )
    check(
        "2d 接管时真的执行 stop $MIVPM_SERVICE",
        re.search(r"stop\s+\$MIVPM_SERVICE", sh) is not None,
        "不停 mivpm ⇒ AudioRecord 拿不到麦克风（禁桌面无效）",
    )

    # -------------------------------------------------------------- 3 屏幕朝向
    print("\n[3] 屏幕朝向与开机直达（Manifest 代码位）")
    xml = xml_code(read(root, MANIFEST))
    check(
        "3a screenOrientation=landscape",
        'android:screenOrientation="landscape"' in xml,
        "实机是 800×480 横屏，锁 portrait 会让画面侧躺 90 度",
    )
    check("3b 没有 portrait", "portrait" not in xml, "出现了与实机相反的朝向")
    check(
        "3c HOME intent-filter 还在",
        "android.intent.category.HOME" in xml,
        "开机直达依赖 HOME 声明",
    )

    # ------------------------------------------------------------ 4 色带宽度口径
    print("\n[4] 5mm 色带的单位口径（LedBandView 代码位）")
    band = kt_code(read(root, BANDVIEW))
    check(
        "4a 用 COMPLEX_UNIT_MM + applyDimension 折算",
        "COMPLEX_UNIT_MM" in band and "applyDimension" in band,
        "按像素硬编码会在换 density 的固件上失去 5mm 语义",
    )
    check(
        "4b 折算参数仍是 5f（5mm）",
        re.search(r"COMPLEX_UNIT_MM\s*,\s*5f", band) is not None,
        "物理宽度被改了，或改成了 dp/px 口径",
    )
    check(
        "4c bandWidthPx 默认值来自折算函数",
        re.search(
            r"var\s+bandWidthPx\s*:\s*Int\s*=\s*defaultBandWidthPx\(context\)", band
        )
        is not None,
        "写死 24/46/47 会重新引入旧结论",
    )

    # -------------------------------------------------------------- 5 真源文档
    print("\n[5] 硬件事实真源文档没被删改")
    facts = read(root, FACTS)
    for tag, anchor in [
        ("5a 唯一真源声明", "唯一真源"),
        ("5b 800×480 横屏结论", "800×480 横屏"),
        ("5c 5mm≈47px", "47px"),
        ("5d mivpm 占麦硬约束", "mivpm"),
        ("5e 确认键 F1", "KEYCODE_F1"),
    ]:
        check(tag, anchor in facts, f"真源文档缺锚点 {anchor!r}")

    # ------------------------------------------------------------ 6 下游一致性
    print("\n[6] 下游文档与真源对账")
    readme = read(root, README)
    check("6a README 是横屏结论", "800×480 横屏" in readme, "README 还停在 480×800")
    check("6b README 认 KEYCODE_F1", "KEYCODE_F1" in readme, "README 仍说 KeyCode 待实测")
    check("6c README 认 mivpm 硬约束", "mivpm" in readme, "README 没写必须先停 mivpm")

    plan = read(root, PLAN)
    concl = [l for l in plan.splitlines() if l.startswith("**LX04 是 Android 设备")]
    check(
        "6d 方案一句话结论是横屏且不含 480×800",
        len(concl) == 1 and "800×480" in concl[0] and "480×800" not in concl[0],
        f"匹配到 {len(concl)} 行结论；内容={concl[:1]}",
    )
    rows = [l for l in plan.splitlines() if l.startswith("| 屏幕 |")]
    check(
        "6e 方案硬件表屏幕行是横屏",
        len(rows) == 1 and "800×480 横屏" in rows[0],
        "硬件表仍按竖屏写",
    )

    checklist = read(root, CHECKLIST)
    n_checked = len(re.findall(r"\|\s*☑\s*\|", checklist))
    check(
        "6f 阶段 0 实测表至少 10 行已填",
        n_checked >= 10,
        f"☑ 行数={n_checked}，实测表被清空会让阶段 0 看起来没做",
    )

    # ---------------------------------------------------------- 8 语音链路契约
    #
    # 2026-11-05 实机事故：按中间键，屏幕表情和侧边色带都切换成功，
    # 但**没有任何答复**。生产日志把两个独立断点分得清清楚楚：
    #
    #   断点①（客户端）：上行 Opus 一帧都没编出来
    #     → ASR 音频指纹 rms=0.0000 peak=0.000 时长=0.0s
    #     → 真因：Concentus 编码器被两个线程并发踩（实测无锁 200/200 失败）
    #
    #   断点②（服务端）：listen start 被待机看门狗掐掉
    #     → listen start（mode=auto）→ 199ms 后「进入待机：连续 120s 没有有效内容」
    #     → listen stop 时 state 已是 idle ⇒ _finalize 直接 return ⇒ 不 ASR 不 TTS
    #     → 再往后 180 秒冷却期内连 listen start 都被忽略
    #
    # 断点②的解法是**客户端先发 detect**：服务端 _on_listen 的 detect 分支
    # 会清 _dismissed_until（解冷却）并刷新 _last_active_at（延后待机判据）。
    # 固件 application.cc:1007-1013 早就这么做了。
    print("\n[8] 语音链路契约（2026-11-05 实机事故的防回归）")

    main_code = kt_code(read(root, MAINACTIVITY))
    vc_code = kt_code(read(root, VOICECLIENT))
    opus_code = kt_code(read(root, OPUSCODEC))
    cap_code = kt_code(read(root, AUDIOCAPTURE))

    # ---- 8a/8b detect 必须先于 listen start -------------------------------
    # 判据要**顺序敏感**：只判"两个都出现了"会让顺序写反也判过。
    # 这正是本仓踩过的坑（判据"某段文本存在"顺带覆盖）。
    det_pos = main_code.find("listenDetect(")
    start_pos = main_code.find("listenStart()")
    check(
        "8a beginListening 里先 detect 再 listen start",
        det_pos != -1 and start_pos != -1 and det_pos < start_pos,
        f"detect@{det_pos} start@{start_pos} —— 顺序反了服务端会掐掉这一轮",
    )
    check(
        "8b detect 必须带 greet:false",
        re.search(r"listenDetect\s*\([^)]*greet\s*=\s*false", main_code) is not None,
        "不带 greet:false ⇒ 服务端会播唤醒应答，把老人刚开口的字吃掉",
    )
    check(
        "8c VoiceClient.listenDetect 支持 greet 开关",
        re.search(r"fun\s+listenDetect\s*\([^)]*greet\s*:\s*Boolean", vc_code) is not None,
        "listenDetect 表达不了 greet:false，就没法对齐服务端的 detect 语义",
    )
    check(
        "8d detect 的 JSON 真的带 greet 字段",
        re.search(r'put\s*\(\s*"greet"\s*,\s*false\s*\)', vc_code) is not None,
        "greet 形参在，但没写进 JSON —— 服务端收不到",
    )

    # ---- 8e/8f/8g Opus 编码器：加锁 + 自愈 -----------------------------------
    #
    # ⚠️ 这三条曾经是**假绿**：第一版只判"文件里出现过 synchronized"、
    # "出现过 rebuildEncoder"、"出现过 createEncoder/createDecoder"，
    # 结果把 encode 里的锁去掉、把自愈删掉、把两个构造合成一个，
    # 断言照样全绿 —— 因为那些字串在别的函数里还有。
    #
    # 教训（本仓反复踩）：判据必须**限定在被判的那段代码里**，
    # 而且"出现某个词"永远不等于"用对了"。下面三条都按函数体切片后判。
    def fn_body(code: str, name: str) -> str:
        """取某个具名函数的函数体（按大括号配平）。取不到返回空串。"""
        m = re.search(rf"fun\s+{re.escape(name)}\s*\(", code)
        if not m:
            return ""
        i = code.find("{", m.end())
        if i == -1:
            return ""
        depth = 0
        for j in range(i, len(code)):
            if code[j] == "{":
                depth += 1
            elif code[j] == "}":
                depth -= 1
                if depth == 0:
                    return code[i : j + 1]
        return ""

    encode_body = fn_body(opus_code, "encode")
    init_body = fn_body(opus_code, "init")

    check(
        "8e OpusCodec.encode 体内有同步锁",
        "synchronized" in encode_body,
        "Concentus 非线程安全；实测无锁并发 200/200 次触发断言",
    )
    check(
        "8f encode 失败会重建编码器并重试（自愈）",
        "rebuildEncoder" in encode_body,
        "断言踩坏编码器后，后续每帧都失败 ⇒ 整轮一帧都发不出去",
    )
    check(
        "8g 编码器与解码器分别构造（不共用一个 runCatching）",
        # ⚠️ 判据要点（三条同时成立才算过）：
        #    ① init 里分别调了这两个构造（不是同一个 runCatching 里的两行）
        #    ② 它们是**两个不同的函数**（函数体不相等）
        #    ③ **解码器那侧不许碰 encoder 字段** —— 这才是"连带废掉编码器"
        #       的真实形态：有人图省事，在 createDecoder 里写一句
        #       `encoder = null`，解码一失败就把上行也带走了。
        #       只判 ①② 抓不到它（第一版就漏在这里，变异验证时才发现）。
        # ⚠️ init 里造编码器走的是**可注入的工厂**（encoderFactory），
        #    不是直接 createEncoder —— 工厂是为了让单测能确定性地
        #    触发自愈路径（"第一次返回坏编码器"）。
        "encoderFactory" in init_body
        and "createDecoder" in init_body
        and fn_body(opus_code, "createEncoder") != fn_body(opus_code, "createDecoder")
        and "OpusEncoder(" in fn_body(opus_code, "createEncoder")
        and "OpusDecoder(" in fn_body(opus_code, "createDecoder")
        # ⚠️ 判据要查的是"解码器那边有没有碰 encoder 字段"，
        #    不是"函数体里有没有 encoder 这个词" ——
        #    createEncoder 里必然有（它就是造编码器的）。
        and not re.search(r"\bencoder\s*=", fn_body(opus_code, "createDecoder")),
        "共用一个 runCatching，或解码失败时连带废掉编码器 ⇒ 上行音频全灭",
    )

    # ---- 8h 采集线程必须确定退出 -----------------------------------------
    # 顺序敏感：先 stop 录音再 join，否则旧线程活着 → 并发踩编码器。
    stop_body = re.search(
        r"fun\s+stop\s*\(\s*\)\s*\{(.*?)\n    \}", cap_code, re.S
    )
    body = stop_body.group(1) if stop_body else ""
    rec_stop_pos = body.find("record?.stop()")
    join_pos = body.find(".join(")
    check(
        "8h stop() 先停录音再 join 采集线程",
        rec_stop_pos != -1 and join_pos != -1 and rec_stop_pos < join_pos,
        f"stop@{rec_stop_pos} join@{join_pos} —— 顺序反了旧线程会活下来并发踩编码器",
    )
    check(
        "8i join 超时要报警而不是静默",
        "JOIN_TIMEOUT_MS" in cap_code and "没退出" in cap_code,
        "旧线程活着就是下一轮必然「编码失败: null」，必须有现场证据",
    )
    check(
        "8j 不用 join 的返回值当布尔（Thread.join 返回 void）",
        # ⚠️ 这条是 CI 编译失败换来的：2026-11-05 第一次推送时写了
        #    `runCatching { it.join(T) }.getOrDefault(false)`，
        #    而 Thread.join(long) 返回 **void** ⇒ Kotlin 编译报
        #    "Unresolved reference: !"（`!joined` 里 joined 不是 Boolean）。
        #    本机没有 Android SDK、编译不了 Kotlin，只有 CI 能兜住这类错。
        #    判据：要么用 isAlive 判断，要么别把 join 的结果当值用。
        not re.search(r"join\s*\([^)]*\)\s*\}\s*\.\s*getOrDefault\s*\(", cap_code),
        "Thread.join 返回 void，getOrDefault(false) 类型对不上 —— CI 编译会红",
    )

    # ---- 8k/8l/8m/8n 采集通道数必须按 HAL 协商 -----------------------------
    #
    # 2026-11-05 实机新事实：录音期间读 /proc/asound/card0/pcm1c/sub0/hw_params
    # 得到 `channels: 2` —— HAL 给的是**双声道**，而代码原先按 CHANNEL_IN_MONO 读。
    # MTK 的 HAL 没有把 stereo downmix，于是读到的是左右交错的立体声：
    #   · 每帧 960 个 short 只覆盖 960/2/16000 = **30ms** 真实音频
    #     （协议要 60ms）⇒ 服务端按 60ms 播 ⇒ 声音慢一倍、音调减半；
    #   · link_test 跑 5 秒采到 165 帧（本该 83 帧，165 ≈ 83×2）。
    #
    # ⚠️ 这类故障**没有任何异常**，只是"声音变得不对"，
    #    所以必须靠代码位 + 单测双重钉死。
    loop_body = fn_body(cap_code, "captureLoop")
    open_body = fn_body(cap_code, "openRecord")
    check(
        "8k 每帧按 HAL 通道数读，再抽左声道还原成 60ms 单声道",
        "HAL_CHANNELS" in cap_code
        and "extractLeftChannel" in loop_body
        and re.search(r"ShortArray\s*\(\s*FRAME_SAMPLES\s*\*\s*ch\s*\)", loop_body) is not None
        and re.search(r"FRAME_SAMPLES\s*\*\s*2\s*\*\s*HAL_CHANNELS", cap_code) is not None,
        "HAL 是交错双声道：每帧只读 960 个 short 就只覆盖 30ms，声音慢一倍",
    )
    check(
        "8l 不许请求 CHANNEL_IN_STEREO（本机 HAL 不支持，读到的全是 0）",
        "CHANNEL_IN_STEREO" not in cap_code,
        "实机矩阵：所有 2ch 组合 rms=0（ALSA PREPARED + 'input device already opened'），"
        "1ch 组合才有信号",
    )
    check(
        "8u 生产采集不许挂平台 NS/AGC/AEC（会把信号压到近零并毒死 Opus 编码）",
        re.search(r"enablePlatformEffects\s*:\s*Boolean\s*=\s*false", cap_code) is not None,
        "挂平台效果链 ⇒ rms 5~80 且 Opus 100% 触发 Inlines.OpusAssert（真机实测）",
    )
    check(
        "8m 必须留有实机编码探针命令 enc_probe",
        # 判据落在**命令分发**与**函数体**两处：
        # 只有函数没有分发 = 命令永远不会被触发；
        # 只有分发没有函数 = 编不过。任缺其一都算不通过。
        re.search(r'"enc_probe"\s*->\s*runEncProbe\s*\(\s*\)', main_code) is not None
        and len(fn_body(main_code, "runEncProbe")) > 0,
        "没有探针就只能在真机上猜，猜错一次就是一轮 CI",
    )
    check(
        "8n 探针必须做「平台效果链开/关」对照",
        "listOf(true, false)" in fn_body(main_code, "runEncProbe"),
        "少了对照就分不清是效果链把数据改坏了，还是运行时差异",
    )
    test_code = kt_code(read(root, "lx04/app/src/test/java/com/elder/lx04/audio/AudioCaptureChannelTest.kt"))
    check(
        "8o 抽左声道有单测覆盖（且不止一个场景）",
        test_code.count("extractLeftChannel") >= 4 and "CHANNELS" not in test_code,
        "通道抽错在真机上是「声音慢一倍」，只能靠单测钉死",
    )

    # ---- 8p/8q/8r/8s 参数矩阵与 stub 真值 ------------------------------
    #
    # 2026-11-05 实机第三层发现：ALSA 层是好的（tinycap 直连 pcm1c 有信号，
    # rms 2270），坏的是 AudioFlinger 打开设备后的启动过程
    # （state: PREPARED + "input device already opened" ⇒ 读到的帧全零）。
    # ⇒ 必须能在真机上一把扫完 (音频源 × 采样率 × 通道数)。
    matrix_body = fn_body(main_code, "runMicMatrix")
    case_body = fn_body(main_code, "tryMicCase")
    check(
        "8p 必须留有麦克风参数矩阵命令 mic_matrix",
        re.search(r'"mic_matrix"\s*->\s*runMicMatrix\s*\(\s*\)', main_code) is not None
        and len(matrix_body) > 0,
        "没有矩阵就只能一轮 CI 试一个参数，定位慢十倍",
    )
    rates_found = set(re.findall(r"MicCase\s*\([^,]+,\s*[^,]+,\s*(\d+)\s*,", main_code))
    sources_found = set(re.findall(r"AudioSource\.([A-Z_]+)", main_code))
    check(
        "8q 矩阵要同时覆盖多个采样率与多个音频源",
        len(rates_found) >= 2 and len(sources_found) >= 3,
        f"rate={sorted(rates_found)} source={sorted(sources_found)} —— 覆盖不够就定位不出可用参数",
    )
    check(
        "8r 每组矩阵之间必须等设备释放",
        "Thread.sleep" in case_body and "release" in case_body,
        "MTK 的 HAL 释放有延迟，立刻开下一组会撞上 "
        "'input device already opened'，那一组必然全零（假阴性）",
    )
    stub = read(root, "tools/androidstub/android/media/MediaRecorder.java")
    def stub_const(name: str) -> int:
        m = re.search(rf"{name}\s*=\s*(\d+)", stub)
        return int(m.group(1)) if m else -1
    check(
        "8s stub 的音频源常量必须与真 Android 一致",
        stub_const("VOICE_RECOGNITION") == 6
        and stub_const("VOICE_COMMUNICATION") == 7
        and stub_const("DEFAULT") == 0
        and stub_const("MIC") == 1,
        "VOICE_RECOGNITION 真值是 6（曾写成 7）：本地与真机跑的是不同的音频源 —— "
        "典型的假绿",
    )
    # ⚠️ 这条守的是「stub 里凭空加 API」这个坑：
    #    2026-11-05 在 stub 里加了 AudioRecord.getReadCount()（真 SDK **没有**
    #    这个方法，getReadCount 是 AudioTrack 的），本地 kotlinc 全绿，
    #    CI 的 gradle（真 SDK）直接编译失败。
    #    ⇒ 往 stub 里加 API 之前必须确认真 SDK 里存在；这里留一条反向断言。
    rec_stub = read(root, "tools/androidstub/android/media/AudioRecord.java")
    def strip_java_comments(s: str) -> str:
        s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
        return re.sub(r"//[^\n]*", "", s)
    check(
        "8t stub 里不得出现真 SDK 没有的 API（AudioRecord.getReadCount）",
        "getReadCount" not in strip_java_comments(rec_stub),
        "AudioRecord 没有 getReadCount（那是 AudioTrack 的）：stub 加了就是给假绿开后门",
    )

    # ---------------------------------------------------------------- 7 接线
    print("\n[7] 判据真的会被 CI 执行")
    wf = read(root, WORKFLOW)
    check(
        "7a workflow 里有 python3 tools/smoke_lx04_facts.py",
        re.search(r"python3\s+tools/smoke_lx04_facts\.py", wf) is not None,
        "断言写了不跑 = 没有门禁（本仓 2026-10-30 真实踩过）",
    )
    # ⚠️ 2026-11-05 新增的两个门禁同样必须真的被执行 ——
    #    "断言写了不跑 = 没有门禁"这条坑本仓踩过不止一次。
    check(
        "7d workflow 里有 python3 tools/kcompile.py",
        re.search(r"python3\s+tools/kcompile\.py", wf) is not None,
        "本地编译检查不跑 = Kotlin 编译错误只能等 CI 打回（已经连中三次）",
    )
    check(
        "7f workflow 里有 python3 tools/ktest.py",
        re.search(r"python3\s+tools/ktest\.py", wf) is not None,
        "本地单测不跑 = 单测失败只能等 CI 打回",
    )
    check(
        "7e workflow 里有 python3 tools/ktlint_struct.py",
        re.search(r"python3\s+tools/ktlint_struct\.py", wf) is not None,
        "结构检查不跑 = 多一个右花括号要到 CI 才被发现",
    )
    # ⚠️ push 与 pull_request 是**两段独立的** paths，必须分别判。
    # 早期版本只在 `on:` 全文里找一次文件名，结果删掉 push 段那行后
    # pull_request 段还留着 ⇒ 判据纹丝不动地全绿（变异 M11 抓到的假绿）。
    def on_paths(key: str) -> str:
        m = re.search(
            rf"(?ms)^  {key}:\s*\n(.*?)(?=^  [A-Za-z_]+:|^jobs:|\Z)", wf
        )
        return m.group(1) if m else ""

    for key, tag in [("push", "7b"), ("pull_request", "7c")]:
        check(
            f"{tag} {key} 的路径过滤含判据文件",
            "tools/smoke_lx04_facts.py" in on_paths(key),
            f"只改判据时 {key} 不会触发 CI",
        )

    print(f"\n{_N - len(_FAILS)}/{_N} 通过")
    if _FAILS:
        print(f"\n{len(_FAILS)} 条失败：")
        for f in _FAILS:
            print(f"  - {f}")
        return 1
    print("LX04 实机结论与代码/文档一致")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="仓库根目录（默认：本脚本所在 tools/ 的上一级）",
    )
    args = ap.parse_args()
    return run(args.root.resolve())


if __name__ == "__main__":
    sys.exit(main())
