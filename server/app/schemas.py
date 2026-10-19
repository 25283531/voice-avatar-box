"""Pydantic 进出参模型（Pydantic v2）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .model_catalog import API_STYLES, KIND_LIST
from .schedule import parse_hhmm


class ORMBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ============================================================ 认证
class UserCreate(ORMBase):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    nickname: str = ""
    phone: str = ""
    email: str = ""


class UserLogin(ORMBase):
    username: str
    password: str


class EmailChangeIn(ORMBase):
    """更换绑定邮箱（APP「我的」页）。

    只校验「长得像邮箱」，不做真实性验证（不发验证邮件）：
    本项目是私有化部署的家庭场景，邮箱主要用于展示与找回线索，
    引入邮件服务会把部署复杂度拉高一大截。
    """

    email: str = Field(min_length=3, max_length=128)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        v = v.strip()
        if v.count("@") != 1 or v.startswith("@") or v.endswith("@"):
            raise ValueError("邮箱格式不正确")
        domain = v.split("@")[1]
        if "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise ValueError("邮箱格式不正确")
        return v


class PasswordChangeIn(ORMBase):
    """修改密码。新密码长度下限与注册保持一致（6 位）。"""

    old_password: str
    new_password: str = Field(min_length=6, max_length=128)


class TokenOut(ORMBase):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    username: str
    is_admin: bool = False


class UserOut(ORMBase):
    """账户信息。GET /api/auth/me 与 PATCH /api/auth/email 共用同一结构，
    对应 APP 的 ElderAccount（用户名 / 昵称 / 手机 / 邮箱 / 是否管理员）。"""

    id: int
    username: str
    nickname: str
    phone: str
    email: str = ""
    is_admin: bool
    created_at: datetime


# ============================================================ 设备
class DeviceCreate(ORMBase):
    device_id: str = Field(min_length=6, max_length=64)
    name: str = "我的陪伴助手"


class DeviceUpdate(ORMBase):
    name: str | None = None
    avatar_id: int | None = None
    settings: dict[str, Any] | None = None


class DeviceOut(ORMBase):
    id: int
    device_id: str
    name: str
    owner_id: int | None
    avatar_id: int | None
    # agent_id：设备当前绑定的 agent 角色（ai_providers.record_type='role'）。
    # **不是新泄露的口子** —— 设备详情本来就带 owner_id，而 require_device_owner
    # 保证只有 owner 与管理员能拿到这条记录；前端设备表（user.html 的「人格角色」
    # 下拉）就是靠它回显绑定态，此前缺这一列导致每次刷新都显示「未设置」，
    # 用户重选一次才「像是生效了」。
    agent_id: int | None
    online: bool
    last_seen_at: datetime | None
    fw_version: str
    ip: str
    settings: dict[str, Any]
    created_at: datetime


class DeviceAgentIn(ORMBase):
    """给设备绑定一个 agent 角色（POST /api/devices/{pk}/agent）。

    role_id 指向 ai_providers 里的一条记录，与 APP「创建我的 agent 角色」
    （POST /api/providers）用的是同一张表，保证角色列表只有一个来源。
    """

    role_id: int


class DeviceAgentOut(ORMBase):
    ok: bool = True
    device_pk: int
    role_id: int
    role_name: str


class BindIn(ORMBase):
    """APP 侧用激活码绑定设备。"""
    code: str
    device_id: str | None = None   # 通用码场景下由 APP 上报设备标识
    alias: str = ""


class BindOut(ORMBase):
    device_id: str
    device_pk: int
    device_token: str
    name: str


class ActivationCodeOut(ORMBase):
    code: str
    display: str
    expires_at: datetime
    device_id: str | None = None


# ============================================================ 形象
class AvatarIn(ORMBase):
    """形象（纯外观）。**刻意不含 system_prompt**。

    人格已统一到 AIProvider（架构 §4.2）。这里即便留着那个字段也不会有用 ——
    它对每个角色都一样，而每个角色的 system_prompt 各不相同。留着只会让
    调用方误以为改形象能改说话方式。
    tts_voice 仍在：它是「形象的**默认**音色」，角色自带音色时以角色的为准。
    """

    name: str
    description: str = ""
    eaf_pack_name: str = ""
    eaf_pack_url: str = ""
    eaf_pack_sha256: str = ""
    screen_width: int = 320
    screen_height: int = 240
    clips: dict[str, str] = Field(default_factory=dict)
    tts_voice: str = ""
    emotion_lights: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class AvatarOut(AvatarIn):
    id: int
    # 归属：preset = 平台形象（仅管理员可写）；role = 用户形象
    record_type: str = "preset"
    owner_id: int | None = None
    created_at: datetime


# ============================================================ AI 供应商（仅平台配置）
# 角色已彻底拆到 /api/roles（见本文件下方的 Role* schema）。
# 因此 ProviderIn 现在**只描述平台配置**：kind 必填非空。
# 这不是为了"接口好看"，而是一条物理保证 ——
# 现状（修复前）允许 kind="" 建行，而这种行会被 device_api 按
# `kind.in_(DEVICE_KINDS)` 之外的路径处理、或被 APP 当成真配置用；
# 一个没有厂商与模型的"配置"一旦混进下发包就会顶掉真实的 LLM 配置。
class ProviderIn(ORMBase):
    name: str
    # 用校验器而不是 regex pattern：候选值是会增长的，写死在 pattern 里每加一种
    # 能力就得改一处字符串，漏改就变成「接口能存进去、列表却查不出来」的玄学问题。
    kind: str
    provider: str = ""
    endpoint: str = ""
    api_key: str = ""
    model: str = ""
    # ---- 调用层元信息（读取方见 app/llm_gateway.py）----
    # 厂商 API 风格：决定调用层拼哪种报文。空串 = 默认 OpenAI 兼容。
    api_style: str = ""
    # 是否支持流式返回；将来语音链路做逐句合成时会用到
    supports_stream: bool = False
    # 单次请求超时（秒）。本地小模型首 token 可能十几秒，云端通常几秒内
    timeout_sec: int = Field(default=30, ge=1, le=600)
    # 失败重试次数（不含首次），只针对「同一条配置」，整条不可用会按 priority 换下一条
    max_retries: int = Field(default=1, ge=0, le=5)
    # 上下文长度上限（token）。0 = 未知/不限制；>0 时调用层据此裁剪对话历史
    context_window: int = Field(default=0, ge=0)
    # 厂商特有参数：temperature / max_tokens / top_p 等，由调用层读取
    params: dict[str, Any] = Field(default_factory=dict)
    priority: int = 10
    enabled: bool = True

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, v: str) -> str:
        v = (v or "").strip()
        # 空 kind 一律拒绝：那是角色，而角色的入口是 /api/roles。
        # 在schema 层就挡住，比在路由里判更早，也让"忘记传 kind"这种调用方 bug
        # 直接暴露成 422 而不是静默落一条残缺数据。
        if not v:
            raise ValueError("kind 必填。创建角色请用 POST /api/roles")
        if v not in KIND_LIST:
            raise ValueError(f"kind 只能是 {'/'.join(KIND_LIST)} 之一")
        return v

    @field_validator("api_style")
    @classmethod
    def _check_api_style(cls, v: str) -> str:
        v = (v or "").strip()
        if v and v not in API_STYLES:
            raise ValueError(
                f"api_style 只能是 {'/'.join(API_STYLES)} 之一，留空表示默认（OpenAI 兼容）")
        return v


class ProviderOut(ProviderIn):
    id: int
    created_at: datetime


class ProviderSafeOut(ORMBase):
    """下发给设备/APP 的脱敏配置，不含 api_key。**只含平台配置**（P0-2 止血）。

    这里曾经还带 description / system_prompt / tts_voice / is_default ——
    那是给 APP 的「agent 角色列表」用的，而角色现已独立到 /api/roles。
    留着这些字段等于让"平台配置"与"角色"在响应里继续混淆，
    而这正是 P0-1 事故链路的起点。

    调用层元信息（超时/重试/api_style/上下文）刻意不带：设备侧不直接调模型，
    带上只会让每次心跳的下发包变胖。想看这些字段走 GET /api/providers。
    """
    id: int
    name: str
    kind: str = ""
    provider: str = ""
    endpoint: str = ""
    model: str = ""
    priority: int = 10
    enabled: bool = True


# ============================================================ 角色（人格的统一入口）
# 权限代号见架构 §6.1：T=登录用户、O=仅 owner、A=仅管理员、AO=owner 或管理员、
# R=公开只读（仍需登录）。所有越权一律 404（唯一例外：preset 的 DELETE 返回 403，
# 因为"平台预设不可删"本身就是要给用户看的规则）。
class RoleIn(ORMBase):
    """新建**原创**角色。parent_id 不在这里 —— 派生走 /derive 专用接口。"""
    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    # 家属特别交代（软性）。医疗红线等硬约束由代码常量注入，不在这里。
    system_prompt: str = ""
    tts_voice: str = ""
    avatar_id: int | None = None
    # 性格 overlay：只存"与母版不同"的项。非法值由 persona.spec 校验拦下。
    traits: dict[str, Any] = Field(default_factory=dict)


class RoleDeriveIn(ORMBase):
    """从预设或自己的角色**派生**一份（copy-on-write 物理复制）。

    ``inherit`` 对应 PRD Q3 的「继承调整」选项，**默认 True**（用户已拍板）：
    为真时把母版的 L2 手动画像（家属对这个角色的交代）复制为新角色的 L2。
    不复制 L1 统计值 —— 那是"这个人"的画像，天然跟着人走，跨角色复制会串味。
    """
    name: str = Field(min_length=1, max_length=64)
    inherit: bool = True


class RoleRestoreIn(ORMBase):
    """还原为预设的入参。预留字段（未来可能支持"只还原某几项"），当前无必填。"""
    confirm: bool = True


class GuardrailIn(ORMBase):
    """家属追加/移除一条**软性**约束。

    只允许从平台白名单里勾选（架构 §12 U4），不接受自由文本：
    自由文本无法校验，且可能与 hard 段直接冲突 —— 而冲突的红线等于没有红线。
    """
    guardrail_id: str
    # True = 追加，False = 移除
    enabled: bool = True


class RoleOut(ORMBase):
    """角色详情 / 列表项。

    ``traits`` 是**overlay 原样**（可能为空），``traits_effective`` 是叠加母版
    之后的生效值 —— 两个都返回，APP 才能既显示"你改了什么"又显示"实际是什么"。
    """
    id: int
    name: str
    description: str = ""
    system_prompt: str = ""
    tts_voice: str = ""
    avatar_id: int | None = None
    is_default: bool = False
    # 'preset' = 平台预设；'role' = 用户角色
    record_type: str = "role"
    owner_id: int | None = None
    parent_id: int | None = None
    traits: dict[str, Any] = Field(default_factory=dict)
    traits_effective: dict[str, Any] = Field(default_factory=dict)
    guardrail_version: str = ""
    guardrail_ack_at: datetime | None = None
    guardrail_acked: bool = False
    deleted_at: datetime | None = None
    archived_at: datetime | None = None
    created_at: datetime


class RoleListOut(ORMBase):
    """GET /api/roles 的分区返回。

    刻意分区而不是给一个扁平 list：APP 的角色页要按「平台推荐 / 我的角色」
    分区渲染（PRD §11.1 线框），让后端把分区做好，前端就不必再自己按
    owner_id == me 过滤一遍 —— 那正是容易漏掉 preset 的一处逻辑。
    """
    presets: list[RoleOut] = Field(default_factory=list)
    mine: list[RoleOut] = Field(default_factory=list)


class RoleDiffItem(ORMBase):
    """一处差异：哪个字段 / trait，从什么变成什么。

    ⚠️ **契约决策 D-3**（2026-10-16 主理人拍板，全文见
    ``docs/架构设计-角色模型与人格能力.md`` §6.1 与 ``routers/roles.py``
    的 ``role_diff`` docstring）：采用「**全量 items + changed 标记**」，
    **不**拆成"仅变更 items + modified_traits"。

    四条理由（写在这里是为了防止后人来"优化"它）：
      1. 向后兼容、纯增量 —— APP 现有遍历 ``items`` 的代码不受影响；
      2. APP 还没接详情/还原页面，现在做破坏性变更零收益；
      3. 全量 + 标记支持 UI 渐进：先照旧渲染 items，接页面时再按 changed 过滤；
      4. 将来真要做「还原确认弹窗」发现不够用时，迁到拆两个数组是纯服务端
         改动、成本很低；反过来现在拆、以后要合回来才叫麻烦。

    → **严禁改名或删除 ``field`` / ``label`` / ``parent_value`` /
    ``current_value``**：它们是 APP 已依赖的契约。
    由 eval 的 ``ok_contract_v2`` 断言守着。
    """
    field: str
    label: str
    parent_value: Any = None
    current_value: Any = None
    # changed=True 表示用户确实改过；changed=False 表示继承自母版、未被修改，
    # 只是列出来让 UI 说明「这项跟平台一致」（QA 报告的 Bug 2：契约歧义）。
    # ⚠️ 只做加法：不改任何既有字段名与结构，items 的遍历方式照旧可用。
    #
    # 默认值刻意是True（"没标明就当成有改动"）而不是 False：
    # 这是**保守方向** —— 一个没读 changed 的旧客户端，它眼里的 items 里
    # 绝大多数确实是用户改过的项；反过来默认 False 会让"忘了赋值"
    # 静默变成"什么都没改"，而那正好是 Bug 2 的原始症状（误显示 0 项）。
    changed: bool = True


class RoleDiffOut(ORMBase):
    """GET /api/roles/{id}/diff：与母版的差异清单。

    ⚠️ 同样是契约 D-3（全量 items + changed 标记）的落点，见
    :class:`RoleDiffItem` 的注释。``items`` / ``id`` / ``parent_id`` /
    ``parent_name`` 四个既有字段一个都不能改名或删除。
    """
    id: int
    parent_id: int | None = None
    parent_name: str = ""
    items: list[RoleDiffItem] = Field(default_factory=list)
    # items 中 changed=True 的条数。给 APP 一个可直接显示的「你改了 N 项」，
    # 不必自己在客户端过滤 —— 过滤逻辑分散到各端，必然有一端漏改。
    #
    # ⚠️ 它必须恒等于 items 里 changed=True 的项数。计数与明细不一致时，
    # APP 上「你改了 3 项」与列表里数出来的 4 行会同时出现，用户无法判断
    # 哪个是真的。eval 的 ok_contract_v2 断言守着这条自洽性。
    changed_count: int = 0


class GuardrailCardOut(ORMBase):
    """GET /api/roles/{id}/guardrails：安全承诺卡的数据。

    ``items`` 是给人看的红线清单（不是给模型看的 prompt 文本），所以带label。
    ``acked`` 表示家属已确认过**当前版本**的红线；红线版本一变就自动变回false，
    让 APP 重新要求确认 —— 这是"红线更新了要让用户知情"的唯一落点。
    """
    role_id: int
    role_name: str
    version: str
    items: list[dict[str, Any]] = Field(default_factory=list)
    soft: list[dict[str, Any]] = Field(default_factory=list)
    acked: bool = False
    ack_at: datetime | None = None
    ack_version: str = ""


class RolePromptOut(ORMBase):
    """GET /api/roles/{id}/prompt：编译产物预览（家属的透明窗口）。"""
    role_id: int
    role_name: str
    # 完整 system prompt
    prompt: str
    # 分段视图，便于 APP 折叠展示（也便于 eval 按段断言）
    sections: list[dict[str, str]] = Field(default_factory=list)
    guardrail_version: str = ""


class ResolvedModelOut(ORMBase):
    """GET /api/providers/resolve 的返回：某用途最终落到哪条配置（脱敏）。

    上层（将来的记忆提取 / 对话编排）只说 usage，不必知道配的是哪一家；
    这里把「它到底是谁」答清楚，便于排查「为什么这句话是用这个模型回的」。
    api_key 一律不返回。
    """
    usage: str
    kind: str
    id: int
    name: str
    provider: str
    model: str
    endpoint: str
    api_style: str = ""
    supports_stream: bool = False
    context_window: int = 0
    timeout_sec: int = 30
    priority: int = 10
    # True = 首选 kind 没启用，是走候选链后面的 kind 兜底命中的
    # （典型场景：「没配 llm_main，退回旧的 llm」）。看到它就该去补配置。
    fallback: bool = False


class ModelTestIn(ORMBase):
    """管理端「测试连接」：按用途真发一次请求，验证这条链路是否可用。"""
    usage: str = "chat"
    # chat / vision 用：OpenAI 形状的 messages
    messages: list[dict[str, Any]] = Field(default_factory=list)
    # embed 用：待向量化的文本
    text: str = ""
    # rerank 用：待重排的候选文档
    documents: list[str] = Field(default_factory=list)


class ModelTestOut(ORMBase):
    """测试连接的统一返回：成功与失败都回 200，用 ok 与 error_code 区分。

    刻意不用 HTTP 错误码表达失败 —— 这是管理页手动点的「试试能不能通」，
    失败本身就是**预期结果之一**。用 4xx/5xx 会让调用方到处写 try/catch，
    而真正有价值的「为什么失败」在 error_code + message 里。
    需要 HTTP 语义时看 error_code 与 llm_gateway 的错误类型对照表即可。
    """
    ok: bool
    usage: str = ""
    kind: str = ""
    provider_pk: int = 0
    provider_name: str = ""
    model: str = ""
    text: str = ""
    vectors: list[list[float]] = Field(default_factory=list)
    scores: list[float] = Field(default_factory=list)
    elapsed_ms: int = 0
    # 成功时为空串；失败时取值见 llm_gateway 各错误类的 code
    error_code: str = ""
    message: str = ""
    # 每次尝试（含失败的那些）的明细，排障时比一句失败原因有用得多
    attempts: list[dict[str, Any]] = Field(default_factory=list)


# ============================================================ 人格预设运营后台
# ⚠️ 本段所有 schema 的第一原则：**可改字段用「schema 里没有」来锁**，而不是
# 「校验时忽略非法字段」。后者会被下一个写入口漏掉，前者不可能被绕过
# （与 RoleIn 不含 guardrails 是同一手法、同一理由）。
class PresetPatchIn(ORMBase):
    """运营调整平台预设。**只有这 5 个字段可改**，逐项依据见设计 §13.1。

   ⚠️⚠️ ``extra="forbid"`` 是这个类**成立的前提**，不是可选加固
   ------------------------------------------------------------------
   Pydantic v2 的默认是 ``extra="ignore"``（本机实测：``A(x=1, y=2)``
   静默丢掉 ``y`` 而不报错）。也就是说：**若不显式 forbid，
   "schema 里没有该字段" 并不等于 "提交会被拒绝"，而是"提交被静默丢弃"**
   —— 那等于根本没有锁：本轮第一次实跑时``{"guardrails": {"hard": []}}``
   返回的是 **200**，运营会以为红线改掉了（实际被丢掉），红线看起来
   "可改"这件事本身就是最大的风险。

   与 ``RoleIn`` 不同（那边是既有 APP 契约，宁可忽略也不能突然422），
   本类是**新建的管理端入参**，没有任何历史客户端，所以直接 forbid。

   刻意**不在**本 schema 里（提交即 422，物理上不可提交）：

    * ``guardrails`` —— 硬红线是代码常量，编译时由 ``regenerate_locked``
      无条件再生，DB 文本一律不信（红线段同理，改它必须是独立一轮设计）；
    * ``record_type`` / ``owner_id`` / ``parent_id`` / ``kind`` ——
      归属与派生关系。改归属是 P0 安全边界（"用户角色被刷成 preset"那次事故
      的形状），不能靠 PATCH 顺手改掉；
    * ``archived_at`` / ``deleted_at`` —— 生命周期走 archive 端点；
    * ``is_default`` —— 它是"APP 新建角色时的默认选中"，改它会让**所有用户**
      的默认角色静默变化；
    * ``enabled`` —— preset 行不参与任何下发判定，是死字段，改它只会制造
      "我关了它怎么没变"的困惑；
    * ``avatar_id`` —— 形象有独立生命周期，形象页已能编辑；
    * ``guardrail_ack_*`` —— 那是**用户**的确认回执，运营替用户确认是造假。
    """

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = None
    # 人格提示词（软性）。红线由代码常量注入，不在这里。
    system_prompt: str | None = None
    tts_voice: str | None = None
    # **整体覆写**（与 PATCH /api/roles/{id} 同一语义）：partial 语义会让
    # "取消勾选"无法表达。写入前逐项 is_valid 校验，非法值 400 且不落库。
    traits: dict[str, Any] | None = None

    # 见类注释：extra="forbid" 是「白名单式schema 锁」成立的前提
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class PresetResetIn(ORMBase):
    """恢复出厂默认。``fields`` 缺省 = 全部可重置字段。

    ⚠️ 可重置的**只有 3 个**：``system_prompt`` / ``traits`` / ``tts_voice``。
    不含 ``name``（运营的品牌标识，"恢复出厂"不该把它改回"小暖（平台预设）"）、
    不含 ``description``（运营文案，重置会让精心写的介绍消失）、
    **不含 ``archived_at``**（下架≠ 恢复，reset 一个已下架的预设不该把它复活）、
    不含软约束（运营自己勾的，reset 掉等于替他做决定）。
    """

    fields: list[str] | None = None

    # 同PresetPatchIn：不可重置的字段提交即422，而不是"静默忽略后全部重置"
    #（后者危险得多：运营传{"fields": ["name", "traits"]} 会被当成
    #  {"fields": ["traits"]} 执行，他以为name 也被重置了）。
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class SoftGuardrailIn(ORMBase):
    """软约束开关。**单独端点**，不合进 PresetPatchIn。

    为什么不合进：软约束是"勾选型"操作，并入整体覆写后
    "忘带这个字段 = 全部清空"成为可能；且它需要独立的审计条目。
    """

    guardrail_id: str
    enabled: bool = True

    # 同上：多传一个 text 字段不能被静默丢弃 ——
    # 运营以为自定义了一条软约束，实际库里仍是白名单那条。
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class PresetOut(ORMBase):
    """平台预设详情 / 列表项。

    ⚠️ ``guardrails_hard`` **不含 tokens**（构造口径见设计 §6.3）：
    只允许走 ``guardrails.card_items``，**绝不能**用 ``regenerate_locked``
    —— 后者的返回结构里多了 ``tokens``，那是 ``assert_present()`` 做
    fail-closed 自检的判定基准，出库等于把门禁怎么判暴露给浏览器。
    断言 T10 守这条。
    """

    id: int
    name: str
    description: str = ""
    system_prompt: str = ""
    tts_voice: str = ""
    traits: dict[str, Any] = Field(default_factory=dict)
    # 只读展示：每项 {id, label, text, locked: true}
    guardrails_hard: list[dict[str, Any]] = Field(default_factory=list)
    # 已勾选的软约束 id 列表（不是文案：文案是代码常量，前端从 meta 取）
    guardrails_soft: list[str] = Field(default_factory=list)
    guardrail_version: str = ""
    archived: bool = False
    # 影响面：直接绑定本预设的设备数（改 personality 后立刻变化）、派生副本数
    bound_device_count: int = 0
    derived_role_count: int = 0
    # 与出厂默认不一致的字段（后端算，前端不做比较）
    dirty_fields: list[str] = Field(default_factory=list)


class PresetListOut(ORMBase):
    """GET /api/admin/presets的分区返回。"""

    presets: list[PresetOut] = Field(default_factory=list)


class PresetMetaOut(ORMBase):
    """GET /api/admin/presets/meta：档位 / 白名单 / 红线**一次性下发**。

    为什么必须由后端下发（前端零硬编码）：``TraitSpec.as_option()`` 的档位
    与文案是 ``catalog`` 的唯一真源。前端硬编码一份，加/删档位后后台会显示
    错位，而那种错位**不会报错**，只会让运营在错的档位上做决定。

    ⚠️ ``trait_specs[].options[].value`` 的语义：**enum = 枚举值本身；
    number = 该档位的上界（bands 的 upper）**。前端提交时必须原样回传这个
    value，不得改成交"档位下界"—— ``to_prompt_sentence`` 的分档判定是
    ``val <= upper`` 取第一个，两者恰好自洽；换成下界同一份 traits 会渲染成
    另一句话，而值域校验仍然通过、**没有任何断言会红**（设计边界 W13）。
    """

    trait_specs: list[dict[str, Any]] = Field(default_factory=list)
    # 7 条软约束白名单：[{id, text}]
    soft_guardrails: list[dict[str, Any]] = Field(default_factory=list)
    # 5 条硬红线（只读展示口径，不含 tokens）：[{id, label, text}]
    hard_guardrails: list[dict[str, Any]] = Field(default_factory=list)
    guardrail_version: str = ""
    # 出厂默认（reset 的基线）：{system_prompt, traits, tts_voice}
    defaults: dict[str, Any] = Field(default_factory=dict)


class PresetAuditOut(ORMBase):
    """GET /api/admin/presets/{id}/audits 的一行。

    ``preset_name`` 取**当前行**而不存快照：改名后审计显示新名是可接受的，
    且避免"审计里一个名、预设上另一个名"这种更难查的不一致。
    """

    id: int
    preset_id: int
    preset_name: str = ""
    admin_id: int
    admin_username: str = ""
    action: str = ""
    changed_fields: list[str] = Field(default_factory=list)
    diff: dict[str, Any] = Field(default_factory=dict)
    guardrail_version: str = ""
    created_at: datetime


# ============================================================ 首页聚合
class HomeDeviceOut(ORMBase):
    """首页设备卡片：状态 + 设备时间 + 当前生效的角色 / 形象名。

    device_time 复用 last_seen_at：设备没有把本地 RTC 时间回传给服务端
    （固件只在心跳里带固件版本与 IP），所以「设备上的时间」只能用它最近一次
    心跳时间近似表示，语义上等于「这台设备最后一次被看到的时间」。
    """

    id: int
    name: str
    online: bool = False
    last_seen_at: datetime | None = None
    ip: str = ""
    fw_version: str = ""
    device_time: str | None = None
    agent_name: str | None = None
    avatar_name: str | None = None


class MedicineTodayOut(ORMBase):
    """当天服药概览（服务端时区 TZ 的自然日）。"""

    # 待服药次数：当天排了但还没确认的（含已过点未确认的漏服）
    pending_count: int = 0
    # 待服药的时间点，"HH:MM"，升序
    pending_times: list[str] = Field(default_factory=list)
    confirmed_count: int = 0
    total_count: int = 0


class HomeSummaryOut(ORMBase):
    """GET /api/home/summary：一次拉齐首页要用的两块信息。

    ⚠️ 取消会员制后移除了 ``membership`` 字段。删字段而不是留空对象：
    留一个恒为「非会员、无限量」的空壳只���让每个客户端都要写一段
    「反正没有会员了」的判断。调用方（APP / Web）已同步删除该 UI 区块。
    """

    devices: list[HomeDeviceOut] = Field(default_factory=list)
    medicine_today: MedicineTodayOut = Field(default_factory=MedicineTodayOut)


# ============================================================ 提醒
class ReminderIn(ORMBase):
    title: str = Field(max_length=128)
    speak_text: str = ""
    rtype: int = 0
    schedule_kind: int = 1
    times_of_day: list[str] = Field(default_factory=list)
    weekdays: list[int] = Field(default_factory=list)
    month_days: list[int] = Field(default_factory=list)
    interval_days: int = 1
    once_date: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    dose: str = ""
    require_confirm: bool = True
    repeat_interval_min: int = 10
    repeat_times: int = 3
    escalate_after_min: int = 30
    confirm_methods: list[str] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("times_of_day")
    @classmethod
    def _check_times(cls, v: list[str]) -> list[str]:
        for t in v:
            if parse_hhmm(t) is None:
                raise ValueError(f"时间格式非法: {t}，应为 HH:MM")
        return v

    @field_validator("weekdays")
    @classmethod
    def _check_weekdays(cls, v: list[int]) -> list[int]:
        for d in v:
            if not 1 <= d <= 7:
                raise ValueError("weekdays 取值应为 1~7（1=周一）")
        return v

    @field_validator("month_days")
    @classmethod
    def _check_month_days(cls, v: list[int]) -> list[int]:
        for d in v:
            if not 1 <= d <= 31:
                raise ValueError("month_days 取值应为 1~31")
        return v


class ReminderOut(ReminderIn):
    id: int
    device_id: int
    creator_id: int | None
    created_at: datetime
    updated_at: datetime
    next_fire_at: datetime | None = None


class EventOut(ORMBase):
    id: int
    reminder_id: int
    device_id: int
    due_at: datetime
    fired_at: datetime | None
    confirmed_at: datetime | None
    state: int
    confirm_method: int
    fired_count: int
    escalated: bool
    note: str


class EventReportIn(ORMBase):
    """设备回传：提醒触发 / 确认 / 超时。

    due_at 用于定位实例；若服务端没有预排该实例（设备离线自建），
    则按 (reminder_id, due_at) 补建一条。
    """
    reminder_id: int
    due_at: datetime
    state: int
    confirm_method: int = 0
    fired_at: datetime | None = None
    confirmed_at: datetime | None = None
    note: str = ""


# ====================================================== 设备自建提醒上报
class LocalReminderIn(ORMBase):
    """设备断网期间本地新建的提醒（firmware 侧 server_id == 0）。

    老人在没网时用离线命令词/按键建的服药提醒只存在设备 NVS 里，
    服务端毫不知情。重新联网后设备把这条补报上来，服务端分配 id 回写，
    之后就能像普通提醒一样被 APP 查看、被 /api/device/events 对账。

    client_key 是设备侧的稳定标识（同一 slot 的「类型+时刻+标题」），
    用来做幂等：设备在响应丢失时会重发，靠它避免建出重复提醒。
    """

    client_key: str = Field(max_length=64)
    title: str = Field(max_length=128)
    speak_text: str = ""
    rtype: int = 0
    schedule_kind: int = 1
    times_of_day: list[str] = Field(default_factory=list)
    weekdays: list[int] = Field(default_factory=list)
    confirm_methods: list[str] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("times_of_day")
    @classmethod
    def _check_local_times(cls, v: list[str]) -> list[str]:
        for t in v:
            if parse_hhmm(t) is None:
                raise ValueError(f"时间格式非法: {t}，应为 HH:MM")
        return v

    @field_validator("weekdays")
    @classmethod
    def _check_local_weekdays(cls, v: list[int]) -> list[int]:
        for d in v:
            if not 1 <= d <= 7:
                raise ValueError(f"weekdays 取值应为 1~7（1=周一）")
        return v


class LocalReminderAck(ORMBase):
    """服务端分配结果。设备据此回写本地 server_id。"""

    client_key: str
    reminder_id: int


class LocalReminderReportOut(ORMBase):
    """一次上报的逐条对账结果（顺序与请求一致，便于设备按下标回写）。"""

    acks: list[LocalReminderAck] = Field(default_factory=list)


# ============================================================ 交互统计
class InteractionReportIn(ORMBase):
    kind: int = 1
    ts: datetime | None = None
    duration_ms: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)


class StatsPoint(ORMBase):
    date: str
    total: int
    wake: int = 0
    dialog: int = 0
    confirm: int = 0
    miss: int = 0
    sos: int = 0


class StatsOverview(ORMBase):
    device_id: str
    device_name: str
    online: bool
    last_seen_at: datetime | None
    today_total: int
    today_dialog: int
    today_confirm: int
    today_miss: int
    total_7d: int
    pending_reminders: int
    missed_today: int
    series: list[StatsPoint]


# ============================================================ 设备陪聊统计
# 放在设备同步之前定义，供 DeviceSyncOut 引用
class DeviceQuotaOut(ORMBase):
    """设备当日陪聊统计快照。

    ⚠️ 取消会员制后移除了 ``is_vip`` 字段（不再有会员这个概念），
    且 ``unlimited`` 对授权设备恒为 ``True``、``limit_seconds`` 恒为 ``None``。
    **两个字段都保留**：老固件按「先看 unlimited」的契约读它们，
    直接删会让固件侧逻辑空掉。
    """
    date: str
    used_seconds: int = 0
    # 不限时为 None；设备应优先判断 unlimited
    limit_seconds: int | None = None
    unlimited: bool = True
    idle_timeout_seconds: int = 90
    reminder_keywords: list[str] = Field(default_factory=list)


# ============================================================ 设备同步
class DeviceSyncOut(ORMBase):
    """设备心跳拉取的完整配置快照（含当日陪聊配额，见规划文档 7.3）。"""
    server_time: datetime
    device: DeviceOut
    avatar: AvatarOut | None
    reminders: list[ReminderOut]
    upcoming: list[EventOut]
    providers: list[ProviderSafeOut]
    ws_url: str
    ota_url: str
    sync_interval_sec: int = 300
    # 新增字段：缺省为 None，老版本设备忽略即可（也可单独调 GET /api/device/quota）
    quota: DeviceQuotaOut | None = None
    # 本次生效的音色（架构 §4.2）：角色自带音色优先，没有才回落到形象的默认音色。
    # 放在顶层而不是 avatar 里，因为音色随**角色**变、而 avatar 段描述的是外观 ——
    # 老固件读 avatar.tts_voice 仍然可用（那是形象的默认值），新固件读这里。
    tts_voice: str = ""


# ============================================================ 陪聊时长上报
# ⚠️ 这里曾有一整段会员 schema（MembershipOut / MembershipPlanOut / Order* /
# PaymentInfoOut / AmountRangeIn / QuotaConfigIn / MembershipConfig* / GrantIn /
# RejectIn / ConfirmResultOut），随会员制取消整体删除。
# 保留下来的只有下面这两个 —— 它们是**计量**的出参，不是会员的。
class ChatUsageIn(ORMBase):
    """设备上报一次会话时长。

    is_reminder=True 表示服药/提醒类交互，与普通陪聊分开累计。
    """

    date: str = ""
    seconds: int = Field(default=0, ge=0)
    is_reminder: bool = False


class ChatUsageOut(ORMBase):
    date: str
    used_seconds: int = 0
    session_count: int = 0
    reminder_seconds: int = 0
    # 以下三个字段取消会员制后对授权设备恒为 unlimited=True / None，
    # 但**必须保留**：老固件与老 APP 都按这个形状读。
    limit_seconds: int | None = None
    unlimited: bool = True
    remaining_seconds: int | None = None


# ============================================================ 邮箱服务
# 平台需要一个邮箱配置，支撑「邮箱找回密码」与「邮箱提交问题」两件事。
# 用户全程在 APP 内操作，所以这里只有 API、没有网页端表单。
def _check_email_str(v: str) -> str:
    """「长得像邮箱」的宽松校验，与 EmailChangeIn 保持同一套规则。

    只做格式检查、不发验证邮件：项目是私有化部署，邮箱是找回密码的线索而不是
    强凭证，卡太死只会让用户在 APP 里填不进去。
    """
    v = (v or "").strip()
    if v.count("@") != 1 or v.startswith("@") or v.endswith("@"):
        raise ValueError("邮箱格式不正确")
    domain = v.split("@")[1]
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("邮箱格式不正确")
    return v


class SmtpConfigIn(ORMBase):
    """管理端局部更新 SMTP 配置：全字段可选，只传要改的。

    password 的语义是**只写**：传了就覆盖，不传就保持原值。读取接口永远
    拿不到明文（只给掩码），所以「改端口」这类操作不需要先把密码读出来再回传。
    """

    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    use_tls: bool | None = None
    use_ssl: bool | None = None
    from_addr: str | None = None
    from_name: str | None = None
    # 工单通知收件人；留空表示回落到 from_addr
    notify_to: str | None = None
    enabled: bool | None = None

    @field_validator("port")
    @classmethod
    def _check_port(cls, v: int | None) -> int | None:
        if v is None:
            return v
        if not 1 <= int(v) <= 65535:
            raise ValueError("端口应在 1~65535 之间")
        return int(v)

    @field_validator("from_addr", "notify_to")
    @classmethod
    def _check_addr(cls, v: str | None) -> str | None:
        # 允许留空（留空回落），填了就必须长得像邮箱
        if v is None or not v.strip():
            return (v or "").strip()
        v = v.strip()
        if v.count("@") != 1 or v.startswith("@") or v.endswith("@"):
            raise ValueError("邮箱格式不正确")
        domain = v.split("@")[1]
        if "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise ValueError("邮箱格式不正确")
        return v


class SmtpConfigOut(ORMBase):
    """管理端读取 SMTP 配置。

    ⚠️ password **只返回掩码**（如 ``ab******gh``），明文永不出口：
    管理端页面与 APP 的日志、截图、抓包都可能把这些响应留存下来，
    一旦明文回显，等于给「看一眼后台」的人发了一份邮箱授权码。
    前端要判断「有没有配密码」请用 has_password。
    """

    host: str = ""
    port: int = 465
    username: str = ""
    password: str = ""          # 掩码，不是明文
    has_password: bool = False
    use_tls: bool = False
    use_ssl: bool = True
    from_addr: str = ""
    from_name: str = ""
    notify_to: str = ""
    enabled: bool = False
    updated_at: datetime | None = None


class MailTestIn(ORMBase):
    """发送测试邮件。to 留空时发给平台自己的通知邮箱（notify_to / from_addr）。"""

    to: str = ""
    subject: str = ""


class MailTestOut(ORMBase):
    """测试发信结果。

    这条接口存在的意义就是「配完立刻知道能不能发出去」：SMTP 参数配错的概率
    极高，没有自检手段就只能等用户投诉了再去翻日志。所以失败时把原因原样返回。
    """

    ok: bool
    error: str = ""
    to: str = ""
    # 伪装模式下并没有真的发信，管理端/测试据此区分
    fake: bool = False
    message: str = ""


class PasswordResetRequestIn(ORMBase):
    """申请找回密码验证码。"""

    email: str = Field(min_length=3, max_length=128)

    @field_validator("email")
    @classmethod
    def _check(cls, v: str) -> str:
        return _check_email_str(v)


class PasswordResetRequestOut(ORMBase):
    """申请找回密码的响应。

    ⚠️ 这个响应体对「邮箱已注册」和「邮箱未注册」必须**完全一致**（连字段值
    都不能差），否则就是一个账号枚举漏洞：攻击者挨个试邮箱就能拿到注册用户
    名单。所以这里不给任何与账号相关的信息，只给固定的阈值提示。
    """

    ok: bool = True
    message: str = "如果该邮箱已注册，验证码已经发送到该邮箱，请查收"
    # 重发冷却秒数，APP 据此把「重新发送」按钮置灰倒计时
    cooldown_sec: int = 60
    # 验证码有效期（秒），APP 展示「15 分钟内有效」
    expires_in_sec: int = 900


class PasswordResetVerifyIn(ORMBase):
    """提交验证码 + 新密码。密码长度下限与注册接口保持一致（6 位）。"""

    email: str = Field(min_length=3, max_length=128)
    code: str = Field(min_length=6, max_length=6)
    new_password: str = Field(min_length=6, max_length=128)

    @field_validator("email")
    @classmethod
    def _check(cls, v: str) -> str:
        return _check_email_str(v)


class PasswordResetVerifyOut(ORMBase):
    ok: bool = True
    message: str = "密码已重置，请使用新密码登录"
    # 走到这里说明验证码校验通过（等于已证明邮箱归属），告诉用户是哪个账号无妨
    username: str = ""


# ================================================ 「用户名 + 邮箱」找回密码（2026-10-19）
#
# 为什么要在既有「邮箱 → 验证码」之外另开一条链路：
# 老人常常只记得**自己注册时用的用户名**，邮箱在哪张卡片上、拼对没有都说不清。
# 让他先填邮箱，等于要求他必须先知道邮箱才能找回密码 —— 对目标用户不成立。
# 新链路把「用户名」放到最前面，邮箱降级为**第二道确认**，与需求的四步一致。

class PasswordLookupIn(ORMBase):
    """第 1 步：只提交用户名。

    ⚠️ 这一步**只**用来确认「能不能继续」，绝不能回显用户名是否存在。
    但用户又要看到明确的推进反馈，所以这里的取舍是：
      * 用户名不存在 → 走与「存在但未绑邮箱」**不同的分支**是必要的
        （前者不可能有邮箱，后者要去联系管理员），但两者的措辞必须
        都**不确认账号存在**，只能靠「能否继续」反推 —— 这在原理上
        无法完全避免，是本设计已知的取舍，见 request_account_probe 注释。
    """

    username: str = Field(min_length=1, max_length=64)


class PasswordLookupOut(ORMBase):
    """第 1 步响应。

    ⚠️ ``email_bound`` 必须**对「未绑邮箱」和「用户名不存在」返回相同值**
    （都是 false），否则就成了用户名枚举接口。区别只在 ``message`` 的措辞
    —— 而措辞差异本身也是侧信道，所以下面的 message 被刻意写得语义模糊，
    真正区分二者的是 ``next_step``（用户接下来该做什么），那是功能需要，
    不是信息泄露。
    """

    ok: bool = True
    #: 该账号是否绑定了可用邮箱。前端据此决定是否提示「请联系管理员」。
    email_bound: bool = False
    #: 未绑邮箱时给管理员邮箱（前端原样展示，避免把地址硬编码在前端）
    admin_email: str = ""
    message: str = ""


class PasswordIssueIn(ORMBase):
    """第 2 步：用户名 + 邮箱，发起验证码。

    ⚠️ 用户名与邮箱**不匹配**时不能返回「不匹配」以外的任何信息
    （尤其不能提示正确的邮箱），否则就是「给定用户名 → 探测邮箱」的接口。
    这里用 ``matched`` 布尔让前端提示"请重新填写"，但不给出正确值。
    """

    username: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=128)

    @field_validator("email")
    @classmethod
    def _check(cls, v: str) -> str:
        return _check_email_str(v)


class PasswordIssueOut(ORMBase):
    ok: bool = True
    #: 用户名与邮箱是否对得上。**只用于决定前端下一步文案**，
    #: 不能携带「正确的邮箱是什么」—— 那才是真正的泄露。
    matched: bool = False
    #: 匹配成功时给出验证码有效期/冷却秒数；不匹配时为 0。
    expires_in_sec: int = 0
    cooldown_sec: int = 0
    message: str = ""


class PasswordSendIn(ORMBase):
    """第 3 步：提交验证码 + **用户自己设定的新密码**。

    ⚠️ 2026-06-19 用户拍板改过一次语义：这一端点原本是「服务端生成随机
    密码并寄到邮箱」，现已改成「用户自己在 APP 里设新密码」，邮件只发验证码。
    理由是后者才正常：明文口令躺在收件箱里（转发、备份、代理都会留存），
    老人还得抄一遍才能登录，抄错一位就登不进来。

    所以本端点现在做的事，与既有 ``PasswordResetVerifyIn``（邮箱链路）
    **完全一样**，只是入参多带一个 ``username``。
    之所以不直接让前端复用 /verify：那条链路以邮箱为起点、只认邮箱；
    本链路以用户名为起点（老人只记得用户名），服务端仍需校验
    「用户名 + 邮箱 + 验证码」三者自洽。
    """

    username: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=128)
    code: str = Field(min_length=6, max_length=6)
    new_password: str = Field(min_length=6, max_length=128)

    @field_validator("email")
    @classmethod
    def _check(cls, v: str) -> str:
        return _check_email_str(v)


class PasswordSendOut(ORMBase):
    ok: bool = True
    message: str = ""
    #: 剩余可尝试次数，前端用来显示"你还可以试 N 次"
    attempts_left: int = 0
    #: 走到这里说明验证码校验通过，告诉用户是哪个账号无妨
    username: str = ""


class TicketCreateIn(ORMBase):
    """提交问题（工单）。匿名可提交，但必须留邮箱，否则管理员没法回复。"""

    email: str = Field(min_length=3, max_length=128)
    title: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=4000)
    # 付款费用 / 设备故障 / 提醒异常 / 账号问题 / 其它，未知值按其它处理
    category: str = "other"
    device_id: str = ""

    @field_validator("email")
    @classmethod
    def _check(cls, v: str) -> str:
        return _check_email_str(v)


class TicketPatchIn(ORMBase):
    """管理端更新工单：改状态 / 写回复。两个字段都可选。"""

    status: str | None = None
    admin_reply: str = ""
    # 是否把回复邮件通知提交者，默认 True（写了回复就通知）
    notify: bool = True


class TicketOut(ORMBase):
    id: int
    user_id: int | None = None
    email: str
    title: str
    content: str
    category: str = "other"
    # 分类 / 状态的中文展示文案：APP 与邮件正文直接显示，不做二次映射
    category_text: str = "其它"
    device_id: str = ""
    status: str = "open"
    status_text: str = "待处理"
    admin_reply: str = ""
    admin_id: int | None = None
    replied_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    # 提交者用户名（匿名提交为空串），管理端列表看得到是谁提的
    username: str = ""


# ============================================================ 模型用量计量
# 字段口径与 app.usage.usage_summary / recent_usage_rows 的返回**逐字对齐** ——
# 「设备 token 用量」要在管理端、用户端、后续可能的设备配额三处展示，
# 聚合口径的单一真源在 usage.py；router 不做二次加工，schema 也不另起名字。
class UsageDayOut(ORMBase):
    """按天汇总的一项（usage_summary(by_day=True) 的 by_day 元素）。"""

    date: str
    total_tokens: int = 0


class UsageSummaryOut(ORMBase):
    """token 用量聚合。

    by_day 由 router 恒以 by_day=True 查询填足（schema 有这个字段，
    各端自己决定传不传就会漂移）；调用数为 0 时其余各项为 0 / 空，
    不是 null —— 前端可以直接 render，不用判空。
    """

    calls: int = 0
    failed_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    by_usage: dict[str, int] = Field(default_factory=dict)
    by_day: list[UsageDayOut] = Field(default_factory=list)


class UsageRowOut(ORMBase):
    """一条调用流水（ModelUsage 行的出参）。

    ⚠️ **刻意不含 prompt / 回复正文**：ModelUsage 表本来就不存正文，
    这个 schema 也不许加 —— 用户端看得到自己的用量，看不到内容；
    管理端看得到调用级对账，同样看不到。要存正文是另一个决策
    （隐私评审），不是"顺手加个字段"。
    """

    id: int
    created_at: datetime
    source: str = ""
    usage: str = ""
    device_id: int | None = None
    user_id: int | None = None
    provider_name: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    ok: bool = True
    error: str = ""
    elapsed_ms: int = 0
