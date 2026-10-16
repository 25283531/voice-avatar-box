"""语音链路的记忆接入点：**说话前召回，说话后沉淀**。

⚠️ 当前状态：接入点已就绪，但**还没有调用方**
------------------------------------------------
`server/app/voice/`（自研 WebSocket 语音服务端，见 app/config.py 的
VOICE_WS_PATH 说明）目前**尚未落地**，server/app 下也没有任何 WebSocket 端点
（`grep -rn "websocket" server/app/` 只有 config.py 的注释命中）。
也就是说：把本模块真正接进语音链路，需要先有那个包。

⚠️ 因此这里**刻意不去造一套 WebSocket 语音服务**。原因有三条，每条都够独立：
1. 语音链路（VAD / Opus 编解码 / 打断 / 复述 / OTA）是另一个大模块的职责，
   在记忆模块里重造一遍等于把同一套协议实现两次，迟早分叉；
2. 记忆是**旁路能力**，它只提供两个纯函数式的钩子，与传输层无关 ——
   硬绑到某个 WS 实现上，等于让记忆模块被迫依赖一个还不存在的模块；
3. 真要在 device_api.py 里塞一个简化版 WS 服务，看着「跑通了」，但它一定会
   与后来的正式实现冲突，届时的迁移成本远高于现在多写一个 bridge。

所以正确用法是：**voice 包落地后，在它的对话编排处调这两个函数**，
一行接线即可（见本文件末尾的接线示例）。

为什么钩子要「永不抛异常」
--------------------------
记忆是**增益**，不是对话的前提。老人正在等一句话，记忆检索慢了或模型没配，
正确的表现是「这次不记得，照常回答」，而不是整个对话失败。
所以这两个函数内部把所有异常都吞掉并降级为空结果。
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

from sqlalchemy.orm import Session

from .client import ModelClient
from .persona import build_persona_prompt, record_feedback
from .service import build_context, memorize_background, recent_turns

log = logging.getLogger("elder.memory.bridge")


def recall_for_reply(
    db: Session,
    user_id: int,
    agent_id: int | None,
    utterance: str,
    *,
    session_id: str = "",
    recent: int = 0,
    client: ModelClient | None = None,
) -> str:
    """说话前：召回与这句话相关的记忆 + 人格提示词，拼成可注入 prompt 的文本。

    返回**空字符串**表示「没什么可注入的」，调用方原样拼到 system prompt 后面即可。

    参数
    ----
    recent  >0 时额外带上最近 N 句原话。语音是流式的，上下文窗口里通常已经有
            最近几轮，这时再塞一遍是重复；只有当调用方自己**不**维护历史
            （把最近对话交给记忆模块管）时才建议 >0。

    为什么把「事实记忆」和「人格提示词」合成一次返回：语音链路每轮只调一次
    模型，两段文本最终都要进同一个 system prompt。分两个函数让调用方多写
    一次拼接，也多一次「这次忘了带人格」的漏调机会。
    """
    try:
        parts: list[str] = []

        persona = build_persona_prompt(db, user_id, agent_id)
        if persona:
            parts.append(persona)

        if recent > 0:
            turns = recent_turns(db, user_id, agent_id, session_id or None, limit=recent)
            if turns:
                lines = ["【刚刚聊过的话】"]
                lines += [f"{'老人' if t['role'] == 'user' else '你'}：{t['content']}"
                          for t in turns]
                parts.append("\n".join(lines))

        # 召回失败（没配向量模型 / 网关不可用）不该让整段上下文归零：
        # 人格提示词和最近对话仍然有用，所以分开 try。
        try:
            ctx = build_context(db, utterance, user_id, agent_id, client=client)
            if ctx:
                parts.append(ctx)
        except Exception as exc:  # noqa: BLE001  召回是增益，失败就只用别的部分
            log.warning("记忆召回失败，本次不带记忆回答：%s: %s", type(exc).__name__, exc)

        return "\n\n".join(p for p in parts if p)
    except Exception as exc:  # noqa: BLE001  记忆永远不该让对话失败
        log.warning("组装记忆上下文失败，按无记忆回答：%s: %s", type(exc).__name__, exc)
        return ""


def remember_after_reply(
    db_factory: Any,
    user_id: int,
    agent_id: int | None,
    turns: Sequence[Any],
    *,
    session_id: str = "",
    user_text: str = "",
    bot_text: str = "",
    prev_bot_text: str = "",
    signal: str = "",
    client: ModelClient | None = None,
) -> None:
    """说话后：异步沉淀记忆 + 记录本轮反馈。

    全程**不阻塞**调用方（对话已经答完了，用户在等下一句）：
      * 记忆写入走 ``memorize_background``（后台线程 + 独立会话）；
      * 反馈判定是纯规则计算（persona.judge_feedback），毫秒级，直接同步做，
        不值得为它再起一个线程 —— 而反馈必须在写入之前判定，否则顺序会乱。

    ``db_factory`` 传 ``app.db.SessionLocal``：新线程必须有自己的会话，
    SQLAlchemy 的 Session 不是线程安全的。

    传入 ``signal``（correction / retry / positive / negative）时会走人格演化；
    不传则只写事实记忆。**信号识别交给调用方**是有意的：语音链路最清楚
    这一轮发生了什么（用户是否打断、是否重复提问），让它自己判断
    比在记忆模块里猜一遍更准。
    """
    # 1) 反馈先落库（要拿到 example_id，且必须早于本轮画像统计）
    if user_text and (signal or bot_text):
        try:
            with db_factory() as db:
                record_feedback(db, user_id, agent_id, user_text,
                                bot_text=bot_text, prev_bot_text=prev_bot_text,
                                hint=signal)
                db.commit()
        except Exception as exc:  # noqa: BLE001  反馈失败不影响记忆写入
            log.warning("记录反馈失败：%s: %s", type(exc).__name__, exc)

    # 2) 事实记忆：后台线程，不等它
    try:
        memorize_background(db_factory, user_id, agent_id, turns,
                            session_id=session_id, client=client)
    except Exception as exc:  # noqa: BLE001
        log.warning("启动后台记忆写入失败：%s: %s", type(exc).__name__, exc)


# ---------------------------------------------------------------- 接线示例
# voice 包落地后，在它的「拿到 ASR 结果 -> 调 LLM -> 合成 TTS」那一段里：
#
#   from ..memory.bridge import recall_for_reply, remember_after_reply
#   from ..db import SessionLocal
#
#   # 1) 说话前：注入上下文
#   with SessionLocal() as db:
#       memory_ctx = recall_for_reply(db, user_id, agent_id, asr_text,
#                                    session_id=sid, recent=4)
#   reply = await llm.chat(system_prompt=base_prompt + memory_ctx,
#                         user_text=asr_text)
#
#   # 2) 说完：异步沉淀
#   remember_after_reply(
#       SessionLocal, user_id, agent_id,
#       turns=[{"role": "user", "content": asr_text},
#               {"role": "assistant", "content": reply}],
#       session_id=sid, user_text=asr_text, bot_text=reply,
#       signal=detect_signal(interrupted=..., repeated=...),
#   )
#
# 注意 `memorize_background` 是 daemon 线程且不等待 flush：进程被 kill 时
# 最后一轮记忆会丢。对本产品可接受（下一轮会重新提到同样的事），
# 但真要保证不丢，得换成有界队列 + 工作线程池（见 service.py 的注释）。
