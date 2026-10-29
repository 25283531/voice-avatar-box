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
  P. 非 TTS 厂商预置（硅基流动等 5 家）：**装配**断言（后端下发 + 前端消费
     缺一即红）、endpoint 精确到 /v1、免费模型名、既有字段不被挤掉
     —— 这一组的存在理由见文件内 P 组开头的长注释（一次真实的「半成品提交」）

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

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_ports import guard_or_die, guard_port, release_port, resolve_port  # noqa: E402
from smoke_common import unlink_retry

# ⚠️ 本地请求绝不走系统代理（同 smoke_models.py 的理由：本机设了 http_proxy，
# 会把「访问自己刚起的 127.0.0.1」绕到代理，表现为超时且零断言失败 —— 极像回归）。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 库文件名带 PID：并行跑时独占一个库，避免互删（见 smoke_models.py DB_PATH 注释）。
DB_PATH = Path(os.environ.get(
    "SMOKE_VOICE_CATALOG_DB",
    str(Path(tempfile.gettempdir()) / f"elder_smoke_vcatalog_{os.getpid()}.db")))
if DB_PATH.exists():
    unlink_retry(DB_PATH)

PORT = resolve_port("smoke_voice_catalog")
BASE = f"http://127.0.0.1:{PORT}"

# 子进程输出**落文件**，绝不接无人消费的 PIPE（Windows 4KB 管道缓冲会锁死服务端，
# 见 smoke_models.py SERVER_LOG 处记录的踩坑史）。
SERVER_LOG = DB_PATH.with_name(DB_PATH.stem + "_server.log")

results: list[tuple[str, bool, str]] = []
#: 跳过的断言（name, 原因）。**与失败严格分开**，汇总时单列。
#:
#: 为什么要第三态（2026-10-19 的教训，比「假绿」更隐蔽）
#: ------------------------------------------------------
#: 本脚本原先按 ``edge_available`` 二选一：有 Edge 依赖就**打真网络**验试听，
#: 没有就只验 503。于是：
#:   · CI 装了 edge-tts/av（requirements.txt 里就有）⇒ 真去连微软的
#:     ``speech.platform.bing.com`` ⇒ **限流/网络抖动 ⇒ 整个后端 CI 变红**，
#:     而红的原因与本仓库代码无关；
#:   · 开发机没装 ⇒ 永远走 503 分支 ⇒ **试听成功路径一次都没被执行过**，
#:     却因为「全绿」被当成验证过了。
#: 也就是说：同一份脚本在两台机器上**验的不是同一段代码**，而两台都显示绿/红
#: 各自自洽。这比假绿更坏 —— 假绿至少有个统一的绿可对照，这里是「按环境分裂」。
#:
#: 结论：**契约必须与环境无关地断言**，联网部分显式 opt-in 且默认 SKIP。
#: SKIP 必须报出来，不能混进「通过」——否则「什么都没验」和「都验过了」同形。
skipped: list[tuple[str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条断言。

    ⚠️ detail **只在失败时打印**（其余冒烟脚本是「有 detail 就打」，本脚本刻意不同）：
    这里的 detail 多为「失败原因」的措辞（如「响应里出现了 api_key 字段」）。
    若 PASS 时也打印，一行绿里混着一句红色解释 —— 人眼扫日志时极易误读成失败，
    本脚本开发过程中就真被它误导过一次（34/34 全绿却以为 C7 挂了）。
    """
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not ok else ""))


def skip(name: str, reason: str) -> None:
    """记录一条**被跳过**的断言，并打印出来。

    跳过不是通过。汇总行里必须能看出「有多少条根本没验」。
    """
    skipped.append((name, reason))
    print(f"  [SKIP] {name}  -> {reason}")


#: 各家 TTS 的**音色 ID 形态**。用来判「前端有没有抄第二份音色清单」。
#:
#: 只收「长得就有辨识度」的两种：Edge 的 BCP-47 名（zh-CN-XiaoxiaoNeural）、
#: 火山的 ``BV001_streaming``。腾讯云的 ID 是 6 位纯数字（601009），
#: 混在 CSS 数值/超时毫秒里全是假阳性，不适合做字面量判据 —— 这也是为什么
#: 「有没有抄清单」不能只靠一种形态来判，G1 之外还有 B 组在做语义核对。
_VOICE_ID_LITERAL = re.compile(
    r"zh-[A-Za-z]{2,4}-[A-Za-z0-9]+Neural"      # Edge
    r"|BV\d{2,4}_streaming"                      # 火山
)


def _strip_js_comments(src: str) -> str:
    """去掉 HTML 注释、``/* */`` 块注释、``//`` 行注释，供「有没有硬编码」类判据使用。

    **为什么必须先剥注释再判**：G1 原来是对整份 index.html 做子串匹配，
    结果「生产上角色表存着 zh-CN-XiaoxiaoNeural 而 TTS 走腾讯云，就是这个坑」
    这种**防回归说明**反而把红线踩红了。那是文档，不是数据 —— 而恰恰是这种
    注释最该留着（后来人不知道有这个坑，就会再犯一次）。红线要防的是
    「前端抄了一份音色清单」，不是「前端提到了一个音色名」。逼人删注释来
    换绿灯，等于用错误的判据把最有价值的说明赶出代码。

    ``(?<!:)`` 是为了不把 ``https://...`` 当成行注释起点 —— 前端里 URL 不少。
    """
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)      # HTML 注释
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)       # 块注释
    src = re.sub(r"(?<!:)//[^\n]*", "", src)              # 行注释（含行尾）
    return src


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


def _lower_headers(resp) -> dict[str, str]:
    """把响应头转成**键统一小写**的 dict。

    ⚠️ 为什么必须转（2026-10-19 server-ci#96 的真实失败原因）
    -------------------------------------------------------
    原代码写的是 ``dict(resp.headers)``，把响应头降级成普通 dict ——
    而 ``resp.headers`` 其实是 ``email.message.Message``，**HTTP 头名按规范
    大小写不敏感**，``.get("Content-Type")`` 与 ``.get("content-type")`` 等价。
    转成普通 dict 后这份能力就**丢了**，只剩字符串精确匹配。

    而 uvicorn/h11 发出的头名是**小写**（HTTP/1.1 语义）⇒
    ``dict(...).get("Content-Type")`` 必然返回 None ⇒
    「Content-Type 是 audio/wav」这条断言恒红，而**服务端返回完全正确**。

    本仓已为同一类坑留过伤疤：``smoke_proxy_headers.py`` 第一版就因为
    「按 `Location` 取拿到空值」红过一次（那里是 h11 发小写、代码按驼峰取）。
    同一个坑在同一个仓里出现第二次，说明**该修的是取法，不是那条断言**。

    统一转小写后，调用方无论写 `content-type` 还是 `Content-Type` 都取得到，
    且不必再依赖「当前 server 恰好发小写」这个隐含前提。
    """
    return {k.lower(): v for k, v in resp.headers.items()}


def req_bin(method: str, path: str, token: str | None = None):
    """拿二进制的请求（试听音频是 wav，不能走 req() 的 JSON 解码）。"""
    r = urllib.request.Request(BASE + path, method=method)
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with _LOCAL_OPENER.open(r, timeout=60) as resp:
            return resp.status, resp.read(), _lower_headers(resp)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), _lower_headers(e)


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

        # ================================================== C. 音色试听
        # 2026-10-19 用户报「音色（可选）里没有任何音色供选择」。
        # 根因：APP 侧那个框是**自由输入**，从没调过 /api/providers/catalog。
        # 试听是这个缺口的另一半——光有名字没法挑，老人必须能先听。
        print("\n── C. 音色试听（/api/providers/catalog/voice-preview）──")

        # C1 未登录必须被拒：试听会消耗 TTS 配额，不能匿名白用。
        # 这一条与环境无关（鉴权在路由依赖层，早于任何合成），任何机器都必须真跑。
        code, _b, _h = req_bin("GET", "/api/providers/catalog/voice-preview?voice=x")
        check("C1 试听需要登录（匿名被拒）", code in (401, 403), f"code={code}")

        # ---- C2..C10：wav 契约，**打桩 PCM、进程内直调端点函数**，不依赖网络 ----
        # 为什么要打桩 + 直调（这是本轮修掉的真 bug 的核心）：
        #   端点真正的职责是「把上游返回的裸 PCM 包成 wav 容器」，而**不是**
        #   「能不能连上微软」。前者是本仓库的契约，后者是外部依赖。
        #   原脚本在有 edge-tts 时去打真网络，于是：
        #     · CI（装了 edge-tts/av）→ 撞微软限流 → 整个后端 CI 红，
        #       红的原因与本仓库代码无关；
        #     · 开发机（没装）→ 只跑 503 分支，C3-C6 **从未执行**，
        #       却因为「全绿」被当成验证过了。
        #   两台机器验的不是同一段代码，而各自都自洽 —— 比假绿更隐蔽。
        #
        #   打桩后：同一段代码在任何机器、任何网络状况下都被验到，且
        #   「限长」这类断言在真网络下根本没法稳定验（只能靠字节数猜）。
        #
        # ⚠️ 为什么是**进程内直调**而不是打桩后走 HTTP：
        #   服务端是**子进程**（subprocess.Popen），测试进程里替换
        #   ``tts_edge.synth_pcm`` 根本影响不到它 —— 那样写会得到一份
        #   「看起来在打桩、其实桩没生效」的假绿，正是本项目反复吃亏的那类。
        #   端点函数本身是纯逻辑（输入 text/voice，输出 wav 字节），
        #   直接调用即可；鉴权那一层（C1）仍然走真 HTTP，因为那是路由依赖的事。
        sys.path.insert(0, str(SERVER))
        from fastapi import HTTPException  # noqa: E402

        from app.routers import providers as _prov  # noqa: E402
        from app.voice import tts_edge as _edge_mod  # noqa: E402

        stub_pcm = bytes(range(256)) * 40        # 10240 字节 = 0.213s，确定性数据
        _seen: list[tuple[str, str]] = []
        _orig_synth = _edge_mod.synth_pcm
        _orig_avail = _edge_mod.edge_available

        def _fake_synth(text: str, voice: str, **_kw) -> bytes:
            _seen.append((text or "", voice or ""))
            return stub_pcm

        def _boom(*_a, **_kw):
            raise RuntimeError("上游炸了")

        def _call_preview(text: str = "您好，我是小暖。",
                          voice: str = "zh-CN-XiaoxiaoNeural"):
            """直调端点函数，返回 (status, body, headers)；异常路径还原成状态码。

            `user` 传 None：它只服务于 `Depends(get_current_user)`，
            函数体内并未使用（鉴权由 C1 走真 HTTP 覆盖）。
            """
            try:
                resp = _prov.voice_preview(voice=voice, text=text, user=None)
                # ⚠️ 这里也**不能**写 dict(resp.headers)（同 D37）：
                # Starlette 的 Response.headers 是 MutableHeaders，本身大小写
                # 不敏感；转成普通 dict 后按驼峰取就取不到。统一走 _lower_headers。
                return (resp.status_code, resp.body,
                        {k.lower(): v for k, v in resp.headers.items()})
            except HTTPException as exc:
                return exc.status_code, str(exc.detail).encode(), {}

        try:
            # C2 正常路径：打桩 PCM ⇒ 纯逻辑，完全离线。
            _edge_mod.synth_pcm = _fake_synth
            _edge_mod.edge_available = lambda: True
            status, wav, hdrs = _call_preview()
            check("C2 试听端点 200（打桩 PCM，不联网）", status == 200,
                  f"status={status} body={wav[:120]!r}")
            # 键已由 _lower_headers 统一成小写，故这里大小写两种写法都能取到
            # （不再依赖「uvicorn 恰好发小写」这个隐含前提）。
            check("C3 Content-Type 是 audio/wav",
                  "audio/wav" in (hdrs.get("content-type") or ""),
                  f"got={hdrs.get('content-type')!r} 全部头={list(hdrs)[:8]}")

            # C4 容器头：RIFF/WAVE 魔数 + 长度自洽（44 字节头 + 桩长度）。
            # 「能播但速度不对」（采样率写错）比不播更难排查，
            # 所以下面 C5 连位深与采样率一起钉死。
            ok_hdr = (len(wav) > 44 and wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
                      and len(wav) == 44 + len(stub_pcm))
            check("C4 是合法 wav 容器（RIFF/WAVE 魔数 + 长度自洽）", ok_hdr,
                  f"magic={wav[:4]!r}/{wav[8:12]!r} len={len(wav)} 期望={44 + len(stub_pcm)}")
            if ok_hdr:
                with contextlib.closing(io.BytesIO(wav)) as b:
                    with wave.open(b) as w:
                        ch, sw, fr, nf = (w.getnchannels(), w.getsampwidth(),
                                          w.getframerate(), w.getnframes())
                check("C5 格式 = 1ch/16bit/24kHz（与 synth_pcm 契约一致）",
                      (ch, sw, fr) == (1, 2, 24000), f"{ch}ch/{sw * 8}bit/{fr}Hz")
                check("C6 音频数据未被容器篡改（帧数与桩一致）",
                      nf * ch * sw == len(stub_pcm), f"frames={nf} 期望={len(stub_pcm) // 2}")

            # C7 text 限长（防「把 TTS 当免费下载器」）：打桩后**必然可断言**，
            # 因为我们知道上游究竟拿到了什么。
            _seen.clear()
            long_text = "试听限长测试" * 40          # 240 字，远超 60 上限
            status, _b, _h = _call_preview(text=long_text)
            got_text = _seen[0][0] if _seen else ""
            check("C7 超长试听文本被截断到 60 字（不是原样合成）",
                  bool(got_text) and len(got_text) <= 60 and len(long_text) > 60,
                  f"上游收到 {len(got_text)} 字 / 原 {len(long_text)} 字")
            check("C8 音色名原样透传给上游（没被端点偷偷改掉）",
                  bool(_seen) and _seen[0][1] == "zh-CN-XiaoxiaoNeural",
                  f"上游收到音色 {_seen[0][1]!r}" if _seen else "上游根本没被调用")

            # C9 合成失败必须映射成 502 而不是 500：500 对用户是「服务器坏了」，
            # 而这明明是上游故障 —— 老人侧排查方向完全不同。
            _edge_mod.synth_pcm = _boom
            status, body, _h = _call_preview()
            check("C9 合成异常映射为 502（不是 500，也不是 200 空音频）",
                  status == 502, f"status={status} body={body[:160]!r}")

            # C10 依赖缺失必须映射成 503（前端据此提示「服务未启用语音合成」）。
            _edge_mod.synth_pcm = _fake_synth
            _edge_mod.edge_available = lambda: False
            status, body, _h = _call_preview()
            check("C10 未启用 TTS 时返回 503（不是 500）", status == 503,
                  f"status={status} body={body[:160]!r}")
        finally:
            _edge_mod.synth_pcm = _orig_synth
            _edge_mod.edge_available = _orig_avail

        # C11 真网络合成：**默认 SKIP**。
        # 这是唯一必须连微软的一条，而它恰恰最不可靠（非公开协议 + 限流，
        # 见 tts_edge 模块头的实测记录）。默认不跑、显式 opt-in，
        # 才能保证「后端 CI 红」永远意味着「代码有问题」而不是「微软抽风」。
        # ⚠️ 走真 HTTP（不是直调）：它要验的正是「真上游能不能通」。
        if os.environ.get("ELDER_SMOKE_EDGE_LIVE") == "1":
            code, wav, _h = req_bin(
                "GET", "/api/providers/catalog/voice-preview?voice=zh-CN-XiaoxiaoNeural",
                token=token)
            check("C11 真网络 Edge 合成（opt-in，可能因限流失败）", code == 200,
                  f"code={code} len={len(wav)}")
        else:
            skip("C11 真网络 Edge 合成",
                 "需 ELDER_SMOKE_EDGE_LIVE=1；Edge 是非公开协议且限流，"
                 "写进默认 CI 会让红灯与本仓库代码无关")

        # ================================================== D. 单一真源一致性
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
        user_html = (SERVER / "app" / "static" / "user.html").read_text(encoding="utf-8")
        # 预置音色必须由服务端目录驱动，而不是前端硬编码 —— 前端硬编码等于
        # 第二份真源，「恢复默认」迟早对不上（见 app/voice/catalog.py 模块说明）。
        #
        # 判据按「代码里的字面音色 ID」判，不按「文件里出现过这个字符串」判：
        # 先剥注释，再匹配音色 ID 形态（见 _strip_js_comments 的说明）。
        # 两个页面都要查 —— 用户端（user.html）也有音色下拉，只查管理端
        # 等于给用户端留了一道没上锁的门。
        for page_name, page_src in (("index.html", html), ("user.html", user_html)):
            hits = _VOICE_ID_LITERAL.findall(_strip_js_comments(page_src))
            check(f"G1 前端不再硬编码音色清单（{page_name}）",
                  not hits,
                  f"{page_name} 代码里出现了字面音色 ID {sorted(set(hits))[:5]}，"
                  f"说明前端又抄了一份真源（写在注释里举例不算）")
        # label 既可以是字面量，也可以绑到变量（:label="voiceGroupLabel"）——
        # 后者正是「标题跟着生效厂商变」的做法，不能因为绑了变量就判红。
        # 这条要防的是「音色框退回成自由文本输入」，不是「标题怎么来的」。
        check("G2 音色框是下拉选择（optgroup 承载预置项）",
              "optgroup" in html
              and (':label="voiceGroupLabel"' in html or 'label="── 预置音色' in html),
              "音色控件不再是 optgroup 分组的下拉框，用户无从知道系统提供哪些音色")
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
        from app.voice.asr_tts import _tts_dispatch_style, _secret_pair  # noqa: E402

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
            # 腾讯云与火山同型：预置会把 api_style 一并写好，必须让
            # provider 优先，否则腾讯云行会被静默路由到 OpenAI 路径。
            ("腾讯云", _Row(provider="tencent",
                          api_style="openai_compatible"), "tencent"),
            ("手填腾讯云（仅 api_style）",
             _Row(provider="my_gateway", api_style="tencent"), "tencent"),
            ("自建未知", _Row(provider="openai_compatible"), "openai_compatible"),
            ("存量老配置（provider 空）", _Row(provider=""), "openai_compatible"),
        ]:
            got = _tts_dispatch_style(row)
            check(f"H8 分发 {name}", got == expect, f"得到 {got}，应为 {expect}")

        check("H9 两段密钥支持中英文冒号分隔（火山 + 腾讯云共用）",
              _secret_pair(_Row(api_key="AK:SEC")) == ("AK", "SEC")
              and _secret_pair(_Row(api_key="AK：SEC")) == ("AK", "SEC")
              and _secret_pair(_Row(api_key="ONLY")) == ("ONLY", ""),
              str(_secret_pair(_Row(api_key="AK：SEC"))))

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

        # ==================== P. 非 TTS 厂商预置（LLM/向量/重排）
        # 为什么必须有这一组（2026-10-18 仓库核验发现的「半成品提交」）
        # ----------------------------------------------------------
        # 这批预置（app/provider_presets.py）曾以「只提交前端」的形态进过
        # main：index.html 消费 providerPresets，而 providers.py 的接线与
        # 预置表本身留在工作区没提交。页面**不报错**（Array.isArray 兜底成
        # 空数组），表现为「下拉里只有自部署三项，硅基流动不出现」——
        # 而当时没有任何一条断言覆盖它，提交信息却写着「冒烟通过」。
        # P1/P9 两条钉的就是这个**装配**（后端发 + 前端消费，缺一即红）。
        print("\n── P. 非 TTS 厂商预置（provider_presets）──")
        presets = (cat2 or {}).get("provider_presets")
        check("P1 catalog 下发 provider_presets 且是非空数组（装配：后端真的发了）",
              isinstance(presets, list) and len(presets) > 0,
              f"type={type(presets).__name__} "
              f"len={len(presets) if isinstance(presets, list) else '-'}")
        pmap = {p.get("value"): p for p in (presets or [])}
        check("P2 五家齐全（硅基流动/DeepSeek/智谱/阿里百炼/OpenAI）",
              set(pmap) >= {"siliconflow", "deepseek", "zhipu", "aliyun", "openai"},
              f"{sorted(pmap)}")
        req_fields = {"value", "label", "api_style", "endpoint", "needs_key",
                      "key_hint", "models", "free_tier", "notes"}
        incomplete = [p.get("value") for p in (presets or [])
                      if not req_fields.issubset(p.keys())]
        check("P3 每条预置字段齐全（下拉/自动填表/费用说明全靠这些字段渲染）",
              not incomplete, f"缺字段: {incomplete}")
        sf = pmap.get("siliconflow") or {}
        check("P4 硅基流动 endpoint 精确到 /v1 一级（少一级会被网关返 HTML 首页）",
              sf.get("endpoint") == "https://api.siliconflow.cn/v1",
              sf.get("endpoint", ""))
        sf_models = sf.get("models") or {}
        check("P5 硅基流动含 embedding+rerank 两个免费模型（记忆检索零成本的两条腿）",
              sf_models.get("embedding") == "BAAI/bge-m3"
              and sf_models.get("rerank") == "BAAI/bge-reranker-v2-m3",
              json.dumps(sf_models, ensure_ascii=False))
        check("P6 硅基流动 key_hint 明确劝退「把整条接口地址粘进 endpoint」"
              "（2026-10-18 大模型调用失败事故的直接对策）",
              sf.get("needs_key") is True and "接口地址" in (sf.get("key_hint") or ""), "")
        # 旧字段不得被新表挤掉：presets_payload 是往同一个 payload 里 update，
        # 不是替换。若有人把它写成 payload = presets_payload()，这里立刻红。
        check("P7 预置表没有挤掉既有字段（kinds/api_styles/vendors/voices 仍在）",
              all(k in (cat2 or {}) for k in ("kinds", "api_styles", "vendors", "voices")),
              str(sorted((cat2 or {}).keys())))
        from app.provider_presets import preset_entry  # noqa: E402
        check("P8 preset_entry：已知值返回条目；未知值/空串/None 返回 None"
              "（前端据此走通用手填路径）",
              (preset_entry("siliconflow") or {}).get("endpoint") == "https://api.siliconflow.cn/v1"
              and preset_entry("nope") is None and preset_entry("") is None
              and preset_entry(None) is None, "")
        # 消费侧装配：后端发了但前端不消费，同样表现为「下拉只有自部署三项」。
        check("P9 前端消费 providerPresets 且有 Array.isArray 兜底"
              "（字段缺失时退回自部署三项而不是模板抛错）",
              "providerPresets" in html and "Array.isArray(r.provider_presets)" in html,
              "index.html 里找不到 providerPresets 的装载处")

        # P10/P11：智谱那一行踩过的两个坑（2026-10-24 用户反馈驱动）
        # ----------------------------------------------------------
        # 反馈原话：「服务端智谱的配置项不能填写模型名称，无法指定使用
        # glm-4-flash-250414」。真因不是后端校验（后端只在 model 为空时
        # 才填预置值，见 _normalize），而是前端把预置模型锁成只读，
        # 只留一个 12px 的「改成自定义」链接 —— 等于没给出口。
        # 顺带查出更危险的：预置把 llm_main 也填成 glm-4-flash，而免费档
        # 实测 5 次 0 次真的调 create_reminder（只会口头答应）。药品提醒
        # 出这种「假成功」是不可接受的，所以默认值必须换成能调工具的。
        zp = pmap.get("zhipu") or {}
        zp_models = zp.get("models") or {}
        check("P10 智谱默认值：小模型=glm-4-flash-250414（实测抽取 4/4、"
              "TTFT 611ms），主对话不得填免费档（实测 0/5 不能调工具）",
              zp_models.get("llm_light") == "glm-4-flash-250414"
              and not str(zp_models.get("llm_main") or "").startswith("glm-4-flash"),
              json.dumps(zp_models, ensure_ascii=False))

        # 钉「非 TTS 的模型栏可编辑」：直接读 modelLocked 的函数体，
        # 确认里面有 tts 分支且非 tts 走 return false。
        # 不钉的话，将来谁把锁加回来，用户又会回到「填不了模型名」。
        import re as _re  # noqa: PLC0415
        _m = _re.search(r"modelLocked\(\)\s*\{(.*?)\n    \},", html, _re.S)
        _body = (_m.group(1) if _m else "")
        check("P11 模型栏只对 TTS 上锁，非 TTS 可自由填模型名"
              "（2026-10-24「智谱填不了模型名」的直接对策）",
              "kind === 'tts'" in _body and "return false" in _body,
              _body.strip()[:160] or "index.html 里找不到 modelLocked()")

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
                unlink_retry(DB_PATH)
        except Exception:
            pass

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    # ⚠️ 跳过**单列**，绝不混进「通过」：把「没验」显示成「验过了」，
    # 是本项目在 libopus、Edge 依赖、SKIP 第三态上反复付过学费的错误。
    summary = f"\n==== {passed}/{total} passed ===="
    if skipped:
        summary += f"（另有 {len(skipped)} 条跳过）"
    print(summary)
    for name, reason in skipped:
        print(f"  SKIPPED: {name} —— {reason}")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())