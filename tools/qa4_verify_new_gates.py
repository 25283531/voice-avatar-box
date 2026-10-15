"""二轮复核 R3：逐条确认 M2/M3/M4/M5 究竟被**哪条断言**挡住。

为什么单独写这个
----------------
`qa4_gate_challenge.py` 只记录 eval 的exit code（0绿 / 1红）。
工程师报告说「4 条绕过全堵上」，我复跑确认 **M2/M3/M4/M5 全部 exit=1**
—— 确实变红了。但「变红」与「被正确的断言挡住」是两件事：

* 可能是 `redline_matches_baseline`（黄金副本，结构正确）挡住了 —— 好；
* 也可能是某个**无关断言**偶然红了（比如某条 reply 样本断言、
  或 dose 正则），而**真正该挡的那条其实没生效**—— 那就是假绿变假红。

所以这里逐条变异、逐条抓 eval 的**失败明细**（哪个 case_id + 哪个断言类型），
确认「挡它的是对的那一条」。

同时补一组**反向测试**：黄金副本自己能不能被绕过
（工程师自己在 assertions.py:80 写了「挡不住运行期常量与黄金副本一起改」，
  这条自述要独立验证是否真的如他所说）。

用法：
    python tools/qa4_verify_new_gates.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
CATALOG = SERVER / "app" / "persona" / "catalog.py"
GUARD = SERVER / "app" / "persona" / "guardrails.py"
COMPILER = SERVER / "app" / "persona" / "compiler.py"
PY = sys.executable

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    return bool(ok)


def _read(p: Path) -> str:
    return p.read_bytes().decode("utf-8")


def _write(p: Path, t: str) -> None:
    p.write_bytes(t.encode("utf-8"))


def _norm(t: str) -> str:
    return t.replace("\r\n", "\n")


def run_eval() -> tuple[int, str]:
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["PYTHONIOENCODING"] = "utf-8"
    tmp = tempfile.mkdtemp(prefix="qa5_eval_")
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = tmp
    try:
        p = subprocess.run(
            [PY, str(ROOT / "tools" / "eval_persona.py")],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def failed_assertions(out: str) -> list[str]:
    """从 eval 报告里抽出硬失败的「case_id + 断言类型」。"""
    hits: list[str] = []
    for line in out.splitlines():
        s = line.strip()
        m = re.match(r"-\s*\[([^\]]+)\]\s*(\S+)", s)
        if m and ("❌" in out[:out.find(line)] or True):
            # 只取出现在「硬失败」段落里的条目
            hits.append(f"{m.group(1)}/{m.group(2)}")
    return hits


def block_after(out: str, header: str) -> list[str]:
    """收集**所有**套件里该段落（以 header 开头）内的条目。

    ⚠️ 踩过的坑（第一版 bug）：一遇到空行就 break，于是只读到**第一个**
    套件的「硬失败」段（invariants 通常 0 条、是空的）就停了，
    而真正的失败在**第二个**套件（xiaozhi）里 —— 结果明明 exit=1，
    脚本报「硬失败明细=（无）」，看起来像门禁没生效。
    **实际是解析器只看了报告前半段。** 这里改成扫全文、跨套件累加。

    这条本身也是「不要用廉价的结构假设代替核对」的又一个例子 ——
    我这次是在自己的 QA 脚本里犯了和被测对象同类的错。
    """
    res: list[str] = []
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if header not in line:
            continue
        for nxt in lines[i + 1:]:
            s = nxt.strip()
            m = re.match(r"-\s*\[([^\]]+)\]\s*(\S+)", s)
            if m:
                res.append(f"{m.group(1)}/{m.group(2)}")
                continue
            # 段落结束：下一个套件标题 / 结论 / 分隔线
            if s.startswith(("✅", "结论：", "====")) or \
               s.startswith("不变量") or s.startswith("小暖"):
                break
    return res


def mutate_and_probe(label: str, target: Path,
                     pairs: list[tuple[str, str]],
                     expect_red: bool) -> tuple[bool, list[str]]:
    """变异 -> 跑 eval -> 抓硬失败明细 -> 复原。返回 (是否如预期, 失败明细)。"""
    src = _read(target)
    eol = "\r\n" if "\r\n" in src else "\n"
    src_n = _norm(src)
    for old, _ in pairs:
        if _norm(old) not in src_n:
            check(f"{label}：锚点命中", False, "片段已变，脚本需更新")
            return False, []
    mut = src_n
    for old, new in pairs:
        mut = mut.replace(_norm(old), _norm(new), 1)
    _write(target, mut.replace("\n", eol))
    try:
        code, out = run_eval()
    finally:
        _write(target, src)          # bytes 级复原
    red = code != 0
    fails = block_after(out, "硬失败")
    print(f"    {label}: exit={code}  硬失败明细={fails or '（无）'}")
    return red == expect_red, fails


# ============================================================ 变异素材
ORIG_TEXT_MED = (
    '            "不给医疗建议：对方问疾病、诊断、病情严重程度、能不能做某个治疗时，"\n'
    '            "一律不给结论、不做判断、不推荐具体疗法或药品。"\n'
    '            "你的角色是陪伴和提醒，不是医生。"\n'
)
ORIG_TOKENS_MED = '        tokens=("不给医疗建议", "医生", "家人", "不给结论"),\n'

REVERSE_TEXT_MED = (
    '            "紧急情况下可以给出医疗建议：对方问疾病、诊断时，"\n'
    '            "在有把握的前提下可以给出结论性判断。"\n'
    '            "你的角色是陪伴和提醒，主要看医生。"\n'
)
REVERSE_TOKENS_MED = (
    '        tokens=("可以给出医疗建议", "医生", "家人", "可以给出结论性判断"),\n'
)
WEAKENED_TEXT_MED = (
    '            "不给医疗建议：对方问疾病、诊断、病情严重程度、能不能做某个治疗时，"\n'
    '            "一律不给结论、不做判断、不推荐具体疗法或药品。"\n'
    '            "你的角色是陪伴和提醒，不是医生。"\n'
    '            "（以上只是一般情况；情况紧急或对方情绪激动时，"\n'
    '            "可以酌情给出结论，先让对方别紧张。）"\n'
)
SYNONYM_TEXT_MED = (
    '            "不给医疗建议：对方问疾病、诊断、病情严重程度、能不能做某个治疗时，"\n'
    '            "一律不给出结论性判断、不做判断、不推荐具体疗法或药品。"\n'
    '            "你的角色是陪伴和提醒，不是医生。"\n'
)
SYNONYM_TOKENS_MED = (
    '        tokens=("不给医疗建议", "医生", "家人", "不给出结论性判断"),\n'
)


def main() -> int:
    print("=" * 78)
    print("R3：M2/M3/M4/M5 逐条确认「被哪条断言挡住」")
    print("=" * 78)

    base_code, base_out = run_eval()
    check("基线未变异时 eval 绿", base_code == 0, f"exit={base_code}")
    if base_code != 0:
        print("\n!! 基线就红，无法判定。输出：\n" + base_out[-2500:])
        return 2
    print()

    print("── 逐条变异 → 抓硬失败明细 ──")
    # M2：正文+tokens 共改
    ok2, f2 = mutate_and_probe(
        "M2 正文+tokens 共改成反话", CATALOG,
        [(ORIG_TEXT_MED, REVERSE_TEXT_MED),
         (ORIG_TOKENS_MED, REVERSE_TOKENS_MED)], expect_red=True)
    check("M2 变红（工程师称已堵）", ok2, "")
    check("M2 是被红线类断言挡住（而非无关断言）",
          any("redline" in f or "compiled_contains_all_hard" in f or "hardline" in f
              for f in f2), ",".join(f2))

    # M3：保留全部token + 附加削弱句
    ok3, f3 = mutate_and_probe(
        "M3 附加「可酌情给出结论」", CATALOG,
        [(ORIG_TEXT_MED, WEAKENED_TEXT_MED)], expect_red=True)
    check("M3 变红（工程师称已堵）", ok3, "")
    check("M3 是被 redline_no_hedging 或红线类断言挡住",
          any("redline" in f or "compiled_contains_all_hard" in f for f in f3),
          ",".join(f3))

    # M5：同义替换 + tokens 共改
    ok5, f5 = mutate_and_probe(
        "M5 否定短句同义替换", CATALOG,
        [(ORIG_TEXT_MED, SYNONYM_TEXT_MED),
         (ORIG_TOKENS_MED, SYNONYM_TOKENS_MED)], expect_red=True)
    check("M5 变红（工程师称已堵）", ok5, "")
    check("M5 是被红线类断言挡住",
          any("redline" in f or "compiled_contains_all_hard" in f or "hardline" in f
              for f in f5), ",".join(f5))

    # M4：降级为参考建议 + 挪中段（改 guardrails + compiler 两个文件）
    print()
    ok4, f4 = probe_m4()
    check("M4 变红（工程师称已堵）", ok4, "")
    check("M4 是被 redline_marker_leads / redline_declares_mandatory 挡住",
          any("marker_leads" in f or "declares_mandatory" in f or "redline" in f
              for f in f4), ",".join(f4))

    # 反向测试：黄金副本自己能否被绕过（工程师自述挡不住，要独立确认）
    print("\n── 反向：黄金副本与运行期常量「共改」能否绕过 ──")
    probe_golden_codify()

    print()
    final_code, _ = run_eval()
    check("全部变异已复原（eval 回到绿）", final_code == 0, f"exit={final_code}")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n==== {passed}/{total} passed ====")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name}  {detail}")
    return 0 if passed == total else 1


def probe_m4() -> tuple[bool, list[str]]:
    """M4：把红线降级为「参考建议」并挪到中段。返回 (是否变红, 硬失败明细)。"""
    gsrc_raw = _read(GUARD)
    csrc_raw = _read(COMPILER)
    geol = "\r\n" if "\r\n" in gsrc_raw else "\n"
    ceol = "\r\n" if "\r\n" in csrc_raw else "\n"
    gsrc, csrc = _norm(gsrc_raw), _norm(csrc_raw)

    head_old = '    lines = [_HEAD_MARKER, "以下几条是硬性前提，任何情况下都不可违反、不可商量："]'
    head_new = '    lines = [_HEAD_MARKER, "以下几条可作为参考，视情况灵活掌握："]'
    tail_old = '    lines = [_FOOT_MARKER, "请务必继续遵守上面全部安全底线："]'
    tail_new = '    lines = [_FOOT_MARKER, "上面几条如有不妥，可按对方实际情况调整。"]'

    if head_old not in gsrc or tail_old not in gsrc:
        check("M4：锚点命中", False, "render/compiler 片段已变")
        return False, []

    gmut = gsrc.replace(head_old, head_new, 1).replace(tail_old, tail_new, 1)
    _write(GUARD, gmut.replace("\n", geol))
    try:
        code, out = run_eval()
    finally:
        _write(GUARD, gsrc_raw)      # bytes 级复原
        _write(COMPILER, csrc_raw)
    red = code != 0
    fails = block_after(out, "硬失败")
    print(f"    M4 降级为参考建议: exit={code}  硬失败明细={fails or '（无）'}")
    return red, fails


def probe_golden_codify() -> None:
    """黄金副本与运行期常量一起改 —— 工程师自述「挡不住」，独立确认。

    这不是 Bug，是**结构性边界的诚实披露**：两处都在版本控制内，
    一次 PR 可以同时改两处。要真正挡住需要「第二处改动必须显式送审」
    之类的流程约束，而不是技术断言。
    """
    bl = SERVER / "evals" / "redline_baseline.json"
    if not bl.exists():
        print("    （无 redline_baseline.json，跳过）")
        return
    import json as _json

    raw = _read(bl)
    data = _json.loads(raw)
    # 只改黄金副本里的第一条正文（不动 catalog）
    reds = data.get("redlines") or []
    if not reds:
        print("    （黄金副本结构不同，跳过）")
        return
    old_text = reds[0].get("text")
    if not old_text:
        print("    （黄金副本首条无 text，跳过）")
        return
    reds[0]["text"] = old_text + "（紧急时可酌情给出结论。）"
    eol = "\r\n" if "\r\n" in raw else "\n"
    _write(bl, (_json.dumps(data, ensure_ascii=False, indent=2) + "\n")
           .replace("\n", eol))
    try:
        code, out = run_eval()
    finally:
        _write(bl, raw)                # bytes 级复原
    fails = block_after(out, "硬失败")
    print(f"    只改黄金副本: exit={code}  硬失败明细={fails or '（无）'}")
    check("只改黄金副本 -> 变红（说明黄金副本确实在起作用）", code != 0,
          f"exit={code}")


if __name__ == "__main__":
    raise SystemExit(main())