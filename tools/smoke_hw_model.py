"""`hw_model` 截断守护冒烟（SQLite 不强制列宽，这行 `[:64]` 是唯一护栏）。

用法：
    python tools/smoke_hw_model.py [--port 8133]

为什么必须有这个脚本
--------------------
``server/app/routers/device_api.py::sync()`` 里有一行::

    dev.hardware_model = hw[:64]     # 与 Device.hardware_model 列宽对齐

它守的是 **SQLite → MySQL 的一致性**：SQLite 的 ``VARCHAR(64)`` **不强制**列宽
（实测写入 200 字符照样成功），所以这行截断是「同一份代码在两种库上行为一致」
的**唯一**护栏。而它此前**没有任何断言守护** —— 谁把 ``[:64]`` 删掉，测试全绿，
直到切到 MySQL 才在生产上炸 ``Data too long for column``。

断言必须有鉴别力
----------------
本脚本断言的是**行为**（上报 200 字符 → 读回必须是前 64 字符），不是"源码里有
``[:64]``"这种静态匹配。把截断删掉，SQLite 会把 200 字符原样存下，第二条断言
立刻红 —— 这就是鉴别力的来源。

变异实证（改完请自己跑一遍，确认脚本会红）：
    把 device_api.py 的 ``hw[:64]`` 改成 ``hw[:200]``
    → ``python tools/smoke_hw_model.py`` 必须以「截断到 64 字符」失败退出；
    → 改回后必须绿。**只跑绿的那一半等于没验证**。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_common import cleanup_db, seed_admin_env, unlink_retry  # noqa: E402

# 本地请求绝不走代理（理由同 smoke_home：本机 http_proxy 会把 127.0.0.1 绕出去）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
DB_PATH = Path(os.environ.get("SMOKE_HW_MODEL_DB",
                              str(DB_DIR / f"elder_smoke_hw_model_{os.getpid()}.db")))
DB_FILE = DB_PATH if DB_PATH.is_absolute() else (SERVER / DB_PATH)

#: 列宽。与 models.Device.hardware_model 的 String(64) 一致。
COLUMN_WIDTH = 64
#: 上报长度：必须远大于列宽，且**不等于**列宽（否则截断与不截断无法区分）。
LONG_MODEL = "ESP32-S3-" + "X" * 190

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def req(method: str, path: str, body: dict | None = None, token: str | None = None,
        headers: dict | None = None):
    url = BASE + path
    data = None
    hdr = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
    if token:
        hdr["Authorization"] = f"Bearer {token}"
    for k, v in (headers or {}).items():
        hdr[k] = v
    r = urllib.request.Request(url, data=data, headers=hdr, method=method)
    try:
        with _OPENER.open(r, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def wait_ready(timeout: float = 120.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, _ = req("GET", "/api/health")
            if code == 200:
                return True
        except Exception:       # noqa: BLE001  服务还没起来
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("SMOKE_HW_MODEL_PORT", "8133")))
    args = ap.parse_args()
    BASE = f"http://127.0.0.1:{args.port}"

    unlink_retry(DB_FILE, quiet=True)
    env = seed_admin_env(dict(os.environ))
    env.update({
        "ELDER_DB": f"sqlite:///{DB_PATH}",
        "ELDER_JWT_SECRET": "smoke-hw-model-secret",
        "ELDER_ALLOW_PUBLIC_REGISTER": "1",
        "ELDER_PORT": str(args.port),
        "PYTHONUNBUFFERED": "1",
    })
    log_path = Path(tempfile.gettempdir()) / f"smoke_hw_model_{os.getpid()}.log"
    log_fp = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.port)],
        cwd=str(SERVER), env=env, stdout=log_fp, stderr=subprocess.STDOUT,
    )

    try:
        if not wait_ready():
            print("服务未就绪，日志尾部：")
            print(log_path.read_text(encoding="utf-8", errors="replace")[-2000:])
            return 1

        print("── 1. 准备账号与已绑定设备 ──")
        code, _ = req("POST", "/api/auth/register",
                      {"username": "hwm", "password": "Passw0rd!x", "email": "hwm@example.com"})
        check("注册账号", code in (200, 201), f"HTTP {code}")
        code, body = req("POST", "/api/auth/login", {"username": "hwm", "password": "Passw0rd!x"})
        tok = (body or {}).get("access_token") or (body or {}).get("token") or ""
        check("登录拿到 token", code == 200 and bool(tok), f"HTTP {code}")

        code, dev = req("POST", "/api/devices", {"device_id": "aabbccddeeff", "name": "截断测试机"}, tok)
        check("登记设备", code == 201, f"HTTP {code}")
        dev_pk = (dev or {}).get("id")

        code, cd = req("GET", "/api/voice/ota", None, None, {"Device-Id": "aabbccddeeff"})
        ws = (cd or {}).get("websocket") or {} if isinstance(cd, dict) else {}
        dev_token = ws.get("token", "")
        # 建档即归属（D44 §3）⇒ 设备已是已绑定态，/code 会 409；凭证走 OTA。
        check("OTA 下发 device_token（建档即归属，不再走激活码绑定）",
              code == 200 and bool(dev_token),
              f"HTTP {code} keys={list(cd) if isinstance(cd, dict) else cd}")

        print("── 2. 上报超长 hw_model（核心断言）──")
        check(f"上报长度 {len(LONG_MODEL)} 远大于列宽 {COLUMN_WIDTH}",
              len(LONG_MODEL) > COLUMN_WIDTH * 2, str(len(LONG_MODEL)))
        code, sync = req("POST", f"/api/device/sync?hw_model={urllib.parse.quote(LONG_MODEL)}",
                         {}, dev_token)
        check("心跳接受超长 hw_model（不该 4xx/5xx）", code == 200, f"HTTP {code}")

        code, devs = req("GET", "/api/devices", token=tok)
        me = next((d for d in (devs or []) if d.get("id") == dev_pk), {})
        got = me.get("hardware_model", "")

        # ① 必须被截断到列宽 —— 删掉 [:64] 时 SQLite 会原样存 200 字符，这里红
        check(f"落库后被截断到 {COLUMN_WIDTH} 字符",
              len(got) == COLUMN_WIDTH, f"实际长度 {len(got)}")
        # ② 截断必须是「前缀」而不是随便截一段
        check("截断结果是原串前 64 字符（前缀语义）",
              got == LONG_MODEL[:COLUMN_WIDTH],
              f"got[:20]={got[:20]!r}")
        # ③ 反面：绝不能把超长原串整段存下来
        check("没有把超长原串整段存下来",
              got != LONG_MODEL, f"len={len(got)}")

        print("── 3. 正常值不被误伤 ──")
        code, _ = req("POST", "/api/device/sync?hw_model=ESP32-S3", {}, dev_token)
        code, devs = req("GET", "/api/devices", token=tok)
        me = next((d for d in (devs or []) if d.get("id") == dev_pk), {})
        check("短型号原样保存", me.get("hardware_model") == "ESP32-S3",
              str(me.get("hardware_model")))
    finally:
        cleanup_db(DB_FILE, procs=(proc,), handles=(log_fp,))
        unlink_retry(log_path, quiet=True)

    if _failures:
        print(f"\n❌ {len(_failures)} 条断言失败：" + ", ".join(_failures))
        return 1
    print("\n✅ 全部断言通过（hw_model 截断护栏有效）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
