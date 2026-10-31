"""角色接口：人格与安全承诺的唯一入口（/api/roles）。

为什么要有这个独立接口（架构 §1.2）
----------------------------------
改造前，角色与平台 LLM 配置挤在 ``/api/providers`` 一张接口里，靠 ``kind``
是否为空来区分。那种设计有三个具体的坏处，都已经被P0 缺陷坐实：

  1. 任何人 ``GET /api/providers/safe`` 就能拿到平台配置的
     provider/endpoint/model 与角色列表（**P0-2**）；
  2. 任何人 ``DELETE /api/providers/{id}`` 就能删掉平台 LLM 配置，
     设备心跳按 ``kind.in_(DEVICE_KINDS)`` 取不到，**设备彻底无法对话**
     （**P0-1**）；
  3. 平台配置与角色互相可见，改角色时可能顺手改掉配置。

拆分之后三条性质由**结构**保证、而不是靠每次判断写对：
  * ``/api/roles`` 只认 ``is_role()`` 的行，``kind != ''`` 的配置在这里
    一律表现为"不存在"；
  * ``/api/providers`` 只认 ``is_config()`` 的行，且**仅管理员**可写；
  * 角色软删（画像与审计保留）、配置硬删（不留凭证）。

权限代号见架构 §6.1：T=登录用户、O=仅 owner、A=仅管理员、AO=owner 或管理员。
**越权一律 404**（唯一例外：preset 的 DELETE 返回 403，因为"平台预设不可删"
本身就是要给用户看的规则，见 deps 模块注释）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import get_current_admin, get_current_user, require_role
from ..memory.persona import (build_persona_prompt, copy_l2_manual, merge_profiles,
                              reset_persona)
from ..persona import admin_audit, catalog, guardrails
from ..persona import spec as spec_mod
from ..persona.compiler import ProfileMerger, PromptCompiler, effective_traits
from ..persona.spec import TraitValueError, from_row
from ..schemas import (
    GuardrailCardOut,
    GuardrailIn,
    RoleDiffItem,
    RoleDiffOut,
    RoleDeriveIn,
    RoleIn,
    RoleListOut,
    RoleOut,
    RolePromptOut,
    RoleRestoreIn,
)

log = logging.getLogger("elder.roles")

router = APIRouter(prefix="/api/roles", tags=["roles"])


# ================================================================ 装配辅助
def _parent_traits(db: Session, row: models.AIProvider) -> dict[str, Any]:
    """取母版的**声明** traits（没有母版则为空 dict）。

    派生角色的 traits 列存的是 overlay，要叠加在母版声明之上；
    原创角色与 preset 的 traits 列本身就是完整声明。
    """
    if not row.parent_id:
        return {}
    parent = db.get(models.AIProvider, int(row.parent_id))
    if parent is None:
        # 母版被硬删过（不该发生：preset 只能 archive 不能 delete）。
        # 此时按"无母版"处理，让派生角色用平台默认值而不是崩掉。
        log.warning("角色 id=%s 的母版 id=%s 不存在，按无母版处理",
                    row.id, row.parent_id)
        return {}
    return guardrails.as_dict(parent.traits)


def _spec_of(db: Session, row: models.AIProvider):
    """装配 RoleSpec（校验失败抛 400）。"""
    spec = from_row(row, _parent_traits(db, row) if row.parent_id else None)
    try:
        spec.validate()
    except TraitValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return spec


def reject_preset_write(row: models.AIProvider) -> None:
    """**平台人格预设**只能从管理端改，走用户端一律 403（断言 T31 守）。

    为什么需要它
    --------------------------------------------------------
    ``require_role(write=True)`` 对 preset 放行管理员
    （``models.AIProvider.can_write``：``preset -> bool(user.is_admin)``），
    所以**管理员能从用户端改平台预设**。这件事本身是有意设计
    （APP 侧历史上就是这么用的），但它与本轮引入的
    ``admin_preset_audits`` 组合起来就成了**审计旁路**：

        用户端 PATCH（不写审计）  +  管理端 PATCH（写审计）

    ⇒ 审计表声称「记录了平台预设的每一次改动」，而实际上有一条谁都能走、
    零留痕的旁路。**一张能被绕过的审计表等于没有审计表** ——
    这与本轮堵掉 ``guardrails/soft`` 的理由是同一条（设计 §3.5）。

    ⚠️ **这里比 ``guardrails/soft`` 更严重**：那条只影响软约束（勾选制、
    7 条白名单）；这条影响 ``update_role`` 覆盖的**全部主字段**
    （name / description / system_prompt / tts_voice / avatar_id / traits），
    而 preset 是全平台共享行 ⇒ 一次误操作影响**所有用户的角色列表**。

    ⚠️⚠️ 连带清空的坑（QA 独立复现、本轮实跑确认）
    ------------------------------------------------
    ``RoleIn`` 里 ``description`` / ``system_prompt`` / ``tts_voice``
    都有默认值 ``""``，而 ``update_role`` 对它们是**无条件整体覆写**
    （只有 ``name`` 写作 ``if payload.name``）。所以：

        PATCH {"name": "只改了名字"}   ← 只传一个字段

    会把 description / system_prompt / tts_voice **全部清空**，
    且 ``admin_preset_audits`` 里**一条记录都不会留**。

    这是「PATCH 用默认值表示未提供」与「无条件整体覆写」两个
    **各自都合理**的设计凑在一起产生的组合缺陷 —— 任何一方单独看都没错。
    正因如此**堵写入口比修默认值更彻底**：只要 preset 不能从这里改，
    清空就无从发生；而修默认值会改动**所有**用户角色的 PATCH 语义
    （那是 APP 已依赖的契约）。

    :raises HTTPException: 目标是 preset 时 403。
    """
    if row.is_preset():
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "平台人格预设请在管理端调整（人格预设 → 编辑），用户端不支持修改")


class _CandidateRow:
    """``from_row`` 的输入替身：只提供它读到的那几个属性。

    为什么不用 ORM 行也不用 dataclass：``from_row`` 一律用 ``getattr`` 读，
    所以任何提供同名属性的对象都行。用一个显式的小类（而不是 ``SimpleNamespace``）
    是为了让"这里到底提供哪些字段"在代码里一目了然 —— 将来 ``from_row``
    新增一个 ``getattr(row, ...)`` 时，本类不会静默少提供而让装配结果失真。
    """

    def __init__(self, attrs: dict[str, Any]) -> None:
        for key, value in attrs.items():
            setattr(self, key, value)


def build_spec_for_preset(row: models.AIProvider,
                          candidate: dict[str, Any] | None = None):
    """装配**平台预设**的 RoleSpec，可叠加一层「候选值」。

    ★ PATCH 与 preview **必须都调这一个函数**
    ------------------------------------------------
    如果 preview 走一条装配路径、真正保存后编译走另一条，那么预览就没有
    「所见即所得」的意义 —— 运营在预览里看到的效果不等于保存后的效果，
    而这种偏差**没有任何断言会红**（它不是 bug，是两条路慢慢分家）。
    所以这是一条**约定**，收在这里的 docstring 里让评审必查（设计边界 W10）。

    :param candidate: ``{字段名: 新值}``；``None`` 的键视为「未提供」。
        注意 ``""`` 与 ``{}`` 都**是**有效的新值（清空 description / traits），
        所以不能用 ``if not v`` 判空。
    :raises HTTPException: traits 含未知维度或非法值时 400（事务未提交）。
    """
    attrs: dict[str, Any] = {
        "id": int(row.id),
        "record_type": row.record_type,
        "guardrails": row.guardrails,
        # 平台预设没有母版：preset 行的 parent_id 恒为空，且本函数只服务
        # preset 端点，所以不传 parent_traits（from_row 会把 traits 当完整声明）。
        "parent_id": None,
        "name": str(row.name or ""),
        "description": str(row.description or ""),
        "system_prompt": str(row.system_prompt or ""),
        "tts_voice": str(row.tts_voice or ""),
        "traits": dict(row.traits) if isinstance(row.traits, dict) else {},
    }
    for key, value in (candidate or {}).items():
        if key in attrs and value is not None:
            attrs[key] = value
    spec = from_row(_CandidateRow(attrs))
    try:
        spec.validate()
    except TraitValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return spec


def _list_spec_of(db: Session, row: models.AIProvider):
    """**列表视图**用的 spec 装配：脏 traits 降级而不是 400（QA 第四轮实测）。

    为什么列表必须比详情宽松
    ----------------------
    ``_spec_of`` 校验失败会抛 400，而列表是 ``[_role_out(r) for r in rows]`` ——
    **一行**脏数据就让整个 ``GET /api/roles`` 返回 400，
    其余所有正常角色一起消失。更糟的是那行往往是 **preset**：
    它是全平台共享的一行，**所有用户**的角色列表都打不开。

    QA 实测（每轮只改一行、轮间复位）：

        ``'"str"'`` / ``'[1,2]'`` / ``'42'`` / ``null``  -> 200
        ``'{"x":1}'``                                   -> **400 整表挂**
        ``'{"humor":"nope"}'``                          -> **400 整表挂**

    前四种是「非 dict」，上一轮的 ``as_dict`` 已经挡住；
    **后两种是结构合法的 dict**，会活到 ``validate()`` 才抛 —— 那才是真缺口。

    所以这里在**进 validate 之前**就把非法项剔掉（``sanitize_traits``）。
    这不是放松校验：单角色详情 / prompt 预览 / diff 仍走 ``_spec_of`` 的
    严格路径（那里 400 是**可操作的**：用户改掉那个值就好了）。

    ⚠️ 丢弃必须留痕，且要带 role id
    ------------------------------
    静默丢弃与「用户本来就没配」在页面上**长得一模一样** ——
    运维看到空 traits 会以为用户没设置，而真相是库里存着解析不了的东西。
    """
    raw = guardrails.as_dict(row.traits, role_id=row.id, column="traits")
    _, dropped = spec_mod.known_valid_traits(raw)
    if dropped:
        log.warning(
            "角色 id=%s「%s」的 traits 含非法项，已在列表视图中剔除：%s"
            "（该值只可能来自直接改库；详情接口仍会 400，便于定位）",
            row.id, row.name, "；".join(dropped))

    # 母版声明：派生角色要叠加在它之上，母版脏同样会让 validate 抛。
    parent_raw: dict[str, Any] | None = None
    p_dropped: list[str] = []
    if row.parent_id:
        parent = db.get(models.AIProvider, int(row.parent_id))
        if parent is None:
            log.warning("角色 id=%s 的母版 id=%s 不存在，按无母版处理",
                        row.id, row.parent_id)
        else:
            parent_raw = guardrails.as_dict(parent.traits,
                                            role_id=row.parent_id,
                                            column="traits(母版)")
            _, p_dropped = spec_mod.known_valid_traits(parent_raw)
            if p_dropped:
                log.warning(
                    "角色 id=%s 的母版 id=%s traits 含非法项，已在列表视图中剔除：%s",
                    row.id, row.parent_id, "；".join(p_dropped))

    if not dropped and parent_raw is not None and not p_dropped:
        return from_row(row, parent_raw)          # 干净数据走原路径，零额外开销

    built = from_row(row, parent_raw)
    built.traits_declared = spec_mod.sanitize_traits(built.traits_declared)
    built.traits_overlay = spec_mod.sanitize_traits(built.traits_overlay)
    return built


def _role_out(db: Session, row: models.AIProvider,
              *, for_listing: bool = False) -> RoleOut:
    """组装一个角色的对外视图。

    traits 给 overlay 原样、traits_effective 给叠加母版后的生效值 ——
    两个都给，APP 才能既显示"你改了什么"又显示"实际是什么"。

    :param for_listing: 列表路径传 True —— 脏 traits 降级而不是 400 整表挂。
        理由与降级细节见 :func:`_list_spec_of`。
    """
    spec = _list_spec_of(db, row) if for_listing else _spec_of(db, row)
    profile = _effective_profile(db, row, for_listing=for_listing)
    ack_version = str(row.guardrail_ack_version or "")
    return RoleOut(
        id=row.id,
        name=row.name,
        description=row.description or "",
        system_prompt=row.system_prompt or "",
        tts_voice=row.tts_voice or "",
        avatar_id=row.avatar_id,
        is_default=bool(row.is_default),
        record_type=row.record_type,
        owner_id=row.owner_id,
        parent_id=row.parent_id,
        traits=(guardrails.as_dict(row.traits, role_id=row.id, column="traits")
                if for_listing else guardrails.as_dict(row.traits)),
        traits_effective=effective_traits(spec, profile),
        guardrail_version=catalog.GUARDRAIL_VERSION,
        guardrail_ack_at=row.guardrail_ack_at,
        # 红线版本一变就变回未确认：用户确认的是"某一个版本"，
        # 版本换了还让 UI 显示"已确认"就是在骗用户。
        guardrail_acked=bool(row.guardrail_ack_at) and
                        ack_version == catalog.GUARDRAIL_VERSION,
        deleted_at=row.deleted_at,
        archived_at=row.archived_at,
        created_at=row.created_at,
    )


def _effective_profile(db: Session, row: models.AIProvider,
                      *, for_listing: bool = False) -> dict[str, Any]:
    """该角色当前生效的画像（两层合并后的数值）。

    没有 owner 的行（平台预设）没有画像 —— 画像是按 (user_id, agent_id)
    组织的，预设角色是全平台共享的，不存在"它的画像"。

    :param for_listing: 列表路径传 True。此时**不**调 ``from_row(...).validate()``
        链，而是复用调用方已经净化过的 spec —— 否则这里会再抛一次
        TraitValueError，把 :func:`_list_spec_of` 辛苦做的降级绕过去。
    """
    if not row.owner_id:
        return {}
    try:
        layers = merge_profiles(db, int(row.owner_id), int(row.id))
    except Exception as exc:  # noqa: BLE001  画像读失败不该让角色列表整个 500
        log.warning("读取角色 id=%s 的画像失败：%s", row.id, exc)
        return {}
    if for_listing:
        declared: dict[str, Any] = {}
        if row.parent_id:
            parent = db.get(models.AIProvider, int(row.parent_id))
            declared = spec_mod.sanitize_traits(
                guardrails.as_dict(parent.traits, role_id=row.parent_id,
                                   column="traits(母版)")) if parent else {}
        declared.update(spec_mod.sanitize_traits(
            guardrails.as_dict(row.traits, role_id=row.id, column="traits")))
        return ProfileMerger.merge(layers.get("l1"), layers.get("l2"), declared)
    spec = from_row(row, _parent_traits(db, row) if row.parent_id else None)
    return ProfileMerger.merge(layers.get("l1"), layers.get("l2"),
                               spec.traits_effective())


def _stable_hash(row: models.AIProvider) -> str:
    """母版各字段的稳定哈希，供契约套件断言"派生不改母版"（INV-1）。

    用 ``json.dumps(sort_keys=True)`` 而不是 repr：字段顺序、浮点格式在两个
    Python 版本间可能不同，哈希必须只依赖**值**。

    本函数由 ``tools/smoke_roles.py`` 的 INV-1 用例经 HTTP 之外的方式调用
    （直接 import 本模块），所以它必须在模块层可导入 —— 这也是它放在这里
    而不是内联进派生逻辑的原因。
    """
    payload = {
        "name": row.name,
        "description": row.description,
        "system_prompt": row.system_prompt,
        "tts_voice": row.tts_voice,
        "traits": guardrails.as_dict(row.traits),
        "guardrails": guardrails.merge_stored(row.guardrails),
        "is_default": bool(row.is_default),
        "enabled": bool(row.enabled),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# ================================================================ 读
@router.get("", response_model=RoleListOut)
def list_roles(scope: str = Query("all", pattern="^(all|preset|mine)$"),
               db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    """列出平台预设（未下架）+ 我的角色。

    默认 scope=all（两者都要），因为 APP 的角色页需要分区渲染
    （PRD §11.1的线框）。软删的行一律不出现在这里。
    """
    base = select(models.AIProvider).where(
        models.AIProvider.deleted_at.is_(None),
        models.AIProvider.archived_at.is_(None),
        models.AIProvider.kind == "",          # 双保险：角色行的 kind 恒为空
        models.AIProvider.record_type.in_(
            (models.RECORD_TYPE_PRESET, models.RECORD_TYPE_ROLE)),
    )
    presets = list(db.scalars(
        base.where(models.AIProvider.record_type == models.RECORD_TYPE_PRESET)
        .order_by(models.AIProvider.id)).all())

    mine_rows: list[models.AIProvider] = []
    if scope in ("all", "mine"):
        q = base.where(models.AIProvider.record_type == models.RECORD_TYPE_ROLE)
        if not user.is_admin:
            # 非管理员只看自己的。管理员看自己的（不是全部人的）——
            # "我的角色"这个语义对管理员也一样，全平台角色列表另走管理端。
            q = q.where(models.AIProvider.owner_id == user.id)
        mine_rows = list(db.scalars(q.order_by(models.AIProvider.id.desc())).all())

    # ⚠️ 两个列表都传 for_listing=True：一行脏 traits 不能让整表 400
    #（preset 脏时尤其严重 —— 它是全平台共享的一行）。理由见 _list_spec_of。
    return RoleListOut(
        presets=[_role_out(db, r, for_listing=True)
                 for r in (presets if scope != "mine" else [])],
        mine=[_role_out(db, r, for_listing=True) for r in mine_rows],
    )


@router.get("/presets", response_model=list[RoleOut])
def list_presets(db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    """仅平台预设，**公开只读**（家属需要能看到有哪些官方角色）。

    仍需登录：这是一个"登录后可用"的产品页，不是营销落地页，
    而放开到匿名只会让"有哪些官方角色"这条信息无门槛外泄。
    """
    rows = db.scalars(
        select(models.AIProvider)
        .where(models.AIProvider.deleted_at.is_(None),
               models.AIProvider.archived_at.is_(None),
               models.AIProvider.record_type == models.RECORD_TYPE_PRESET,
               models.AIProvider.kind == "")
        .order_by(models.AIProvider.id)).all()
    # 同 list_roles：列表路径降级，一行脏数据不许打挂整表。
    return [_role_out(db, r, for_listing=True) for r in rows]


@router.get("/traits")
def list_trait_specs(user: models.User = Depends(get_current_user)):
    """10 个性格维度的档位定义（含后果说明），供 APP 渲染设置页。

    APP **不硬编码**档位文案：档位会随运营调整，改前端就意味着要发版。
    """
    from ..persona.compiler import describe_traits

    return {
        "traits": describe_traits(),
        "soft_guardrails": catalog.soft_options(),
        "guardrail_version": catalog.GUARDRAIL_VERSION,
    }


@router.get("/{role_pk}", response_model=RoleOut)
def get_role(role_pk: int, db: Session = Depends(get_db),
             user: models.User = Depends(get_current_user)):
    """角色详情。preset 任何人可读；role 仅 owner / 管理员（越权 404）。"""
    row = require_role(db, role_pk, user)
    return _role_out(db, row)


# ================================================================ 写
def _clamp_voice(db: Session, voice: str) -> str:
    """把角色音色收敛到**当前生效 TTS 厂商**的清单里（写路径统一过这一道）。

    为什么必须在**写**的时候收敛，而不是读的时候兜底
    ----------------------------------------------
    音色 ID 不跨厂商通用（Edge 是 ``zh-CN-XiaoxiaoNeural``、腾讯云是纯整数
    ``601009``）。而角色只有一个 ``tts_voice``，最终会被原样塞进当前生效那条
    TTS 配置的请求里。塞错了不会报错 —— ``asr_tts._shape_ok_voice`` 会**静默
    置空**（宁可让厂商用自己的默认音色，也不能整句没声音）。

    生产上真实发生过：生效的是腾讯云，角色里存的是 Edge 的音色名，
    那个音色**从来没生效过**，而用户在页面上一直看到「晓晓」。
    写时收敛让"存下来的值"与"实际生效的值"永远一致，用户不会看到假的选中态。

    三种放行，其余一律回落（顺序即判据）
    ----------------------------------
      1. **空串原样返回** —— 它的语义是「继承形象 / 配置行」，不是无效值；
      2. 自建 / 认不出厂商（``allow_custom``）—— 那家的音色名我们无从校验，
         拦它等于让自建 TTS 彻底不可用；
      3. 在该厂商清单里 —— 正常路径。

    第 3 条之外的值回落到 ``default_voice`` 并留一条 warning：
    **回落必须留痕**，否则「我明明选了音色」会变成没有线索的悬案。

    :return: 可安全落库的音色（空串 / 原值 / 该厂商默认值）。
    """
    voice = voice or ""
    if not voice.strip():
        return voice
    from ..voice import catalog as voice_catalog

    opts = voice_catalog.options_for(db)
    if opts.get("allow_custom"):
        return voice
    if voice.strip() in {str(v.get("id") or "") for v in (opts.get("voices") or [])}:
        return voice
    fallback = str(opts.get("default_voice") or "")
    log.warning(
        "角色音色 %r 不在当前 TTS 厂商 %s 的音色清单里，已回落到默认 %r"
        "（各厂商音色 ID 不通用，留着它只会静默失效）",
        voice, opts.get("vendor") or "自建", fallback)
    return fallback


@router.post("", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
def create_role(payload: RoleIn, db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    """**原创**一个角色（parent_id 必须为空 —— 派生请用 /derive）。

    一律 record_type='role' + owner_id=当前用户。这两个字段**不接受请求体
    覆写**：归属是安全边界，不能由调用方自称。
    """
    try:
        # 先校验再落库：非法 trait 绝不能进库，否则角色列表会因一条脏数据
        # 整个打不开（_role_out 里每行都要 validate）。
        if payload.traits:
            for name, value in payload.traits.items():
                spec = catalog.TRAIT_SPECS.get(name)
                if spec is None:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        f"未知的性格维度 {name!r}；合法取值：{'/'.join(catalog.TRAIT_NAMES)}")
                if not spec.is_valid(value):
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        f"性格维度 {name!r}（{spec.label}）取值非法：{value!r}；"
                        f"合法范围 {spec.describe_range()}")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001  兜底：校验层不该把 500 抛给调用方
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"参数不合法：{exc}") from exc

    row = models.AIProvider(
        name=payload.name,
        description=payload.description,
        system_prompt=payload.system_prompt,
        tts_voice=_clamp_voice(db, payload.tts_voice),
        avatar_id=payload.avatar_id,
        kind="",                      # 角色行的 kind 恒为空（不参与下发/导出）
        record_type=models.RECORD_TYPE_ROLE,
        owner_id=user.id,             # 归属当前用户，不接受请求体指定
        parent_id=None,               # 原创
        traits=dict(payload.traits or {}),
        guardrails=guardrails.merge_stored(None),
        enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    log.info("用户 id=%s 创建角色 id=%s「%s」", user.id, row.id, row.name)
    return _role_out(db, row)


@router.post("/{role_pk}/derive", response_model=RoleOut,
             status_code=status.HTTP_201_CREATED)
def derive_role(role_pk: int, payload: RoleDeriveIn, db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    """从预设或自己的角色**派生**一份新的（copy-on-write 物理复制）。

    派生的是**数据副本**而不是引用：母版之后被改（甚至下架），
    已派生的角色不受影响 —— 这条由契约套件 INV-1 断言。

    ``inherit`` 默认 True（用户已拍板）：复制母版的 L2 **手动画像**
    （家属对这个角色的交代），**不复制** L1 统计值 —— 统计是"这个人跟机器人
    互动出来的结果"，跨角色复制会让新角色一上来就带着不属于它的历史。

    复制来的 traits 是空overlay：用户还没改过任何一项，之后每改一项
    才往 overlay 里加一项。「还原为预设」因此就是清空 overlay，一行搞定。
    """
    parent = require_role(db, role_pk, user)

    row = models.AIProvider(
        name=payload.name,
        description=parent.description or "",
        system_prompt=parent.system_prompt or "",
        # 母版多半是**平台预设**，而预设的 tts_voice 是出厂值（Edge 的音色名）。
        # 部署换成腾讯云之后，原样复制等于把失效值再复制一份 —— 副本从此
        # 「页面上显示选了音色，实际一直是厂商默认」。派生同样要过归一化。
        tts_voice=_clamp_voice(db, parent.tts_voice or ""),
        avatar_id=parent.avatar_id,
        kind="",
        record_type=models.RECORD_TYPE_ROLE,
        owner_id=user.id,
        parent_id=parent.id,
        traits={},                                  # overlay 从空开始
        # hard 段深拷贝并由常量再生；soft 段按白名单重新归一
        guardrails=guardrails.merge_stored(parent.guardrails),
        is_default=False,
        enabled=True,
    )
    db.add(row)
    db.flush()

    if payload.inherit:
        try:
            copy_l2_manual(db, parent.owner_id or user.id, parent.id, int(row.id))
        except Exception as exc:  # noqa: BLE001  画像复制失败不该让建角色失败
            log.warning("派生角色 id=%s 时复制 L2 画像失败：%s", row.id, exc)

    db.commit()
    db.refresh(row)
    log.info("用户 id=%s 从 id=%s 派生角色 id=%s「%s」（inherit=%s）",
             user.id, parent.id, row.id, row.name, payload.inherit)
    return _role_out(db, row)


@router.patch("/{role_pk}", response_model=RoleOut)
def update_role(role_pk: int, payload: RoleIn, db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    """改角色（owner 或管理员）。preset 仅管理员。

    ⚠️ **guardrails 的 hard 段不在 RoleIn schema 里**，所以它**物理上无法提交**
    —— 不是靠"校验时忽略非法字段"，而是没有这个字段可传。
    这比任何校验都可靠：绕过接口直接改库也没用，因为编译时一律用常量再生
    （见 persona/guardrails.py）。

    ⚠️⚠️ **平台人格预设一律 403**（本轮新增，断言 T31 守）
    ------------------------------------------------
    目标为 preset 时整个端点拒绝，理由与危害见
    :func:`reject_preset_write`。一句话版本：这里放行 admin，
    而本函数对 ``description`` / ``system_prompt`` / ``tts_voice``
    是**无条件整体覆写**（``RoleIn`` 里它们的默认值是 ``""``），
    所以 admin 误传 ``{"name": "..."}`` 就会**把三者全清空**，
    且不留任何审计 —— 全平台共享行被清空，影响所有用户。

    影响面：preset 的用户端写入口**目前没有任何 UI**
    （管理台改走 /api/admin/presets，APP 未接角色编辑），实际调用方为零。
    为什么是 403 而不是 404：这是**产品规则**不是安全边界，
    与 preset 的 DELETE / guardrails/soft 返回 403 同理。
    """
    row = require_role(db, role_pk, user, write=True)
    reject_preset_write(row)
    if row.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "角色不存在")

    if payload.name:
        row.name = payload.name
    row.description = payload.description
    row.system_prompt = payload.system_prompt
    row.tts_voice = _clamp_voice(db, payload.tts_voice)
    row.avatar_id = payload.avatar_id
    if payload.traits:
        for name, value in payload.traits.items():
            spec = catalog.TRAIT_SPECS.get(name)
            if spec is None:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"未知的性格维度 {name!r}；合法取值：{'/'.join(catalog.TRAIT_NAMES)}")
            if not spec.is_valid(value):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"性格维度 {name!r}（{spec.label}）取值非法：{value!r}；"
                    f"合法范围 {spec.describe_range()}")
        row.traits = dict(payload.traits)
    # record_type / owner_id / parent_id **刻意不改**：它们是归属与派生关系，
    # 改归属必须走显式的路径（P0 安全边界不能靠 PATCH 顺手改掉）。
    db.commit()
    db.refresh(row)
    return _role_out(db, row)


@router.delete("/{role_pk}", status_code=status.HTTP_204_NO_CONTENT)
def delete_role(role_pk: int, db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    """**软删**角色 + 自动解绑设备。

    为什么软删而不是真删（架构 §1.4）：``AgentPersonaProfile`` /
    ``AgentPreferenceExample`` / ``AgentPersonaAudit`` 都以 (user_id, agent_id)
    组织，而"审计不可让渡"是产品底线 —— 真删会连带丢审计。同一哲学在
    ``persona.delete_example``（软作废 + 留痕）里已经用过一次。

    preset 一律 **403**：硬删母版会让所有派生角色的 parent_id 指向一行
    不存在的记录。管理员想下架走 /archive。
    """
    row = require_role(db, role_pk, user, write=True)
    if row.is_preset() and not user.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "平台预设角色不可删除，管理员可在管理端将其下架")
    if row.is_preset():
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "平台预设角色不可删除（会导致派生角色的来源失效），请改用下架")

    row.deleted_at = models.utcnow()
    row.enabled = False
    # 自动解绑：否则设备上仍指向一个已删角色，编译时会拿到软删行的空 spec。
    for dev in db.scalars(select(models.Device).where(
            models.Device.agent_id == row.id)).all():
        dev.agent_id = None
    db.commit()


@router.post("/{role_pk}/restore", response_model=RoleOut)
def restore_role(role_pk: int, payload: RoleRestoreIn, db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    """还原为预设：清空 traits overlay、system_prompt/tts_voice 回到母版当前值、
    guardrails 重新深拷贝。

    **保留画像与示例**（架构 §6.1）：用户想撤掉的只是"我改的那些设定"，
    而机器人学到的说话习惯与审计记录不属于"设定"，一并清掉等于让家属
    重新教一遍 —— 那是数据损失，不是"还原"。
    """
    row = require_role(db, role_pk, user, write=True)
    if row.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "角色不存在")
    if not row.parent_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "原创角色没有母版可还原；你可以在设置里直接改回想要的数值")

    parent = require_role(db, int(row.parent_id), user, allow_archived=True)

    row.traits = {}                                  # 清空 overlay
    row.system_prompt = parent.system_prompt or ""  # 回到母版当前值
    # 母版可能是**另一家厂商**的音色（换过 TTS 配置后不会自动清理），
    # 照抄等于把失效值再传播一次，所以还原也要过归一化。
    row.tts_voice = _clamp_voice(db, parent.tts_voice or "")
    row.description = parent.description or ""
    row.guardrails = guardrails.merge_stored(parent.guardrails)
    db.commit()
    db.refresh(row)
    log.info("用户 id=%s 把角色 id=%s 还原为预设（母版 id=%s）", user.id, row.id, parent.id)
    return _role_out(db, row)


# ================================================================ 人格画像：显式重置
class PersonaResetIn(BaseModel):
    """显式重置画像的入参。

    ``confirm`` 必须是 true —— 这个动作不可逆（清统计 / manual / 示例池），
    默认 false 让"手滑调用"直接 400，而不是悄悄清掉一位老人的画像。
    """

    user_id: int | None = None    # None = 当前登录用户
    agent_id: int | None = None   # None = L0（换了一位老人）；指定 = 该角色重来
    confirm: bool = False


@router.post("/persona/reset")
def reset_persona_profile(payload: PersonaResetIn, db: Session = Depends(get_db),
                          user: models.User = Depends(get_current_user)):
    """**显式**重置语气画像（换了一位老人 / 这个角色重来）。

    ⚠️ 路径为什么是 ``/api/roles/persona/reset`` 而不是 ``/api/persona/reset``：
    ``main.py`` 只 include 已登记的 router，而本轮改动范围不含 ``main.py``；
    新建 ``routers/persona.py`` 也不会被注册 —— 那会静默 404，比路径难看更糟。
    所以挂在角色接口下：画像本来就是"角色 ↔ 老人"的交叉数据。

    语义（细节见 ``memory.persona.reset_persona``）：
      * ``agent_id=None`` -> 清 L0 统计 + 全部 manual + 整个示例池（换人）；
      * ``agent_id`` 指定 -> 只清该角色的 L2 manual 与它的示例（角色重来）；
      * ``AgentPersonaAudit`` **一律保留**，并额外写一条 ``signal=reset`` 的审计。

    权限：只能重置**自己**的画像；管理员可指定 ``user_id`` 重置他人。
    ``agent_id`` 给定时还要过 ``require_role``（越权一律 404，与本文件其它接口
    同口径）—— 但用的是**读**权限：平台预设角色人人都能用，家属给自己正在用的
    预设角色"重来"一次是完全正常的诉求，用写权限会把预设角色挡在外面。

    ⚠️ 这里**不**解绑设备、**不**改任何角色字段：重置的只是"机器人学到的
    说话方式"。解绑可能只是换硬件，不等于换人（见 devices.unbind_device）。
    """
    if not payload.confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "重置不可逆，请显式传 confirm=true")

    target_id = int(payload.user_id) if payload.user_id is not None else int(user.id)
    if target_id != int(user.id) and not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能重置自己的画像")
    target = db.get(models.User, target_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")

    if payload.agent_id is not None:
        # 用**被重置者**的身份判可见性：管理员替别人重置时，判的是"那个人
        # 能不能看到这个角色"，而不是管理员自己。
        require_role(db, int(payload.agent_id), target, allow_archived=True)

    result = reset_persona(db, target_id, payload.agent_id)
    db.commit()
    log.warning("用户 id=%s 显式重置人格画像（user_id=%s agent_id=%s）：%s",
                user.id, target_id, payload.agent_id, result)
    return result


@router.post("/{role_pk}/archive")
def archive_role(role_pk: int, db: Session = Depends(get_db),
                 admin: models.User = Depends(get_current_admin)):
    """平台预设**下架**（archived_at）。仅管理员。

    有派生角色或设备绑定时返回 **409 + 引用方清单** —— 这是**引导性错误**
    而不是安全边界，所以给出足够信息让管理员去处理（先迁移派生、再下架），
    而不是一句"有引用无法下架"。

    ⚠️ 本次**新增一行审计写入**（``action='archive'``），不新增端点面。
    下架是结构性变化（预设不再出现在用户可选列表里），最需要留痕；
    而它唯一的写入口就是这个已有端点，所以审计也必须落在这里 ——
    审计与业务写**同一个事务**（理由见 ``persona/admin_audit`` 模块注释）。
    """
    row = require_role(db, role_pk, admin, write=True, allow_archived=True)
    if not row.is_preset():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有平台预设角色可以下架")

    derived = list(db.scalars(select(models.AIProvider).where(
        models.AIProvider.parent_id == row.id,
        models.AIProvider.deleted_at.is_(None))).all())
    bound = list(db.scalars(select(models.Device).where(
        models.Device.agent_id == row.id)).all())

    if derived or bound:
        # ⚠️ detail 必须是**字符串**（A19：前端 api() 直接 new Error(data.detail)，
        # dict 会渲染成 [object Object]），所以引用清单拼进这句话里，
        # 而不是塞一个 dict 出去。
        #
        # ⚠️⚠️ 这里是本轮修掉的**既有 bug**（归属 7200c28 baseline，非本次引入）
        # ----------------------------------------------------------------------
        # 原写法是 ``HTTPException(409, "该预设仍有引用，请先处理", detail={...})``
        # —— **detail 被传了两次**。``HTTPException.__init__`` 的第二个形参就叫
        # ``detail``，于是 Python 在**构造异常的那一行**就抛
        # ``TypeError: got multiple values for argument 'detail'``，
        # 端点变成 **500 而不是 409**（本机Python 3.14 + FastAPI 实测）。
        #
        # 为什么一直没人发现：这条分支要求「预设**同时**有派生角色或设备绑定」
        # 才进得去，而既有冒烟从未在**有引用**的状态下试过 archive。
        # 也就是说这是一条**从未被执行过**的代码路径 ——
        # 与 server-ci 那个「空步骤 → jobs=0」同一类问题：
        # **没跑过的代码不等于对的代码。** 本轮 T26 断言把它第一次跑起来。
        refs: list[str] = []
        if derived:
            refs.append("派生角色 " + "、".join(
                f"{d.name}(id={d.id})" for d in derived))
        if bound:
            refs.append("绑定设备 " + "、".join(
                f"{d.device_id or d.id}(id={d.id})" for d in bound))
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "该预设仍有引用，请先处理后下架 —— " + "；".join(refs))
    before = admin_audit.snapshot(row)
    row.archived_at = models.utcnow()
    after = admin_audit.snapshot(row)
    admin_audit.record(db, admin, row, "archive", before, after)
    db.commit()
    return {"ok": True, "id": row.id, "archived_at": row.archived_at}


# ================================================================ diff / prompt
@router.get("/{role_pk}/diff", response_model=RoleDiffOut)
def role_diff(role_pk: int, db: Session = Depends(get_db),
              user: models.User = Depends(get_current_user)):
    """与母版的差异清单：改了哪些字段、哪些 trait。

    diff 直接就是 ``overlay`` 与母版声明的差集 —— 这正是 traits 用overlay
    存储换来的好处（架构 §1.3 第4 条）：不需要额外维护一份"我改了什么"。

    ⚠️ 契约决策：**全量 items + changed 标记**，不拆成"仅变更 items +
    modified_traits"（2026-10-16 主理人拍板，架构 §6.1 决策记录 D-3）
    ------------------------------------------------------------------
    下面是拍板的四条理由，**照抄在这里是为了防止后人来"修"它**：

    1. **向后兼容、纯增量** —— APP 侧现有遍历 ``items`` 的代码完全不受影响。
       拆成两个数组是破坏性变更：旧版 APP 拿到 ``changed_items=None`` 时
       要么崩、要么显示"你没有改动过任何设置"。
    2. **APP 还没接角色详情/还原页面** —— 现在做破坏性变更零收益。
       没有消费方的时候改契约，只是在给未来添迁移成本。
    3. **全量 + 标记更适合 UI 渐进** —— APP 可以**先继续按items 渲染**
       （行为与从前完全一致），等接了详情/还原页面再按 ``changed`` 过滤。
       一个字段支持两种用法，不需要两次发版。
    4. **将来真不够用时，迁移成本很低** —— 拆成两个数组是纯服务端改动，
       到时再迁也不迟。反过来现在拆、以后要合回来才叫麻烦。

    ⚠️ 因此**严禁修改或删除任何既有字段名与结构**
    （``items`` / ``parent_value`` / ``current_value`` / ``label`` /
    ``field``）：它们是 APP 已依赖的契约，改名等于让线上 APP 崩。
    这条由 eval 套件的 ``ok_contract_v2`` 断言守着（反向验证见
    ``tools/qa5_reverse.py`` 的 M2：把 ``changed`` 改名 -> eval 立刻红）。

    ⚠️ ``items`` 里混着两类项，用 ``changed`` 区分（QA 报告的 Bug 2）
    -----------------------------------------------------------
    * ``changed=True``  —— 用户确实改了（overlay 里有且与母版不同）；
    * ``changed=False`` —— 继承自母版、**未被修改**，列出来只为让 UI 说明
      "这一项跟平台一致"。

    为什么要显式区分：``restore``（还原为预设）清空 overlay 之后，这里仍会
    返回 10 个 ``traits.*`` 项且 ``parent_value == current_value``。APP 若按
    「items 非空 = 有改动」渲染，还原后会误显示"你改了 10 项"。
    """
    row = require_role(db, role_pk, user, write=True)
    parent: models.AIProvider | None = None
    if row.parent_id:
        parent = db.get(models.AIProvider, int(row.parent_id))

    items: list[RoleDiffItem] = []
    overlay = guardrails.as_dict(row.traits)
    parent_traits = guardrails.as_dict(parent.traits) if parent is not None else {}

    for name in catalog.TRAIT_NAMES:
        in_overlay = name in overlay
        parent_value = parent_traits.get(name)
        if in_overlay and overlay[name] != parent_value:
            items.append(RoleDiffItem(
                field=f"traits.{name}",
                label=catalog.TRAIT_SPECS[name].label,
                parent_value=parent_value, current_value=overlay[name],
                changed=True))
        elif parent_value is not None and not in_overlay:
            # 继承自母版，未被修改 —— 列出便于 UI 说明"这一项跟平台一致"，
            # 但changed=False：它**不是**一处"差异"，不能计入"你改了 N 项"。
            items.append(RoleDiffItem(
                field=f"traits.{name}",
                label=catalog.TRAIT_SPECS[name].label,
                parent_value=parent_value, current_value=parent_value,
                changed=False))

    for field_name, label in (("system_prompt", "家属特别交代"),
                              ("tts_voice", "音色"),
                              ("description", "描述")):
        cur = getattr(row, field_name, "") or ""
        par = (getattr(parent, field_name, "") or "") if parent is not None else ""
        if cur != par:
            # 这三个字段只在**真的不同**时才进 items，所以恒为 changed=True。
            items.append(RoleDiffItem(field=field_name, label=label,
                                      parent_value=par, current_value=cur,
                                      changed=True))

    return RoleDiffOut(id=row.id,
                       parent_id=row.parent_id,
                       parent_name=(parent.name if parent is not None else ""),
                       items=items,
                       changed_count=sum(1 for i in items if i.changed))


@router.get("/{role_pk}/prompt", response_model=RolePromptOut)
def role_prompt(role_pk: int, db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    """编译产物预览：家属可见的透明窗口，同时是 eval 的入口。

    ⚠️ 红线缺失时这里会抛 500（GuardrailViolation）而**不是**返回一段
    缺红线的 prompt。宁可让页面报错，也不能让家属看到"这才是机器人现在的
    全部设定"而它其实没带红线 —— 那会让红线问题对用户不可见。
    """
    row = require_role(db, role_pk, user, write=True)
    spec = _spec_of(db, row)
    profile = _effective_profile(db, row)
    examples = ""
    if row.owner_id:
        try:
            examples = build_persona_prompt(db, int(row.owner_id), int(row.id))
        except Exception as exc:  # noqa: BLE001  示例读失败不该让预览整个失败
            log.warning("读取角色 id=%s 的偏好示例失败：%s", row.id, exc)

    compiler = PromptCompiler()
    prompt = compiler.compile(spec, profile, "", examples)
    sections = compiler.compile_sections(spec, profile, "", examples)
    return RolePromptOut(role_id=row.id, role_name=row.name, prompt=prompt,
                         sections=sections,
                         guardrail_version=catalog.GUARDRAIL_VERSION)


# ================================================================ 承诺卡
@router.get("/{role_pk}/guardrails", response_model=GuardrailCardOut)
def get_guardrail_card(role_pk: int, db: Session = Depends(get_db),
                       user: models.User = Depends(get_current_user)):
    """安全承诺卡数据：人话红线清单 + locked 状态 + 是否已确认。

    返回的 hard 段一律由**代码常量**再生，不读 DB 里那份 ——
    这张卡必须与真正编译进 prompt 的内容一致，否则就成了"承诺给用户看的
    和实际执行的不是一回事"。
    """
    row = require_role(db, role_pk, user)
    stored = guardrails.as_dict(row.guardrails)
    card = guardrails.card_items(row.id, row.name)
    card["soft"] = guardrails.normalize_soft(guardrails.as_list(stored.get("soft")))
    ack_version = str(row.guardrail_ack_version or "")
    card["acked"] = bool(row.guardrail_ack_at) and ack_version == catalog.GUARDRAIL_VERSION
    card["ack_at"] = row.guardrail_ack_at
    card["ack_version"] = ack_version
    return GuardrailCardOut(**card)


@router.post("/{role_pk}/guardrails/soft", response_model=GuardrailCardOut)
def toggle_soft_guardrail(role_pk: int, payload: GuardrailIn, db: Session = Depends(get_db),
                          user: models.User = Depends(get_current_user)):
    """追加 / 移除一条**软性**约束（owner 或管理员）。

    只允许平台白名单里的 id（架构 §12 U4）：自由文本无法校验，
    而且可能与硬红线直接冲突 —— 一条能自我否定的安全配置比没有配置更糟。

    ⚠️ 目标为**平台预设**时一律403（本次的行为变更，断言 T23 守）
    ------------------------------------------------------------------
    改之前这里是**能改的**：`require_role(write=True)` → ``can_write``
    对 preset 是 ``bool(user.is_admin)`` ⇒ **任何管理员都能从这条用户端
    路径改平台预设的软约束，而``admin_preset_audits`` 里一条记录都不会留**。
    两条写路径、只有一条写审计，等于审计可以被从旁边绕过 —— 那等于没有审计。

    所以：**改 preset 的软约束只能走** ``POST /api/admin/presets/{id}/soft-guardrails``。
    影响面：现状该路径**没有任何 UI 入口**（管理台无角色页、APP 未接），
    实际调用方为零。

    为什么是 403 而不是 404：这是**产品规则**不是安全边界 ——
    "平台预设的软约束归管理端管"这条规则本身就是要给管理员看的，
    与 preset 的 DELETE 返回 403 同理（``deps`` 模块注释已论证过这个例外）。

    ⚠️ 判定与理由统一收在 :func:`reject_preset_write` 里，与
    ``update_role`` 共用**同一个守卫函数** —— 这是刻意的：
    两处各写一遍 ``if row.is_preset(): raise 403`` 的话，改了一处忘了另一处
    就是下一个旁路，而这种旁路**不会被任何断言抓到**（因为断言通常只盯
    其中一个入口）。收在一处后，" preset 的用户端写入口"就有了唯一清单。
    """
    row = require_role(db, role_pk, user, write=True)
    reject_preset_write(row)
    try:
        row.guardrails = guardrails.toggle_soft(row.guardrails, payload.guardrail_id,
                                                bool(payload.enabled))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    db.refresh(row)
    card = guardrails.card_items(row.id, row.name)
    card["soft"] = guardrails.normalize_soft(guardrails.as_list(guardrails.as_dict(row.guardrails).get("soft")))
    card["acked"] = bool(row.guardrail_ack_at) and \
        str(row.guardrail_ack_version or "") == catalog.GUARDRAIL_VERSION
    card["ack_at"] = row.guardrail_ack_at
    card["ack_version"] = str(row.guardrail_ack_version or "")
    return GuardrailCardOut(**card)


@router.post("/{role_pk}/guardrails/ack", response_model=GuardrailCardOut)
def ack_guardrails(role_pk: int, db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    """一次性确认承诺卡：记确认人 + 时间 + **当时的红线版本**。

    记版本而不是记一个布尔值，是为了让"红线更新了要重新确认"这件事可判定：
    比较 ``guardrail_ack_version`` 与当前常量即可，不依赖任何外部状态。
    """
    row = require_role(db, role_pk, user, write=True)
    row.guardrail_ack_at = models.utcnow()
    row.guardrail_ack_by = user.id
    row.guardrail_ack_version = catalog.GUARDRAIL_VERSION
    db.commit()
    db.refresh(row)
    card = guardrails.card_items(row.id, row.name)
    card["soft"] = guardrails.normalize_soft(guardrails.as_list(guardrails.as_dict(row.guardrails).get("soft")))
    card["acked"] = True
    card["ack_at"] = row.guardrail_ack_at
    card["ack_version"] = row.guardrail_ack_version
    return GuardrailCardOut(**card)