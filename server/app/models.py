"""ORM 数据模型。

设计要点
--------
1. 设备离线也能响铃（需求 8），所以提醒规则会**下发到设备**，
   提醒事件（ReminderEvent）由设备本地产生、联网后回传对账。
2. 形象（Avatar）与提示词、TTS 音色、情绪灯效映射绑定在一起，
   一台设备同一时刻只有一个生效形象，便于端侧只维护一份资源。
3. AI 供应商（AIProvider）按能力拆行（asr / llm / llm_main / llm_light /
   embedding / rerank / tts / vad ...），同一用途可配多条并靠 priority 决定主备，
   满足需求 15「多平台接入」。详细的分层口径与「旧 llm 值如何平滑过渡」见
   app/model_catalog.py，调用层见 app/llm_gateway.py。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import IntEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from .db import Base


def utcnow() -> datetime:
    """统一返回带 UTC 时区的当前时间。"""
    return datetime.now(timezone.utc)


# ================================================================ 记录类型
# ai_providers / avatars 两张表各自承载两类语义，靠 record_type 显式区分。
#
# 为什么要有这一列（而不是继续靠 "kind 是否为空" 猜）：
#   1. "kind 为空串 = 这是个角色" 是一个**只存在于人脑里**的约定。现状
#      schemas.ProviderIn 允许空串、routers 里却按 kind 过滤，任何人新增
#      一条 WHERE kind != '' 的查询都可能忘记这个约定；
#   2. 设备下发用的是 `kind.in_(DEVICE_KINDS)` 这种**黑名单**过滤 ——
#      现在恰好安全（角色 kind 为空不在名单里），但那是"恰好"不是"必然"；
#   3. 人格统一到 AIProvider 之后，必须有一个明确标记把"这行是角色"
#      与"这行是可调用的平台配置"在**数据库层面**分开，越权守卫才能
#      写成一句 `record_type == 'config'`。
#
# 不变量（由应用层守卫 + 契约套件双重保证，见 evals/suites/invariants.json）：
#   (record_type == 'config') <=> (kind != '')
#   record_type in ('config', 'preset') => owner_id IS NULL
#   record_type == 'role'              => owner_id IS NOT NULL
RECORD_TYPE_CONFIG = "config"   # 平台 LLM/TTS/ASR 配置：参与下发与导出
RECORD_TYPE_PRESET = "preset"   # 平台预设角色（小暖）：仅管理员可写
RECORD_TYPE_ROLE = "role"       # 用户原创 / 派生角色：owner 本人或管理员可写
RECORD_TYPES = (RECORD_TYPE_CONFIG, RECORD_TYPE_PRESET, RECORD_TYPE_ROLE)


# ================================================================ 用户
class User(Base):
    """子女 / 管理员账号。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    nickname: Mapped[str] = mapped_column(String(64), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    # 绑定邮箱，APP「我的」页可自行更换（PATCH /api/auth/email）。
    # 这里**不加唯一索引**：老账号的 email 都是空串，唯一索引会让第二个用户直接写不进去；
    # 唯一性由接口层查询校验（重复返回 409）保证。
    email: Mapped[str] = mapped_column(String(128), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    devices: Mapped[list["Device"]] = relationship(back_populates="owner")


# ================================================================ 设备
class Device(Base):
    """一台老人端硬件。"""

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 设备唯一标识，固件侧取 eFuse MAC，形如 "aabbccddeeff"
    device_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), default="我的陪伴助手")
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    avatar_id: Mapped[int | None] = mapped_column(ForeignKey("avatars.id"), nullable=True)
    # 生效的 agent 角色（对应 ai_providers 里的一条记录，APP「创建我的 agent 角色」
    # 走的也是 POST /api/providers）。形象（avatar_id）管的是屏幕上的外观与音色，
    # 角色（agent_id）管的是人格与提示词，两者互不影响、可各选一个。
    # TODO: 目前只落库，/api/device/sync 还没有把 agent 下发给设备；
    #       等设备侧支持按角色切换人格时，在 DeviceSyncOut 里补一个 agent 字段即可。
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id"), nullable=True)

    # 运行时状态
    online: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fw_version: Mapped[str] = mapped_column(String(32), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")

    # 端侧个性化配置（音量、亮度、灯效开关、离线提示音开关等）
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    owner: Mapped["User | None"] = relationship(back_populates="devices")
    avatar: Mapped["Avatar | None"] = relationship()
    reminders: Mapped[list["Reminder"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )


# ================================================================ 激活码
class ActivationCode(Base):
    """设备绑定激活码：1 位字母 + 12 位数字（末位为 Luhn 校验位）。"""

    __tablename__ = "activation_codes"
    __table_args__ = (Index("ix_act_code", "code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    # 为空表示「通用码」，任意设备均可申领；否则绑定到指定 device_id
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ================================================================ 形象 / 角色
class Avatar(Base):
    """形象 = **纯形象**（屏幕外观 + 资源包 + 灯效 + 默认音色）。

    ⚠️ 决策变更（docs/架构设计-角色模型与人格能力.md §4.2）：人格（system_prompt）
    与角色级音色已**迁移到 AIProvider**，Avatar 退化为"这个角色长什么样"。
    理由是设备侧把"外观"和"人格"绑在一起会让换角色必须换形象，而这两件事
    家属 wanting 的粒度完全不同。

    ``system_prompt`` / ``tts_voice`` **本轮不删列**（本项目没有 Alembic，
    schema_patch 只能 ADD COLUMN，删列做不到），但语义已降级：
      * ``system_prompt`` —— 停用。启动迁移会把老库里的值搬到 preset 角色行
        然后置空（main._seed_migration），AvatarIn/Out 也**不再暴露**它。
      * ``tts_voice`` —— 保留为「形象的**默认**音色」。角色自己有音色时
        用角色的，没有才回落到形象（device_api.sync 里做这个 fallback）。
    这样既拿到了"人格与形象解耦"，又没有为一次重构付"删列"的代价。

    clips 约定（对应端侧 EAF 资源包内的片段名）：
        idle        待机
        listening   聆听中
        thinking    思考中
        mouth_open  说话·张嘴
        mouth_half  说话·半张
        smile       微笑
        blink       眨眼
        sleepy      困倦
        alert       警示（未服药）
    """

    __tablename__ = "avatars"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(255), default="")

    # ---- 归属（新增列，见 schema_patch.NEW_COLUMNS）----
    # 'preset' = 平台形象（仅管理员可写）；'role' = 用户自建形象（owner 可写）。
    # 与 AIProvider.record_type 同一套语义，但取值只有两档 —— 形象没有"派生"。
    record_type: Mapped[str] = mapped_column(
        String(16), default=RECORD_TYPE_PRESET, server_default=RECORD_TYPE_PRESET, index=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    # 软删标记。非空即已删除，但行还在 —— 设备上可能仍有 avatar_id 指向它，
    # 真删会让 devices.avatar_id 变成悬空外键。
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    # 乐鑫 EAF 序列帧资源包（esp_emote_expression / esp_emote_gfx）
    eaf_pack_name: Mapped[str] = mapped_column(String(128), default="")
    eaf_pack_url: Mapped[str] = mapped_column(String(512), default="")
    eaf_pack_sha256: Mapped[str] = mapped_column(String(64), default="")
    # 屏幕规格，切换平台（320x240 横屏 / 360x360 圆屏）时用于校验资源是否匹配
    screen_width: Mapped[int] = mapped_column(Integer, default=320)
    screen_height: Mapped[int] = mapped_column(Integer, default=240)

    # 片段名映射，键为上面约定的语义名，值为资源包内实际片段名
    clips: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)

    # ---- 已降级的人格字段（保留列，不再对外暴露，理由见类注释）----
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    # 形象的**默认**音色；角色自带音色时以角色的为准
    tts_voice: Mapped[str] = mapped_column(String(64), default="")

    # 情绪 -> RGB 灯效，键为情绪名，值为 {"rgb":"#RRGGBB","effect":"breath|blink|solid|rainbow"}
    emotion_lights: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def is_alive(self) -> bool:
        """未软删。读路径一律先判这个，否则会把已删形象当成可用资源下发。"""
        return self.deleted_at is None

    def can_write(self, user: "User") -> bool:
        """能否被该用户修改/删除：preset 仅管理员，用户形象 owner 或管理员。"""
        if self.record_type == RECORD_TYPE_PRESET or self.owner_id is None:
            return bool(getattr(user, "is_admin", False))
        return bool(getattr(user, "is_admin", False)) or self.owner_id == user.id

    def is_visible_to(self, user: "User") -> bool:
        """能否被该用户看到：管理员看全部；普通用户只看 preset + 自己的。"""
        if getattr(user, "is_admin", False):
            return True
        return self.record_type == RECORD_TYPE_PRESET or self.owner_id == user.id


# ================================================================ AI 供应商 / 角色
class AIProvider(Base):
    """多平台 AI API 接入配置（需求 15）+ **角色**（人格的唯一载体）。

    同一张表承载三类记录，靠 ``record_type`` 区分（``kind`` 只在 config 时非空）：

      * ``config``  一条真实可调用的平台配置（kind ∈ model_catalog.KIND_LIST），
        参与 /api/device/sync 的下发与 /api/providers/export/xiaozhi 的导出；
      * ``preset``  平台预设角色（小暖），由代码 seed，**仅管理员可写**；
      * ``role``    用户原创或派生的角色，**owner 本人**（或管理员）可写。

    ⚠️ 决策变更（docs/架构设计-角色模型与人格能力.md §0/§1.2）：人格从 Avatar
    统一搬到这里。``Avatar`` 退化为纯形象后，"角色是谁、怎么说话"只剩这一个
    落点，``/api/roles`` 也才有一个单一数据来源。

    **读路径为什么只认 is_config()，不直接判 kind**：``kind.in_(DEVICE_KINDS)``
    是黑名单式过滤 —— 将来有人往DEVICE_KINDS 里加一个 kind，角色行就会
    （在kind 被误填的情况下）混进下发包，把真实配置顶掉。正向断言没有这个
    漏法：``record_type == 'config'`` 独立于 kind 判定，两者互不影响。
    """
    __tablename__ = "ai_providers"
    __table_args__ = (
        # 角色列表 / is_config() 过滤 / "我的角色" 查询都走这三个索引。
        # 老库不会自动建索引，由 schema_patch.ensure_missing_indexes() 幂等补上
        # （create_all 只对**新**库建索引，见该函数注释）。
        Index("ix_prov_record_type", "record_type"),
        Index("ix_prov_owner", "owner_id"),
        Index("ix_prov_parent", "parent_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    # 能力分类，取值见 app/model_catalog.py 的 KIND_LIST：
    # asr / tts / vad / llm / llm_main / llm_light / embedding / rerank / vision。
    # ⚠️ 只有 record_type == 'config' 的行才允许非空；角色行恒为空串。
    # 这条不变量由 providers.create_provider 拒绝空 kind + deps.require_config 保证。
    kind: Mapped[str] = mapped_column(String(16), default="", index=True)
    # 厂商：aliyun / volcengine / deepseek / zhipu / openai / ollama / dify / xinference ...
    provider: Mapped[str] = mapped_column(String(32), default="")
    endpoint: Mapped[str] = mapped_column(String(512), default="")
    api_key: Mapped[str] = mapped_column(String(512), default="")
    model: Mapped[str] = mapped_column(String(128), default="")

    # ---- 以下是调用层（llm_gateway）需要的元信息 ----
    # 厂商 API 风格（openai_compatible / openai / anthropic / gemini / ollama ...），
    # 取值表见 model_catalog.API_STYLES。空串按默认的 openai_compatible 处理：
    # 老库升级上来的行没有这一列（或为 NULL），必须能当 OpenAI 兼容来调。
    #
    # 以下五个列都刻意带 server_default：本项目没有 Alembic，老库靠 schema_patch
    # 的 ALTER TABLE ADD COLUMN 补列，DDL 里带上 DEFAULT 才能让**已有行**也取到值
    # （否则旧行读出 None，Pydantic 序列化会直接报字段类型错误）。
    # 因此往 model_catalog / 这里加列时，必须同步把同一份默认值登记进
    # schema_patch.NEW_COLUMNS，两边不一致就会炸在升级上来的老库上。
    api_style: Mapped[str] = mapped_column(String(32), default="", server_default="")
    # 是否支持流式返回（SSE）。本地部署的小模型常把 kv-cache 占住慢慢吐，
    # 云厂商一般 5xx 之前都支持；配错会在首次请求就被厂商以 400 拒掉，
    # 所以宁可按「不支持」保守处理。
    supports_stream: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 单次请求超时（秒）。云端 LLM 通常几秒内回，本地 7B/14B 首 token 可能要十几秒，
    # 写死一个全局值必然有一边被拖垮，所以每条配置自己带一个。
    timeout_sec: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    # 单条配置的失败重试次数（不含首次调用），0 = 不重试，最多 5 次。
    # 注意这是「同一条」的重试；整条不可用会按 priority 换下一条配置。
    max_retries: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # 上下文长度上限（token）。0 = 未知/不限制；>0 时调用层据此裁剪对话历史，
    # 否则长对话会直接被厂商拒绝（context_length_exceeded）。
    context_window: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # ---- 角色字段（人格的统一落点）----
    description: Mapped[str] = mapped_column(String(255), default="")
    # 角色人格 / 系统提示词（用户可编辑的**软性**部分；硬红线由代码常量注入）
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    # 角色音色，如 zh-CN-XiaoxiaoNeural。为空时回落到所绑定形象的 tts_voice
    tts_voice: Mapped[str] = mapped_column(String(64), default="")
    # 是否默认角色（APP 新建角色时可选中）
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # 厂商特有参数（温度、top_p、采样率、音色等）
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 数字越小优先级越高，主用失败时自动切下一个
    priority: Mapped[int] = mapped_column(Integer, default=10)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # ---- 归属与生命周期（新增列，全部登记进 schema_patch.NEW_COLUMNS）----
    # 这行到底是什么：config / preset / role。老库回填规则见 schema_patch
    # （kind 非空 -> config；kind 为空 -> preset，因为老库没有 owner 字段，
    #   无法证明某行属于谁，判给平台是安全侧选择）。
    record_type: Mapped[str] = mapped_column(
        String(16), default=RECORD_TYPE_CONFIG, server_default=RECORD_TYPE_CONFIG)
    # 归属用户。**只有 record_type == 'role' 才有值**；config / preset 恒为 NULL。
    # 有了它，「这是我的角色吗」才有判据 —— 这正是 P0-1 越权删除的根治点。
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # 派生来源（copy-on-write）。NULL = 原创；preset 也是 NULL。
    # 派生只复制一份数据、不共享，所以母版后续修改不会影响已派生的角色（INV-1）。
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id"), nullable=True)
    # 角色可选绑定的形象（形象已退化为纯外观/灯效）
    avatar_id: Mapped[int | None] = mapped_column(ForeignKey("avatars.id"), nullable=True)
    # traits **只存 overlay**（与母版不同的项），未覆盖项读母版当前值。
    # 这样「还原为预设」= 清空本列一行搞定，diff = 与母版求差集。
    traits: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # {"hard": [...], "soft": [...]}；hard 段每项带 locked 标记，仅供 UI 展示，
    # 编译时**不信**这份数据，一律用代码常量重新生成（见 persona/guardrails.py）。
    guardrails: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 软删标记。角色软删（保留画像/示例/审计，见架构 §1.4），
    # 平台配置维持硬删（它的价值全在 api_key，留着是负资产）。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 预设下架标记。仅 preset 用：硬删母版会让所有派生角色的 parent_id 悬空。
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ---- 安全承诺卡的确认回执（架构 §12 U5：确认是「角色级」事件，
    #      塞进 agent_persona_audits 会污染那张「每轮对话」表的语义）----
    guardrail_ack_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    guardrail_ack_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # 确认时红线的版本号。红线版本一变，这一列就与当前常量对不上 -> 需重新确认。
    guardrail_ack_version: Mapped[str] = mapped_column(String(32), default="", server_default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # ============================================================ 判定方法
    # 这四个方法是**全部权限判断的唯一入口**。散落在各router 里现写
    # `if row.record_type == "role" and row.owner_id == user.id` 必然漂移：
    # 漏一处就是一个越权洞。所以路由层一律调can_write / is_visible_to，
    # 不自己拼条件。
    def is_config(self) -> bool:
        """是否平台配置（参与设备下发 / 小智导出 / 网关解析）。"""
        return (self.record_type or RECORD_TYPE_CONFIG) == RECORD_TYPE_CONFIG

    def is_role(self) -> bool:
        """是否角色行。

        额外要求 ``kind == ''`` 作为**双保险**：即使 record_type 被误改成
        'config'，一个非空的 kind 也不会让角色混进平台配置的下发路径。
        """
        return (self.record_type in (RECORD_TYPE_PRESET, RECORD_TYPE_ROLE)) and not (self.kind or "")

    def is_preset(self) -> bool:
        """是否平台预设角色（公开只读，仅管理员可写）。"""
        return self.record_type == RECORD_TYPE_PRESET

    def is_alive(self) -> bool:
        """未软删、未下架。"""
        return self.deleted_at is None and self.archived_at is None

    def is_visible_to(self, user: "User") -> bool:
        """读权限：preset 任何人可读；role 仅 owner / 管理员。"""
        if getattr(user, "is_admin", False):
            return True
        if self.is_preset():
            return True
        return self.owner_id is not None and self.owner_id == user.id

    def can_write(self, user: "User") -> bool:
        """写权限：preset -> 仅管理员；role -> owner 或管理员；config -> 仅管理员。"""
        if self.is_config():
            return bool(getattr(user, "is_admin", False))
        if self.is_preset():
            return bool(getattr(user, "is_admin", False))
        return bool(getattr(user, "is_admin", False)) or (
            self.owner_id is not None and self.owner_id == user.id)


# ================================================================ 提醒规则
class ScheduleKind(IntEnum):
    """重复类型。"""

    ONCE = 0           # 指定日期一次性
    DAILY = 1          # 每天
    WEEKLY = 2         # 每周指定几天
    MONTHLY = 3        # 每月指定几号
    INTERVAL_DAYS = 4  # 每隔 N 天


class ReminderType(IntEnum):
    """提醒类别。"""

    MEDICINE = 0   # 服药
    SCHEDULE = 1   # 日程
    WATER = 2      # 喝水
    EXERCISE = 3   # 运动
    CUSTOM = 4     # 自定义


class ConfirmMethod(IntEnum):
    """确认方式。"""

    NONE = 0
    VOICE = 1      # 语音关键词（离线命令词）
    BUTTON = 2     # 物理按键长按 3s + 30s 内短按
    APP = 3        # 手机端代确认
    AUTO = 4       # 超时自动确认（不建议用于服药）


class Reminder(Base):
    """提醒规则。支持一天多次、按周/按月/隔天、生效区间等复杂逻辑。"""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    creator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    title: Mapped[str] = mapped_column(String(128))
    # 播报文案，为空时回落到 title。TTS 离线兜底音效按 type 选取
    speak_text: Mapped[str] = mapped_column(String(512), default="")
    rtype: Mapped[int] = mapped_column(Integer, default=int(ReminderType.MEDICINE))
    schedule_kind: Mapped[int] = mapped_column(Integer, default=int(ScheduleKind.DAILY))

    # 一天内的触发时刻列表，如 ["08:00", "12:30", "18:00"]
    times_of_day: Mapped[list[str]] = mapped_column(JSON, default=list)
    # WEEKLY：1=周一 ... 7=周日
    weekdays: Mapped[list[int]] = mapped_column(JSON, default=list)
    # MONTHLY：1~31
    month_days: Mapped[list[int]] = mapped_column(JSON, default=list)
    # INTERVAL_DAYS：每隔 N 天
    interval_days: Mapped[int] = mapped_column(Integer, default=1)
    # ONCE：YYYY-MM-DD
    once_date: Mapped[str | None] = mapped_column(String(16), nullable=True)

    start_date: Mapped[str | None] = mapped_column(String(16), nullable=True)
    end_date: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # 服药剂量等补充信息，展示用
    dose: Mapped[str] = mapped_column(String(128), default="")

    # ---- 确认与升级策略 ----
    require_confirm: Mapped[bool] = mapped_column(Boolean, default=True)
    # 触发后未确认，间隔多少分钟重复播报一次
    repeat_interval_min: Mapped[int] = mapped_column(Integer, default=10)
    # 最多重复几次
    repeat_times: Mapped[int] = mapped_column(Integer, default=3)
    # 超过该分钟数仍未确认 -> 升级（红灯常亮 + 通知子女）
    escalate_after_min: Mapped[int] = mapped_column(Integer, default=30)
    # 允许哪些确认方式，["voice","button","app"]
    confirm_methods: Mapped[list[str]] = mapped_column(JSON, default=list)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    device: Mapped["Device"] = relationship(back_populates="reminders")
    events: Mapped[list["ReminderEvent"]] = relationship(
        back_populates="reminder", cascade="all, delete-orphan"
    )


class EventState(IntEnum):
    """单次提醒实例的状态流转。"""

    PENDING = 0     # 已排期，未到点
    FIRED = 1       # 已响铃/播报，等待确认
    CONFIRMED = 2   # 已确认（绿灯）
    MISSED = 3      # 超时未确认（红灯常亮）
    SKIPPED = 4     # 主动跳过


class ReminderEvent(Base):
    """一次具体的提醒实例（由服务端预排，或设备离线触发后回传）。"""

    __tablename__ = "reminder_events"
    __table_args__ = (
        UniqueConstraint("reminder_id", "due_at", name="uq_reminder_due"),
        Index("ix_event_device_due", "device_id", "due_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reminder_id: Mapped[int] = mapped_column(ForeignKey("reminders.id"), index=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    # 计划触发时刻（设备本地时区）
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    state: Mapped[int] = mapped_column(Integer, default=int(EventState.PENDING))
    confirm_method: Mapped[int] = mapped_column(Integer, default=int(ConfirmMethod.NONE))
    # 已重复播报次数
    fired_count: Mapped[int] = mapped_column(Integer, default=0)
    # 是否已升级（红灯 + 通知子女）
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(String(255), default="")

    reminder: Mapped["Reminder"] = relationship(back_populates="events")


# ================================================================ 交互日志
class InteractionKind(IntEnum):
    WAKE = 0             # 语音唤醒
    DIALOG = 1           # 一轮对话
    REMINDER_CONFIRM = 2 # 提醒确认
    REMINDER_MISS = 3    # 提醒超时未确认
    SOS = 4              # 求助键
    TOUCH = 5            # 屏幕触摸


class Interaction(Base):
    """客户端交互日志，用于「查看客户端交互次数」（需求：子女端统计）。"""

    __tablename__ = "interactions"
    __table_args__ = (Index("ix_ia_device_ts", "device_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    kind: Mapped[int] = mapped_column(Integer, default=int(InteractionKind.DIALOG))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    # 扩展信息：如 ASR 文本、情绪标签、提醒事件 id
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# ================================================================ 会员
# 当前没有支付接口，走「用户线下转账 -> 订单号 + 随机金额 -> 管理员肉眼核对 -> 手动核销」链路。
# 详见 docs/会员功能规划.md。
class MembershipSource:
    """会员来源。"""

    ORDER = "order"    # 订单核销
    ADMIN = "admin"    # 后台直接分配
    GIFT = "gift"      # 赠送 / 补偿


class MembershipStatus:
    """会员记录状态。"""

    ACTIVE = "active"
    EXPIRED = "expired"


class Membership(Base):
    """用户会员记录。

    一个用户只保留一行（uq_membership_user），续费时在原记录上延长 expires_at，
    便于 GET /api/membership 一次查清状态；开通历史由 MembershipOrder 承担审计。
    """

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_membership_user"),
        Index("ix_membership_expires", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # 套餐 code（membership_plans.code），后台直接分配时填自定义标识
    plan: Mapped[str] = mapped_column(String(32), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), default=MembershipSource.ORDER)
    status: Mapped[str] = mapped_column(String(16), default=MembershipStatus.ACTIVE, index=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MembershipPlan(Base):
    """会员套餐（定义开通天数与展示价，实际收款金额以订单的随机金额为准）。"""

    __tablename__ = "membership_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(255), default="")
    days: Mapped[int] = mapped_column(Integer, default=30)
    # 展示价（整数分），仅用于页面展示与套餐对比，不参与实际收款对账
    price_cents: Mapped[int] = mapped_column(Integer, default=0)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OrderStatus:
    """订单状态机：pending -> notified -> confirmed / rejected，旁支 cancelled / expired。"""

    PENDING = "pending"      # 已创建，等待用户付款
    NOTIFIED = "notified"    # 用户声称已付款，等待管理员核对
    CONFIRMED = "confirmed"  # 管理员确认到账，会员已开通
    REJECTED = "rejected"    # 管理员未查到入账
    CANCELLED = "cancelled"  # 用户主动放弃
    EXPIRED = "expired"      # 超过 48 小时未支付


class MembershipOrder(Base):
    """会员订单。

    code 为 4 位 `[A-Z0-9]`，落库与查询统一大写；
    amount_cents 为整数分，在区间内随机且 48 小时内不重复，
    管理员凭「订单号 + 金额」在收款账号里反查唯一订单。
    """

    __tablename__ = "membership_orders"
    __table_args__ = (
        Index("ix_order_code", "code"),
        Index("ix_order_status_created", "status", "created_at"),
        Index("ix_order_amount", "amount_cents"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(4), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan_code: Mapped[str] = mapped_column(String(32), default="")
    # 实际应付金额（整数分，禁止浮点）
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default=OrderStatus.PENDING, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 核销管理员与备注（审计）
    admin_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    admin_note: Mapped[str] = mapped_column(String(255), default="")


class MembershipAmountRange(Base):
    """金额区间配置（单行）：随机收款金额的取值区间，单位分。"""

    __tablename__ = "membership_amount_range"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    min_cents: Mapped[int] = mapped_column(Integer, default=1000)
    max_cents: Mapped[int] = mapped_column(Integer, default=9999)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PaymentConfig(Base):
    """收款信息配置（单行）：收款账号 / 收款二维码，由管理端维护后下发给 APP。"""

    __tablename__ = "payment_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 收款渠道名，如「微信」「支付宝」「银行卡」
    channel: Mapped[str] = mapped_column(String(32), default="微信")
    payee_name: Mapped[str] = mapped_column(String(64), default="")
    account: Mapped[str] = mapped_column(String(128), default="")
    qr_url: Mapped[str] = mapped_column(String(512), default="")
    note: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChatQuotaConfig(Base):
    """陪聊配额配置（单行）：非会员每日秒数、空闲切断秒数、会员是否不限。"""

    __tablename__ = "chat_quota_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    free_daily_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    idle_timeout_seconds: Mapped[int] = mapped_column(Integer, default=90)
    vip_unlimited: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChatUsage(Base):
    """设备每日陪聊时长（服务端按日对账，端侧本地也有一份兜底统计）。"""

    __tablename__ = "chat_usage"
    __table_args__ = (UniqueConstraint("device_id", "date", name="uq_chat_usage_device_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    # 设备本地日期 YYYY-MM-DD
    date: Mapped[str] = mapped_column(String(10), index=True)
    used_seconds: Mapped[int] = mapped_column(Integer, default=0)
    session_count: Mapped[int] = mapped_column(Integer, default=0)
    # 服药/提醒类会话不计入 used_seconds，这里单独累计便于排查
    reminder_seconds: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ModelUsage(Base):
    """模型调用用量计量（token 级）：每一次 LLM/ASR/TTS 调用落一行。

    为什么单独建表而不并进 chat_usage
    ---------------------------------
    chat_usage 是「陪聊秒数」的**设备日汇总**，服务配额判定；这里是
    「token 数」的**调用级流水**，服务成本对账与「设备 token 用量」展示。
    粒度（秒 vs 次）、用途（配额 vs 对账）、保留策略都不同，硬合进一张表
    会让两件事都做不好。

    为什么不给 provider_id 建外键
    -----------------------------
    平台配置行（ai_providers）允许被删除 / 重建，而计量数据必须留存对账 ——
    外键级联会把账本删出洞来。provider_name / model 冗余存字符串，正是为了
    配置行消失之后这一行仍然自解释。

    新表由 init_db 的 create_all 自动创建（老库升级同样生效），
    不需要 schema_patch 补列。
    """

    __tablename__ = "model_usage"
    __table_args__ = (
        Index("ix_usage_device_created", "device_id", "created_at"),
        Index("ix_usage_user_created", "user_id", "created_at"),
        Index("ix_usage_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 调用来源：voice = 语音链路 WS 会话；http = 服务内其它调用（记忆抽取等）
    source: Mapped[str] = mapped_column(String(16), default="", index=True)
    # 用途，取值见 model_catalog.USAGE_*（chat / light / asr / tts / embed / rerank）
    usage: Mapped[str] = mapped_column(String(16), default="", index=True)
    # 设备 / 用户 / 角色上下文：可空（启动期或后台调用没有这些）
    device_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_name: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # 一次 LLM 调用的追踪 id（同一语音会话内多轮可串起来）；ASR/TTS 可为空
    request_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    session_id: Mapped[str] = mapped_column(String(40), default="")
    # 失败调用也落一行（ok=False + error）：排查「为什么用量是 0」时，
    # 「调用过但失败了」与「从来没调用过」必须可区分。
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(String(255), default="")
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReminderKeyword(Base):
    """服药/提醒关键词表，下发给设备做本地意图判定（命中则不占用陪聊配额）。"""

    __tablename__ = "reminder_keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    word: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ================================================================ 邮箱服务
# 需求：平台需要一个邮箱配置，用来 (1) 用户通过邮箱找回密码、(2) 用户通过邮箱提交问题。
# 用户的所有操作都在手机 APP 完成、不访问任何网页后台，所以下面三张表只服务于 API，
# 没有任何服务端渲染页面依赖它们。
class SmtpConfig(Base):
    """平台 SMTP 配置（单行表，约定只使用 id=1 那一行）。

    字段与邮件发送层一一对应（见 app/mailer.py）：
      * use_ssl  直连 SSL，典型端口 465；
      * use_tls  先明文握手再 STARTTLS 升级，典型端口 587。
        两者互斥，实际以管理员填的端口为准，这里不做强制校验（各家邮箱服务商
        的端口/加密组合并不统一，硬校验只会把正确配置也拦下来）。

    ⚠️ password 在库里是**明文**存的，与 ai_providers.api_key 的处理方式一致：
    本项目是私有化部署的家庭场景，不做 KMS / 密钥托管，引入加密只会带来
    「密钥放哪儿」这个同级别的问题。真正的防护落在接口层：
    GET 永不返回明文（只给掩码），PATCH 接受写入，日志不打印该字段。

    TODO（已知、本轮有意不做）：要加固就应给这类机密字段统一加一层基于
    ELDER_JWT_SECRET 的对称加密，且必须与 ai_providers.api_key **一起做**，
    不单独立项 —— 只加密 SMTP 密码而留下 api_key 明文，等于什么都没解决。
    """

    __tablename__ = "smtp_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host: Mapped[str] = mapped_column(String(128), default="")
    port: Mapped[int] = mapped_column(Integer, default=465)
    username: Mapped[str] = mapped_column(String(128), default="")
    password: Mapped[str] = mapped_column(String(255), default="")
    use_tls: Mapped[bool] = mapped_column(Boolean, default=False)
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    from_addr: Mapped[str] = mapped_column(String(128), default="")
    from_name: Mapped[str] = mapped_column(String(64), default="")
    # 工单通知的收件邮箱：留空时回落到 from_addr（发件箱即管理员邮箱，
    # 私有化部署下通常就是同一个）。单独给一个字段是考虑到「用 noreply@ 发件、
    # 但通知要进 support@ 收件箱」这种常见拆法。
    notify_to: Mapped[str] = mapped_column(String(128), default="")
    # 默认关闭：没配过的平台不该假装能发信（会让用户以为验证码已发出）
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PasswordResetCode(Base):
    """邮箱找回密码的一次性验证码。

    为什么是「6 位数字验证码」而不是「带签名的 token」：
      * 用户的所有操作都在 APP 里完成，没有网页后台，token 方案唯一的优势
        （点开一条深链直接进重置页）在这里无处安放，用户拿到也只能在 APP 里
        手输 —— 那还不如直接给 6 位数字，输入法体验最好、老人也抄得下来；
      * token 一旦被截获（邮件明文传输、手机通知栏预览）就是完整凭证，
        而验证码即便泄露，也只有 15 分钟窗口 + 5 次尝试机会。

    code_hash 用 security.hash_password()（PBKDF2-SHA256 + 随机盐）存，
    不存明文：这样数据库被整份拷走也拿不到验证码，不能直接重置任意账号。

    ⚠️ user_id 可空，且**未注册的邮箱也会建一条 user_id=None 的行**，
    这是账号枚举防护的一部分，理由见 routers/mail.py 的 request_reset()。
    """

    __tablename__ = "password_reset_codes"
    __table_args__ = (Index("ix_reset_email_created", "email", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 统一小写存：邮箱的域名部分大小写不敏感，本地部分按 RFC 虽敏感但实际无人依赖，
    # 统一小写才能保证「找回时填的写法和注册时不同」也能命中。
    email: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    code_hash: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 已错误尝试次数，达到 max_attempts 即作废
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    # 非空表示已消费（成功重置 / 超次作废 / 被新验证码顶掉），一次性
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TicketCategory:
    """工单分类。落库存英文短码，展示时转中文（APP 只展示，不解析）。"""

    PAYMENT = "payment"      # 会员支付
    DEVICE = "device"        # 设备故障
    REMINDER = "reminder"    # 提醒异常
    ACCOUNT = "account"      # 账号问题
    OTHER = "other"          # 其它

    TEXTS = {
        PAYMENT: "会员支付",
        DEVICE: "设备故障",
        REMINDER: "提醒异常",
        ACCOUNT: "账号问题",
        OTHER: "其它",
    }


class TicketStatus:
    """工单状态机：open -> processing -> resolved / closed（管理员可回退重开）。"""

    OPEN = "open"              # 待处理
    PROCESSING = "processing"  # 处理中
    RESOLVED = "resolved"      # 已解决
    CLOSED = "closed"          # 已关闭

    TEXTS = {
        OPEN: "待处理",
        PROCESSING: "处理中",
        RESOLVED: "已解决",
        CLOSED: "已关闭",
    }


class SupportTicket(Base):
    """用户通过邮箱提交的问题（工单）。

    允许匿名提交（user_id 可空）但**必须留邮箱**：忘记密码的用户根本登不进 APP，
    如果强制登录才能提工单，「找回密码」这条路断了就没有兜底入口了。
    登录时提交会自动带上 user_id，管理端列表据此显示用户名。
    """

    __tablename__ = "support_tickets"
    __table_args__ = (
        Index("ix_ticket_status_created", "status", "created_at"),
        Index("ix_ticket_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    email: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    # TicketCategory 里的短码，未知值一律按 other 处理，不让老数据炸掉
    category: Mapped[str] = mapped_column(String(16), default=TicketCategory.OTHER)
    # 出问题的设备（device_id 字符串，不是 devices 表主键），便于管理员定位
    device_id: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default=TicketStatus.OPEN)
    admin_reply: Mapped[str] = mapped_column(Text, default="")
    admin_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ================================================================ 长期记忆
# 设计依据见 docs/架构设计.md 第 6 节：借鉴 Mem0 2026 的 ADD-only 算法自行实现，
# 存储落在内置 SQLite，不引任何向量数据库（理由见 app/memory/store.py 的注释）。
#
# ⚠️ 三条铁律，改这几张表时不要破坏：
#   1. 记忆**只追加，不覆盖**（没有 UPDATE 语义的字段）；
#   2. 删除一律**软失效**（superseded_at），家属删掉的东西要能审计；
#   3. 一切按 (user_id, agent_id) 隔离 —— 换角色 = 另一份人格与记忆。
class MemoryCategory:
    """记忆分类。落库存英文短码，展示转中文（APP 只展示，不解析）。"""

    PROFILE = "profile"            # 个人画像：年龄、住址、职业、习惯
    PREFERENCE = "preference"      # 偏好：爱听京剧、不吃香菜
    EVENT = "event"                # 已发生的事件：上周摔了一跤
    RELATIONSHIP = "relationship"  # 人物关系：张阿姨是邻居
    MEDICINE = "medicine"          # 用药：药名、剂量、过敏
    SCHEDULE = "schedule"          # 日程：周三去透析
    OTHER = "other"

    TEXTS = {
        PROFILE: "个人情况",
        PREFERENCE: "喜好习惯",
        EVENT: "发生的事",
        RELATIONSHIP: "人际关系",
        MEDICINE: "用药健康",
        SCHEDULE: "日程安排",
        OTHER: "其它",
    }

    LIST = (PROFILE, PREFERENCE, EVENT, RELATIONSHIP, MEDICINE, SCHEDULE, OTHER)

    # medicine 与 schedule 是本产品的**高频刚需**（用药与就诊），
    # 抽取提示词里会单独强调，检索时也给更高的重要度基线。
    IMPORTANT = (MEDICINE, SCHEDULE)


class AgentMemory(Base):
    """一条长期记忆（事实）。

    ⚠️ 本表**只有 INSERT 语义**：除了 access_count / last_accessed_at 这两个
    统计字段和 superseded_at 这个软失效标记，业务上不存在「改内容」的路径。
    用户改口（"我以前养猫，现在养狗"）时是**再插一条**，靠检索时的时序打分
    决定用哪条（见 app/memory/retrieve.py）。这就是 ADD-only 的核心。
    """

    __tablename__ = "agent_memories"
    __table_args__ = (
        # 检索的主查询就是 (user_id, agent_id) 的全量扫描，复合索引是唯一必要的那个
        Index("ix_mem_user_agent", "user_id", "agent_id"),
        Index("ix_mem_user_agent_cat", "user_id", "agent_id", "category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    # 绑定的 agent 角色（ai_providers 里 kind 为空的角色记录）。
    # 可空表示「未指定角色的通用记忆」；**空值与具体 agent_id 互相隔离**，
    # 不会串台（检索条件就是这一列相等）。
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id"), nullable=True)
    # 事实文本，一句话一条，独立可理解（不依赖上下文指代）
    content: Mapped[str] = mapped_column(Text, default="")
    # MemoryCategory 短码，未知值按 other 处理
    category: Mapped[str] = mapped_column(String(16), default=MemoryCategory.OTHER)

    # ---- 检索信号 ----
    # 语义向量：float32 紧凑数组，见 app/memory/vectors.py
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # 产出该向量的模型名。用于判断向量是否过期：换了 embedding 模型（维度/语义空间
    # 都变了）必须重算，否则新旧向量混在一起算余弦等于拿两把尺子量长度。
    embedding_model: Mapped[str] = mapped_column(String(128), default="")
    embedding_dim: Mapped[int] = mapped_column(Integer, default=0)
    # 关键词路用的预分词（空格分隔的中文二元组 + 英文词），写入时算好，
    # 检索时就不必对全库重新分词了
    tokens: Mapped[str] = mapped_column(Text, default="")
    # 实体名列表（JSON 数组），冗余存一份是为了避免检索时再去 join 实体表
    entities: Mapped[list[str]] = mapped_column(JSON, default=list)

    # ---- 重要度与溯源 ----
    # 重要度 0~1，影响融合打分；用药 / 过敏 / 日程类默认更高
    salience: Mapped[float] = mapped_column(Float, default=1.0)
    # 溯源：来自哪一次对话会话（语音链路的 session_id），家属查"这条哪来的"时有用
    source_session: Mapped[str] = mapped_column(String(64), default="")
    # 原始对话里最能支撑这条事实的那句话（可空），审计用
    source_quote: Mapped[str] = mapped_column(Text, default="")

    # ---- 时序与生命周期 ----
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 软失效：非空表示已被家属删掉 / 判定为错误。**不真删行**，
    # 这样"机器人曾经学过什么、什么时候被删"始终可追溯。
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    supersede_reason: Mapped[str] = mapped_column(String(255), default="")

    # 命中次数与最近命中时间：既用于「常被用到的记忆更可信」的加权，
    # 也用于冷记忆的观察（长期 0 命中说明这条事实没用）
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentMemoryEntity(Base):
    """实体索引表：实体 -> 一组记忆 id（Mem0 图里 entity 节点的扁平版）。

    为什么Entity 要单独一张表，而不只存在 agent_memories.entities 里：
    实体检索要回答的是「提到张阿姨的记忆有哪些」，这是一次**按实体名查**的查询，
    走实体表一次 join 就出来了；全表扫 memories 再解析 JSON 在 SQLite 上是 O(n)。
    现在量小看不出差别，但实体表是「以后要做关系图」时唯一的扩展点
    （加一列 related_entity 就成图了），先留好位置。

    同一个 (user_id, agent_id, entity, memory_id) 组合是唯一的：
    一条记忆里同一个实体只记一次，避免重复计入实体信号。
    """

    __tablename__ = "agent_memory_entities"
    __table_args__ = (
        UniqueConstraint("user_id", "agent_id", "entity", "memory_id",
                         name="uq_mem_entity"),
        Index("ix_mem_entity_lookup", "user_id", "agent_id", "entity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id"), nullable=True)
    entity: Mapped[str] = mapped_column(String(64), index=True)
    # 实体类型：person / medicine / time / place / other（见 memory/textutil.py）
    entity_type: Mapped[str] = mapped_column(String(16), default="other")
    memory_id: Mapped[int] = mapped_column(ForeignKey("agent_memories.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatMessage(Base):
    """原始对话历史（Mem0 里叫 Recall Memory，是「最近说过什么」那一层）。

    与 agent_memories 的区别：这里存**逐字原文**，不做任何提炼，只用于
    (1) 组装短期上下文（最近 N 轮原样塞进 prompt）、(2) 记忆提取的输入、
    (3) 家属回放「今天聊了什么」。
    它不参与检索打分，检索只走提炼后的事实表。
    """

    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_user_agent_session", "user_id", "agent_id", "session_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id"), nullable=True)
    # 一次会话 = 设备从唤醒到结束的一轮或多轮对话
    session_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # user / assistant / system
    role: Mapped[str] = mapped_column(String(16), default="user")
    content: Mapped[str] = mapped_column(Text, default="")
    # 设备上这一句说出来的时间（老人视角的"刚才"），排期/回溯用
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ================================================================ 人格与语气演化
# 见 docs/架构设计.md 第 7 节：**不做模型微调**（成本极高且不可逆），
# 用「偏好示例池 + 语气画像 + 人工可改」三条低成本路径让机器人越用越贴合。
class PersonaSignal:
    """人格反馈信号的来源类型。"""

    CORRECTION = "correction"  # 明确纠正："不是，我的意思是 X" / "别这么说话"
    RETRY = "retry"            # 重试成功：机器人换说法后用户接受了
    POSITIVE = "positive"      # 积极反馈：笑了、接梗、继续聊
    NEGATIVE = "negative"      # 消极反馈：打断、重复提问、"算了"
    MANUAL = "manual"          # 家属手动添加（人工纠偏，与自动演化并列）

    TEXTS = {
        CORRECTION: "明确纠正",
        RETRY: "换说法后被接受",
        POSITIVE: "积极反馈",
        NEGATIVE: "消极反馈",
        MANUAL: "家属手动添加",
    }

    LIST = (CORRECTION, RETRY, POSITIVE, NEGATIVE, MANUAL)


class PreferenceKind:
    """偏好示例的正/反例。"""

    POSITIVE = "positive"  # 正例：应该这么说话
    NEGATIVE = "negative"  # 反例：不该这么说话

    TEXTS = {POSITIVE: "正例（应该这样说）", NEGATIVE: "反例（不该这样说）"}
    LIST = (POSITIVE, NEGATIVE)


class ExampleStatus:
    ACTIVE = "active"        # 生效中，会注入 prompt
    DISCARDED = "discarded"  # 已被家属/系统判定为错误学习，不再注入

    TEXTS = {ACTIVE: "生效中", DISCARDED: "已作废"}
    LIST = (ACTIVE, DISCARDED)


class AgentPreferenceExample(Base):
    """偏好示例池：把「用户反馈」沉淀成可注入 prompt 的 few-shot 正/反例。

    bot_text / user_text 的含义按 source 而变：
      * correction：bot_text = 被否掉的机器人原话（反例），user_text = 用户的表述（正例）；
      * retry：bot_text = 第一次没被接受的说法，user_text = 第二次被接受的说法；
      * positive：bot_text = 被认可的那句；
      * negative：bot_text = 引起反感的那句。
    检索后按 weight 取前几条注入 system prompt。
    """

    __tablename__ = "agent_preference_examples"
    __table_args__ = (Index("ix_pref_user_agent", "user_id", "agent_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id"), nullable=True)
    # PreferenceKind
    kind: Mapped[str] = mapped_column(String(16), default=PreferenceKind.POSITIVE)
    # PersonaSignal
    source: Mapped[str] = mapped_column(String(16), default=PersonaSignal.MANUAL)
    bot_text: Mapped[str] = mapped_column(Text, default="")
    user_text: Mapped[str] = mapped_column(Text, default="")
    # 权重 0~1：明确纠正的可信度高于"好像笑了"，重复提问这类弱信号给 0.5
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String(16), default=ExampleStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AgentPersonaProfile(Base):
    """语气画像：把「这个人喜欢机器人怎么说话」数值化。

    **数值化 + 可解释**是刻意的设计：黑盒演化一旦跑偏，家属无从下手；
    数值 + 自然语言描述（describe_tone()）既能注入 prompt，也能直接展示在
    APP「我的角色」页给家属看，还支持手动改（manual 字段优先级最高）。

    一个 (user_id, agent_id) 只有一行；统计用增量更新（EMA），
    不做全量重算 —— 老人一天聊几十轮，全量扫历史不划算。

    ⚠️ ``agent_key`` 是为修一个**已存在的唯一性漏洞**加的：
    ``UniqueConstraint("user_id", "agent_id")`` 在 SQLite 与 MySQL 下都**不约束
    ``agent_id IS NULL`` 的行**（SQL 里 ``NULL != NULL``），而 L1 全局画像恰恰是
    ``agent_id IS NULL`` 的那一行。于是理论上能插入多行 L1 画像，而
    ``_get_profile`` 用 ``db.scalar(select(...))`` 取值 —— 读哪一行是不确定的，
    且不报错，只是静默给出错误的语气画像。
    修法是加一个**哨兵普通列** ``agent_key``（0 = 全局，否则等于 agent_id）
    并对它建唯一索引：``(user, 0)`` 这一行从此真正受约束保护。
    不用 SQL 生成列，因为 SQLite 3.31+ 才支持、与 MySQL 语法也不同；
    也不用部分索引（``WHERE agent_id IS NULL``），两种库行为不一致。
    """

    __tablename__ = "agent_persona_profiles"
    __table_args__ = (
        # 老约束保留不动（schema_patch 不删约束），新约束才是真正生效的那个。
        UniqueConstraint("user_id", "agent_id", name="uq_persona_user_agent"),
        UniqueConstraint("user_id", "agent_key", name="uq_persona_user_agentkey"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id"), nullable=True)
    # 0 = L1 全局画像；= agent_id = L2 角色画像。由应用层写入时计算
    # （memory/persona._get_profile），不依赖任何数据库方言特性。
    agent_key: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # ---- 统计侧（机器自动算，EMA 增量）----
    replies_count: Mapped[int] = mapped_column(Integer, default=0)
    positive_count: Mapped[int] = mapped_column(Integer, default=0)
    negative_count: Mapped[int] = mapped_column(Integer, default=0)
    # 被认可的回复的平均字数（老人普遍偏好短句）
    avg_reply_len: Mapped[float] = mapped_column(Float, default=0.0)
    # 用户自己说话的平均字数，作为"该怎么接话"的参考
    avg_user_len: Mapped[float] = mapped_column(Float, default=0.0)
    # 爱开玩笑的程度 0~1
    humor_score: Mapped[float] = mapped_column(Float, default=0.0)
    # 称呼习惯：您 / 你 / 爷爷 / 奶奶 / 阿姨 ...（取被认可的回复里出现最多的那个）
    address: Mapped[str] = mapped_column(String(32), default="")
    # 称呼的计数明细 {"您": 12, "爷爷": 3}：只存结果列就没法在"换称呼"时正确改判，
    # 老人对称呼很敏感，宁可多存一个字典
    address_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 语速偏好：slow / normal / fast（由句长与标点密度推，最后由家属可改）
    speed: Mapped[str] = mapped_column(String(16), default="normal")
    # 口头禅：{"短语": 出现次数}
    catchphrases: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # ---- 人工侧（家属手动覆盖，优先级高于上面的统计值）----
    # 形如 {"avg_reply_len": 25, "humor": false, "address": "爷爷",
    #       "speed": "slow", "note": "别跟他说网络梗"}
    manual: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AgentPersonaAudit(Base):
    """人格演化的**审计记录**：每一次"机器人以为自己学到了什么"都留一行。

    ⚠️ 这一层比自动学习本身更重要。原因写在 docs/架构设计.md 第 7.1 节：
    老人的表达可能含糊，也可能被旁人逗弄——"哈哈哈"可能是觉得好笑，
    也可能是觉得机器人蠢得可笑。所以：

      * 每条推断**先判有效性**再入库，判定结果（valid / reason）原样留档；
      * 即便判定为无效，也照记一条（valid=False），家属能看到
        "机器人刚才差点被逗，还好没学"；
      * 生效的推断指向 example_id，家属一键作废即可，作废也留痕
        （discarded_at / discard_reason）。

    没有这层，"越用越懂用户"就变成了"越用越不知道它在想什么"。
    """

    __tablename__ = "agent_persona_audits"
    __table_args__ = (Index("ix_audit_user_agent", "user_id", "agent_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id"), nullable=True)
    # PersonaSignal 短码
    signal: Mapped[str] = mapped_column(String(16), default="")
    # 触发这次推断的原始语料（用户说的 / 机器人说的 / 机器人上一句）
    user_text: Mapped[str] = mapped_column(Text, default="")
    bot_text: Mapped[str] = mapped_column(Text, default="")
    prev_bot_text: Mapped[str] = mapped_column(Text, default="")
    # 人话描述："机器人学到了什么"，直接给 APP 展示用
    inferred: Mapped[str] = mapped_column(Text, default="")
    # 是否判定为**有效**反馈。False 表示"看到了但没学"
    valid: Mapped[bool] = mapped_column(Boolean, default=False)
    # 判定理由（为什么有效 / 为什么当成噪声丢弃），同样给家属看
    reason: Mapped[str] = mapped_column(Text, default="")
    # 关联的偏好示例（valid=True 时通常有）
    example_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 家属作废该学习的留痕。**不删行**，删行就谈不上审计了
    discarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discard_reason: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ================================================================ 管理操作审计
class AdminPresetAudit(Base):
    """**管理员改平台人格预设**的审计（逐字段 from→to）。

    ⚠️ 这张表里**绝对不出现**的东西（设计 §3.2，将来有人要加字段时必读）
    ------------------------------------------------------------------
    * **硬红线正文**：红线不在可改字段集合里，diff 里天然不会有；但必须守住
      —— 否则红线文案会从代码常量流进一张可变的、可被管理端读走的表。
    * **红线 tokens**：``catalog.HARD_GUARDRAILS[*].tokens`` 是
      ``guardrails.assert_present()`` 做 fail-closed 自检的**唯一判定基准**。
      把它写进审计表 = 把"门禁怎么判"写进一张运营可读的表。断言 T10 守。
    * **任何用户派生角色的内容**：派生角色带 ``owner_id``，抄进来这张表就
      成了跨用户数据表，而它只受 ``get_current_admin`` 保护，比 ``ai_providers``
      的行级可见性保护更弱。
    * **编译产物全文**（画像摘要、偏好示例、家属写的话）：那是运行时数据，
      含用户与家属的私密内容。审计表绝不能成为用户内容的副本。

    ``admin_username`` 是**快照**而不是 join 查：用户改名后审计必须仍然
    回答"当时是谁改的"。``preset_id`` 不设 ON DELETE —— preset 只能 archive，
    不能硬删（``/api/roles/{id}`` 的 DELETE 对 preset 永远 403）。
    """

    __tablename__ = "admin_preset_audits"
    __table_args__ = (
        # 「这个预设历史上被改过什么」与「最近的操作流水」是两条最常见的查询
        Index("ix_admin_preset_audits_preset_created",
              "preset_id", "created_at"),
        Index("ix_admin_preset_audits_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    preset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ai_providers.id"), nullable=False)
    admin_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False)
    # 快照，不 join（理由见类注释）
    admin_username: Mapped[str] = mapped_column(String(64), default="")
    # update / reset / archive
    action: Mapped[str] = mapped_column(String(16), default="update")
    # ["system_prompt", "traits.humor", "soft_guardrails"]
    changed_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    # {"system_prompt": {"from": ..., "to": ...}, "traits.humor": {...}}
    diff: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 操作当时的红线版本：改红线语义后要能回答"这条改动是在哪版红线下做的"
    guardrail_version: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ================================================================ 迁移登记（不是业务表）
class PatchMeta(Base):
    """记录「哪些**会改业务数据**的迁移**已经执行过**」。

    ⚠️ **这不是业务表，是迁移状态表**，所以放在 models.py 的最后、
    与所有业务模型明确分开。理由：本项目**没有 Alembic**（见 schema_patch
    模块注释），于是"代码版本"与"迁移执行记录"必须自己找地方落 ——
    这张表就是那个地方。

    为什么只登记「改业务数据」的迁移
    --------------------------------
    登记是有成本的：每登记一个 id，就多一份"将来要不要重跑"的解释负担。
    而两种迁移的性质完全不同：

    * **补列 / 补索引** —— 幂等且**无副作用**（重复执行不改变任何已有行的
      业务含义）。重复跑一百次与跑一次完全等价，登记它纯属负担。
    * **回填 / 合并 / 删除** —— **会改业务数据**，且往往不可逆
      （把用户的 role 刷成 preset、把重复画像行 DELETE 掉）。
      这类必须"只执行一次"，否则每次启动都在改数据。

    → 所以 :data:`app.schema_patch.PATCH_REGISTRY` 里**只登记改业务数据的**。
    补列与补索引**刻意不登记**。这个区分很重要：一旦"什么都登记"，
    标记位就退化成"启动时跳过一切"的开关，那才是真正的危险。

    为什么必须有这层（不能只靠"判据正确"）
    ------------------------------------
    **判据正确**与**只执行一次**是两个**独立**的性质，不能互相替代：

    * 只修判据（``owner_id IS NULL``）→ 现在对了，但将来有人把 WHERE
      条件改错一处（这在 review 里**极难发现**，因为改完看起来完全合理），
      就会**重演同一个 P0**；
    * 只加标记位 → 第一次执行时的判据若本来就是错的，那错误会被**永久固化**
      （跑一次错的东西，之后再也不跑，看起来还"幂等"了）。

    两层都要：**判据对**保证第一次改对，**标记位**保证第一次之后不再改。
    后者才是真正兜底的那层 —— 它让"判据被改坏"从**数据事故**降级为
    "迁移没生效"（可发现、可修）。
    """

    __tablename__ = "patch_meta"

    # 迁移 id。用字符串主键而不是自增整数：迁移 id 应当是**可读、可写死、
    # 可在代码里 grep 到**的语义标识（见 schema_patch.PATCH_REGISTRY），
    # 自增 id 做不到这一点。
    patch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)
