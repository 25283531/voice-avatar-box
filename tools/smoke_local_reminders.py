"""断网自建提醒上报端点冒烟（POST /api/device/reminders/local）。

为什么单独一个脚本
----------------
需求是「老人断网时建的服药提醒，重新联网后要能上报服务端、由服务端分配
ID 回写本地」。这条链路上服务端那半段此前完全没有端点，所以补了
/api/device/reminders/local。这里验证三件事：

  1. 鉴权：没带 device_token 必须 401/403（不能是任何人能往里塞提醒）；
  2. 落库：断网自建的服药提醒能被建出来，并出现在 /api/device/sync 的
     下发列表里 —— 这是老人「我建了提醒」的最终可见性；
  3. 幂等：同一批重复上报**不能**建出重复提醒（设备在「已入库但响应丢失」
     时必然会重发；重复提醒=老人被反复响铃打扰）。

用法：
    ELDER_SEED_ADMIN_PASSWORD=admin123 python tools/smoke_local_reminders.py [端口]
"""
from __future__ import annotations

import json
import os
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
from smoke_ports import guard_or_die, guard_port, release_port, resolve_port  # noqa: E402

# 本机设了 http_proxy，urllib 默认会读它，于是「访问自己刚起的 127.0.0.1 服务」
# 被绕到代理一跳 —— 代理不通就 TimeoutError 且零断言失败，极易被误判成代码回归。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DB_PATH = Path(tempfile.gettempdir()) / f"elder_local_rmd_{os.getpid()}.db"
if DB_PATH.exists():
    DB_PATH.unlink()
SERVER_LOG = DB_PATH.with_name(DB_PATH.stem + "_server.log")

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else resolve_port("smoke_local_reminders")
BASE = f"http://127.0.0.1:{PORT}"

results: list[tuple[str, bool, str]] = []


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
        except Exception:
            return e.code, raw


def wait_ready(timeout: float = 120.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if req("GET", "/api/health")[0] == 200:
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
    env["ELDER_SEED_ADMIN_PASSWORD"] = "admin123"
    env["PYTHONIOENCODING"] = "utf-8"

    guard_or_die(PORT, "smoke_local_reminders.py")
    hold, hold_hint = guard_port(PORT)
    if hold is None:
        print("[boot] " + hold_hint)
        return 1
    release_port(hold, PORT)

    print(f"[boot] starting server on :{PORT}")
    log_fp = SERVER_LOG.open("w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT),
         "--log-level", "warning"],
        cwd=str(SERVER), env=env,
        stdout=log_fp, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace",
    )
    try:
        if not wait_ready():
            print("[boot] server did not become ready:")
            print(SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-3000:])
            return 1
        print("[boot] ready\n")

        # ---------------------------------------------- 1. 鉴权
        code, _ = req("POST", "/api/device/reminders/local",
                      [{"client_key": "k", "title": "早上的药",
                        "times_of_day": ["08:00"]}])
        check("未带 device_token 被拒", code in (401, 403), f"HTTP {code}")

        # ---------------------------------------------- 2. 建账号 + 设备
        # 用初始管理员（ELDER_SEED_ADMIN_PASSWORD=admin123 起种子），
        # 与 tools/smoke_server.py 一致，避免依赖注册接口的开关策略。
        code, reg = req("POST", "/api/auth/login",
                        {"username": "admin", "password": "admin123"})
        tok = (reg or {}).get("access_token", "")
        check("管理员登录", code == 200 and bool(tok), f"HTTP {code}")

        # ⚠️ 取码必须走**设备自己那条路**（POST /api/voice/ota + Device-Id 头）。
        # 2026-10-20 起 `POST /api/devices/{pk}/code` 对「已绑定设备」返回 409
        # （语义：已绑定不再发码），而 `POST /api/devices` 是「建档即归属」——
        # 于是老的「建档 → 管理端取码 → 绑定」三步在同一份代码上**自相矛盾**，
        # 本脚本曾因此从全绿变成 3/12（申请激活码 -> HTTP 409）。
        # 走 OTA 不仅不再矛盾，还是真实链路：设备开机取码 → 家属在 APP 输码。
        dev_mac = "e1de7700c0de"
        code, ota = req("POST", "/api/voice/ota", {}, headers={"Device-Id": dev_mac})
        full_code = ((ota or {}).get("activation") or {}).get("code", "")
        check("设备经 OTA 领到激活码（固件真链路）",
              code == 200 and len(full_code) == 7, f"HTTP {code} len={len(full_code)}")
        code, bind = req("POST", "/api/devices/bind",
                         {"code": full_code, "device_id": dev_mac}, tok)
        dev_token = (bind or {}).get("device_token", "")
        dev_pk = (bind or {}).get("device_pk")
        check("绑定拿到 device_token", code == 200 and bool(dev_token), f"HTTP {code}")

        # ---------------------------------------------- 2b. 设备时区接口
        # 网页端与 APP 的「设备时区」都只读这一个接口（GET /api/devices/{pk}/timezone），
        # 前端一个字都不自己拼。所以这里必须验**序列化出来的形状**，而不只是
        # "函数算得对" —— 字段漏进 DeviceTimezoneOut 是最典型的失误：
        # 函数是对的、单测全绿，接口却少一个 key，前端那一格永远是空的。
        #
        # 本脚本里的设备没有公网 IP（ip=""），所以多半走「判不出」那一支。
        # 判得出那一支的内容断言在 tools/smoke_geo.py 的 F31/F32（用注入的
        # device_guess 造，不依赖外网），两边分工，不重复也不留缺口。
        code, tz = req("GET", f"/api/devices/{dev_pk}/timezone", None, tok)
        check("设备时区接口 200", code == 200, f"HTTP {code}")
        _missing = [k for k in ("ok", "timezone", "offset_from_beijing_min",
                                "region", "source", "hint", "short")
                    if k not in (tz or {})]
        check("设备时区接口字段齐备（含 short）", not _missing,
              "缺 " + ",".join(_missing) if _missing else json.dumps(tz, ensure_ascii=False)[:160])
        check("short 是字符串（不是 null / 数字）",
              isinstance((tz or {}).get("short"), str), repr((tz or {}).get("short")))
        # 契约：判得出 ⇒ short 非空（前端靠它渲染那一格）；判不出 ⇒ short 空
        # 且 hint 非空（前端显示「判不出」+ 说明会退回哪一级兜底）。
        # 两种情形都必须让前端有东西可显示，不能两个都空。
        if (tz or {}).get("ok"):
            check("判得出时 short 非空", bool((tz or {}).get("short")), repr(tz.get("short")))
        else:
            check("判不出时 short 为空串且 hint 非空",
                  (tz or {}).get("short") == "" and bool((tz or {}).get("hint")),
                  f"short={tz.get('short')!r} hint={tz.get('hint')!r}")

        # ---------------------------------------------- 3. 上报断网自建提醒
        batch = [
            {"client_key": "1-0800-早上的药", "title": "早上的药", "rtype": 0,
             "schedule_kind": 1, "times_of_day": ["08:00"], "weekdays": [1, 2, 3, 4, 5, 6, 7],
             "confirm_methods": ["voice", "button"], "enabled": True},
            {"client_key": "1-2000-晚上的药", "title": "晚上的药", "rtype": 0,
             "schedule_kind": 1, "times_of_day": ["20:00"], "weekdays": [1, 2, 3, 4, 5, 6, 7],
             "confirm_methods": ["voice", "button"], "enabled": True},
        ]
        code, rep = req("POST", "/api/device/reminders/local", batch, dev_token)
        acks = (rep or {}).get("acks", [])
        check("上报返回 200", code == 200, f"HTTP {code}")
        check("逐条回写 id", len(acks) == 2 and all(a.get("reminder_id") for a in acks),
              json.dumps(acks, ensure_ascii=False))
        check("client_key 原样回传",
              [a.get("client_key") for a in acks] == [b["client_key"] for b in batch],
              str([a.get("client_key") for a in acks]))

        # ---------------------------------------------- 4. 幂等：重发同一批
        code, rep2 = req("POST", "/api/device/reminders/local", batch, dev_token)
        acks2 = (rep2 or {}).get("acks", [])
        check("重复上报返回同一 id（不新建）",
              code == 200
              and [a.get("reminder_id") for a in acks2] == [a.get("reminder_id") for a in acks],
              f"{[a.get('reminder_id') for a in acks]} -> {[a.get('reminder_id') for a in acks2]}")

        # ---------------------------------------------- 5. 设备同步能看到它们
        code, sync = req("POST", "/api/device/sync?fw_version=test", {}, dev_token)
        titles = sorted(r.get("title", "") for r in (sync or {}).get("reminders", []))
        check("sync 下发含断网自建提醒",
              code == 200 and "早上的药" in titles and "晚上的药" in titles, str(titles))

        # 确认方式含 voice/button —— 断网时老人只有这两条确认路径
        voices = [r for r in (sync or {}).get("reminders", [])
                  if "voice" in (r.get("confirm_methods") or [])]
        check("下发确认方式含 voice", len(voices) >= 2, f"{len(voices)} 条")

        # ---------------------------------------------- 6. 回执能对上刚建的 id
        if acks and acks[0].get("reminder_id"):
            rid = acks[0]["reminder_id"]
            code, _ = req("POST", "/api/device/events",
                          {"reminder_id": rid, "due_at": "2026-10-17T08:00:00+08:00",
                           "state": 2, "confirm_method": 1, "note": "confirmed"}, dev_token)
            check("对刚上报的 id 能回传回执（语音确认 method=1）", code == 200, f"HTTP {code}")
        else:
            check("对刚上报的 id 能回传回执（语音确认 method=1）", False, "没有可用的 reminder_id")

        # ---------------------------------------------- 7. 全置信合并（VOICE+BUTTON=5）
        #
        # 用户 2026-11-03 定义：「先按了服药提醒按键进行确认，再通过追问的方式
        # 确认服用的哪一种药，那就属于语音+按键的全置信」。机制在
        # routers/device_api.report_event：**库里是 VOICE** 且**这次回执是
        # BUTTON** 且 state=CONFIRMED ⇒ 落 VOICE_AND_BUTTON(5)。
        #
        # 这条规则此前**没有任何测试覆盖**（全仓 grep VOICE_AND_BUTTON 在
        # tools/ 下零命中）—— 而它一旦坏掉是完全静默的：家属端只是永远显示
        # 「按键确认」，依从率统计也照常算已确认，没人会发现"全置信"没了。
        #
        # 这里从**回执侧**直接验证，不经过语音链路（那需要真设备）。
        rid = acks[0]["reminder_id"] if acks else 0
        if rid:
            due1 = "2026-10-17T09:00:00+08:00"
            req("POST", "/api/device/events",
                {"reminder_id": rid, "due_at": due1, "state": 2,
                 "confirm_method": 1}, dev_token)
            req("POST", "/api/device/events",
                {"reminder_id": rid, "due_at": due1, "state": 2,
                 "confirm_method": 2}, dev_token)
            code, evs = req("GET", f"/api/devices/{dev_pk}/events?limit=200", None, tok)
            hit = [e for e in (evs or []) if "T09:00" in str(e.get("due_at", ""))]
            check("先 VOICE 后 BUTTON ⇒ 合并成全置信(5)",
                  bool(hit) and hit[0].get("confirm_method") == 5,
                  json.dumps(hit[:1], ensure_ascii=False))

            # 反向用例（更重要）：库里是 NONE（纯按键，没走过任何对话）时
            # 报 BUTTON **不许**变成 5 —— 那会把"老人自己按的"记成
            # "语音也确认过"，是替老人编造依从记录。
            due2 = "2026-10-17T10:00:00+08:00"
            req("POST", "/api/device/events",
                {"reminder_id": rid, "due_at": due2, "state": 2,
                 "confirm_method": 2}, dev_token)
            code, evs = req("GET", f"/api/devices/{dev_pk}/events?limit=200", None, tok)
            hit2 = [e for e in (evs or []) if "T10:00" in str(e.get("due_at", ""))]
            check("库里是 NONE 时纯按键回执仍是按键(2)，不冒充全置信",
                  bool(hit2) and hit2[0].get("confirm_method") == 2,
                  json.dumps(hit2[:1], ensure_ascii=False))

            # ---------------------------------------------- 7b. 已确认不许被
            # 队列补发的旧回执翻回去（2026-11-04）
            #
            # 设备回执走 NVS 队列补发（sync.c 的 flush_queue），断网期间产生的
            # 那几条会在联网后按**旧顺序**重放。服务端 2026-11-04 起允许「4 小时
            # 内迟确认」（voice/tools._CONFIRM_WINDOW_AFTER_MIN）⇒ 新时序：
            #   ① 离线期间本地超时，队列里塞了一条 MISSED 回执；
            #   ② 老人长按确认，服务端落 CONFIRMED、收屏、灭灯；
            #   ③ 队列 flush，旧的 MISSED 到达 ⇒ 若无条件覆盖就把记录翻回
            #      "漏服"，依从率/漏服告警/家属端全反转，而屏幕已经收了。
            # 这个洞是完全静默的（无日志、无异常），所以必须有断言。
            due3 = "2026-10-17T11:00:00+08:00"
            req("POST", "/api/device/events",
                {"reminder_id": rid, "due_at": due3, "state": 2,
                 "confirm_method": 2}, dev_token)
            req("POST", "/api/device/events",
                {"reminder_id": rid, "due_at": due3, "state": 3,
                 "note": "timeout"}, dev_token)
            code, evs = req("GET", f"/api/devices/{dev_pk}/events?limit=200", None, tok)
            hit3 = [e for e in (evs or []) if "T11:00" in str(e.get("due_at", ""))]
            check("★ 已确认(2) 后到达的 MISSED(3) 回执不许翻回去",
                  bool(hit3) and hit3[0].get("state") == 2,
                  json.dumps(hit3[:1], ensure_ascii=False))

            # 反向用例：未确认的实例照旧接受 MISSED（挡多了会让漏服永远发现不了）
            due4 = "2026-10-17T12:00:00+08:00"
            req("POST", "/api/device/events",
                {"reminder_id": rid, "due_at": due4, "state": 1}, dev_token)
            req("POST", "/api/device/events",
                {"reminder_id": rid, "due_at": due4, "state": 3,
                 "note": "timeout"}, dev_token)
            code, evs = req("GET", f"/api/devices/{dev_pk}/events?limit=200", None, tok)
            hit4 = [e for e in (evs or []) if "T12:00" in str(e.get("due_at", ""))]
            check("★ 未确认(FIRED) 的实例照旧接受 MISSED(3)",
                  bool(hit4) and hit4[0].get("state") == 3,
                  json.dumps(hit4[:1], ensure_ascii=False))
        else:
            check("先 VOICE 后 BUTTON ⇒ 合并成全置信(5)", False, "没有可用的 reminder_id")
            check("库里是 NONE 时纯按键回执仍是按键(2)，不冒充全置信", False, "同上")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fp.close()

    print()
    failed = [n for n, ok, _ in results if not ok]
    print(f"=== {len(results) - len(failed)}/{len(results)} 通过 ===")
    if failed:
        print("失败: " + "; ".join(failed))
        print("--- server log tail ---")
        print(SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-3000:])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())