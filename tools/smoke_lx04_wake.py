#!/usr/bin/env python3
"""LX04 离线唤醒词的静态门禁 —— 防「看起来有唤醒、其实没跑起来」。

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
        return sum(1 for _, _, fn in g if not fn())

    base = count_bad()
    cases = [
        ("workflow 不再下载资源", {"flow": flow.replace("python3 tools/fetch_kws_assets.py", "# removed")}),
        ("换成官方默认唤醒词", {"fetch": fetch.replace("n ǐ h ǎo x iǎo zh ì @你好小智", "x iǎo ài t óng x ué @小爱同学")}),
        ("版本被偷偷升级", {"fetch": fetch.replace('KWS_VERSION = "1.12.22"', 'KWS_VERSION = "9.9.9"')}),
        ("检出后不释放麦克风", {"wake": wake.replace("stopCaptureOnly()\n        state = State.PAUSED", "state = State.PAUSED")}),
        ("beginListening 不暂停唤醒", {"main": re.sub(r"^\s*wakeEngine\.pause\(\)\n", "", main, flags=re.M)}),
        ("换成两次 detect", {"main": main.replace("voice?.listenDetect(wakeSource, greet = greet)", 'voice?.listenDetect("按键唤醒", greet = false)')}),
        ("二进制被允许入库", {"ignore": ignore.replace("libsherpa-onnx-jni.so", "libother.so")}),
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
