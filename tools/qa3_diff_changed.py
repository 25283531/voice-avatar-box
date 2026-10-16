"""反向验证：role_diff 的 ``changed`` 字段是否真的可用（Bug 2）。

要证明的是两件事，缺一不可
------------------------
1. **正向**：用户改了3 项traits + 1 个字段 -> ``changed=True`` 恰好 4 条，
   ``changed=False`` 恰好 6 条（10 个维度里未改的），``changed_count == 4``。
2. **反向**：``restore`` 之后 overlay 被清空 -> ``changed=True`` **必须降到 0**。
   这一条才是Bug 2 的要害：修之前 restore 之后 items 仍有 10 项，
   APP 按「items 非空 = 有改动」渲染就会误显示"你改了 10 项"。

只做正向不做反向，等于只证明了"新字段被填充了"，没证明"它解决了那个 bug"。
所以本脚本在 restore 之后**再断言一次 count == 0**，并且把修复前的
症状（items 非空但 count 为 0）显式打印出来对照。

用法：
    python tools/qa3_diff_changed.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    return bool(ok)


def run(code: str):
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{(Path(tempfile.gettempdir()) / 'qa3_diff.db').as_posix()}"
    env["ELDER_JWT_SECRET"] = "qa3-diff"
    env["PYTHONIOENCODING"] = "utf-8"
    tmp = tempfile.mkdtemp(prefix="qa3_diff_")
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = tmp
    drv = Path(tmp) / "d.py"
    drv.write_text(f"import sys\nsys.path.insert(0, {str(SERVER)!r})\n" + code,
                   encoding="utf-8")
    try:
        p = subprocess.run([PY, str(drv)], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=240)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def last_json(out: str):
    """抠出 ###JSON### 之后的 JSON。

    不能直接 json.loads(尾部)：stderr 的日志行会粘在后面（脚本把 stdout 与
    stderr 合在一起了）。逐步截短重试，与 qa3_edge.py 同一口径。
    """
    idx = out.rfind("###JSON###")
    if idx < 0:
        return None
    tail = out[idx + len("###JSON###"):].strip()
    try:
        return json.loads(tail)
    except json.JSONDecodeError:
        for cut in range(len(tail), 0, -1):
            try:
                return json.loads(tail[:cut])
            except json.JSONDecodeError:
                continue
    return None


PRELUDE = r'''
import json, os, sys
from app.db import init_db, SessionLocal
from app.schema_patch import run_all
from app.main import _seed, _seed_migration
from app import models
from app.persona import catalog, guardrails
init_db(); run_all(); _seed(); _seed_migration()
from app.routers.roles import derive_role, restore_role, role_diff
from app.deps import require_role
def mk(name, traits=None):
    with SessionLocal() as db:
        r = models.AIProvider(name=name, kind="", record_type="preset", owner_id=None,
                              traits=traits or {},
                              guardrails=guardrails.merge_stored(None), enabled=True)
        db.add(r); db.commit(); return r.id
def brief(rep):
    """把 RoleDiffOut 压成可 JSON 化的摘要。"""
    return {
        "items_len": len(rep.items),
        "changed_count": getattr(rep, "changed_count", None),
        "changed_true": [i.field for i in rep.items if getattr(i, "changed", None)],
        "changed_false": [i.field for i in rep.items if not getattr(i, "changed", None)],
    }
'''

BODY = r'''
res = {}
# 母版必须声明**全部 10 个**维度：diff 只列出母版有值的项，
# 母版只声明 1 个的话，"未修改项"就只有 1 条，测不出东西。
with SessionLocal() as db:
    parent_id = mk("母版", dict(catalog.DEFAULT_TRAITS))
with SessionLocal() as db:
    admin = db.get(models.User, 1)
    child = derive_role(parent_id, type("P", (), {"name": "派生", "inherit": False})(),
                        db, admin)
    cid = int(child.id)
# 改 3 个 trait + 1 个字段（system_prompt）
with SessionLocal() as db:
    row = db.get(models.AIProvider, cid)
    row.traits = {"verbosity": "verbose", "humor": 0.9, "warmth": "neutral"}
    row.system_prompt = "我叫小暖"
    db.commit()
with SessionLocal() as db:
    admin = db.get(models.User, 1)
    res["改过之后"] = brief(role_diff(cid, db, admin))
# restore：清空 overlay + system_prompt 回到母版 -> changed 必须归零
with SessionLocal() as db:
    admin = db.get(models.User, 1)
    restore_role(cid, type("P", (), {"confirm": True})(), db, admin)
with SessionLocal() as db:
    admin = db.get(models.User, 1)
    res["还原之后"] = brief(role_diff(cid, db, admin))
print("###JSON###" + json.dumps(res, ensure_ascii=False))
'''


def main() -> int:
    print("=" * 72)
    print("反向验证：role_diff 的 changed 字段（Bug 2）")
    print("=" * 72)
    db = Path(tempfile.gettempdir()) / "qa3_diff.db"
    if db.exists():
        db.unlink()
    rc, out = run(PRELUDE + BODY)
    rep = last_json(out)
    if not check("探针跑通", rc == 0 and rep is not None, out[-600:]):
        return 1

    edited = rep["改过之后"]
    restored = rep["还原之后"]

    print("\n── 正向：改了 3 个 trait + 1 个字段 ──")
    check("改了之后 changed=True 恰好 4 项", len(edited["changed_true"]) == 4,
          json.dumps(edited["changed_true"], ensure_ascii=False))
    check("改了之后 changed=False 恰好 7 项", len(edited["changed_false"]) == 7,
          json.dumps(edited["changed_false"], ensure_ascii=False))
    check("改了之后 changed_count == 4", edited["changed_count"] == 4,
          str(edited["changed_count"]))
    check("4 个 changed 项分别是 3 个 trait + system_prompt",
          sorted(edited["changed_true"]) == sorted(
              ["traits.verbosity", "traits.humor", "traits.warmth", "system_prompt"]),
          json.dumps(edited["changed_true"], ensure_ascii=False))

    print("\n── 反向：restore 之后必须归零（这才是 Bug 2 的要害）──")
    check("还原之后 changed=True 为 0 项", len(restored["changed_true"]) == 0,
          json.dumps(restored["changed_true"], ensure_ascii=False))
    check("还原之后 changed_count == 0", restored["changed_count"] == 0,
          str(restored["changed_count"]))
    check("还原之后 items 仍有 10 项且全是继承未改动的"
          "（traits 10 项 + system_prompt 因与母版相同而不入列）",
          restored["items_len"] == 10, str(restored["items_len"]))
    check("还原之后全部项都是 changed=False",
          len(restored["changed_false"]) == restored["items_len"] == 10,
          f"false={len(restored['changed_false'])} items={restored['items_len']}")
    print(f"\n  症状对照（修之前的 APP 误判来源）：还原后 items 非空 "
          f"({restored['items_len']} 项) 但 changed_count = "
          f"{restored['changed_count']}")
    print("  → APP 按 changed_count 显示「你改了 0 项」，不再误显示 10 项。")

    passed = sum(1 for _n, ok, _d in results if ok)
    total = len(results)
    print("\n" + "=" * 72)
    print(f"==== {passed}/{total} passed ====")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name}  -> {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
