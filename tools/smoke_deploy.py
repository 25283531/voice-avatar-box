"""部署模式冒烟：ELDER_DEPLOY_MODE=tunnel/direct/auto + 双域名 WS + 启动自检。

覆盖（与任务书断言清单一一对应）
------------------------------
A. **auto 模式与今天逐字一致（回归）**：无显式地址时按请求推导；旧变量
   ``ELDER_VOICE_PUBLIC_BASE`` 的各種取值（含历史上不校验的 ``wss://``）
   输出与改动前完全一致；新变量 ``ELDER_PUBLIC_BASE_URL`` 在 auto 下同样
   生效（逃生舱语义不变）。
B. **direct 模式与今天逐字一致**：direct 是 auto 的显式声明版，推导逻辑
   逐字相同（无地址 / 旧变量 / 新变量三条路各自钉死）。
C. **tunnel 模式**：显式对外地址优先，与请求无关；支持 HTTP 与 WS **两个
   不同域名**；旧变量在 tunnel 下可用（向后兼容）；明文 http 地址 ->
   下发 ws://。
D. **启动期校验必须真的生效**：tunnel 缺地址 / 只配 WS 不配 HTTP / 模式名
   写错 / scheme 写错 / tunnel 下旧变量 scheme 错 —— 全部断言**启动失败**
   （SystemExit），且报错信息点名是哪个变量、哪里不合法；失败后恢复正常
   配置必须能重新启动（无状态污染）。
E. **uvicorn 框架默认层钉住**：回环对端 + ``X-Forwarded-Proto: https`` 会
   被 uvicorn 采纳（proxy_headers 默认 True、只信 127.0.0.1）—— 这正是
   生产里"对端是 Docker 网桥网关 ⇒ 不被采纳 ⇒ ws://"的对照面。
F. **真实 uvicorn 子进程**（Dockerfile CMD 形态，不传任何 proxy 参数）：
   tunnel 缺地址 -> 子进程非零退出 + stderr 点名；tunnel 配齐 -> 真起来、
   /api/health 200、启动日志里带部署摘要。
G. **反向验证（改坏必须转红）**：把 tunnel 启动校验改 neuter -> 缺地址不再
   报错（证明 D 组断言是有负载的，不是恒真）；把 WS 推导的 https->wss 改错
   -> 推导结果确实变错（证明 C 组断言会红）；把请求推导的 scheme 改恒 ws
   -> https 请求下结果确实变错（证明 A/B 组断言会红）。
H. **与 git 基线的逐字比对**：从 ``BASELINE_REF`` 取出改动前的 config.py 与
   device_api.py，在同一组 (环境 x 请求) 上跑旧代码与新代码，输出必须
   **逐字相同**。这是"auto/direct 与今天一致"最直接的证明 —— 不是"看起来
   一样"，是两边跑出来比对。

⚠️ 为什么进程内起服务（uvicorn 后台线程 + threading.Timer 兜底）：
   后台任务起的服务前台连不上（网络隔离），所以本脚本必须在**单条前台命令**
   内自己起服务、自己断言、自己关服。Timer 是兜底：断言阶段万一卡死，
   300 秒后强制关服，保证进程能退出、CI 不会挂住。

⚠️ 为什么 G 组用 exec 变异而不是 qa5_reverse.py 那种"落盘变异再复原"：
   本脚本与其它 agent 共享同一个 git 工作区，落盘变异有覆盖别人改动的风险。
   这里把源码读出来做文本变异、在**全新模块命名空间**里 exec —— 磁盘文件
   一个字节都不动，不存在复原问题，且变异必然打在真实源码上。

用法::

    python tools/smoke_deploy.py [--port 8122]
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable

sys.path.insert(0, str(ROOT / "tools"))
from smoke_ports import guard_or_die, resolve_port  # noqa: E402

PORT = resolve_port("smoke_deploy")
BASE = f"http://127.0.0.1:{PORT}"
SMOKE_ROUTE = "/__deploy_smoke/endpoints"

WS_PATH = "/api/voice/v1/ws"
OTA_PATH = "/api/voice/ota"

#: H 组逐字比对的基线：本特性之前的最后一个提交。取旧代码用。
#: 将来若因历史重写找不到这个 ref，H 组会硬失败并提示更新这里 —— 刻意不
#: 静默跳过："与今天逐字一致"是本轮的核心承诺，证不了就是证不了。
BASELINE_REF = "c6f00e8"

#: 全部部署相关环境变量。每个场景开始前先全部清空再按场景设置，
#: 否则上个场景会漏过来（config 是 import 期读 env 的，漏一个就是假绿）。
DEPLOY_ENV_KEYS = ("ELDER_DEPLOY_MODE", "ELDER_PUBLIC_BASE_URL",
                   "ELDER_WS_PUBLIC_BASE_URL", "ELDER_VOICE_PUBLIC_BASE")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"\n         -> {detail}" if detail else ""))
    return bool(ok)


def group(title: str) -> None:
    print(f"\n---- {title} " + "-" * max(4, 58 - len(title)))


# ================================================================ 进程内起服务
# 环境变量必须在 import app.* **之前**设好（config 是 import 期读 env 的）。
TMPDIR = Path(tempfile.mkdtemp(prefix="smoke_deploy_"))
DB_PATH = TMPDIR / "smoke_deploy.db"

os.environ["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["ELDER_JWT_SECRET"] = "smoke-deploy-secret-0123456789abcdef"
os.environ["ELDER_SCHEDULER_INTERVAL"] = "3600"   # 不起后台调度
os.environ["PYTHONIOENCODING"] = "utf-8"
for _k in DEPLOY_ENV_KEYS:
    os.environ.pop(_k, None)
for _p in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_p, None)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")

sys.path.insert(0, str(SERVER))
import uvicorn  # noqa: E402
from fastapi import Request  # noqa: E402

import app.main as main_mod  # noqa: E402
import app.config as config_mod  # noqa: E402
import app.routers.device_api as device_api_mod  # noqa: E402

#: 进程内的 uvicorn 服务（后台线程）。启动参数与 Dockerfile CMD 同形：
#: **不传任何 proxy 参数**，用框架默认值（proxy_headers=True、
#: forwarded_allow_ips=127.0.0.1）—— 生产容器就是这个形态。
server: uvicorn.Server | None = None


def install_probe_route() -> None:
    """装一个测试专用探针：原样返回 _voice_endpoints() 的推导结果。

    走**真实 HTTP**（uvicorn + 中间件全链）而不是直接 import 函数调，
    这样 request.url.scheme / Host 头的行为与生产逐字一致。
    动态取模块属性：场景之间会 importlib.reload(device_api)，探针打到
    的必须永远是**当前场景**的代码。
    """
    app = main_mod.app

    @app.get(SMOKE_ROUTE)
    def _probe(request: Request):
        from app.routers import device_api as _da
        ws_url, ota_url = _da._voice_endpoints(request)
        return {"ws_url": ws_url, "ota_url": ota_url}


def start_server() -> None:
    global server
    server = uvicorn.Server(uvicorn.Config(
        main_mod.app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()


def wait_ready(timeout: float = 90.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if server is not None and server.should_exit:
            print("[fatal] 服务线程提前退出（端口被抢或启动异常）")
            return False
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


# ================================================================ 场景基础设施
def set_deploy_env(env: dict[str, str]) -> None:
    for key in DEPLOY_ENV_KEYS:
        os.environ.pop(key, None)
    for key, value in env.items():
        if value:
            os.environ[key] = value


def reload_config_and_router(env: dict[str, str]) -> str | None:
    """按 env 重载 config + device_api。

    Returns:
        None = 启动通过；字符串 = SystemExit 的消息（= 启动失败）。
        这正是"启动期校验"的执行路径：config 被 import 的那一刻就是校验时刻，
        对 uvicorn 来说比任何 lifespan 钩子都早。
    """
    set_deploy_env(env)
    try:
        importlib.reload(config_mod)
    except SystemExit as exc:
        # config 可能停在半初始化状态；下一个场景会整体重载，不会漏脏数据。
        return str(exc)
    importlib.reload(device_api_mod)
    return None


def probe(extra_headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(BASE + SMOKE_ROUTE, headers=extra_headers or {})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fake_request(scheme: str, host: str) -> Request:
    """合成一个 starlette Request（scheme/Host 可控，不走网络）。

    用于直接调 _voice_endpoints()：https 请求在回环探测下只能靠 XFP 头
    间接造（见 E 组），合成请求让"https + 任意 Host"变成确定项。
    """
    if ":" in host:
        hostname, port_s = host.rsplit(":", 1)
        port = int(port_s)
    else:
        hostname = host
        port = 443 if scheme == "https" else 80
    scope = {
        "type": "http", "method": "GET", "path": "/", "raw_path": b"/",
        "headers": [(b"host", host.encode())],
        "scheme": scheme, "server": (hostname, port),
        "client": ("203.0.113.7", 51000), "query_string": b"",
        "http_version": "1.1", "root_path": "",
    }
    return Request(scope)


def expect_http(name: str, env: dict[str, str], ws: str, ota: str) -> None:
    """启动必须通过，且真实 HTTP 探针拿到的地址逐字等于期望。"""
    err = reload_config_and_router(env)
    if not check(f"{name}：启动通过", err is None, err or ""):
        return
    got = probe()
    check(f"{name}：ws_url == {ws}", got["ws_url"] == ws, f"实际 {got['ws_url']}")
    check(f"{name}：ota_url == {ota}", got["ota_url"] == ota, f"实际 {got['ota_url']}")


def expect_call(name: str, env: dict[str, str], request: Request,
                ws: str, ota: str) -> None:
    err = reload_config_and_router(env)
    if not check(f"{name}：启动通过", err is None, err or ""):
        return
    got_ws, got_ota = device_api_mod._voice_endpoints(request)
    check(f"{name}：ws_url == {ws}", got_ws == ws, f"实际 {got_ws}")
    check(f"{name}：ota_url == {ota}", got_ota == ota, f"实际 {got_ota}")


def expect_boot_fail(name: str, env: dict[str, str], needles: tuple[str, ...]) -> None:
    """启动必须失败，且报错点名每一个 needle（变量名/取值/合法清单）。"""
    err = reload_config_and_router(env)
    if not check(f"{name}：启动必须失败（不能静默降级）", err is not None,
                 "未报错 —— 配置矛盾被放过了，正是「能心跳不能说话」的成因"
                 if err is None else ""):
        return
    missing = [n for n in needles if n not in err]
    check(f"{name}：报错点名 {list(needles)}", not missing,
          f"缺失 {missing}；原文首行：{err.splitlines()[0] if err else ''}")


# ================================================================ 变异执行
def exec_fresh_module(name: str, src: str,
                      package: str | None = None) -> types.ModuleType:
    """把一段源码 exec 进一个**全新模块命名空间**（磁盘文件不动）。

    package 用于让源码里的相对导入（``from ..config import ...``）解析到
    真实的 app 包 —— 这样变异版 device_api 读到的就是当前场景的 config。
    """
    mod = types.ModuleType(name)
    if package:
        mod.__package__ = package
    sys.modules[name] = mod
    exec(compile(src, name, "exec"), mod.__dict__)
    return mod


def git_show(ref: str, rel_path: str) -> str:
    proc = subprocess.run(["git", "show", f"{ref}:{rel_path}"], cwd=str(ROOT),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(
            f"git show {ref}:{rel_path} 失败（{proc.stderr.strip()[:200]}）—— "
            f"请把 smoke_deploy.py 的 BASELINE_REF 更新到本特性之前的提交")
    return proc.stdout


# ================================================================ 子进程（真实 uvicorn）
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_uvicorn_child(env: dict[str, str], port: int, tag: str) -> subprocess.Popen:
    """按 Dockerfile CMD 的形态起一个真实 uvicorn（不传任何 proxy 参数）。"""
    child = dict(os.environ)
    for key in DEPLOY_ENV_KEYS:
        child.pop(key, None)
    child.update(env)
    child["PYTHONIOENCODING"] = "utf-8"
    child["PYTHONUNBUFFERED"] = "1"   # Windows 上硬杀进程时管道里的日志不能丢
    child["ELDER_DB"] = f"sqlite:///{(TMPDIR / f'child_{tag}.db').as_posix()}"
    child["ELDER_JWT_SECRET"] = "smoke-deploy-child-secret-0123456789abcdef"
    return subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "info"],
        cwd=str(SERVER), env=child, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")


# ================================================================ 主流程
def main() -> int:
    guard_or_die(PORT, "smoke_deploy.py")

    print("=" * 64)
    print("部署模式冒烟：tunnel / direct / auto + 双域名 WS + 启动自检")
    print(f"端口 {PORT}（tools/smoke_ports.py 登记）；进程内起 uvicorn 后台线程")
    print("=" * 64)

    install_probe_route()
    start_server()
    if not wait_ready():
        print("[fatal] 服务未就绪")
        return 1
    print(f"[boot] ready（auto 模式，无显式地址）\n")

    # 兜底关服：断言阶段万一卡死，300 秒后强制退出，保证进程不挂住
    watchdog = threading.Timer(300.0, lambda: (
        print("[watchdog] 300 秒兜底触发：强制关服"),
        setattr(server, "should_exit", True)))
    watchdog.daemon = True
    watchdog.start()

    try:
        # ---------------------------------------------------------- A. auto 回归
        group("A. auto 模式（默认）：与今天逐字一致")
        expect_http("A1 auto 无显式地址（真实 HTTP，无 XFP）", {},
                    f"ws://127.0.0.1:{PORT}{WS_PATH}",
                    f"http://127.0.0.1:{PORT}{OTA_PATH}")
        expect_call("A2 auto 无显式地址（合成 https 请求）", {},
                    fake_request("https", "voice.250886.xyz"),
                    f"wss://voice.250886.xyz{WS_PATH}",
                    f"https://voice.250886.xyz{OTA_PATH}")
        expect_http("A3 auto + 旧变量 https", {
            "ELDER_VOICE_PUBLIC_BASE": "https://voice.250886.xyz"},
            f"wss://voice.250886.xyz{WS_PATH}",
            f"https://voice.250886.xyz{OTA_PATH}")
        # 旧变量历史上**不校验 scheme**：wss:// 会被原样当 base 用（OTA 段因此
        # 是坏的）。这是今天的真实行为，本轮刻意未改 —— 钉死它，防止有人
        # "顺手"给旧变量加校验，那是静默改存量语义。
        expect_http("A4 auto + 旧变量 wss（历史不校验，逐字保持）", {
            "ELDER_VOICE_PUBLIC_BASE": "wss://voice.250886.xyz"},
            f"wss://voice.250886.xyz{WS_PATH}",
            f"wss://voice.250886.xyz{OTA_PATH}")
        expect_http("A5 auto + 旧变量 http+端口（直暴露形态）", {
            "ELDER_VOICE_PUBLIC_BASE": "http://12.34.56.78:8064"},
            f"ws://12.34.56.78:8064{WS_PATH}",
            f"http://12.34.56.78:8064{OTA_PATH}")
        expect_http("A6 auto + 新变量 ELDER_PUBLIC_BASE_URL（逃生舱不变）", {
            "ELDER_PUBLIC_BASE_URL": "https://voice.250886.xyz"},
            f"wss://voice.250886.xyz{WS_PATH}",
            f"https://voice.250886.xyz{OTA_PATH}")

        # ---------------------------------------------------------- B. direct 回归
        group("B. direct 模式：与今天逐字一致（direct = auto 的显式声明版）")
        expect_http("B1 direct 无显式地址", {"ELDER_DEPLOY_MODE": "direct"},
                    f"ws://127.0.0.1:{PORT}{WS_PATH}",
                    f"http://127.0.0.1:{PORT}{OTA_PATH}")
        expect_http("B2 direct + 旧变量 https", {
            "ELDER_DEPLOY_MODE": "direct",
            "ELDER_VOICE_PUBLIC_BASE": "https://voice.250886.xyz"},
            f"wss://voice.250886.xyz{WS_PATH}",
            f"https://voice.250886.xyz{OTA_PATH}")
        expect_http("B3 direct + 旧变量 wss（历史不校验，逐字保持）", {
            "ELDER_DEPLOY_MODE": "direct",
            "ELDER_VOICE_PUBLIC_BASE": "wss://voice.250886.xyz"},
            f"wss://voice.250886.xyz{WS_PATH}",
            f"wss://voice.250886.xyz{OTA_PATH}")
        expect_http("B4 direct + 新变量", {
            "ELDER_DEPLOY_MODE": "direct",
            "ELDER_PUBLIC_BASE_URL": "https://voice.250886.xyz"},
            f"wss://voice.250886.xyz{WS_PATH}",
            f"https://voice.250886.xyz{OTA_PATH}")

        # ---------------------------------------------------------- C. tunnel
        group("C. tunnel 模式：显式对外地址（支持 HTTP/WS 两个域名）")
        expect_http("C1 tunnel + 新 HTTP 变量", {
            "ELDER_DEPLOY_MODE": "tunnel",
            "ELDER_PUBLIC_BASE_URL": "https://voice.250886.xyz"},
            f"wss://voice.250886.xyz{WS_PATH}",
            f"https://voice.250886.xyz{OTA_PATH}")
        expect_http("C2 tunnel + HTTP/WS 双域名（Cloudflare 多 hostname）", {
            "ELDER_DEPLOY_MODE": "tunnel",
            "ELDER_PUBLIC_BASE_URL": "https://voice.250886.xyz",
            "ELDER_WS_PUBLIC_BASE_URL": "wss://voice-ws.250886.xyz"},
            f"wss://voice-ws.250886.xyz{WS_PATH}",
            f"https://voice.250886.xyz{OTA_PATH}")
        expect_http("C3 tunnel + 旧变量（向后兼容，满足显式地址要求）", {
            "ELDER_DEPLOY_MODE": "tunnel",
            "ELDER_VOICE_PUBLIC_BASE": "https://voice.250886.xyz"},
            f"wss://voice.250886.xyz{WS_PATH}",
            f"https://voice.250886.xyz{OTA_PATH}")
        expect_http("C4 tunnel + 明文 http 地址 -> 下发 ws://", {
            "ELDER_DEPLOY_MODE": "tunnel",
            "ELDER_PUBLIC_BASE_URL": "http://12.34.56.78:8064"},
            f"ws://12.34.56.78:8064{WS_PATH}",
            f"http://12.34.56.78:8064{OTA_PATH}")
        expect_http("C5 tunnel + 新变量带尾斜杠（归一化）", {
            "ELDER_DEPLOY_MODE": "tunnel",
            "ELDER_PUBLIC_BASE_URL": "https://voice.250886.xyz/"},
            f"wss://voice.250886.xyz{WS_PATH}",
            f"https://voice.250886.xyz{OTA_PATH}")
        expect_http("C6 tunnel 模式名大小写不敏感（TUNNEL）", {
            "ELDER_DEPLOY_MODE": "TUNNEL",
            "ELDER_PUBLIC_BASE_URL": "https://voice.250886.xyz"},
            f"wss://voice.250886.xyz{WS_PATH}",
            f"https://voice.250886.xyz{OTA_PATH}")

        # ---------------------------------------------------------- D. 启动失败
        group("D. 启动期校验：配置矛盾必须启动失败（不能静默降级）")
        expect_boot_fail("D1 tunnel 无任何对外地址", {
            "ELDER_DEPLOY_MODE": "tunnel"},
            ("【启动失败】", "ELDER_DEPLOY_MODE=tunnel", "ELDER_PUBLIC_BASE_URL"))
        expect_boot_fail("D2 只配 WS 域名不配 HTTP 地址（auto 模式同样拦）", {
            "ELDER_WS_PUBLIC_BASE_URL": "wss://voice-ws.250886.xyz"},
            ("【启动失败】", "ELDER_WS_PUBLIC_BASE_URL", "ELDER_PUBLIC_BASE_URL"))
        expect_boot_fail("D3 模式名写错", {
            "ELDER_DEPLOY_MODE": "cloudflare"},
            ("【启动失败】", "ELDER_DEPLOY_MODE", "auto / direct / tunnel"))
        expect_boot_fail("D4 ELDER_PUBLIC_BASE_URL scheme 错（ftp）", {
            "ELDER_PUBLIC_BASE_URL": "ftp://voice.250886.xyz"},
            ("【启动失败】", "ELDER_PUBLIC_BASE_URL", "ftp://voice.250886.xyz"))
        expect_boot_fail("D5 ELDER_PUBLIC_BASE_URL 缺 scheme", {
            "ELDER_PUBLIC_BASE_URL": "voice.250886.xyz"},
            ("【启动失败】", "ELDER_PUBLIC_BASE_URL"))
        expect_boot_fail("D6 ELDER_WS_PUBLIC_BASE_URL scheme 错（https）", {
            "ELDER_PUBLIC_BASE_URL": "https://voice.250886.xyz",
            "ELDER_WS_PUBLIC_BASE_URL": "https://voice-ws.250886.xyz"},
            ("【启动失败】", "ELDER_WS_PUBLIC_BASE_URL", "ws"))
        expect_boot_fail("D7 tunnel 下旧变量 scheme 错（wss 当 HTTP 用）", {
            "ELDER_DEPLOY_MODE": "tunnel",
            "ELDER_VOICE_PUBLIC_BASE": "wss://voice.250886.xyz"},
            ("【启动失败】", "ELDER_VOICE_PUBLIC_BASE", "http(s)"))
        # 失败之后必须还能正常启动：证明上面的 SystemExit 没把进程状态弄脏
        expect_http("D8 连续失败后恢复正常配置仍能启动", {
            "ELDER_DEPLOY_MODE": "tunnel",
            "ELDER_PUBLIC_BASE_URL": "https://voice.250886.xyz"},
            f"wss://voice.250886.xyz{WS_PATH}",
            f"https://voice.250886.xyz{OTA_PATH}")

        # ---------------------------------------------------------- E. 框架层
        group("E. uvicorn 框架默认层现状钉住（回环对端 + XFP）")
        # uvicorn 默认 proxy_headers=True 且只信任 127.0.0.1：本机回环客户端
        # 带的 X-Forwarded-Proto 会被采纳。生产里对端是 Docker 网桥网关、
        # 不在信任名单，同样一个头会被忽略 —— 这条断言把"采纳"那一侧钉住，
        # 与 A1（不带头 => ws://）合起来就是完整的机制画像。
        reload_config_and_router({})
        got = probe({"X-Forwarded-Proto": "https"})
        check("E1 回环对端 + X-Forwarded-Proto:https ⇒ uvicorn 采纳（wss://）",
              got["ws_url"] == f"wss://127.0.0.1:{PORT}{WS_PATH}",
              f"实际 {got['ws_url']}（若 uvicorn 改了默认值，此处会红，"
              f"请同步 review DEPLOY.md §4.2 的机制描述）")

        # ---------------------------------------------------------- F. 真实子进程
        group("F. 真实 uvicorn 子进程（Dockerfile CMD 形态，无 proxy 参数）")
        port_fail = _free_port()
        proc = run_uvicorn_child({"ELDER_DEPLOY_MODE": "tunnel"}, port_fail, "fail")
        try:
            out, _ = proc.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        check("F1 tunnel 缺地址：uvicorn 子进程非零退出",
              proc.returncode not in (0, None), f"returncode={proc.returncode}")
        check("F1 子进程 stderr 点名 ELDER_PUBLIC_BASE_URL 与【启动失败】",
              "ELDER_PUBLIC_BASE_URL" in out and "【启动失败】" in out,
              (out.strip().splitlines() or ["(无输出)"])[0])

        port_ok = _free_port()
        proc = run_uvicorn_child({
            "ELDER_DEPLOY_MODE": "tunnel",
            "ELDER_PUBLIC_BASE_URL": "https://voice.250886.xyz",
            "ELDER_WS_PUBLIC_BASE_URL": "wss://voice-ws.250886.xyz"},
            port_ok, "ok")
        healthy = False
        deadline = time.time() + 90
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port_ok}/api/health", timeout=2) as resp:
                    if resp.status == 200:
                        healthy = True
                        break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        if healthy:
            check("F2 tunnel 配齐：真实 uvicorn 起来且 /api/health 200", True)
            proc.terminate()
            try:
                out, _ = proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
            check("F2 启动日志含部署摘要（设备心跳将拿到…）",
                  "设备心跳将拿到" in out and "wss://voice-ws.250886.xyz" in out,
                  next((ln for ln in out.splitlines() if "设备心跳" in ln), "(未找到)"))
        else:
            proc.kill()
            out, _ = proc.communicate()
            check("F2 tunnel 配齐：真实 uvicorn 起来且 /api/health 200", False,
                  (out or "")[-800:])

        # ---------------------------------------------------------- G. 反向验证
        group("G. 反向验证：把实现改坏，断言必须转红")
        cfg_src = (SERVER / "app" / "config.py").read_text(encoding="utf-8")
        g1_old = '''    if DEPLOY_MODE == "tunnel":
        if not PUBLIC_BASE_URL:'''
        g1_new = '''    if False:  # 变异 G1：反向验证临时去掉 tunnel 启动校验
        if not PUBLIC_BASE_URL:'''
        if g1_old not in cfg_src:
            check("G1 锚点存在（config.py 的 tunnel 校验）", False,
                  "锚点没找到 —— smoke_deploy.py 需随 config.py 更新")
        else:
            set_deploy_env({"ELDER_DEPLOY_MODE": "tunnel"})
            mutated_raised = False
            try:
                exec_fresh_module("_mut_config",
                                  cfg_src.replace(g1_old, g1_new, 1))
            except SystemExit:
                mutated_raised = True
            check("G1 去掉 tunnel 校验后缺地址不再报错（证明校验有负载）",
                  not mutated_raised,
                  "变异后仍然启动失败 —— 说明拦住它的不是这段校验，"
                  "D1 的断言可能没打到点上")
            err = reload_config_and_router({"ELDER_DEPLOY_MODE": "tunnel"})
            check("G1 复原（用真实源码重载）后同一环境重新启动失败",
                  err is not None and "ELDER_PUBLIC_BASE_URL" in err,
                  (err or "").splitlines()[0] if err else "未报错")

        da_src = (SERVER / "app" / "routers" / "device_api.py").read_text(
            encoding="utf-8")
        g2_old = 'base.replace("https://", "wss://").replace("http://", "ws://")'
        g2_new = ('base.replace("https://", "ws://")'
                  '.replace("http://", "ws://")  # 变异 G2')
        if g2_old not in da_src:
            check("G2 锚点存在（device_api.py 的 WS scheme 推导）", False,
                  "锚点没找到 —— smoke_deploy.py 需随 device_api.py 更新")
        else:
            reload_config_and_router({
                "ELDER_DEPLOY_MODE": "tunnel",
                "ELDER_PUBLIC_BASE_URL": "https://voice.250886.xyz"})
            mut = exec_fresh_module(
                "_mut_device_api_g2", da_src.replace(g2_old, g2_new, 1),
                package="app.routers")
            req = fake_request("https", "voice.250886.xyz")
            mut_ws, _ = mut._voice_endpoints(req)
            real_ws, _ = device_api_mod._voice_endpoints(req)
            check("G2 变异版推导结果确实是错的（ws:// 而非 wss://）",
                  mut_ws == f"ws://voice.250886.xyz{WS_PATH}",
                  f"变异结果 {mut_ws}")
            check("G2 真实代码给出正确的 wss://",
                  real_ws == f"wss://voice.250886.xyz{WS_PATH}",
                  f"实际 {real_ws}")
            check("G2 若按 C1 的期望断言，变异版必然转红", mut_ws != real_ws)

        g3_old = 'scheme = "wss" if request.url.scheme == "https" else "ws"'
        g3_new = 'scheme = "ws"  # 变异 G3：请求推导恒 ws'
        if g3_old not in da_src:
            check("G3 锚点存在（device_api.py 的请求 scheme 推导）", False,
                  "锚点没找到 —— smoke_deploy.py 需随 device_api.py 更新")
        else:
            reload_config_and_router({})
            mut = exec_fresh_module(
                "_mut_device_api_g3", da_src.replace(g3_old, g3_new, 1),
                package="app.routers")
            req = fake_request("https", "voice.250886.xyz")
            mut_ws, mut_ota = mut._voice_endpoints(req)
            real_ws, real_ota = device_api_mod._voice_endpoints(req)
            check("G3 变异版 https 请求推出 ws://（确实是错的）",
                  mut_ws == f"ws://voice.250886.xyz{WS_PATH}",
                  f"变异结果 {mut_ws}")
            check("G3 真实代码 https 请求推出 wss://",
                  real_ws == f"wss://voice.250886.xyz{WS_PATH}",
                  f"实际 {real_ws}")
            check("G3 若按 A2 的期望断言，变异版必然转红", mut_ws != real_ws)

        # ---------------------------------------------------------- H. 逐字比对
        group(f"H. 与 git 基线（{BASELINE_REF}）逐字比对：auto/direct 行为未变")

        def old_endpoints(env: dict[str, str], request: Request) -> tuple[str, str]:
            """跑一遍**改动前**的代码：旧 config + 旧 device_api（同 env 同请求）。"""
            set_deploy_env(env)
            old_cfg = exec_fresh_module(
                "_old_config", git_show(BASELINE_REF, "server/app/config.py"))
            old_da_src = git_show(BASELINE_REF,
                                  "server/app/routers/device_api.py")
            # 旧 device_api 从 ..config 读 VOICE_PUBLIC_BASE；把相对导入改到
            # 刚 exec 出来的旧 config 上，其它相对导入（models/db/...）保持原样。
            shimmed = old_da_src.replace("from ..config import (",
                                         "from _old_config import (", 1)
            assert "from _old_config import (" in shimmed, "旧源码结构变了？"
            # 旧 device_api 还会 ``from ..membership import quota_snapshot``，
            # 而该模块已随取消会员制改名/删除 —— 基线代码本身不变，只把这一行
            # 指到桩模块。_voice_endpoints 完全不碰 quota_snapshot，
            # 所以桩只要能过 import 即可（下方 H 组断言会验证输出没被桩影响）。
            exec_fresh_module(
                "_old_membership",
                "def quota_snapshot(*a, **k):  # 桩：H 组不调用它\n"
                "    raise AssertionError('H 组不应调用 quota_snapshot')\n")
            shimmed = shimmed.replace(
                "from ..membership import quota_snapshot",
                "from _old_membership import quota_snapshot", 1)
            old_da = exec_fresh_module("_old_device_api", shimmed,
                                       package="app.routers")
            return old_da._voice_endpoints(request)

        h_combos = [
            ("H1 auto 无地址 + http 请求", {}, "http", "127.0.0.1:8122"),
            ("H2 auto 无地址 + https 请求", {}, "https", "voice.250886.xyz"),
            ("H3 auto 旧变量 https", {
                "ELDER_VOICE_PUBLIC_BASE": "https://voice.250886.xyz"},
             "http", "voice.250886.xyz"),
            ("H4 auto 旧变量 wss（历史不校验）", {
                "ELDER_VOICE_PUBLIC_BASE": "wss://voice.250886.xyz"},
             "http", "voice.250886.xyz"),
            ("H5 auto 旧变量 http+端口", {
                "ELDER_VOICE_PUBLIC_BASE": "http://12.34.56.78:8064"},
             "https", "voice.250886.xyz"),
            ("H6 auto 旧变量带尾斜杠", {
                "ELDER_VOICE_PUBLIC_BASE": "https://voice.250886.xyz/"},
             "http", "voice.250886.xyz"),
            ("H7 direct 无地址 + https 请求", {
                "ELDER_DEPLOY_MODE": "direct"}, "https", "voice.250886.xyz"),
            ("H8 direct 旧变量 https", {
                "ELDER_DEPLOY_MODE": "direct",
                "ELDER_VOICE_PUBLIC_BASE": "https://voice.250886.xyz"},
             "http", "voice.250886.xyz"),
        ]
        for name, env, scheme, host in h_combos:
            request = fake_request(scheme, host)
            old = old_endpoints(env, request)
            err = reload_config_and_router(env)
            new = device_api_mod._voice_endpoints(request) if err is None else None
            check(f"{name}：新旧输出逐字一致", new == old and err is None,
                  f"旧 {old} / 新 {new} / 启动 {err or '通过'}")

    finally:
        watchdog.cancel()
        try:
            if server is not None:
                server.should_exit = True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
        shutil.rmtree(TMPDIR, ignore_errors=True)

    passed = sum(1 for _n, ok, _d in results if ok)
    failed = [n for n, ok, _d in results if not ok]
    print("\n" + "=" * 64)
    print(f"共 {len(results)} 条断言：通过 {passed}，失败 {len(failed)}")
    for name in failed:
        print(f"  ✗ {name}")
    print("=" * 64)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
