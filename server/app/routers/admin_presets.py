"""管理端 · **平台人格预设**运营（``/api/admin/presets``）。

为什么单独一个 router，而不是往 ``/api/roles`` 上加
------------------------------------------------
``/api/roles`` 是**用户端**接口：越权一律404（不泄露 id 存在性），写权限是
owner 或管理员。而本模块是**管理端**：非管理员403（先鉴权后取行），
且写的是全平台共享的一行。两个模型的鉴权语义相反，混在一个 router 里
迟早会出现"这个端点该按哪套来"的争议。管理端惯例见 ``routers/support.py``
（prefix ``/api/admin/<模块>`` + router 级 ``get_current_admin`` + 不包 envelope）。

**本模块处理「调整现有平台预设」与「新建平台预设」**。新建入口
（``POST /api/admin/presets/``，**带尾斜杠**，原因见下方集合端点注释）
是 2026-10-19 用户报「只有一个人格、无法新增」
后补的：此前 ``_seed()`` 只写一个「小智」，运营想加第二种说话风格时
只能改数据库，而预设又是全平台共享行，手工改既无校验也无审计。

================================ 五条不可越界的边界 =================================
1. **可改字段只有 5 个**（``PresetPatchIn``）：``name`` / ``description`` /
   ``system_prompt`` / ``tts_voice`` / ``traits``。其余字段**不在 schema 里**
   → 提交即 422。逐项依据见 schemas.PresetPatchIn 与设计 §13.1。
2. **硬红线与 tokens 不出库**：展示一律走 ``guardrails.card_items``
   （见 :func:`_preset_out` 的注释与设计 §6.3 的泄漏路径图）。
3. **审计与业务写同事务**（4 个写入点，见 :mod:`app.persona.admin_audit`）。
4. **preview 不落库**：全程只读，不 ``add`` 不 ``commit``。
5. **平台预设的软约束只能从这里改**：用户端 ``POST /api/roles/{id}/guardrails/soft``
   对 preset 已改为 403，否则审计可被从旁边绕过（设计 §3.5，断言 T23 守）。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import get_current_admin, require_role
from ..persona import admin_audit, catalog, guardrails
from ..persona.compiler import PromptCompiler
from ..schemas import (
    PresetAuditOut,
    PresetCreateIn,
    PresetListOut,
    PresetMetaOut,
    PresetOut,
    PresetPatchIn,
    PresetResetIn,
    RolePromptOut,
    SoftGuardrailIn,
)
from .roles import build_spec_for_preset

log = logging.getLogger("elder.admin_presets")

router = APIRouter(prefix="/api/admin/presets", tags=["admin-presets"],
                   dependencies=[Depends(get_current_admin)])

#: ``reset`` 允许重置的字段，**仅这 3 个**。
#:不含 name/description（运营的品牌标识与文案，"恢复出厂"不该抹掉）、
#: 不含 archived_at（下架≠ 恢复，reset 一个已下架的预设不该把它复活）、
#: 不含软约束（运营自己勾的，reset 掉等于替他做决定）。
RESETTABLE_FIELDS: tuple[str, ...] = ("system_prompt", "traits", "tts_voice")


# ================================================================ 内部助手
def require_preset(db: Session, preset_pk: int, admin: models.User) -> models.AIProvider:
    """取一个**平台预设**行，否则 404。

    两层判据（缺一不可）：
    * ``require_role`` 判 ``is_role()`` —— 平台 LLM 配置（``record_type='config'``）
      与不存在的主键都表现为"不存在"，否则就能把平台配置当人格行改
      （改它= 设备全线无法对话，P0-1 的形状）；
    * ``is_preset()`` —— 用户自己的角色（``record_type='role'``）也不能被
      当成平台预设改，那是别人的数据。

    ``allow_archived=True``：**已下架的预设仍然要能编辑**（否则"下架后想改回
    来"就没有路径了）。
    """
    row = require_role(db, preset_pk, admin, write=True, allow_archived=True)
    if not row.is_preset():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "角色不存在")
    return row


def _bound_device_count(db: Session, preset_id: int) -> int:
    """直接绑定本预设的设备数（改 personality 后立刻影响它们）。

    ⚠️ 设备表**没有软删字段**（``Device`` 只有 ``enabled``），所以这里数的是
    全部绑定设备，不存在"设备未删除"这个额外条件。若将来给 Device 加软删，
    这里必须同步加 ``deleted_at.is_(None)`` —— 否则下架设备会被算进影响面，
    让运营看到虚高的数字而误判。
    """
    return int(db.scalar(
        select(func.count(models.Device.id))
        .where(models.Device.agent_id == preset_id)) or 0)


def _derived_role_count(db: Session, preset_id: int) -> int:
    """已派生出去的独立副本数（母版改动**不影响**它们，copy-on-write）。"""
    return int(db.scalar(
        select(func.count(models.AIProvider.id))
        .where(models.AIProvider.parent_id == preset_id,
               models.AIProvider.deleted_at.is_(None))) or 0)


def _preset_out(db: Session, row: models.AIProvider) -> PresetOut:
    """组装响应。

    ⚠️⚠️ **红线展示只能用** ``guardrails.card_items``，**绝不能**用
    ``guardrails.regenerate_locked``（设计 §6.3 / 边界表 W12）
    --------------------------------------------------------------------
    两个函数签名相似、返回结构相似，而 ``regenerate_locked`` 的 docstring 里
    恰好写着「每一项带 ``locked: True``，**供 UI 决定这一条不给编辑入口**」
    —— 这句话极易让人误以为它就是给 UI 用的。实际上它**多返回一个
    ``tokens``**，那是 ``guardrails.assert_present()`` 做 fail-closed 自检的
    **唯一判定基准**：

        catalog.HARD_GUARDRAILS[i].tokens
          └─► regenerate_locked(stored) ──► [{id,label,text,**tokens**,locked}]
                 └─► spec.guardrails_hard ──► compile() ──► assert_present()

    误用它构造响应，token 集合就随 JSON 出库到浏览器 / devtools / 网关日志，
    后果是**把门禁的判定基准暴露出去**（可反推"删哪个 token 门禁变弱"，
    并顺带发现"保留全部 token 另加一句相反的话"这类门禁本就挡不住的注入）。
    让运营知道"怎么绕"没有产品价值。断言 T10 守这条。

    另外：这一层**不是新增暴露面** —— ``GET /api/roles/{id}/guardrails``
    的承诺卡对 preset 早已登录后可读，且走的是同一个 ``card_items`` 口径。
    """
    card = guardrails.card_items(int(row.id), str(row.name or ""))
    stored = guardrails.as_dict(getattr(row, "guardrails", None))
    soft_ids = [str(item.get("id")) for item in
                guardrails.normalize_soft(guardrails.as_list(stored.get("soft")))
                if isinstance(item, dict) and item.get("id")]
    traits = dict(row.traits) if isinstance(row.traits, dict) else {}
    return PresetOut(
        id=int(row.id),
        name=str(row.name or ""),
        description=str(row.description or ""),
        system_prompt=str(row.system_prompt or ""),
        tts_voice=str(row.tts_voice or ""),
        traits=traits,
        guardrails_hard=card["items"],
        guardrails_soft=soft_ids,
        guardrail_version=str(card["version"]),
        archived=row.archived_at is not None,
        bound_device_count=_bound_device_count(db, int(row.id)),
        derived_role_count=_derived_role_count(db, int(row.id)),
        dirty_fields=_dirty_fields(row, traits),
    )


def _dirty_fields(row: models.AIProvider, traits: dict[str, Any]) -> list[str]:
    """与出厂默认不一致的字段（**后端算**，前端不做比较）。

    只算**可重置的 3 个**字段：name/description 不可重置，拿它们判"偏离默认"
    会让"运营改了个名字"被标成偏离，而 reset 也不会把它改回去 —— badge 与
    按钮行为对不上。
    """
    dirty: list[str] = []
    if str(row.system_prompt or "") != catalog.DEFAULT_PRESET_PROMPT:
        dirty.append("system_prompt")
    if traits != dict(catalog.DEFAULT_TRAITS):
        dirty.append("traits")
    if str(row.tts_voice or "") != catalog.DEFAULT_PRESET_TTS_VOICE:
        dirty.append("tts_voice")
    return dirty


# ================================================================ 读
# ⚠️ 集合端点（GET 列表 + POST 创建）**两个方法必须挂在同一条路径上**，
# 且这里选 ``"/"``（即 ``/api/admin/presets/``）而不是 ``""``。
#
# 为什么同路径：2026-10-19 先加了 ``@router.post("")``（不带斜杠）而 GET 仍在
# ``"/"``，结果 ``GET /api/admin/presets`` 从 200 变 **405**。原因是 Starlette
# 的匹配顺序：路径相同但方法不同的路由算 **PARTIAL 匹配**，会直接走该路由的
# ``handle()`` 抛 405，**根本走不到** redirect_slashes 那段兜底 —— 原来靠
# ``"/"``→``""`` 的 307 重定向被精确匹配顶掉了，而库里数据完全正常，
# 表现是「管理员可列平台预设」断言莫名变红。两个方法分居两条路径，
# 就是同一件事有两份真源，与版本号那次事故同形。
#
# 为什么选 ``"/"`` 而不是 ``""``（与 roles.py 的 ``""`` 故意不同）：
# 无尾斜杠路径 ``/api/admin/presets`` 必须继续 307 重定向到带斜杠形态 ——
# 那是 2026-10-18 线上 Mixed Content 事故的被测路径，管理端前端
# （static/index.html 的 loadPresets）至今仍用它，``smoke_proxy_headers``
# 的 C 组整组（C1/C1b/C1c/C2/C3/C4，共 6 条）都拿它当探针。
# 改成 ``""`` 会让全仓再无一条尾斜杠路由 ⇒ 那 6 条断言失去被测对象，
# 从"通过"退化成"恒真"，比红更难发现。
#
# ⚠️ 因此**所有 POST 调用方必须显式写带斜杠的** ``/api/admin/presets/``：
# urllib 的 HTTPRedirectHandler 只对 GET/HEAD 跟随 307，POST 的 307 会直接
# 抛 HTTPError（见 urllib/request.py::redirect_request 的条件），
# 前端 fetch 虽然会跟，但没必要白跑一趟。
@router.get("/", response_model=PresetListOut)
def list_presets(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    """平台预设列表（按 id **倒序**：新建的在前；本轮无新建但先定死）。"""
    rows = list(db.scalars(
        select(models.AIProvider)
        .where(models.AIProvider.record_type == models.RECORD_TYPE_PRESET)
        .order_by(models.AIProvider.id.desc())
        .limit(limit)).all())
    return PresetListOut(presets=[_preset_out(db, r) for r in rows])


@router.post("/", response_model=PresetOut, status_code=status.HTTP_201_CREATED)
def create_preset(payload: PresetCreateIn, db: Session = Depends(get_db),
                  admin: models.User = Depends(get_current_admin)):
    """新建平台人格预设。

    ⚠️ 为什么补这个端点（2026-10-19 用户报「平台人格预设中只有一个人格，
    无法新增多个人格预设」）：此前 ``_seed()`` 只写一个「小智」，管理端又只有
    PATCH / preview / reset / archive —— **没有任何创建入口** ⇒ 运营想加第二种
    说话风格（例如更活泼的"小太阳"给不同老人）时无路可走，只能改数据库。

    与 :func:`update_preset` 共用同一条 fail-closed 路径：先装配 + 编译校验
    （红线齐全）再落库。编译不过就 ``rollback``，**不留半行** ——
    一个编译不过的 preset 会让所有派生它的设备拿到不完整的提示词。

    归属字段（``record_type='preset'`` / ``kind=''`` / ``owner_id=None`` /
    ``parent_id=None``）按常量写死，不来自请求体：创建入口不该有能力决定
    "这行归谁"，那是 P0 安全边界（"用户角色被刷成 preset"那次事故的形状）。
    """
    name = payload.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "名称不能全是空白")

    row = models.AIProvider(
        name=name,
        description=(payload.description or "").strip(),
        kind="",
        record_type=models.RECORD_TYPE_PRESET,
        owner_id=None,
        parent_id=None,
        # 缺省即出厂值，与 _seed() 建的默认预设逐字段一致
        system_prompt=(payload.system_prompt
                       if payload.system_prompt is not None
                       else catalog.DEFAULT_PRESET_PROMPT),
        tts_voice=(payload.tts_voice
                   if payload.tts_voice is not None
                   else catalog.DEFAULT_PRESET_TTS_VOICE),
        traits=(dict(payload.traits) if payload.traits is not None
                else dict(catalog.DEFAULT_TRAITS)),
        # 硬红线按当前 GUARDRAIL_VERSION 注入，不接受外部传入
        guardrails=guardrails.merge_stored(None),
        enabled=True,
    )
    db.add(row)
    db.flush()  # 先拿 id：build_spec_for_preset 与审计都要用

    try:
        candidate_spec = build_spec_for_preset(row)
        PromptCompiler().compile(candidate_spec, {}, "", "")
    except Exception:
        # 编译不过 ⇒ 整个事务作废，不留一行"建了一半"的 preset
        db.rollback()
        raise

    after = admin_audit.snapshot(row)
    admin_audit.record(db, admin, row, "create", None, after)
    db.commit()
    db.refresh(row)
    log.info("管理员 id=%s 新建平台预设 id=%s「%s」", admin.id, row.id, row.name)
    return _preset_out(db, row)


@router.get("/meta", response_model=PresetMetaOut)
def presets_meta(db: Session = Depends(get_db),
                 admin: models.User = Depends(get_current_admin)):
    """档位 / 软约束白名单 / 红线 / 出厂默认，**一次性下发**（前端零硬编码）。

    ⚠️ **必须声明在 ``/{preset_pk}`` 之前**，否则 ``meta`` 会被当 int 路径参数
    解析成 422。这是 FastAPI 的路由匹配顺序问题，写在后面就晚了。
    """
    card = guardrails.card_items(0, "")
    return PresetMetaOut(
        # as_option() 是档位的唯一真源（catalog.py）。number 型的 value 是
        # **档位上界**（bands 的 upper）—— 前端必须原样回传，见 schemas.PresetMetaOut
        # 的说明与设计边界 W13。
        trait_specs=[catalog.TRAIT_SPECS[name].as_option()
                     for name in catalog.TRAIT_NAMES],
        soft_guardrails=catalog.soft_options(),
        hard_guardrails=card["items"],
        guardrail_version=catalog.GUARDRAIL_VERSION,
        defaults={
            "system_prompt": catalog.DEFAULT_PRESET_PROMPT,
            "traits": dict(catalog.DEFAULT_TRAITS),
            "tts_voice": catalog.DEFAULT_PRESET_TTS_VOICE,
        },
    )


@router.get("/{preset_pk}", response_model=PresetOut)
def get_preset(preset_pk: int, db: Session = Depends(get_db),
               admin: models.User = Depends(get_current_admin)):
    """预设详情 + 影响面统计 + 只读红线。"""
    return _preset_out(db, require_preset(db, preset_pk, admin))


# ================================================================ 写
@router.patch("/{preset_pk}", response_model=PresetOut)
def update_preset(preset_pk: int, payload: PresetPatchIn, db: Session = Depends(get_db),
                  admin: models.User = Depends(get_current_admin)):
    """运营调整平台预设（**仅这 5 个字段**，其余提交即 422）。

    流程刻意是「先装配 + 编译校验，再落库」：
    1. ``build_spec_for_preset`` 装出候选 spec（顺带做 traits 值域校验 → 400，
       此时事务未提交，库里旧值仍在）；
    2. 用候选 spec **编译一次**：``assert_present`` 若报红线缺失就抛错、
       事务不提交（fail-closed，与 ``GET /api/roles/{id}/prompt`` 同一口径）；
    3. 落库 + 写审计，**同一个事务、一次 commit**。

    编译结果**不返回给前端**（预览有专门的端点，且预览不落库）。
    """
    row = require_preset(db, preset_pk, admin)
    candidate = {k: v for k, v in {
        "name": payload.name, "description": payload.description,
        "system_prompt": payload.system_prompt, "tts_voice": payload.tts_voice,
        "traits": payload.traits,
    }.items() if v is not None}

    # fail-closed：候选值必须编译得出来（红线齐全）才允许落库
    candidate_spec = build_spec_for_preset(row, candidate)
    PromptCompiler().compile(candidate_spec, {}, "", "")

    before = admin_audit.snapshot(row)
    if payload.name is not None:
        row.name = payload.name
    if payload.description is not None:
        row.description = payload.description
    if payload.system_prompt is not None:
        row.system_prompt = payload.system_prompt
    if payload.tts_voice is not None:
        row.tts_voice = payload.tts_voice
    if payload.traits is not None:
        row.traits = dict(payload.traits)
    after = admin_audit.snapshot(row)
    admin_audit.record(db, admin, row, "update", before, after)
    db.commit()
    db.refresh(row)
    log.info("管理员 id=%s 更新平台预设 id=%s，变更字段=%s",
             admin.id, row.id, ",".join(_changed_names(before, after)))
    return _preset_out(db, row)


def _changed_names(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    changed, _ = admin_audit.build_diff(before, after)
    return changed


@router.post("/{preset_pk}/preview", response_model=RolePromptOut)
def preview_preset(preset_pk: int, payload: PresetPatchIn, db: Session = Depends(get_db),
                   admin: models.User = Depends(get_current_admin)):
    """**不落库**预览：候选值编译出来的完整 system prompt。

    ⚠️ 全程只读 —— 不 ``add``、不 ``commit``、不碰任何行。"预览"一旦落库，
    它就变成了"草稿"，而用户以为没保存其实改了（断言 T21 守）。

    与 PATCH 共用 :func:`build_spec_for_preset`，所以"所见即所得"
    （这是一个**约定**，不是结构保证 —— 见该函数 docstring 与设计边界 W10）。

    ``assert_present`` 仍然会跑：候选值若导致红线缺失，这里**直接报错**，
    而不是返回一段缺红线的 prompt —— 那会让运营看到"这就是机器人现在的
    全部设定"而它其实没带红线。
    """
    row = require_preset(db, preset_pk, admin)
    candidate = {k: v for k, v in {
        "name": payload.name, "description": payload.description,
        "system_prompt": payload.system_prompt, "tts_voice": payload.tts_voice,
        "traits": payload.traits,
    }.items() if v is not None}
    spec = build_spec_for_preset(row, candidate)
    compiler = PromptCompiler()
    return RolePromptOut(
        role_id=int(row.id), role_name=str(row.name or ""),
        prompt=compiler.compile(spec, {}, "", ""),
        sections=compiler.compile_sections(spec, {}, "", ""),
        guardrail_version=catalog.GUARDRAIL_VERSION)


@router.post("/{preset_pk}/reset", response_model=PresetOut)
def reset_preset(preset_pk: int, payload: PresetResetIn, db: Session = Depends(get_db),
                 admin: models.User = Depends(get_current_admin)):
    """恢复出厂默认（**仅 3 个字段**，见 :data:`RESETTABLE_FIELDS`）。

    **要写审计**（``action='reset'``）：reset 是**一次破坏性写入** —— 把运营
    之前的所有调整抹掉。不记的话，事后看到"预设变回默认了"既不知道谁干的、
    也不知道之前被改成了什么。"恢复动作不算变更"是日志系统的思路，不是审计
    的思路。

    ``changed_fields`` 只列**真的被改回去的**字段（值已经等于默认的不进 diff）；
    一个字段都没变时**不写审计**（什么都不变的记录只是噪音，会把真审计淹掉）。

    ⚠️ **不重置** ``archived_at``：下架与恢复出厂是两个独立动作。
    """
    row = require_preset(db, preset_pk, admin)
    fields = list(payload.fields) if payload.fields is not None else list(RESETTABLE_FIELDS)
    unknown = [f for f in fields if f not in RESETTABLE_FIELDS]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"不可重置的字段：{'/'.join(unknown)}；"
            f"可重置字段：{'/'.join(RESETTABLE_FIELDS)}")
    if not fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请至少指定一个要重置的字段")

    before = admin_audit.snapshot(row)
    if "system_prompt" in fields:
        row.system_prompt = catalog.DEFAULT_PRESET_PROMPT
    if "traits" in fields:
        # 整体覆写为出厂完整声明（preset 的 traits 本就是完整声明，没有 overlay）
        row.traits = dict(catalog.DEFAULT_TRAITS)
    if "tts_voice" in fields:
        row.tts_voice = catalog.DEFAULT_PRESET_TTS_VOICE
    after = admin_audit.snapshot(row)
    admin_audit.record(db, admin, row, "reset", before, after)
    db.commit()
    db.refresh(row)
    log.info("管理员 id=%s 把平台预设 id=%s 恢复默认（字段=%s）",
             admin.id, row.id, ",".join(fields))
    return _preset_out(db, row)


@router.post("/{preset_pk}/soft-guardrails", response_model=PresetOut)
def toggle_preset_soft(preset_pk: int, payload: SoftGuardrailIn,
                       db: Session = Depends(get_db),
                       admin: models.User = Depends(get_current_admin)):
    """勾选/取消一条软约束（**平台预设的唯一软约束写入口**）。

    单独端点而不合进 PATCH 的理由见 ``schemas.PresetPatchIn``：并入整体覆写后
    "忘带这个字段 = 全部清空"成为可能，且它需要独立的审计条目。

    未知 ``guardrail_id`` → **400**（``toggle_soft`` 抛 ValueError）：软约束是
    **7 条白名单制**，不接受自定义文本 —— 自由文本无法校验，且可能与硬红线
    直接冲突，而一条能自我否定的安全配置比没有配置更糟。
    """
    row = require_preset(db, preset_pk, admin)
    before = admin_audit.snapshot(row)
    try:
        row.guardrails = guardrails.toggle_soft(
            row.guardrails, payload.guardrail_id, bool(payload.enabled))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    after = admin_audit.snapshot(row)
    admin_audit.record(db, admin, row, "update", before, after)
    db.commit()
    db.refresh(row)
    log.info("管理员 id=%s 调整平台预设 id=%s 的软约束 %s -> %s",
             admin.id, row.id, payload.guardrail_id, payload.enabled)
    return _preset_out(db, row)


@router.get("/{preset_pk}/audits", response_model=list[PresetAuditOut])
def list_preset_audits(
    preset_pk: int,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    """审计流水，按 ``created_at`` **倒序**（最近的在前）。"""
    row = require_preset(db, preset_pk, admin)
    audits = list(db.scalars(
        select(models.AdminPresetAudit)
        .where(models.AdminPresetAudit.preset_id == int(row.id))
        .order_by(models.AdminPresetAudit.created_at.desc(),
                  models.AdminPresetAudit.id.desc())
        .limit(limit)).all())
    return [
        PresetAuditOut(
            id=int(a.id), preset_id=int(a.preset_id), preset_name=str(row.name or ""),
            admin_id=int(a.admin_id), admin_username=str(a.admin_username or ""),
            action=str(a.action or ""), changed_fields=list(a.changed_fields or []),
            diff=dict(a.diff or {}), guardrail_version=str(a.guardrail_version or ""),
            created_at=a.created_at)
        for a in audits
    ]