"""红线的**权威副本**读取与结构解析（门禁的期望值来源）。

⚠️⚠️ 这个模块存在的**唯一**理由：把红线的期望值从 catalog 里搬出来
-------------------------------------------------------------
上一轮的门禁有一个**同源自证**的结构性缺陷：

    ``guardrails.assert_present()`` 遍历 ``catalog.HARD_GUARDRAILS``，
    而 ``tokens`` 与被它保护的 ``text`` **在同一个文件、同一个常量里**，
    且 token 是正文的**子串**。于是任何人改坏正文时，token 会跟着一起变
    （或被删掉后顺手补一个），断言永远绿 —— 用被测对象的一部分去验证
    被测对象自己。

⚠️ **更正一处不实陈述**（2026-10-16 第四轮）
------------------------------------------
上一轮的交付说明写着「给 9 条红线补了 ``must_negation`` 否定式短句断言」。
**这个断言类型在生产代码里不存在**：``grep -rn must_negation server/app/`` 零命中，
它只出现在 QA 自己的验证脚本里。真实的机制是：把「不给结论」这类否定短句
塞进了 ``app/persona/catalog.py`` 的 ``HARD_GUARDRAILS[].tokens`` 元组，
断言仍然是原来的 ``assert_present``（token 存在性）。

这个差别不是措辞问题：``must_negation`` 如果存在，是一条**独立于 catalog**
的断言；而 ``tokens`` 与正文同源，**共改即可让门禁自我一致**。
（``evals/assertions.py`` 的旧文档注释里也写了「must_negation 系列」，
那是同一个误解的残留，已一并更正。）

所以期望值必须放在**读不到 catalog** 的地方。本模块 + ``redline_baseline.json``
就是那个地方：

* ``redline_baseline.json`` 是**逐字誊抄**的静态数据，**不含任何 import**；
* 本模块只 ``json.load`` 它，**不 import app.persona**；
* 这条独立性由 ``assertions.redline_baseline_is_independent`` **断言守着**
  —— 不是靠注释承诺，而是靠 AST 检查（见 :func:`forbidden_imports`）。

代价是刻意的摩擦：**改红线正文必须同时改 JSON**，否则门禁立刻变红。
这不是缺陷，是"改红线要过两道手续"的实现方式。

================================ 已知边界（不要误以为它是万能门禁）
============================================================
本模块挡不住「**同时篡改代码与黄金副本**」：

* 若 override 机制能改 ``text``，它同样能改 ``redline_baseline.json``；
* 因此真正的边界是「同时改两处」的成本 —— 远高于只改一处，
  且 ``git diff`` 会**同时暴露两处改动**，review 时看得见；
* 要彻底消除需要把黄金副本放进独立的、只有安全负责人能写的流程
  （签名 / 独立仓库）—— 那是组织问题，不是代码问题。

另外，**语义正确性依然不在门禁能力范围内**：
本模块保证的是「红线正文与上一次人工确认过的那一版**逐字相同**」，
**不是**「这一版的语义是对的」。人工审阅首次誊抄时的判断力仍是唯一防线。

⚠️ 边界之二：**本模块只比对「红线那两块文本」，产物其余部分一句都不看**
----------------------------------------------------------------------
这是第五轮 QA 实测确认的一条真实绕过（E1）：在 ``compiler.compile()`` 里、
``assert_present(compiled)`` **之前**追加一句与红线相反的指令，
``exit=0`` 全绿 —— 门禁只保证「红线原文逐字存在于 prompt 中」，
**不**保证「prompt 里没有与红线相反的指令」。

之所以挡不住：``expected_head_block`` / ``expected_tail_block`` 是
「**连续文本**」比对，判据是"这段话在不在产物里"，而产物里**别处**多一句
什么，本模块完全看不见。何况那句恰好落在 prompt 末尾 —— 注意力最高的位置。

⚠️ 这条比「同时篡改两处」更危险，因为它**更便宜**：不需要改红线一个字、
不需要改本文件，diff 里只有一行。完整讨论见
``server/evals/assertions.py`` 模块注释的「不能挡什么」表（那里是它该被
维护的地方）。**刻意不为它增加断言** —— 要挡就得对整份产物做语义判定，
那会制造自欺的假绿。

⚠️ 边界之三：``tokens`` 字段此前是**死数据**（第五轮已修）
------------------------------------------------------------
QA 实测：把本文件里的 ``tokens`` 全改成 ``["这是一套全新的假token","XYZ"]``，
或者把医疗红线的 4 个 token 削成 1 个（正文一字未改），**eval 全绿**。
原因是 :func:`assertions.redline_matches_baseline` 只比 ``id`` 与 ``text``，
**从不读 ``tokens``**。

留着这个洞的真正危害是**误导**：文件里有 tokens、副本里有 tokens、
文档里写着 tokens，看起来"token 也受副本保护"。「以为有保护」比
「知道没保护」更危险。现已把 ``tokens`` 纳入逐字比对范围（见
:func:`assertions.redline_matches_baseline`），于是 tokens 也**改一处即红**。
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any, NamedTuple

# 黄金副本路径：与本模块同目录（``server/evals/``）。刻意用 ``Path(__file__)``
# 定位而不是相对 CWD —— 后者会让「从仓库根跑」与「从 server/ 跑」拿到不同文件，
# 而那种差异只在 CI 上偶发，是最难查的一类门禁假绿。
BASELINE_PATH = Path(__file__).resolve().parent / "redline_baseline.json"

# 红线段的编号行（开头声明）：``1. 正文`` -> group(2) 是正文
_HEAD_ITEM = re.compile(r"^(\d+)\.\s+(.*)$")
# 红线段的列表行（结尾复述）：``- 正文``     -> group(1) 是正文
_TAIL_ITEM = re.compile(r"^-\s+(.*)$")


class Redline(NamedTuple):
    """一条红线的权威副本。

    ``text`` 与 ``tokens`` **都**是逐字比对的依据（第五轮起）。
    此前 ``tokens`` 只被下限校验读了一次，等于死数据 ——
    「token 被削弱」曾能全绿通过，详见模块注释「边界之三」。
    """

    id: str
    label: str
    text: str
    tokens: tuple[str, ...]


class Baseline(NamedTuple):
    """``redline_baseline.json`` 的结构化视图。"""

    guardrail_version: str
    count: int
    head_marker: str
    foot_marker: str
    head_declaration: str
    tail_declaration: str
    hedging_words: tuple[str, ...]
    redlines: tuple[Redline, ...]


def load_baseline() -> Baseline:
    """读黄金副本。**只读文件，不读 catalog。**

    刻意不缓存到模块级常量：缓存会让「改了 JSON 但进程没重启」这种状态在
    测试里变得不可见，而门禁最怕的就是这种说不清的绿。每次调用重新读盘 ——
    这个文件只有几 KB，代价可以忽略。
    """
    with BASELINE_PATH.open("r", encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    redlines = tuple(
        Redline(
            id=str(item["id"]),
            label=str(item.get("label") or ""),
            # 刻意**不** strip()：逐字比对就是要连空白都比。
            # 这里 strip 会让「正文多了一个空格」这种改动溜过去。
            text=str(item["text"]),
            tokens=tuple(str(t) for t in (item.get("tokens") or [])),
        )
        for item in (data.get("redlines") or [])
    )
    return Baseline(
        guardrail_version=str(data.get("guardrail_version") or ""),
        count=int(data.get("count") or 0),
        head_marker=str(data.get("head_marker") or ""),
        foot_marker=str(data.get("foot_marker") or ""),
        head_declaration=str(data.get("head_declaration") or ""),
        tail_declaration=str(data.get("tail_declaration") or ""),
        hedging_words=tuple(str(w) for w in (data.get("hedging_words") or [])),
        redlines=redlines,
    )


def expected_head_block(base: Baseline) -> str:
    """按黄金副本**重建**开头红线段的期望文本（逐字）。

    为什么是「重建」而不是「从产物里切一段出来比」：重建让判定方向是
    「产物必须**逐字等于**期望值」，而期望值完全来自 JSON。切段比对会引入
    「切在哪」的自由度，而那种自由度就是新的绕过面。
    """
    lines = [base.head_marker, base.head_declaration]
    lines.extend(f"{i}. {r.text}" for i, r in enumerate(base.redlines, 1))
    return "\n".join(lines)


def expected_tail_block(base: Baseline) -> str:
    """按黄金副本重建结尾复述段的期望文本（逐字）。"""
    lines = [base.foot_marker, base.tail_declaration]
    lines.extend(f"- {r.text}" for r in base.redlines)
    return "\n".join(lines)


def extract_redline_texts(compiled: str, base: Baseline) -> tuple[list[str], list[str]]:
    """从**编译产物**里切出红线正文（开头编号项 + 结尾列表项）。

    只切这两处、且**只切编号/列表行** —— 这样削弱词黑名单不会被
    「家属的特别交代」或画像段里的话误伤（那正是误伤最容易被拿来
    调松黑名单的理由）。

    :return: (开头段的正文列表, 结尾段的正文列表)
    """
    return (_head_items(compiled, base), _tail_items(compiled, base))


def _head_items(compiled: str, base: Baseline) -> list[str]:
    idx = compiled.find(base.head_marker)
    if idx < 0:
        return []
    return [m.group(2) for m in
            (_HEAD_ITEM.match(ln.strip())
             for ln in _block_lines(compiled, idx)) if m]


def _tail_items(compiled: str, base: Baseline) -> list[str]:
    idx = compiled.find(base.foot_marker)
    if idx < 0:
        return []
    return [m.group(1) for m in
            (_TAIL_ITEM.match(ln.strip())
             for ln in _block_lines(compiled, idx)) if m]


def _block_lines(compiled: str, marker_idx: int) -> list[str]:
    """marker 之后、直到空行为止的块里的所有行（跳过 marker 那一行）。"""
    out: list[str] = []
    for line in compiled[marker_idx:].splitlines()[1:]:
        if not line.strip():
            break                      # 段落之间是空行 = 块结束
        out.append(line)
    return out


# ================================================================ 独立性自检
def forbidden_imports(source_path: Path | None = None) -> list[str]:
    """返回本模块源码里**所有** import 的模块名（含 __import__ 之类的动态取法）。

    为什么用 AST 而不是子串匹配
    --------------------------
    踩过的坑（QA 在它自己的脚本里也踩了同一个）：用 ``"catalog" in 源码全文``
    判定独立性，会被**文档注释里提到 catalog** 命中 —— 一个假阳性会让
    「没修」被误报成「已修」，或反过来让真问题被当成通过。
    AST 只看真正的 import 语句，注释与字符串里的字样一律不算。

    动态取法（``__import__`` / ``importlib.import_module``）也一并算进违规 ——
    否则绕过者只要写 ``__import__("app.persona.catalog")`` 就能让本检查失效。
    """
    path = source_path or Path(__file__).resolve()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.append(base)
            names.extend(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            fname = (func.id if isinstance(func, ast.Name)
                     else getattr(func, "attr", ""))
            if fname in ("__import__", "import_module"):
                names.append("<dynamic __import__>")
    return names


def is_independent_of_runtime() -> tuple[bool, str]:
    """本模块（及黄金副本）是否**结构上**读不到运行期常量。

    判定两件事：

    1. ``redline_baseline.py`` 的 import 清单里**没有** ``app.*``；
    2. ``redline_baseline.json`` 里**没有** ``import`` / ``app.persona``
       这类字样（静态 JSON 本来就 import 不进来，但有人可能把
       「从运行期常量复制的痕迹」写进注释字段里误导后人）。

    :return: (是否独立, 说明)
    """
    bad = [n for n in forbidden_imports()
           if n == "app" or n.startswith("app.") or n.startswith("<dynamic")]
    if bad:
        return False, "redline_baseline.py import 了运行期模块：" + "、".join(bad)
    raw = BASELINE_PATH.read_text(encoding="utf-8")
    for probe in ("import", "app.persona", "catalog"):
        if probe in raw:
            return False, f"黄金副本里出现了 {probe!r}（不该有的运行期痕迹）"
    return True, (f"黄金副本与本模块均不依赖运行期常量"
                  f"（{len(forbidden_imports())} 个 import 全是标准库）")


# 黑名单的**下限**。写死在这里而不是只存在 JSON 里，是为了守住一个
# QA 实测过的绕过：把 ``hedging_words`` 清空成 ``[]``。
# 那是最省事的削弱门禁方式 —— 不改任何红线正文，只让黑名单失效，
# 而黑名单本身在 ``redline_no_hedging`` 的判定里是唯一输入。
# JSON 在版本控制内，但「改 JSON」不需要改代码，两者门槛差一个量级，
# 所以这条下限必须由**代码**来守。
REQUIRED_HEDGING_WORDS = ("可酌情", "视情况", "不必太当真", "不一定", "除非")


def check_baseline_integrity() -> tuple[bool, str]:
    """黄金副本自身的**下限**校验（防「从最省事的地方削弱门禁」）。

    为什么需要这条（QA 反向验证实测到的真实缺口）
    ----------------------------------------------
    反向验证里有一组变异是：把 ``hedging_words`` 整个清空成 ``[]``，
    **红线正文一个字都不改**。结果 eval **全绿**。

    也就是说：只要有人动了黑名单，门禁就静默失效了 ——
    而这恰恰是最不可能被 review 发现的改动（diff 里只有一行 JSON）。

    同类的「最省事削弱」还有把 ``count`` 改小、把 ``redlines`` 删空，
    所以这里一并守住：条数、正文非空、黑名单下限、声明非空。

    ⚠️ 这条守的是**下限**，不是内容 —— 它挡不住"把某个词换成另一个词"。
    那仍然要靠 code review（完整边界见模块注释）。
    """
    problems: list[str] = []
    try:
        base = load_baseline()
    except FileNotFoundError as exc:
        return False, f"找不到黄金副本 {exc.filename}"

    if not base.redlines:
        problems.append("redlines 为空（门禁会比对空气）")
    if base.count != len(base.redlines):
        problems.append(
            f"count={base.count} 与实际条数 {len(base.redlines)} 不一致")
    if base.count < 5:
        problems.append(f"红线只有 {base.count} 条（下限 5）")
    if not base.hedging_words:
        problems.append("hedging_words 为空 —— 削弱词黑名单已失效")
    missing = [w for w in REQUIRED_HEDGING_WORDS if w not in base.hedging_words]
    if missing:
        problems.append("hedging_words 缺关键项：" + "、".join(missing))
    if not base.head_declaration or "不可商量" not in base.head_declaration:
        problems.append("head_declaration 不是不可商量声明"
                        f"（当前 {base.head_declaration!r}）")
    for r in base.redlines:
        if not r.text.strip():
            problems.append(f"{r.id}: 正文为空")
        if not r.tokens:
            problems.append(f"{r.id}: tokens 为空")
    if problems:
        return False, "；".join(problems)
    return True, (f"黄金副本下限校验通过：{base.count} 条红线、"
                  f"{len(base.hedging_words)} 个削弱词")
