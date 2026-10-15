"""专门量化after_rollback 监听器是否在长生命周期会话上累积。

mark_pending 每次都``event.listen(..., once=True)``挂**一对**监听器。
``after_commit`` 触发后自己被摘掉（once=True），于是下一轮mark_pending 会重挂 ——
这是设计意图。但 ``after_rollback`` 在「走的是 commit 而不是 rollback」时
**永远不触发**，因此永不摘除。每写一轮就多挂一个，直到会话关闭。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
DB_PATH = Path(tempfile.gettempdir()) / "elder_qa2_leak.db"
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["ELDER_JWT_SECRET"] = "qa2-leak"

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import models  # noqa: E402
from app.memory.store import NewFact, SqliteMemoryStore  # noqa: E402

engine = create_engine(os.environ["ELDER_DB"], future=True)
models.Base.metadata.create_all(engine)
SL = sessionmaker(bind=engine, future=True)

s0 = SL()
s0.add(models.User(username="leak", password_hash="x", nickname="甲"))
s0.commit()
uid = int(s0.query(models.User).first().id)
s0.close()

live: dict[str, int] = {"after_commit": 0, "after_rollback": 0}
fired: dict[str, int] = {"after_commit": 0, "after_rollback": 0}
orig_listen = event.listen


def counting_listen(target, ident, fn, **kw):
    key = str(ident)
    live[key] = live.get(key, 0) + 1

    def wrapper(*a, **k):
        fired[key] += 1
        live[key] -= 1
        return fn(*a, **k)
    return orig_listen(target, ident, wrapper, **kw)


N = 20
w = SL()
event.listen = counting_listen
try:
    for i in range(N):
        SqliteMemoryStore(w).add_fact(uid, None, NewFact(content=f"第{i}轮写入的记忆"))
        w.commit()
finally:
    event.listen = orig_listen

print(f"【{N} 轮写+commit，同一个长生命周期会话】")
print(f"  after_commit  累计注册 {live['after_commit'] + fired['after_commit']}，已触发 {fired['after_commit']}")
print(f"  after_rollback 累计注册 {live['after_rollback'] + fired['after_rollback']}，"
      f"已触发 {fired['after_rollback']}，**仍然挂在会话上 {live['after_rollback']} 个**")
if live["after_rollback"] > 0:
    print(f"  ⚠️ 泄漏确认：{N} 轮写后残留 {live['after_rollback']} 个 after_rollback 监听器")
    print("     影响：1) 内存随写入轮数线性增长；"
          "2) 之后任何一次 rollback 会被触发 N 次（幂等，不致错，但白跑）")
else:
    print("  ✅ 无残留")

# 验证残留是否真的会在rollback 时被批量触发
fired["after_rollback"] = 0
SqliteMemoryStore(w).add_fact(uid, None, NewFact(content="触发一次回滚"))
before = fired["after_rollback"]
w.rollback()
print(f"\n  一次 rollback 实际触发了 {fired['after_rollback']} 个残留回调"
      f"（注册残留 {live['after_rollback'] + fired['after_rollback']} 个）")
w.close()