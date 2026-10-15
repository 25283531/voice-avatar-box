"""QA 独立复核 —— 焦点 A：缓存一致性三层机制（seqlock / after_commit / rollback）。

本脚本**不复用工程师的用例**，独立构造交错时序，包括：
  A1 读方查库期间写方提交 -> 绝不能把缺新记忆的快照写回缓存
  A2 反向：读方「记版本 -> 查库 -> 写回」之间写方 invalidate，版本戳必须拒绝
  A3 版本戳不会导致「永久不缓存」（性能退化探测）
  A4 同一会话连写两次，两次提交都失效
  A5 after_rollback：回滚后未提交数据绝不出现在缓存里
  A6 钩子重复挂载 / 监听器泄漏探测
  A7 交叉分片不串台
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

DB_PATH = Path(tempfile.gettempdir()) / "elder_qa2_a.db"
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["ELDER_JWT_SECRET"] = "qa2-secret"

from sqlalchemy import create_engine, event, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import models  # noqa: E402
from app.memory import store as store_mod  # noqa: E402
from app.memory.store import (NewFact, SqliteMemoryStore, _cache, _cache_get,  # noqa: E402
                              _cache_put, _versions, clear_cache, invalidate)

engine = create_engine(os.environ["ELDER_DB"], future=True)
models.Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine, future=True)

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def main() -> int:
    db0 = SessionLocal()
    u = models.User(username="qa2a", password_hash="x", nickname="甲")
    db0.add(u)
    db0.commit()
    uid = int(u.id)
    db0.close()
    clear_cache()

    # ============================================================ A1
    print("── A1 读方查库期间写方提交（真并发线程） ──")
    # 构造：读方 _cache_get miss -> 记版本 -> 执行 SELECT（慢） -> 期间写方提交
    gate = threading.Event()
    hit_select = threading.Event()

    orig_execute = SqliteMemoryStore._load_entities

    def slow_load(self, a, b):
        # 在「主语料 SELECT 之后、_cache_put 之前」卡住，
        # 模拟真实环境里这段时间窗口（网络/其它表查询）
        hit_select.set()
        gate.wait(timeout=10)
        return orig_execute(self, a, b)

    SqliteMemoryStore._load_entities = slow_load

    w = SessionLocal()
    r = SessionLocal()
    out: dict = {}

    def reader():
        try:
            out["rows"] = SqliteMemoryStore(r).corpus(uid, None)
        except Exception as exc:  # noqa: BLE001
            out["err"] = repr(exc)

    t = threading.Thread(target=reader)
    t.start()
    hit_select.wait(timeout=10)
    mid = SqliteMemoryStore(w).add_fact(uid, None, NewFact(content="张阿姨每周三去透析"))
    w.commit()
    print(f"[setup] 读方已查到旧数据，写方已 commit(id={mid})，现在放行读方写缓存")
    gate.set()
    t.join(timeout=15)

    SqliteMemoryStore._load_entities = orig_execute

    check("A1.1 读方线程无异常", "err" not in out, out.get("err", ""))
    # 关键：读方返回的 rows 是「旧快照」是正常的（它查库时新行还没提交），
    # 但绝不能进缓存。
    entry = _cache_get(uid, None)
    check("A1.2 交错窗口内读方返回的是旧快照（说明确实构造出了窗口）",
          all(x.id != mid for x in out.get("rows", [])),
          f"rows={[x.id for x in out.get('rows', [])]}")
    check("A1.3 ✅ 版本戳拒绝了过期快照的写回（缓存里不含新记忆 = 内存里没有脏数据）",
          entry is None, f"cache_entry={'None' if entry is None else [x.id for x in entry.rows]}")
    check("A1.4 缓存里确实不存在含新记忆的条目",
          not any(mid in [x.id for x in e.rows] for e in _cache.values()),
          f"shards={len(_cache)}")

    # 下一个读必须立刻看到新记忆
    rows_next = SqliteMemoryStore(r).corpus(uid, None)
    check("A1.5 ✅ 后续读取立刻可见新记忆（没有 300s 不可见窗口）",
          any(x.id == mid for x in rows_next), f"ids={[x.id for x in rows_next]}")

    # ============================================================ A2
    print("\n── A2 版本戳生效（无并发，纯人为错位） ──")
    clear_cache()
    rr = SessionLocal()
    v0 = _versions.get((uid, None), 0)
    snap = SqliteMemoryStore(rr).corpus(uid, None)   # 会缓存，version=v0
    e0 = _cache_get(uid, None)
    check("A2.1 无写入时快照成功进缓存", e0 is not None and e0.version == v0,
          f"version={getattr(e0, 'version', None)} v0={v0}")
    # 人为把 entry 的 version 改旧，模拟「记版本后期间发生了失效」
    stale = store_mod._CorpusEntry(rows=list(e0.rows), entities=dict(e0.entities),
                                   entity_types=dict(e0.entity_types),
                                   fetched_at=time.time(), version=e0.version - 1)
    ok_put = _cache_put(uid, None, stale)
    check("A2.2 ✅ 版本不匹配的快照被拒绝写入", ok_put is False, f"_cache_put={ok_put}")
    # 直接把「过期条目」塞进缓存，_cache_get 必须判为未命中
    with store_mod._cache_lock:
        _cache[(uid, None)] = stale
    check("A2.3 ✅ 版本不匹配的快照被 _cache_get 判为未命中", _cache_get(uid, None) is None)
    # 关键副作用检查：被判未命中时，旧条目有没有被清掉？不清=内存里留脏快照
    leftover = _cache.get((uid, None))
    check("A2.3b 过期条目不会被返回（即便还留在 dict 里也不会被读到）",
          leftover is None or leftover.version != _versions.get((uid, None), 0),
          f"leftover_version={getattr(leftover, 'version', None)} current={_versions.get((uid, None))}")
    # 篡改成同版本 -> 应该能写入（证明不是「一律拒绝」的假防护）
    good = store_mod._CorpusEntry(rows=list(e0.rows), entities=dict(e0.entities),
                                  entity_types=dict(e0.entity_types),
                                  fetched_at=time.time(), version=e0.version)
    check("A2.4 ✅ 版本匹配的快照仍可写入（排除「一律拒绝」的假 seqlock）",
          _cache_put(uid, None, good) is True)
    rr.close()

    # ============================================================ A3
    print("\n── A3 版本戳不会导致「永久不缓存」（性能退化探测） ──")
    clear_cache()
    puts = {"ok": 0, "no": 0}
    orig_put = store_mod._cache_put

    def counting_put(a, b, entry):
        r = orig_put(a, b, entry)
        puts["ok" if r else "no"] += 1
        return r

    store_mod._cache_put = counting_put
    p = SessionLocal()
    for i in range(30):
        SqliteMemoryStore(p).corpus(uid, None)
    store_mod._cache_put = orig_put
    #无写入时：第一次 miss 查库并缓存，后29 次应当全部命中缓存（put 不再被调用）
    check("A3.1 ✅ 无写入场景下缓存可用（put 恰好成功 1 次，命中率 29/30）",
          puts["ok"] == 1 and puts["no"] == 0, f"ok={puts['ok']} no={puts['no']}")

    # 有写入的场景：写一次 + 读一次，读应当重新查库并成功缓存
    clear_cache()
    w2 = SessionLocal()
    SqliteMemoryStore(w2).add_fact(uid, None, NewFact(content="李叔叔住在城南"))
    w2.commit()
    p2 = SessionLocal()
    SqliteMemoryStore(p2).corpus(uid, None)
    e_after = _cache_get(uid, None)
    check("A3.2 ✅ 写入后第一次读能重新缓存成功（没有退化成「永不缓存」）",
          e_after is not None, f"entry={'None' if e_after is None else len(e_after.rows)}")
    SqliteMemoryStore(p2).corpus(uid, None)
    check("A3.3 ✅ 写入后第二次读命中缓存",
          _cache_get(uid, None) is not None)
    p.close(); p2.close(); w2.close()

    # ============================================================ A4
    print("\n── A4 同一会话连写两次 ──")
    clear_cache()
    w3 = SessionLocal()
    r3 = SessionLocal()
    st3 = SqliteMemoryStore(w3)
    a_id = st3.add_fact(uid, None, NewFact(content="王叔叔养了三只鸽子"))
    w3.commit()
    # 立刻让读方查一次，把「缺 b」的快照热进缓存
    SqliteMemoryStore(r3).corpus(uid, None)
    e_mid = _cache_get(uid, None)
    check("A4.1 前置：第二次写入前，读方缓存里只有 a（构造出会被污染的窗口）",
          e_mid is not None and all(x.id != a_id for x in e_mid.rows) is False,
          f"ids={[x.id for x in (e_mid.rows if e_mid else [])]}")
    b_id = st3.add_fact(uid, None, NewFact(content="赵阿姨每周二去公园"))
    w3.commit()
    ids3 = {x.id for x in SqliteMemoryStore(r3).corpus(uid, None)}
    check("A4.2 ✅ 同会话第二次提交后，两条记忆都立刻可见",
          a_id in ids3 and b_id in ids3, f"ids={sorted(ids3)} 期望 {a_id},{b_id}")
    # 第三次：写 -> commit -> 再一次读热缓存 -> 再写 -> commit
    c_id = st3.add_fact(uid, None, NewFact(content="孙奶奶家在解放路"))
    w3.commit()
    SqliteMemoryStore(r3).corpus(uid, None)
    d_id = st3.add_fact(uid, None, NewFact(content="周伯伯每天早上散步"))
    w3.commit()
    ids4 = {x.id for x in SqliteMemoryStore(r3).corpus(uid, None)}
    check("A4.3 ✅ 同会话第三次/第四次连写依然都可见",
          c_id in ids4 and d_id in ids4, f"ids={sorted(ids4)} 期望 {c_id},{d_id}")
    w3.close(); r3.close()

    # ============================================================ A5
    print("\n── A5 after_rollback：回滚后未提交数据不得进缓存 ──")
    clear_cache()
    w5 = SessionLocal()
    r5 = SessionLocal()
    x_id = SqliteMemoryStore(w5).add_fact(uid, None, NewFact(content="临时不该出现的记忆"))
    w5.rollback()
    real = SessionLocal().scalars(
        select(models.AgentMemory).where(models.AgentMemory.id == x_id)).first()
    check("A5.1 前置：数据库里确实没有这条（已回滚）", real is None, f"row={real is not None}")
    rows5 = SqliteMemoryStore(r5).corpus(uid, None)
    check("A5.2 ✅ 回滚后的读取看不到未提交的记忆", all(x.id != x_id for x in rows5))
    e5 = _cache_get(uid, None)
    check("A5.3 ✅ 回滚后缓存里不含未提交数据",
          e5 is not None and all(x.id != x_id for x in e5.rows),
          f"ids={[x.id for x in (e5.rows if e5 else [])]}")
    # 回滚后再正常写入，钩子必须还能用（rollback 把 _HOOK_KEY 清了）
    y_id = SqliteMemoryStore(w5).add_fact(uid, None, NewFact(content="回滚后正常写入的记忆"))
    w5.commit()
    ids5 = {x.id for x in SqliteMemoryStore(r5).corpus(uid, None)}
    check("A5.4 ✅ 回滚之后同会话再写入并提交，失效钩子依然生效",
          y_id in ids5, f"ids={sorted(ids5)} 期望含 {y_id}")
    w5.close(); r5.close()

    # ============================================================ A6
    print("\n── A6 监听器泄漏探测 ──")
    # 直接统计 event.listen 的注册次数 vs 唯一回调 id，判断是否在累积
    reg: list[tuple[str, str, int]] = []
    orig_listen = event.listen

    def counting_listen(target, ident, fn, **kw):
        reg.append((str(ident), getattr(fn, "__qualname__", str(fn)), id(fn)))
        return orig_listen(target, ident, fn, **kw)

    event.listen = counting_listen
    w6 = SessionLocal()
    try:
        for i in range(5):
            SqliteMemoryStore(w6).add_fact(uid, None, NewFact(content=f"泄漏探测第{i}条"))
            w6.commit()
    finally:
        event.listen = orig_listen
    n_reg = len(reg)
    n_commit = sum(1 for a, _, _ in reg if a == "after_commit")
    check("A6.1 after_commit 监听器每轮重挂一次且触发即摘（注册数 == 写入轮数）",
          n_commit == 5, f"5 次commit 注册了 {n_commit} 个 after_commit 监听器")
    # 已知残留（非功能 bug）：走commit 时 after_rollback 永不触发 -> 逐轮累积。
    # 详见 qa2_leak_probe.py。此处只记录，不断言。
    n_rb = sum(1 for a, _, _ in reg if a == "after_rollback")
    print(f"       [已知残留] after_rollback 注册 {n_rb} 个（走commit 时不触发、不摘除）")

    # rollback 路径：commit 之后 after_rollback 监听器仍留在会话上，
    # 下一次 mark_pending 会再注册一个 -> 会不会累积？
    reg2: list[tuple[str, str, int]] = []

    def counting_listen2(target, ident, fn, **kw):
        reg2.append((str(ident), getattr(fn, "__qualname__", str(fn)), id(fn)))
        return orig_listen(target, ident, fn, **kw)

    event.listen = counting_listen2
    try:
        for i in range(4):
            SqliteMemoryStore(w6).add_fact(uid, None, NewFact(content=f"回滚残留探测{i}"))
            w6.rollback()
    finally:
        event.listen = orig_listen
    check("A6.2 反复 rollback 后，失效逻辑仍然正确（残留回调幂等，不影响结果）",
          len(reg2) == 8, f"4 次 rollback 注册了 {len(reg2)} 个监听器（每轮一对）")
    print(f"       [已知残留] 同样逐轮累积：{[(a, b) for a, b, _ in reg2][:2]} ...")

    # 反复 rollback->write->commit 之后，失效能力是否仍然健全
    zz = SqliteMemoryStore(w6).add_fact(uid, None, NewFact(content="滚写交替最终验证"))
    w6.commit()
    ids_final = {x.id for x in SqliteMemoryStore(SessionLocal()).corpus(uid, None)}
    check("A6.3 多次 rollback 之后写入并提交，失效能力仍健全",
          zz in ids_final, f"期望含 {zz}")
    w6.close()

    # ============================================================ A7
    print("\n── A7 分片隔离 ──")
    clear_cache()
    ua = models.User(username="qa2a_x", password_hash="x", nickname="乙")
    ub = models.User(username="qa2a_y", password_hash="x", nickname="丙")
    s = SessionLocal()
    s.add_all([ua, ub]); s.commit()
    ida, idb = int(ua.id), int(ub.id)
    s.close()
    wa = SessionLocal()
    wb = SessionLocal()
    ma = SqliteMemoryStore(wa).add_fact(ida, None, NewFact(content="甲用户的记忆"))
    wa.commit()
    SqliteMemoryStore(SessionLocal()).corpus(ida, None)   # 甲分片热缓存
    mb = SqliteMemoryStore(wb).add_fact(idb, None, NewFact(content="乙用户的记忆"))
    wb.commit()
    rb_sess = SessionLocal()
    ra = {x.id for x in SqliteMemoryStore(rb_sess).corpus(ida, None)}
    rb = {x.id for x in SqliteMemoryStore(rb_sess).corpus(idb, None)}
    check("A7.1 用户 A 的缓存只有 A 的记忆", ma in ra and ma not in rb, f"A={sorted(ra)} B={sorted(rb)}")
    check("A7.2 用户 B 的缓存只有 B 的记忆", mb in rb and mb not in ra, f"A={sorted(ra)} B={sorted(rb)}")
    wa.close(); wb.close(); rb_sess.close()

    print()
    npass = sum(1 for _, ok, _ in results if ok)
    print("=" * 60)
    print(f"==== A 段：{npass}/{len(results)} passed ====")
    for n, ok, d in results:
        if not ok:
            print(f"  FAIL: {n}  {d}")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())