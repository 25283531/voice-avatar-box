"""QA 第 2 轮：**重启后用户派生角色变成只读** —— 疑似严重回归，定位用。

现象（qa3_r2_e2e.py 观察到）
--------------------------
对同一个用户派生角色（record_type='role', kind='', owner_id=U），
**重启服务**后：
    GET  /api/roles/{id}          -> 200（读得到）
    GET  /api/roles/{id}/diff     -> 404（写路径被拒）
    POST /api/roles/{id}/guardrails/soft -> 404
    GET  /api/roles/{id}/prompt   -> 404

同一个 token、同一个 id，读得到却写不了 —— 只能是 ``can_write`` 变了。

假设
----
``schema_patch.backfill_record_types()`` 里有这么一条：
    UPDATE ai_providers SET record_type = 'preset'
    WHERE (kind IS NULL OR kind = '') AND (record_type != 'preset')

而**用户派生角色的 kind 恒为空串**（角色不参与下发/导出）。于是每次启动
都会把**所有用户派生角色**的 record_type 从 'role' 刷成 'preset'，
而 ``can_write`` 对 preset 只认管理员 —— 于是用户自己的角色突然变成只读。

后果：家属派生一个角色、重启一次服务，角色就废了（改不了、删不了），
且因为被判成 preset，它还会**对全平台所有用户可见**。

本脚本直接查库确认这个假设。
"""
from __future__ import annotations

import json
import os
import shutil
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
        except Exception:
            return e.code, raw


def wait_ready(timeout=120.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if req("GET", "/api/health")[0] == 200:
                return True
        except Exception:
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
        print(f"  {r[0]:>3} {(r[2] or '')!r:<12} {r[3]!r:<14} {str(r[4]):>5}  {r[1]}")
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
    try:
        rows2 = dump_rows("★ 重启后（第二次跑 backfill_record_types）")
        after = {r[0]: r[3] for r in rows2}
        after_uniq, after_n = uniqueness_of(rows2)
        print()
        changed = []
        for k, v in before.items():
            if after.get(k) != v:
                changed.append((k, v, after.get(k)))
        if changed:
            print("  ★★★ record_type 被改写的行：")
            for k, a, b in changed:
                print(f"      id={k}: {a!r} -> {b!r}")
        else:
            print("  record_type 未被改写")

        # ---- 唯一性判据（不依赖任何时间戳精度）----
        # 行数变了 = 启动流程增删了行；归属变了 = P0 的核心症状。
        # 这两条与上面的 record_type 比对互补：那一条查「类型」，
        # 这一条查「这一行还是不是原来那个角色、还有没有别的行」。
        if before_n != after_n:
            print(f"  ★★★ 行数被改写：{before_n} -> {after_n}"
                  f"（启动流程增删了角色行）")
        else:
            print(f"  行数未被改写（{after_n} 行）")
        moved = [(k, v, after_uniq.get(k)) for k, v in before_uniq.items()
                 if after_uniq.get(k) != v]
        if moved:
            print("  ★★★ (record_type, owner_id) 被改写的行：")
            for k, a, b in moved:
                print(f"      id={k}: {a} -> {b}")

        # 用HTTP 验证这个改写是否真的伤害了用户
        _, t = req("POST", "/api/auth/login",
                   {"username": "r2b", "password": "test123456"})
        ua2 = t.get("access_token", "")
        c_read, _ = req("GET", f"/api/roles/{rid}", token=ua2)
        c_write, _ = req("PATCH", f"/api/roles/{rid}",
                         {"name": "改个名"}, ua2)
        c_del, _ = req("DELETE", f"/api/roles/{rid}", token=ua2)
        print(f"\n  ★ 重启后，owner 自己的派生角色：")
        print(f"      GET    /roles/{rid}          -> {c_read}（读得到）")
        print(f"      PATCH  /roles/{rid}          -> {c_write}（应200，实际被拒）")
        print(f"      DELETE /roles/{rid}          -> {c_del}（应 204）")

        # 可见性是否也泄露了：另一个用户能看到这个角色吗？
        req("POST", "/api/auth/register",
            {"username": "r2c", "password": "test123456", "nickname": "r2c"})
        _, t2 = req("POST", "/api/auth/login",
                    {"username": "r2c", "password": "test123456"})
        ub = t2.get("access_token", "")
        c_other, _ = req("GET", f"/api/roles/{rid}", token=ub)
        print(f"\n  ★ 另一个用户读这个角色 -> {c_other}"
              f"（**不应可见**；200 = 泄露）")
    finally:
        stop(proc)

    print("\n" + "=" * 72)
    verdict = bool(changed) and c_write == 404 and c_other == 200
    print("判定：" + ("**确认严重回归**" if verdict else "未复现（需进一步定位）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())