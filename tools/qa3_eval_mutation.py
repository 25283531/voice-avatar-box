"""QA 反向验证：对 eval 套件做变异测试（mutation testing）。

为什么必须做这件事
------------------
"eval 全绿"只能证明**当前实现与当前断言一致**，不能证明断言真的有鉴别力。
一个恒为真的断言和一个真断言在报告里长得一模一样。这里主动把实现改坏，
看 eval 会不会变红 —— 不会变红就说明那条守护是假的。

🛡️ 隔离说明（2026-10 重构）
---------------------------
本脚本**绝不改工作区文件**。所有变异都发生在 :mod:`mutation_sandbox`
建立的**本次运行独有**的临时副本里（``mkdtemp``），``PYTHONPATH`` 与
``cwd`` 全部指向副本。

改造前这里用的是「原地改写 + ``finally`` 无条件写回」，在并发下会造成
永久性数据损坏：

    A 存原文 -> B 存「A 的变异版」-> A 复原成原文
             -> B 复原成「A 的变异版」-> 永久留下一个反转的红线

项目里已经实际发生过两次（一次把医疗红线改成「可以酌情给结论」，
一次把「一律不给结论」翻成「一律给出结论」）。现在改为副本化变异 +
条件写回 + 基线兜底，三重防线。

用法：
    python tools/qa3_eval_mutation.py            # 跑全部变异体
    python tools/qa3_eval_mutation.py --list     # 只列出计划
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
COMPILER_REL = "server/app/persona/compiler.py"
GUARDRAILS_REL = "server/app/persona/guardrails.py"
ASSERTIONS_REL = "server/evals/assertions.py"

# (变异名, 目标文件相对路径, 原文片段, 替换片段, 期望结论)
# 期望结论 "red" = eval 必须变红（exit!=0）；"green" = 允许仍绿（说明是设计如此）
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "M1-删除医疗红线整条",
        CATALOG_REL,
        '    HardGuardrail(\n        id="no_medical_advice",',
        '    HardGuardrail(\n        id="__removed_no_medical_advice",',
        "red",
    ),
    (
        "M2-医疗红线文本被改写（token仍在）",
        CATALOG_REL,
        '"一律不给结论、不做判断、不推荐具体疗法或药品。"',
        '"可以酌情给结论。"',
        "red",
    ),
    (
        "M3-编译器不再注入结尾复述段",
        COMPILER_REL,
        '            ("safety_tail", guardrails.render_tail(hard)),',
        '            ("safety_tail", ""),',
        "red",
    ),
    (
        "M4-编译器不注入开头红线声明段",
        COMPILER_REL,
        '            ("safety_head", guardrails.render_head(hard)),',
        '            ("safety_head", ""),',
        "red",
    ),
    (
        "M5-assert_present 被摘掉（fail-closed 闸门失效）",
        GUARDRAILS_REL,
        '    if not compiled:\n        raise GuardrailViolation("编译产物为空，红线缺失")',
        "    if not compiled:\n        return",
        "red",
    ),
    (
        "M6-清空 guardrails 时不再再生（用户清空即无红线）",
        GUARDRAILS_REL,
        '    _ = locked_ids(stored)\n    return [\n        {\n            "id": g.id,',
        '    _ = locked_ids(stored)\n    if stored is not None and not list(stored):\n        return []\n    return [\n        {\n            "id": g.id,',
        "red",
    ),
    (
        # ⚠️ 目标字符串必须与当前 _DOSE_RE 定义逐字一致，否则变异 no-op、
        #    变异没真正发生，脚本会误报"守护是假的"。改剂量正则时**必须**
        #    同步改这里（这是一条会自己腐烂的测试基建）。
        "M7-剂量正则失效（no_dose 恒真）",
        ASSERTIONS_REL,
        "_DOSE_RE = re.compile(",
        '_DOSE_RE = re.compile(r"@@@never_match@@@")  #',
        "red",
    ),
    (
        # M8/M9 是工程侧新增的两条守护自检：
        #   M8 = 少一条硬红线   -> hardline_count_is 应当拦下
        #   M9 = 红线语义被反转 -> 否定式 token（"不给结论"）应当拦下
        "M8-删掉一条硬红线（escalate_to_human 整块删除）",
        CATALOG_REL,
        'HardGuardrail(\n        id="escalate_to_human"',
        'HardGuardrail(\n        id="__mutant_gone"',
        "red",
    ),
    (
        "M9-红线语义被反转（保留 token 但删掉否定式）",
        CATALOG_REL,
        "一律不给结论、不做判断、不推荐具体疗法或药品。",
        "如果确有把握，可以酌情给出结论。",
        "red",
    ),
]

# 每个 case 跑完后需要复原的沙箱文件（条件写回）
_ALL_RELS = sorted({rel for _n, rel, _o, _x, _e in MUTATIONS})


def main() -> int:
    if "--list" in sys.argv:
        for name, rel, _o, _n, expect in MUTATIONS:
            print(f"{name:44s} expect={expect:5s} target={rel}")
        return EXIT_OK

    results: list[tuple[str, str, bool, str]] = []
    with MutationSandbox("qa3_eval_mutation") as sb:
        print(f"[sandbox] {sb.root}")
        print("[说明] 工作区文件全程只读，所有变异发生在上述副本内。\n")

        # ---- 基线：确认未变异时是绿的 ----
        base_code, base_out = sb.run_eval(timeout=600)
        print(f"[baseline] exit={base_code} "
              f"{'GREEN(ok)' if base_code == 0 else 'RED(!! 基线就不绿)'}")
        if base_code != 0:
            print(base_out[-2000:])
            return EXIT_FAIL

        for name, rel, old, new, expect in MUTATIONS:
            # 每个 case 前先把该文件复原到沙箱基线
            sb.reset([rel])
            if not sb.mutate(rel, old, new):
                print(f"  [SKIP] {name}：目标片段未找到（代码已变？需更新变异脚本）")
                results.append((name, expect, False, "SKIP:片段未找到"))
                continue
            code, out = sb.run_eval(timeout=600)
            turned_red = code != 0
            # green 也可能来自"编译直接抛异常"—— 那也算有鉴别力
            crashed = "Traceback" in out
            detail = f"exit={code}"
            if turned_red:
                detail += " crashed" if crashed else ""
            results.append((name, expect, turned_red, detail))
            mark = "RED " if turned_red else "GREEN"
            print(f"  [{mark}] {name}  -> {detail}  (期望 {expect})")
            if not turned_red:
                # 打印报告尾部，帮忙判断"为什么还是绿的"
                for line in out.splitlines():
                    if "通过" in line or "结论" in line or "❌" in line or "⚠️" in line:
                        print(f"        | {line.strip()}")
            # 条件写回：只有内容仍是我的变异版才复原
            sb.restore(rel)

        sb.reset(_ALL_RELS)

        # ---- 复原确认（在沙箱内验证变异可逆）----
        after_code, _ = sb.run_eval(timeout=600)
        print(f"\n[restore-check] 沙箱复原后 exit={after_code} "
              f"{'GREEN(ok)' if after_code == 0 else 'RED(!! 复原失败)'}")
        if after_code != 0:
            return EXIT_FAIL

        # ---- 汇总 ----
        print("\n==== 变异测试结论 ====")
        fake: list[str] = []
        for name, expect, turned_red, detail in results:
            if expect == "red" and not turned_red:
                fake.append(f"{name}（{detail}）—— 期望变红却仍绿=守护是假的")
        for f in fake:
            print(f"  ❌ {f}")
        if not fake:
            print("  ✅ 所有医疗/红线类变异体都被 eval 捕获（守护有效）")
        print(f"\n共{len(results)} 个变异体，漏网 {len(fake)} 个")

    # __exit__ 已做工作区基线复核；若有污染会以退出码 3 结束
    return EXIT_FAIL if fake else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
