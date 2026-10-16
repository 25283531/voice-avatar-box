"""第四轮反向验证：逐条打新门禁，确认「改了必红」。

为什么要有这个脚本
------------------
断言全绿**不能证明断言有鉴别力** —— 唯一能证明的是「把被保护的东西改坏，
它就变红」。本脚本对第四轮新增/加固的每条断言各跑一组变异，并要求：

* 期望变红的，**必须红**，且必须指出**是靠哪条断言挡住的**；
* 变异后**条件复原**（仅当内容仍等于我的变异版），退出前自检复原结果。

🛡️ 隔离说明
-----------
所有变异只发生在 :mod:`mutation_sandbox` 建立的**本次运行独有**的临时
副本里，``ROOT`` / ``SERVER`` / ``CATALOG`` 等常量在导入期就已指向沙箱。
**工作区 ``server/app/persona/catalog.py`` 全程只读。**

改造前这里是「原地改写工作区 + ``finally`` 无条件写回」。无条件写回有个
更隐蔽的危害：若测试窗口期内别的 agent 改了同一文件，那行 ``write_text``
会把**对方的改动整段抹掉**。现在改为条件写回——内容不再等于我的变异版
就跳过并报警。宁可人工介入，不可覆盖别人的工作。

⚠️ 复原失败立刻 exit 3 —— 比门禁红更严重：工作区被污染了，
后面所有结论都不可信。（Windows 上 ``Path.write_text`` 会把 LF 悄悄转成
CRLF，所以全程走 bytes。）

用法：
    python tools/qa7_redline_gate.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_sandbox import (  # noqa: E402
    EXIT_CONTAMINATED,
    ConditionalRestore,
    install_global_sandbox,
)

# 建立本次运行独有的沙箱；下面所有常量都指向副本而非工作区。
_SB = install_global_sandbox("qa7_redline_gate")

ROOT = _SB.root
SERVER = _SB.server
CATALOG = SERVER / "app" / "persona" / "catalog.py"
GUARD = SERVER / "app" / "persona" / "guardrails.py"
COMPILER = SERVER / "app" / "persona" / "compiler.py"
BASELINE = SERVER / "evals" / "redline_baseline.json"
PY = sys.executable

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    return bool(ok)


def _read(p: Path) -> str:
    return p.read_bytes().decode("utf-8")


def _write_exact(p: Path, text: str) -> None:
    """以 bytes 写回，不做任何行尾转换。

    ⚠️ 仅用于**沙箱副本**。工作区文件永远不经过这里。
    """
    p.write_bytes(text.encode("utf-8"))


def _norm(t: str) -> str:
    return t.replace("\r\n", "\n")


def run_eval() -> tuple[int, str]:
    """在沙箱里跑一次 eval（PYTHONPATH / cwd 均指向副本）。"""
    env = _SB.subprocess_env()
    try:
        p = subprocess.run(
            [PY, str(ROOT / "tools" / "eval_persona.py")],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "[TIMEOUT] eval 超过 300s 未返回"


# ⚠️ 锚点一律用 **LF** 书写。catalog.py 在 Windows 上是 CRLF，
# 而 with_mutation 只对**文件内容**做归一化 —— 锚点字面量如果自带 CRLF，
# 归一后反而对不上（这正是第一版「3 组变异全部锚点不命中」的根因：
# 报告看起来像跑过了，实际一组都没执行 —— 又一次假绿）。
# 所以：写锚点时永远用 \n，不要从文件里复制粘贴。


def which_assertions(out: str) -> list[str]:
    """从 eval 输出里提取「哪些 case 红了」——用于回答"靠哪条挡住的"。

    ⚠️ 解析的是 ``- [case_id] assertion_type  -> detail`` 这一行。
    取的是**断言类型**（第二个字段），不是 case_id ——
    case_id 是人写的名字（可能叫 ``redline-golden-copy``），
    断言类型才是「哪条断言机制生效」的答案。
    """
    hit: list[str] = []
    for line in out.splitlines():
        s = line.strip()
        if not s.startswith("- [") or "->" not in s:
            continue
        body = s[3:].split("]", 1)[-1].strip()      # 去掉 "[case_id]" 后的部分
        atype = body.split("->")[0].strip().split()[0] if body.strip() else ""
        if not atype:
            continue
        if (atype.startswith("redline") or atype in (
                "no_weakening_words", "redline_declares_mandatory",
                "compiled_contains_all_hard", "hardline_count_is")):
            hit.append(atype)
    out2: list[str] = []
    for h in hit:
        if h not in out2:
            out2.append(h)
    return out2


def with_mutation(target: Path, old: str, new: str, label: str,
                  expect_red: bool) -> bool:
    """在 ``target`` 里做一次替换、跑 eval、**无条件复原**。

    ⚠️ 锚点不命中时**必须** return���而不是继续跑 ——
    踩过的坑：第一版把「锚点漂移」当成一次普通失败记 0 分，
    结果 3 组变异根本没执行，报告却看起来像「测过了」。
    这正是本项目反复出现的那类假绿：没跑 ≠ 通过。
    """
    src = _read(target)
    eol = "\r\n" if "\r\n" in src else "\n"
    src_n = _norm(src)
    old_n, new_n = _norm(old), _norm(new)
    if old_n not in src_n:
        return check(f"{label}：变异前置条件命中", False,
                     f"目标片段已变（{target.name}），脚本需更新")
    # 条件写回：只有内容仍等于我的变异版才复原，否则跳过 + 报警
    guard = ConditionalRestore(target, label=f"{target.name}（{label}）")
    guard.write(src_n.replace(old_n, new_n, 1).replace("\n", eol).encode("utf-8"))
    try:
        code, out = run_eval()
    finally:
        guard.restore()
    red = code != 0
    detail = f"exit={code}"
    if red:
        hit = which_assertions(out)
        detail += " | 靠：" + ("、".join(hit) if hit else "(未识别)")
    return check(label, red == expect_red, detail)


def with_json_mutation(pairs: list[tuple[str, str]], label: str,
                       expect_red: bool) -> bool:
    """同时改多处：运行期常量（catalog）与黄金副本（JSON）各改一处。

    这是**唯一能真正打破同源自证**的变异形态：正文与 tokens 一起改，
    且看门禁是否仍然抓到 —— 因为期望值在第三个文件里。
    """
    cat_src = _read(CATALOG)
    base_src = _read(BASELINE)
    cat_eol = "\r\n" if "\r\n" in cat_src else "\n"
    cat_n, base_n = _norm(cat_src), _norm(base_src)
    missing = [old for old, _ in pairs
               if _norm(old) not in cat_n and _norm(old) not in base_n]
    if missing:
        return check(f"{label}：变异前置条件命中", False,
                     f"锚点不存在（{len(missing)} 处）：{missing[0][:60]!r}")
    mut_cat, mut_base = cat_n, base_n
    for old, new in pairs:
        if _norm(old) in mut_cat:
            mut_cat = mut_cat.replace(_norm(old), _norm(new), 1)
        elif _norm(old) in mut_base:
            mut_base = mut_base.replace(_norm(old), _norm(new), 1)
    # 条件写回：两个文件各自独立判断，只复原仍等于我变异版的那个
    g_cat = ConditionalRestore(CATALOG, "catalog.py")
    g_base = ConditionalRestore(BASELINE, "redline_baseline.json")
    g_cat.write(mut_cat.replace("\n", cat_eol).encode("utf-8"))
    g_base.write(mut_base.encode("utf-8"))
    try:
        code, out = run_eval()
    finally:
        g_cat.restore()
        g_base.restore()
    red = code != 0
    detail = f"exit={code}"
    if red:
        hit = which_assertions(out)
        detail += " | 靠：" + ("、".join(hit) if hit else "(未识别)")
    return check(label, red == expect_red, detail)


def with_mutation_multi(edits: list[tuple[Path, str, str]], label: str,
                        expect_red: bool) -> bool:
    """在**多个文件 / 同一文件多处**同时做替换，跑 eval，然后全部复原。

    M2/M4/M5 需要一次改多处（正文 + tokens、渲染 + 分段），单点替换路径
    表达不了。复原同样是 bytes 级、且**任一锚点缺失就不动任何文件** ——
    半改完才发现锚点漂移会留下一个既不是基线也不是变体的 workspace。
    """
    originals = {p: _read(p) for p in {t[0] for t in edits}}
    missing = [old for _p, old, _n in edits
               if _norm(old) not in _norm(originals[_p])]
    if missing:
        return check(f"{label}：变异前置条件命中", False,
                     f"锚点不存在：{missing[0][:70]!r}")
    # 条件写回：每个文件独立判断，仍是我的变异版才复原
    guards = {p: ConditionalRestore(p, p.name) for p in originals}
    try:
        for path, old, new in edits:
            src = _norm(originals[path])
            eol = "\r\n" if "\r\n" in originals[path] else "\n"
            guards[path].write(
                src.replace(_norm(old), _norm(new), 1).replace("\n", eol).encode("utf-8"))
        code, out = run_eval()
    finally:
        for path in originals:
            guards[path].restore()
    red = code != 0
    detail = f"exit={code}"
    if red:
        hit = which_assertions(out)
        detail += " | 靠：" + ("、".join(hit) if hit else "(未识别)")
    return check(label, red == expect_red, detail)


# ============================================================ 变异素材
ORIG_TEXT_MED = (
    '            "不给医疗建议：对方问疾病、诊断、病情严重程度、能不能做某个治疗时，"\n'
    '            "一律不给结论、不做判断、不推荐具体疗法或药品。"\n'
    '            "你的角色是陪伴和提醒，不是医生。"\n'
)
ORIG_TOKENS_MED = '        tokens=("不给医疗建议", "医生", "家人", "不给结论"),\n'

# R1 语义反转 + 同步改 tokens（QA 的 M2：门禁基准与被测对象同源）
REVERSED_TEXT_MED = (
    '            "紧急时可以给出医疗建议：对方问疾病、诊断时，"\n'
    '            "在有把握的前提下可以给出结论。"\n'
    '            "你的角色是陪伴和提醒，主要看医生。"\n'
)
REVERSED_TOKENS_MED = (
    '        tokens=("紧急时可以给出医疗建议", "医生", "家人", "可以给出结论"),\n'
)

# R2 保留全部 token，附加「可酌情」（QA 的 M3）
HEDGED_TEXT_MED = ORIG_TEXT_MED.replace(
    '            "你的角色是陪伴和提醒，不是医生。"\n',
    '            "你的角色是陪伴和提醒，不是医生。"\n'
    '            "（以上只是一般情况；情况紧急或对方情绪激动时，"\n'
    '            "可以酌情给出结论，先让对方别紧张。）"\n'
)

# R3 整段删除
DELETED_TEXT_MED = '            ""\n'

# R4 marker / 声明被挤到后面（QA 的 M4 的位置变体）
ORIG_RENDER_HEAD = (
    '    items = items or regenerate_locked(None)\n'
    '    lines = [_HEAD_MARKER, "以下几条是硬性前提，任何情况下都不可违反、不可商量："]\n'
    '    for i, item in enumerate(items, 1):\n'
    '        lines.append(f"{i}. {item[\'text\']}")\n'
    '    return "\\n".join(lines)\n'
)
PADDED_RENDER_HEAD = (
    '    items = items or regenerate_locked(None)\n'
    '    lines = [_HEAD_MARKER]\n'
    '    lines.append("以下几条是硬性前提，任何情况下都不可违反、不可商量：")\n'
    '    for _ in range(40):\n'
    '        lines.append("（以下是需要长期记住的日常沟通习惯，请认真遵守。）")\n'
    '    for i, item in enumerate(items, 1):\n'
    '        lines.append(f"{i}. {item[\'text\']}")\n'
    '    return "\\n".join(lines)\n'
)


def main() -> int:
    print("=" * 74)
    print("第四轮反向验证：新门禁的鉴别力（改了必红）")
    print("=" * 74)

    base_code, _ = run_eval()
    check("基线：未变异时 eval 绿", base_code == 0, f"exit={base_code}")
    print()

    print("── R1 语义反转 + 同步改 tokens（原 M2：同源自证）──")
    if with_json_mutation([(ORIG_TEXT_MED, REVERSED_TEXT_MED),
                            (ORIG_TOKENS_MED, REVERSED_TOKENS_MED)],
                           "R1 正文与 tokens 共改 -> 期望变红（黄金副本挡）", True):
        pass

    print("\n── R2 保留全部 token + 插入「可酌情」（原 M3）──")
    with_mutation(CATALOG, ORIG_TEXT_MED, HEDGED_TEXT_MED,
                  "R2 正文加「可酌情」-> 期望变红", expect_red=True)

    print("\n── R3 整条红线正文删除 ──")
    with_mutation(CATALOG, ORIG_TEXT_MED, DELETED_TEXT_MED,
                  "R3 红线正文整段删除 -> 期望变红", expect_red=True)

    print("\n── R4 不可商量声明被挤到 40 行之后（位置绕过）──")
    with_mutation(GUARD, ORIG_RENDER_HEAD, PADDED_RENDER_HEAD,
                  "R4 声明被前置内容稀释 -> 期望变红（位置断言挡）", expect_red=True)

    print("\n── R5 门禁退化成同源自证（把黄金副本改成读运行期常量）──")
    # 这一条验的是 redline_baseline_is_independent 真的有鉴别力：
    # 模拟有人「为了少维护一份」给 redline_baseline.py 加一句 import。
    rb = SERVER / "evals" / "redline_baseline.py"
    orig_rb = _read(rb)
    eol = "\r\n" if "\r\n" in orig_rb else "\n"
    tampered = _norm(orig_rb).replace(
        "import ast\n",
        "import ast\nfrom app.persona import catalog  # 故意：同源自证\n", 1)
    g_rb = ConditionalRestore(rb, "redline_baseline.py")
    g_rb.write(tampered.replace("\n", eol).encode("utf-8"))
    try:
        code, out = run_eval()
    finally:
        g_rb.restore()
    check("R5 黄金副本被改成 import 运行期常量 -> 期望变红",
          code != 0, f"exit={code} | 靠：{'、'.join(which_assertions(out))}")

    print("\n── R6 只改黄金副本（代码不动）-> 也必须红 ──")
    tampered_base = _norm(_read(BASELINE)).replace(
        '"hedging_words": [', '"hedging_words": [], "_removed": [', 1)
    g_base = ConditionalRestore(BASELINE, "redline_baseline.json")
    g_base.write(tampered_base.encode("utf-8"))
    try:
        code, out = run_eval()
    finally:
        g_base.restore()
    check("R6 削弱词黑名单被清空（正文一个字未改）-> 期望变红",
          code != 0, f"exit={code} | 靠：{'、'.join(which_assertions(out))}")

    # ------------------------------------------------------------------
    # QA 第四轮实测的 M1~M5：逐个重跑，标注「靠哪条挡住」
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("QA 第四轮 M1~M5 逐个重跑（上一轮：M1 红 / M2~M5 全绿）")
    print("=" * 70)

    print("\n── M1 只改正文、保留 tokens ──")
    with_mutation(CATALOG, ORIG_TEXT_MED, REVERSED_TEXT_MED,
                  "M1 语义反转但 tokens 不动 -> 期望变红", expect_red=True)

    print("\n── M2 正文 + tokens 共改（原「同源自证」，上一轮全绿）──")
    with_mutation_multi(
        [(CATALOG, ORIG_TEXT_MED, REVERSED_TEXT_MED),
         (CATALOG, ORIG_TOKENS_MED, REVERSED_TOKENS_MED)],
        "M2 正文与 tokens 共改 -> 期望变红（黄金副本挡）", expect_red=True)

    print("\n── M3 保留全部 token + 附加「可酌情」（上一轮全绿）──")
    with_mutation(CATALOG, ORIG_TEXT_MED, HEDGED_TEXT_MED,
                  "M3 插入削弱词 -> 期望变红", expect_red=True)

    print("\n── M4 红线降级为「参考建议」并挪到中段（上一轮全绿）──")
    # 降级 render_head/render_tail + 把清单挪进 compiler 的 body 段
    orig_render_tail = (
        '    items = items or regenerate_locked(None)\n'
        '    lines = [_FOOT_MARKER, "请务必继续遵守上面全部安全底线："]\n'
        '    for item in items:\n'
        '        lines.append(f"- {item[\'text\']}")\n'
        '    return "\\n".join(lines)\n'
    )
    demoted_head = (
        '    items = items or regenerate_locked(None)\n'
        '    lines = [_HEAD_MARKER, "以下几条可作为参考，视情况灵活掌握："]\n'
        '    return "\\n".join(lines)\n'
    )
    demoted_tail = (
        '    items = items or regenerate_locked(None)\n'
        '    lines = [_FOOT_MARKER, "上面几条如有不妥，可按对方实际情况调整。"]\n'
        '    return "\\n".join(lines)\n'
    )
    orig_sections = '            ("safety_head", guardrails.render_head(hard)),\n'
    demoted_sections = (
        '            ("safety_head", guardrails.render_head(hard)),\n'
        '            ("body_rules", "\\n".join(f"{i}. {it[\'text\']}"\n'
        '                                      for i, it in enumerate(hard, 1))),\n'
    )
    with_mutation_multi(
        [(GUARD, ORIG_RENDER_HEAD, demoted_head),
         (GUARD, orig_render_tail, demoted_tail),
         (COMPILER, orig_sections, demoted_sections)],
        "M4 降级为参考建议 + 首尾只剩 marker -> 期望变红", expect_red=True)

    print("\n── M5 否定短句同义替换「不给结论」→「不给出结论性判断」──")
    synonym_text = ORIG_TEXT_MED.replace("一律不给结论、", "一律不给出结论性判断、")
    synonym_tokens = ORIG_TOKENS_MED.replace("不给结论", "不给出结论性判断")
    with_mutation_multi(
        [(CATALOG, ORIG_TEXT_MED, synonym_text),
         (CATALOG, ORIG_TOKENS_MED, synonym_tokens)],
        "M5 同义替换否定短句 + tokens 同步 -> 期望变红", expect_red=True)

    print()
    final_code, _ = run_eval()
    check("全部变异已复原（eval 回到绿）", final_code == 0, f"exit={final_code}")

    # 复原自检：字节级比对
    print("\n── 复原自检（字节级）──")
    for p in (CATALOG, GUARD, COMPILER, BASELINE, rb):
        print(f"  {p.relative_to(ROOT)}  md5={_md5(p)}")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n==== {passed}/{total} passed ====")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name}  {detail}")
    return 0 if passed == total else 1


def _md5(p: Path) -> str:
    import hashlib
    return hashlib.md5(p.read_bytes()).hexdigest()[:12]


if __name__ == "__main__":
    raise SystemExit(main())
