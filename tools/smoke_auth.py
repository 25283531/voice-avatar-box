"""注册 / 鉴权安全边界冒烟（``ELDER_ALLOW_PUBLIC_REGISTER`` + 初始管理员引导）。

用法：
    python tools/smoke_auth.py [--port 8121]

这个文件为什么必须存在
----------------------
``POST /api/auth/register`` 是**匿名可达**的（没有任何鉴权装饰器），而这个服务
是公开部署的。因此"谁能注册"和"注册出来是不是管理员"两件事一旦改错，
症状是**线上被陌生人拿管理员**，本地却什么都看不出来 —— 只能靠断言钉死。

历史缺陷（本文件是它的回归网）：

* 问题 A：注册端点常年敞开且无开关；一旦前端挂上注册页，任何人都能开号消耗
  AI token。
* 问题 B（更危险）：老代码的 ``is_admin`` 判定是「库里没人 -> 你就是管理员」::

      is_admin = db.scalar(select(models.User).limit(1)) is None

  在公开部署上等价于**把管理员发给第一个点开注册页的路人**。

被钉住的不变式
--------------
1. 开关关闭 -> 403，且 detail 是**中文整句**（前端直接弹，排障时也能一眼区分
   "策略关闭"与"接口坏了"）；
2. 开关关闭 -> 连"用户名是否已存在"都**探测不到**（策略判定先于查重，
   403 必须盖过 400）；
3. 开关开启 -> 能注册；
4. **未配置引导用户名时，注册出来的 ``is_admin`` 恒为 False** —— 问题 B 的核心；
5. 配置了引导用户名 -> 只有**逐字相等的那个**是 True，换个名字就是 False；
6. 库里已有管理员时再注册 -> 现有管理员**不被降级**（仍能登录、仍能进管理端），
   新用户也不是管理员；
7. 注册出来的非管理员访问 ``/api/admin/...`` -> 403。

种子管理员的口令（本轮新增，P0）
--------------------------------
``main._seed()`` 曾经用**硬编码常量** ``admin123`` 建管理员，而本服务是公开部署的
-> 任何人 admin/admin123 登录即拿到平台管理员。被钉住的不变式：

8. 新部署空库 + 未设 ``ELDER_SEED_ADMIN_PASSWORD`` -> 种子口令是**随机的**，
   ``admin/admin123`` 必须**登不上**（核心断言）；
9. 随机口令会以 **WARNING** 打进启动日志，且用日志里那个口令**能登录**；
10. 设了 ``ELDER_SEED_ADMIN_PASSWORD`` -> 用它、admin123 失败，且口令不回显进日志；
11. **存量库**（口令已是 admin123 的库）-> 每次启动都打告警，且**只告警命中者**；
12. ``ELDER_RESET_ADMIN_PASSWORD`` -> 重启一次即轮换：旧口令登不上、新口令能登上，
    且**幂等**（第二次启动不再改）；
13. 回归保护：普通用户、已改过口令的管理员在上述所有情形下**不受影响**。

⚠️ 为什么随机分支必须**在进程内显式清空** ``ELDER_SEED_ADMIN_PASSWORD``
------------------------------------------------------------------
CI 为了让 8 个硬依赖 admin/admin123 的冒烟脚本继续跑，会在 smoke job 里**全局**
设 ``ELDER_SEED_ADMIN_PASSWORD: admin123``。这个变量会一路继承到本脚本起的子进程。
于是如果随机场景只是"不传这个变量"，CI 上它拿到的仍是 admin123 ——
**随机分支在 CI 上永远走不到**，断言会变成"只在本机能跑"的摆设。
所以下面每个需要随机口令的场景都显式传 ``None``（= 从子进程 env 里删掉它）。

为什么要用三个不同的服务端配置跑
--------------------------------
``ALLOW_PUBLIC_REGISTER`` 与 ``ELDER_BOOTSTRAP_ADMIN_USERNAME`` 都是**启动期读入**
的环境变量（``config.py`` 在模块级求值），改完不能靠发一次请求生效。
所以这里按 fixture 的方式起三个**串行**的服务进程，各自一套 env，
共用同一个 SQLite 库（这样场景 3 才能建立在"库里已经有管理员"之上 ——
那正是生产库的样子）。

⚠️ 三个场景**必须串行**、且都在**同一条前台命令**里跑完：多 agent 环境下后台
任务起的服务前台连不上，拆成多次调用会全部超时。
"""
from __future__ import annotations

import json
import os
import re
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_ports import (  # noqa: E402
    detect_addr_in_use,
    guard_or_die,
    guard_port,
    release_port,
    resolve_port,
)

# ⚠️ **本地请求绝不走代理**：本机设了 http_proxy 时，urllib 会把「访问自己刚起的
# 127.0.0.1」绕到代理一跳 —— 代理不通就 TimeoutError 且零断言失败，
# 极易被误判成代码回归。显式关闭代理让请求一定直连本机。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
DB_PATH = Path(os.environ.get("SMOKE_AUTH_DB",
                              str(DB_DIR / f"elder_smoke_auth_{os.getpid()}.db")))
if DB_PATH.exists():
    DB_PATH.unlink()

PORT = resolve_port("smoke_auth")
BASE = f"http://127.0.0.1:{PORT}"

# 子进程输出**必须落文件**而不是 PIPE：Windows 匿名管道只有 4KB，写满后
# 服务端会被永久阻塞，表现成"服务整体失去响应"，而客户端只看到读超时。
SERVER_LOG = DB_PATH.with_name(DB_PATH.stem + "_server.log")

results: list[tuple[str, bool, str]] = []

#: 要与后端 ``config.REGISTER_CLOSED_DETAIL`` 逐字对齐。
#: 断言"逐字相等"而不是"包含某几个字" —— 前端会把这句话直接弹给用户，
#: 后端改了措辞前端却不知道，是最难发现的一类失配。
CLOSED_DETAIL = "当前未开放自助注册，请联系管理员开通账号"

BOOTSTRAP_NAME = "smoke_boss"

#: 旧代码硬编码在 main._seed() 里的种子口令。**只用于构造"存量库"与旧场景**，
#: 新断言要证明的正是"它不再能登录"。
LEGACY_PW = "admin123"
#: 场景 5 里通过 ELDER_SEED_ADMIN_PASSWORD 指定的口令
SEED_PW = "Sm0ke-Seed-Pw!2026"
#: 场景 7 里通过 ELDER_RESET_ADMIN_PASSWORD 轮换到的新口令
ROTATE_PW = "N3w-Str0ng-Pw!2026"
#: 普通用户 / 已改过口令的管理员用的口令（回归保护：它们不该被动到）
NORMAL_PW = "test123456"

# 下面三个串与 server/app/main.py 的 SEED_PASSWORD_MARKER /
# DEFAULT_PASSWORD_ALERT_MARKER / PASSWORD_ROTATED_MARKER **逐字对齐**。
# 断言"逐字相等"而不是"包含某几个字"：运维要拿它们 grep 日志，
# 后端改了标记、冒烟却还绿，等于告警实际已经失效却没人知道。
SEED_PASSWORD_MARKER = "ELDER-INITIAL-PASSWORD"
DEFAULT_PASSWORD_ALERT_MARKER = "ELDER-DEFAULT-PASSWORD-ALERT"
PASSWORD_ROTATED_MARKER = "ELDER-ADMIN-PASSWORD-ROTATED"
# ⚠️ 断言一律用 ``<标记> user=`` 这个**更严的**串，不能只匹配标记本身：
# 告警正文里的处置命令会写出「日志出现 ELDER-ADMIN-PASSWORD-ROTATED 即成功」
# 这句提示，只匹配标记会把**告警行**误判成"发生了轮换"——
# 断言会红，但红的原因不是被测行为（M3 变异实测踩到过）。
ALERT_NEEDLE = DEFAULT_PASSWORD_ALERT_MARKER + " user="
ROTATED_NEEDLE = PASSWORD_ROTATED_MARKER + " user="
#: 随机口令告警必须出现的中文提示（主理人指定文案）
SEED_RANDOM_HINT = "这是唯一的初始口令，请立即保存并登录后修改"


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def group(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def req(method: str, path: str, body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
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
        except Exception:
            return e.code, raw


def _read_log_tail(max_bytes: int = 8192) -> str:
    """读服务端日志尾部。**另开句柄**读，不能复用传给子进程的那个 log_fp
    （它被子进程继承、带缓冲，read() 只会拿到空串）。"""
    try:
        return SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-max_bytes:]
    except OSError:
        return ""


def _log_text(max_bytes: int = 65536) -> str:
    """本次启动的完整日志（启动期日志量不小，窗口比默认的大一档）。"""
    return _read_log_tail(max_bytes=max_bytes)


def _log_lines_with(marker: str) -> list[str]:
    """日志里含某个标记的所有行。用于判定"告警发生了几次/针对谁"。"""
    return [ln for ln in _log_text().splitlines() if marker in ln]


def find_seed_password() -> str | None:
    """从启动日志里把随机种子口令抠出来；没有则返回 None。

    为什么要从日志里抠而不是让服务端暴露一个接口：运维拿初始口令的**唯一**
    途径就是 ``docker logs``（随机口令重启后无法复现）。断言"日志里那个口令
    能登录"，等于同时验了「打印了」和「打印的正是生效的那个」两件事 ——
    只断言"日志里出现了某个字符串"是抓不住"打印了 A、实际设了 B"的错实现的。
    """
    pat = re.compile(re.escape(SEED_PASSWORD_MARKER) + r"=(\S+)")
    for ln in _log_text().splitlines():
        m = pat.search(ln)
        if m:
            return m.group(1)
    return None


def wait_ready(timeout: float = 120.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            code, _ = req("GET", "/api/health")
            if code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.6)
    return False


def start_server(extra_env: dict[str, str | None], db_path: Path | None = None):
    """起一个带指定 env 的服务进程。返回 ``proc``；失败返回 None。

    :param db_path: 该场景**独占**的库文件，默认 :data:`DB_PATH`。
        场景 2b 必须另开一个库 —— 它会把 users 表抹空，
        留在主库上会让后续场景里的 seed 管理员凭空消失。
    :param extra_env: 要注入/覆盖的环境变量。**值为 ``None`` 表示"显式删除"**，
        不是"设成空串"。

        ⚠️ 这个语义是**必需**的，不是语法糖：CI 的 smoke job 为了那 8 个硬依赖
        admin/admin123 的冒烟脚本，会在 job 级设 ``ELDER_SEED_ADMIN_PASSWORD``。
        它会一路继承到这里的子进程，于是"随机口令"场景如果只是**不传**这个变量，
        拿到的仍是 admin123 —— 随机分支在 CI 上**永远走不到**。
        必须显式删掉才能真的触发随机分支。
    """
    db_path = db_path or DB_PATH
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{db_path.as_posix()}"
    env["ELDER_JWT_SECRET"] = "smoke-auth-secret"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"
    env["PYTHONIOENCODING"] = "utf-8"
    for key, val in extra_env.items():
        if val is None:
            env.pop(key, None)
        else:
            env[key] = val

    guard_or_die(PORT, "smoke_auth.py")
    hold, hold_hint = guard_port(PORT)
    if hold is None:
        print("[boot] " + hold_hint.replace("\n", "\n[boot] "))
        return None
    # 占位必须在 spawn 之前释放，否则自己挡住自己
    release_port(hold, PORT)

    log_fp = SERVER_LOG.open("w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=str(SERVER), env=env,
        stdout=log_fp, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace",
    )
    proc._log_fp = log_fp  # type: ignore[attr-defined]
    if not wait_ready():
        print(f"[boot] server did not become ready. log tail ({SERVER_LOG}):")
        print(_read_log_tail())
        stop_server(proc)
        return None
    # 阶段③：就绪也要确认没有 bind 失败痕迹。否则 wait_ready 探到的可能是
    # **别人的服务**，全部断言会打在别人身上 —— 那比超时危险得多（假绿）。
    if detect_addr_in_use(_read_log_tail()):
        print(f"[boot] ✗ 端口 {PORT} bind 失败，本次结果不可信。换端口重跑。")
        stop_server(proc)
        return None
    print(f"[boot] ready on :{PORT}  env={extra_env or '{}'}")
    return proc


def stop_server(proc) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    log_fp = getattr(proc, "_log_fp", None)
    if log_fp is not None:
        try:
            log_fp.close()
        except OSError:
            pass


def register(username: str, password: str = "test123456"):
    return req("POST", "/api/auth/register",
               {"username": username, "password": password, "nickname": username})


def login(username: str, password: str = "test123456"):
    return req("POST", "/api/auth/login", {"username": username, "password": password})


def main() -> int:
    print(f"[auth-smoke] db={DB_PATH}  port={PORT}")
    ok_all = True

    # ================================================================ 场景 1
    group("场景 1：ELDER_ALLOW_PUBLIC_REGISTER=0 —— 注册关闭")
    # ⚠️ 下面每个旧场景都显式传 ELDER_SEED_ADMIN_PASSWORD=admin123：它们断言的是
    #「注册/引导」的行为，种子口令对它们是**前提**而不是被测对象。
    # 显式写死而不是依赖"没设就是 admin123" —— 那正是本轮要消灭的假设。
    proc = start_server({"ELDER_ALLOW_PUBLIC_REGISTER": "0",
                         "ELDER_SEED_ADMIN_PASSWORD": LEGACY_PW})
    if proc is None:
        return 1
    try:
        code, body = register("closeduser")
        check("A01 关闭时注册 -> 403（不是 404/500：要在排障时一眼认出是策略关闭）",
              code == 403, f"code={code} body={body!r}")
        check("A02 detail 是**字符串**而不是对象（前端 api() 直接 new Error(detail)）",
              isinstance(body, dict) and isinstance(body.get("detail"), str),
              repr(body.get("detail") if isinstance(body, dict) else body))
        check("A03 detail 逐字== config.REGISTER_CLOSED_DETAIL（措辞不许漂移）",
              isinstance(body, dict) and body.get("detail") == CLOSED_DETAIL,
              repr(body.get("detail") if isinstance(body, dict) else None))
        check("A04 detail 含中文（给用户的必须是人话）",
              isinstance(body, dict) and isinstance(body.get("detail"), str)
              and any('一' <= ch <= '鿿' for ch in body["detail"]),
              repr(body.get("detail") if isinstance(body, dict) else None))
        # 开关关闭时不该变成用户名枚举口：策略判定必须先于查重。
        # 用**肯定存在的**用户名（种子建的 admin）去注册：
        #   若实现是先查重 -> 会拿到 400「用户名已存在」-> 泄露该用户有没有注册过。
        code_exist, body_exist = register("admin")
        check("A05 ★ 关闭时用**已存在的用户名**注册仍 403（不得泄露用户名是否已存在）",
              code_exist == 403,
              f"code={code_exist} body={body_exist!r}")
        # 登录必须仍然可用 —— 关的是"注册"，不是整个账号体系
        code, tk = login("admin", "admin123")
        check("A06 关闭注册不影响已有账号登录", code == 200 and bool(tk.get("access_token")),
              str(code))
    finally:
        stop_server(proc)

    # ================================================================ 场景 2
    group("场景 2：未配置 ELDER_BOOTSTRAP_ADMIN_USERNAME —— 注册者一律不是管理员")
    proc = start_server({"ELDER_ALLOW_PUBLIC_REGISTER": "1",
                         "ELDER_SEED_ADMIN_PASSWORD": LEGACY_PW})
    if proc is None:
        return 1
    try:
        code, body = register("plainuser")
        check("B01 开关开启 -> 注册成功 201", code == 201, f"code={code} body={body!r}")
        check("B02 ★ 未配引导用户名 -> is_admin is False（问题 B 的核心断言）",
              isinstance(body, dict) and body.get("is_admin") is False,
              f"is_admin={body.get('is_admin') if isinstance(body, dict) else body!r}")
        _, tk = login("plainuser")
        check("B03 登录成功", bool(tk.get("access_token")), str(tk)[:80])
        check("B04 登录响应 is_admin 也是 False（token 里的 admin 声明同步为假）",
              tk.get("is_admin") is False, f"is_admin={tk.get('is_admin')!r}")
        token = tk.get("access_token", "")
        code, me = req("GET", "/api/auth/me", token=token)
        check("B05 /api/auth/me 回读 is_admin=False",
              code == 200 and me.get("is_admin") is False, str(me.get("is_admin")))
        # 越权：注册出来的非管理员不得进管理端
        code, _ = req("GET", "/api/admin/presets", token=token)
        check("B06 ★ 非管理员访问 /api/admin/presets -> 403（不是 200/404）",
              code == 403, f"code={code}")
        # 既有管理员不被降级（种子建的 admin/admin123）
        code, adm = login("admin", "admin123")
        code_admin_ep, _ = req("GET", "/api/admin/presets", token=adm.get("access_token", ""))
        check("B07 ★ 已有管理员（seed）未被降级：能登录且管理端 200",
              code == 200 and code_admin_ep == 200,
              f"login={code} admin_ep={code_admin_ep}")
        code, me_admin = req("GET", "/api/auth/me", token=adm.get("access_token", ""))
        check("B08 ★ 已有管理员的 is_admin 仍为 True（别人注册没改它）",
              code == 200 and me_admin.get("is_admin") is True, str(me_admin.get("is_admin")))
        # 第二个普通用户也不是管理员 —— 排掉"只保护了第一个"的错实现
        code, b2 = register("plainuser2")
        check("B09 ★ 再注册一个用户同样 is_admin=False（不是只挡第一个）",
              isinstance(b2, dict) and b2.get("is_admin") is False,
              f"is_admin={b2.get('is_admin') if isinstance(b2, dict) else b2!r}")
    finally:
        stop_server(proc)

    # ================================================================ 场景 2b
    # ⚠️ 这一段是**整套断言里唯一能真正甄别「第一个注册者=管理员」的那一条**，别删。
    #
    # 为什么必须先把库里的用户**删空**：``main._seed()`` 在空库首次启动时会先建
    # ``admin/admin123``（is_admin=True）。也就是说，只要库里有人，老规则
    # ``select(User).limit(1) is None`` 就恒为 False —— 它和新规则在"非空库"上
    # **表现完全相同**。若不把库清空，上面 B02 那条"核心断言"其实是**假绿**：
    # 把实现改回老规则它照样通过（本轮变异实测确认过）。
    #
    # 所以这里用 sqlite 直连把 users 表抹掉，造出真正的"全新部署的第一个注册者"
    # 场景 —— 那正是老规则会发管理员、新规则不会的那个点。
    group("场景 2b：★ 空库上的第一个注册者（区分「顺序授予」与「显式引导」）")
    # ⚠️ 必须**独占一个库**：本场景会把 users 表抹空。若与后续场景共用，
    # 后面那条「已有管理员不被降级」就失去了可打架的对象（管理员已经不存在了）。
    db_empty = DB_PATH.with_name(DB_PATH.stem + "_empty.db")
    if db_empty.exists():
        db_empty.unlink()
    proc = start_server({"ELDER_ALLOW_PUBLIC_REGISTER": "1",
                         "ELDER_SEED_ADMIN_PASSWORD": LEGACY_PW}, db_path=db_empty)
    if proc is None:
        return 1
    try:
        import sqlite3
        # 重试若干次：服务端 session 若正持有写事务，SQLite 会短瞬锁库。
        deleted = False
        for _ in range(20):
            try:
                con = sqlite3.connect(str(db_empty), timeout=5)
                try:
                    con.execute("DELETE FROM users")
                    con.commit()
                    deleted = True
                    break
                finally:
                    con.close()
            except sqlite3.OperationalError:
                time.sleep(0.2)
        check("D00 前置：库已被抹空（否则本段断言不成立）", deleted, f"deleted={deleted}")

        code, b1 = register("firstblood")
        check("D01 ★★ 空库第一个注册者 is_admin is False"
              "（老规则：库里没人 -> 你就是管理员；新规则：没有任何显式引导 -> 谁都不是）",
              isinstance(b1, dict) and b1.get("is_admin") is False,
              f"code={code} is_admin={b1.get('is_admin') if isinstance(b1, dict) else b1!r}")
        _, tkf = login("firstblood")
        code_first_ep, _ = req("GET", "/api/admin/presets", token=tkf.get("access_token", ""))
        check("D02 ★ 空库第一个注册者进不了管理端 -> 403", code_first_ep == 403,
              f"code={code_first_ep}")
        code, b2 = register("secondblood")
        check("D03 空库第二个注册者同样 False",
              isinstance(b2, dict) and b2.get("is_admin") is False,
              f"is_admin={b2.get('is_admin') if isinstance(b2, dict) else b2!r}")
    finally:
        stop_server(proc)

    # ================================================================ 场景 3
    group(f"场景 3：ELDER_BOOTSTRAP_ADMIN_USERNAME={BOOTSTRAP_NAME} —— 只认这一个名字")
    proc = start_server({"ELDER_ALLOW_PUBLIC_REGISTER": "1",
                         "ELDER_BOOTSTRAP_ADMIN_USERNAME": BOOTSTRAP_NAME,
                         "ELDER_SEED_ADMIN_PASSWORD": LEGACY_PW})
    if proc is None:
        return 1
    try:
        code, body = register(BOOTSTRAP_NAME)
        check("C01 ★ 引导用户名注册 -> is_admin is True",
              isinstance(body, dict) and body.get("is_admin") is True,
              f"code={code} is_admin={body.get('is_admin') if isinstance(body, dict) else body!r}")
        _, tk = login(BOOTSTRAP_NAME)
        check("C02 引导用户拿到的 token 带 admin 声明",
              tk.get("is_admin") is True, f"is_admin={tk.get('is_admin')!r}")
        code_admin_ep, _ = req("GET", "/api/admin/presets", token=tk.get("access_token", ""))
        check("C03 引导用户能进管理端 200", code_admin_ep == 200, f"code={code_admin_ep}")

        code, b2 = register("stranger")
        check("C04 ★ 换个用户名注册 -> is_admin is False（权限没泛化到其它名字）",
              isinstance(b2, dict) and b2.get("is_admin") is False,
              f"code={code} is_admin={b2.get('is_admin') if isinstance(b2, dict) else b2!r}")
        _, tk2 = login("stranger")
        code_admin_ep2, _ = req("GET", "/api/admin/presets", token=tk2.get("access_token", ""))
        check("C05 ★ 换名字注册者的 token 进不了管理端 -> 403",
              code_admin_ep2 == 403, f"code={code_admin_ep2}")

        # 大小写 / 前后缀必须不匹配 —— 排掉"子串包含"式的错实现
        code, b3 = register(BOOTSTRAP_NAME.upper())
        check("C06 ★ 大小写不同 -> is_admin is False（必须逐字相等，不是忽略大小写）",
              isinstance(b3, dict) and b3.get("is_admin") is False,
              f"is_admin={b3.get('is_admin') if isinstance(b3, dict) else b3!r}")
        code, b4 = register(BOOTSTRAP_NAME + "_x")
        check("C07 ★ 前缀相同 -> is_admin is False（不是 startswith 匹配）",
              isinstance(b4, dict) and b4.get("is_admin") is False,
              f"is_admin={b4.get('is_admin') if isinstance(b4, dict) else b4!r}")

        # 已有管理员仍然不被降级
        code, adm = login("admin", "admin123")
        check("C08 ★ 引导不影响既有管理员：admin 仍是 is_admin=True",
              code == 200 and adm.get("is_admin") is True, str(adm.get("is_admin")))
    finally:
        stop_server(proc)

    # ================================================================ 场景 4
    # ★★ 本轮 P0 的核心：新部署空库不再产生可猜测口令。
    group("场景 4：★ 新部署空库 + 未设 ELDER_SEED_ADMIN_PASSWORD -> 随机口令")
    db_rand = DB_PATH.with_name(DB_PATH.stem + "_rand.db")
    if db_rand.exists():
        db_rand.unlink()
    # ⚠️ 显式传 None = 从子进程 env 里**删掉**这个变量。CI 在 job 级设了它，
    #   不删就永远走不到随机分支（详见模块 docstring）。
    proc = start_server({"ELDER_SEED_ADMIN_PASSWORD": None}, db_path=db_rand)
    if proc is None:
        return 1
    try:
        code, body = login("admin", LEGACY_PW)
        check("E01 ★★ 空库 + 未设种子口令：admin/admin123 登录必须**失败**（P0 核心）",
              code == 401, f"code={code} body={body!r}")
        rand_pw = find_seed_password()
        check("E02 启动日志打印了随机口令（ELDER-INITIAL-PASSWORD=<pwd>）",
              rand_pw is not None, f"parsed={rand_pw!r}")
        check("E03 打印出来的口令**不是**默认口令且够长（>=16 字符）",
              bool(rand_pw) and rand_pw != LEGACY_PW and len(rand_pw) >= 16,
              f"len={len(rand_pw) if rand_pw else 0}")
        seed_lines = _log_lines_with(SEED_PASSWORD_MARKER)
        check("E04 该条日志是 **WARNING** 级（运维的第一现场是 docker logs）",
              bool(seed_lines) and all("WARNING" in ln for ln in seed_lines),
              (seed_lines[0][:120] if seed_lines else "无该日志"))
        check("E05 文案含「这是唯一的初始口令，请立即保存并登录后修改」",
              SEED_RANDOM_HINT in _log_text(),
              "" if SEED_RANDOM_HINT in _log_text() else "缺失：" + SEED_RANDOM_HINT)
        code_rand, tk_rand = login("admin", rand_pw or "__none__")
        check("E06 ★ 用**日志里那个**随机口令能登录成功"
              "（排掉「打印了 A、实际设成 B」的错实现）",
              code_rand == 200 and bool(tk_rand.get("access_token")),
              f"code={code_rand}")
        check("E07 随机口令登进来的是**管理员**（种子账号角色没被改坏）",
              tk_rand.get("is_admin") is True, f"is_admin={tk_rand.get('is_admin')!r}")
        code_admin_ep, _ = req("GET", "/api/admin/presets",
                               token=tk_rand.get("access_token", ""))
        check("E08 该账号能进管理端 200", code_admin_ep == 200, f"code={code_admin_ep}")
        check("E09 本场景**不**产生默认口令告警（口令不是默认值）",
              not _log_lines_with(ALERT_NEEDLE),
              str(_log_lines_with(ALERT_NEEDLE))[:120])
    finally:
        stop_server(proc)

    # ================================================================ 场景 5
    group("场景 5：设了 ELDER_SEED_ADMIN_PASSWORD -> 用它，admin123 失败")
    db_seedpw = DB_PATH.with_name(DB_PATH.stem + "_seedpw.db")
    if db_seedpw.exists():
        db_seedpw.unlink()
    proc = start_server({"ELDER_SEED_ADMIN_PASSWORD": SEED_PW}, db_path=db_seedpw)
    if proc is None:
        return 1
    try:
        code, tk = login("admin", SEED_PW)
        check("F01 用 ELDER_SEED_ADMIN_PASSWORD 的值登录成功",
              code == 200 and bool(tk.get("access_token")), f"code={code}")
        code_old, _ = login("admin", LEGACY_PW)
        check("F02 ★ admin123 登录失败（种子口令真的被换掉了）", code_old == 401,
              f"code={code_old}")
        check("F03 配置来的口令**不回显**进日志（日志是多一个泄露面）",
              find_seed_password() is None, f"parsed={find_seed_password()!r}")
    finally:
        stop_server(proc)

    # ================================================================ 场景 6
    # ⚠️ 这一段打的是**存量库**：先按老代码的样子造一个「admin 的口令就是
    # admin123」的库，再关掉种子口令重启 —— 那正是生产库现在的样子。
    # 改种子对这种库**一点用都没有**，所以必须有体检告警。
    group("场景 6：★ 存量库（admin 口令仍是 admin123）-> 启动打印告警")
    db_legacy = DB_PATH.with_name(DB_PATH.stem + "_legacy.db")
    if db_legacy.exists():
        db_legacy.unlink()
    # ---- 6A：造存量库（顺带放一个「已改过口令的管理员」和一个普通用户做回归对象）
    proc = start_server({"ELDER_ALLOW_PUBLIC_REGISTER": "1",
                         "ELDER_SEED_ADMIN_PASSWORD": LEGACY_PW,
                         "ELDER_BOOTSTRAP_ADMIN_USERNAME": "boss2"},
                        db_path=db_legacy)
    if proc is None:
        return 1
    try:
        code, adm = login("admin", LEGACY_PW)
        check("L00 前置：存量库已就位（admin/admin123 能登录）",
              code == 200 and adm.get("is_admin") is True, f"code={code}")
        code, b2 = register("boss2", NORMAL_PW)
        check("L01 前置：造出第二个管理员 boss2（口令不是默认值，作为回归对象）",
              isinstance(b2, dict) and b2.get("is_admin") is True,
              f"is_admin={b2.get('is_admin') if isinstance(b2, dict) else b2!r}")
        code, n1 = register("normal1", NORMAL_PW)
        check("L02 前置：造出普通用户 normal1（回归对象）",
              isinstance(n1, dict) and n1.get("is_admin") is False,
              f"is_admin={n1.get('is_admin') if isinstance(n1, dict) else n1!r}")
    finally:
        stop_server(proc)

    # ---- 6B：不再给种子口令（存量库不会重建 admin），体检必须告警
    proc = start_server({"ELDER_SEED_ADMIN_PASSWORD": None}, db_path=db_legacy)
    if proc is None:
        return 1
    try:
        alerts = _log_lines_with(ALERT_NEEDLE)
        check("G01 ★ 存量库启动打印默认口令告警（ELDER-DEFAULT-PASSWORD-ALERT）",
              bool(alerts), f"{len(alerts)} 条告警行")
        check("G02 告警点名的是 admin", bool(alerts) and any("user=admin" in ln for ln in alerts),
              (alerts[0][:160] if alerts else "无告警行"))
        check("G03 该告警是 **WARNING** 级",
              bool(alerts) and all("WARNING" in ln for ln in alerts),
              (alerts[0][:120] if alerts else "无告警行"))
        check("G04 告警给出了处置命令（ELDER_RESET_ADMIN_PASSWORD）",
              "ELDER_RESET_ADMIN_PASSWORD" in _log_text(),
              "" if "ELDER_RESET_ADMIN_PASSWORD" in _log_text() else "日志里没有处置命令")
        check("G05 ★★ 只告警**命中者**：告警恰好 1 条（boss2 口令不是默认值，不该被告警）",
              len(alerts) == 1, f"{len(alerts)} 条 -> "
              + " | ".join(ln[:80] for ln in alerts))
        check("G06 告警文案里不出现 boss2 / normal1（未命中者完全不受影响）",
              not any(x in " ".join(alerts) for x in ("boss2", "normal1")),
              " ".join(alerts)[:160])
        code, adm = login("admin", LEGACY_PW)
        check("G07 告警对应的账号**真的**能用默认口令登录（告警非空喊）",
              code == 200, f"code={code}")
        code, boss = login("boss2", NORMAL_PW)
        check("G08 ★ 回归：已改过口令的管理员 boss2 仍能登录且仍是管理员",
              code == 200 and boss.get("is_admin") is True,
              f"code={code} is_admin={boss.get('is_admin')!r}")
        code, nor = login("normal1", NORMAL_PW)
        check("G09 ★ 回归：普通用户 normal1 仍能登录且不是管理员",
              code == 200 and nor.get("is_admin") is False,
              f"code={code} is_admin={nor.get('is_admin')!r}")
    finally:
        stop_server(proc)

    # ================================================================ 场景 7
    group("场景 7：★ ELDER_RESET_ADMIN_PASSWORD —— 存量口令一次性轮换")
    # ---- 7A：护栏 —— 开关值自己就是默认口令时必须拒绝（否则每 boot 改一次）
    proc = start_server({"ELDER_SEED_ADMIN_PASSWORD": None,
                         "ELDER_RESET_ADMIN_PASSWORD": LEGACY_PW}, db_path=db_legacy)
    if proc is None:
        return 1
    try:
        refused = "已拒绝轮换" in _log_text()
        check("H01 ★ 开关值是默认口令 -> 拒绝轮换并打印说明",
              refused, "" if refused else "日志里没有「已拒绝轮换」")
        code, _ = login("admin", LEGACY_PW)
        check("H02 拒绝轮换后旧口令仍有效（没被偷偷改成别的）", code == 200, f"code={code}")
        n_alert = len(_log_lines_with(ALERT_NEEDLE))
        check("H03 仍在告警（问题没被解决就该继续喊）", n_alert >= 1, f"{n_alert} 条告警行")
    finally:
        stop_server(proc)

    # ---- 7B：真的轮换
    proc = start_server({"ELDER_SEED_ADMIN_PASSWORD": None,
                         "ELDER_RESET_ADMIN_PASSWORD": ROTATE_PW}, db_path=db_legacy)
    if proc is None:
        return 1
    try:
        rots = _log_lines_with(ROTATED_NEEDLE)
        check("I01 ★ 日志出现轮换标记（ELDER-ADMIN-PASSWORD-ROTATED user=admin）",
              bool(rots) and any("user=admin" in ln for ln in rots),
              " ".join(rots)[:160])
        check("I02 轮换提示「用完即删」",
              "用完即删" in _log_text(),
              "" if "用完即删" in _log_text() else "日志里没有「用完即删」")
        code_old, _ = login("admin", LEGACY_PW)
        check("I03 ★★ 旧口令 admin123 登录失败（存量漏洞已处置）", code_old == 401,
              f"code={code_old}")
        code_new, tk_new = login("admin", ROTATE_PW)
        check("I04 ★★ 新口令登录成功且仍是管理员",
              code_new == 200 and tk_new.get("is_admin") is True,
              f"code={code_new} is_admin={tk_new.get('is_admin')!r}")
        check("I05 轮换后本轮**不再**告警（体检跑在轮换之后）",
              not _log_lines_with(ALERT_NEEDLE),
              " ".join(_log_lines_with(ALERT_NEEDLE))[:160])
        code, boss = login("boss2", NORMAL_PW)
        check("I06 ★ 回归：轮换**没有**波及已改过口令的管理员 boss2",
              code == 200 and boss.get("is_admin") is True, f"code={code}")
        code, nor = login("normal1", NORMAL_PW)
        check("I07 ★ 回归：轮换**没有**波及普通用户 normal1",
              code == 200 and nor.get("is_admin") is False, f"code={code}")
    finally:
        stop_server(proc)

    # ---- 7C：幂等 —— 开关还挂着时再启一次，**不许**再改一遍
    proc = start_server({"ELDER_SEED_ADMIN_PASSWORD": None,
                         "ELDER_RESET_ADMIN_PASSWORD": ROTATE_PW}, db_path=db_legacy)
    if proc is None:
        return 1
    try:
        check("J01 ★ 幂等：第二次启动**不再**轮换（不产生 ROTATED 标记）",
              not _log_lines_with(ROTATED_NEEDLE),
              " ".join(_log_lines_with(ROTATED_NEEDLE))[:160])
        check("J02 第二次启动也不告警（口令已不是默认值）",
              not _log_lines_with(ALERT_NEEDLE),
              " ".join(_log_lines_with(ALERT_NEEDLE))[:160])
        code, tk = login("admin", ROTATE_PW)
        check("J03 新口令在第二次启动后仍有效（没被改坏）",
              code == 200 and bool(tk.get("access_token")), f"code={code}")
        code, boss = login("boss2", NORMAL_PW)
        check("J04 回归：boss2 仍不受影响", code == 200, f"code={code}")
    finally:
        stop_server(proc)

    # ================================================================ 汇总
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
