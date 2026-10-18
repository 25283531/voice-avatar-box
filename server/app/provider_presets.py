"""非 TTS 厂商预置：LLM / 向量 / 重排的「下拉 + 自动填表」。

为什么不复用 voice/catalog.py 的 VENDOR_CATALOG
----------------------------------------------
``voice/catalog.py`` 的模块说明里已经写死这条纪律：**那张表只覆盖 TTS**。
差别不是洁癖，是字段集根本不同 —— TTS 要音色、要 AK:SK 两段密钥、要
``api_style=edge/volcengine`` 这种私有协议；LLM 这边只要一个 OpenAI 兼容
的 endpoint + 一段 key + 一个模型名。把两族塞进一张表，最终必然出现
「一半条目没有 voices 字段」的空壳，TTS 那侧的渲染逻辑要到处判空。

所以这里另起一张表，**只管 OpenAI 兼容这一族**（事实上 DeepSeek / 智谱 /
硅基流动 / Moonshot / 百炼 / OpenAI 官方全都是这一族），差异压缩到三个字段：
endpoint、key 怎么填、有哪些值得用的模型。

为什么值得做成预置，而不是让人手填 endpoint
-------------------------------------------
本项目已在 TTS 上吃过一模一样的亏：管理员把 endpoint 填成官网首页，服务端
报的是「返回不是合法 JSON」，完全指不到真因。LLM 侧更危险，因为**填错
endpoint 有时不会立刻报错** —— 少写了 ``/v1`` 时，某些网关会返回 HTML 首页，
解析后才炸；更糟的是把 ``https://api.siliconflow.cn/v1/chat/completions``
整条粘进 endpoint 字段，于是拼出来是 ``.../chat/completions/chat/completions``，
同样是「连不上」而非「填错了」。

为什么模型也要预置
------------------
「免费」这件事是**按模型逐个**变化的，不是厂商级属性。硅基流动 9B 以下
模型永久免费、bge 系列向量化与重排免费，但换个模型就要重新查文档；
让管理员自己凭记忆填模型名，最可能的结果是填了个收费模型而毫不知情。
因此本表的 ``models`` 按 kind 给出「已核实免费/性价比」的候选，并把
``free_tier`` 的口径写进人能读懂的句子，而不是一个「免费」标签。

⚠️ 单一真源纪律
--------------
本表是**唯一**记录「某厂商某个模型名」的 Python 位置。文档（README、
免费语音服务商选型.md）里出现的模型名如果与本表冲突，以本表为准；
新增条目时顺手核一遍文档。
"""
from __future__ import annotations

from typing import Final, TypedDict

from .model_catalog import (
    KIND_EMBEDDING,
    KIND_LLM_LIGHT,
    KIND_LLM_MAIN,
    KIND_RERANK,
)


class ProviderPreset(TypedDict):
    value: str              # 存进 ai_providers.provider 的值（不改已有取值口径）
    label: str              # 下拉显示
    api_style: str          # 厂商 API 风格（见 model_catalog.API_STYLES）
    endpoint: str           # 预填 endpoint，**必须精确到 /v1 这一级**
    needs_key: bool
    key_hint: str           # 密钥怎么填（含格式与常见坑）
    models: dict[str, str]  # kind -> 推荐模型名（前端按当前 kind 取）
    free_tier: str          # 费用口径（用户最关心的实际成本）
    notes: str              # 坑


PROVIDER_PRESETS: Final[tuple[ProviderPreset, ...]] = (
    {
        "value": "siliconflow",
        "label": "硅基流动 SiliconFlow（OpenAI 兼容 · 向量与重排永久免费）",
        "api_style": "openai_compatible",
        "endpoint": "https://api.siliconflow.cn/v1",
        "needs_key": True,
        "key_hint": "到硅基流动控制台「API Key」页新建，填 `sk-` 开头的单段密钥。"
                    "**不要填账号密码，也不要把整条接口地址粘进「接口地址」栏**。",
        "models": {
            KIND_EMBEDDING: "BAAI/bge-m3",
            KIND_RERANK: "BAAI/bge-reranker-v2-m3",
            KIND_LLM_MAIN: "Qwen/Qwen2.5-7B-Instruct",
            KIND_LLM_LIGHT: "Qwen/Qwen2.5-1.5B-Instruct",
        },
        "free_tier": "**9B 及以下模型永久免费**（含 bge-m3 向量化与 "
                     "bge-reranker-v2-m3 重排、Qwen2.5-7B/1.5B）。"
                     "这是本项目记忆检索（向量+重排）零成本落地的首选。",
        "notes": "⚠️ ① 向量化与重排是**两行独立配置**（能力选 embedding / rerank），"
                 "不是一行两个模型 ② 重排端点是 `/v1/rerank`，"
                 "本项目已按此实现，无需改代码 ③ 换更大的模型就会开始计费。",
    },
    {
        "value": "deepseek",
        "label": "DeepSeek（OpenAI 兼容 · 便宜）",
        "api_style": "openai_compatible",
        "endpoint": "https://api.deepseek.com/v1",
        "needs_key": True,
        "key_hint": "填 `sk-` 开头的单段 API Key。",
        "models": {
            KIND_LLM_MAIN: "deepseek-chat",
            KIND_LLM_LIGHT: "deepseek-chat",
        },
        "free_tier": "**无免费额度**，按 token 计费，单价在国产大模型里最低之一。",
        "notes": "⚠️ 不提供向量化与重排模型 —— 记忆检索仍需另配一家（如硅基流动）。",
    },
    {
        "value": "zhipu",
        "label": "智谱 GLM（OpenAI 兼容 · 有免费档）",
        "api_style": "openai_compatible",
        "endpoint": "https://open.bigmodel.cn/api/paas/v4",
        "needs_key": True,
        "key_hint": "填智谱开放平台生成的 API Key（单段）。",
        "models": {
            KIND_LLM_MAIN: "glm-4-flash",
            KIND_LLM_LIGHT: "glm-4-flash",
            KIND_EMBEDDING: "embedding-3",
        },
        "free_tier": "GLM-4-Flash 有免费调用额度；向量模型按量计费。",
        "notes": "⚠️ endpoint 要填到 `/api/paas/v4` 这一级，"
                 "填 `bigmodel.cn` 首页必然失败。",
    },
    {
        "value": "aliyun",
        "label": "阿里云百炼 qwen（OpenAI 兼容 · 送额度）",
        "api_style": "aliyun",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "needs_key": True,
        "key_hint": "填百炼的 API Key（`sk-` 开头，单段）。"
                    "**不要填阿里云主账号 AK/SK**，那是另一套鉴权。",
        "models": {
            KIND_LLM_MAIN: "qwen-plus",
            KIND_LLM_LIGHT: "qwen-turbo",
            KIND_EMBEDDING: "text-embedding-v3",
        },
        "free_tier": "新用户有赠送额度，用完后按量。",
        "notes": "⚠️ 能力选 `aliyun` 时 endpoint 必须走 `compatible-mode/v1`。",
    },
    {
        "value": "openai",
        "label": "OpenAI 官方",
        "api_style": "openai",
        "endpoint": "https://api.openai.com/v1",
        "needs_key": True,
        "key_hint": "填 `sk-` 开头的 OpenAI API Key。",
        "models": {
            KIND_LLM_MAIN: "gpt-4o-mini",
            KIND_LLM_LIGHT: "gpt-4o-mini",
            KIND_EMBEDDING: "text-embedding-3-small",
        },
        "free_tier": "**无免费额度**，需绑卡。",
        "notes": "⚠️ 国内直连不稳定；本项目不为其做特殊处理，"
                 "连不上先查网络再查 key。",
    },
)

PROVIDER_PRESET_VALUES: Final[tuple[str, ...]] = tuple(
    p["value"] for p in PROVIDER_PRESETS)


def preset_entry(value: str) -> ProviderPreset | None:
    """按 provider 值取预置。未知返回 None（调用方据此走通用手填路径）。"""
    return next((p for p in PROVIDER_PRESETS
                 if p["value"] == (value or "").strip()), None)


def presets_payload() -> dict:
    """给 ``GET /api/providers/catalog`` 用的载荷（与 TTS 的 vendors_payload 同形）。"""
    return {"provider_presets": [dict(p) for p in PROVIDER_PRESETS]}
