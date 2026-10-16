"""QA 第 2 轮 · **重启不变式回归门禁**：启动流程不得改写用户角色。

它守什么（一句话）
------------------
同一个用户派生角色（``record_type='role'``、``kind=''``、``owner_id=U``），
**重启服务后必须原样仍是 'role'**：owner 仍能读/写/删，其他用户仍看不到。

历史：它曾经是「缺陷复现器」
--------------------------
2026-10 的 P0：``schema_patch.backfill_record_types()`` 的判据只看
``kind``，而角色行的 ``kind`` 恒为空串（角色不参与下发/导出）—— 于是
每次启动都把用户派生角色刷成 ``preset``：

* 角色变只读（``can_write`` 对 preset 只认管理员，改不了也删不了）；
* preset 对全平台可见 = **跨用户信息泄漏**（A 家人的角色设定 B 家人能看到）；
* 且**不可逆** —— PATCH 刻意不改归属，没有任何接口能改回 'role'。

本脚本最初就是为定位这个缺陷而写（查库确认"重启后 record_type 被改写"）。

修复落地了**两层独立防线**（缺一不可，详见 ``schema_patch.py`` 顶部注释）：

1. **判据层**：刷 preset 的 WHERE 带 ``owner_id IS NULL`` —— 用户角色
   必然有 owner（``create_role`` / ``derive_role`` 写死 ``owner_id=user.id``），
   永不匹配；
2. **只执行一次层**：``patch_meta`` 表的
   ``record_type_owner_guard_v1`` 标记（``_is_applied``）—— 第二次启动
   直接返回，一行数据都不碰。判据正确**不能**替代只执行一次：将来 WHERE
   被改错一处（review 里几乎看不出来），P0 会立刻重演。

缺陷修复后，本脚本从「复现器」改造为「回归门禁」（2026-10-16 主理人拍板）：
它不再期待缺陷复现，而是把上述不变式钉成具名断言 ——
**任何一条不成立 = P0 重演 = FAIL（exit 2）**。旧版的失败形态是
"测出了防线有效却报成'未复现'"：判定要求 ``changed`` 非空，而防线有效时
``changed`` 恒为空 —— 那正说明脚本测的是"缺陷在不在"，而门禁该测的是
"防线还在不在"。

它守不住什么（写清楚，防止被当成它不是的东西）
--------------------------------------------
* 只覆盖 ``record_type`` 归属回填**这一条路径**（``ai_providers`` 的
  ``kind=''`` 分支）。不验证 ``patch_meta`` 登记表本身是否被正确写入
  （归 qa3_migration / 迁移完整性）；
* 不验证 avatars 的回填分支、``dedupe_persona_globals`` 等其它启动期
  数据改写；
* 不验证「老库里 owner_id 为 NULL 的历史角色行」被归为 preset 的行为
  —— 那是安全侧的**显式选择**（无法证明归属的行判给平台，而不是猜一个
  owner），不是缺陷；
* 场景是最简形态：全新库 + 单用户 + 派生/原创各一个角色。多角色、多用户、
  老库升级不在本场景内。

判据口径
--------
**不用时间戳挑行**：同秒写入的 ``created_at`` 在 SQLite 下无法区分先后，
按时间挑行会让结论随机翻转（见 ``dump_rows`` / ``uniqueness_of`` 注释）。
一律按 **id 唯一键**比 ``record_type`` / ``owner_id`` / 行数。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_ports import resolve_port

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable
DB = Path(tempfile.gettempdir()) / f"qa3_r2b_{os.getpid()}.db"
# 默认端口统一由 tools/smoke_ports.py 托管（唯一真源）——
# 早先各脚本各写一份端口，并行跑必然撞车（QA 亲历过一次：
# 本脚本的 8155 与手动指定的 smoke_roles 8155 撞了，
# 症状是 TimeoutError + 零断言失败，极易被误判成代码回归）。
PORT = resolve_port("qa3_r2_restart")   # 重启不变式：启动流程不得改写用户角色
BASE = f"http://127.0.0.1:{PORT}"


def req(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=25) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001  harness 有意吞掉一切解析异常：
            # 4xx 的 body 可能是 HTML/纯文本，解析失败按原文返回即可，
            # 断言只看 status code。宁要一次降级返回，不要整个脚本崩掉。
            return e.code, raw


def wait_ready(timeout=120.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if req("GET", "/api/health")[0] == 200:
                return True
        except Exception:  # noqa: BLE001, S110  服务未就绪时连接被拒是
            # 常态，轮询语义就是"试到超时为止"；静默是这里的正确行为，
            # 真正的失败由 120s 超时兜底（返回 False -> 调用方 return 1）。
            pass
        time.sleep(0.6)
    return False


def boot():
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{DB.as_posix()}"
    env["ELDER_JWT_SECRET"] = "qa3-r2b"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "error"],
        cwd=str(SERVER), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    return p, wait_ready()


def stop(p):
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()


def dump_rows(tag):
    """读出全部角色行，并按 **id 唯一键** 建索引。

    ⚠️ **不要用「取最新/最旧一条」来定位被测行**（2026-10-16 第四轮更正）
    ------------------------------------------------------------------
    本脚本早先被指出「按时间戳挑行」在 SQLite 下会抖（同秒写入的
    ``created_at`` 完全相同 -> 挑到哪一行取决于存储顺序 -> 偶发误报）。
    核对后确认：**本文件从未引用过 ``created_at``**，它一直是
    ``ORDER BY id`` + 按 id 建字典，所以那个描述与代码不符。

    但**同一个坑确实存在**，只是在本脚本之外：``server/app/main.py``
    的 ``_seed_migration`` 用 ``device.created_at <= preset.created_at``
    判断「存量设备」，而 SQLite 的 DATETIME 列读回来是 naive 值，
    同秒写入时精度不足以区分「早于」与「等于」。生产代码已用
    ``_as_utc()`` 在 Python 侧归一后比较（见该函数注释）。

    这里保持 id 唯一键口径，并补上**归属与数量**两个判据 ——
    ``owner_id + 行数 + record_type`` 是「这行还是不是原来那个角色」的
    **唯一性**判据，不依赖任何时间精度。
    """
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT id,name,kind,record_type,owner_id,deleted_at,archived_at "
        "FROM ai_providers ORDER BY id").fetchall()
    conn.close()
    print(f"\n  -- {tag} --")
    print(f"  {'id':>3} {'kind':<10} {'record_type':<12} {'owner':>5}  name")
    for r in rows:
        print(f"  {r[0]:>3} {(r[2] or '')!r:<12} {r[3]!r:<14} {r[4]!s:>5}  {r[1]}")
    return rows


def uniqueness_of(rows):
    """把行集合压成「唯一性指纹」：``{id: (record_type, owner_id)}`` + 行数。

    为什么用这个而不是「取最新一条」
    ------------------------------
    取最新一条依赖时间戳精度：同一秒写入的两行 ``created_at`` 完全相同，
    挑到哪一行取决于存储顺序，于是「排障结论」会随机翻转 ——
    这种 flaky 比没有断言更糟，因为它让人去查一个不存在的问题。

    ``owner_id + 行数 + 归属`` 这三项都是**精确值**，不随时间抖动：
    * 行数变了 -> 启动流程增删了行（那本身就是 bug）；
    * owner_id 变了 -> 归属被改写（P0 的核心症状）；
    * record_type 变了 ->角色被刷成 preset（另一个核心症状）。
    """
    return ({r[0]: (r[3], r[4]) for r in rows}, len(rows))


def main() -> int:
    if DB.exists():
        DB.unlink()
    proc, ready = boot()
    if not ready:
        print("[boot] 未就绪")
        return 1
    ua = ""
    rid = 0
    try:
        req("POST", "/api/auth/register",
            {"username": "r2b", "password": "test123456", "nickname": "r2b"})
        _, t = req("POST", "/api/auth/login",
                   {"username": "r2b", "password": "test123456"})
        ua = t.get("access_token", "")
        _, rl = req("GET", "/api/roles", token=ua)
        pid = rl["presets"][0]["id"]
        _, dv = req("POST", f"/api/roles/{pid}/derive",
                    {"name": "我派生的角色", "inherit": False}, ua)
        rid = dv.get("id", 0)
        _, o = req("POST", "/api/roles", {"name": "我原创的角色"}, ua)
        oid = o.get("id", 0)
        print(f"\n派生角色 id={rid}，原创角色 id={oid}")
    finally:
        stop(proc)

    rows = dump_rows("★ 首次启动后（派生/原创刚建成）")
    before = {r[0]: r[3] for r in rows}
    before_uniq, before_n = uniqueness_of(rows)
    print(f"\n  派生角色 record_type = {before.get(rid)!r}（应为 'role'）")
    print(f"  原创角色 record_type = {before.get(oid)!r}（应为 'role'）")
    print(f"  唯一性指纹：{before_n} 行 / 派生={before_uniq.get(rid)} / "
          f"原创={before_uniq.get(oid)}")

    # ---- 重启一次（模拟服务重启 / 部署 / 容器重建）----
    proc, ready = boot()
    if not ready:
        print("\n[2] 重启后服务未就绪")
        return 1
    # 判据变量**先于一切查询与请求**初始化（2026-10-16 门禁化改造）。
    # 门禁语义：查库或 HTTP 任一环节抛异常时，下方判定块拿到的必须是
    # 空值（每条具名判据自然判 FAIL），而不是"沿用上一次查询的旧值"
    # —— 后者是假绿，比崩溃更坏（崩溃会有人去查，假绿会让人相信结论）。
    changed: list[tuple[int, object, object]] = []
    moved: list[tuple[int, object, object]] = []
    rows_unchanged = False
    c_read = c_write = c_del = c_other = -1
    probe_error = ""
    try:
        rows2 = dump_rows("★ 重启后（第二次跑 backfill_record_types）")
        after = {r[0]: r[3] for r in rows2}
        after_uniq, after_n = uniqueness_of(rows2)
        print()
        for k, v in before.items():
            if after.get(k) != v:
                changed.append((k, v, after.get(k)))
        if changed:
            print("  ★★★ record_type 被改写的行（FAIL）：")
            for k, a, b in changed:
                print(f"      id={k}: {a!r} -> {b!r}")
        else:
            print("  record_type 未被改写")

        # ---- 唯一性判据（不依赖任何时间戳精度）----
        # 行数变了 = 启动流程增删了行；归属变了 = P0 的核心症状。
        # 这两条与上面的 record_type 比对互补：那一条查「类型」，
        # 这一条查「这一行还是不是原来那个角色、还有没有别的行」。
        rows_unchanged = before_n == after_n
        if not rows_unchanged:
            print(f"  ★★★ 行数被改写：{before_n} -> {after_n}"
                  f"（启动流程增删了角色行）")
        else:
            print(f"  行数未被改写（{after_n} 行）")
        for k, v in before_uniq.items():
            if after_uniq.get(k) != v:
                moved.append((k, v, after_uniq.get(k)))
        if moved:
            print("  ★★★ (record_type, owner_id) 被改写的行（FAIL）：")
            for k, a, b in moved:
                print(f"      id={k}: {a} -> {b}")

        # 用 HTTP 验证不变式的用户侧观感：owner 自己的角色仍可读写删
        _, t = req("POST", "/api/auth/login",
                   {"username": "r2b", "password": "test123456"})
        ua2 = t.get("access_token", "")
        c_read, _ = req("GET", f"/api/roles/{rid}", token=ua2)
        c_write, _ = req("PATCH", f"/api/roles/{rid}",
                         {"name": "改个名"}, ua2)
        c_del, _ = req("DELETE", f"/api/roles/{rid}", token=ua2)
        print("\n  ★ 重启后，owner 自己的派生角色：")
        print(f"      GET    /roles/{rid}          -> {c_read}（不变式：200）")
        print(f"      PATCH  /roles/{rid}          -> {c_write}（不变式：200）")
        print(f"      DELETE /roles/{rid}          -> {c_del}（不变式：204）")

        # 可见性不变式：另一个用户不能看到这个角色
        req("POST", "/api/auth/register",
            {"username": "r2c", "password": "test123456", "nickname": "r2c"})
        _, t2 = req("POST", "/api/auth/login",
                    {"username": "r2c", "password": "test123456"})
        ub = t2.get("access_token", "")
        c_other, _ = req("GET", f"/api/roles/{rid}", token=ub)
        print(f"\n  ★ 另一个用户读这个角色 -> {c_other}"
              f"（不变式：404；200 = 泄露）")
    except Exception as exc:  # noqa: BLE001  门禁语义：探针异常 = FAIL，
        # 不是崩溃、更不是沿用旧值。harness 有意吞掉一切异常，但这里必须
        # 留下类型与信息，让判定块能把失败归因到真因（probe_error）。
        probe_error = f"{type(exc).__name__}: {exc}"
        print(f"\n  ★★★ 重启后探针执行失败（按 FAIL 计）：{probe_error}")
    finally:
        stop(proc)

    # ======================================================== 回归门禁判定
    # 通过条件 = 重启不变式逐条保持。每条都有名字：红一条就能指认坏在哪，
    # 而不是一坨"未复现"。``clean`` 参与每一条：探针异常时没有任何一条
    # 有资格判 PASS（fail-closed）。
    print("\n" + "=" * 72)
    clean = not probe_error
    gates = [
        ("G1 record_type 未被改写", clean and not changed),
        ("G2 行数未被改写", clean and rows_unchanged),
        ("G3 (record_type, owner_id) 归属未被改写", clean and not moved),
        ("G4 owner 仍可读（200）", clean and c_read == 200),
        ("G5 owner 仍可写（200）", clean and c_write == 200),
        ("G6 owner 仍可删（204）", clean and c_del == 204),
        ("G7 他人仍不可见（404）", clean and c_other == 404),
    ]
    for name, g in gates:
        print(f"  [{'PASS' if g else 'FAIL'}] {name}")
    ok = all(g for _, g in gates)
    print("=" * 72)
    print("判定：" + ("回归门禁通过（重启不变式保持）" if ok
                     else "**回归！重启流程改写了用户角色（P0 重演）**"))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
