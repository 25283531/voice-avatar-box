"""邮箱服务冒烟测试（SMTP 配置 / 邮箱找回密码 / 邮箱提交问题）。

用法：
    python tools/smoke_mail.py [--port 8105]

覆盖：
    A. 平台 SMTP 配置（管理端）：读取 / 修改 / 测试发信 / 伪装发件箱 / 非管理员 403 /
       **密码不明文回显**
    B. 邮箱找回密码：未配置邮件时优雅降级 503（且未注册邮箱响应一致）/
       已注册与未注册邮箱**响应完全一致**（账号枚举防护）/ 冷却 429 /
       错误验证码计数 / 超次作废 / 正确验证码重置 / 一次性 /
       重置后原密码失效新密码可登录
    C. 工单：匿名提交 / 登录提交自动关联用户 / 新工单通知邮件 /
       我的工单 / 管理端列表与回复 / 回复通知邮件 / 非管理员 403
    D. 进程内：真实 SMTP 连不通时 send_mail 返回失败而不抛异常（不发信也不 500）
    D'. **反向验证**：SMTP 服务器「只 accept 不回话」（连接挂起）时，
       send_mail 必须在超时窗口内返回 MailResult(ok=False) 而不是挂死；
       以及超时配置的环境变量覆盖与非法值回落
    E. 过期验证码清理：过期/已消费/超次删、缓冲期内与有效期内不误删

SMTP 伪装层
-----------
本机连不通外网 SMTP，所以服务端以 ``ELDER_MAIL_FAKE=1`` 启动：邮件不进网络，
只写进内存 OUTBOX；测试通过 ``GET /api/admin/mail/outbox``（仅伪装模式可用）
把验证码与通知邮件内容读出来做断言。
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable

# 默认端口统一由 tools/smoke_ports.py 托管（唯一真源），各脚本不再各写一份 ——
# 早先 smoke_models/smoke_mail 都是 8096，并行跑必然撞车。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_ports import (  # noqa: E402
    detect_addr_in_use,
    guard_or_die,
    resolve_port,
)

# 默认文件名带 PID：多人/CI 并行跑时固定文件名会互相抢同一个文件，
# Windows 上直接 PermissionError（另一个进程正占用）。要固定路径就用
# SMOKE_DB_DIR 覆盖目录，或 SMOKE_MAIL_DB 指定完整路径。
DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
DB_PATH = Path(os.environ.get("SMOKE_MAIL_DB",
                              str(DB_DIR / f"elder_smoke_mail_{os.getpid()}.db")))
if DB_PATH.exists():
    DB_PATH.unlink()

PORT = resolve_port("smoke_mail")
BASE = f"http://127.0.0.1:{PORT}"

results: list[tuple[str, bool, str]] = []

# 单个 HTTP 请求的超时（秒）。刻意大于服务端 SMTP 超时（默认 10s），
# 这样「服务端真的卡住了」的表现是本脚本判定 [FAIL]，而不是把服务端的
# 超时掩盖掉。设环境变量 SMOKE_MAIL_HTTP_TIMEOUT 可调。
REQ_TIMEOUT_SEC = float(os.environ.get("SMOKE_MAIL_HTTP_TIMEOUT", "30"))

# 服务端日志落盘路径。⚠️ 这个文件**必须**用（见 main 里的注释）：
# 把子进程 stdout 接成 PIPE 却不消费，是 Windows 上一个必踩的坑 ——
# 匿名管道缓冲区只有 4096 字节，缓冲区一满，服务端进程**写日志时会永久阻塞**，
# 于是表现为「服务端突然不响应任何请求」，客户端超时。
# 而本项目 main.py 开了 logging.INFO，每发一封信、每建一个工单都打日志，
# 跑到几百行就必然撑满 4KB。表象极具有误导性（像服务端逻辑死锁、
# 像 DNS 挂了、像端口冲突），排查成本很高。
SERVER_LOG = DB_PATH.with_name(DB_PATH.stem + "_server.log")

# 测试用的 SMTP 密码（只用于验证「不明文回显」，不会真的拿去登录）
SMTP_PASSWORD = "AbcDefGhIjK"
MASKED = "Ab******jK"

# 测试用 SMTP 主机：**必须是本地地址**。
# 早先这里写的是公网域名 smtp.example.com，测试结果会随「外网 DNS 是否可用」
# 漂移：解析失败是 gaierror，解析成功但连不上是 timeout，两者走不同的失败分支，
# 让冒烟结果不可复现。伪装模式下压根不会建连，这里只是配置里的一个占位值，
# 所以用 127.0.0.1（配合端口 9 = discard，见下）既能表达「一个不可达的地址」，
# 又完全不依赖外网行为。
SMTP_TEST_HOST = "127.0.0.1"
# 端口 9（discard）：标准保留端口，实际不会有人在上面跑 SMTP，
# 连它必然失败 —— 正好用来表达「配了但连不通」这个状态。
SMTP_TEST_PORT = 9


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def req(method: str, path: str, body: dict | None = None, token: str | None = None):
    """发一个 HTTP 请求，返回 (状态码, 解析后的响应体)。

    ⚠️ **顺序陷阱**：``urllib.error.HTTPError`` 是 ``URLError`` 的子类，
    ``TimeoutError`` / ``OSError`` 又是更底层的。所以必须**先判 HTTPError**
    （它带 status_code，是「服务端明确拒绝」），再判网络异常
    （连接被拒 / 读超时 / DNS 失败等）。反过来的话，4xx/5xx 会被误报成
    网络错误，断言全部失真。

    网络异常统一返回 ``(0, {...})``：状态码 0 是个不可能来自 HTTP 的哨兵值，
    调用方现有的 ``code == 200`` / ``code == 403`` 断言会自然判成失败，
    一条条跑完而不是整个脚本崩在这里 —— 崩了就等于丢掉前面所有 PASS 结果，
    而且看不出「卡在第几条」。
    """
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=REQ_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        # 必须在 URLError 之前：http 4xx/5xx 也是 URLError
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # socket.timeout 在 Py3.10+ 就是 TimeoutError；OSError 覆盖
        # ConnectionResetError / ConnectionAbortedError 等「连上了但中途断了」
        reason = getattr(e, "reason", None) or e
        print(f"  [NET-ERR] {method} {path} -> {type(e).__name__}: {reason}")
        return 0, {"error": f"{type(e).__name__}: {reason}"}


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


def find_code(token: str, email: str) -> str:
    """从伪装发件箱里掏出发给该邮箱的 6 位验证码。"""
    _, box = req("GET", "/api/admin/mail/outbox?limit=50", token=token)
    for m in box.get("items", []):
        if email.lower() in [x.lower() for x in m.get("to", [])]:
            hit = re.search(r"验证码是：(\d{6})", m.get("body", ""))
            if hit:
                return hit.group(1)
    return ""


def _start_blackhole_server() -> socket.socket:
    """起一个「只 accept、永远不回话」的 TCP 服务器，返回监听 socket。

    这是模拟「连接建立成功但对端不回应」的最小装置。之所以不用
    ConnectionRefused（连 127.0.0.1:1 那种）来代替：拒绝是**立刻**返回的
    OSError，根本不会走到「等对端数据」那条路径，也就测不到读超时。
    防火墙静默丢包、协议层中转设备故障、连上了但服务僵死 —— 这些真实故障
    全都是「握手成功、之后静默」，只有黑色服务器能稳定复现。

    端口用 0 让内核分配，避免和冒烟服务自己的端口（8105）撞车。
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def acceptor() -> None:
        """接受连接后立刻丢进列表**不关闭也不写入**。

        关键：绝不能 close。close 之后客户端会立刻拿到 EOF
        （ConnectionReset），那又变成「立刻失败」而不是「挂起」了。
        持有引用即可让连接一直处于「已建立但静默」状态。
        """
        held: list[socket.socket] = []
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            held.append(conn)

    threading.Thread(target=acceptor, daemon=True).start()
    return srv


def main() -> int:
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
    env["ELDER_JWT_SECRET"] = "smoke-mail-secret"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"
    env["ELDER_MAIL_FAKE"] = "1"       # 伪装发信：邮件进内存 OUTBOX，不碰网络
    env["PYTHONIOENCODING"] = "utf-8"

    print(f"[boot] starting server on :{PORT} (db: {DB_PATH}, ELDER_MAIL_FAKE=1)")
    # ⚠️ stdout/stderr 必须**落文件**，绝不能接成无人消费的 PIPE。
    # Windows 匿名管道缓冲区只有 4KB：管道写满后，服务端进程写日志会被
    # 永久阻塞，表现成「服务端整体失去响应」，而客户端只看到读超时。
    # 这个坑的真实代价：本文件曾稳定在第 66 条断言后 TimeoutError 崩掉，
    # 且因为 req() 当时不捕获网络异常，前面 66 条 PASS 的结果全丢，
    # 看起来像服务端逻辑有问题，实际只是日志把管道写满了。
    # 见 SERVER_LOG 常量处的说明。
    log_fp = SERVER_LOG.open("w", encoding="utf-8", errors="replace")
    # ---- 三段式端口守卫（QA 第 4 轮「假绿」修复）----
    # 端口被占时 uvicorn 的 socket bind 会失败，**但进程不退出**（活着、只是没在
    # 监听）；而 /api/health **永远返回 200** -> wait_ready 会探到**别人的服务**
    # -> 全部断言打到别人身上、用自己从未 seed 过的 DB。
    # 三段：①事前 is_port_free ②socket 占位缩小竞态窗口 ③起服务后校验 stderr
    # ⚠️ 三段加起来**仍不是绝对保证**（①与真正 bind 之间有不可消除的竞态窗口）。
    #    真正的绝对保证是让服务自证身份（run_id），需改生产代码，留给下一轮。
    guard_or_die(PORT, "smoke_mail.py")

    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT),
         "--log-level", "warning"],
        cwd=str(SERVER), env=env,
        stdout=log_fp, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace",
    )
    try:
        if not wait_ready():
            print(f"[boot] server did not become ready. log tail ({SERVER_LOG}):")
            print(log_fp.seek(0) or log_fp.read()[-3000:])
            return 1
        print("[boot] ready\n")

        # ==================================================== 准备账号
        code, tk = req("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
        check("登录默认管理员", code == 200 and bool(tk.get("access_token")), str(code))
        admin_token = tk.get("access_token", "")

        def reg(name: str, email: str, pwd: str = "test123456") -> str:
            req("POST", "/api/auth/register",
                {"username": name, "password": pwd, "nickname": name, "email": email})
            _, t = req("POST", "/api/auth/login", {"username": name, "password": pwd})
            return t.get("access_token", "")

        u1 = reg("mail1", "mail1@example.com")
        u2 = reg("mail2", "mail2@example.com")
        check("注册并登录 2 个带邮箱的普通用户", bool(u1 and u2), "")

        MAIL1 = "mail1@example.com"
        MAIL2 = "mail2@example.com"
        NOBODY = "nobody@example.com"

        # ==================================================== 1. SMTP 配置（管理端）
        print("\n--- 1. 平台 SMTP 配置 ---")
        code, _ = req("GET", "/api/admin/mail/smtp", token=u1)
        check("非管理员读取 SMTP 配置返回 403", code == 403, f"code={code}")

        code, _ = req("GET", "/api/admin/mail/smtp")
        check("无凭证读取 SMTP 配置返回 401", code in (401, 403), f"code={code}")

        code, cfg0 = req("GET", "/api/admin/mail/smtp", token=admin_token)
        check("管理员读取 SMTP 配置", code == 200, f"code={code}")
        check("初始未配置密码（has_password=False）",
              cfg0.get("has_password") is False and cfg0.get("password") == "",
              str(cfg0.get("password")))
        check("初始默认关闭（enabled=False）", cfg0.get("enabled") is False, "")

        code, cfg1 = req("PATCH", "/api/admin/mail/smtp", {
            "host": SMTP_TEST_HOST, "port": SMTP_TEST_PORT, "username": "noreply@example.com",
            "password": SMTP_PASSWORD, "use_ssl": True, "use_tls": False,
            "from_addr": "noreply@example.com", "from_name": "老人陪伴提醒助手",
            "notify_to": "support@example.com", "enabled": True,
        }, admin_token)
        check("管理员写入 SMTP 配置", code == 200 and cfg1.get("host") == SMTP_TEST_HOST,
              f"code={code}")
        check("回写响应里密码是掩码（不明文回显）",
              cfg1.get("password") == MASKED and cfg1.get("has_password") is True,
              str(cfg1.get("password")))

        code, cfg2 = req("GET", "/api/admin/mail/smtp", token=admin_token)
        raw_json = json.dumps(cfg2, ensure_ascii=False)
        check("读取接口不含密码明文", SMTP_PASSWORD not in raw_json, "")
        check("读取接口返回掩码占位", cfg2.get("password") == MASKED, str(cfg2.get("password")))
        check("其余字段回读一致",
              cfg2.get("port") == SMTP_TEST_PORT and cfg2.get("notify_to") == "support@example.com"
              and cfg2.get("enabled") is True, "")

        code, bad = req("PATCH", "/api/admin/mail/smtp", {"port": 70000}, admin_token)
        check("非法端口返回 422", code == 422, f"code={code}")
        code, bad = req("PATCH", "/api/admin/mail/smtp", {"from_addr": "not-an-email"},
                        admin_token)
        check("非法发件邮箱返回 422", code == 422, f"code={code}")

        # 密码是「只写」字段：不带 password 的 PATCH 不应清空已有密码
        code, cfg3 = req("PATCH", "/api/admin/mail/smtp", {"port": 587}, admin_token)
        check("不带 password 的更新不清空已有密码",
              code == 200 and cfg3.get("has_password") is True, str(cfg3.get("password")))
        req("PATCH", "/api/admin/mail/smtp", {"port": SMTP_TEST_PORT}, admin_token)

        # ---- 测试发信 ----
        code, test = req("POST", "/api/admin/mail/test", {"to": "admin@example.com"},
                         admin_token)
        check("发送测试邮件成功（伪装模式）",
              code == 200 and test.get("ok") is True and test.get("fake") is True,
              str(test))
        check("测试邮件收件人正确", test.get("to") == "admin@example.com", str(test.get("to")))

        code, box = req("GET", "/api/admin/mail/outbox?limit=10", token=admin_token)
        check("伪装发件箱可读（管理员）",
              code == 200 and box.get("count", 0) >= 1, f"count={box.get('count')}")
        check("发件箱里能查到测试邮件",
              any("SMTP 配置测试" in m.get("subject", "") for m in box.get("items", [])), "")
        code, _ = req("GET", "/api/admin/mail/outbox", token=u1)
        check("非管理员读发件箱返回 403", code == 403, f"code={code}")
        req("GET", "/api/admin/mail/outbox?clear=true", token=admin_token)

        code, test2 = req("POST", "/api/admin/mail/test", {}, admin_token)
        check("不填收件人时发给平台通知邮箱",
              code == 200 and test2.get("to") == "support@example.com", str(test2.get("to")))
        req("GET", "/api/admin/mail/outbox?clear=true", token=admin_token)

        # ==================================================== 2. 找回密码
        print("\n--- 2. 邮箱找回密码 ---")
        # ---- 2.1 未配置邮件时的优雅降级（不能 500，且两种邮箱响应一致）----
        req("PATCH", "/api/admin/mail/smtp", {"enabled": False}, admin_token)
        code, off1 = req("POST", "/api/auth/password/reset/request", {"email": MAIL1})
        code2, off2 = req("POST", "/api/auth/password/reset/request", {"email": NOBODY})
        check("未启用邮件时申请验证码返回 503（不是 500）",
              code == 503 and code2 == 503, f"{code}/{code2}")
        check("未启用邮件时两种邮箱响应完全一致",
              json.dumps(off1, sort_keys=True) == json.dumps(off2, sort_keys=True),
              f"{off1} vs {off2}")
        req("PATCH", "/api/admin/mail/smtp", {"enabled": True}, admin_token)

        # ---- 2.2 账号枚举防护：已注册 / 未注册邮箱返回完全相同的响应 ----
        code, r1 = req("POST", "/api/auth/password/reset/request", {"email": MAIL1})
        code2, r2 = req("POST", "/api/auth/password/reset/request", {"email": NOBODY})
        check("已注册邮箱申请验证码返回 200", code == 200, f"code={code}")
        check("未注册邮箱申请验证码也返回 200（不泄露是否注册）", code2 == 200, f"code={code2}")
        check("**已注册与未注册邮箱响应体完全一致**",
              json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True),
              f"{json.dumps(r1, ensure_ascii=False)} vs {json.dumps(r2, ensure_ascii=False)}")
        check("响应体不携带任何账号信息",
              set(r1.keys()) == {"ok", "message", "cooldown_sec", "expires_in_sec"},
              str(list(r1.keys())))

        # ---- 2.3 冷却 ----
        code3, cd1 = req("POST", "/api/auth/password/reset/request", {"email": MAIL1})
        code4, cd2 = req("POST", "/api/auth/password/reset/request", {"email": NOBODY})
        check("60 秒内重复申请被拒（429）", code3 == 429 and code4 == 429,
              f"{code3}/{code4}")
        check("未注册邮箱同样受冷却约束（否则仍是枚举侧信道）", code4 == 429, f"code={code4}")

        # ---- 2.4 验证码 ----
        code1 = find_code(admin_token, MAIL1)
        check("邮件里能取到 6 位验证码", len(code1) == 6 and code1.isdigit(), code1)
        _, box = req("GET", "/api/admin/mail/outbox?limit=10", token=admin_token)
        check("未注册邮箱不会收到邮件",
              not any(NOBODY in [x.lower() for x in m.get("to", [])]
                      for m in box.get("items", [])), "")

        wrong = "000000" if code1 != "000000" else "111111"
        # 首次错误：已注册邮箱与未注册邮箱各错一次，两者响应必须一模一样
        # （attempts_left 也要相同 —— 未注册邮箱同样会建行、同样计数）
        code, e1 = req("POST", "/api/auth/password/reset/verify",
                       {"email": MAIL1, "code": wrong, "new_password": "newpass123"})
        check("错误验证码被拒（400）", code == 400, f"code={code}")
        check("错误验证码返回剩余次数（5->4）",
              e1.get("detail", {}).get("attempts_left") == 4, str(e1))

        code, e3 = req("POST", "/api/auth/password/reset/verify",
                       {"email": NOBODY, "code": "999999", "new_password": "newpass123"})
        check("未注册邮箱的错误验证码响应与已注册邮箱完全一致",
              json.dumps(e1.get("detail"), sort_keys=True)
              == json.dumps(e3.get("detail"), sort_keys=True),
              f"{e1.get('detail')} vs {e3.get('detail')}")

        code, e2 = req("POST", "/api/auth/password/reset/verify",
                       {"email": MAIL1, "code": wrong, "new_password": "newpass123"})
        check("再次错误继续计数（4->3）",
              e2.get("detail", {}).get("attempts_left") == 3, str(e2))

        code, e4 = req("POST", "/api/auth/password/reset/verify",
                       {"email": MAIL1, "code": code1[:5] + "0" if code1[-1] != "0"
                        else code1[:5] + "1", "new_password": "short"})
        check("新密码不足 6 位返回 422", code == 422, f"code={code}")

        code, ok1 = req("POST", "/api/auth/password/reset/verify",
                        {"email": MAIL1, "code": code1, "new_password": "newpass123"})
        check("正确验证码重置密码成功", code == 200 and ok1.get("ok") is True, str(ok1))
        check("返回被重置的账号名", ok1.get("username") == "mail1", str(ok1.get("username")))

        code, again = req("POST", "/api/auth/password/reset/verify",
                          {"email": MAIL1, "code": code1, "new_password": "another123"})
        check("验证码一次性（重复使用被拒）", code == 400, f"code={code}")

        code, _ = req("POST", "/api/auth/login", {"username": "mail1", "password": "test123456"})
        check("重置后原密码失效", code == 401, f"code={code}")
        code, lg = req("POST", "/api/auth/login", {"username": "mail1", "password": "newpass123"})
        check("重置后新密码可登录", code == 200 and bool(lg.get("access_token")), f"code={code}")

        # ---- 2.5 尝试次数上限：连续错 5 次后作废 ----
        _, r3 = req("POST", "/api/auth/password/reset/request", {"email": MAIL2})
        code2 = find_code(admin_token, MAIL2)
        check("第二个邮箱也收到验证码", len(code2) == 6, code2)
        lefts = []
        for _ in range(5):
            _, er = req("POST", "/api/auth/password/reset/verify",
                        {"email": MAIL2, "code": "123456" if code2 != "123456" else "654321",
                         "new_password": "newpass123"})
            lefts.append(er.get("detail", {}).get("attempts_left"))
        check("连续错误 5 次剩余次数递减到 0", lefts == [4, 3, 2, 1, 0], str(lefts))
        code, dead = req("POST", "/api/auth/password/reset/verify",
                         {"email": MAIL2, "code": code2, "new_password": "newpass123"})
        check("超次后即使填正确验证码也作废", code == 400, f"code={code}")
        code, _ = req("POST", "/api/auth/login", {"username": "mail2", "password": "test123456"})
        check("超次作废后原密码仍然有效（未被动过）", code == 200, f"code={code}")

        # ==================================================== 3. 工单
        print("\n--- 3. 邮箱提交问题（工单）---")
        req("GET", "/api/admin/mail/outbox?clear=true", token=admin_token)

        code, t_anon = req("POST", "/api/support/tickets", {
            "email": "anon@example.com", "title": "设备连不上 WiFi",
            "content": "换了路由器之后设备一直离线", "category": "device",
            "device_id": "aabbccddeeff",
        })
        check("匿名提交工单成功（不需要登录）",
              code == 201 and t_anon.get("user_id") is None, f"code={code}")
        check("工单分类与中文文案", t_anon.get("category") == "device"
              and t_anon.get("category_text") == "设备故障", str(t_anon.get("category_text")))
        check("新工单状态为待处理", t_anon.get("status") == "open", str(t_anon.get("status")))

        _, box = req("GET", "/api/admin/mail/outbox?limit=10", token=admin_token)
        notify = [m for m in box.get("items", [])
                  if "新工单" in m.get("subject", "")]
        check("新工单给平台邮箱发了通知邮件",
              bool(notify) and notify[0].get("to") == ["support@example.com"],
              str(notify[0].get("to")) if notify else "无")
        check("通知邮件正文含标题与联系邮箱",
              bool(notify) and "设备连不上 WiFi" in notify[0].get("body", "")
              and "anon@example.com" in notify[0].get("body", ""), "")

        code, t_user = req("POST", "/api/support/tickets", {
            "email": MAIL1, "title": "会员支付后没到账",
            "content": "昨天转了 12.34 元，到现在还没开通", "category": "payment",
        }, u1)
        check("登录提交工单自动关联用户",
              code == 201 and t_user.get("user_id") is not None
              and t_user.get("username") == "mail1", str(t_user.get("username")))

        code, t_other = req("POST", "/api/support/tickets", {
            "email": MAIL1, "title": "随便问问", "content": "这个分类我不填",
            "category": "not-a-category",
        }, u1)
        check("未知分类回落到 other",
              code == 201 and t_other.get("category") == "other"
              and t_other.get("category_text") == "其它", str(t_other.get("category")))

        code, bad_t = req("POST", "/api/support/tickets",
                          {"email": "bad-email", "title": "x", "content": "y"})
        check("邮箱格式非法返回 422", code == 422, f"code={code}")

        code, mine = req("GET", "/api/support/tickets/mine", token=u1)
        check("我的工单列表（登录后）",
              code == 200 and len(mine) == 2
              and all(t["user_id"] for t in mine), f"{len(mine) if isinstance(mine, list) else 0} 条")
        code, _ = req("GET", "/api/support/tickets/mine")
        check("未登录看我的工单返回 401", code in (401, 403), f"code={code}")

        # ---- 管理端 ----
        code, _ = req("GET", "/api/admin/support/tickets", token=u1)
        check("非管理员查看全部工单返回 403", code == 403, f"code={code}")

        code, all_t = req("GET", "/api/admin/support/tickets", token=admin_token)
        check("管理端工单列表含匿名与登录工单",
              code == 200 and len(all_t) == 3
              and any(t["user_id"] is None for t in all_t)
              and any(t["user_id"] is not None for t in all_t),
              f"{len(all_t) if isinstance(all_t, list) else 0} 条")

        code, opened = req("GET", "/api/admin/support/tickets?status=open", token=admin_token)
        check("按状态过滤工单",
              code == 200 and len(opened) == 3 and all(t["status"] == "open" for t in opened),
              f"{len(opened) if isinstance(opened, list) else 0} 条")

        code, one = req("GET", f"/api/admin/support/tickets/{t_anon.get('id')}",
                        token=admin_token)
        check("管理端按 id 查工单", code == 200 and one.get("id") == t_anon.get("id"),
              f"code={code}")

        req("GET", "/api/admin/mail/outbox?clear=true", token=admin_token)
        code, patched = req("PATCH", f"/api/admin/support/tickets/{t_anon.get('id')}", {
            "admin_reply": "已收到，请确认路由器是 2.4G 频段",
        }, admin_token)
        check("管理员回复工单", code == 200
              and patched.get("admin_reply") == "已收到，请确认路由器是 2.4G 频段", f"code={code}")
        check("写了回复自动推进到处理中",
              patched.get("status") == "processing"
              and patched.get("status_text") == "处理中", str(patched.get("status")))
        check("回复记录管理员与回复时间",
              patched.get("admin_id") is not None and patched.get("replied_at") is not None, "")

        _, box = req("GET", "/api/admin/mail/outbox?limit=10", token=admin_token)
        reply_mail = [m for m in box.get("items", []) if "工单回复" in m.get("subject", "")]
        check("回复时给提交者发邮件通知",
              bool(reply_mail) and reply_mail[0].get("to") == ["anon@example.com"],
              str(reply_mail[0].get("to")) if reply_mail else "无")

        code, closed = req("PATCH", f"/api/admin/support/tickets/{t_anon.get('id')}",
                           {"status": "resolved"}, admin_token)
        check("管理员改状态为已解决",
              code == 200 and closed.get("status") == "resolved", str(closed.get("status")))
        code, bad_st = req("PATCH", f"/api/admin/support/tickets/{t_anon.get('id')}",
                           {"status": "whatever"}, admin_token)
        check("非法状态返回 400", code == 400, f"code={code}")
        code, _ = req("PATCH", "/api/admin/support/tickets/999999",
                      {"status": "resolved"}, admin_token)
        check("不存在的工单返回 404", code == 404, f"code={code}")

        # ---- 3.1 没有通知邮箱时提交工单不能失败 ----
        saved = cfg2.get("notify_to", "")
        req("PATCH", "/api/admin/mail/smtp", {"notify_to": ""}, admin_token)
        # from_addr 还在，通知会发给 from_addr；这里连 from_addr / username 一起清空，
        # 模拟「平台压根没配收件邮箱」的最坏情况
        req("PATCH", "/api/admin/mail/smtp", {"from_addr": "", "username": ""}, admin_token)
        code, t_no_mail = req("POST", "/api/support/tickets", {
            "email": "x@example.com", "title": "没邮箱也能提", "content": "不应 500",
        })
        check("无通知邮箱时工单仍能提交（邮件失败不影响落库）",
              code == 201 and t_no_mail.get("id") is not None, f"code={code}")
        req("PATCH", "/api/admin/mail/smtp", {
            "from_addr": "noreply@example.com", "username": "noreply@example.com",
            "notify_to": saved,
        }, admin_token)

        # ==================================================== 4. 进程内：真实 SMTP 失败
        print("\n--- 4. 真实发送失败不抛异常（进程内）---")
        os.environ["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
        sys.path.insert(0, str(SERVER))
        from app import mailer                                   # noqa: E402
        from app.db import SessionLocal                          # noqa: E402

        # 关掉伪装模式，指向一个必然连不通的地址，验证 send_mail 返回失败而不是抛异常
        os.environ["ELDER_MAIL_FAKE"] = "0"
        with SessionLocal() as s:
            cfg = mailer.get_smtp_config(s)
            cfg.enabled = True
            cfg.host = "127.0.0.1"
            cfg.port = 1                      # 必然 ConnectionRefused
            cfg.username = ""
            cfg.password = ""
            cfg.use_ssl = False
            cfg.use_tls = False
            cfg.from_addr = "noreply@example.com"
            s.commit()
            try:
                res = mailer.send_mail(s, "someone@example.com", "测试", "正文")
                check("真实 SMTP 连不通时返回失败而不抛异常",
                      res.ok is False and bool(res.error), res.error[:60])
            except Exception as exc:                             # noqa: BLE001
                check("真实 SMTP 连不通时返回失败而不抛异常", False, f"抛出了 {type(exc).__name__}")
                # 收件人为空时也应安静失败
            res2 = mailer.send_mail(s, "", "测试", "正文")
            check("收件人为空时安静失败（不抛异常）",
                  res2.ok is False and res2.error == "收件人为空", str(res2.error))

            # ---- 4.1 反向验证：连接**挂起**（不是拒绝）时必须超时返回，而不是挂死 ----
            # 为什么必须专门测这个：ConnectionRefused（端口 1）是「立刻失败」，
            # 根本走不到超时逻辑。而真实部署里最危险的形态是
            # 「防火墙静默丢包」/「host 写错到了一个会 accept 但不回话的中转」——
            # TCP 握手成功，之后 readline() 无限期等不到 SMTP 应答。
            # 这种情况没有任何东西会「自然失败」，只有显式 socket 超时能救。
            # 本项目 SMTP 发送是**同步阻塞在 FastAPI 请求线程**里的，
            # 挂住就等于占死一个工作线程，挂满几个整个服务对前端就不可用了。
            blackhole = _start_blackhole_server()
            bport = blackhole.getsockname()[1]
            try:
                cfg.host = "127.0.0.1"
                cfg.port = bport
                cfg.use_ssl = False       # 明文：确保卡在「等 220 应答」而非 TLS 握手
                cfg.use_tls = False
                s.commit()
                # 把读超时压到 2 秒，让这条用例在秒级内跑完；
                # 真实部署用的是默认值（10s），行为一致只是更慢。
                os.environ["ELDER_SMTP_READ_TIMEOUT"] = "2"
                os.environ["ELDER_SMTP_CONNECT_TIMEOUT"] = "2"
                t0 = time.monotonic()
                try:
                    res3 = mailer.send_mail(s, "someone@example.com", "挂起测试", "正文")
                    cost = time.monotonic() - t0
                    check("**连接挂起时超时返回失败而不是挂死**",
                          res3.ok is False and bool(res3.error), f"{cost:.2f}s {res3.error[:70]}")
                    check("挂起场景确实被判定为超时（错误信息含「超时」）",
                          "超时" in res3.error, res3.error[:80])
                    check("**挂起场景在超时窗口内就返回了**（没被拖死）",
                          cost < 15.0, f"耗时 {cost:.2f}s（阈值 15s）")
                except Exception as exc:                          # noqa: BLE001
                    check("**连接挂起时超时返回失败而不是挂死**", False,
                          f"抛出了 {type(exc).__name__}: {exc}")
                finally:
                    os.environ.pop("ELDER_SMTP_READ_TIMEOUT", None)
                    os.environ.pop("ELDER_SMTP_CONNECT_TIMEOUT", None)
            finally:
                blackhole.close()

            # 超时配置可被环境变量覆盖（运维可调），且非法值会安全回落
            os.environ["ELDER_SMTP_READ_TIMEOUT"] = "abc"
            check("非法超时配置回落到默认值而不是抛异常",
                  mailer.smtp_read_timeout() == mailer.SMTP_READ_TIMEOUT_SEC,
                  str(mailer.smtp_read_timeout()))
            os.environ["ELDER_SMTP_READ_TIMEOUT"] = "-1"
            check("非正数的超时配置也回落到默认值",
                  mailer.smtp_read_timeout() == mailer.SMTP_READ_TIMEOUT_SEC,
                  str(mailer.smtp_read_timeout()))
            os.environ["ELDER_SMTP_READ_TIMEOUT"] = "3.5"
            check("合法超时配置被正确读取",
                  mailer.smtp_read_timeout() == 3.5, str(mailer.smtp_read_timeout()))
            os.environ.pop("ELDER_SMTP_READ_TIMEOUT", None)
        os.environ["ELDER_MAIL_FAKE"] = "1"

        # 掩码函数：短串与空串都不崩
        check("掩码函数对空串返回空串", mailer.mask_secret("") == "", "")
        check("掩码函数对短串只打星", mailer.mask_secret("abc") == "***", mailer.mask_secret("abc"))

        # ==================================================== 5. 过期验证码清理
        print("\n--- 5. 找回密码验证码清理（进程内）---")
        from datetime import timedelta                            # noqa: E402
        from sqlalchemy import delete as sa_delete                # noqa: E402
        from sqlalchemy import select as sa_select                # noqa: E402
        from app.models import PasswordResetCode, utcnow          # noqa: E402

        base = utcnow()
        # (邮箱, 过期时刻, 已尝试次数, used_at) —— 后三个字段决定该不该被删
        seeds = [
            # 过期且已超过 1 小时缓冲           -> 删
            ("purge_old@example.com", base - timedelta(hours=2), 0, None),
            # 刚过期，但还在缓冲期内（防误删）   -> 留
            ("purge_recent@example.com", base - timedelta(minutes=10), 0, None),
            # **仍在有效期内**（最关键的一条）   -> 留
            ("purge_valid@example.com", base + timedelta(minutes=10), 0, None),
            # 已消费（一次性）                   -> 删
            ("purge_used@example.com", base + timedelta(minutes=10), 0, base),
            # 尝试次数已用尽                     -> 删
            ("purge_maxed@example.com", base + timedelta(minutes=10), 5, None),
            # 只错一次、仍然有效                 -> 留
            ("purge_tried@example.com", base + timedelta(minutes=10), 1, None),
        ]
        with SessionLocal() as s:
            # 先清空前面用例留下的行，让本次的删除行数只反映这 6 行种子
            s.execute(sa_delete(PasswordResetCode))
            s.commit()
            for email, exp, att, used in seeds:
                s.add(PasswordResetCode(
                    email=email, user_id=None, code_hash="not-a-real-hash",
                    expires_at=exp, attempts=att, max_attempts=5,
                    used_at=used, created_at=base,
                ))
            s.commit()

            deleted = mailer.purge_expired_reset_codes(s)
            left = sorted(
                r.email for r in s.scalars(
                    sa_select(PasswordResetCode)
                    .where(PasswordResetCode.email.like("purge_%"))
                ).all()
            )
            check("清理删掉了 3 行（过期超缓冲 / 已消费 / 超次）", deleted == 3, str(deleted))
            check("剩下的正好是那 3 行该留的（不多不少）",
                  left == ["purge_recent@example.com", "purge_tried@example.com",
                           "purge_valid@example.com"], str(left))
            check("**仍在有效期内的验证码没有被误删**",
                  "purge_valid@example.com" in left, str(left))
            check("刚过期但在缓冲期内的验证码被保留（防误删）",
                  "purge_recent@example.com" in left, str(left))
            check("只错一次且仍有效的验证码被保留",
                  "purge_tried@example.com" in left, str(left))
            check("过期超缓冲的已删除", "purge_old@example.com" not in left, str(left))
            check("已消费的已删除", "purge_used@example.com" not in left, str(left))
            check("超次的已删除", "purge_maxed@example.com" not in left, str(left))

            again = mailer.purge_expired_reset_codes(s)
            check("重复调用不误删（剩下的都在有效期内）", again == 0, str(again))

            # now 参数可注入：把「现在」推到 2 小时后，仍在有效期内的那批也该被清掉
            later = mailer.purge_expired_reset_codes(s, now=base + timedelta(hours=2))
            left2 = sorted(
                r.email for r in s.scalars(
                    sa_select(PasswordResetCode)
                    .where(PasswordResetCode.email.like("purge_%"))
                ).all()
            )
            check("now 参数可注入（推进时间后有效期内的也被清掉）",
                  later == 3 and left2 == [], f"deleted={later} left={left2}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            log_fp.close()
        except OSError:
            pass
        # 失败时把服务端日志尾部打出来：定位卡点/500 最直接的证据
        failed = [r for r in results if not r[1]]
        if failed:
            print(f"\n[diag] ---- server log tail ({SERVER_LOG}) ----")
            try:
                txt = SERVER_LOG.read_text(encoding="utf-8", errors="replace")
                print("\n".join(txt.splitlines()[-40:]))
            except OSError as exc:
                print(f"(read failed: {exc})")
        # 清掉临时库（含 -journal/-wal 副产物），避免在系统临时目录里堆垃圾
        for suffix in ("", "-journal", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n==== {passed}/{total} passed ====")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
