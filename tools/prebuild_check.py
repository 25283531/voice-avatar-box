#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prebuild_check.py —— 编译前的配置一致性自检（秒级，先于 idf.py build 跑）。

为什么需要这个脚本
------------------
2026-10-19 的实测事故：**改了仓库里的配置，但编译出来的固件没变**。
用户按 10-18 的要求把唤醒词改成了「你好，小暖」，`sdkconfig.defaults` 里
确实改对了 —— 但真正参与编译的 `sdkconfig`（本地生成、**不入库**）里
还是 10-17 的旧值「你好小智」，于是烧到设备上的固件喊「你好小暖」不应答。
用户报上来的是「离线唤醒不能用」，与「模型没烧进去」症状**完全一样**。

根因是 ESP-IDF 的标准行为，但**没有任何提示**：
  * `sdkconfig` 一旦存在，kconfig 就以它为准；
  * `sdkconfig.defaults` **只在首次生成时**参与；
  * 而 kconfig 对「已存在的选项」默认不覆盖 ⇒ 改了 defaults 也白改。

这类问题的特点是「不报错、不告警、改了仓库不生效」，属于最难查的一类。
所以把它变成**编译前必过的门禁**：不查出来就不让编译。

检查项
------
C1. ``sdkconfig`` 残留且与 ``sdkconfig.defaults`` 冲突（★ 本次事故的真凶）
C2. 关键选项与「仓库里的期望值」不符（如显示风格/唤醒词/分区表）
C3. 分区表文件存在（配置指向的文件别是空指针）
C4. merge 注入完整（elder 组件 + 70/30 布局标记）

用法：
    python tools/prebuild_check.py           # 有问题退 1
    python tools/prebuild_check.py --strict  # 警告也退 1（CI 用）
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
XIAOZHI = ROOT / "firmware" / "xiaozhi"
SDKCONFIG = XIAOZHI / "sdkconfig"
SDK_DEFAULTS = XIAOZHI / "sdkconfig.defaults"
BUILD_H = XIAOZHI / "build" / "config" / "sdkconfig.h"

#: overlay 段标记。⚠️ 必须与 tools/merge_to_xiaozhi.py 的 BEGIN/END **逐字一致**，
#: 改那边要改这边 —— 不一致的后果是 overlay_keys() 返回空集，
#: 于是 C1 什么都不检查、静默放行（比报错更糟）。
BEGIN_MARK = "# >>> BEGIN elder-companion overlay"
END_MARK = "# <<< END elder-companion overlay"

results: list[tuple[str, bool, str, bool]] = []  # (名称, 通过, 详情, 是否致命)
#: 本轮被 SKIP 掉的检查项名称。SKIP 内部记为 passed（好让 --strict 的
#: 通过/失败判定不看它），但**汇总必须把它单列**——把 SKIP 混进「通过 N 条」
#: 会让人以为每一项都真的查过了，而它们其实无判据可查。
skipped: list[str] = []

#: 「本项无判据可查」的标记。用了它的检查项打印 [SKIP]，
#: 既不算通过也不算警告 —— 在 --strict 下也不拦。
SKIP = object()


def check(name: str, ok: bool | object, detail: str = "", fatal: bool = True) -> None:
    """登记一条检查结果。

    ``ok`` 传 `SKIP` 表示「本项此刻无判据可查」——例如 sdkconfig.h 还没生成。
    这类项**不算失败也不算警告**：门禁必须只约束「已经存在的东西」，
    否则 CI 干净环境（无 build/、无 sdkconfig）会永远红，且它红的原因与
    真实缺陷无关 ⇒ 流水线从此不可信（这正是 2026-10-19 CI 失败的真因）。
    """
    if ok is SKIP:
        results.append((name, True, detail, False))
        skipped.append(name)
        print(f"  [SKIP] {name}")
        for ln in detail.splitlines():
            if ln.strip() and not ln.strip().startswith("⚠️"):
                print("         -> " + ln)
        return
    results.append((name, bool(ok), detail, fatal))
    tag = "PASS" if ok else ("FAIL" if fatal else "WARN")
    print(f"  [{tag}] {name}")
    # ⚠️ detail 里常写「⚠️ xxx」这样的告警文案（同一份文案在 FAIL 时是解释、
    #    在 PASS 时就是噪音）。PASS 时把以 ⚠️ 开头的行去掉，避免
    #    「[PASS] … -> ⚠️ 这些配置的改动不会生效」这种自相矛盾的输出
    #    —— 那种输出会让人怀疑整个脚本是不是坏了。
    if detail:
        # ⚠️ 只在 **PASS** 时滤掉以 ⚠️ 开头的行：那类文案在通过时是噪音，
        #    但失败时它**正是唯一的解释**。原先无条件过滤，导致 C11/C12 这类
        #    「[FAIL] 但一个字原因都不打」的输出 —— 一个不解释原因的失败门禁，
        #    比没有门禁更折磨人（2026-10-27 踩到）。
        lines = detail.splitlines()
        if ok:
            lines = [ln for ln in lines if not ln.strip().startswith("⚠️")]
        if lines:
            print("         -> " + "\n         ".join(lines))


def read_kv(path: Path) -> dict[str, str]:
    """读 kconfig 风格文件的 key=value（只看最后出现的，后定义覆盖先定义）。

    同时支持两种形态：
      * ``CONFIG_X=y``（sdkconfig / sdkconfig.defaults）
      * ``#define CONFIG_X "v"``（build/config/sdkconfig.h，C 头文件）
    ⚠️ 后者**必须能读**：C3b 要校验的目标芯片只存在于 .h 里，
       拿 kconfig 解析器去读 .h 会一律返回空 ⇒ 永远判红（第一版就这毛病）。
    """
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        # 形态二：C 头文件的 #define
        m = re.match(r"#define\s+(CONFIG_\S+)\s+(.+)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"')
            continue
        if not line or line.startswith("#"):
            # 「# CONFIG_X is not set」也算一种明确取值
            m = re.match(r"#\s*(CONFIG_\S+)\s+is not set", line)
            if m:
                out[m.group(1)] = ""
            continue
        # 形态一：kconfig 的 key=value
        m = re.match(r"(CONFIG_\S+?)[\s]*[=]", line)
        if m:
            key = m.group(1)
            val = line.split("=", 1)[1].strip() if "=" in line else "y"
            out[key] = val.strip('"')
    return out


def overlay_keys(defaults_path: Path) -> set[str]:
    """取 sdkconfig.defaults 里**我们自己的 overlay 段**里的键。

    段边界是 merge_to_xiaozhi.py 写下的 BEGIN/END 标记（同一份常量，
    改动时两处必须同步——已在 docstring 里注明）。

    为什么要单独取：
      * 我们主动改的只有这一段，改它时「忘了删 sdkconfig」才是真事故；
      * 其余行来自上游（板型默认值、Kconfig 默认），它们与 sdkconfig 不一致
        是**正常**的，逐条比对只会产出永久性假红，把真信号淹掉。
    """
    if not defaults_path.is_file():
        return set()
    inside = False
    keys: set[str] = set()
    for raw in defaults_path.read_text(encoding="utf-8",
                                       errors="replace").splitlines():
        s = raw.strip()
        if s.startswith(BEGIN_MARK):
            inside = True
            continue
        if s.startswith(END_MARK):
            inside = False
            continue
        if inside and s and not s.startswith("#") and "=" in s:
            keys.add(s.split("=", 1)[0].strip())
    return keys


def main() -> int:
    ap = argparse.ArgumentParser(description="编译前配置一致性自检")
    ap.add_argument("--strict", action="store_true", help="警告也视为失败（CI 用）")
    args = ap.parse_args()

    print("=" * 68)
    print("编译前配置自检（2026-10-19「改了配置但固件没变」事故而加）")
    print("=" * 68)

    defaults = read_kv(SDK_DEFAULTS)

    # ------------------------------------------------------ C1 sdkconfig 残留
    if SDKCONFIG.is_file():
        actual = read_kv(SDKCONFIG)
        # ⚠️ 只比**我们自己 overlay 的那一段**。上游按板型切了多个
        #    sdkconfig.defaults.esp32s3 等文件，它们的默认值（如 WiFi 缓冲区
        #    3/6）与我们的 sdkconfig.defaults（6/8）本来就不一样，**不是冲突**。
        #    第一版没排除它们 ⇒ set-target 之后永远报 C1 失败，而真正该拦的
        #    「改了我们自己的配置却没生效」反而被淹没在噪音里。
        #    做法：以 overlay 段标记为界，只取那段里的键。
        ours = overlay_keys(SDK_DEFAULTS)
        conflicts = []
        for k in ours:
            v = defaults.get(k, "")
            if k not in actual:
                continue
            av = actual[k]
            norm = {"y": "y", "": "n", "n": "n"}.get(av, av)
            want = {"y": "y", "": "n", "n": "n"}.get(v, v)
            if norm != want and not (want == "y" and norm == "y"):
                conflicts.append(f"{k}: defaults={v!r} 但 sdkconfig={av!r}")
        check(
            "C1 sdkconfig 与我们的 overlay 段无冲突",
            not conflicts,
            "⚠️ **这些配置的改动不会生效**（kconfig 以已存在的 sdkconfig 为准）："
            + "；".join(conflicts[:6])
            + ("…" if len(conflicts) > 6 else "")
            + f"\n         处置：删掉 {SDKCONFIG.name} 后重新编译（它不入库，"
              f"会自动按 defaults 重新生成）",
        )
    else:
        check("C1 无 sdkconfig 残留（将按 defaults 生成）", True)

    # ------------------------------------------------------ C2 关键选项
    # 用哪份为准：sdkconfig 在则以它为准（编译真正读的是它），否则用 defaults
    src = read_kv(SDKCONFIG) if SDKCONFIG.is_file() else defaults
    where = "sdkconfig" if SDKCONFIG.is_file() else "sdkconfig.defaults"

    check(
        "C2a 显示风格是标准 LVGL（不是 emote）",
        src.get("CONFIG_USE_DEFAULT_MESSAGE_STYLE", "n") == "y",
        f"{where} 里 CONFIG_USE_DEFAULT_MESSAGE_STYLE="
        f"{src.get('CONFIG_USE_DEFAULT_MESSAGE_STYLE', '(未设置)')!r}。"
        f"emote 那条路真机黑屏已实证（gfx_label: font context is NULL），"
        f"详见 sdkconfig.defaults 的注释段",
    )

    # 2026-10-20 回归上游小智：唤醒词 = WakeNet「你好小智」（wn9_nihaoxiaozhi_tts）。
    # 自定义 MultiNet 唤醒词（CONFIG_CUSTOM_WAKE_WORD / 「你好小安」）已整体删除。
    # WakeNet 的唤醒词训练在模型里，选错模型 = 设备应答别的名字，而真机上
    # 只表现为「喊它没反应 / 应答陌生称呼」，必须在这里钉死具体符号。
    # 判据必须扫 **defaults 全家族**（含 sdkconfig.defaults.esp32s3），
    # 不能只看 sdkconfig.defaults：wn9_nihaoxiaozhi_tts 是**上游自带**的，
    # 写在 sdkconfig.defaults.esp32s3 里；set-target 时两个 defaults 叠加读取。
    # 只看 sdkconfig.defaults 会永远假红（2026-10-20 实证）。
    wn_in_defaults: list[str] = []
    for dpath in sorted(XIAOZHI.glob("sdkconfig.defaults*")):
        for lineno, raw in enumerate(
            dpath.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            s = raw.strip()
            if s.startswith("#"):
                continue
            if re.match(r"CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=y\s*$", s):
                wn_in_defaults.append(f"{dpath.name}:{lineno}")
    wn_ok = (
        src.get("CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS", "n") == "y"
        or bool(wn_in_defaults)
    )
    check(
        "C2b2 WakeNet 唤醒模型是「你好小智」（wn9_nihaoxiaozhi_tts）",
        wn_ok,
        (f"defaults 家族里已开：{wn_in_defaults}"
         if wn_in_defaults else
         f"{where} 里 CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS="
         f"{src.get('CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS', '(未设置)')!r}，"
         f"且 defaults 家族也没开 ⇒ 唤醒失效（上游默认开启，缺失多半是误删）"),
    )

    # 反向守卫：别的 wn9 模型被同时打开会抢唤醒词（AfeWakeWord 只认最后
    # 匹配到的 wn 模型）——「改了没生效」的另一种形态。
    other_wn = [
        k for k in src
        if k.startswith("CONFIG_SR_WN_WN9") and k != "CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS"
        and src[k] == "y"
    ]
    check(
        "C2b3 没有别的 wn9 唤醒模型同时启用",
        not other_wn,
        f"{where} 里同时启用了 {other_wn} ⇒ 唤醒词可能被别的模型顶掉"
        if other_wn else "只有 wn9_nihaoxiaozhi_tts 一个 wn9 模型",
    )

    # 反向守卫之二：**defaults 全家族**（sdkconfig.defaults / .esp32s3 / .esp32p4 …）
    #
    # 为什么不并进 C2b3：判据对象不同。C2b3 查的是 sdkconfig ——「此刻编译真正
    # 读的那份」；这条查的是 defaults 家族 ——「下次重新生成时会不会又退回去」。
    #
    # 为什么必须扫**全部变体**，而不是只看 sdkconfig.defaults：
    #   IDF 在 `idf.py set-target <target>` 时会把 sdkconfig.defaults 与
    #   sdkconfig.defaults.<target> **叠加**读取（后者覆盖前者）。若两者设的是
    #   **不同的符号**（小智 vs 小安）⇒ 不会互相覆盖，只会「两个都 y」，
    #   两个模型双双打进 srmodels.bin，用哪个取决于加载顺序。
    #   2026-10-20 CI 实证：上游自带的 sdkconfig.defaults.esp32s3 里就留着
    #   CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=y。当时 C2b3 在 CI 上红、本地（把
    #   sdkconfig 删了）却绿 —— 正是「按环境分叉的假绿」，故把判据前移到
    #   defaults 家族，让本地和 CI 走**同一条路径**。
    wn_bad: list[str] = []
    dflt_files = sorted(XIAOZHI.glob("sdkconfig.defaults*"))
    for dpath in dflt_files:
        for lineno, raw in enumerate(
            dpath.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            s = raw.strip()
            if s.startswith("#"):
                continue
            m = re.match(r"(CONFIG_SR_WN_WN9\w*)=y\s*$", s)
            if m and m.group(1) != "CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS":
                wn_bad.append(f"{dpath.name}:{lineno} {m.group(1)}")
    check(
        "C2b4 defaults 全家族里没有别的 wn9 唤醒模型",
        not wn_bad,
        f"这些位置还开着别的 wn9 唤醒模型：{wn_bad}。"
        f"⚠️ set-target 会把 sdkconfig.defaults 与 sdkconfig.defaults.<target> "
        f"叠加读取，两者设的是不同符号 ⇒ 不会互相覆盖，只会两个都 y。"
        f"处置：把该行删掉（唤醒模型唯一真源 = 上游 sdkconfig.defaults.esp32s3 的 "
        f"CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=y）"
        if wn_bad else
        f"扫了 {len(dflt_files)} 个 defaults 变体"
        f"（{', '.join(p.name for p in dflt_files)}），"
        f"只有 wn9_nihaoxiaozhi_tts 一个 wn9 模型",
    )

    # 自研双引擎/自定义唤醒词已删除（2026-10-20 回归上游）。若有人把
    # MultiNet 自定义唤醒词或 dual_wake_word 又加回来，必须在这里吵出来：
    # 否则「回归上游」会静默变回自研路径，且没有任何报错。
    custom_wake = src.get("CONFIG_USE_CUSTOM_WAKE_WORD", "n") == "y"
    check(
        "C2b5 未启用自定义 MultiNet 唤醒词（回归上游）",
        not custom_wake,
        f"{where} 里 CONFIG_USE_CUSTOM_WAKE_WORD=y ⇒ 又切回自研 MultiNet 唤醒，"
        f"与「回归上游小智」的方向相反。处置：删掉该行并重跑 merge。",
    )

    part = src.get("CONFIG_PARTITION_TABLE_CUSTOM_FILENAME", "").strip('"')
    if not part:
        # 没有分区表配置 ⇒ 用的是默认分区表，此时「默认表是否含 assets 分区」
        # 无从判断。与 C3b 同理：无判据 ≠ 不通过。
        check("C3 分区表文件存在（含 assets 分区）", SKIP,
              "未显式配置 CONFIG_PARTITION_TABLE_CUSTOM_FILENAME ⇒ 用的是 Kconfig "
              "默认分区表，无具体文件可校验。assets 分区缺失则模型与形象都烧不进去，"
              "真出现时症状是「离线唤醒与表情全失效」。")
    else:
        check(
            "C3 分区表文件存在（含 assets 分区）",
            (XIAOZHI / part).is_file(),
            f"CONFIG_PARTITION_TABLE_CUSTOM_FILENAME={part!r}；"
            f"assets 分区缺失则模型与形象都烧不进去",
        )

    # ------------------------------------------------------ C3b 目标芯片
    # 真实事故（2026-10-19）：删掉 build/ 与 sdkconfig 后直接 `idf.py build`，
    # 此时 IDF 没有目标芯片的概念，**默认按 esp32 构建**。日志里那句
    # `-- Building ESP-IDF components for target esp32` 就是它。
    # 后果不是报错，而是连锁误判：
    #   * esp_video 的 rule 是 `if: target not in [esp32]` ⇒ 在 esp32 下
    #     它是「可选依赖」被跳过 ⇒ esp_cam_sensor 被判定无用 ⇒ 组件管理器
    #     尝试删除 179 个文件 ⇒ 在本机触发安全删除守卫，编译直接失败；
    #   * 即便侥幸编过，出来的也是**给 esp32 的固件**，烧到 S3 上不工作。
    # ⇒ 删 build 后的正确顺序是 set-target → build，脚本头部已写明，这里守着。
    if BUILD_H.is_file():
        target = read_kv(BUILD_H).get("CONFIG_IDF_TARGET", "")
        check(
            "C3b 目标芯片是 esp32s3（删 build 后必须先 set-target）",
            target == "esp32s3",
            f"build/config/sdkconfig.h 里 CONFIG_IDF_TARGET={target!r}，"
            f"期望 'esp32s3'。为空说明还没 set-target —— 直接 build 会按 "
            f"esp32 编译（esp_video 等依赖的 rule 会把摄像头组件判成无用）。"
            f"处置：python tools/idfwin.py set-target esp32s3",
            fatal=True,
        )
    else:
        # ⚠️⚠️ 这里**不能**报 WARN、更不能让它在 --strict 下红。
        #
        # 本工作流里自检步骤刻意排在 `set-target` **之前**，而 sdkconfig.h
        # 正是 set-target 生成的 ⇒ CI 干净环境下它必然不存在 ⇒ 报 WARN
        # 就等于把「流水线配置正确」判成「有问题」。
        # 2026-10-19 CI 就是这样红的：编译根本没开始，自检先拦下了。
        # 流水线的可信度一旦被这种假红破坏，人就会开始习惯性忽略红色，
        # 下次真的编不过时也不会看了。
        #
        # 正确的做法是让 set-target **之后再自检一次**（workflow 里已加），
        # 那时 sdkconfig.h 存在、C3b 才有判据可查，也才真的拦得住
        # 「按 esp32 编出固件」这个真事故。
        check(
            "C3b 目标芯片配置存在", SKIP,
            f"尚未 set-target，{BUILD_H.name} 还不存在 ⇒ 本项无判据可查。"
            f"（set-target 之后的第二次自检会真正校验它。编译顺序必须是"
            f"set-target esp32s3 → build，直接 build 会按 esp32 编）",
        )

    # ------------------------------------------------------ C5 固件自检期望值
    # config_report.c 里有一处**硬编码的期望唤醒词**（WakeNet 模型对应的汉字），
    # 设备开机时比对，不符就 ESP_LOGE。
    # 2026-10-20 回归上游小智后：期望值 = 「你好小智」，与 wn9_nihaoxiaozhi_tts 对应。
    # ⇒ 编译期把 C 侧期望值与「模型符号」对齐校验，防止改了模型却忘了改 C 侧，
    # 真机每次开机都误报 ERROR。误报比不报更糟——老人现场日志只有这一段。
    cfg_report = ROOT / "firmware" / "components" / "elder" / "src" / "config_report.c"
    try:
        csrc = cfg_report.read_text(encoding="utf-8")
        m = re.search(r'#define\s+EXPECTED_WAKENET_WORD\s+"([^"]*)"', csrc)
        expected_word = m.group(1) if m else None
        norm = lambda s: s.replace("，", "").replace(",", "").replace(" ", "")
        check(
            "C5 固件开机自检的期望唤醒词是「你好小智」",
            expected_word is not None and norm(expected_word) == "你好小智",
            f"config_report.c 里 EXPECTED_WAKENET_WORD={expected_word!r}，"
            f"期望 '你好小智'（对应 wn9_nihaoxiaozhi_tts）。"
            f"不一致时设备每次开机都会 ESP_LOGE 误报。",
            fatal=expected_word is not None,
        )
        # 反向守卫：C 侧不得再残留自研旧唤醒词期望值（wn9_nihaoxiaoan / 你好小安）。
        # 判据只查**期望值宏定义**，不查注释里的历史说明文字 —— 后者提到旧词是
        # 为了记录「为什么删掉它」，不是残留。若将来 EXPECTED_WAKENET_WORD 又被
        # 改回旧词，这里必须红。
        stale_expected = (
            expected_word is not None
            and expected_word.replace("，", "").replace(" ", "") == "你好小安"
        )
        # 反向守卫之二：宏定义行里不得出现旧模型符号（NIHAOXIAOAN）。
        # 只查**宏定义那一行**，不查整文件 —— 注释里保留旧符号是历史说明，不是残留。
        define_line = next(
            (ln for ln in csrc.splitlines()
             if ln.lstrip().startswith("#define EXPECTED_WAKENET_WORD")),
            "",
        )
        stale_define = "NIHAOXIAOAN" in define_line
        check(
            "C5b config_report.c 期望值未回退到自研旧唤醒词",
            not stale_expected and not stale_define,
            (f"宏定义行={define_line.strip()!r}，EXPECTED_WAKENET_WORD={expected_word!r} "
             f"—— 回归上游后期望值应为「你好小智」")
            if (stale_expected or stale_define) else "期望值仍是上游「你好小智」",
        )
    except Exception as exc:  # noqa: BLE001
        # 同 C3b：读不到判据本身 ≠ 判据不通过。报 WARN 会在 --strict 下拦 CI。
        check("C5 固件开机自检期望值校验", SKIP,
              f"无法读取 {cfg_report}: {exc}")

    # ------------------------------------------------------ C4 merge 注入
    try:
        rc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "merge_to_xiaozhi.py"), "--check"],
            capture_output=True, text=True, timeout=120, cwd=str(ROOT),
        ).returncode
        check("C4 merge 注入完整（elder 组件 + 70/30 布局）", rc == 0,
              "" if rc == 0 else "merge_to_xiaozhi.py --check 失败，先跑 merge")
    except Exception as exc:  # noqa: BLE001
        check("C4 merge 注入完整", False, f"无法执行 merge --check: {exc}")

    # ------------------------------------------------------ C6 emote 引擎已删除
    # 2026-10-20 用户要求「emote 现在能调用吗？不能调用就删除」。
    # 本产品固定走 LVGL 界面（SpiLcdDisplay），CONFIG_USE_EMOTE_MESSAGE_STYLE 恒 n
    # ⇒ emote 引擎永远不会被调用，已连同源文件与三个依赖一起删掉。
    #
    # 为什么需要一条守卫：这三个依赖是上游模板带的，`idf.py` 只会下载、不会报错；
    # 一旦有人「顺手补回依赖」，固件里就会重新出现一个没人调用的表情引擎
    # （它没有整体 #if 保护，654 行全部生成目标码）。**失败方向必须是红**，
    # 否则下次体积又悄悄涨回去且没人知道来源。
    comp_yml = ROOT / "firmware" / "xiaozhi" / "main" / "idf_component.yml"
    emote_src = ROOT / "firmware" / "xiaozhi" / "main" / "display" / "emote_display.cc"
    if comp_yml.exists():
        ytext = comp_yml.read_text(encoding="utf-8")
        # 只看**生效的配置行**：注释里必然还会提到这三个名字（说明为什么删的），
        # 那是文档不是依赖。判据必须是「这行会不会真被组件管理器读成依赖」。
        effective = "\n".join(
            line.split("#", 1)[0] for line in ytext.splitlines()
        )
        leftovers = [k for k in ("esp_emote_gfx", "image_player", "otto-emoji-gif")
                     if k in effective]
        check(
            "C6 emote 引擎依赖没有回到 idf_component.yml",
            not leftovers,
            f"依赖清单里又出现 {leftovers} —— 产品走 LVGL，这些永远不会被调用"
            if leftovers else "emote 三件套依赖均已移除（注释里的说明不算依赖）",
        )
        check(
            "C6b emote 源文件已删除（不是仅移出编译）",
            not emote_src.exists(),
            f"{emote_src} 仍然存在；若确实要恢复 emote，必须同时改显示层依赖"
            f"并打开 CONFIG_USE_EMOTE_MESSAGE_STYLE，不能只把文件放回来"
            if emote_src.exists() else "emote_display.cc/.h 已从仓库删除",
            fatal=emote_src.exists(),
        )
    else:
        check("C6 emote 依赖清理", SKIP, f"找不到 {comp_yml}")

    # ------------------------------------------------------ C7 网络栈已拆分
    # 2026-10-20 用户要求「4G 模组用不到就删除」。
    # 78/esp-ml307 是「ESP 原生网络栈 + Cat.1 4G 模组」的合体包，19 个源文件
    # 一起编译；main 是 WHOLE_ARCHIVE 链接 ⇒ 4G 那 12 个文件会整个进固件。
    # WiFi 侧的 7 个源文件已搬进 main/net（逐字未改），组件依赖整个删掉。
    #
    # C7c 是这里最要命的一条：esp_ssl.cc 里挂着证书 bundle。
    # 搬文件时漏掉那一行，HTTPS 会以「No server verification option set」
    # 在建连阶段就死，症状是「设备一直离线」，而日志只说连不上 —— 排查成本极高。
    net_dir = ROOT / "firmware" / "xiaozhi" / "main" / "net"
    ssl_src = net_dir / "esp" / "esp_ssl.cc"
    if comp_yml.exists():
        effective2 = "\n".join(line.split("#", 1)[0]
                              for line in comp_yml.read_text(encoding="utf-8").splitlines())
        check(
            "C7 4G 模组依赖（78/esp-ml307）已从依赖清单移除",
            "esp-ml307" not in effective2,
            "idf_component.yml 里仍有 esp-ml307 —— 它会带回 12 个 4G 源文件，"
            "而 main 是 WHOLE_ARCHIVE 链接，这些代码会整个进固件"
            if "esp-ml307" in effective2 else "WiFi 网络栈已内聚到 main/net，4G 依赖已删",
        )
    check(
        "C7b 网络栈源文件在 main/net 下齐备",
        (net_dir / "esp_network.h").exists() and (net_dir / "esp" / "esp_network.cc").exists()
        and (net_dir / "http_client.cc").exists() and (net_dir / "web_socket.cc").exists()
        and (net_dir / "network_interface.h").exists(),
        f"{net_dir} 缺文件；缺哪个都会在链接期报 undefined reference",
        fatal=True,
    )
    if ssl_src.exists():
        ssl_text = ssl_src.read_text(encoding="utf-8", errors="ignore")
        check(
            "C7c HTTPS 证书 bundle 仍在 esp_ssl.cc 里",
            "esp_crt_bundle_attach" in ssl_text,
            "esp_ssl.cc 里找不到 esp_crt_bundle_attach —— HTTPS 会在建连阶段"
            "以 No server verification option set 失败，表现为「设备一直离线」"
            if "esp_crt_bundle_attach" not in ssl_text else "证书 bundle 挂载点还在",
            fatal=True,
        )

    # ------------------------------------------------------ 双引擎/离线命令词已移除
    # 2026-10-20 回归上游小智：dual_wake_word.cc 与 elder_cmd/elder_mn 全部删除。
    # 这里守「删干净」：若有人把 dual_wake_word.cc 又加回来，必须吵出来，
    # 否则回归会静默变回自研双引擎路径。
    dual_src = ROOT / "firmware" / "xiaozhi" / "main" / "audio" / "wake_words" / "dual_wake_word.cc"
    elder_cmd_h = ROOT / "firmware" / "components" / "elder" / "include" / "elder_cmd.h"
    check(
        "C9 自研双引擎与离线命令词已删除",
        not dual_src.exists() and not elder_cmd_h.exists(),
        f"仍存在：{[str(p) for p in (dual_src, elder_cmd_h) if p.exists()]} ⇒ "
        f"与回归上游方向相反，处置：删除这些文件并重跑 merge"
        if (dual_src.exists() or elder_cmd_h.exists())
        else "dual_wake_word.cc 与 elder_cmd.h 均不存在（回归上游已落实）",
    )

    # ------------------------------------------------------ 表情动图
    # 帧数据（anim_frames.bin）是**生成物但入库**，索引头（anim_emoji_index.h）
    # 和它必须同步。改了 tools/anim_emoji_pack.py 的 --frames / --size 之后
    # 只重跑了一半，就会得到「bin 比索引头小」⇒ 运行时 dsc 指到越界地址，
    # 表现是随机花屏（读 PSRAM/flash 映射区的脏数据），极难归因。
    anim_dir = ROOT / "firmware" / "xiaozhi" / "main" / "display" / "lvgl_display" / "anim"
    anim_bin = anim_dir / "anim_frames.bin"
    anim_hdr = anim_dir / "anim_emoji_index.h"
    if anim_bin.exists() and anim_hdr.exists():
        hdr_text = anim_hdr.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"kAnimEmojiBlobSize\s*=\s*(\d+)", hdr_text)
        declared = int(m.group(1)) if m else -1
        actual = anim_bin.stat().st_size
        check(
            "C8 动图帧数据与索引头同步",
            declared == actual,
            f"索引头声明 kAnimEmojiBlobSize={declared}，实际 bin={actual} 字节。"
            f"改过 tools/anim_emoji_pack.py 的参数就重跑一遍生成"
            if declared != actual else f"bin {actual} 字节与索引头一致",
            fatal=True,
        )
        # 帧偏移必须首尾相接且落在 bin 内，否则中间有空洞/重叠
        ok_layout = True
        detail = ""
        entries = re.findall(
            r'\{"([^"]+)",\s*(\d+)u,\s*(\d+)u,\s*(\d+)u,\s*(\d+)u,\s*(\d+)u\}', hdr_text)
        expect_off = 0
        total_frames = 0
        for name, off, nfr, fbytes, _ms_off, _side in entries:
            off, nfr, fbytes = int(off), int(nfr), int(fbytes)
            total_frames += nfr
            if off != expect_off:
                ok_layout = False
                detail = f"{name} 的 offset={off}，按前一条推算应为 {expect_off}"
                break
            if off + nfr * fbytes > actual:
                ok_layout = False
                detail = (f"{name} 末尾 {off + nfr * fbytes} 超出 bin 长度 {actual}")
                break
            expect_off = off + nfr * fbytes
        if ok_layout and entries and expect_off != actual:
            ok_layout = False
            detail = f"所有动画合计 {expect_off} 字节，与 bin {actual} 字节不等（尾部有残留）"
        check(
            "C8b 动图索引的偏移首尾相接且不越界",
            ok_layout and len(entries) > 0,
            detail or f"{len(entries)} 个动画的偏移连续，合计 {expect_off} 字节",
            fatal=True,
        )
        # 每帧一个时长，少一条运行时就会退化到 40ms 兜底 ⇒ 整段动画快慢错乱。
        # 这条在只有一张动画时最容易漏：改帧数只重跑一半、时长表还是旧的。
        m = re.search(r"kAnimEmojiFrameMsCount\s*=\s*sizeof\(kAnimEmojiFrameMs\)"
                      r"\s*/\s*sizeof\(kAnimEmojiFrameMs\[0\]\)", hdr_text)
        ms_vals = re.findall(r"(\d+)u,", hdr_text.split("kAnimEmojiFrameMs[]")[-1]
                             .split("};")[0]) if "kAnimEmojiFrameMs[]" in hdr_text else []
        check(
            "C8c 动图时长表条数等于总帧数",
            m is not None and len(ms_vals) == total_frames and total_frames > 0,
            f"时长表 {len(ms_vals)} 条，帧数合计 {total_frames} —— 不等就说明索引头"
            f"是半新半旧，重跑 tools/anim_emoji_pack.py 生成",
            fatal=True,
        )

    # --------------------------------- C10 elder 段是否被"手改生成文件"骗过
    # 2026-10-23 真事故（DS1302 接入时暴露，代价是一次假绿 CI）：
    # `firmware/xiaozhi/sdkconfig.defaults` 是**生成文件** —— 它的 elder 段
    # 每次都由 merge_to_xiaozhi.py 用 SDKCONFIG_OVERLAY 常量整块**覆盖重写**
    # （append_overlay() 先 strip_overlay() 再拼模板）。
    # 手改那个文件在本机看着生效（sdkconfig 早就生成过、C1 也不比），
    # 一到 CI 就被覆盖回模板里的旧值 ⇒ 固件里 CONFIG_ELDER_RTC_ENABLE=n，
    # `#if CONFIG_ELDER_RTC_ENABLE && defined(...DS1302)` 整块被预处理跳过，
    # 而**构建一路绿灯**（剩下的空 TU 也能链过，四个符号由 rtc.c 的桩提供）。
    # 判据：模板拼出来的整段必须与 defaults 里的 BEGIN..END 段逐字节相同。
    try:
        if str(ROOT / "tools") not in sys.path:
            sys.path.insert(0, str(ROOT / "tools"))
        import merge_to_xiaozhi as _m2x  # noqa: E402  （只在 __main__ 里跑 main）

        _text = SDK_DEFAULTS.read_text(encoding="utf-8", errors="replace")
        _i = _text.find(_m2x.BEGIN)
        _j = _text.find(_m2x.END)
        _cur = _text[_i:_j + len(_m2x.END)] if (_i >= 0 and _j > _i) else ""
        _same = _cur.strip("\n") == _m2x.SDKCONFIG_OVERLAY.strip("\n")
        check(
            "C10 sdkconfig.defaults 的 elder 段与 merge 模板一致",
            _same,
            "一致：生成文件没被手改，模板也没漂移"
            if _same else
            "**不一致**！`firmware/xiaozhi/sdkconfig.defaults` 的 elder 段由 "
            "tools/merge_to_xiaozhi.py 的 SDKCONFIG_OVERLAY **整块重写**，"
            "手改那个文件等于没改：CI 上会被覆盖回模板值，对应功能静默编空"
            "（构建还照样绿）。\n"
            "         处置：改 tools/merge_to_xiaozhi.py 的 SDKCONFIG_OVERLAY，"
            "再跑 python tools/merge_to_xiaozhi.py 重新生成",
            fatal=True,
        )
    except Exception as exc:  # noqa: BLE001
        check("C10 sdkconfig.defaults 的 elder 段与 merge 模板一致", False,
              f"无法比对（导入 merge_to_xiaozhi 失败：{exc}）", fatal=True)

    # --------------------------------- C11 时钟/彩灯引脚不得碰音频 I2C 总线
    # 用户的硬约束是「声音现在正常，千万不要再修的破音、卡顿」。
    # DS1302 是三线 bit-bang，若 CLK/DAT 落在 GPIO1/GPIO2（板载音频 codec
    # ES8311/ES7210 与触摸 FT5x06 共用的 I2C 总线，SDA=1/SCL=2）就会与触摸
    # 轮询持续抢总线 ⇒ codec 寄存器被写坏，听感就是破音/卡顿/断续。
    # rtc_ds1302.c 的 pins_guard() 运行时也会拒，但那时固件已经烧进设备了；
    # 这里在**编译前**就拦，本机与 CI 都跑。
    if src.get("CONFIG_ELDER_RTC_ENABLE", "n") != "y":
        check("C11 时钟/彩灯引脚不与音频 I2C 总线冲突", SKIP,
              "CONFIG_ELDER_RTC_ENABLE=n，未接外部时钟，本项无判据可查")
    else:
        _drv = ("DS1302" if src.get("CONFIG_ELDER_RTC_DRIVER_DS1302", "") == "y"
                else "DS3231" if src.get("CONFIG_ELDER_RTC_DRIVER_DS3231", "") == "y"
                else "(未选)")
        _allow = src.get("CONFIG_ELDER_RTC_ALLOW_SHARED_I2C_BUS", "n") == "y"
        _bad: list[str] = []
        if _drv == "DS1302":
            _pins = {k: src.get(f"CONFIG_ELDER_RTC_{k}", "(未设置)")
                     for k in ("CLK_GPIO", "DAT_GPIO", "RST_GPIO")}
            if not _allow:
                for _k, _v in _pins.items():
                    if _v in ("1", "2"):
                        _bad.append(f"{_k}={_v} 落在音频/触摸 I2C 总线"
                                    f"（SDA=GPIO1, SCL=GPIO2）")
            _key = src.get("CONFIG_ELDER_KEY_GPIO", "")
            if _key and _pins.get("RST_GPIO") == _key:
                _bad.append(f"RST_GPIO 与 ELDER_KEY_GPIO 同为 GPIO{_key}"
                            f"（CE 输出会把服药确认键一直拉高）")
            _detail = (f"芯片 {_drv}，CLK={_pins['CLK_GPIO']} "
                       f"DAT={_pins['DAT_GPIO']} RST={_pins['RST_GPIO']}"
                       + ("（已显式允许共用 I2C 总线）" if _allow else ""))
        else:
            _detail = f"芯片 {_drv}（走 I2C，不占用额外 bit-bang 引脚）"
        _rgb = src.get("CONFIG_ELDER_ENABLE_RGB", "n") == "y"
        _rgbp = src.get("CONFIG_ELDER_RGB_GPIO", "(未设置)")
        if _rgb and _rgbp in ("1", "2"):
            _bad.append(f"RGB_GPIO={_rgbp} 落在音频/触摸 I2C 总线")
        if _rgb:
            _detail += f"；WS2812 已启用，DIN=GPIO{_rgbp}"
        check(
            "C11 时钟/彩灯引脚不与音频 I2C 总线冲突",
            not _bad,
            ("⚠️ " + "；".join(_bad) + "。GPIO1/GPIO2 是板载音频 codec"
             "(ES8311/ES7210) 与触摸屏(FT5x06) 共用的 I2C 总线，软件 bit-bang "
             "会与触摸轮询抢总线，听感就是破音/卡顿。请改线到 GPIO4/5/6。")
            if _bad else _detail,
            fatal=True,
        )

    # ------------------- C12 DS1302 突发字节顺序（地址序，不是中文语序）
    # 2026-10-26 复核 rtc_ds1302.c 时发现真错位：写侧写成
    #   b[3]=星期 / b[4]=日 / b[5]=月
    # 而 DS1302 的突发顺序是**地址序** 0x86 日(几号) → 0x88 月 → 0x8A 星期。
    # 这个错**回读自检发现不了**：读写用同一套错索引，彼此自洽、BCD 合法、
    # 范围也合法；只有芯片自己的日历进位会坏（午夜递增的是星期寄存器，
    # 几号永远不动 ⇒ 掉电几天后开机，日期差好几天）。
    # 参考实现：esp-idf-lib components/ds1302/ds1302.c 的
    # ds1302_set_time / ds1302_get_time。
    # 为什么放在这里：固件没有 C 单元测试框架，prebuild_check 是**本机与 CI
    # 共有的唯一编译前钩子**，静态扫源码是成本最低的回归护栏。
    _ds = ROOT / "firmware" / "components" / "elder" / "src" / "rtc_ds1302.c"
    if not _ds.is_file():
        check("C12 DS1302 突发字节顺序符合数据手册", SKIP,
              "rtc_ds1302.c 不存在，本项无判据可查")
    else:
        _src = _ds.read_text(encoding="utf-8", errors="replace")
        _bad: list[str] = []
        for _i, _field in ((3, "tm_mday"), (4, "tm_mon"),
                           (5, "tm_wday"), (6, "tm_year")):
            if not re.search(rf"^\s*b\[{_i}\]\s*=\s*[^;]*{_field}", _src, re.M):
                _bad.append(f"写侧 b[{_i}] 没有赋 {_field}")
        if not re.search(r"mday\s*=\s*bcd2bin\(b\[3\]", _src):
            _bad.append("读侧没有从 b[3] 取 mday")
        if not re.search(r"mon\s*=\s*bcd2bin\(b\[4\]", _src):
            _bad.append("读侧没有从 b[4] 取 mon")
        check(
            "C12 DS1302 突发字节顺序符合数据手册",
            not _bad,
            ("⚠️ " + "；".join(_bad) + "。突发顺序是地址序："
             "秒(0x80)/分(0x82)/时(0x84)/日-几号(0x86)/月(0x88)/星期(0x8A)/"
             "年(0x8C)/控制(0x8E)，日-几号在月**之前**、星期在年之前。"
             "写成「秒分时日 月 星期 年」会把日/月/星期整体错位，"
             "且回读自检查不出来。参考 esp-idf-lib ds1302.c。")
            if _bad else
            "写侧 b[3..6] = 日/月/星期/年，读侧从 b[3]/b[4] 取日/月 —— 与地址序一致",
            fatal=True,
        )

    # --------- C13 提醒表定长数组只许待在 typedef struct 里，不许压任务栈
    # 2026-10-27 真机事故：elder_reminder_replace_all() 与 do_sync() 里有
    # 7 份 ELDER_REMINDER_MAX 大小的局部数组，合计约 4.2KB，而 elder_sync
    # 任务栈只有 6KB。后端下发 6 条提醒时正好压穿：
    #     I elder.reminder: 提醒表已合并：后端 6 条 + 本地 0 条 = 6 条
    #     ***ERROR*** A stack overflow in task elder_sync has been detected.
    # 后果是**每轮同步（5 分钟）都重启**，设备永远停在开机阶段、提醒全失效。
    # 之所以要在编译前拦：这种溢出与"表里几条数据"强相关 —— 3 条时完全正常，
    # 6 条时崩，编译、链接、单测、甚至短时间试机都发现不了。
    # 修法是把快照区搬到堆（rmd_merge_scratch_t / sync_rmd_buf_t），
    # 本项保证没人再把它们改回栈上数组。
    # 局限：只认单行声明；写成多行会漏 —— 但漏了也不比现在更差。
    _c13_files = [
        ROOT / "firmware" / "components" / "elder" / "src" / "reminder.c",
        ROOT / "firmware" / "components" / "elder" / "src" / "sync.c",
    ]
    _c13_arr = re.compile(
        r"^\s*[A-Za-z_][A-Za-z0-9_ \t\*]*\b[A-Za-z_][A-Za-z0-9_]*"
        r"\s*\[\s*ELDER_REMINDER_MAX\s*\]\s*;")
    _c13_bad: list[str] = []
    _c13_scanned = 0
    for _f in _c13_files:
        if not _f.is_file():
            continue
        _c13_scanned += 1
        _in_typedef = False
        for _ln, _line in enumerate(
                _f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            # 只要进了**任何** struct 体就算"不是栈上"：文件级 `static struct {...} g;`
            # 与 `typedef struct {...} x_t;` 都是静态存储，都安全。
            # （第一版只认 `typedef struct`，把 reminder.c 里 `static struct {...} g;`
            #  的 4 个成员数组误判成栈上 —— 门禁的第一版先要自己不误报。）
            if re.search(r"\bstruct\b", _line) and "{" in _line:
                _in_typedef = True
            if _in_typedef and re.match(r"^\}\s*[A-Za-z_]", _line):
                _in_typedef = False
                continue
            if _c13_arr.match(_line) and not _in_typedef:
                _c13_bad.append(f"{_f.name}:{_ln} {_line.strip()}")
    if _c13_scanned == 0:
        check("C13 提醒表定长数组不压任务栈", SKIP,
              "reminder.c / sync.c 都不存在，本项无判据可查")
    else:
        check(
            "C13 提醒表定长数组只在 typedef struct 里（不许压任务栈）",
            not _c13_bad,
            ("⚠️ 这些定长数组出现在了函数栈上：" + "；".join(_c13_bad[:6]) +
             "。ELDER_REMINDER_MAX 条 × 每条几十字节，叠起来会把 8KB 的 "
             "elder_sync 任务栈压穿（真机表现是每轮同步重启一次、提醒全失效）。"
             "请放进 typedef struct 再用 heap_caps_malloc 分配，"
             "参考 reminder.c 的 rmd_merge_scratch_t 与 sync.c 的 sync_rmd_buf_t。")
            if _c13_bad else
            f"{_c13_scanned} 个文件里的提醒表数组都在 typedef struct 内（堆上）",
            fatal=True,
        )

    # --------- C14 分区表布局自洽（2026-10-29 NVS 扩容 0x4000 → 0x10000）
    # 分区表是"改错一个数字就整机变砖"的文件，而 IDF 自己的编译期检查只保证
    # **不重叠、不越界**，它不保证：
    #   ① nvs 的起始偏移没被挪 —— 挪了照样编译通过，但存量设备升级后 NVS
    #      里的提醒表 / WiFi 凭据 / 启动计数全部读不出来（起点变了 = 换了一块
    #      空白区）。老人设备要重新配网、提醒表靠服务端重下发，现场极难解释。
    #   ② nvs 没被改小 —— evtq 一条 blob 就 2312 字节 ≈ 90 个 entry，
    #      16KB 分区（504 entry、实测可用 53）根本写不进去，而 nvs_set_blob
    #      失败是**静默**的，离线回执会悄悄丢。
    #   ③ ota_0 / assets 的偏移没动 —— 一动，所有存量设备都必须整机重刷
    #      （app 与 8MB 形象包都错位）。
    # 这三条都是"编译全绿、上机才发现"的形态，所以必须在这里拦。
    _c14_csv = ROOT / "firmware" / "xiaozhi" / "partitions" / "v2" / "16m.csv"
    _c14_bad: list[str] = []
    _c14_info = ""
    if not _c14_csv.is_file():
        _c14_bad.append("缺 partitions/v2/16m.csv")
    else:
        def _num(s: str) -> int:
            s = s.strip()
            if s.upper().endswith("M"):
                return int(s[:-1]) * 1024 * 1024
            if s.upper().endswith("K"):
                return int(s[:-1]) * 1024
            return int(s, 0)

        _parts: list[tuple[str, int | None, int]] = []
        for _ln, _line in enumerate(
                _c14_csv.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            _line = _line.split("#", 1)[0].strip()
            if not _line:
                continue
            _cols = [c.strip() for c in _line.split(",")]
            if len(_cols) < 5:
                _c14_bad.append(f"第 {_ln} 行列数不足 5")
                continue
            try:
                _off = _num(_cols[3]) if _cols[3] else None
                _sz = _num(_cols[4])
            except ValueError:
                _c14_bad.append(f"第 {_ln} 行 offset/size 不是数字")
                continue
            _parts.append((_cols[0], _off, _sz))

        # offset 留空 = 紧跟上一个分区（ota_1 就是这么写的）
        _resolved: list[tuple[str, int, int]] = []
        _cursor = 0
        for _name, _off, _sz in _parts:
            _real = _cursor if _off is None else _off
            _resolved.append((_name, _real, _sz))
            _cursor = _real + _sz

        for _i, (_name, _off, _sz) in enumerate(_resolved):
            if _off % 0x1000:
                _c14_bad.append(f"{_name} offset 0x{_off:x} 未按 4KB 对齐")
            if _sz <= 0 or _sz % 0x1000:
                _c14_bad.append(f"{_name} size 0x{_sz:x} 未按 4KB 对齐")
            if _i + 1 < len(_resolved):
                _nn, _no, _ = _resolved[_i + 1]
                if _off + _sz > _no:
                    _c14_bad.append(
                        f"{_name}(0x{_off:x}+0x{_sz:x}) 压到 {_nn}(0x{_no:x})")

        _end = (_resolved[-1][1] + _resolved[-1][2]) if _resolved else 0
        if _end > 16 * 1024 * 1024:
            _c14_bad.append(f"末尾 0x{_end:x} 超出 16MB flash")

        _by = {n: (o, s) for n, o, s in _resolved}
        if "nvs" not in _by:
            _c14_bad.append("没有 nvs 分区")
        else:
            _no, _ns = _by["nvs"]
            if _no != 0x9000:
                _c14_bad.append(
                    f"nvs 起点被挪到 0x{_no:x}（必须 0x9000）—— "
                    "挪起点 = 存量设备的提醒表/WiFi 凭据全部作废")
            if _ns < 0x10000:
                _c14_bad.append(
                    f"nvs 只有 0x{_ns:x}（2026-10-29 已扩到 0x10000）—— "
                    "evtq 一条 blob 约 90 entry，缩回去会静默写不进")
        if _by.get("ota_0", (0, 0))[0] != 0x20000:
            _c14_bad.append("ota_0 起点不是 0x20000（app 偏移一变必须整机重刷）")
        if _by.get("assets", (0, 0))[0] != 0x800000:
            _c14_bad.append("assets 起点不是 0x800000")

        _c14_info = (f"{len(_resolved)} 个分区，nvs "
                     f"0x{_by.get('nvs', (0, 0))[0]:x}+0x{_by.get('nvs', (0, 0))[1]:x}"
                     f"，末尾 0x{_end:x}")

    check("C14 分区表布局自洽（nvs 起点/大小、app 与 assets 偏移不许变）",
          not _c14_bad,
          ("⚠️ " + "；".join(_c14_bad[:5])) if _c14_bad else _c14_info,
          fatal=True)

    # --------- C15 提醒回执的 due_at 必须指向「本次实例」（R2）
    # 2026-10-31 用户实报：8 点的服药提醒，20:49 子女端在 APP 上点了代确认，
    # 设备红灯一直闪。库里同一件事裂成三行：
    #     id=107/108 due_at=08:27:11   ← 真正触发过的那两次，永远等不到确认
    #     id=109     due_at=20:58:58   ← due_at 与 confirmed_at 逐秒相同
    #     id=110     due_at=20:59:09
    # 服务端按 (reminder_id, due_at) 定位实例（device_api.py 找不到就 db.add
    # 新建），due_at 一旦变成"上报那一刻"就等于换了一个实例 ⇒ 原实例永远不
    # 确认 + 凭空多一条假记录，依从率把一次服药算成三次。
    # 两个口子，缺一不可：
    #   ① reminder.c 的 replace_all 用 memset 整表重建，却只继承 state /
    #      fired_today / confirm_deadline_us —— fired_due_at 跟着被清零。
    #      于是**一次普通心跳同步**就能把"8 点那一次"打成 0。
    #   ② sync.c 的 on_reminder_state 拿 0 兜底成 time(NULL)。
    # 都是"编译全绿、单测全绿，只有真实数据量 + 真实跨天才会暴露"的形态，
    # 所以必须在编译前按代码位拦住。
    _c15_rmd = ROOT / "firmware" / "components" / "elder" / "src" / "reminder.c"
    _c15_sync = ROOT / "firmware" / "components" / "elder" / "src" / "sync.c"

    def _strip_comments(s: str) -> str:
        """把注释换成等长空白（保留换行与偏移）。

        为什么必须先剥注释：本项的断言是"某处不许出现 time(NULL)"，而
        **解释为什么不许出现的那段注释里就写着 time(NULL)** —— 不剥注释
        等于让防回归注释自己踩红（这是本仓库反复踩过的坑）。
        字符串字面量要整体跳过，否则 `"//"` 会被当成行注释起点。
        """
        out: list[str] = []
        i, n = 0, len(s)
        while i < n:
            c = s[i]
            if c == "/" and i + 1 < n and s[i + 1] == "*":
                j = s.find("*/", i + 2)
                j = n if j < 0 else j + 2
                out.append(re.sub(r"[^\n]", " ", s[i:j]))
                i = j
            elif c == "/" and i + 1 < n and s[i + 1] == "/":
                j = s.find("\n", i)
                j = n if j < 0 else j
                out.append(" " * (j - i))
                i = j
            elif c in "\"'":
                q, j = c, i + 1
                while j < n:
                    if s[j] == "\\":
                        j += 2
                        continue
                    if s[j] == q:
                        j += 1
                        break
                    j += 1
                out.append(s[i:j])
                i = j
            else:
                out.append(c)
                i += 1
        return "".join(out)

    def _func_body(code: str, name: str) -> str | None:
        """按花括号配平切出函数体（含外层大括号）。取不到返回 None。

        为什么不用"整文件子串匹配"：`fired_due_at = old_due[` 这种判据一旦
        放宽到全文件，别处（例如某次实验代码、某个无关函数）出现一次就能
        让断言假绿。要防的是"这一条继承没写"，不是"文件里提过这个名字"。
        """
        m = re.search(r"\b" + re.escape(name) + r"\s*\(", code)
        if not m:
            return None
        i = code.find("{", m.end())
        if i < 0:
            return None
        depth = 0
        for j in range(i, len(code)):
            if code[j] == "{":
                depth += 1
            elif code[j] == "}":
                depth -= 1
                if depth == 0:
                    return code[i:j + 1]
        return None

    if not _c15_rmd.is_file() or not _c15_sync.is_file():
        check("C15 提醒回执 due_at 指向本次实例", SKIP,
              "reminder.c / sync.c 不存在，本项无判据可查")
    else:
        _rmd_code = _strip_comments(
            _c15_rmd.read_text(encoding="utf-8", errors="replace"))
        _sync_code = _strip_comments(
            _c15_sync.read_text(encoding="utf-8", errors="replace"))
        _c15_bad: list[str] = []

        _ra = _func_body(_rmd_code, "elder_reminder_replace_all")
        if _ra is None:
            _c15_bad.append("取不到 elder_reminder_replace_all() 的函数体")
        else:
            _m_wipe = re.search(r"memset\s*\(\s*g\s*\.\s*table\s*\.\s*items", _ra)
            _m_snap = re.search(
                r"old_due\s*\[[^\]]*\]\s*=\s*g\s*\.\s*table\s*\.\s*items\s*"
                r"\[[^\]]*\]\s*\.\s*fired_due_at", _ra)
            _m_inherit = re.search(r"fired_due_at\s*=\s*old_due\s*\[", _ra)
            if _m_wipe is None:
                # 整表重建的写法变了，判据本身失效 —— 显式报出来让人复核，
                # 绝不"取不到就 SKIP"（那等于让断言永远不执行）。
                _c15_bad.append(
                    "replace_all 里找不到整表重建（memset(g.table.items)）—— "
                    "实现已变，本判据失效，请人工复核运行态继承")
            else:
                if _m_snap is None:
                    _c15_bad.append(
                        "replace_all 没有快照 fired_due_at（old_due[i] = "
                        "g.table.items[i].fired_due_at）")
                elif _m_snap.start() > _m_wipe.start():
                    _c15_bad.append(
                        "fired_due_at 的快照写在 memset 之后 —— 快照到的是空表")
                if _m_inherit is None:
                    _c15_bad.append(
                        "replace_all 的运行态继承里没有 fired_due_at"
                        "（一次心跳同步就把本次实例的到期时刻清零）")

        _on = _func_body(_sync_code, "elder_sync_on_reminder_state")
        if _on is None:
            _c15_bad.append("取不到 elder_sync_on_reminder_state() 的函数体")
        else:
            _n_now = len(re.findall(r"\btime\s*\(\s*NULL\s*\)", _on))
            if _n_now:
                _c15_bad.append(
                    f"on_reminder_state 里仍有 {_n_now} 处 time(NULL) 兜底 "
                    "—— 回执会被挂到上报那一刻，服务端据此新建假实例")
            if "due_of_item(" not in _on:
                _c15_bad.append(
                    "on_reminder_state 没走 due_of_item() 取到期时刻")

        _doi = _func_body(_sync_code, "due_of_item")
        if _doi is None:
            _c15_bad.append("缺 due_of_item()：due_at 的兜底没有单点实现")
        else:
            if "tm_hour" not in _doi or "tm_min" not in _doi:
                _c15_bad.append(
                    "due_of_item() 的兜底没用条目自己的 hour/minute 拼计划时刻")
            if "mktime" not in _doi:
                _c15_bad.append(
                    "due_of_item() 没用 mktime 把计划时刻转成绝对时间")

        check("C15 提醒回执的 due_at 必须指向「本次实例」（R2）",
              not _c15_bad,
              ("⚠️ " + "；".join(_c15_bad[:5]) +
               "。服务端按 (reminder_id, due_at) 定位实例，due_at 一变就等于"
               "换了一条记录：原实例永远等不到确认（红灯一直闪），同时凭空"
               "多出一条 due_at == confirmed_at 的假记录，依从率把一次服药"
               "算成三次。") if _c15_bad else
              "replace_all 在整表重建前快照并继承了 fired_due_at；"
              "sync 层用 due_of_item() 按计划时刻兜底，无 time(NULL)",
              fatal=True)

    # ------------------------------------------------------ 汇总
    failed = [r for r in results if not r[1] and r[3]]
    warned = [r for r in results if not r[1] and not r[3]]
    passed = len(results) - len(failed) - len(warned) - len(skipped)
    print("\n" + "=" * 68)
    print(f"共 {len(results)} 条：通过 {passed}，失败 {len(failed)}，"
          f"警告 {len(warned)}，跳过 {len(skipped)}")
    for n, _ok, _d, _f in failed:
        print(f"  ✗ {n}")
    for n, _ok, _d, _f in warned:
        print(f"  ! {n}")
    print("=" * 68)

    if failed:
        print("\n**先修掉上面的失败项再编译** —— 带着过期配置编译出来的固件，"
              "\n症状是「改了代码但设备行为没变」，比不编译更难排查。")
        return 1
    if warned and args.strict:
        return 1
    if warned:
        print("\n有警告项，已放行（--strict 会拦）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())