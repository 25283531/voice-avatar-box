"""QA 独立复核 —— 焦点 D2：反向验证「回归守护」是不是真的。

方法论：把工程师的修复**故意删掉一行**，确认对应的冒烟用例会红。
如果删了照样绿，说明那条守护是假的（根本没在守），必须报出来。

本脚本只做静态层面的一致性核对 + 给出可复现的删除方案，
真正的「删一行 -> 跑冒烟 -> 看是否变红」由 qa2_d2_mutate.sh 执行。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


# (文件, 相对行内容, 该行是干什么的)
GUARDS = [
    ("server/app/memory/router.py",
     "invalidate(user.id, agent_id)",
     "手动写入 POST /api/memory/facts 在 commit 之后补失效"),
    ("server/app/memory/router.py",
     "invalidate(row.user_id, row.agent_id)",
     "删除记忆 DELETE /api/memory/{id} 在 commit 之后补失效"),
    ("server/app/memory/service.py",
     "invalidate(user_id, agent_id)",
     "service._commit() 统一 commit + 失效"),
]


def main() -> int:
    print("── D2.1 定位「commit 后补失效」的关键行 ──")
    located = []
    for rel, snippet, why in GUARDS:
        p = ROOT / rel
        lines = p.read_text(encoding="utf-8").splitlines()
        hits = [(i + 1, ln) for i, ln in enumerate(lines)
                if snippet in ln and not ln.strip().startswith("#")]
        # 只认「在函数体内真正执行」的那一行，排除 import / 注释 / 函数定义默认值
        real = [(n, ln) for n, ln in hits if ln.strip() == snippet]
        check(f"D2.1 {rel}: 找到可删的 {why}", bool(real),
              f"命中行号={[n for n, _ in real]}（注释里另有 {[n for n, _ in hits] if len(hits) != len(real) else ''}）")
        for n, ln in real:
            located.append((rel, n, ln, why))

    print("\n  可删行清单（供 qa2_d2_mutate.sh 使用）：")
    for rel, n, ln, why in located:
        print(f"    {rel}:{n}  {ln.strip()}# {why}")

    print("\n── D2.2 静态检查：seqlock 三件套是否都在 ──")
    store = (SERVER / "app/memory/store.py").read_text(encoding="utf-8")
    trio = {
        "_versions 版本戳": "_versions" in store and "entry.version" in store,
        "_cache_put 版本比对": bool(re.search(r"if entry\.version != _versions\.get", store)),
        "after_commit 钩子": 'event.listen(db, "after_commit"' in store,
        "after_rollback 钩子": 'event.listen(db, "after_rollback"' in store,
        "once=True": store.count("once=True") >= 2,
        "钩子标记复位": 'info[_HOOK_KEY] = False' in store,
    }
    for k, v in trio.items():
        check(f"D2.2 {k}", v)

    print("\n── D2.3 静态检查：注入清洗接入点 ──")
    svc = (SERVER / "app/memory/service.py").read_text(encoding="utf-8")
    per = (SERVER / "app/memory/persona.py").read_text(encoding="utf-8")
    txt = (SERVER / "app/memory/textutil.py").read_text(encoding="utf-8")
    hooks = {
        "sanitize_for_prompt 定义": "def sanitize_for_prompt" in txt,
        "零宽字符被删": "\\u200b-\\u200f" in txt or "200b" in txt.lower(),
        "换行压成空格": "_NEWLINE_RE" in txt and '_NEWLINE_RE.sub(" "' in txt,
        "角色标记转义": "_ROLE_KV_RE" in txt,
        "标签删除": "_TAG_RE" in txt,
        "build_context 接入": "sanitize_for_prompt(h.content" in svc,
        "PROMPT_CONTENT_MAX 存在": "PROMPT_CONTENT_MAX" in svc,
        "persona._brief 接入": per.count("sanitize_for_prompt") >= 2,
    }
    for k, v in hooks.items():
        check(f"D2.3 {k}", v)

    print("\n── D2.4 未清洗的拼接点扫描（潜在漏网） ──")
    # 全仓库里把记忆正文往 prompt 里塞的地方
    suspects = []
    for py in (SERVER / "app").rglob("*.py"):
        if "__pycache__" in str(py):
            continue
        txt2 = py.read_text(encoding="utf-8", errors="replace")
        for i, ln in enumerate(txt2.splitlines(), 1):
            if "sanitize_for_prompt" in ln or ln.strip().startswith("#"):
                continue
            # 直接把 memory/content/hit 拼进 system prompt 的痕迹
            if re.search(r"(system_prompt|build_context|_brief|profile_text|"
                         r"persona_text|memory_block)", ln, re.I) and \
               re.search(r"f[\"']|\+.*content|\.content", ln):
                suspects.append(f"{py.relative_to(ROOT)}:{i}: {ln.strip()[:110]}")
    print("  可疑拼接点（需人工确认是否已在上游清洗）：")
    for s in suspects:
        print(f"    {s}")
    check("D2.4 未发现明显绕过清洗的 prompt 拼接点",
          not any("memory/service.py" in s or "persona.py" in s for s in suspects),
          f"可疑 {len(suspects)} 处")

    print()
    npass = sum(1 for _, ok, _ in results if ok)
    print("=" * 60)
    print(f"==== D2 静态段：{npass}/{len(results)} passed ====")
    for n, ok, d in results:
        if not ok:
            print(f"  FAIL: {n}  {d}")
    # 输出机器可读的可删行清单
    (ROOT / "out" / "qa2_mutatable_lines.txt").write_text(
        "\n".join(f"{rel}\t{n}\t{ln.strip()}" for rel, n, ln, _ in located),
        encoding="utf-8")
    print(f"\n[已写出] out/qa2_mutatable_lines.txt （{len(located)} 行可删）")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())