"""服务端「一次性提醒缺日期」三面校验：回填 / 入参拦截 / 出参不被拦。

为什么需要这个脚本（本轮的真 bug）
--------------------------------
APP 的 ``reminder-edit.vue`` 里 ``kindOptions`` 曾整体错位一格：显示"每天"的
那一项下标是 ``0``（= ``ScheduleKind.ONCE``）。于是用户选"每天"，服务端存下来
的是 ``schedule_kind = 0`` + **空的** ``once_date``。

这个组合在服务端**算不出任何触发时刻**（``occurrences_on`` 拿不到日期），
所以那条提醒**永远不会响**；而 APP 上它显示成"指定日期一次"，看起来完全正常。
对老人来说这是最糟的失败形态：**该响的时候不响，且没有任何提示**。

本轮补的三件事，各自挡一个**不同的失败面**，缺一条就有洞：

  1. **回填**（``schema_patch.backfill_once_without_date``）—— 修库里的存量
     坏行，把 ``0 + 空日期`` 改成 ``1``（每天）。改 DAILY 而不是删，是因为
     错位一格之前用户选的就是"每天"，且删行等于静默丢功能。
  2. **入参拦截**（``routers/reminders._reject_once_without_date``）—— 挡住
     **新的**坏数据进来。没有它，回填只修一次，之后 APP 旧版本/直接调 API
     还能继续造坏行。
  3. **出参不被拦** —— 这条是**反向断言**，也是本脚本最重要的部分。校验必须
     放在 router 层而不是 ``ReminderIn`` 的 ``model_validator``：因为
     ``ReminderOut(ReminderIn)`` 会继承那个校验器，而 ``_with_next()`` 与设备
     下发同步都会 ``ReminderOut.model_validate(库里那一行)``。一旦挂错层，
     一条历史坏行就会让 ``GET /api/reminders`` 和 ``/api/device/sync`` 抛
     异常 -> 500 -> **整台设备一条提醒都同步不到**，故障面从"一条规则不响"
     扩大到"设备彻底静音"。C8~C10 就是钉死这一点的。

为什么要三层都测（而不是只测回填）
--------------------------------
回填是**一次性**的（登记进 ``patch_meta``），它在"第一次启动"之后就不再修
任何数据。所以「第二次启动起，坏数据靠什么挡」必须有独立答案 —— 答案是入参
拦截 + 固件兜底（``elder_rmd_sched.c`` 遇 ``once_ymd == 0`` 按每天处理）。
本脚本的 C16 专门验证「登记后不再重跑」，从而证明后两层不是可选项。

用法：
    C:/Python314/python.exe tools/check_reminder_once.py
退出码 0 = 全通过；非 0 = 有断言失败。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

DB_PATH = Path(tempfile.gettempdir()) / f"elder_rmd_once_{os.getpid()}.db"
if DB_PATH.exists():
    DB_PATH.unlink()
# 必须在 import app.* 之前设置：config 在模块导入期就把 DATABASE_URL 定死了。
os.environ["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["ELDER_JWT_SECRET"] = "check-rmd-once"

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import models, schema_patch  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.routers import device_api  # noqa: E402
from app.routers import reminders as rmd_mod  # noqa: E402
from app.schemas import LocalReminderIn, ReminderIn, ReminderOut  # noqa: E402

models.Base.metadata.create_all(engine)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def expect_422(fn, label: str) -> None:
    """断言调用抛出 422（而不是别的异常 / 静默成功）。"""
    try:
        fn()
    except HTTPException as exc:
        check(label, exc.status_code == 422,
              f"HTTPException {exc.status_code}: {exc.detail}")
        return
    except Exception as exc:  # noqa: BLE001  其它异常一律算失败，并把类型打出来
        check(label, False, f"抛了非 HTTPException: {type(exc).__name__}: {exc}")
        return
    check(label, False, "没有抛异常（坏数据被放行）")


def payload(**kw) -> ReminderIn:
    base = {"title": "测试提醒", "times_of_day": ["08:00"], "schedule_kind": 1}
    base.update(kw)
    return ReminderIn(**base)


def make_reminder(db, **kw) -> models.Reminder:
    """直接落一条提醒行（绕过 ReminderIn 校验）—— 复刻库里历史坏行的存在方式。"""
    data = {"device_id": DEV_ID, "creator_id": USER_ID, "title": "x",
            "times_of_day": ["08:00"], "schedule_kind": 1}
    data.update(kw)
    r = models.Reminder(**data)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def kind_of(db, pk: int) -> tuple[int, str | None]:
    db.expire_all()
    r = db.get(models.Reminder, pk)
    return int(r.schedule_kind), r.once_date


def main() -> int:
    global DEV_ID, USER_ID

    db = SessionLocal()
    user = models.User(username="rmd_once_u", password_hash="x", nickname="甲")
    db.add(user)
    db.commit()
    dev = models.Device(device_id="rmdonce0001", owner_id=user.id, name="测试机")
    db.add(dev)
    db.commit()
    DEV_ID, USER_ID = int(dev.id), int(user.id)
    print(f"测试库: {DB_PATH}  设备 pk={DEV_ID}  用户 pk={USER_ID}")

    # ============================================================ 一、入参拦截
    print("\n一、入参拦截（router 层，create）")
    before = len(db.scalars(select(models.Reminder)).all())

    expect_422(lambda: rmd_mod.create_reminder(
        DEV_ID, payload(schedule_kind=0, once_date=None), None, db, user),
        "C1 ONCE + once_date=None -> 422")
    expect_422(lambda: rmd_mod.create_reminder(
        DEV_ID, payload(schedule_kind=0, once_date=""), None, db, user),
        "C2 ONCE + once_date='' -> 422")
    expect_422(lambda: rmd_mod.create_reminder(
        DEV_ID, payload(schedule_kind=0, once_date="   "), None, db, user),
        "C3 ONCE + once_date 全空白 -> 422（去空白后为空也算缺）")

    after = len(db.scalars(select(models.Reminder)).all())
    check("C4 被拒的三次一条都没落库", before == after, f"{before} -> {after}")

    out = rmd_mod.create_reminder(
        DEV_ID, payload(title="一次性有日期", schedule_kind=0,
                        once_date="2026-12-01"), None, db, user)
    k, d = kind_of(db, out.id)
    check("C5 ONCE + 有日期 -> 建成且 kind/日期都对", k == 0 and d == "2026-12-01",
          f"kind={k} once_date={d}")

    out = rmd_mod.create_reminder(
        DEV_ID, payload(title="每天", schedule_kind=1, once_date=None), None, db, user)
    k, _ = kind_of(db, out.id)
    check("C6 DAILY + 无日期 -> 建成（不能被误拦）", k == 1, f"kind={k}")

    out = rmd_mod.create_reminder(
        DEV_ID, payload(title="每周", schedule_kind=2, weekdays=[1, 3],
                        once_date=None), None, db, user)
    k, _ = kind_of(db, out.id)
    check("C7 WEEKLY + 无日期 -> 建成（once_date 对非 ONCE 本就该为空）",
          k == 2, f"kind={k}")

    print("\n一'、入参拦截（router 层，update）")
    daily = make_reminder(db, title="先建每天")
    expect_422(lambda: rmd_mod.update_reminder(
        daily.id, payload(schedule_kind=0, once_date=None), None, db, user),
        "C8 把每天改成 ONCE 且不给日期 -> 422")
    k, _ = kind_of(db, daily.id)
    check("C9 被拒的 PATCH 没改动库里那行（仍是每天）", k == 1, f"kind={k}")

    rmd_mod.update_reminder(
        daily.id, payload(schedule_kind=0, once_date="2026-11-05"), None, db, user)
    k, d = kind_of(db, daily.id)
    check("C10 改成 ONCE 且给日期 -> 成功", k == 0 and d == "2026-11-05",
          f"kind={k} once_date={d}")

    # ================================================== 二、出参不被拦（关键回归）
    print("\n二、出参不被拦 —— 库里已有坏行时，读接口必须照常工作")
    bad_read = make_reminder(db, title="历史坏行", schedule_kind=0, once_date=None)

    ok_validate, detail = True, ""
    try:
        ReminderOut.model_validate(bad_read)
    except Exception as exc:  # noqa: BLE001
        ok_validate, detail = False, f"{type(exc).__name__}: {exc}"
    check("C11 ReminderOut 能校验库里那条坏行（校验器没挂到 model 上）",
          ok_validate, detail)

    ok_list, detail = True, ""
    try:
        rows = rmd_mod.list_reminders(None, db, user)
        ok_list = any(r.id == bad_read.id for r in rows)
        detail = f"返回 {len(rows)} 条"
    except Exception as exc:  # noqa: BLE001
        ok_list, detail = False, f"{type(exc).__name__}: {exc}"
    check("C12 GET /api/reminders 不被坏行打成 500", ok_list, detail)

    # ============================================== 二'、设备补报（第三条写入路径）
    # POST /api/device/reminders/local 的协议里**没有** once_date 字段，设备却
    # 会上报 schedule_kind。所以它是「一次性缺日期」的第三个入口：这里若直接
    # 落库，等于绕过 router 的 422 继续造坏行。
    print("\n二'、设备断网自建提醒补报（协议无 once_date）")
    acks = device_api.report_local_reminders(
        [LocalReminderIn(client_key="dev-once-1", title="设备报的一次性",
                         schedule_kind=0, times_of_day=["09:00"])],
        db, dev)
    rid = int(acks["acks"][0]["reminder_id"])
    k, d = kind_of(db, rid)
    check("C12b 设备报 ONCE 过来按每天落库（不丢、不造永不触发的行）",
          k == 1 and d is None, f"kind={k} once_date={d}")

    acks = device_api.report_local_reminders(
        [LocalReminderIn(client_key="dev-daily-1", title="设备报的每天",
                         schedule_kind=1, times_of_day=["09:00"])],
        db, dev)
    rid2 = int(acks["acks"][0]["reminder_id"])
    k2, _ = kind_of(db, rid2)
    check("C12c 设备报 DAILY 原样落库", k2 == 1, f"kind={k2}")

    # ============================================================ 三、回填
    print("\n三、回填 backfill_once_without_date")
    bf_null = make_reminder(db, title="bf-null", schedule_kind=0, once_date=None)
    bf_empty = make_reminder(db, title="bf-empty", schedule_kind=0, once_date="")
    bf_good = make_reminder(db, title="bf-good", schedule_kind=0,
                            once_date="2026-12-31")
    bf_week = make_reminder(db, title="bf-week", schedule_kind=2,
                            weekdays=[2], once_date=None)
    bf_month = make_reminder(db, title="bf-month", schedule_kind=3,
                             month_days=[15], once_date="")
    bf_interval = make_reminder(db, title="bf-interval", schedule_kind=4,
                                interval_days=3, once_date=None)

    n = schema_patch.backfill_once_without_date()
    # 坏行 = bf-null + bf-empty + 第二节那条 bad_read + C1~C3 一条都没落库
    expect = 3
    check(f"C13 回填改动 {expect} 行（NULL 与 '' 两种写法都要捞到）",
          n == expect, f"实际 {n} 行")

    for row, label in ((bf_null, "bf-null"), (bf_empty, "bf-empty"),
                       (bad_read, "历史坏行")):
        k, _ = kind_of(db, row.id)
        check(f"C14 {label} 已改成每天", k == 1, f"kind={k}")

    k, d = kind_of(db, bf_good.id)
    check("C15 真正有日期的一次性**没被碰**", k == 0 and d == "2026-12-31",
          f"kind={k} once_date={d}")

    for row, want, label in ((bf_week, 2, "每周"), (bf_month, 3, "每月"),
                             (bf_interval, 4, "每 N 天")):
        k, _ = kind_of(db, row.id)
        check(f"C16 {label}规则没被误改成每天", k == want, f"kind={k} 期望 {want}")

    n2 = schema_patch.backfill_once_without_date()
    check("C17 再跑一次改动 0 行（幂等）", n2 == 0, f"第二次 {n2} 行")

    db2 = SessionLocal()
    meta = db2.get(models.PatchMeta, "reminder_once_without_date_v1")
    check("C18 patch_meta 已登记 reminder_once_without_date_v1", meta is not None,
          "未登记" if meta is None else "已登记")
    db2.close()

    # 登记之后**故意**再造一条坏行：它不会再被自动修 —— 这正是「只跑一次」那层
    # 在起作用，也说明入参拦截 + 固件兜底不是可选项。
    late = make_reminder(db, title="登记后的新坏行", schedule_kind=0, once_date=None)
    n3 = schema_patch.backfill_once_without_date()
    k, _ = kind_of(db, late.id)
    check("C19 登记后新出现的坏行不再被自动修（'只跑一次'那层生效）",
          n3 == 0 and k == 0, f"改动 {n3} 行, kind={k}")

    # ============================================================ 四、接线
    print("\n四、run_all 真的调用了它")
    report = schema_patch.run_all()
    check("C20 run_all() 的返回里带 reminder_once_fixed", "reminder_once_fixed" in report,
          str(sorted(report)))

    # ============================================================ 汇总
    failed = [r for r in results if not r[1]]
    print(f"\n{'=' * 60}\n通过 {len(results) - len(failed)} / {len(results)}，失败 {len(failed)}")
    if failed:
        for name, _, detail in failed:
            print(f"  FAIL: {name}  {detail}")
    db.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
