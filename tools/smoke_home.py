"""首页聚合 / 账户 / 设备角色 冒烟测试。

用法：
    python tools/smoke_home.py [--port 8097]

覆盖 APP 四个新增后端接口的字段契约（对照 app/src/api/elder/index.ts）：
    1. POST   /api/auth/password    修改密码（旧密码错 400 / 新密码太短 422 / 未登录 401）
    2. PATCH  /api/auth/email       更换邮箱（格式非法 422 / 与他人重复 409 / me 回读）
    3. GET    /api/home/summary     首页聚合（设备状态 + 当天服药 + 会员，按服务端时区）
    4. POST   /api/devices/{pk}/agent  绑定 agent 角色（他人设备 404 / 未登录 401）
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
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable


DB_PATH = Path(os.environ.get(
    "SMOKE_HOME_DB", str(Path(tempfile.gettempdir()) / "elder_smoke_home.db")))
# 相对路径是相对 server 进程的工作目录（server/）解析的，清理时要拼上
DB_FILE = DB_PATH if DB_PATH.is_absolute() else (SERVER / DB_PATH)

PORT = int(next((a for a in sys.argv[1:] if a.isdigit()), 8097))
BASE = f"http://127.0.0.1:{PORT}"

# 当天（本机时区，与服务端 TZ 一致）与两个固定的服药时间点
TODAY = datetime.now().strftime("%Y-%m-%d")
DOSE_TIMES = ["08:00", "20:00"]

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
        with urllib.request.urlopen(r, timeout=20) as resp:
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
    env["ELDER_JWT_SECRET"] = "smoke-home-secret"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"   # 不跑后台调度，避免超时升级干扰计数
    env["PYTHONIOENCODING"] = "utf-8"

    if DB_FILE.exists():
        DB_FILE.unlink()

    print(f"[boot] starting server on :{PORT} (db: {DB_FILE})")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT),
         "--log-level", "warning"],
        cwd=str(SERVER), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace",
    )
    try:
        if not wait_ready():
            out = proc.stdout.read() if proc.stdout else ""
            print("[boot] server did not become ready. output:\n" + out[-3000:])
            return 1
        print("[boot] ready\n")

        # ==================================================== 准备账号
        code, tk = req("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
        check("登录默认管理员", code == 200 and bool(tk.get("access_token")), str(code))

        def reg(name: str) -> str:
            req("POST", "/api/auth/register",
                {"username": name, "password": "test123456", "nickname": name})
            _, t = req("POST", "/api/auth/login", {"username": name, "password": "test123456"})
            return t.get("access_token", "")

        # u1 有设备，u2 是「别人」，u3 是完全干净的新用户
        u1, u2, u3 = reg("home1"), reg("home2"), reg("home3")
        check("注册并登录 3 个普通用户", all([u1, u2, u3]), "")

        # ==================================================== 3.1 新用户无设备
        code, s0 = req("GET", "/api/home/summary", token=u3)
        check("新用户首页不报错", code == 200, f"code={code}")
        check("新用户 devices 为空", isinstance(s0.get("devices"), list)
              and len(s0["devices"]) == 0, str(s0.get("devices")))
        check("新用户服药统计全 0",
              s0.get("medicine_today", {}).get("total_count") == 0
              and s0.get("medicine_today", {}).get("pending_count") == 0
              and s0.get("medicine_today", {}).get("pending_times") == [],
              str(s0.get("medicine_today")))
        check("非会员 is_vip 为 false", s0.get("membership", {}).get("is_vip") is False,
              str(s0.get("membership")))
        check("首页会员卡字段齐全",
              set(["is_vip", "plan", "expires_at", "days_left", "benefits"])
              <= set(s0.get("membership", {})),
              ",".join(sorted(s0.get("membership", {}))))
        check("benefits 是字符串数组",
              isinstance(s0.get("membership", {}).get("benefits"), list)
              and all(isinstance(x, str) for x in s0["membership"]["benefits"]),
              str(s0.get("membership", {}).get("benefits")))

        # ==================================================== 准备：u1 绑定设备
        code, dev = req("POST", "/api/devices", {"device_id": "homedev000001"}, u1)
        dev_pk = dev.get("id", 0)
        code, cd = req("POST", f"/api/devices/{dev_pk}/code", {}, u1)
        code, bind = req("POST", "/api/devices/bind", {"code": cd.get("code", "")}, u1)
        dev_token = bind.get("device_token", "")
        check("u1 绑定设备", bool(dev_token), str(code))

        # 建一条当天两次的服药提醒（rtype=0 服药，schedule_kind=1 每天）
        code, rem = req("POST", f"/api/reminders?device_pk={dev_pk}", {
            "title": "降压药",
            "rtype": 0,
            "schedule_kind": 1,
            "times_of_day": DOSE_TIMES,
            "dose": "1 片",
            "require_confirm": True,
            "enabled": True,
        }, u1)
        rem_pk = rem.get("id", 0)
        check("创建当天服药提醒", code == 201 and rem.get("rtype") == 0, str(code))

        # ==================================================== 3.2 未确认时
        code, s1 = req("GET", "/api/home/summary", token=u1)
        med1 = s1.get("medicine_today", {})
        check("首页返回已绑定设备", code == 200 and len(s1.get("devices", [])) == 1, str(code))
        check("设备字段对齐 HomeSummary.devices",
              set(["id", "name", "online", "last_seen_at", "ip", "fw_version",
                   "device_time", "agent_name", "avatar_name"])
              <= set(s1.get("devices", [{}])[0]),
              ",".join(sorted(s1.get("devices", [{}])[0])))
        check("当天合计 2 次", med1.get("total_count") == 2, str(med1))
        check("待服药 2 次且时间点与规则一致",
              med1.get("pending_count") == 2
              and med1.get("pending_times") == DOSE_TIMES,
              str(med1.get("pending_times")))
        check("未确认时 confirmed_count 为 0", med1.get("confirmed_count") == 0,
              str(med1.get("confirmed_count")))
        check("未心跳时 online 为 false", s1["devices"][0].get("online") is False, "")

        # ==================================================== 3.3 确认一次
        # 走设备回执接口上报 08:00 已确认（实例表没预排时该接口会自动补建），
        # 这样无论脚本在当天的哪个时刻运行都能稳定地多出一条确认记录。
        code, ev = req("POST", "/api/device/events", {
            "reminder_id": rem_pk,
            "due_at": f"{TODAY}T08:00:00",
            "state": 2,                       # 2 = CONFIRMED
            "confirm_method": 2,              # 2 = 物理按键
            "confirmed_at": f"{TODAY}T08:00:30",
        }, dev_token)
        check("上报 08:00 已确认", code == 200 and ev.get("state") == 2, str(code))

        code, s2 = req("GET", "/api/home/summary", token=u1)
        med2 = s2.get("medicine_today", {})
        check("确认后 confirmed_count 变 1", med2.get("confirmed_count") == 1, str(med2))
        check("确认后待服药降到 1 次且只剩 20:00",
              med2.get("pending_count") == 1 and med2.get("pending_times") == ["20:00"],
              str(med2.get("pending_times")))
        check("当天合计仍为 2", med2.get("total_count") == 2, str(med2.get("total_count")))

        # ==================================================== 3.3b 排期未生成时 confirm
        # 建一条今天 00:05 的服药提醒：排期只覆盖 now-1h 之后，这条不会被预排，
        # 以前这种时候在 APP 上点「代确认」会直接 404。
        code, rem2 = req("POST", f"/api/reminders?device_pk={dev_pk}", {
            "title": "早起降压药",
            "rtype": 0,
            "schedule_kind": 1,
            "times_of_day": ["00:05"],
            "require_confirm": True,
            "enabled": True,
        }, u1)
        rem2_pk = rem2.get("id", 0)
        check("创建当天未预排的服药提醒", code == 201, str(code))
        code, s2b = req("GET", "/api/home/summary", token=u1)
        check("未预排的服药点也计入当天合计",
              s2b.get("medicine_today", {}).get("total_count") == 3,
              str(s2b.get("medicine_today")))
        code, ev2 = req("POST", f"/api/reminders/{rem2_pk}/confirm", {}, u1)
        check("排期未生成时 confirm 返回 2xx（不再 404）", 200 <= code < 300, f"code={code}")
        check("补建的实例落在当天 00:05（服务端时区）",
              isinstance(ev2, dict) and "T00:05" in str(ev2.get("due_at", "")),
              str(ev2.get("due_at") if isinstance(ev2, dict) else ev2))
        code, s2c = req("GET", "/api/home/summary", token=u1)
        med2c = s2c.get("medicine_today", {})
        check("confirm 后 confirmed_count 变 2", med2c.get("confirmed_count") == 2, str(med2c))
        check("待服药只剩 20:00",
              med2c.get("pending_count") == 1 and med2c.get("pending_times") == ["20:00"],
              str(med2c.get("pending_times")))
        # 重复确认不应再生成第二条实例（去重键 reminder_id + due_at）
        req("POST", f"/api/reminders/{rem2_pk}/confirm", {}, u1)
        code, s2d = req("GET", "/api/home/summary", token=u1)
        check("重复确认不产生重复实例",
              s2d.get("medicine_today", {}).get("total_count") == 3,
              str(s2d.get("medicine_today")))

        # ==================================================== 3.4 心跳后在线
        code, sync = req("POST", "/api/device/sync?fw_version=0.1.0", {}, dev_token)
        check("设备心跳同步", code == 200, str(code))
        code, s3 = req("GET", "/api/home/summary", token=u1)
        d3 = s3["devices"][0]
        check("心跳后 online 为 true（5 分钟窗口）", d3.get("online") is True,
              str(d3.get("last_seen_at")))
        check("device_time 与 last_seen_at 一致", d3.get("device_time") == d3.get("last_seen_at"),
              f"{d3.get('device_time')} vs {d3.get('last_seen_at')}")

        # ==================================================== 4. agent 角色字段
        # 角色走 /api/providers（APP 的「创建我的 agent 角色」也是这个接口）。
        # 4.1 只传 AgentRole 的字段就要能建成（APP createAgentRole 就是这么发的）
        code, role = req("POST", "/api/providers", {
            "name": "小暖医生",
            "description": "温和的服药提醒助手",
            "system_prompt": "你是耐心的服药提醒助手",
            "tts_voice": "zh-CN-XiaoxiaoNeural",
            "is_default": True,
        }, u1)
        role_id = role.get("id", 0)
        check("只传角色字段即可创建（不再 422）", code == 201 and bool(role_id), f"code={code}")
        check("角色字段原样返回",
              role.get("description") == "温和的服药提醒助手"
              and role.get("system_prompt") == "你是耐心的服药提醒助手"
              and role.get("tts_voice") == "zh-CN-XiaoxiaoNeural"
              and role.get("is_default") is True,
              str({k: role.get(k) for k in ("description", "system_prompt", "tts_voice", "is_default")}))
        check("角色记录 kind 为空（不参与下发 / 导出）", role.get("kind") == "",
              str(role.get("kind")))

        # 4.2 完整 AI 平台配置的行为照旧
        code, plat = req("POST", "/api/providers", {
            "name": "主力对话", "kind": "llm", "provider": "deepseek",
            "model": "deepseek-chat", "api_key": "sk-should-not-leak", "priority": 1,
        }, u1)
        check("完整 AI 平台配置照旧可建", code == 201 and plat.get("kind") == "llm", str(code))

        # 4.3 脱敏列表要带上角色字段，且不能泄密钥
        code, safe = req("GET", "/api/providers/safe", token=u1)
        safe_rows = safe if isinstance(safe, list) else []
        role_row = next((p for p in safe_rows if p.get("id") == role_id), {})
        check("/safe 返回含角色字段",
              set(["description", "system_prompt", "tts_voice", "is_default"]) <= set(role_row),
              ",".join(sorted(role_row)))
        check("/safe 不含 api_key", not any("api_key" in p for p in safe_rows),
              f"{len(safe_rows)} 条")
        check("/safe 同时含角色与平台配置", len(safe_rows) >= 2, f"{len(safe_rows)} 条")

        # 4.4 导出小智配置只应含真实平台配置，不能被角色污染
        code, exported = req("GET", "/api/providers/export/xiaozhi", token=u1)
        mods = exported.get("selected_module", {}) if isinstance(exported, dict) else {}
        check("导出只含真实平台配置（角色不混入）",
              code == 200 and "llm" in mods
              and set(mods) <= set(["asr", "llm", "tts", "vad"]),
              ",".join(sorted(mods)))

        # ==================================================== 4.5 绑定 agent 角色
        code, bound = req("POST", f"/api/devices/{dev_pk}/agent", {"role_id": role_id}, u1)
        check("给自己的设备绑定角色 -> 2xx",
              200 <= code < 300 and bound.get("role_id") == role_id, f"code={code}")
        check("绑定返回角色名", bound.get("role_name") == "小暖医生", str(bound.get("role_name")))

        code, s4 = req("GET", "/api/home/summary", token=u1)
        check("首页 device.agent_name 回显角色名",
              s4["devices"][0].get("agent_name") == "小暖医生",
              str(s4["devices"][0].get("agent_name")))

        code, _ = req("POST", f"/api/devices/{dev_pk}/agent", {"role_id": role_id}, u2)
        check("绑定他人设备 -> 404（不暴露设备存在性）", code == 404, f"code={code}")
        code, _ = req("POST", f"/api/devices/{dev_pk}/agent", {"role_id": role_id})
        check("未登录绑定角色 -> 401", code == 401, f"code={code}")
        code, _ = req("POST", f"/api/devices/{dev_pk}/agent", {"role_id": 999999}, u1)
        check("角色不存在 -> 404", code == 404, f"code={code}")
        code, _ = req("POST", "/api/devices/999999/agent", {"role_id": role_id}, u1)
        check("设备不存在 -> 404", code == 404, f"code={code}")

        # ==================================================== 2. 更换邮箱
        code, _ = req("PATCH", "/api/auth/email", {"email": "not-an-email"}, u1)
        check("邮箱格式非法 -> 422", code == 422, f"code={code}")

        code, _ = req("PATCH", "/api/auth/email", {"email": "dup@example.com"}, u2)
        check("u2 先占用邮箱", code == 200, str(code))
        code, _ = req("PATCH", "/api/auth/email", {"email": "dup@example.com"}, u1)
        check("与他人重复 -> 409", code == 409, f"code={code}")

        code, acc = req("PATCH", "/api/auth/email", {"email": "home1@example.com"}, u1)
        check("更换邮箱成功", code == 200 and acc.get("email") == "home1@example.com",
              str(acc.get("email")))
        check("返回体字段对齐 ElderAccount",
              set(["id", "username", "nickname", "phone", "email", "is_admin"]) <= set(acc),
              ",".join(sorted(acc)))
        code, me = req("GET", "/api/auth/me", token=u1)
        check("GET /api/auth/me 回读新邮箱",
              code == 200 and me.get("email") == "home1@example.com", str(me.get("email")))
        code, _ = req("PATCH", "/api/auth/email", {"email": "x@example.com"})
        check("未登录换邮箱 -> 401", code == 401, f"code={code}")

        # ==================================================== 3.5 会员路径
        # 用管理端直接分配会员，验证首页会员卡能正确反映「已开通 + 到期时间 + 剩余天数」
        admin_token = tk.get("access_token", "")
        u1_id = me.get("id", 0)
        code, granted = req("POST", "/api/admin/membership/grant",
                            {"user_id": u1_id, "days": 10, "note": "冒烟测试"}, admin_token)
        check("管理端开通会员", code == 200 and granted.get("is_vip") is True, str(code))
        code, s5 = req("GET", "/api/home/summary", token=u1)
        mem5 = s5.get("membership", {})
        check("首页会员卡显示已开通", mem5.get("is_vip") is True, str(mem5))
        check("首页会员剩余天数 10", mem5.get("days_left") == 10, str(mem5.get("days_left")))
        check("会员到期时间非空", bool(mem5.get("expires_at")), str(mem5.get("expires_at")))
        check("会员权益含陪聊不限时长",
              "陪聊不限时长" in (mem5.get("benefits") or []), str(mem5.get("benefits")))

        # ==================================================== 3.6 benefits 必须是数组
        # APP 首页写的是 (membership.benefits || []).join(' · ')，
        # profile 页写的是 membership.benefits.join(' / ')，对字典调 join 会直接崩。
        code, ms1 = req("GET", "/api/membership", token=u1)
        ben1 = ms1.get("benefits") if isinstance(ms1, dict) else None
        check("/api/membership 的 benefits 是字符串数组",
              isinstance(ben1, list) and all(isinstance(x, str) for x in ben1), str(ben1))
        check("APP 的 join 用法不会报错",
              isinstance(ben1, list) and " · ".join(ben1) != "", "" if not ben1 else " · ".join(ben1))
        check("会员 benefits 含陪聊不限时长", isinstance(ben1, list) and "陪聊不限时长" in ben1,
              str(ben1))
        detail = ms1.get("benefits_detail", {}) if isinstance(ms1, dict) else {}
        check("明细没有丢，挪到 benefits_detail",
              detail.get("chat_unlimited") is True
              and detail.get("reminder_always_free") is True, str(detail))
        check("首页与 /api/membership 的权益文案一致",
              mem5.get("benefits") == ben1, f"{mem5.get('benefits')} vs {ben1}")

        code, plans = req("GET", "/api/membership/plans", token=u1)
        first_plan = plans[0] if isinstance(plans, list) and plans else {}
        pben = first_plan.get("benefits")
        check("套餐 benefits 也是字符串数组",
              isinstance(pben, list) and all(isinstance(x, str) for x in pben), str(pben))
        check("套餐页面的 join 用法不会报错",
              isinstance(pben, list) and " / ".join(pben) != "", "" if not pben else " / ".join(pben))

        # ==================================================== 1. 修改密码
        code, _ = req("POST", "/api/auth/password",
                      {"old_password": "wrong-password", "new_password": "newpass123"}, u1)
        check("旧密码错误 -> 400（不误判成登录失效）", code == 400, f"code={code}")
        code, _ = req("POST", "/api/auth/password",
                      {"old_password": "test123456", "new_password": "123"}, u1)
        check("新密码太短 -> 4xx", 400 <= code < 500, f"code={code}")
        code, body = req("POST", "/api/auth/password",
                         {"old_password": "test123456", "new_password": "newpass123"}, u1)
        check("修改密码成功", 200 <= code < 300, f"code={code} {body}")
        code, again = req("POST", "/api/auth/login",
                          {"username": "home1", "password": "newpass123"})
        check("新密码可登录", code == 200 and bool(again.get("access_token")), str(code))
        code, old = req("POST", "/api/auth/login",
                        {"username": "home1", "password": "test123456"})
        check("旧密码已失效", code == 401, f"code={code}")
        code, _ = req("POST", "/api/auth/password",
                      {"old_password": "newpass123", "new_password": "another123"})
        check("未登录改密码 -> 401", code == 401, f"code={code}")

        # ==================================================== 5. 离线清扫
        # 后台周期任务每轮都会把「超过在线窗口没心跳」的设备 online 置回 False。
        # 这里进程内把心跳时间回拨到窗口之外，再手动跑一次 _tick 验证整条链路。
        os.environ["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
        sys.path.insert(0, str(SERVER))
        from sqlalchemy import select as sa_select            # noqa: E402
        from app import scheduler                             # noqa: E402
        from app.db import SessionLocal, engine               # noqa: E402
        from app.device_state import ONLINE_WINDOW            # noqa: E402
        from app.models import Device, utcnow                 # noqa: E402

        with SessionLocal() as s:
            row = s.scalar(sa_select(Device).where(Device.id == dev_pk))
            # 回拨到「在线窗口的 2 倍」之前，确保已超时
            row.last_seen_at = utcnow() - 2 * ONLINE_WINDOW
            row.online = True
            s.commit()

        code, s8 = req("GET", "/api/home/summary", token=u1)
        check("心跳超时后首页判定为离线",
              s8["devices"][0].get("online") is False, str(s8["devices"][0].get("online")))
        code, dev_before = req("GET", f"/api/devices/{dev_pk}", token=u1)
        check("清扫前 devices.online 仍是 True（只增不减的老毛病）",
              dev_before.get("online") is True, str(dev_before.get("online")))

        with SessionLocal() as s:
            scheduler._tick(s)          # 同步跑一轮周期任务（含第 5 步离线清扫）

        code, dev_after = req("GET", f"/api/devices/{dev_pk}", token=u1)
        check("周期任务清扫后 devices.online 变 False",
              dev_after.get("online") is False, str(dev_after.get("online")))
        code, s9 = req("GET", "/api/home/summary", token=u1)
        check("清扫后首页与设备列表口径一致",
              s9["devices"][0].get("online") is False, str(s9["devices"][0].get("online")))
        # 进程内开过一次连接，先释放再删临时库，否则 Windows 上文件被占着删不掉
        engine.dispose()

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if DB_FILE.exists():
            DB_FILE.unlink()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n==== {passed}/{total} passed ====")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
