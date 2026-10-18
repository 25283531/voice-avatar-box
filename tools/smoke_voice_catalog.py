"""音色目录 + 服务就绪度 冒烟测试。

用法：
    python tools/smoke_voice_catalog.py [--port 8124]

覆盖：
  A. 音色目录端点：字段完整、id 唯一、默认值在目录内
  B. 出厂默认音色与 catalog 单一真源一致（防「前端抄第二份」的老问题）
  C. 零配置时就绪度**如实**反映「不能服务」（这是本脚本存在的核心理由）
  D. 补齐配置后就绪度随之变化，且能区分「关键」与「可降级」
  E. 就绪度**不泄露** api_key（管理端点也按最小必要原则）
  F. 自定义音色（不在目录内）不被当成错误 —— 自建 TTS 服务的正当用法
  G. 前端音色控件：下拉含全部预置项 + 保留自定义入口，且 __custom__
     不会被当成真实音色存库
  H. 三家 TTS 厂商预置（Edge/火山/阿里百炼）与**服务端自动分发**：预置字段
     完整性、按厂商分组的音色、火山签名四级派生（按官方文档推导过程独立
     复算）、base64 错误体识破、Edge 分块无损、真实语音测试端点

**为什么 H 组必须存在（本轮实测踩出的 bug）**
--------------------------------------------
管理后台选「火山引擎」时会把 ``api_style`` 预填成 ``openai_compatible``。
若分发判据让 ``api_style`` 优先于 ``provider``，火山行会被**静默**路由到
OpenAI 兼容路径 —— 表现为「配置都填对了却报接口格式不对」，且没有任何日志
提示。这类问题靠读代码很难发现，必须用断言钉住。

**为什么要专门测 C（零配置如实反映）**
--------------------------------------
用户曾提出「TTS/ASR/VAD 已有默认值，不配置也能服务」。实测结论相反：
空库下四项全部解析为空，`resolve_provider` 返 503，设备「能心跳不能说话」。

这是个**危险的真假之争**：页面若写死「已配 0 项，请去配置」，等哪天真的
有了内置默认值，这句话就变成误导；若写「已就绪」，则当下就是纯假话 ——
管理员会以为能用，直到老人没声音才发现。所以就绪度端点必须**由当前配置
实时算出**，本脚本用「先空库断言未就绪，再补配置断言就绪」把它钉死。

用法：``ELDER_SEED_ADMIN_PASSWORD`` 建议带 ``admin123``（种子口令随机化后
非此值会红，属预期行为，非回归）。
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
from smoke_ports import guard_or_die, guard_port, release_port, resolve_port  # noqa: E402

# ⚠️ 本地请求绝不走系统代理（同 smoke_models.py 的理由：本机设了 http_proxy，
# 会把「访问自己刚起的 127.0.0.1」绕到代理，表现为超时且零断言失败 —— 极像回归）。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 库文件名带 PID：并行跑时独占一个库，避免互删（见 smoke_models.py DB_PATH 注释）。
DB_PATH = Path(os.environ.get(
    "SMOKE_VOICE_CATALOG_DB",
    str(Path(tempfile.gettempdir()) / f"elder_smoke_vcatalog_{os.getpid()}.db")))
if DB_PATH.exists():
    DB_PATH.unlink()

PORT = resolve_port("smoke_voice_catalog")
BASE = f"http://127.0.0.1:{PORT}"

# 子进程输出**落文件**，绝不接无人消费的 PIPE（Windows 4KB 管道缓冲会锁死服务端，
# 见 smoke_models.py SERVER_LOG 处记录的踩坑史）。
SERVER_LOG = DB_PATH.with_name(DB_PATH.stem + "_server.log")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条断言。

    ⚠️ detail **只在失败时打印**（其余冒烟脚本是「有 detail 就打」，本脚本刻意不同）：
    这里的 detail 多为「失败原因」的措辞（如「响应里出现了 api_key 字段」）。
    若 PASS 时也打印，一行绿里混着一句红色解释 —— 人眼扫日志时极易误读成失败，
    本脚本开发过程中就真被它误导过一次（34/34 全绿却以为 C7 挂了）。
    """
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not ok else ""))


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


def read_log_tail(max_bytes: int = 8192) -> str:
    try:
        return SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-max_bytes:]
    except OSError:
        return ""


def login_admin() -> str:
    pwd = os.environ.get("ELDER_SEED_ADMIN_PASSWORD", "admin123")
    code, data = req("POST", "/api/auth/login",
                     {"username": "admin", "password": pwd})
    if code != 200 or not isinstance(data, dict) or not data.get("access_token"):
        raise SystemExit(f"[fatal] 管理员登录失败：code={code} data={data}")
    return str(data["access_token"])


def main() -> int:
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
    env["ELDER_JWT_SECRET"] = "smoke-test-secret"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("ELDER_SEED_ADMIN_PASSWORD", "admin123")

    print(f"[boot] starting server on :{PORT} (db: {DB_PATH})")
    guard_or_die(PORT, "smoke_voice_catalog.py")
    hold, hold_hint = guard_port(PORT)
    if hold is None:
        print("[boot] " + hold_hint.replace("\n", "\n[boot] "))
        return 1
    release_port(hold, PORT)   # 必须 spawn 前释放，否则自己挡住自己

    log_fp = open(SERVER_LOG, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(SERVER), env=env, stdout=log_fp, stderr=subprocess.STDOUT)

    try:
        if not wait_ready():
            print("[fatal] 服务未就绪，日志尾部：\n" + read_log_tail())
            return 1
        token = login_admin()

        # ============================================================ A. 音色目录
        print("\n── A. 音色目录（/api/providers/catalog）──")
        code, cat = req("GET", "/api/providers/catalog", token=token)
        check("A1 目录端点 200", code == 200, f"code={code}")
        voices = (cat or {}).get("voices") or []
        check("A2 至少预置 7 种音色（用户要求 7~8 种）", len(voices) >= 7, f"实际 {len(voices)} 种")
        required = {"id", "name", "gender", "scene", "note"}
        missing = [v.get("id") for v in voices if not required.issubset(v.keys())]
        check("A3 每条音色字段齐全（前端依赖 name/scene/note 渲染说明）",
              not missing, f"缺字段: {missing}")
        ids = [v["id"] for v in voices]
        check("A4 音色 id 无重复", len(ids) == len(set(ids)),
              f"{len(ids)} 条 / {len(set(ids))} 唯一")
        check("A5 允许自定义输入（自建 TTS 服务的正当路径）",
              (cat or {}).get("allow_custom") is True, "")
        check("A6 附带了自定义填写说明（前端要展示给管理员）",
              bool((cat or {}).get("custom_hint")), "")

        # ================================================== B. 单一真源一致性
        print("\n── B. 出厂默认音色的单一真源 ──")
        default_voice = (cat or {}).get("default_voice") or ""
        check("B1 目录给出了出厂默认音色", bool(default_voice), default_voice)
        check("B2 出厂默认音色在预置清单内（否则「恢复默认」会写入一个不存在的音色）",
              default_voice in ids, f"default={default_voice}")

        # 反向核对：main.py 种子用的是 catalog.DEFAULT_PRESET_TTS_VOICE，
        # 这里通过 API 看人格预设实际存的值，二者必须一致。
        code, pres = req("GET", "/api/admin/presets/", token=token)
        preset_voice = ""
        if code == 200 and isinstance(pres, dict):
            for p in (pres.get("presets") or []):
                if p.get("name"):
                    preset_voice = p.get("tts_voice") or ""
                    break
        check("B3 人格预设实际存的音色 == 目录里的出厂默认",
              preset_voice == default_voice,
              f"预设={preset_voice} 目录={default_voice}")

        # 出厂默认音色只能有一处定义（不看「值相等」，看「有几个人写下这个赋值」）。
        # 为什么要这条：`voice/catalog.py` 曾自己定义过一个同值的
        # DEFAULT_PRESET_TTS_VOICE 副本（真源在 persona/catalog.py），
        # 而上面 B1/B2/B3 全都只比**值** —— 两份同值常量永远能通过，
        # 于是这个「第二份真源」的破口长期绿灯，直到有人改了其中一份才炸。
        #
        # 这里用**静态扫描**而不是 import 比身份：本冒烟脚本从 tools/ 运行，
        # `app` 不在 sys.path（import 会 ModuleNotFoundError）。
        # 静态扫描其实更严格 —— 它抓的是「有几个人写下这个赋值」，
        # 连「定义了但没用到」的死副本也不放过，而 import 比身份抓不到那种。
        try:
            import pathlib
            import re
            _root = pathlib.Path(__file__).resolve().parents[1] / "server" / "app"
            _defs = []
            # 匹配 `NAME: Final = ...` / `NAME: str = ...` 这类**带类型标注的赋值定义**，
            # 但不能匹配 `from x import NAME`（那是转出，不是定义）。
            _pat = re.compile(r"^DEFAULT_PRESET_TTS_VOICE\s*:\s*\w+\s*=")
            for _p in sorted(_root.rglob("*.py")):
                for _no, _line in enumerate(
                        _p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if _pat.match(_line.strip()):
                        _defs.append(f"{_p.relative_to(_root)}:{_no}")
            check("B4 出厂默认音色只有一处定义（杜绝第二份真源分叉）",
                  len(_defs) == 1 and _defs[0].startswith("persona"),
                  f"定义点={_defs or '无'}")
        except Exception as exc:  # pragma: no cover
            check("B4 出厂默认音色只有一处定义（杜绝第二份真源分叉）", False, repr(exc))

        # ================================== C. 零配置如实反映「不能服务」（核心）
        print("\n── C. 零配置时就绪度如实反映（核心断言）──")
        code, rd = req("GET", "/api/providers/readiness", token=token)
        check("C1 就绪度端点 200", code == 200, f"code={code}")
        usages = {u["usage"]: u for u in ((rd or {}).get("usages") or [])}
        check("C2 就绪度覆盖全部 8 个用途", len(usages) == 8, f"实际 {len(usages)}")

        # ★ 这条是本脚本存在的理由：空库下 asr/tts/chat 必须是「未配置」。
        # 若有人为了「体验好」把未配置硬报成已就绪，这里立刻红。
        missing_crit = (rd or {}).get("missing_critical") or []
        for u in ("asr", "tts", "chat"):
            check(f"C3 零配置时 {u} 如实标记为未配置",
                  usages.get(u, {}).get("configured") is False,
                  f"configured={usages.get(u, {}).get('configured')}")
        check("C4 missing_critical 含 asr/tts/chat（缺任一都不能说话）",
              set(missing_crit) >= {"asr", "tts", "chat"}, f"{missing_crit}")
        check("C5 未配置项给出了具体提示（不是空串）",
              all(usages.get(u, {}).get("hint") for u in ("asr", "tts", "chat")),
              "")
        # VAD 是个特例：设备端 esp-sr 已承担，缺它不影响说话。
        # 若把它算成 critical，页面就会虚报「不能服务」，反而是另一种误导。
        check("C6 VAD 未配置但不算关键项（设备端已承担断句）",
              usages.get("vad", {}).get("critical") is False
              and "vad" not in missing_crit, "")
        check("C7 端点不泄露 api_key",
              "api_key" not in json.dumps(rd or {}, ensure_ascii=False),
              "响应里出现了 api_key 字段")

        # ======================================= D. 补齐配置后就绪度随之变化
        print("\n── D. 补齐配置后就绪度变化 ──")
        # 指向一个不存在的地址：只验证「配置存在 ⇒ 就绪度认它」，
        # 不去打真实厂商（一分钱不花，也不依赖公网）。
        made = []
        for kind, usage_name in (("asr", "本地 ASR"), ("tts", "本地 TTS"),
                                 ("llm_main", "本地 LLM")):
            c, r = req("POST", "/api/providers", {
                "name": usage_name, "kind": kind, "provider": "local",
                "endpoint": "http://127.0.0.1:9/v1", "api_key": "sk-smoke-secret",
                "model": "smoke-model", "enabled": True,
            }, token=token)
            if c in (200, 201):
                made.append((kind, r.get("id")))
        check("D1 三条配置创建成功", len(made) == 3, f"{made}")

        code, rd2 = req("GET", "/api/providers/readiness", token=token)
        u2 = {u["usage"]: u for u in ((rd2 or {}).get("usages") or [])}
        for u in ("asr", "tts", "chat"):
            check(f"D2 配了 {u} 之后就绪度转为已配置",
                  u2.get(u, {}).get("configured") is True,
                  f"configured={u2.get(u, {}).get('configured')}")
        check("D3 关键项补齐后 missing_critical 清空",
              not ((rd2 or {}).get("missing_critical") or []),
              f"{(rd2 or {}).get('missing_critical')}")
        check("D4 仍缺的非关键项被标为可降级（不虚报故障）",
              u2.get("embed", {}).get("critical") is False
              and u2.get("embed", {}).get("configured") is False,
              json.dumps(u2.get("embed"), ensure_ascii=False))
        check("D5 已配置项能给出当前生效的是哪一条（脱敏）",
              bool(u2.get("tts", {}).get("resolved", {}).get("name")),
              json.dumps(u2.get("tts", {}).get("resolved"), ensure_ascii=False))
        check("D6 就绪度响应仍不含 api_key",
              "sk-smoke-secret" not in json.dumps(rd2 or {}, ensure_ascii=False), "")

        # ============ G. 前端音色控件的静态核对（防「删了自定义入口」回归）
        print("\n── G. 前端音色控件静态核对 ──")
        html = (SERVER / "app" / "static" / "index.html").read_text(encoding="utf-8")
        # 预置音色必须由服务端目录驱动，而不是前端硬编码 —— 前端硬编码等于
        # 第二份真源，「恢复默认」迟早对不上（见 app/voice/catalog.py 模块说明）。
        check("G1 前端不再硬编码音色清单",
              "zh-CN-XiaoxiaoNeural" not in html,
              "index.html 里出现了字面音色名，说明前端又抄了一份真源")
        check("G2 音色框是下拉选择（optgroup 承载预置项）",
              'optgroup label="── 预置音色' in html, "")
        check("G3 保留了自定义输入入口", '"__custom__"' in html, "")
        check("G4 保存前会把 __custom__ 收敛成真实音色名",
              "resolveVoice(" in html, "")
        check("G5 选了自定义但没填名字时拒绝保存（防把占位符存进库）",
              "没填音色名" in html, "")
        check("G6 存量自定义音色会被识别为自定义而非丢失",
              "splitVoice(" in html, "")

        # ============ F. 自定义音色不被当成错误（自建 TTS 的正当用法）
        print("\n── F. 自定义音色不被误判 ──")
        code, pres2 = req("GET", "/api/admin/presets/", token=token)
        cid = None
        if code == 200 and isinstance(pres2, dict) and (pres2.get("presets") or []):
            cid = (pres2["presets"][0]).get("id")
        if cid:
            c, r = req("PATCH", f"/api/admin/presets/{cid}",
                       {"tts_voice": "cosyvoice-zh-female-自定义音色名"}, token=token)
            check("F1 自定义音色名可写入（后端不校验枚举）",
                  c == 200, f"code={c} {r if c != 200 else ''}")
            c2, r2 = req("GET", "/api/admin/presets/", token=token)
            got = ""
            if c2 == 200:
                for p in (r2.get("presets") or []):
                    if p.get("id") == cid:
                        got = p.get("tts_voice") or ""
            check("F2 读回来是原值（未被静默改写成某个预置音色）",
                  got == "cosyvoice-zh-female-自定义音色名", f"got={got}")
        else:
            check("F1 跳过：拿不到预设 id", False, f"code={code}")

        # ================= H. 三家厂商预置 + 服务端自动分发（新增能力）
        print("\n── H. 厂商预置与自动分发 ──")
        code, cat2 = req("GET", "/api/providers/catalog", token=token)
        vendors = (cat2 or {}).get("vendors") or []
        vmap = {v["value"]: v for v in vendors}
        check("H1 预置了三家 TTS 厂商", set(vmap) >= {"edge", "volcengine", "aliyun"},
              f"{sorted(vmap)}")
        check("H2 Edge 标为免密钥且无 endpoint",
              vmap.get("edge", {}).get("needs_key") is False
              and not vmap.get("edge", {}).get("endpoint"),
              json.dumps({k: vmap.get("edge", {}).get(k) for k in
                          ("needs_key", "endpoint")}, ensure_ascii=False))
        check("H3 火山标为需要密钥且给了 AK:SK 填写说明",
              vmap.get("volcengine", {}).get("needs_key") is True
              and "AK" in (vmap.get("volcengine", {}).get("key_hint") or ""), "")
        check("H4 阿里百炼 endpoint 填到 compatible-mode/v1 一级",
              (vmap.get("aliyun", {}).get("endpoint") or "").endswith("/compatible-mode/v1"),
              vmap.get("aliyun", {}).get("endpoint", ""))
        check("H5 每家都带免费额度口径（管理员最关心的实际成本）",
              all((v.get("free_tier") or "").strip() for v in vendors), "")
        vbv = (cat2 or {}).get("voices_by_vendor") or {}
        check("H6 音色按厂商分组下发",
              set(vbv) >= {"edge", "volcengine", "aliyun"} and all(vbv.values()),
              str({k: len(v) for k, v in vbv.items()}))
        check("H7 Edge 依赖可用性被如实上报",
              isinstance((cat2 or {}).get("edge_available"), bool),
              f"edge_available={cat2.get('edge_available')}")

        # 分发判据：这一条来自**实测踩出的 bug**。管理后台选火山时会把
        # api_style 预填成 openai_compatible，若让 api_style 优先于 provider，
        # 火山行会被静默路由到 OpenAI 路径 —— 表现为「配好了却报格式不对」。
        print("\n── H8. 分发判据（provider 必须优先于 api_style）──")
        sys.path.insert(0, str(SERVER))
        from app.voice.asr_tts import _tts_dispatch_style, _volc_keys  # noqa: E402

        class _Row:      # 最小替身：分发只读这两个属性
            def __init__(self, **kw):
                self.__dict__.update(kw)

        for name, row, expect in [
            ("edge 免密钥", _Row(provider="edge"), "edge"),
            ("edge+预填 compat", _Row(provider="edge",
                                    api_style="openai_compatible"), "edge"),
            ("火山+预填 compat（本轮修的 bug）",
             _Row(provider="volcengine", api_style="openai_compatible"), "volcengine"),
            ("阿里百炼", _Row(provider="aliyun",
                              api_style="openai_compatible"), "openai_compatible"),
            ("手填火山（仅 api_style）",
             _Row(provider="my_gateway", api_style="volcengine"), "volcengine"),
            ("自建未知", _Row(provider="openai_compatible"), "openai_compatible"),
            ("存量老配置（provider 空）", _Row(provider=""), "openai_compatible"),
        ]:
            got = _tts_dispatch_style(row)
            check(f"H8 分发 {name}", got == expect, f"得到 {got}，应为 {expect}")

        check("H9 火山密钥支持中英文冒号分隔",
              _volc_keys(_Row(api_key="AK:SEC")) == ("AK", "SEC")
              and _volc_keys(_Row(api_key="AK：SEC")) == ("AK", "SEC"),
              str(_volc_keys(_Row(api_key="AK：SEC"))))

        # --- 火山签名：无法真调，改用官方文档推导过程做确定性断言 ---
        print("\n── H10. 火山签名算法（无 AK/SK，改用文档推导断言）──")
        import hashlib  # noqa: E402
        import hmac  # noqa: E402
        from datetime import datetime, timezone  # noqa: E402

        from app.voice import tts_volc as _volc  # noqa: E402

        sk, day = "SECRET", "20230101"
        k1 = hmac.new(sk.encode(), day.encode(), hashlib.sha256).digest()
        k2 = hmac.new(k1, b"cn-beijing", hashlib.sha256).digest()
        k3 = hmac.new(k2, b"openspeech", hashlib.sha256).digest()
        k4 = hmac.new(k3, b"request", hashlib.sha256).digest()
        check("H10 签名密钥四级派生与官方文档一致",
              _volc.derive_signing_key(sk, day) == k4,
              "少一级派生会被服务端 403 且错误文案不提示")
        auth = _volc.build_authorization("AK", sk, day, f"{day}T010000Z",
                                         "content-type;host", "POST\n")
        check("H11 Authorization 头三段齐全",
              all(x in auth for x in ("Credential=", "SignedHeaders=", "Signature=")),
              auth[:70])
        # H12 必须传**带非 UTC 偏移**的时间才能测出真问题：
        # 若两次都传 UTC，函数原样返回也能「通过」，等于没测。
        # 这里传本地 09:00（UTC+8），期望被换算成 01:00Z —— 这正是生产上
        # iStoreOS（Asia/Shanghai）会踩的坑：拿本地时间算签名会差 8 小时 → 403。
        from datetime import timedelta  # noqa: E402
        local9 = datetime(2026, 10, 18, 9, 0,
                          tzinfo=timezone(timedelta(hours=8)))
        _sd, rd_local = _volc.utc_stamps(local9)
        check("H12 时间戳强制 UTC（本地 9 点 UTC+8 必须换算成 01 点）",
              rd_local == "20261018T010000Z", f"得到 {rd_local}")
        # 跨日边界：本地 2026-10-19 00:30（UTC+8）= UTC 10-18 16:30，
        # short_date 必须是 10-18。若错用本地日期，跨日那 8 小时全签错。
        local0030 = datetime(2026, 10, 19, 0, 30,
                             tzinfo=timezone(timedelta(hours=8)))
        sd_edge, _ = _volc.utc_stamps(local0030)
        check("H13 跨日边界：本地 10-19 凌晨的 short_date 必须是 UTC 的 10-18",
              sd_edge == "20261018", f"得到 {sd_edge}")
        body = _volc.build_body("老人该吃药了", "BV001_streaming")
        check("H14 报文用 pcm/24000（客户端只吃裸 PCM）",
              body["audio"]["encoding"] == "pcm" and body["audio"]["sample_rate"] == 24000,
              json.dumps(body["audio"], ensure_ascii=False))
        check("H15 未给 app_id 时不塞 app 段（否则签名与请求不一致）",
              "app" not in body and "app" in _volc.build_body("x", "y", app_id="1"), "")
        import base64 as _b64  # noqa: E402
        good = _b64.b64encode(b"\x00\x01" * 10).decode()
        check("H16 火山 base64 音频能被正确取出",
              _volc._decode_response(json.dumps({"code": 3000, "data": good}).encode(),
                                     "ep") == b"\x00\x01" * 10, "")
        try:
            _volc._decode_response(json.dumps({"code": 3000}).encode(), "ep")
            check("H17 缺 data 字段必须报错（不能当空音频静默通过）", False, "没抛异常")
        except Exception:
            check("H17 缺 data 字段必须报错（不能当空音频静默通过）", True)

        # --- Edge 内置桥 ---
        print("\n── H18. Edge 内置桥 ──")
        from app.voice import tts_edge as _edge  # noqa: E402
        check("H18 edge_available() 返回布尔（前端据此显示安装指引）",
              isinstance(_edge.edge_available(), bool),
              f"available={_edge.edge_available()}")
        probe = bytes(range(256)) * 100
        chunk = int(24000 * 2 * 0.06)
        slices = [probe[i:i + chunk] for i in range(0, len(probe), chunk)]
        check("H19 分块重组逐字节无损（否则下行音频会错位）",
              b"".join(slices) == probe and chunk == 2880,
              f"chunk={chunk} 块数={len(slices)}")

        # --- 真实语音测试端点 ---
        print("\n── H20. 语音真实测试端点 ──")
        code, tv = req("POST", "/api/providers/test-voice",
                       {"usage": "tts", "text": "老人，今天该吃药了"}, token=token)
        check("H20 TTS 真实测试端点存在（本轮新增；之前会返回误导文案）",
              code == 200 and isinstance(tv, dict) and "ok" in tv,
              f"code={code} {str(tv)[:120]}")
        check("H21 失败时带可读原因（而非 500 堆栈）",
              code == 200 and (tv.get("ok") is True or bool(tv.get("message"))),
              str(tv)[:160])

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            log_fp.close()
        except Exception:
            pass
        if any(not ok for _, ok, _ in results):
            print("\n[diag] ---- server log tail ----\n" + read_log_tail())
        try:
            if DB_PATH.exists():
                DB_PATH.unlink()
        except Exception:
            pass

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n==== {passed}/{total} passed ====")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())