"""冒烟：激活码的 10 分钟 TTL、服务端锁定、已绑定不发码、管理员解绑。

覆盖用户 2026-10-20 拍板的五条规则（每条都要有**行为级**断言，不看代码）：

  R1 激活码有效期 10 分钟，过期后重新下发**新的**码（码值必须变）；
  R2 有效期内服务端锁定：同一台设备同一时刻只有一枚有效码，换新码时旧码立即作废；
  R3 已绑定设备不再下发激活码（OTA 响应里没有 activation 段）；
  R4 已绑定设备不允许重复绑定（用别人的账户再绑一次必须被拒）；
  R5 用户忘记用户名时，唯一通道是管理员后台解绑（非管理员必须 403）。

时间相关的断言怎么做到不 sleep 10 分钟
------------------------------------
**两阶段**，各自起一个服务实例：

  * 阶段 A 注入 ``ELDER_ACT_TTL_MIN=0`` —— ``code_expires_at()`` 返回 now，码刚
    发出就过期。用来逼出「过期 ⇒ 换新码」分支：连打两次 OTA，码值必须不同，
    且第一枚拿去绑定必须被拒（已过期）。这不是绕过被测逻辑，被测的正是
    ``_reusable_activation_code`` 里的 ``expires_at > now`` 判据。
  * 阶段 B 用**生产默认 TTL=10** —— 验「有效期内复用同一枚码」（服务端锁定）、
    绑定成功、已绑定不发码、不允许重复绑定、管理员解绑。

为什么必须分两次起服务：TTL=0 时任何码都绑不上（生成即过期），把绑定相关
断言和过期断言塞进同一个实例里，等于用一个必然失败的夹具去测另一件事
（本脚本第一版就是这么错的：5 条绑定断言全红，而代码是对的）。

用法：
    python tools/smoke_activation_ttl.py
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


def main() -> int:
    try:
        phase_a()
        phase_b()
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
