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
        lines = [ln for ln in detail.splitlines() if not ln.strip().startswith("⚠️")]
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

    wake = src.get("CONFIG_CUSTOM_WAKE_WORD", "")
    display = src.get("CONFIG_CUSTOM_WAKE_WORD_DISPLAY", "")
    # 判据只认**拼音词根**：汉字里没有拉丁串，
    # 不能拿拉丁串去 display 里找（第一版就这么写的，必然假红）。
    # 正确做法是查两件事各自是否**正确**，而不是要求二者「互相包含」——
    # 拼音↔汉字一致是 Kconfig/编码层面的事实，这里只能验「是当前值」。
    #
    # 2026-10-20：产品未发布，唤醒词一次定死为「你好小安」（两个引擎都认真个词）。
    ok_pinyin = wake.replace(" ", "") == "nihaoxiaoan"
    ok_display = display.replace("，", "").replace(",", "").replace(" ", "") == "你好小安"
    check(
        "C2b 唤醒词是「你好，小安」（拼音与汉字都必须是当前值）",
        ok_pinyin and ok_display,
        f"{where}: 拼音={wake!r} 屏显={display!r}"
        + ("" if ok_pinyin and ok_display
           else "。⚠️ 与期望值不符 —— 最常见原因是 sdkconfig 残留"
                "（kconfig 以已存在的 sdkconfig 为准，会静默覆盖 defaults）"),
    )

    # WakeNet 专用模型：唤醒词训练在模型里，选错模型 = 设备应答别的名字，
    # 而真机上只表现为「喊它没反应 / 应答陌生称呼」，必须在这里钉死具体符号。
    wn_ok = src.get("CONFIG_SR_WN_WN9_NIHAOXIAOAN_TTS2", "n") == "y"
    check(
        "C2b2 WakeNet 唤醒模型是「你好小安」（wn9_nihaoxiaoan_tts2）",
        wn_ok,
        f"{where} 里 CONFIG_SR_WN_WN9_NIHAOXIAOAN_TTS2="
        f"{src.get('CONFIG_SR_WN_WN9_NIHAOXIAOAN_TTS2', '(未设置)')!r}；"
        f"没开它 ⇒ 只剩 MultiNet 唤醒，召回明显变差",
    )

    # 反向守卫：别的 wn9 模型被同时打开会抢唤醒词（AfeWakeWord 只认最后
    # 匹配到的 wn 模型）——「改了没生效」的另一种形态。
    other_wn = [
        k for k in src
        if k.startswith("CONFIG_SR_WN_WN9") and k != "CONFIG_SR_WN_WN9_NIHAOXIAOAN_TTS2"
        and src[k] == "y"
    ]
    check(
        "C2b3 没有别的 wn9 唤醒模型同时启用",
        not other_wn,
        f"{where} 里同时启用了 {other_wn} ⇒ 唤醒词可能被别的模型顶掉"
        if other_wn else "只有 wn9_nihaoxiaoan_tts2 一个 wn9 模型",
    )

    # 反向守卫之二：**defaults 全家族**（sdkconfig.defaults / .esp32s3 / .esp32p4 …）
    #
    # 为什么不并进 C2b3：判据对象不同。C2b3 查的是 sdkconfig ——「此刻编译真正
    # 读的那份」；这条查的是 defaults 家族 ——「下次重新生成时会不会又退回去」。
    #
    # 为什么必须扫**全部变体**，而不是只看 sdkconfig.defaults：
    #   IDF 在 `idf.py set-target <target>` 时会把 sdkconfig.defaults 与
    #   sdkconfig.defaults.<target> **叠加**读取（后者覆盖前者）。但两者设的是
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
            if m and m.group(1) != "CONFIG_SR_WN_WN9_NIHAOXIAOAN_TTS2":
                wn_bad.append(f"{dpath.name}:{lineno} {m.group(1)}")
    check(
        "C2b4 defaults 全家族里没有别的 wn9 唤醒模型",
        not wn_bad,
        f"这些位置还开着别的 wn9 唤醒模型：{wn_bad}。"
        f"⚠️ set-target 会把 sdkconfig.defaults 与 sdkconfig.defaults.<target> "
        f"叠加读取，两者设的是不同符号 ⇒ 不会互相覆盖，只会两个都 y。"
        f"处置：把该行删掉（唤醒模型唯一真源 = sdkconfig.defaults 的 overlay 段）"
        if wn_bad else
        f"扫了 {len(dflt_files)} 个 defaults 变体"
        f"（{', '.join(p.name for p in dflt_files)}），"
        f"只有 wn9_nihaoxiaoan_tts2 一个 wn9 模型",
    )

    check(
        "C2c 离线唤醒模型已选（mn7_cn）",
        src.get("CONFIG_SR_MN_CN_MULTINET7_QUANT", "n") == "y",
        f"{where} 里 CONFIG_SR_MN_CN_MULTINET7_QUANT="
        f"{src.get('CONFIG_SR_MN_CN_MULTINET7_QUANT', '(未设置)')!r}；"
        f"没开它则 srmodels.bin 不打包 ⇒ 离线唤醒与命令词双双失效",
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
    # config_report.c 里有两处**硬编码的期望值**（唤醒词屏显、目标芯片），
    # 设备开机时逐条比对，不符就 ESP_LOGE。
    # ⇒ 这里必须在编译期把它们与 sdkconfig.defaults 对齐校验：
    # 改了 defaults 的唤醒词却忘了改 C 侧，真机每次开机都会误报 ERROR。
    # 误报比不报更糟——老人现场的日志只有这一段，一直刷 ERROR 会让人
    # 忽略后面真正的报错。
    cfg_report = ROOT / "firmware" / "components" / "elder" / "src" / "config_report.c"
    try:
        csrc = cfg_report.read_text(encoding="utf-8")
        m = re.search(r'#define\s+EXPECTED_WAKE_WORD_DISPLAY\s+"([^"]*)"', csrc)
        expected_display = m.group(1) if m else None
        defaults_display = defaults.get("CONFIG_CUSTOM_WAKE_WORD_DISPLAY", "")
        # defaults 里带引号，read_kv 已剥掉；这里只需去掉标点后比对
        norm = lambda s: s.replace("，", "").replace(",", "").replace(" ", "")
        check(
            "C5 固件开机自检的期望唤醒词与 sdkconfig.defaults 一致",
            expected_display is not None
            and norm(expected_display) == norm(defaults_display),
            f"config_report.c 里 EXPECTED_WAKE_WORD_DISPLAY={expected_display!r}，"
            f"sdkconfig.defaults 里 CONFIG_CUSTOM_WAKE_WORD_DISPLAY="
            f"{defaults_display!r}。不一致时设备每次开机都会 ESP_LOGE 误报。",
            fatal=expected_display is not None,
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