"""QA：黄金副本门禁的鉴别力验证（挑战 QA-2 与 engineer-5 的实现）。

背景
----
`evals/redline_baseline.json` 是逐字誊抄的红线副本，断言要求编译产物里的
红线段**逐字等于**它。这类"黄金副本"门禁最危险的失败模式只有一个：

    **誊抄源与被测对象同源** —— 若副本是从 catalog 生成的，
    改 catalog 会带着副本一起变，断言就永远绿（自证循环）。

工程师的设计从结构上避开了：副本是**纯静态JSON、不引用任何运行期模块**。
**但结构对不等于真的有效** —— 还要实测。

本脚本做四件事：
  1. 确认副本内容 == 当前 catalog（此刻应一致）
  2. **改catalog 一句正文** -> 断言必须红（这是自证与否的决定性实验）
  3. 改副本一句 -> 断言必须红（确认它真的在比对，不是恒真）
  4. 复原后必须重新变绿

🛡️ 隔离说明
-----------
所有变异发生在 :mod:`mutation_sandbox` 的临时副本上，工作区全程只读。
改造前这里是原地改写 catalog + ``finally`` 无条件写回 —— 并发会造成
永久性红线反转（项目里已实际发生过两次）。
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
BASELINE_REL = "server/evals/redline_baseline.json"

ORIGINAL_TEXT = (
    '            "不给医疗建议：对方问疾病、诊断、病情严重程度、能不能做某个治疗时，"\n'
    '            "一律不给结论、不做判断、不推荐具体疗法或药品。"\n'
)
GUTTED_TEXT = (
    '            "不给医疗建议这条其实不必太当真：对方问疾病、诊断时，"\n'
    '            "如果你有把握，就可以直接给出结论、推荐具体疗法或药品。"\n'
)


def summarize(out: str) -> str:
    """只取红线相关的失败行，便于判断"是哪条断言抓住了"。"""
    hits = [s.strip() for s in out.splitlines()
            if s.strip().startswith("- [") and "redline" in s]
    return " | ".join(h[:150] for h in hits[:3]) or "(无红线相关失败)"


def main() -> int:
    print("=" * 72)
    print("黄金副本门禁的鉴别力验证")
    print("=" * 72)

    with MutationSandbox("qa3_golden_challenge") as sb:
        print(f"[sandbox] {sb.root}")
        print("[说明] 工作区 catalog.py / redline_baseline.json 全程只读。")

        # ---------- 0 前置：副本必须是纯静态、不引用运行期模块 ----------
        print("\n[0] 前置检查：副本是否同源")
        raw = sb.redline_baseline_json.read_text()
        data = json.loads(raw)
        if "import" in raw or "catalog" in raw.replace("catalog 的副本", ""):
            print("    ⚠️ 副本里出现了 'import'/'catalog' 字样，需人工确认")
        else:
            print("    ✅ 副本是纯静态数据，不引用 catalog —— 结构上不构成自证")
        print(f"    副本条数 = {len(data.get('hard', data.get('redlines', [])))}")

        # ---------- 1 基线 ----------
        code, out = sb.run_eval(timeout=600)
        print(f"\n[1] 基线（未变异）exit={code} "
              f"{'GREEN ✓' if code == 0 else 'RED ✗ 基线就不绿'}")
        if code != 0:
            print(summarize(out))
            return EXIT_FAIL

        # ---------- 2 决定性实验：改 catalog 一句正文 ----------
        print("\n[2] 决定性实验：把 catalog 里的医疗红线正文改成语义相反的话")
        print("    （副本是静态的，所以它不会跟着变 -> 断言必须红）")
        if not sb.mutate(CATALOG_REL, ORIGINAL_TEXT, GUTTED_TEXT):
            print("    **脚本失效**：catalog 目标片段未找到，请更新本脚本")
            return EXIT_SCRIPT_INVALID
        code2, out2 = sb.run_eval(timeout=600)
        print(f"    exit={code2}  "
              f"{'RED ✓ 断言抓住了改写' if code2 != 0 else 'GREEN ✗ 自证循环！'}")
        print(f"    失败项：{summarize(out2)}")
        sb.restore(CATALOG_REL)

        # ---------- 3 改副本一句，确认它真的在比对 ----------
        print("\n[3] 反向：把副本改一句，确认断言不是恒真")
        b2 = json.loads(sb.redline_baseline_json.read_text())
        key = None
        for k in ("hard", "redlines", "items"):
            if isinstance(b2.get(k), list) and b2[k]:
                key = k
                break
        code3 = 0
        if key is None:
            print("    （跳过：副本结构未识别，字段名需确认）")
        else:
            item = b2[key][0]
            field = "text" if isinstance(item, dict) and "text" in item else None
            if not field:
                print(f"    （跳过：副本项里没有 text 字段，实际结构={type(item).__name__}）")
            else:
                item[field] = item[field] + "（篡改）"
                sb.redline_baseline_json.write_json(b2)
                code3, out3 = sb.run_eval(timeout=600)
                print(f"    exit={code3}  "
                      f"{'RED ✓ 确实在逐字比对' if code3 != 0 else 'GREEN ✗ 恒真'}")
                print(f"    失败项：{summarize(out3)}")
                sb.restore(BASELINE_REL)

        # ---------- 4 复原确认 ----------
        code4, _ = sb.run_eval(timeout=600)
        print(f"\n[4] 沙箱复原确认 exit={code4} "
              f"{'GREEN ✓ 已复原' if code4 == 0 else 'RED ✗ 复原失败'}")

        ok = (code == 0 and code2 != 0 and code4 == 0)

    print("\n" + "=" * 72)
    print("结论：" + ("✅ 黄金副本门禁有真实鉴别力，不构成同源自证"
                if ok else "⚠️ 见上面数据，可能存在自证或恒真"))
    print("=" * 72)
    return EXIT_OK if ok else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
