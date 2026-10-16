"""会员系统冒烟测试（人工核销版）。

用法：
    python tools/smoke_membership.py [--port 8106]

覆盖：
    A. 用户端：会员状态 / 套餐 / 下单 / 订单号冲突 409 / 金额区间内随机（不要求唯一） /
       频率限制 429 / 已支付通知 / 放弃 / 收款信息
    B. 管理端：配置读写 / 订单列表 / 按订单号查金额 / **按金额反查订单号** /
       确认到账（幂等）/ 驳回 / 后台直接分配 / 过期清理 / 非管理员 403
    C. 设备端：当日配额 / 陪聊时长上报（提醒类不计入配额）

另外有一段**进程内**用例，直接调 app.membership 验证：
    - 连续分配 20 个金额都落在区间内（金额不独占，允许重复）
    - 窄区间（仅 6 个候选值）下仍能连续分配，不会因「被占满」抛 409
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

# 默认端口统一由 tools/smoke_ports.py 托管（唯一真源），各脚本不再各写一份 ——
# 早先 smoke_server 是 8099，本脚本是 8098，而 CI 的 MySQL job 又拿 8098 跑
# smoke_server，两边端口表对不上。并行跑必然撞车。
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


# 默认文件名带 PID：多人/CI 并行跑时固定文件名会互相抢同一个文件，
# Windows 上直接 PermissionError（另一个进程正占用）。要固定路径就用
# SMOKE_DB_DIR 覆盖目录，或 SMOKE_MEMBER_DB 指定完整路径。
DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
DB_PATH = Path(os.environ.get("SMOKE_MEMBER_DB",
                              str(DB_DIR / f"elder_smoke_member_{os.getpid()}.db")))
if DB_PATH.exists():
    DB_PATH.unlink()

PORT = resolve_port("smoke_membership")
BASE = f"http://127.0.0.1:{PORT}"

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
    env["ELDER_JWT_SECRET"] = "smoke-member-secret"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"   # 不跑后台调度，过期清理走手动接口
    env["PYTHONIOENCODING"] = "utf-8"

    print(f"[boot] starting server on :{PORT} (db: {DB_PATH})")
    # ---- 三段式端口守卫（QA 第 4 轮「假绿」修复）----
    # 端口被占时 uvicorn 的 socket bind 会失败，**但进程不退出**（活着、只是没在
    # 监听）；而 /api/health **永远返回 200** -> wait_ready 会探到**别人的服务**
    # -> 全部断言打到别人身上、用自己从未 seed 过的 DB。
    # 三段：①事前 is_port_free ②socket 占位缩小竞态窗口 ③起服务后校验 stderr
    # ⚠️ 三段加起来**仍不是绝对保证**（①与真正 bind 之间有不可消除的竞态窗口）。
    #    真正的绝对保证是让服务自证身份（run_id），需改生产代码，留给下一轮。
    #
    # ⚠️ **不能只调 guard_or_die()**：它拿到占位 socket 后**立刻** release
    #（见 smoke_ports.py 末两行），阶段②的占位只覆盖「检查→释放」一瞬间，等于
    # 没有占位。故下面显式持有 guard_port() 的 hold，攥到 spawn 前才 release。
    # guard_or_die 保留：负责「被占就立刻 SystemExit(1)+打印换端口提示」。
    guard_or_die(PORT, "smoke_membership.py")
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
        # 进程可能「活着但没在监听」，而 wait_ready 探到的是**别人的服务」——
        # 这是假绿，比超时危险。此前只 import 了 detect_addr_in_use 却从未调用，
        # 第三段形同虚设，这里补上真正的调用。
        # 本脚本的子进程输出走 SERVER_LOG 文件（不是 PIPE，见上），所以从文件读。
        if detect_addr_in_use(_read_log_tail()):
            print(f"[boot] ✗ 端口 {PORT} bind 失败（占位未生效，疑似被别人抢占）。")
            print("[boot]   为避免把断言打到别人的服务上，本次结果**不可信**，直接退出。")
            print("[boot]   请换端口重跑：python tools/smoke_membership.py <其它端口>")
            return 1
        print("[boot] ready\n")

        # ==================================================== 准备账号
        code, tk = req("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
        check("登录默认管理员", code == 200 and bool(tk.get("access_token")), str(code))
        admin_token = tk.get("access_token", "")

        def reg(name: str) -> str:
            c, _ = req("POST", "/api/auth/register",
                       {"username": name, "password": "test123456", "nickname": name})
            c, t = req("POST", "/api/auth/login", {"username": name, "password": "test123456"})
            return t.get("access_token", "")

        u1, u2, u3 = reg("mem1"), reg("mem2"), reg("mem3")
        check("注册并登录 3 个普通用户", all([u1, u2, u3]), "")

        # 用户 1 建设备并绑定，拿到设备 token
        code, dev = req("POST", "/api/devices", {"device_id": "memdev000001"}, u1)
        dev_pk = dev.get("id", 0)
        code, cd = req("POST", f"/api/devices/{dev_pk}/code", {}, u1)
        code, bind = req("POST", "/api/devices/bind", {"code": cd.get("code", "")}, u1)
        dev_token = bind.get("device_token", "")
        check("用户 1 绑定设备", bool(dev_token), str(code))

        # ==================================================== 1. 初始会员状态
        code, ms = req("GET", "/api/membership", token=u1)
        check("初始非会员", code == 200 and ms.get("is_vip") is False, str(ms.get("status")))
        check("非会员配额上限 3600 秒",
              ms.get("quota", {}).get("limit_seconds") == 3600
              and ms.get("quota", {}).get("unlimited") is False,
              str(ms.get("quota")))
        check("空闲切断 90 秒", ms.get("quota", {}).get("idle_timeout_seconds") == 90, "")
        # benefits 是给 APP 直接 join 展示的字符串数组（对字典调 join 会崩），
        # 结构化明细另放在 benefits_detail 里
        check("权益是字符串数组（APP 直接 join）",
              isinstance(ms.get("benefits"), list)
              and all(isinstance(x, str) for x in ms["benefits"])
              and " · ".join(ms["benefits"]) != "",
              str(ms.get("benefits")))
        check("权益明细保留在 benefits_detail",
              ms.get("benefits_detail", {}).get("reminder_always_free") is True,
              str(ms.get("benefits_detail")))

        # ==================================================== 2. 套餐
        code, plans = req("GET", "/api/membership/plans", token=u1)
        check("套餐列表（月/季/年）", code == 200 and len(plans) == 3,
              ",".join(p["code"] for p in plans) if isinstance(plans, list) else str(code))

        # ==================================================== 3. 管理端配置
        code, cfg = req("PATCH", "/api/admin/membership/config", {
            # 默认区间是 10.00~12.00 元；这里显式写一遍，顺带验证可写
            "amount_range": {"min_cents": 1000, "max_cents": 1200, "enabled": True},
            "payment": {"channel": "微信", "payee_name": "张三", "account": "wxp://test",
                        "qr_url": "https://example.com/qr.png", "note": "转账请备注订单号"},
        }, admin_token)
        check("管理端写入配置", code == 200 and cfg["amount_range"]["max_cents"] == 1200,
              str(code))
        check("收款信息回读一致", cfg["payment"]["payee_name"] == "张三", "")

        code, bad_cfg = req("PATCH", "/api/admin/membership/config",
                            {"amount_range": {"min_cents": 9000, "max_cents": 1000}},
                            admin_token)
        check("非法金额区间被拒（min>=max）", code == 400, f"code={code}")

        code, pi = req("GET", "/api/membership/payment-info", token=u1)
        check("用户端读取收款信息", code == 200 and pi.get("payee_name") == "张三", str(code))

        # ==================================================== 4. 下单
        code, o1 = req("POST", "/api/membership/orders",
                       {"code": "a3f9", "plan_code": "monthly"}, u1)
        check("下单成功（小写订单号自动转大写）",
              code == 201 and o1.get("code") == "A3F9", f"code={o1.get('code')}")
        amount1 = o1.get("amount_cents", 0)
        check("金额落在 1000~1200 分", 1000 <= amount1 <= 1200, f"{amount1} 分")
        check("返回元单位展示值", o1.get("amount_yuan") == f"{amount1 / 100:.2f}",
              str(o1.get("amount_yuan")))
        check("下单返回收款信息", bool(o1.get("payment_info", {}).get("payee_name")), "")

        code, dup = req("POST", "/api/membership/orders",
                        {"code": "A3F9", "plan_code": "monthly"}, u1)
        check("重复订单号返回 409", code == 409, f"code={code}")

        code, _ = req("POST", "/api/membership/orders", {"code": "AB", "plan_code": "monthly"}, u1)
        check("订单号长度非法返回 422", code == 422, f"code={code}")

        code, _ = req("POST", "/api/membership/orders",
                      {"code": "!!!!", "plan_code": "monthly"}, u1)
        check("订单号字符集非法返回 422", code == 422, f"code={code}")

        code, _ = req("POST", "/api/membership/orders",
                      {"code": "Z9Z9", "plan_code": "not-exist"}, u1)
        check("套餐不存在返回 404", code == 404, f"code={code}")

        # 金额不做独占：第二笔只要落在区间内即可，允许与首笔相同
        # （订单号才是唯一标识，管理员既能按订单号查金额，也能按金额反查订单号）
        code, o2 = req("POST", "/api/membership/orders",
                       {"code": "B7K1", "plan_code": "quarterly"}, u1)
        check("第二笔订单金额落在区间内",
              code == 201 and 1000 <= o2.get("amount_cents", -1) <= 1200,
              f"{o2.get('amount_cents')} 分")

        # 频率限制：60 秒内第 4 笔
        code, o3 = req("POST", "/api/membership/orders", {"code": "C2M8", "plan_code": "yearly"}, u1)
        code4, too_many = req("POST", "/api/membership/orders",
                              {"code": "D9P3", "plan_code": "monthly"}, u1)
        check("60 秒内第 4 笔下单返回 429", code4 == 429, f"code={code4}")

        # ==================================================== 5. 我已支付 / 放弃
        code, n1 = req("POST", "/api/membership/orders/A3F9/notify", {}, u1)
        check("标记已支付 -> notified", code == 200 and n1.get("status") == "notified", str(code))
        code, n1b = req("POST", "/api/membership/orders/A3F9/notify", {}, u1)
        check("重复标记已支付幂等", code == 200 and n1b.get("status") == "notified", str(code))

        code, _ = req("POST", "/api/membership/orders/C2M8/cancel", {}, u1)
        code, mine = req("GET", "/api/membership/orders/mine", token=u1)
        cancelled = [o for o in mine if o["code"] == "C2M8"]
        check("放弃订单 -> cancelled", bool(cancelled) and cancelled[0]["status"] == "cancelled", "")
        check("我的订单列表含全部订单", len(mine) >= 3, f"{len(mine)} 条")

        code, _ = req("POST", "/api/membership/orders/B7K1/notify", {}, u2)
        check("他人订单不可操作（403）", code == 403, f"code={code}")

        # ==================================================== 6. 管理端核销
        code, _ = req("GET", "/api/admin/membership/orders?status=notified", token=u1)
        check("非管理员访问管理端返回 403", code == 403, f"code={code}")

        code, pending_list = req("GET", "/api/admin/membership/orders?status=notified",
                                 token=admin_token)
        check("待核销列表含 A3F9",
              code == 200 and any(o["code"] == "A3F9" for o in pending_list),
              f"{len(pending_list) if isinstance(pending_list, list) else 0} 条")

        code, found = req("GET", "/api/admin/membership/orders/a3f9", token=admin_token)
        check("按订单号查询（大小写不敏感）",
              code == 200 and found.get("amount_cents") == amount1,
              f"amount={found.get('amount_cents')}")
        check("查询结果含用户与套餐",
              found.get("username") == "mem1" and found.get("plan_name") == "月卡",
              f"{found.get('username')}/{found.get('plan_name')}")

        # ---- 人工核销的第二个入口：按金额反查订单号 ----
        # 金额不独占，所以是「管理员先看到收款记录里的这笔钱，反查是谁下的单」。
        code, by_amt = req("GET", f"/api/admin/membership/orders/by-amount?amount_cents={amount1}",
                           token=admin_token)
        check("按金额反查返回 200", code == 200, f"code={code}")
        check("按金额反查能命中 A3F9",
              isinstance(by_amt, list) and any(o["code"] == "A3F9" for o in by_amt),
              str([o.get("code") for o in by_amt]) if isinstance(by_amt, list) else str(by_amt))
        check("反查结果带金额与用户",
              isinstance(by_amt, list)
              and any(o.get("amount_cents") == amount1 and o.get("username") == "mem1"
                      for o in by_amt), "")
        # 元 -> 分必须用 Decimal：10.53 用 float 乘 100 会变成 1052，查不到
        yuan = f"{amount1 / 100:.2f}"
        code, by_yuan = req("GET",
                            f"/api/admin/membership/orders/by-amount?amount_yuan={yuan}",
                            token=admin_token)
        check("按元单位反查与按分反查结果一致",
              code == 200 and isinstance(by_yuan, list)
              and any(o["code"] == "A3F9" for o in by_yuan), f"{yuan} 元")
        # 查不到是正常结果，返回空数组而不是 404
        code, none_hit = req("GET", "/api/admin/membership/orders/by-amount?amount_cents=1",
                             token=admin_token)
        check("金额无对应订单时返回空数组（不报 404）",
              code == 200 and isinstance(none_hit, list) and len(none_hit) == 0, f"code={code}")
        # 参数缺失 / 非法
        code, _ = req("GET", "/api/admin/membership/orders/by-amount", token=admin_token)
        check("缺金额参数返回 422", code == 422, f"code={code}")
        code, _ = req("GET", "/api/admin/membership/orders/by-amount?amount_yuan=abc",
                      token=admin_token)
        check("金额格式非法返回 422", code == 422, f"code={code}")
        # 管理端接口必须鉴权
        code, _ = req("GET", f"/api/admin/membership/orders/by-amount?amount_cents={amount1}",
                      token=u1)
        check("非管理员按金额反查返回 403", code == 403, f"code={code}")

        code, conf = req("POST", "/api/admin/membership/orders/A3F9/confirm", {},
                         admin_token)
        check("管理员确认到账 -> 开通会员",
              code == 200 and conf.get("activated") is True
              and conf["membership"]["is_vip"] is True, str(code))
        check("开通 30 天", conf["membership"]["days_left"] == 30,
              f"days_left={conf['membership']['days_left']}")
        check("订单置 confirmed", conf["order"]["status"] == "confirmed", "")

        code, again = req("POST", "/api/admin/membership/orders/A3F9/confirm", {}, admin_token)
        check("重复核销幂等（不再加时长）",
              code == 200 and again.get("activated") is False
              and again["membership"]["days_left"] == 30,
              f"days_left={again['membership']['days_left']}")

        code, ms1 = req("GET", "/api/membership", token=u1)
        check("用户端看到已开通", code == 200 and ms1.get("is_vip") is True
              and ms1.get("days_left") == 30, str(ms1.get("days_left")))
        check("会员配额不限", ms1.get("quota", {}).get("unlimited") is True
              and ms1.get("quota", {}).get("limit_seconds") is None, "")

        # ==================================================== 7. 驳回
        code, o4 = req("POST", "/api/membership/orders", {"code": "E4R6", "plan_code": "monthly"}, u2)
        check("用户 2 下单", code == 201, str(code))
        req("POST", "/api/membership/orders/E4R6/notify", {}, u2)
        code, rej = req("POST", "/api/admin/membership/orders/E4R6/reject",
                        {"reason": "未查到该金额"}, admin_token)
        check("驳回订单并写备注",
              code == 200 and rej.get("status") == "rejected"
              and rej.get("admin_note") == "未查到该金额", str(code))
        code, _ = req("POST", "/api/admin/membership/orders/E4R6/confirm", {}, admin_token)
        check("已驳回订单不可再确认", code == 400, f"code={code}")

        # ==================================================== 8. 后台直接分配
        u2_id = None
        code, u2_me = req("GET", "/api/membership", token=u2)
        check("用户 2 仍为非会员", code == 200 and u2_me.get("is_vip") is False, "")

        # 从订单里取用户 id
        code, orders_all = req("GET", "/api/admin/membership/orders?status=rejected",
                               token=admin_token)
        u2_id = orders_all[0]["user_id"] if isinstance(orders_all, list) and orders_all else 0
        code, granted = req("POST", "/api/admin/membership/grant",
                            {"user_id": u2_id, "days": 10, "note": "亲友赠送"}, admin_token)
        check("后台直接分配会员 10 天",
              code == 200 and granted.get("is_vip") is True
              and granted.get("days_left") == 10, str(code))
        check("分配来源标记为 admin", granted.get("source") == "admin", str(granted.get("source")))

        # ==================================================== 9. 设备端配额
        code, q1 = req("POST", "/api/device/chat-usage", {"seconds": 600}, dev_token)
        check("上报陪聊时长 600 秒", code == 200 and q1.get("used_seconds") == 600,
              f"used={q1.get('used_seconds')}")
        check("会员不限时长（remaining 为 None）",
              q1.get("unlimited") is True and q1.get("remaining_seconds") is None, "")

        code, q2 = req("POST", "/api/device/chat-usage",
                       {"seconds": 300, "is_reminder": True}, dev_token)
        check("服药类交互不计入配额",
              code == 200 and q2.get("used_seconds") == 600
              and q2.get("reminder_seconds") == 300,
              f"used={q2.get('used_seconds')} reminder={q2.get('reminder_seconds')}")

        code, q3 = req("GET", "/api/device/quota", token=dev_token)
        check("设备端配额接口", code == 200 and q3.get("used_seconds") == 600
              and q3.get("unlimited") is True, str(q3.get("used_seconds")))
        check("下发服药关键词表", len(q3.get("reminder_keywords", [])) >= 10,
              f"{len(q3.get('reminder_keywords', []))} 个")
        check("下发空闲切断秒数", q3.get("idle_timeout_seconds") == 90, "")

        code, sync = req("POST", "/api/device/sync?fw_version=0.1.0", {}, dev_token)
        check("心跳同步顺带下发配额（规划 7.3）",
              code == 200 and sync.get("quota", {}).get("used_seconds") == 600
              and len(sync.get("quota", {}).get("reminder_keywords", [])) >= 10,
              str(sync.get("quota", {}).get("used_seconds")))

        code, _ = req("GET", "/api/device/quota")
        check("设备端接口无凭证被拒", code in (401, 403), f"code={code}")

        # 非会员设备的配额（用户 3 建一台设备）
        code, dev3 = req("POST", "/api/devices", {"device_id": "memdev000003"}, u3)
        pk3 = dev3.get("id", 0)
        code, cd3 = req("POST", f"/api/devices/{pk3}/code", {}, u3)
        code, bind3 = req("POST", "/api/devices/bind", {"code": cd3.get("code", "")}, u3)
        code, q4 = req("GET", "/api/device/quota", token=bind3.get("device_token", ""))
        check("非会员设备配额上限 3600",
              code == 200 and q4.get("unlimited") is False
              and q4.get("limit_seconds") == 3600, str(q4.get("limit_seconds")))
        code, q5 = req("POST", "/api/device/chat-usage", {"seconds": 120},
                       bind3.get("device_token", ""))
        check("非会员剩余额度计算", q5.get("remaining_seconds") == 3480,
              str(q5.get("remaining_seconds")))

        # ==================================================== 10. 过期清理
        code, _ = req("POST", "/api/admin/membership/orders/expire", {}, admin_token)
        check("手动清理（当前无过期订单）",
              code == 200 and _ .get("expired_orders") == 0 if isinstance(_, dict) else False,
              str(_))

        # 进程内把 B7K1 的创建时间回拨 49 小时，再触发清理
        # （进程内也要用同一个 SQLite 库，因此先补上 ELDER_DB 再 import app）
        os.environ["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
        sys.path.insert(0, str(SERVER))
        from datetime import timedelta                           # noqa: E402
        from sqlalchemy import select as sa_select               # noqa: E402
        from fastapi import HTTPException                        # noqa: E402
        from app import membership as svc                        # noqa: E402
        from app.db import SessionLocal                          # noqa: E402
        from app.models import MembershipOrder, utcnow           # noqa: E402

        with SessionLocal() as s:
            row = s.scalar(
                sa_select(MembershipOrder).where(MembershipOrder.code == "B7K1")
            )
            old = utcnow() - timedelta(hours=49)
            row.created_at = old
            row.expires_at = old + timedelta(hours=48)
            s.commit()

        code, exp = req("POST", "/api/admin/membership/orders/expire", {}, admin_token)
        check("超时订单被清理为 expired",
              code == 200 and exp.get("expired_orders") == 1, str(exp))

        code, after = req("GET", "/api/admin/membership/orders/B7K1", token=admin_token)
        check("订单状态已为 expired", code == 200 and after.get("status") == "expired",
              str(after.get("status")))

        # 过期后号段释放：用户 3 可用同一个订单号重新下单
        code, reuse = req("POST", "/api/membership/orders",
                          {"code": "B7K1", "plan_code": "monthly"}, u3)
        check("过期后订单号可复用", code == 201 and reuse.get("code") == "B7K1", f"code={code}")

        # ==================================================== 11. 进程内：金额分配
        with SessionLocal() as s:
            picked: list[int] = []
            for i in range(20):
                picked.append(svc.pick_amount_cents(s))
                s.add(MembershipOrder(
                    code=f"{i:04d}", user_id=1, plan_code="monthly",
                    amount_cents=picked[-1], status="pending",
                    created_at=utcnow(), expires_at=utcnow() + timedelta(hours=48),
                ))
                s.commit()
            check("连续分配 20 个金额全部落在区间内",
                  all(1000 <= v <= 1200 for v in picked),
                  f"{min(picked)}~{max(picked)}")
            # 金额不独占，所以「重复」是允许的、也是预期的；这里只验证不会越界
            check("金额允许重复（不再做 48 小时独占）",
                  len(picked) == 20, f"{len(set(picked))} 个不同值（允许少于 20）")

            # 区间很窄（只有 6 个候选值）时依然能连续下单，不会因为「被占满」而 409
            rng = svc.get_amount_range(s)
            rng.min_cents = 1000
            rng.max_cents = 1005
            s.commit()
            got: list[int] = []
            err = None
            try:
                for i in range(20, 40):
                    got.append(svc.pick_amount_cents(s))
            except HTTPException as e:  # noqa: BLE001
                err = e
            check("窄区间下仍可连续分配（不再因占满抛 409）",
                  err is None and len(got) == 20,
                  f"分配 {len(got)} 次，err={err.status_code if err else None}")
            check("窄区间分配仍不越界",
                  all(1000 <= v <= 1005 for v in got), f"{min(got)}~{max(got)}")

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
