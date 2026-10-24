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

from typing import Final, NotRequired, TypedDict

from .model_catalog import (
    KIND_ASR,
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
    # ---- 以下只对「本地自建服务」预置有意义（2026-10-22 接入）----
    # 用 NotRequired 而不是另起一张表：云端预置一个都不写也合法，
    # 本地预置则带上「这个预置隐含的能力分类」——前端选中它时同步 kind，
    # 后端 _normalize_local_service 还会再强制一次（双保险，防前端漏改）。
    # 两边都读 model_catalog.LOCAL_SERVICE_DEFAULTS 的同源常量。
    kind: NotRequired[str]


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
        # 2026-10-23 横向实测（12 个候选模型）后改的两个默认值，都不是口味问题：
        #
        # ① llm_light 用**带日期版本** glm-4-flash-250414，不用别名 glm-4-flash。
        #    同一段含药名/摔跤/家属/喜好的老人对话，用真实 _EXTRACT_SYSTEM 跑：
        #    glm-4-flash-250414 抽取 4/4 全对、TTFT 611~622ms，两次结果一致；
        #    DeepSeek（现用）也 4/4 但总耗时 3.2~8.4s 抖动。抽取是后台任务，
        #    这种抖动纯属浪费钱。另外别名会随平台升级漂移，带日期版本才可复现。
        #
        # ② llm_main **绝不能**再填 glm-4-flash。免费档（glm-4-flash /
        #    glm-4-flash-250414 / glm-4.5-air）实测 5 次里 0 次真的调
        #    create_reminder，只会回「好的，记下了」—— 这正是本项目
        #    2026-10-23 07:12 那次「假成功」的形状：老人以为提醒设好了，
        #    库里什么都没有。药品提醒出这种事故是不可接受的，所以宁可慢。
        #    能稳定调工具的只有 glm-4.7（5/5，TTFT 3.2~8.9s）与
        #    glm-5.3（5/5，TTFT 2.0~5.3s）。这里取 glm-4.7：
        #    glm-5.x 是「始终思考」模型，连 thinking 都关不掉（传 disabled
        #    直接 400 code 1210），留作管理员手填的可选项。
        "models": {
            KIND_LLM_MAIN: "glm-4.7",
            KIND_LLM_LIGHT: "glm-4-flash-250414",
            KIND_EMBEDDING: "embedding-3",
        },
        "free_tier": "`glm-4-flash` / `glm-4-flash-250414` 有免费调用额度；"
                     "`glm-4.7` / `glm-5.3` 按量计费；向量 `embedding-3` 按量计费。"
                     "**免费的是「便宜」，不是「够用」** —— 免费档不能调工具。",
        "notes": "⚠️ ① endpoint 要填到 `/api/paas/v4` 这一级，"
                 "填 `bigmodel.cn` 首页必然失败"
                 " ② **免费档不能调工具**（glm-4-flash / glm-4-flash-250414 / "
                 "glm-4.5-air 实测 0/5）：它只会嘴上答应「记下了」，"
                 "不会真的建提醒。这类模型只能放在「小模型」档做记忆抽取，"
                 "**放主对话档等于让药品提醒静默失效**"
                 " ③ 能调工具的是 `glm-4.7` / `glm-5.3`，但首字 2~9 秒，"
                 "老人端体感明显比 DeepSeek 慢"
                 " ④ 模型名可手填，平台会不断出新版本（如 "
                 "`glm-4-flash-250414` 这种带日期后缀的才是可复现的固定版）",
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
    # ============================================================ 本地自建（Docker）
    # 为什么本地服务也要做预置：需求是「下拉选中本地 ASR / 本地向量化，
    # 其它什么都不填，点保存就能用」。没有预置条目，管理员就只能手填
    # api_style=local_asr —— 而那个下拉在「厂商 API 风格」里，正常人找不到。
    # 这两条的 endpoint 与 model_catalog.LOCAL_SERVICE_DEFAULTS 同源，
    # 后端保存时还会再归一化一次（见 routers/providers._normalize_local_service）。
    {
        "value": "funasr_local",
        "label": "本地 ASR（FunASR/SenseVoice · 免密钥）",
        "api_style": "local_asr",
        "endpoint": "http://127.0.0.1:8082",
        "needs_key": False,
        "key_hint": "本地服务没有密钥这一说 —— 选中直接保存即可。",
        "models": {KIND_ASR: "sensevoice-small"},
        "free_tier": "**零调用成本**：模型跑在本机（llama.cpp GGUF 版 SenseVoice），"
                     "代价是常驻内存与 CPU，且请求串行执行。",
        "notes": "⚠️ ① 选中后能力会自动锁定为「语音识别 ASR」"
                 "② 「模型」栏只是展示名，真正识别的模型由服务端加载的 gguf 决定"
                 "③ 助手后端与模型不在同一 Docker 网络时，把接口地址改成宿主地址"
                 "（如 http://10.2.0.16:8082）。",
        "kind": KIND_ASR,
    },
    {
        "value": "tencent_asr",
        "label": "腾讯云语音识别 ASR（一句话识别 · 国内直连）",
        "api_style": "tencent",
        "endpoint": "https://asr.tencentcloudapi.com",
        "needs_key": True,
        "key_hint": "填 **SecretId:SecretKey**，用**英文冒号**分隔"
                    "（例：AKIDxxxx:xxxxxx）。这是访问密钥的两段，"
                    "不是 API 密钥单串。",
        # 「模型」在本厂商里存的是**引擎类型**（EngSerViceType），
        # 本平台音频固定 16kHz ⇒ 只列 16k_* 系列。选 8k_* 的后果是
        # 「能识别但全是乱码」——接口照样 200，最难自查。
        "models": {KIND_ASR: "16k_zh"},
        "free_tier": "新用户每月有免费额度（以控制台「语音识别」页面为准）；"
                     "之后按小时计费。**非永久免费**。",
        "notes": "⚠️ ① 引擎只选 **16k_** 开头（本平台音频是 16kHz，"
                 "选 8k_ 会把音频按半速解释，结果全是乱字）"
                 "② 老人说方言时选 `16k_zh_dialect`（支持 23 种方言）"
                 "③ 单段上限 60s / 3MB，超了由服务端提前拦截并报错"
                 " ④ 一句话识别不保留音频，适合隐私敏感场景",
        "kind": KIND_ASR,
    },
    {
        "value": "fastembed_local",
        "label": "本地向量化（FastEmbed/bge · 免密钥）",
        "api_style": "local_embed",
        "endpoint": "http://127.0.0.1:8080",
        "needs_key": False,
        "key_hint": "本地服务没有密钥这一说 —— 选中直接保存即可。",
        "models": {KIND_EMBEDDING: "bge-small-zh-v1.5"},
        "free_tier": "**零调用成本**：FastEmbed（ONNX Runtime，CPU），"
                     "512 维且已 L2 归一化（点积即余弦相似度），"
                     "常驻约 222MB、单条约 30ms。",
        "notes": "⚠️ ① 选中后能力会自动锁定为「向量模型 Embedding」"
                 "② 助手后端与模型不在同一 Docker 网络时，把接口地址改成宿主地址"
                 "（如 http://10.2.0.16:8080）"
                 "③ 换不同维度的模型后，旧记忆的语义路会自动失效（维度不符不参与计算），"
                 "需要重新入库。",
        "kind": KIND_EMBEDDING,
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
