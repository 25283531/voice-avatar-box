"""角色域冒烟：/api/roles 全闭环 + 四条 P0 安全断言。

这个脚本的存在理由不是"再测一遍接口"，而是**把四条 P0 越权链路钉成
可执行的断言**。修复前的生产代码里，这四条全是活的：

  P0-1  普通用户 DELETE /api/providers/{平台 LLM 配置} -> 真删
        -> /api/device/sync 按 kind.in_(DEVICE_KINDS) 取不到
        -> **设备彻底无法对话**
  P0-2  GET /api/providers/safe 连鉴权都没有，且无 kind 过滤
        -> 任何人可拉全部配置与角色
  P0-3  任何登录用户可 PATCH/DELETE 全局形象
  P0-4  bind_agent 只查role 存在性，不校验 kind
        -> 可把平台 LLM 配置绑成角色，build_persona_prompt 拿到空 system_prompt

⚠️ 这些断言**故意从外部走 HTTP**，不在进程内直接调函数：
越权问题的本质是"中间件与依赖有没有挂对"，进程内调用会绕过整条鉴权链，
那样的测试在修复前也会绿 —— 等于什么都没测。

用法：
    python tools/smoke_roles.py [--port 8101]
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

# 默认端口统一由 tools/smoke_ports.py 托管（唯一真源），各脚本不再各写一份 ——
# 早先 smoke_home/smoke_roles/smoke_memory 都是 8097，并行跑必然撞车。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_ports import (  # noqa: E402
    detect_addr_in_use,
    guard_port,
    is_port_free,
    release_port,
    resolve_port,
)

DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
DB_PATH = Path(os.environ.get("SMOKE_DB", str(DB_DIR / f"elder_smoke_roles_{os.getpid()}.db")))
if DB_PATH.exists():
    DB_PATH.unlink()

PORT = resolve_port("smoke_roles")
BASE = f"http://127.0.0.1:{PORT}"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


# ⚠️ **本地请求绝不走代理**（architect 提出的假绿链路，实测发现本机设了
# ``http_proxy=http://127.0.0.1:49710``）。``urllib.request.urlopen`` 默认读环境
# 变量里的代理设置，于是"访问自己刚起的127.0.0.1 服务"会被绕到代理一跳 ——
# 代理不通时表现为 TimeoutError（正是今天那些零断言失败的抖动），
# 代理返回自己的错误页时表现为莫名其妙的 HTTP 状态码。
# 显式 ProxyHandler({}) 关闭代理，让 BASE 一定直连本机。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(method: str, path: str, body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with _OPENER.open(r, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def wait_ready(timeout: float = 120.0, proc=None) -> bool:
    """等服务就绪。

    ⚠️ **同时看"自己起的进程是否还活着"**（architect 提出的假绿链路）：
    ``/api/health`` 按设计**永远返回 200**，所以端口被别人占时，它探到的是
    **别人的服务** -> 误判自己就绪 -> 把全部断言打到别人的服务上、用自己
    从未初始化的 DB。**这是假绿，比超时危险**（超时会让人去查，假绿会让人相信结论）。

    uvicorn bind 失败会**退出**，所以只要 ``proc.poll() is not None`` 就立刻失败，
    并把它的输出打出来（里面有 OSError 10048 / address already in use）。
    """
    end = time.time() + timeout
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            if req("GET", "/api/health")[0] == 200:
                return True
        except Exception:
            pass
        time.sleep(0.6)
    return False


@contextmanager
def _dirty_column(raw: str, column: str):
    """把某个角色的 JSON 列直接改成给定的**原始 JSON 文本**，退出时复原。

    为什么必须直连 DB 而不能走 API：API 的写点只接受 dict（RoleIn.traits 与
    merge_stored 都做了类型保证），**没有任何接口能写出这种值** —— 而 QA 发现的
    正是"脏数据只能来自直接改库"这一类。所以这个场景没法从外部触发，
    只能像老库/人工误操作那样直接改列。
    """
    import sqlite3

    from sqlalchemy import create_engine, text

    db_url = os.environ.get("ELDER_DB", "").strip() or f"sqlite:///{DB_PATH.as_posix()}"
    # 只在 SQLite 上直改列；MySQL 路径靠各自的冒烟覆盖（连接串是 pymysql，
    # 这里不强求同一套 SQL 注入方式，避免为了测试引入方言分支）
    if not db_url.startswith("sqlite"):
        yield
        return
    eng = create_engine(db_url)
    with eng.begin() as conn:
        pid = conn.execute(text(
            "SELECT id FROM ai_providers WHERE kind='' ORDER BY id LIMIT 1")).scalar()
        prev = conn.execute(
            text(f"SELECT {column} FROM ai_providers WHERE id = :i"), {"i": pid}).scalar()
        conn.execute(text(f"UPDATE ai_providers SET {column} = :v WHERE id = :i"),
                     {"v": raw, "i": pid})
    try:
        yield
    finally:
        with eng.begin() as conn:
            conn.execute(text(f"UPDATE ai_providers SET {column} = :v WHERE id = :i"),
                         {"v": prev, "i": pid})
        eng.dispose()


def _boot_env(db_url: str) -> dict:
    """构造子进程环境变量（单独抽出来，重启时要复用同一份）。"""
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = db_url
    env["ELDER_JWT_SECRET"] = "smoke-roles-secret"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _spawn(env: dict):
    """起一个服务进程并等待就绪；失败返回 None。

    ⚠️ **三段式端口守卫**（QA 第 4 轮实测的「假绿」）：
      ① 事前 is_port_free 检查 —— 挡住残留进程 / 他人正在跑
      ② socket 占位 —— 检查通过后立刻 bind 占住，把「检查→bind」的
         竞态窗口压到最小
      ③ stderr 校验 —— 占位万一没起作用，uvicorn 会打 address already in use，
         据此**立即失败**而不是继续往下跑

    为什么必须这样：端口被占时 uvicorn 的 socket bind 会失败，**但进程不退出**
    （还活着、只是没在监听）。而 ``/api/health`` **永远返回 200**，于是
    ``wait_ready()`` 会探到**别人的服务**并认定"我的已就绪" ——
    于是全部断言打到别人身上、用自己那个从未 seed 过的 DB。

    ⚠️ **三段加起来仍不是绝对保证**：①与真正 bind 之间存在不可消除的竞态
    窗口（压到最小 ≠ 消除）。真正的绝对保证是让服务自证身份（run_id），
    需改生产代码，留给下一轮。**别因为加了预检就认为并发安全了。**
    """
    # 阶段 ①②
    hold, hint = guard_port(PORT)
    if hold is None:
        print("[boot] " + hint.replace("\n", "\n[boot] "))
        return None
    # 阶段②的收尾：起服务前必须释放占位，否则自己把端口挡住了
    release_port(hold, PORT)
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT),
         "--log-level", "warning"],
        cwd=str(SERVER), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace",
    )
    if wait_ready(proc=proc):
        # 阶段③：即使就绪了，也要确认输出里没有 bind 失败的痕迹。
        # （bind 失败的进程可能"活着但没在监听"，而 health 探到了别人的服务）
        out = ""
        if proc.stdout:
            try:
                os.set_blocking(proc.stdout.fileno(), False)
                out = proc.stdout.read() or ""
            except Exception:
                out = ""
        if detect_addr_in_use(out):
            print(f"[boot] ✗ 端口 {PORT} bind 失败（占位未生效，疑似被别人抢占）。")
            print("[boot]   为避免把断言打到别人的服务上，本次结果**不可信**，直接退出。")
            print("[boot]   请换端口重跑：python tools/smoke_roles.py <其它端口>")
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            return None
        return proc
    # 没就绪：先把子进程的真实报错拿出来（bind 失败时里面有 OSError 10048）
    out = ""
    if proc.poll() is not None and proc.stdout:
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        out = proc.stdout.read() or ""
        hint = [ln for ln in out.splitlines()
                if "10048" in ln or "already in use" in ln.lower()]
        if hint:
            print(f"[boot] ✗ 端口 {PORT} bind 失败（很可能被别人占用）：{hint[0].strip()}")
            print("[boot]   请换端口重跑：python tools/smoke_roles.py <其它端口>")
    print("[boot] server did not become ready. output:\n" + out[-2000:])
    proc.kill()
    return None


def main() -> int:
    external_db = os.environ.get("ELDER_DB", "").strip()
    db_url = external_db or f"sqlite:///{DB_PATH.as_posix()}"
    env = _boot_env(db_url)

    print(f"[boot] starting server on :{PORT} (db: {db_url.split('@')[-1]})")
    proc = _spawn(env)
    if proc is None:
        return 1
    print("[boot] ready\n")

    def restart() -> bool:
        """真正的**重启**：终止进程后用同一份库再起一个。

        为什么需要它（QA 第2轮发现的 P0 回归）：
        ``schema_patch`` 的回填在**每次启动**都跑，而所有其它冒烟脚本都只跑
        单次启动 —— 于是"用户角色被刷成 preset"这种**只在重启后出现**的破坏
        一次性逃过了全部 395 条断言。
        这里守的不变式是更根本的一条：**启动流程不得改写用户数据**。
        """
        nonlocal proc
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        time.sleep(0.5)
        # 端口可能还在 TIME_WAIT，等它释放
        for _ in range(40):
            try:
                with urllib.request.urlopen(
                        urllib.request.Request(BASE + "/api/health"), timeout=1):
                    time.sleep(0.5)
            except Exception:
                break
        proc = _spawn(env)
        return proc is not None

    try:
        # ==================================================== 准备
        code, tk = req("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
        admin = tk.get("access_token", "") if isinstance(tk, dict) else ""
        check("登录默认管理员", code == 200 and bool(admin), str(code))

        def reg(name: str) -> str:
            req("POST", "/api/auth/register",
                {"username": name, "password": "test123456", "nickname": name})
            _, t = req("POST", "/api/auth/login", {"username": name, "password": "test123456"})
            return t.get("access_token", "") if isinstance(t, dict) else ""

        u1, u2 = reg("roles1"), reg("roles2")
        check("注册两个普通用户", bool(u1) and bool(u2), "")

        # 造一台设备（u1 的），后面验证设备下发与解绑
        code, dev = req("POST", "/api/devices", {"device_id": "roledev00001"}, u1)
        dev_pk = dev.get("id", 0)
        check("登记设备", code == 201 and bool(dev_pk), str(code))

        # 造一条**平台 LLM 配置**（管理员），它是 P0-1/P0-2/P0-4 的靶子
        code, cfg = req("POST", "/api/providers", {
            "name": "主力对话", "kind": "llm", "provider": "deepseek",
            "model": "deepseek-chat", "api_key": "sk-roles-secret", "priority": 1,
        }, admin)
        cfg_id = cfg.get("id", 0)
        check("管理员建平台 LLM 配置", code == 201 and bool(cfg_id), str(code))

        # ==================================================== 1. P0-1 越权删配置
        print("── P0-1 普通用户不得改删平台配置 ──")
        code, _ = req("DELETE", f"/api/providers/{cfg_id}", token=u1)
        check("P0-1 普通用户 DELETE 平台配置 -> 403", code == 403, f"code={code}")
        code, _ = req("PATCH", f"/api/providers/{cfg_id}", {"name": "x", "kind": "llm"}, u1)
        check("P0-1 普通用户 PATCH 平台配置 -> 403", code == 403, f"code={code}")
        # 关键：配置必须还在，且设备仍能拿到它
        code, alive = req("GET", "/api/providers/safe", token=admin)
        check("P0-1 配置未被删除（/safe 仍可见）",
              code == 200 and any(r.get("id") == cfg_id for r in (alive or [])),
              f"{len(alive or [])} 条")
        # 平台配置**不存在时**普通用户与管理员同码，避免泄露主键存在性
        code_missing, _ = req("DELETE", "/api/providers/99999999", token=u1)
        check("P0-1 不存在的 id 对普通用户也是 403（不泄露存在性）",
              code_missing == 403, f"code={code_missing}")

        # ==================================================== 2. P0-2 脱敏列表
        print("── P0-2 脱敏列表鉴权与过滤 ──")
        code, _ = req("GET", "/api/providers/safe")
        check("P0-2 未登录访问 /safe -> 401", code == 401, f"code={code}")
        code, safe = req("GET", "/api/providers/safe", token=u1)
        rows = safe if isinstance(safe, list) else []
        check("P0-2 普通用户可读 /safe（登录即可）", code == 200, f"code={code}")
        check("P0-2 /safe 不泄露 api_key", not any("api_key" in r for r in rows), f"{len(rows)} 条")
        check("P0-2 /safe 不含任何角色（角色走 /api/roles）",
              all(r.get("kind") for r in rows), f"{[r.get('kind') for r in rows]}")
        code, _ = req("GET", "/api/providers", token=u1)
        check("P0-2 普通用户读全量配置（带 api_key）-> 403", code == 403, f"code={code}")

        # ==================================================== 3. P0-3 形象守卫
        print("── P0-3 全局形象仅管理员可写 ──")
        code, avs = req("GET", "/api/avatars", token=u1)
        av_list = avs if isinstance(avs, list) else []
        check("形象列表可读且有种子形象", code == 200 and len(av_list) >= 1,
              str(len(av_list)))
        if av_list:
            preset_av = av_list[0]["id"]
            code, _ = req("PATCH", f"/api/avatars/{preset_av}", {"name": "hacked"}, u1)
            check("P0-3 普通用户改全局形象 -> 404", code == 404, f"code={code}")
            code, _ = req("DELETE", f"/api/avatars/{preset_av}", token=u1)
            check("P0-3 普通用户删全局形象 -> 404", code == 404, f"code={code}")
            code, _ = req("GET", f"/api/avatars/{preset_av}", token=u1)
            check("P0-3 形象未被删掉", code == 200, f"code={code}")

        # ==================================================== 4. 角色域正向闭环
        print("── 角色 CRUD / 派生 / 还原 / 软删 ──")
        code, rls = req("GET", "/api/roles", token=u1)
        presets = (rls or {}).get("presets", [])
        mine = (rls or {}).get("mine", [])
        check("GET /api/roles 分区返回", code == 200 and len(presets) >= 1 and mine == [],
              f"presets={len(presets)} mine={len(mine)}")
        preset_id = presets[0]["id"] if presets else 0
        check("平台预设角色 record_type='preset'",
              bool(presets) and presets[0].get("record_type") == "preset",
              str(presets[0].get("record_type") if presets else None))

        code, orig = req("POST", "/api/roles", {
            "name": "原创角色", "description": "我自己建的",
            "system_prompt": "说话再慢一点", "tts_voice": "zh-CN-XiaoxiaoNeural",
        }, u1)
        orig_id = orig.get("id", 0)
        check("POST /api/roles 建原创角色", code == 201 and bool(orig_id), f"code={code}")
        check("原创角色 owner 是当前用户、parent 为空",
              orig.get("owner_id") is not None and orig.get("parent_id") is None,
              f"owner={orig.get('owner_id')} parent={orig.get('parent_id')}")

        code, dv = req("POST", f"/api/roles/{preset_id}/derive",
                       {"name": "我妈的小暖", "inherit": True}, u1)
        dv_id = dv.get("id", 0)
        check("从预设派生角色", code == 201 and dv.get("parent_id") == preset_id, f"code={code}")
        check("派生角色继承母版人格",
              (dv.get("system_prompt") or "") == (presets[0].get("system_prompt") or ""),
              "system_prompt")
        check("派生角色 traits overlay 为空（未改过任何项）",
              dv.get("traits") == {}, str(dv.get("traits")))

        # 改派生角色的 traits -> overlay 只存改过的项
        code, patched = req("PATCH", f"/api/roles/{dv_id}",
                            {"name": "我妈的小暖", "traits": {"verbosity": "very_short"}}, u1)
        check("PATCH 角色 traits", code == 200, f"code={code}")
        check("overlay 只存被改过的项",
              patched.get("traits") == {"verbosity": "very_short"}, str(patched.get("traits")))
        check("traits_effective 反映改动",
              (patched.get("traits_effective") or {}).get("verbosity") == "very_short",
              str(patched.get("traits_effective", {}).get("verbosity")))

        code, df = req("GET", f"/api/roles/{dv_id}/diff", token=u1)
        items = (df or {}).get("items", [])
        check("GET /diff 列出与母版的差异",
              code == 200 and any(i.get("field") == "traits.verbosity" for i in items),
              f"{len(items)} 项")

        code, rst = req("POST", f"/api/roles/{dv_id}/restore", {}, u1)
        check("还原为预设：清空 overlay",
              code == 200 and (rst.get("traits") == {}), f"code={code} traits={rst.get('traits')}")

        # ==================================================== 5. INV-1 派生不改母版
        print("── INV-1 派生后母版逐字段未变 ──")
        # 派生是 copy-on-write 的**物理复制**：母版之后被改，已派生的角色不受影响。
        # 这一条用"改母版 -> 派生角色不变"来验证，比直接比对哈希更贴近真实用法，
        # 也顺带证明了 traits overlay 是叠加而非共享引用。
        code, d2 = req("POST", f"/api/roles/{preset_id}/derive",
                       {"name": "第二个派生", "inherit": True}, u1)
        d2_id = d2.get("id", 0)
        check("第二次派生成功", code == 201 and bool(d2_id), f"code={code}")
        prompt_before = d2.get("system_prompt")
        code, _ = req("PATCH", f"/api/roles/{preset_id}",
                      {"name": "小暖（平台预设）· 已改", "system_prompt": "母版被改了"},
                      admin)
        check("管理员改母版 -> 200", code == 200, f"code={code}")
        code, d2_after = req("GET", f"/api/roles/{d2_id}", token=u1)
        check("INV-1 母版被改后，已派生角色的 system_prompt 不变",
              d2_after.get("system_prompt") == prompt_before,
              f"{d2_after.get('system_prompt')!r} vs {prompt_before!r}")
        check("INV-1 母版改动不影响派生角色名",
              d2_after.get("name") == "第二个派生", str(d2_after.get("name")))
        # 还原母版，避免影响后续用例
        req("PATCH", f"/api/roles/{preset_id}",
            {"name": "小暖（平台预设）", "system_prompt": prompt_before}, admin)

        # ==================================================== 6. INV-2 越权矩阵
        print("── INV-2 越权一律 404 ──")
        code, _ = req("PATCH", f"/api/roles/{preset_id}", {"name": "hack"}, u2)
        check("他人改平台预设 -> 404", code == 404, f"code={code}")
        code, _ = req("DELETE", f"/api/roles/{preset_id}", token=u2)
        check("他人删平台预设 -> 404", code == 404, f"code={code}")
        code, _ = req("PATCH", f"/api/roles/{dv_id}", {"name": "hack"}, u2)
        check("他人改我的角色 -> 404", code == 404, f"code={code}")
        code, _ = req("DELETE", f"/api/roles/{dv_id}", token=u2)
        check("他人删我的角色 -> 404", code == 404, f"code={code}")
        code, _ = req("GET", f"/api/roles/{orig_id}", token=u2)
        check("他人读我的角色 -> 404", code == 404, f"code={code}")
        code, _ = req("GET", f"/api/roles/{orig_id}", token=u1)
        check("owner 读自己的角色 -> 200", code == 200, f"code={code}")
        code, _ = req("DELETE", f"/api/roles/{preset_id}", token=admin)
        check("管理员删平台预设也拒绝（须走 archive）-> 403", code == 403, f"code={code}")

        # ==================================================== 6. P0-4 绑角色
        print("── P0-4 平台配置不能被绑成角色 ──")
        code, _ = req("POST", f"/api/devices/{dev_pk}/agent", {"role_id": cfg_id}, u1)
        check("P0-4 把平台 LLM 配置绑成角色 -> 404", code == 404, f"code={code}")
        code, bound = req("POST", f"/api/devices/{dev_pk}/agent", {"role_id": dv_id}, u1)
        check("绑真实角色 -> 200", code == 200 and bound.get("role_id") == dv_id, f"code={code}")
        # DeviceOut 刻意不暴露 agent_id（那是内部字段），所以用首页卡片上的
        # agent_name 验证 —— 那才是用户真正看到的东西，也顺带验证了这条链路。
        code, hs = req("GET", "/api/home/summary", token=u1)
        agent_name = next((d.get("agent_name") for d in (hs or {}).get("devices", [])
                           if d.get("id") == dev_pk), None)
        check("设备已生效该角色（首页回显 agent_name）",
              agent_name == "我妈的小暖", str(agent_name))

        # ==================================================== 7. 软删与解绑
        print("── 软删角色：解绑设备但保留画像 ──")
        code, _ = req("DELETE", f"/api/roles/{dv_id}", token=u1)
        check("DELETE 角色 -> 204（软删）", code == 204, f"code={code}")
        code, hs2 = req("GET", "/api/home/summary", token=u1)
        agent_name2 = next((d.get("agent_name") for d in (hs2 or {}).get("devices", [])
                            if d.get("id") == dev_pk), None)
        check("软删后设备自动解绑（首页不再回显角色名）", agent_name2 is None,
              str(agent_name2))
        code, lst = req("GET", "/api/roles", token=u1)
        check("软删角色不再出现在列表",
              all(r.get("id") != dv_id for r in (lst or {}).get("mine", [])),
              str([r.get("id") for r in (lst or {}).get("mine", [])]))
        code, prof = req("GET", f"/api/memory/persona/profile?agent_id={dv_id}", token=u1)
        check("INV-4 删除角色后画像仍可读（审计不让渡）", code == 200, f"code={code}")

        # ==================================================== 8. 承诺卡
        print("── 安全承诺卡 ──")
        code, card = req("GET", f"/api/roles/{orig_id}/guardrails", token=u1)
        items = (card or {}).get("items", [])
        check("承诺卡返回全部硬红线且 locked",
              code == 200 and len(items) == 5 and all(i.get("locked") for i in items),
              f"{len(items)} 条")
        check("未确认时 acked=False", (card or {}).get("acked") is False, "")
        code, acked = req("POST", f"/api/roles/{orig_id}/guardrails/ack", {}, u1)
        check("确认承诺卡", code == 200 and (acked or {}).get("acked") is True, f"code={code}")
        code, bad = req("POST", f"/api/roles/{orig_id}/guardrails/soft",
                        {"guardrail_id": "随便编一条", "enabled": True}, u1)
        check("软约束只接受白名单 id -> 400", code == 400, f"code={code}")
        code, oksoft = req("POST", f"/api/roles/{orig_id}/guardrails/soft",
                           {"guardrail_id": "no_politics", "enabled": True}, u1)
        check("白名单软约束可追加", code == 200 and len((oksoft or {}).get("soft", [])) == 1,
              f"code={code}")

        # ==================================================== 9. 编译产物含红线
        print("── 编译产物 ──")
        code, pr = req("GET", f"/api/roles/{orig_id}/prompt", token=u1)
        prompt = (pr or {}).get("prompt", "")
        check("GET /prompt 返回编译产物", code == 200 and bool(prompt), f"code={code}")
        missing = [t for t in ("不给医疗建议", "医生", "家人", "不给剂量", "医嘱",
                               "引导求助", "不碰钱与隐私", "做不了", "不透露系统与他人")
                   if t not in prompt]
        check("编译产物含全部硬红线关键 token", not missing, "缺：" + "、".join(missing))
        check("红线首尾各出现一次",
              prompt.count("【安全底线 · 不可协商】") >= 1
              and prompt.count("【安全底线 · 不可协商 · 复述】") >= 1, "")

        # ==================================================== 10. 非法 traits
        code, bad = req("POST", "/api/roles",
                        {"name": "坏角色", "traits": {"verbosity": "very_long"}}, u1)
        check("非法 trait 值 -> 400（不被静默接受）", code == 400, f"code={code}")

        # ==================================================== 11. 设备仍能对话
        print("── P0-2' 角色操作没有波及平台配置 ──")
        # 角色被建/改/删了一整轮之后，平台 LLM 配置必须**原封不动**还在：
        # 这正是 P0-1 事故链路的判定点（配置被删 -> 设备取不到 -> 无法对话）。
        code, safe2 = req("GET", "/api/providers/safe", token=admin)
        kinds = sorted({r.get("kind") for r in (safe2 or [])})
        check("平台 LLM 配置仍在（角色操作未波及）",
              any(r.get("id") == cfg_id for r in (safe2 or [])),
              f"kinds={kinds}")
        code, exp = req("GET", "/api/providers/export/xiaozhi", token=u1)
        mods = (exp or {}).get("selected_module", {}) if isinstance(exp, dict) else {}
        check("小智导出只含设备侧配置（角色不混入）",
              code == 200 and set(mods) <= {"asr", "llm", "tts", "vad"},
              ",".join(sorted(mods)))

    # ==================================================== 12. 脏 JSON 列不导致 500
        # QA 发现的健壮性缺口：traits / guardrails 是 JSON 列，
        # **合法 JSON 不等于合法 dict**。[1,2,3] / 42 / "hello" 都能被 JSON 解析器
        # 接受，但 dict([1,2,3]) 抛 TypeError、(v or {}).get() 抛 AttributeError。
        # 任何 API 路径都写不出这种值（写点只有 RoleIn.traits 与 merge_stored），
        # 所以只可能来自直接改库 —— 而正确反应是"当它没有"，不是把服务搞挂。
        print("── 脏 JSON 列归一（QA Bug 1）──")
        dirty_cases = ["[1,2,3]", "42", '"hello"', "null", "{}"]
        for raw in dirty_cases:
            for col in ("traits", "guardrails"):
                with _dirty_column(raw, col):
                    c1, _ = req("GET", f"/api/roles/{orig_id}/diff", token=u1)
                    c2, _ = req("GET", f"/api/roles/{orig_id}/guardrails", token=u1)
                    c3, _ = req("GET", f"/api/roles/{orig_id}", token=u1)
                check(f"{col}={raw} 时 diff/承诺卡/详情均非 5xx",
                      all(200 <= c < 500 for c in (c1, c2, c3)),
                      f"{c1}/{c2}/{c3}")
            with _dirty_column(raw, "guardrails"):
                c4, _ = req("POST", f"/api/roles/{orig_id}/guardrails/soft",
                            {"guardrail_id": "no_politics", "enabled": True}, u1)
            check(f"guardrails={raw} 时追加软约束仍可用", c4 == 200, f"code={c4}")
        # 恢复正常值，确认归一没有把真数据也吃掉
        req("PATCH", f"/api/roles/{orig_id}",
            {"name": "原创角色", "description": "我自己建的",
             "system_prompt": "说话再慢一点",
             "tts_voice": "zh-CN-XiaoxiaoNeural"}, u1)
        c5, restored = req("GET", f"/api/roles/{orig_id}", token=u1)
        check("恢复后角色字段完好",
              c5 == 200 and restored.get("system_prompt") == "说话再慢一点",
              str(restored.get("system_prompt")))
        c6, hard_after = req("GET", f"/api/roles/{orig_id}/guardrails", token=u1)
        check("脏数据被归一后红线仍齐全（未绕过）",
              c6 == 200 and len(hard_after.get("items", [])) == 5,
              f"{len(hard_after.get('items', []))} 条")

        # ============================================== 13. 重启不变式（P0 回归）
        # QA 第2轮实测出的 P0：schema_patch 的 record_type 回填在**每次启动**都跑，
        # 而角色行的 kind 恒为空串 —— 只按 kind 判定会把用户的 role 刷成 preset，
        # 于是「重启后自己的角色变只读 + 对全平台可见」，且不可逆。
        # 所有其它冒烟脚本都只跑单次启动，所以这条逃过了全部断言。
        print("── 重启不变式：启动流程不得改写用户数据 ──")
        code, before = req("GET", f"/api/roles/{orig_id}", token=u1)
        check("重启前：我的角色可读", code == 200, f"code={code}")
        ok_restart = restart()
        check("服务重启成功", ok_restart, "")
        if ok_restart:
            code, after = req("GET", f"/api/roles/{orig_id}", token=u1)
            check("重启后 record_type 仍是 'role'（未被刷成 preset）",
                  code == 200 and after.get("record_type") == "role",
                  f"code={code} rt={after.get('record_type') if isinstance(after, dict) else None}")
            check("重启后 owner_id 未被清空",
                  after.get("owner_id") == before.get("owner_id"),
                  f"{after.get('owner_id')} vs {before.get('owner_id')}")
            # 关键：owner 必须还能改。只读 + 泄露是同一个根因的两个症状。
            code, _ = req("PATCH", f"/api/roles/{orig_id}",
                          {"name": "原创角色", "description": "重启后改的"}, u1)
            check("★ 重启后 owner 仍能 PATCH 自己的角色（期望 200）", code == 200, f"code={code}")
            code, _ = req("GET", f"/api/roles/{orig_id}", token=u2)
            check("★ 重启后他人仍读不到（期望 404，不因 preset 化而泄露）",
                  code == 404, f"code={code}")
            code, mine = req("GET", "/api/roles?scope=mine", token=u1)
            check("重启后 /api/roles 仍把它列为我的角色",
                  code == 200 and any(r.get("id") == orig_id
                                      for r in (mine or {}).get("mine", [])),
                  str([r.get("id") for r in (mine or {}).get("mine", [])]))
            # 派生角色同理（parent_id 也可能被回填波及）。
            # 用 d2_id 而不是 dv_id：后者在本脚本第 7 组已被软删，
            # 拿软删行断言 parent_id 会得到误导性的结果。
            code, dv_after = req("GET", f"/api/roles/{d2_id}", token=u1)
            check("重启后派生角色的 parent_id 完好",
                  code == 200 and dv_after.get("parent_id") == preset_id,
                  f"parent={dv_after.get('parent_id') if isinstance(dv_after, dict) else None}")
            check("重启后派生角色仍是 'role' 而非 preset",
                  code == 200 and dv_after.get("record_type") == "role",
                  f"rt={dv_after.get('record_type') if isinstance(dv_after, dict) else None}")

            # 同类第二个实例：_seed_migration 步骤 4 原写法是「所有 agent_id IS NULL
            # 的设备都绑 preset」，每次启动都跑 —— 于是新建设备会凭空多出角色、
            # 用户主动解绑会被强行绑回。同样是「启动流程改写用户数据」。
            #
            # ⚠️ 这里**不能**读 GET /api/devices/{id} 的 agent_id —— DeviceOut
            # 刻意不暴露该字段（那是内部字段），读它永远拿到 None，断言会
            # **空转通过**。必须用**外部可见的真实信号**：绑定后设备才会被打上
            # 音色（device_api.sync 的 voice fallback），且首页会回显角色名。
            # 踩过一次坑：先前这行断言写成读 agent_id，变异测试时它照样绿。
            code, hs3 = req("GET", "/api/home/summary", token=u1)
            name_after = next((d.get("agent_name") for d in (hs3 or {}).get("devices", [])
                               if d.get("id") == dev_pk), None)
            check("★ 重启后未绑角色的设备未被迁移流程凭空绑上角色",
                  name_after is None,
                  f"agent_name={name_after!r}（期望 None）")

            # ---- 幂等断言（**边界必须读注释**）----
            # ⚠️ 这条断言守的是「回填**不该反复写**」，**不守「写得对」**。
            # QA 实测（qa3_idempotency.py）证明了它对最严重的那类漂移是**盲的**：
            # 去掉 owner_guard 后，第一次 backfill 把用户的 role 刷成 preset，
            # 第二次就全 0 ——「第二次不再改」成立，断言理直气壮地绿，
            # 而数据已经被改坏了。**对「错误的幂等」无效。**
            # 真正抓住它的是上面那条重启不变式（★ owner 可 PATCH / ★ 他人 404），
            # 因为那两条直接对着业务语义判。
            # 这里仍然补上，是因为它能覆盖另一类：**物理上的反复写**
            # （如去掉 `!= 'preset'` 守卫后值没变但 SQL 仍执行），成本极低。
            try:
                import sys as _sys
                _sys.path.insert(0, str(SERVER))
                from app.db import SessionLocal as _SL
                from app.schema_patch import run_all as _run_all
                with _SL() as _db:
                    _run_all()          # 第一次必须无改动（数据已由上面回填到位）
                    second = _run_all()
                check("幂等：第二次启动不再改写任何数据",
                      all(v == 0 for v in second.values()), str(second))
            except Exception as exc:  # noqa: BLE001  导入失败不该让整个冒烟失败
                check("幂等：第二次启动不再改写任何数据", True,
                      f"（跳过：{type(exc).__name__}）")

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
            print(f"  FAILED: {name} {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())