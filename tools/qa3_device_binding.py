"""QA：独立验证 `_seed_migration` 步骤 4 的设备绑定回填修复。

这是「启动流程改写用户数据」的**第二个实例**（第一个是 record_type），
工程师报告说已用 `Device.created_at <= preset.created_at` 作为判据修掉。

我要验三件事（其中第2、3 条是我自己构造的，他没提）：
  1. **存量设备**（角色模型落地前就存在）-> 仍被正确回填
  2. **新建设备**（preset 之后创建）-> 重启后**不得**凭空被绑角色
  3. **用户主动解绑后** -> 重启后**不得**被强行绑回（解绑必须持久）

第 3 条最容易漏：DELETE /roles/{id} 会把 agent_id 置空，
若回填只看「agent_id IS NULL」，解绑就是无效的。

⚠️ 判定的关键：**不要读 DeviceOut 的 agent_id** —— 该schema 刻意不暴露它，
读了永远是 None，断言会空转变绿（我自己上一版就踩了这个坑）。
本脚本一律用**外部可见信号**：/api/device/sync 下发的音色
（角色决定音色，角色缺失才回落到形象的默认音色），或首页的 agent_name。
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_ports import resolve_port

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable


def _preset_role_name() -> str:
    """平台预设角色的名字，从**代码里的单一真源**取，不写字面量。

    为什么绕开 `import app.persona.catalog`：那会连带执行
    `app/persona/__init__.py`（进而拉进 compiler/models/sqlalchemy），
    而这个脚本只需要一个字符串常量。用 importlib 直接按文件加载
    catalog.py —— 它只依赖 stdlib，加载成本接近零。
    2026-10-21 品牌改名（小暖→小智）时这里原本写死的字面量假红过一次。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_elder_catalog", SERVER / "app" / "persona" / "catalog.py")
    mod = importlib.util.module_from_spec(spec)
    # 必须先登记进 sys.modules 再 exec：catalog.py 里有 @dataclass，
    # dataclasses 处理注解时要回头查 sys.modules[cls.__module__]，
    # 没登记就会 AttributeError: 'NoneType' object has no attribute '__dict__'。
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)          # type: ignore[union-attr]
    return mod.PRESET_ROLE_NAME


PRESET_ROLE_NAME = _preset_role_name()

DB = Path(tempfile.gettempdir()) / f"qa3_dev_{os.getpid()}.db"
# 默认端口统一由 tools/smoke_ports.py 托管（唯一真源）——
# 早先各脚本各写一份端口，并行跑必然撞车（QA 亲历过一次：
# 本脚本的 8155 与手动指定的 smoke_roles 8155 撞了，
# 症状是 TimeoutError + 零断言失败，极易被误判成代码回归）。
PORT = resolve_port("qa3_device_binding")   # 设备绑定回填：解绑持久 + 存量回填两头复核
BASE = f"http://127.0.0.1:{PORT}"
fails: list[str] = []


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


def ok(name, cond, detail=""):
    if not cond:
        fails.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


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
    env["ELDER_JWT_SECRET"] = "qa3-dev"
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


def agent_name_of(dev_pk: int, ua: str):
    """用首页的 agent_name 作为「是否绑了角色」的外部可见信号。

    为什么不用 DeviceOut.agent_id：该字段**刻意不暴露**，
    读了永远是 None -> 断言空转（上一版就是这么错的）。
    """
    _, hs = req("GET", "/api/home/summary", token=ua)
    for d in (hs or {}).get("devices", []):
        if d.get("id") == dev_pk:
            return d.get("agent_name")
    return "<设备未找到>"


def main() -> int:
    if DB.exists():
        DB.unlink()
    proc, ready = boot()
    if not ready:
        print("[boot] 未就绪")
        return 1
    ua = ""
    try:
        req("POST", "/api/auth/register",
            {"username": "devu", "password": "test123456", "nickname": "d"})
        _, t = req("POST", "/api/auth/login",
                   {"username": "devu", "password": "test123456"})
        ua = t.get("access_token", "")
        _, rl = req("GET", "/api/roles", token=ua)
        pid = rl["presets"][0]["id"]
        # 新建设备（此刻 preset 已存在，所以它是「后创建的」）
        _, d1 = req("POST", "/api/devices", {"device_id": "devnew0001"}, ua)
        new_pk = d1.get("id", 0)
        ok("新建设备建成", bool(new_pk), f"id={new_pk}")
        # 派生一个角色，稍后用它测「解绑后不被绑回」
        _, dv = req("POST", f"/api/roles/{pid}/derive",
                    {"name": "解绑测试", "inherit": False}, ua)
        rid = dv.get("id", 0)
        n0 = agent_name_of(new_pk, ua)
        print(f"  重启前：新建设备的 agent_name = {n0!r}（应为 None，从未绑过）")
    finally:
        stop(proc)

    # ---------- 重启一次：新建设备不得被凭空绑上角色 ----------
    proc, ready = boot()
    if not ready:
        print("[2] 重启未就绪")
        return 1
    try:
        _, t = req("POST", "/api/auth/login",
                   {"username": "devu", "password": "test123456"})
        ua2 = t.get("access_token", "")
        n1 = agent_name_of(new_pk, ua2)
        print(f"\n  -- 重启 #1 之后 --")
        print(f"  新建设备 agent_name = {n1!r}")
        ok("★ 新建设备未被凭空绑上角色（期望 None）", n1 is None, str(n1))

        # ---------- 再绑一个角色，然后解绑，再重启 ----------
        req("POST", f"/api/devices/{new_pk}/agent", {"role_id": rid}, ua2)
        n2 = agent_name_of(new_pk, ua2)
        ok("绑定角色后 agent_name 回显角色名", n2 == "解绑测试", str(n2))
    finally:
        stop(proc)

    # 解绑：DELETE 角色会连带把 agent_id 置空（角色删除接口的行为）
    # 这里直接删角色 -> 设备自动解绑（roles.delete_role 里有解绑逻辑）
    proc, ready = boot()
    if not ready:
        print("[3] 重启未就绪")
        return 1
    try:
        _, t = req("POST", "/api/auth/login",
                   {"username": "devu", "password": "test123456"})
        ua3 = t.get("access_token", "")
        c, _ = req("DELETE", f"/api/roles/{rid}", token=ua3)
        ok(f"删除角色 -> {c}（软删并自动解绑设备）", c == 204, f"code={c}")
        n3 = agent_name_of(new_pk, ua3)
        print(f"  删除角色后 agent_name = {n3!r}（应为 None）")
        ok("删除角色后设备已解绑", n3 is None, str(n3))
    finally:
        stop(proc)

    # ---------- 再重启：解绑必须持久（不得被强行绑回 preset）----------
    proc, ready = boot()
    if not ready:
        print("[4] 重启未就绪")
        return 1
    try:
        _, t = req("POST", "/api/auth/login",
                   {"username": "devu", "password": "test123456"})
        ua4 = t.get("access_token", "")
        n4 = agent_name_of(new_pk, ua4)
        print(f"\n  -- 重启 #2（用户已主动解绑）--")
        print(f"  设备 agent_name = {n4!r}")
        ok("★ 用户解绑后重启，未被强行绑回 preset（期望 None）",
           n4 is None, str(n4))

        # 复核：存量设备仍被正确回填（造一台「早于 preset」的设备）
        print("\n  -- 复核：存量设备仍应被回填 --")
        # 直接改库把设备时间挪到 preset 之前，模拟存量设备
        import sqlite3
        from datetime import datetime, timedelta
        conn = sqlite3.connect(DB)
        preset_ct = conn.execute(
            "SELECT created_at FROM ai_providers WHERE kind='' "
            "ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute("UPDATE devices SET created_at=? WHERE id=?",
                     (str(datetime.fromisoformat(preset_ct) - timedelta(days=1)), new_pk))
        conn.commit()
        conn.close()
        print("  已把该设备的 created_at 改到 preset 之前（模拟存量设备）")
    finally:
        stop(proc)

    proc, ready = boot()
    if not ready:
        print("[5] 重启未就绪")
        return 1
    try:
        _, t = req("POST", "/api/auth/login",
                   {"username": "devu", "password": "test123456"})
        ua5 = t.get("access_token", "")
        n5 = agent_name_of(new_pk, ua5)
        print(f"  存量设备 agent_name = {n5!r}（期望 {PRESET_ROLE_NAME!r}）")
        ok("★ 存量设备（早于 preset）仍被正确回填",
           n5 == PRESET_ROLE_NAME, str(n5))
    finally:
        stop(proc)

    print("\n" + "=" * 72)
    print(f"==== {'全部通过' if not fails else f'{len(fails)} 条失败'} ====")
    for f in fails:
        print(f"  FAILED: {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())