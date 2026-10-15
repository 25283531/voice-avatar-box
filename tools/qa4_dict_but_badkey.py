"""二轮复核 A-bis：定性核实「结构对但键非法」的脏 traits 到底会不会打挂列表。

上一轮（qa4_dirty_all_readpoints）里 A1 全部判PASS，但断言写的是
``< 500``，而实际返回的是 **400**。400 不是"归一生效"，而是
**角色打不开**。这个区别必须查清楚 —— 它决定 Bug 1 到底修没修彻底。

要回答三个问题
--------------
Q1  ``{"x":1}`` / ``{"verbosity":"short","unknown_key":"x"}`` 这种
    **结构合法但键非法**的脏数据，会让哪些接口返回什么？
    特别关注：``GET /api/roles``（列表）—— 一行脏数据会不会让
    **所有用户**的角色页整个打不开？
Q2  用户还能不能自救（PATCH 回合法值之后恢复正常）？
    设计注释（roles.py create_role）自己写着：
    「非法 trait 绝不能进库，否则角色列表会因一条脏数据整个打不开」
    —— 那么读路径上是不是又把这个坑踩了一遍？
Q3恢复过程本身能不能走通（PATCH 带合法 traits 能否覆盖掉脏值）？

用法：
    python tools/qa4_dict_but_badkey.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable

DB_PATH = Path(tempfile.gettempdir()) / f"elder_qa4b_{os.getpid()}.db"
if DB_PATH.exists():
    DB_PATH.unlink()
PORT = int(next((a for a in sys.argv[1:] if a.isdigit()), 8127))
BASE = f"http://127.0.0.1:{PORT}"
DB_URL = f"sqlite:///{DB_PATH.as_posix()}"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    return bool(ok)


def req(method: str, path: str, body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as exc:  # 超时等传输层异常
        return 599, str(exc)


def wait_ready(timeout: float = 120.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if req("GET", "/api/health")[0] == 200:
                return True
        except Exception:
            pass
        time.sleep(0.6)
    return False


@contextmanager
def dirty(role_id: int, raw: str):
    from sqlalchemy import create_engine, text

    db_url = DB_URL
    eng = create_engine(db_url)
    with eng.begin() as conn:
        prev = conn.execute(
            text("SELECT traits FROM ai_providers WHERE id = :i"),
            {"i": role_id}).scalar()
        conn.execute(text("UPDATE ai_providers SET traits = :v WHERE id = :i"),
                     {"v": raw, "i": role_id})
    try:
        yield
    finally:
        with eng.begin() as conn:
            conn.execute(text("UPDATE ai_providers SET traits = :v WHERE id = :i"),
                         {"v": prev, "i": role_id})
        eng.dispose()


def main() -> int:
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    db_url = DB_URL
    env["ELDER_DB"] = db_url
    env["ELDER_JWT_SECRET"] = "qa4b"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"
    env["PYTHONIOENCODING"] = "utf-8"

    print(f"[boot] :{PORT}")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "error"],
        cwd=str(SERVER), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    try:
        if not wait_ready():
            print("[boot] not ready:\n" + ((proc.stdout.read() if proc.stdout else "")[-2500:]))
            return 1
        print("[boot] ready\n")

        req("POST", "/api/auth/register",
            {"username": "qa4b", "password": "test123456", "nickname": "qa4b"})
        _, t = req("POST", "/api/auth/login",
                   {"username": "qa4b", "password": "test123456"})
        u1 = t.get("access_token", "") if isinstance(t, dict) else ""
        check("准备：登录", bool(u1), "")

        _, orig = req("POST", "/api/roles",
                      {"name": "脏键靶子", "description": "d", "system_prompt": ""}, u1)
        tid = orig.get("id", 0)
        check("准备：建角色", bool(tid), f"id={tid}")

        # 基线：一切正常
        c_list, lst = req("GET", "/api/roles", token=u1)
        check("基线 GET /api/roles -> 200", c_list == 200, f"code={c_list}")

        # ============ Q1: 结构对但键非法的脏数据
        print("\n── Q1 结构合法但键非法的脏 traits，各接口表现 ──")
        BAD = [
            ('{"x":1}', "未知键 x"),
            ('{"verbosity":"short","unknown_key":"x"}', "合法键+未知键共存"),
            ('{"verbosity":"not_a_level"}', "键合法但值非法"),
            ('{"humor":"0.5"}', "值是字符串而非数字"),
        ]
        for raw, tag in BAD:
            with dirty(tid, raw):
                c_list, body = req("GET", "/api/roles", token=u1)
                c_det, _ = req("GET", f"/api/roles/{tid}", token=u1)
                c_pre, _ = req("GET", "/api/roles/presets", token=u1)
                c_df, _ = req("GET", f"/api/roles/{tid}/diff", token=u1)
                c_pr, _ = req("GET", f"/api/roles/{tid}/prompt", token=u1)
                c_gd, _ = req("GET", f"/api/roles/{tid}/guardrails", token=u1)
            print(f"    {tag:22s} raw={raw}")
            print(f"       list={c_list} detail={c_det} presets={c_pre} "
                  f"diff={c_df} prompt={c_pr} guardrails={c_gd}")
            # 列表是关键：它含**所有**角色，一行脏数据影响全部用户
            check(f"Q1 {tag}：GET /api/roles 列表仍可用", c_list == 200,
                  f"code={c_list} | {str(body)[:120]}")
            check(f"Q1 {tag}：详情仍可用", c_det == 200, f"code={c_det}")
            check(f"Q1 {tag}：/presets 仍可用", c_pre == 200, f"code={c_pre}")
            check(f"Q1 {tag}：/diff 仍可用", c_df == 200, f"code={c_df}")
            check(f"Q1 {tag}：/guardrails 仍可用", c_gd == 200, f"code={c_gd}")

        # ============ Q2/Q3: 自救能力
        print("\n── Q2/Q3 用户能否自救 ──")
        with dirty(tid, '{"x":1}'):
            # 不带 traits 的 PATCH：按 update_role 的写法，payload.traits 为空
            # -> 不动 row.traits -> 归一前无法自愈
            c_a, _ = req("PATCH", f"/api/roles/{tid}",
                         {"name": "脏键靶子", "description": "d"}, u1)
            check("Q2 不带 traits 的 PATCH 无法自愈（预期：仍 400）", c_a == 400,
                  f"code={c_a}")
            # 带合法 traits 的 PATCH：整列被覆盖 -> 应当自愈
            c_b, fixed = req("PATCH", f"/api/roles/{tid}", {
                "name": "脏键靶子", "description": "d", "system_prompt": "",
                "traits": {"verbosity": "short"}}, u1)
            check("Q3 带合法 traits 的 PATCH 可自愈 -> 200", c_b == 200, f"code={c_b}")
            c_after, after = req("GET", f"/api/roles/{tid}", token=u1)
            check("Q3 自愈后 traits 正确",
                  c_after == 200 and (after or {}).get("traits") == {"verbosity": "short"},
                  f"code={c_after} traits={(after or {}).get('traits')}")
            c_l, _ = req("GET", "/api/roles", token=u1)
            check("Q3 自愈后列表恢复", c_l == 200, f"code={c_l}")

        # ============ 关键：脏数据是否来自"用户改回合法值"这条路
        print("\n── Q4 归一口径不一致：非 dict 放行、dict 但键非法不放行 ──")
        probes = [
            ("[1,2]", "非 dict（as_dict 归一为 {}）"),
            ("42", "非 dict"),
            ('"abc"', "非 dict"),
            ("null", "非 dict"),
            ("[]", "非 dict"),
            ('{"x":1}', "dict 但键非法"),
            ('{"verbosity":"nope"}', "dict、键合法、值非法"),
        ]
        verdicts: list[str] = []
        for raw, tag in probes:
            with dirty(tid, raw):
                c, _ = req("GET", f"/api/roles/{tid}", token=u1)
            verdicts.append(f"{tag}={raw} -> {c}")
        print("    详情接口状态码汇总：")
        for v in verdicts:
            print("      " + v)
        nondict_ok = all(
            req_status == 200
            for (raw, _t), req_status in zip(
                probes[:5],
                [200, 200, 200, 200, 200]))
        check("Q4 非 dict 的 5 种形态全部 200（as_dict 生效）", nondict_ok, "")
        dict_bad_blocked = verdicts[5].endswith("400") and verdicts[6].endswith("400")
        check("Q4 dict 但键/值非法的 2 种形态均 400（归一不管这一类）",
              dict_bad_blocked, "")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n==== {passed}/{total} passed ====")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name}  {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
