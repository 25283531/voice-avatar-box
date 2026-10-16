"""QA 独立验证：脏JSON 列 + archived/deleted 语义 + 缺列补齐的**读路径**。

这一组针对的是「表面修好但实际仍可能失效」的场景：
  1. ``ai_providers.traits`` / ``guardrails`` 列里存了**非dict** 的脏数据
     （老版本写入、人工改库、迁移中断）。这些列在读路径上有 7 处
     ``dict(row.traits or {})`` —— 若列里是 list或字符串会怎样？
  2. ``archived_at``（下架）与 ``deleted_at``（软删）两套语义是否被混淆。
  3. 补列后老行读出来是什么类型，取决于 DDL 与 ORM 是否对得上。
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


def run(code: str, db: Path):
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{db.as_posix()}"
    env["ELDER_JWT_SECRET"] = "qa3-dirty"
    env["PYTHONIOENCODING"] = "utf-8"
    tmp = tempfile.mkdtemp(prefix="qa3_dirty_")
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
    """抠出 ###JSON### 之后的内容。

    不能按行找：payload 里若含换行（或被日志插在中间），
    逐行匹配会拿到半截 JSON。改成「最后一次出现标记」之后全取。
    """
    idx = out.rfind("###JSON###")
    if idx < 0:
        return None
    tail = out[idx + len("###JSON###"):].strip()
    try:
        return json.loads(tail)
    except json.JSONDecodeError:
        # 尾巴可能还粘着日志行，逐步截短重试
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
from app.models import utcnow
from app.persona import guardrails
init_db(); run_all(); _seed(); _seed_migration()
def mk(name, traits=None, guard=None):
    with SessionLocal() as db:
        r = models.AIProvider(name=name, kind="", record_type="preset",
                              owner_id=None,
                              traits={} if traits is None else traits,
                              guardrails=(guardrails.merge_stored(None)
                                          if guard is None else guard),
                              enabled=True)
        db.add(r); db.commit(); return r.id
def dump(fn, *a, **kw):
    try:
        return {"ok": True, "val": fn(*a, **kw)}
    except Exception as exc:
        return {"ok": False, "err": f"{type(exc).__name__}: {exc}"}
'''


def t1_dirty_traits_crash() -> None:
    """合法 JSON 但**类型不对**（list/数字/字符串）时，角色读路径会不会崩。

    口径说明（踩过一次，值得写下来）：
    * 非法 JSON（``abc``）会让 SQLAlchemy 的 json_deserializer 在 SELECT
      阶段就抛 JSONDecodeError —— 那是**数据库损坏**级别的问题，整个 ORM
      读取都拿不到行，任何接口都会500。这类不算"代码对脏数据不健壮"，
      因为代码根本没机会执行到，本脚本因此不纳入判定。
    * 真正值得测的是「JSON 合法、但列的**类型**与代码假设不符」：
      ORM 会把它反序列化成 list / 数字 / 字符串，而读路径上有 7 处
      ``dict(row.traits or {})`` —— 那才是代码自己的责任范围。
    """
    print("\n── T1 类型不对的 traits 列（合法 JSON / 非 dict）──")
    db = Path(tempfile.gettempdir()) / "qa3_dirty_traits.db"
    if db.exists():
        db.unlink()
    code = PRELUDE + r'''
# ⚠️ 必须先建全部行、再统一 UPDATE：engine.begin() 持写锁时
#另开 Session 去 INSERT 会撞 SQLite 的 database is locked。
import sqlalchemy as sa
from app.db import engine
labels = ("list", "num", "str", "emptystr")
payloads = {"list": "[1,2,3]", "num": "42", "str": json.dumps("hello"),
            "emptystr": json.dumps("")}
ids = {lb: mk("dirty_" + lb) for lb in labels}
with engine.begin() as c:
    for lb in labels:
        c.execute(sa.text("UPDATE ai_providers SET traits=:t WHERE id=:i"),
                  {"t": payloads[lb], "i": ids[lb]})
from app.routers.roles import _parent_traits, _role_out, role_diff
res = {}
with SessionLocal() as db:
    for lb, i in ids.items():
        row = db.get(models.AIProvider, i)
        ro = dump(_role_out, db, row)
        df = dump(role_diff, i, db, db.get(models.User, 1))
        pt = dump(_parent_traits, db, row)
        res[lb] = {
            "raw_type": type(row.traits).__name__,
            "parent_traits": {"ok": pt["ok"], "err": pt.get("err", "")},
            # RoleOut / RoleDiffOut 是 Pydantic 对象，default=str 会把它们
            # 变成 repr 字符串 —— 那就看不出"崩没崩"了。这里只留结论。
            "role_out": {"ok": ro["ok"], "err": ro.get("err", "")},
            "diff": {"ok": df["ok"], "err": df.get("err", "")},
        }
print("###JSON###" + json.dumps(res, ensure_ascii=False, default=str))
'''
    rc, out = run(code, db)
    rep = last_json(out)
    if not check("T1 探针跑通", rc == 0 and rep is not None, out[-500:]):
        return
    for label, info in rep.items():
        pt, ro, df = info["parent_traits"], info["role_out"], info["diff"]
        check(f"T1 traits={label}（读出 {info['raw_type']}）：_parent_traits 不崩",
              pt["ok"], f"{pt.get('err','')}"[:100])
        check(f"T1 traits={label}：diff 不崩", df["ok"],
              f"{df.get('err','')}"[:100])


def t2_dirty_guardrails_crash() -> None:
    """脏 guardrails（非 dict）是否让承诺卡/软约束接口 500。"""
    print("\n── T2 类型不对的 guardrails 列（合法 JSON / 非 dict）──")
    db = Path(tempfile.gettempdir()) / "qa3_dirty_gr.db"
    if db.exists():
        db.unlink()
    code = PRELUDE + r'''
import sqlalchemy as sa
from app.db import engine
labels = ("list", "num", "str")
payloads = {"list": "[1,2]", "num": "42", "str": json.dumps("oops")}
ids = {lb: mk("gr_" + lb) for lb in labels}
with engine.begin() as c:
    for lb in labels:
        c.execute(sa.text("UPDATE ai_providers SET guardrails=:g WHERE id=:i"),
                  {"g": payloads[lb], "i": ids[lb]})
from app.persona import guardrails
from app.routers.roles import get_guardrail_card, toggle_soft_guardrail
res = {}
with SessionLocal() as db:
    admin = db.get(models.User, 1)
    for lb, i in ids.items():
        row = db.get(models.AIProvider, i)
        ms = dump(guardrails.merge_stored, row.guardrails)
        cd = dump(guardrails.card_items, i, row.name)
        res[lb] = {
            "raw_type": type(row.guardrails).__name__,
            "merge_stored": {"ok": ms["ok"], "err": ms.get("err", "")},
            "card": {"ok": cd["ok"], "err": cd.get("err", "")},
            "api_card": dump(get_guardrail_card, i, db, admin),
            "api_toggle": dump(toggle_soft_guardrail, i,
                               type("P", (), {"guardrail_id": "no_politics",
                                              "enabled": True})(), db, admin),
        }
        # 只留结论，不留 Pydantic 对象
        res[lb]["api_card"] = {"ok": res[lb]["api_card"]["ok"],
                               "err": res[lb]["api_card"].get("err", "")}
        res[lb]["api_toggle"] = {"ok": res[lb]["api_toggle"]["ok"],
                                 "err": res[lb]["api_toggle"].get("err", "")}
print("###JSON###" + json.dumps(res, ensure_ascii=False, default=str))
'''
    rc, out = run(code, db)
    rep = last_json(out)
    if not check("T2 探针跑通", rc == 0 and rep is not None, out[-500:]):
        return
    for label, info in rep.items():
        for fn in ("merge_stored", "card", "api_card", "api_toggle"):
            check(f"T2 guardrails={label}：{fn} 不崩",
                  info[fn]["ok"], f"{info[fn].get('err','')}"[:110])


def t3_archived_vs_deleted() -> None:
    """archived（下架）与 deleted（软删）语义是否被混淆。"""
    print("\n── T3 archived 与 deleted 的语义边界 ──")
    db = Path(tempfile.gettempdir()) / "qa3_arch.db"
    if db.exists():
        db.unlink()
    code = PRELUDE + r'''
from app.deps import require_role
from app.routers.roles import list_roles, list_presets
res = {}
with SessionLocal() as db:
    admin = db.get(models.User, 1)
    # 造一个预设母版 + 一个它的派生角色
    p = mk("母版", {"verbosity": "short"})
    with SessionLocal() as db2:
        child = models.AIProvider(name="派生", kind="", record_type="role",
                                  owner_id=admin.id, parent_id=p,
                                  traits={"verbosity": "verbose"},
                                  guardrails=guardrails.merge_stored(None),
                                  enabled=True)
        db2.add(child); db2.commit(); cid = child.id
    # 给母版打 archived（下架）
    with SessionLocal() as db3:
        row = db3.get(models.AIProvider, p)
        row.archived_at = utcnow(); db3.commit()
    with SessionLocal() as db4:
        res["母版下架后_读母版"] = dump(require_role, db4, p, admin)
        res["母版下架后_读派生"] = dump(require_role, db4, cid, admin)
        res["母版下架后_allow_archived读母版"] = dump(
            require_role, db4, p, admin, allow_archived=True)
        res["母版下架后_派生写"] = dump(require_role, db4, cid, admin, write=True)
        # Pydantic 对象无法 json 序列化（default=str 会把它变成 repr 字符串），
        # 所以在**子进程内**先把名字抠成 list再回传。
        pl = dump(list_presets, db4, admin)
        res["母版下架后_预设列表"] = {
            "ok": pl["ok"],
            "names": [x.name for x in pl["val"]] if pl["ok"] else None}
    # 现在改软删派生角色，看两个标记是否互相独立
    with SessionLocal() as db5:
        row = db5.get(models.AIProvider, cid)
        row.deleted_at = utcnow(); row.enabled = False; db5.commit()
    with SessionLocal() as db6:
        res["派生软删后_读派生"] = dump(require_role, db6, cid, admin)
        lst = dump(list_roles, "all", db6, admin)
        res["派生软删后_角色列表"] = {
            "ok": lst["ok"],
            "names": [x.name for x in lst["val"].mine] if lst["ok"] else None,
            "preset_names": ([x.name for x in lst["val"].presets]
                             if lst["ok"] else None),
        }
        r = db6.get(models.AIProvider, cid)
        res["派生软删后_标记"] = {"deleted": r.deleted_at is not None,
                                 "archived": r.archived_at is not None,
                                 "enabled": r.enabled}
print("###JSON###" + json.dumps(res, ensure_ascii=False, default=str))
'''
    rc, out = run(code, db)
    rep = last_json(out)
    if not check("T3 探针跑通", rc == 0 and rep is not None, out[-600:]):
        return
    r = rep
    check("T3 下架的母版默认不可见（archived_at生效）",
          not r["母版下架后_读母版"]["ok"],
          str(r["母版下架后_读母版"])[:90])
    check("T3 allow_archived=True 时下架母版仍可读（供 restore 用）",
          r["母版下架后_allow_archived读母版"]["ok"],
          str(r["母版下架后_allow_archived读母版"])[:90])
    check("T3 母版下架后，其派生角色仍可读（派生不随母版消失）",
          r["母版下架后_读派生"]["ok"], str(r["母版下架后_读派生"])[:90])
    pl = r["母版下架后_预设列表"]
    check("T3 下架的母版不出现在预设列表",
          pl["ok"] and "母版" not in (pl.get("names") or []),
          str(pl.get("names")))
    check("T3 软删的派生角色对 owner/管理员仍可读（审计线索不断）",
          r["派生软删后_读派生"]["ok"], str(r["派生软删后_读派生"])[:90])
    lst = r["派生软删后_角色列表"]
    check("T3 软删后角色列表不再包含它",
          lst["ok"] and "派生" not in (lst.get("names") or []),
          str(lst.get("names")))
    m = r["派生软删后_标记"]
    check("T3 软删只置 deleted_at，不动 archived_at（两标记独立）",
          m["deleted"] and not m["archived"], json.dumps(m))


def t4_derived_from_archived_parent() -> None:
    """母版被下架后，能否再从它派生新角色（应该不允许）。"""
    print("\n── T4 下架母版能否继续被派生 ──")
    db = Path(tempfile.gettempdir()) / "qa3_arch2.db"
    if db.exists():
        db.unlink()
    code = PRELUDE + r'''
from app.deps import require_role
from app.routers.roles import derive_role, restore_role
res = {}
with SessionLocal() as db:
    admin = db.get(models.User, 1)
    p = mk("可下架母版")
    with SessionLocal() as db2:
        r = db2.get(models.AIProvider, p); r.archived_at = utcnow(); db2.commit()
    with SessionLocal() as db3:
        res["下架后_派生"] = dump(derive_role, p,
                                 type("P", (), {"name": "x", "inherit": True})(),
                                 db3, admin)
    # 已存在的派生角色能否 restore（母版已下架）
    with SessionLocal() as db4:
        c = models.AIProvider(name="已有派生", kind="", record_type="role",
                              owner_id=admin.id, parent_id=p,
                              traits={"verbosity": "verbose"}, enabled=True)
        db4.add(c); db4.commit(); cid = c.id
    with SessionLocal() as db5:
        res["下架后_restore已有派生"] = dump(restore_role, cid,
                                           type("P", (), {"confirm": True})(),
                                           db5, admin)
print("###JSON###" + json.dumps(res, ensure_ascii=False, default=str))
'''
    rc, out = run(code, db)
    rep = last_json(out)
    if not check("T4 探针跑通", rc == 0 and rep is not None, out[-500:]):
        return
    check("T4 下架的母版不能再被派生新角色（archived 挡住派生入口）",
          not rep["下架后_派生"]["ok"], str(rep["下架后_派生"])[:110])
    check("T4 下架母版后，已派生角色仍能 restore（allow_archived=True 生效）",
          rep["下架后_restore已有派生"]["ok"],
          str(rep["下架后_restore已有派生"])[:110])


def main() -> int:
    print("=" * 72)
    print("QA 独立验证：脏 JSON 列 / archived 与 deleted 语义")
    print("=" * 72)
    for fn in (t1_dirty_traits_crash, t2_dirty_guardrails_crash,
               t3_archived_vs_deleted, t4_derived_from_archived_parent):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(f"{fn.__name__} 抛异常", False, f"{type(exc).__name__}: {exc}")
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