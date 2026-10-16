"""QA：验证团队 lead 指出的那个缺口**确实存在**。

他说得比谁都准确
--------------
原先的说法是「``compiled_contains_all_hard`` 是自证的，现在有 golden copy
兜着所以无害」。他指出：**这个"无害"依赖「golden copy 存在」这一个前提，
而当时没有任何断言在守这个前提** —— 删掉 golden copy，门禁立刻退回自证状态，
而全部断言仍然绿。

而且他把失效场景说得更现实：不需要恶意改动，**只需要将来某次重构觉得
golden copy 冗余而删掉它**。

已有守卫 `redline_baseline_is_independent` 守的是「副本**结构上**不 import
运行期常量」—— 但它**不检查这个断言是否还在 ASSERTIONS 表里**。
所以本脚本实测：把 ``redline_matches_baseline`` 从套件里删掉，看门禁是否仍绿。

🛡️ 隔离说明
-----------
套件 JSON 与 catalog 的改动只发生在 :mod:`mutation_sandbox` 的临时副本上。
改造前这里是原地改写 + ``finally`` 无条件写回，并发下会抹掉同事的改动。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_sandbox import (  # noqa: E402
    EXIT_FAIL,
    EXIT_OK,
    EXIT_SCRIPT_INVALID,
    MutationSandbox,
)

CATALOG_REL = "server/app/persona/catalog.py"

# 决定性实验用的 catalog 正文（改坏它，看还有没有门禁报警）
_MED_OLD = (
    '            "不给医疗建议：对方问疾病、诊断、病情严重程度、能不能做某个治疗时，"\n'
    '            "一律不给结论、不做判断、不推荐具体疗法或药品。"\n'
)
_MED_NEW = (
    '            "不给医疗建议这条其实不必太当真：对方问疾病、诊断时，"\n'
    '            "如果你有把握，就可以直接给出结论、推荐具体疗法或药品。"\n'
)


def which_cases(sb: MutationSandbox, name: str, needle: str) -> bool:
    """套件里是否存在某类断言。"""
    data = json.loads(sb.suite(name).read_text())
    for case in data.get("cases") or []:
        for assertion in case.get("assertions") or []:
            if assertion.get("type") == needle:
                return True
    return False


def remove_assertion_type(sb: MutationSandbox, remove: str) -> bool:
    """从所有套件里移除某个断言类型（模拟"重构觉得冗余，删了"）。"""
    hit = False
    for suite in sb.suites():
        data = json.loads(suite.read_text())
        changed = False
        for case in data.get("cases") or []:
            keep = [a for a in (case.get("assertions") or [])
                    if a.get("type") != remove]
            if len(keep) != len(case.get("assertions") or []):
                case["assertions"] = keep
                changed = True
        if changed:
            suite.write_json(data)
            hit = True
    return hit


def main() -> int:
    print("=" * 72)
    print("验证：删掉 golden copy 断言后，门禁是否会退回自证而仍然全绿？")
    print("=" * 72)

    with MutationSandbox("qa3_guard_dependency") as sb:
        print(f"[sandbox] {sb.root}")
        print("[说明] 工作区套件 JSON 与 catalog.py 全程只读。")

        code0, _ = sb.run_eval(suites=["invariants"], timeout=300)
        code1, _ = sb.run_eval(suites=["xiaozhi"], timeout=300)
        print(f"\n[基线] invariants exit={code0}  xiaozhi exit={code1}")
        if code0 != 0 or code1 != 0:
            print("  基线不绿，先修基线再谈别的")
            return EXIT_SCRIPT_INVALID
        print(f"  redline_matches_baseline 在 xiaozhi 里 = "
              f"{which_cases(sb, 'xiaozhi', 'redline_matches_baseline')}")
        print(f"  compiled_contains_all_hard 在 xiaozhi 里 = "
              f"{which_cases(sb, 'xiaozhi', 'compiled_contains_all_hard')}")
        if not which_cases(sb, "xiaozhi", "redline_matches_baseline"):
            print("  （golden copy 断言不在 xiaozhi 里，本实验跳过）")
            return EXIT_SCRIPT_INVALID

        suite_rels = [s.relpath for s in sb.suites()]
        print("\n>>> 模拟重构：把 redline_matches_baseline 全部删掉")
        if not remove_assertion_type(sb, "redline_matches_baseline"):
            print("  未找到可删的用例（套件结构可能已变）")
            return EXIT_SCRIPT_INVALID
        c0, _o0 = sb.run_eval(suites=["invariants"], timeout=300)
        c1, _o1 = sb.run_eval(suites=["xiaozhi"], timeout=300)
        print(f"  invariants exit={c0}  xiaozhi exit={c1}")
        still_green = (c0 == 0 and c1 == 0)
        print(f"  -> 门禁状态：**{'仍然全绿（缺口成立）' if still_green else '变红（有守卫）'}**")

        if still_green:
            print("\n  复现同源自证：现在再把 catalog 的红线正文改坏")
            if not sb.mutate(CATALOG_REL, _MED_OLD, _MED_NEW):
                print("    （跳过：catalog 片段未找到）")
                return EXIT_SCRIPT_INVALID
            c2, _o2 = sb.run_eval(suites=["invariants"], timeout=300)
            c3, _o3 = sb.run_eval(suites=["xiaozhi"], timeout=300)
            print(f"    invariants exit={c2}  xiaozhi exit={c3}")
            escaped = (c2 == 0 and c3 == 0)
            print(f"    -> **语义反转的红线被放行？"
                  f"{'是（缺口完整复现）' if escaped else '否（仍被别处守住）'}**")
            if escaped:
                print("       （说明连 compiled_contains_all_hard 都拦不住 —— "
                      "因为它的基准就是被改的那个常量）")
            sb.restore(CATALOG_REL)

        # 条件写回：复原所有套件
        sb.reset(suite_rels)

        code2, _ = sb.run_eval(suites=["invariants"], timeout=300)
        code3, _ = sb.run_eval(suites=["xiaozhi"], timeout=300)
        print(f"[沙箱复原确认] invariants exit={code2}  xiaozhi exit={code3} "
              f"{'GREEN ✓' if code2 == 0 and code3 == 0 else 'RED ✗ 复原失败'}")
        ok = code2 == 0 and code3 == 0

    return EXIT_OK if ok else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
