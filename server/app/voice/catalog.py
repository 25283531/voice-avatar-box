"""TTS 音色目录 —— 运营后台音色下拉框的**单一真源**。

为什么要有这个模块
------------------
音色标识符（如 ``zh-CN-XiaoxiaoNeural``）是**厂商侧的名字**，不是我们能编的
字符串。填错的后果是静默失败：厂商收到不认识的 voice，要么报错、要么悄悄
用它的默认音色 —— 后者更糟，因为管理员以为换成功了，老人听出来还是同一个人。

所以音色选择框不能是空白输入框，必须给出一份**确认过存在的**清单，让人
从里挑；挑不动时才允许手填（自建 TTS 服务的音色名不在任何官方清单里）。

为什么这个目录放在服务端而不是 index.html 里
--------------------------------------------
出厂默认音色（``DEFAULT_PRESET_TTS_VOICE``）的真源在``app.persona.catalog``
（见 main.py 种子处注释：「运营后台的恢复出厂默认需要单一真源」，且
``admin_presets.py`` 的默认值比对也读那一份）。若前端再抄一份，就是第二份
真源 —— 迟早出现「下拉框里选得到的音色，恢复默认后却是另一个」。
前端因此改为 ``GET /api/providers/catalog`` 拉取。

⚠️ **本模块曾定义过一份同值的 ``DEFAULT_PRESET_TTS_VOICE``，是无人使用的副本**
（所有消费者都从 ``app.persona.catalog`` 取）。两份同名常量迟早会分叉，
故已删除，只保留 ``persona.catalog`` 一处。改动请先看
``docs/决策记录.md`` 里「音色目录单一真源」那条。

预置的是哪一组
--------------
Edge TTS（微软）官方音色。它不需要自建服务、免费，
与项目现有默认技术栈一致。**不混入本地开源 TTS
（CosyVoice / GPT-SoVITS 等）的音色名**：那些填进去在当前部署下必然调用
失败，混在一起会让管理员误以为「从下拉框里选就能用」。

选型依据与完整对比见 ``docs/免费语音服务商选型.md`` §2 / §5。

为什么给**用户端**的清单必须按「当前生效的那一家」算
--------------------------------------------------
上面的 ``catalog_payload()`` 是给**管理端**的：管理员正在挑厂商，所以要看到
全部四家的音色（``vendors_payload`` 按厂商分组）。用户端完全不同 —— 用户改的是
**角色**，而角色只有一个 ``tts_voice`` 字段，它最终会被 ``pick_voice()`` 原样
塞进**当前生效的那一条** TTS 配置的请求里。

而音色 ID **不跨厂商通用**：

* Edge 是 BCP-47 名（``zh-CN-XiaoxiaoNeural``）
* 腾讯云是**纯整数**（``601009``）
* 火山是 ``BV001_streaming`` 这种带后缀的串
* 百炼是 ``Cherry`` 这种英文名

跨厂商混用的后果**不是报错可见，而是静默失效**：``asr_tts._shape_ok_voice()``
发现形态与目标厂商不符时会**置空**（宁可让厂商用自己的默认音色，也不能整句
没声音）。于是用户在下拉框里选了「晓晓」，保存成功、页面显示「晓晓」，
实际听到的却一直是厂商默认音色 —— 生产上真实发生过：生效的是腾讯云，
角色里存的却是 ``zh-CN-XiaoxiaoNeural``，那个音色**从来没生效过**。

所以用户端的音色下拉必须满足两条：
  1. **只列出生效厂商的音色** —— 让用户选到的每一个都真的能生效；
  2. **存的旧值若已不在清单里，就回落到该厂商默认** —— 换过厂商的存量数据
     不该继续躺着一个永远不生效的值（回落要留日志，不能静默）。

:func:`options_for` 就是这两条的单一真源，``GET /api/providers/voice-options``
与 ``routers/roles._clamp_voice`` 都读它 —— 分开写两份判据，迟早出现
「下拉里选得到、保存时被改掉」的自相矛盾。
"""

from __future__ import annotations

from typing import Final, TypedDict


class VoiceEntry(TypedDict):
    """一条音色。字段名与前端模板一一对应，不要随意改名。"""

    id: str      # 音色标识符，真正发给厂商的 value
    name: str    # 中文名，给人看的
    gender: str  # 男/女/中性，用于分组排序
    scene: str   # 适合什么场景（老人场景下这句比"音色好不好听"更重要）
    note: str    # 补充说明，尤其是「不适合老人」这类反向提示


# 音色目录。顺序即前端下拉框的展示顺序（已按老人场景友好度排好序）。
#
# 排序原则：**温和的男声/女声在前，活泼/少年腔在后**。本项目服务对象是老人，
# 「年轻活泼」类音色虽然更清晰，但语调偏轻快，老人容易听成小孩在说话，
# 反而降低信任感。Yunyang（新闻播报腔）虽不属于 Neural 全家桶里最柔和的，
# 但咬字最清楚，所以在老人场景里反而是可用选项，放在 Xiaoxiao 之后。
VOICE_CATALOG: Final[tuple[VoiceEntry, ...]] = (
    {
        "id": "zh-CN-XiaoxiaoNeural",
        "name": "晓晓（女声· 温柔亲和）",
        "gender": "女",
        "scene": "日常陪伴、闲聊、提醒播报 —— 首选",
        "note": "微软最柔和的女声，长时间听不累。本项目出厂默认音色。",
    },
    {
        "id": "zh-CN-XiaoyiNeural",
        "name": "晓伊（女声· 活泼年轻）",
        "gender": "女",
        "scene": "需要更明快的语气时",
        "note": "比晓晓更年轻、更有活力。老人听着可能偏「年轻女性」。",
    },
    {
        "id": "zh-CN-YunyangNeural",
        "name": "云扬（男声· 播报腔，咬字最清楚）",
        "gender": "男",
        "scene": "服药提醒等**必须听清**的播报",
        "note": "新闻播报腔，咬字最清楚。听感偏正式，但信息传达最可靠。",
    },
    {
        "id": "zh-CN-YunjianNeural",
        "name": "云健（男声· 浑厚有力）",
        "gender": "男",
        "scene": "需要明确可靠的男性播报者形象",
        "note": "偏体育解说腔，音色厚。老人听着够沉稳。",
    },
    {
        "id": "zh-CN-YunxiNeural",
        "name": "云希（男声· 阳光清亮）",
        "gender": "男",
        "scene": "日常对话的男性角色",
        "note": "偏年轻男声，可能显年轻。",
    },
    {
        "id": "zh-CN-YunxiaNeural",
        "name": "云夏（男声· 年长温和）",
        "gender": "男",
        "scene": "**拟子女/孙辈陪护角色**",
        "note": "年龄感最接近中老年男声，老人听着更自然。",
    },
    {
        "id": "zh-CN-YunfengNeural",
        "name": "云枫（男声 · 情感充沛）",
        "gender": "男",
        "scene": "需要表达力、不适合安静场景",
        "note": "情感色彩很重，语气起伏大，老人陪伴场景一般不建议。",
    },
    {
        "id": "zh-CN-XiaochenNeural",
        "name": "晓辰（女声 · 知性沉稳）",
        "gender": "女",
        "scene": "需要可靠、冷静的女性角色",
        "note": "偏成熟女声，适合讲解类内容。",
    },
    {
        "id": "zh-CN-XiaohanNeural",
        "name": "晓涵（女声 · 温暖亲切）",
        "gender": "女",
        "scene": "日常陪伴的备选",
        "note": "温暖亲切，气质与晓晓接近但更收敛。",
    },
    {
        "id": "zh-TW-HsiaoChenNeural",
        "name": "曉臻（台湾女声 · 繁体）",
        "gender": "女",
        "scene": "**只**在需要台湾用语/繁体时用",
        "note": "台湾国语，**用词与大陆不同**（如「计算机」）。大陆老人慎用。",
    },
    {
        "id": "zh-TW-HsiaoYuNeural",
        "name": "曉雨（台湾女声 · 繁体）",
        "gender": "女",
        "scene": "同上",
        "note": "同上，台湾国语用词与大陆不同。",
    },
)

VOICE_IDS: Final[tuple[str, ...]] = tuple(v["id"] for v in VOICE_CATALOG)

# 火山音色清单**不在这里重新抄一份** —— 它住在 tts_volc.VOLC_VOICES
# （跟协议实现放在一起，改音色 ID 时不会漏改前端）。
# 延迟导入避免循环依赖（tts_volc 不 import catalog，反向 import 是安全的，
# 但放在模块顶部会让「只想读音色目录」的调用方被迫拉起 urllib 依赖链）。
def _volc_voices() -> tuple[dict, ...]:
    from .tts_volc import VOLC_VOICES
    return VOLC_VOICES


# 腾讯云音色同理：清单住在 ``tts_tencent.TENCENT_VOICES``，本模块只转出。
# ⚠️ 腾讯云的音色 ID 是**整数**（如 1001），但本表统一用字符串承载 ——
# ai_providers.voice 是文本列，前端下拉也按字符串传。转换发生在
# ``tts_tencent._voice_type``，不要在本表里塞 int（会与其它厂商的行不同型）。
def _tencent_voices() -> tuple[dict, ...]:
    from .tts_tencent import TENCENT_VOICES
    return TENCENT_VOICES

# 出厂默认音色：⚠️ **这里只做转出，不做定义**。
#
# 真源在 ``app.persona.catalog.DEFAULT_PRESET_TTS_VOICE`` —— main.py 的种子写入、
# admin_presets.py 的默认值比对、smoke_admin_presets.py 的断言都读那一份。
# 本模块此前自己定义过一个同值副本，是**无人使用的第二份真源**（已删除）。
# 保留这个转出名只为兼容既有 import，不要在这里改值。
#
# ⚠️ 改这个值等于给所有已有部署的默认音色换人，且**不会触发任何迁移**
#（已在库里的行仍是旧值）。真要换，必须同时写迁移。
from ..persona.catalog import DEFAULT_PRESET_TTS_VOICE  # noqa: F401


def voice_known(voice_id: str) -> bool:
    """该音色是否在预置目录内。

    用于**区分两种失败**，这点很重要：

    - 预置内 + 调用失败  => 音色名没错，是 endpoint/key/网络的问题
    - 预置外 + 调用失败  => 十有八九是音色名写错了（自建服务另说）

    两者排查方向完全不同，所以必须能区分，不能笼统报「TTS 失败」。
    """
    return (voice_id or "").strip() in VOICE_IDS


def catalog_payload() -> dict:
    """给 ``GET /api/voice-catalog`` 的返回体。"""
    return {
        "voices": [dict(v) for v in VOICE_CATALOG],
        "default_voice": DEFAULT_PRESET_TTS_VOICE,
        # 明确告诉前端「允许手填」这件事本身，避免有人为图省事
        # 把 input 改成 select —— 那会让自建 TTS 服务的音色无法使用。
        "allow_custom": True,
        "custom_hint": (
            "下拉框里是 Edge TTS 官方音色（在线、免费、无需自建）。"
            "若你自建了 TTS 服务（CosyVoice / GPT-SoVITS 等），"
            "请直接把该服务的音色名手填进来 —— 各家命名不统一，无法预置。"
        ),
        "custom_mismatch_hint": (
            "填了不在预置清单里的音色名不一定是错的："
            "只要你的 TTS 服务认识它就能用。预置清单只用于降低手打错字的概率。"
        ),
    }


# ============================================================================
# TTS 厂商预置
# ============================================================================
#
# 为什么厂商也要做成「下拉 + 自动填表」而不是让人手填 endpoint
# -------------------------------------------------------------
# 三家要填的东西完全不同：
#   Edge     —— **什么都不用填**。没有 endpoint、没有 key、没有 model
#   火山     —— endpoint 有官方默认值，但密钥是 AK+SK 两段（不是一段 key）
#   阿里百炼 —— endpoint 要到控制台复制粘贴，key 一段
#
# 让人手填的实际后果（本项目已吃过亏）：管理员把「阿里云百炼」的 endpoint
# 填成官网首页、或把火山的 AK:SK 只填一半，服务端报的是「返回不是合法 JSON」
# 或「连接被拒绝」，**完全指不到真因**。下拉 + 自动填表把这件事消灭在配置阶段。
#
# ⚠️ 本表只覆盖 **TTS**。ASR/LLM 的厂商预置是另一张表、另一套字段，
# 不要因为「看起来对称」就顺手加进来 —— 那会把本模块变成大杂烩。

class VendorEntry(TypedDict):
    value: str         # 存进 ai_providers.provider 的值
    label: str         # 下拉显示
    api_style: str     # 决定服务端走哪条实现（见 asr_tts._tts_dispatch）
    endpoint: str      # 预填的 endpoint（Edge 为空串 = 不需要）
    model: str         # 预填的 model
    needs_key: bool    # 表单是否显示「密钥」输入框
    key_hint: str      # 该厂商密钥字段的填写说明（含格式）
    needs_voice: bool  # 是否需要音色选择
    voices: list       # 该厂商的预置音色（为空表示手填）
    free_tier: str     # 免费额度口径（用户最关心的实际成本）
    notes: str         # 坑


VENDOR_CATALOG: Final[tuple[VendorEntry, ...]] = (
    {
        "value": "edge",
        "label": "Edge TTS（微软 · 免费 · 免密钥）",
        "api_style": "edge",
        # ⚠️ endpoint 刻意留空：Edge 不是 HTTP API，走内置桥
        # （tts_edge.py），填了 endpoint 反而会让人以为该填什么。
        "endpoint": "",
        "model": "",
        "needs_key": False,
        "key_hint": "**不需要密钥**。Edge TTS 是微软 Edge 浏览器的公开语音服务，"
                    "无需账号。选择后只要点保存即可生效。",
        "needs_voice": True,
        "voices": [dict(v) for v in VOICE_CATALOG],
        "free_tier": "永久免费",
        "notes": "⚠️ 无商业授权，且是微软非公开接口 —— 实测高频调用会被限流"
                 "（连续 3 次第 3 次连不上）。建议作为零成本兜底，"
                 "正式商用请另配一家。",
    },
    {
        "value": "volcengine",
        "label": "火山引擎 / 豆包语音（中文质量最好 · 音色按年计费）",
        "api_style": "volcengine",
        "endpoint": "https://openspeech.bytedance.com",
        "model": "volc.tts_async",
        "needs_key": True,
        "key_hint": "填 **AK:SK**，用**英文冒号**分隔"
                    "（例：AKLTxxxx:WGRrxxxx）。只填一段会 403。",
        "needs_voice": True,
        "voices": [dict(v) for v in _volc_voices()],
        "free_tier": "新用户试用额度（约 20000 字符 / 半年），**非永久免费**。"
                     "正式版公版音色按量约 6.5 元/万字符起；"
                     "付费专属音色约 150 元/年/音色。",
        "notes": "⚠️ ① 付费音色需在控制台单独开通，填了没权限会报错"
                 " ② 下面预置的都是**公版免费音色**"
                 " ③ 密钥是 AK/SK 两段，别当成一个 key",
    },
    {
        "value": "tencent",
        "label": "腾讯云语音合成 TTS（国内直连 · 支持多情感音色）",
        "api_style": "tencent",
        # 腾讯云的域名是固定的（与 endpoint 填不填无关），这里填上是让人
        # 看得见"请求打到哪"，私有化/代理场景可改成自己的地址。
        "endpoint": "https://tts.tencentcloudapi.com",
        # 腾讯云 TTS 没有"模型名"这个概念（ModelType 恒为 1，不是可选模型），
        # 所以这一栏留空 —— 别为了填满表单编一个值出来。
        "model": "",
        "needs_key": True,
        "key_hint": "填 **SecretId:SecretKey**，用**英文冒号**分隔"
                    "（例：AKIDxxxx:xxxxxx）。注意是「访问密钥」的两段，"
                    "**不是** API 密钥单串，也不是 AppID。",
        "needs_voice": True,
        "voices": [dict(v) for v in _tencent_voices()],
        "free_tier": "新用户有试用额度（具体额度以腾讯云控制台「语音合成」"
                     "页面为准）；之后按量计费，音色不同价位不同。"
                     "**非永久免费**。",
        "notes": "⚠️ ① 音色 ID 是**整数**（如 601009）—— 把火山/百炼那种字符串"
                 " ID 粘过来会报 InvalidParameterValue.VoiceType"
                 " ② 下列 8 个预置音色**全部支持 24kHz**（本平台要的采样率），"
                 "可直接选用；若手填别的 ID 请先确认它支持 24k，否则合成会失败"
                 " ③ **情感只有 601xxx 大模型音色支持**（9 种）；402/403/502 "
                 "超自然音色只支持中性，选了它们再传情感会自动降级为无情感"
                 " ④ 单次合成上限 150 个汉字，超长由服务端自动分段拼接",
    },
    {
        "value": "aliyun",
        "label": "阿里云百炼 qwen-tts（OpenAI 兼容 · 国内直连）",
        "api_style": "openai_compatible",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-tts",
        "needs_key": True,
        "key_hint": "填百炼的 API Key（sk- 开头，单段）。"
                    "**不要填阿里云主账号 AK/SK**，那是另一套鉴权。",
        "needs_voice": True,
        "voices": [
            {"id": "Cherry", "name": "Cherry（女声· 亲和清亮）",
             "gender": "女", "scene": "日常陪伴",
             "note": "百炼预置音色，老人听着清晰"},
            {"id": "Ethan", "name": "Ethan（男声· 沉稳）",
             "gender": "男", "scene": "日常对话的男性角色",
             "note": "语速适中"},
            {"id": "Nofish", "name": "Nofish（男声· 播报腔）",
             "gender": "男", "scene": "服药提醒等必须听清的播报",
             "note": "咬字清楚"},
            {"id": "Jennifer", "name": "Jennifer（女声· 温暖）",
             "gender": "女", "scene": "日常陪伴的备选",
             "note": "语气温暖"},
        ],
        "free_tier": "新用户试用约 10000 字符 / 90 天。之后按量，"
                     "1000 字约 ¥0.013。",
        "notes": "⚠️ endpoint 要填到 **compatible-mode/v1** 这一级，"
                 "填官网首页必然失败。免费额度很小（约几十轮对话），"
                 "额度用尽会直接停 —— 请务必在后台多配几行做 priority 兜底。",
    },
)

VENDOR_VALUES: Final[tuple[str, ...]] = tuple(v["value"] for v in VENDOR_CATALOG)


def vendor_entry(value: str) -> VendorEntry | None:
    """按 provider 值取厂商预置。未知返回 None（调用方据此走通用路径）。"""
    return next((v for v in VENDOR_CATALOG
                 if v["value"] == (value or "").strip()), None)


def voices_for_vendor(value: str) -> list[dict]:
    """某厂商的预置音色。未知厂商回落到 Edge 清单（最通用的一组）。

    ⚠️ 不能让前端拿全局 VOICE_CATALOG 渲染任意厂商的音色框 ——
    火山只认 ``BV001_streaming``，百炼只认 ``Cherry``，跨厂商混用必然失败。
    """
    entry = vendor_entry(value)
    return list(entry["voices"]) if entry else [dict(v) for v in VOICE_CATALOG]


def vendors_payload() -> dict:
    """给 ``GET /api/providers/catalog`` 的厂商部分。"""
    return {
        "vendors": [dict(v) for v in VENDOR_CATALOG],
        "voices_by_vendor": {v["value"]: list(v["voices"]) for v in VENDOR_CATALOG},
        "default_vendor": "edge",
    }


# ============================================================================
# 用户端：**当前生效**厂商的音色选项
# ============================================================================

#: ``api_style`` -> 厂商值。只收**能唯一确定厂商**的三种。
#:
#: ⚠️ ``openai_compatible`` **刻意不在表内**：它是「自建 / 兼容网关」的通用风格，
#: 阿里百炼也用它 —— 把它映射成某一家会把自建服务误判成百炼，进而给出一份
#: 它根本不认的音色清单（比不给清单更坏）。百炼行靠 ``provider="aliyun"``
#: 在 :func:`_vendor_of` 的第一步就命中，轮不到这张表。
#:
#: ⚠️ 也**不能**拿 ``asr_tts._tts_dispatch_style()`` 的返回值当厂商名：
#: 它把 ``aliyun`` 归到 ``openai_compatible``（因为调用上确实是 OpenAI 兼容），
#: 用它的返回值当 vendor key 会把百炼的身份丢掉。
_STYLE_VENDORS: Final[dict[str, str]] = {
    "edge": "edge",
    "volcengine": "volcengine",
    "tencent": "tencent",
}

#: 认不出厂商时（自建 / 兼容网关）给人看的说明。
_CUSTOM_HINT: Final[str] = (
    "当前部署的 TTS 是**自建服务或 OpenAI 兼容网关**，音色名由那家服务自己定义，"
    "本平台无从预置 —— 请按该服务的文档手填音色名；留空则用它的默认音色。"
)


def _vendor_of(row) -> str | None:
    """这条配置**实际会走**哪一家 TTS。认不出返回 ``None``（自建/兼容网关）。

    判据顺序（与 ``asr_tts._tts_dispatch_style`` 保持一致，别换）：
      1. ``provider`` 命中预置厂商 —— **必须排第一**。管理后台选厂商时会把
         ``api_style`` 一并预填，若让 api_style 优先，火山/腾讯云行会被判成
         别家（同一个坑在分发层已经踩过一次，见那里「判据优先级不能换」）。
      2. ``api_style`` 是能唯一确定厂商的三种之一。
      3. 都不认 -> ``None``。
    """
    provider = (getattr(row, "provider", "") or "").strip().lower()
    entry = vendor_entry(provider)
    if entry is not None:
        return str(entry["value"])
    style = (getattr(row, "api_style", "") or "").strip().lower()
    return _STYLE_VENDORS.get(style)


def _vendor_default_voice(vendor: str, voices: list[dict]) -> str:
    """该厂商的**出厂默认**音色（调用方已保证 ``voices`` 非空）。

    ⚠️ 优先用厂商实现模块自己声明的默认常量，**不是**简单取清单第一条：
    腾讯云清单第一条是男声 ``601008``，而它的默认是女声 ``601009``
    （``tts_tencent.DEFAULT_VOICE``，见那里的选型说明）。取第一条会让
    「配置行没填音色」时下拉框的默认值与老人**实际听到的声音**不是同一个。
    """
    ids = {str(v.get("id") or "") for v in voices}
    if vendor == "edge" and DEFAULT_PRESET_TTS_VOICE in ids:
        return DEFAULT_PRESET_TTS_VOICE
    if vendor == "tencent":
        from .tts_tencent import DEFAULT_VOICE as tencent_default

        if tencent_default in ids:
            return tencent_default
    return str(voices[0].get("id") or "")


def options_for(db) -> dict:
    """当前部署**实际生效**的 TTS 厂商 -> 该厂商可选音色 + 默认音色。

    给用户端角色编辑用（``GET /api/providers/voice-options``）。返回体字段名
    已被前端依赖，**不要改名**：

    ``vendor`` / ``vendor_label`` / ``voices`` / ``default_voice`` /
    ``allow_custom`` / ``custom_hint``

    为什么按「生效厂商」而不是「全部厂商」
    ------------------------------------
    音色 ID 不跨厂商通用，而角色只有一个 ``tts_voice`` 字段，最终会被原样塞进
    当前生效那条配置的请求里。列出生效厂商之外的音色，等于给用户一个
    **选了也不生效**的选项 —— 而失效是静默的（``_shape_ok_voice`` 会置空），
    用户完全看不出来。详见模块文档字符串「为什么给用户端的清单必须按当前生效的那一家算」。

    ``default_voice`` 的三级回落（顺序不能换）
    ----------------------------------------
      ① 该配置行**自己**填的音色（``asr_tts._row_voice``，同时认 ``tts_voice``
         列与 ``params.voice``）且确实在该厂商清单里 —— 管理员显式指定过就用它；
      ② 否则该厂商的出厂默认（见 :func:`_vendor_default_voice`）；
      ③ 自建/认不出厂商 -> ``""``（没有清单，也就没有「默认音色」这回事）。

    第①级必须校验「在清单里」：配置行里可能躺着上一家厂商的音色名（换厂商时
    不会自动清理），照抄它会把这个失效值当成默认值再传播给所有角色。

    :return: 见上；``vendor`` 为空串表示自建/认不出，此时 ``voices`` 为 ``[]``、
        ``allow_custom`` 为 True。
    """
    from ..llm_gateway import resolve_row      # 懒 import：避免循环依赖

    row = resolve_row(db, "tts")
    # 一条 tts 配置都没有：按 Edge 处理。理由 —— Edge 是零配置成本的那条路，
    # 也是本项目历史上所有部署的默认技术栈；给空清单会让「还没配 TTS 的
    # 新部署」的用户端下拉框彻底没东西可选，比给一份 Edge 清单更没用。
    vendor = "edge" if row is None else _vendor_of(row)

    if not vendor:
        return {
            "vendor": "",
            "vendor_label": "自建 / OpenAI 兼容 TTS",
            "voices": [],
            "default_voice": "",
            "allow_custom": True,
            "custom_hint": _CUSTOM_HINT,
        }

    voices = voices_for_vendor(vendor)
    entry = vendor_entry(vendor)
    default_voice = ""

    if row is not None:
        from .asr_tts import _row_voice      # 只 import，不改它的逻辑

        own = _row_voice(row)
        if own and own in {str(v.get("id") or "") for v in voices}:
            default_voice = own
    if not default_voice:
        default_voice = _vendor_default_voice(vendor, voices)

    return {
        "vendor": vendor,
        "vendor_label": str((entry or {}).get("label") or vendor),
        "voices": [dict(v) for v in voices],
        "default_voice": default_voice,
        "allow_custom": False,
        "custom_hint": (
            "当前生效的是「" + str((entry or {}).get("label") or vendor) + "」，"
            "上面列出的就是它认的全部音色 —— 换成别的厂商的音色名会**静默失效**"
            "（厂商回参数错误时整句没有声音，所以平台会退回它的默认音色）。"
            "换厂商请先在「AI 接入」里改配置，再回来重选音色。"
        ),
    }