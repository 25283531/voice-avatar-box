"""QA-B 第二层独立验证：激活码五条规则 + 解绑后旧设备凭证是否失效。

**与 tools/smoke_activation_ttl.py 的关系：不共享任何断言。**
那个脚本是实现者自己写的冒烟，本脚本从零独立复现，且刻意用两种它不用的手段：

  1. **直连 SQLite** 复核「同一 MAC 同时只有 1 枚有效码」（不信接口自述）；
  2. 额外验证实现者没覆盖的缺口：管理员解绑后，设备手里那枚 30 天期的
     ``device_token``（JWT）**是否仍然能调 /api/device/** 下的端点；
  3. **真实 WS 握手**（W1~W4）：`voice/session.py` 的代次校验是本轮唯一
     「代码里写了、却没有行为断言」的地方，而它失效时**没有任何日志** ——
     表现是「解绑后原家属仍能连上 WS 听老人说话」。用 ``QA_B_SERVER_DIR``
     指向一份被改坏的 server 副本可做变异测试（删掉那句校验后 W2/W3 必须红）。

两阶段各起一次服务（TTL 注入 0 / 10），理由见 smoke_activation_ttl.py 的
模块 docstring（TTL=0 时任何码都绑不上，混在一起等于用必然失败的夹具测绑定）。

用法：
    python tools/qa_b_activation_gate.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
#: 变异测试用：指向一份被改坏的 server 副本，验证断言**真的会红**。
#: 不设时就是真身。见文件末尾 main() 的说明。
SERVER_DIR = Path(os.environ.get("QA_B_SERVER_DIR", str(SERVER)))
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

# 服务端常量与签发函数**直接 import**，不硬编码 4401/4001：
# 硬编码的后果是「session.py 改了关闭码，测试仍然绿」—— 用例会变成摆设。
# 导入期会跑 config 的部署自检并打 INFO 日志，这里重定向掉，别污染断言输出。
sys.path.insert(0, str(SERVER))
import contextlib  # noqa: E402
import io  # noqa: E402

with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from app.security import issue_device_token  # noqa: E402
    from app.voice.session import (  # noqa: E402
        CLOSE_DEVICE_MISMATCH,
        CLOSE_UNAUTHORIZED,
    )

_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
PORT = resolve_port("qa_b_activation_gate")
BASE = f"http://127.0.0.1:{PORT}"
WS_URL = f"ws://127.0.0.1:{PORT}/api/voice/v1/ws"
LEGACY_PW = "admin123"
ADMIN_USER = "admin"

_failures: list[str] = []
_passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))
    (_passes if ok else _failures).append(name)
    return ok


def req(method: str, path: str, body: dict | None = None, token: str | None = None,
        headers: dict | None = None) -> tuple[int, dict | None, str]:
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
            return resp.status, (json.loads(raw) if raw.strip() else None), raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, (json.loads(raw) if raw.strip() else None), raw
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw}, raw


def ota(mac: str) -> tuple[int, dict, str]:
    return req("GET", "/api/voice/ota", headers={"Device-Id": mac})


def act_of(body: dict | None) -> dict:
    return (body or {}).get("activation") or {}


def wait_ready(timeout: float = 120.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            code, _, _ = req("GET", "/api/health")
            if code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def login(username: str, password: str) -> str | None:
    code, body, _ = req("POST", "/api/auth/login", {"username": username, "password": password})
    return (body or {}).get("access_token") if code == 200 else None


def new_account(tag: str) -> tuple[str, str | None]:
    """注册 + 登录，返回 (用户名, token)。"""
    name = f"qb{tag}{os.getpid() % 10000}"
    code, body, raw = req("POST", "/api/auth/register",
                          {"username": name, "password": "Passw0rd!23"})
    if code not in (200, 201):
        print(f"    [warn] 注册 {name} 失败 http={code} {raw[:120]}")
        return name, None
    return name, login(name, "Passw0rd!23")


# ---------------------------------------------------------------- 直连 SQLite
def norm(mac: str) -> str:
    """与 app.macs.norm_mac 同口径：去分隔符 + 小写。

    ⚠️ 必须归一化再查库：服务端入库的是 ``aabbccddeeb1``（见 voice/__init__.py
    的 ``device_id=norm_mac(mac)``），而固件请求头给的是 ``aa:bb:cc:dd:ee:b1``。
    第一版本脚本没归一化，直连库读回 0 行 —— 那是**脚本自己的夹具错误**，
    差点被误报成「服务端没锁码」的 P0。
    """
    return "".join(ch for ch in (mac or "") if ch.isalnum()).lower()


def valid_codes(db_path: Path, mac: str) -> list[tuple]:
    """**绕过接口**直接查库：该 MAC 名下 used=0 且未过期的码。"""
    mac = norm(mac)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    last: Exception | None = None
    for _ in range(5):
        try:
            con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)
            try:
                return con.execute(
                    "SELECT id, code, expires_at FROM activation_codes "
                    "WHERE device_id=? AND used=0 AND expires_at > ? ORDER BY id",
                    (mac, now),
                ).fetchall()
            finally:
                con.close()
        except sqlite3.OperationalError as exc:      # 写事务进行中 -> 重试
            last = exc
            time.sleep(0.2)
    raise RuntimeError(f"直连 SQLite 读失败: {last}")


def device_row(db_path: Path, mac: str) -> tuple | None:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)
    try:
        return con.execute(
            "SELECT id, owner_id, online, last_seen_at, token_epoch "
            "FROM devices WHERE device_id=?",
            (norm(mac),),
        ).fetchone()
    finally:
        con.close()


# ---------------------------------------------------------------- 真实 WS 握手
async def _ws_probe(token: str, mac: str, timeout: float = 5.0) -> tuple[str, int | None, str]:
    """真连一次 WS，返回 ``(结论, close_code, 说明)``。

    结论取值：
      ``hello``  —— 握手成功且收到服务端的 ``{"type":"hello"}``（基线）
      ``open``   —— 握手成功、超时内无下行帧（连接没被关）
      ``closed`` —— 被服务端关闭（正常路径：鉴权失败）
      ``error``  —— 连不上/客户端异常（用例应视为**未跑通**，不是通过）

    ⚠️ ``proxy=None`` 必须显式给：websockets ≥15 会读环境里的 HTTP_PROXY，
    在带代理的机器上会把 127.0.0.1 也代理出去，表现为连接超时 ——
    那是夹具问题，会被误读成「服务端不让我连」。
    """
    import asyncio

    import websockets
    from websockets.exceptions import ConnectionClosed

    headers = {"Authorization": "Bearer " + token, "Device-Id": mac}
    try:
        async with websockets.connect(WS_URL, additional_headers=headers,
                                      open_timeout=timeout, proxy=None) as ws:
            try:
                first = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                return ("open", None, f"{timeout}s 内无下行帧，连接未关闭")
            except ConnectionClosed as exc:
                return ("closed", getattr(exc, "code", None),
                        f"recv 抛 {type(exc).__name__} code={getattr(exc, 'code', None)}")
            if isinstance(first, bytes):
                return ("binary", None, f"收到二进制帧 {len(first)}B")
            return ("hello" if '"hello"' in first else "open", None, first[:200])
    except ConnectionClosed as exc:
        return ("closed", getattr(exc, "code", None),
                f"connect 阶段即被关闭 code={getattr(exc, 'code', None)}")
    except Exception as exc:  # noqa: BLE001
        return ("error", None, f"{type(exc).__name__}: {exc}")


def ws_probe(token: str, mac: str, timeout: float = 5.0) -> tuple[str, int | None, str]:
    import asyncio

    return asyncio.run(_ws_probe(token, mac, timeout))


# ============================================================ 服务生命周期
class Server:
    def __init__(self, ttl_min: str, tag: str, fresh: bool = True,
                 keep_db: bool = False) -> None:
        self.ttl_min = ttl_min
        self.tag = tag
        self.fresh = fresh
        self.keep_db = keep_db
        self.db = DB_DIR / f"qa_b_{tag}_{os.getpid()}.db"
        self.log = DB_DIR / f"qa_b_{tag}_{os.getpid()}.log"
        self.proc = None
        self.fp = None

    def __enter__(self) -> "Server":
        if self.fresh and self.db.exists():
            unlink_retry(self.db)
        env = dict(os.environ)
        env.pop("MSYSTEM", None)
        env["ELDER_DB"] = f"sqlite:///{self.db.as_posix()}"
        env["ELDER_SEED_ADMIN_PASSWORD"] = LEGACY_PW
        env["ELDER_ACT_TTL_MIN"] = self.ttl_min
        env["ELDER_OTA_NEW_CODE_PER_HOUR"] = "1000"
        env["ELDER_LOGIN_RATE_LIMIT"] = "0"

        print(f"[boot:{self.tag}] port={PORT} TTL={self.ttl_min}min db={self.db.name}")
        guard_or_die(PORT, "qa_b_activation_gate.py")
        hold, hint = guard_port(PORT)
        if hold is None:
            print("[boot] " + hint.replace("\n", "\n[boot] "))
            raise SystemExit(1)
        release_port(hold, PORT)

        self.fp = self.log.open("w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT),
             "--log-level", "warning"],
            cwd=str(SERVER_DIR), env=env,
            stdout=self.fp, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace",
        )
        if not wait_ready():
            print("[boot] 服务未就绪，日志尾部：")
            print(self.tail())
            raise SystemExit(1)
        if detect_addr_in_use(self.tail()):
            print(f"[boot] ✗ 端口 {PORT} bind 失败，结果不可信")
            raise SystemExit(1)
        print(f"[boot:{self.tag}] ready\n")
        return self

    def tail(self, n: int = 8192) -> str:
        try:
            return self.log.read_bytes()[-n:].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return "<no log>"

    def __exit__(self, *exc: object) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            self.proc.kill()
        try:
            self.fp.close()
        except Exception:  # noqa: BLE001
            pass
        for p in (self.db, self.log):
            if p.name == self.db.name and self.keep_db:
                continue
            if p.name == self.log.name and os.environ.get("QA_B_KEEP_LOG") == "1":
                continue
            try:
                if p.exists():
                    unlink_retry(p)
            except Exception:  # noqa: BLE001
                pass


# ============================================================ 阶段 A：R1 过期换新
def phase_a() -> None:
    print("=== 阶段 A：TTL=0 —— R1「过期即换新码，旧码不得再可用」===")
    mac = "aa:bb:cc:dd:ee:a1"
    with Server("0", "a") as srv:
        code, b1, raw1 = ota(mac)
        c1 = act_of(b1).get("code")
        check("A1 首次 OTA 返回 activation.code（7 位纯数字）",
              code == 200 and isinstance(c1, str) and c1.isdigit() and len(c1) == 7,
              f"http={code} code={c1}")
        code, b2, raw2 = ota(mac)
        c2 = act_of(b2).get("code")
        check("A2 TTL=0 连打两次 OTA，码值必须不同",
              code == 200 and bool(c2) and c2 != c1, f"{c1} -> {c2}")
        check("A3 TTL=0 时 expires_in 归零（不返回负数）",
              act_of(b1).get("expires_in") == 0, f"expires_in={act_of(b1).get('expires_in')}")

        rows = valid_codes(srv.db, mac)
        check("A4 直连库：TTL=0 时没有任何「有效」未用码",
              len(rows) == 0, f"count={len(rows)} rows={rows}")

        _, tok = new_account("a")
        check("A5 测试账户可用", bool(tok))
        if tok:
            code, body, _ = req("POST", "/api/devices/bind", {"code": c1}, token=tok)
            detail = (body or {}).get("detail") or ""
            check("A6 ★ 第一枚（已过期）码拿去 bind 必须被拒",
                  code in (400, 404), f"http={code} detail={detail!r}")


# ============================================================ 阶段 B：R1/R2/R3/R4/R5
def phase_b() -> None:
    print("\n=== 阶段 B：TTL=10 —— R1 复用 / R2 锁定 / R3 已绑定不发码 / R4 重复绑定 ===")
    mac = "aa:bb:cc:dd:ee:b1"
    with Server("10", "b") as srv:
        # ---- R1：有效期内同一枚码 ----
        code, b1, _ = ota(mac)
        c1 = act_of(b1).get("code")
        left = act_of(b1).get("expires_in")
        check("B1 TTL=10 首次取码，expires_in 在 540~600 秒",
              code == 200 and isinstance(left, int) and 540 <= left <= 600,
              f"http={code} expires_in={left}")

        # ---- R2：连打 5 次 + 每轮直连库复核 ----
        seen = [c1]
        counts = []
        for i in range(4):
            _, bi, _ = ota(mac)
            seen.append(act_of(bi).get("code"))
            counts.append(len(valid_codes(srv.db, mac)))
        counts.insert(0, len(valid_codes(srv.db, mac)))
        check("B2 TTL=10 连打 5 次 OTA 始终拿到同一枚码",
              len(set(seen)) == 1 and seen[0], f"codes={seen}")
        check("B3 ★ 直连库：每次请求后该 MAC 有效未用码恒为 1 枚",
              counts == [1, 1, 1, 1, 1], f"counts={counts}")

        # ---- 绑定 ----
        _, tok_a = new_account("b")
        check("B4 账户 A 可用", bool(tok_a))
        code, bind_body, _ = req("POST", "/api/devices/bind", {"code": c1}, token=tok_a)
        check("B5 有效码绑定成功（200）", code == 200, f"http={code} body={str(bind_body)[:160]}")
        pk = (bind_body or {}).get("device_pk")

        # ---- R3：已绑定不发码 ----
        code, b3, raw3 = ota(mac)
        check("B6 ★ 已绑定设备的 OTA 响应里不存在 activation 这个 key",
              code == 200 and "activation" not in (b3 or {}),
              f"keys={sorted((b3 or {}).keys())}")
        check("B7 ★ 已绑定响应原文里也不出现 activation 字样（防藏进嵌套）",
              "activation" not in raw3, f"len(raw)={len(raw3)}")
        ws_token = ((b3 or {}).get("websocket") or {}).get("token")
        check("B8 已绑定设备仍拿到 websocket.token（设备能继续连）",
              bool(ws_token), f"token_len={len(ws_token or '')}")

        rows = valid_codes(srv.db, mac)
        check("B9 直连库：绑定成功后该 MAC 无任何有效未用码",
              len(rows) == 0, f"count={len(rows)} rows={rows}")

        # ---- 真实 WS 握手（HTTP 之外唯一能读数据的通道）----
        print("\n--- WS 握手实测：真实连接，不是代码核查 ---")
        r, cc, detail = ws_probe(ws_token or "", mac)
        check("W1 ★ 绑定后用设备 token 能握上 WS（基线，证明用例本身没写错）",
              r == "hello", f"result={r} close_code={cc} detail={detail}")

        # ---- R4：同一枚码绑给第二个账户 ----
        _, tok_b = new_account("c")
        check("B10 账户 B 可用", bool(tok_b))
        if tok_b:
            code, body, _ = req("POST", "/api/devices/bind", {"code": c1}, token=tok_b)
            detail = (body or {}).get("detail") or ""
            check("B11 ★ 同一枚码绑给第二个账户必须被拒",
                  code in (400, 404, 409), f"http={code} detail={detail!r}")

            # 换绑场景：B 尝试为自己拿这台设备的新码
            code, bb, _ = ota(mac)
            has_act = "activation" in (bb or {})
            check("B12 换绑通道：B 对同一 MAC 打 OTA 拿不到新码（R3 生效）",
                  code == 200 and not has_act,
                  f"http={code} activation={'有' if has_act else '无'} keys={sorted((bb or {}).keys())}")

        if pk and tok_a:
            code, body, _ = req("POST", f"/api/devices/{pk}/code", token=tok_a)
            detail = (body or {}).get("detail") or ""
            check("B13 已绑定设备调 /devices/{pk}/code 补发码必须 409",
                  code == 409, f"http={code} detail={detail!r}")

        # R2 的另一半：锁定 = 这枚码不会再发给**别的设备**
        mac2 = "aa:bb:cc:dd:ee:b2"
        _, b_other, _ = ota(mac2)
        c_other = act_of(b_other).get("code")
        check("B14 另一台设备（不同 MAC）拿到自己的码，而不是 A 的那枚",
              bool(c_other) and c_other != c1, f"{c1} vs {c_other}")
        check("B15 直连库：A 已绑定 ⇒ 0 枚有效码；B 未绑定 ⇒ 1 枚（互不串）",
              len(valid_codes(srv.db, mac)) == 0 and len(valid_codes(srv.db, mac2)) == 1,
              f"A={len(valid_codes(srv.db, mac))} B={len(valid_codes(srv.db, mac2))}")

        # ---- 额外重点：解绑后旧 device_token 是否还有效 ----
        print("\n--- 额外：解绑后旧设备凭证（device_token）是否失效 ---")
        pre_status = None
        if ws_token:
            pre_status, pre_body, _ = req("POST", "/api/device/sync", token=ws_token)
            check("X1 解绑前：设备 token 调 /api/device/sync 正常（基线）",
                  pre_status == 200, f"http={pre_status}")

        # ---- R5：解绑权限 ----
        print("\n=== 阶段 B：R5 管理员解绑 ===")
        if pk and tok_a:
            code, body, _ = req("POST", f"/api/devices/{pk}/unbind", token=tok_a)
            detail = (body or {}).get("detail") or ""
            check("C1 ★ 非管理员（设备主人本人）调 unbind 必须 403",
                  code == 403, f"http={code} detail={detail!r}")

        admin = login(ADMIN_USER, LEGACY_PW)
        check("C2 管理员 admin/admin123 登录成功", bool(admin))
        if pk and admin:
            code, _, _ = req("POST", f"/api/devices/{pk}/unbind", token=admin)
            check("C3 ★ 管理员解绑成功（204）", code == 204, f"http={code}")

            # ---- 解绑后 WS 必须握不上（本轮核心：没有任何日志的静默失效）----
            print("\n--- 解绑后的 WS 握手（核心）---")
            if ws_token:
                r, cc, detail = ws_probe(ws_token, mac)
                check("W2 ★ 解绑后同一枚 token 握不上 WS，且关闭码 = CLOSE_UNAUTHORIZED",
                      r == "closed" and cc == CLOSE_UNAUTHORIZED,
                      f"result={r} close_code={cc} 期望={CLOSE_UNAUTHORIZED} detail={detail}")

            # 伪造一枚代次错误的 token：若 session.py 那句校验被注释掉，
            # W2 可能因别的原因绿，而这条会红 —— 它是「校验真的在跑」的判据。
            row_now = device_row(srv.db, mac)
            epoch_now = row_now[4] if row_now else None
            if pk:
                forged = issue_device_token(pk, norm(mac), 99)
                r, cc, detail = ws_probe(forged, mac)
                check("W3 ★ 代次不匹配（ep=99）的 token 握不上 WS",
                      r == "closed" and cc == CLOSE_UNAUTHORIZED,
                      f"result={r} close_code={cc} 期望={CLOSE_UNAUTHORIZED} "
                      f"库内epoch={epoch_now} detail={detail}")

                # 对照：代次正确、但 Device-Id 是别的设备 ⇒ 必须是 4001 而不是 4401。
                # 没有这条，「关闭码恰好是 4401」可能只是所有失败都关成 4401。
                if epoch_now is not None:
                    good = issue_device_token(pk, norm(mac), int(epoch_now))
                    r, cc, detail = ws_probe(good, "aa:bb:cc:dd:ee:ff")
                    check("W4 （对照）代次正确但 Device-Id 不符 ⇒ CLOSE_DEVICE_MISMATCH",
                          r == "closed" and cc == CLOSE_DEVICE_MISMATCH,
                          f"result={r} close_code={cc} 期望={CLOSE_DEVICE_MISMATCH} detail={detail}")

            code, body, _ = req("POST", f"/api/devices/{pk}/unbind", token=admin)
            detail = (body or {}).get("detail") or ""
            check("C4 重复解绑返回 400（不是 500）", code == 400, f"http={code} detail={detail!r}")

            code, body, _ = req("POST", "/api/devices/999999/unbind", token=admin)
            check("C5 解绑不存在的设备返回 404", code == 404, f"http={code}")

        code, b4, _ = ota(mac)
        c4 = act_of(b4).get("code")
        check("C6 ★ 解绑后同一设备 OTA 重新出现 activation 段",
              code == 200 and "activation" in (b4 or {}) and bool(c4),
              f"http={code} code={c4} keys={sorted((b4 or {}).keys())}")
        check("C7 解绑后下发的是新码（不是被用掉的那枚）",
              bool(c4) and c4 != c1, f"old={c1} new={c4}")
        row = device_row(srv.db, mac)
        check("C8 直连库：解绑后 owner_id 已清空（设备行保留）",
              row is not None and row[1] is None, f"row={row}")

        # ---- 额外重点：解绑后旧 token ----
        #
        # ⚠️ 历史快照（重要）：本脚本第一版在这两条上断言的是「仍可用（200）」，
        # 因为 17:33~17:35 那次运行**实测就是 200**：解绑只清了归属，没清访问权。
        # 随后 deps.get_current_device 加了 token_epoch 代次校验（并发落地），
        # 现在期望值反过来 —— 旧 token 必须 401。断言方向改的是**产品期望**，
        # 不是为了让测试变绿：原始 200 的证据保留在本报告的「历史快照」一节。
        if ws_token:
            code, body, _ = req("POST", "/api/device/sync", token=ws_token)
            check("X2 ★ 解绑后旧 device_token 必须被拒（401）",
                  code == 401, f"http={code}（解绑前={pre_status}）")
            row2 = device_row(srv.db, mac)
            print(f"        → 旧 token 解绑后状态码 = {code}；"
                  f"响应含 reminders={'reminders' in (body or {})}")
            print(f"        → 解绑后设备行 = {row2}")

        # ---- 缺口的严重度：解绑后换绑给 B，旧 token 还能不能读到 B 的提醒 ----
        if ws_token and tok_b and c4 and pk:
            print("\n--- 额外：旧 token 在「已换绑给 B」之后是否还能读数据 ---")
            code, _, _ = req("POST", "/api/devices/bind", {"code": c4}, token=tok_b)
            check("X3 新码可由账户 B 重新绑定（换绑闭环成立）", code == 200, f"http={code}")
            if code == 200:
                marker = f"QAB{os.getpid()}越权探针"
                code, _, _ = req("POST", f"/api/reminders?device_pk={pk}",
                                 {"title": marker, "speak_text": marker,
                                  "times_of_day": ["08:00"]}, token=tok_b)
                check("X4 账户 B 能在该设备上建提醒", code == 201, f"http={code}")
                code, sbody, raw = req("POST", "/api/device/sync", token=ws_token)
                leaked = marker in raw
                check("X5 ★ 换绑给 B 之后，旧 token 读不到 B 新建的提醒",
                      code == 401 and not leaked, f"http={code} 原文含标记={leaked}")

                # 根因补充：设备侧身份只有 MAC —— 任何人知道 MAC 都能从无鉴权
                # OTA 端点领到同一台已绑定设备的 token。
                code, b6, _ = ota(mac)
                fresh = ((b6 or {}).get("websocket") or {}).get("token")
                code, _, raw6 = req("POST", "/api/device/sync", token=fresh)
                check("X6 （设计性观察，非缺陷）仅凭 MAC 从 OTA 领到的新 token 可读该设备数据",
                      code == 200 and marker in raw6, f"http={code} 含标记={marker in raw6}")


# ============================================================ 阶段 C：代次修复的存量库
def phase_c() -> None:
    """验证「解绑吊销 token」这个修复对**存量库**是安全的。

    修复本身（deps.get_current_device 校验 ep）不是我的验证对象，但它是验证期间
    才落地的改动，而它引入了一个新列 —— 存量库没有这一列时，若迁移缺失，
    每一次设备心跳都会 500。所以这里补两条：
      * 迁移能把列补回来；
      * 升级**之前**签发的 token（没有 ep claim）不会被一刀切锁死。
    """
    print("\n=== 阶段 C：token_epoch 修复的存量库迁移与旧 token 兼容 ===")
    import jwt as pyjwt

    mac = "aa:bb:cc:dd:ee:c1"
    tag = "c"
    db = DB_DIR / f"qa_b_{tag}_{os.getpid()}.db"
    log = DB_DIR / f"qa_b_{tag}_{os.getpid()}.log"
    secret = os.environ.get("ELDER_JWT_SECRET", "please-change-this-secret-in-production")

    try:
        with Server("10", tag, keep_db=True):
            pass                                  # 第一次启动：建出完整表结构
        con = sqlite3.connect(db)
        con.execute("ALTER TABLE devices DROP COLUMN token_epoch")
        con.commit()
        cols = [r[1] for r in con.execute("PRAGMA table_info(devices)")]
        con.close()
        check("C9 已构造「存量库」：devices 表里没有 token_epoch 列",
              "token_epoch" not in cols, f"cols={len(cols)}")

        with Server("10", tag, fresh=False, keep_db=True):
            con = sqlite3.connect(db)
            cols = [r[1] for r in con.execute("PRAGMA table_info(devices)")]
            con.close()
            check("C10 启动期 schema_patch 自动补回 token_epoch 列",
                  "token_epoch" in cols, f"has_col={'token_epoch' in cols}")

            _, b, _ = ota(mac)
            c = act_of(b).get("code")
            _, tok = new_account("d")
            code, body, _ = req("POST", "/api/devices/bind", {"code": c}, token=tok)
            pk = (body or {}).get("device_pk")
            check("C11 迁移后绑定流程可用", code == 200 and bool(pk), f"http={code} pk={pk}")

            _, b2, _ = ota(mac)
            newtok = ((b2 or {}).get("websocket") or {}).get("token")
            code, _, _ = req("POST", "/api/device/sync", token=newtok)
            check("C12 迁移后新签发的设备 token 调 sync 正常", code == 200, f"http={code}")

            legacy = pyjwt.encode({"sub": str(pk), "typ": "device",
                                   "device_id": norm(mac)}, secret, algorithm="HS256")
            code, _, _ = req("POST", "/api/device/sync", token=legacy)
            check("C13 升级前签发的旧 token（无 ep claim）仍可用（存量设备不被锁死）",
                  code == 200, f"http={code}")

            stale = pyjwt.encode({"sub": str(pk), "typ": "device", "device_id": norm(mac),
                                  "ep": 99}, secret, algorithm="HS256")
            code, _, _ = req("POST", "/api/device/sync", token=stale)
            check("C14 代次不匹配的 token 被 401（吊销判据真的生效）", code == 401, f"http={code}")
    except SystemExit:
        check("C9-C14 阶段 C 跑通（存量库启动未失败）", False, "服务起不来，见日志")
    finally:
        if os.environ.get("QA_B_KEEP_LOG") != "1":
            for p in (db, log):
                try:
                    if p.exists():
                        unlink_retry(p)
                except Exception:  # noqa: BLE001
                    pass


# ============================================================ 静态：四端文案
def static_checks() -> None:
    print("\n=== 静态核查：四端激活码文案一致性 ===")
    files = {
        "server/app/static/user.html": ROOT / "server/app/static/user.html",
        "server/app/static/index.html": ROOT / "server/app/static/index.html",
        "app/src/pages/device/index.vue": ROOT / "app/src/pages/device/index.vue",
        "server/app/config.py": ROOT / "server/app/config.py",
    }
    for label, path in files.items():
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        hits13 = [(i + 1, ln.strip()) for i, ln in enumerate(lines)
                  if ("13 位" in ln or "13位" in ln or "!== 13" in ln or "length !== 13" in ln
                      or 'maxlength="13"' in ln)]
        hits7 = [(i + 1, ln.strip()) for i, ln in enumerate(lines)
                 if ("7 位" in ln or "7位" in ln or "CODE_LEN = 7" in ln
                     or "ACT_CODE_LEN = ACT_CODE_BODY_LEN + 1" in ln)]
        hits10 = [(i + 1, ln.strip()) for i, ln in enumerate(lines)
                  if ("10 分钟" in ln or "10分钟" in ln or '"10"' in ln)]
        print(f"\n  {label}")
        print(f"    7 位命中: {[(n, s[:70]) for n, s in hits7][:4]}")
        print(f"    10 分钟命中: {[(n, s[:70]) for n, s in hits10][:4]}")
        print(f"    13 位残留: {[(n, s[:70]) for n, s in hits13]}")
        if label.endswith("user.html"):
            check("S1 user.html 不再有 13 位前端校验（否则 7 位码被前端拦死）",
                  len(hits13) == 0, f"hits={len(hits13)}")
            check("S2 user.html 文案写的是 7 位",
                  len(hits7) > 0, f"hits={[n for n, _ in hits7][:3]}")
            check("S3 user.html 文案写的是 10 分钟有效",
                  len(hits10) > 0, f"hits={[n for n, _ in hits10][:3]}")
        if label.endswith("index.vue"):
            check("S4 APP 设备页 CODE_LEN=7 且无 13 位残留",
                  "const CODE_LEN = 7" in text and len(hits13) == 0,
                  f"CODE_LEN=7 {'在' if 'const CODE_LEN = 7' in text else '缺'}；13位残留={len(hits13)}")
        if label.endswith("config.py"):
            check("S5 config.ACT_CODE_LEN = 6+1 = 7，TTL 默认 10 分钟",
                  "ACT_CODE_BODY_LEN = 6" in text
                  and "ACT_CODE_LEN = ACT_CODE_BODY_LEN + 1" in text
                  and 'ELDER_ACT_TTL_MIN", "10"' in text,
                  "见 config.py 激活码一节")
        if label.endswith("index.html"):
            check("S6 index.html（管理端）不含激活码位数/有效期自述（无第二套口径）",
                  len(hits7) == 0 and len(hits13) == 0,
                  f"7位命中={len(hits7)} 13位命中={len(hits13)}")


def main() -> int:
    t0 = time.time()
    try:
        phase_a()
        phase_b()
        phase_c()
        static_checks()
    finally:
        print(f"\n耗时 {time.time() - t0:.1f}s")
        print(f"PASS {len(_passes)} / FAIL {len(_failures)}")
        if _failures:
            print("FAIL 清单：")
            for f in _failures:
                print("  - " + f)
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
