"""A5 修正版：按**行范围**正确删除整条 no_medical_advice。

上一版用 ``src.rindex("HardGuardrail(", 0, i)`` 反查起点，落在 tuple 外层括号上，
删完剩下 ``)`` —— 那是**语法错误**，不是门禁行为。必须重做。
另附「删到 4 条但把 id/正文换掉」的变体，检验是否只能靠 hardline_count_is。

🛡️ 隔离说明
-----------
原实现用**固定的**共享沙箱 ``%TEMP%/qa3_sandbox``。固定路径在并发下
会互相踩踏（restore 从 pristine 拷回，会把对方的变异写进对方在用的文件）。
现改为 :mod:`mutation_sandbox` 的每次运行独有沙箱；工作区 ``catalog.py``
全程只读。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_sandbox import (  # noqa: E402
    EXIT_OK,
    MutationSandbox,
)

CATALOG_REL = "server/app/persona/catalog.py"


def main() -> int:
    with MutationSandbox("qa3_a5_fixed") as sb:
        print(f"[sandbox] {sb.root}（本次运行独有）")
        print("[说明] 工作区 catalog.py 全程只读。\n")

        catalog = sb.catalog
        before = catalog.read_text().count("HardGuardrail(")

        # 按括号配平删掉第一个 HardGuardrail( ... ) 整块
        lines = catalog.read_text().splitlines(keepends=True)
        start = next(i for i, ln in enumerate(lines) if ln.startswith("    HardGuardrail("))
        depth = 0
        end = -1
        for j in range(start, len(lines)):
            depth += lines[j].count("(") - lines[j].count(")")
            if depth == 0:
                end = j
                break
        if end < 0:
            print("**脚本失效**：括号配平失败，未做变异")
            return 2
        catalog.write_text("".join(lines[:start] + lines[end + 1:]))

        after = catalog.read_text().count("HardGuardrail(")
        print(f"删块：HardGuardrail( 出现 {before} -> {after}（含tuple 声明那1 处，故 -1）")

        # 语法自检：确保不是我自己写坏
        try:
            ast.parse(catalog.read_text())
            print("语法自检：OK（ast.parse 通过）")
        except SyntaxError as exc:
            print(f"**脚本失效**：变异后语法错误 {exc}")
            sb.restore(CATALOG_REL)
            return 2

        code, out = sb.run_eval(timeout=900)
        print("=" * 76)
        print(f"A5' 整条红线删除（语法正确版）  →  eval exit={code}")
        print("=" * 76)
        print(out[-4000:])

        # 条件写回
        sb.restore(CATALOG_REL)
        print("\n[沙箱内已复原；工作区从未被写入]")

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
