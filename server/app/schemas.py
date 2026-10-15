"""Pydantic 进出参模型（Pydantic v2）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    name: str
    description: str = ""
    eaf_pack_name: str = ""
    eaf_pack_url: str = ""
    eaf_pack_sha256: str = ""
    screen_width: int = 320
    screen_height: int = 240
    clips: dict[str, str] = Field(default_factory=dict)
    system_prompt: str = ""
    tts_voice: str = ""
    emotion_lights: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class AvatarOut(AvatarIn):
    id: int
    created_at: datetime


# ============================================================ AI 供应商 / agent 角色
# 同一张表两类记录（详见 models.AIProvider 的类注释）：
#   kind 非空 -> 一条真实可调用的平台配置（asr/llm/tts/vad），参与下发与导出；
#   kind 为空 -> APP 自建的 agent 角色，只有人格与音色，不参与下发与导出。
# 因此 kind / provider 都可空：APP 的「创建我的 agent 角色」只传
# {name, description, system_prompt, tts_voice} 也要能建成。
class ProviderIn(ORMBase):
    name: str
    kind: str = Field(default="", pattern="^(asr|llm|tts|vad)?$")
    provider: str = ""
    endpoint: str = ""
    api_key: str = ""
    model: str = ""
    # ---- agent 角色字段，对应 APP 的 AgentRole ----
    description: str = ""
    system_prompt: str = ""
    tts_voice: str = ""
    is_default: bool = False
    params: dict[str, Any] = Field(default_factory=dict)
    priority: int = 10
    enabled: bool = True


class ProviderOut(ProviderIn):
    id: int
    created_at: datetime


class ProviderSafeOut(ORMBase):
    """下发给设备/APP 的脱敏配置，不含 api_key。

    APP 的「系统形象 / agent 角色列表」走的就是这个接口（/api/providers/safe），
    所以角色相关的字段也要带上，否则 APP 拿到的是半个 AgentRole。
    """
    id: int
    name: str
    kind: str = ""
    provider: str = ""
    endpoint: str = ""
    model: str = ""
    description: str = ""
    system_prompt: str = ""
    tts_voice: str = ""
    is_default: bool = False
    priority: int = 10
    enabled: bool = True


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


class HomeMembershipOut(ORMBase):
    """首页会员卡片。

    字段与 APP 的 MembershipInfo 对齐；注意 benefits 这里是字符串数组
    （APP 直接 join 展示），而 GET /api/membership 的 benefits 是字典
    （给管理端和调试看明细），两边刻意不同，见 routers/home.py 的注释。
    """

    is_vip: bool = False
    plan: str = ""
    expires_at: datetime | None = None
    days_left: int = 0
    benefits: list[str] = Field(default_factory=list)


class HomeSummaryOut(ORMBase):
    """GET /api/home/summary：一次拉齐首页要用的三块信息。"""

    devices: list[HomeDeviceOut] = Field(default_factory=list)
    medicine_today: MedicineTodayOut = Field(default_factory=MedicineTodayOut)
    membership: HomeMembershipOut = Field(default_factory=HomeMembershipOut)


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


# ============================================================ 设备陪聊配额
# 放在设备同步之前定义，供 DeviceSyncOut 引用
class DeviceQuotaOut(ORMBase):
    """设备当日陪聊配额快照。"""
    date: str
    is_vip: bool = False
    used_seconds: int = 0
    # 不限时为 None；设备应优先判断 unlimited
    limit_seconds: int | None = None
    unlimited: bool = False
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


# ============================================================ 会员（人工核销）
class MembershipOut(ORMBase):
    """会员状态 + 当日陪聊配额。

    benefits 直接给中文短句数组：APP 的 MembershipInfo.benefits 声明为 string[]，
    页面上是 ``(membership.benefits || []).join(' · ')`` 直接拼字符串展示，
    早先这里给的是字典，对字典调 join 会运行时报错。
    原来字典里的明细并没有丢，挪到 benefits_detail 供管理端/排查使用。
    """
    is_vip: bool
    plan: str = ""
    started_at: datetime | None = None
    expires_at: datetime | None = None
    days_left: int = 0
    source: str = ""
    status: str = ""
    # --- 权益（展示用，中文短句，APP 直接 join） ---
    benefits: list[str] = Field(default_factory=list)
    # --- 权益明细（结构化，给管理端与排查用） ---
    benefits_detail: dict[str, Any] = Field(default_factory=dict)
    # --- 当日陪聊额度 ---
    quota: dict[str, Any] = Field(default_factory=dict)


class MembershipPlanOut(ORMBase):
    id: int
    code: str
    name: str
    description: str = ""
    days: int
    # 月数（由 days 折算）。APP 的 MembershipPlan 里就有 months 字段，
    # 套餐卡片直接渲染「{{ p.months }} 个月」，后端不给的话页面上会是个空白的「个月」。
    # 套餐是按天存的（30/90/365），这里按 30 天一个月折算，至少显示 1 个月。
    months: int = 0
    price_cents: int
    # 展示价（元，字符串，避免浮点误差）
    price_yuan: str = ""
    sort: int
    enabled: bool
    # APP 的 MembershipPlan.benefits 也是 string[]，页面上是 (p.benefits||[]).join(' / ')
    benefits: list[str] = Field(default_factory=list)


class OrderCreateIn(ORMBase):
    """APP 下单：code 由 APP 用安全随机生成（4 位 A-Z/0-9），冲突时服务端返回 409。"""
    code: str = Field(min_length=4, max_length=4)
    plan_code: str


class OrderOut(ORMBase):
    id: int
    code: str
    user_id: int
    plan_code: str
    plan_name: str = ""
    amount_cents: int
    amount_yuan: str = ""
    status: str
    created_at: datetime
    expires_at: datetime
    notified_at: datetime | None = None
    confirmed_at: datetime | None = None
    admin_id: int | None = None
    admin_note: str = ""
    # 管理端列表用
    username: str = ""
    nickname: str = ""


class OrderCreateOut(ORMBase):
    """下单成功返回：订单号 + 应付金额 + 收款信息，供 APP 展示给用户转账。"""
    id: int
    code: str
    plan_code: str
    plan_name: str = ""
    days: int = 0
    amount_cents: int
    amount_yuan: str
    status: str
    expires_at: datetime
    expires_in_sec: int = 0
    payment_info: dict[str, Any] = Field(default_factory=dict)
    tip: str = ""


class PaymentInfoOut(ORMBase):
    """管理端配置的收款账号 / 二维码。"""
    channel: str = ""
    payee_name: str = ""
    account: str = ""
    qr_url: str = ""
    note: str = ""


class AmountRangeIn(ORMBase):
    """金额区间（整数分）。"""
    min_cents: int = 1000
    max_cents: int = 9999
    enabled: bool = True


class QuotaConfigIn(ORMBase):
    """陪聊配额。"""
    free_daily_seconds: int = 3600
    idle_timeout_seconds: int = 90
    vip_unlimited: bool = True


class MembershipConfigOut(ORMBase):
    """管理端「会员配置」聚合视图。"""
    amount_range: AmountRangeIn
    quota: QuotaConfigIn
    payment: PaymentInfoOut


class MembershipConfigPatch(ORMBase):
    """管理端局部更新：三块都可选，只传要改的。"""
    amount_range: AmountRangeIn | None = None
    quota: QuotaConfigIn | None = None
    payment: PaymentInfoOut | None = None


class GrantIn(ORMBase):
    """后台直接给用户开通会员。"""
    user_id: int
    days: int = Field(default=30, ge=1, le=3650)
    note: str = ""
    plan: str = "admin"


class RejectIn(ORMBase):
    reason: str = ""


class ConfirmResultOut(ORMBase):
    order: OrderOut
    membership: MembershipOut
    # 幂等标志：False 表示本次为重复核销，未再次加时长
    activated: bool


class ChatUsageIn(ORMBase):
    """设备上报一次会话时长。

    is_reminder=True 表示服药/提醒类交互，不计入配额。
    """
    date: str = ""
    seconds: int = Field(default=0, ge=0)
    is_reminder: bool = False


class ChatUsageOut(ORMBase):
    date: str
    used_seconds: int = 0
    session_count: int = 0
    reminder_seconds: int = 0
    limit_seconds: int | None = None
    unlimited: bool = False
    remaining_seconds: int | None = None
