"""设备 token 用量冒烟：网关提取真实 usage + 双端查询接口。

覆盖（与任务清单一一对应）：
    A. ``llm_gateway.ChatResult`` 从**真实 HTTP 响应**里提取 usage
       （prompt/completion/total = 120/35/155）；
    B. ``usage.normalize_usage`` 四种厂商形状（OpenAI / Anthropic /
       Gemini / Ollama）+ 脏数据归零；
    C. 用户端 ``/api/usage/summary|rows`` 只统计本人设备；换他人 token
       看到 0（越权隔离）；传 device_id 参数被忽略；
    D. 管理端 ``/api/admin/usage/summary|rows`` 全量 + device_id /
       user_id / usage / since 过滤；
    E. 权限：普通用户访问管理端 403、未登录 401、非法时间参数 422；
    F. 响应契约：summary / rows 的键集合逐字锁定（多一个键就可能是
       把正文或内部字段泄出来了）。

两个基础设施决策
----------------
1. **进程内起服务**（uvicorn 跑后台线程）：usage 路由还没接进
   ``app.main``（集成由 team-lead 做），本脚本自己
   ``include_router``（用户端 + 管理端两个都要 include，少一个
   管理端断言全 404）；
2. **假上游是真 HTTP**（threading + http.server，端口 bind 0 自选）：
   要验的正是"从厂商响应体提取 usage"这条链路，mock 掉网关就等于
   把被测点mock 了。

用法::

    python tools/smoke_usage.py [--port 8120]
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable

# 默认端口统一由 tools/smoke_ports.py 托管（唯一真源）：smoke_usage=8120，
# 与 smoke_voice(8119) 错开。各脚本不再各写一份，并行跑必然撞车。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_ports import (  # 见 smoke_ports.py 模块 docstring
    detect_addr_in_use,
    guard_or_die,
    guard_port,
    release_port,
    resolve_port,
)

# ⚠️ **本地请求绝不走代理**（工程师核实：本机设了 http_proxy，urllib 默认会读它，
# 于是「访问自己刚起的 127.0.0.1 服务」被绕到代理一跳 —— 代理不通就 TimeoutError
# 且零断言失败，极易被误判成代码回归；CI runner 常注入代理，还会造成
# 「本地全绿、CI 超时」。显式关闭代理让请求一定直连本机。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 默认文件名带 PID：多人/CI 并行跑时固定文件名会互相抢同一个文件。
DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
DB_PATH = DB_DIR / f"elder_smoke_usage_{os.getpid()}.db"
# 日志落文件：服务跑在本进程的后台线程里，日志接 PIPE 无人消费会写满
# 缓冲区把写日志的一端永久阻塞（本仓 smoke_mail 的真实事故）。
SERVER_LOG = DB_PATH.with_name(DB_PATH.stem + "_server.log")
if DB_PATH.exists():
    DB_PATH.unlink()

PORT = resolve_port("smoke_usage")
BASE = f"http://127.0.0.1:{PORT}"

# 假上游固定返回的用量（测试基准值）。
FAKE_PROMPT_TOKENS = 120
FAKE_COMPLETION_TOKENS = 35
FAKE_TOTAL_TOKENS = 155

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


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
        except Exception:  # noqa: BLE001  4xx 的 body 可能是 HTML/纯文本，
            # 解析失败按原文返回；断言只看 status code。harness 有意吞掉。
            return e.code, raw


def wait_ready(timeout: float = 120.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            code, _ = req("GET", "/api/health")
            if code == 200:
                return True
        except Exception:  # noqa: BLE001, S110  服务未就绪时连接被拒是常态，
            # 静默试到超时为止正是这里的设计；失败由超时后返回 False 兜底。
            pass
        time.sleep(0.6)
    return False


def _read_log_tail(max_bytes: int = 8192) -> str:
    """读服务日志尾部，供启动失败 / bind 失败判定（输出走文件，见上）。"""
    try:
        return SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-max_bytes:]
    except OSError:
        return ""


# ================================================================ 假上游
class _FakeUpstream(BaseHTTPRequestHandler):
    """假 OpenAI 兼容上游：只回 ``/v1/chat/completions``，带 usage 段。

    刻意用 ``ThreadingHTTPServer`` + ``bind 0``：端口让内核自选，
    免得又一个硬编码端口和别人撞车；多线程是因为网关可能重试，
    单线程服务器在被自己占住时会死锁。
    """

    def log_message(self, *args):  # 静默：别往 stdout 刷访问日志
        pass

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except Exception:  # noqa: BLE001  请求体坏掉与本测试无关，按空处理
            body = {}
        last = (body.get("messages") or [{}])[-1]
        payload = {
            "choices": [{
                "message": {"role": "assistant",
                            "content": f"echo:{last.get('content', '')}"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": FAKE_PROMPT_TOKENS,
                      "completion_tokens": FAKE_COMPLETION_TOKENS,
                      "total_tokens": FAKE_TOTAL_TOKENS},
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_fake_upstream() -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    port = int(srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


# ================================================================ 进程内起服务
def start_app_server() -> tuple[object, threading.Thread]:
    """在后台线程里跑 app（uvicorn.Server + Config）。

    ⚠️ 环境变量必须在 import app.* **之前**设好：``app.config`` 在
    import 期读 ELDER_DB 绑定 engine。来晚一步，全部读写会落到真实库上。
    """
    os.environ["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
    os.environ["ELDER_JWT_SECRET"] = "smoke-usage-secret"
    os.environ["ELDER_SCHEDULER_INTERVAL"] = "3600"   # 不起后台调度
    os.environ["PYTHONIOENCODING"] = "utf-8"

    SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[logging.FileHandler(SERVER_LOG, encoding="utf-8")],
    )

    sys.path.insert(0, str(SERVER))
    import uvicorn
    from app.main import app
    from app.routers import usage as usage_router

    # usage 路由尚未接进 app.main（集成由 team-lead 做）—— 冒烟自己接。
    # 两个 router 都要 include：用户端 + 管理端，少一个管理端断言全 404。
    app.include_router(usage_router.router)
    app.include_router(usage_router.admin_router)

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


# ================================================================ 进程内播种
def seed(upstream_port: int) -> dict:
    """进程内播种：两台设备 + 一条 llm_main 配置。账号走 HTTP 注册（见 main）。

    返回后续断言要用的 id。设备与模型配置**刻意不走 HTTP**：建设备要激活码
    四步流程（那是 smoke_server/smoke_membership 的领地），本测试关心的是
    用量口径，不是设备绑定链路。
    """
    from app.db import SessionLocal
    from sqlalchemy import select

    from app import models

    db = SessionLocal()
    try:
        u1 = db.scalar(select(models.User).where(models.User.username == "usage_u1"))
        admin = db.scalar(select(models.User).where(models.User.username == "admin"))
        if u1 is None or admin is None:
            raise RuntimeError("播种前置不满足：usage_u1 / admin 不存在"
                               "（HTTP 注册或默认 seed 没生效）")
        dev_u1 = models.Device(device_id="usage-dev-u1", owner_id=u1.id,
                               name="u1 的设备")
        dev_admin = models.Device(device_id="usage-dev-admin", owner_id=admin.id,
                                  name="admin 的设备")
        provider = models.AIProvider(
            name="假上游（冒烟）", kind="llm_main", api_style="openai_compatible",
            endpoint=f"http://127.0.0.1:{upstream_port}/v1", api_key="sk-fake",
            model="fake-chat-1", enabled=True, priority=1,
        )
        db.add_all([dev_u1, dev_admin, provider])
        db.commit()
        return {"u1_id": int(u1.id), "admin_id": int(admin.id),
                "dev_u1": int(dev_u1.id), "dev_admin": int(dev_admin.id),
                "provider_id": int(provider.id)}
    finally:
        db.close()


def chat_and_record(ids: dict, device_id: int, user_id: int) -> object:
    """真调一次网关（打假上游）并按**生产同款路径**记账（record_from_result）。

    刻意走 ``record_from_result`` 而不是 ``record_model_usage``：前者读
    result 对象上的 token 字段，正是语音链路（source="voice"）与 HTTP 侧
    要共用的那条埋点路径。它曾有一个变量名笔误（``completion_tokens``
    写成未定义的 ``completion_tokens``，实为 ``completion``）导致每次
    调用必抛 NameError —— 本脚本先用 record_model_usage 绕过，修复落地后
    切回，让这条路径重新被冒烟覆盖。
    """
    from app.db import SessionLocal
    from app.llm_gateway import chat

    from app import usage as usage_svc

    db = SessionLocal()
    try:
        result = chat(db, "chat",
                      [{"role": "user", "content": "你好，今天天气怎么样"}])
        usage_svc.record_from_result(
            db, result, source="http", usage="chat",
            device_id=device_id, user_id=user_id,
            request_id=usage_svc.new_request_id(),
        )
        return result
    finally:
        db.close()


def main() -> int:
    upstream, up_port = start_fake_upstream()
    print(f"[boot] fake upstream on 127.0.0.1:{up_port} (db: {DB_PATH})")

    guard_or_die(PORT, "smoke_usage.py")
    hold, hold_hint = guard_port(PORT)
    if hold is None:            # 理论上 guard_or_die 已挡住，第二道保险
        print("[boot] " + hold_hint.replace("\n", "\n[boot] "))
        return 1
    release_port(hold, PORT)    # 起服务前必须释放，否则自己挡住自己

    server, _thread = start_app_server()
    try:
        if not wait_ready():
            print(f"[boot] server did not become ready. log tail ({SERVER_LOG}):")
            print(_read_log_tail())
            return 1
        # 阶段③：确认日志里没有 bind 失败痕迹（活着但没监听 -> 探到别人的服务）。
        if detect_addr_in_use(_read_log_tail()):
            print(f"[boot] ✗ 端口 {PORT} bind 失败（占位未生效）。结果不可信，退出。")
            print("[boot]   请换端口重跑：python tools/smoke_usage.py <其它端口>")
            return 1
        print("[boot] ready\n")

        # ==================================================== 账号（HTTP）
        def reg(name: str) -> str:
            req("POST", "/api/auth/register",
                {"username": name, "password": "test123456", "nickname": name})
            _, t = req("POST", "/api/auth/login",
                       {"username": name, "password": "test123456"})
            return t.get("access_token", "")

        u1 = reg("usage_u1")
        u2 = reg("usage_u2")
        _, t = req("POST", "/api/auth/login",
                   {"username": "admin", "password": "admin123"})
        admin = t.get("access_token", "")
        check("注册 u1/u2 并登录默认管理员", bool(u1 and u2 and admin),
              f"u1={bool(u1)} u2={bool(u2)} admin={bool(admin)}")

        # ==================================================== 播种 + 调网关
        ids = seed(up_port)
        r1 = chat_and_record(ids, ids["dev_u1"], ids["u1_id"])
        # 第二次调用打 admin 的设备：结果不在进程内断言（A 段已验过提取），
        # 它的意义是让管理端"全量"有 2 条可数。
        chat_and_record(ids, ids["dev_admin"], ids["admin_id"])

        # ---- A. ChatResult 提取真实 usage ----
        print("── A. ChatResult 从真实响应提取 usage ──")
        check("★ prompt_tokens == 120", r1.prompt_tokens == FAKE_PROMPT_TOKENS,
              str(r1.prompt_tokens))
        check("★ completion_tokens == 35", r1.completion_tokens == FAKE_COMPLETION_TOKENS,
              str(r1.completion_tokens))
        check("★ total_tokens == 155", r1.total_tokens == FAKE_TOTAL_TOKENS,
              str(r1.total_tokens))
        check("text 仍正常提取（加字段不能挤掉老功能）",
              r1.text.startswith("echo:"), str(r1.text)[:30])

        # ---- B. normalize_usage 四种厂商形状 ----
        print("── B. normalize_usage 四种厂商形状 + 脏数据 ──")
        from app.usage import normalize_usage

        cases = [
            ("OpenAI（usage 段）",
             {"usage": {"prompt_tokens": 11, "completion_tokens": 22,
                        "total_tokens": 33}}, (11, 22, 33)),
            ("Anthropic（input/output_tokens，total 缺省按和折算）",
             {"usage": {"input_tokens": 7, "output_tokens": 5}}, (7, 5, 12)),
            ("Gemini（usageMetadata + *TokenCount）",
             {"usageMetadata": {"promptTokenCount": 9,
                                "candidatesTokenCount": 4,
                                "totalTokenCount": 13}}, (9, 4, 13)),
            ("Ollama（顶层 prompt_eval_count/eval_count）",
             {"prompt_eval_count": 3, "eval_count": 8}, (3, 8, 11)),
            ("None", None, (0, 0, 0)),
            ("脏值（负数/字符串/布尔）",
             {"usage": {"prompt_tokens": -5, "completion_tokens": "x",
                        "total_tokens": True}}, (0, 0, 0)),
        ]
        for name, raw, want in cases:
            got = normalize_usage(raw)
            check(f"{name} -> {want}", got == want, str(got))

        # ---- C. 用户端 ----
        print("── C. 用户端 /api/usage（只看本人设备）──")
        code, s1 = req("GET", "/api/usage/summary", token=u1)
        check("u1 summary 200", code == 200, str(code))
        check("★ u1 只看到自己设备的 1 次调用", s1.get("calls") == 1, str(s1.get("calls")))
        check("★ u1 total_tokens == 155（不是全量 310）",
              s1.get("total_tokens") == FAKE_TOTAL_TOKENS, str(s1.get("total_tokens")))
        check("u1 prompt/completion 分解正确",
              s1.get("prompt_tokens") == FAKE_PROMPT_TOKENS
              and s1.get("completion_tokens") == FAKE_COMPLETION_TOKENS,
              f"{s1.get('prompt_tokens')}/{s1.get('completion_tokens')}")
        check("u1 summary 键集合逐字锁定（7 个）",
              set(s1) == {"calls", "failed_calls", "prompt_tokens",
                          "completion_tokens", "total_tokens", "by_usage", "by_day"},
              ",".join(sorted(s1)))
        check("u1 by_usage 按用途分解", s1.get("by_usage") == {"chat": 155},
              str(s1.get("by_usage")))
        check("u1 by_day 一条且总数对（不断言日期串：UTC/本地日界会 flake）",
              len(s1.get("by_day") or []) == 1
              and s1["by_day"][0]["total_tokens"] == 155,
              str(s1.get("by_day")))
        code, s2 = req("GET", "/api/usage/summary", token=u2)
        check("★ u2（无设备）看到 0 而不是报错",
              code == 200 and s2.get("calls") == 0 and s2.get("total_tokens") == 0,
              f"code={code} calls={s2.get('calls')}")
        code, s1x = req("GET", f"/api/usage/summary?device_id={ids['dev_admin']}",
                        token=u1)
        check("★ 用户端传 device_id 被忽略（可见范围只由 token 推导，"
              "且不报错——报错会泄露 id 存在性）",
              code == 200 and s1x.get("total_tokens") == FAKE_TOTAL_TOKENS,
              f"code={code} total={s1x.get('total_tokens')}")

        code, rows1 = req("GET", "/api/usage/rows", token=u1)
        row_keys = {"id", "created_at", "source", "usage", "device_id", "user_id",
                    "provider_name", "model", "prompt_tokens", "completion_tokens",
                    "total_tokens", "ok", "error", "elapsed_ms"}
        check("u1 rows 有 1 条且键集合逐字锁定（14 个，无正文字段）",
              code == 200 and len(rows1) == 1
              and set(rows1[0]) == row_keys,
              f"code={code} n={len(rows1) if isinstance(rows1, list) else '?'}")
        if isinstance(rows1, list) and rows1:
            r = rows1[0]
            check("u1 rows 内容正确（source/usage/tokens/ok）",
                  r.get("source") == "http" and r.get("usage") == "chat"
                  and r.get("total_tokens") == 155 and r.get("ok") is True
                  and r.get("provider_name") == "假上游（冒烟）"
                  and r.get("model") == "fake-chat-1",
                  f"{r.get('source')}/{r.get('usage')}/{r.get('total_tokens')}")
        code, rows2 = req("GET", "/api/usage/rows", token=u2)
        check("u2 rows 为空（越权隔离）",
              code == 200 and rows2 == [], f"code={code} n={len(rows2)}")

        # ---- D. 管理端 ----
        print("── D. 管理端 /api/admin/usage ──")
        code, sa = req("GET", "/api/admin/usage/summary", token=admin)
        check("admin summary 200", code == 200, str(code))
        check("★ admin 看到全量 2 次调用 / 310 tokens",
              sa.get("calls") == 2 and sa.get("total_tokens") == 310,
              f"calls={sa.get('calls')} total={sa.get('total_tokens')}")
        check("admin failed_calls == 0（两次都成功）",
              sa.get("failed_calls") == 0, str(sa.get("failed_calls")))
        code, sf = req("GET", f"/api/admin/usage/summary?device_id={ids['dev_u1']}",
                       token=admin)
        check("★ device_id 过滤生效（只剩 u1 那次的 155）",
              code == 200 and sf.get("calls") == 1 and sf.get("total_tokens") == 155,
              f"calls={sf.get('calls')} total={sf.get('total_tokens')}")
        code, su = req("GET", f"/api/admin/usage/summary?user_id={ids['u1_id']}",
                       token=admin)
        check("user_id 过滤生效（u1 的 1 次）",
              code == 200 and su.get("calls") == 1 and su.get("total_tokens") == 155,
              f"calls={su.get('calls')}")
        code, snone = req("GET", f"/api/admin/usage/summary?user_id={ids['admin_id'] + 9999}",
                          token=admin)
        check("不存在的 user_id 过滤出 0（不是全量）",
              code == 200 and snone.get("calls") == 0, f"calls={snone.get('calls')}")
        code, sc = req("GET", "/api/admin/usage/summary?usage=chat", token=admin)
        check("usage=chat 过滤生效（2 次）",
              code == 200 and sc.get("calls") == 2, f"calls={sc.get('calls')}")
        code, snt = req("GET", "/api/admin/usage/summary?usage=tts", token=admin)
        check("usage=tts 过滤出 0", code == 200 and snt.get("calls") == 0,
              f"calls={snt.get('calls')}")
        code, sold = req("GET", "/api/admin/usage/summary?since=2020-01-01", token=admin)
        check("since=过去日期 -> 全量仍在（ISO 日期按当天 00:00 解析）",
              code == 200 and sold.get("calls") == 2, f"calls={sold.get('calls')}")
        code, sfut = req("GET", "/api/admin/usage/summary?since=2099-01-01", token=admin)
        check("since=未来日期 -> 0", code == 200 and sfut.get("calls") == 0,
              f"calls={sfut.get('calls')}")
        code, sdt = req("GET",
                        "/api/admin/usage/summary?since=2020-01-01T00:00:00",
                        token=admin)
        check("since 也收日期时间（带 T 的 ISO）",
              code == 200 and sdt.get("calls") == 2, f"calls={sdt.get('calls')}")

        code, arows = req("GET", "/api/admin/usage/rows", token=admin)
        check("admin rows 全量 2 条", code == 200 and len(arows) == 2,
              f"code={code} n={len(arows) if isinstance(arows, list) else '?'}")
        code, arows_f = req("GET",
                            f"/api/admin/usage/rows?device_id={ids['dev_admin']}",
                            token=admin)
        check("admin rows device_id 过滤生效（1 条）",
              code == 200 and len(arows_f) == 1,
              f"code={code} n={len(arows_f) if isinstance(arows_f, list) else '?'}")

        # ---- E. 权限与参数校验 ----
        print("── E. 权限与参数校验 ──")
        code, _ = req("GET", "/api/usage/summary")
        check("未登录 -> 401", code == 401, f"code={code}")
        code, _ = req("GET", "/api/usage/summary", token=u1)
        code2, _ = req("GET", "/api/admin/usage/summary", token=u1)
        check("普通用户访问管理端 -> 403", code2 == 403, f"code={code2}")
        code3, _ = req("GET", "/api/admin/usage/rows", token=u1)
        check("普通用户访问管理端 rows -> 403", code3 == 403, f"code={code3}")
        code, _ = req("GET", "/api/usage/summary?since=not-a-date", token=u1)
        check("非法 since -> 422（pydantic 解析，不静默忽略）",
              code == 422, f"code={code}")
        code, _ = req("GET", "/api/usage/rows?limit=999", token=u1)
        check("limit 超上限 -> 422（Query ge/le 约束）", code == 422, f"code={code}")

        passed = sum(1 for _, ok, _ in results if ok)
        total = len(results)
        print(f"\n==== {passed}/{total} passed ====")
        for name, ok, detail in results:
            if not ok:
                print(f"  FAILED: {name} {detail}")
        return 0 if passed == total else 1
    finally:
        # 收尾：先让 uvicorn 退出（触发 lifespan shutdown），再关假上游，
        # 最后 dispose engine 释放 SQLite 文件句柄（Windows 上不释放删不掉）。
        server.should_exit = True
        time.sleep(0.3)
        upstream.shutdown()
        try:
            from app.db import engine
            engine.dispose()
        except Exception:  # noqa: BLE001, S110  收尾失败不影响判定，别掩盖真异常
            pass
        if DB_PATH.exists():
            DB_PATH.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
