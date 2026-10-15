"""C 项：假红摩擦评估 + 第三处副本清点。

C1 合法改动（修错别字）只改 catalog          -> 应红（提示同步副本）
C2 catalog + json 同步改（同一处错别字）       -> 应绿
C3 改版本号 catalog only                     -> 应红
C4 catalog + json 版本号同步-> 应绿
C5 只改 json 不改 catalog（方向反过来）         -> 应红（反向破坏也抓）
C6 副本被**手滑改坏**（json 里加个空格）        -> 应红（证明逐字比对无 trim 豁免）

另：清点红线正文/声明/marker 到底有几处需要同步改。

🛡️ 隔离说明
-----------
原实现用**固定的**共享沙箱 ``%TEMP%/qa3_sandbox`` + ``qa3_pristine``。
固定路径在并发下会互相踩踏（``restore()`` 从 pristine 拷回，会把对方的
变异内容写进对方正在使用的沙箱文件）。现改为 :mod:`mutation_sandbox`
的每次运行独有沙箱；工作区全程只读。
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
BASELINE_REL = "server/evals/redline_baseline.json"

_TOUCHED = (CATALOG_REL, BASELINE_REL)

TYPO_OLD = "你的角色是陪伴和提醒，不是医生。"
TYPO_NEW = "你的角色是陪伴和提醒，不是大夫。"      # 合法的错别字修正


def edit_catalog(sb: MutationSandbox, old: str, new: str) -> bool:
    """在沙箱副本的 catalog 里做一次替换。"""
    return sb.mutate(CATALOG_REL, f'"{old}"', f'"{new}"')


def edit_json(sb: MutationSandbox, mut) -> None:
    """在沙箱副本的黄金副本 JSON 上跑一个变换函数。"""
    data = json.loads(sb.redline_baseline_json.read_text())
    mut(data)
    sb.redline_baseline_json.write_json(data)


# ---------------------------------------------------------------- 六个 case

def c1(sb: MutationSandbox) -> str:
    edit_catalog(sb, TYPO_OLD, TYPO_NEW)
    return "只改 catalog：不是医生 -> 不是大夫"


def c2(sb: MutationSandbox) -> str:
    edit_catalog(sb, TYPO_OLD, TYPO_NEW)

    def mut(d):
        for r in d["redlines"]:
            r["text"] = r["text"].replace(TYPO_OLD, TYPO_NEW)
    edit_json(sb, mut)
    return "catalog + json 同步：不是医生 -> 不是大夫"


def c3(sb: MutationSandbox) -> str:
    edit_catalog(sb, 'GUARDRAIL_VERSION: Final = "2026-10-15.1"',
                 'GUARDRAIL_VERSION: Final = "2026-10-15.2"')
    return "只改 catalog：版本 2026-10-15.1 -> .2"


def c4(sb: MutationSandbox) -> str:
    edit_catalog(sb, 'GUARDRAIL_VERSION: Final = "2026-10-15.1"',
                 'GUARDRAIL_VERSION: Final = "2026-10-15.2"')
    edit_json(sb, lambda d: d.__setitem__("guardrail_version", "2026-10-15.2"))
    return "catalog + json 同步：版本 -> 2026-10-15.2"


def c5(sb: MutationSandbox) -> str:
    edit_json(sb, lambda d: d.__setitem__("guardrail_version", "2099-01-01"))
    return "只改 json：版本 -> 2099-01-01（源码不动）"


def c6(sb: MutationSandbox) -> str:
    """副本里正文尾部多一个空格 —— 逐字比对应当抓住，不该有 strip 豁免。"""
    def mut(d):
        d["redlines"][0]["text"] = d["redlines"][0]["text"] + " "
    edit_json(sb, mut)
    return "只改 json：医疗红线正文尾部加一个空格"


CASES = [
    ("C1 只改 catalog（合法错别字）", c1, "红"),
    ("C2 catalog+json 同步", c2, "绿"),
    ("C3 只改 catalog 版本号", c3, "红"),
    ("C4 catalog+json 同步版本号", c4, "绿"),
    ("C5 只改 json 版本号", c5, "红"),
    ("C6 只改 json（正文加空格）", c6, "红"),
]


def main() -> int:
    print("=" * 76)
    print("C 项：假红摩擦")
    print("=" * 76)

    with MutationSandbox("qa3_friction") as sb:
        print(f"[sandbox] {sb.root}（本次运行独有）")
        print("[说明] 工作区 catalog.py / redline_baseline.json 全程只读。")

        code, _ = sb.run_eval(timeout=900)
        print(f"[基线] eval exit={code}（期望 0）")
        print()

        for name, fn, expect in CASES:
            sb.reset(_TOUCHED)
            desc = fn(sb)
            code, out = sb.run_eval(timeout=900)
            red = code != 0
            ok = (expect == "红" and red) or (expect == "绿" and not red)
            reasons = re.findall(r"\] (\w+)\s+-> ([^\n]{0,150})", out)
            print(f"{name}")
            print(f"  手法：{desc}")
            print(f"  期望：{expect}   实际：{'🔴 红' if red else '🟢 绿'}   "
                  f"{'✅' if ok else '❌ 不符合预期'}")
            for a, d in reasons[:2]:
                print(f"    · [{a}] {d}")
            print()

        sb.reset(_TOUCHED)

        # ---------------- 第三处副本清点（只读，无需隔离）----------------
        print("=" * 76)
        print("第三处副本清点：红线相关字面量在几处需要同步？")
        print("=" * 76)
        probes = {
            "红线正文（5 条）": "不给医疗建议：对方问疾病",
            "开头声明": "以下几条是硬性前提",
            "结尾复述声明": "请务必继续遵守上面全部安全底线",
            "head marker": "【安全底线 · 不可协商】",
            "foot marker": "【安全底线 · 不可协商 · 复述】",
            "版本号": "2026-10-15.1",
        }
        srv = sb.server
        for label, needle in probes.items():
            hits: list[str] = []
            for p in srv.rglob("*"):
                if p.suffix not in (".py", ".json") or "__pycache__" in str(p):
                    continue
                try:
                    if needle in p.read_text(encoding="utf-8"):
                        hits.append(p.relative_to(srv).as_posix())
                except (OSError, UnicodeDecodeError):
                    continue
            runtime = [h for h in hits if h.startswith("app/")]
            gate = [h for h in hits if h.startswith("evals/")]
            print(f"\n  【{label}】")
            print(f"    运行期(app/) {len(runtime)} 处: {runtime}")
            print(f"    门禁(evals/) {len(gate)} 处: {gate}")
            print(f"    → 改这一项需要同步改的文件数：{len(runtime) + len(gate)}")

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
