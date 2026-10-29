"""用户端音色下拉（按**生效厂商**给清单 + 失效音色回落默认）冒烟。

用法：
    python tools/smoke_voice_options.py [--port 8136]

**这个脚本守的是什么**
--------------------
生产 ``ai_providers`` 里 ``kind='tts'`` 有两行：``tencent``（priority 6，
``tts_voice='601009'``）与 ``edge``（priority 10）。``llm_gateway.resolve_row``
按 priority 升序取第一条 ⇒ **生效的是腾讯云**。

而角色表里存的却是 ``zh-CN-XiaoxiaoNeural``（Edge 的音色名）。
``asr_tts._shape_ok_voice()`` 发现形态与 tencent 不符时会**静默置空** ——
角色选的音色从来没生效过，用户完全看不出来。这就是要修的东西：

  1. 用户端的音色下拉必须只列**生效厂商**的音色（选了就能生效）；
  2. 存的旧值若不在该厂商清单里，写库时回落到该厂商默认音色（并留日志）。

**为什么不断言真调腾讯云 / Edge**
--------------------------------
本脚本只测 catalog 与归一化这两层。真打厂商要密钥、要花钱，且失败原因
（网络 / 限流 / 欠费）与本仓库代码无关 —— 那种断言会让 CI 红得毫无信息量。

覆盖：
  A. 空库（一条 tts 配置都没有）按 Edge 处理
  B. 复刻生产两行配置 ⇒ 生效厂商是 tencent，清单是纯数字 ID
  C. 角色写路径归一化：create / patch / restore 三处
  D. 自建厂商（认不出）时放行任意音色名
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_common import cleanup_db, seed_admin_env, unlink_retry  # noqa: E402
from smoke_ports import guard_or_die, resolve_port  # noqa: E402

# ⚠️ 本地请求绝不走系统代理（同其余冒烟脚本：本机设了 http_proxy，
# 会把「访问自己刚起的 127.0.0.1」绕到代理，表现为超时且零断言失败 —— 极像回归）。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 库文件名带 PID：并行跑时独占一个库，避免互删。
DB_PATH = Path(os.environ.get(
    "SMOKE_VOICE_OPTIONS_DB",
    str(Path(tempfile.gettempdir()) / f"elder_smoke_voptions_{os.getpid()}.db")))
if DB_PATH.exists():
    unlink_retry(DB_PATH)

PORT = resolve_port("smoke_voice_options")
BASE = f"http://127.0.0.1:{PORT}"

# 子进程输出**落文件**，绝不接无人消费的 PIPE（Windows 4KB 管道缓冲会锁死服务端）。
SERVER_LOG = DB_PATH.with_name(DB_PATH.stem + "_server.log")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def req(method: str, path: str, body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with _LOCAL_OPENER.open(r, timeout=40) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def wait_ready(proc, timeout: float = 120.0) -> bool:
    """等服务就绪，**同时看自己起的进程是否还活着**。

    ``/api/health`` 按设计永远 200，端口被别人占时会探到**别人的服务** ⇒
    误判就绪 ⇒ 全部断言打到别人身上（假绿，比超时危险）。所以必须先查
    ``proc.poll()``：uvicorn bind 失败会退出。
    """
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            if req("GET", "/api/health")[0] == 200:
                return True
        except Exception:
            pass
        time.sleep(0.6)
    return False


def read_log_tail(max_bytes: int = 8192) -> str:
    try:
        return SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-max_bytes:]
    except OSError:
        return ""


def _ids(opts: dict) -> list[str]:
    return [str(v.get("id") or "") for v in (opts.get("voices") or [])]


def main() -> int:
    env = seed_admin_env(dict(os.environ))
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
    env["ELDER_JWT_SECRET"] = "smoke-voice-options-secret"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"
    env["PYTHONIOENCODING"] = "utf-8"

    print(f"[boot] starting server on :{PORT} (db: {DB_PATH})")
    guard_or_die(PORT, "smoke_voice_options.py")

    log_fp = open(SERVER_LOG, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(SERVER), env=env, stdout=log_fp, stderr=subprocess.STDOUT)

    try:
        if not wait_ready(proc):
            print("[fatal] 服务未就绪，日志尾部：\n" + read_log_tail())
            return 1

        code, tk = req("POST", "/api/auth/login",
                       {"username": "admin", "password": "admin123"})
        admin = tk.get("access_token", "") if isinstance(tk, dict) else ""
        if not admin:
            print(f"[fatal] 管理员登录失败：code={code} {tk}")
            return 1
        req("POST", "/api/auth/register",
            {"username": "vopt", "password": "test123456", "nickname": "vopt"})
        _, t = req("POST", "/api/auth/login",
                   {"username": "vopt", "password": "test123456"})
        user = t.get("access_token", "") if isinstance(t, dict) else ""
        if not user:
            print("[fatal] 普通用户登录失败")
            return 1

        # ================================================ A. 空库按 Edge 处理
        print("\n── A. 空库（一条 tts 配置都没有）──")
        code, o0 = req("GET", "/api/providers/voice-options", token=user)
        check("A1 端点 200 且普通用户可访问（不是 admin-only）",
              code == 200 and isinstance(o0, dict), f"code={code}")
        o0 = o0 if isinstance(o0, dict) else {}
        check("A2 零配置时按 edge 处理（不给空清单：新部署至少能选 Edge）",
              o0.get("vendor") == "edge", f"vendor={o0.get('vendor')!r}")
        check("A3 零配置时默认音色是出厂默认且在清单内",
              o0.get("default_voice") == "zh-CN-XiaoxiaoNeural"
              and o0.get("default_voice") in _ids(o0),
              f"default={o0.get('default_voice')!r}")
        check("A4 认得出的厂商 allow_custom=False（不允许手填会静默失效的名字）",
              o0.get("allow_custom") is False, f"{o0.get('allow_custom')!r}")

        # ============================== B. 复刻生产：tencent(6) + edge(10)
        print("\n── B. 复刻生产两行配置 ⇒ 生效厂商 tencent ──")
        code, t_row = req("POST", "/api/providers", {
            "name": "腾讯云 TTS", "kind": "tts", "provider": "tencent",
            "tts_voice": "601009", "api_key": "AKIDsmoke:SKsmoke",
            "priority": 6, "enabled": True,
        }, admin)
        t_id = (t_row or {}).get("id") if isinstance(t_row, dict) else None
        check("B1 建腾讯云 tts 配置（priority 6）", code == 201 and bool(t_id),
              f"code={code} {str(t_row)[:120]}")
        code, e_row = req("POST", "/api/providers", {
            "name": "Edge TTS", "kind": "tts", "provider": "edge",
            "tts_voice": "", "priority": 10, "enabled": True,
        }, admin)
        e_id = (e_row or {}).get("id") if isinstance(e_row, dict) else None
        check("B2 建 Edge tts 配置（priority 10，兜底）", code == 201 and bool(e_id),
              f"code={code} {str(e_row)[:120]}")

        code, opts = req("GET", "/api/providers/voice-options", token=user)
        opts = opts if isinstance(opts, dict) else {}
        check("B3 生效厂商按 priority 判定为 tencent（不是清单里的第一家 Edge）",
              code == 200 and opts.get("vendor") == "tencent",
              f"code={code} vendor={opts.get('vendor')!r}")
        vids = _ids(opts)
        check("B4 清单 id 全是纯数字（腾讯云音色形态；混入 Edge 名会让整句没声音）",
              bool(vids) and all(i.isdigit() for i in vids), f"ids={vids[:6]}")
        check("B5 清单包含生产用的 601009", "601009" in vids, f"ids={vids}")
        check("B6 配置行自己填的 601009 就是默认音色（管理员显式指定优先）",
              opts.get("default_voice") == "601009",
              f"default={opts.get('default_voice')!r}")
        check("B7 default_voice 落在清单的 id 集合里",
              opts.get("default_voice") in set(vids),
              f"default={opts.get('default_voice')!r} ids={vids[:6]}")
        check("B8 allow_custom=False（生效厂商认得出，不允许跨厂商手填）",
              opts.get("allow_custom") is False, f"{opts.get('allow_custom')!r}")
        check("B9 vendor_label 是给人看的厂商名（含「腾讯」）",
              "腾讯" in str(opts.get("vendor_label") or ""),
              f"{opts.get('vendor_label')!r}")
        check("B10 custom_hint 非空（认得出厂商时也要说清为什么不能手填）",
              bool(str(opts.get("custom_hint") or "").strip()), "")

        default_voice = str(opts.get("default_voice") or "")

        # ============================ C. 角色写路径归一化（三处）
        print("\n── C. 角色写路径：失效音色回落默认 ──")
        code, r1 = req("POST", "/api/roles",
                       {"name": "音色回落测试", "tts_voice": "zh-CN-XiaoxiaoNeural"},
                       user)
        r1 = r1 if isinstance(r1, dict) else {}
        check("C1 提交 Edge 音色 → 落库变成该厂商默认（不再静默失效）",
              code == 201 and r1.get("tts_voice") == default_voice,
              f"code={code} got={r1.get('tts_voice')!r} 期望={default_voice!r}")
        role_id = r1.get("id")

        code, r2 = req("POST", "/api/roles", {"name": "空音色", "tts_voice": ""}, user)
        r2 = r2 if isinstance(r2, dict) else {}
        check("C2 提交空串 → 仍是空串（空 = 继承形象/配置行，语义不能被破坏）",
              code == 201 and r2.get("tts_voice") == "", f"got={r2.get('tts_voice')!r}")

        code, r3 = req("POST", "/api/roles", {"name": "合法音色", "tts_voice": "601008"}, user)
        r3 = r3 if isinstance(r3, dict) else {}
        check("C3 提交清单内的合法音色 → 原样保留（不能被无差别改写）",
              code == 201 and r3.get("tts_voice") == "601008",
              f"got={r3.get('tts_voice')!r}")

        if role_id:
            code, r4 = req("PATCH", f"/api/roles/{role_id}",
                           {"name": "音色回落测试", "tts_voice": "zh-CN-YunyangNeural"},
                           user)
            r4 = r4 if isinstance(r4, dict) else {}
            check("C4 PATCH 路径同样回落（改角色时也走归一化）",
                  code == 200 and r4.get("tts_voice") == default_voice,
                  f"code={code} got={r4.get('tts_voice')!r}")
        else:
            check("C4 PATCH 路径同样回落（改角色时也走归一化）", False, "拿不到角色 id")

        # restore：派生自预设（预设的 tts_voice 是 Edge 的 Xiaoxiao），
        # 派生会原样复制母版音色，还原时若不归一化就又把失效值塞回去。
        code, presets = req("GET", "/api/roles/presets", token=user)
        pid = None
        if code == 200 and isinstance(presets, list) and presets:
            pid = (presets[0] or {}).get("id")
        if pid:
            code_d, d = req("POST", f"/api/roles/{pid}/derive",
                            {"name": "派生还原测试", "inherit": False}, user)
            d = d if isinstance(d, dict) else {}
            code_r, r5 = req("POST", f"/api/roles/{d.get('id')}/restore",
                             {"confirm": True}, user)
            r5 = r5 if isinstance(r5, dict) else {}
            check("C5 restore 也归一化（母版音色属别家厂商时不照抄）",
                  code_d == 201 and code_r == 200 and r5.get("tts_voice") == default_voice,
                  f"derive={code_d} restore={code_r} got={r5.get('tts_voice')!r}")
        else:
            check("C5 restore 也归一化（母版音色属别家厂商时不照抄）", False,
                  f"拿不到预设 id: code={code}")

        # ==================================== D. 自建厂商：放行任意音色名
        print("\n── D. 自建 / 兼容网关（认不出厂商）──")
        for rid in (t_id, e_id):
            if rid:
                req("DELETE", f"/api/providers/{rid}", token=admin)
        code, own = req("POST", "/api/providers", {
            "name": "自建 TTS", "kind": "tts", "provider": "my_tts",
            "api_style": "openai_compatible",
            "endpoint": "http://127.0.0.1:9/v1", "api_key": "sk-smoke",
            "priority": 1, "enabled": True,
        }, admin)
        check("D1 建自建 tts 配置（provider 认不出 + openai_compatible）",
              code == 201, f"code={code} {str(own)[:120]}")

        code, o2 = req("GET", "/api/providers/voice-options", token=user)
        o2 = o2 if isinstance(o2, dict) else {}
        check("D2 认不出厂商时 vendor 为空串、voices 为空",
              code == 200 and o2.get("vendor") == "" and o2.get("voices") == [],
              f"vendor={o2.get('vendor')!r} n={len(o2.get('voices') or [])}")
        check("D3 认不出厂商时 allow_custom=True（否则自建 TTS 的音色彻底填不进来）",
              o2.get("allow_custom") is True, f"{o2.get('allow_custom')!r}")
        check("D4 认不出厂商时 default_voice 为空串（没有清单就没有默认音色）",
              o2.get("default_voice") == "", f"{o2.get('default_voice')!r}")

        code, r6 = req("POST", "/api/roles",
                       {"name": "自建音色", "tts_voice": "cosyvoice-zh-female"},
                       user)
        r6 = r6 if isinstance(r6, dict) else {}
        check("D5 自建厂商的自定义音色原样保留（不被当成非法值改掉）",
              code == 201 and r6.get("tts_voice") == "cosyvoice-zh-female",
              f"got={r6.get('tts_voice')!r}")

    finally:
        cleanup_db(DB_PATH, procs=(proc,), handles=(log_fp,))
        if any(not ok for _, ok, _ in results):
            print("\n[diag] ---- server log tail ----\n" + read_log_tail())

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n==== {passed}/{total} passed ====")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
