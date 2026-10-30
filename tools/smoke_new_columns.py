"""老库升级冒烟 —— 守 ``schema_patch.NEW_COLUMNS`` 的每一条登记。

## 为什么需要这个脚本（这是本仓唯一会「部署即炸」的形态）

本项目只用 ``Base.metadata.create_all`` 建表，没有 Alembic。而 create_all
**不会给已存在的表补列**：

  * 全新部署 —— create_all 建的是新表，一切正常；
  * **生产老库** —— 表已经存在，新列不会被创建 ⇒ 第一次 SELECT 抛
    ``no such column: xxx`` ⇒ **服务起不来**。

唯一补救是 ``schema_patch.NEW_COLUMNS`` 那张「表 -> 列 -> DDL」清单。
漏登记一行的代价不是"少个字段"，是"线上服务起不来"。

## 为什么不能靠 ``smoke_schema_migrations.py``

那个脚本守的是 MAC 归一化、record_type 回填这类**回填逻辑**，
``grep NEW_COLUMNS`` 在它里面**一次都不出现** —— 补列这条路径它完全不管。
2026-10-30 加 `reminders.deleted_at / deleted_by / deleted_via` 三列时，
这条路径没有任何机器证据，只能靠人工读代码判断"看着对"。

## 这个脚本怎么判

对 ``NEW_COLUMNS`` 里的**每一条**登记：
  1. 先把库造成"没有这一列"的老库形态（整表重建，因为 SQLite 的
     ``ALTER TABLE ... DROP COLUMN`` 在列被外键引用时会直接报错）；
  2. 跑真实启动顺序：``create_all()`` -> ``schema_patch.run_all()``；
  3. 断言这一列回来了，且存量行读到的是 ``server_default`` 而不是 NULL。

第 3 条的后半句是关键：``ALTER TABLE ADD COLUMN x VARCHAR(16)`` **不带
DEFAULT** 时，老行读出的是 NULL，而 Pydantic 的 ``str`` 字段遇到 NULL 会
在序列化时炸掉 —— 这个坑本仓踩过一次（见 schema_patch 模块头的警告）。

## 边界

* 不绑端口，不需要服务端进程；
* 用临时库文件，跑完即删，不碰任何真实库；
* 只读仓库代码，不改任何源文件。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRV = ROOT / "server"

passed = 0
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"[ OK ] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}" + (f"  -> {detail}" if detail else ""))


def _ddl_of(col: tuple) -> str:
    """把 PRAGMA table_info 的一行还原成列定义。"""
    _cid, name, typ, notnull, dflt, pk = col
    s = f'"{name}" {typ}'
    if pk:
        s += " PRIMARY KEY"
    if notnull:
        s += " NOT NULL"
    if dflt is not None:
        s += f" DEFAULT {dflt}"
    return s


def _drop_column_by_rebuild(con: sqlite3.Connection, table: str, column: str) -> None:
    """从 ``table`` 里去掉 ``column`` —— 靠整表重建，不靠 DROP COLUMN。

    为什么不用 ``ALTER TABLE ... DROP COLUMN``：当被删的列带 ForeignKey 时，
    SQLite 会报
    ``error in table X after drop column: unknown column Y in foreign key definition``
    （2026-10-30 实测，删 ``reminders.deleted_by`` 时踩到）。
    整表重建没有这个限制，而且更接近"老库本来就没这一列"的真实形态。
    """
    info = con.execute(f'PRAGMA table_info("{table}")').fetchall()
    keep = [c for c in info if c[1] != column]
    assert len(keep) == len(info) - 1, f"{table}.{column} 不在表里，PRAGMA 没看到它"
    names = ", ".join(f'"{c[1]}"' for c in keep)
    con.execute(f'ALTER TABLE "{table}" RENAME TO "_tmp_rebuild"')
    con.execute(f'CREATE TABLE "{table}" (%s)' % ", ".join(_ddl_of(c) for c in keep))
    con.execute(f'INSERT INTO "{table}" ({names}) SELECT {names} FROM _tmp_rebuild')
    con.execute("DROP TABLE _tmp_rebuild")


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="elder_nc_")
    db_file = os.path.join(tmpdir, "probe.db")
    # ⚠️ 显式 ELDER_DB 是**逐字连接串**，不是路径（config.py 的约定）。
    # 传带盘符的 Windows 绝对路径会被拼成 `g://\...` 这种畸形 URL 而报
    # `Could not parse SQLAlchemy URL`（2026-10-30 实测）。
    os.environ["ELDER_DB"] = "sqlite:///" + db_file.replace("\\", "/")
    os.environ.setdefault("ELDER_SCHEDULER_INTERVAL", "3600")
    sys.path.insert(0, str(SRV))

    from app import db as app_db  # noqa: E402
    from app import models, schema_patch  # noqa: E402

    cols = schema_patch.NEW_COLUMNS
    check("NEW_COLUMNS 非空（清单存在）", bool(cols), "清单为空则本脚本无从判起")

    tables = set(app_db.Base.metadata.tables.keys())
    registered = {(t, c) for t, c, _s, _m in cols}

    # ---- 静态：每条登记都要对得上真实的表与列 ----
    # 这一组防的是"抄错表名/列名"—— 登记一条 reminders.deleted_att，
    # 补列时会静默地给表加一个没人读的列，而真正的列依然缺失。
    for table, column, _sqlite_ddl, _mysql_ddl in cols:
        check(f"登记 {table}.{column} 的表存在",
              table in tables, f"models 里没有表 {table}")
        if table in tables:
            model_cols = set(app_db.Base.metadata.tables[table].columns.keys())
            check(f"登记 {table}.{column} 的列存在",
                  column in model_cols,
                  f"{table} 的模型里没有 {column}，登记表与 models 已经不同步")

    # ---- 反向：模型里有、登记表里没有的"疑似新列" ----
    # 无法 100% 自动判定（分不清"老列"和"忘了登记的新列"），所以只对
    # 带 server_default 且非主键的列做提示，不 fail —— 避免变成噪音门禁。
    suspect: list[str] = []
    for table in tables:
        t = app_db.Base.metadata.tables[table]
        for col in t.columns:
            if col.primary_key or col.foreign_keys and col.nullable:
                continue
            if col.server_default is not None and (table, col.name) not in registered:
                if col.name in {"created_at", "updated_at", "id"}:
                    continue
                suspect.append(f"{table}.{col.name}")
    if suspect:
        print(f"[ .. ] 提示（不判失败）：这些列带 server_default 但没登记补列，"
              f"若它们是新加的，老库升级会缺列 -> {suspect[:8]}")

    # ---- 建库 + **先写一条存量行** ----
    #
    # ⚠️ 顺序很重要，第一版这里写错了：存量行必须在**退化之前**插入。
    # 如果先升级再插行，读到的是 ORM 的**客户端** default（`mapped_column(default="")`），
    # 而不是 ``ALTER TABLE ADD COLUMN`` 的 ``server_default`` —— 于是"DEFAULT 漏了"
    # 这个最危险的坑**根本验不出来**（实测：把 deleted_via 的 DDL 改成不带 DEFAULT，
    # 第一版仍然 120 项全绿）。
    #
    # 真实场景就是「老库里先有数据，然后升级」，所以顺序必须是：
    #   create_all -> 插存量行 -> 退化掉新列 -> 升级补列 -> 读存量行看默认值
    app_db.Base.metadata.create_all(bind=app_db.engine)
    app_db.engine.dispose()

    from sqlalchemy import select  # noqa: E402

    with app_db.SessionLocal() as s:
        u = models.User(username="nc_probe", password_hash="x")
        s.add(u)
        s.flush()
        dev = models.Device(device_id="aa:bb:cc:dd:ee:99", name="nc_dev", owner_id=u.id)
        s.add(dev)
        s.flush()
        rem = models.Reminder(device_id=dev.id, title="老库存量提醒",
                              times_of_day=["08:00"])
        s.add(rem)
        s.commit()
        rem_id, dev_id = rem.id, dev.id
    app_db.engine.dispose()

    # ---- 逐条：退化 -> 真实启动顺序 -> 断言补回来 ----
    by_table: dict[str, list[str]] = {}
    for table, column, _s, _m in cols:
        by_table.setdefault(table, []).append(column)

    con = sqlite3.connect(db_file)
    for table, column in registered:
        if table not in tables:
            continue
        try:
            _drop_column_by_rebuild(con, table, column)
        except Exception as exc:  # noqa: BLE001
            check(f"退化 {table}.{column}（造出老库形态）", False, repr(exc))
    con.commit()
    dropped = {t: [c for c in cs] for t, cs in by_table.items() if t in tables}
    con.close()

    # 退化之后存量行必须还在（整表重建时把数据带过去了）
    con = sqlite3.connect(db_file)
    still = con.execute("SELECT COUNT(*) FROM reminders WHERE id=?", (rem_id,)).fetchone()[0]
    con.close()
    check("退化后存量行仍在（老库确实有数据要升级）", still == 1, f"count={still}")

    app_db.engine.dispose()
    # 真实启动顺序：init_db() 的 create_all 先建新表（新表不需要补列），
    # 然后 main.py 才调 schema_patch.run_all() 补列/补索引。
    app_db.Base.metadata.create_all(bind=app_db.engine)
    app_db.engine.dispose()
    report = schema_patch.run_all()
    check("run_all 报告里确有补列动作",
          int(report.get("columns_added", 0)) >= len(registered),
          f"columns_added={report.get('columns_added')} 期望 >= {len(registered)}")

    con = sqlite3.connect(db_file)
    for table, columns in dropped.items():
        info = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
        for column in columns:
            check(f"老库升级后 {table}.{column} 被补回", column in info,
                  f"{table} 现有列里没有 {column}")
    con.close()

    # ---- 存量行必须读到 server_default，而不是 NULL ----
    con = sqlite3.connect(db_file)
    for table, columns in dropped.items():
        # 只对"我们真的插了行"的表做默认值检查；其余表没有数据可查。
        if table != "reminders":
            continue
        model = app_db.Base.metadata.tables[table]
        cols_sql = ", ".join(f'"{c}"' for c in columns)
        row = con.execute(
            f'SELECT {cols_sql} FROM "{table}" WHERE id = ?', (rem_id,)).fetchone()
        for column, value in zip(columns, row):
            # ⚠️ 期望值取决于**列本身可不可空**，不能一律要求非 NULL：
            #   * 不可空列（如 deleted_via VARCHAR NOT NULL DEFAULT ''）——
            #     DDL 漏了 DEFAULT 就会让老行读成 NULL，而 Pydantic 的 str
            #     字段遇到 NULL 会在序列化时炸掉，必须拦住；
            #   * **可空列**（deleted_at / deleted_by）—— NULL 就是正确值，
            #     它正是"这条提醒没被删除"的语义。要求它有默认值反而是错的。
            # 第一版断言一律要求非 NULL，于是 deleted_at/deleted_by 假红两条。
            nullable = model.columns[column].nullable
            if nullable:
                check(f"存量行 {table}.{column} 为可空列且读到 NULL（符合语义）",
                      value is None, f"读到 {value!r}，可空列应为 NULL")
            else:
                check(f"存量行 {table}.{column} 读到默认值而非 NULL",
                      value is not None, f"读到 {value!r}（多半是 DDL 漏了 DEFAULT）")
    con.close()

    # ---- 走一次真实 ORM 查询：线上第一次 SELECT 就是这条路 ----
    with app_db.SessionLocal() as s:
        r = s.scalar(select(models.Reminder).where(models.Reminder.id == rem_id))
        check("ORM 能读出老库升级后的提醒", r is not None and r.title == "老库存量提醒",
              repr(r))
        if r is not None:
            check("新增的 is_alive() 在存量行上为 True", r.is_alive() is True, repr(r.deleted_at))
            a = models.ReminderAudit(
                device_id=dev_id, reminder_id=r.id, action="delete",
                actor_type="user", actor_name="nc", via="web",
                title=r.title, summary="冒烟写入",
            )
            s.add(a)
            s.commit()
            check("审计表在老库上升级后可写入", a.id is not None, repr(a.id))

    shutil.rmtree(tmpdir, ignore_errors=True)

    print("=" * 60)
    print(f"通过 {passed} 项，失败 {failed} 项")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
