"""回归：手动写入的记忆必须被标成「家属确认」，且可信度**真的更高**。

历史缺陷（Bug#6）
------------------
``POST /api/memory/facts`` 是家属**亲眼确认**后手动补写的兜底入口
（「机器人漏记了」时用）。但它落库时 ``source_quote`` 被填成了
``"LLM 抽取"`` —— 与模型自动归纳的事实**同一个标记**。

危害不在文案，而在于它**反转了产品承诺的防污染机制**：
家属确认过的事实是「人给的证据」，LLM 归纳的只是「机器的猜测」，
两者的可信度天然不对等。标反之后：

  * 按可信度排序/取舍时，人的判断被当成机器猜测，轻��丢掉；
  * 机器猜错的内容被当成家属确认，继续当成事实用；
  * 审计页家属看到「LLM 抽取」，误以为是自己确认过的，防线形同虚设。

修法与本脚本断言的三件事
------------------------
1. **标记独立**：手动写入走 ``store.SOURCE_MANUAL``（"家属手动确认"），
   不再与 ``SOURCE_LLM`` 共用同一个字面量；
2. **逻辑读字段**：可信度不是只写在审计页上的一行文案，而是由
   ``retrieve.trust_weight()`` 读 ``MemoryRow.source_quote`` 算出，
   乘进融合分 —— 也就是说手动记忆在得分接近时**排在抽取记忆之前**；
3. **接口可见**：``trust`` 系数随命中一起出接口，家属端能区分二者。

为什么留这个脚本：这是「文案对了但逻辑没动」这类缺陷的唯一护栏。
只查库里的字符串，改成纯文案也能全绿；必须断言**打分确实不同**。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

DB_PATH = Path(tempfile.gettempdir()) / "elder_qa_manual_quote.db"
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["ELDER_JWT_SECRET"] = "qa-manual-quote-secret"

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import models  # noqa: E402
from app.memory.retrieve import TRUST_LLM, TRUST_MANUAL, trust_weight  # noqa: E402
from app.memory.store import (SOURCE_LLM, SOURCE_MANUAL, MemoryRow,  # noqa: E402
                              NewFact, SqliteMemoryStore, clear_cache)

engine = create_engine(os.environ["ELDER_DB"], future=True)
models.Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine, future=True)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def _row(content: str, source_quote: str, source_session: str = "") -> MemoryRow:
    """构造一个 MemoryRow（不落库），用于直接验证打分逻辑。"""
    return MemoryRow(id=1, user_id=1, agent_id=None, content=content,
                     category="other", source_session=source_session,
                     source_quote=source_quote)


def main() -> int:
    print("── 1. 常量本身必须是两个不同的值 ──")
    check("手动与LLM 的来源标记不相同",
          SOURCE_MANUAL != SOURCE_LLM, f"{SOURCE_MANUAL!r} vs {SOURCE_LLM!r}")
    check("手动来源标记是「家属确认」语义", "家属" in SOURCE_MANUAL, SOURCE_MANUAL)

    print("── 2. trust_weight 必须按来源给出不同权重 ──")
    manual = _row("爸每周三去透析", SOURCE_MANUAL)
    llm = _row("爸每周三去透析", SOURCE_LLM)
    w_manual, w_llm = trust_weight(manual), trust_weight(llm)
    check("手动确认的可信度 > LLM 抽取的可信度（方向不能反）",
          w_manual > w_llm, f"manual={w_manual:.3f} llm={w_llm:.3f}")
    check("手动确认的权重就是常量声明值（不是碰巧更大）",
          w_manual == TRUST_MANUAL, f"{w_manual:.3f} vs {TRUST_MANUAL:.3f}")
    check("LLM 抽取的权重就是常量声明值",
          w_llm == TRUST_LLM, f"{w_llm:.3f} vs {TRUST_LLM:.3f}")
    # 幅度要有节制：太大就变成「溯源偏好压过相关性」，召回准确率会退化
    check("可信度幅度是保守的（0.8~1.3之间，避免压过相关性）",
          0.8 <= TRUST_LLM <= w_manual <= 1.3,
          f"[{TRUST_LLM}, {TRUST_MANUAL}]")

    print("── 3. 兼容与兜底 ──")
    # Bug#6 时期写进库的历史标记不该让手动记忆被判成低可信
    check("历史标记「家属手动补写」仍被认作手动确认（无需回刷全表）",
          trust_weight(_row("x", "家属手动补写")) == TRUST_MANUAL)
    # source_session 一直是对的，作为二级信号兜底
    check("只有 source_session=manual（来源标记丢失）也能识别为手动",
          trust_weight(_row("x", "", source_session="manual")) == TRUST_MANUAL)
    check("完全无来源信息的旧数据不奖不罚（=1.0）",
          trust_weight(_row("x", "")) == 1.0)
    check("未知来源标记不误判为高可信",
          trust_weight(_row("x", "某次导入")) == 1.0)

    print("── 4. 端到端：走真实的 add_fact 落库，再读回来验证 ──")
    db = SessionLocal()
    try:
        store = SqliteMemoryStore(db)
        mid_manual = store.add_fact(
            1, None,
            NewFact(content="爸每周三去透析", category="health",
                    source_session="manual", source_quote=SOURCE_MANUAL))
        mid_llm = store.add_fact(
            1, None,
            NewFact(content="爸每周三去透析", category="health",
                    source_session="sess-abc", source_quote=SOURCE_LLM))
        db.commit()
        clear_cache()

        rows = {r.id: r for r in db.scalars(
            select(models.AgentMemory).where(
                models.AgentMemory.id.in_([mid_manual, mid_llm]))).all()}
        check("手动写入的那条落库标记为「家属手动确认」",
              (rows[mid_manual].source_quote or "") == SOURCE_MANUAL,
              repr(rows[mid_manual].source_quote))
        check("LLM 写入的那条落库标记为「LLM 抽取」",
              (rows[mid_llm].source_quote or "") == SOURCE_LLM,
              repr(rows[mid_llm].source_quote))

        # 关键断言：source_quote 必须**进到检索层的快照里**，
        # 否则 trust_weight 在真实链路上读不到它（只在单测里成立）。
        snap = {r.id: r for r in SqliteMemoryStore(db).corpus(1, None)}
        check("来源标记进入了语料快照 MemoryRow（打分链路读得到）",
              snap[mid_manual].source_quote == SOURCE_MANUAL
              and snap[mid_llm].source_quote == SOURCE_LLM,
              f"{snap[mid_manual].source_quote!r} / {snap[mid_llm].source_quote!r}")
        check("两条同内容记忆的快照给出不同可信度（不是只改了文案）",
              trust_weight(snap[mid_manual]) > trust_weight(snap[mid_llm]),
              f"{trust_weight(snap[mid_manual]):.3f} > {trust_weight(snap[mid_llm]):.3f}")
    finally:
        db.close()

    print()
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"===== {passed}/{total} passed =====")
    fails = [(n, d) for n, ok, d in results if not ok]
    if fails:
        print("失败项：")
        for n, d in fails:
            print(f"  - {n}  {d}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
