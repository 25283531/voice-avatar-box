"""陪聊计量与「取消会员制」核心不变量冒烟。

用法：
    python tools/smoke_quota.py [--port 8106]

## 这个脚本为什么还在（会员制已删，但它不能跟着删）

它曾经叫 ``smoke_membership.py``，测的是会员订单/支付/核销 —— 那套已经没了。
但它同时还测了两样**与会员无关、且不能丢**的东西：

1. **计量链路**：``POST /api/device/chat-usage`` 写入 ``chat_usage``、
   ``GET /api/device/quota`` 读回、``/api/device/sync`` 心跳顺带下发。
   用户还要看「今天聊了多久」，运营也要对账 token 成本 —— 删了它用量就断源。
2. **服药关键词下发**：``reminder_keywords`` 是固件本地识别服药提醒的依据，
   属于提醒链路，删了会连累服药提醒。

所以脚本改名 ``smoke_quota``，只保留上面两块，并**新增本次改动的核心不变量**。

## 核心不变量：普通用户（=没买过会员的普通用户）不再被拦

产品决策是「取消会员制，有一台授权设备就无限量对话」。这个不变量必须有人盯着，
否则将来任何人把「按用户判会员」的逻辑加回来，没人会立刻发现。

对应断言在 ``2. 核心不变量`` 一节，判据是**改库把 free_daily_seconds 设成 0**
（比任何真实用量都更狠）之后，``/api/device/quota`` 仍返回
``unlimited=true`` / ``limit_seconds=null``。
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

# 默认端口统一由 tools/smoke_ports.py 托管（唯一真源），各脚本不再各写一份。
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
# SMOKE_DB_DIR 覆盖目录，或 SMOKE_QUOTA_DB 指定完整路径。
DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
DB_PATH = Path(os.environ.get("SMOKE_QUOTA_DB",
                              str(DB_DIR / f"elder_smoke_quota_{os.getpid()}.db")))
if DB_PATH.exists():
    DB_PATH.unlink()

PORT = resolve_port("smoke_quota")
BASE = f"http://127.0.0.1:{PORT}"

# ⚠️ 子进程输出**绝不能**用 `subprocess.PIPE` +「只在启动失败时读一次」——
# 日志量超过管道缓冲区（Windows 匿名管道 4096 字节）后，
# **服务端会被永久阻塞**，而客户端只看到「读超时」。
# 症状具有强误导性：看起来像网络/DNS/端口问题，实际是测试基础设施缺陷。
# → 输出必须落文件。
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
    env["ELDER_JWT_SECRET"] = "smoke-quota-secret"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"   # 不跑后台调度
    env["PYTHONIOENCODING"] = "utf-8"

    print(f"[boot] starting server on :{PORT} (db: {DB_PATH})")
    # ---- 三段式端口守卫（QA 第 4 轮「假绿」修复）----
    # 端口被占时 uvicorn 的 socket bind 会失败，而 /api/health **永远返回 200**
    # -> wait_ready 会探到**别人的服务** -> 全部断言打到别人身上、用自己从未
    # seed 过的 DB。三段：①事前 is_port_free ②socket 占位缩小竞态窗口
    # ③起服务后校验 stderr。
    # ⚠️ 三段加起来**仍不是绝对保证**（①与真正 bind 之间有不可消除的竞态窗口）。
    # ⚠️ **不能只调 guard_or_die()**：它拿到占位 socket 后**立刻** release
    #（见 smoke_ports.py 末两行），阶段②的占位只覆盖「检查→释放」一瞬间，等于
    # 没有占位。故下面显式持有 guard_port() 的 hold，攥到 spawn 前才 release。
    guard_or_die(PORT, "smoke_quota.py")
    hold, hold_hint = guard_port(PORT)
    if hold is None:            # 理论上 guard_or_die 已挡住，这里是第二道保险
        print("[boot] " + hold_hint.replace("\n", "\n[boot] "))
        return 1

    # 阶段②收尾：**必须在 spawn 之前**释放，否则自己把端口挡住了
    release_port(hold, PORT)

    # ⚠️ stdout/stderr 必须**落文件**，绝不能接成无人消费的 PIPE。
    # Windows 匿名管道缓冲区只有 4KB：管道写满后，服务端进程写日志会被
    # 永久阻塞，表现成「服务端整体失去响应」，而客户端只看到读超时。
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
        # 这是假绿，比超时危险。
        if detect_addr_in_use(_read_log_tail()):
            print(f"[boot] ✗ 端口 {PORT} bind 失败（占位未生效，疑似被别人抢占）。")
            print("[boot]   为避免把断言打到别人的服务上，本次结果**不可信**，直接退出。")
            print("[boot]   请换端口重跑：python tools/smoke_quota.py <其它端口>")
            return 1
        print("[boot] ready\n")

        # ==================================================== 准备账号与设备
        code, tk = req("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
        check("登录默认管理员", code == 200 and bool(tk.get("access_token")), str(code))

        def reg(name: str) -> str:
            req("POST", "/api/auth/register",
                {"username": name, "password": "test123456", "nickname": name})
            _, t = req("POST", "/api/auth/login", {"username": name, "password": "test123456"})
            return t.get("access_token", "")

        u1, u2 = reg("quo1"), reg("quo2")
        check("注册并登录 2 个普通用户", all([u1, u2]), "")

        def bind_device(user_token: str, device_id: str) -> tuple[int, str]:
            _, dev = req("POST", "/api/devices", {"device_id": device_id}, user_token)
            pk = dev.get("id", 0)
            _, cd = req("POST", f"/api/devices/{pk}/code", {}, user_token)
            _, b = req("POST", "/api/devices/bind", {"code": cd.get("code", "")}, user_token)
            return pk, b.get("device_token", "")

        dev_pk, dev_token = bind_device(u1, "quodev000001")
        check("用户 1 绑定设备", bool(dev_token), str(dev_pk))
        dev2_pk, dev2_token = bind_device(u2, "quodev000002")
        check("用户 2 绑定设备", bool(dev2_token), str(dev2_pk))

        # ==================================================== 1. 计量链路
        code, q1 = req("POST", "/api/device/chat-usage", {"seconds": 600}, dev_token)
        check("上报陪聊时长 600 秒", code == 200 and q1.get("used_seconds") == 600,
              f"used={q1.get('used_seconds')}")
        check("授权设备 unlimited（remaining 为 None）",
              q1.get("unlimited") is True and q1.get("remaining_seconds") is None,
              f"unlimited={q1.get('unlimited')} limit={q1.get('limit_seconds')}")

        code, q2 = req("POST", "/api/device/chat-usage",
                       {"seconds": 300, "is_reminder": True}, dev_token)
        check("服药类交互与普通陪聊分开累计",
              code == 200 and q2.get("used_seconds") == 600
              and q2.get("reminder_seconds") == 300,
              f"used={q2.get('used_seconds')} reminder={q2.get('reminder_seconds')}")

        code, q2b = req("POST", "/api/device/chat-usage", {"seconds": 60}, dev_token)
        check("重复上报按设备+日期累加（幂等聚合）",
              q2b.get("used_seconds") == 660, f"used={q2b.get('used_seconds')}")

        code, q3 = req("GET", "/api/device/quota", token=dev_token)
        check("设备端统计接口回读一致", code == 200 and q3.get("used_seconds") == 660,
              str(q3.get("used_seconds")))
        check("下发服药关键词表", len(q3.get("reminder_keywords", [])) >= 10,
              f"{len(q3.get('reminder_keywords', []))} 个")
        check("下发空闲切断秒数", q3.get("idle_timeout_seconds") == 90,
              str(q3.get("idle_timeout_seconds")))

        code, sync = req("POST", "/api/device/sync?fw_version=0.1.0", {}, dev_token)
        check("心跳同步顺带下发统计（规划 7.3）",
              code == 200 and sync.get("quota", {}).get("used_seconds") == 660
              and len(sync.get("quota", {}).get("reminder_keywords", [])) >= 10,
              str(sync.get("quota", {}).get("used_seconds")))
        check("心跳下发的 unlimited 为 true",
              sync.get("quota", {}).get("unlimited") is True,
              str(sync.get("quota", {}).get("unlimited")))

        code, _ = req("GET", "/api/device/quota")
        check("设备端接口无凭证被拒", code in (401, 403), f"code={code}")

        code, _ = req("POST", "/api/device/chat-usage",
                      {"seconds": 10, "date": "not-a-date"}, dev_token)
        check("非法日期被拒 422", code == 422, f"code={code}")

        # ==================================================== 2. 核心不变量
        # 取消会员制：**授权设备恒为无限量**。判据故意做得很狠 ——
        # 直接把 free_daily_seconds 改成 0（比任何真实用量都更极端），
        # 若闸门还在，这里必然变成 limited。
        os.environ["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
        sys.path.insert(0, str(SERVER))
        from app.db import SessionLocal                  # noqa: E402
        from app.models import ChatQuotaConfig           # noqa: E402

        with SessionLocal() as s:
            cfg = s.get(ChatQuotaConfig, 1)
            cfg.free_daily_seconds = 0        # 上限归零：闸门若存在必拦
            s.commit()

        code, q4 = req("GET", "/api/device/quota", token=dev_token)
        check("【不变量】上限设为 0 时授权设备仍 unlimited",
              code == 200 and q4.get("unlimited") is True
              and q4.get("limit_seconds") is None,
              f"unlimited={q4.get('unlimited')} limit={q4.get('limit_seconds')}")

        # 累计到远超任何配额上限的用量后，依然不拦。
        for _ in range(5):
            req("POST", "/api/device/chat-usage", {"seconds": 36000}, dev_token)
        code, q5 = req("GET", "/api/device/quota", token=dev_token)
        check("【不变量】累计 18 万秒后仍 unlimited",
              code == 200 and q5.get("unlimited") is True
              and q5.get("used_seconds") > 180000,
              f"used={q5.get('used_seconds')} unlimited={q5.get('unlimited')}")

        code, q5b = req("POST", "/api/device/chat-usage", {"seconds": 60}, dev_token)
        check("【不变量】超量后上报仍返回 unlimited",
              q5b.get("unlimited") is True and q5b.get("limit_seconds") is None,
              f"limit={q5b.get('limit_seconds')}")

        # 两个用户互不影响：用量是**按设备**聚合的，不是按用户。
        code, q6 = req("GET", "/api/device/quota", token=dev2_token)
        check("【不变量】另一台设备用量独立（未被前一台的 18 万秒牵连）",
              code == 200 and q6.get("used_seconds") == 0,
              f"used={q6.get('used_seconds')}")

        # 会员相关端点必须已经不存在（老前端 / 老 APP 缓存仍可能打过来）。
        for path in ("/api/membership", "/api/membership/plans",
                     "/api/membership/orders/mine", "/api/membership/payment-info",
                     "/api/admin/membership/config", "/api/admin/membership/orders",
                     "/api/admin/membership/grant"):
            code, _ = req("GET", path, token=u1)
            check(f"会员端点已下线 {path}", code == 404, f"code={code}")

        # 恢复上限，避免影响后续断言
        with SessionLocal() as s:
            cfg = s.get(ChatQuotaConfig, 1)
            cfg.free_daily_seconds = 3600
            s.commit()

        # ==================================================== 3. 未授权设备降级
        # 未绑定设备拿不到 device_token（/api/device/ota 只对 owner_id 非空的
        # 设备签发），所以这条降级路径**只能进程内验证**：
        # 判据仍是 chat_quota.quota_snapshot(authorized=False)。
        with SessionLocal() as s:
            from app.chat_quota import quota_snapshot      # noqa: E402
            snap_unauth = quota_snapshot(s, device_ids=[dev_pk], authorized=False)
            snap_auth = quota_snapshot(s, device_ids=[dev_pk], authorized=True)
            check("进程内：未授权设备走 free_daily_seconds 上限",
                  snap_unauth["unlimited"] is False
                  and snap_unauth["limit_seconds"] == 3600,
                  str(snap_unauth.get("limit_seconds")))
            check("进程内：授权设备 unlimited",
                  snap_auth["unlimited"] is True
                  and snap_auth["limit_seconds"] is None,
                  str(snap_auth.get("limit_seconds")))
            check("进程内：两种授权态下 used_seconds 口径相同（记账不受影响）",
                  snap_unauth["used_seconds"] == snap_auth["used_seconds"],
                  f"{snap_unauth['used_seconds']} vs {snap_auth['used_seconds']}")
            check("quota_snapshot 不再返回 is_vip（会员概念已删）",
                  "is_vip" not in snap_auth, str(sorted(snap_auth.keys())))
            # 提醒关键词不随授权态变化 —— 它属于提醒链路，与会员无关
            check("授权态不影响服药关键词下发",
                  snap_unauth["reminder_keywords"] == snap_auth["reminder_keywords"]
                  and len(snap_auth["reminder_keywords"]) >= 10,
                  f"{len(snap_auth['reminder_keywords'])} 个")

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
