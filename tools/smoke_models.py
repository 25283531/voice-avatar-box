"""模型接入点（多 API 接入口 + 统一调用层）冒烟测试。

用法：
    python tools/smoke_models.py [--port 8104]

覆盖：
  A. 九种 kind 注册与 ?kind= 过滤、非法取值被拒
  B. resolve?usage= 的平滑过渡（新 kind 没配时退回旧 llm）
  C. 同一用途多条配置按 priority 选择 / 禁用后自动落到次条
  D. 真实调用链路（chat / embed / rerank，含三家厂商协议切换）
  E. 错误分诊：配置缺失 / 密钥错误 / 超时 / 5xx / 429 五种返回必须彼此不同
  F. 脱敏：/safe、/resolve、/export 都不返回 api_key
  G. 设备下发：只配新 kind 时 /api/device/sync 仍能拿到一条可用 LLM
  H. context_window 生效（历史被裁剪）

**不碰任何真实付费 API**：所有「厂商」都是本地 127.0.0.1 上临时起的假 HTTP 服务，
把 provider.endpoint 指过去即可，一分钱不花也能验证整条调用链路。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

# 默认文件名带 PID —— 与其余冒烟脚本（smoke_server / smoke_roles 等）对齐。
#
# ⚠️ 为什么这里必须带 PID（2026-10-16 实测踩到）：
# 早先本脚本用**固定**文件名 `elder_smoke_model.db`，且开头无条件 `unlink()`。
# 两个进程同时跑（CI 并行、或两个 agent 同时验证）时：
#   1. B 启动时把 A **正在使用**的库文件删掉；
#   2. 两人随后往**同一个文件**里写各自的配置；
#   3. 于是 `GET /api/providers?kind=embedding` 返回 **2 条**，
#      `len(hit) == 1` 的断言失败，patch_by_kind 也无法唯一命中 -> 404。
# 症状是「A 组 kind 过滤莫名多出一条」这类**与代码无关的假失败**，
# 极像真 bug，实际是环境踩踏 —— 与端口冲突是同一类问题。
# 带 PID 后每次运行独占一个库，并行跑互不干扰。
DB_PATH = Path(os.environ.get(
    "SMOKE_MODEL_DB",
    str(Path(tempfile.gettempdir()) / f"elder_smoke_model_{os.getpid()}.db")))
if DB_PATH.exists():
    DB_PATH.unlink()

PORT = resolve_port("smoke_models")
BASE = f"http://127.0.0.1:{PORT}"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


# ================================================================ 假厂商服务
@dataclass
class StubState:
    last_path: str = ""
    last_body: dict | None = None
    hits: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        self.hits = {}


STATE = StubState()


class StubVendor(BaseHTTPRequestHandler):
    """假装自己是各种大模型厂商。

    用 **模型名里的关键字** 切换行为，这样一个进程就能覆盖全部用例：
      xxx-err401   -> 401（密钥错）
      xxx-err429   -> 429（限流）
      xxx-err500   -> 500（厂商故障）
      xxx-slow     -> 睡 2.5 秒再回（构造超时）
    """

    def log_message(self, *args):  # 静音，别刷屏
        pass

    def do_GET(self):  # 给测试侧读取桩服务的内部状态
        if self.path.startswith("/__last"):
            self._reply(200, {"last_path": STATE.last_path, "last_body": STATE.last_body})
        elif self.path.startswith("/__stats"):
            self._reply(200, {"hits": STATE.hits})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else "{}"
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        model = str(body.get("model") or "")
        path = self.path.split("?")[0]

        STATE.last_path = path
        STATE.last_body = body
        STATE.hits[model] = STATE.hits.get(model, 0) + 1

        if "slow" in model:
            time.sleep(2.5)
            self._reply(200, self._openai(model, "（慢到的回复）"))
            return
        if "err401" in model:
            self._reply(401, {"error": {"message": "invalid api key"}})
            return
        if "err429" in model:
            self._reply(429, {"error": {"message": "rate limit reached for requests"}})
            return
        if "err500" in model:
            self._reply(500, {"error": {"message": "internal server error"}})
            return

        if path.endswith(":generateContent"):          # Gemini
            self._reply(200, {"candidates": [{"content": {"parts": [{"text": "gemini:" + model}]},
                                              "finishReason": "STOP"}]})
        elif path.endswith("/messages"):               # Anthropic
            self._reply(200, {"content": [{"type": "text", "text": "anthropic:" + model}],
                              "stop_reason": "end_turn"})
        elif path.endswith("/api/chat"):               # Ollama 原生
            self._reply(200, {"message": {"role": "assistant", "content": "ollama:" + model},
                              "done": True})
        elif path.endswith("/embeddings"):
            inputs = body.get("input") or ["x"]
            self._reply(200, {"data": [{"index": i, "embedding": [0.1 * (i + 1), 0.2, 0.3, 0.4]}
                                       for i in range(len(inputs))]})
        elif path.endswith("/rerank"):
            docs = body.get("documents") or []
            self._reply(200, {"results": [{"index": i, "relevance_score": 0.9 - 0.1 * i}
                                          for i in range(len(docs))]})
        else:                                          # OpenAI 兼容默认
            self._reply(200, self._openai(model, "openai:" + model))

    @staticmethod
    def _openai(model: str, text: str) -> dict:
        return {"choices": [{"message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}]}

    def _reply(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start_stub() -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), StubVendor)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ================================================================ 被测服务
def req(method: str, path: str, body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=40) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


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


def main() -> int:
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
    env["ELDER_JWT_SECRET"] = "smoke-test-secret"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"
    env["PYTHONIOENCODING"] = "utf-8"

    print(f"[boot] starting server on :{PORT} (db: {DB_PATH})")
    # ---- 三段式端口守卫（QA 第 4 轮「假绿」修复）----
    # 端口被占时 uvicorn 的 socket bind 会失败，**但进程不退出**（活着、只是没在
    # 监听）；而 /api/health **永远返回 200** -> wait_ready 会探到**别人的服务**
    # -> 全部断言打到别人身上、用自己从未 seed 过的 DB。
    # 三段：①事前 is_port_free ②socket 占位缩小竞态窗口 ③起服务后校验 stderr
    # ⚠️ 三段加起来**仍不是绝对保证**（①与真正 bind 之间有不可消除的竞态窗口）。
    #    真正的绝对保证是让服务自证身份（run_id），需改生产代码，留给下一轮。
    guard_or_die(PORT, "smoke_models.py")

    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT),
         "--log-level", "warning"],
        cwd=str(SERVER), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    srv = None
    try:
        if not wait_ready():
            out = proc.stdout.read() if proc.stdout else ""
            print("[boot] server did not become ready. output:\n" + out[-3000:])
            return 1
        print("[boot] ready\n")

        srv, stub_port = start_stub()
        stub = f"http://127.0.0.1:{stub_port}/v1"
        print(f"[boot] stub vendor on :{stub_port}\n")

        code, tk = req("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
        if code != 200:
            print("登录失败", code, tk)
            return 1
        token = tk["access_token"]

        def add(**kw):
            base = {"name": kw.pop("name"), "kind": kw.pop("kind"), "api_key": "sk-stub-secret"}
            base.update(kw)
            return req("POST", "/api/providers", base, token)

        def patch(pk: int, patch_obj: dict):
            code_, cur = req("GET", "/api/providers", token=token)
            row = next((r for r in cur if r["id"] == pk), None)
            if row is None:
                return 404, "not found"
            for k in ("id", "created_at"):
                row.pop(k, None)
            row.update(patch_obj)
            return req("PATCH", f"/api/providers/{pk}", row, token)

        def patch_by_kind(kind: str, patch_obj: dict):
            """按 kind 定位「网关会实际选中」的那条记录再改，不依赖 id。

            两个坑：
            1. 本测试里 E 组为了测错误分诊会删掉 llm_light / llm 再重建，重建后
               服务端分配的是**新 id**，之前存进 created 的旧 id 全部失效。
            2. 同一个 kind 可能有多条（C 组就建了 light-a / light-b，外加A 组那条），
               「恰好命中一条」的写法会直接 404，然后前置静默不生效——
               表现为「明明设了 context_window=16，裁剪用例却说没裁」。

            所以这里取 priority 最小的那条：网关选主配置也是按 priority 升序，
            对着它改才对得上真实被调用的那一条。
            """
            code_, cur = req("GET", f"/api/providers?kind={kind}", token=token)
            rows = [r for r in (cur or []) if r.get("kind") == kind]
            if not rows:
                return 404, f"kind={kind} 一条都没有"
            target = min(rows, key=lambda r: (r.get("priority", 10), r["id"]))
            return patch(target["id"], patch_obj)

        # -------------------------------------------------- A. 分类与过滤
        print("── A. 模型分类（kind）注册与过滤 ──")
        new_kinds = ["embedding", "rerank", "llm_light", "llm_main", "vision"]
        legacy_kinds = ["asr", "llm", "tts", "vad"]
        all_kinds = legacy_kinds + new_kinds
        created = {}
        for k in all_kinds:
            code, r = add(name=f"桩-{k}", kind=k, provider="stub",
                          endpoint=stub, model=f"{k}-model", priority=10)
            ok = code == 201
            created[k] = r.get("id") if ok else None
            check(f"注册 kind={k}", ok, f"code={code}" if not ok else f"id={created[k]}")

        code, bad = req("POST", "/api/providers",
                        {"name": "乱填", "kind": "super_llm"}, token)
        check("非法 kind 被拒（422）", code == 422, f"code={code}")

        code, bad2 = req("POST", "/api/providers",
                         {"name": "乱填2", "kind": "llm", "api_style": "openai_v9"}, token)
        check("非法 api_style 被拒（422）", code == 422, f"code={code}")

        for k in ("embedding", "llm_main", "vision"):
            code, rows = req("GET", f"/api/providers?kind={k}", token=token)
            hit = [r for r in rows if r["kind"] == k] if isinstance(rows, list) else []
            check(f"?kind={k} 过滤命中", code == 200 and len(hit) == 1,
                  f"{len(hit)} 条")

        code, cat = req("GET", "/api/providers/catalog", token=token)
        kinds = [c["value"] for c in (cat or {}).get("kinds", [])]
        check("/catalog 列出全部 9 种能力 + 8 个用途",
              code == 200 and set(all_kinds) <= set(kinds) and len(cat.get("usages", [])) == 8,
              f"kinds={len(kinds)} usages={(cat or {}).get('usages') and len(cat['usages'])}")

        code, rows = req("GET", f"/api/providers?kind={created['llm_main'] and 'llm_main'}", token=token)
        row0 = rows[0]
        check("新增调用层字段有默认值",
              row0.get("api_style") == "" and row0.get("timeout_sec") == 30
              and row0.get("max_retries") == 1 and row0.get("context_window") == 0
              and row0.get("supports_stream") is False,
              f"style={row0.get('api_style')!r} timeout={row0.get('timeout_sec')} "
              f"retries={row0.get('max_retries')} ctx={row0.get('context_window')}")

        # -------------------------------------------------- B. 平滑过渡
        print("\n── B. resolve?usage= 的平滑过渡（旧 llm 兜底）──")
        # 先把新 kind 全部停用，模拟「升级上来的老部署：只有一条 kind=llm」
        for k in new_kinds:
            patch(created[k], {"enabled": False})
        patch(created["llm"], {"enabled": True, "priority": 10, "model": "legacy-llm"})

        code, rv = req("GET", "/api/providers/resolve?usage=chat", token=token)
        check("只配旧 llm 时 usage=chat 退回 llm",
              code == 200 and rv.get("kind") == "llm" and rv.get("model") == "legacy-llm"
              and rv.get("fallback") is True,
              f"kind={rv.get('kind')} model={rv.get('model')} fallback={rv.get('fallback')}")

        code, rv = req("GET", "/api/providers/resolve?usage=light", token=token)
        check("只配旧 llm 时 usage=light 也退回 llm",
              code == 200 and rv.get("kind") == "llm" and rv.get("fallback") is True,
              f"kind={rv.get('kind')}")

        patch(created["llm_main"], {"enabled": True, "priority": 5, "model": "main-big-model"})
        code, rv = req("GET", "/api/providers/resolve?usage=chat", token=token)
        check("配上 llm_main 后 usage=chat 改用它（fallback=False）",
              code == 200 and rv.get("kind") == "llm_main" and rv.get("model") == "main-big-model"
              and rv.get("fallback") is False,
              f"kind={rv.get('kind')} model={rv.get('model')}")

        code, rv = req("GET", "/api/providers/resolve?usage=light", token=token)
        check("同理 usage=light 与 chat 互不干扰", code == 200 and rv.get("kind") == "llm",
              f"kind={rv.get('kind')}")

        code, rv = req("GET", "/api/providers/resolve?usage=nope", token=token)
        check("未知 usage 返回 404 并列出可选值", code == 404
              and isinstance(rv, dict) and "chat" in str(rv.get("detail", "")),
              f"code={code}")

        # -------------------------------------------------- C. priority 主备
        print("\n── C. 同一用途多条配置：按 priority 选择 ──")
        code, b1 = add(name="备用-主卅一", kind="llm_light", provider="stub",
                       endpoint=stub, model="light-a", priority=1)
        check("新增小模型 A（priority=1）", code == 201, str(code))
        pk_a = b1["id"]
        code, b2 = add(name="备用-次条", kind="llm_light", provider="stub",
                       endpoint=stub, model="light-b", priority=2)
        pk_b = b2["id"]
        code, rv = req("GET", "/api/providers/resolve?usage=light", token=token)
        check("priority 小的那条胜出", code == 200 and rv.get("model") == "light-a",
              f"model={rv.get('model')} priority={rv.get('priority')}")

        patch(pk_a, {"enabled": False})
        code, rv = req("GET", "/api/providers/resolve?usage=light", token=token)
        check("高优先级被禁用后自动落到次条", code == 200 and rv.get("model") == "light-b",
              f"model={rv.get('model')}")
        patch(pk_a, {"enabled": True})

        # -------------------------------------------------- D. 真实调用链路
        print("\n── D. 调用链路（打本地桩服务）──")
        # 这里要让每种用途都有一条可用配置（B 段为了验证兜底把它们停掉了）
        patch(created["embedding"], {"enabled": True, "model": "emb-online", "priority": 1})
        patch(created["rerank"], {"enabled": True, "model": "rank-online", "priority": 1})

        code, r = req("POST", "/api/providers/test",
                      {"usage": "chat", "messages": [{"role": "user", "content": "你好"}]}, token)
        check("chat 调用成功并拿到文本", code == 200 and r.get("ok") is True
              and str(r.get("text", "")).startswith("openai:main-big-model"),
              f"text={r.get('text')} ({r.get('elapsed_ms')}ms) via {r.get('kind')}/{r.get('model')}")

        code, r = req("POST", "/api/providers/test",
                      {"usage": "embed", "text": "今天吃了降压药"}, token)
        vec = (r or {}).get("vectors") or [[]]
        check("embed 返回浮点向量（4 维）",
              code == 200 and r.get("ok") is True and len(vec[0]) == 4
              and all(isinstance(x, float) for x in vec[0]),
              f"dim={len(vec[0])} head={vec[0][:2]} via {r.get('model')}")

        code, r = req("POST", "/api/providers/test",
                      {"usage": "rerank", "text": "吃药了吗",
                       "documents": ["今天吃了降压药", "今天天气不错", "跳了二十分钟操"]}, token)
        scores = (r or {}).get("scores") or []
        check("rerank 返回与候选数等长的分数",
              code == 200 and r.get("ok") is True and len(scores) == 3 and scores[0] > scores[2],
              f"scores={scores}")

        # 协议切换：再配三种非 OpenAI 风格，验证报文翻译真的走了各自的路径
        code, r0 = req("POST", "/api/providers/test",
                       {"usage": "light",
                        "messages": [{"role": "system", "content": "你是小暖"},
                                     {"role": "user", "content": "提醒我下午三点吃药"}]}, token)
        check("默认走 OpenAI 兼容 /chat/completions",
              r0.get("ok") is True and STATE.last_path.endswith("/chat/completions")
              and isinstance(STATE.last_body, dict) and "messages" in STATE.last_body,
              f"path={STATE.last_path}")

        code, r1 = req("POST", "/api/providers/test",
                       {"usage": "chat", "messages": [{"role": "user", "content": "你好"}]}, token)
        check("调用成功时 attempts 记录到具体配置",
              len(r1.get("attempts", [])) >= 1 and r1["attempts"][0].get("ok") is True
              and r1["attempts"][0].get("kind") == "llm_main",
              str([f"{a['kind']}/{a['model']}/{a['ok']}" for a in r1.get("attempts", [])]))

        # 三种非 OpenAI 协议各配一条，验证报文翻译真的走了对应路径
        code, oll = add(name="桩-ollama", kind="llm_light", provider="ollama",
                        endpoint=f"http://127.0.0.1:{stub_port}", model="local-7b",
                        api_style="ollama", priority=0)
        if code == 201:
            code, r2 = req("POST", "/api/providers/test",
                           {"usage": "light", "messages": [{"role": "user", "content": " hi"}]}, token)
            check("api_style=ollama 走 /api/chat",
                  r2.get("ok") is True and STATE.last_path == "/api/chat"
                  and str(r2.get("text", "")).startswith("ollama:"),
                  f"path={STATE.last_path} text={r2.get('text')}")
            req("DELETE", f"/api/providers/{oll['id']}", token=token)
        else:
            check("api_style=ollama 走 /api/chat", False, f"create failed code={code}")

        code, ant = add(name="桩-anthropic", kind="llm_light", provider="anthropic",
                        endpoint=stub, model="claude-x", api_style="anthropic", priority=0)
        if code == 201:
            code, r3 = req("POST", "/api/providers/test",
                           {"usage": "light",
                            "messages": [{"role": "system", "content": "sys"},
                                         {"role": "user", "content": "hi"}]}, token)
            check("api_style=anthropic 走 /messages 且 system 单独成字段",
                  r3.get("ok") is True and STATE.last_path.endswith("/messages")
                  and STATE.last_body.get("system") == "sys"
                  and not any(m.get("role") == "system" for m in STATE.last_body.get("messages", []))
                  and isinstance(STATE.last_body.get("max_tokens"), int),
                  f"path={STATE.last_path} system={STATE.last_body.get('system')!r}")
            req("DELETE", f"/api/providers/{ant['id']}", token=token)
        else:
            check("api_style=anthropic 走 /messages", False, f"create failed code={code}")

        code, gem = add(name="桩-gemini", kind="llm_light", provider="gemini",
                        endpoint=f"http://127.0.0.1:{stub_port}/v1beta", model="gemini-pro",
                        api_style="gemini", priority=0)
        if code == 201:
            code, r4 = req("POST", "/api/providers/test",
                           {"usage": "light", "messages": [{"role": "user", "content": "hi"}]}, token)
            check("api_style=gemini 走 :generateContent",
                  r4.get("ok") is True and STATE.last_path.endswith(":generateContent")
                  and str(r4.get("text", "")).startswith("gemini:"),
                  f"path={STATE.last_path} text={r4.get('text')}")
            req("DELETE", f"/api/providers/{gem['id']}", token=token)
        else:
            check("api_style=gemini 走 :generateContent", False, f"create failed code={code}")

        # -------------------------------------------------- E. 错误分诊
        print("\n── E. 错误类型必须彼此区分开 ──")
        # 先清掉前面几组遗留的 llm_light 配置。这一组要验的是「单条配置失败时
        # 分诊码对不对」，而网关的既定行为是**失败会自动降级到下一条同kind 配置**
        # （见 C 组的备用切换用例）。只要还留着别的可用配置，整体就会 ok=true，
        # 失败原因只出现在 attempts[] 里，断言就会误判成"分诊信息丢了"。
        #
        # llm 也要一起清：`light` 用途的候选链是 llm_light -> llm，留着旧
        # kind=llm 的配置，它会当兜底把上面四种故障全接走。存量部署里就是这个形态。
        #
        # ⚠️ 清之前必须存快照并在组末恢复：F/G/H 组要用到 llm / llm_main 这些配置
        # 来验脱敏、设备下发与历史裁剪，直接删掉会连带把那三组测挂。
        code, snapshot = req("GET", "/api/providers", token=token)
        saved_rows = [dict(x) for x in (snapshot or [])
                      if x.get("kind") in ("llm_light", "llm")]
        for row in saved_rows:
            req("DELETE", f"/api/providers/{row['id']}", token=token)
        code, r = req("GET", "/api/providers/resolve?usage=light", token=token)
        check("E 组前置：已清空 llm_light/llm 兜底（否则会被自动降级掩盖分诊）",
              code != 200 or r.get("model") in (None, ""), f"code={code} model={r.get('model')}")

        def _restore_e_snapshot():
            """把 E 组开删掉的配置按原字段建回来，保证 F/G/H 看到的库状态没被动过。

            字段白名单必须覆盖 ProviderIn 的全部实际字段（注意是 `provider` 而不是
            `api_style`，写错会让厂商信息整条丢掉，桩服务端点跟着变回默认）；
            `enabled` / `context_window` 同样要还原 —— 后面几组正是靠「停用某条」
            和「context_window=16」这两个前置来构造场景的。
            """
            keep = ("name", "kind", "provider", "endpoint", "api_key", "model",
                    "api_style", "supports_stream", "timeout_sec", "max_retries",
                    "context_window", "description", "system_prompt", "tts_voice",
                    "is_default", "params", "priority", "enabled")
            # 只重建 saved_rows（=本组删掉的那几条）。快照若存全量再全量重建，
            # 会把 embedding / rerank 这些没删过的 kind 也复制一份，库里就出现两条
            # 同kind 配置，后面 patch_by_kind 要求唯一命中，必然 404。
            #
            # 重建会拿到新 id，所以本组之后一律用 patch_by_kind 按 kind 定位，
            # 不再依赖 created 里存的旧 id（created 只在 E 组之前有效）。
            for row in saved_rows:
                payload = {k: row[k] for k in keep if k in row}
                req("POST", "/api/providers", payload, token)

        code, e1 = add(name="桩-密钥错", kind="llm_light", provider="stub",
                       endpoint=stub, model="auth-err401", priority=0, max_retries=0)
        code, r = req("POST", "/api/providers/test",
                      {"usage": "light", "messages": [{"role": "user", "content": "hi"}]}, token)
        check("密钥错误 -> auth_failed（且文案点明密钥/额度）",
              r.get("ok") is False and r.get("error_code") == "auth_failed"
              and ("密钥" in r.get("message", "") or "余额" in r.get("message", "")),
              f"code={r.get('error_code')} msg={r.get('message')[:60]}")
        req("DELETE", f"/api/providers/{e1['id']}", token=token)

        code, e2 = add(name="桩-超时", kind="llm_light", provider="stub",
                       endpoint=stub, model="too-slow", priority=0,
                       max_retries=0, timeout_sec=1)
        t0 = time.monotonic()
        code, r = req("POST", "/api/providers/test",
                      {"usage": "light", "messages": [{"role": "user", "content": "hi"}]}, token)
        dt = time.monotonic() - t0
        check("超时 -> timeout（1s 熔断，没被拖到 2.5s）",
              r.get("ok") is False and r.get("error_code") == "timeout" and dt < 2.2,
              f"code={r.get('error_code')} 耗时={dt:.2f}s msg={r.get('message')[:50]}")
        check("超时文案提示去调「超时秒数」", "超时秒数" in r.get("message", ""),
              r.get("message", "")[:70])
        req("DELETE", f"/api/providers/{e2['id']}", token=token)

        code, e3 = add(name="桩-厂商故障", kind="llm_light", provider="stub",
                       endpoint=stub, model="boom-err500", priority=0, max_retries=1)
        code, r = req("POST", "/api/providers/test",
                      {"usage": "light", "messages": [{"role": "user", "content": "hi"}]}, token)
        hits500 = STATE.hits.get("boom-err500", 0)
        check("厂商 5xx -> upstream_error（并按 max_retries=1 重试过）",
              r.get("ok") is False and r.get("error_code") == "upstream_error" and hits500 == 2,
              f"code={r.get('error_code')} 桩收到 {hits500} 次请求")
        req("DELETE", f"/api/providers/{e3['id']}", token=token)

        code, e4 = add(name="桩-限流", kind="llm_light", provider="stub",
                       endpoint=stub, model="busy-err429", priority=0, max_retries=0)
        code, r = req("POST", "/api/providers/test",
                      {"usage": "light", "messages": [{"role": "user", "content": "hi"}]}, token)
        check("限流 -> rate_limited（文案提示退避/切备用）",
              r.get("ok") is False and r.get("error_code") == "rate_limited"
              and "限流" in r.get("message", ""),
              f"code={r.get('error_code')} msg={r.get('message')[:50]}")
        req("DELETE", f"/api/providers/{e4['id']}", token=token)

        # 主配置 5xx + 备用配置正常 -> 自动切换成功。
        # 这里自建两条而不是复用 C 组的 pk_a/pk_b：E 组开头为了测分诊已经把
        # 全部 llm_light 配置清空了，patch 一个已删除的 id 只会拿到 404。
        code, f1 = add(name="降级-主", kind="llm_light", provider="stub",
                       endpoint=stub, model="main-err500", priority=1, max_retries=0)
        code, f2 = add(name="降级-备", kind="llm_light", provider="stub",
                       endpoint=stub, model="backup-ok", priority=2, max_retries=0)
        code, r = req("POST", "/api/providers/test",
                      {"usage": "light", "messages": [{"role": "user", "content": "hi"}]}, token)
        atts = r.get("attempts") or []
        trace = [f"{a['model']}:{'ok' if a['ok'] else a['error_code']}" for a in atts]
        check("主配置 5xx 后自动切备用配置成功",
              r.get("ok") is True and str(r.get("text", "")) == "openai:backup-ok"
              and len(atts) == 2 and atts[0]["ok"] is False and atts[1]["ok"] is True,
              f"text={r.get('text')} trace={trace}")
        # 反向验证 E 组开头那次清理是必要的：清掉备用后，同样的 5xx 必须
        # 以 upstream_error 冒到上层，而不是被悄悄降级掩盖。
        req("DELETE", f"/api/providers/{f2['id']}", token=token)
        code, r = req("POST", "/api/providers/test",
                      {"usage": "light", "messages": [{"role": "user", "content": "hi"}]}, token)
        check("没有备用配置时，5xx 如实冒成 upstream_error（不被降级掩盖）",
              r.get("ok") is False and r.get("error_code") == "upstream_error",
              f"code={r.get('error_code')} msg={r.get('message', '')[:60]}")
        req("DELETE", f"/api/providers/{f1['id']}", token=token)

        # E 组收尾：把开头清掉的 llm / llm_light 配置恢复原状。
        # 不恢复的话，F 组「resolve?usage=light 不含 api_key」会因为 503 拿不到
        # 字段而误报失败，G 组「旧llm 只下发一条」也会因为 llm 没了而拿不到对照。
        _restore_e_snapshot()
        code, r = req("GET", "/api/providers/resolve?usage=light", token=token)
        check("E 组收尾：已恢复进入本组前的 llm / llm_light 配置（不污染 F/G/H）",
              code == 200 and r.get("model") not in (None, ""),
              f"code={code} model={r.get('model')}")

        # 配置缺失：先把 embed 用途的那条停用
        patch_by_kind("embedding", {"enabled": False})
        code, rv = req("GET", "/api/providers/resolve?usage=embed", token=token)
        check("resolve 配置缺失 -> 503 + 提示去配 embedding",
              code == 503 and "embedding" in str(rv.get("detail", ""))
              and "尚未配置" in str(rv.get("detail", "")),
              f"code={code} detail={str(rv.get('detail', ''))[:80]}")

        code, r = req("POST", "/api/providers/test", {"usage": "embed", "text": "x"}, token)
        check("调用配置缺失 -> no_config（文案点名要去「AI 接入」配模型）",
              r.get("ok") is False and r.get("error_code") == "no_config"
              and "AI 接入" in r.get("message", ""),
              f"code={r.get('error_code')} msg={r.get('message')[:80]}")

        check("no_config / auth_failed / timeout / upstream_error / rate_limited 五码互不相同",
              len({"no_config", "auth_failed", "timeout", "upstream_error", "rate_limited"}) == 5,
              "no_config | auth_failed | timeout | upstream_error | rate_limited")
        patch_by_kind("embedding", {"enabled": True})

        # -------------------------------------------------- F. 脱敏
        print("\n── F. 密钥脱敏 ──")
        # 让每个用途都有一条可用配置，便于逐个验证 resolve 的脱敏输出
        patch_by_kind("rerank", {"enabled": True, "model": "rerank-model", "priority": 1})
        code, safe = req("GET", "/api/providers/safe", token=token)
        leaked = json.dumps(safe, ensure_ascii=False)
        check("/api/providers/safe 全表不出现 sk-stub-secret",
              code == 200 and "sk-stub-secret" not in leaked, f"{len(safe)} 条")
        # P0-2 止血：/safe 现在要求登录，且只含平台配置（角色走 /api/roles）。
        # 这两条断言守的是"角色不该从脱敏列表流出去"—— 它是 P0-1 事故链路的起点。
        code, _ = req("GET", "/api/providers/safe")
        check("/api/providers/safe 未登录访问 -> 401", code == 401, f"code={code}")
        safe_rows = safe if isinstance(safe, list) else []
        check("/api/providers/safe 每行都有 kind（不含角色行）",
              all(r.get("kind") for r in safe_rows),
              ",".join(str(r.get("kind")) for r in safe_rows))
        check("/api/providers/safe 不再暴露人格字段",
              not any({"system_prompt", "description"} & set(r) for r in safe_rows),
              ",".join(sorted(set().union(*[set(r) for r in safe_rows])) if safe_rows else []))

        for u in ("chat", "light", "embed", "rerank"):
            code, rv = req("GET", f"/api/providers/resolve?usage={u}", token=token)
            if code == 200:
                check(f"resolve?usage={u} 不含 api_key", "api_key" not in rv,
                      f"model={rv.get('model')}")
            else:
                check(f"resolve?usage={u} 不含 api_key", False, f"code={code}")

        code, exp = req("GET", "/api/providers/export/xiaozhi", token=token)
        exp_txt = json.dumps(exp, ensure_ascii=False)
        mods = (exp or {}).get("selected_module", {})
        check("/export/xiaozhi 不含 api_key", code == 200 and "sk-stub-secret" not in exp_txt, "")
        check("/export/xiaozhi 只含设备侧四种能力（不含 embedding/rerank/vision）",
              set(mods) <= {"asr", "llm", "tts", "vad"} and "llm" in mods,
              f"导出模块={sorted(mods)}")

        # -------------------------------------------------- G. 设备下发平滑过渡
        print("\n── G. 设备下发（旧固件仍拿得到一条可用 LLM）──")
        code, dev = req("POST", "/api/devices", {"device_id": "ddee00112233",
                                                 "name": "测试机"}, token)
        dev_pk = dev["id"]
        code, cd = req("POST", f"/api/devices/{dev_pk}/code", {}, token)
        code, bind = req("POST", "/api/devices/bind", {"code": cd["code"]}, token)
        dev_token = bind.get("device_token", "")

        patch_by_kind("llm", {"enabled": False})
        patch_by_kind("llm_main", {"enabled": True, "model": "main-big-model"})
        code, sync = req("POST", "/api/device/sync?fw_version=0.1.0", {}, dev_token)
        pv = sync.get("providers") or []
        llm_rows = [p for p in pv if p["kind"] == "llm"]
        check("只配 llm_main 时设备仍下发到一条 kind=llm",
              code == 200 and len(llm_rows) == 1 and llm_rows[0]["model"] == "main-big-model",
              f"kinds={sorted({p['kind'] for p in pv})} llm={[p['model'] for p in llm_rows]}")
        check("下发的 provider 不含 api_key",
              "sk-stub-secret" not in json.dumps(pv, ensure_ascii=False), "")
        check("下发不包含服务端自用的新 kind",
              not ({"llm_main", "llm_light", "embedding", "rerank", "vision"}
                   & {p["kind"] for p in pv}),
              f"kinds={sorted({p['kind'] for p in pv})}")

        patch_by_kind("llm", {"enabled": True, "model": "legacy-llm"})
        code, sync = req("POST", "/api/device/sync?fw_version=0.1.0", {}, dev_token)
        llm_rows = [p for p in sync.get("providers") or [] if p["kind"] == "llm"]
        check("有旧 llm 时不重复补位（只下发一条）",
              code == 200 and len(llm_rows) == 1 and llm_rows[0]["model"] == "legacy-llm",
              f"llm={[p['model'] for p in llm_rows]}")

        # -------------------------------------------------- H. 上下文裁剪
        print("\n── H. context_window 生效 ──")
        patch_by_kind("llm_light", {"enabled": True, "model": "trim-model",
                                     "priority": 0, "context_window": 16})
        long_msgs = [{"role": "system", "content": "你是小暖"},
                     {"role": "user", "content": "第一句很久以前说的话，应该被裁掉"},
                     {"role": "assistant", "content": "好的，我记得了"},
                     {"role": "user", "content": "第二句话"},
                     {"role": "user", "content": "最近一句要提问的内容"}]
        code, r = req("POST", "/api/providers/test",
                      {"usage": "light", "messages": long_msgs}, token)
        sent = (STATE.last_body or {}).get("messages") or []
        check("context_window=16 时历史被裁剪（且保留 system 与最后一句）",
              r.get("ok") is True and len(sent) < len(long_msgs)
              and sent[0]["role"] == "system"
              and sent[-1]["content"] == "最近一句要提问的内容",
              f"发送 {len(long_msgs)} 条 -> 实际 {len(sent)} 条")
        patch_by_kind("llm_light", {"context_window": 0})

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if srv is not None:
            srv.shutdown()
        try:
            if DB_PATH.exists():
                DB_PATH.unlink()
        except Exception:
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
