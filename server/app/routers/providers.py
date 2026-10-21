"""多平台 AI API 接入配置（需求 15）：本服务做「配置的托管、下发与调用」。

两层职责，别混在一起看
----------------------
1. **托管**：CRUD + 列表过滤 + 导出成小智的 config.yaml 片段；
2. **调用**：server/app/llm_gateway.py 按「用途（usage）」把请求派到某条配置上。

本文件只暴露两个让上层"不必知道配了哪家"的入口：

    GET  /api/providers/resolve?usage=chat   这条用途最终会落到哪条配置（脱敏）
    POST /api/providers/test                 按用途真打一次，验证配置可用与否

为什么要「按用途解析」而不是「按 kind 查」
------------------------------------------
kind 是**储值/能力分类**，会随业务演进（今天 llm 一个够，明天要分 llm_main /
llm_light）；usage 是**调用方的意图**（"我要聊天"、"我要抽意图"、"我要向量"），
比 kind 稳定得多。上层只认 usage，我们把旧部署历史上只有 kind=llm 的老配置
通过候选链（llm_main -> llm）兜住，两边都不用改代码。详见 model_catalog.py。
"""
from __future__ import annotations

import io
import time
import wave

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from typing import Final

from .. import models
from ..db import get_db
from ..deps import get_current_admin, get_current_user, require_config
from ..llm_gateway import GatewayError, chat, embed, rerank, resolve_plan
from .. import provider_presets
from ..model_catalog import (
    API_STYLE_LOCAL_ASR,
    API_STYLE_LOCAL_EMBED,
    API_STYLES,
    EXPORT_KINDS,
    KIND_LABELS,
    KIND_LIST,
    LOCAL_ASR_TIMEOUT_SEC,
    LOCAL_EMBED_TIMEOUT_SEC,
    LOCAL_SERVICE_DEFAULTS,
    LOCAL_STYLE_KIND,
    USAGE_ASR,
    USAGE_CHAT,
    USAGE_EMBED,
    USAGE_LABELS,
    USAGE_LIGHT,
    USAGE_LIST,
    USAGE_RERANK,
    USAGE_ROUTES,
    USAGE_TTS,
    USAGE_VAD,
    USAGE_VISION,
    describe_kinds,
)
from ..schemas import (
    ModelTestIn,
    ModelTestOut,
    ProviderIn,
    ProviderOut,
    ProviderSafeOut,
    ResolvedModelOut,
)

router = APIRouter(prefix="/api/providers", tags=["providers"])

# 兼容历史写法：别处可能 `from ..routers.providers import VALID_KINDS`。
# 语义上它现在是「真实平台配置的全部 kind」，不再等于下发/导出的那四种。
VALID_KINDS = KIND_LIST

# usage -> 走哪三个入口
_CHAT_USAGES = frozenset({"chat", "light", "vision"})
_EMBED_USAGES = frozenset({"embed"})
_RERANK_USAGES = frozenset({"rerank"})

#: 本地自建风格 -> 默认超时（秒）。为什么本地要单独一档：GGUF 版 FunASR
#: 冷启动约 2.8s、稳态 ~1s，且请求串行执行，并发高时会排队；云端 ASR 的
#: 30s 默认值在这里会偶发误判超时（本地慢 ≫ 云端慢，而超时是同一个数）。
#: 与 model_catalog.LOCAL_ASR_TIMEOUT_SEC / LOCAL_EMBED_TIMEOUT_SEC 同源，
#: 或许将来该把这份映射搬过去 —— 现在两个消费方都小，先各自引用常量即可。
_LOCAL_STYLE_TIMEOUT: Final[dict[str, int]] = {
    API_STYLE_LOCAL_ASR: LOCAL_ASR_TIMEOUT_SEC,
    API_STYLE_LOCAL_EMBED: LOCAL_EMBED_TIMEOUT_SEC,
}


def _normalize_local_service(payload: ProviderIn, explicit: set[str]) -> bool:
    """本地自建服务（FunASR / FastEmbed）的**保存即用**归一化。

    需求原文：下拉选中「本地 ASR / 本地向量化」时，**不需要填写其他信息，
    直接点击保存就能使用**。要满足它，四条属性必须有一个地方在保存时
    自动补齐：kind（能力分类）、endpoint（服务地址）、timeout（本地模型
    首帧慢）、model（仅作展示）。集中定义在
    ``model_catalog.LOCAL_SERVICE_DEFAULTS``，前端自动填表
    （见 static/index.html 的 onApiStyleChange）与本函数读同一份常量，
    才不会出现「前端说不用填、后端说缺 endpoint」。

    为什么 create 与 update 必须共用这一个函数：两个入口各写一遍，
    迟早出现「新建能直接保存、再编辑一次反而丢默认值」的漂移 ——
    而那种故障的表现是「明明配过，过几天自己坏了」。

    :param payload: 请求体（会被就地修正）。
    :param explicit: 请求体里**显式出现过**的字段名
        （``model_dump(exclude_unset=True).keys()``）。用它区分
        「管理员没填超时」与「管理员显式填了 30s」：``ProviderIn`` 的
        ``timeout_sec`` 默认值本身就是 30，不区分就永远不知道
        该不该换成本地更宽的默认值。
    :return: 这一行是不是本地服务（调方据此决定要不要清缓存/记日志）。
    """
    style = (payload.api_style or "").strip()
    meta = LOCAL_SERVICE_DEFAULTS.get(style)
    if meta is None:
        return False

    # ① kind 强制对齐：选了「本地 ASR」却落在 llm_main 上，配置存得进、
    #    列表看得见，调用时才报「尚未配置 chat 用途的模型」——
    #    页面一切正常而功能全废，是最难自查的一类故障（见 D50 同款成因）。
    payload.kind = LOCAL_STYLE_KIND[style]
    # ② name 允许只选下拉就保存：schema 层 name 必填但允许空串，
    #    这里补一个可读的默认名（管理员随后可在列表里改）。
    if not (payload.name or "").strip():
        payload.name = meta["label"]
    # ③ provider 留空则填预置值（填了不动）：asr_tts._asr_style 与
    #    前端「当前厂商」回显都认它。
    if not (payload.provider or "").strip():
        payload.provider = meta["provider"]
    # ④ endpoint 留空填默认（同机部署走 127.0.0.1）；管理员填了地址
    #    （Docker 不在同一 netns 时填宿主 IP，如 10.2.0.16）就以他为准 ——
    #    这个字段仍然是可编辑的，只是不再是必填。
    if not (payload.endpoint or "").strip():
        payload.endpoint = meta["endpoint"]
    # ⑤ 本地服务没有密钥这一说：一律清空。前端对这个风格隐藏 API Key 栏，
    #    但**后端不能依赖前端**（旧请求体、脚本调用、以后新前端都可能带回它）。
    #    带着Bearer 头的另一个代价：将来换实现时没人想得起这个 key 是哪来的。
    payload.api_key = ""
    # ⑥ model 仅作展示：FunASR 按服务端加载的 gguf 识别、FastEmbed 同理，
    #    都不看这个字段。留空填展示名；管理员填了就以他为准
    #    （留一个不改代码就能改展示名的口子）。
    if not (payload.model or "").strip():
        payload.model = meta["model"]
    # ⑦ 超时：没显式填才用本地默认值（理由见 _LOCAL_STYLE_TIMEOUT）
    if "timeout_sec" not in explicit:
        payload.timeout_sec = _LOCAL_STYLE_TIMEOUT[style]
    return True


def _normalize_preset_service(payload: ProviderIn, explicit: set[str]) -> None:
    """预置厂商的「只填密钥就能用」归一化（就地补全 endpoint/model/api_style/name）。

    为什么要做在这里而不只是运行时兜底
    --------------------------------
    ``voice/asr_tts._row_endpoint`` 已经能在**调用时**回落到厂商默认地址，
    但那只解决「能不能跑」。库里躺着一行 ``endpoint='' AND model=''``
    的配置仍有三个实际代价：

      1. 管理后台打开它、原样点保存，``PATCH`` 会用请求体整体覆盖（见
         update_provider 的 ``setattr`` 循环），空串又被写回一次；
      2. 管理员在列表里看不出这条到底打的是哪个域名，只能猜；
      3. 导出/排障时（``/export/xiaozhi``、readiness 页）拿到的是空串，
         排查方向直接跑偏。

    所以**保存时补齐**，运行时兜底保留作为第二层（存量行不必手工重存）。

    只填空值，绝不覆盖管理员填过的内容 —— 私有化/代理场景填自己的地址
    是合法需求；预置只是默认值，不是强制值。本地自建服务
    （``local_*``）由 :func:`_normalize_local_service` 先处理，那一路的
    默认值来自 ``LOCAL_SERVICE_DEFAULTS``，本函数看到 endpoint 已非空就跳过。

    :param payload: 请求体（会被就地修正）。
    :param explicit: 请求体里显式出现过的字段名。目前只用于将来按需区分
        「没填」与「显式填空串」；保留参数是为了与姊妹函数同签名。
    """
    del explicit  # 预留：当前归一化只认「空就补」，无需区分是否显式传了空串

    provider = (payload.provider or "").strip().lower()
    if not provider:
        return

    kind = (payload.kind or "").strip()

    # ---- TTS 厂商预置（腾讯云 / 火山 / 百炼 / Edge…）----
    from ..voice.catalog import vendor_entry

    entry = vendor_entry(provider)
    if entry is not None:
        # ⚠️ 只有**可存风格**才写进 api_style 列。
        # 腾讯云 / Edge 的 api_style 是「供应商级」的（tencent / edge），
        # 它们既不在 API_STYLES 白名单里，也不该存 —— 调用层
        # ``asr_tts._tts_dispatch_style`` 是**先认 provider 预置**再认 api_style
        # （见那段"判据优先级不能换"的注释），写进列反而会被 ProviderIn 的
        # 校验器直接 500。实测过：写 'tencent' → 400/500「api_style 只能是…」。
        style = str(entry.get("api_style") or "")
        if not (payload.api_style or "").strip() and style in API_STYLES:
            payload.api_style = style
        if not (payload.endpoint or "").strip():
            payload.endpoint = str(entry.get("endpoint") or "")
        # 腾讯云 TTS 没有「模型名」这个概念（ModelType 恒为 1），预置里就是
        # 空串 —— 这里**不要**给它编一个值出来，留空才是正确形态。
        if not (payload.model or "").strip():
            payload.model = str(entry.get("model") or "")
        if not (payload.name or "").strip():
            payload.name = str(entry.get("label") or "")
        return

    # ---- 其余能力（ASR / LLM / Embedding / Rerank / Vision）预置 ----
    preset = _preset_entry(provider)
    if preset is None:
        return
    preset_style = str(preset.get("api_style") or "")
    if not (payload.api_style or "").strip() and preset_style in API_STYLES:
        payload.api_style = preset_style
    if not (payload.endpoint or "").strip():
        payload.endpoint = str(preset.get("endpoint") or "")
    if not (payload.model or "").strip():
        # 「模型」按**能力**取：同一个厂商在不同 kind 下模型名不同
        # （如腾讯云的 ASR 是引擎类型 16k_zh）。该 kind 没有预置
        # （如 deepseek 没有 rerank）就什么都不做 —— 既不清空也不瞎填。
        models = preset.get("models") or {}
        payload.model = str(models.get(kind, "") or "")
    if not (payload.name or "").strip():
        payload.name = str(preset.get("label") or "")


def _check_tts_voice_shape(payload: ProviderIn) -> None:
    """TTS 配置里填的音色必须符合该厂商的形态（各厂商音色 ID 不通用）。

    为什么要在保存时就报错
    ----------------------
    ``voice/asr_tts._shape_ok_voice`` 运行时已经会「形态不符就退回厂商默认」，
    但那属于**静默兜底**：管理员在弹窗里选了腾讯云，却填了 Edge 的音色名
    （``zh-CN-XiaoxiaoNeural``）—— 听到的是默认音色，自己却以为配的音色生效了。
    2026-10-22 那次「有文字没声音」正是这一族问题（腾讯云收到 Edge 音色名，
    回「VoiceType 必须是整数」，整句音频被跳过）。

    运行时要宽容（配好的服务不能因为一个音色字段就整条不出声），
    保存时要严格（让人在**改配置的那一刻**就知道填错了）。两者不矛盾：
    严格的是「新写入的数据」，宽容的是「已经在库里的数据」。

    只校验形态封闭的厂商（腾讯云纯整数、Edge BCP-47 名）。火山、OpenAI 兼容
    等一律放行 —— 它们的音色表含私有/克隆 ID，形态不封闭，硬校验只会误伤。
    """
    if (payload.kind or "").strip() != "tts":
        return
    voice = str((payload.params or {}).get("voice") or "").strip()
    if not voice:
        return
    from ..voice.asr_tts import _VOICE_SHAPES, _tts_dispatch_style

    style = _tts_dispatch_style(payload)
    pattern = _VOICE_SHAPES.get(style)
    if pattern is None or pattern.match(voice):
        return
    hint = ("只能是**整数**音色 ID（如 601009）"
            if style == "tencent"
            else "必须是 Edge 音色名（如 zh-CN-XiaoxiaoNeural）")
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(f"{style} 的音色 {voice!r} 不合法：{hint}。"
                "各厂商音色 ID 不通用 —— 想换厂商就得在这个弹窗里重选音色"
                "（留空则用该厂商默认音色）。"))


def _preset_entry(provider: str) -> dict | None:
    """按厂商名取非 TTS 预置条目（找不到返回 None）。"""
    from ..provider_presets import preset_entry

    return preset_entry(provider)


def _clear_query_cache_if_embedding(kind: str | None) -> None:
    """改动了向量模型配置时，清掉记忆检索的**查询向量缓存**。

    为什么必须联动清：``memory/retrieve.py`` 的 ``_query_cache`` 只按 LRU 淘汰，
    里面存的是**旧模型**算出来的查询向量。换模型后若不清，新查询会拿新模型的
    向量去和库里旧模型写入的记忆向量算余弦 —— 等于拿两把不同的尺子量长度，
    而且**不报错**，只是静默给出错误排序（表现为「换了模型之后召回突然变差」，
    极难定位）。函数名里就写明了这个调用点，避免以后新增改配置的入口时漏掉。

    只在**向量相关**的 kind 上做：改 ASR/TTS 与记忆检索无关，不必连坐。
    """
    if (kind or "") not in USAGE_ROUTES.get(USAGE_EMBED, ()):
        return
    try:
        # 局部导入：memory 包会 import models/db，反向依赖 routers 层会成环
        from ..memory.retrieve import clear_query_cache

        clear_query_cache()
    except Exception:  # noqa: BLE001  清缓存失败不该让配置保存失败
        pass


def _require_usage(usage: str) -> str:
    """校验 usage；未知值明确列出可选清单（拼错是最常见的一类调用方 bug）。"""
    usage = (usage or "").strip()
    if usage not in USAGE_ROUTES:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"未知用途 {usage!r}，可选：{'、'.join(f'{k}（{USAGE_LABELS[k]}）' for k in USAGE_LIST)}",
        )
    return usage


@router.post("", response_model=ProviderOut, status_code=status.HTTP_201_CREATED)
def create_provider(payload: ProviderIn, db: Session = Depends(get_db),
                    admin: models.User = Depends(get_current_admin)):
    """新建一条**平台配置**（仅管理员）。

    ⚠️ 禁止 ``kind == ""``：空 kind 的行是**角色**，而角色已由 /api/roles 独立
    承载（人格、traits、guardrails、派生关系全在那里）。在这里放行空 kind
    等于给"角色改造波及平台配置"留了一条后门 —— APP 建角色时少传一个字段就
    会静默落成一条没有厂商的"平台配置"，然后被设备下发当成真配置用。

    ⚠️ 归一化在校验**之前**：本地服务风格会自己补 kind（选中「本地 ASR」
    却把能力留空是需求允许的用法），先校验证就把它和"真忘了传 kind"一起拒了。
    """
    explicit = set(payload.model_dump(exclude_unset=True).keys())
    _normalize_local_service(payload, explicit)
    _normalize_preset_service(payload, explicit)
    if not (payload.kind or "").strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "创建角色请用 POST /api/roles；本接口只接受带 kind 的平台 AI 配置",
        )
    _check_tts_voice_shape(payload)
    p = models.AIProvider(**payload.model_dump())
    # 显式写死 record_type：不依赖模型默认值，避免将来默认值改动波及这里
    p.record_type = models.RECORD_TYPE_CONFIG
    db.add(p)
    db.commit()
    db.refresh(p)
    # 新增向量模型配置 -> 旧查询向量缓存全部作废（见 _clear_query_cache_if_embedding）
    _clear_query_cache_if_embedding(p.kind)
    return p


@router.get("", response_model=list[ProviderOut])
def list_providers(kind: str | None = None, db: Session = Depends(get_db),
                   admin: models.User = Depends(get_current_admin)):
    """配置列表（**仅管理员**）。?kind=embedding 这类过滤对新加的 kind 同样生效。

    ⚠️ 收紧为 admin only（架构 §12 U2）：这个接口返回带 api_key 的完整行，
    现状是**任何登录用户**都能读到全平台的密钥。即使 /safe 加了过滤，
    不收紧这里就等于留了个后门 —— 密钥必须只在管理端可见。

    同时加 ``record_type == 'config'`` 过滤：角色行不再混进平台配置列表。
    """
    q = select(models.AIProvider).where(models.AIProvider.record_type == models.RECORD_TYPE_CONFIG)
    if kind:
        q = q.where(models.AIProvider.kind == kind)
    return list(db.scalars(q.order_by(models.AIProvider.kind, models.AIProvider.priority)).all())


@router.get("/safe", response_model=list[ProviderSafeOut])
def list_providers_safe(db: Session = Depends(get_db),
                        user: models.User = Depends(get_current_user)):
    """脱敏列表（不含 api_key），供设备与 APP 拉取。

    ⚠️ 两处收紧（均为 P0-2 的止血点）：
      1. **加鉴权**。现状这个接口连 ``Depends(get_current_user)`` 都没有，
         任何人（包括完全未登录的调用方）都能拉到全量配置与角色；
      2. **加 record_type == 'config' 过滤**。角色改由 /api/roles 提供，
         平台配置不该再从"脱敏列表"里流出去 —— 这条链路正是 P0-1 事故的
         起点（拿到配置 id -> DELETE -> 设备失去对话能力）。
    """
    rows = db.scalars(
        select(models.AIProvider)
        .where(models.AIProvider.enabled.is_(True),
               models.AIProvider.record_type == models.RECORD_TYPE_CONFIG)
        .order_by(models.AIProvider.kind, models.AIProvider.priority)
    ).all()
    return list(rows)


@router.get("/catalog")
def provider_catalog(user: models.User = Depends(get_current_user)):
    """把「能力分类 / 用途 / 厂商风格」三张枚举表一起给出，便于管理页渲染下拉框。

    枚举值会随业务增长（比如以后加 kind=tts_stream），硬编码在前端的话每次都要
    改两个地方；这里做成接口，前端跟着它走就行。

    同时带上**音色目录**：音色是厂商侧标识符，填错会静默失败（厂商悄悄用
    自己的默认音色，管理员以为换成功了）。所以音色框必须由服务端给清单，
    见 ``app.voice.catalog`` 的模块说明。
    """
    from ..voice import catalog as voice_catalog

    payload = {
        "kinds": [{"value": k, "label": KIND_LABELS.get(k, k)} for k in KIND_LIST],
        "usages": [
            {"value": u, "label": USAGE_LABELS.get(u, u), "kinds": list(USAGE_ROUTES[u])}
            for u in USAGE_LIST
        ],
        "api_styles": [{"value": v, "label": t} for v, t in API_STYLES.items()],
        # 本地自建服务（选中即用）：把默认 endpoint / kind / model / 超时
        # 一起给前端，自动填表不硬编码地址 —— 单一真源在
        # model_catalog.LOCAL_SERVICE_DEFAULTS，后端保存时还会再归一化一次。
        "local_services": {style: dict(meta)
                           for style, meta in LOCAL_SERVICE_DEFAULTS.items()},
    }
    payload.update(voice_catalog.catalog_payload())
    payload.update(voice_catalog.vendors_payload())
    payload.update(provider_presets.presets_payload())
    # 让前端知道 Edge 这条路在本部署里**现在到底能不能走**。
    # 缺依赖时返回 false 而不是静默：否则管理员配好 Edge、页面一切正常，
    # 直到老人没声音才发现少装了两个包。
    payload["edge_available"] = _edge_available()
    return payload


def _edge_available() -> bool:
    try:
        from ..voice.tts_edge import edge_available
    except Exception:  # noqa: BLE001  模块本身出问题也按不可用处理
        return False
    return edge_available()


@router.get("/catalog/voice-preview")
def voice_preview(voice: str, text: str = "您好，我是小智。",
                  user: models.User = Depends(get_current_user)):
    """合成一段**试听音频**，让用户挑音色时能先听再选。

    为什么必须有这个（2026-10-19 用户报「音色没有任何音色供选择」）
    ------------------------------------------------------------------
    音色是厂商侧标识符（``zh-CN-XiaoxiaoNeural`` 这种），**纯文字无法判断**：
    「晓晓」和「晓伊」在列表里都是「女声· 温柔/活泼」，光看名字和说明分不出
    差别，而老人听力衰减使他们的偏好与年轻人明显不同。挑音色必须听。

    为什么这里返回 wav，而设备语音链路用裸 PCM
    -------------------------------------------
    两者是**不同消费者**，别被「统一契约」四个字误导：
      * 固件走 WebSocket 语音链路，吃 24kHz/mono/S16LE **裸 PCM**
        （全项目 TTS 契约，见 app/voice/tts_edge.py）；
      * APP 试听交给系统播放器（createInnerAudioContext），
        它只认 wav/mp3 这类带容器头的格式。
    给试听端点返回裸 PCM 会让前端必须自己拼 44 字节头，
    而各端（尤其小程序端）拿 ArrayBuffer 写文件的差异比容器本身还麻烦。
    容器头在服务端用标准库 wave 包一下，30 行换两端都省事。

    错误处理：TTS 不可用抛 **503** 而不是 500 ——「服务没配 TTS」是部署状态问题，
    客户端据此提示「服务未配置」比「服务器内部错误」可行动得多。
    """
    from ..voice import tts_edge

    if not tts_edge.edge_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="试听不可用：本部署未启用 TTS 合成",
        )
    # 限长：这是「试听」，不是「把 TTS 当免费下载器」。
    # 不限的话任何登录用户都能拿它做免费 TTS 服务。
    snippet = (text or "").strip()[:60] or "您好，我是小智。"
    try:
        pcm = tts_edge.synth_pcm(snippet, voice)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"试听合成失败：{exc}",
        ) from exc

    buf = io.BytesIO()
    # 1 声道 / 16 位 / 24kHz —— 与 synth_pcm 的输出严格一致，三个参数不能错，
    # 否则是「能播但速度不对」的声音（比不播更难排查）。
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    return Response(
        content=buf.getvalue(),
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/readiness")
def provider_readiness(db: Session = Depends(get_db),
                       user: models.User = Depends(get_current_user)):
    """每个用途**现在能不能用** —— 管理页顶部的「服务就绪度」面板数据源。

    为什么必须做这个，而不是在页面上写死一句「已配置 0 项请去配置」
    --------------------------------------------------------------
    管理员最痛的时刻不是「没配置」，而是「配了但设备还是不说话」。
    两种情况在页面上长得一模一样（都是没声音），排查方向却相反：前者是
    这里少了行，后者是 endpoint/key/音色错了。

    所以这里返回**分层**事实，而不是一个笼统的 bool：

    - ``configured``  该用途有没有启用且健康的配置行（能否解析出候选）
    - ``resolved``    现在实际会选中哪一条（kind + 名字，**脱敏**）
    - ``hint``        没配时给出**具体**该配什么，含 kind 与 endpoint 形状
    - ``critical``    缺它设备就完全不能说话（asr/tts/chat），与
                     「锦上添花」（rerank/vision）区分开，避免吓人

    脱敏：只回 provider/kind/name，绝不回 api_key（这是管理端点，
    但仍按最小必要原则 —— 前端并不需要密钥就能渲染这个面板）。
    """
    rows: dict[str, dict] = {}
    for usage in USAGE_LIST:
        plan = resolve_plan(db, usage)
        if plan:
            kind, row = plan[0]
            rows[usage] = {
                "usage": usage,
                "label": USAGE_LABELS.get(usage, usage),
                "configured": True,
                "resolved": {
                    "kind": kind,
                    "name": row.name,
                    "provider": row.provider,
                    "model": row.model,
                    # fallback=True 表示这是从候选链兜底来的（如退回旧 llm），
                    # 值得让管理员知道「你现在用的不是你以为的那个」
                    "fallback": kind != USAGE_ROUTES[usage][0],
                },
                "hint": None,
                "critical": usage in _CRITICAL_USAGES,
            }
        else:
            rows[usage] = {
                "usage": usage,
                "label": USAGE_LABELS.get(usage, usage),
                "configured": False,
                "resolved": None,
                "hint": _MISSING_HINTS.get(usage, "请在「AI 接入」里添加并启用对应类型的配置"),
                "critical": usage in _CRITICAL_USAGES,
            }
    return {
        "usages": [rows[u] for u in USAGE_LIST],
        "missing_critical": [u for u in USAGE_LIST
                             if u in _CRITICAL_USAGES and not rows[u]["configured"]],
    }


# 缺这三项，设备「能连上但一句话也说不出」；缺其余项功能降级但仍可用。
_CRITICAL_USAGES: frozenset[str] = frozenset({USAGE_ASR, USAGE_TTS, USAGE_CHAT})

# 没配时给出的提示。刻意写清 endpoint 的**形状**而不只是名称：
# 老人场景下最大的坑是把「OpenAI 兼容地址」理解成厂商官网首页。
_MISSING_HINTS: dict[str, str] = {
    USAGE_ASR: (
        "未配置语音识别：老人说话服务端听不懂。"
        "请添加 kind=asr 的行。OpenAI 兼容写法 endpoint 填到"
        "「…/v1」一级（如 https://api.openai.com/v1），"
        "不要填官网首页；本项目上传音频走 multipart，"
        "字段名 file。完整对比见 docs/免费语音服务商选型.md §3"
    ),
    USAGE_TTS: (
        "未配置语音合成：服务端无法把回答念给老人听。"
        "请添加 kind=tts 的行，endpoint 同样是「…/v1」一级。"
        "音色可选本平台的预置清单（音色框下拉），"
        "自建服务则手填其音色名。详见 docs/免费语音服务商选型.md §2"
    ),
    USAGE_CHAT: (
        "未配置大模型：识别到的语音无法变成回答。"
        "请添加 kind=llm_main（或旧值 llm）的行。"
        "详见 docs/免费语音服务商选型.md §5"
    ),
    USAGE_LIGHT: "未配置轻量模型：提醒解析等简单任务会退回主模型（能跑，但更慢更贵）",
    USAGE_EMBED: "未配置向量化：语义记忆检索不可用（其余功能正常）",
    USAGE_RERANK: "未配置重排：记忆检索只按向量相似度排序（可用，质量略低）",
    USAGE_VISION: "未配置视觉理解：摄像头画面无法理解（不影响对话）",
    USAGE_VAD: (
        "未配置独立 VAD：端点检测由设备端 esp-sr 承担"
        "（已随固件启用，联网与断网都生效），此项可不配"
    ),
}


@router.get("/resolve", response_model=ResolvedModelOut)
def resolve_provider(usage: str, db: Session = Depends(get_db),
                     user: models.User = Depends(get_current_user)):
    """按用途解析出最终生效的那条配置（脱敏）。

    这是给上层用的视图：调用方只需要说「我要 chat」，不必知道平台配的是
    DeepSeek 还是通义、是 llm_main 还是退回的旧 llm。
    一条都没配时返回 503 + 「请配置 XXX」，而不是让上层拿到 None 然后炸在别处。
    """
    usage = _require_usage(usage)
    plan = resolve_plan(db, usage)          # 未知 usage 已在 _require_usage 挡掉
    if not plan:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"尚未配置「{USAGE_LABELS.get(usage, usage)}」用途的模型"
            f"（需要 {describe_kinds(usage)} 中的一种），请先在「AI 接入」里添加并启用",
        )
    kind, row = plan[0]
    return ResolvedModelOut(
        usage=usage,
        kind=kind,
        id=row.id,
        name=row.name,
        provider=row.provider,
        model=row.model,
        endpoint=row.endpoint,
        api_style=(row.api_style or "").strip() or "openai_compatible",
        supports_stream=bool(row.supports_stream),
        context_window=int(row.context_window or 0),
        timeout_sec=int(row.timeout_sec or 0) or 30,
        priority=int(row.priority or 0),
        # 首选 kind（候选链第一个）没命中才是兜底，常见的就是「退回旧 llm」
        fallback=(kind != USAGE_ROUTES[usage][0]),
    )


@router.post("/test", response_model=ModelTestOut)
def test_provider(payload: ModelTestIn, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    """按用途真打一次请求，验证配置链路是否可用（管理页的「测试连接」）。

    **不要**用这里的返回值做业务推断：它是一次性的探针。
    成功与失败都返回 200，看 ok 与 error_code。
    """
    usage = _require_usage(payload.usage)
    try:
        if usage in _EMBED_USAGES:
            res = embed(db, payload.text or "老人陪伴助手", usage=usage)
            return ModelTestOut(
                ok=True, usage=usage, kind=res.kind, provider_pk=res.provider_pk,
                provider_name=res.provider_name, model=res.model,
                vectors=res.vectors, elapsed_ms=res.elapsed_ms, attempts=res.attempts,
            )
        if usage in _RERANK_USAGES:
            docs = payload.documents or ["今天吃了降压药", "今天天气不错"]
            res2 = rerank(db, payload.text or "吃药了吗", docs, usage=usage)
            return ModelTestOut(
                ok=True, usage=usage, kind=res2.kind, provider_pk=res2.provider_pk,
                provider_name=res2.provider_name, model=res2.model,
                scores=res2.scores, elapsed_ms=res2.elapsed_ms, attempts=res2.attempts,
            )
        msgs = payload.messages or [{"role": "user", "content": "你好，请用一句话介绍自己"}]
        r = chat(db, usage, msgs)
        return ModelTestOut(
            ok=True, usage=usage, kind=r.kind, provider_pk=r.provider_pk,
            provider_name=r.provider_name, model=r.model, text=r.text,
            elapsed_ms=r.elapsed_ms, attempts=r.attempts,
        )
    except GatewayError as exc:
        # 把调用层的错误类型原样透出：no_config / auth_failed / rate_limited /
        # timeout / upstream_error 对运维的含义完全不同，不能合并成一个 500
        return ModelTestOut(
            ok=False, usage=usage, error_code=exc.code, message=exc.message,
            attempts=exc.attempts,
        )


@router.post("/test-voice")
def test_voice(payload: ModelTestIn, db: Session = Depends(get_db),
               admin: models.User = Depends(get_current_admin)):
    """**真打一次**语音合成/识别，验证整条链路（不是只查库）。

    为什么必须单独做一个端点，而不是复用 ``/api/providers/test``
    --------------------------------------------------------
    那个端点只覆盖 embed / rerank / chat 三类。asr / tts 会掉到最后的
    兜底分支，被当成「请先创建角色」返回 —— 管理员看到这句会以为是自己建错了
    角色，实际上他只是点了「测试连接」。

    更糟的是**它不会真的调用**：配置错了、密钥错了、火山签名错了，
    全都是「测试通过」，直到老人没声音才发现。火山的签名/AK-SK 尤其如此
    （本仓已实现完整适配，见 app/voice/tts_volc.py）。

    所以语音侧必须**打真实请求**。它会消耗厂商额度（一次合成约几十字），
    这是应该的 —— 不消耗任何额度的「测试通过」没有任何证明力。
    """
    from ..voice import asr_tts
    from ..voice.asr_tts import VoiceConfigMissingError, VoiceUpstreamError

    usage = _require_usage(payload.usage)
    # ⚠️ text 允许是 list（embed 的批量入口，见 ModelTestIn）—— 语音侧只吃
    # 单段字符串。必须在**调 .strip() 之前**归一：顺序反了的话一个 list 会
    # 先在 .strip() 上抛 AttributeError → 500，而调用方（管理页）只会看到
    # 一句"测试失败"，排查时根本看不出是类型不匹配。
    raw_text = payload.text if isinstance(payload.text, str) else ""
    text = (raw_text or "老人，今天该吃药了").strip()
    started = time.monotonic()
    try:
        if usage == USAGE_TTS:
            pcm = asr_tts.tts(db, text)
            return {
                "ok": True, "usage": usage,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                # 回长度而不是回音频：音频没必要给前端，字节数足够证明
                # 「真的合成出了音频」，且能被老人语音时长换算出来。
                "audio_bytes": len(pcm),
                "duration_ms": int(len(pcm) / 2 / 24000 * 1000),
                "detail": "已真实合成并解码为 24kHz 裸 PCM",
            }
        if usage == USAGE_ASR:
            # ⚠️ 这里**不**真调 ASR：要真调必须先有真实录音，
            # 而冒烟/测试环境里没有（伪造音频只会得到一个识别不出的空串，
            # 让人误以为 ASR 配好了）。所以 ASR 只校验配置链路是否成立，
            # 并明确告诉管理员这一点 —— 宁可说「没验」，不可假装「验过」。
            from ..llm_gateway import resolve_row
            row = resolve_row(db, USAGE_ASR)
            if row is None:
                raise VoiceConfigMissingError(USAGE_ASR)
            return {
                "ok": True, "usage": usage,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "detail": "已找到可用的 ASR 配置（未发起真实识别："
                          "需要真实录音，验证请直接对设备说一句话）",
                "endpoint": (getattr(row, "endpoint", "") or "")[:60],
            }
        return {"ok": False, "usage": usage,
                "message": "该用途没有语音侧测试，请在下方选择 ASR 或 TTS"}
    except VoiceConfigMissingError as exc:
        # ⚠️ error_code 必须保持 "no_config" 不变 —— 它是机器可读的契约
        #（tools/smoke_models.py 等冒烟按它断言，客户端也按它分流）。
        # 这里只把**人读**的 message 补全。
        #
        # 为什么必须补：管理员的典型路径是「后台新增一条 TTS 配置 -> 填音色 ->
        # 点真实测试」，此时表单内容**还没进库**，而本端点一律
        # ``resolve_row(db, usage)`` 从数据库读配置 —— 于是回 no_config。
        # 管理员看到「未配置可用的 tts 能力」会得出完全错误的结论：
        # 「Edge TTS 不能用 / 得去别处配」，而真相只是「你还没点保存」。
        # 把这层歧义在文案里直接说破，比让管理员自己反推便宜得多。
        return {"ok": False, "usage": usage, "error_code": "no_config",
                "message": (
                    f"{exc}。"
                    "这通常是因为配置还没保存 —— 本次测试读的是**数据库里已保存**的"
                    "配置，页面上填了但没点「保存」的内容在这里读不到。"
                    "请先点「保存」再点「真实测试」。"
                )}
    except VoiceUpstreamError as exc:
        return {"ok": False, "usage": usage, "error_code": "upstream_error",
                "message": str(exc)}
    except Exception as exc:  # noqa: BLE001  适配层抛的原生异常也要能显示
        return {"ok": False, "usage": usage, "error_code": "error",
                "message": f"{type(exc).__name__}: {exc}"}


@router.patch("/{provider_pk}", response_model=ProviderOut)
def update_provider(provider_pk: int, payload: ProviderIn, db: Session = Depends(get_db),
                    admin: models.User = Depends(get_current_admin)):
    """改一条平台配置（P0-1 修复：加 admin 校验 + record_type 断言）。

    非管理员一律 403，且**在取行之前**就拒 —— 否则 403/404 的差异会成为
    主键存在性的探测信道。管理员若传了一个非 config 的 id（角色行），返回
    404：这既防住了"把角色改成配置"，也让角色接口的语义保持唯一。

    与 create_provider 同样：本地风格先归一化再校验 kind（内侧注释见那里）。
    """
    explicit = set(payload.model_dump(exclude_unset=True).keys())
    _normalize_local_service(payload, explicit)
    _normalize_preset_service(payload, explicit)
    _check_tts_voice_shape(payload)
    p = require_config(db, provider_pk, admin)
    if not (payload.kind or "").strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "kind 不允许置空；平台配置与角色是两类记录，角色请用 /api/roles",
        )
    before_kind = p.kind
    for k, v in payload.model_dump().items():
        setattr(p, k, v)
    # record_type / owner_id 不接受来自请求体的覆写：它们是**归属**信息，
    # 一旦被 PATCH 改成 'role'，这条配置就会从设备下发里消失。
    p.record_type = models.RECORD_TYPE_CONFIG
    p.owner_id = None
    db.commit()
    db.refresh(p)
    # kind 可能被改过（embedding -> llm），所以改前改后两个 kind 都要判
    _clear_query_cache_if_embedding(before_kind)
    _clear_query_cache_if_embedding(p.kind)
    return p


@router.delete("/{provider_pk}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider(provider_pk: int, db: Session = Depends(get_db),
                    admin: models.User = Depends(get_current_admin)):
    """删除一条平台配置（P0-1 修复：加 admin 校验 + record_type 断言 + 硬删）。

    ⚠️ 这条接口在修复前是**当前生产代码里最严重的一处越权**：任何登录用户
    都能删掉平台 LLM 配置，而 /api/device/sync 只按
    ``enabled=True AND kind.in_(DEVICE_KINDS)`` 取配置，取不到就直接让设备
    **彻底无法对话**。现在守卫在 deps.require_config 里，与其余写接口共用。

    为什么平台配置维持**硬删**而角色是软删（架构 §1.4）：
      * 平台配置没有归属者，也不被任何 agent_id 外键引用，硬删不产生孤儿；
      * 它的价值全在 api_key / endpoint，软删留着等于在库里留一份"曾经能用过"
        的凭证，是负资产；
      * 硬删意味着设备下发那条敏感查询**一个字都不用改** —— 改成软删就得给它
        加 deleted_at IS NULL，而那正是事故链路的敏感点，任何改动都是新增风险。
    """
    p = require_config(db, provider_pk, admin)
    kind = p.kind
    db.delete(p)
    db.commit()
    # 删掉向量模型后，下一次 embed 会走另一条配置或直接失败；
    # 留着旧查询向量就是「新旧向量混算」，必须清
    _clear_query_cache_if_embedding(kind)


@router.get("/export/xiaozhi")
def export_for_xiaozhi(db: Session = Depends(get_db),
                       user: models.User = Depends(get_current_user)):
    """导出成小智 server 的 selected_module 片段，便于把配置同步过去。

    只导出真正的**设备侧**平台配置（asr/llm/tts/vad）：ai_providers 里还存着
    APP 自建的 agent 角色（kind 为空），它们没有厂商和模型，混进导出片段里
    会把真实的 LLM/ASR 配置顶掉。

    同理，新的 llm_main / llm_light / embedding / rerank / vision 只由本后端
    自调用，小智那边不认识这些 key，导过去只会变成无用条目 ——
    但它们承担的「对话」角色会通过下面的 LLM 兜底折算成一条 llm 导出，
    保证只配了 llm_main 的新部署也能导出一份能用的片段。
    """
    out: dict[str, dict] = {}
    rows = db.scalars(
        select(models.AIProvider)
        .where(models.AIProvider.enabled.is_(True), models.AIProvider.kind.in_(EXPORT_KINDS))
        .order_by(models.AIProvider.kind, models.AIProvider.priority)
    ).all()
    for r in rows:
        out.setdefault(r.kind, {})
        if r.kind not in out[r.kind] or "name" not in out[r.kind]:
            out[r.kind] = {
                "name": r.provider,
                "model": r.model,
                "endpoint": r.endpoint,
                "params": r.params or {},
            }

    # LLM 兜底：一个 llm 都没配时，用解析出来的默认对话模型补位
    if "llm" not in out:
        from ..llm_gateway import resolve_row  # 局部导入避免模块级循环依赖

        row = resolve_row(db, "chat")
        if row is not None:
            out["llm"] = {
                "name": row.provider,
                "model": row.model,
                "endpoint": row.endpoint,
                "params": row.params or {},
                "note": f"由 {row.kind} 折算（未显式配置 llm）",
            }
    return {"selected_module": out}
