"""进程内故障注入：**审计写失败时，业务写必须一起回滚**（E1 的运行时形态）。

这是「审计与业务写同事务」的**行为判据**，与 ``smoke_admin_presets.py``
的 T32（AST 静态断言）互补，两个都要：

* **T32 守"写法"**：源码里不许拆成两次 commit（record 行号必须早于
  每一个 commit）。治本，但抓不到某种静态判不出的拆分（比如 commit
  被藏进某个工具函数、或换了 import 写法导致匹配失效）；
* **本脚本守"后果"**：monkeypatch ``admin_audit.record`` 抛异常，然后
  断言业务行的改动**没有落库**、审计表**没有多行**。哪怕事务真被拆开，
  这里也能抓住"业务成功而审计丢失"这个最坏后果。

为什么"业务成功而审计丢失"是最坏后果
------------------------------------
请求失败用户会看到错误、会重试；而"改成功了但审计里没有任何痕迹"是
**静默的** —— 运营改了平台预设，审计查不到是谁改的、改了什么，
事后追溯直接断线。审计丢一次就永远丢了，那等于没有审计。

它守不住什么（边界，防止被当成它不是的东西）
--------------------------------------------
* 只覆盖 ``update_preset`` / ``reset_preset`` 两个写入口，且是**进程内
  直调函数**（绕过 HTTP 层与 FastAPI 依赖注入）。``archive`` /
  ``soft-guardrails`` 未覆盖 —— 它们的同事务由 T32 的 AST 判据守；
* 不验证"审计写成功时业务一定成功"（那是正常路径，冒烟已覆盖）；
* 在 SQLite 文件库上验证；MySQL 的回滚语义未在此脚本范围内。

反绿地狱纪律
------------
本脚本自身的断言也经过反向验证：把 ``update_preset`` 改成"先 commit
业务再写审计"（正是 T32 要挡的写法），本脚本的故障注入用例**必须转红**
—— 证据见引入它的那次提交的 commit message。

用法::

    python tools/qa_audit_rollback.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"

DB_FILE = Path(tempfile.gettempdir()) / f"qa_audit_rollback_{os.getpid()}.db"
# ⚠️ 必须在 import app.* **之前**设置 ELDER_DB：app.config 在 import 期
# 读它并据此绑定 engine。来晚一步，全部读写会落到真实库上 —— 那会把
# 开发数据写坏，而且测试还在"自己的临时库"上绿灯。
os.environ["ELDER_DB"] = f"sqlite:///{DB_FILE.as_posix()}"

sys.path.insert(0, str(SERVER))

from app.db import Base, SessionLocal, engine
from app.persona import admin_audit, catalog
from app.routers import admin_presets
from app.schemas import PresetPatchIn, PresetResetIn
from sqlalchemy import func, select

from app import models  # 先注册全部模型，create_all 才建得出表

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记一条断言并打印。与冒烟脚本同一格式，失败时退出码非零。"""
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


class AuditBoom(RuntimeError):
    """故障注入：模拟 ``admin_audit.record`` 写入失败。

    刻意用一个**自定义异常类型**而不是裸 RuntimeError：harness 要精确
    捕获"我们注入的这一种"失败，把端点自己抛的其它异常（比如 400）
    与注入故障区分开 —— 混在一起会让"端点因为别的原因失败"被误判成
    "注入生效了"。
    """


def _boom(*args, **kwargs):
    """替身 record：什么都不做，直接抛。"""
    raise AuditBoom(" injected fault: audit write failed ")


def seed() -> tuple[int, int]:
    """建表 + 种一个管理员和一个平台预设，返回 ``(admin_id, preset_id)``。

    traits 用 ``catalog.DEFAULT_TRAITS`` 而不是手写字典：手写迟早与
    catalog 漂移（少一维就 400），用常量做种子才能保证"播种合法"
    这件事本身不需要额外维护。
    """
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        admin = models.User(username="qa_rollback_admin", password_hash="x",
                            nickname="qa", is_admin=True)
        db.add(admin)
        db.flush()
        row = models.AIProvider(
            name="小暖（平台预设）", kind="", record_type=models.RECORD_TYPE_PRESET,
            owner_id=None, description="出厂简介",
            system_prompt="出厂提示词", tts_voice="zh-CN-XiaoxiaoNeural",
            traits=dict(catalog.DEFAULT_TRAITS), guardrails={"soft": []},
        )
        db.add(row)
        db.commit()
        return int(admin.id), int(row.id)
    finally:
        db.close()


def read_state(pk: int) -> tuple[str, int]:
    """**新开会话**读 preset 的 name 与审计条数。

    必须新开会话：故障用例的旧会话里还挂着未提交的内存改动，用同一个
    会话读等于"自己读自己"，什么都验证不到。这也正是生产的行为 ——
    请求结束会话就关了，下一个请求看到的是落库后的状态。
    """
    db = SessionLocal()
    try:
        row = db.get(models.AIProvider, pk)
        n_audit = db.scalar(
            select(func.count()).select_from(models.AdminPresetAudit))
        return (str(row.name) if row is not None else "<row-gone>",
                int(n_audit or 0))
    finally:
        db.close()


def call_update(pk: int, admin_id: int, fault: bool) -> str:
    """直调 ``update_preset``，返回 "raised" / "ok"。

    teardown **镜像 ``get_db``**：不 commit、直接 close（未提交事务随之
    回滚）。不镜像的话，本脚本验的就是自己臆想的会话生命周期，而不是
    生产真实使用的那一种。

    fault=True 时注入 AuditBoom，并在 finally 里**无条件还原** record ——
    还原必须放在 finally：否则某一用例中途抛了别的异常，被换掉的 record
    会污染后续所有用例（那种污染极难归因）。
    """
    db = SessionLocal()
    original = admin_audit.record
    try:
        admin = db.get(models.User, admin_id)
        if fault:
            admin_audit.record = _boom  # type: ignore[assignment]
        try:
            admin_presets.update_preset(
                pk, PresetPatchIn(name="被改名", description="被改简介"),
                db, admin)
            return "ok"
        except AuditBoom:
            return "raised"
        finally:
            admin_audit.record = original
    finally:
        db.close()


def call_reset(pk: int, admin_id: int, fault: bool) -> str:
    """直调 ``reset_preset``（第二个写入口），注入/语义同 call_update。"""
    db = SessionLocal()
    original = admin_audit.record
    try:
        admin = db.get(models.User, admin_id)
        if fault:
            admin_audit.record = _boom  # type: ignore[assignment]
        try:
            admin_presets.reset_preset(pk, PresetResetIn(), db, admin)
            return "ok"
        except AuditBoom:
            return "raised"
        finally:
            admin_audit.record = original
    finally:
        db.close()


def main() -> int:
    if DB_FILE.exists():
        DB_FILE.unlink()
    try:
        admin_id, pk = seed()
        name0, audit0 = read_state(pk)
        print(f"播种完成：admin_id={admin_id} preset_id={pk} "
              f"name={name0!r} audits={audit0}")

        # ---- 用例 1：update_preset，审计写失败 ----
        print("\n── 用例1 update_preset：record() 抛异常 ──")
        outcome = call_update(pk, admin_id, fault=True)
        name1, audit1 = read_state(pk)
        check("注入生效：update_preset 抛出 AuditBoom", outcome == "raised", outcome)
        check("★ 业务写已回滚：name 逐字未变（改成功了却查不到痕迹 = 最坏后果）",
              name1 == name0, f"{name0!r} -> {name1!r}")
        check("★ 审计表未新增（没有'半个审计行'残留）",
              audit1 == audit0, f"{audit0} -> {audit1}")

        # ---- 用例 2：正向对照，同一调用不注入必须成功 ----
        # 没有这一条，上面两个断言分不清"事务真的回滚了"和"端点本来就
        # 什么都写不进去"（后者也会 name 不变）—— 恒绿比没有断言更坏。
        print("\n── 用例2 正向对照：不注入时同一调用必须成功落库 ──")
        outcome = call_update(pk, admin_id, fault=False)
        name2, audit2 = read_state(pk)
        check("不注入时 update_preset 正常返回", outcome == "ok", outcome)
        check("★ 正向对照：name 真的被改写了（证明用例1的'未变'是回滚所致）",
              name2 == "被改名", f"{name2!r}")
        check("★ 正向对照：审计真的多了一条（证明夹具有分辨力）",
              audit2 == audit0 + 1, f"{audit0} -> {audit2}")

        # ---- 用例 3：reset_preset，审计写失败 ----
        print("\n── 用例3 reset_preset：record() 抛异常 ──")
        outcome = call_reset(pk, admin_id, fault=True)
        name3, audit3 = read_state(pk)
        check("注入生效：reset_preset 抛出 AuditBoom", outcome == "raised", outcome)
        check("★ 业务写已回滚：reset 未生效（system_prompt 应仍是'被改'后的值）",
              name3 == "被改名", f"{name3!r}")
        check("★ 审计表未新增", audit3 == audit2, f"{audit2} -> {audit3}")

        # ---- 用例 4：reset 正向对照 ----
        print("\n── 用例4 正向对照：不注入时 reset 必须生效 ──")
        outcome = call_reset(pk, admin_id, fault=False)
        _name4, audit4 = read_state(pk)
        check("不注入时 reset_preset 正常返回", outcome == "ok", outcome)
        check("★ 正向对照：reset 审计多了一条", audit4 == audit3 + 1,
              f"{audit3} -> {audit4}")
        db = SessionLocal()
        try:
            row = db.get(models.AIProvider, pk)
            check("★ 正向对照：reset 后 system_prompt 回到出厂默认"
                  "（证明用例3的'未生效'是回滚所致）",
                  row.system_prompt == catalog.DEFAULT_PRESET_PROMPT,
                  repr(row.system_prompt)[:40])
        finally:
            db.close()

        passed = sum(1 for _, ok, _ in results if ok)
        total = len(results)
        print(f"\n==== {passed}/{total} passed ====")
        for name, ok, detail in results:
            if not ok:
                print(f"  FAILED: {name} {detail}")
        return 0 if passed == total else 1
    finally:
        engine.dispose()
        if DB_FILE.exists():
            DB_FILE.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
