"""E 项 + 第二轮绕过：找「文档没写」的绕过面。

已文档化的已知边界（不重复验证）：
  - 同时改 catalog 与json      （文档已承认）
  - 黑名单外的同义削弱        （文档已承认）
  - 红线整段挪到中段          （文档已声称挡不住）

本轮找**文档没提**的：
  A7  结尾复述段被稀释（marker_leads 只管开头 —— 它承认了吗？）
  A8  catalog + json 一起改（验证文档的「同时改两处」说法是否真如所说）
  A9  json 的 hedging_words 被删空（削弱词黑名单是门禁的一部分，也在 json 里！）
  A10 json 的 head_declaration 被软化（marker_leads 拿它当期望值）
  A11 红线条目顺序对调
  A12 guardrails.py 的 marker 改名 + json 同步（marker 漂移）

🛡️ 隔离说明
-----------
原实现用**固定的**共享沙箱 ``%TEMP%/qa3_sandbox`` + ``qa3_pristine``，
``restore()`` 每次从 pristine 拷回。固定路径在并发下会互相踩踏 ——
与原地改写工作区是同一类事故。现改为 :mod:`mutation_sandbox` 的
每次运行独有沙箱；工作区全程只读。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_sandbox import (  # noqa: E402
    EXIT_OK,
    MutationSandbox,
)

CATALOG_REL = "server/app/persona/catalog.py"
GUARD_REL = "server/app/persona/guardrails.py"
BASELINE_REL = "server/evals/redline_baseline.json"

_TOUCHED = (CATALOG_REL, GUARD_REL, BASELINE_REL)

_MED_OLD = '"一律不给结论、不做判断、不推荐具体疗法或药品。"'
_MED_NEW = '"一律给出结论、给出判断、推荐具体疗法或药品。"'
_TOK_OLD = 'tokens=("不给医疗建议", "医生", "家人", "不给结论"),'
_TOK_NEW = 'tokens=("不给医疗建议", "医生", "家人", "给出结论"),'


def ejson(sb: MutationSandbox, mut) -> None:
    """在沙箱副本的黄金副本 JSON 上跑一个变换函数。"""
    data = json.loads(sb.redline_baseline_json.read_text())
    mut(data)
    sb.redline_baseline_json.write_json(data)


def a7(sb: MutationSandbox) -> str:
    """结尾复述段稀释：把尾 marker 后的声明推到很靠后。"""
    old = '    lines = [_FOOT_MARKER, "请务必继续遵守上面全部安全底线："]'
    noise = "".join(f"补充说明{i}：请多陪老人聊聊天。" for i in range(1, 16))
    if not sb.mutate(
            GUARD_REL, old,
            f'    lines = [_FOOT_MARKER, "{noise}"]\n'
            f'    lines.append("请务必继续遵守上面全部安全底线：")'):
        raise ValueError("A7 锚点未命中")
    return "render_tail：尾 marker 后垫噪声，结尾声明挪到段末（marker_leads 只管开头）"


def a8(sb: MutationSandbox) -> str:
    """catalog + json 一起改成反话 —— 文档声称的已知边界。"""
    if not sb.mutate(CATALOG_REL, _MED_OLD, _MED_NEW):
        raise ValueError("A8 正文锚点未命中")
    if not sb.mutate(CATALOG_REL, _TOK_OLD, _TOK_NEW):
        raise ValueError("A8 tokens 锚点未命中")

    def mut(d):
        d["redlines"][0]["text"] = d["redlines"][0]["text"].replace(
            "一律不给结论、不做判断、不推荐具体疗法或药品。",
            "一律给出结论、给出判断、推荐具体疗法或药品。")
        d["redlines"][0]["tokens"] = ["不给医疗建议", "医生", "家人", "给出结论"]
    ejson(sb, mut)
    return "catalog + json 同步反转（文档声称的固有边界）"


def a9(sb: MutationSandbox) -> str:
    """把 json 里的削弱词黑名单删空 —— 黑名单本身住在被保护的 json 里。"""
    ejson(sb, lambda d: d.__setitem__("hedging_words", []))
    if not sb.mutate(
            CATALOG_REL, _MED_OLD,
            '"一律不给结论、不做判断、不推荐具体疗法或药品。紧急时可酌情给出结论。"'):
        raise ValueError("A9 锚点未命中")
    return "json 的 hedging_words 清空 + catalog 插入「可酌情」（两处同步削弱）"


def a10(sb: MutationSandbox) -> str:
    """把 json 的 head_declaration 软化 —— marker_leads 拿它当期望值。"""
    ejson(sb, lambda d: d.__setitem__(
        "head_declaration", "以下几条是硬性前提，最好尽量遵守："))
    if not sb.mutate(
            GUARD_REL,
            '"以下几条是硬性前提，任何情况下都不可违反、不可商量："',
            '"以下几条是硬性前提，最好尽量遵守："'):
        raise ValueError("A10 锚点未命中")
    return "json + guardrails 同步把「不可违反、不可商量」软化成「最好尽量遵守」"


def a11(sb: MutationSandbox) -> str:
    """把两条红线的定义顺序对调（内容不变）。"""
    handle = sb.catalog
    lines = handle.read_text().splitlines(keepends=True)
    starts = [i for i, ln in enumerate(lines) if ln.startswith("    HardGuardrail(")]
    if len(starts) < 3:
        raise ValueError("A11 锚点未命中：找不到足够多的 HardGuardrail 块")

    def block(s: int) -> list[str]:
        depth = 0
        for j in range(s, len(lines)):
            depth += lines[j].count("(") - lines[j].count(")")
            if depth == 0:
                return lines[s:j + 1]
        raise ValueError("A11 括号配平失败")

    first, second = block(starts[0]), block(starts[1])
    new = lines[:starts[0]] + second + first + lines[starts[2]:]
    handle.write_text("".join(new))
    return "catalog：前两条红线定义顺序对调"


def a12(sb: MutationSandbox) -> str:
    """marker 改名，guardrails + json 同步 —— 门禁会不会跟着一起改名而失效？"""
    old_h, new_h = "【安全底线 · 不可协商】", "【安全底线】"
    if not sb.mutate(GUARD_REL, f'_HEAD_MARKER = "{old_h}"',
                     f'_HEAD_MARKER = "{new_h}"'):
        raise ValueError("A12 guardrails 锚点未命中")
    ejson(sb, lambda d: d.__setitem__("head_marker", new_h))
    # 套件里的 marker 引用是否也要改？
    for suite in sb.suites():
        suite.write_text(suite.read_text().replace(old_h, new_h))
    return f"marker 由 {old_h} 改为 {new_h}（guardrails+json+两套件 全部同步）"


CASES = [
    ("A7 结尾段稀释", a7),
    ("A8 catalog+json 同步反转", a8),
    ("A9 json 黑名单清空+插削弱", a9),
    ("A10 声明软化（json+guardrails）", a10),
    ("A11 红线条目顺序对调", a11),
    ("A12 marker 改名（全同步）", a12),
]


def main() -> int:
    print("=" * 78)
    print("E 项：找文档未写的绕过面")
    print("=" * 78)

    with MutationSandbox("qa3_undocumented") as sb:
        print(f"[sandbox] {sb.root}（本次运行独有）")
        print("[说明] 工作区全程只读。")

        code, _ = sb.run_eval(timeout=900)
        print(f"[基线] eval exit={code}（期望 0）\n")

        results: list[tuple[str, bool, bool, list[str]]] = []
        for name, fn in CASES:
            sb.reset(_TOUCHED)
            try:
                desc = fn(sb)
            except ValueError as exc:
                print(f"{name}\n  **脚本失效**：{exc}\n")
                return 2
            code, out = sb.run_eval(timeout=900)
            red = code != 0
            crashed = "Traceback" in out
            who = sorted(set(re.findall(r"\] (\w+)\s+->", out))) if not crashed else []
            results.append((name, red, crashed, who))
            tag = "🔴 红" if red else "🟢 ★绿（绕过成功）"
            print(f"{name}\n  手法：{desc}")
            print(f"  结果：{tag}   exit={code}"
                  f"{'  ★崩溃(无报告)' if crashed else ''}")
            if who:
                print(f"  拦截断言：{', '.join(who)}")
            for a, d in re.findall(r"\] (\w+)\s+-> ([^\n]{0,140})", out)[:3]:
                print(f"    · [{a}] {d}")
            print()
            # 条件写回（catalog / guardrails / json / 两个套件）
            for rel in (*_TOUCHED, *(s.relpath for s in sb.suites())):
                sb.restore(rel)

        sb.reset(_TOUCHED)

    print("=" * 78)
    print("汇总：")
    for name, red, crashed, who in results:
        print(f"  {'🔴' if red else '🟢★'} {name:32s} exit变红={red} "
              f"{','.join(who)}")
    print("=" * 78)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
