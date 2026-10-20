"""冒烟：pk 复用 → 已删除设备的旧 token 复活（P0）+ 从属行串台。

缺陷的两条通道（详见 design-p2 §H）
-----------------------------------
`devices.id` 是裸 ``Integer`` 主键（无 AUTOINCREMENT）⇒ 删掉当前最大行后，SQLite
下一次插入**复用同一个 pk**，而新行的 ``token_epoch`` 又从 DEFAULT 0 起算：

* **token 通道**：旧 ``device_token`` 的 ``ep=0``、新行 ``epoch=0`` ⇒ ``0 == 0``
  通过 ⇒ 一枚 365 天有效期的旧 token 读到**另一家人**的提醒与服药回执。
* **从属行通道**（更强，不需要任何 token）：``Interaction`` / ``ChatUsage`` 只有
  外键、没有 cascade ⇒ 删设备后成孤儿行，pk 一复用就挂到新设备名下。新家属正常
  打开 App 就会看到上一家的「交互次数」与「陪聊时长」。

为什么必须起真实服务（不能像 B 节那样进程内直调）
------------------------------------------------
这条缺陷依赖**跨请求的状态**（删 → 建 → 旧 token 三跳），并且要穿过
``require_device_owner`` 与每次请求新建的 DB 会话。进程内打桩拿不到同一份行为。

断言清单
--------
  P5  前置自检：建 A 记 pk_a → 删 A → 建 B，断言 pk_b == pk_a（复用**确实发生了**）。
      不相等 ⇒ **SKIP 而不是 PASS**（D33：门禁只能约束已存在的东西；SKIP 必须
      单列可见，混进"通过"就是反向假绿），且 P1/P2/P3 一并 SKIP —— 因为在不复用时
      它们会**因为别的原因**通过（旧 token 的 sub 指向不存在的行），那是假绿。
  P1  用已删除设备 A 的 token 调 ``POST /api/device/sync`` ⇒ 401
  P2  同上，用 A 的 token 发起 WS 握手 ⇒ 必须 4001 关闭
      ⚠️ 握手头里的 ``Device-Id`` 故意填**新设备 B 的 MAC**：pk 复用后 header 与
      库里那一行天然一致（这才是真实形态，也是"header 比较挡不住"的证明）。
      若填 A 的 MAC，header 那段校验就会先拒，P2 变成假绿。
  P3  A 名下写 1 条 ChatUsage(今日) + 1 条 Interaction → 删 A → 建 B →
      B 的 ``/api/device/quota``、``/api/stats/overview`` 与 ``/api/stats/recent``
      必须是 **0**。``recent`` 单列是因为 PM **证明这个洞时用的就是它**
      （返回 200 + 上一家那条交互），而且它回的是**明细**：聚合过了不代表明细没漏。
  P4  ① 历史形态 token（claim 里 ``AA:BB:CC:DD:EE:22``、库里已归一化为
      ``aabbccddee22``）⇒ **仍然 200**（宽容，守住"别把兼容修成故障"）；
      ② 同一枚 token 的 claim 换成另一个 MAC ⇒ 401
  P7  删设备后服务端日志里必须有 ``DELETE-DEVICE``，且含**硬编码的 pk 与 MAC**
      （这是「这一行原本属于谁」唯一可推的依据 —— 日志给 pk↔mac↔owner，
      ``interactions.ts`` 给时间，少任一侧归属都不可得）。P7 不依赖 pk 复用。
  P7b 同一条审计日志**不得**含老人姓名 / 提醒标题等业务内容
      （日志会进 CI artifact 与被拷走的容器日志，进得去收不回来）。

用法：
    python tools/smoke_pk_reuse.py [--port 8130]
"""
from __future__ import annotations

import asyncio
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

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_common import unlink_retry  # noqa: E402
from smoke_ports import (  # noqa: E402
    detect_addr_in_use,
    guard_or_die,
    guard_port,
    release_port,
    resolve_port,
)

# `ws_connect` 是 smoke_voice 里的薄包装：websockets 17.x 把 headers 参数名
# 从 `extra_headers` 改成了 `additional_headers`，它按运行时签名探测。
# 这里**复用而不是抄一份** —— 抄一份就是两份真源，下次库升级只会修好其中一处。
from smoke_voice import ws_connect  # noqa: E402

# 本地请求绝不走代理（本机/CI 都可能注入了 http_proxy，走代理会超时而零断言失败）
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
DB_PATH = Path(os.environ.get("SMOKE_DB", str(DB_DIR / f"elder_pk_reuse_{os.getpid()}.db")))
SERVER_LOG = DB_DIR / f"pk_reuse_server_{os.getpid()}.log"

PORT = resolve_port("smoke_pk_reuse")
BASE = f"http://127.0.0.1:{PORT}"
WS_URL = f"ws://127.0.0.1:{PORT}/api/voice/v1/ws"

# 服务端与断言进程必须用**同一个** JWT 密钥：P4 要自己签一枚"历史形态"的 token
# （claim 里带冒号、库里已归一化），而解码侧只认这个密钥。
# 长度 ≥32 是刻意的：PyJWT 对短于 32 字节的 HS256 密钥会打 InsecureKeyLengthWarning，
# 那条警告会混进 CI 日志，看起来像"新加的断言在抱怨密钥"。
JWT_SECRET = "smoke-pk-reuse-secret-0123456789abcdef"
JWT_ALG = "HS256"
LEGACY_PW = "admin123"

#: 两台设备用**不同的 MAC**（它们属于两家人），pk 却会被复用 —— 这正是缺陷形态。
MAC_A = "aa:bb:cc:dd:ee:21"
MAC_B = "aa:bb:cc:dd:ee:22"

#: P7b 用的「业务内容」探针：老人姓名与提醒标题。
#: 造这两样东西**只为证明审计日志里没有它们** —— 日志进 CI artifact、会被随手
#: 拷走，业务内容一旦进去就收不回来（口径见 tools/qa3_log_privacy.py）。
ELDER_NAME = "张桂芳"
REMINDER_TITLE = "吃降压药"

_passed: list[str] = []
_failed: list[str] = []
_skipped: list[tuple[str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))
    (_passed if ok else _failed).append(name)
    return ok


def skip(name: str, why: str) -> None:
    """SKIP **不是** PASS：单独打一行并进汇总，绝不计入"通过"。"""
    print(f"  ⊘ SKIP {name}  [{why}]")
    _skipped.append((name, why))


def req(method: str, path: str, body: dict | None = None, token: str | None = None,
        headers: dict | None = None) -> tuple[int, dict | None]:
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
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, (json.loads(raw) if raw.strip() else None)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw}


def wait_ready(timeout: float = 120.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            code, _ = req("GET", "/api/health")
            if code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def _log_tail(max_bytes: int = 65536) -> str:
    try:
        return SERVER_LOG.read_bytes()[-max_bytes:].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return "<no log>"


def _wait_log_contains(marker: str, timeout: float = 10.0) -> str:
    """等日志里出现 ``marker``，返回**命中之后**的整段日志。

    为什么要等：日志由子进程写文件，断言进程读完 DELETE 响应再读文件时，
    那一行可能还没落盘 —— 不等就会变成「偶发红」，那种红最容易被当成"日志没写"
    而去改代码（实际上代码是对的）。
    """
    end = time.time() + timeout
    while True:
        text = _log_tail()
        if marker in text or time.time() >= end:
            return text
        time.sleep(0.2)


# ============================================================ 起停服务
class Server:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.fp = None

    def __enter__(self) -> "Server":
        if DB_PATH.exists():
            unlink_retry(DB_PATH)
        env = dict(os.environ)
        env.pop("MSYSTEM", None)
        env["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
        env["ELDER_SEED_ADMIN_PASSWORD"] = LEGACY_PW
        env["ELDER_JWT_SECRET"] = JWT_SECRET
        # 关掉公开注册也照样能测：本脚本全部用 admin? 不 —— 用注册拿普通账户，
        # 所以显式开启（默认就是开的，这里显式写出来是为了不被 CI 的 job 级 env 影响）。
        env["ELDER_ALLOW_PUBLIC_REGISTER"] = "1"

        print(f"[boot] port={PORT} db={DB_PATH.name}")
        guard_or_die(PORT, "smoke_pk_reuse.py")
        hold, hint = guard_port(PORT)
        if hold is None:
            print("[boot] " + hint.replace("\n", "\n[boot] "))
            raise SystemExit(1)
        release_port(hold, PORT)

        self.fp = SERVER_LOG.open("w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT),
             "--log-level", "warning"],
            cwd=str(SERVER), env=env,
            stdout=self.fp, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace",
        )
        if not wait_ready():
            print("[boot] server did not become ready. log tail:")
            print(_log_tail())
            raise SystemExit(1)
        if detect_addr_in_use(_log_tail()):
            print(f"[boot] ✗ 端口 {PORT} bind 失败，结果不可信")
            raise SystemExit(1)
        print("[boot] ready\n")
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if self.proc is not None:
                self.proc.terminate()
                self.proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            if self.proc is not None:
                self.proc.kill()
        try:
            if self.fp is not None:
                self.fp.close()
        except Exception:  # noqa: BLE001
            pass
        for p in (DB_PATH, SERVER_LOG):
            try:
                if p.exists():
                    unlink_retry(p)
            except Exception:  # noqa: BLE001
                pass


# ============================================================ 账号 / 设备 / token
def login(username: str, password: str) -> str | None:
    code, body = req("POST", "/api/auth/login", {"username": username, "password": password})
    return (body or {}).get("access_token") if code == 200 else None


def register(username: str, password: str) -> str | None:
    code, body = req("POST", "/api/auth/register", {"username": username, "password": password})
    if code in (200, 201):
        return (body or {}).get("access_token") or login(username, password)
    return None


def create_device(user_tok: str, mac: str, name: str) -> tuple[int, dict | None]:
    return req("POST", "/api/devices", {"device_id": mac, "name": name}, token=user_tok)


def ota(mac: str) -> tuple[int, dict]:
    return req("GET", "/api/voice/ota", headers={"Device-Id": mac})


def device_token_via_ota(mac: str) -> str | None:
    """已归属设备在 OTA 响应里拿到 ws token（这就是设备真正在用的那枚）。"""
    code, body = ota(mac)
    if code != 200:
        return None
    return ((body or {}).get("websocket") or {}).get("token")


def mint_device_token(pk: int, claim_mac: str, epoch: int = 0) -> str:
    """自己签一枚设备 token（只用于 P4：伪造"历史形态"的 claim）。

    服务端用 ``ELDER_JWT_SECRET`` 校验签名，本脚本起的服务把它固定成
    ``JWT_SECRET``，所以这里签出来的 token 在服务端看来与真 token 无差别 ——
    P4 要验的正是"claim 的写法不同"这一件事，不是验签名。
    """
    import jwt

    now = datetime.now(timezone.utc)
    body = {"sub": str(pk), "typ": "device", "device_id": claim_mac, "ep": int(epoch),
            "iat": now, "exp": now + timedelta(hours=1)}
    return jwt.encode(body, JWT_SECRET, algorithm=JWT_ALG)


def ws_probe(token: str, header_mac: str) -> tuple[str, int | None]:
    """发起一次真实 WS 握手，返回 ``(结局, 关闭码)``。

    结局三态：
      ``hello``   —— 收到服务端 hello（鉴权通过，会话真的建立了）
      ``closed``  —— 被服务端关闭，关闭码见第二项
      ``timeout`` —— 既没 hello 也没关闭（异常形态，不能当通过）

    ``header_mac`` 传**新设备 B 的 MAC**：pk 复用后 header 与库里那一行一致，
    所以 header 那段校验会放行 —— 只有 token 里的 claim 校验能拦住它。
    """

    async def _run() -> tuple[str, int | None]:
        def _code(exc: BaseException) -> int | None:
            rcvd = getattr(exc, "rcvd", None)
            return getattr(rcvd, "code", None) if rcvd is not None else None

        try:
            async with ws_connect(
                WS_URL, open_timeout=10,
                headers={"Authorization": f"Bearer {token}",
                         "Device-Id": header_mac,
                         "Protocol-Version": "1"},
            ) as ws:
                try:
                    first = await asyncio.wait_for(ws.recv(), timeout=10)
                except asyncio.TimeoutError:
                    return "timeout", None
                except Exception as exc:  # noqa: BLE001  关闭走这里
                    return "closed", _code(exc)
                try:
                    payload = json.loads(first) if isinstance(first, str) else {}
                except Exception:  # noqa: BLE001
                    payload = {}
                return ("hello", None) if payload.get("type") == "hello" else ("other", None)
        except Exception as exc:  # noqa: BLE001  握手阶段就被拒
            return "closed", _code(exc)

    return asyncio.run(_run())


# ============================================================ 断言
def run() -> None:
    admin_tok = login("admin", LEGACY_PW)
    check("种子管理员登录成功（后续不需要它，但它是服务真的起来了的证据）",
          bool(admin_tok))

    tag = os.getpid() % 100000
    tok_a_owner = register(f"pkr1{tag}", "Passw0rd!23")
    tok_b_owner = register(f"pkr2{tag}", "Passw0rd!23")
    check("账户 A（设备 A 的主人）注册成功", bool(tok_a_owner))
    check("账户 B（另一家人）注册成功", bool(tok_b_owner))
    if not (tok_a_owner and tok_b_owner):
        return

    # ---------------- 建 A + 拿 A 的真 token ----------------
    # 设备名里带上老人姓名、并建一条带标题的提醒：**只为 P7b** 有东西可查
    # （审计日志里不许出现它们）。
    code, dev_a = create_device(tok_a_owner, MAC_A, f"设备A-{ELDER_NAME}")
    pk_a = (dev_a or {}).get("id")
    check("账户 A 登记设备 A（201）", code == 201 and pk_a is not None,
          f"http={code} pk={pk_a}")
    if pk_a is None:
        return
    code, _ = req("POST", f"/api/reminders?device_pk={pk_a}",
                  {"title": REMINDER_TITLE, "times_of_day": ["08:00"], "weekdays": [1, 2, 3]},
                  token=tok_a_owner)
    check("P7b 前置：设备 A 名下建一条带标题的提醒", code == 201, f"http={code}")
    tokdev_a = device_token_via_ota(MAC_A)
    check("设备 A 通过 OTA 拿到 device_token", bool(tokdev_a))

    # ---------------- P3 的脏数据（此时 A 还活着，写进去是合理的） ----------------
    code, cu = req("POST", "/api/device/chat-usage", {"seconds": 600}, token=tokdev_a)
    check("P3 前置：设备 A 上报今日陪聊 600 秒",
          code == 200 and (cu or {}).get("used_seconds") == 600,
          f"http={code} used={(cu or {}).get('used_seconds')}")
    code, _ = req("POST", "/api/device/interactions", {"kind": 1}, token=tokdev_a)
    check("P3 前置：设备 A 上报 1 条交互", code == 201, f"http={code}")
    code, qa = req("GET", "/api/device/quota", token=tokdev_a)
    check("P3 前置：A 自己的 quota 确实是 600（否则后面的 0 是空断言）",
          code == 200 and (qa or {}).get("used_seconds") == 600,
          f"http={code} used={(qa or {}).get('used_seconds')}")
    code, st_a = req("GET", f"/api/stats/overview?device_pk={pk_a}", token=tok_a_owner)
    check("P3 前置：A 的统计 today_total ≥ 1（同上，防空断言）",
          code == 200 and int((st_a or {}).get("today_total") or 0) >= 1,
          f"http={code} today_total={(st_a or {}).get('today_total')}")
    code, recent_a = req("GET", f"/api/stats/recent?device_pk={pk_a}", token=tok_a_owner)
    check("P3 前置：A 的 /api/stats/recent 确实能读到明细（否则后面那条 0 是空断言）",
          code == 200 and isinstance(recent_a, list) and len(recent_a) >= 1,
          f"http={code} n={len(recent_a) if isinstance(recent_a, list) else recent_a}")

    # ---------------- 删 A ----------------
    code, _ = req("DELETE", f"/api/devices/{pk_a}", token=tok_a_owner)
    check("账户 A 硬删设备 A（204）", code == 204, f"http={code}")

    # ---------------- P7 / P7b：删除审计（不依赖 pk 复用，永远跑） ----------------
    text = _wait_log_contains("DELETE-DEVICE")
    audit = "\n".join(ln for ln in text.splitlines() if "DELETE-DEVICE" in ln)
    norm_mac_a = MAC_A.replace(":", "").lower()
    check("P7 ★ 删设备留下 DELETE-DEVICE 审计行，且含硬编码的 pk 与 MAC",
          bool(audit) and f"device_pk={pk_a}" in audit and norm_mac_a in audit.lower(),
          f"audit={audit[:160]!r}")
    check("P7 审计行还带上原归属用户、操作人与两处删除行数（少任一项都推不出归属）",
          "原归属用户=" in audit and "操作人=" in audit
          and "interactions=" in audit and "chat_usage=" in audit,
          f"audit={audit[:200]!r}")
    check("P7 行数是真的（本次确实删掉 1 条 interaction + 1 条 chat_usage）",
          "interactions=1" in audit and "chat_usage=1" in audit,
          f"audit={audit[:200]!r}")
    check("P7b ★ 审计行里不得出现老人姓名（业务内容不进日志）",
          bool(audit) and ELDER_NAME not in audit, f"audit={audit[:200]!r}")
    check("P7b ★ 审计行里不得出现提醒标题（同上）",
          bool(audit) and REMINDER_TITLE not in audit, f"audit={audit[:200]!r}")

    # ---------------- 建 B（另一家人，另一个 MAC） ----------------
    code, dev_b = create_device(tok_b_owner, MAC_B, "设备B")
    pk_b = (dev_b or {}).get("id")
    check("账户 B 登记设备 B（201）", code == 201 and pk_b is not None,
          f"http={code} pk={pk_b}")
    if pk_b is None:
        return

    # ---------------- P5：复用真的发生了吗 ----------------
    reused = pk_b == pk_a
    if reused:
        check("P5 前置自检：新设备 B 复用了被删设备 A 的 pk（缺陷前提成立）",
              True, f"pk_a={pk_a} pk_b={pk_b}")
    else:
        skip("P5 前置自检：新设备 B 复用了被删设备 A 的 pk",
             f"pk_a={pk_a} != pk_b={pk_b}（本引擎/DDL 不复用 rowid，"
             f"P1/P2/P3 在不复用时会因其它原因通过 = 假绿，故一并 SKIP）")

    tokdev_b = device_token_via_ota(MAC_B)
    check("设备 B 通过 OTA 拿到自己的 device_token", bool(tokdev_b))

    # ---------------- P1：旧 token 打 HTTP ----------------
    if reused:
        code, body = req("POST", "/api/device/sync", token=tokdev_a)
        detail = (body or {}).get("detail") or ""
        check("P1 ★ 已删除设备的旧 token 调 /api/device/sync ⇒ 401",
              code == 401, f"http={code} detail={detail!r}")
        check("P1 拒绝理由指向「设备不符」而不是别的（便于排障）",
              "设备" in str(detail), f"detail={detail!r}")
    else:
        skip("P1 旧 token 调 /api/device/sync 必须 401", "P5 未复用")

    # ---------------- P2：旧 token 打 WS ----------------
    if reused:
        outcome, close_code = ws_probe(tokdev_a, MAC_B)
        check("P2 ★ 已删除设备的旧 token 发起 WS 握手 ⇒ 4001 关闭",
              outcome == "closed" and close_code == 4001,
              f"outcome={outcome} close_code={close_code}")
    else:
        skip("P2 旧 token 发起 WS 握手必须被关掉", "P5 未复用")

    # ---------------- P3：从属行不能串台 ----------------
    if reused:
        code, qb = req("GET", "/api/device/quota", token=tokdev_b)
        check("P3 ★ 新设备 B 的 quota 是 0（不是上一家的 600 秒）",
              code == 200 and int((qb or {}).get("used_seconds") or 0) == 0,
              f"http={code} used={(qb or {}).get('used_seconds')}")
        code, st_b = req("GET", f"/api/stats/overview?device_pk={pk_b}", token=tok_b_owner)
        check("P3 ★ 新设备 B 的统计 today_total 是 0（不是上一家的交互）",
              code == 200 and int((st_b or {}).get("today_total") or 0) == 0,
              f"http={code} today_total={(st_b or {}).get('today_total')}")
        # ⚠️ 聚合过了**不代表明细没漏**：`/api/stats/recent` 回的是一条条记录，
        # 而 PM 证明这个洞用的正是它（200 + 上一家那条交互）。单列一条守着它。
        code, recent = req("GET", f"/api/stats/recent?device_pk={pk_b}", token=tok_b_owner)
        check("P3 ★ 新设备 B 的 /api/stats/recent 返回 0 条明细（PM 的复现路径）",
              code == 200 and isinstance(recent, list) and len(recent) == 0,
              f"http={code} n={len(recent) if isinstance(recent, list) else recent}")
    else:
        skip("P3 从属行（ChatUsage / Interaction）不得串到新设备", "P5 未复用")

    # ---------------- P4：宽容历史写法 + 拦另一个 MAC ----------------
    # 库里 B 的 device_id 是归一化的 `aabbccddee22`；这里故意签一枚 claim 为
    # `AA:BB:CC:DD:EE:22` 的 token（schema_patch 收敛之前签发的形态）。
    hist_tok = mint_device_token(pk_b, "AA:BB:CC:DD:EE:22")
    code, body = req("POST", "/api/device/sync", token=hist_tok)
    check("P4① ★ 历史形态 claim（AA:BB:.. / 库里 aabb..）仍然 200（别把兼容修成故障）",
          code == 200, f"http={code} detail={(body or {}).get('detail')!r}")

    other_tok = mint_device_token(pk_b, "AA:BB:CC:DD:EE:99")
    code, body = req("POST", "/api/device/sync", token=other_tok)
    check("P4② ★ claim 换成另一个 MAC ⇒ 401",
          code == 401, f"http={code} detail={(body or {}).get('detail')!r}")

    # 缺声明的 token 直接拒（不做"老格式"兼容放行）
    no_claim = mint_device_token(pk_b, "")
    code, body = req("POST", "/api/device/sync", token=no_claim)
    check("P4③ claim 缺失 ⇒ 401（不存在无声明的老 token，放行只会把洞留下）",
          code == 401, f"http={code} detail={(body or {}).get('detail')!r}")

    # P4④：**WS 侧**的同一宽容判据。deps 与 session 各有一次 norm_mac 比较，
    # 只测 HTTP 的话"把 session 那处改成裸串比"不会有任何断言变红 —— 而那正是
    # 会让全部存量设备"能连上、一开口就断"的那一行。
    outcome, close_code = ws_probe(hist_tok, MAC_B)
    check("P4④ ★ WS 侧同一个宽容判据：历史形态 claim 仍能建立会话（收到 hello）",
          outcome == "hello", f"outcome={outcome} close_code={close_code}")


def main() -> int:
    try:
        with Server():
            run()
    except SystemExit:
        raise
    print()
    print(f"==== {len(_passed)} passed, {len(_skipped)} skipped, {len(_failed)} failed ====")
    for name, why in _skipped:
        print(f"  SKIP: {name} —— {why}")
    for name in _failed:
        print(f"  FAILED: {name}")
    return 0 if not _failed else 1


if __name__ == "__main__":
    sys.exit(main())
