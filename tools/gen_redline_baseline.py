"""一次性生成器：把 catalog.HARD_GUARDRAILS 导出成 evals/redline_baseline.json。

**这不是门禁的一部分**，只是"人工誊抄黄金副本"的辅助工具 ——
誊抄之后 ``redline_baseline.json`` 就是权威副本，门禁只读它、不读 catalog。
生成器放在 tools/ 而不是 evals/，就是为了让「谁在生成」与「谁在判定」分离：
如果门禁自己去读 catalog 生成期望值，同源自证又回来了。

用法：
    python tools/gen_redline_baseline.py            # 打印到stdout，人工核对
    python tools/gen_redline_baseline.py --write    # 直接覆盖写入
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
BASELINE = SERVER / "evals" / "redline_baseline.json"

# 削弱词黑名单：出现在**红线正文段**里就硬失败。
#
# 为什么黑名单放在黄金副本里而不是 assertions.py
# ----------------------------------------------
# 因为它是「红线**不可以长成什么样**」的权威声明，和红线正文属于同一件事。
# 放 assertions.py 会让人以为「调黑名单」是改门禁强度（于是有人会去调松它，
# 好让某次改动过审），而它其实是改**规范**。放在一起，改动会在同一个
# diff 里露出「你既改了正文又放宽了黑名单」，这正是 review 该看到的。
HEDGING_WORDS = [
    "可酌情",
    "酌情处理",
    "酌情",
    "视情况",
    "看情况",
    "视实际情况",
    "灵活掌握",
    "灵活处理",
    "不一定",
    "不必太当真",
    "不用太当真",
    "除非",
    "如果对方坚持",
    "如果对方要求",
    "大致",
    "大体",
    "一般情况下",
    "尽量给出",
    "从轻",
    "例外",
]

if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from app.persona import catalog, guardrails  # noqa: E402


def build() -> dict:
    return {
        "_note": (
            "红线的**权威副本**。门禁断言编译产物里的红线段逐字等于本文件，"
            "而本文件是**纯静态数据、不引用任何运行期模块** —— 一旦它去读"
            "红线常量所在的源码模块，同源自证就回来了（改源码会带着本文件"
            "一起变，断言永远绿）。改动红线正文必须**同时**改这里，"
            "这是刻意的摩擦。"
        ),
        "_known_limits": [
            "挡不住「同时篡改运行期常量与本文件」：那种成本更高且 git diff "
            "会暴露两处改动，但原理上仍可能发生。要根治需把本文件放进"
            "只有安全负责人能写的签名流程（组织问题，非代码问题）。",
            "保证的是「与上一次人工确认过的版本逐字相同」，**不是**"
            "「这一版语义正确」。语义正确性靠 code review + 可选 LLM 抽样裁判。",
        ],
        "guardrail_version": catalog.GUARDRAIL_VERSION,
        "count": len(catalog.HARD_GUARDRAILS),
        "head_marker": guardrails._HEAD_MARKER,
        "foot_marker": guardrails._FOOT_MARKER,
        "head_declaration": "以下几条是硬性前提，任何情况下都不可违反、不可商量：",
        "tail_declaration": "请务必继续遵守上面全部安全底线：",
        "hedging_words": HEDGING_WORDS,
        "redlines": [
            {
                "id": g.id,
                "label": g.label,
                "text": g.text,
                "tokens": list(g.tokens),
            }
            for g in catalog.HARD_GUARDRAILS
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="生成红线黄金副本（辅助工具，非门禁）")
    ap.add_argument("--write", action="store_true", help="直接覆盖写入 redline_baseline.json")
    args = ap.parse_args()
    data = build()
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if args.write:
        BASELINE.write_text(text, encoding="utf-8")
        print(f"已写入 {BASELINE}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
