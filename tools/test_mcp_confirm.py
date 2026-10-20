"""服务端 → 设备 MCP 工具通道 + 服药确认的单测（不烧板子、不连真设备）。

为什么单独一个文件
------------------
这条链路横跨三层，任何一层错位，现象都长成同一句话：「设备没反应」——

  1. ``VoiceSession.call_device_tool``  发 JSON-RPC ``tools/call``，按 id 等回包
  2. ``VoiceSession._on_json`` 的 mcp 分支  把回包塞回等待中的 Future
  3. ``tools._confirm_reminder``  把设备结果翻译成 LLM 能用的结果字典

所以这里用**假 WS** 把三层一起跑通：假 WS 收到 tools/call 后，像固件那样
构造一条 ``{"type":"mcp","payload":{"id":N,"result":...}}`` 回灌给
``_on_json``。真正的收包循环（``run()`` 里的 while）不参与，但**消息形状
和 id 类型与固件完全一致**（见 ``main/mcp_server.cc`` 的 ``ReplyResult``）。

钉死的几件事
------------
* id **必须是整数**：固件 ``ParseMessage`` 用 ``cJSON_IsNumber`` 校验 id，
  字符串 id 会被直接丢弃 —— 服务端只会看到超时。这条用「设备故意回字符串
  id」来反向验证，防止以后有人把 id 改成 uuid。
* 超时和「设备答 false」**必须区分**：前者要让老人改按按键，后者是正常业务
  结果（老人顺口说「吃过了」而已）。混成一个会让 LLM 对老人说「失败了」。
* 只改数据库不算确认：``_confirm_reminder`` 全程不碰 db（参数留着但没用），
  状态以设备回执为准。这条靠代码结构保证，这里用 ``db=None`` 反证。

跑法
----
    python tools/test_mcp_confirm.py
退出码 0 = 全绿。CI 的 server-ci job 会跑它。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

# 导入 app.* 需要的最小环境（不连真库：sqlite 落在临时目录）
_TMP = Path(tempfile.mkdtemp(prefix="mcp-confirm-"))
os.environ.setdefault("ELDER_DB", f"sqlite:///{(_TMP / 'test.db').as_posix()}")
os.environ.setdefault("ELDER_JWT_SECRET", "mcp-confirm-secret-0123456789abcdef")
os.environ.setdefault("ELDER_SCHEDULER_INTERVAL", "3600")   # 不起后台调度
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_k, None)

_g_pass = 0
_g_fail = 0
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global _g_pass, _g_fail
    if ok:
        _g_pass += 1
        print(f"  [OK]   {name}")
    else:
        _g_fail += 1
        _failures.append(name)
        print(f"  [FAIL] {name}" + (f"  —— {detail}" if detail else ""))


# ================================================================ 假设备
class FakeDeviceWS:
    """假 WS。``responder`` 决定设备怎么回包（None = 永远不回，模拟掉线）。

    ``send_text`` 是 async（真 WS 也是），回包通过 ``_on_json`` 回灌，
    并且用 ``call_soon`` 制造一次真实的挂起 —— 否则 Future 可能在
    ``wait_for`` 之前就已完成，测不出等待逻辑。
    """

    def __init__(self, responder=None):
        self.responder = responder
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        obj = json.loads(text)
        self.sent.append(obj)
        if self.responder is None:
            return
        payload = obj.get("payload") or {}
        reply = self.responder(payload)
        if reply is None:
            return
        loop = asyncio.get_running_loop()
        loop.call_soon(lambda: asyncio.ensure_future(self._deliver(reply)))

    async def _deliver(self, reply: dict) -> None:
        await self.session._on_json(json.dumps(reply, ensure_ascii=False))

    async def close(self, code: int = 1000) -> None:   # pragma: no cover
        pass


def make_session(responder=None):
    """造一个只差「收包循环」的会话对象。"""
    from app.voice.session import VoiceSession

    ws = FakeDeviceWS(responder)
    s = VoiceSession(ws, protocol_version=1)
    s.session_id = "test-mcp"
    ws.session = s
    return s, ws


def ok_reply(payload: dict, text: str = "true") -> dict:
    """仿固件 ``McpTool::Call`` 的成功回包（bool 结果序列化成字符串）。"""
    return {
        "type": "mcp",
        "payload": {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "result": {"content": [{"type": "text", "text": text}], "isError": False},
        },
    }


# ================================================================ 第 1 组
async def t_roundtrip_true() -> None:
    print("\n[1] 下发 tools/call → 设备答 true → 服务端拿到结果")

    def responder(payload):
        return ok_reply(payload, "true")

    session, ws = make_session(responder)
    out = await session.call_device_tool("self.elder.confirm_reminder", {}, 2.0)

    check("只发了一条消息", len(ws.sent) == 1, f"实际 {len(ws.sent)} 条")
    msg = ws.sent[0] if ws.sent else {}
    check("外层 type 是 mcp（固件 application.cc 按它分流）", msg.get("type") == "mcp")
    p = msg.get("payload") or {}
    check("jsonrpc 是 2.0（固件 ParseMessage 第一道校验）", p.get("jsonrpc") == "2.0")
    check("★id 是整数（固件用 cJSON_IsNumber 校验，字符串 id 会被丢弃）",
          isinstance(p.get("id"), int), f"实际类型 {type(p.get('id')).__name__}")
    check("method 是 tools/call", p.get("method") == "tools/call")
    check("工具名是 self.elder.confirm_reminder",
          (p.get("params") or {}).get("name") == "self.elder.confirm_reminder")
    check("arguments 是对象（固件要求 cJSON_IsObject）",
          isinstance((p.get("params") or {}).get("arguments"), dict))
    check("回包被正确唤醒", isinstance(out, dict) and out.get("id") == p.get("id"),
          f"拿到 {out!r}")
    check("等待表已清空（不泄漏 Future）", session._mcp_pending == {},
          f"残留 {list(session._mcp_pending)}")


async def t_timeout() -> None:
    print("\n[2] 设备不回包 → 必须抛 TimeoutError（不能静默返回 None）")

    session, _ = make_session(None)          # responder=None：永不回
    raised = None
    try:
        await session.call_device_tool("self.elder.confirm_reminder", {}, 0.2)
    except TimeoutError as exc:
        raised = exc
    except Exception as exc:  # noqa: BLE001
        raised = exc
    check("超时抛 TimeoutError", isinstance(raised, TimeoutError),
          f"抛的是 {type(raised).__name__}")
    check("超时后等待表也要清空", session._mcp_pending == {},
          f"残留 {list(session._mcp_pending)}")


async def t_string_id_ignored() -> None:
    print("\n[3] 设备回字符串 id → 不得唤醒（固件不会这么回，但别把 id 改成 uuid）")

    def responder(payload):
        r = ok_reply(payload, "true")
        r["payload"]["id"] = str(payload["id"])     # 故意改成字符串
        return r

    session, _ = make_session(responder)
    raised = None
    try:
        await session.call_device_tool("self.elder.confirm_reminder", {}, 0.3)
    except TimeoutError as exc:
        raised = exc
    check("字符串 id 的回包不算数（按整数 id 建索引）",
          isinstance(raised, TimeoutError), f"抛的是 {type(raised).__name__}")


async def t_out_of_order() -> None:
    print("\n[4] 两条并发请求按 id 各归各位（不能串包）")

    def responder(payload):
        # 让 id 小的那条回 false、id 大的回 true
        return ok_reply(payload, "false" if payload["id"] == 1 else "true")

    session, _ = make_session(responder)
    a, b = await asyncio.gather(
        session.call_device_tool("self.elder.confirm_reminder", {}, 2.0),
        session.call_device_tool("self.elder.confirm_reminder", {}, 2.0),
    )
    check("第 1 条拿到 false",
          (a.get("result") or {}).get("content", [{}])[0].get("text") == "false",
          f"{a!r}")
    check("第 2 条拿到 true",
          (b.get("result") or {}).get("content", [{}])[0].get("text") == "true",
          f"{b!r}")
    check("并发后等待表清空", session._mcp_pending == {},
          f"残留 {list(session._mcp_pending)}")


# ================================================================ 第 2 组
def t_tool_mapping() -> None:
    print("\n[5] tools._confirm_reminder 把设备结果翻译成 LLM 话术")
    from app.voice.tools import _confirm_reminder, TOOL_DEFS

    names = [d["function"]["name"] for d in TOOL_DEFS]
    check("confirm_reminder 已注册给 LLM", "confirm_reminder" in names,
          f"实际 {names}")
    check("原有四个工具没丢",
          {"create_reminder", "list_reminders", "delete_reminder",
           "toggle_reminder"} <= set(names), f"实际 {names}")

    def reply(text):
        return {"jsonrpc": "2.0", "id": 1,
                "result": {"content": [{"type": "text", "text": text}],
                           "isError": False}}

    # db=None 是刻意的：这条路径**不许**依赖数据库改状态
    r = _confirm_reminder(None, 1, lambda n, a, t: reply("true"))
    check("设备答 true → confirmed=True", r.get("ok") and r.get("confirmed") is True,
          f"{r!r}")

    r = _confirm_reminder(None, 1, lambda n, a, t: reply("false"))
    check("设备答 false → ok 但仍算成功（不是错误）",
          r.get("ok") is True and r.get("confirmed") is False, f"{r!r}")
    check("答 false 时提示「不要说失败」",
          "不要说失败" in str(r.get("message", "")), f"{r!r}")

    def boom(n, a, t):
        raise TimeoutError()

    r = _confirm_reminder(None, 1, boom)
    check("超时 → 让老人按设备上的按键", "按键" in str(r.get("message", "")), f"{r!r}")
    check("超时 → ok=False（LLM 才知道没办成）", r.get("ok") is False, f"{r!r}")

    r = _confirm_reminder(None, 1, None)
    check("没有设备通道 → 也要给按键兜底话术",
          r.get("ok") is False and "按键" in str(r.get("message", "")), f"{r!r}")

    r = _confirm_reminder(None, 1, lambda n, a, t: {"error": {"message": "boom"}})
    check("设备回 error → ok=False", r.get("ok") is False, f"{r!r}")

    r = _confirm_reminder(None, 1, lambda n, a, t: {
        "result": {"content": [{"type": "text", "text": "x"}], "isError": True}})
    check("isError=True → ok=False", r.get("ok") is False, f"{r!r}")

    r = _confirm_reminder(None, 1, lambda n, a, t: "不是字典")
    check("设备回了看不懂的东西 → ok=False 而不是崩",
          r.get("ok") is False, f"{r!r}")


def t_execute_tool_wiring() -> None:
    print("\n[6] execute_tool 把 device_call 正确转交给 confirm_reminder")
    from app.voice.tools import execute_tool

    seen: list[tuple] = []

    def device_call(name, args=None, timeout=3.0):
        seen.append((name, args, timeout))
        return {"result": {"content": [{"type": "text", "text": "true"}],
                           "isError": False}}

    r = execute_tool(None, 1, "confirm_reminder", "{}", device_call=device_call)
    check("真的下发了设备工具调用", seen == [("self.elder.confirm_reminder", {}, 3.0)],
          f"实际 {seen!r}")
    check("结果 ok", r.get("ok") is True, f"{r!r}")

    # 不带 device_call（老调用点 / 单测）不能抛异常
    r = execute_tool(None, 1, "confirm_reminder", "{}")
    check("缺 device_call 时降级成按键提示，不抛异常",
          r.get("ok") is False and "按键" in str(r.get("message", "")), f"{r!r}")


async def main() -> int:
    print("=== 服务端 → 设备 MCP 工具通道 / 服药确认 单测 ===")
    await t_roundtrip_true()
    await t_timeout()
    await t_string_id_ignored()
    await t_out_of_order()
    t_tool_mapping()
    t_execute_tool_wiring()

    print(f"\n=== {_g_pass} 通过 / {_g_fail} 失败 ===")
    if _failures:
        print("失败项：")
        for f in _failures:
            print(f"  - {f}")
    return 1 if _g_fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
