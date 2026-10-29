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

  * ``llm_main``  大参数模型 —— 正式对话，质量优先；**工具调用也只走这里**；
  * ``llm_light`` 小参数模型 —— 目前**只用于记忆事实抽取**（见下）;
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

⚠️ ``llm_light`` 的真实职责（2026-10-23 全仓核查后订正）
--------------------------------------------------------
上面那句「意图识别 / 结构化抽取 / 提醒语义解析 / 任务执行」是**设计意图**，
代码里**只实现了「结构化抽取」一项**：全仓 ``USAGE_LIGHT`` 的唯一调用点是
``memory/extract.py:294``（记忆事实抽取，不带 tools）。

也就是说 —— **提醒语义解析、任务执行、工具调用全部走 ``llm_main``**
（``voice/session.py`` 的 ``_chat_round`` 用 ``usage="chat"`` 且带 TOOL_DEFS）。
按注释想当然会踩两个坑：
  1. 以为给 ``llm_light`` 换个便宜模型就能省下任务执行的钱 —— 省不到；
  2. 以为 ``llm_light`` 里的模型「能调工具」—— 抽取场景根本不发 tools，
     所以「工具调用 0/5」的模型放在这一档完全没问题（glm-4-flash-250414
     就是典型：当小模型 4/4 全对，当主模型 0/5 全假成功）。
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
USAGE_LIGHT: Final = "light"      # 轻量任务：目前只有记忆事实抽取（不带 tools）
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
    USAGE_LIGHT: "轻量任务（记忆抽取等，不带工具）",
    USAGE_EMBED: "文本向量化",
    USAGE_RERANK: "检索结果重排",
    USAGE_VISION: "视觉理解",
    USAGE_ASR: "语音识别",
    USAGE_TTS: "语音合成",
    USAGE_VAD: "端点检测",
}

# ---------------------------------------------------------------- 用途 -> 模型类别（给用户看）
# 为什么单独一张表，不复用上面的 USAGE_LABELS
# ------------------------------------------
# ``USAGE_LABELS`` 是**管理端**口径：管理员要看得出「chat 与 light 的区别
# 是带不带工具」，所以文案里带括号解释。而这两个字串会**直接显示给家属**，
# 「轻量任务（记忆抽取等，不带工具）」出现在老人子女的手机上就是天书。
#
# 用户端还多一条硬约束：**不显示具体模型名**（DeepSeek-V4-Flash / glm-4-flash
# / BAAI-bge-large-zh 这些对家属没有意义，还会暴露平台采购细节）。所以用户端
# 只显示「类别」这一层抽象，类别名必须自解释、不带厂商与技术术语。
#
# ⚠️ 这是**唯一真源**：``models.ModelUsage.kind_label``（property）与
# ``schemas.UsageRowOut.kind_label`` 都从这里取，前端不许再抄一份映射
# —— 抄一份的下场是加一个新 usage 时，管理端显示"正式对话"、用户端显示空白。
USAGE_KIND_LABELS: Final[dict[str, str]] = {
    USAGE_CHAT: "正式对话模型",
    USAGE_LIGHT: "轻量任务模型",
    USAGE_EMBED: "向量化模型",
    USAGE_RERANK: "重排模型",
    USAGE_VISION: "视觉模型",
    USAGE_ASR: "语音识别",
    USAGE_TTS: "语音合成",
    USAGE_VAD: "端点检测",
}

#: 空 usage 的类别名。历史库里可能有 usage 为空串的行（早期记账没有 usage
#: 参数），显示「其他」比显示一个空白单元格好 —— 空白会被当成渲染 bug。
USAGE_KIND_UNKNOWN: Final = "其他"


def usage_kind_label(usage: str) -> str:
    """用途 -> 用户端可显示的「模型类别」。

    未知 usage 原样返回：将来新增 usage 而忘了登记时，界面上会显示
    ``foo`` 这种原始值 —— **能被发现**，而不是悄悄显示成「其他」再也
    没人注意。空串返回「其他」（见 ``USAGE_KIND_UNKNOWN``）。
    """
    key = (usage or "").strip()
    if not key:
        return USAGE_KIND_UNKNOWN
    return USAGE_KIND_LABELS.get(key, key)


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
# 本机自建的两种服务（2026-10-22 接入）：FunASR/SenseVoice 语音识别与
# FastEmbed/bge 向量化。它们**不是** OpenAI 兼容（报文形状完全不同），
# 也**不需要**密钥与模型名，所以单独立风格而不是复用 localhost 的
# openai_compatible —— 后者会逼管理员填一个用不上的 model，且调用层
# 会按 /audio/transcriptions 拼路径，连不上还查不出原因。
API_STYLE_LOCAL_ASR: Final = "local_asr"
API_STYLE_LOCAL_EMBED: Final = "local_embed"

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
    API_STYLE_LOCAL_ASR: "本地 ASR（FunASR/SenseVoice，无需密钥）",
    API_STYLE_LOCAL_EMBED: "本地向量化（FastEmbed/bge，无需密钥）",
}

# ---------------------------------------------------------------- 本机自建服务
# 为什么这些默认值必须集中在这里，而不是散落在路由/前端各写一份
# ------------------------------------------------------------------
# 需求是「在下拉里选中本地 ASR / 本地向量化，**其它什么都不填，点保存就能用**」。
# 要满足它，kind、endpoint、超时必须有一个地方能在保存时自动补齐 ——
# 三个消费方（路由归一化、前端自动填表、文档）读的是同一份常量，
# 才不会出现「接口存得进但调不通」或「前端说不用填、后端说缺 endpoint」。
#
# endpoint 默认取 127.0.0.1 是因为这两个服务与本品端通常同机部署
# （systemd 常驻，见 docs/DEPLOY.md §1.12）。Docker 部署时若不在同一
# network namespace，管理员把它改成宿主地址（如 10.2.0.16）即可 ——
# 字段仍然可编辑，只是不再是必填。
LOCAL_SERVICE_DEFAULTS: Final[dict[str, dict[str, str]]] = {
    API_STYLE_LOCAL_ASR: {
        "kind": KIND_ASR,
        "provider": "funasr_local",
        "endpoint": "http://127.0.0.1:8082",
        # 仅作展示用：FunASR 服务端按加载的模型识别，不看这个字段
        "model": "sensevoice-small",
        "label": "本地 ASR（FunASR/SenseVoice）",
    },
    API_STYLE_LOCAL_EMBED: {
        "kind": KIND_EMBEDDING,
        "provider": "fastembed_local",
        "endpoint": "http://127.0.0.1:8080",
        "model": "bge-small-zh-v1.5",
        "label": "本地向量化（FastEmbed/bge）",
    },
}

#: 这两种风格的隐含能力分类。保存时按它强制 kind，避免"选了本地 ASR 却落在
#: llm_main 上"——那种配置存得进去、用的时候才报 no_config。
LOCAL_STYLE_KIND: Final[dict[str, str]] = {
    style: meta["kind"] for style, meta in LOCAL_SERVICE_DEFAULTS.items()
}

#: 本地服务的默认超时（秒）。GGUF 版 FunASR 冷启动约 2.8s、稳态 ~1s，
#: 请求串行执行，高并发会排队 —— 比云端 ASR 宽一倍，宁可等也别误判超时。
LOCAL_ASR_TIMEOUT_SEC: Final[int] = 60
LOCAL_EMBED_TIMEOUT_SEC: Final[int] = 30

# 风格 -> 线路协议。多个厂商共用一个协议是正常的：它们只是鉴权头或计费口径不同，
# 报文形状一致。留此映射是为了将来某家改协议时只动一行。
PROTOCOL_OPENAI: Final = "openai"
PROTOCOL_ANTHROPIC: Final = "anthropic"
PROTOCOL_GEMINI: Final = "gemini"
PROTOCOL_OLLAMA: Final = "ollama"
# 本机自建服务的私有协议。不复用 openai：报文形状（/asr 的 multipart、
# /embed 的 input/texts）与鉴权（无密钥）都不同，混在一起会让调用层
# 到处判 style，正是本项目反复吃过的亏。
PROTOCOL_LOCAL_ASR: Final = "local_asr"
PROTOCOL_LOCAL_EMBED: Final = "local_embed"

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
    API_STYLE_LOCAL_ASR: PROTOCOL_LOCAL_ASR,
    API_STYLE_LOCAL_EMBED: PROTOCOL_LOCAL_EMBED,
}

# 哪些协议支持 /embeddings 与 /rerank。
# Anthropic 官方没有 embeddings 接口（要用得配 Voyage），Gemini 走的是
# :embedContent 另一套路径，先不实现，命中时给一句明确报错，好过返回一个空向量。
PROTOCOL_WITH_EMBEDDINGS: Final[tuple[str, ...]] = (
    PROTOCOL_OPENAI, PROTOCOL_OLLAMA, PROTOCOL_LOCAL_EMBED,
)
PROTOCOL_WITH_RERANK: Final[tuple[str, ...]] = (PROTOCOL_OPENAI,)


def protocol_of_style(api_style: str | None) -> str:
    """与 :func:`protocol_of` 同义，但**只认显式风格**（不回落默认）。

    给「保存时归一化」这类需要区分"用户到底选没选本地服务"的场合用：
    空风格要按 OpenAI 兼容处理，而这里必须返回空串让调用方自己决定。
    """
    style = (api_style or "").strip()
    if not style:
        return ""
    return STYLE_PROTOCOL.get(style, PROTOCOL_OPENAI)


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
