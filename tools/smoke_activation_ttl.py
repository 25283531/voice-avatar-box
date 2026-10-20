"""冒烟：激活码的 10 分钟 TTL、服务端锁定、已绑定不发码、管理员解绑。

覆盖用户 2026-10-20 拍板的五条规则（每条都要有**行为级**断言，不看代码）：

  R1 激活码有效期 10 分钟，过期后重新下发**新的**码（码值必须变）；
  R2 有效期内服务端锁定：同一台设备同一时刻只有一枚有效码，换新码时旧码立即作废；
  R3 已绑定设备不再下发激活码（OTA 响应里没有 activation 段）；
  R4 已绑定设备不允许重复绑定（用别人的账户再绑一次必须被拒）；
  R5 用户忘记用户名时，唯一通道是管理员后台解绑（非管理员必须 403）。

时间相关的断言怎么做到不 sleep 10 分钟
------------------------------------
**三阶段**，各自起一个服务实例：

  * 阶段 A 注入 ``ELDER_ACT_TTL_MIN=0`` —— ``code_expires_at()`` 返回 now，码刚
    发出就过期。用来逼出「过期 ⇒ 换新码」分支：连打两次 OTA，码值必须不同，
    且第一枚拿去绑定必须被拒（已过期）。这不是绕过被测逻辑，被测的正是
    ``_reusable_activation_code`` 里的 ``expires_at > now`` 判据。
  * 阶段 B 用**生产默认 TTL=10** —— 验「有效期内复用同一枚码」（服务端锁定）、
    绑定成功、已绑定不发码、不允许重复绑定、管理员解绑。
  * 阶段 C（2026-10-20 QA 补，另起一个 TTL=10 实例）—— 专门守
    ``_retire_codes_for`` 的**作废语义**：绑定成功后，该设备名下**不能残留**
    仍然有效且未使用的码。这一条此前**没有任何脚本守**：QA 把
    ``_retire_codes_for`` 的条件写反（只「退役」已经过期的行 = 只新建不废旧码），
    阶段 A/B 与 qa_b 全部照样绿 —— 属「有防线、无回归网」的假绿。

为什么必须分三个实例起服务：TTL=0 时任何码都绑不上（生成即过期），把绑定相关
断言和过期断言塞进同一个实例里，等于用一个必然失败的夹具去测另一件事
（本脚本第一版就是这么错的：5 条绑定断言全红，而代码是对的）；阶段 C 则要求
「绑定前该 MAC 只有一枚有效码 + 一枚人为残留」，与阶段 B 的既有夹具状态不同。
  * 阶段 C（2026-10-20 QA 补，另起一个 TTL=10 实例）—— 专门守
    ``_retire_codes_for`` 的**作废语义**：绑定成功后，该设备名下**不能残留**
    仍然有效且未使用的码。这一条此前**没有任何脚本守**：QA 把
    ``_retire_codes_for`` 的条件写反（只「退役」已经过期的行 = 只新建不废旧码），
    阶段 A/B 与 qa_b 全部照样绿 —— 属「有防线、无回归网」的假绿。

为什么阶段 C 要**直连库播种**一枚「残留码」，而不是「再取一次码产生新码」
--------------------------------------------------------------------
「再触发一次取码就产生新码、旧码被作废」在当前实现里**不可能发生**：
TTL=10 且未过期时 ``_reusable_activation_code`` 命中复用，压根不走新建分支
（阶段 B 第 3 条断言的正是这件事）。所以想让「同一 MAC 同时有两枚有效码」，
唯一现实来源就是**库里的历史残留** —— 旧版本服务端签发过、或管理员手工改过
归属（这正是 ``_retire_codes_for`` docstring 点名的真实场景），于是用直连
SQLite 播一枚 ``c_stale`` 来复现它。

⚠️ 也不能拿阶段 A（TTL=0）的旧码来充当「被作废的旧码」：那枚码是**已过期**
才绑不上的，绑不上与退役无关 ⇒ 断言恒绿（假绿）。阶段 C 的 ``c_stale`` 在
绑定前**仍然有效**，绑定后才会因为退役而失效 —— 这是它唯一有鉴别力的形态。

用法：
    python tools/smoke_activation_ttl.py
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_common import unlink_retry  # noqa: E402
from smoke_ports import detect_addr_in_use, guard_or_die, guard_port, release_port, resolve_port  # noqa: E402

_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
DB_PATH = Path(os.environ.get("SMOKE_DB", str(DB_DIR / f"elder_act_ttl_{os.getpid()}.db")))
if DB_PATH.exists():
    unlink_retry(DB_PATH)

PORT = resolve_port("smoke_activation_ttl")
BASE = f"http://127.0.0.1:{PORT}"
SERVER_LOG = DB_DIR / f"act_ttl_server_{os.getpid()}.log"

LEGACY_PW = "admin123"
ADMIN_USER = "admin"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        _failures.append(name)
    return ok


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


def ota(mac: str) -> tuple[int, dict]:
    return req("GET", "/api/voice/ota", headers={"Device-Id": mac})


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


def _log_tail(max_bytes: int = 8192) -> str:
    try:
        return SERVER_LOG.read_bytes()[-max_bytes:].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return "<no log>"


def login(username: str, password: str) -> str | None:
    code, body = req("POST", "/api/auth/login", {"username": username, "password": password})
    return (body or {}).get("access_token") if code == 200 else None


def register(username: str, password: str) -> str | None:
    code, body = req("POST", "/api/auth/register", {"username": username, "password": password})
    if code in (200, 201):
        return (body or {}).get("access_token") or login(username, password)
    return None


# ============================================================ 起停服务
class Server:
    """一次服务实例的生命周期（两阶段各起一次，见模块 docstring）。"""

    def __init__(self, ttl_min: str, tag: str) -> None:
        self.ttl_min = ttl_min
        self.tag = tag
        self.db = DB_DIR / f"elder_act_ttl_{tag}_{os.getpid()}.db"
        self.log = DB_DIR / f"act_ttl_{tag}_{os.getpid()}.log"
        self.proc = None
        self.fp = None

    def __enter__(self) -> "Server":
        if self.db.exists():
            unlink_retry(self.db)
        env = dict(os.environ)
        env.pop("MSYSTEM", None)
        env["ELDER_DB"] = f"sqlite:///{self.db.as_posix()}"
        env["ELDER_SEED_ADMIN_PASSWORD"] = LEGACY_PW
        env["ELDER_ACT_TTL_MIN"] = self.ttl_min
        env["ELDER_OTA_NEW_CODE_PER_HOUR"] = "1000"

        print(f"[boot:{self.tag}] port={PORT} TTL={self.ttl_min}min db={self.db.name}")
        guard_or_die(PORT, "smoke_activation_ttl.py")
        hold, hint = guard_port(PORT)
        if hold is None:
            print("[boot] " + hint.replace("\n", "\n[boot] "))
            raise SystemExit(1)
        release_port(hold, PORT)

        self.fp = self.log.open("w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT),
             "--log-level", "warning"],
            cwd=str(SERVER), env=env,
            stdout=self.fp, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace",
        )
        if not wait_ready():
            print("[boot] server did not become ready. log tail:")
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
            try:
                if p.exists():
                    unlink_retry(p)
            except Exception:  # noqa: BLE001
                pass


def phase_a() -> None:
    """TTL=0：过期即换新码，且旧码不能再用于绑定。"""
    print("--- 阶段 A：TTL=0（过期换新码）---")
    mac = "aa:bb:cc:dd:ee:01"
    with Server("0", "a"):
        code, b1 = ota(mac)
        c1 = ((b1 or {}).get("activation") or {}).get("code")
        check("首次请求拿到 7 位纯数字码",
              code == 200 and bool(c1) and str(c1).isdigit() and len(str(c1)) == 7,
              f"http={code} code={c1}")
        act = (b1 or {}).get("activation") or {}
        check("activation 段带 expires_in / expires_at",
              "expires_in" in act and "expires_at" in act, str(list(act.keys())))

        code, b2 = ota(mac)
        c2 = ((b2 or {}).get("activation") or {}).get("code")
        check("★ TTL 过期后重新下发的是新码（码值必须变）",
              code == 200 and bool(c2) and c2 != c1, f"{c1} -> {c2}")

        tok = register(f"ttla{os.getpid() % 100000}", "Passw0rd!23")
        check("测试用户注册成功", bool(tok))
        if tok:
            code, body = req("POST", "/api/devices/bind", {"code": c1}, token=tok)
            detail = (body or {}).get("detail") or ""
            check("★ 过期码拿去绑定被拒（不会因为「曾经有效」而放行）",
                  code in (400, 404), f"http={code} detail={detail!r}")


def phase_b() -> None:
    """TTL=10（生产默认）：有效期内复用同一枚码 = 服务端锁定；绑定与解绑。"""
    print("\n--- 阶段 B：TTL=10（锁定 / 已绑定不发码 / 管理员解绑）---")
    mac = "aa:bb:cc:dd:ee:11"
    mac2 = "aa:bb:cc:dd:ee:12"
    with Server("10", "b"):
        code, b1 = ota(mac)
        c1 = ((b1 or {}).get("activation") or {}).get("code")
        code, b2 = ota(mac)
        c2 = ((b2 or {}).get("activation") or {}).get("code")
        check("★ 有效期内重复请求拿到**同一枚**码（服务端锁定，不重复下发新码）",
              code == 200 and c1 == c2, f"{c1} == {c2}")
        left = ((b1 or {}).get("activation") or {}).get("expires_in")
        check("有效期是 10 分钟量级（expires_in 在 540~600 秒）",
              isinstance(left, int) and 540 <= left <= 600, f"expires_in={left}")

        tok = register(f"ttlb{os.getpid() % 100000}", "Passw0rd!23")
        check("账户 A 注册成功", bool(tok))
        code, body = req("POST", "/api/devices/bind", {"code": c1}, token=tok)
        check("有效码绑定成功", code == 200, f"http={code} body={body}")

        code, b3 = ota(mac)
        check("★ 已绑定设备的 OTA 响应**没有** activation 段",
              code == 200 and "activation" not in (b3 or {}),
              f"keys={list((b3 or {}).keys())}")
        check("已绑定设备仍拿到 websocket + token（设备能正常连）",
              bool(((b3 or {}).get("websocket") or {}).get("token")), "有 token")

        # 定位 device_pk
        pk = None
        code, lst = req("GET", "/api/devices", token=tok)
        for d in (lst or []):
            if str(d.get("device_id", "")).lower().replace(":", "") == mac.replace(":", ""):
                pk = d.get("id")
        check("能定位到该设备的 device_pk", pk is not None, f"pk={pk}")

        # 已绑定设备调 /code 必须拒绝
        if pk is not None:
            code, body = req("POST", f"/api/devices/{pk}/code", token=tok)
            check("★ 已绑定设备调 /code 被 409 拒绝（不再下发激活码）",
                  code == 409, f"http={code}")

        # 同一枚码不能绑给第二个账户
        _, bn = ota(mac2)
        c_new = ((bn or {}).get("activation") or {}).get("code")
        tok2 = register(f"ttlb2{os.getpid() % 100000}", "Passw0rd!23")
        if tok and tok2 and c_new:
            code, _ = req("POST", "/api/devices/bind", {"code": c_new}, token=tok)
            check("设备 2 先绑给账户 A", code == 200, f"http={code}")
            code, body = req("POST", "/api/devices/bind", {"code": c_new}, token=tok2)
            detail = (body or {}).get("detail") or ""
            check("★ 同一枚码不允许绑给第二个账户",
                  code in (400, 409), f"http={code} detail={detail!r}")

        # 管理员解绑
        admin_tok = login(ADMIN_USER, LEGACY_PW)
        check("管理员登录成功", bool(admin_tok))

        # ---- MAC 两种历史写法必须指向同一台设备（R3 的关键泄漏点）----
        # 网页端「登记设备」存的是不带冒号形式；固件 Device-Id 头带冒号。
        # 若比较不做归一化，这台**已绑定**设备会被判成未绑定并下发激活码。
        mac3_nocolon = "aabbccddee13"
        mac3_colon = "aa:bb:cc:dd:ee:13"
        if tok:
            code, _ = req("POST", "/api/devices",
                          {"device_id": mac3_nocolon, "name": "网页登记设备"}, token=tok)
            check("网页端按无冒号 MAC 登记设备", code == 201, f"http={code}")
            code, b5 = ota(mac3_colon)
            check("★ 固件用**带冒号** MAC 请求时，该设备被认成已绑定（不发激活码）",
                  code == 200 and "activation" not in (b5 or {}),
                  f"keys={list((b5 or {}).keys())}")
            check("且仍能拿到 websocket（说明是按同一行查到的，不是查不到）",
                  bool(((b5 or {}).get("websocket") or {}).get("token")), "有 token")

        if pk is not None and admin_tok:
            code, _ = req("POST", f"/api/devices/{pk}/unbind", token=tok)
            check("★ 非管理员解绑被拒（403）", code == 403, f"http={code}")
            code, _ = req("POST", f"/api/devices/{pk}/unbind", token=admin_tok)
            check("★ 管理员解绑成功（204）", code == 204, f"http={code}")

            code, b4 = ota(mac)
            c_after = ((b4 or {}).get("activation") or {}).get("code")
            check("★ 解绑后设备重新拿到激活码（可被新账户绑定）",
                  code == 200 and bool(c_after), f"code={c_after}")

            code, _ = req("POST", f"/api/devices/{pk}/unbind", token=admin_tok)
            check("重复解绑返回 400（不是 500）", code == 400, f"http={code}")


def skip(name: str, reason: str) -> None:
    """第三态：判据对象不存在。

    D33：门禁只能约束**已存在**的东西。前置自检没过（比如这一版实现不再出现
    「同一 MAC 两枚有效码」）时，后续断言会**因为别的原因**通过 —— 那是假绿，
    必须 SKIP 且单列可见，不能算「通过」。
    """
    print(f"  [SKIP] {name}  {reason}")


def _seed_stale_code(db_path: Path, mac: str) -> str:
    """直连 SQLite 播一枚「残留的有效未用码」，返回码值。

    为什么必须直连库：见模块 docstring「为什么阶段 C 要直连库播种」。
    码值用产品自己的 ``generate_activation_code()``（不手写 Luhn）：手写一份
    7 位校验算法，一旦算错，绑定会因**格式**被拒，断言就变成假绿。
    """
    import contextlib
    import io

    sys.path.insert(0, str(SERVER))
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from app.security import generate_activation_code

    code = generate_activation_code()
    plain = mac.lower().replace(":", "")
    con = sqlite3.connect(str(db_path), timeout=10)
    try:
        con.execute(
            "INSERT INTO activation_codes (code, device_id, used, attempts,"
            " max_attempts, expires_at, created_at)"
            " VALUES (?, ?, 0, 0, 5, datetime('now','+9 minutes'), datetime('now'))",
            (code, plain))
        con.commit()
    finally:
        con.close()
    return code


def _valid_unused_codes(db_path: Path, mac: str) -> list[str]:
    """直连库数「该 MAC 仍然有效且未使用」的码（不信接口自述）。

    ⚠️ 用 ``julianday()`` 而不是 ``expires_at > datetime('now')``：
    退役写入的是 ``expires_at = now``（**带微秒**，如 ``...02:35:12.123456``），
    而 ``datetime('now')`` 精确到秒（``...02:35:12``）。裸字符串比较时
    ``"…12.123456" > "…12"`` 为真 ⇒ 刚被退役的行仍被数成「有效」，
    这条断言就会**恒红**（红得莫名其妙，且改断言的人容易顺手把它改成恒绿）。
    ``julianday()`` 按时间数值比较，微秒不影响边界判断。
    """
    plain = mac.lower().replace(":", "")
    con = sqlite3.connect(str(db_path), timeout=10)
    try:
        rows = con.execute(
            "SELECT code FROM activation_codes"
            " WHERE device_id = ? AND used = 0"
            " AND julianday(expires_at) > julianday('now')",
            (plain,)).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def phase_c() -> None:
    """``_retire_codes_for`` 作废语义的行为断言（此前完全没有回归网）。

    判据（与 QA 变异探针 ``logs/qa_regress/m2_gap_probe.py`` 同一套，未另发明）：
      1. 正常取码 ``c_keep``（有效）；
      2. 直连库播一枚 ``c_stale``（**同样有效**且未使用）—— 模拟历史残留码；
      3. 账户 A 用 ``c_keep`` 绑定成功 ⇒ ``c_stale`` 必须立刻作废；
      4. 绑定成功后，该 MAC 名下**有效未用码 = 0**（直连库）；
      5. 管理员解绑后用 ``c_stale`` 去绑 ⇒ 必须 400/404（且不是「设备已绑定」）。
    """
    print("\n--- 阶段 C：_retire_codes_for 作废语义（绑定后不留可用码）---")
    mac = "aa:bb:cc:dd:ee:21"
    with Server("10", "c") as srv:
        code, b = ota(mac)
        c_keep = ((b or {}).get("activation") or {}).get("code")
        ok = check("C1 取到一枚有效码 c_keep", code == 200 and bool(c_keep), f"http={code} {c_keep}")
        if not ok:
            skip("C2/C3/C4 作废语义", "没拿到基准码，夹具不成立")
            return

        # ---- 前置自检：必须真的存在「两枚有效码」，否则后面的 0 是空断言 ----
        c_stale = _seed_stale_code(srv.db, mac)
        valid_before = _valid_unused_codes(srv.db, mac)
        ok = check("C2 前置自检：播种后该 MAC 确实有 2 枚有效未用码",
                   len(valid_before) == 2, f"codes={valid_before}")
        if not ok:
            skip("C3/C4 作废语义", "前置不成立（本版实现不再允许两枚有效码）⇒ 断言会空转")
            return

        tok_a = register(f"ttlc{os.getpid() % 100000}", "Passw0rd!23")
        check("C3 账户 A 可用", bool(tok_a))
        code, _ = req("POST", "/api/devices/bind", {"code": c_keep}, token=tok_a)
        check("C4 用 c_keep 绑定成功", code == 200, f"http={code}")

        # ★ 断言一：绑定成功后不能残留任何仍有效未用的码（red 于「只新建不废旧码」）
        left = _valid_unused_codes(srv.db, mac)
        check("★ C5 绑定成功后该 MAC 名下有效未用码 = 0（残留码必须被作废）",
              left == [], f"left={left}（c_stale={c_stale}）")

        # 为了排除「已绑定」这个与作废无关的拒绝理由：先解绑，再拿残留码去绑。
        admin_tok = login(ADMIN_USER, LEGACY_PW)
        pk = None
        code, lst = req("GET", "/api/devices", token=tok_a)
        for d in (lst or []):
            if str(d.get("device_id", "")).lower().replace(":", "") == mac.replace(":", ""):
                pk = d.get("id")
        check("C6 能定位到 device_pk", pk is not None, f"pk={pk}")
        if pk is None or not admin_tok:
            skip("C7/C8 残留码不可再绑", "解绑前置不成立")
            return
        code, _ = req("POST", f"/api/devices/{pk}/unbind", token=admin_tok)
        check("C7 管理员解绑成功（204）", code == 204, f"http={code}")

        # ★ 断言二：换绑通道上，残留码必须已经失效（red 于「只新建不废旧码」：
        #   变异下这枚码仍然有效且设备已解绑 ⇒ 绑定会成功 200）。
        tok_b = register(f"ttlc2{os.getpid() % 100000}", "Passw0rd!23")
        check("C8 账户 B 可用", bool(tok_b))
        if tok_b:
            code, body = req("POST", "/api/devices/bind", {"code": c_stale}, token=tok_b)
            detail = (body or {}).get("detail") or ""
            check("★ C9 残留码 c_stale 不得把设备绑走（400/404）",
                  code in (400, 404), f"http={code} detail={detail!r}")
            # 鉴别力备注：只断言 400 不够 —— 「设备已绑定」也会给 400/409。
            # C7 已解绑，所以这里若出现「已绑定」字样，说明拒绝理由与本缺陷无关。
            check("C10 拒绝理由不是「设备已绑定」（排除无关原因）",
                  "已绑定" not in str(detail), f"detail={detail!r}")
            code, lst_b = req("GET", "/api/devices", token=tok_b)
            got = [d.get("id") for d in (lst_b or [])]
            check("★ C11 账户 B 名下没有这台设备（绑定确实没发生）",
                  code == 200 and not got, f"http={code} devices={got}")


def main() -> int:
    try:
        phase_a()
        phase_b()
        phase_c()
    except SystemExit:
        raise
    print()
    if _failures:
        print(f"✗ {len(_failures)} 项失败：{_failures}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
