"""P0 反向验证：``patch_meta`` 标记位 + ``owner_id`` 判据，两层防线都必须真的有效。

这个 P0 的性质决定了验证方式
----------------------------
上一轮那个 P0 最恶劣的地方不是「判据写错了」，而是
**「每次启动都执行一次不可逆的业务数据改写」**。所以本脚本不满足于
「跑一次看看对不对」，而是专门攻击**幂等性**与**判据**两条：

* 只验「第一次改对」→ 会漏掉"改对了但每次都改"（那仍是事故，只是这次没后果）；
* 只验「第二次是0」→ 会漏掉"第二次是 0 但因为表被清空了"。

主理人给的**三条验收标准**逐条对应下面的 V1~V3 与 E2E 组。

三组核心变异
------------
V1 **判据被改坏**（去掉 ``owner_id IS NULL``）
    模拟「将来有人把 WHERE 改错一处」—— 这是 review 里几乎看不出来的那种改动。
    预期：``patch_meta`` 这层拦住它（第二次不再执行），数据不被改坏。
    ⚠️ 注意这**只对已经登记过的库**成立；首次执行时判据仍要是对的
    （所以两层都要，缺一不可 —— 见脚本末尾的说明）。

V2 **标记位被绕过**（``_is_applied`` 恒返回 False，等于没有标记位）
    预期：退化回旧行为 —— 用户角色被刷成 preset。用来证明
    **标记位真的在起作用**，而不是「反正判据也对所以看起来没事」。

V3 **误登记**（把幂等无副作用的补列也登记进 patch_meta）
    预期：补列**仍然必须每次都跑**。这条是反向的 —— 验证「只登记改业务数据的」
    这个区分真的被代码强制住了，而不是"什么都登记"（那会让标记位
    退化成"启动时跳过一切"的开关）。

三条验收标准
------------
A1 连跑两次 ``ensure_missing_columns()``：第二次 changed 全 0，
   且用户派生角色 ``record_type`` 仍是 ``'role'``。
A2 ``kind=''+owner_id=42+record_type='role'`` 跑两次 -> 第二次及以后仍是 role。
A3 ``kind=''+owner_id=NULL+record_type='role'``（历史脏数据）-> 被纠正为 preset。
E2E 派生角色 -> **重启两次服务** -> 仍能 PATCH 成功（端到端，走真实 HTTP）。

⚠️ **一个容易被自己骗到的点**（本脚本第一版就栽在这里）
    探针必须**真的造出用户角色**。库里若只有 preset 一行 ``kind=''``，
    那么「去掉 owner_guard」也看不出任何差别 —— 症状是探针**假绿**。
    所以 :func:`_seed_roles` 里用户角色是硬性前置。

用法：
    python tools/qa6_patch_meta.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
SCHEMA = SERVER / "app" / "schema_patch.py"
PY = sys.executable

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    return bool(ok)


def _env(tag: str) -> tuple[dict, Path, Path]:
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["PYTHONIOENCODING"] = "utf-8"
    tmp = Path(tempfile.mkdtemp(prefix=f"qa6_{tag}_"))
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = str(tmp)
    return env, tmp, tmp / "p.db"


def _run_py(code: str, tag: str, port: int | None = None) -> tuple[int, dict, str]:
    """在独立临时库里跑一段 python，返回 (exit, json结果, 原始输出)。"""
    env, tmp, dbp = _env(tag)
    env["ELDER_DB"] = f"sqlite:///{dbp.as_posix()}"
    env["ELDER_JWT_SECRET"] = "qa6"
    if port:
        env["ELDER_PORT"] = str(port)
    drv = tmp / "p.py"
    drv.write_text(code, encoding="utf-8")
    try:
        p = subprocess.run([PY, str(drv)], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
        out = p.stdout or ""
        idx = out.rfind("###JSON###")
        data: dict = {}
        if idx >= 0:
            tail = out[idx + len("###JSON###"):].strip()
            for cut in range(len(tail), 0, -1):
                try:
                    data = json.loads(tail[:cut])
                    break
                except json.JSONDecodeError:
                    continue
        return p.returncode, data, out + (p.stderr or "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 探针
# A1/A2/A3：纯 SQL 层，验「两次执行」与判据
PROBE_IDEMPOTENT = r'''
import json, sys
sys.path.insert(0, "server")
from app.db import init_db, engine, SessionLocal
from app.schema_patch import run_all, backfill_record_types
from app import models
from sqlalchemy import text

init_db()
# ⚠️ 必须先清库：V1 要在**同一个库文件**里跑两遍探针（先正确、后变异），
# 而探针会插入 username='u42' 的用户，第二次撞 UNIQUE 约束直接崩。
# 「跑一次绿、跑两次红」的门禁比没有门禁更糟。
with engine.begin() as c:
    for tbl in ("patch_meta", "agent_persona_profiles", "ai_providers", "users"):
        c.execute(text(f"DELETE FROM {tbl}"))
with SessionLocal() as db:
    u = models.User(username="u42", password_hash="x")
    db.add(u); db.commit(); uid = int(u.id)
    # A2 的前置：用户派生角色（kind='' + 有 owner + role）
    db.add(models.AIProvider(name="用户派生", kind="", record_type="role",
                             owner_id=uid, enabled=True))
    # A3 的前置：历史脏数据（kind='' + 无 owner + 错标 role）
    db.add(models.AIProvider(name="历史无主", kind="", record_type="role",
                             owner_id=None, enabled=True))
    # 平台预设（kind='' + 无owner + preset）不应被动
    db.add(models.AIProvider(name="平台预设", kind="", record_type="preset",
                             owner_id=None, enabled=True))
    # 一条平台配置，kind 非空，不该被动
    db.add(models.AIProvider(name="平台配置", kind="llm", record_type="config",
                             owner_id=None, enabled=True))
    db.commit()

r1 = run_all()
r2 = run_all()
r3 = run_all()          # 第三次：多跑一次确认稳定
r4 = backfill_record_types()   # 直接单点再调一次

def rows():
    with engine.begin() as c:
        return [dict(x) for x in c.execute(text(
            "SELECT name,kind,record_type,owner_id FROM ai_providers "
            "ORDER BY id")).mappings().all()]

def patches():
    with engine.begin() as c:
        try:
            return sorted(r[0] for r in c.execute(text(
                "SELECT patch_id FROM patch_meta")).all())
        except Exception as exc:
            return ["ERR:" + str(exc)]

print("###JSON###" + json.dumps(
    {"r1": r1, "r2": r2, "r3": r3, "r4": r4,
     "rows": rows(), "patches": patches()}, ensure_ascii=False, default=str))
'''


# E2E：真起服务、派生角色、重启两次、再 PATCH
SERVER_PROBE = r'''
import json, os, sys, time, urllib.request, urllib.error
sys.path.insert(0, "server")
PORT = int(os.environ["ELDER_PORT"])
BASE = f"http://127.0.0.1:{PORT}"

def req(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as exc:
        return 599, str(exc)

def wait_ready(timeout=120.0):
    end = time.time() + timeout
    while time.time() < end:
        if req("GET", "/api/health")[0] == 200:
            return True
        time.sleep(0.5)
    return False

assert wait_ready(), "服务未就绪"
req("POST", "/api/auth/register",
    {"username": "qa6user", "password": "test123456", "nickname": "qa6"})
_, tk = req("POST", "/api/auth/login",
            {"username": "qa6user", "password": "test123456"})
token = tk.get("access_token", "") if isinstance(tk, dict) else ""
if not token:
    raise SystemExit("###JSON###" + json.dumps(
        {"error": f"登录失败: {str(tk)[:200]}"}, ensure_ascii=False))

# 造一个平台预设当母版（库里 seed 的那个）
cp, presets = req("GET", "/api/roles/presets", token=token)
# ⚠️ 必须校验形状再取下标：/presets 失败时返回的是**字符串**（错误详情），
# 于是presets[0] 拿到一个字符、["id"] 抛TypeError —— 症状看起来像
# "P0 复现了"，实际只是探针自己写脆了。这类假红比假绿更费时间。
if not isinstance(presets, list) or not presets:
    raise SystemExit("###JSON###" + json.dumps(
        {"error": f"presets 不是非空列表: code={cp} body={str(presets)[:200]}"},
        ensure_ascii=False))
pid = presets[0].get("id")
if not pid:
    raise SystemExit("###JSON###" + json.dumps(
        {"error": f"preset 没有 id: {str(presets[0])[:200]}"}, ensure_ascii=False))

# 派生一个角色
c, child = req("POST", f"/api/roles/{pid}/derive",
               {"name": "我的小暖", "inherit": False}, token)
cid = child.get("id") if isinstance(child, dict) else 0

# 改一项，制造 overlay
c1, _ = req("PATCH", f"/api/roles/{cid}",
            {"name": "我的小暖", "description": "改过了",
             "system_prompt": "", "traits": {"verbosity": "verbose"}}, token)
print("###JSON###" + json.dumps(
    {"parent_id": pid, "child_id": cid, "first_patch": c1}, ensure_ascii=False))
'''


def _boot_server(env: dict, port: int, log: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "error"],
        cwd=str(SERVER), env=env, stdout=log.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")


def e2e_restart_twice(port: int) -> tuple[bool, str]:
    """派生 -> 重启 -> 重启 -> 每次重启后都 PATCH。返回 (是否始终成功, 详情)。

    ⚠️ 三轮必须**复用同一个库文件**，否则每轮都是全新库，等于没测「重启」。
    而角色是在第 1 轮建的，所以第 2、3 轮只做 PATCH 验证，**不能重新建**——
    第一版把 SERVER_PROBE 每轮都跑一遍，于是每轮都新建一个角色，
    PATCH 的永远是「刚建的那个」，**重启后的持久性根本没被验证**。
    """
    env, tmp, dbp = _env("e2e")
    env["ELDER_DB"] = f"sqlite:///{dbp.as_posix()}"
    env["ELDER_JWT_SECRET"] = "qa6-e2e"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"
    env["ELDER_PORT"] = str(port)
    log = tmp / "server.log"
    details: list[str] = []
    proc = None
    cid = 0
    try:
        for round_no in (1, 2, 3):
            proc = _boot_server(env, port, log)
            if round_no == 1:
                rc, data, raw = _run_py_with_env(SERVER_PROBE, env)
                if not data:
                    return False, "首轮探针失败：" + raw[-500:]
                cid = int(data.get("child_id") or 0)
                if not cid:
                    return False, f"首轮没拿到 child_id：{data}"
                if data.get("first_patch") != 200:
                    return False, f"首轮 PATCH 就失败：{data.get('first_patch')}"
                details.append(f"第1轮 建角色+改={data['first_patch']}")
            else:
                # ⚠️ 返回值顺序是 (进程返回码, HTTP 状态码, 原始输出)。
                # 第一版按 (code, body, raw) 解包，于是把「进程返回码 0」
                # 当成了 HTTP 状态码 —— 明明 PATCH 成功(=200) 却判失败。
                # 这类错误必须归零：它会把**通过**报成**不通过**，
                # 而更糟的是它只在成功路径上出现，测得越顺越容易踩。
                _rc, code, body = _run_py_patch(env, cid)
                details.append(f"第{round_no}轮 重启后 PATCH={code}")
                if code != 200:
                    return False, (f"第 {round_no} 次重启后 PATCH 失败："
                                   f"code={code} body={str(body)[:300]}"
                                   f" serverlog={_tail(log)}")
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            proc = None
        return True, " → ".join(details)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def _run_py_with_env(code: str, env: dict) -> tuple[int, dict, str]:
    tmp = Path(env["TMPDIR"])
    drv = tmp / "e2e.py"
    drv.write_text(code, encoding="utf-8")
    p = subprocess.run([PY, str(drv)], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    out = p.stdout or ""
    idx = out.rfind("###JSON###")
    data: dict = {}
    if idx >= 0:
        tail = out[idx + len("###JSON###"):].strip()
        for cut in range(len(tail), 0, -1):
            try:
                data = json.loads(tail[:cut])
                break
            except json.JSONDecodeError:
                continue
    return p.returncode, data, out + (p.stderr or "")


def _run_py_patch(env: dict, cid: int) -> tuple[int, object, str]:
    code = (
        "import json,os,sys,urllib.request,urllib.error\n"
        "sys.path.insert(0,'server')\n"
        f"PORT=int(os.environ['ELDER_PORT']); BASE=f'http://127.0.0.1:{{PORT}}'\n"
        "def req(m,p,b=None,t=None):\n"
        "    d=json.dumps(b).encode() if b is not None else None\n"
        "    r=urllib.request.Request(BASE+p,data=d,method=m)\n"
        "    r.add_header('Content-Type','application/json')\n"
        "    if t: r.add_header('Authorization','Bearer '+t)\n"
        "    try:\n"
        "        with urllib.request.urlopen(r,timeout=30) as x:\n"
        "            raw=x.read().decode('utf-8')\n"
        "            return x.status,(json.loads(raw) if raw else None)\n"
        "    except urllib.error.HTTPError as e:\n"
        "        raw=e.read().decode('utf-8')\n"
        "        try: return e.code,json.loads(raw)\n"
        "        except Exception: return e.code,raw\n"
        "    except Exception as e: return 599,str(e)\n"
        # 必须**先等健康检查**再登录：服务刚起时端口已监听但 lifespan 里的
        # init_db/run_all 还没跑完，此时登录会失败。第一版没等，于是第2 轮
        # 直接 login 失败 —— 看起来像 P0 复现，实际是**竞态**。
        # 这类假红比假绿更费时间：它会让人去查一个不存在的 bug。
        "import time as _t\n"
        "_ok=False; _end=_t.time()+60\n"
        "while _t.time()<_end:\n"
        "    if req('GET','/api/health')[0]==200:\n"
        "        _ok=True; break\n"
        "    _t.sleep(0.5)\n"
        "if not _ok:\n"
        "    print('###PATCH###'+json.dumps({'code':-2,'err':'health timeout'},ensure_ascii=False))\n"
        "    raise SystemExit(0)\n"
        # ⚠️ 必须**重新登录**拿 token：服务重启后旧 token 理论上仍可用，
        # 但若拿不到，多半是用户真没了（那才是 P0 的症状），所以不能沿用。
        "c,tk=req('POST','/api/auth/login',"
        "{'username':'qa6user','password':'test123456'})\n"
        "t=tk.get('access_token','') if isinstance(tk,dict) else ''\n"
        "if not t:\n"
        "    print('###PATCH###'+json.dumps({'code':-1,'err':'login failed: %s' % str(tk)[:200]},ensure_ascii=False))\n"
        "    raise SystemExit(0)\n"
        f"c,body=req('PATCH','/api/roles/{cid}',"
        "{'name':'我的小暖','description':'重启后改',"
        "'system_prompt':'','traits':{'verbosity':'short'}},t)\n"
        "print('###PATCH###'+json.dumps({'code':c,'body':str(body)[:200]},ensure_ascii=False))\n"
    )
    rc, _d, raw = _run_py_with_env(code, env)
    idx = raw.rfind("###PATCH###")
    if idx < 0:
        return rc, raw[-300:], raw
    try:
        return rc, json.loads(raw[idx + len("###PATCH###"):].strip()).get("code"), raw
    except json.JSONDecodeError:
        return rc, None, raw


# ================================================================ 变异
M_OWNER_GUARD_FROM = '        owner_guard = " AND owner_id IS NULL" if has_owner else ""'
M_OWNER_GUARD_TO = '        owner_guard = ""'
M_APPLIED_FROM = '''def _is_applied(conn: Any, patch_id: str) -> bool:
    """这个改业务数据的迁移是否已经执行过。"""
    try:'''
M_APPLIED_TO = '''def _is_applied(conn: Any, patch_id: str) -> bool:
    """这个改业务数据的迁移是否已经执行过。"""
    return False
    try:'''


# V1 用：**不清 patch_meta**，只清业务行。
# ⚠️ 这一点是V1 的全部意义：它要在**已登记**的库里把判据改坏，
# 验证标记位兜得住。若这里也把 patch_meta 删了，第二轮就变成"全新库"，
# 标记位自然不存在 —— 于是测到的是"判据错就改坏数据"（本来就该如此），
# 而不是"标记位能不能兜住"。那就白测了。
PROBE_IDEMPOTENT_KEEP_META = PROBE_IDEMPOTENT.replace(
    'for tbl in ("patch_meta", "agent_persona_profiles", "ai_providers", "users"):',
    'for tbl in ("agent_persona_profiles", "ai_providers", "users"):')

# V2 用：把登记表整个删掉，验证「表不存在 -> 当没登记过 -> 照常执行」
PROBE_DROP_META = r'''
import json, sys
sys.path.insert(0, "server")
from app.db import init_db, engine, SessionLocal
from app.schema_patch import run_all
from app import models
from sqlalchemy import text

init_db()
with engine.begin() as c:
    c.execute(text("DROP TABLE IF EXISTS patch_meta"))
# 再塞一行新的历史脏数据（表已没，标记位无从登记）
with SessionLocal() as db:
    db.add(models.AIProvider(name="脏数据A", kind="", record_type="role",
                             owner_id=None, enabled=True))
    db.commit()

r = run_all()
def rt(name):
    with engine.begin() as c:
        row = c.execute(text(
            "SELECT record_type FROM ai_providers WHERE name = :n"),
            {"n": name}).first()
        return row[0] if row else None

first_legacy = rt("历史无主")
with SessionLocal() as db:
    db.add(models.AIProvider(name="脏数据B", kind="", record_type="role",
                             owner_id=None, enabled=True))
    db.commit()
run_all()
second_legacy = rt("脏数据B")
print("###JSON###" + json.dumps(
    {"changed": r, "legacy_after": first_legacy,
     "second_legacy_after": second_legacy}, ensure_ascii=False, default=str))
'''


def _run_py_with_env_env(code: str, env: dict) -> tuple[int, dict, str]:
    """在**指定 env**（含已存在的库文件）里跑代码，用于「先跑一次再变异」的时序。"""
    tmp = Path(env["TMPDIR"])
    drv = tmp / "seq.py"
    drv.write_text(code, encoding="utf-8")
    p = subprocess.run([PY, str(drv)], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    out = p.stdout or ""
    idx = out.rfind("###JSON###")
    data: dict = {}
    if idx >= 0:
        tail = out[idx + len("###JSON###"):].strip()
        for cut in range(len(tail), 0, -1):
            try:
                data = json.loads(tail[:cut])
                break
            except json.JSONDecodeError:
                continue
    return p.returncode, data, out + (p.stderr or "")


def mutate(path: Path, old: str, new: str) -> str:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"{path.name} 找不到待替换片段，脚本需更新：{old[:70]!r}")
    path.write_text(src.replace(old, new, 1), encoding="utf-8")
    return src


def restore(path: Path, original: str, mutated_hint: str | None = None) -> None:
    """复原文件，但**只在没人动过它**的前提下写回。

    ⚠️ 与 qa5_reverse 同一条教训（本轮实测踩到）：本脚本与别的 agent
    共用同一个工作区。若在「我变异完、还没复原」的窗口里对方也改了同一个
    文件，无脑写回 original 会**把对方的改动整段覆盖掉** ——
    也就是「为了复原自己的变异，反而制造了别人的数据丢失」。
    实测症状：脚本报「复原后仍红」，而文件其实停在正确版本。

    所以：内容仍等于变异版才写回；否则跳过并大声报警。
    宁可不复原（人可介入），不可覆盖别人的工作。
    """
    current = path.read_text(encoding="utf-8")
    if current == original:
        return
    if mutated_hint is not None and current != mutated_hint:
        print(f"⚠️ 跳过复原 {path.name}：内容既不是原始版也不是本脚本的变异版，"
              f"说明期间有第三方改动。为避免覆盖别人的工作，不写回。"
              f"请人工确认该文件状态。")
        return
    path.write_text(original, encoding="utf-8")


def _tail(log: Path, n: int = 600) -> str:
    """读服务日志尾部。失败时没有它就只剩一个 HTTP 状态码，无法判断原因。"""
    try:
        return log.read_text(encoding="utf-8", errors="replace")[-n:]
    except Exception:  # noqa: BLE001
        return "(无日志)"


def by_name(rep: dict, name: str) -> dict:
    for r in rep.get("rows", []):
        if r["name"] == name:
            return r
    return {}


def main() -> int:
    print("=" * 72)
    print("P0 反向验证：patch_meta 标记位 + owner_id 判据（两层防线）")
    print("=" * 72)

    # ---------- A1/A2/A3 ----------
    print("\n── A1/A2/A3 纯 SQL 层：连跑三次 run_all ──")
    rc, rep, raw = _run_py(PROBE_IDEMPOTENT, "idem")
    if rep is None or not rep:
        print(raw[-2500:])
        check("探针跑通", False)
        return 1
    check("探针跑通", True)

    r1, r2, r3, r4 = rep["r1"], rep["r2"], rep["r3"], rep["r4"]
    print(f"    第1次 = {r1}")
    print(f"    第2次 = {r2}")
    print(f"    第3次 = {r3}")
    print(f"    第4次（单点调 backfill）= {r4}")
    print(f"    已登记迁移 = {rep['patches']}")
    for r in rep["rows"]:
        print(f"      {r['name']}: kind={r['kind']!r} "
              f"record_type={r['record_type']} owner_id={r['owner_id']}")

    # A1：第二次必须全 0
    check("A1 第2次 run_all 全为 0", not any(r2.values()), json.dumps(r2))
    check("A1 第3次 run_all 全为 0", not any(r3.values()), json.dumps(r3))
    check("A1 第4次单点调 backfill 为 0", int(r4 or 0) == 0, str(r4))

    # A2：用户派生角色必须仍是 role
    derived = by_name(rep, "用户派生")
    check("A2 用户派生角色 record_type 仍是 'role'",
          derived.get("record_type") == "role", json.dumps(derived, default=str))
    check("A2 用户派生角色 owner_id 未被改动",
          derived.get("owner_id") == 1, json.dumps(derived, default=str))

    # A3：历史脏数据（无 owner + role）必须被纠正为 preset
    legacy = by_name(rep, "历史无主")
    check("A3 历史无主行被纠正为 'preset'",
          legacy.get("record_type") == "preset", json.dumps(legacy, default=str))

    preset = by_name(rep, "平台预设")
    check("A3 平台预设仍是 'preset'（没被误伤）",
          preset.get("record_type") == "preset", json.dumps(preset, default=str))
    cfg = by_name(rep, "平台配置")
    check("A3 平台配置仍是 'config'（kind 非空不受影响）",
          cfg.get("record_type") == "config", json.dumps(cfg, default=str))

    # 标记位确实登记了，且只登记了改业务数据的两个
    check("patch_meta 登记了 record_type 回填",
          "record_type_owner_guard_v1" in rep["patches"], str(rep["patches"]))
    check("patch_meta 登记了画像去重",
          "persona_agent_key_v1" in rep["patches"], str(rep["patches"]))
    check("补列/补索引**未被**登记（区分必须被代码强制）",
          not any(p.startswith(("column", "index")) for p in rep["patches"]),
          str(rep["patches"]))

    # ---------- V1：判据被改坏，但库已登记过 -> 标记位必须兜住 ----------
    # ⚠️ 这里**必须先跑一次正确的**（让 patch_meta 登记），再改坏判据。
    # 第一版直接在一个全新库里改坏判据，然后期望标记位兜住 —— 那是**错的**：
    # 全新库里标记位还没登记，第一次执行时没有任何东西能保护它，
    # 于是判据错就必然改坏数据。这恰恰说明**两层缺一不可**：
    # 标记位只能保护「第一次之后」，第一次仍然靠判据。
    # 所以 V1 测的必须是「判据在登记之后被改坏」这个真实场景。
    print("\n── V1 库已登记过之后，把判据改坏 -> 标记位必须兜住 ──")
    env_v1, tmp_v1, dbv1 = _env("v1")
    env_v1["ELDER_DB"] = f"sqlite:///{dbv1.as_posix()}"
    env_v1["ELDER_JWT_SECRET"] = "qa6-v1"
    # 先用「保留 patch_meta」的探针跑一次 -> 登记表建立、判据正确
    _r, _d, _raw = _run_py_with_env_env(PROBE_IDEMPOTENT_KEEP_META, env_v1)
    src = SCHEMA.read_text(encoding="utf-8")
    mutate(SCHEMA, M_OWNER_GUARD_FROM, M_OWNER_GUARD_TO)
    try:
        # 判据改坏后再跑：标记位已存在 -> 必须跳过回填 -> 数据不动
        _r, rep1, raw1 = _run_py_with_env_env(PROBE_IDEMPOTENT_KEEP_META, env_v1)
        if rep1:
            d1 = by_name(rep1, "用户派生")
            check("V1 判据在登记后被改坏，用户角色仍未被刷成 preset",
                  d1.get("record_type") == "role",
                  json.dumps(d1, default=str))
            check("V1 判据改坏后回填被跳过（changed=0，标记位生效）",
                  int(rep1["r1"].get("record_types_backfilled") or 0) == 0,
                  json.dumps(rep1["r1"], default=str))
        else:
            check("V1 探针跑通", False, raw1[-300:])
    finally:
        restore(SCHEMA, src)
        shutil.rmtree(tmp_v1, ignore_errors=True)

    # ---------- V2：标记位不存在（老库没有该表）-> 必须退回「靠判据」 ----------
    # 这一条验的是**降级方向**：登记表不可用时不能当成「已登记」
    # （那会让老库的 record_type 永远不被修正，且**静默**）。
    # 做法：把登记表整个删掉，再确认回填**仍然执行**（修好历史脏数据）。
    print("\n── V2 登记表缺失（老库场景）-> 必须降级为「照常执行」而非跳过 ──")
    env_v2, tmp_v2, dbv2 = _env("v2")
    env_v2["ELDER_DB"] = f"sqlite:///{dbv2.as_posix()}"
    env_v2["ELDER_JWT_SECRET"] = "qa6-v2"
    _r, _d, _raw = _run_py_with_env_env(PROBE_IDEMPOTENT, env_v2)
    rc_drop, rep_drop, raw_drop = _run_py_with_env_env(PROBE_DROP_META, env_v2)
    if rep_drop:
        check("V2 删掉 patch_meta 表后回填仍然执行（老库脏数据被修正）",
              rep_drop.get("legacy_after") == "preset"
              and rep_drop.get("changed") is not None,
              json.dumps(rep_drop, default=str))
        check("V2 删表后第二个脏数据行也被修正（证明不是「跳过」）",
              rep_drop.get("second_legacy_after") == "preset",
              json.dumps(rep_drop, default=str))
    else:
        check("V2 探针跑通", False, raw_drop[-400:])

    # ---------- V3：误登记（把补列也登记）必须被架构挡住 ----------
    print("\n── V3 误把补列登记进 patch_meta：补列必须仍每次执行 ──")
    # 直接验「补列函数源码里没有 _is_applied」—— 这是「不登记」的结构保证
    body = SCHEMA.read_text(encoding="utf-8")
    fn_start = body.index("def ensure_missing_columns()")
    fn_end = body.index("def backfill_record_types()")
    col_body = body[fn_start:fn_end]
    check("V3 ensure_missing_columns 里没有标记位守卫（补列必须每次跑）",
          "_is_applied" not in col_body,
          "补列里出现了 _is_applied —— 误登记，幂等补丁被跳过了")

    fn_start2 = body.index("def ensure_missing_indexes()")
    idx_body = body[fn_start2:]
    check("V3 ensure_missing_indexes 里没有标记位守卫（补索引必须每次跑）",
          "_is_applied" not in idx_body, "")

    # ---------- E2E：真起服务，重启两次 ----------
    print("\n── E2E 派生角色 → 重启两次 → 仍能 PATCH ──")
    port = 9700 + (os.getpid() % 200)
    ok, detail = e2e_restart_twice(port)
    check("E2E 派生角色重启两次后仍可 PATCH", ok, detail)

    print("\n" + "=" * 72)
    bad = [n for n, ok_, _d in results if not ok_]
    passed = sum(1 for _n, ok_, _d in results if ok_)
    print(f"==== {passed}/{len(results)} passed ====")
    for n in bad:
        print(f"  FAILED: {n}")
    print("VERDICT:", "PASS" if not bad else "FAIL")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())