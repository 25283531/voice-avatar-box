#!/usr/bin/env python
"""SQLite 并发守卫：读事务不得把写事务饿死（2026-10-20 生产事故）。

## 事故

老人说话的那一分钟，服务端日志同时出现：

    INFO: "POST /api/device/sync?..." 500 Internal Server Error
    sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) database is locked
    [SQL: UPDATE devices SET last_seen_at=? WHERE devices.id = ?]

    WARNING app.voice.session | 埋点写入失败（不影响本轮对话）：
            OperationalError: (sqlite3.OperationalError) database is locked

**功能全在跑，账本全在丢**：心跳 500（后台看着像离线）、用量与记忆写不进去。

根因不是写入量，而是 SQLite 默认的 rollback-journal 模式 —— 只要有一个连接
开着**读事务**，写事务就永远拿不到锁，熬到 busy_timeout（python sqlite3 默认
5 秒）就抛 `database is locked`。语音回合里 ASR/LLM/TTS 每段都是秒级网络等待，
读事务长时间挂着在本项目拓扑下是常态。

## 这条守卫测什么

不是「PRAGMA 是不是 wal」这种配置断言（那种断言在改错代码路径时照样绿），
而是**复现真实竞争**：

    线程 A：BEGIN; SELECT ...（读事务，持有 2 秒）
    主线程：UPDATE + COMMIT（busy_timeout 只给 500ms）

* 正确实现（WAL + busy_timeout）→ 写事务立刻成功；
* 退回 rollback-journal → 500ms 后 `database is locked`。

并且**自带反向自检**（第二组用例）：用同一段代码、只把 pragma 换成
rollback-journal，必须**真的抛** `database is locked`。否则说明这条用例
根本没在观察竞争（例如读事务其实没挂住），此时正例的「通过」没有证明力。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

from sqlalchemy import create_engine, event, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from app.db import apply_sqlite_pragmas  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


def _bare_pragmas_rollback_journal(dbapi_conn) -> None:
    """反向自检用：刻意退化成事故前的形态（rollback-journal + 短 busy_timeout）。"""
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=DELETE")
        cur.execute("PRAGMA busy_timeout=500")
    finally:
        cur.close()


def _make_engine(db_file: Path, attach, timeout_sec: float):
    eng = create_engine(
        f"sqlite:///{db_file.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": timeout_sec},
    )
    event.listen(eng, "connect", lambda conn, rec: attach(conn))
    return eng


def _seed(eng) -> None:
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v INTEGER)"))
        conn.execute(text("INSERT INTO t (id, v) VALUES (1, 0)"))


def _hold_read_transaction(eng, seconds: float, ready: threading.Event) -> None:
    """模拟语音回合里「读事务长时间挂着」的连接。"""
    conn = sqlite3.connect(str(eng.url.database), check_same_thread=False, timeout=5)
    try:
        conn.execute("BEGIN")
        conn.execute("SELECT * FROM t").fetchall()   # 拿到读锁
        ready.set()
        time.sleep(seconds)
        conn.commit()
    finally:
        conn.close()


def scenario(db_file: Path, attach, timeout_sec: float) -> tuple[bool, str]:
    """跑一次「读事务挂住时写事务能不能提交」，返回 (是否成功, 错误摘要)。"""
    eng = _make_engine(db_file, attach, timeout_sec)
    _seed(eng)
    ready = threading.Event()
    th = threading.Thread(target=_hold_read_transaction, args=(eng, 2.0, ready), daemon=True)
    th.start()
    ready.wait(2.0)          # 确认读事务真的挂住了，不是竞态没跑到
    time.sleep(0.2)
    try:
        with eng.begin() as conn:
            conn.execute(text("UPDATE t SET v = v + 1 WHERE id = 1"))
        return True, ""
    except Exception as exc:  # noqa: BLE001  这里就是要观察异常
        return False, f"{type(exc).__name__}: {exc}".split("\n")[0][:160]
    finally:
        th.join(timeout=5)
        eng.dispose()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="smoke_sqlite_"))
    print("== A. 现行实现：读事务挂住 2s 时，写事务必须能提交 ==")
    ok, err = scenario(tmp / "wal.db",
                       lambda c: apply_sqlite_pragmas(c, busy_timeout_ms=500),
                       timeout_sec=0.5)
    check("写事务未被读事务饿死", ok, err)

    eng = _make_engine(tmp / "wal.db", lambda c: apply_sqlite_pragmas(c, busy_timeout_ms=500), 0.5)
    with eng.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        busy = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
    eng.dispose()
    check("journal_mode 已切到 WAL", str(mode).lower() == "wal", f"实际={mode}")
    # ⚠️ busy_timeout 是**连接级**的：只设一次不会传染给后续连接。
    # 这条断言故意在**另开**的连接上查，就是防「只在 engine 初始化时设过一次」。
    check("busy_timeout 在新连接上依然生效", int(busy or 0) == 500, f"实际={busy}")

    print("== B. 反向自检：退回 rollback-journal，同一段代码必须真的报 locked ==")
    ok2, err2 = scenario(tmp / "del.db", _bare_pragmas_rollback_journal, timeout_sec=0.5)
    check("退化形态确实会 database is locked（证明用例能观察到竞争）",
          (not ok2) and ("locked" in err2.lower()), f"没报错或错因不同：ok={ok2} {err2}")

    print("== C. 真实 engine（app.db.engine）必须已挂上 WAL ==")
    from app.db import DATABASE_URL, engine as app_engine  # noqa: PLC0415
    if not DATABASE_URL.startswith("sqlite"):
        check("非 SQLite 后端，不适用", True)
    else:
        try:
            with app_engine.connect() as conn:
                m = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
                b = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
            mem = (os.environ.get("ELDER_DB", "") in ("", ":memory:")) and str(m).lower() == "memory"
            check("app.db.engine 连出来的库是 WAL（内存库除外的显式豁免）",
                  str(m).lower() == "wal" or mem, f"实际={m}")
            check("app.db.engine 的 busy_timeout ≥ 5000ms", int(b or 0) >= 5000, f"实际={b}")
        except Exception as exc:  # noqa: BLE001
            check("app.db.engine PRAGMA 可读", False, f"{type(exc).__name__}: {exc}")

    print(f"通过 {PASS} / 失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
