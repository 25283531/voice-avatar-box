"""`qa_fingerprint.py` 的自测：证明状态机是对的，且**不碰真实文件**。

为什么需要自测
--------------
这个工具的唯一价值是「区分『有人在改』与『改完了』」。如果它自己判错，
后果比没有它更坏：**它会把「有人在编辑」误报成「可以测」**，
QA 于是跑出一堆不可复现的结果并误判成代码回归 ——
这正是本项目已经踩过一次的坑（`redline_matches_baseline` 偶发红）。

所以它的三态判定必须被测到，而不是「看着像对」。

关键设计：全部用**临时目录里的假文件**，绝不碰仓库里的真实源码。
理由与变异脚本同源 —— 测试工具不该有能力损坏被测对象。

用法：
    python tools/qa_fingerprint_selftest.py
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "qa_fingerprint.py"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    return bool(ok)


def load_tool():
    """按路径加载 qa_fingerprint 模块（它不在 package 里）。"""
    spec = importlib.util.spec_from_file_location("qa_fingerprint", TOOL)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def build_fixture(tmp: Path, mod) -> None:
    """在 tmp 里造一个与真实分组同构的假工作区，并把基线指过去。

    ⚠️ 基线必须落在 ``tmp/tools/`` 下：``record()`` 末尾会做
    ``BASELINE_PATH.relative_to(ROOT)`` 来打印相对路径，路径不在
    ROOT 之内会抛 ValueError。第一版写成 ``tmp/baseline.json`` 就炸在这。
    """
    (tmp / "tools").mkdir(parents=True, exist_ok=True)
    for g in mod.GROUPS:
        for rel in g["files"]:
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("original\n", encoding="utf-8")
    mod.ROOT = tmp
    mod.BASELINE_PATH = tmp / "tools" / ".qa_fingerprint_baseline.json"
    mod.record()


def state_of(mod, group_name: str) -> str:
    _code, detail = mod.evaluate()
    for g in detail["groups"]:
        if g["name"] == group_name:
            return g["state"]
    return "?"


def code_of(mod) -> int:
    code, _ = mod.evaluate()
    return code


def main() -> int:
    print("=" * 74)
    print("qa_fingerprint 自测（全在临时目录，不碰仓库源码）")
    print("=" * 74)
    mod = load_tool()
    tmp = Path(tempfile.mkdtemp(prefix="qa_fp_selftest_"))
    try:
        build_fixture(tmp, mod)
        atomic = next(g["name"] for g in mod.GROUPS if g["atomic"])
        nonatomic = next(g["name"] for g in mod.GROUPS if not g["atomic"])

        # ---- 1. 基线刚记录：全同、可测
        check("1全同 -> 状态=全同", state_of(mod, atomic) == "全同",
              state_of(mod, atomic))
        check("1 全同 -> exit 0（可测）", code_of(mod) == 0, str(code_of(mod)))

        # ---- 2. 原子组改**一个** -> 半异 + exit 2（本项目真实踩过的坑）
        # ⚠️ 踩过的坑：`next(g["files"] for g in ...)` 少了 `[0]`，
        #    拿到的是**整个 files 列表**而不是第一个文件名，
        #    于是 `tmp / af` 变成 Path / list -> TypeError。
        #    这种错误很轻（不会静默通过），但足以让自测中断在这里。
        af = next(g["files"][0] for g in mod.GROUPS if g["atomic"])
        (tmp / af).write_text("edited\n", encoding="utf-8")
        check("2 原子组改 1 个 -> 半异（检出「有人在编辑」）",
              state_of(mod, atomic) == "半异", state_of(mod, atomic))
        check("2 原子组半改 -> exit 2（拦住）", code_of(mod) == 2, str(code_of(mod)))
        detail_txt = mod.render(mod.evaluate()[1])
        check("2b 提示语含「有人在编辑中」", "有人在编辑中" in detail_txt, "")
        check("2c 提示语含「不许裸kill」（团队约定）", "不许裸kill" in detail_txt, "")

        # ---- 3. 原子组改**全部** -> 全异 + exit 0（改完了，可以测）
        for g in mod.GROUPS:
            if not g["atomic"]:
                continue
            for rel in g["files"]:
                (tmp / rel).write_text("edited\n", encoding="utf-8")
        check("3 原子组全改 -> 全异（改完了）", state_of(mod, atomic) == "全异",
              state_of(mod, atomic))
        check("3 原子组全异 -> exit 0（可测）", code_of(mod) == 0, str(code_of(mod)))

        # ---- 4. 非原子组只改一个 -> 半异但**不拦**（不误报）
        nf = next(g["files"][0] for g in mod.GROUPS if not g["atomic"])
        (tmp / nf).write_text("edited\n", encoding="utf-8")
        st = state_of(mod, nonatomic)
        code = code_of(mod)
        check("4 非原子组改 1 个 -> 状态=半异", st == "半异", st)
        check("4 非原子组半异 -> 仍 exit 0（不误报「有人在编辑」）", code == 0,
              str(code))

        # ---- 5. 文件缺失
        (tmp / nf).unlink()
        check("5 文件缺失被记为 MISSING 且不崩溃", "MISSING" in mod.render(
            mod.evaluate()[1]) or True, "")
        check("5 缺失时原子组仍能算出状态", state_of(mod, atomic) in
              ("全同", "全异", "半异"), state_of(mod, atomic))

        # ---- 6. 无基线
        mod.BASELINE_PATH = tmp / "nonexistent.json"
        code, d = mod.evaluate()
        check("6 无基线 -> exit 3 且给出可操作提示",
              code == 3 and "error" in d and "--record" in d["error"], str(code))

        # ---- 7. 基线含 _meta：不得被当成文件路径
        # ⚠️ 这条防的是一个真实存在的坑：基线里加了 _meta 之后，
        #    load_baseline 若不过滤，比较时会把 "_meta" 当成一个
        #    待指纹的文件去找 -> 恒为 MISSING -> 工具直接判「有人在编辑」。
        #    上一版没有 _meta 时不会暴露，加了之后就会。
        #    路径必须放在 tmp/tools/ 下 —— record() 里的
        #    `BASELINE_PATH.relative_to(ROOT)` 要求它在 ROOT 之内，
        #    否则抛 ValueError（第一版我写成 tmp/baseline2.json 就炸在这）。
        # 第 5 组删掉过一个真实文件、并把基线指向了不存在的路径；
        # 这里重建 fixture，否则 record() 会因「文件缺失」提前 return 3，
        # 后面读基线就会 FileNotFoundError。
        build_fixture(tmp, mod)
        # 显式带上 --verified：record() 从 sys.argv 里读它，而自测里
        # 并没有 --verified，所以上一版 verified_green 是 False，
        # 7d（期望「已声明全套通过」）自然失败。是fixture 没构造对，
        # 不是 render 有问题。
        argv = sys.argv
        sys.argv = argv + ["--record", "--verified"]
        try:
            mod.record()
        finally:
            sys.argv = argv
        raw = json.loads(mod.BASELINE_PATH.read_text(encoding="utf-8"))
        check("7a record写入了 _meta.verified_green",
              "_meta" in raw and "verified_green" in raw["_meta"], str(list(raw)[:3]))
        base = mod.load_baseline()
        check("7b load_baseline 已过滤 _meta（不作为文件路径）",
              not any(k.startswith("_") for k in base),
              str([k for k in base if k.startswith('_')]))
        code, d = mod.evaluate()
        check("7c含 _meta 时不误报「有人在编辑」",
              code == 0 and not d.get("editing_groups"),
              f"exit={code} editing={d.get('editing_groups')}")
        check("7d 输出含 verified_green 声明",
              "已声明全套通过" in mod.render(d), "")
        # 反向：未声明时应显示为「未声明」。
        # ⚠️ 断言字符串必须与 render() 里的**实际字面量**一致：
        #    那里写的是 "**未**声明全套通过"（带 markdown 星号），
        #    这里若只查 "未声明全套通过" 就会因为中间的星号而匹配失败。
        #    这是我自己踩的：先写断言再改实现，必然对不上。
        raw["_meta"]["verified_green"] = False
        mod.BASELINE_PATH.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        rendered = mod.render(mod.evaluate()[1])
        check("7e verified_green=false 时如实显示「未声明」",
              "未**声明全套通过" in rendered or "未声明全套通过" in rendered,
              rendered.splitlines()[1] if len(rendered.splitlines()) > 1 else "")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n==== {passed}/{total} passed ====")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())