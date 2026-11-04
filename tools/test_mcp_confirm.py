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
* 只改数据库不算确认：状态以设备回执为准，``_confirm_reminder`` 不会因为自己
  写了一次库就宣布成功。第 [8] 组反向守住这一点 —— 库里"刚刚确认过"的记录
  只能用来**回答"是不是已经办完了"**，不能用来替代一次真正的设备确认。
  这也是为什么回看窗口必须短（<= 15 分钟）：窗口一长，它就会把"老人其实
  还没吃药"静默吞掉，那比多问一句危险得多。

跑法
----
    python tools/test_mcp_confirm.py
退出码 0 = 全绿。CI 的 server-ci job 会跑它。
"""

from __future__ import annotations

import asyncio
import inspect
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
    # ⚠️ 2026-11-04：超时从 3.0 提到 8.0。判据不能只比"等于某个数"，
    # 必须钉住**为什么是这个数**：设备侧处理 confirm_reminder 是同步阻塞的
    # （sync.c 的 flush_queue → esp_http_client_perform），实测 3.5 秒，
    # 而设备自己的 HTTP 超时是 CONFIG_ELDER_HTTP_TIMEOUT_MS=8000。
    # 服务端比设备先放弃 ⇒ 每次都输 0.5 秒 ⇒ 老人听到"请按一下按键"（他刚按过）。
    from app.voice.tools import _DEVICE_CALL_TIMEOUT_SEC
    check("confirm_reminder 等设备回包的时间 ≥ 设备自己的 HTTP 超时（8s）",
          _DEVICE_CALL_TIMEOUT_SEC >= 8.0,
          f"实际 {_DEVICE_CALL_TIMEOUT_SEC}s；小于 8 就会稳定输给设备的同步回执（3.5s）")
    check("真的下发了设备工具调用",
          seen == [("self.elder.confirm_reminder", {}, _DEVICE_CALL_TIMEOUT_SEC)],
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


def t_recent_confirm() -> None:
    """「刚刚已经确认过」的回查 —— 2026-11-04 用户实报「按键按了不能确认」的根因防线。

    现场（生产日志逐行核对）：
      09:14:07 老人说「确认。」，待确认清单里有那条
      09:14:08 大模型开始生成，首字卡了 **65.4 秒**
      09:15:12 这 65 秒里老人自己长按了按键，设备把那条写成 CONFIRMED
      09:15:13 服务端才执行工具，清单空了 ⇒ 退回设备旧路径
      09:15:16 设备正忙着同步 POST 回执 ⇒ 超时
      09:15:18 模型说「您按一下设备上的按键」—— 他 6 秒前刚按完

    所以这里钉两件事：
      1. 清单为空但 15 分钟内有确认 ⇒ 直接回「已经记上了」，**不调设备**；
      2. 设备超时但 15 分钟内有确认 ⇒ 按成功处理，**不让老人重按**。
    两个都必须有负向对照（40 分钟前的旧确认不许被当成"刚刚"），
    否则"回查"会变成把所有失败都吞掉。
    """
    print("\n[8] 清单为空 / 设备超时 ⇒ 回查「刚刚确认过」，不让老人重按")
    from datetime import timedelta

    from app import models
    from app.db import SessionLocal, init_db
    from app.schedule import local_now
    from app.voice.tools import (_RECENT_CONFIRM_MIN, _confirm_reminder,
                                 _recently_confirmed_medicine)

    init_db()
    db = SessionLocal()
    try:
        # 干净起点：这个测试库是本进程独占的临时文件，清掉上次运行的行。
        db.query(models.ReminderEvent).delete()
        db.query(models.Reminder).delete()
        db.query(models.Device).delete()
        db.commit()

        dev = models.Device(device_id="aabbccdd0011", name="测试设备")
        db.add(dev)
        db.commit()
        pk = int(dev.id)

        rule = models.Reminder(
            device_id=pk, title="新的服药提醒", rtype=int(models.ReminderType.MEDICINE),
            require_confirm=True, enabled=True,
            times_of_day='["09:00"]', weekdays="[]", month_days="[]",
            medicines="[]", tz="Asia/Shanghai", tz_offset_min=0,
        )
        db.add(rule)
        db.commit()

        def add_event(minutes_ago: int, method: int) -> int:
            now = local_now()
            ev = models.ReminderEvent(
                reminder_id=rule.id, device_id=pk,
                due_at=now - timedelta(minutes=minutes_ago),
                fired_at=now - timedelta(minutes=minutes_ago),
                confirmed_at=now - timedelta(minutes=minutes_ago),
                state=int(models.EventState.CONFIRMED),
                confirm_method=method, tz="Asia/Shanghai", tz_offset_min=0,
            )
            db.add(ev)
            db.commit()
            return int(ev.id)

        # ---- ① 15 分钟内的按键确认：必须被认出来 ----
        ev_id = add_event(3, int(models.ConfirmMethod.BUTTON))
        hit = _recently_confirmed_medicine(db, pk)
        check("3 分钟前的按键确认能被回查到",
              hit is not None and int(hit[0].id) == ev_id, f"hit={hit!r}")
        check("回查给出的分钟数是 3",
              hit is not None and hit[2] == 3, f"mins={hit and hit[2]}")

        # ---- ② 清单为空 + 有近期确认 ⇒ 不调设备，直接说"已经记上了" ----
        called: list[tuple] = []

        def must_not_call(name, args, timeout):
            called.append((name, args, timeout))
            raise AssertionError("不该调设备：刚刚已经确认过了")

        r = _confirm_reminder(db, pk, must_not_call, {"medicine": ""})
        check("清单为空时不再调设备", called == [], f"called={called}")
        check("结果标成 already=True", r.get("already") is True, f"{r!r}")
        check("结果标成 confirmed=True", r.get("confirmed") is True, f"{r!r}")
        check("话术里说「已经确认」，不再让老人按键",
              "已经确认" in str(r.get("message", ""))
              and "按一下设备上的按键" not in str(r.get("message", "")),
              f"{r.get('message')!r}")
        check("话术里带上确认方式（按键）",
              "按键" in str(r.get("message", "")), f"{r.get('message')!r}")

        # ---- ③ 负向对照：40 分钟前的旧确认不许被当成"刚刚" ----
        db.query(models.ReminderEvent).delete()
        db.commit()
        add_event(40, int(models.ConfirmMethod.BUTTON))
        check("40 分钟前的确认不算「刚刚」",
              _recently_confirmed_medicine(db, pk) is None)

        # 这条旧确认存在时，清单为空仍要走设备旧路径（不能吞掉真正的确认）
        called2: list[tuple] = []

        def fake_ok(name, args, timeout):
            called2.append((name, args, timeout))
            return {"jsonrpc": "2.0", "id": 1,
                    "result": {"content": [{"type": "text", "text": "true"}],
                               "isError": False}}

        r2 = _confirm_reminder(db, pk, fake_ok, {"medicine": ""})
        check("旧确认不算数时仍然会调设备", len(called2) == 1, f"called={called2}")
        check("走设备路径时不带 already 标记", not r2.get("already"), f"{r2!r}")

        # ---- ④ 设备超时 + 回执在等待期间落库 ⇒ 按成功处理 ----
        #
        # ⚠️ 这里的假设备**必须先写库再超时**，不能只是 raise TimeoutError。
        # 只 raise 的话，``_confirm_reminder`` 上面那次回查就已经命中并 return 了，
        # 根本走不到 ``_confirm_device_pending`` 的超时分支 —— 用例看起来是绿的，
        # 实际一条断言都没覆盖到（2026-11-04 用 M3 变异实测确认过这一点）。
        # 真实场景也正是这样：设备侧 POST 回执会自己把库改成 CONFIRMED，
        # 而 tools/call 的回包没赶上（net.c 的同步 POST 实测 3.5 秒）。
        db.query(models.ReminderEvent).delete()
        db.commit()

        def timeout_but_receipt_lands(name, args, timeout):
            add_event(0, int(models.ConfirmMethod.BUTTON))
            raise TimeoutError("设备 8 秒没回包")

        r3 = _confirm_reminder(db, pk, timeout_but_receipt_lands, {"medicine": ""})
        check("设备超时但回执已落库 ⇒ ok=True", r3.get("ok") is True, f"{r3!r}")
        check("设备超时但回执已落库 ⇒ confirmed=True",
              r3.get("confirmed") is True, f"{r3!r}")
        check("设备超时但回执已落库 ⇒ 不让老人重按",
              "按一下设备上的按键" not in str(r3.get("message", "")),
              f"{r3.get('message')!r}")
        check("设备超时但回执已落库 ⇒ 带上 already 标记",
              r3.get("already") is True, f"{r3!r}")

        # ---- ⑤ 负向对照：超时 + 没有近期确认 ⇒ 仍然要说"设备没响应" ----
        db.query(models.ReminderEvent).delete()
        db.commit()

        def timeout_call(name, args, timeout):
            raise TimeoutError("设备 8 秒没回包")

        r4 = _confirm_reminder(db, pk, timeout_call, {"medicine": ""})
        check("超时且没有近期确认 ⇒ ok=False", r4.get("ok") is False, f"{r4!r}")
        check("超时且没有近期确认 ⇒ 提示设备没响应",
              "没有响应" in str(r4.get("message", "")), f"{r4.get('message')!r}")

        # ---- ⑥ 窗口常量必须短：太长会把"真的还没吃药"静默吞掉 ----
        check("回看窗口 <= 30 分钟（再长就会吞掉真正需要再确认的场景）",
              0 < _RECENT_CONFIRM_MIN <= 30, f"_RECENT_CONFIRM_MIN={_RECENT_CONFIRM_MIN}")
    finally:
        db.close()

def t_whole_event_confirm() -> None:
    """「确认」必须能落库，但**不许**替老人报"都吃了" —— 医疗安全底线。

    现场（2026-11-04 生产库 + 日志逐行核对）：

      ev=125 rmd=5「测试」 due=12:43 fired=16:19:26 state=3(MISSED)
              conf_at=None note='timeout' meds=['降压药','降脂药']
      pending_medicine_events -> []            ← 空的
      16:21:50 识别：确认。
      16:21:55 tool=0ms                        ← confirm_reminder 一次没调
      16:21:58 播报完成（1 句）
      （没有任何「兜底确认跳过」日志 ⇒ 兜底在第一道闸就返回了）

    两个独立的洞，缺一个都还是"喊半天没反应"：

      A. ``pending_medicine_events`` 只认 PENDING/FIRED，而 scheduler 在
         ``due + 30min`` 就把实例落成 MISSED，固件屏幕却还挂 4 小时
         （``ui.c`` 的 ``ELDER_UI_REMINDER_HOLD_US``）。第 30 分钟起服务端
         眼里就没这条提醒了 —— system 里没有清单、兜底在第一道闸返回。
      B. ``_CONFIRM_RE`` 里没有「确认」两个字，``match_medicine("确认", …)``
         也返回空 ⇒ 设备问「您刚才已经服用过降压药了吗？」，老人答「确认」，
         服务端当成普通闲聊。

    **但用户明确裁定（2026-11-04）：「第4条必须问清楚。医疗安全不容忽视。」**
    所以「确认」只解决 B（让这句话被当成一次确认），**不**解决"哪几味"：
    挂两味药时只答「确认」必须转追问，只有明确说「都吃了」才整条确认。

    这里钉死的判据（每条都要有负向对照，否则"放宽"会变成"什么都确认"）：
      1. MISSED 且屏幕窗口内 ⇒ 算待确认；超出 4 小时 ⇒ 不算；
      2. 「确认」+ 挂两味药 ⇒ **need_detail**，库里 state 不变；
      3. 「都吃了」+ 挂两味药 ⇒ 整条确认，并**留痕**；
      4. 「确认」+ 只挂一味药 ⇒ 整条确认（没什么可问的）；
      5. 多条候选 ⇒ 一律拒绝（"确认错提醒"是真实风险，一个字不许松）；
      6. 「好的 / 行 / 知道了 / 好像吃了」⇒ 一律不算确认。
    """
    print("\n[9] 「确认」能落库 / 多味药必须问清 / MISSED 仍在屏幕窗口内")
    from datetime import timedelta

    from app import models
    from app.db import SessionLocal, init_db
    from app.schedule import local_now
    from app.voice.tools import (_CONFIRM_WINDOW_AFTER_MIN, _confirm_reminder,
                                 is_all_medicines_yes, is_whole_event_yes,
                                 medicine_names, pending_medicine_events)
    from app.voice import session as vs

    # ---- ① 措辞判据：这是一次「确认」 ----
    for word in ("确认。", "确认", "对", "对的", "是的", "嗯", "没错", "吃了",
                 "吃过了", "都吃了", "全都吃了", "全部吃了", "两样都吃了",
                 "两种都吃了", "我已经全部吃过了"):
        check(f"「{word}」判为确认措辞", is_whole_event_yes(word) is True)

    # ---- ② 措辞判据：这是「都吃了」（比 ① 严，是它的真子集）----
    for word in ("都吃了", "都吃过了", "全都吃了", "全部吃了", "两样都吃了",
                 "两种都吃了", "两味都吃了", "一起吃了"):
        check(f"「{word}」判为「都吃了」", is_all_medicines_yes(word) is True)

    # ---- ③ 关键负向：「确认 / 吃了」**不是**「都吃了」----
    # 这一组一旦变红，说明有人把两层判据合并了 —— 那就是替老人谎报服药。
    for word in ("确认", "确认。", "确定", "对", "对的", "是的", "嗯", "没错",
                 "吃了", "吃过了", "吃完了", "已经吃了", "喝过了"):
        check(f"★「{word}」**不算**「都吃了」（不能整条确认多味药）",
              is_all_medicines_yes(word) is False)

    # ---- ④ 两层判据的包含关系必须是真子集 ----
    from app.voice.tools import _ALL_MEDICINES_YES, _WHOLE_YES
    check("★ _ALL_MEDICINES_YES 是 _WHOLE_YES 的真子集",
          _ALL_MEDICINES_YES < _WHOLE_YES,
          f"差集={_ALL_MEDICINES_YES - _WHOLE_YES}")

    # ---- ⑤ 措辞判据：负向（放宽最容易在这里出事）----
    for word in ("好的", "行", "知道了", "好像吃了", "应该吃了吧", "还没吃",
                 "你确认一下", "我确认一下", "降压药吃了", "",
                 "我刚才吃完饭把降压药吃了还有那个降脂药也吃了"):
        check(f"「{word}」不算确认措辞", is_whole_event_yes(word) is False)

    # ---- ⑥ 屏幕窗口常量必须与固件同源 ----
    check("过期后窗口是 4 小时（= 固件 ui.c 的 ELDER_UI_REMINDER_HOLD_US）",
          _CONFIRM_WINDOW_AFTER_MIN == 240, f"{_CONFIRM_WINDOW_AFTER_MIN}")

    init_db()
    db = SessionLocal()
    try:
        db.query(models.ReminderEvent).delete()
        db.query(models.Reminder).delete()
        db.query(models.Device).delete()
        db.commit()

        dev = models.Device(device_id="aabbccdd0022", name="整条确认设备")
        db.add(dev)
        db.commit()
        pk = int(dev.id)

        rule = models.Reminder(
            device_id=pk, title="测试",
            rtype=int(models.ReminderType.MEDICINE),
            require_confirm=True, enabled=True,
            times_of_day='["12:43"]', weekdays="[]", month_days="[]",
            medicines=[{"name": "降压药", "dose": "1片"},
                       {"name": "降脂药", "dose": "1片"}],
            tz="Asia/Shanghai", tz_offset_min=0,
        )
        db.add(rule)
        db.commit()

        # 只挂一味药的规则，用来验「没什么可问的就直接确认」。
        rule1 = models.Reminder(
            device_id=pk, title="单味药",
            rtype=int(models.ReminderType.MEDICINE),
            require_confirm=True, enabled=True,
            times_of_day='["12:44"]', weekdays="[]", month_days="[]",
            medicines=[{"name": "阿司匹林", "dose": "1片"}],
            tz="Asia/Shanghai", tz_offset_min=0,
        )
        db.add(rule1)
        db.commit()

        def add_ev(rule_obj, state: int, due_min_ago: int,
                   fired_min_ago: int | None = None,
                   note: str = "") -> int:
            now = local_now()
            ev = models.ReminderEvent(
                reminder_id=rule_obj.id, device_id=pk,
                due_at=now - timedelta(minutes=due_min_ago),
                fired_at=(None if fired_min_ago is None
                          else now - timedelta(minutes=fired_min_ago)),
                state=int(state), note=note,
                tz="Asia/Shanghai", tz_offset_min=0,
            )
            db.add(ev)
            db.commit()
            return int(ev.id)

        def clear() -> None:
            db.query(models.ReminderEvent).delete()
            db.commit()

        calls: list[tuple] = []

        def fake_ok(name, args, timeout):
            calls.append((name, args, timeout))
            return {"jsonrpc": "2.0", "id": 1,
                    "result": {"content": [{"type": "text", "text": "true"}],
                               "isError": False}}

        # ---- ⑦ 核心：MISSED + 屏幕窗口内 ⇒ 仍是待确认候选 ----
        clear()
        ev_id = add_ev(rule, models.EventState.MISSED, 60, 40)
        rows = pending_medicine_events(db, pk)
        check("★ MISSED 且 4 小时内 ⇒ 仍算待确认（这就是 ev=125 那条）",
              [int(e.id) for e, _ in rows] == [ev_id],
              f"rows={[(e.id, e.state) for e, _ in rows]}")
        label = vs._pending_reminder_label(pk)
        check("★ 待确认清单非空 ⇒ system 里会注入提醒（模型才知道要调工具）",
              bool(label), f"label={label!r}")
        check("清单里两味药都列出来了（逐味列出，不是「两种药」）",
              all(n in label for n in medicine_names(rule)), f"label={label!r}")

        # ---- ⑧ 负向：超出 4 小时 ⇒ 屏幕早收了，不再算待确认 ----
        clear()
        add_ev(rule, models.EventState.MISSED, 5 * 60, 5 * 60)
        check("★ MISSED 且超出 4 小时 ⇒ 不算待确认（屏幕已经收了）",
              pending_medicine_events(db, pk) == [])

        # ---- ⑨ 负向：CONFIRMED / SKIPPED 永远不算待确认 ----
        clear()
        add_ev(rule, models.EventState.CONFIRMED, 30, 30)
        check("CONFIRMED 不算待确认", pending_medicine_events(db, pk) == [])
        clear()
        add_ev(rule, models.EventState.SKIPPED, 30, 30)
        check("SKIPPED 不算待确认", pending_medicine_events(db, pk) == [])

        # ---- ⑩ 兜底判据：多味药只有「都吃了」才放行 ----
        clear()
        add_ev(rule, models.EventState.MISSED, 60, 40)
        ok_plain, why_plain = vs._fallback_confirm_verdict(pk, all_medicines=False)
        check("★ 多味药 + 只说「确认」⇒ 兜底拒绝（医疗安全：不许替老人报全部）",
              ok_plain is False and "味药" in why_plain, f"why={why_plain!r}")
        ok_yes, why_yes = vs._fallback_confirm_verdict(pk, all_medicines=True)
        check("★ 多味药 + 明确「都吃了」⇒ 兜底放行", ok_yes is True,
              f"why={why_yes!r}")

        # ---- ⑪ 多条候选 ⇒ 兜底一律拒绝（这一条一个字都不许松）----
        rule2 = models.Reminder(
            device_id=pk, title="第二条",
            rtype=int(models.ReminderType.MEDICINE),
            require_confirm=True, enabled=True,
            times_of_day='["12:50"]', weekdays="[]", month_days="[]",
            medicines=[{"name": "二甲双胍", "dose": "0.5g"}],
            tz="Asia/Shanghai", tz_offset_min=0,
        )
        db.add(rule2)
        db.commit()
        add_ev(rule2, models.EventState.MISSED, 50, 45)
        ok_multi, why_multi = vs._fallback_confirm_verdict(pk, all_medicines=True)
        check("★ 两条候选 + 「都吃了」⇒ 兜底仍拒绝（确认错对象是真风险）",
              ok_multi is False and "2 条" in why_multi, f"why={why_multi!r}")
        db.query(models.Reminder).filter(
            models.Reminder.id == rule2.id).delete()
        clear()

        # ---- ⑫ 工具路径：「确认」+ 两味药 ⇒ **必须转追问**，不许落库 ----
        ev_id = add_ev(rule, models.EventState.MISSED, 60, 40, note="timeout")
        r = _confirm_reminder(db, pk, fake_ok, {"medicine": "确认"})
        check("★ medicine=「确认」+ 两味药 ⇒ confirmed 不为 True",
              r.get("confirmed") is not True, f"{r!r}")
        check("★ 结果是 need_detail（让模型问是哪一味）",
              r.get("need_detail") is True, f"{r!r}")
        check("★ 候选药名原样回给模型（它才能念给老人听）",
              set(r.get("candidates") or []) == {"降压药", "降脂药"}, f"{r!r}")
        check("话术里点名了两味药、并要求问清",
              "降压药" in str(r.get("message", ""))
              and "降脂药" in str(r.get("message", "")),
              f"{r.get('message')!r}")
        check("★ 一个字都不许落库：库里的 state 仍是 MISSED",
              int(db.get(models.ReminderEvent, ev_id).state)
              == int(models.EventState.MISSED),
              f"state={db.get(models.ReminderEvent, ev_id).state}")
        check("★ 没确认就不许通知设备收屏（屏幕还得挂着让他回答）",
              calls == [], f"calls={calls}")

        # ---- ⑬ 「确认」+ 只挂一味药 ⇒ 直接确认（没什么可问的）----
        clear()
        ev1 = add_ev(rule1, models.EventState.MISSED, 60, 40)
        r = _confirm_reminder(db, pk, fake_ok, {"medicine": "确认"})
        check("★ 单味药 + 「确认」⇒ 直接确认", r.get("confirmed") is True, f"{r!r}")
        db.expire_all()
        check("单味药确认后 state 落 CONFIRMED",
              int(db.get(models.ReminderEvent, ev1).state)
              == int(models.EventState.CONFIRMED))
        check("单味药不需要「整条确认」留痕（本来就只有一味）",
              models.WHOLE_EVENT_CONFIRM_TAG
              not in str(db.get(models.ReminderEvent, ev1).note or ""),
              f"note={db.get(models.ReminderEvent, ev1).note!r}")

        # ---- ⑭ 「都吃了」+ 两味药 ⇒ 整条确认并留痕 ----
        clear()
        calls.clear()
        ev_id = add_ev(rule, models.EventState.MISSED, 60, 40, note="timeout")
        r = _confirm_reminder(db, pk, fake_ok, {"medicine": "都吃了"})
        check("★ medicine=「都吃了」+ 两味药 ⇒ 确认成功",
              r.get("confirmed") is True, f"{r!r}")
        db.expire_all()
        ev = db.get(models.ReminderEvent, ev_id)
        check("★ 库里的 state 落成 CONFIRMED",
              int(ev.state) == int(models.EventState.CONFIRMED),
              f"state={ev.state}")
        check("两味药都记进了 confirmed_medicines",
              set(ev.confirmed_medicines or []) == {"降压药", "降脂药"},
              f"{ev.confirmed_medicines!r}")
        check("★ note 里留痕「整条确认（未逐味点名）」—— 不许假装是逐味确认",
              models.WHOLE_EVENT_CONFIRM_TAG in str(ev.note or ""),
              f"note={ev.note!r}")
        check("★ 留痕**追加**而不是覆盖（timeout 是审计线索，抹掉就查不出"
              "这条曾经超时升级过）",
              "timeout" in str(ev.note or "")
              and models.WHOLE_EVENT_CONFIRM_TAG in str(ev.note or ""),
              f"note={ev.note!r}")
        check("确认后通知了设备收屏（server_id = reminders.id）",
              calls and calls[0][1] == {"server_id": int(rule.id)},
              f"calls={calls}")

        # ---- ⑮ 负向：多味药 + 「好像吃了」⇒ 不许整条确认 ----
        clear()
        ev_id2 = add_ev(rule, models.EventState.MISSED, 60, 40)
        r2 = _confirm_reminder(db, pk, fake_ok, {"medicine": "好像吃了"})
        check("★ 「好像吃了」不许整条确认", r2.get("confirmed") is not True,
              f"{r2!r}")
        db.expire_all()
        check("库里的 state 没被改成 CONFIRMED",
              int(db.get(models.ReminderEvent, ev_id2).state)
              != int(models.EventState.CONFIRMED))

        # ---- ⑯ 接线：_run_turn / 兜底 必须用上新判据 ----
        src = inspect.getsource(vs.VoiceSession._run_turn)
        check("★ _run_turn 的确认判据用上了 is_whole_event_yes",
              "is_whole_event_yes(" in src, "判据没接上 ⇒ 「确认」永远不落库")
        src_fb = inspect.getsource(vs.VoiceSession._maybe_confirm_fallback)
        check("★ 兜底确认用的是 is_all_medicines_yes（不是 is_whole_event_yes）",
              "is_all_medicines_yes(utterance)" in src_fb
              and "is_whole_event_yes(utterance)" not in src_fb,
              "用错判据 ⇒ 「确认」也会整条确认多味药，医疗安全事故")
        src_tool = inspect.getsource(_confirm_reminder)
        check("★ 工具路径也要求 all_medicines 才整条确认",
              "all_medicines" in src_tool
              and "if len(names_here) <= 1 or all_medicines:" in src_tool)

        # ---- ⑰ 设备回执不许抹掉留痕 / 不许把已确认翻回去 ----
        #
        # 固件每条 CONFIRMED 回执都带 note="confirmed"（sync.c），而
        # report_event 的 note 是**覆盖**语义 ⇒ 不挡的话上面刚写下的
        # 「整条确认（未逐味点名）」会立刻被冲掉，家属端再也看不出粒度。
        # 同理，断网期间进 NVS 队列的旧 MISSED 回执会在联网后补发，
        # 无条件覆盖就把记录翻回"漏服"，而屏幕和灯已经收了。
        from app.routers.device_api import report_event
        from app.schemas import EventReportIn

        # 重新造一条"刚被整条确认过"的实例：上面清过表，``ev_id`` 会被
        # SQLite 重新分配给新行（自增列在空表上从 1 起），直接复用会指到
        # 一条没有留痕的实例上 —— 那样断言红得毫无意义。
        clear()
        ev_id = add_ev(rule, models.EventState.MISSED, 60, 40, note="timeout")
        _confirm_reminder(db, pk, fake_ok, {"medicine": "都吃了"})
        db.expire_all()
        ev = db.get(models.ReminderEvent, ev_id)
        check("（前置）这条实例已带上留痕",
              models.WHOLE_EVENT_CONFIRM_TAG in str(ev.note or ""),
              f"note={ev.note!r}")
        due = ev.due_at
        report_event(EventReportIn(reminder_id=int(rule.id), due_at=due,
                                   state=int(models.EventState.CONFIRMED),
                                   confirm_method=int(models.ConfirmMethod.BUTTON),
                                   note="confirmed"),
                     db=db, dev=dev)
        db.expire_all()
        ev = db.get(models.ReminderEvent, ev_id)
        check("★ 设备 CONFIRMED 回执的 note 不覆盖「整条确认」留痕",
              models.WHOLE_EVENT_CONFIRM_TAG in str(ev.note or ""),
              f"note={ev.note!r}")

        report_event(EventReportIn(reminder_id=int(rule.id), due_at=due,
                                   state=int(models.EventState.MISSED),
                                   note="timeout"),
                     db=db, dev=dev)
        db.expire_all()
        ev = db.get(models.ReminderEvent, ev_id)
        check("★ 队列补发的旧 MISSED 回执不许把已确认翻回去",
              int(ev.state) == int(models.EventState.CONFIRMED),
              f"state={ev.state}")
        check("★ 被忽略的那条回执也不许改 note",
              models.WHOLE_EVENT_CONFIRM_TAG in str(ev.note or ""),
              f"note={ev.note!r}")
    finally:
        db.close()


def t_confirm_anchor() -> None:
    """确认窗口的锚点必须是 ``max(due, fired_at)``。

    为什么不能只用 ``due_at``：设备离线 / 重启会**迟响**。生产库 ev=125 的
    due 是 12:43，设备 16:19:26 才真的响 —— 迟了 3 小时 36 分。而屏幕的 4 小时
    计时是从"卡片显示出来那一刻"开始的（``ui.c`` 在 ``OnReminder`` 里设
    ``reminder_expire_us``）。锚点不跟着迟响走，就会出现
    「屏上还挂着、服务端说早就过期了」——正是本次事故。

    两条都要有：迟响要认，脏时间戳要退回 due（不能整条消失）。
    """
    print("\n[10] 确认窗口锚点 = max(due, fired_at)")
    from datetime import timedelta

    from app import models
    from app.db import SessionLocal, init_db
    from app.schedule import local_now
    from app.voice.tools import _confirm_anchor, pending_medicine_events

    init_db()
    db = SessionLocal()
    try:
        db.query(models.ReminderEvent).delete()
        db.query(models.Reminder).delete()
        db.query(models.Device).delete()
        db.commit()

        dev = models.Device(device_id="aabbccdd0033", name="锚点设备")
        db.add(dev)
        db.commit()
        pk = int(dev.id)

        rule = models.Reminder(
            device_id=pk, title="迟响测试",
            rtype=int(models.ReminderType.MEDICINE),
            require_confirm=True, enabled=True,
            times_of_day='["12:43"]', weekdays="[]", month_days="[]",
            medicines=[{"name": "降压药", "dose": "1片"}],
            tz="Asia/Shanghai", tz_offset_min=0,
        )
        db.add(rule)
        db.commit()

        now = local_now()

        # ---- ① 迟响：due 5 小时前，fired 1 小时前 ⇒ 屏幕还挂着 ⇒ 算待确认 ----
        ev = models.ReminderEvent(
            reminder_id=rule.id, device_id=pk,
            due_at=now - timedelta(hours=5),
            fired_at=now - timedelta(hours=1),
            state=int(models.EventState.MISSED),
            tz="Asia/Shanghai", tz_offset_min=0,
        )
        db.add(ev)
        db.commit()
        anchor = _confirm_anchor(ev)
        check("★ 锚点跟着 fired_at 走（迟响 4 小时）",
              abs((anchor - (now - timedelta(hours=1))).total_seconds()) < 120,
              f"anchor={anchor} due={ev.due_at} fired={ev.fired_at}")
        check("★ due 5 小时前但迟响 1 小时 ⇒ 仍算待确认",
              [int(e.id) for e, _ in pending_medicine_events(db, pk)]
              == [int(ev.id)])

        # ---- ② 负向：fired_at 也是 5 小时前 ⇒ 屏幕早收了 ----
        ev.fired_at = now - timedelta(hours=5)
        db.commit()
        check("fired 也是 5 小时前 ⇒ 不算待确认",
              pending_medicine_events(db, pk) == [])

        # ---- ③ 脏时间戳 ⇒ 退回 due，不能整条消失或算成未来 ----
        ev.fired_at = None
        db.commit()
        check("没有 fired_at ⇒ 锚点就是 due",
              _confirm_anchor(ev) == _confirm_anchor(ev) and
              abs((_confirm_anchor(ev) - (now - timedelta(hours=5)))
                  .total_seconds()) < 120,
              f"anchor={_confirm_anchor(ev)}")
    finally:
        db.close()


def t_device_controls() -> None:
    print("\n[7] 音量 / 亮度 / 设备状态：服务端工具 → 设备 MCP 工具")
    from app.voice.tools import TOOL_DEFS, execute_tool

    names = [d["function"]["name"] for d in TOOL_DEFS]
    check("三个设备能力工具都注册给 LLM 了",
          {"set_volume", "set_brightness", "get_device_status"} <= set(names),
          f"names={names}")
    check("★ 必须追加在工具表末尾（插中间会让 prompt 前缀缓存全失效）",
          names[-6:] == ["set_volume", "set_brightness", "get_device_status",
                         "get_wifi_info", "get_headlines", "set_status_light"],
          f"tail={names[-6:]}")

    # ---- 绝对量：一次往返，参数名必须与固件 Property 名逐字一致 ----
    dev = FakeDevice()
    r = execute_tool(None, 1, "set_volume", '{"level": 40}', device_call=dev)
    check("set_volume(level=40) 下发到固件工具 self.audio_speaker.set_volume",
          dev.calls and dev.calls[0][0] == "self.audio_speaker.set_volume",
          f"calls={dev.calls}")
    check("参数名是 volume（固件 Property('volume', ...) 拼错就 Unknown tool）",
          dev.calls and dev.calls[0][1] == {"volume": 40}, f"calls={dev.calls}")
    check("超时用 2.0 秒（回调是毫秒级的；confirm_reminder 那条才要 8.0）",
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
    t_recent_confirm()
    t_whole_event_confirm()
    t_confirm_anchor()

    print(f"\n=== {_g_pass} 通过 / {_g_fail} 失败 ===")
    if _failures:
        print("失败项：")
        for f in _failures:
            print(f"  - {f}")
    return 1 if _g_fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
