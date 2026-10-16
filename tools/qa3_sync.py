"""把工作区当前状态**只读复制**一份到独立沙箱（工作区是权威）。

🛡️ 隔离说明
-----------
改造前本脚本往一个**固定的**共享目录 ``%TEMP%/qa3_sandbox`` 拷文件，
并且 ``shutil.rmtree(pri)`` 会**删掉** ``%TEMP%/qa3_pristine``。

两个问题：
  1. 固定路径 = 并发踩踏。另一个 agent 同时跑qa3_golden_harness 时，
     本脚本会把它的沙箱文件覆盖掉，或把它的 pristine 删掉。
  2. 它是**手工同步步骤**：忘了跑，另一个脚本就基于过期副本做变异，
     结论不可信。

现在改为 :func:`mutation_sandbox.MutationSandbox`：每次运行建立
**独有**的沙箱，从工作区复制一份当前状态，全程只读工作区。
沙箱路径会打印出来，供需要手工排查时使用（默认退出即清理）。

用法：
    python tools/qa3_sync.py              # 建沙箱、复制、跑一次 eval 自检
    python tools/qa3_sync.py --keep       # 保留沙箱目录，打印路径
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

#: 需要确保存在于沙箱里的关键文件（相对 ``server/``）。
#: 保留原脚本的清单，作为「副本完整性」的自检项。
FILES = [
    "evals/assertions.py", "evals/redline_baseline.py",
    "evals/redline_baseline.json", "evals/perm_invariants.py",
    "evals/runner.py", "evals/suites/xiaozhi.json", "evals/suites/invariants.json",
    "app/persona/catalog.py", "app/persona/guardrails.py",
    "app/persona/compiler.py", "app/persona/spec.py",
    "app/models.py", "app/routers/roles.py", "app/schemas.py",
    "app/schema_patch.py", "app/deps.py", "app/main.py", "app/config.py",
]


def main() -> int:
    keep = "--keep" in sys.argv

    with MutationSandbox("qa3_sync") as sb:
        print(f"[sandbox] {sb.root}（本次运行独有）")
        print("[说明] 工作区只读，本脚本不写工作区任何文件。\n")

        # ---- 副本完整性自检----
        print("关键文件清单核对：")
        missing: list[str] = []
        for rel in FILES:
            target = sb.server / rel
            if target.exists():
                print(f"  ✅ {rel}")
            else:
                print(f"  ❌ 缺失: {rel}")
                missing.append(rel)
        if missing:
            print(f"\n**副本不完整**（缺 {len(missing)} 个文件）—— 变异测试不可靠")
            return EXIT_SCRIPT_INVALID
        print(f"\n全部 {len(FILES)} 个关键文件已就位。")

        # ---- 顺带跑一次 eval，确认这份副本本身是绿的 ----
        code, out = sb.run_eval(timeout=900)
        print(f"\n[自检] 沙箱内 eval exit={code} "
              f"{'GREEN(ok)' if code == 0 else 'RED(!! 副本基线不绿)'}")
        if code != 0:
            print(out[-2500:])
            return EXIT_FAIL

        if keep:
            print(f"\n[--keep] 沙箱保留在：{sb.root}")
            print("[注意] 该目录不会被自动清理，请手工删除。")
            # 阻止 __exit__ 清理
            sb.cleanup = lambda: None  # type: ignore[method-assign]

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
