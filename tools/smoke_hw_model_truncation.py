"""``hw_model`` 截断护栏冒烟 —— 守 ``device_api.sync`` 里那行 ``hw[:64]``。

## 为什么需要这个脚本

``server/app/routers/device_api.py:115``::

    dev.hardware_model = hw[:64]     # 与 Device.hardware_model 列宽对齐

这一行是 **SQLite → MySQL 一致性的唯一护栏**，但没有任何测试守它。原因是
SQLite **不强制列宽**：``VARCHAR(64)`` 只是类型亲和性，200 字符照样写进去
（实测：写入 200 字符后 ``length(hardware_model)`` 返回 200）。也就是说：

* 删掉 ``[:64]`` → 本地 SQLite 一切正常，**没有任何测试会红**；
* 同一个库迁到 MySQL（``VARCHAR(64)`` 会截断或报 1406）→ 行为分叉；
* 设备表的「硬件型号」列变成 200 字符的乱码，UI 直接错位。

「删掉护栏也不会红」正是本项目反复吃亏的**假绿**形态。本脚本把它变成红。

## 两条判据，一条行为一条静态（都**有鉴别力**）

1. **行为判据（强）**：真起服务，用设备 token 上报 200 字符 ``hw_model``，
   然后**直连 sqlite3 读原始列值**断言 ``length == 64`` 且等于前 64 字符。
   为什么必须读库、不能只读接口：接口层（Pydantic / 响应模型）以后完全可能
   再加一道截断，那样「接口回读 ≤ 64」就**恒真**了 —— 库里的原始值才是真凭据。
2. **静态判据（补充）**：用 ``ast`` 解析 ``device_api.py``，断言
   ``dev.hardware_model = hw[:64]`` 这个切片的上界**就是 64**。
   用 AST 而不是 grep 正则：注释、换行、空格改动不会误报，但把 64 改成别的
   数字 / 删掉切片一定红。

## 断言本身的鉴别力怎么证明（``--mutation-selftest``）

光「跑绿了」不能证明断言有效 —— 恒真断言也会绿。所以本脚本默认在**同一份
代码上**再跑一遍**变异版**：用 import hook 把 ``hw[:64]`` 就地改写成
``hw[:200]``（**不修改任何源文件**），然后重跑同一套断言，要求它**必须变红**。

* 变异跑绿 → 说明断言没有鉴别力（改坏了也不红）→ 本脚本 exit 1；
* 变异跑红 → 证明「删掉/放宽截断，这套断言会红」→ 这才是有效护栏。

不想跑变异（例如只想快速自检）用 ``--no-mutation``。

## 用法

::

    python tools/smoke_hw_model_truncation.py            # 默认端口（smoke_ports 登记）
    python tools/smoke_hw_model_truncation.py 8126       # 指定端口
    python tools/smoke_hw_model_truncation.py --no-mutation

退出码：0 = 行为/静态判据全过 **且** 变异验证证明断言有鉴别力；1 = 其余情况。
"""
from __future__ import annotations

import ast
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

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable
DEVICE_API = SERVER / "app" / "routers" / "device_api.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_common import cleanup_db, seed_admin_env  # noqa: E402
from smoke_ports import (  # noqa: E402
    DEFAULT_PORTS,
    guard_or_die,
    guard_port,
    is_port_free,
    release_port,
    resolve_port,
)

# 本机设了 http_proxy，urllib 默认会读它，于是「访问自己刚起的 127.0.0.1 服务」
# 被绕到代理一跳 —— 代理不通就 TimeoutError 且零断言失败，极易被误判成代码回归。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: 列宽。与 ``models.Device.hardware_model = String(64)`` 和
#: ``schema_patch.NEW_COLUMNS`` 的 ``VARCHAR(64)`` 三处必须一致。
COLUMN_WIDTH = 64

DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
DB_PATH = DB_DIR / f"elder_smoke_hwmodel_{os.getpid()}.db"
SERVER_LOG = DB_PATH.with_name(DB_PATH.stem + "_server.log")
LAUNCHER = DB_DIR / f"elder_hwmodel_mutate_{os.getpid()}.py"

#: 变异跑的端口 = 正常跑 + 1（同一脚本内两段串行，错开端口避免 TIME_WAIT 干扰）
PORT = resolve_port("smoke_hw_model_truncation")
BASE = f"http://127.0.0.1:{PORT}"

results: list[tuple[str, bool, str]] = []

#: 变异跑里**必须**变红的那几条（都是「截断是否生效」的直接判据）。
TRUNCATION_CHECKS = {
    "接口回读长度 ≤ 64",
    "接口回读 == 前 64 字符",
    "库里原始值长度 == 64（SQLite 不强制列宽，护栏只在代码里）",
    "库里原始值 == 前 64 字符",
    "恰好 64 字符原样保存（无 off-by-one）",
    "65 字符截断为 64",
}

#: 变异注入器：用 import hook 在**内存里**把 ``hw[:64]`` 改写成 ``hw[:LIMIT]``。
#: 不落盘、不改源文件 —— 所以跑完 ``git status`` 必然是干净的（本项目踩过
#: 「改回后忘记改回」的坑）。
_MUTATION_LAUNCHER = r'''"""自动生成的变异启动器（跑完即删，不修改任何源文件）。"""
import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys

sys.path.insert(0, os.getcwd())          # cwd = server/
LIMIT = os.environ["HW_MUTATE_LIMIT"]
TARGET = "app.routers.device_api"


class _Loader(importlib.abc.Loader):
    def __init__(self, origin):
        self.origin = origin

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        with open(self.origin, encoding="utf-8") as fp:
            src = fp.read()
        mutated = src.replace("hw[:64]", "hw[:%s]" % LIMIT)
        if mutated == src:
            raise SystemExit(
                "变异注入失败：device_api.py 里找不到 'hw[:64]' —— "
                "护栏可能已被删除/改名，本脚本的变异自检无从谈起")
        module.__file__ = self.origin
        exec(compile(mutated, self.origin, "exec"), module.__dict__)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin:
            return None
        return importlib.util.spec_from_file_location(
            fullname, spec.origin, loader=_Loader(spec.origin))


sys.meta_path.insert(0, _Finder())

import uvicorn                                                       # noqa: E402

uvicorn.run("app.main:app", host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning")
'''


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def req(method: str, path: str, body=None, token: str | None = None,
        headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with _LOCAL_OPENER.open(r, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:                                    # noqa: BLE001
            return e.code, raw


def wait_ready(proc, timeout: float = 120.0) -> bool:
    """等就绪，**同时查自己的进程是否还活着**。

    只看 ``/api/health`` 会连到别人的服务（端口被占时 uvicorn bind 失败但
    ``/api/health`` 按设计永远 200）—— 那是假绿。判据见 smoke_ports.py 的
    「防线 #2」。
    """
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            if req("GET", "/api/health")[0] == 200:
                return True
        except Exception:                                    # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def db_value(db_path: Path) -> str | None:
    """直连 sqlite3 读**原始列值**（绕过任何接口层截断，这是强判据的来源）。

    ⚠️ 必须显式 ``close()``：``with sqlite3.connect(...) as conn`` 只管事务
    （提交/回滚），**不关连接**。连接不关，本进程就攥着 ``.db`` 的文件句柄，
    收尾时 ``unlink`` 必然 ``WinError 32`` —— 本脚本第一版正是这么踩的，
    和 smoke_home 的历史症状同一个根因。
    """
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True,
                               timeout=5)
        row = conn.execute(
            "SELECT hardware_model FROM devices ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else row[0]
    except sqlite3.Error as exc:
        return f"<sqlite error: {exc}>"
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def spawn_server(db_path: Path, port: int, mutate_limit: int | None):
    """起服务。``mutate_limit`` 非空时用变异启动器（内存改写 ``hw[:64]``）。"""
    env = seed_admin_env(dict(os.environ))
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{db_path.as_posix()}"
    env["ELDER_JWT_SECRET"] = "smoke-hwmodel-secret"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"
    env["PYTHONIOENCODING"] = "utf-8"

    log_fp = SERVER_LOG.open("w", encoding="utf-8", errors="replace")
    if mutate_limit is None:
        cmd = [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
               "--port", str(port), "--log-level", "warning"]
    else:
        LAUNCHER.write_text(_MUTATION_LAUNCHER, encoding="utf-8")
        env["HW_MUTATE_LIMIT"] = str(mutate_limit)
        cmd = [PY, str(LAUNCHER), str(port)]
    proc = subprocess.Popen(
        cmd, cwd=str(SERVER), env=env,
        stdout=log_fp, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace",
    )
    return proc, log_fp


def run_flow(mutate_limit: int | None) -> list[tuple[str, bool, str]]:
    """跑一遍完整流程，返回断言结果（清空全局 ``results`` 后重新收集）。"""
    results.clear()
    db_path = DB_PATH
    if mutate_limit is not None:
        db_path = DB_PATH.with_name(DB_PATH.stem + "_mut.db")
    for suffix in ("", "-journal", "-wal", "-shm"):
        p = Path(f"{db_path}{suffix}")
        if p.exists():
            p.unlink()

    guard_or_die(PORT, "smoke_hw_model_truncation.py")
    hold, hint = guard_port(PORT)
    if hold is None:
        print("[boot] " + hint)
        results.append(("端口可用", False, hint))
        return list(results)
    release_port(hold, PORT)

    label = "变异版(hw[:%d])" % mutate_limit if mutate_limit else "原始版"
    print(f"[boot] {label} 起服务 :{PORT}  db={db_path}")
    proc, log_fp = spawn_server(db_path, PORT, mutate_limit)
    try:
        if not wait_ready(proc):
            tail = SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-3000:]
            print(f"[boot] 服务未就绪，日志尾部：\n{tail}")
            results.append(("服务就绪", False, "见日志尾部"))
            return list(results)
        print("[boot] ready\n")

        # ------------------------------------------------ 账号 / 设备
        code, tk = req("POST", "/api/auth/login",
                       {"username": "admin", "password": "admin123"})
        admin = (tk or {}).get("access_token", "")
        check("管理员登录", code == 200 and bool(admin), f"HTTP {code}")
        if not admin:
            return list(results)

        dev_id = "aa:bb:cc:dd:ee:0a" if mutate_limit is None else "aa:bb:cc:dd:ee:0b"

        # ⚠️ 取码必须走**设备自己那条路**（POST /api/voice/ota + Device-Id 头），
        # 不能用管理端的 `POST /api/devices/{pk}/code`：
        #   * 2026-10-20 起该端点对「已绑定设备」返回 **409**（语义：已绑定不发码），
        #     而 `POST /api/devices` 是「建档即归属」，于是「建 → 取码 → 绑」
        #     这条老流程在**同一份代码上必然自相矛盾**（smoke_local_reminders
        #     就是这么红的：申请激活码 -> HTTP 409）；
        #   * 真实家庭场景也从不走那个端点 —— 码是设备开机时向 OTA 申领的。
        # 走 OTA 还顺带让本脚本覆盖「固件取码 → APP 输码绑定」这条真链路。
        code, ota = req("POST", "/api/voice/ota", {}, headers={"Device-Id": dev_id})
        act = (ota or {}).get("activation") or {}
        full = act.get("code", "")
        check("设备经 OTA 领到激活码（固件真链路）", code == 200 and bool(full),
              f"HTTP {code} code={'有' if full else '无'}")

        code, bind = req("POST", "/api/devices/bind",
                         {"code": full, "device_id": dev_id}, admin)
        dev_token = (bind or {}).get("device_token", "")
        dev_pk = (bind or {}).get("device_pk")
        check("绑定拿到 device_token（显式带 device_id）",
              code == 200 and bool(dev_token) and bool(dev_pk), f"HTTP {code}")
        if not dev_token:
            return list(results)

        # ------------------------------------------------ ① 200 字符
        long_model = "ESP32-S3-" + "X" * 191          # 正好 200 字符
        code, _ = req("POST", f"/api/device/sync?fw_version=0.1.0&hw_model={long_model}",
                      {}, dev_token)
        check("上报 200 字符 hw_model 返回 200", code == 200, f"HTTP {code}")

        code, dev = req("GET", f"/api/devices/{dev_pk}", token=admin)
        api_val = (dev or {}).get("hardware_model", "")
        check("接口回读长度 ≤ 64", isinstance(api_val, str) and len(api_val) <= COLUMN_WIDTH,
              f"len={len(api_val) if isinstance(api_val, str) else '?'}")
        check("接口回读 == 前 64 字符", api_val == long_model[:COLUMN_WIDTH],
              f"{api_val[:20]}…")

        raw = db_value(db_path)
        check("库里原始值长度 == 64（SQLite 不强制列宽，护栏只在代码里）",
              isinstance(raw, str) and len(raw) == COLUMN_WIDTH,
              f"len={len(raw) if isinstance(raw, str) else raw}")
        check("库里原始值 == 前 64 字符", raw == long_model[:COLUMN_WIDTH],
              f"{str(raw)[:20]}…")

        # ------------------------------------------------ ② 边界：恰好 64 / 65
        exact = "M" * COLUMN_WIDTH
        req("POST", f"/api/device/sync?hw_model={exact}", {}, dev_token)
        check("恰好 64 字符原样保存（无 off-by-one）", db_value(db_path) == exact,
              f"len={len(str(db_value(db_path)))}")

        over = "N" * (COLUMN_WIDTH + 1)
        req("POST", f"/api/device/sync?hw_model={over}", {}, dev_token)
        check("65 字符截断为 64", db_value(db_path) == over[:COLUMN_WIDTH],
              f"len={len(str(db_value(db_path)))}")

        # ------------------------------------------------ ③ 空值不清空（契约）
        req("POST", "/api/device/sync?fw_version=0.1.1", {}, dev_token)
        check("空 hw_model 不清空已有值（老固件心跳不抹型号）",
              db_value(db_path) == over[:COLUMN_WIDTH],
              f"{str(db_value(db_path))[:20]}…")
    finally:
        # 收尾：先停进程再删库（顺序反了就是 WinError 32，见 smoke_common）
        cleanup_db(db_path, procs=(proc,), handles=(log_fp,))
        if LAUNCHER.exists():
            cleanup_db(LAUNCHER, attempts=4)
    return list(results)


def source_guard() -> tuple[bool, str]:
    """静态判据：``dev.hardware_model = hw[<上界>]`` 的上界必须等于 64。

    用 AST 而不是 grep：注释/换行/空格改动不会误报；删掉切片、把 64 改成
    别的数字一定红。
    """
    try:
        tree = ast.parse(DEVICE_API.read_text(encoding="utf-8"))
    except OSError as exc:
        return False, f"读不到 {DEVICE_API}: {exc}"

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            is_col = (isinstance(tgt, ast.Attribute) and tgt.attr == "hardware_model")
            if not is_col:
                continue
            val = node.value
            if not isinstance(val, ast.Subscript):
                return False, f"第 {node.lineno} 行：hardware_model 赋值不再是切片（截断护栏被删？）"
            sl = val.slice
            upper = sl.upper if isinstance(sl, ast.Slice) else None
            if not (isinstance(upper, ast.Constant) and upper.value == COLUMN_WIDTH):
                got = ast.unparse(sl) if hasattr(ast, "unparse") else "?"
                return False, f"第 {node.lineno} 行：切片上界不是 {COLUMN_WIDTH}，实际 [{got}]"
            return True, f"第 {node.lineno} 行 hardware_model = ...[:{COLUMN_WIDTH}]"
    return False, "device_api.py 里找不到对 hardware_model 的赋值（护栏可能被整体删除）"


def _pick_free_port(base_port: int) -> int:
    """给变异跑挑一个空闲且未登记的端口（跳过 DEFAULT_PORTS 里的值）。"""
    taken = set(DEFAULT_PORTS.values())
    for candidate in range(base_port + 1, base_port + 60):
        if candidate in taken:
            continue
        if is_port_free(candidate):
            return candidate
    return base_port + 1          # 兜底：交给 guard_or_die 报错


def main() -> int:
    global PORT, BASE
    argv = [a for a in sys.argv[1:]]
    mutation = "--no-mutation" not in argv
    argv = [a for a in argv if not a.startswith("--")]
    if argv:
        PORT = int(argv[0])
        BASE = f"http://127.0.0.1:{PORT}"

    print("=" * 70)
    print("第一段：原始代码 —— 断言必须全绿")
    print("=" * 70)
    normal = run_flow(None)
    normal_fail = [n for n, ok, _ in normal if not ok]

    print()
    print("=" * 70)
    print("第二段：静态判据（AST 读源码，不改源码）")
    print("=" * 70)
    src_ok, src_detail = source_guard()
    print(f"  [{'PASS' if src_ok else 'FAIL'}] device_api.py 截断上界 == {COLUMN_WIDTH}  -> {src_detail}")

    mutation_verdict = True
    mutation_detail = "跳过（--no-mutation）"
    if mutation:
        print()
        print("=" * 70)
        print("第三段：变异自检 —— 把 hw[:64] 在内存里改成 hw[:200]，断言**必须变红**")
        print("=" * 70)
        # 变异版用另一个端口，避免上一段的 TIME_WAIT 干扰。
        # 不能简单 +1：8127 历史上被 QA 脚本用过（见 smoke_ports 文档），
        # 所以显式跳过已登记端口与已被占用的端口。
        saved = (PORT, BASE)
        mut_port = _pick_free_port(saved[0])
        PORT, BASE = mut_port, f"http://127.0.0.1:{mut_port}"
        try:
            mutated = run_flow(200)
        finally:
            PORT, BASE = saved

        if mutated and mutated[0][0] == "服务就绪" and not mutated[0][1]:
            mutation_verdict = False
            mutation_detail = "变异版服务没起来，自检无效（环境问题，不算断言失败）"
            print(f"  [WARN] {mutation_detail}")
        else:
            # ⚠️ 计数口径：日志里第三段会打出**所有**变红的断言（[FAIL] 行），
            # 而这里若只报「截断判据」的条数，读者会看到「12 条 FAIL、却写着 5 条变红」，
            # 以为有一条不该红的红了。所以两个数都报，并把非截断判据的名字列出来。
            # （变红的那条「空 hw_model 不清空已有值」确实是同一行 `hw[:64]` 的连带
            # 影响，但它不属于 TRUNCATION_CHECKS —— 不解释就等于留了个谜。）
            failed_all = [n for n, ok, _ in mutated if not ok]
            failed_trunc = [n for n in failed_all if n in TRUNCATION_CHECKS]
            if failed_trunc:
                mutation_verdict = True
                extra = [n for n in failed_all if n not in TRUNCATION_CHECKS]
                mutation_detail = (f"变异后 {len(failed_all)} 条断言变红"
                                   f"（其中 {len(failed_trunc)} 条属截断判据，"
                                   f"如「{failed_trunc[0]}」）→ 断言有鉴别力")
                if extra:
                    mutation_detail += f"；另 {len(extra)} 条连带守卫也红：{extra}"
                print(f"  [PASS] {mutation_detail}")
            else:
                mutation_verdict = False
                mutation_detail = ("变异后截断判据**全部仍然绿** → 这些断言没有鉴别力"
                                   "（改坏了也不红），等于假绿")
                print(f"  [FAIL] {mutation_detail}")

    print()
    print("=" * 70)
    passed = sum(1 for _, ok, _ in normal if ok)
    print(f"原始版断言：{passed}/{len(normal)} 通过")
    for name, ok, detail in normal:
        if not ok:
            print(f"  FAILED: {name}  {detail}")
    print(f"静态判据：{'通过' if src_ok else '失败'}")
    print(f"变异自检：{mutation_detail}")
    ok_all = (not normal_fail) and src_ok and mutation_verdict
    print(f"\n==== 结论：{'PASS' if ok_all else 'FAIL'} ====")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
