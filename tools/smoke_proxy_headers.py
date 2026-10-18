"""可信反代冒烟：ProxyHeaders 算法 + 307 Location 的 scheme 推导 + Dockerfile CMD 形态。

用法：
    python tools/smoke_proxy_headers.py [--port 8125]

覆盖（与 2026-10-18 线上事故一一对应）
--------------------------------------
A. **uvicorn ProxyHeadersMiddleware 单元层**（跨版本行为钉住）：
   主路径（对端可信取第一个不可信地址 / 对端不可信全忽略 / `*` 取最左段 /
   XFP 改写 scheme）在 0.23.2 与 ≥0.30 上实测一致；唯一分叉点是
   「XFF 全段可信」（≤0.30 置 (None, 0)，≥0.31 置最左段），A5 按版本门控。
B. **两层算法一致性**：uvicorn 的 ``get_trusted_client_host`` 与
   ``app/ratelimit.py::client_ip`` 在主路径上必须给出同一个真实客户端 IP
   —— 同一个 ``ELDER_TRUSTED_PROXY_IPS`` 旋钮同时驱动它们，算法分叉会让
   「限速记在假 IP 上」这类问题无声发生。
C. **进程内真实 uvicorn**：带 ``--proxy-headers --forwarded-allow-ips`` 时
   307 Location 是 ``https://``；把 Caddy 挡在信任名单外时复现 ``http://``
   （用户遇到的 Mixed Content）；``proxy_headers=False`` 时必为 ``http://``
   （反向验证，证明断言不是恒真）。
D. **Dockerfile CMD 静态核对**：防有人把 ``--proxy-headers`` 删回去、
   或把 ``--forwarded-allow-ips`` 写死成 ``*``。
E. **ELDER_TRUSTED_PROXY_IPS 贯通**：config -> ratelimit 一层确实读的是它。

⚠️ 为什么进程内起服务（uvicorn 后台线程 + watchdog）：
   后台任务起的服务前台连不上（网络隔离），所以本脚本必须在**单条前台命令**
   内自己起服务、自己断言、自己关服。两个场景（信任/不信任）顺序起停同一个
   端口；watchdog 是兜底：断言阶段万一卡死，240 秒后强制关服。

⚠️ 本脚本不起子进程、不碰数据库写路径，因此**不注册新端口之外的服务**；
   端口 8125 已在 tools/smoke_ports.py 登记。
"""
from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"

sys.path.insert(0, str(ROOT / "tools"))
from smoke_ports import guard_or_die, resolve_port  # noqa: E402

PORT = resolve_port("smoke_proxy_headers")
BASE = f"http://127.0.0.1:{PORT}"

#: 用户线上踩的那个路径：前端相对路径 /api/admin/presets（无尾斜杠）被
#: FastAPI redirect_slashes 改成 307。**不需要登录** —— 重定向发生在路由层，
#: 早于任何鉴权依赖，所以这条路径在空库下也能稳定复现。
REDIRECT_PATH = "/api/admin/presets"

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
TMPDIR = Path(tempfile.mkdtemp(prefix="smoke_proxy_headers_"))
DB_PATH = TMPDIR / "smoke_proxy_headers.db"

os.environ["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["ELDER_JWT_SECRET"] = "smoke-proxy-headers-secret-0123456789abcdef"
os.environ["ELDER_SCHEDULER_INTERVAL"] = "3600"   # 不起后台调度
os.environ["PYTHONIOENCODING"] = "utf-8"
for _k in ("ELDER_TRUSTED_PROXY_IPS",):
    os.environ.pop(_k, None)
for _p in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_p, None)

sys.path.insert(0, str(SERVER))
import uvicorn  # noqa: E402
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # noqa: E402

import app.main as main_mod  # noqa: E402
import app.config as config_mod  # noqa: E402
import app.ratelimit as ratelimit_mod  # noqa: E402

server: uvicorn.Server | None = None
_server_thread: threading.Thread | None = None


def start_server(**cfg_kwargs) -> None:
    """按给定 uvicorn.Config 参数起服务（默认即 Dockerfile 修复后的形态）。"""
    global server, _server_thread
    server = uvicorn.Server(uvicorn.Config(
        main_mod.app, host="127.0.0.1", port=PORT, log_level="warning",
        **cfg_kwargs))
    _server_thread = threading.Thread(target=server.run, daemon=True)
    _server_thread.start()


def wait_ready(timeout: float = 60.0) -> bool:
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
        time.sleep(0.3)
    return False


def stop_server(timeout: float = 30.0) -> None:
    """关服并等到端口真正可再 bind（下一个场景要在同一端口上起来）。"""
    global server, _server_thread
    if server is not None:
        server.should_exit = True
    if _server_thread is not None:
        _server_thread.join(timeout=timeout)
    server = None
    _server_thread = None
    # 端口从 LISTEN 到可再 bind 有极短残留窗口；轮询到空闲为止
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", PORT))
                return
            except OSError:
                time.sleep(0.2)


# ================================================================ A. 单元层
def _run_mw(trusted_hosts: str, client: tuple[str, int], headers: list[tuple[bytes, bytes]]) -> dict:
    """直接调 ProxyHeadersMiddleware，返回被改写后的 scope。

    不走网络，因此可以造出**任意 TCP 对端**（包括 172.18.0.1 这种
    Docker 网桥地址）—— 那正是生产里 Caddy 连进来的形态，而回环探测
    永远造不出来，只能在这一层验证。
    """
    captured: dict = {}

    async def dummy_app(scope, receive, send):  # noqa: ANN001
        captured.update(scope)

    async def receive():  # noqa: ANN001
        return {"type": "http.request"}

    async def send(message):  # noqa: ANN001
        return None

    mw = ProxyHeadersMiddleware(dummy_app, trusted_hosts=trusted_hosts)
    scope = {
        "type": "http", "method": "GET", "path": "/", "raw_path": b"/",
        "headers": headers, "scheme": "http",
        "server": ("127.0.0.1", PORT), "client": client,
        "query_string": b"", "http_version": "1.1", "root_path": "",
    }
    asyncio.run(mw(scope, receive, send))
    return captured


def _uvicorn_flavor() -> str:
    """本环境 uvicorn 在「XFF 全段可信」上的形态。

    - ``"none"``：≤0.30，client 置 ``(None, 0)``（issue #1068 讨论前的形态）
    - ``"leftmost"``：≥0.31，client 置 XFF **最左段**（该段由最外层可信代理
      写入，是真实客户端而非伪造段；同一 issue 的最终修法）
    - ``"unknown"``：版本号解析失败（调用方退化为两种形态都接受）

    边界实证过，不是推理：下载 0.30.0 / 0.31.0 / 0.32.0 / 0.33.0 / 0.34.0
    的 wheel 读 ``proxy_headers.py`` 源码，0.30.0 仍返回 None、0.31.0 起
    返回最左段；本地 0.23.2 与 elder venv 0.54.0 各实跑一遍全量冒烟。
    """
    parts = uvicorn.__version__.split(".")
    try:
        major, minor = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return "unknown"
    return "none" if (major, minor) < (0, 31) else "leftmost"


def group_a() -> None:
    group("A. ProxyHeadersMiddleware 单元层（跨版本行为钉住）")

    # A1 主路径：对端可信（Caddy 在名单里）+ 两跳 XFF，左边还有一段伪造。
    # 从右往左跳过可信的 10.0.0.9，第一个不可信的是 1.2.3.4 —— 真实客户端。
    sc = _run_mw("172.18.0.1, 10.0.0.9", ("172.18.0.1", 5000),
                 [(b"x-forwarded-for", b"9.9.9.9, 1.2.3.4, 10.0.0.9")])
    check("A1 对端可信 + 多跳 XFF（含左段伪造）⇒ 取第一个不可信地址",
          sc["client"] == ("1.2.3.4", 0), f"client={sc.get('client')}")

    # A2 对端不可信：整个头被忽略。这是"默认只信 127.0.0.1"的保护面 ——
    # Docker 网桥对端（172.18.0.x）不在名单时，攻击者写的 XFF 一律无效。
    sc = _run_mw("127.0.0.1", ("172.18.0.1", 5000),
                 [(b"x-forwarded-for", b"9.9.9.9"),
                  (b"x-forwarded-proto", b"https")])
    check("A2 对端不在信任名单 ⇒ XFF/XFP 全部忽略（client 保持对端）",
          sc["client"] == ("172.18.0.1", 5000), f"client={sc.get('client')}")
    check("A2b 对端不可信 ⇒ scheme 保持 http（307 Location 因此是 http://）",
          sc["scheme"] == "http", f"scheme={sc.get('scheme')}")

    # A3 ★ `*` 分支：always_trust 直接取最左段 —— 伪造段被当成真实客户端。
    sc = _run_mw("*", ("172.18.0.1", 5000),
                 [(b"x-forwarded-for", b"9.9.9.9, 1.2.3.4, 10.0.0.9")])
    check("A3 `*` ⇒ always_trust 取 XFF 最左段（攻击者伪造的 9.9.9.9）",
          sc["client"] == ("9.9.9.9", 0), f"client={sc.get('client')}")

    # A4 XFP 被采纳时改写 scheme（这正是 307 Location 变 https 的那一环）
    sc = _run_mw("127.0.0.1", ("127.0.0.1", 5000),
                 [(b"x-forwarded-proto", b"https")])
    check("A4 对端可信 + X-Forwarded-Proto: https ⇒ scheme 改写为 https",
          sc["scheme"] == "https", f"scheme={sc.get('scheme')}")

    # A5 XFF 全段都可信时的框架边界行为 —— **版本分叉点，按版本门控**：
    #   uvicorn ≤0.30：client 置成 (None, 0)
    #   uvicorn ≥0.31：client 置成 XFF **最左段**（最外层可信代理写入的真实
    #     客户端，不是伪造段）
    # 两种形态对本站点都无害：ratelimit.client_ip 对 None 对端归 "unknown"，
    # 对最左段正常限速。但**不容第三方形态**（如 ("", 0)、或不在信任名单里的
    # 地址）—— 那说明 trusted_hosts 解析或 always_trust 判断被改坏，必须停下
    # 来重评对限速键的影响。
    # ⚠️ requirements 允许 uvicorn>=0.30,<1.0 ⇒ CI 装的是最新版（≥0.31 形态），
    # 本地曾是 0.23.2（≤0.30 形态）—— 本断言两条路径都实测绿过。
    flavor = _uvicorn_flavor()
    sc = _run_mw("127.0.0.1", ("127.0.0.1", 5000),
                 [(b"x-forwarded-for", b"127.0.0.1")])
    client = sc.get("client")
    if flavor == "none":
        ok, expect = client == (None, 0), "(None, 0)"
    elif flavor == "leftmost":
        ok, expect = client == ("127.0.0.1", 0), "('127.0.0.1', 0)"
    else:  # 版本号解析不了：两种已知形态都接受，仍不容第三方形态
        ok = client in ((None, 0), ("127.0.0.1", 0))
        expect = "(None, 0) 或 ('127.0.0.1', 0)"
    check(f"A5 XFF 全段可信 ⇒ client={expect}"
          f"（uvicorn {uvicorn.__version__} ⇒ {flavor} 形态）",
          ok, f"client={client}")


# ================================================================ B. 两层一致性
def _fake_request(client: tuple[str, int], xff: str = ""):
    from starlette.requests import Request
    headers = [(b"host", b"voice.250886.xyz")]
    if xff:
        headers.append((b"x-forwarded-for", xff.encode()))
    scope = {
        "type": "http", "method": "GET", "path": "/api/auth/login",
        "raw_path": b"/api/auth/login", "headers": headers,
        "scheme": "http", "server": ("voice.250886.xyz", 443),
        "client": client, "query_string": b"", "http_version": "1.1",
        "root_path": "",
    }
    return Request(scope)


def group_b() -> None:
    group("B. 与 ratelimit.client_ip 的算法一致性（同一个旋钮两层）")

    # B1 主路径一致：同一份信任名单、同一条 XFF，两层必须给出同一个 IP。
    os.environ["ELDER_TRUSTED_PROXY_IPS"] = "172.18.0.1, 10.0.0.9"
    importlib.reload(config_mod)
    importlib.reload(ratelimit_mod)
    xff = "9.9.9.9, 1.2.3.4, 10.0.0.9"
    req = _fake_request(("172.18.0.1", 5000), xff)
    app_ip = ratelimit_mod.client_ip(req)
    sc = _run_mw("172.18.0.1, 10.0.0.9", ("172.18.0.1", 5000),
                 [(b"x-forwarded-for", xff.encode())])
    check("B1 多跳链（含伪造左段）：uvicorn 层与 ratelimit 层同判 1.2.3.4",
          app_ip == "1.2.3.4" and sc["client"][0] == "1.2.3.4",
          f"ratelimit={app_ip} uvicorn={sc.get('client')}")

    # B2 对端不可信：两层都只认对端（XFF 里的内容谁都不采信）
    os.environ["ELDER_TRUSTED_PROXY_IPS"] = ""
    importlib.reload(config_mod)
    importlib.reload(ratelimit_mod)
    req = _fake_request(("172.18.0.1", 5000), "9.9.9.9")
    app_ip = ratelimit_mod.client_ip(req)
    check("B2 未声明可信代理：ratelimit 只认 TCP 对端（XFF 一律不采信）",
          app_ip == "172.18.0.1", f"实际 {app_ip}")

    # B3 ★ 语义分歧：`*` 在 uvicorn 是"信任所有人"（取最左段），在 ratelimit
    # 却因为解析不成 IP 网络而退化成"谁都不信"（只认对端）。两层相反 ⇒
    # 一旦按外部建议写上 --forwarded-allow-ips="*"，uvicorn 会把 scope 的
    # client 改写成攻击者选的 IP，ratelimit 再拿它当限速键 —— 限速器失效。
    os.environ["ELDER_TRUSTED_PROXY_IPS"] = "*"
    importlib.reload(config_mod)
    importlib.reload(ratelimit_mod)
    req = _fake_request(("172.18.0.1", 5000), "9.9.9.9")
    app_ip = ratelimit_mod.client_ip(req)
    sc = _run_mw("*", ("172.18.0.1", 5000), [(b"x-forwarded-for", b"9.9.9.9")])
    check("B3 `*` 两层语义相反：uvicorn 采最左段 9.9.9.9，ratelimit 只认对端",
          sc["client"][0] == "9.9.9.9" and app_ip == "172.18.0.1",
          f"uvicorn={sc.get('client')} ratelimit={app_ip}")

    # 恢复干净状态（E 组会再按需设置）
    os.environ.pop("ELDER_TRUSTED_PROXY_IPS", None)
    importlib.reload(config_mod)
    importlib.reload(ratelimit_mod)


# ================================================================ C. 307 Location
def _get_no_redirect(path: str, extra_headers: dict[str, str]):
    """发请求但**不跟随重定向**，返回 (status, headers)。

    用 http.client 而不是 urllib：urllib 的 HTTPRedirectHandler 会跟 307，
    跟过去是 401/403，看不到 Location 头 —— 而 Location 的 scheme 正是
    本组唯一要断言的东西。

    ⚠️ 头部名统一转**小写**：h11/uvicorn 发出去的就是小写（HTTP/1.1 语义），
    按 "Location" 取会拿到空值 —— 本脚本第一版就是这么红的，表现是
    「307 回来了但 Location 是空的」，极易误判成服务端没发头。
    """
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    try:
        conn.request("GET", path, headers=extra_headers or {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}
    finally:
        conn.close()


def group_c() -> None:
    group("C. 进程内真实 uvicorn：307 Location 的 scheme（Mixed Content 根治）")

    # C1 Dockerfile 修复后的形态：--proxy-headers --forwarded-allow-ips 127.0.0.1
    start_server(proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    if not check("C0a 服务（信任回环）就绪", wait_ready(), "未就绪"):
        stop_server()
        return
    try:
        status, headers = _get_no_redirect(
            REDIRECT_PATH, {"X-Forwarded-Proto": "https"})
        loc = headers.get("location", "")
        check("C1 无尾斜杠路径返回 307", status == 307, f"status={status}")
        check("C1b Location 是 https://（修复后的形态，Mixed Content 消除）",
              loc.startswith("https://"), f"Location={loc}")
        check("C1c Location 指向带尾斜杠的路径",
              loc.endswith(REDIRECT_PATH + "/"), f"Location={loc}")
    finally:
        stop_server()

    # C2 复现事故：Caddy 从 Docker 网桥连入（172.18.0.x），不在默认信任
    # 名单里 ⇒ XFP 被忽略 ⇒ Location 是 http:// ⇒ HTTPS 页面被浏览器拦截。
    # 注意这里用 192.0.2.1 这个文档专用地址代表"没被列进名单的上一级代理"。
    start_server(proxy_headers=True, forwarded_allow_ips="192.0.2.1")
    if not check("C0b 服务（不信任回环）就绪", wait_ready(), "未就绪"):
        stop_server()
        return
    try:
        status, headers = _get_no_redirect(
            REDIRECT_PATH, {"X-Forwarded-Proto": "https"})
        loc = headers.get("location", "")
        check("C2 上一级代理不在名单 ⇒ Location 退回 http://（事故复现）",
              status == 307 and loc.startswith("http://"), f"status={status} Location={loc}")
    finally:
        stop_server()

    # C3 反向验证：把 proxy_headers 整个关掉，同样请求必须也是 http:// ——
    # 证明 C1b 的 https 不是"本来就成立"的恒真断言。
    start_server(proxy_headers=False)
    if not check("C0c 服务（proxy_headers=False）就绪", wait_ready(), "未就绪"):
        stop_server()
        return
    try:
        status, headers = _get_no_redirect(
            REDIRECT_PATH, {"X-Forwarded-Proto": "https"})
        loc = headers.get("location", "")
        check("C3 关掉 proxy_headers ⇒ Location 必为 http://（证明 C1 有负载）",
              status == 307 and loc.startswith("http://"), f"status={status} Location={loc}")
    finally:
        stop_server()

    # C4 `*` 也能让 Location 变 https —— 外部 AI 的方案并非无效，只是不该用
    # （代价见 A3/B3）。断言它"能修"，让读者在一条输出里看全三个形态。
    start_server(proxy_headers=True, forwarded_allow_ips="*")
    if not check("C0d 服务（forwarded-allow-ips=*）就绪", wait_ready(), "未就绪"):
        stop_server()
        return
    try:
        status, headers = _get_no_redirect(
            REDIRECT_PATH, {"X-Forwarded-Proto": "https"})
        loc = headers.get("location", "")
        check("C4 `*` 也能修 307（但 A3/B3 证明它会拆掉限速器）",
              status == 307 and loc.startswith("https://"), f"status={status} Location={loc}")
    finally:
        stop_server()


# ================================================================ D. Dockerfile
def group_d() -> None:
    group("D. Dockerfile CMD 静态核对（防回退 / 防写死通配）")
    dockerfile = (SERVER / "Dockerfile").read_text(encoding="utf-8")
    # 只认**顶层** CMD 指令（行首无缩进）。HEALTHCHECK 里也有一行 CMD
    # （健康检查探针），按"行首 CMD"过滤才不会把它算进来 —— 否则本断言
    # 永远红，而红的原因与要守的东西无关。
    cmd_lines = [ln.strip() for ln in dockerfile.splitlines()
                 if ln.startswith("CMD ")]
    cmd = "\n".join(cmd_lines)
    check("D1 Dockerfile 有且仅有一条顶层 CMD", len(cmd_lines) == 1, f"{len(cmd_lines)} 条")
    check("D2 CMD 带 --proxy-headers（否则 XFP 一律不被采纳）",
          "--proxy-headers" in cmd, cmd[:120])
    check("D3 CMD 带 --forwarded-allow-ips 且默认值是 127.0.0.1",
          "--forwarded-allow-ips ${ELDER_TRUSTED_PROXY_IPS:-127.0.0.1}" in cmd,
          cmd[:160])
    check("D4 CMD 绝不写死 `*`（always_trust 取 XFF 最左段，可伪造）",
          'forwarded-allow-ips "*"' not in cmd
          and 'forwarded-allow-ips=*' not in cmd
          and 'forwarded-allow-ips ${ELDER_TRUSTED_PROXY_IPS:-*}' not in cmd,
          cmd[:160])


# ================================================================ E. 环境变量贯通
def group_e() -> None:
    group("E. ELDER_TRUSTED_PROXY_IPS 贯通 config -> ratelimit")
    check("E1 config 默认值为空（= 谁都不信，直连部署的默认形态）",
          os.environ.get("ELDER_TRUSTED_PROXY_IPS") is None
          and config_mod.TRUSTED_PROXY_IPS == "",
          f"TRUSTED_PROXY_IPS={config_mod.TRUSTED_PROXY_IPS!r}")
    os.environ["ELDER_TRUSTED_PROXY_IPS"] = "172.18.0.0/16"
    importlib.reload(config_mod)
    importlib.reload(ratelimit_mod)
    check("E2 设成 CIDR 后 ratelimit 的信任表随之更新",
          config_mod.TRUSTED_PROXY_IPS == "172.18.0.0/16"
          and len(ratelimit_mod._TRUSTED) == 1,
          f"_TRUSTED={ratelimit_mod._TRUSTED}")
    req = _fake_request(("172.18.0.7", 5000), "1.2.3.4")
    check("E3 网桥网关在网段内 ⇒ 解析 XFF 拿到真实客户端",
          ratelimit_mod.client_ip(req) == "1.2.3.4",
          ratelimit_mod.client_ip(req))
    os.environ.pop("ELDER_TRUSTED_PROXY_IPS", None)
    importlib.reload(config_mod)
    importlib.reload(ratelimit_mod)


# ================================================================ 主流程
def main() -> int:
    guard_or_die(PORT, "smoke_proxy_headers.py")

    print("=" * 64)
    print("可信反代冒烟：ProxyHeaders 算法 + 307 Location scheme + Dockerfile CMD")
    print(f"端口 {PORT}（tools/smoke_ports.py 登记）；进程内起 uvicorn 后台线程")
    print("=" * 64)

    # 兜底关服：断言阶段万一卡死，240 秒后强制退出，保证进程不挂住
    watchdog = threading.Timer(240.0, lambda: (
        print("[watchdog] 240 秒兜底触发：强制关服"),
        setattr(server, "should_exit", True)))
    watchdog.daemon = True
    watchdog.start()

    try:
        group_a()
        group_b()
        group_c()
        group_d()
        group_e()
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
