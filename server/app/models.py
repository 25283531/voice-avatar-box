"""ORM 数据模型。

设计要点
--------
1. 设备离线也能响铃（需求 8），所以提醒规则会**下发到设备**，
   提醒事件（ReminderEvent）由设备本地产生、联网后回传对账。
2. 形象（Avatar）与提示词、TTS 音色、情绪灯效映射绑定在一起，
   一台设备同一时刻只有一个生效形象，便于端侧只维护一份资源。
3. AI 供应商（AIProvider）按能力拆行（asr/llm/tts/vad），
   可同时配置多个并靠 priority 决定主备，满足需求 15「多平台接入」。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import IntEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
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
    """形象（角色）+ 系统提示词 + 音色 + 情绪映射。

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

    # 乐鑫 EAF 序列帧资源包（esp_emote_expression / esp_emote_gfx）
    eaf_pack_name: Mapped[str] = mapped_column(String(128), default="")
    eaf_pack_url: Mapped[str] = mapped_column(String(512), default="")
    eaf_pack_sha256: Mapped[str] = mapped_column(String(64), default="")
    # 屏幕规格，切换平台（320x240 横屏 / 360x360 圆屏）时用于校验资源是否匹配
    screen_width: Mapped[int] = mapped_column(Integer, default=320)
    screen_height: Mapped[int] = mapped_column(Integer, default=240)

    # 片段名映射，键为上面约定的语义名，值为资源包内实际片段名
    clips: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)

    # AI 人格
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    tts_voice: Mapped[str] = mapped_column(String(64), default="")

    # 情绪 -> RGB 灯效，键为情绪名，值为 {"rgb":"#RRGGBB","effect":"breath|blink|solid|rainbow"}
    emotion_lights: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ================================================================ AI 供应商
class AIProvider(Base):
    """多平台 AI API 接入配置（需求 15）+ 自建 agent 角色（APP「我的 agent 角色」）。

    同一张表承载两类记录，靠 ``kind`` 是否为空区分：
      * kind 为 asr / llm / tts / vad：一条真实可调用的平台配置，会参与
        /api/device/sync 的下发与 /api/providers/export/xiaozhi 的导出；
      * kind 为空字符串：APP 自建的「agent 角色」，只有人格（system_prompt）、
        音色（tts_voice）等描述性字段，不参与下发与导出（否则会被当成一条
        真实的 LLM 配置，把既有 AI 平台配置顶掉）。
    APP 的 AgentRole（见 app/src/api/elder/index.ts）走的就是后一种。
    """

    __tablename__ = "ai_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    # asr / llm / tts / vad；为空表示这是一条「角色」记录而非平台配置
    kind: Mapped[str] = mapped_column(String(16), default="", index=True)
    # 厂商：aliyun / volcengine / deepseek / zhipu / openai / ollama / dify / xinference ...
    provider: Mapped[str] = mapped_column(String(32), default="")
    endpoint: Mapped[str] = mapped_column(String(512), default="")
    api_key: Mapped[str] = mapped_column(String(512), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    # ---- 以下是 agent 角色字段（APP 的 AgentRole），平台配置用不到 ----
    description: Mapped[str] = mapped_column(String(255), default="")
    # 角色人格 / 系统提示词
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    # 角色音色，如 zh-CN-XiaoxiaoNeural
    tts_voice: Mapped[str] = mapped_column(String(64), default="")
    # 是否默认角色（APP 新建角色时可选中）
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # 厂商特有参数（温度、top_p、采样率、音色等）
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 数字越小优先级越高，主用失败时自动切下一个
    priority: Mapped[int] = mapped_column(Integer, default=10)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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


class ReminderKeyword(Base):
    """服药/提醒关键词表，下发给设备做本地意图判定（命中则不占用陪聊配额）。"""

    __tablename__ = "reminder_keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    word: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
