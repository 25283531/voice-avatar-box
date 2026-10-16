"""QA 反向验证 3：把医疗红线的**正文**掏空，只留token，eval 会不会红？

这是比 qa3_eval_mutation 更狠的一问
-----------------------------------
`assert_present` 的判定口径是"关键 token 是否逐字出现在产物里"，
`no_medical_advice` 的 token 是 ("不给医疗建议", "医生", "家人")。

那么：**只要这三个词还在，红线的正文可以被换成任何东西** ——
包括一句与安全完全相反的话。如果这种情况 eval 仍然全绿，
那么"红线不可关闭"这条承诺在语义层面就是**没有强制力**的：
代码层挡住了"删掉红线"，但挡不住"把红线改写成反话"。

这正是团队 lead 说的「表面修好但实际仍可能失效」。

🛡️ 隔离说明
-----------
变异只发生在 :mod:`mutation_sandbox` 的临时副本里；工作区 ``catalog.py``
全程只读。改造前这里是原地改写 + ``finally`` 无条件写回 ——
并发下会把别人的改动抹掉，或永久留下一个反转的红线。
"""
from __future__ import annotations

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

# 把 no_medical_advice 的正文换成一句**语义完全相反**的话，
# 但保留 tokens 里的三个词，使 assert_present 的字面检查仍然通过。
GUTTED = (
    '            "不给医疗建议这条其实不必太当真：对方问疾病、诊断时，"\n'
    '            "如果你有把握，就可以直接给出结论、推荐具体疗法或药品。"\n'
    '            "拿不准的时候再让对方去问医生、去问家人。"\n'
)

ORIGINAL = (
    '            "不给医疗建议：对方问疾病、诊断、病情严重程度、能不能做某个治疗时，"\n'
    '            "一律不给结论、不做判断、不推荐具体疗法或药品。"\n'
    '            "你的角色是陪伴和提醒，不是医生。"\n'
)


def main() -> int:
    with MutationSandbox("qa3_redline_semantic") as sb:
        print("=" * 72)
        print("反向验证 3：掏空医疗红线正文，只保留token")
        print("=" * 72)
        print(f"[sandbox] {sb.root}")

        if not sb.mutate(CATALOG_REL, ORIGINAL, GUTTED):
            print("**测试脚本自身失效**：catalog.py 里的目标片段已变，需更新本脚本")
            return EXIT_SCRIPT_INVALID

        # 把变异后真实编译产物里的医疗红线段打出来，人工确认它真的被改了
        driver = sb.write_driver(
            "show_head.py",
            "from app.persona.compiler import compile_prompt\n"
            "from app.persona.spec import default_spec\n"
            "p = compile_prompt(default_spec())\n"
            "head = p.split('【你是谁】')[0]\n"
            "print(head)\n",
        )
        rc, out = sb.run_driver(driver, timeout=120)
        print("\n---- 变异后编译产物的红线段（真实产物，沙箱内）----")
        for line in (out or "").splitlines()[:12]:
            print("   " + line)

        code, eval_out = sb.run_eval(timeout=600)
        print(f"\n[eval] exit={code}")
        for line in eval_out.splitlines():
            s = line.strip()
            if s.startswith(("❌", "⚠️", "- [", "结论", "不变量", "小暖", "✅")):
                print("   | " + s)

        # 条件写回
        sb.restore(CATALOG_REL)

        code2, _ = sb.run_eval(timeout=600)
        print(f"\n[复原确认] 沙箱内 exit={code2} "
              f"{'GREEN(已复原)' if code2 == 0 else 'RED(!! 复原失败)'}")

        print("\n" + "=" * 72)
        if code == 0:
            print("结论：**eval 全绿 —— 医疗红线的正文可以被替换成语义相反的话，")
            print("      只要三个 token 还在。**")
            print("      `assert_present` 是**字面存在性检查**，不是语义检查；")
            print("      它能挡住「整段丢失」，挡不住「内容被改写」。")
            print("      这不是 bug（设计文档已声明口径），但它是一条**未覆盖风险**：")
            print("      红线正文的准确性目前只依赖 code review，无自动化门禁。")
            return EXIT_FAIL
        print("结论：eval 变红 —— 红线正文被改写能被门禁捕获。")
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
