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
出厂默认音色（``DEFAULT_PRESET_TTS_VOICE``）本来就以本目录为真源
（见 main.py 种子处注释：「运营后台的恢复出厂默认需要单一真源」）。若前端
再抄一份，就是第二份真源 —— 迟早出现「下拉框里选得到的音色，恢复默认后却
是另一个」。前端改为 ``GET /api/voice-catalog`` 拉取。

预置的是哪一组
--------------
Edge TTS（微软）官方音色。它不需要自建服务、免费、Python 侧零新增依赖
（走 HTTP + SSML），与项目现有默认技术栈一致。**不混入本地开源 TTS
（CosyVoice / GPT-SoVITS 等）的音色名**：那些填进去在当前部署下必然调用
失败，混在一起会让管理员误以为「从下拉框里选就能用」。

选型依据与完整对比见 ``docs/免费语音服务商选型.md`` §2 / §5。
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

# 出厂默认音色。⚠️ 这个值被 main.py 的种子写入人格预设，
# 「恢复出厂默认」也读它。**值不可随意改**：改了等于给所有已有部署
# 的默认音色换人，且不会触发任何迁移（已在库里的行仍是旧值）。
DEFAULT_PRESET_TTS_VOICE: Final = "zh-CN-XiaoxiaoNeural"


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