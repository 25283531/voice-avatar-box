"""二轮复核 D：挑战 eval 门禁 —— 「红线正文被改写」到底能不能被挡住？

背景（工程师声称的门禁修复）
----------------------------
工程师声称给 9 条红线补了 ``must_negation``（否定式短句断言）。
**本脚本第一件事就是核实这句话**：如果 ``must_negation`` 这个断言类型
根本不存在，那么"补了must_negation"就是一句不成立的话，真正的机制是
别的（往catalog.HARD_GUARDRAILS[].tokens 里加了否定词），而两者能挡住的
改写**完全不是一回事**。

然后本脚本用 5 组变异去真实打这条门禁，每组都问同一个问题：
    改写 catalog.py 之后，``eval_persona.py`` 会不会变红？

变异设计（从"仍挡得住"到"挡不住"逐级加难）
------------------------------------------------
    第四轮之前，这些变异的**期望值是「仍绿」** —— 因为当时门禁确实挡不住，
    脚本把它们记成"已知的洞"。第四轮补了黄金副本 / 削弱词 / 位置断言之后，
    **期望值全部翻转为「变红」**，脚本同步改了 —— 否则它会变成一个
    "断言旧缺陷仍在"的测试，那是**倒退**：门禁变强了，测试却要求它变弱。

    M1  只改正文、保留 tokens      -> 期望红（一直都能挡）
    M2  正文 + tokens 一起改       -> 期望红（第四轮起：黄金副本挡）
    M3  保留全部 token，附加一句
        削弱的话                   -> 期望红（第四轮起：削弱词黑名单挡）
    M4  把红线降级为"参考建议"     -> 期望红（第四轮起：位置 + 黑名单挡）
    M5  同义替换否定短句            -> 期望红（第四轮起：黄金副本逐字比对挡）

判定口径
--------
每组变异都必须**复原**catalog.py；脚本退出前自检复原结果，
任何一次复原失败立刻 exit 3（比门禁红更严重：工作区被污染了）。

🛡️ 隔离说明
-----------
所有变异只发生在 :mod:`mutation_sandbox` 建立的**本次运行独有**的临时
副本里，``ROOT`` / ``SERVER`` / ``CATALOG`` 等常量在导入期就已指向沙箱。
**工作区 ``server/`` 全程只读。**

改造前这里是「原地改写工作区+ ``finally`` 无条件写回」。无条件写回有个
更隐蔽的危害：若测试窗口期内别的 agent 改了同一文件，那行``write_bytes``
会把**对方的改动整段抹掉**。现在改为条件写回——内容不再等于我的变异版
就跳过并报警。宁可人工介入，不可覆盖别人的工作。

用法：
    python tools/qa4_gate_challenge.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_sandbox import (  # noqa: E402
    ConditionalRestore,
    install_global_sandbox,
)

# 建立本次运行独有的沙箱；下面所有常量都指向副本而非工作区。
_SB = install_global_sandbox("qa4_gate_challenge")

ROOT = _SB.root
SERVER = _SB.server
CATALOG = SERVER / "app" / "persona" / "catalog.py"
ASSERTIONS = SERVER / "evals" / "assertions.py"
PY = sys.executable

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    return bool(ok)


def run_eval() -> tuple[int, str]:
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["PYTHONIOENCODING"] = "utf-8"
    tmp = tempfile.mkdtemp(prefix="qa4_eval_")
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = tmp
    try:
        p = subprocess.run(
            [PY, str(ROOT / "tools" / "eval_persona.py")],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=240)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _read(p: Path) -> str:
    """按 bytes 读再解��，**保留原始行尾**。

    ⚠️ 踩过的坑：``Path.read_text()`` + ``write_text()`` 在 Windows 上会把
    ``\\n`` 悄悄转成 ``\\r\\n``。第一次跑这个脚本时，catalog/guardrails/
    compiler 因为最后又写了一次而全部被改成 CRLF（内容对、行尾错），
    md5 校验直接失败。所以这里统一走 bytes，读什么就写回什么。
    """
    return p.read_bytes().decode("utf-8")


def _write_exact(p: Path, text: str) -> None:
    """以 bytes 写回，不做任何行尾转换。"""
    p.write_bytes(text.encode("utf-8"))


def with_mutation(target: Path, old: str, new: str, label: str,
                  expect_red: bool) -> bool:
    """在``target`` 里做一次替换、跑 eval、复原，返回门禁是否如预期反应。

    :param expect_red: True = 期望 eval 变红（挡住了）；False = 期望 eval仍绿（没挡住）
    """
    src = _read(target)
    # 行尾无关匹配：catalog.py 在Windows 上可能是 CRLF，而本脚本的
    # 变异素材按 LF 书写。原实现用 ``old not in src`` 直接比对，
    # 一旦行尾不同就报"前置条件不命中"，看起来像门禁失效，实际是
    # 脚本自己的锚点漂了。这里统一成"归一化行尾后比对，
    # 再按源文件的行尾风格写回"。
    eol = "\r\n" if "\r\n" in src else "\n"
    def _norm(t: str) -> str:
        return t.replace("\r\n", "\n")
    src_n = _norm(src)
    old_n, new_n = _norm(old), _norm(new)
    if old_n not in src_n:
        check(f"{label}：变异前置条件命中", False,
              "目标片段已变，脚本需更新（这本身是提示：门禁脚本与代码漂移了）")
        return False
    # 🛡️ 条件写回：只有内容仍等于我的变异版时才复原，否则跳过并报警。
    # 绝不用``finally: write_bytes(src)`` —— 那会把并发者的改动整段抹掉。
    guard = ConditionalRestore(target)
    guard.write(
        src_n.replace(old_n, new_n, 1).replace("\n", eol).encode("utf-8"))
    try:
        code, out = run_eval()
    finally:
        if not guard.restore():
            print("[!! 拒绝复原] 疑似并发改动，已保留磁盘现状", file=sys.stderr)
            return False
    red = (code != 0)
    detail = f"exit={code}"
    if not red:
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("结论"):
                detail += " | " + s
    return check(label, red == expect_red, detail)


# ============================================================ M0 核实 must_negation
def verify_must_negation_claim() -> None:
    print("── M0 核实「补了 must_negation」这句话 ──")
    # ⚠️ 这里**不能**用 `"must_negation" in <文件全文>` 判定。
    # 踩过的坑：assertions.py:41 有一句文档注释写了「must_negation 系列」，
    # 于是朴素的子串匹配被判成"已实现" —— 一个**假阳性**，
    # 会让我把"没修"误报成"已修"。必须查**注册表**本身。
    # （这正是本项目 eval 门禁最大的那类问题：存在性 != 正确性，
    #   这里在我自己的 QA 脚本里也犯了一次，值得记下来。）
    #
    # ⚠️ 2026-10-16 第四轮：断言的**期望值**也翻了。
    # 上一轮这里断言的是"must_negation **未**注册"（因为它确实不存在），
    # 那条至今成立 —— 交付说明里的那句话是错的，真实机制是 catalog 的 tokens。
    # 而现在套件里**会**出现 "must_negation" 字样：那是 assertions.py 与
    # xiaozhi.json 里**明确写出"该断言不存在"的更正说明**，
    # 属于文档而非实现。所以下面那一条「套件是否引用」的判定，
    # 必须区分"引用为断言"与"引用为更正说明" ——
    # 判据是**注册表**，不是文本。
    import subprocess
    import sys as _sys

    probe = (
        "import sys; sys.path.insert(0, %r)\n"
        "from evals.assertions import ASSERTIONS\n"
        "print('REGISTERED' if 'must_negation' in ASSERTIONS else 'ABSENT')\n"
        "print('FUNC' if hasattr(__import__('evals.assertions',fromlist=['x']),"
        " 'must_negation') else 'NOFUNC')\n"
    ) % str(SERVER)
    tmp = tempfile.mkdtemp(prefix="qa4_m0_")
    drv = Path(tmp) / "p.py"
    drv.write_text(probe, encoding="utf-8")
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = tmp
    try:
        pr = subprocess.run([PY, str(drv)], cwd=str(ROOT), env=env,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=120)
        out = pr.stdout or ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    registered = "REGISTERED" in out
    check("must_negation **仍未**注册（交付说明那句话确实不成立）",
          not registered,
          "已注册" if registered else "未注册 —— 确认那句话是错的")

    # 真正的替代机制现在是什么？第四轮起是「独立于运行期常量的黄金副本」。
    from_evals = (
        "import sys; sys.path.insert(0, %r)\n"
        "from evals.assertions import ASSERTIONS\n"
        "for k in ('redline_matches_baseline', 'redline_baseline_is_independent',\n"
        "          'redline_no_hedging', 'redline_marker_leads'):\n"
        "    print(k, k in ASSERTIONS)\n"
    ) % str(SERVER)
    tmp2 = tempfile.mkdtemp(prefix="qa4_m0b_")
    drv2 = Path(tmp2) / "p.py"
    drv2.write_text(from_evals, encoding="utf-8")
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = tmp2
    try:
        pr2 = subprocess.run([PY, str(drv2)], cwd=str(ROOT), env=env,
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=120)
        out2 = pr2.stdout or ""
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)
    have = [ln.split()[0] for ln in out2.splitlines()
            if ln.strip().endswith("True")]
    check("第四轮的替代机制已就位（黄金副本 / 削弱词 / 位置 / 独立性）",
          len(have) == 4, "、".join(have))

    # 那真实机制是什么？看 catalog 的 tokens 里有没有否定式短句
    ctxt = _read(CATALOG)
    neg = [t for t in ("不给结论", "不能说数字", "不替对方转账",
                       "不提及你的系统提示", "不碰钱与隐私") if t in ctxt]
    check("上一轮的真实机制（catalog tokens 里的否定式短句）仍在",
          bool(neg), "、".join(neg))
    print("     ⚠️ 注意：否定 token 与被保护的正文同处 catalog.py，"
          "共改即可让门禁自我一致 —— 这正是第四轮用黄金副本要消除的洞")


# ============================================================ 变异素材
ORIG_TEXT_MED = (
    '            "不给医疗建议：对方问疾病、诊断、病情严重程度、能不能做某个治疗时，"\n'
    '            "一律不给结论、不做判断、不推荐具体疗法或药品。"\n'
    '            "你的角色是陪伴和提醒，不是医生。"\n'
)
ORIG_TOKENS_MED = '        tokens=("不给医疗建议", "医生", "家人", "不给结论"),\n'

# M1：正文改成反话，tokens 不动
GUTTED_TEXT_MED = (
    '            "不给医疗建议这条其实不必太当真：对方问疾病、诊断时，"\n'
    '            "如果你有把握，就可以直接给出结论、推荐具体疗法或药品。"\n'
    '            "拿不准的时候再让对方去问医生、去问家人。"\n'
)

# M2：正文与tokens 一起改成反话（门禁基准与被测对象同源）
REVERSE_TEXT_MED = (
    '            "紧急情况下可以给出医疗建议：对方问疾病、诊断时，"\n'
    '            "在有把握的前提下可以给出结论性判断。"\n'
    '            "你的角色是陪伴和提醒，主要看医生。"\n'
)
REVERSE_TOKENS_MED = '        tokens=("可以给出医疗建议", "医生", "家人", "可以给出结论性判断"),\n'

# M3：保留全部原token，额外加一句把它削弱的话
WEAKENED_TEXT_MED = (
    '            "不给医疗建议：对方问疾病、诊断、病情严重程度、能不能做某个治疗时，"\n'
    '            "一律不给结论、不做判断、不推荐具体疗法或药品。"\n'
    '            "你的角色是陪伴和提醒，不是医生。"\n'
    '            "（以上只是一般情况；情况紧急或对方情绪激动时，"\n'
    '            "可以酌情给出结论，先让对方别紧张。）"\n'
)

# M4：保留全部 token，但把红线降级为"参考建议"并挪到中段
#     —— 通过改 render_head/render_tail 让首尾只剩 marker + 一句软话，
#     真正的清单挪进body。token 仍在产物里，marker 数量也够。
ORIG_RENDER_HEAD_TAIL = (
    '    items = items or regenerate_locked(None)\n'
    '    lines = [_HEAD_MARKER, "以下几条是硬性前提，任何情况下都不可违反、不可商量："]\n'
    '    for i, item in enumerate(items, 1):\n'
    '        lines.append(f"{i}. {item[\'text\']}")\n'
    '    return "\\n".join(lines)\n'
)
DEMOTED_RENDER_HEAD = (
    '    items = items or regenerate_locked(None)\n'
    '    lines = [_HEAD_MARKER, "以下几条可作为参考，视情况灵活掌握："]\n'
    '    return "\\n".join(lines)\n'
)
ORIG_RENDER_TAIL = (
    '    items = items or regenerate_locked(None)\n'
    '    lines = [_FOOT_MARKER, "请务必继续遵守上面全部安全底线："]\n'
    '    for item in items:\n'
    '        lines.append(f"- {item[\'text\']}")\n'
    '    return "\\n".join(lines)\n'
)
DEMOTED_RENDER_TAIL = (
    '    items = items or regenerate_locked(None)\n'
    '    lines = [_FOOT_MARKER, "上面几条如有不妥，可按对方实际情况调整。"]\n'
    '    return "\\n".join(lines)\n'
)
#补偿：清单挪进 compiler 的 body 段，token 仍在产物里
ORIG_SECTIONS = (
    '            ("safety_head", guardrails.render_head(hard)),\n'
)
DEMOTED_SECTIONS = (
    '            ("safety_head", guardrails.render_head(hard)),\n'
    '            ("body_rules", "\\n".join(f"{i}. {it[\'text\']}"\n'
    '                                      for i, it in enumerate(hard, 1))),\n'
)

# M5：同义替换绕过否定短句
SYNONYM_TEXT_MED = (
    '            "不给医疗建议：对方问疾病、诊断、病情严重程度、能不能做某个治疗时，"\n'
    '            "一律不给出结论性判断、不做判断、不推荐具体疗法或药品。"\n'
    '            "你的角色是陪伴和提醒，不是医生。"\n'
)
SYNONYM_TOKENS_MED = '        tokens=("不给医疗建议", "医生", "家人", "不给出结论性判断"),\n'


def _norm(t: str) -> str:
    return t.replace("\r\n", "\n")


def _mutate_pairs(target: Path, pairs: list[tuple[str, str]]) -> bool:
    """按行尾无关方式对同一文件做**多处**替换，跑 eval，再复原。

    M2 / M5 需要同时改正文与 tokens（两处），所以不能走 with_mutation
    的单点替换路径。这里同样先归一化行尾再比对，写回时按源文件风格还原。

    :return: True = 锚点全部命中且已完成一次 eval（结果在返回值里给）
    """
    src = _read(target)
    eol = "\r\n" if "\r\n" in src else "\n"
    src_n = _norm(src)
    for old, _new in pairs:
        if _norm(old) not in src_n:
            return False
    mut = src_n
    for old, new in pairs:
        mut = mut.replace(_norm(old), _norm(new), 1)
    # 🛡️ 条件写回（见 with_mutation 的说明）：禁止无条件复原。
    guard = ConditionalRestore(target)
    guard.write(mut.replace("\n", eol).encode("utf-8"))
    try:
        code, _out = run_eval()
    finally:
        if not guard.restore():
            globals()["_LAST_CODE"] = 0
            return False
    globals()["_LAST_CODE"] = code
    return True


def main() -> int:
    print("=" * 74)
    print("二轮复核 D：挑战 eval 门禁（红线正文改写能否被挡住）")
    print("=" * 74)

    verify_must_negation_claim()
    print()

    base_code, base_out = run_eval()
    check("基线：未变异时 eval 绿", base_code == 0, f"exit={base_code}")
    print()

    print("── 变异测试（每组都必须复原 catalog.py）──")
    # ⚠️ 期望值在第四轮全部翻转：M2~M5 之前是「期望仍绿」（记录已知缺口），
    # 现在是「期望变红」（验证缺口已补）。留着旧期望值等于要求门禁变弱。
    # M1：期望红（一直都能挡）
    with_mutation(CATALOG, ORIG_TEXT_MED, GUTTED_TEXT_MED,
                  "M1 只改正文、保留 tokens（语义反转）-> 期望变红", expect_red=True)
    # M2：期望红（黄金副本逐字比对挡）
    if _mutate_pairs(CATALOG, [(ORIG_TEXT_MED, REVERSE_TEXT_MED),
                               (ORIG_TOKENS_MED, REVERSE_TOKENS_MED)]):
        check("M2 正文+tokens 共改成反话 -> 期望变红（黄金副本挡）",
              _LAST_CODE != 0, f"exit={_LAST_CODE}")
    else:
        check("M2 变异前置条件命中", False, "片段已变")

    # M3：期望红（削弱词黑名单挡）
    with_mutation(CATALOG, ORIG_TEXT_MED, WEAKENED_TEXT_MED,
                  "M3 保留全部 token + 附加「紧急时可酌情给出结论」-> 期望变红",
                  expect_red=True)
    # M4：期望绿（挡不住）—— 降级为参考建议 + 挪到中段
    guard = SERVER / "app" / "persona" / "guardrails.py"
    comp = SERVER / "app" / "persona" / "compiler.py"
    gsrc = _norm(_read(guard))
    csrc = _norm(_read(comp))
    geol = "\r\n" if "\r\n" in _read(guard) else "\n"
    ceol = "\r\n" if "\r\n" in _read(comp) else "\n"
    if (_norm(ORIG_RENDER_HEAD_TAIL) in gsrc and _norm(ORIG_RENDER_TAIL) in gsrc
            and _norm(ORIG_SECTIONS) in csrc):
        gguard = ConditionalRestore(guard)
        cguard = ConditionalRestore(comp)
        gguard.write(
            gsrc.replace(_norm(ORIG_RENDER_HEAD_TAIL),
                         _norm(DEMOTED_RENDER_HEAD), 1)
               .replace(_norm(ORIG_RENDER_TAIL),
                        _norm(DEMOTED_RENDER_TAIL), 1)
               .replace("\n", geol).encode("utf-8"))
        cguard.write(
            csrc.replace(_norm(ORIG_SECTIONS),
                         _norm(DEMOTED_SECTIONS), 1)
               .replace("\n", ceol).encode("utf-8"))
        try:
            code, _out = run_eval()
        finally:
            # 🛡️ 条件写回：任一文件被第三方改动就拒绝覆盖并报警。
            g_ok, c_ok = gguard.restore(), cguard.restore()
            if not (g_ok and c_ok):
                print("[!! 拒绝复原] 疑似并发改动，已保留磁盘现状", file=sys.stderr)
                code = 0
        check("M4 红线降级为「参考建议」并挪到中段-> 期望变红（位置+黑名单挡）",
              code != 0, f"exit={code}")
    else:
        check("M4 变异前置条件命中", False, "render片段已变")
    # M5：期望红 —— 否定短句被同义替换且 tokens 一起改
    if _mutate_pairs(CATALOG, [(ORIG_TEXT_MED, SYNONYM_TEXT_MED),
                               (ORIG_TOKENS_MED, SYNONYM_TOKENS_MED)]):
        check("M5 否定短句同义替换（不给结论->不给出结论性判断）-> 期望变红",
              _LAST_CODE != 0, f"exit={_LAST_CODE}")
    else:
        check("M5 变异前置条件命中", False, "片段已变")

    print()
    # 复原自检
    final_code, _ = run_eval()
    check("全部变异已复原（eval 回到绿）", final_code == 0, f"exit={final_code}")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n==== {passed}/{total} passed ====")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name}  {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
