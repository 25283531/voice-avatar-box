"""首页聚合 / 账户 / 设备角色 冒烟测试。

用法：
    python tools/smoke_home.py [--port 8102]

覆盖 APP 四个新增后端接口的字段契约（对照 app/src/api/elder/index.ts）：
    1. POST   /api/auth/password    修改密码（旧密码错 400 / 新密码太短 422 / 未登录 401）
    2. PATCH  /api/auth/email       更换邮箱（格式非法 422 / 与他人重复 409 / me 回读）
    3. GET    /api/home/summary     首页聚合（设备状态 + 当天服药，按服务端时区）
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable

# 默认端口统一由 tools/smoke_ports.py 托管（唯一真源），各脚本不再各写一份 ——
# 早先 smoke_home/smoke_roles/smoke_memory 都是 8097，并行跑必然撞车。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_common import cleanup_db, unlink_retry  # noqa: E402
from smoke_ports import (  # noqa: E402
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



# 默认文件名带 PID：多人/CI 并行跑时固定文件名会互相抢同一个文件，
# Windows 上直接 PermissionError（另一个进程正占用）。要固定路径就用
# SMOKE_DB_DIR 覆盖目录，或 SMOKE_HOME_DB 指定完整路径。
DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
DB_PATH = Path(os.environ.get(
    "SMOKE_HOME_DB", str(DB_DIR / f"elder_smoke_home_{os.getpid()}.db")))
# 相对路径是相对 server 进程的工作目录（server/）解析的，清理时要拼上
DB_FILE = DB_PATH if DB_PATH.is_absolute() else (SERVER / DB_PATH)

PORT = resolve_port("smoke_home")
BASE = f"http://127.0.0.1:{PORT}"

# 与 smoke_auth.py / smoke_mail.py / smoke_admin_presets.py / smoke_server.py
# 的 LEGACY_PW 同名同义（五处保持一致，改一处记得同步）。
LEGACY_PW = "admin123"

# ⚠️ 子进程输出**绝不能**用 `subprocess.PIPE` +「只在启动失败时读一次」——
# 日志量超过管道缓冲区（Windows 匿名管道 4096 字节）后，
# **服务端会被永久阻塞**，而客户端只看到「读超时」。
# 症状具有强误导性：看起来像网络/DNS/端口问题，实际是测试基础设施缺陷。
# → 输出必须落文件。
# 本仓已付出过真金代价：smoke_mail.py 曾长期稳定在第 66 条断言后抛 TimeoutError，
# 先后被误判为公网 SMTP / DNS / 端口 / scheduler / 异步任务问题（三轮全错），
# 真因就是这里：服务端日志共 4122 字节，最后一行起始偏移 3999、长度 123，
# 只剩 97 字节塞得进去 —— 恰好卡在 4096 边界上。
SERVER_LOG = DB_PATH.with_name(DB_PATH.stem + "_server.log")

# 「今天」必须按**服务端**的时区分日，不能按宿主时区。
#
# 原写法 `datetime.now()` 用的是宿主时区，注释还写着「与服务端 TZ 一致」——
# 那句在 CI 上是假的：服务端 `config.TZ` 默认 Asia/Shanghai（与宿主无关），
# 而 GitHub runner 是 UTC。北京时间 00:00–08:00（= UTC 前一天 16:00–24:00）
# 这段窗口里两边算出的"今天"差一天 ⇒ 造出来的提醒落在"昨天"，确认接口按
# "今天"查不到 ⇒ 4 条断言红。
#
# 2026-10-30 实测：CI 在 22:40 UTC 跑就红了 4 条（74/78），本地（北京时区）
# 却全绿。这类「按钟点变红」的用例比没有用例更坏 —— 它教人忽略红灯。
#
# 两道保险：① 下面按 SMOKE_TZ 算日期；② 起服务时显式注入 ELDER_TZ=SMOKE_TZ，
# 不靠"默认值恰好相同"。
SMOKE_TZ = "Asia/Shanghai"
try:
    _TZINFO = ZoneInfo(SMOKE_TZ)
except Exception:                      # 精简环境缺 tzdata：中国无夏令时，+08:00 等价
    _TZINFO = timezone(timedelta(hours=8))
TODAY = datetime.now(_TZINFO).strftime("%Y-%m-%d")
DOSE_TIMES = ["08:00", "20:00"]

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def req(method: str, path: str, body: dict | None = None, token: str | None = None,
        headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
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
            code, _ = req("GET", "/api/health")
            if code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.6)
    return False


def _read_log_tail(max_bytes: int = 8192) -> str:
    """读子进程日志（:data:`SERVER_LOG`）的尾部，供启动失败/bind 失败判定。

    本脚本刻意把子进程输出**重定向到文件**而不是 PIPE（原因见 SERVER_LOG
    常量处：PIPE 会被日志写满导致服务端永久阻塞）。代价是拿不到 ``proc.stdout``，
    所以诊断一律从文件读。

    ⚠️ 必须**按路径另开一个句柄**读，不能复用传给子进程的那个 ``log_fp``：
    它被子进程继承并持有写位置、且带缓冲，用它 read() 会拿到**空内容**。
    读不到就返回空串（宁可漏判，也不要把正常启动误报成失败）。
    """
    try:
        return SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-max_bytes:]
    except OSError:
        return ""


def main() -> int:
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
    env["ELDER_JWT_SECRET"] = "smoke-home-secret"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"   # 不跑后台调度，避免超时升级干扰计数
    env["PYTHONIOENCODING"] = "utf-8"
    # 把服务端时区**钉死**成算 TODAY 用的那一个。不写这句就依赖
    # config.TZ 的默认值恰好等于 SMOKE_TZ —— 默认值一改，两边又会错开一天，
    # 而症状仍然是"某几个小时里莫名其妙红 4 条"。
    env["ELDER_TZ"] = SMOKE_TZ
    # 种子管理员口令 bb5ca98 起不再硬编码：空库不配 ELDER_SEED_ADMIN_PASSWORD
    # 会生成随机口令。本脚本自己起服务却用 admin/admin123 登录 —— 不在这里
    # 显式注入，本地裸跑就是 401（CI 靠 job 级 env 兜住，掩盖了这个不自足）。
    # 约定同 smoke_auth.py / smoke_mail.py / smoke_admin_presets.py /
    # smoke_server.py 的 LEGACY_PW（五处同名同义，改一处记得同步）。
    env["ELDER_SEED_ADMIN_PASSWORD"] = LEGACY_PW

    if DB_FILE.exists():
        unlink_retry(DB_FILE)

    print(f"[boot] starting server on :{PORT} (db: {DB_FILE})")
    # ---- 三段式端口守卫（QA 第 4 轮「假绿」修复）----
    # 端口被占时 uvicorn 的 socket bind 会失败，**但进程不退出**（活着、只是没在
    # 监听）；而 /api/health **永远返回 200** -> wait_ready 会探到**别人的服务**
    # -> 全部断言打到别人身上、用自己从未 seed 过的 DB。
    # 三段：①事前 is_port_free ②socket 占位缩小竞态窗口 ③起服务后校验 stderr
    # ⚠️ 三段加起来**仍不是绝对保证**（①与真正 bind 之间有不可消除的竞态窗口）。
    #    真正的绝对保证是让服务自证身份（run_id），需改生产代码，留给下一轮。
    # ⚠️ **不能只调 guard_or_die()**：它拿到占位 socket 后**立刻** release
    #（见 smoke_ports.py 末两行），阶段②的占位只覆盖「检查→释放」一瞬间，等于
    # 没有占位。故下面显式持有 guard_port() 的 hold，攥到 spawn 前才release。
    # guard_or_die 保留：负责「被占就立刻 SystemExit(1)+打印换端口提示」。
    #
    # ⚠️ **三段加起来仍然不是绝对保证**（务必读完再用）：①事前检查与真正 bind
    # 之间存在**不可消除的竞态窗口**（压到最小 ≠ 消除）；②占位在 release 到
    # uvicorn 真正 bind 之间仍有间隙；③bind 失败的 uvicorn **可能仍活着**。
    # **别因为「加了预检」就认为并发跑已经安全** —— architect 栽的正是这个坑。
    # 真正的绝对保证是让服务自证身份（run_id），需改生产代码，留给下一轮。
    guard_or_die(PORT, "smoke_home.py")
    hold, hold_hint = guard_port(PORT)
    if hold is None:            # 理论上 guard_or_die 已挡住，这里是第二道保险
        print("[boot] " + hold_hint.replace("\n", "\n[boot] "))
        return 1

    # 阶段②收尾：**必须在 spawn 之前**释放，否则自己把端口挡住了
    release_port(hold, PORT)

    # ⚠️ stdout/stderr 必须**落文件**，绝不能接成无人消费的 PIPE。
    # Windows 匿名管道缓冲区只有 4KB：管道写满后，服务端进程写日志会被
    # 永久阻塞，表现成「服务端整体失去响应」，而客户端只看到读超时。
    # 见 SERVER_LOG 常量处的完整说明。
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
            print(f"[boot] server did not become ready. log tail ({SERVER_LOG}):")
            print(_read_log_tail())
            return 1
        # 阶段③：就绪了也要确认输出里**没有 bind 失败痕迹**。必须做：bind 失败的
        # 进程可能「活着但没在监听」，而 wait_ready 探到的是**别人的服务**——
        # 这是假绿，比超时危险。此前只 import 了 detect_addr_in_use 却从未调用，
        # 第三段形同虚设，这里补上真正的调用。
        # 本脚本的子进程输出走 SERVER_LOG 文件（不是 PIPE，见上），所以从文件读。
        if detect_addr_in_use(_read_log_tail()):
            print(f"[boot] ✗ 端口 {PORT} bind 失败（占位未生效，疑似被别人抢占）。")
            print("[boot]   为避免把断言打到别人的服务上，本次结果**不可信**，直接退出。")
            print("[boot]   请换端口重跑：python tools/smoke_home.py <其它端口>")
            return 1
        print("[boot] ready\n")

        # ==================================================== 准备账号
        code, tk = req("POST", "/api/auth/login", {"username": "admin", "password": LEGACY_PW})
        check("登录默认管理员", code == 200 and bool(tk.get("access_token")), str(code))
        # 平台 AI 配置现在是**仅管理员**可写（原先任何登录用户都能建/改/删，
        # 那正是 P0-1 事故链路的源头），所以建配置要用管理员 token。
        admin_token = tk.get("access_token", "")

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
        # ⚠️ 首页不再返回 membership 段（会员制已取消）。断言「没有」而不是
        # 「为空」—— 后者对 {"membership": {}} 也成立，挡不住「删字段」漏改成空壳。
        check("首页不再返回 membership 段（会员制已取消）",
              "membership" not in s0, ",".join(sorted(s0.keys())))

        # ==================================================== 准备：u1 绑定设备
        # ⚠️ 2026-10-20 契约变更（D44 §3）后这条夹具有两种写法都错：
        #   「建档 → /code 取码 → bind」：建档即归属（create_device 直接写 owner_id），
        #   所以设备**已是已绑定态**，`POST /api/devices/{pk}/code` 返回 409；
        #   对照真实链路也不该这么走 —— 码是设备自己开机向 OTA 端点申领的。
        # 因此改为「建档 → 带 Device-Id 打 OTA → 取 websocket 段的 device_token」，
        # 与固件完全相同的一条路。**别改回取码绑定的写法**：那会把 409 当成
        # 「设备绑定失败」写进断言，看起来像服务端坏了。
        code, dev = req("POST", "/api/devices", {"device_id": "homedev000001"}, u1)
        dev_pk = dev.get("id", 0)
        code, ota = req("GET", "/api/voice/ota", None, None,
                        {"Device-Id": "homedev000001"})
        dev_token = ((ota or {}).get("websocket") or {}).get("token", "") if isinstance(ota, dict) else ""
        check("u1 绑定设备（建档即归属，device_token 由 OTA 下发）",
              code == 200 and bool(dev_token),
              f"code={code} keys={list(ota) if isinstance(ota, dict) else ota}")

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
        # 角色走 /api/roles（平台配置走 /api/providers，两者已彻底拆分：
        # /api/providers 只接受带 kind 的平台配置，角色建在它上面会被 422）。
        # 4.1 只传角色的字段就要能建成（APP createAgentRole 就是这么发的）
        code, role = req("POST", "/api/roles", {
            "name": "小暖医生",
            "description": "温和的服药提醒助手",
            "system_prompt": "你是耐心的服药提醒助手",
            "tts_voice": "zh-CN-XiaoxiaoNeural",
        }, u1)
        role_id = role.get("id", 0)
        check("只传角色字段即可创建（不再 422）", code == 201 and bool(role_id), f"code={code}")
        check("角色字段原样返回",
              role.get("description") == "温和的服药提醒助手"
              and role.get("system_prompt") == "你是耐心的服药提醒助手"
              and role.get("tts_voice") == "zh-CN-XiaoxiaoNeural",
              str({k: role.get(k) for k in ("description", "system_prompt", "tts_voice")}))
        check("角色记录 record_type='role'、parent 为空",
              role.get("record_type") == "role" and role.get("parent_id") is None,
              f"{role.get('record_type')} parent={role.get('parent_id')}")

        # 4.2 完整 AI 平台配置的行为照旧（但仅管理员可建）
        code, plat = req("POST", "/api/providers", {
            "name": "主力对话", "kind": "llm", "provider": "deepseek",
            "model": "deepseek-chat", "api_key": "sk-should-not-leak", "priority": 1,
        }, admin_token)
        check("完整 AI 平台配置照旧可建（管理员）",
              code == 201 and plat.get("kind") == "llm", str(code))
        code, _ = req("POST", "/api/providers", {"name": "普通用户建配置", "kind": "llm"}, u1)
        check("普通用户建平台配置 -> 403", code == 403, f"code={code}")

        # 4.3 脱敏列表只含平台配置、且不能泄密钥；角色改由 /api/roles 提供
        code, safe = req("GET", "/api/providers/safe", token=u1)
        safe_rows = safe if isinstance(safe, list) else []
        check("/safe 返回含平台配置字段",
              all({"id", "name", "kind", "provider", "endpoint", "model"} <= set(r)
                  for r in safe_rows),
              f"{len(safe_rows)} 条")
        check("/safe 不含 api_key", not any("api_key" in p for p in safe_rows),
              f"{len(safe_rows)} 条")
        check("/safe 不再混角色（角色走 /api/roles）",
              all(p.get("kind") for p in safe_rows),
              ",".join(str(p.get("kind")) for p in safe_rows))
        code, _ = req("GET", "/api/providers/safe")
        check("/safe 未登录访问 -> 401（P0-2 止血）", code == 401, f"code={code}")
        code, roles_of_u1 = req("GET", "/api/roles", token=u1)
        check("/api/roles 分区含新建的角色",
              code == 200 and any(r.get("id") == role_id
                                  for r in (roles_of_u1 or {}).get("mine", [])),
              str([r.get("id") for r in (roles_of_u1 or {}).get("mine", [])]))

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

        # P0-4：平台 LLM 配置不能被绑成角色（原先只查存在性）
        code, _ = req("POST", f"/api/devices/{dev_pk}/agent", {"role_id": plat.get("id", 0)}, u1)
        check("把平台 LLM 配置绑成角色 -> 404", code == 404, f"code={code}")

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

        # ==================================================== 3.5 会员端点已下线
        # 会员制取消后 /api/membership* 与 /api/admin/membership* 全部 404。
        # 老前端 / 老 APP 缓存仍可能打过来，这里钉住「必须是 404」而不是 500 ——
        # 500 说明路由还在、只是内部炸了，属于另一种缺陷。
        for _path in ("/api/membership", "/api/membership/plans",
                      "/api/membership/orders/mine", "/api/membership/payment-info"):
            code, _ = req("GET", _path, token=u1)
            check(f"会员端点已下线 {_path}", code == 404, f"code={code}")
        code, _ = req("POST", "/api/admin/membership/grant",
                       {"user_id": me.get("id", 0), "days": 10}, tk.get("access_token", ""))
        check("管理端授予会员接口已下线", code == 404, f"code={code}")
        # 首页对**任何**用户都不再出现 membership 段（不是「非会员才没有」）
        code, s5 = req("GET", "/api/home/summary", token=u1)
        check("有设备的用户首页也没有 membership 段",
              code == 200 and "membership" not in s5,
              ",".join(sorted(s5.keys())) if isinstance(s5, dict) else str(s5))

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

        # ============================================ 6. 后台任务健康快照
        # 周期任务可能在后台连着失败、而业务 API 照旧 200，所以要有专门端点能看出来；
        # 同时必须守住 /api/health 只表达「进程活着」，不能被后台任务状态污染
        # （Dockerfile 的 HEALTHCHECK 每 30 秒打它一次，非 200 就会把容器判死）。
        code, hs = req("GET", "/api/health/scheduler")
        check("后台任务健康端点返回 200", code == 200, str(code))
        check("健康快照含三个约定字段",
              {"last_ok_at", "consecutive_failures", "last_error"} <= set((hs or {}).keys()),
              str(sorted((hs or {}).keys())))
        check("刚起来时连续失败次数为 0", (hs or {}).get("consecutive_failures") == 0,
              str((hs or {}).get("consecutive_failures")))
        # 端点只是「如实反映模块状态」，这里进程内改坏状态验证函数不是永远返回 0
        # （服务端跑在子进程里，改不到它的模块变量，故函数级断言放本地做）
        from app import scheduler as _sched
        _sched._SCHEDULER_HEALTH["consecutive_failures"] = 3
        _sched._SCHEDULER_HEALTH["last_error"] = "boom"
        snap = _sched.scheduler_health()
        check("状态改坏后快照如实反映次数", snap.get("consecutive_failures") == 3,
              str(snap.get("consecutive_failures")))
        check("状态改坏后快照如实反映错误信息", snap.get("last_error") == "boom",
              str(snap.get("last_error")))
        # 快照必须是副本，否则调用方能改到内部状态
        snap["consecutive_failures"] = 999
        check("快照是副本（不会污染内部状态）",
              _sched.scheduler_health().get("consecutive_failures") == 3,
              str(_sched.scheduler_health().get("consecutive_failures")))
        # 守门：探活端点绝不能受后台任务状态影响
        code, hc = req("GET", "/api/health")
        check("探活端点不受后台任务状态影响，仍为 200", code == 200, str(code))
        check("探活端点仍只表达进程存活", (hc or {}).get("ok") is True, str(hc))

        # 进程内开过一次连接，先释放再删临时库，否则 Windows 上文件被占着删不掉
        engine.dispose()

    finally:
        # 收尾统一走 smoke_common.cleanup_db：**先停子进程 → 再关句柄 → 最后删库**
        # （顺序反了就是 WinError 32），删不掉只打警告、**绝不把「断言全绿」
        # 打成 exit 1 + traceback** —— 清理是卫生问题，不该污染正确性结论。
        #
        # ⚠️ 本脚本比别的冒烟脚本多一个句柄来源：第 5 段「离线清扫」在**本进程内**
        # import 了 app.db 的 engine 并开了 Session（为了进程内跑 scheduler._tick）。
        # SQLAlchemy 连接池会一直攥着 SQLite 文件，而原来那行 `engine.dispose()`
        # 写在 try 体**末尾** —— 断言中途抛异常时根本走不到，于是收尾 unlink
        # 必然 PermissionError。这是本脚本 WinError 32 的真实根因之一，
        # 所以 dispose 必须挪进 finally（放在停进程之前，先松本进程自己的句柄）。
        try:
            engine.dispose()          # noqa: F821  第 5 段 import 的，可能尚未定义
        except Exception:             # noqa: BLE001, S110  收尾失败不影响判定
            pass
        cleanup_db(DB_FILE, procs=(proc,), handles=(log_fp,))
        # 失败时把服务端日志尾部打出来：定位卡点/500 最直接的证据
        failed = [r for r in results if not r[1]]
        if failed:
            print(f"\n[diag] ---- server log tail ({SERVER_LOG}) ----")
            print(_read_log_tail())

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n==== {passed}/{total} passed ====")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
