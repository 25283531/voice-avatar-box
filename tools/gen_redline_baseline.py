"""把 catalog.HARD_GUARDRAILS 导出成 evals/redline_baseline.json 的生成器。

================================================================================
==   警  告   请先读完这一段再往下滚动 / 请不要跳过文件头           ==
================================================================================

    跑这个脚本 = **宣布「此刻 catalog.py 里的红线正文就是正确基线」**。

    而一次**有意的安全策略放松**，与一次**被顺手"优化"掉的医疗红线**，
    在 ``git diff`` 里**长得一模一样** —— 都是「某个文件变了」。

    这正是它危险的地方：

        catalog.py 的红线正文被改坏  +  你跑了本脚本
            -> redline_baseline.json 被重新生成成同样坏的内容
            -> redline_matches_baseline 逐字比对 **依然全绿**
            -> **门禁亲手把被削弱的红线盖章为"正确基线"**

    事后没有任何一条断言会变红来告诉你刚才发生了什么。唯一还能救这件事的，
    就是**盖章之前那一刻，人是否真的看过差异**。

    所以本脚本的规矩是：

    1. **跑之前必须先确认 catalog.py 的改动是"刻意的安全策略调整"**，
       而不是笔误、顺手改的、或者别人正在编辑到一半。
    2. **拿不准就不要跑。** 多花十分钟人工逐字核对 catalog.py 的红线正文，
       比事后发现门禁给削弱红线盖了章便宜得多 ——
       后者的代价是「没人再知道基线是什么时候被改坏的」。

    这不是免责声明，是使用前提。用它之前请确认你真的做到了第1 条。

================================================================================
==   四道护栏（都在下面代码里，各自有注释说明为什么这样设计）        ==
================================================================================

    护栏 1  文件头（本段）—— 让人在**动手之前**就知道代价。
    护栏 2  dry-run 打印 diff + **默认交互确认**（``--yes`` 可跳过）。
    护栏 3  工作区处于「正在编辑中」时**拒绝运行**。
    护栏 4  写完立刻跑 eval 自证；红则**自动回滚**并报错。

    设计目标（这一点决定了整套护栏的形态）：

        **不是**阻止人改红线 —— 改红线是完全合理的业务需求，
        家属确认过的红线本来就会迭代。

        **是**让「改红线」这件事**必须伴随一次明确的、有痕迹的确认**，
        从而把「顺手改坏」和「刻意调整」在**证据上区分开**：
        前者会留下一条没人看过的 diff，后者会留下一个有人按过回车的记录。

    因此这里刻意**不**做「检测到红线有变化就拒绝执行」——
    那会把唯一合法的用法（刻意收紧/放松红线后重新生成）也一起禁掉，
    逼着大家去改门禁脚本，那是更坏的结果。

================================================================================
==   用法==
================================================================================

    python tools/gen_redline_baseline.py# 只算不写：打印 diff（默认，最常用）
    python tools/gen_redline_baseline.py --print          # 打印将要写入的完整 JSON
    python tools/gen_redline_baseline.py --check         # CI 用：有漂移则 exit 4
    python tools/gen_redline_baseline.py --write         # 写；**默认要求交互确认**
    python tools/gen_redline_baseline.py --write --yes   # 写；跳过交互确认

    逃生舱（每一个都会在输出里留下显式痕迹，不静默）：
    python tools/gen_redline_baseline.py --write --yes --force-unstable
    python tools/gen_redline_baseline.py --write --yes --allow-hedging

    ⚠️ 护栏 4（写后 eval 自证）**刻意没有开关**。
       留一个「跳过自证」的开关，等于留一个「跳过唯一能发现手滑的检查」的
       开关 —— 那护栏 4 就不存在了。

================================================================================
==   本脚本**不是**门禁，且刻意不参与判定                                ==
================================================================================

它放在 tools/ 而不是 evals/，就是为了让「谁在生成」与「谁在判定」分离：
门禁自己去读 catalog 生成期望值，同源自证立刻就回来了。

    誊抄之后 ``redline_baseline.json`` 就是权威副本，门禁只读它、不读 catalog。

================================================================================
==   哪些字段是「运行期派生的」，哪些是「人工维护的」                    ==
================================================================================

这个划分必须显式，否则会出现两种很糟的情况：

* **生成器覆写人工维护的说明** —— 于是跑一次脚本就丢掉了后人写下的
  「本门禁挡不住什么」。这是**跑脚本本身造成的损害**，比红线被改坏更隐蔽。
* **生成器用脚本内的默认值覆盖人工加严的黑名单** —— 削弱词黑名单是
  「红线不可以长成什么样」的权威声明，跑一次脚本就悄悄放宽了它。

所以口径是：

* **运行期派生**（每次重新生成）：``guardrail_version`` / ``count`` /
  ``head_marker`` / ``foot_marker`` / ``head_declaration`` /
  ``tail_declaration`` / ``redlines``。
* **本脚本内的策略常量**（每次重新生成，但**写前校验下限**）：
  ``hedging_words``。
* **人工维护**（**从现有副本继承，绝不覆盖**）：``_note`` /
  ``_known_limits``。

继承人工字段带来一个好性质：**当 catalog 没有变化时，本脚本是严格的
no-op**（字节级相同）。「跑一下看看」因此永远是安全的。

================================================================================
==   退出码==
================================================================================

    0  正常（dry-run 完成 / 写入并自证通过 / 无漂移）
    1  **写后自证失败**：重新生成与既有基准存在实质冲突。已自动回滚。
    2  工作区处于「正在编辑中」，拒绝运行
    3  交互确认被否决，或需要确认但stdin 不是终端
    4  ``--check`` 发现副本与 catalog 已漂移
    5  用法错误 / 环境异常
"""
from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
BASELINE = SERVER / "evals" / "redline_baseline.json"
CATALOG_PY = SERVER / "app" / "persona" / "catalog.py"
EVAL_CLI = ROOT / "tools" / "eval_persona.py"

if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

# 交互确认必须**手打**这个词。刻意不做成 "y/N"：
# 一个能被一路回车穿过去的确认，对「区分顺手改坏与刻意调整」毫无价值。
CONFIRM_TOKEN = "REGENERATE"

# 稳定性采样的时间窗。两次采样之间内容变了= 有人在并发写这个文件。
# 1.2 秒足够抓住「编辑器保存 / agent 写文件」这种秒级动作，又不至于让人等太久。
STABILITY_WINDOW_SEC = 1.2

EXIT_OK = 0
EXIT_SELF_CHECK_FAILED = 1
EXIT_EDITING_IN_PROGRESS = 2
EXIT_DECLINED = 3
EXIT_CHECK_DRIFT = 4
EXIT_USAGE = 5

# 削弱词黑名单：出现在**红线正文段**里就硬失败。
#
# 为什么黑名单放在黄金副本里而不是 assertions.py
# ----------------------------------------------
# 因为它是「红线**不可以长成什么样**」的权威声明，和红线正文属于同一件事。
# 放 assertions.py 会让人以为「调黑名单」是改门禁强度（于是有人会去调松它，
# 好让某次改动过审），而它其实是改**规范**。放在一起，改动会在同一个
# diff 里露出「你既改了正文又放宽了黑名单」，这正是 review 该看到的。
HEDGING_WORDS = [
    "可酌情",
    "酌情处理",
    "酌情",
    "视情况",
    "看情况",
    "视实际情况",
    "灵活掌握",
    "灵活处理",
    "不一定",
    "不必太当真",
    "不用太当真",
    "除非",
    "如果对方坚持",
    "如果对方要求",
    "大致",
    "大体",
    "一般情况下",
    "尽量给出",
    "从轻",
    "例外",
]

# 黑名单的**下限**。与 evals/redline_baseline.py 的 REQUIRED_HEDGING_WORDS
# 同源，刻意在这里再写一份：不 import 就能在写盘前拦住「黑名单被清空」。
# 那是最省事的削弱门禁方式 —— 一个字红线正文都不用改。
REQUIRED_HEDGING_WORDS_FALLBACK: tuple[str, ...] = (
    "可酌情", "视情况", "不必太当真", "不一定", "除非",
)

# 「有人正在并发写红线资产」的哨兵与变异特征串。
#
# 来源是 tools/qa8_concurrency_probe.py 的 SENTINEL / FORBIDDEN。
# 刻意**不 import 那个模块** —— 它在import 时会启动后台线程。
# 这里只抄一份清单用于**检测**，抄漏的后果是少拦一次（而非误拦）。
CONCURRENCY_SENTINELS: tuple[str, ...] = (
    "# eng7-concurrent-probe",
)
MUTATION_MARKERS: tuple[str, ...] = (
    "可以酌情给结论",
    "一律给出结论、给出判断",
    "酌情给出结论",
    "__mutant_gone",
    "__removed_no_medical_advice",
    "@@@never_match@@@",
    "其实可以酌情给结论",
)

# 编辑器/工具在保存过程中留下的伴生文件。存在即说明有人正在编辑。
SIBLING_TEMP_GLOBS: tuple[str, ...] = (
    "*.orig", "*.rej", "*.bak", "*.swp", "*.swo", "*~", ".#*",
)

# 医疗 / 红线类断言。自证时按名字定位它们的失败，比只看 exit code 更有用 ——
# 「哪一条红了」直接告诉人「这次重新生成动到了什么」。
SAFETY_ASSERTION_NAMES: tuple[str, ...] = (
    "redline_matches_baseline",
    "redline_baseline_is_independent",
    "redline_no_hedging",
    "redline_marker_leads",
    "no_weakening_words",
    "redline_declares_mandatory",
    "compiled_contains_all_hard",
    "hardline_count_is",
    "compiled_count_marker",
    "no_dose",
    "guardrails_no_unknown_hard_key",
)

# 人工维护、必须从现有副本继承的字段。
HUMAN_MAINTAINED_KEYS: tuple[str, ...] = ("_note", "_known_limits")

# 逐字派生的字段（不在人工维护之列）。
DERIVED_SCALAR_KEYS: tuple[str, ...] = (
    "guardrail_version",
    "count",
    "head_marker",
    "foot_marker",
    "head_declaration",
    "tail_declaration",
)

# --------------------------------------------------------------- 运行期导入
#放在 import 之后是因为下面的注释需要先解释清楚「为什么它必须在 tools/」。
from app.persona import catalog, guardrails  # noqa: E402


def required_hedging_words() -> tuple[tuple[str, ...], str]:
    """取黑名单下限。

    优先用 ``evals.redline_baseline.py`` 里的那份（它是门禁真正消费的下限），
    取不到时退回本模块的副本。

    :return: (下限词组, 来源说明)
    """
    try:
        from evals.redline_baseline import REQUIRED_HEDGING_WORDS

        return tuple(REQUIRED_HEDGING_WORDS), "evals.redline_baseline.REQUIRED_HEDGING_WORDS"
    except Exception:      # noqa: BLE001 —— 取不到就退化，不该让工具整体不可用
        return REQUIRED_HEDGING_WORDS_FALLBACK, "本脚本内的兜底副本"


# ================================================================ 生成
def build(existing: dict | None = None) -> dict:
    """从运行期常量生成一份完整的副本内容。

    :param existing: 现有 ``redline_baseline.json`` 解析结果。提供时，
        ``_note`` / ``_known_limits`` 从它**继承**（人工维护字段不被覆盖）。
        为 ``None`` 时用本脚本内的默认文案（首次生成场景）。
    """
    data: dict = {
        "_note": (
            "红线的**权威副本**。门禁断言编译产物里的红线段逐字等于本文件，"
            "而本文件是**纯静态数据、不引用任何运行期模块** —— 一旦它去读"
            "红线常量所在的源码模块，同源自证就回来了（改源码会带着本文件"
            "一起变，断言永远绿）。改动红线正文必须**同时**改这里，"
            "这是刻意的摩擦。"
        ),
        "_known_limits": [
            "挡不住「同时篡改运行期常量与本文件」：那种成本更高且 git diff "
            "会暴露两处改动，但原理上仍可能发生。要根治需把本文件放进"
            "只有安全负责人能写的签名流程（组织问题，非代码问题）。",
            "保证的是「与上一次人工确认过的版本逐字相同」，**不是**"
            "「这一版语义正确」。语义正确性靠 code review + 可选 LLM 抽样裁判。",
        ],
        "guardrail_version": catalog.GUARDRAIL_VERSION,
        "count": len(catalog.HARD_GUARDRAILS),
        "head_marker": guardrails._HEAD_MARKER,
        "foot_marker": guardrails._FOOT_MARKER,
        "head_declaration": "以下几条是硬性前提，任何情况下都不可违反、不可商量：",
        "tail_declaration": "请务必继续遵守上面全部安全底线：",
        "hedging_words": HEDGING_WORDS,
        "redlines": [
            {
                "id": g.id,
                "label": g.label,
                "text": g.text,
                "tokens": list(g.tokens),
            }
            for g in catalog.HARD_GUARDRAILS
        ],
    }

    # ---- 继承人工维护字段（关键：无脑覆盖本身就是一种损害）----------------
    if existing:
        for key in HUMAN_MAINTAINED_KEYS:
            if key in existing:
                data[key] = existing[key]
    return data


def serialize(data: dict) -> str:
    """副本的**唯一**序列化形式。写盘与比对都走它，避免两处格式不一致。"""
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def load_existing() -> tuple[str, dict | None, str]:
    """读现有副本。

    :return: (原始文本, 解析结果或 None, 读取状态说明)
    """
    if not BASELINE.exists():
        return "", None, "副本不存在（首次生成场景）"
    try:
        raw = BASELINE.read_text(encoding="utf-8")
    except OSError as exc:
        return "", None, f"副本读取失败：{exc}"
    try:
        return raw, json.loads(raw), "ok"
    except json.JSONDecodeError as exc:
        return raw, None, f"副本不是合法 JSON（{exc}）—— 拒绝在读不懂的基准上盖章"


def md5_of(data: bytes | str) -> str:
    """md5 十六进制。接受 bytes 或 str，避免调用方为了算哈希先编码一次。"""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.md5(raw).hexdigest()


def atomic_write(path: Path, text: str) -> None:
    """原子写：同目录临时文件 + ``os.replace``。

    为什么不用 ``write_text``：那个写法会先把目标截断再写，
    中途被打断就留下一个**半截的 JSON**—— 而这正是护栏 3 要防的那类污染。
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".redline-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        # 只删自己创建的临时文件，绝不碰目标文件。
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ================================================================ 护栏 3
def _read_text_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def check_editing_in_progress() -> tuple[list[str], list[str]]:
    """检测工作区是否处于「正在编辑中」。

    为什么这一条**不能**简单地判「catalog 与副本不一致就拒绝」
    ------------------------------------------------------
    「catalog 改了、副本还没改」**正是本脚本唯一合法的使用场景**
    （改完红线、准备重新誊抄）。把它当成「有人在编辑」而拒绝，
    等于把唯一正确的用法也一起禁掉。

    真正危险的不是「不一致」，而是**不一致的那一瞬间文件还在被写**。
    所以这里只回答「此刻文件是静止的吗」，判据四条，全部可机械判定：

    1. ``catalog.py`` 能否被**语法解析**。半写到一半的文件必然解析失败 ——
       这条对「编辑中」是最强的信号，且零误报（合法的catalog.py 一定能parse）。
    2. ``catalog.py`` 里是否残留并发哨兵 / 变异特征串。
       有哨兵 = 有一个协程正在往这个文件里写东西（实测会持续几百毫秒到数秒）。
    3. **两次采样之间内容是否变化**。这才是「正在写」的直接证据。
       跨时刻采样在这里是**正确**用法：要下的结论是「变了」，
       而「变了」是可以直接观察到的 —— 与qa_fingerprint 文档里批评的
       「跑两次比对、没变就认为没人在改」正好相反。
    4. 同目录是否存在编辑器伴生临时文件。

    :return: (拒绝理由列表, 仅供提示的观察列表)。拒绝理由非空即须拒绝运行。
    """
    blockers: list[str] = []
    notes: list[str] = []

    # ---- 1. 语法完整性 ----
    if not CATALOG_PY.exists():
        blockers.append(f"找不到 {CATALOG_PY.relative_to(ROOT)}")
    else:
        try:
            ast.parse(CATALOG_PY.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            blockers.append(
                f"catalog.py 语法错误（line {exc.lineno}: {exc.msg}）"
                "—— 文件多半只写了一半。在半成品上盖章 = 把半成品固化成基线。"
            )
        except OSError as exc:
            blockers.append(f"catalog.py 读取失败：{exc}")

    # ---- 2. 并发哨兵 / 变异特征串 ----
    catalog_text = _read_text_or_empty(CATALOG_PY)
    baseline_text = _read_text_or_empty(BASELINE)
    for label, text in (("catalog.py", catalog_text), ("redline_baseline.json", baseline_text)):
        hit_sentinel = [s for s in CONCURRENCY_SENTINELS if s in text]
        hit_marker = [m for m in MUTATION_MARKERS if m in text]
        if hit_sentinel:
            blockers.append(f"{label} 里残留并发写哨兵 {'、'.join(hit_sentinel)}"
                            "—— 有另一个进程正在写这个文件")
        if hit_marker:
            blockers.append(f"{label} 里残留变异特征串 {'、'.join(hit_marker)}"
                            "—— 像是变异脚本留下的污染，绝不能固化")

    # ---- 3. 两次采样的稳定性 ----
    before = {p: _read_text_or_empty(p) for p in (CATALOG_PY, BASELINE)}
    time.sleep(STABILITY_WINDOW_SEC)
    after = {p: _read_text_or_empty(p) for p in (CATALOG_PY, BASELINE)}
    moving = [p.relative_to(ROOT).as_posix() for p in before if before[p] != after[p]]
    if moving:
        blockers.append(
            f"{STABILITY_WINDOW_SEC:g} 秒采样窗口内内容仍在变化：{'、'.join(moving)}"
            "—— 文件正在被写，此刻读到的是中间态"
        )
    else:
        notes.append(f"稳定性采样通过（{STABILITY_WINDOW_SEC:g}s 窗口内 "
                     "catalog.py 与副本均无变化）")

    # ---- 4. 编辑器伴生文件 ----
    leftovers = []
    for pattern in SIBLING_TEMP_GLOBS:
        leftovers.extend(p.name for p in CATALOG_PY.parent.glob(pattern))
    if leftovers:
        blockers.append("catalog.py 同目录存在编辑器伴生文件："
                        + "、".join(sorted(leftovers)[:5])
                        + "—— 有人正在编辑这个文件")

    return blockers, notes


def check_fingerprint_context() -> list[str]:
    """用 tools/qa_fingerprint.py 的分组指纹补充上下文。

    ⚠️ **这一段刻意不做硬拒绝**，理由写在 :func:`check_editing_in_progress`
    的文档字符串里：「半改」正是重新生成的正常前置态。

    它在这里的作用是**披露**：让人知道当前 diff 处在哪个状态，
    以及「门禁」那一组是否也有未记录的改动（那会改变判定的口径）。
    """
    notes: list[str] = []
    try:
        import qa_fingerprint
    except Exception as exc:      # noqa: BLE001 —— 缺工具不该拦住主流程
        return [f"（未能加载 qa_fingerprint，跳过分组上下文：{exc}）"]

    try:
        code, detail = qa_fingerprint.evaluate()
    except Exception as exc:      # noqa: BLE001
        return [f"（qa_fingerprint 自检异常，跳过分组上下文：{exc}）"]

    if "error" in detail:
        notes.append(f"qa_fingerprint：{detail['error']}（不影响本次判定）")
        return notes

    for g in detail.get("groups", []):
        if not g.get("changed_count"):
            continue
        tag = "原子同步组" if g.get("atomic") else "可独立改组"
        notes.append(f"qa_fingerprint：[{g['name']}] {g['state']}"
                     f"（{g['changed_count']}/{g['total']} 变化，{tag}）")
    if code == 2:
        notes.append("qa_fingerprint：存在原子组处于「半改」—— "
                     "这与「catalog 已改、副本待重新誊抄」是同一件事，故不作为拒绝理由")
    return notes


# ================================================================ 写前预检
def check_hedging_integrity(new_data: dict) -> list[str]:
    """校验黑名单没被写空/ 写漏下限。

    这是**写盘前**的最后一道：副本被清空的黑名单会让
    ``redline_no_hedging`` 直接失去判定输入，而那是一个字的红线正文都不用改
    就能做到的削弱。留在这里是因为「跑一次脚本」不该有能力做到这件事。
    """
    problems: list[str] = []
    words = list(new_data.get("hedging_words") or [])
    if not words:
        return ["hedging_words 为空 —— 削弱词黑名单已失效，写入即门禁失效"]
    floor, source = required_hedging_words()
    missing = [w for w in floor if w not in words]
    if missing:
        problems.append(f"hedging_words 缺下限项（来源 {source}）：{'、'.join(missing)}")
    return problems


def check_new_hedging_in_text(old_data: dict | None, new_data: dict) -> list[str]:
    """检测红线正文里**新出现**的削弱词。

    ``redline_no_hedging`` 只会在**写入之后**的 eval 里变红。放到写盘前，
    是为了让「正文被顺手软化」这件事不会哪怕短暂地成为既成事实。

    只报「新出现」的：既有的削弱词是历史遗留，不该让本脚本拒绝运行
    （否则会逼着人绕过护栏，而不是去修那个历史遗留）。
    """
    problems: list[str] = []
    old_words: set[str] = set()
    old_ids: set[str] = set()
    for rl in (old_data or {}).get("redlines") or []:
        old_ids.add(str(rl.get("id")))
        old_words |= set(HEDGING_WORDS) & set(str(rl.get("text") or ""))
    for rl in new_data.get("redlines") or []:
        rid = str(rl.get("id"))
        if rid in old_ids:
            continue                     # 已存在的条目由上面的逻辑放过
        found = [w for w in HEDGING_WORDS if w in str(rl.get("text") or "")]
        if found:
            problems.append(f"新增红线 {rid} 的正文含削弱词：{'、'.join(found)}")
    return problems


def check_structure(new_data: dict) -> list[str]:
    """结构下限：条数、正文非空、tokens 非空。

    与 ``evals/redline_baseline.py`` 的 ``check_baseline_integrity`` 同源，
    在写盘前先跑一遍，让「删掉一条红线」这种手滑不会成为既成事实。
    """
    problems: list[str] = []
    redlines = new_data.get("redlines") or []
    n = len(redlines)
    if n < 5:
        problems.append(f"红线只有 {n} 条（下限 5）—— 少一条就是少一条保护")
    if new_data.get("count") != n:
        problems.append(f"count={new_data.get('count')} 与实际条数 {n} 不一致")
    for rl in redlines:
        rid = str(rl.get("id") or "?")
        if not str(rl.get("text") or "").strip():
            problems.append(f"{rid}: 正文为空")
        if not (rl.get("tokens") or []):
            problems.append(f"{rid}: tokens 为空")
    return problems


# ================================================================ 护栏 2
def _inline_char_diff(old: str, new: str) -> str:
    """字符级内联差异：``删除`` 标为 ``[-...-]``，新增标为 ``{+...+}``。

    为什么不用 unified diff当主输出：红线正文是一整段中文，
    行级 diff 只会整段标红，**看不出到底哪个字变了**。
    而「哪个字变了」正是人判断「这是笔误还是刻意调整」的唯一依据。
    """
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    out: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.append(old[i1:i2])
        elif tag == "delete":
            out.append("[-" + old[i1:i2] + "-]")
        elif tag == "insert":
            out.append("{+" + new[j1:j2] + "+}")
        else:
            out.append("[-" + old[i1:i2] + "-]{+" + new[j1:j2] + "+}")
    return "".join(out)


def _fmt(obj: object) -> str:
    if isinstance(obj, (list, tuple)):
        return "[" + ", ".join(repr(x) for x in obj) + "]"
    return repr(obj)


def render_diff(old_raw: str, old_data: dict | None, new_data: dict) -> tuple[str, int]:
    """生成人可读的差异报告。

    :return: (报告文本, **直接改动门禁期望值**的处数)
        第二个返回值用于确认环节分级：只改 ``_note`` 与改动``redlines``
        需要的心理准备完全不同，不该用同一个提示强度。
    """
    lines: list[str] = []
    critical = 0

    if old_data is None:
        lines.append("现有副本无法解析或不存在 —— 下面列出的是「将写入的完整内容」。")
        lines.append("")
        for rl in new_data.get("redlines") or []:
            lines.append(f"  [{rl.get('id')}] {rl.get('label')}")
            lines.append(f"      {rl.get('text')}")
        return "\n".join(lines), len(new_data.get("redlines") or [])

    # ---- 标量字段 ----
    for key in DERIVED_SCALAR_KEYS:
        o, n = old_data.get(key), new_data.get(key)
        if o != n:
            critical += 1
            lines.append(f"[{key}] 已变化")
            lines.append(f"    旧：{_fmt(o)}")
            lines.append(f"    新：{_fmt(n)}")

    # ---- 黑名单 ----
    o_words = list(old_data.get("hedging_words") or [])
    n_words = list(new_data.get("hedging_words") or [])
    if o_words != n_words:
        critical += 1
        removed = [w for w in o_words if w not in n_words]
        added = [w for w in n_words if w not in o_words]
        lines.append("[hedging_words] 已变化（削弱词黑名单）")
        if removed:
            lines.append(f"    少了：{'、'.join(removed)}"
                         "⚠️ 少一个词 = 少拦一种削弱表达")
        if added:
            lines.append(f"    多了：{'、'.join(added)}")
    else:
        lines.append(f"[hedging_words] 未变（{len(n_words)} 个）")

    # ---- 人工维护字段（继承而来，通常显示「未变」）----
    for key in HUMAN_MAINTAINED_KEYS:
        o, n = old_data.get(key), new_data.get(key)
        state = "未变（人工维护，本次继承保留）" if o == n else "⚠️ 已变化"
        lines.append(f"[{key}] {state}")
        if o != n:
            lines.append(f"    旧：{_fmt(o)}")
            lines.append(f"    新：{_fmt(n)}")

    # ---- 红线逐条 ----
    old_by_id = {str(rl.get("id")): rl for rl in (old_data.get("redlines") or [])}
    new_by_id = {str(rl.get("id")): rl for rl in (new_data.get("redlines") or [])}
    total = len(new_by_id)
    lines.append("")
    lines.append(f"红线逐条对比（{total} 条）：")
    for idx, (rid, new_rl) in enumerate(new_by_id.items(), 1):
        old_rl = old_by_id.get(rid)
        if old_rl is None:
            critical += 1
            lines.append(f"  {idx}. [{rid}] ⚠️ **新增条目**（旧副本里没有）")
            lines.append(f"       label：{new_rl.get('label')}")
            lines.append(f"       text ：{new_rl.get('text')}")
            continue
        changed_fields = [
            f for f in ("label", "text", "tokens")
            if old_rl.get(f) != new_rl.get(f)
        ]
        if not changed_fields:
            lines.append(f"  {idx}. [{rid}] 未变  ({new_rl.get('label')})")
            continue
        critical += 1
        lines.append(f"  {idx}. [{rid}] ⚠️ 已变化  ({new_rl.get('label')})")
        for f in changed_fields:
            if f == "text":
                lines.append("       text 逐字差异：")
                lines.append("         " + _inline_char_diff(
                    str(old_rl.get("text") or ""), str(new_rl.get("text") or "")))
            elif f == "tokens":
                o_t = list(old_rl.get("tokens") or [])
                n_t = list(new_rl.get("tokens") or [])
                lost = [t for t in o_t if t not in n_t]
                gained = [t for t in n_t if t not in o_t]
                lines.append("       tokens 已变化：")
                if lost:
                    lines.append(f"         丢了：{'、'.join(lost)}"
                                 "⚠️ 丢 token = 生产 fail-closed 闸门的判定输入变弱")
                if gained:
                    lines.append(f"         多了：{'、'.join(gained)}")
                if not lost and not gained:
                    lines.append("         用词相同但顺序有变，请人工确认")
            else:
                lines.append(f"       label：{_fmt(old_rl.get(f))} -> {_fmt(new_rl.get(f))}")

    for rid in old_by_id:
        if rid not in new_by_id:
            critical += 1
            lines.append(f"  ⚠️ [{rid}] **条目消失** —— 少一条红线就是少一层保护")

    return "\n".join(lines), critical


# ================================================================ 护栏 4
def run_eval() -> tuple[int, str]:
    """跑一次完整 eval，返回 (exit code, 合并后的输出)。

    为什么用**子进程**而不是直接 import 跑：
    ``catalog`` 一旦在本进程里 import 过就常驻了，之后磁盘上怎么改都读不到新的
    —— 那样自证就变成在验一个内存里的旧快照，**恰好在最需要它的时候失效**。

    输出落临时文件而不是 PIPE：eval 输出不短，走管道有静默阻塞的风险，
    而一个卡住的门禁比一个红门禁更难查。
    """
    if not EVAL_CLI.exists():
        return -1, f"找不到 eval 入口 {EVAL_CLI}"

    fd, out_path = tempfile.mkstemp(prefix="redline-selfcheck-", suffix=".log")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            try:
                proc = subprocess.run(
                    [sys.executable, str(EVAL_CLI)],
                    cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT,
                    timeout=900, check=False,
                )
                code = proc.returncode
            except subprocess.TimeoutExpired:
                return -1, "eval 超时（900s）—— 无法完成自证，按失败处理"
            except OSError as exc:
                return -1, f"eval 无法启动：{exc}"
        try:
            return code, Path(out_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return code, f"（自证输出读取失败：{exc}）"
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def find_safety_failures(eval_output: str) -> list[str]:
    """从 eval 输出里挑出医疗 / 红线类断言的失败行。

    只看 exit code 是不够的：``max_len`` 这类 report 级失败也会让输出变红，
    而它与「红线是否被削弱」毫无关系。全绿/全红之间需要能指认**哪一条**红了。
    """
    hits: list[str] = []
    for line in eval_output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- ["):
            continue
        if any(name in stripped for name in SAFETY_ASSERTION_NAMES):
            hits.append(stripped)
    return hits


def self_check_after_write() -> tuple[bool, str, list[str]]:
    """写盘后的自证。**这是护栏 4，不可关闭。**

    为什么「重新生成之后跑 eval」能当防手滑的闸门
    ----------------------------------------------
    它挡的不是「红线被削弱但断言都还能过」那种情况（那种确实挡不住，
    见 assertions.py 的边界表），而是**重新生成动作本身引入了冲突**：

    * ``catalog.py`` 只改了一半（比如删掉一条红线、或漏改 tokens）——
      重新生成把那个半成品固化成基线，而 ``check_baseline_integrity``
      的「条数下限 5」「tokens 非空」会立刻变红。
    * ``GUARDRAIL_VERSION`` 没跟着升 —— 版本一致性断言会红。
    * 渲染层与正文不再自洽 —— 逐字块比对会红。

    也就是说：**它把「手滑」从「不可见」变成「一条红字」。**
    而在正常情况下（有意的策略调整），重新生成与既有基准自洽，eval 保持绿。

    :return: (是否通过, 说明, 医疗类失败行)
    """
    print("-" * 74)
    print("[护栏 4] 写后自证：立刻跑一次 eval，验证这次重新生成没有引入冲突")
    print("-" * 74)
    code, output = run_eval()
    safety = find_safety_failures(output)

    if code == -1:
        return False, output, safety
    if code == 0:
        return True, output, safety

    detail = (f"eval exit={code}"
              + (f"，其中医疗/红线类断言失败 {len(safety)} 条" if safety else ""))
    return False, f"{detail}\n{output}", safety


# ================================================================ 确认
def ask_confirmation(diff_text: str, critical: int, assume_yes: bool) -> bool:
    """默认交互确认。``--yes`` 可跳过。

    确认门槛按**改动是否触及门禁期望值**分级：
    只改说明文字时提示轻，动了 ``redlines`` / 黑名单 / 版本号时要求手打确认词。
    理由与 :func:`render_diff` 的第二个返回值相同。

    非交互环境（CI、管道、重定向）下**不降级为「默认同意」**——
    那种情况下没人能看见 diff，自动同意等于把护栏 2 关掉。
    """
    if assume_yes:
        print()
        print("⚠️  已用 --yes 跳过交互确认。**这次盖章没有人工确认记录。**")
        print("   请确认 catalog.py 的改动是刻意的安全策略调整，而不是笔误。")
        return True

    if not sys.stdin or not sys.stdin.isatty():
        print()
        print("❌ 需要交互确认，但 stdin 不是终端（CI / 管道 / 重定向）。")
        print("   在无人能看见 diff 的情况下自动同意 = 护栏 2 被关掉，因此拒绝。")
        print("   若你确实确认过，请显式加 --yes（该行为会留在输出里）。")
        return False

    print()
    print("=" * 74)
    print("请先读完上面的差异，再决定是否盖章。")
    if critical:
        print(f"⚠️  本次改动触及 {critical} 处**直接决定门禁期望值**的内容。")
        print("   逐条问自己：这是刻意的安全策略调整，还是笔误 / 顺手改的？")
        print("   两者在 git diff 里长得一模一样 —— 这是本脚本存在的全部理由。")
    else:
        print("ℹ️  本次未触及红线正文 / 黑名单 / 版本号。")
    print(f"确认写入请手打 {CONFIRM_TOKEN}（直接回车 = 放弃）")
    print("=" * 74)
    try:
        answer = input(f"> {CONFIRM_TOKEN}? ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        print("已取消（无输入）。未做任何改动。")
        return False
    if answer != CONFIRM_TOKEN:
        print()
        print(f"未收到 {CONFIRM_TOKEN} —— 判定为放弃。**未做任何改动。**")
        return False
    return True


# ================================================================ 主流程
BANNER = "=" * 74


def _print_banner() -> None:
    print(BANNER)
    print("  红线黄金副本生成器")
    print(BANNER)
    print("  跑这个脚本 = **宣布此刻 catalog.py 里的红线正文就是正确基线**。")
    print("  一次有意的策略放松，与一次被顺手改坏的医疗红线，在 diff 里完全一样。")
    print("  确认过 catalog.py 的改动是**刻意**的安全策略调整，再往下走。")
    print("  拿不准就不要跑：多花十分钟人工核对，比事后发现门禁给削弱红线盖章便宜得多。")
    print(BANNER)
    print()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="生成红线黄金副本（辅助工具，非门禁）。默认 dry-run。",
        epilog="详细前提见本文件顶部的警告段。",
    )
    ap.add_argument("--write", action="store_true",
                    help="写入 redline_baseline.json（默认仍要求交互确认）")
    ap.add_argument("--yes", action="store_true",
                    help="跳过交互确认。**会跳过护栏 2**，其余护栏不受影响")
    ap.add_argument("--print", dest="dump", action="store_true",
                    help="额外打印将要写入的完整 JSON")
    ap.add_argument("--check", action="store_true",
                    help="只检查副本与 catalog 是否漂移，不写；有漂移则 exit 4")
    ap.add_argument("--force-unstable", action="store_true",
                    help="绕过护栏 3（正在编辑中）。会在输出里留下显式记录")
    ap.add_argument("--allow-hedging", action="store_true",
                    help="绕过「红线正文含削弱词」的写前拦截")
    args = ap.parse_args()

    if args.yes and not args.write:
        print("❌ --yes 只在 --write 时有意义（它跳过的是写入前的确认）。")
        print("   若只想看 diff，去掉 --yes 即可 —— 默认就是dry-run。")
        return EXIT_USAGE

    _print_banner()

    # ---------------- 护栏 3：正在编辑中 -> 拒绝运行 ----------------
    blockers, notes = check_editing_in_progress()
    for n in notes:
        print(f"ℹ️  {n}")
    for n in check_fingerprint_context():
        print(f"ℹ️  {n}")
    if blockers and not args.force_unstable:
        print()
        print("❌ 工作区处于「正在编辑中」，**拒绝运行**。")
        for b in blockers:
            print(f"   · {b}")
        print()
        print("   编辑中态下跑本脚本 = 把半成品固化成基线。")
        print("   等对方改完（或确认无人编辑）后重跑。")
        print("   若你就是那个编辑者、且确实要现在生成：加 --force-unstable")
        print("   （该行为会留在输出里，不会静默生效）。")
        return EXIT_EDITING_IN_PROGRESS
    if blockers:
        print()
        print("⚠️  --force-unstable：**本次跳过了「正在编辑中」检测**。")
        for b in blockers:
            print(f"   · 原本会被拒：{b}")
        print("   这条记录会留在输出里。若刚才是并发编辑，生成结果不可信。")

    # ---------------- 读现状 + 生成新内容 ----------------
    old_raw, old_data, read_state = load_existing()
    if old_data is None and BASELINE.exists():
        print()
        print(f"❌ {read_state}")
        print("   拒绝在读不懂的基准上盖章。请先人工修复该文件。")
        return EXIT_USAGE

    print(f"ℹ️  现有副本：{BASELINE.relative_to(ROOT).as_posix()}"
          f"（{read_state}，md5={md5_of(old_raw) if old_raw else 'N/A'}）")

    try:
        new_data = build(old_data)
    except Exception as exc:      # noqa: BLE001
        print()
        print(f"❌ 从 catalog 生成内容失败：{exc}")
        print("   （常见原因：catalog.py 处于半改状态、语法错误或常量被删）")
        return EXIT_USAGE

    new_raw = serialize(new_data)

    # ---------------- 护栏 2：dry-run 打印 diff ----------------
    diff_text, critical = render_diff(old_raw, old_data, new_data)
    changed = old_raw != new_raw
    print()
    print("=" * 74)
    print("【DRY-RUN】以下是「现有副本」与「将写入内容」的差异。**尚未写入任何东西。**")
    print("=" * 74)
    print(diff_text)
    print()
    print("-" * 74)
    if not changed:
        print("✅ 内容完全一致 —— 本次是**严格 no-op**（人工维护字段已继承，字节级相同）。")
    else:
        print(f"⚠️  有差异：{len(old_raw.splitlines())} 行 -> "
              f"{len(new_raw.splitlines())} 行，"
              f"其中 {critical} 处直接决定门禁期望值。")
        print(f"   catalog.py md5 = {md5_of(CATALOG_PY.read_bytes())}")
    print(f"   将写入 md5     = {md5_of(new_raw)}")
    print("-" * 74)

    if args.dump:
        print()
        print("--- 将写入的完整 JSON ---")
        print(new_raw, end="")

    # ---------------- --check：只报漂移 ----------------
    if args.check:
        if changed:
            print()
            print("❌ --check：副本与 catalog 已漂移。")
            print("   若漂移是有意的，请重跑本脚本完成誊抄（会走确认 + 写后自证）。")
            return EXIT_CHECK_DRIFT
        print()
        print("✅ --check：副本与 catalog 一致。")
        return EXIT_OK

    if not args.write:
        print()
        print("ℹ️  dry-run 结束，未写入。确认无误后加 --write 重跑。")
        return EXIT_OK

    # ---------------- 无差异就不写 ----------------
    if not changed:
        print()
        print("✅ 无差异，无需写入（护栏 2 本来会在此处要求确认，现已无意义）。")
        return EXIT_OK

    # ---------------- 写前预检 ----------------
    preflight: list[str] = []
    preflight += check_hedging_integrity(new_data)
    preflight += check_structure(new_data)
    hedging_hits = check_new_hedging_in_text(old_data, new_data)
    if hedging_hits and not args.allow_hedging:
        preflight += hedging_hits
    elif hedging_hits:
        print()
        print("⚠️  --allow-hedging：**本次跳过了「正文含削弱词」的写前拦截**。")
        for h in hedging_hits:
            print(f"   · 原本会被拦：{h}")
        print("   注意：eval 的 redline_no_hedging 仍会在自证阶段变红并触发自动回滚。")

    if preflight:
        print()
        print("❌ 写前预检不通过，**拒绝写入**：")
        for p in preflight:
            print(f"   · {p}")
        print()
        print("   这些都是「红线被削弱」的可判定形态。确认是刻意调整后，")
        print("   请改代码/规范本身，而不是在这里绕过。")
        return EXIT_EDITING_IN_PROGRESS

    # ---------------- 护栏 2：交互确认 ----------------
    if not ask_confirmation(diff_text, critical, args.yes):
        return EXIT_DECLINED

    # ---------------- 写入 + 护栏 4 自证 ----------------
    original_bytes = BASELINE.read_bytes() if BASELINE.exists() else b""
    original_md5 = md5_of(original_bytes) if original_bytes else "(不存在)"
    try:
        atomic_write(BASELINE, new_raw)
    except OSError as exc:
        print()
        print(f"❌ 写入失败：{exc}")
        return EXIT_USAGE
    print(f"✅ 已写入 {BASELINE.relative_to(ROOT).as_posix()}")

    ok, detail, safety = self_check_after_write()
    if not ok:
        print()
        print("=" * 74)
        print("❌ 护栏 4 自证失败 —— 这次重新生成与既有基准存在实质冲突。")
        print("=" * 74)
        if safety:
            print("医疗 / 红线类断言失败：")
            for s in safety[:20]:
                print(f"   · {s}")
            print()
        print(detail[-4000:] if len(detail) > 4000 else detail)
        print()
        print("-" * 74)
        # 自动回滚：把一个已被证明有问题的副本留在盘上，
        # 只靠一句「请回滚」是不够的 —— 下一个人未必会回滚。
        if original_bytes:
            try:
                atomic_write(BASELINE, original_bytes.decode("utf-8"))
                restored = md5_of(BASELINE.read_bytes())
                print(f"↩️  已自动回滚到写入前的内容（md5 {restored}，期望 {original_md5}）")
            except (OSError, UnicodeDecodeError) as exc:
                print(f"⚠️  自动回滚失败：{exc}")
                print(f"   请手动恢复：git restore -- "
                      f"{BASELINE.relative_to(ROOT).as_posix()}")
        else:
            print("⚠️  写入前副本不存在，无可回滚内容。请手动删除 "
                  f"{BASELINE.relative_to(ROOT).as_posix()} 以回到无基线状态。")
        print("-" * 74)
        print()
        print("下一步（不要跳过）：")
        print("  1. 确认 catalog.py 的改动到底是刻意的还是手滑 —— 看上面红色失败项")
        print("  2. 若是手滑：修好 catalog.py，重新生成")
        print("  3. 若是刻意调整：确认失败的断言**本来就该变**，")
        print("     再连带更新套件/断言，并把这个决定写进 review 记录")
        return EXIT_SELF_CHECK_FAILED

    print()
    print("✅ 护栏 4 自证通过：eval 保持绿，重新生成与既有基准自洽。")
    print()
    print("提醒：这次盖章的依据是「你看过上面的 diff 并确认过catalog.py 的改动是刻意的」。")
    print("请顺手确认 review 时能看到这次改动：")
    print(f"    git --no-pager diff -- {BASELINE.relative_to(ROOT).as_posix()}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())