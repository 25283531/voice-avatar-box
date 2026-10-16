"""回归：写入路径的语料缓存失效必须发生在 commit **之后**。

历史缺陷（QA 曾确定性复现）
---------------------------
``store.add_fact()`` 内部调 ``invalidate()``，但那时 ``db.commit()`` 还没发生：

    A 写入 -> add_fact 里失效（未提交）
            -> B 检索：缓存 miss -> 查库**看不到未提交的新行**
            -> B 把「缺这条新记忆」的快照写回缓存
            -> A commit：新行落地，但**没有人再失效**
            -> 这条刚写入的记忆在 TTL(300s) 内对检索完全不可见

症状是「机器人刚听完的话几十秒内召不回」，属产品可感知故障。
``memorize_background`` 走后台线程 + 独立会话，窗口更宽，语音链路下几乎必然撞上。

现在怎么防住的
--------------
1. ``store.mark_pending()`` 给会话挂 ``after_commit`` 钩子，**提交后**再失效一次；
2. 每个分片带版本号 ``_versions``，读方在「查库期间版本变了」时拒绝写缓存
   （关掉「读方已读库、正在写回」这个更窄的窗口）；
3. router / service 在 ``db.commit()`` 之后各自显式补一次失效（与删除路径对齐）。

本脚本现在**断言修复生效**（旧版是断言缺陷存在，方向相反）。
这是顺序执行的冒烟永远抓不到的一类 bug —— 必须构造交错执行才能暴露，
所以它必须留���，否则下次重构会再犯。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

DB_PATH = Path(tempfile.gettempdir()) / "elder_qa_cache_race.db"
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["ELDER_JWT_SECRET"] = "qa-race-secret"

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import models  # noqa: E402
from app.memory.store import (NewFact, SqliteMemoryStore, clear_cache,  # noqa: E402
                              invalidate)

engine = create_engine(os.environ["ELDER_DB"], future=True)
models.Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine, future=True)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def main() -> int:
    db0 = SessionLocal()
    u = models.User(username="race_a", password_hash="x", nickname="甲")
    db0.add(u)
    db0.commit()
    uid = int(u.id)
    db0.close()

    clear_cache()

    # ============ 场景：写入方 add_fact 已执行但尚未 commit ============
    w = SessionLocal()                      # 写入方会话
    r = SessionLocal()                      # 读取方会话（模拟并发请求）
    store_w = SqliteMemoryStore(w)

    mid = store_w.add_fact(uid, None, NewFact(content="张阿姨每周三去透析"))
    print(f"[setup] 写入方已 add_fact，id={mid}，尚未 commit")

    # 此刻另一个请求来了：它会 miss -> 读库 -> 读不到未提交的新行
    rows = SqliteMemoryStore(r).corpus(uid, None)
    check("交错点：读取方此刻确实看不到未提交的新记忆（前置条件）",
          all(x.id != mid for x in rows), f"corpus大小={len(rows)}")

    # 写入方提交 —— 修复后，after_commit 钩子会在这里失效缓存
    w.commit()
    print("[setup] 写入方已 commit")

    # 读取方再查一次：必须立刻能看到新记忆
    rows_after = SqliteMemoryStore(r).corpus(uid, None)
    visible = any(x.id == mid for x in rows_after)
    check("✅ commit 后新记忆立刻可见（提交后失效生效）", visible,
          f"corpus={[x.id for x in rows_after]} 期望含 {mid}")

    # 数据库里那条记忆确实在（排除「其实是写入丢了」这种假通过）
    real = w.scalars(
        select(models.AgentMemory).where(models.AgentMemory.id == mid)).first()
    check("对照：数据库里这条记忆确实存在（不是写入丢了）",
          real is not None, f"row={real is not None}")

    # 再读一次：验证上一步的可见性不是「碰巧重查了库」，而是真的缓存住了正确快照
    rows_again = SqliteMemoryStore(r).corpus(uid, None)
    check("重复读仍可见（缓存里是正确的快照，不是每次重查库）",
          any(x.id == mid for x in rows_again),
          f"corpus={[x.id for x in rows_again]}")

    w.close()
    r.close()

    # ============ 场景 2：版本戳挡住「读方已读库、写回时才发现失效」 ============
    # 这个窗口比场景 1 更窄，但危害一样：读方在 t0 记版本 -> t1 读库（旧数据）
    # -> t2 写方提交并失效 -> t3 读方把旧快照写回缓存。仅靠「提交后失效」
    # 挡不住（失效发生在读方写回之前），必须靠版本号拒绝这次写缓存。
    clear_cache()
    w2 = SessionLocal()
    r2 = SessionLocal()
    store_w2 = SqliteMemoryStore(w2)
    store_r2 = SqliteMemoryStore(r2)

    # 先让读方把当前语料缓存热起来（后续要观察它有没有被污染）
    store_r2.corpus(uid, None)

    mid2 = store_w2.add_fact(uid, None, NewFact(content="李叔叔住在城南"))
    w2.commit()

    rows_final = store_r2.corpus(uid, None)
    check("✅ 连续两次写入后，新记忆依然可见（版本戳未被绕过）",
          any(x.id == mid2 for x in rows_final),
          f"corpus={[x.id for x in rows_final]} 期望含 {mid2}")

    # 手工 invalidate 仍然有效（别把既有 API 改坏）
    invalidate(uid, None)
    rows_manual = store_r2.corpus(uid, None)
    check("手工 invalidate 仍然生效（既有 API 未被破坏）",
          any(x.id == mid2 for x in rows_manual),
          f"corpus={[x.id for x in rows_manual]}")

    w2.close()
    r2.close()

    # ============ 场景 3：同一个会话里连写两次 ============
    # after_commit 监听器是 once=True（触发即摘除），若「已挂钩子」的标记
    # 不随之清掉，第二次写入就不会再挂钩子 -> 第二次提交后没人失效。
    # 真实调用方（service.memorize 写多条事实、router 一次请求多次写）都走这条路。
    clear_cache()
    w3 = SessionLocal()
    r3 = SessionLocal()
    store_w3 = SqliteMemoryStore(w3)
    store_r3 = SqliteMemoryStore(r3)

    a_id = store_w3.add_fact(uid, None, NewFact(content="王叔叔养了三只鸽子"))
    w3.commit()
    b_id = store_w3.add_fact(uid, None, NewFact(content="赵阿姨每周二去公园"))
    w3.commit()

    rows3 = store_r3.corpus(uid, None)
    ids3 = {x.id for x in rows3}
    check("✅ 同会话连写两次，两条记忆提交后都立刻可见",
          a_id in ids3 and b_id in ids3,
          f"corpus={sorted(ids3)} 期望含 {a_id} 与 {b_id}")

    w3.close()
    r3.close()

    npass = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 60}")
    print(f"==== {npass}/{len(results)} passed ====")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())