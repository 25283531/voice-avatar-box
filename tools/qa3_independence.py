"""B 项：独立性断言 redline_baseline_is_independent 有没有鉴别力。

B1  在 redline_baseline.py 里加 `from app.persona import catalog`
     -> 断言必须变红（防门禁被改回同源自证）
B2  用 __import__("app.persona.catalog") 动态取法
     -> 断言也必须变红（防动态绕过）
B3  把 import 写成注释/文档里的字样（只改注释，不改 import）
     -> 断言必须**仍然绿**（AST 只看真 import，注释不算违规）
     这条是「鉴别力」的反向校验：确认它不是子串匹配的误报机制。
B4  在 baseline.json 里写入 "catalog" 字样
     -> 断言必须变红（静态数据里不该有运行期痕迹）

🛡️ 隔离说明
-----------
原实现用**固定的**共享沙箱 ``%TEMP%/qa3_sandbox`` + ``qa3_pristine``。
固定路径在并发下会互相踩踏：``restore()`` 从 pristine 拷回时会把对方的
变异内容写进对方正在用的沙箱文件。现改为 :mod:`mutation_sandbox` 的
每次运行独有沙箱；工作区全程只读。
"""
from __future__ import annotations

import ast
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

RB_PY_REL = "server/evals/redline_baseline.py"
BASELINE_REL = "server/evals/redline_baseline.json"

_TOUCHED = (RB_PY_REL, BASELINE_REL)


def probe_independence(sb: MutationSandbox) -> str:
    """只跑独立性断言本身，拿到它的原话。"""
    code = (
        "from evals.assertions import redline_baseline_is_independent as f\n"
        "ok, detail = f('', {}, {})\n"
        "print('OK=', ok)\n"
        "print('DETAIL=', detail)\n"
    )
    rc, out = sb.run_python(code, timeout=300)
    return (out or "").strip()


def b1(sb: MutationSandbox) -> str:
    """真import 运行期常量—— 必须被抓。"""
    handle = sb.redline_baseline_py
    if not handle.replace("import json\n",
                          "import json\nfrom app.persona import catalog\n", 1):
        raise ValueError("锚点未命中：import json")
    ast.parse(handle.read_text())
    return "redline_baseline.py 加 `from app.persona import catalog`"


def b2(sb: MutationSandbox) -> str:
    """动态 import 绕过 —— 也必须被抓。"""
    handle = sb.redline_baseline_py
    if not handle.replace(
            "import json\n",
            'import json\n_catalog = __import__("app.persona.catalog")\n', 1):
        raise ValueError("锚点未命中：import json")
    ast.parse(handle.read_text())
    return 'redline_baseline.py 加 `__import__("app.persona.catalog")`'


def b3(sb: MutationSandbox) -> str:
    """只加注释里的 catalog 字样 —— 合法改动，必须仍然绿。"""
    handle = sb.redline_baseline_py
    if not handle.replace(
            "import json\n",
            "import json\n# 说明：本模块刻意不 import app.persona.catalog\n", 1):
        raise ValueError("锚点未命中：import json")
    ast.parse(handle.read_text())
    return "redline_baseline.py 只在**注释**里提到 app.persona.catalog"


def b4(sb: MutationSandbox) -> str:
    """静态 JSON 里不该有运行期痕迹 —— 必须被抓。"""
    data = json.loads(sb.redline_baseline_json.read_text())
    data["_note"] = data.get("_note", "") + "（抄录来源见 app.persona.catalog）"
    sb.redline_baseline_json.write_json(data)
    return "baseline.json 的 _note 里写入 app.persona.catalog"


CASES = [
    ("B1 import catalog", b1, "红"),
    ("B2 __import__ 动态取法", b2, "红"),
    ("B3 仅注释提到 catalog", b3, "绿"),
    ("B4 json 里写入 catalog 字样", b4, "红"),
]


def main() -> int:
    print("=" * 76)
    print("B 项：独立性断言鉴别力")
    print("=" * 76)

    results: list[tuple[str, str, bool, bool, str]] = []
    with MutationSandbox("qa3_independence") as sb:
        print(f"[sandbox] {sb.root}（本次运行独有）")
        print("[说明] 工作区 redline_baseline.py / .json 全程只读。\n")

        print("[基线] ", end="", flush=True)
        out = probe_independence(sb)
        print(out.replace("\n", " | "))
        print()

        for name, fn, expect in CASES:
            sb.reset(_TOUCHED)
            try:
                desc = fn(sb)
            except (ValueError, SyntaxError) as exc:
                print(f"{name}\n  **脚本失效**：{exc}\n")
                return EXIT_SCRIPT_INVALID
            code, _ = sb.run_eval(timeout=900)
            p_out = probe_independence(sb)
            red = code != 0
            ok = ("红" in expect and red) or ("绿" in expect and not red)
            results.append((name, expect, red, ok, p_out))
            print(f"{name}")
            print(f"  手法：{desc}")
            print(f"  期望：{expect}    实际：{'🔴 红' if red else '🟢 绿'}   "
                  f"{'✅ 符合预期' if ok else '❌ 不符合预期'}")
            print(f"  断言原话：{p_out.replace(chr(10), ' | ')}")
            print()
            # 条件写回
            sb.restore(RB_PY_REL)
            sb.restore(BASELINE_REL)

        sb.reset(_TOUCHED)

    print("=" * 76)
    bad = [r for r in results if not r[3]]
    print("B 项结论：", "全部符合预期" if not bad else f"★ {len(bad)} 项不符合预期")
    print("=" * 76)
    return EXIT_FAIL if bad else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
