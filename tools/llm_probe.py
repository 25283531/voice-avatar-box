"""单个（或多个）LLM 模型的综合探针：速度 + 工具调用稳定性 + 强制模式对照。

与 ``tools/llm_bench.py`` 的分工
-----------------------------
``llm_bench.py`` 只测速度（三场景 TTFT/总耗时），适合横向比一排模型；
本脚本额外回答**这个模型能不能干活**，因为对一个吃药提醒场景来说，
"答应得很好但没调工具"比"慢三秒"严重得多 —— 而速度测试看不出来。

三段输出：
1. **速度**：闲聊 / 工具决策，各 3 次取中位（含预热）。
2. **稳定性**：工具意图连打 5 次（``tool_choice=auto``），看它愿不愿意调。
3. **强制对照**：``tool_choice=required`` 打 3 次，区分「不会调」与「不愿调」——
   强制下参数填得对 ⇒ 能力在，只是行为倾向用嘴答应；强制下空返回 ⇒ 能力缺陷。
   ⚠️ 智谱（bigmodel）的 ``tool_choice`` 只支持 ``auto``，传 required 会 400，
   此时该段只作"不支持"提示，不算失败。

用法（容器内，才能读到 /data/elder.db 的密钥）::

    # 硅基流动（默认，key 从生产库读）
    docker cp tools/llm_probe.py elder-server:/app/_llm_probe.py
    docker exec -w /app elder-server python _llm_probe.py THUDM/GLM-4-9B-0414

    # 智谱（key 走环境变量，不落盘）
    docker exec -w /app -e ZHIPU_KEY="$K" elder-server python _llm_probe.py \\
        --base https://open.bigmodel.cn/api/paas/v4 --key-env ZHIPU_KEY \\
        --thinking-off glm-5.3 glm-4.7-flash
"""
from __future__ import annotations

import json
import os
import sqlite3
import statistics as st
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "https://api.siliconflow.cn/v1"
TIMEOUT = 60
BASELINE = "deepseek-ai/DeepSeek-V4-Flash"   # 线上现用，作为参照

Q_REMIND = "小暖小暖，明天早上八点提醒我吃药"
Q_CASUAL = "我今天有点累，不想出门了"


def _key(env_name: str | None) -> str:
    """取密钥：优先环境变量（智谱），否则从生产库读（硅基流动）。"""
    if env_name:
        val = (os.getenv(env_name) or "").strip()
        if not val:
            raise SystemExit(f"环境变量 {env_name} 为空")
        return val
    conn = sqlite3.connect("/data/elder.db")
    row = conn.execute(
        "select api_key from ai_providers "
        "where provider='siliconflow' and kind='llm' and api_key<>'' "
        "order by priority limit 1").fetchone()
    conn.close()
    if not row or not row[0]:
        raise SystemExit("库里没有可用的硅基流动 LLM 密钥")
    return row[0].strip()


def _call(key: str, model: str, messages: list[dict], tools: list[dict],
          base: str, tool_choice: str = "auto",
          thinking_off: bool = False) -> dict:
    """一次流式请求。返回 ttft / total / 文本 / 工具调用（含参数）/ 思考字数。"""
    payload: dict = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "stream": True,
    }
    if thinking_off:
        # 智谱 GLM-4.5+ 的思维链开关。不关的话首 token 会被几百个思考 token
        # 顶到几秒后 —— 语音场景等不起，生产必须压到最低。
        #
        # ⚠️ glm-5.x 是「始终思考」的模型，传 thinking:disabled 会直接 400：
        #   {"error":{"code":"1210","message":"该模型始终思考，不支持关闭思考；
        #    请使用 low、high 或 max。"}}
        # 对这种模型只能把 reasoning_effort 压到 low（它没有 none/minimal 档）。
        if model.startswith("glm-5"):
            payload["reasoning_effort"] = "low"
        else:
            payload["thinking"] = {"type": "disabled"}
    else:
        # 硅基流动口径：这个字段对不支持的模型是无害的（被忽略）。
        payload["enable_thinking"] = False

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    })

    t0 = time.monotonic()
    ttft = None
    text: list[str] = []
    think = 0
    slots: dict[int, dict] = {}
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj.get("error"), dict):
                    return {"ok": False, "total": int((time.monotonic() - t0) * 1000),
                            "err": str(obj["error"])[:140]}
                for ch in obj.get("choices") or []:
                    delta = ch.get("delta") or {}
                    think += len(delta.get("reasoning_content") or "")
                    if delta.get("content"):
                        if ttft is None:
                            ttft = int((time.monotonic() - t0) * 1000)
                        text.append(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = slots.setdefault(idx, {"name": "", "args": ""})
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
                        if ttft is None:
                            ttft = int((time.monotonic() - t0) * 1000)
        total = int((time.monotonic() - t0) * 1000)
        calls = [v for v in slots.values() if v["name"]]
        return {"ok": True, "ttft": ttft if ttft is not None else total,
                "total": total, "text": "".join(text), "calls": calls,
                "think": think}
    except urllib.error.HTTPError as exc:
        # 必须把响应体读出来：智谱的 400 会给出 code + 人话原因
        # （例如 code=1210「该模型始终思考，不支持关闭思考」），
        # 只报 "HTTP Error 400" 等于什么都没说。
        try:
            detail = exc.read().decode("utf-8", "replace")[:180]
        except Exception:  # noqa: BLE001
            detail = ""
        return {"ok": False, "total": int((time.monotonic() - t0) * 1000),
                "err": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "total": int((time.monotonic() - t0) * 1000),
                "err": f"{type(exc).__name__}: {str(exc)[:140]}"}


def _probe(key: str, model: str, system: str, tools: list[dict], base: str,
           thinking_off: bool) -> None:
    print("=" * 74)
    print(model)
    print("=" * 74)
    kw = {"base": base, "thinking_off": thinking_off}
    head = [{"role": "system", "content": system}]

    # 预热：把冷启动（2~3s）移出测量窗口。三个场景里"闲聊"永远排第一，
    # 不预热会系统性冤枉它。
    _call(key, model, head + [{"role": "user", "content": "你好"}], tools, **kw)

    print("  -- 速度（3 次中位，TTFT/总 ms）--")
    for name, q in (("闲聊", Q_CASUAL), ("工具决策", Q_REMIND)):
        runs = [_call(key, model, head + [{"role": "user", "content": q}],
                      tools, **kw) for _ in range(3)]
        oks = [r for r in runs if r["ok"]]
        if not oks:
            print(f"    [{name}] 全失败：{runs[0]['err']}")
            continue
        ttft = int(st.median([r["ttft"] for r in oks]))
        tot = int(st.median([r["total"] for r in oks]))
        hit = sum(1 for r in oks if r["calls"])
        think = max(r.get("think", 0) for r in oks)
        extra = f"  ⚠️思考{think}字" if think else ""
        print(f"    [{name:4s}] TTFT {ttft:5d}ms  总 {tot:5d}ms  "
              f"（{hit}/{len(oks)} 次调了工具）{extra}")
        print(f"           样本：{oks[0]['text'][:70]!r}")

    print("  -- 工具意图 ×5（tool_choice=auto，看愿不愿意调）--")
    ok_hits = 0
    for i in range(5):
        r = _call(key, model, head + [{"role": "user", "content": Q_REMIND}],
                  tools, **kw)
        if not r["ok"]:
            print(f"    #{i + 1} 失败 {r['err']}")
            continue
        hit = bool(r["calls"])
        ok_hits += hit
        mark = "✅调工具" if hit else "❌没调"
        arg = (r["calls"][0]["args"][:52] if hit else r["text"][:44])
        print(f"    #{i + 1} {r['ttft']:5d}/{r['total']:5d}ms  {mark}  {arg!r}")
    print(f"    ⇒ 工具调用成功率 {ok_hits}/5")

    print("  -- 强制对照 ×3（tool_choice=required，看会不会调）--")
    for i in range(3):
        r = _call(key, model, head + [{"role": "user", "content": Q_REMIND}],
                  tools, tool_choice="required", **kw)
        if not r["ok"]:
            note = "（该厂商可能不支持 required）" if "400" in r["err"] else ""
            print(f"    #{i + 1} 失败 {r['err']} {note}")
            continue
        if r["calls"]:
            c = r["calls"][0]
            print(f"    #{i + 1} {r['total']:6d}ms  {c['name']}({c['args'][:70]})")
        else:
            print(f"    #{i + 1} {r['total']:6d}ms  ⚠️ 空返回（工具和文本都没给）"
                  f"  文本={r['text'][:40]!r}")
    print()


def main() -> int:
    args = sys.argv[1:]
    base = DEFAULT_BASE
    key_env: str | None = None
    thinking_off = False
    models: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--base":
            base = args[i + 1]; i += 2
        elif a == "--key-env":
            key_env = args[i + 1]; i += 2
        elif a == "--thinking-off":
            thinking_off = True; i += 1
        else:
            models.append(a); i += 1
    models = models or ["THUDM/GLM-4-9B-0414"]
    # 硅基流动的池子里才带基准线；测别的厂商时硬塞基准会多花钱且不可比。
    if base == DEFAULT_BASE and BASELINE not in models:
        models.append(BASELINE)

    from app.voice.prompts import BASE_VOICE_PROMPT
    from app.voice.tools import TOOL_DEFS

    key = _key(key_env)
    print(f"endpoint {base}，system prompt {len(BASE_VOICE_PROMPT)} 字符，"
          f"工具 {len(TOOL_DEFS)} 个，thinking_off={thinking_off}\n")
    for model in models:
        _probe(key, model, BASE_VOICE_PROMPT, TOOL_DEFS, base, thinking_off)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
