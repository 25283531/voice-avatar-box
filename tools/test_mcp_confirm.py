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


#: 固件 ``WifiBoard::GetDeviceStatusJson()`` 的真实形状（原文见 wifi_board.cc:193）。
_STATUS_JSON = (
    '{"audio_speaker":{"volume":70},'
    '"screen":{"brightness":100,"theme":"light"},'
    '"battery":{"level":55,"charging":true},'
    '"network":{"type":"wifi","ssid":"XiaoZhi-XXXX","rssi":-60},'
    '"chip":{"temperature":25}}'
)


class FakeDevice:
    """按工具名回包的假设备，同时记录**调用顺序**。

    顺序是这组用例的核心：相对量（delta）必须先读状态再写，写反了就是
    「用上一次的旧值算」，老人连说两句「大一点」只会调上去一次。
    """

    def __init__(self, status: str | None = _STATUS_JSON,
                 set_result: str = "true", error: str | None = None,
                 set_timeout: bool = False):
        self.status = status
        self.set_result = set_result
        self.error = error
        self.set_timeout = set_timeout
        self.calls: list[tuple] = []

    def __call__(self, name, args=None, timeout=3.0):
        self.calls.append((name, dict(args or {}), timeout))
        if name == "self.get_device_status":
            if self.status is None:
                raise TimeoutError()
            return _text_reply(self.status)
        if self.set_timeout:
            raise TimeoutError()
        if self.error:
            return {"error": {"message": self.error}}
        return _text_reply(self.set_result)


def _text_reply(text: str, is_error: bool = False) -> dict:
    return {"jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": text}],
                       "isError": is_error}}


def t_device_controls() -> None:
    print("\n[7] 音量 / 亮度 / 设备状态：服务端工具 → 设备 MCP 工具")
    from app.voice.tools import TOOL_DEFS, execute_tool

    names = [d["function"]["name"] for d in TOOL_DEFS]
    check("三个设备能力工具都注册给 LLM 了",
          {"set_volume", "set_brightness", "get_device_status"} <= set(names),
          f"names={names}")
    check("★ 必须追加在工具表末尾（插中间会让 prompt 前缀缓存全失效）",
          names[-3:] == ["set_volume", "set_brightness", "get_device_status"],
          f"tail={names[-3:]}")

    # ---- 绝对量：一次往返，参数名必须与固件 Property 名逐字一致 ----
    dev = FakeDevice()
    r = execute_tool(None, 1, "set_volume", '{"level": 40}', device_call=dev)
    check("set_volume(level=40) 下发到固件工具 self.audio_speaker.set_volume",
          dev.calls and dev.calls[0][0] == "self.audio_speaker.set_volume",
          f"calls={dev.calls}")
    check("参数名是 volume（固件 Property('volume', ...) 拼错就 Unknown tool）",
          dev.calls and dev.calls[0][1] == {"volume": 40}, f"calls={dev.calls}")
    check("超时用 2.0 秒（小于 confirm_reminder 的 3.0，回调是毫秒级的）",
          dev.calls and dev.calls[0][2] == 2.0, f"calls={dev.calls}")
    check("绝对量只走一次往返，不先读状态",
          len(dev.calls) == 1, f"calls={dev.calls}")
    check("返回 ok 且带上调到的值", r.get("ok") is True and r.get("level") == 40,
          f"{r!r}")

    # ---- 相对量：先读状态再写，且算的是「当前 + delta」 ----
    dev = FakeDevice()
    r = execute_tool(None, 1, "set_volume", '{"delta": 20}', device_call=dev)
    check("相对量先调 self.get_device_status",
          dev.calls and dev.calls[0][0] == "self.get_device_status",
          f"calls={dev.calls}")
    check("相对量再调 set_volume，且是 70+20=90",
          len(dev.calls) == 2
          and dev.calls[1] == ("self.audio_speaker.set_volume",
                               {"volume": 90}, 2.0), f"calls={dev.calls}")
    check("相对量结果 ok", r.get("ok") is True and r.get("level") == 90, f"{r!r}")

    dev = FakeDevice()
    execute_tool(None, 1, "set_volume", '{"delta": -30}', device_call=dev)
    check("负数 delta 是调小（70-30=40）",
          dev.calls[-1][1] == {"volume": 40}, f"calls={dev.calls}")

    # ---- 读不到当前值：不许瞎猜，也不许乱写 ----
    dev = FakeDevice(status=None)
    r = execute_tool(None, 1, "set_volume", '{"delta": 20}', device_call=dev)
    check("读不到当前值时不发 set 请求（猜错方向会让老人越喊越小声）",
          all(c[0] != "self.audio_speaker.set_volume" for c in dev.calls),
          f"calls={dev.calls}")
    check("读不到当前值时让老人说个具体数值",
          r.get("ok") is False and "具体数值" in str(r.get("message", "")), f"{r!r}")

    # ---- 上下限必须服务端自己夹住 ----
    # 固件 Property("volume", Integer, 0, 100) 带 min/max，超范围会 throw
    # std::invalid_argument → 回 error。等固件兜底就等于「调不动还没提示」。
    dev = FakeDevice()
    execute_tool(None, 1, "set_volume", '{"level": 120}', device_call=dev)
    check("level 超过 100 被夹到 100", dev.calls[-1][1] == {"volume": 100},
          f"calls={dev.calls}")

    dev = FakeDevice()
    execute_tool(None, 1, "set_volume", '{"level": -5}', device_call=dev)
    check("level 小于 0 被夹到 0", dev.calls[-1][1] == {"volume": 0},
          f"calls={dev.calls}")

    dev = FakeDevice()
    execute_tool(None, 1, "set_volume", '{"level": "50%"}', device_call=dev)
    check("模型给 \"50%\" 也要认成 50", dev.calls[-1][1] == {"volume": 50},
          f"calls={dev.calls}")

    # bool 是 int 的子类：True 被当成 1 的话，老人说「调大点」音量会变 1
    dev = FakeDevice()
    r = execute_tool(None, 1, "set_volume", '{"level": true}', device_call=dev)
    check("level=true 不得被当成 1（bool 是 int 子类，必须挡住）",
          dev.calls == [] and r.get("ok") is False, f"calls={dev.calls} {r!r}")

    # ---- 亮度：工具名 / 参数名换一套 ----
    dev = FakeDevice()
    r = execute_tool(None, 1, "set_brightness", '{"level": 0}', device_call=dev)
    check("set_brightness 下发到 self.screen.set_brightness",
          dev.calls and dev.calls[0][0] == "self.screen.set_brightness",
          f"calls={dev.calls}")
    check("参数名是 brightness", dev.calls and dev.calls[0][1] == {"brightness": 0},
          f"calls={dev.calls}")
    check("亮度结果 ok", r.get("ok") is True and r.get("level") == 0, f"{r!r}")

    # 没背光的板子固件压根没注册这个工具 → 回 "Unknown tool"。
    # 这不是故障，不能让 LLM 反复重试。
    dev = FakeDevice(error="Unknown tool: self.screen.set_brightness")
    r = execute_tool(None, 1, "set_brightness", '{"delta": 20}', device_call=dev)
    check("设备没有该工具时判成「不支持」而不是「失败」",
          r.get("ok") is False and r.get("unsupported") is True, f"{r!r}")
    check("不支持时明确让模型别再试",
          "再试" in str(r.get("message", "")), f"{r!r}")

    # ---- 超时 / 没有通道 ----
    dev = FakeDevice(set_timeout=True)
    r = execute_tool(None, 1, "set_volume", '{"level": 30}', device_call=dev)
    check("设备超时 → ok=False 且不崩", r.get("ok") is False, f"{r!r}")
    check("超时时提示设备没回应",
          "没有回应" in str(r.get("message", "")), f"{r!r}")

    r = execute_tool(None, 1, "set_volume", '{"level": 30}')
    check("没有 device_call 时降级不抛异常", r.get("ok") is False, f"{r!r}")

    # ---- 设备状态：固件把 JSON 塞在 text 里，要再 loads 一次 ----
    dev = FakeDevice()
    r = execute_tool(None, 1, "get_device_status", "{}", device_call=dev)
    st = r.get("status") or {}
    check("get_device_status 能解出固件塞在 text 里的 JSON",
          r.get("ok") is True and st.get("volume") == 70, f"{r!r}")
    check("亮度也解出来了", st.get("brightness") == 100, f"{r!r}")
    check("电量与充电状态也解出来了",
          st.get("battery_level") == 55 and st.get("charging") is True, f"{r!r}")
    check("★ 不把 ssid / 芯片温度这类字段喂给模型（会被念成「rssi -60」）",
          not ({"ssid", "rssi", "temperature"} & set(st)), f"status={st}")

    dev = FakeDevice(status="这不是 JSON")
    r = execute_tool(None, 1, "get_device_status", "{}", device_call=dev)
    check("状态不是 JSON 时 ok=False 而不是崩", r.get("ok") is False, f"{r!r}")


async def main() -> int:
    print("=== 服务端 → 设备 MCP 工具通道 / 服药确认 单测 ===")
    await t_roundtrip_true()
    await t_timeout()
    await t_string_id_ignored()
    await t_out_of_order()
    t_tool_mapping()
    t_execute_tool_wiring()
    t_device_controls()

    print(f"\n=== {_g_pass} 通过 / {_g_fail} 失败 ===")
    if _failures:
        print("失败项：")
        for f in _failures:
            print(f"  - {f}")
    return 1 if _g_fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
