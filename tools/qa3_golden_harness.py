"""QA-3 黄金副本门禁反向验证台（六种绕过）。

🛡️ 隔离说明（本次改造的重点）
---------------------------
改造前本脚本用的是一个**固定的**共享沙箱 ``%TEMP%/qa3_sandbox`` +
``%TEMP%/qa3_pristine``，并在 ``main()`` 开头 ``make_pristine()``
**删掉重建** pristine。

这在并发下是双重踩踏：
  1. 两个 agent 同时跑本脚本 → 共享同一个 ``CATALOG`` 路径，
     A 的变异会被 B 的 ``restore()`` 用 pristine 覆盖掉（或反之），
     留下一个既不是基线也不是变体的状态；
  2. ``make_pristine()`` 会 ``rmtree`` 掉B 正在依赖的 pristine 目录。

现在改为 :mod:`mutation_sandbox` 的**每次运行独有**的 ``mkdtemp`` 沙箱：
A 与 B 各有各的完整副本，结构上不可能互相影响，也不需要 pristine ——
沙箱副本本身就是基线（``SandboxFile.original_bytes``）。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_sandbox import (  # noqa: E402
    EXIT_FAIL,
    EXIT_OK,
    MutationSandbox,
)

CATALOG_REL = "server/app/persona/catalog.py"
GUARDRAILS_REL = "server/app/persona/guardrails.py"
BASELINE_REL = "server/evals/redline_baseline.json"
RB_PY_REL = "server/evals/redline_baseline.py"

_TOUCHED = (CATALOG_REL, GUARDRAILS_REL, BASELINE_REL, RB_PY_REL)


def hard_fails(out: str) -> list[str]:
    """抽取硬失败明细行。"""
    res: list[str] = []
    grab = False
    for ln in out.splitlines():
        if "硬失败" in ln and "CI 会红" in ln:
            grab = True
            continue
        if grab:
            if ln.strip().startswith("- ["):
                res.append(ln.strip())
            elif ln.strip() == "" or ln.strip().startswith("==="):
                grab = False
    return res


def blockers(out: str) -> list[str]:
    """抽取「是哪条断言挡住的」。"""
    found: list[str] = []
    for ln in hard_fails(out):
        m = re.search(r"\] (\w+)\s+->", ln)
        if m and m.group(1) not in found:
            found.append(m.group(1))
    return found


# ============================================ 六种绕过

def a1(sb: MutationSandbox) -> str:
    """正文反转，tokens 不动。"""
    if not sb.mutate(
            CATALOG_REL,
            '"一律不给结论、不做判断、不推荐具体疗法或药品。"',
            '"一律给出结论、给出判断、推荐具体疗法或药品。"'):
        raise ValueError("A1 锚点未命中")
    return "catalog 正文：不给结论 -> 给出结论（tokens 不动）"


def a2(sb: MutationSandbox) -> str:
    """正文与 tokens 一起反转 —— 同源自证的原始绕过手法。"""
    ok1 = sb.mutate(
        CATALOG_REL,
        '"一律不给结论、不做判断、不推荐具体疗法或药品。"',
        '"一律给出结论、给出判断、推荐具体疗法或药品。"')
    ok2 = sb.mutate(
        CATALOG_REL,
        'tokens=("不给医疗建议", "医生", "家人", "不给结论"),',
        'tokens=("不给医疗建议", "医生", "家人", "给出结论"),')
    if not (ok1 and ok2):
        raise ValueError("A2 锚点未命中")
    return "catalog 正文反转 + tokens 同步反转（同源自证原始绕过）"


def a3(sb: MutationSandbox) -> str:
    """尾部追加「可酌情」—— token 全保留。"""
    if not sb.mutate(
            CATALOG_REL,
            '"一律不给结论、不做判断、不推荐具体疗法或药品。"',
            '"一律不给结论、不做判断、不推荐具体疗法或药品。'
            '紧急情况下可酌情给出结论。"'):
        raise ValueError("A3 锚点未命中")
    return "catalog 正文尾部追加「紧急情况下可酌情给出结论」（token 全保留）"


def a4(sb: MutationSandbox) -> str:
    """在marker 之后垫噪声，把「不可商量」声明挤到段末。"""
    old = ('    lines = [_HEAD_MARKER, '
           '"以下几条是硬性前提，任何情况下都不可违反、不可商量："]')
    noise = "".join(f"第{i}条说明：请耐心倾听家属的交代并给予充分回应。"
                    for i in range(1, 19))
    new = (f'    lines = [_HEAD_MARKER, "{noise}"]\n'
           f'    lines.append("以下几条是硬性前提，任何情况下都不可违反、不可商量：")')
    if not sb.mutate(GUARDRAILS_REL, old, new):
        raise ValueError("A4 锚点未命中（guardrails.render_head 结构变了？）")
    return f"render_head：marker 后垫 {len(noise)} 字噪声，不可商量声明挪到段末"


def a5(sb: MutationSandbox) -> str:
    """整条删除 no_medical_advice。"""
    handle = sb.catalog
    src = handle.read_text()
    try:
        i = src.index('id="no_medical_advice"')
        j = src.index('id="no_dose_or_adjust"')
        start = src.rindex("HardGuardrail(", 0, i)
    except ValueError as exc:
        raise ValueError(f"A5 锚点未命中：{exc}") from exc
    handle.write_text(src[:start] + src[j:])
    return "catalog：整条删除 no_medical_advice"


def a6(sb: MutationSandbox) -> str:
    """只改黄金副本，源码不动。"""
    data = json.loads(sb.redline_baseline_json.read_text())
    data["redlines"][0]["text"] = "其实可以酌情给结论，也可以推荐具体药品。"
    sb.redline_baseline_json.write_json(data)
    return "baseline.json：医疗红线改成「其实可以酌情给结论…」（源码不动）"


MUTATIONS = [
    ("A1 语义反转", a1),
    ("A2 语义反转+同步改tokens", a2),
    ("A3 插入削弱词", a3),
    ("A4 marker前垫噪声淹没", a4),
    ("A5 整条红线删除", a5),
    ("A6 只改黄金副本", a6),
]


def main() -> int:
    print("=" * 76)
    print("QA-3 黄金副本门禁反向验证（每次运行独有沙箱）")
    print("=" * 76)

    with MutationSandbox("qa3_golden_harness") as sb:
        print(f"[sandbox] {sb.root}")
        print("[说明] 工作区全程只读；无需 pristine —— 沙箱副本自身即基线。")

        print("沙箱副本指纹（md5）：")
        for rel in _TOUCHED:
            handle = sb.handle(rel)
            print(f"  {rel:42s} "
                  f"{hashlib.md5(handle.original_bytes).hexdigest()}")

        code, out = sb.run_eval(timeout=900)
        print(f"\n[基线] eval exit={code}（期望 0）")
        if code != 0:
            print("!! 沙箱基线不绿，终止")
            for f in hard_fails(out):
                print("   ", f)
            return 2

        results: list[tuple[str, bool, int, list[str]]] = []
        for name, fn in MUTATIONS:
            sb.reset(_TOUCHED)
            try:
                desc = fn(sb)
            except ValueError as exc:
                print(f"\n{name}\n  **脚本失效**：{exc}")
                return 2
            code, out = sb.run_eval(timeout=900)
            who = sorted(set(blockers(out)))
            blocked = code != 0
            print(f"\n{'-' * 76}")
            print(f"{name}\n  手法：{desc}")
            print(f"  结果：{'🔴 变红（挡住）' if blocked else '🟢 仍绿（★没挡住）'}   "
                  f"eval exit={code}")
            print(f"  挡它的断言：{', '.join(who) if who else '（无）'}")
            for f in hard_fails(out)[:5]:
                print(f"    · {f[:190]}")
            results.append((name, blocked, code, who))
            # 条件写回
            for rel in _TOUCHED:
                sb.restore(rel)

        print("\n" + "=" * 76)
        print(f"{'绕过':30s} {'结果':6s} exit  拦截断言")
        print("-" * 76)
        allblocked = True
        for name, blocked, code, who in results:
            allblocked = allblocked and blocked
            print(f"{name:30s} {'🔴红' if blocked else '🟢绿':6s} {code}     "
                  f"{','.join(who) or '-'}")
        print("=" * 76)
        print("结论：六种绕过全部被挡住" if allblocked
              else "结论：★ 存在未被挡住的绕过")

    return EXIT_OK if allblocked else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
