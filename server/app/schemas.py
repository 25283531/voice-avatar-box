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


class UserLogin(ORMBase):
    username: str
    password: str


class TokenOut(ORMBase):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    username: str
    is_admin: bool = False


class UserOut(ORMBase):
    id: int
    username: str
    nickname: str
    phone: str
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


# ============================================================ AI 供应商
class ProviderIn(ORMBase):
    name: str
    kind: str = Field(pattern="^(asr|llm|tts|vad)$")
    provider: str
    endpoint: str = ""
    api_key: str = ""
    model: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    priority: int = 10
    enabled: bool = True


class ProviderOut(ProviderIn):
    id: int
    created_at: datetime


class ProviderSafeOut(ORMBase):
    """下发给设备/APP 的脱敏配置，不含 api_key。"""
    id: int
    name: str
    kind: str
    provider: str
    endpoint: str
    model: str
    priority: int
    enabled: bool


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


# ============================================================ 设备同步
class DeviceSyncOut(ORMBase):
    """设备心跳拉取的完整配置快照。"""
    server_time: datetime
    device: DeviceOut
    avatar: AvatarOut | None
    reminders: list[ReminderOut]
    upcoming: list[EventOut]
    providers: list[ProviderSafeOut]
    ws_url: str
    ota_url: str
    sync_interval_sec: int = 300
