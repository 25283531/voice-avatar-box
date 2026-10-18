"""回归门禁：会员制遗留的五张表**必须**在启动时被真正删掉。

它守什么（一句话）
------------------
``schema_patch.drop_membership_tables()`` 一旦被掏空（或存在性检测失效、
清单被改错），**本脚本必须变红** —— 而不是像 875b38c 当时那样：
删表函数整个删掉，全仓 CI 依然全绿。

为什么要单独写这一份（假绿的具体形态）
--------------------------------------
875b38c 落地时，没有任何一条断言碰过这五张表：

* ``tools/qa3_migration.py`` 的老库 DDL 根本不建这五张表，它虽然连跑三次
  ``run_all()``，删表分支**恒为空操作** —— 断言「第二次 run_all 全为 0」
  在删表逻辑**完全不存在**时也照样成立；
* MySQL job 连的是**全新** service DB，而当前 ORM 已经没有这五张表的映射，
  ``create_all`` 不会建它们 —— 于是那条 job 真实执行的 DROP 是 **0 条**。

也就是说：当时「删表成功」这件事**没有任何运行证据**。本脚本补的就是这个洞。
做法是自己**先把五张旧表造出来**（按 e264200^ 的历史结构，含出向外键），
再走真实入口，用「表还在不在」而不是「函数自称删了几张」作判据。

⚠️ 判据必须是「复查表是否存在」，不能只看返回值
------------------------------------------------
只看返回值会退化成「函数自称删了」——把清单改成空的、或存在性检测恒返回
"不存在"，都能让返回值看起来正常。所以每次删表都配一条
``inspect(engine).get_table_names()`` 的复查，并且额外钉一条
**保留表哨兵**（``chat_quota_config``）：证明删表没有扩大化。

两种方言都跑（这是本脚本与其它 qa3_* 最大的不同）
------------------------------------------------
其余 qa3_* 脚本都无条件 ``env["ELDER_DB"] = sqlite:///...``，会**静默覆盖**
CI 传入的 MySQL 连接串，因此只能挂在 SQLite job（见 server-ci.yml 的大段注释）。
本脚本刻意**尊重外部 ``ELDER_DB``**：

* 不设 / 设为 sqlite -> 用临时目录下的独立库文件，跑完删文件；
* 设为 mysql+pymysql -> 用同一账号**新建一个专属库** ``elder_droptables_test``，
  跑完 ``DROP DATABASE`` 删掉（不污染 CI 那个共享库）。

于是 CI 的 mysql-smoke job 挂上它之后，**MySQL 上真的会执行 5 条 DROP**
（此前是 0 条），补齐「MySQL 删表路径无人覆盖」这个洞。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Table
from sqlalchemy import event, inspect, text

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

#: 五张要被删掉的遗留表（与 schema_patch.DROPPED_TABLES 一致）
DROPPED = ("memberships", "membership_plans", "membership_orders",
           "membership_amount_range", "payment_config")
#: 保留表哨兵：**必须还在用**，用它证明删表没有扩大化
RETAINED = "chat_quota_config"

_results: list[tuple[str, bool, str]] = []


def ok(name: str, cond: bool, detail: str = "") -> bool:
    _results.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    return bool(cond)


def _mysql_admin_conn(url):
    import pymysql
    from sqlalchemy.engine import make_url
    u = make_url(url)
    # ⚠️ **不要传 charset** —— 它与认证无关。
    # MySQL 8 的 caching_sha2_password 认证码是
    #   XOR(SHA256(pw), SHA256(SHA256(SHA256(pw)), nonce))
    # 只吃 (口令, nonce)，PyMySQL 的 pymysql/_auth.py 里 charset 出现 0 次。
    # charset 只影响建连后的 SET NAMES。带上它会让人误以为「成功那步没传、
    # 失败那步传了」是差异变量 —— 这正是本 bug 当时最大的误导点。
    return pymysql.connect(host=u.host or "127.0.0.1", user=u.username or "root",
                           password=u.password or "", port=u.port or 3306,
                           autocommit=True)


def prepare_db() -> tuple[str, str | None]:
    """决定本轮用哪个库。返回 ``(连接串, 需清理的临时目录)``。

    ⚠️ MySQL 分支新建**专属库**而不是复用 CI 那个：本脚本会在库里留下
    五张已删表的残留（若断言失败），复用会让后续 job 的 schema 不干净，
    排查时更难判断「表是谁留下的」。
    """
    url = (os.environ.get("ELDER_DB") or "").strip()
    tmpdir = None
    if not url:
        tmpdir = tempfile.mkdtemp(prefix="qa3_drop_tables_")
        return "sqlite:///" + (Path(tmpdir) / "legacy.db").as_posix(), tmpdir
    if url.startswith("mysql"):
        from sqlalchemy.engine import make_url
        conn = _mysql_admin_conn(url)
        conn.cursor().execute(
            "CREATE DATABASE IF NOT EXISTS elder_droptables_test "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        conn.close()
        # ⚠️⚠️ 这里**绝不能用 str()** —— 这是本脚本曾经把 CI 跑红的原因
        # ------------------------------------------------------------------
        # SQLAlchemy 的 ``URL.__str__`` 默认 ``hide_password=True``，会把口令
        # 替换成**字面量** ``***``：
        #     str(make_url("mysql+pymysql://root:smoke-pass@h/d"))
        #       -> "mysql+pymysql://root:***@h/d"
        # 把它写回 os.environ 之后，app/db.py 的 engine 拿到的口令就是三个星号，
        # 必然 1045 "Access denied ... (using password: YES)"。
        #
        # 为什么当初极难定位：GitHub Actions 的 secret masking **也**把
        # CI 注入的真值打印成 ``***``，于是日志里「真的」和「假的」长得一模一样，
        # 肉眼无法区分。只有回到「对象上的属性 vs 字符串往返后的属性」这一层
        # 才看得见：``url.password == 'smoke-pass'`` 而 ``make_url(str(url)).password == '***'``。
        #
        # 正确的写法是显式要求不隐藏口令（SQLAlchemy >= 1.4）：
        #     make_url(url).set(database=...).render_as_string(hide_password=False)
        return make_url(url).set(database="elder_droptables_test").render_as_string(
            hide_password=False), None
    return url, None


def cleanup(url: str, dialect: str, tmpdir: str | None) -> None:
    if dialect == "mysql":
        try:
            conn = _mysql_admin_conn(url)
            conn.cursor().execute("DROP DATABASE IF EXISTS elder_droptables_test")
            conn.close()
        except Exception as exc:  # noqa: BLE001  清理失败不该掩盖断言结果
            print(f"  [warn] MySQL 测试库清理失败：{exc}")
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)


_LEGACY_DEFINED = False


def build_legacy_tables(engine) -> None:
    """按 e264200^ 的历史结构造出五张遗留表（含出向外键）+ 各插一行。

    用 SQLAlchemy Core 而不是写死 DDL：SQLite 与 MySQL 两种方言都能建，
    外键语法差异交给引擎，本脚本不必维护两份 DDL（也就不会像 qa3_migration
    的 OLD_DDL 那样只覆盖一种方言）。

    ⚠️ **定义只做一次**：``Table(...)`` 往 ``Base.metadata`` 里注册表名，
    第二次再调会抛 ``InvalidRequestError: Table 'x' is already defined``。
    两个场景都要"重新造出这五张表"，所以把「定义」与「建表 + 插数」分开 ——
    后者可重复调用（场景 B 就是先被场景 A 删掉、再建回来）。
    """
    global _LEGACY_DEFINED
    md = Base.metadata
    if not _LEGACY_DEFINED:
        Table("memberships", md,
              Column("id", Integer, primary_key=True),
              Column("user_id", Integer, ForeignKey("users.id")),
              Column("expires_at", DateTime, nullable=True))
        Table("membership_plans", md,
              Column("id", Integer, primary_key=True),
              Column("code", String(32)))
        Table("membership_orders", md,
              Column("id", Integer, primary_key=True),
              Column("code", String(4)),
              Column("user_id", Integer, ForeignKey("users.id")),
              Column("admin_id", Integer, ForeignKey("users.id"), nullable=True),
              Column("amount_cents", Integer))
        Table("membership_amount_range", md,
              Column("id", Integer, primary_key=True),
              Column("min_cents", Integer))
        Table("payment_config", md,
              Column("id", Integer, primary_key=True),
              Column("account", String(128)))
        _LEGACY_DEFINED = True
    md.create_all(bind=engine, tables=[md.tables[t] for t in DROPPED])
    with engine.begin() as conn:
        # ⚠️⚠️ 必须先造出被外键引用的 users 行，否则**只有 MySQL 会红**
        # ------------------------------------------------------------------
        # memberships.user_id / membership_orders.user_id|admin_id 都
        # `ForeignKey("users.id")`。插数时引用 users.id=1，就必须真有这一行。
        #
        # SQLite **默认不强制外键**（`PRAGMA foreign_keys` 默认 OFF），
        # 所以少了这一行本地照样 14/14 全绿；MySQL/InnoDB 立即强制，
        # 抛 `IntegrityError 1452 Cannot add or update a child row`。
        # 这就是「本地绿灯、CI 红灯」的一个真实样本。
        #
        # ⚠️ 顺序要求：**先删子表、再删父行**。
        # 场景 B 是在场景 A 之后重建同一批表；若删表逻辑被变异掏空 / 真的有回归，
        # 旧表和旧行都还在，此时 ``DELETE FROM users WHERE id=1`` 会因
        # memberships 仍引用着 user_id=1 而抛 FOREIGN KEY constraint failed ——
        # 脚本崩掉，后面的断言一条都跑不到。**红灯必须是断言的红，不是脚本的红**，
        # 否则真正该看的「★ A2 五张表全部不再存在」会被掩盖。
        for table in DROPPED:
            conn.execute(text(f"DELETE FROM {table}"))
        conn.execute(text("DELETE FROM users WHERE id = 1"))
        # ⚠️⚠️ 必须**显式写全每一列**，不能靠 default 兜：
        #   users 的 nickname / phone / email / is_admin / created_at 都是
        #   ``nullable=False``，它们挂的是 **Python 层 Column default** ——
        #   只在 ORM 走 INSERT 时生效；本脚本用裸 ``text()`` 插，default 不参与，
        #   于是 NOT NULL 直接违反（实测踩到：`NOT NULL constraint failed:
        #   users.nickname`）。这与方言无关，SQLite/MySQL 都会红。
        #   全列写死还有个好处：两种方言行为完全一致，不依赖各自的默认值语义。
        conn.execute(text(
            "INSERT INTO users (id, username, password_hash, nickname, phone, "
            "email, is_admin, created_at) VALUES "
            "(1, 'legacy_fk_sentinel', 'not-a-real-hash', '', '', '', 0, "
            "CURRENT_TIMESTAMP)"))
        # 插一行：确保「有数据的表也会一起消失」。
        # ⚠️ 上面的 DELETE 之所以必要：不清行会撞主键抛 IntegrityError，
        # 把「断言失败」变成「脚本崩了」，同样掩盖真正该看的那几条断言。
        conn.execute(text("INSERT INTO memberships (id,user_id) VALUES (1,1)"))
        conn.execute(text("INSERT INTO membership_plans (id,code) VALUES (1,'m1')"))
        conn.execute(text("INSERT INTO membership_orders (id,code,user_id,amount_cents)"
                          " VALUES (1,'A1B2',1,1234)"))
        conn.execute(text("INSERT INTO membership_amount_range (id,min_cents) VALUES (1,1000)"))
        conn.execute(text("INSERT INTO payment_config (id,account) VALUES (1,'acct')"))


def force_sqlite_fk_checks() -> None:
    """在 SQLite 上打开外键强制，让本地就能抓出 MySQL 才会犯的错。

    为什么需要：SQLite 的 ``PRAGMA foreign_keys`` **默认 OFF**，也就是说
    本地跑出来��「14/14 全绿」对「引用的父行存不存在」这件事**毫无分辨力**。
    真实缺陷正是这样溜过去的 —— 五张表插数引用 users.id=1 而 users 是空的，
    SQLite 静默放行、MySQL 立刻 1452。

    打开后 SQLite 才有和 MySQL 同等的约束力，本地即可复现。
    这个 PRAGMA 是**按连接**生效的，所以要挂在 engine 的 connect 事件上，
    一次性设给池里后续借出的每一条连接。
    """
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    # 已借出的连接不受上面这个 hook 影响，给当前连接也立刻补上
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")


def scenario_run_all(engine) -> None:
    """场景 A：走**真实启动入口** ``run_all()``（main.lifespan 调的就是它）。"""
    print("\n── 场景 A：run_all()（真实启动入口）──")
    build_legacy_tables(engine)
    before = set(inspect(engine).get_table_names())
    ok("A1 五张遗留表已按历史结构建出（含出向外键）",
       set(DROPPED) <= before, str(sorted(set(DROPPED) - before)))

    first = run_all()
    after = set(inspect(engine).get_table_names())
    remaining = sorted(set(DROPPED) & after)
    ok("★ A2 五张遗留表在启动后**全部不再存在**", not remaining, str(remaining))
    ok("★ A3 保留表哨兵仍在（删表没有扩大化）",
       RETAINED in after, f"{RETAINED} 也被删了")
    ok("A4 run_all 报告 membership_tables_dropped=5",
       first.get("membership_tables_dropped") == 5, json.dumps(first, ensure_ascii=False))

    second = run_all()
    after2 = set(inspect(engine).get_table_names())
    ok("★ A5 第二次 run_all 的删表计数为 0（幂等）",
       second.get("membership_tables_dropped") == 0,
       json.dumps(second, ensure_ascii=False))
    ok("A6 幂等重跑后保留表哨兵依然在", RETAINED in after2)


def scenario_direct(engine) -> None:
    """场景 B：直接调 ``drop_membership_tables()``（把函数本身钉住）。"""
    print("\n── 场景 B：drop_membership_tables()（函数本身）──")
    build_legacy_tables(engine)
    before = set(inspect(engine).get_table_names())
    ok("B1 五张遗留表已重建（场景 A 删掉后能再建回来）",
       set(DROPPED) <= before, str(sorted(set(DROPPED) - before)))

    first = drop_membership_tables()
    after = set(inspect(engine).get_table_names())
    ok("★ B2 直接调用后五张表全部不再存在",
       not (set(DROPPED) & after), str(sorted(set(DROPPED) & after)))
    ok("★ B3 直接调用返回 5", first == 5, str(first))
    ok("★ B4 保留表哨兵仍在", RETAINED in after, f"{RETAINED} 也被删了")

    second = drop_membership_tables()
    third = drop_membership_tables()
    ok("★ B5 第二次 / 第三次调用返回 0（幂等）",
       second == 0 and third == 0, f"2nd={second} 3rd={third}")


# ================================================================ 环境准备
# ⚠️ **顺序不能改**：必须在 ``import app.*`` 之前定好 ELDER_DB。
# app.db 在**模块导入期**就 create_engine，之后再改环境变量没有任何作用
# （其它 qa3_* 脚本为了绕开这点才改用子进程，本脚本不需要 —— 见模块头）。
URL, TMPDIR = prepare_db()
os.environ["ELDER_DB"] = URL
os.environ["ELDER_JWT_SECRET"] = os.environ.get("ELDER_JWT_SECRET") or "qa3-drop"
os.environ.setdefault("ELDER_DB_WAIT_SEC", "10")

from app.db import Base, engine, init_db          # noqa: E402  必须晚于上面的 env
from app.schema_patch import drop_membership_tables, run_all   # noqa: E402


def main() -> int:
    print("=" * 72)
    print("回归门禁：会员制遗留五张表必须在启动时被真正删掉")
    print("=" * 72)

    url, tmpdir, dialect = URL, TMPDIR, engine.dialect.name
    print(f"\n  方言：{dialect}")
    print(f"  库：{url}")

    try:
        init_db()
        force_sqlite_fk_checks()
        fresh = set(inspect(engine).get_table_names())
        ok("0 全新库里 ORM 不再建这五张表（它们只能来自老库）",
           not (set(DROPPED) & fresh), str(sorted(set(DROPPED) & fresh)))
        ok(f"0 保留表哨兵 {RETAINED} 已存在（哨兵承重的前提）",
           RETAINED in fresh, str(sorted(fresh))[:200])
        scenario_run_all(engine)
        scenario_direct(engine)
        # 哨兵承重：若 RETAINED 哪天从 ORM 里被移除，上面所有「哨兵仍在」
        # 的断言会变成空转（查不到就是不在），这里把它钉在元数据上。
        ok("9 保留表哨兵确实还是 ORM 里的活表（哨兵不空转）",
           RETAINED in Base.metadata.tables, str(sorted(Base.metadata.tables))[:200])
    except Exception as exc:  # noqa: BLE001  门禁自己崩了必须算失败，不能静默绿
        ok(f"脚本运行期抛异常（{dialect}）", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            engine.dispose()
        except Exception:  # noqa: BLE001
            pass
        cleanup(url, dialect, tmpdir)

    passed = sum(1 for _n, o, _d in _results if o)
    total = len(_results)
    print("\n" + "=" * 72)
    print(f"==== {passed}/{total} passed（{dialect}）====")
    for name, _o, detail in _results:
        if not _o:
            print(f"  FAILED: {name}  -> {detail}")
    return 1 if passed != total else 0


if __name__ == "__main__":
    raise SystemExit(main())
