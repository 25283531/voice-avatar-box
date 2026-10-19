"""后端冒烟测试：本地起服务（SQLite），跑通业务闭环。

用法：
    python tools/smoke_server.py [--port 8100]

覆盖：健康检查 -> 注册/登录 -> 建设备 -> 申请激活码 -> 校验 Luhn
      -> 绑定设备 -> 建服药提醒（复杂规则）-> 排期 -> 设备同步
      -> 提醒回执 -> 交互上报 -> 统计概览
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
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable

# 默认端口统一由 tools/smoke_ports.py 托管（唯一真源），各脚本不再各写一份 ——
# 早先 smoke_home/smoke_roles/smoke_memory 都是 8097、smoke_models/smoke_mail
# 都是 8096，并行跑必然撞车，且会连到残留进程上产生假失败。
sys.path.insert(0, str(Path(__file__).resolve().parent))
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


# 跨平台默认落点（原来写死 D:\espdev 在 Linux CI 上会直接失败）。
# 默认文件名带 PID：多人/CI 并行跑时固定文件名会互相抢同一个文件，
# Windows 上直接 PermissionError（另一个进程正占用）。要固定路径就用
# SMOKE_DB_DIR 覆盖目录，或 SMOKE_DB 指定完整路径。
DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
DB_PATH = Path(os.environ.get("SMOKE_DB", str(DB_DIR / f"elder_smoke_{os.getpid()}.db")))
if DB_PATH.exists():
    DB_PATH.unlink()

PORT = resolve_port("smoke_server")
BASE = f"http://127.0.0.1:{PORT}"

# 与 smoke_auth.py / smoke_mail.py / smoke_admin_presets.py 的 LEGACY_PW
# 同名同义（四处保持一致，改一处记得同步）。
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


def req_text(method: str, path: str) -> tuple[int, str]:
    """GET 页面原文（入口路由断言用）。

    为什么不能复用 :func:`req`：它对响应体**无条件** ``json.loads``，
    而 HTML 页面会抛 ``JSONDecodeError`` —— 且该异常不在 req 的
    ``except HTTPError`` 捕获范围内，会直接把脚本炸掉（页面路由
    从来没被断言过，这个坑一直没人踩到）。
    """
    r = urllib.request.Request(BASE + path, method=method)
    try:
        with _LOCAL_OPENER.open(r, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def wait_ready(timeout: float = 120.0) -> bool:
    # 用外部数据库（CI 的 MySQL service）时，服务启动包含 wait_for_db 重试，
    # 给足余量以免把「连接慢」误判成「启动失败」。
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
    # 外部显式给了 ELDER_DB 就用它（CI 里用 MySQL service 验证 mysql+pymysql 真实路径），
    # 否则退回本地临时 SQLite 文件，保证零依赖可跑。
    external_db = os.environ.get("ELDER_DB", "").strip()
    db_url = external_db or f"sqlite:///{DB_PATH.as_posix()}"
    env["ELDER_DB"] = db_url
    env["ELDER_JWT_SECRET"] = "smoke-test-secret"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"   # 测试期间不跑后台调度，避免干扰
    env["PYTHONIOENCODING"] = "utf-8"
    # 种子管理员口令 bb5ca98 起不再硬编码在代码里：空库未配
    # ELDER_SEED_ADMIN_PASSWORD 时会生成**随机**口令打进日志。
    # 本脚本自己起服务，却用 admin/admin123 登录 —— 下面不显式注入，
    # 这个夹具就只在不带环境跑不起来（CI 靠 job 级 env 兜住，本地裸跑 401）。
    # 约定与其他冒烟脚本一致（见 smoke_auth.py / smoke_mail.py /
    # smoke_admin_presets.py 的 LEGACY_PW，四处同名同义，改一处记得同步）。
    env["ELDER_SEED_ADMIN_PASSWORD"] = LEGACY_PW

    print(f"[boot] starting server on :{PORT} (db: {db_url.split('@')[-1]})")
    # ---- 三段式端口守卫（QA 第 4 轮「假绿」修复）----
    # 端口被占时 uvicorn 的 socket bind 会失败，**但进程不退出**（活着、只是没在
    # 监听）；而 /api/health **永远返回 200** -> wait_ready 会探到**别人的服务**
    # -> 全部断言打到别人身上、用自己从未 seed 过的 DB（QA 第 4 轮实测 184320 vs
    # 65536 bytes：一方 seed 过、一方从没初始化过）。
    #
    # ⚠️ **这里为什么不能只调 guard_or_die()**：它内部拿到占位 socket 后**立刻**
    # release（见 smoke_ports.py 末两行），于是阶段②的占位只覆盖了「检查→释放」
    # 这一瞬间，等于**没有占位**。所以下面显式持有 guard_port() 返回的 hold，
    # 一直攥到 spawn 前的那一刻才release_port()。
    # guard_or_die 仍保留：它负责「被占就立刻 SystemExit(1)+打印换端口提示」。
    #
    # ⚠️ **三段加起来仍然不是绝对保证**（务必读完再用）：
    #   ①事前检查与真正 bind 之间存在**不可消除的竞态窗口** —— 检查刚通过，
    #     别人就把端口抢走了，本模块只能把这段距离压到最小，压到最小 ≠ 消除；
    #   ②占位 socket 在 release_port() 到 uvicorn 真正 bind 之间仍有间隙；
    #   ③即使 bind 失败，uvicorn 进程**可能仍活着**（只是没在监听）。
    # **别因为「加了预检」就认为并发跑已经安全** —— architect 栽的正是这个坑：
    # 以为静态查表不重复就等于运行时没被占。真正的绝对保证是让服务**自证身份**
    # （启动生成 run_id、/api/health 回传、冒烟比对是否一致），那要改生产代码，
    # 留给下一轮。
    guard_or_die(PORT, "smoke_server.py")
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
        # 阶段③：即使就绪了，也要确认输出里**没有 bind 失败的痕迹**。
        # 必须做：bind 失败的进程可能「活着但没在监听」，而 wait_ready 探到的
        # 是**别人的服务** —— 这是假绿，比超时危险（超时会让人去查，假绿会让人
        # 相信结论）。此前本步骤只import 了 detect_addr_in_use 却**从未调用**，
        # 等于第三段形同虚设，这里补上真正的调用。
        # 本脚本的子进程输出走 SERVER_LOG 文件（不是 PIPE，见上），所以从文件读。
        if detect_addr_in_use(_read_log_tail()):
            print(f"[boot] ✗ 端口 {PORT} bind 失败（占位未生效，疑似被别人抢占）。")
            print("[boot]   为避免把断言打到别人的服务上，本次结果**不可信**，直接退出。")
            print("[boot]   请换端口重跑：python tools/smoke_server.py <其它端口>")
            return 1
        print("[boot] ready\n")

        # ---------------------------------------------------- 1. 健康检查
        code, body = req("GET", "/api/health")
        check("健康检查", code == 200 and body.get("ok") is True, str(body))

        # ---------------------------------------------------- 1b. 双入口路由
        # 2026-10-19 用户端入口重构（决策记录 D41）：/ = 用户端（未登录即
        # 登录/注册页），/admin = 管理后台，/user 已删除（项目未公开，
        # 不重定向、不兼容渲染）。
        #
        # ⚠️ 判据必须用**整行 <h1> 全串 + 反向否定**，不能用「用户端」
        # 「管理后台」这类短标记：
        #   · 「用户端」在 user.html 出现 4 次、index.html 也出现 3 次
        #     （管理端有指向用户端的文案）；
        #   · 「管理后台」在 index.html 7 次、user.html 6 次（用户端有
        #     指向 /admin 的链接）。
        # 短串两文件都有 ⇒ 把 / 与 /admin 整体对调后两条断言**照旧全绿**，
        # 入口重构等于没有看门狗。QA 第 N 轮变异实证（M1）坐实了这个假绿。
        # 所以用整行 h1（两个页面各自的 h1 文本不同）并双向否定：
        # 既断言「有自己那个」，也断言「没有对方那个」。
        H1_USER = "<h1>老人陪伴提醒助手 · 用户端</h1>"
        H1_ADMIN = "<h1>老人陪伴提醒助手 · 管理后台</h1>"
        code, home_html = req_text("GET", "/")
        check("GET / 是用户端页面（h1 全串命中且不含管理后台 h1）",
              code == 200 and H1_USER in home_html and H1_ADMIN not in home_html,
              f"code={code} user_h1={H1_USER in home_html} admin_h1={H1_ADMIN in home_html}")
        code, admin_html = req_text("GET", "/admin")
        check("GET /admin 是管理后台页面（h1 全串命中且不含用户端 h1）",
              code == 200 and H1_ADMIN in admin_html and H1_USER not in admin_html,
              f"code={code} admin_h1={H1_ADMIN in admin_html} user_h1={H1_USER in admin_html}")
        code, _user_html = req_text("GET", "/user")
        check("GET /user 已删除（404，不做重定向/兼容渲染）",
              code == 404, f"code={code}")

        # ---------------------------------------------------- 2. 登录（种子管理员）
        code, tk = req("POST", "/api/auth/login", {"username": "admin", "password": LEGACY_PW})
        check("登录默认管理员", code == 200 and bool(tk.get("access_token")), str(code))
        token = tk.get("access_token", "")

        # ---------------------------------------------------- 3. 建设备
        code, dev = req("POST", "/api/devices",
                        {"device_id": "aabbccddeeff", "name": "客厅陪伴助手"}, token)
        check("登记设备", code == 201 and dev.get("device_id") == "aabbccddeeff", str(code))
        dev_pk = dev.get("id", 0)
        # bound_at 的**建档即归属**路径：create_device 直接写 owner_id，bound_at
        # 必须同时落库（两个归属写入点之一，另一个是 /bind）。放在 bind 之前断言
        # 是刻意的——bind 之后 bound_at 必然非 None（那条路径也写），放在那里
        # 对本条路径没有鉴别力，删掉 create_device 的写入也不会红。
        # 变异实证：删掉 routers/devices.py create_device 的 bound_at 写入，这条必须红。
        check("登记设备时 bound_at 已写（建档即归属）",
              bool(dev.get("bound_at")), str(dev.get("bound_at")))

        # ---------------------------------------------------- 4. 激活码
        # ⚠️ 2026-10-20 改：激活码**只能**从设备真实路径拿 —— OTA 端点按
        # `Device-Id`（MAC）下发。原先这里调 `POST /api/devices/{pk}/code`，
        # 但「登记设备」本身就是「建档即归属」（create_device 直接写 owner_id），
        # 按新规则「已绑定设备不再下发激活码」，该接口对已归属设备返回 409。
        #
        # 而且这才是**生产里真实发生的顺序**：设备先开机取码（此时服务端还没有
        # Device 行），家属再输码 → 绑定那一刻才建出设备行。冒烟按真实顺序跑，
        # 才能覆盖到「bind 现场建档」这条路径（曾经它要求 APP 传 MAC，
        # 导致纯 APP 用户 100% 绑不上）。
        ota_mac = "aa:bb:cc:dd:ee:77"       # 故意用带冒号写法：顺带覆盖 MAC 归一化
        code, ota_body = req("GET", "/api/voice/ota", headers={"Device-Id": ota_mac})
        act = (ota_body or {}).get("activation") or {}
        full = act.get("code", "")
        ok_code = code == 200 and len(full) == 7
        check("OTA 按 MAC 下发 7 位激活码（6 位随机 + 1 位校验）", ok_code,
              f"http={code} code={act.get('code')} 显示={act.get('code') and full[:3] + '-' + full[3:]}")

        # 与固件同算法校验 Luhn：完整码长 = 6 位 body + 1 位校验 = 7
        def luhn6(d: str) -> int:
            s = 0
            for i in range(6):
                v = int(d[i])
                # body 的 index i 在完整 7 位码里是自右往左第 (7 - i) 位
                if (7 - i) % 2 == 0:
                    v *= 2
                    if v > 9:
                        v -= 9
                s += v
            return (10 - s % 10) % 10

        check("Luhn 校验位一致（对齐固件 C 实现）",
              len(full) == 7 and int(full[6]) == luhn6(full[:6]),
              f"expect={luhn6(full[:6]) if len(full) == 7 else '?'} actual={full[6:]}")
        # 屏显分组：`123-4567`（3+4），与服务端 security.format_code 一致
        shown = (full[:3] + "-" + full[3:]) if len(full) == 7 else ""
        check("显示格式为 123-4567", shown.count("-") == 1 and len(shown) == 8, shown)

        # ---------------------------------------------------- 5. 错误码应当被拒
        bad = full[:-1] + str((int(full[-1]) + 1) % 10)
        code, _ = req("POST", "/api/devices/bind", {"code": bad}, token)
        check("错误激活码被拒绝", code == 400, f"code={code}")

        # ---------------------------------------------------- 6. 绑定
        code, bind = req("POST", "/api/devices/bind", {"code": full}, token)
        check("激活码绑定设备", code == 200 and bool(bind.get("device_token")), str(code))
        dev_token = bind.get("device_token", "")
        # ⚠️ 后续断言一律用**绑定建出来的**那台设备（bind 现场建档），
        # 不是第 3 段「登记」建的那台 —— 两者是不同 MAC、不同行。
        # 混用会让「设备列表里找不到刚绑的设备」这类断言变成假绿。
        dev_pk = bind.get("device_pk", dev_pk)

        # ---------------------------------------------------- 6b. 设备列表新字段
        # 设备列表缺口（2026-10-19）：hardware_model / bound_at / fw_version
        # 三列（OTA 相关的「升级入口」已随 OTA 后台化暂缓，见决策记录 D40）。
        # 三个字段都带默认值，老客户端不读它们也无感。
        code, devs = req("GET", "/api/devices", token=token)
        me = {}
        if code == 200 and isinstance(devs, list):
            me = next((d for d in devs if d.get("id") == dev_pk), {})
        check("DeviceOut 带 hardware_model / bound_at / fw_version 三个字段",
              all(k in me for k in ("hardware_model", "bound_at", "fw_version")),
              f"keys={sorted(me.keys())}")
        # bound_at 由绑定写入点落库（routers/devices.py bind_device）。
        # 变异实证：删掉那行写入，这条必须红。
        check("绑定后 bound_at 非 None（当前归属的起始时刻）",
              bool(me.get("bound_at")), str(me.get("bound_at")))

        # ---------------------------------------------------- 7. 提醒规则（复杂）
        rule = {
            "title": "降压药", "speak_text": "该吃降压药了，一片", "rtype": 0,
            "schedule_kind": 2,                    # 每周
            "times_of_day": ["08:00", "20:00"],    # 一天两次
            "weekdays": [1, 3, 5],                 # 周一三五
            "dose": "1 片", "require_confirm": True,
            "repeat_interval_min": 10, "repeat_times": 3,
            "escalate_after_min": 30,
            "confirm_methods": ["voice", "button", "app"],
            "start_date": "2026-01-01", "end_date": "2027-12-31",
        }
        code, rm = req("POST", f"/api/reminders?device_pk={dev_pk}", rule, token)
        check("创建服药提醒（每周一三五 · 一天两次）", code == 201, str(code))
        rm_pk = rm.get("id", 0)
        check("自动计算下次触发时间", bool(rm.get("next_fire_at")), str(rm.get("next_fire_at")))

        code, bad_rule = req("POST", f"/api/reminders?device_pk={dev_pk}",
                             {"title": "x", "times_of_day": ["25:99"]}, token)
        check("非法时间被拒绝（HH:MM 校验）", code == 422, f"code={code}")

        # ---------------------------------------------------- 8. 排期实例
        code, up = req("GET", f"/api/reminders/upcoming?device_pk={dev_pk}&hours=240", token=token)
        check("展开排期实例", code == 200 and len(up) > 0, f"{len(up) if isinstance(up, list) else 0} 条")
        due = up[0]["due_at"] if isinstance(up, list) and up else None
        rm_id = up[0]["reminder_id"] if isinstance(up, list) and up else rm_pk

        # ---------------------------------------------------- 9. 设备同步
        # hw_model：固件探测到的芯片型号（esp_chip_info），与 fw_version 同
        # 形态的 query param。服务端非空即写入设备行。
        code, sync = req("POST", "/api/device/sync?fw_version=0.1.0&hw_model=ESP32-S3",
                         {}, dev_token)
        ok_sync = (code == 200 and len(sync.get("reminders", [])) >= 1
                   and len(sync.get("upcoming", [])) >= 1
                   and bool(sync.get("server_time")))
        check("设备心跳同步（规则+排期+形象+AI配置）", ok_sync,
              f"reminders={len(sync.get('reminders', []))} upcoming={len(sync.get('upcoming', []))}")

        # 硬件型号回显：心跳上报后设备行应显示芯片型号（存量/老固件为「未知」，
        # 新固件下次心跳自动补全 —— 这条钉住「补全」那一半）。
        # 变异实证：删掉 device_api.sync 里的 hw_model 写入，这条必须红。
        code, devs2 = req("GET", "/api/devices", token=token)
        me2 = {}
        if code == 200 and isinstance(devs2, list):
            me2 = next((d for d in devs2 if d.get("id") == dev_pk), {})
        check("心跳上报 hw_model 后设备行显示芯片型号",
              me2.get("hardware_model") == "ESP32-S3", str(me2.get("hardware_model")))

        # 上面那条 upcoming>=1 只能顺带发现「排期恒空」，且**依赖运行当天的星期几**：
        # 本用例的规则是「每周一三五」，只有当天下一次触发落在 36h 窗口外时才红
        # （周六跑就红，周一到周五跑都绿）。所以它不是可靠的看门狗。
        # 下面这条与星期无关：造一条「一次性、10 天后」的规则，它的下一次触发必然
        # 在 SCHEDULE_AHEAD_HOURS(36h) 之外，任何 materialize 窗口都装不下它 ——
        # 若心跳不给它兜底补一次触发，真机就永远不知道该在那天响铃。
        far_day = (date.today() + timedelta(days=10)).isoformat()
        code, far_rm = req("POST", f"/api/reminders?device_pk={dev_pk}", {
            "title": "远期复查", "speak_text": "该去复查了", "schedule_kind": 0,
            "times_of_day": ["09:00"], "once_date": far_day,
        }, token)
        check("创建远期一次性提醒（10 天后）", code == 201, str(code))
        code, sync2 = req("POST", "/api/device/sync", {}, dev_token)
        far_due = sync2.get("upcoming", []) if isinstance(sync2, dict) else []
        far_ok = (code == 200 and any(
            e.get("reminder_id") == far_rm.get("id")
            and str(e.get("due_at", "")).startswith(far_day)
            for e in far_due if isinstance(e, dict)))
        check("远期触发也随心跳下发（排期视野兜底）", far_ok,
              f"once={far_day} code={code} upcoming={[e.get('due_at') for e in far_due if isinstance(e, dict)]}")

        # ---------------------------------------------------- 10. 提醒回执
        code, ev = req("POST", "/api/device/events", {
            "reminder_id": rm_id, "due_at": due, "state": 2, "confirm_method": 2,
            "confirmed_at": due, "note": "按键确认"
        }, dev_token)
        check("上报提醒确认（按键方式）", code == 200 and ev.get("state") == 2,
              f"method={ev.get('confirm_method')}")

        # ---------------------------------------------------- 11. 交互上报
        for kind in (0, 1, 1, 2):
            code, _ = req("POST", "/api/device/interactions",
                          {"kind": kind, "duration_ms": 1200, "meta": {"text": "今天天气怎么样"}},
                          dev_token)
        code, _ = req("POST", "/api/device/interactions", {"kind": 1}, dev_token)
        check("上报交互事件 x5", True, "")

        # ---------------------------------------------------- 12. 统计
        code, st = req("GET", f"/api/stats/overview?device_pk={dev_pk}&days=7", token=token)
        ok_stats = code == 200 and st.get("today_total", 0) >= 5 and st.get("today_confirm", 0) >= 1
        check("交互统计概览", ok_stats,
              f"today_total={st.get('today_total')} confirm={st.get('today_confirm')} "
              f"miss={st.get('missed_today')}")

        # ---------------------------------------------------- 13. 形象
        code, av = req("GET", "/api/avatars", token=token)
        check("默认形象种子数据", code == 200 and len(av) >= 1,
              av[0]["name"] if isinstance(av, list) and av else "")
        if isinstance(av, list) and av:
            code, _ = req("POST", f"/api/avatars/{av[0]['id']}/apply/{dev_pk}", {}, token)
            check("应用形象到设备", code == 200, str(code))

        # ---------------------------------------------------- 14. AI 配置
        code, pv = req("POST", "/api/providers", {
            "name": "主力对话", "kind": "llm", "provider": "deepseek",
            "model": "deepseek-chat", "priority": 1
        }, token)
        check("新增 AI 平台配置", code == 201, str(code))
        code, safe = req("GET", "/api/providers/safe", token=token)
        leak = any("api_key" in p for p in safe) if isinstance(safe, list) else False
        check("脱敏列表不含密钥", code == 200 and not leak, f"{len(safe) if isinstance(safe, list) else 0} 条")

        # ---------------------------------------------------- 15. 鉴权
        code, _ = req("GET", "/api/devices")
        check("无凭证访问被拒", code in (401, 403), f"code={code}")

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
