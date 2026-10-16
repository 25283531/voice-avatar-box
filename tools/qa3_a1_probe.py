"""A1 复核：语义反转为何是**崩溃**变红，而不是某条断言变红。

现象：只改 catalog 正文（tokens 不动）-> eval 抛 GuardrailViolation 栈，
      整个runner 挂掉，报告根本没生成。

要回答两个问题：
  Q1 这是 fail-closed 自检（assert_present）在**如实报警**，
     还是它本身就是「让门禁成为唯一防线」——即黄金副本断言其实没参与？
  Q2 如果绕过者**同时**把 token 也改成反话（A2），assert_present 就不再报警，
     此时是否只剩黄金副本兜着？—— 已在主台验证 A2 变红。

做法：把 catalog 的 tokens 也改成反话，让 assert_present 通过，
     再看 eval 是否变红 —— 若仍红，说明黄金副本断言独立有效。

🛡️ 隔离说明（重要）
-------------------
改造前本脚本用的是一个**固定的**共享沙箱``%TEMP%/qa3_sandbox`` +
``%TEMP%/qa3_pristine``。固定路径意味着：两个 agent 同时跑本脚本就会
互相踩踏对方的"变异版"，``restore()`` 从 pristine 拷回时会把对方的
变异内容写进对方正在用的文件 —— **与原地改写工作区是同一类事故**，
只是位置从工作区搬到了临时目录。

现在改为 :mod:`mutation_sandbox` 的**每次运行独有**的 ``mkdtemp`` 沙箱：
A 与 B 各有各的副本，结构上不可能互相影响。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_sandbox import (  # noqa: E402
    EXIT_OK,
    MutationSandbox,
)

CATALOG_REL = "server/app/persona/catalog.py"

OLD_TEXT = '"一律不给结论、不做判断、不推荐具体疗法或药品。"'
NEW_TEXT = '"一律给出结论、给出判断、推荐具体疗法或药品。"'
OLD_TOK = 'tokens=("不给医疗建议", "医生", "家人", "不给结论"),'
NEW_TOK = 'tokens=("不给医疗建议", "医生", "家人", "给出结论"),'


def run_variant(sb: MutationSandbox, label: str, also_tokens: bool) -> None:
    """执行一个变体：跑 eval 并打印结果，再问一次 fail-closed 自检。"""
    code, out = sb.run_eval(timeout=900)
    crashed = "Traceback" in out
    print("=" * 76)
    print(f"{label}  →  eval exit={code}   {'★崩溃(无报告)' if crashed else '正常生成报告'}")
    print("=" * 76)
    if crashed:
        print(out[-900:])
    else:
        print(out[out.find("不变量"):][:1600])
    print()

    # 直接问 fail-closed 自检：绕过 assert_present 之后它还报不报警？
    probe = (
        "from app.persona import guardrails\n"
        "from app.persona.compiler import compile_prompt\n"
        "from app.persona.spec import default_spec\n"
        "c = compile_prompt(default_spec())\n"
        "print('COMPILE_OK, 产物长度', len(c))\n"
        "print('产物含「给出结论」:', '给出结论' in c)\n"
        "print('产物含「不给结论」:', '不给结论' in c)\n"
    )
    rc, po = sb.run_python(probe, timeout=300)
    print(f"  [fail-closed 自检 assert_present 视角] rc={rc}")
    print("  " + po.strip().replace("\n", "\n  "))
    print()
    sys.stdout.flush()


def main() -> int:
    with MutationSandbox("qa3_a1_probe") as sb:
        print(f"[sandbox] {sb.root}（本次运行独有）")
        print("[说明] 工作区 catalog.py 全程只读。\n")

        if not sb.mutate(CATALOG_REL, OLD_TEXT, NEW_TEXT):
            print("**脚本失效**：catalog 正文锚点未找到，请更新本脚本")
            return 2
        run_variant(sb, "A1 只改正文", also_tokens=False)
        sb.restore(CATALOG_REL)

        # A2：正文与 tokens 一起改 —— 让 assert_present 无话可说
        sb.reset([CATALOG_REL])
        if not sb.mutate(CATALOG_REL, OLD_TEXT, NEW_TEXT):
            print("**脚本失效**：catalog 正文锚点未找到，请更新本脚本")
            return 2
        if not sb.mutate(CATALOG_REL, OLD_TOK, NEW_TOK):
            print("**脚本失效**：catalog tokens 锚点未找到，请更新本脚本")
            return 2
        run_variant(sb, "A2 改正文+改tokens", also_tokens=True)
        sb.restore(CATALOG_REL)

    print("[沙箱已随with 结束自动清理；工作区从未被写入]")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
