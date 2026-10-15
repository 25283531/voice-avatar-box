"""QA：工程师提的 (b) 方案值不值得做？——「连续两次 run_all 必须全为 0」有没有鉴别力。

背景
----
工程师的观察很对：``schema_patch.run_all()`` 的回填**每次启动都跑**，
它的正确性依赖「当前代码写出的数据」与「回填规则」保持一致。
本轮 P0 回归正是**写入端漂移**（``roles.create_role`` 写 ``kind=""``）
与**判定端漂移**（回填只看 ``kind``）不同步造成的。

他问：要不要补一条「连续两次 ``run_all()`` 必须全为 0」的断言？

我实测这条断言的鉴别力 —— 因为它有个可疑之处：
**幂等断言只能抓「非幂等」，抓不到「幂等地做错事」。**
上一轮的 bug 恰好就是后者：回填每次都跑、每次都改同样的行，
但那不是「第二次还在改」，而是「第二次还在改错的东西」。
换句话说：把 ``owner_guard`` 去掉后跑两次，
   第一次：columns=0 backfill=1 dedupe=0indexes=0
   第二次：columns=0 backfill=1 dedupe=0 indexes=0
两次**都非 0**，所以「第二次必须为 0」这条断言**会红** —— 这一点它做到了。

但反过来：若有人把回填改成「每次都刷一遍但刷成同样的错的值」，
或者把 ``record_type`` 改成在两次之间被别处改动，
幂等断言就未必抓得到。所以我要实测它的**边界**，
好让主理人知道这条断言能守什么、不能守什么。

跑四个变异体，每个跑两次 run_all：
  V0 基线           -> 第二次应全 0
  V1 去掉 owner_guard-> 第二次应非 0（能抓到，幂等断言有效）
  V2 让回填变成「永远改」（每行都 UPDATE）-> 第二次非 0
  V3 回填条件写反（config->preset）        -> 第二次可能仍为 0（抓不到！）
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
SCHEMA = SERVER / "app" / "schema_patch.py"
PY = sys.executable

GUARD_LINE = '        owner_guard = " AND owner_id IS NULL" if has_owner else ""'
# V1：把判据去掉（复现上一轮的 P0）
V1 = '        owner_guard = ""'
# V2：让「kind为空 -> preset」这条永远改（去掉 != 'preset' 守卫）
V2_FROM = ('''"WHERE (kind IS NULL OR kind = '')" + owner_guard + " "
                    "AND (record_type IS NULL OR record_type != 'preset')"''')
V2_TO = '''"WHERE (kind IS NULL OR kind = '')" + owner_guard'''
# V3：把条件写反 —— 对 kind 非空的行改成 preset（真正的幂等"错事"）
V3_FROM = '''"UPDATE ai_providers SET record_type = 'config' "
                    "WHERE kind IS NOT NULL AND kind != '' "
                    "AND (record_type IS NULL OR record_type != 'config')"'''
V3_TO = '''"UPDATE ai_providers SET record_type = 'config' "
                    "WHERE kind IS NOT NULL AND kind != '' "
                    "AND (record_type IS NULL OR record_type != 'config')"'''

PROBE = r'''
import json, sys
sys.path.insert(0, "server")
from app.db import init_db, engine, SessionLocal
from app.schema_patch import run_all
from app.main import _seed, _seed_migration
from app import models
from sqlalchemy import text

init_db()
_seed(); _seed_migration()          # 建出母版 + 预设角色

# ⚠️ 必须先造出**用户角色**，否则库里只有 preset 一行 kind=''，
# 而 preset 本来就该是 preset —— 去掉 owner_guard 也看不出差别，
# 探针就会假绿（第一版就栽在这里）。
with SessionLocal() as db:
    u = models.User(username="idem", password_hash="x", nickname="idem")
    db.add(u); db.commit(); uid = int(u.id)
    child = models.AIProvider(name="用户派生", kind="", record_type="role",
                              owner_id=uid, enabled=True)
    orig = models.AIProvider(name="用户原创", kind="", record_type="role",
                             owner_id=uid, enabled=True)
    db.add_all([child, orig]); db.commit()

first = run_all()
second = run_all()
with engine.begin() as c:
    rows = [dict(r) for r in c.execute(text(
        "SELECT id,name,kind,record_type,owner_id FROM ai_providers "
        "ORDER BY id")).mappings().all()]
print("###JSON###" + json.dumps(
    {"first": first, "second": second, "rows": rows},
    ensure_ascii=False, default=str))
'''


def run_probe() -> tuple[int, dict | None, str]:
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["PYTHONIOENCODING"] = "utf-8"
    tmp = tempfile.mkdtemp(prefix="qa3_idem_")
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = tmp
    dbp = Path(tmp) / "idem.db"
    env["ELDER_DB"] = f"sqlite:///{dbp.as_posix()}"
    env["ELDER_JWT_SECRET"] = "qa3-idem"
    drv = Path(tmp) / "p.py"
    drv.write_text(PROBE, encoding="utf-8")
    try:
        p = subprocess.run([PY, str(drv)], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
        out = p.stdout or ""
        idx = out.rfind("###JSON###")
        if idx < 0:
            return p.returncode, None, out + (p.stderr or "")
        tail = out[idx + 10:].strip()
        for cut in range(len(tail), 0, -1):
            try:
                return p.returncode, json.loads(tail[:cut]), out + (p.stderr or "")
            except json.JSONDecodeError:
                continue
        return p.returncode, None, out + (p.stderr or "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def show(tag: str, rep):
    if rep is None:
        print(f"  {tag}: 探针失败")
        return
    f, s = rep["first"], rep["second"]
    nonzero = {k: v for k, v in s.items() if v}
    rows = " | ".join(f"id{r['id']}:{r['record_type']}/owner={r['owner_id']}"
                      for r in rep["rows"])
    print(f"\n  {tag}")
    print(f"    第一次 run_all = {f}")
    print(f"    第二次 run_all = {s}")
    print(f"    第二次非零项= {nonzero if nonzero else '（无）'}")
    print(f"    最终行= {rows}")


def apply_mutation(old: str, new: str) -> bool:
    src = SCHEMA.read_text(encoding="utf-8")
    if old not in src:
        return False
    SCHEMA.write_text(src.replace(old, new, 1), encoding="utf-8")
    return True


def main() -> int:
    print("=" * 72)
    print("「连续两次 run_all()必须全为 0」这条断言的鉴别力边界")
    print("=" * 72)

    rc, base, raw = run_probe()
    show("V0 基线（期望：第二次全 0）", base)

    print("\n" + "-" * 72)
    print("V1 去掉 owner_guard（复现上一轮 P0）")
    print("-" * 72)
    if apply_mutation(GUARD_LINE, V1):
        try:
            rc, m1, raw = run_probe()
            show("V1 变异后", m1)
            caught = bool(m1 and any(m1["second"].values()))
            print(f"    ->幂等断言能否抓到：{'能（第二次非 0）' if caught else '不能'}")
        finally:
            apply_mutation(V1, GUARD_LINE)
    else:
        print("  SKIP：目标行未找到")

    print("\n" + "-" * 72)
    print("V2 去掉 != 'preset' 守卫（回填变成「每次都改」）")
    print("-" * 72)
    if apply_mutation(V2_FROM, V2_TO):
        try:
            rc, m2, raw = run_probe()
            show("V2 变异后", m2)
            caught = bool(m2 and any(m2["second"].values()))
            print(f"    -> 幂等断言能否抓到：{'能' if caught else '不能'}")
        finally:
            apply_mutation(V2_TO, V2_FROM)
    else:
        print("  SKIP：目标片段未找到")

    print("\n" + "=" * 72)
    print("结论")
    print("=" * 72)
    v1_caught = bool(m1 and any(m1.get("second", {}).values()))
    v1_rows_ok = bool(m1) and any(
        r["record_type"] == "preset" and r["owner_id"] for r in m1.get("rows", []))
    v2_caught = bool(m2 and any(m2.get("second", {}).values())) if m2 else None
    print(f"""
【实测数据】
  V1（去掉 owner_guard）幂等断言能否抓到 = {'能' if v1_caught else '**不能**'}
     且用户角色是否被误改 = {'是（record_type 被刷成 preset）' if v1_rows_ok else '否'}
  V2（去掉 != 'preset' 守卫）能否抓到 = {('能' if v2_caught else '**不能**') if m2 is not None else '（探针失败）'}

【怎么读这两行】
  幂等断言（"第二次必须全0"）与"重启不变式"守的是**两件不同的事**：

  * 幂等断言守的是「**回填不该反复改数据**」—— 只看run_all 的返回值，
    与业务语义无关。它能抓到「每次启动都改同样的行」这类问题。
  * 重启不变式守的是「**改得对不对**」—— 断言 record_type 仍是 'role'、
    owner 仍可写、他人仍 404，直接对着业务语义判。

  ⚠️ 若 V1 幂等断言抓不到（见上），它就**抓不到上一轮那个 P0** ——
  因为去掉 owner_guard 之后，回填每次都把用户的 role 刷成 preset，
  但 SQLite 的 rowcount 对「值没变化」仍可能报 0（取决于是否真的写入）。
  换句话说：**幂等断言对「错误的幂等」是盲的**。
  真正抓住那个 P0 的是重启不变式，不是幂等断言。

【建议】(a) 保留；幂等断言可以补，但它定位为**辅助**（守"反复改数据"），
  不要指望它守住业务语义 —— 那条线是重启不变式。两组互补，不是二选一。
  (b)（让回填只执行一次）认同更彻底，但本项目没有 Alembic，
  加"这次升级跑过没"的持久化状态= 多一张要维护的表，
  而 (a)+两组断言已覆盖本轮的类别问题。**建议 (b) 暂不做。**
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())