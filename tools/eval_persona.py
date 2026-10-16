"""eval 套件 CLI（CI 调用）。

用法：
    python tools/eval_persona.py                 # 跑全部套件
    python tools/eval_persona.py xiaozhi         # 只跑某个套件
    python tools/eval_persona.py --live-sample 3 # 额外抽样真调 LLM（仅参考）

分级（用户拍板Q4）：
    * 医疗安全类失败 -> exit 1 -> CI 红
    * 长度 / 用词 / 重复类失败 -> 只打印 ⚠️ -> exit 仍 0

⚠️ **不要**用调低阈值的方式让 CI 变绿。report 类之所以只是报告，
是因为它们反映的是"回复风格可以更好"，而不是"系统不安全"；把 report
改成 block 会让人去调松阈值而不是去改对话风格，门禁就失去意义了。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"

# 必须把 server/ 放进 sys.path才能 import app.* —— tools/ 在仓库根，
# 不在包路径里（与其它 tools 脚本同样的处理）。
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from evals.runner import SUITES_DIR, run_suites  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="人格引擎 eval 套件")
    ap.add_argument("suites", nargs="*",
                    help="套件名（省略文件名后缀）；不填则跑全部")
    ap.add_argument("--live-sample", type=int, default=0,
                    help="抽样真调 LLM 的条数（仅参考，不进门禁）")
    args = ap.parse_args()

    if args.suites:
        paths = []
        for name in args.suites:
            p = SUITES_DIR / (name if name.endswith(".json") else f"{name}.json")
            if not p.exists():
                print(f"❌ 找不到套件 {name}（期望 {p}）")
                return 2
            paths.append(p)
    else:
        paths = sorted(SUITES_DIR.glob("*.json"))

    if not paths:
        print(f"❌ {SUITES_DIR} 下没有任何套件")
        return 2

    print(f"[eval] 套件 {len(paths)} 个：{'、'.join(p.name for p in paths)}")
    print(f"[eval] 分级：医疗安全类硬失败，长度/用词类仅报告\n")
    code, _reports = run_suites(paths, live_sample=args.live_sample)
    return code


if __name__ == "__main__":
    raise SystemExit(main())