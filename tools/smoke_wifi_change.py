"""冒烟：已配网设备「改 WiFi」服务端（契约 build-out/_wifi-spec.md §一）。

守的四条不变量
--------------
1. **离线直接拒绝**（409 + 可行动的文案）。排队会让家属拿到假成功：提交时
   200、设备几小时后才上线试连、失败回滚，家属全程不知情。
2. **pending 只下发一次**。下发后立刻置 delivered，否则设备每轮心跳都收到同
   一条指令 → 反复 Stop/Start → 老人家里 WiFi 图标闪一整天。
3. **提速只生效一次**。pending 时 15 秒，下一跳必须回 300。永久 15 秒等于
   把全平台设备的心跳量放大 20 倍，收益为零。
4. **过期 req_id 的回报必须被忽略**。设备侧回报是"至少一次"语义（NVS 队列
   重发 + sync 请求体里也带），旧结果覆盖新请求会让一条还没试过的请求被上一次
   的失败判死。

为什么必须直连 SQLite 读 wifi_state / wifi_req_pwd
--------------------------------------------------
「密码被清空了」这件事**任何接口都看不到**（GET 只回 state/req_id/ssid），
问接口等于让被测方自证。而"明文密码长期留库"正是这组列唯一的安全纪律，
所以只能绕过接口直接看盘上那一行。

用法：
    python tools/smoke_wifi_change.py [--port 8138]
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
from smoke_ports import (  # noqa: E402
    detect_addr_in_use,
    guard_or_die,
    guard_port,
    release_port,
    resolve_port,
)

# 本地请求绝不走代理（本机/CI 都可能注入了 http_proxy，走代理会超时而零断言失败）
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
DB_PATH = Path(os.environ.get("SMOKE_DB", str(DB_DIR / f"elder_wifi_change_{os.getpid()}.db")))
SERVER_LOG = DB_DIR / f"wifi_change_server_{os.getpid()}.log"

PORT = resolve_port("smoke_wifi_change")
BASE = f"http://127.0.0.1:{PORT}"

JWT_SECRET = "smoke-wifi-change-secret-0123456789abcdef"
ADMIN_PW = "admin123"

MAC = "aa:bb:cc:dd:ee:31"
SSID = "MyHome-5G"
PWD = "12345678"

_passed: list[str] = []
_failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))
    (_passed if ok else _failed).append(name)
    return ok


def req(method: str, path: str, body: dict | None = None, token: str | None = None,
        headers: dict | None = None) -> tuple[int, object]:
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


def db_wifi(pk: int) -> dict:
    """直连库读 wifi 六列（唯一能看到「密码是否被清空」的口子，见模块头）。

    ⚠️ 用 ``mode=ro`` 只读打开：本脚本**不许**改被测服务的数据，一旦写入就会
    把「服务端自己清了密码」与「测试脚本清了密码」混成一件事。
    """
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT wifi_req_id, wifi_req_ssid, wifi_req_pwd, wifi_state,"
            " wifi_req_at, wifi_result_at FROM devices WHERE id = ?", (pk,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


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
        env["ELDER_SEED_ADMIN_PASSWORD"] = ADMIN_PW
        env["ELDER_JWT_SECRET"] = JWT_SECRET
        # 本脚本用注册拿普通账户（不用 admin），显式开启公开注册以免被 CI 的
        # job 级 env 关掉 —— 关掉时表现为「注册 403 → 后续断言全红」，很难看出
        # 根因是环境而不是代码。
        env["ELDER_ALLOW_PUBLIC_REGISTER"] = "1"

        print(f"[boot] port={PORT} db={DB_PATH.name}")
        guard_or_die(PORT, "smoke_wifi_change.py")
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


# ============================================================ 账号 / 设备
def login(username: str, password: str) -> str | None:
    code, body = req("POST", "/api/auth/login",
                     {"username": username, "password": password})
    return (body or {}).get("access_token") if code == 200 else None


def register(username: str, password: str) -> str | None:
    """注册并登录（``/register`` 只回 UserOut，**不发 token**，所以必须再登录一次）。"""
    code, _ = req("POST", "/api/auth/register",
                  {"username": username, "password": password})
    if code not in (200, 201):
        return None
    return login(username, password)


def device_token_via_ota(mac: str) -> str | None:
    """已归属设备在 OTA 响应里拿到 ws token（固件真正走的那条路）。"""
    code, body = req("GET", "/api/voice/ota", headers={"Device-Id": mac})
    if code != 200 or not isinstance(body, dict):
        return None
    return ((body.get("websocket") or {}).get("token")) or None


# ============================================================ 断言
def run() -> None:
    tag = os.getpid() % 100000
    tok = register(f"wifi{tag}", "Passw0rd!23")
    check("家属账户注册成功", bool(tok))
    if not tok:
        return

    code, dev = req("POST", "/api/devices", {"device_id": MAC, "name": "客厅设备"}, token=tok)
    pk = (dev or {}).get("id") if isinstance(dev, dict) else None
    check("登记设备（201）", code == 201 and pk is not None, f"http={code} pk={pk}")
    if pk is None:
        return

    dtok = device_token_via_ota(MAC)
    check("设备通过 OTA 拿到 device_token（后续心跳/回报都用它）", bool(dtok))
    if not dtok:
        return

    # 前置自检：新设备**从未心跳**，所以此刻必须真的离线 —— 否则 C1 的 409
    # 可能是别的原因（比如判定写反），C2 之后的"在线"也没有对照。
    row0 = db_wifi(pk)
    check("前置：新设备 wifi_state 为 ''（从未改过），且 wifi_req_id 为 0（哨兵值）",
          row0.get("wifi_state") == "" and row0.get("wifi_req_id") == 0,
          str(row0))

    # ---------------- C1 离线必须拒绝 ----------------
    code, body = req("POST", f"/api/devices/{pk}/wifi",
                     {"ssid": SSID, "password": PWD}, token=tok)
    detail = (body or {}).get("detail", "") if isinstance(body, dict) else ""
    check("C1 ★ 设备离线时提交改 WiFi ⇒ 409", code == 409, f"http={code} body={body}")
    check("C1 detail 是那句可行动的话（含「不在线」）", "不在线" in str(detail),
          repr(detail))
    row1 = db_wifi(pk)
    check("C1 离线被拒时**不留任何痕迹**（state 仍为 ''、req_id 仍为 0）",
          row1.get("wifi_state") == "" and row1.get("wifi_req_id") == 0, str(row1))

    # ---------------- 一次心跳让设备上线 ----------------
    code, sync0 = req("POST", "/api/device/sync", None, dtok)
    check("设备心跳成功（此后 is_online 为真）", code == 200, f"http={code}")
    check("无待下发时 sync 的 wifi_change 为 null、间隔为 300",
          (sync0 or {}).get("wifi_change") is None
          and (sync0 or {}).get("sync_interval_sec") == 300,
          f"wifi_change={(sync0 or {}).get('wifi_change')} "
          f"interval={(sync0 or {}).get('sync_interval_sec')}")

    # ---------------- C2 在线提交 ----------------
    code, body = req("POST", f"/api/devices/{pk}/wifi",
                     {"ssid": SSID, "password": PWD}, token=tok)
    check("C2 在线时提交改 WiFi ⇒ 200", code == 200, f"http={code} body={body}")
    check("C2 响应含 ok=true / state=pending / req_id=1 / ssid 回显",
          isinstance(body, dict) and body.get("ok") is True
          and body.get("state") == "pending" and body.get("req_id") == 1
          and body.get("ssid") == SSID, str(body))
    row2 = db_wifi(pk)
    check("C2 库里 wifi_state == 'pending'", row2.get("wifi_state") == "pending", str(row2))
    check("C2 库里 wifi_req_id 从 0 递增到 1", row2.get("wifi_req_id") == 1, str(row2))
    check("C2 库里 wifi_req_pwd 已落库（下发前设备要靠它取密码）",
          row2.get("wifi_req_pwd") == PWD, str(row2))
    check("C2 库里 wifi_req_at 已写入", row2.get("wifi_req_at") is not None, str(row2))

    # ---------------- C3 下一次心跳下发 + 提速 ----------------
    code, sync1 = req("POST", "/api/device/sync", None, dtok)
    wc = (sync1 or {}).get("wifi_change") if isinstance(sync1, dict) else None
    check("C3 ★ pending 时 sync 响应里出现 wifi_change", code == 200 and wc is not None,
          f"http={code} wifi_change={wc}")
    check("C3 wifi_change 的 req_id/ssid/password 三项都对",
          isinstance(wc, dict) and wc.get("req_id") == 1 and wc.get("ssid") == SSID
          and wc.get("password") == PWD, str(wc))
    check("C3 ★ 有待下发时 sync_interval_sec 提速到 15",
          (sync1 or {}).get("sync_interval_sec") == 15,
          str((sync1 or {}).get("sync_interval_sec")))
    row3 = db_wifi(pk)
    check("C3 下发后库里 wifi_state 立刻变 'delivered'",
          row3.get("wifi_state") == "delivered", str(row3))

    # ---------------- C4 不重复下发 + 不永久提速 ----------------
    code, sync2 = req("POST", "/api/device/sync", None, dtok)
    check("C4 ★ 再调一次 sync：wifi_change 必须为 null（同一条指令不下两次）",
          code == 200 and (sync2 or {}).get("wifi_change") is None,
          f"http={code} wifi_change={(sync2 or {}).get('wifi_change')}")
    check("C4 ★ 再调一次 sync：sync_interval_sec 必须回到 300（提速只生效一次）",
          (sync2 or {}).get("sync_interval_sec") == 300,
          str((sync2 or {}).get("sync_interval_sec")))

    # ---------------- C5 失败回报 → rolled_back + 清密码 ----------------
    code, body = req("POST", "/api/device/wifi-result",
                     {"req_id": 1, "ok": False, "ssid": SSID, "reason": "auth_fail"},
                     dtok)
    check("C5 设备回报 ok=false ⇒ 200", code == 200, f"http={code} body={body}")
    row4 = db_wifi(pk)
    check("C5 ★ 库里 wifi_state == 'rolled_back'", row4.get("wifi_state") == "rolled_back",
          str(row4))
    check("C5 ★ 库里 wifi_req_pwd 被清空（明文密码不留存）",
          row4.get("wifi_req_pwd") == "", repr(row4.get("wifi_req_pwd")))
    check("C5 库里 wifi_result_at 已写入", row4.get("wifi_result_at") is not None, str(row4))
    check("C5 wifi_req_ssid **不清**（APP 还要显示「已经切换到 xxx」）",
          row4.get("wifi_req_ssid") == SSID, repr(row4.get("wifi_req_ssid")))

    # ---------------- C6 过期 req_id 不得覆盖新请求 ----------------
    code, body = req("POST", f"/api/devices/{pk}/wifi",
                     {"ssid": "NewHome", "password": "abcdefgh"}, token=tok)
    check("C6 前置：再提交一次，req_id 递增到 2",
          code == 200 and (body or {}).get("req_id") == 2, f"http={code} body={body}")

    code, body = req("POST", "/api/device/wifi-result",
                     {"req_id": 1, "ok": True, "ssid": SSID, "reason": ""}, dtok)
    row5 = db_wifi(pk)
    check("C6 ★ 用过期 req_id（1）回报 ⇒ 200 而不是 4xx（4xx 会让固件无限重试）",
          code == 200, f"http={code} body={body}")
    check("C6 ★ 过期回报**不改状态**：仍是 pending，没被上一次的旧结果判死",
          row5.get("wifi_state") == "pending" and row5.get("wifi_req_id") == 2, str(row5))
    check("C6 ★ 过期回报也不清密码（清的是「当前请求」的，不是「回报里那个」的）",
          row5.get("wifi_req_pwd") == "abcdefgh", repr(row5.get("wifi_req_pwd")))

    # ---------------- C7 入参校验（422） ----------------
    code, _ = req("POST", f"/api/devices/{pk}/wifi", {"ssid": SSID, "password": "123"},
                  token=tok)
    check("C7 ★ 密码太短（123）⇒ 422", code == 422, f"http={code}")
    code, _ = req("POST", f"/api/devices/{pk}/wifi", {"ssid": "", "password": PWD},
                  token=tok)
    check("C7 空 SSID ⇒ 422", code == 422, f"http={code}")
    code, _ = req("POST", f"/api/devices/{pk}/wifi", {"ssid": "我" * 11, "password": PWD},
                  token=tok)
    check("C7 SSID 超过 32 **字节**（11 个汉字 = 33 字节）⇒ 422", code == 422, f"http={code}")
    row6 = db_wifi(pk)
    check("C7 三条 422 都没污染库（req_id 仍为 2、state 仍为 pending）",
          row6.get("wifi_req_id") == 2 and row6.get("wifi_state") == "pending", str(row6))

    # ---------------- C8 家属端能读到最新状态 ----------------
    code, body = req("GET", f"/api/devices/{pk}/wifi", None, tok)
    check("C8 家属端 GET /api/devices/{pk}/wifi ⇒ 200", code == 200, f"http={code}")
    check("C8 ★ GET 读到的是最新 state/req_id/ssid",
          isinstance(body, dict) and body.get("state") == "pending"
          and body.get("req_id") == 2 and body.get("ssid") == "NewHome", str(body))
    check("C8 GET 带一句给家属看的话（由服务端按 state 派生，不是设备原文）",
          isinstance(body, dict) and bool(body.get("message")), str((body or {}).get("message")))
    check("C8 GET 带 updated_at（APP 的「最后更新」）",
          isinstance(body, dict) and body.get("updated_at") is not None,
          str((body or {}).get("updated_at")))

    # ---------------- C9 成功回报 → applied ----------------
    code, body = req("POST", "/api/device/wifi-result",
                     {"req_id": 2, "ok": True, "ssid": "NewHome", "reason": ""}, dtok)
    row7 = db_wifi(pk)
    check("C9 设备回报 ok=true ⇒ 库里 wifi_state == 'applied'",
          code == 200 and row7.get("wifi_state") == "applied", f"http={code} {row7}")
    check("C9 成功后同样清空 wifi_req_pwd", row7.get("wifi_req_pwd") == "", str(row7))
    code, body = req("GET", f"/api/devices/{pk}/wifi", None, tok)
    check("C9 家属端立刻能看到 applied",
          isinstance(body, dict) and body.get("state") == "applied", str(body))

    # ---------------- C10 越权（别人的设备） ----------------
    tok2 = register(f"wifi{tag}b", "Passw0rd!23")
    code, _ = req("GET", f"/api/devices/{pk}/wifi", None, tok2)
    check("C10 别人的设备 GET wifi ⇒ 403（owner 校验走的是同一套 require_device_owner）",
          code == 403, f"http={code}")
    code, _ = req("POST", f"/api/devices/{pk}/wifi", {"ssid": "X", "password": "12345678"},
                  tok2)
    check("C10 别人的设备 POST wifi ⇒ 403", code == 403, f"http={code}")

    # ---------------- C11 终态走**心跳体**（曾经永久丢失的那条路径） ----------------
    #
    # 为什么单独一条、且是全文件最重要的一条：C5/C9 走的是
    # `POST /api/device/wifi-result`，而**固件实际走的是心跳体**
    # （firmware/components/elder/src/sync.c 把 wifi_result 拼进 /sync 的 body，
    # 拿到 200 就删掉本地结果）。2026-10-31 之前 `/sync` 从头到尾没读过
    # request body ⇒ 固件以为送达、服务端什么都没收到：state 永远停在
    # delivered、明文密码永久留库、APP 永远显示「正在切换」。
    # 只测 /wifi-result 的 C5/C9 一直是绿的 —— 正是它们让这个缺陷活了下来。
    code, body = req("POST", f"/api/devices/{pk}/wifi",
                     {"ssid": "HeartbeatHome", "password": "hbpass1234"}, token=tok)
    rid = (body or {}).get("req_id") if isinstance(body, dict) else None
    check("C11 前置：再提交一次，拿到新的 req_id", code == 200 and rid is not None,
          f"http={code} req_id={rid}")

    code, sync1 = req("POST", "/api/device/sync", None, dtok)
    wc = (sync1 or {}).get("wifi_change") if isinstance(sync1, dict) else None
    check("C11 前置：这一轮心跳把指令下发出去",
          code == 200 and isinstance(wc, dict) and wc.get("req_id") == rid,
          f"http={code} wifi_change={wc}")
    row8 = db_wifi(pk)
    check("C11 前置：state=delivered 且密码在库",
          row8.get("wifi_state") == "delivered"
          and row8.get("wifi_req_pwd") == "hbpass1234", str(row8))

    code, sync2 = req("POST", "/api/device/sync",
                      {"wifi_result": {"req_id": rid, "ok": False,
                                       "ssid": "HeartbeatHome",
                                       "reason": "auth_or_not_found"}},
                      dtok)
    check("C11 心跳体带 wifi_result 时 /sync 仍返回 200", code == 200, f"http={code}")
    row9 = db_wifi(pk)
    check("C11 ★ 心跳体里的终态必须落库（state=rolled_back）",
          row9.get("wifi_state") == "rolled_back", str(row9))
    check("C11 ★ 心跳体回报也要清空明文密码（否则它永久留库）",
          row9.get("wifi_req_pwd") == "", repr(row9.get("wifi_req_pwd")))
    check("C11 心跳体回报也写 wifi_result_at", row9.get("wifi_result_at") is not None,
          str(row9))
    # 顺序判据：先收结果、再决定下发。反过来的话这一轮会既写 rolled_back
    # 又把同一条指令再发一次，APP 那 5 秒一次的轮询能看到自相矛盾的中间态。
    check("C11 ★ 收完结果后这一轮不得再下发同一条指令",
          isinstance(sync2, dict) and (sync2 or {}).get("wifi_change") is None,
          str((sync2 or {}).get("wifi_change")))

    # 过期 req_id 走心跳体：只记日志、不改状态、不清密码。
    # 先造一个新的 pending（密码重新入库），否则"密码没被清"是空断言 ——
    # 上一步已经把它清成空串了，再断言等于断言 ""==""。
    code, body = req("POST", f"/api/devices/{pk}/wifi",
                     {"ssid": "ThirdHome", "password": "thirdpass99"}, token=tok)
    rid2 = (body or {}).get("req_id") if isinstance(body, dict) else None
    check("C11 前置：再提交一次（pending，密码重新入库）",
          code == 200 and rid2 is not None, f"http={code} rid2={rid2}")

    code, sync3 = req("POST", "/api/device/sync",
                      {"wifi_result": {"req_id": rid, "ok": True,
                                       "ssid": "HeartbeatHome", "reason": ""}},
                      dtok)
    row10 = db_wifi(pk)
    # 这一轮**同时**做了两件事：丢弃过期回报、把 ThirdHome 下发出去。
    # 所以终态是 delivered（而不是过期回报里的 applied）才算对。
    check("C11 ★ 心跳体里的过期 req_id 不得改状态（不能把还没试过的请求判死）",
          code == 200 and row10.get("wifi_state") == "delivered"
          and row10.get("wifi_req_id") == rid2, f"http={code} {row10}")
    check("C11 ★ 过期回报也不清当前请求的密码",
          row10.get("wifi_req_pwd") == "thirdpass99", repr(row10.get("wifi_req_pwd")))


def main() -> int:
    try:
        with Server():
            run()
    except SystemExit:
        raise
    print()
    print(f"==== {len(_passed)} passed, {len(_failed)} failed ====")
    for name in _failed:
        print(f"  FAILED: {name}")
    return 0 if not _failed else 1


if __name__ == "__main__":
    sys.exit(main())
