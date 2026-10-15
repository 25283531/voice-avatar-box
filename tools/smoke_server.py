"""后端冒烟测试：本地起服务（SQLite），跑通业务闭环。

用法：
    python tools/smoke_server.py [--port 8099]

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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable

# 跨平台默认落点（原来写死 D:\espdev 在 Linux CI 上会直接失败）
DB_PATH = Path(os.environ.get("SMOKE_DB", str(Path(tempfile.gettempdir()) / "elder_smoke.db")))
if DB_PATH.exists():
    DB_PATH.unlink()

PORT = int(next((a for a in sys.argv[1:] if a.isdigit()), 8099))
BASE = f"http://127.0.0.1:{PORT}"

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

    print(f"[boot] starting server on :{PORT} (db: {db_url.split('@')[-1]})")
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

        # ---------------------------------------------------- 1. 健康检查
        code, body = req("GET", "/api/health")
        check("健康检查", code == 200 and body.get("ok") is True, str(body))

        # ---------------------------------------------------- 2. 登录（种子管理员）
        code, tk = req("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
        check("登录默认管理员", code == 200 and bool(tk.get("access_token")), str(code))
        token = tk.get("access_token", "")

        # ---------------------------------------------------- 3. 建设备
        code, dev = req("POST", "/api/devices",
                        {"device_id": "aabbccddeeff", "name": "客厅陪伴助手"}, token)
        check("登记设备", code == 201 and dev.get("device_id") == "aabbccddeeff", str(code))
        dev_pk = dev.get("id", 0)

        # ---------------------------------------------------- 4. 激活码
        code, cd = req("POST", f"/api/devices/{dev_pk}/code", {}, token)
        ok_code = code == 200 and len(cd.get("code", "")) == 13
        check("生成 13 位激活码", ok_code, cd.get("display", ""))
        full = cd.get("code", "")

        # 与固件同算法校验 Luhn
        def luhn11(d: str) -> int:
            s = 0
            for i in range(11):
                v = int(d[i])
                if (11 - i) % 2 == 0:
                    v *= 2
                    if v > 9:
                        v -= 9
                s += v
            return (10 - s % 10) % 10

        check("Luhn 校验位一致（对齐固件 C 实现）",
              len(full) == 13 and int(full[12]) == luhn11(full[1:12]),
              f"expect={luhn11(full[1:12]) if len(full) == 13 else '?'} actual={full[12:]}")
        check("显示格式分四段",
              cd.get("display", "").count("-") == 3 and len(cd.get("display", "")) == 16,
              cd.get("display", ""))

        # ---------------------------------------------------- 5. 错误码应当被拒
        bad = full[:-1] + str((int(full[-1]) + 1) % 10)
        code, _ = req("POST", "/api/devices/bind", {"code": bad}, token)
        check("错误激活码被拒绝", code == 400, f"code={code}")

        # ---------------------------------------------------- 6. 绑定
        code, bind = req("POST", "/api/devices/bind", {"code": full}, token)
        check("激活码绑定设备", code == 200 and bool(bind.get("device_token")), str(code))
        dev_token = bind.get("device_token", "")

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
        code, sync = req("POST", "/api/device/sync?fw_version=0.1.0", {}, dev_token)
        ok_sync = (code == 200 and len(sync.get("reminders", [])) >= 1
                   and len(sync.get("upcoming", [])) >= 1
                   and bool(sync.get("server_time")))
        check("设备心跳同步（规则+排期+形象+AI配置）", ok_sync,
              f"reminders={len(sync.get('reminders', []))} upcoming={len(sync.get('upcoming', []))}")

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

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n==== {passed}/{total} passed ====")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
