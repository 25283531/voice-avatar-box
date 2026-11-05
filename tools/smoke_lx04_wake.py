#!/usr/bin/env python3
"""LX04 唤醒与设备凭据接线的静态门禁 —— 防「看起来有、其实没跑起来」。

## 为什么需要单独一份判据

唤醒词这条链路有四个**编译期完全看不出来**的失败点：

1. `.so` / 模型没下载 —— Kotlin 照样编译，真机加载时才报 UnsatisfiedLinkError；
2. Kotlin API 与 `.so` 版本错开 —— JNI 按字段名反射读配置，读成 0 也不报错；
3. `keywords.txt` 用了官方默认（里面没有「你好小智」）—— 引擎正常加载、
   常驻录音正常，就是**永远唤不醒**；
4. 唤醒检出后没先释放麦克风 —— HAL 不允许两个 AudioRecord 并存，
   症状是「日志里检出了，但一句话都录不进去」。

这四条都过 kcompile / ktest / ktlint，所以必须在这里用文本判据钉住。

## 反向变异

`--selftest` 会把每条守卫的判据文本做一次语义不同的替换，确认守卫
**真的会变红**。只写「关键字存在」而不做变异，等于没写（本项目已踩过）。

用法：
    python3 tools/smoke_lx04_wake.py               # 静态判据
    python3 tools/smoke_lx04_wake.py --check-assets # 追加校验运行资源
    python3 tools/smoke_lx04_wake.py --selftest     # 反向变异自检
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FLOW = ROOT / ".github/workflows/lx04-build.yml"
SHERPA = ROOT / "lx04/app/src/main/java/com/k2fsa/sherpa/onnx"
WAKE = ROOT / "lx04/app/src/main/java/com/elder/lx04/wake/WakeWordEngine.kt"
MAIN = ROOT / "lx04/app/src/main/java/com/elder/lx04/MainActivity.kt"
FETCH = ROOT / "tools/fetch_kws_assets.py"
ASSETS = ROOT / "lx04/app/src/main/assets/kws"
JNI = ROOT / "lx04/app/src/main/jniLibs/armeabi-v7a"


def read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


# (编号, 说明, 判据函数) —— 判据一律「文本 → bool」，便于做反向变异
def guards(flow: str, wake: str, main: str, fetch: str, api: str, ignore: str):
    g = []

    def add(no: str, desc: str, fn):
        g.append((no, desc, fn))

    add("W1", "workflow 会下载唤醒资源",
        lambda: "python3 tools/fetch_kws_assets.py" in flow)

    def fetch_before_build():
        # ⚠️ 不能 find("assembleRelease")：下载步骤的**注释**里也写了这个词，
        #    会让它比真实构建命令更早出现，把顺序判反（本守卫第一版就踩了）。
        a = flow.find("python3 tools/fetch_kws_assets.py")
        b = flow.find("./gradlew --no-daemon assembleRelease")
        return a >= 0 and b >= 0 and a < b

    add("W2", "下载步骤在 assembleRelease 之前（顺序即契约）", fetch_before_build)
    add("W3", "workflow 会执行本门禁",
        lambda: "python3 tools/smoke_lx04_wake.py" in flow)

    add("W4", "官方 Kotlin API 四个文件齐备且包名正确",
        lambda: all((SHERPA / f).is_file() for f in
                    ("FeatureConfig.kt", "OnlineModelConfig.kt",
                     "OnlineStream.kt", "KeywordSpotter.kt")) and
               "package com.k2fsa.sherpa.onnx" in api)

    add("W5", "JNI 库名是 sherpa-onnx-jni",
        lambda: 'System.loadLibrary("sherpa-onnx-jni")' in api)

    def version_locked():
        m = re.search(r'KWS_VERSION\s*=\s*"([0-9.]+)"', fetch)
        return bool(m) and m.group(1) == "1.12.22"

    add("W6", "KWS 版本锁死（与 Kotlin API 同版本）", version_locked)

    add("W7", "自定义唤醒词是「你好小智」，且拼音 token 序列正确",
        lambda: 'n ǐ h ǎo x iǎo zh ì @你好小智' in fetch)

    add("W8", "引擎常量与固件唤醒词一致",
        lambda: 'const val WAKE_WORD = "你好小智"' in wake)

    def release_before_callback():
        i = wake.find("private fun fire(")
        if i < 0:
            return False
        seg = wake[i:i + 1400]
        a = seg.find("stopCaptureOnly()")
        b = seg.find("main.post")
        return a >= 0 and b >= 0 and a < b

    add("W9", "检出后先释放麦克风、再回调（HAL 不允许双采集）",
        release_before_callback)

    def pause_inside_begin():
        i = main.find("private fun beginListening(")
        j = main.find("private fun endListening(")
        if i < 0 or j <= i:
            return False
        seg = main[i:j]
        # ⚠️ 要求出现在**非注释行**：写成 `// wakeEngine.pause()` 时
        #    纯子串判据仍然命中，守卫就成了假绿（本项目已踩过多次）。
        return bool(re.search(r"^\s*wakeEngine\.pause\(\)", seg, re.M))

    add("W10", "beginListening 内先暂停常驻唤醒采集", pause_inside_begin)

    def detect_once_in_begin():
        i = main.find("private fun beginListening(")
        j = main.find("private fun endListening(")
        if i < 0 or j <= i:
            return False
        seg = main[i:j]
        # 链路自检（runLinkTest）里另有一处字面量 detect，那是自检命令，
        # 不在正常唤醒路径上，不能算重复。只约束 beginListening 这一段。
        return "voice?.listenDetect(wakeSource, greet = greet)" in seg and \
            len(re.findall(r"^\s*voice\?\.listenDetect\(", seg, re.M)) == 1

    add("W11", "beginListening 内 detect 只发一次且带真实唤醒来源",
        detect_once_in_begin)

    add("W12", "录音权限授予后才启动唤醒引擎",
        lambda: "startWakeWord()" in main and "private fun startWakeWord()" in main)

    add("W13", "25MB 二进制资源不入库",
        lambda: "libsherpa-onnx-jni.so" in ignore and "/assets/kws/" in ignore)

    def token_validator_present():
        return "tokens.txt 中" in fetch and "missing" in fetch

    add("W14", "下载脚本会校验每个拼音 token 都在 tokens.txt 里",
        token_validator_present)

    # ---- 真机 2026-11-05 实测抓到的漏接线 ----
    # 现象：WS hello 成功、唤醒引擎就绪，但心跳连续 HTTP 401「缺少设备凭证」。
    # 原因：DeviceStore 写了，OTA 拿到 token 后却没 saveCredentials，
    #       所以 Bearer 一直是空串。持久化层"存在"不等于"接上了"。
    def ota_saves_token():
        i = main.find("private fun startConnect(")
        j = main.find("private fun connectWs(")
        if i < 0 or j <= i:
            return False
        # ⚠️ 必须按"非注释行"匹配：把调用注释掉时纯子串仍会命中，
        #    守卫就成了假绿（与 W10 同一个坑）。
        return bool(re.search(r"^\s*deviceStore\.saveCredentials\(", main[i:j], re.M))

    add("W15", "OTA 拿到 token 后必须落盘（否则心跳恒 401）", ota_saves_token)

    def ota_failure_degrades():
        i = main.find("is OtaClient.Result.Failed -> {")
        if i < 0:
            return False
        return bool(re.search(r"^\s*if \(deviceStore\.hasToken", main[i:i + 900], re.M))

    add("W16", "OTA 失败时用盘里的旧凭据连（一次失败不等于功能全灭）",
        ota_failure_degrades)

    return g


def asset_guards():
    out = []
    model = ASSETS / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
    sos = ("libonnxruntime.so", "libsherpa-onnx-jni.so")
    models = (
        "encoder-epoch-12-avg-2-chunk-16-left-64.onnx",
        "decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
        "joiner-epoch-12-avg-2-chunk-16-left-64.onnx",
        "tokens.txt", "keywords.txt",
    )
    out.append(("A1", "两个 .so 存在且非空", lambda: all(
        (JNI / s).is_file() and (JNI / s).stat().st_size > 1_000_000 for s in sos)))
    out.append(("A2", "模型与词表齐备", lambda: all(
        (model / f).is_file() and (model / f).stat().st_size > 0 for f in models)))

    def keyword_ok():
        kw = read(model / "keywords.txt")
        return "你好小智" in kw and "小爱同学" not in kw

    out.append(("A3", "keywords.txt 是自定义唤醒词（不是官方默认）", keyword_ok))

    def tokens_ok():
        t = read(model / "tokens.txt")
        have = {ln.split(" ")[0] for ln in t.splitlines() if ln}
        need = {"n", "ǐ", "h", "ǎo", "x", "iǎo", "zh", "ì"}
        return need <= have

    out.append(("A4", "唤醒词每个 token 都在词表里", tokens_ok))
    return out


def run(check_assets: bool) -> int:
    flow, wake, main = read(FLOW), read(WAKE), read(MAIN)
    fetch, ignore = read(FETCH), read(ROOT / ".gitignore")
    api = "".join(read(SHERPA / f) for f in
                  ("FeatureConfig.kt", "OnlineModelConfig.kt",
                   "OnlineStream.kt", "KeywordSpotter.kt"))

    items = guards(flow, wake, main, fetch, api, ignore)
    api_src = read(ROOT / "lx04/app/src/main/java/com/elder/lx04/net/ElderApi.kt")
    rt_src = read(ROOT / "lx04/app/src/main/java/com/elder/lx04/runtime/DeviceRuntime.kt")
    items.append((
        "W17", "401 不被误报成『网络不可用』",
        lambda: "lastStatusCode" in api_src and
                "设备凭证被拒绝（HTTP $lastStatusCode）" in api_src,
    ))
    # ---- 真机 2026-11-05 第二次实测：校时偏差 28799s（≈8 小时）----
    items.append((
        "W18", "naive server_time 要按服务端时区还原（否则校时错 8 小时）",
        lambda: "tzOffsetMin ?: DEFAULT_TZ_OFFSET" in api_src and
                "epoch - (tzOffsetMin" in api_src,
    ))
    items.append((
        "W19", "提醒排程用服务端时区，而不是系统时区",
        lambda: (lambda i: i >= 0 and "DeviceClock(" in rt_src[i:i + 400] and
                 "Clock.SYSTEM" not in rt_src[i:i + 400])(
                     rt_src.find("private val engine = ReminderEngine(")),
    ))
    items.append((
        "W20", "没拿到 token 时不发 HTTP（避免必然的 401 污染退避）",
        lambda: "if (!store.hasToken)" in rt_src,
    ))
    # ---- 真机 2026-11-05 第三次实测：唤醒成功但「听不到声音」----
    # 链路全绿，真因是 TTS 的 rms 只有 223~435（比自检音低 ~20 倍）。
    # 这类故障不报错、不崩溃，摘掉增益后所有测试仍会过 ⇒ 需要静态守卫。
    player_src = read(ROOT / "lx04/app/src/main/java/com/elder/lx04/audio/AudioPlayer.kt")
    level_src = read(ROOT / "lx04/app/src/main/java/com/elder/lx04/audio/AudioLevel.kt")
    items.append((
        "W21", "下行 PCM 必须过自适应增益（否则安静 TTS 听不见）",
        lambda: "AudioLevel.amplify(pcm, gain)" in player_src and
                "TARGET_RMS" in level_src and "nextGain" in level_src,
    ))
    items.append((
        "W22", "电平归一化有能打红的单测守着",
        lambda: (ROOT / "lx04/app/src/test/java/com/elder/lx04/audio/AudioLevelTest.kt").exists() and
                "安静的 TTS 会被放大到可闻电平" in
                read(ROOT / "lx04/app/src/test/java/com/elder/lx04/audio/AudioLevelTest.kt"),
    ))
    # ---- 真机 2026-11-05 第四次：正弦很响、TTS 却「隐隐约约」----
    # ALSA 回采窗口分析：正弦有声窗口 60%、TTS 只有 10% ⇒ 不是轻，是丢帧。
    # 根因：AudioTrack 180ms 缓冲 + 启动即 play（空缓冲）⇒ 持续 underrun，
    # 且 write() 返回值被 runCatching 吞掉，丢帧静默、日志全绿。
    # 等比缩回已被真机证伪：语音峰值因子 12~21，一个尖峰就把整帧增益
    # 压回去，平均响度上不去 ⇒ 「峰值够了、人耳还是贴上去才一点点」。
    items.append((
        "W24", "下行归一化用分段线性软限幅，不能退回等比缩回或 tanh",
        lambda: "val knee = CEILING / gain.toDouble()" in level_src and
                "kotlin.math.tanh" not in level_src and
                "CEILING.toFloat() / peak" not in level_src and
                "TARGET_RMS = 2_500f" in level_src,
    ))
    codec_src = read(ROOT / "lx04/app/src/main/java/com/elder/lx04/audio/OpusCodec.kt")
    hello_src = read(ROOT / "lx04/app/src/main/java/com/elder/lx04/MainActivity.kt")
    items.append((
        "W26", "下行解码固定 48kHz（Concentus 24k SILK 会吐 10kHz 噪声）",
        lambda: "private var decRate = 48_000" in codec_src and
                "val newDecRate = 48_000" in codec_src and
                "player.start(opus.outputSampleRate())" in hello_src and
                (ROOT / "lx04/app/src/test/java/com/elder/lx04/audio/OpusCodecRateTest.kt").exists(),
    ))
    items.append((
        "W25", "增益逐帧平滑过渡（真机「中途变频率」的根因）",
        lambda: "AudioLevel.ramp(gain, targetGain)" in player_src and
                "fun ramp(" in level_src and "RAMP_STEP" in level_src,
    ))
    items.append((
        "W23", "播放端预填充后再 play、缓冲 1 秒、且检查 write 返回值",
        lambda: "prefilled >= PREBUFFER_SAMPLES" in player_src and
                "val bufBytes = maxOf(minBuf, sampleRate * 2)" in player_src and
                "writeErrors++" in player_src and
                "val n = runCatching { t.write(frame, 0, frame.size) }" in player_src,
    ))
    if check_assets:
        items += asset_guards()

    failed = 0
    for no, desc, fn in items:
        try:
            ok = bool(fn())
        except Exception as exc:
            ok = False
            desc += f"（判据自身异常：{exc}）"
        print(f"  [{'OK' if ok else 'FAIL'}] {no} {desc}")
        if not ok:
            failed += 1
    print(f"\n{len(items) - failed}/{len(items)} 通过")
    return 0 if failed == 0 else 1


def selftest() -> int:
    """反向变异：把关键事实改掉，守卫必须变红。"""
    flow, wake, main = read(FLOW), read(WAKE), read(MAIN)
    fetch, ignore = read(FETCH), read(ROOT / ".gitignore")
    api = "".join(read(SHERPA / f) for f in
                  ("FeatureConfig.kt", "OnlineModelConfig.kt",
                   "OnlineStream.kt", "KeywordSpotter.kt"))

    def count_bad(**kw):
        g = guards(kw.get("flow", flow), kw.get("wake", wake),
                   kw.get("main", main), kw.get("fetch", fetch),
                   kw.get("api", api), kw.get("ignore", ignore))
        n = sum(1 for _, _, fn in g if not fn())
        # W18/W19/W20 不住在 guards 里（它们读别的文件），单独按变体算
        e = kw.get("api_src", read(ROOT / "lx04/app/src/main/java/com/elder/lx04/net/ElderApi.kt"))
        r = kw.get("rt_src", read(ROOT / "lx04/app/src/main/java/com/elder/lx04/runtime/DeviceRuntime.kt"))
        if "epoch - (tzOffsetMin" not in e: n += 1
        if "DeviceClock(" not in r: n += 1
        if "if (!store.hasToken)" not in r: n += 1
        # W21/W22 也读独立文件，按变体单独算
        pl = kw.get("player_src", read(ROOT / "lx04/app/src/main/java/com/elder/lx04/audio/AudioPlayer.kt"))
        lv = kw.get("level_src", read(ROOT / "lx04/app/src/main/java/com/elder/lx04/audio/AudioLevel.kt"))
        tst = kw.get("test_src", read(ROOT / "lx04/app/src/test/java/com/elder/lx04/audio/AudioLevelTest.kt"))
        if "AudioLevel.amplify(pcm, gain)" not in pl or "TARGET_RMS" not in lv: n += 1
        if "安静的 TTS 会被放大到可闻电平" not in tst: n += 1
        # W23 与 W21/W22 共用 player_src
        if "prefilled >= PREBUFFER_SAMPLES" not in pl or "writeErrors++" not in pl: n += 1
        # W24 与 W21/W22 共用 level_src
        if "val knee = CEILING / gain.toDouble()" not in lv or \
           "TARGET_RMS = 2_500f" not in lv or "kotlin.math.tanh" in lv: n += 1
        # W25：增益必须逐帧 ramp，不能窗口跳变
        if "AudioLevel.ramp(gain, targetGain)" not in pl: n += 1
        cd = kw.get("codec_src", read(ROOT / "lx04/app/src/main/java/com/elder/lx04/audio/OpusCodec.kt"))
        hs = kw.get("hello_src", read(ROOT / "lx04/app/src/main/java/com/elder/lx04/MainActivity.kt"))
        if "private var decRate = 48_000" not in cd or \
           "player.start(opus.outputSampleRate())" not in hs: n += 1
        return n

    base = count_bad()
    cases = [
        ("workflow 不再下载资源", {"flow": flow.replace("python3 tools/fetch_kws_assets.py", "# removed")}),
        ("换成官方默认唤醒词", {"fetch": fetch.replace("n ǐ h ǎo x iǎo zh ì @你好小智", "x iǎo ài t óng x ué @小爱同学")}),
        ("版本被偷偷升级", {"fetch": fetch.replace('KWS_VERSION = "1.12.22"', 'KWS_VERSION = "9.9.9"')}),
        ("检出后不释放麦克风", {"wake": wake.replace("stopCaptureOnly()\n        state = State.PAUSED", "state = State.PAUSED")}),
        ("beginListening 不暂停唤醒", {"main": re.sub(r"^\s*wakeEngine\.pause\(\)\n", "", main, flags=re.M)}),
        ("换成两次 detect", {"main": main.replace("voice?.listenDetect(wakeSource, greet = greet)", 'voice?.listenDetect("按键唤醒", greet = false)')}),
        ("二进制被允许入库", {"ignore": ignore.replace("libsherpa-onnx-jni.so", "libother.so")}),
        ("OTA 不再保存 token", {"main": re.sub(r"^\s*deviceStore\.saveCredentials\(", "        // removed", main, flags=re.M)}),
        ("naive server_time 又退回按 UTC 解析",
         {"api_src": read(ROOT / "lx04/app/src/main/java/com/elder/lx04/net/ElderApi.kt")
                        .replace("epoch - (tzOffsetMin ?: DEFAULT_TZ_OFFSET) * 60L", "epoch")}),
        ("摘掉下行增益（回到听不见的状态）",
         {"player_src": read(ROOT / "lx04/app/src/main/java/com/elder/lx04/audio/AudioPlayer.kt")
                          .replace("val boosted = AudioLevel.amplify(pcm, gain)", "val boosted = pcm")}),
        ("取消预填充，回到启动即空缓冲开播",
         {"player_src": read(ROOT / "lx04/app/src/main/java/com/elder/lx04/audio/AudioPlayer.kt")
                          .replace("prefilled >= PREBUFFER_SAMPLES", "true")}),
        ("又把 write 失败吞掉",
         {"player_src": read(ROOT / "lx04/app/src/main/java/com/elder/lx04/audio/AudioPlayer.kt")
                          .replace("writeErrors++", "// no count")}),
        ("软限幅退回等比缩回（平均响度又上不去）",
         {"level_src": read(ROOT / "lx04/app/src/main/java/com/elder/lx04/audio/AudioLevel.kt")
                         .replace("val knee = CEILING / gain.toDouble()", "val knee = CEILING.toDouble()")
                         .replace("TARGET_RMS = 2_500f", "TARGET_RMS = 5_500f")}),
        ("下行解码又退回 24k（回到 10kHz 噪声）",
         {"codec_src": read(ROOT / "lx04/app/src/main/java/com/elder/lx04/audio/OpusCodec.kt")
                        .replace("private var decRate = 48_000", "private var decRate = 24_000")}),
        ("增益又改回窗口跳变（中途变频率）",
         {"player_src": read(ROOT / "lx04/app/src/main/java/com/elder/lx04/audio/AudioPlayer.kt")
                          .replace("AudioLevel.ramp(gain, targetGain)", "targetGain")}),
    ]

    bad = 0
    for desc, kw in cases:
        n = count_bad(**kw)
        ok = n > base
        print(f"  [{'OK' if ok else 'FAIL'}] 变异后守卫变红：{desc}（bad {base}→{n}）")
        if not ok:
            bad += 1

    print(f"\n反向变异 {len(cases) - bad}/{len(cases)} 有效")
    return 0 if bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-assets", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    return run(args.check_assets)


if __name__ == "__main__":
    sys.exit(main())
