"""AI 模型能力分类表：把「笼统的一个 llm」拆成按用途分层的多个接入点。

为什么单独抽一个文件
--------------------
同一份 kind 清单要被四处用到，分散写死必然漂移（加一个 kind 要改四个地方，
漏一处就表现为「接口能存进去但查不出来」）：

  1. ``schemas.ProviderIn``   —— 入库前的取值合法性校验；
  2. ``routers/providers.py`` —— 列表过滤、导出、按用途解析；
  3. ``routers/device_api.py``—— 决定哪些配置下发到 ESP32；
  4. ``llm_gateway.py``       —— 真正的调用层，按用途挑落到哪条配置。

kind 怎么分的
------------
原来只有 asr / llm / tts / vad 四种，一个 ``llm`` 同时承担「陪老人聊天」和
「识别一句话是不是在设提醒」两种活。前者值得用贵的大模型，后者用便宜的小模型
就够了（延迟也更低），混在一起只能按最贵的一档付费。现在拆成：

  * ``llm_main``  大参数模型 —— 正式对话，质量优先；
  * ``llm_light`` 小参数模型 —— 意图识别、结构化抽取、提醒语义解析、任务执行；
  * ``embedding`` 向量模型   —— 只做文本嵌入（记忆检索用）；
  * ``rerank``    重排模型   —— 检索结果的二次打分（可选）；
  * ``vision``    视觉理解   —— 预留，看图说话 / 拍药盒识别。

旧的 ``llm`` 怎么办（平滑过渡）
------------------------------
**保留**，语义定为「默认 LLM」。原因：存量部署里已经有一批 kind=llm 的记录，
直接废弃会让升级上来的老系统瞬间失声——明明之前能聊，升级后报「请配置大模型」。

做法是给每个「用途（usage）」配一个**有序的 kind 候选链**：

    chat  -> (llm_main, llm)     # 没配 llm_main 就退回 llm
    light -> (llm_light, llm)    # 没配 llm_light 就退回 llm

上层只用 usage 说话（"我要 chat"），旧部署自动走到唯一的 llm 上继续工作；
新部署可以逐步把精细档配齐。``_LEGACY_LLM`` 这个常量就是为了让这段历史包袱
在代码里显式可见，而不是隐藏在调用层深处。
"""
from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------- kind 取值
KIND_ASR: Final = "asr"                  # 语音识别
KIND_TTS: Final = "tts"                  # 语音合成
KIND_VAD: Final = "vad"                  # 端点检测（判断一句话说完没有）
KIND_LLM: Final = "llm"                  # 【旧值】默认 LLM，保留作兜底
KIND_LLM_MAIN: Final = "llm_main"        # 大参数模型：正式对话
KIND_LLM_LIGHT: Final = "llm_light"      # 小参数模型：意图识别 / 抽取 / 提醒解析
KIND_EMBEDDING: Final = "embedding"      # 向量模型：文本嵌入
KIND_RERANK: Final = "rerank"            # 重排模型：检索结果二次打分
KIND_VISION: Final = "vision"            # 视觉理解（预留）

# 唯一的真值来源：入库校验、列表过滤、解析路由全部以它为准。
# 注意这里**不包含空字符串**——空 kind 另有含义（APP 自建的 agent 角色），
# 见 models.AIProvider 的类注释，别把它当成一种能力。
KIND_LIST: Final[tuple[str, ...]] = (
    KIND_ASR, KIND_TTS, KIND_VAD,
    KIND_LLM, KIND_LLM_MAIN, KIND_LLM_LIGHT,
    KIND_EMBEDDING, KIND_RERANK, KIND_VISION,
)

# 管理界面下拉框用的中文名
KIND_LABELS: Final[dict[str, str]] = {
    KIND_ASR: "语音识别 ASR",
    KIND_TTS: "语音合成 TTS",
    KIND_VAD: "端点检测 VAD",
    KIND_LLM: "大模型 LLM（默认/兜底）",
    KIND_LLM_MAIN: "大模型·主对话（大参数）",
    KIND_LLM_LIGHT: "小模型·识别与执行（小参数）",
    KIND_EMBEDDING: "向量模型 Embedding",
    KIND_RERANK: "重排模型 Rerank",
    KIND_VISION: "视觉理解 Vision（预留）",
}

# 只由**本后端自己调用**的能力：设备端（ESP32）与小智导出段都不认识这些 key，
# 把它们塞进下发包只会让固件拿到一堆无用条目。
SERVER_ONLY_KINDS: Final[tuple[str, ...]] = (
    KIND_LLM_MAIN, KIND_LLM_LIGHT, KIND_EMBEDDING, KIND_RERANK, KIND_VISION,
)

# 会下发到设备 / 导出给小智的四种经典能力。
# LLM 这一位只需一条：设备拿到的是「默认对话模型」，
# 具体是 llm_main 还是 llm，由 llm_gateway.resolve_row("chat") 决定。
DEVICE_KINDS: Final[tuple[str, ...]] = (KIND_ASR, KIND_LLM, KIND_TTS, KIND_VAD)
EXPORT_KINDS: Final[tuple[str, ...]] = DEVICE_KINDS


# ---------------------------------------------------------------- 用途 -> kind 候选链
# 上层（记忆提取、意图识别、正式对话）只说 usage，不知道背后配的是哪一家。
# 元组顺序即优先级：前面的 kind 一条都没启用，才轮到后面。
USAGE_CHAT: Final = "chat"        # 正式对话
USAGE_LIGHT: Final = "light"      # 轻量任务：意图识别 / 结构化抽取 / 提醒解析
USAGE_EMBED: Final = "embed"      # 文本向量化
USAGE_RERANK: Final = "rerank"    # 检索结果重排
USAGE_VISION: Final = "vision"    # 看图说话（预留）
USAGE_ASR: Final = "asr"
USAGE_TTS: Final = "tts"
USAGE_VAD: Final = "vad"

_LEGACY_LLM = KIND_LLM  # 上面「平滑过渡」里说的兜底值，单独起个名字便于检索

USAGE_ROUTES: Final[dict[str, tuple[str, ...]]] = {
    USAGE_CHAT: (KIND_LLM_MAIN, _LEGACY_LLM),
    USAGE_LIGHT: (KIND_LLM_LIGHT, _LEGACY_LLM),
    USAGE_EMBED: (KIND_EMBEDDING,),
    USAGE_RERANK: (KIND_RERANK,),
    USAGE_VISION: (KIND_VISION,),
    USAGE_ASR: (KIND_ASR,),
    USAGE_TTS: (KIND_TTS,),
    USAGE_VAD: (KIND_VAD,),
}

USAGE_LIST: Final[tuple[str, ...]] = tuple(USAGE_ROUTES.keys())

USAGE_LABELS: Final[dict[str, str]] = {
    USAGE_CHAT: "正式对话",
    USAGE_LIGHT: "轻量任务（意图识别 / 抽取 / 提醒解析）",
    USAGE_EMBED: "文本向量化",
    USAGE_RERANK: "检索结果重排",
    USAGE_VISION: "视觉理解",
    USAGE_ASR: "语音识别",
    USAGE_TTS: "语音合成",
    USAGE_VAD: "端点检测",
}


# ---------------------------------------------------------------- 厂商 API 风格
# OpenAI 的 /chat/completions 已经是事实标准：DeepSeek、通义、智谱、豆包、
# 硅基流动、Moonshot、本地 Ollama / vLLM / Xinference 都能直接说这套话。
# 但仍然有例外（Anthropic、Gemini 的报文形状完全不同），所以显式存一个风格位，
# 调用层据此切换线路协议，而不是靠猜 endpoint 后缀。
API_STYLE_OPENAI_COMPAT: Final = "openai_compatible"
API_STYLE_OPENAI: Final = "openai"
API_STYLE_ANTHROPIC: Final = "anthropic"
API_STYLE_GEMINI: Final = "gemini"
API_STYLE_ALIYUN: Final = "aliyun"
API_STYLE_VOLCENGINE: Final = "volcengine"
API_STYLE_OLLAMA: Final = "ollama"
API_STYLE_XINFERENCE: Final = "xinference"
API_STYLE_CUSTOM: Final = "custom"

DEFAULT_API_STYLE: Final = API_STYLE_OPENAI_COMPAT

API_STYLES: Final[dict[str, str]] = {
    API_STYLE_OPENAI_COMPAT: "OpenAI 兼容（默认）",
    API_STYLE_OPENAI: "OpenAI 官方",
    API_STYLE_ANTHROPIC: "Anthropic Claude",
    API_STYLE_GEMINI: "Google Gemini",
    API_STYLE_ALIYUN: "阿里云百炼（OpenAI 兼容端点）",
    API_STYLE_VOLCENGINE: "火山方舟（OpenAI 兼容端点）",
    API_STYLE_OLLAMA: "Ollama（本地）",
    API_STYLE_XINFERENCE: "Xinference（本地推理网关）",
    API_STYLE_CUSTOM: "自定义（OpenAI 兼容）",
}

# 风格 -> 线路协议。多个厂商共用一个协议是正常的：它们只是鉴权头或计费口径不同，
# 报文形状一致。留此映射是为了将来某家改协议时只动一行。
PROTOCOL_OPENAI: Final = "openai"
PROTOCOL_ANTHROPIC: Final = "anthropic"
PROTOCOL_GEMINI: Final = "gemini"
PROTOCOL_OLLAMA: Final = "ollama"

STYLE_PROTOCOL: Final[dict[str, str]] = {
    API_STYLE_OPENAI_COMPAT: PROTOCOL_OPENAI,
    API_STYLE_OPENAI: PROTOCOL_OPENAI,
    API_STYLE_ALIYUN: PROTOCOL_OPENAI,
    API_STYLE_VOLCENGINE: PROTOCOL_OPENAI,
    API_STYLE_XINFERENCE: PROTOCOL_OPENAI,
    API_STYLE_CUSTOM: PROTOCOL_OPENAI,
    API_STYLE_OLLAMA: PROTOCOL_OLLAMA,
    API_STYLE_ANTHROPIC: PROTOCOL_ANTHROPIC,
    API_STYLE_GEMINI: PROTOCOL_GEMINI,
}

# 哪些协议支持 /embeddings 与 /rerank。
# Anthropic 官方没有 embeddings 接口（要用得配 Voyage），Gemini 走的是
# :embedContent 另一套路径，先不实现，命中时给一句明确报错，好过返回一个空向量。
PROTOCOL_WITH_EMBEDDINGS: Final[tuple[str, ...]] = (PROTOCOL_OPENAI, PROTOCOL_OLLAMA)
PROTOCOL_WITH_RERANK: Final[tuple[str, ...]] = (PROTOCOL_OPENAI,)


def usage_kinds(usage: str) -> tuple[str, ...]:
    """取某个用途的 kind 候选链；未知用途返回空元组（由调用方决定报错口径）。"""
    return USAGE_ROUTES.get(usage, ())


def protocol_of(api_style: str | None) -> str:
    """由厂商风格得到线路协议，未登记的风格按 OpenAI 兼容处理。"""
    style = (api_style or "").strip() or DEFAULT_API_STYLE
    return STYLE_PROTOCOL.get(style, PROTOCOL_OPENAI)


def describe_kinds(usage: str) -> str:
    """给报错文案用：把候选链拼成人类可读的形式，如「llm_main 或 llm」。"""
    kinds = usage_kinds(usage)
    return " 或 ".join(kinds) if kinds else "（未知用途）"
