"""LLM 候选模型响应速度基准：用**线上真实 payload**量 TTFT 与总耗时。

为什么不能用「随便发一句 hello」测
--------------------------------
语音链路的首帧延迟几乎全部由 LLM 决定（见 ``deliverables/latency-analysis-2026-10-23.md``：
第一轮 2391ms + 第二轮 4239ms，占处理段 82%）。而 LLM 的耗时对**输入结构**极度敏感：
- 带 ``tools`` 定义（本项目有 6 个工具，约 1.5k token）时，很多小模型会先"想一遍"
  再决定要不要调，TTFT 明显高于纯闲聊；
- Qwen3 系列默认开 thinking，不显式关掉的话首 token 会被几百个思考 token 顶到几秒后。

所以本脚本用 ``app.voice.prompts.BASE_VOICE_PROMPT`` + ``app.voice.tools.TOOL_DEFS``
原样构造请求，跑三个场景（闲聊 / 工具决策 / 工具后生成），与线上同口径。

用法（容器内，才能读到 /data/elder.db 里的密钥）::

    docker cp tools/llm_bench.py elder-server:/tmp/llm_bench.py
    docker exec -e PYTHONPATH=/app -w /app elder-server python /tmp/llm_bench.py
"""
from __future__ import annotations

import json
import statistics as st
import time
import urllib.request

# 待测模型。第一个是**线上正在用**的，作为基准线；换模型时先看它再比。
MODELS: list[str] = [
    "deepseek-ai/DeepSeek-V4-Flash",   # 线上现用（基准）
    "XingChenAGI/Xing4.0-29B",
    "Qwen/Qwen3.5-4B",
    "Qwen/Qwen3-8B",
]

ENDPOINT = "https://api.siliconflow.cn/v1/chat/completions"
TIMEOUT = 60
REPEAT = 3          # 每场景重复次数（取中位，抵消单次抖动）
# 每个模型先打一次「预热」并丢弃：冷启动（模型调度/连接建立）动辄 2~3 秒，
# 而三个场景里"闲聊"永远排第一 ⇒ 不预热的话第一个场景会系统性偏慢，
# 看起来像"这个模型闲聊特别慢"，其实是冷启动。第一版基准就踩了这个坑。

# 三个场景对应线上真实的两轮结构：
#   1) 闲聊：不需要工具，一轮结束
#   2) 工具决策：模型该返回 tool_calls（= 线上的 round[0]）
#   3) 工具后生成：拿到工具结果后说人话（= 线上的 round[1]）
SCENARIOS: list[tuple[str, list[dict]]] = [
    ("闲聊", [{"role": "user", "content": "我今天有点累，不想出门了"}]),
    ("工具决策", [{"role": "user",
                   "content": "小暖小暖，明天早上八点提醒我吃药"}]),
    ("工具后生成", [
        {"role": "user", "content": "小暖小暖，明天早上八点提醒我吃药"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "create_reminder",
                                      "arguments": '{"title":"吃药","when":"明天早上八点"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "create_reminder",
         "content": json.dumps({"ok": True,
                                "summary": "明天早上 8:00 提醒吃药"},
                               ensure_ascii=False)},
    ]),
]


def _load_key_and_prompts() -> tuple[str, str, list[dict]]:
    """从生产库取硅基流动密钥，从项目代码取真实 system prompt 与工具定义。"""
    import sqlite3

    from app.voice.prompts import BASE_VOICE_PROMPT
    from app.voice.tools import TOOL_DEFS

    conn = sqlite3.connect("/data/elder.db")
    row = conn.execute(
        "select api_key from ai_providers "
        "where provider='siliconflow' and kind='llm' and api_key<>'' "
        "order by priority limit 1").fetchone()
    conn.close()
    if not row or not row[0]:
        raise SystemExit("库里没有可用的硅基流动 LLM 密钥")
    return row[0].strip(), BASE_VOICE_PROMPT, TOOL_DEFS


def _one_call(key: str, model: str, messages: list[dict],
              tools: list[dict]) -> dict:
    """发一次流式请求，返回 TTFT / 总耗时 / 输出摘要。

    手工解 SSE 而不是用 SDK：线上 ``llm_gateway.chat_stream`` 就是逐行读
    ``data: {...}``，这里保持同一路径，测出来的数才和日志里的 ttft 可比。
    """
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": messages[0]["content"]}]
                    + list(messages[1:]),
        "tools": tools,
        "tool_choice": "auto",
        "stream": True,
        # Qwen3 系列默认开思考；关掉才代表"我们要的那种快"。
        # 不支持的模型会忽略这个字段，不影响结果。
        "enable_thinking": False,
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    })

    t0 = time.monotonic()
    ttft = None
    text_parts: list[str] = []
    think_chars = 0
    tool_names: list[str] = []
    usage: dict = {}
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if obj.get("usage"):
                    usage = obj["usage"]
                for choice in obj.get("choices") or []:
                    delta = choice.get("delta") or {}
                    # 思考增量：Qwen3 系列若没被 enable_thinking:false 关掉，
                    # 这些字会先把 TTFT 顶到几秒后，而且一个都不念给老人。
                    think_chars += len(delta.get("reasoning_content") or "")
                    piece = delta.get("content") or ""
                    if piece:
                        if ttft is None:
                            ttft = int((time.monotonic() - t0) * 1000)
                        text_parts.append(piece)
                    for tc in delta.get("tool_calls") or []:
                        name = ((tc.get("function") or {}).get("name") or "")
                        if name and name not in tool_names:
                            if ttft is None:
                                ttft = int((time.monotonic() - t0) * 1000)
                            tool_names.append(name)
        total = int((time.monotonic() - t0) * 1000)
        return {"ok": True, "ttft": ttft if ttft is not None else total,
                "total": total, "text": "".join(text_parts)[:60],
                "tools": tool_names, "think_chars": think_chars,
                "out_tokens": int(usage.get("completion_tokens") or 0),
                "in_tokens": int(usage.get("prompt_tokens") or 0)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "err": f"{type(exc).__name__}: {str(exc)[:120]}",
                "total": int((time.monotonic() - t0) * 1000)}


def main() -> int:
    key, system_prompt, tools = _load_key_and_prompts()
    print(f"system prompt {len(system_prompt)} 字符，工具 {len(tools)} 个，"
          f"每场景重复 {REPEAT} 次\n")

    results: dict[str, dict[str, list[dict]]] = {}
    for model in MODELS:
        print("=" * 76)
        print(model)
        print("=" * 76)
        results[model] = {}
        # 预热：结果直接丢弃，只为把冷启动成本移出测量窗口。
        _one_call(key, model,
                  [{"role": "system", "content": system_prompt},
                   {"role": "user", "content": "你好"}], tools)
        for name, msgs in SCENARIOS:
            runs = []
            payload = [{"role": "system", "content": system_prompt}] + msgs
            for _ in range(REPEAT):
                runs.append(_one_call(key, model, payload, tools))
            results[model][name] = runs
            oks = [r for r in runs if r["ok"]]
            if not oks:
                print(f"  [{name:6s}] 失败：{runs[0]['err']}")
                continue
            ttft = int(st.median([r["ttft"] for r in oks]))
            tot = int(st.median([r["total"] for r in oks]))
            r0 = oks[0]
            extra = (f"tool={','.join(r0['tools'])}" if r0["tools"]
                     else f"文本={r0['text'][:28]!r}")
            if r0.get("think_chars"):
                extra += f"  ⚠️思考{r0['think_chars']}字"
            print(f"  [{name:6s}] TTFT {ttft:5d}ms  总 {tot:5d}ms  "
                  f"in={r0['in_tokens']} out={r0['out_tokens']}  {extra}")

    print()
    print("=" * 76)
    print("汇总（中位，ms）")
    print("=" * 76)
    head = f"{'模型':30s}" + "".join(f"{n:>16s}" for n, _ in SCENARIOS)
    print(head)
    for model in MODELS:
        cells = []
        for name, _ in SCENARIOS:
            oks = [r for r in results[model][name] if r["ok"]]
            if not oks:
                cells.append("失败")
            else:
                cells.append(f"{int(st.median([r['ttft'] for r in oks]))}"
                             f"/{int(st.median([r['total'] for r in oks]))}")
        print(f"{model[:29]:30s}" + "".join(f"{c:>16s}" for c in cells))
    print("\n（单元格为 TTFT/总耗时）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
