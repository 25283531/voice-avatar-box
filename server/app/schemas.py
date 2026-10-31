"""Pydantic 进出参模型（Pydantic v2）。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from .config import TZ
from .model_catalog import API_STYLES, KIND_LIST, LOCAL_SERVICE_DEFAULTS
from .schedule import parse_hhmm


# ============================================================ 时间口径（单一真源）
# 本项目**不是**"所有时间戳都是 UTC"，而是**两种口径并存**，全部集中在这里说明：
#
#   A. UTC（``models.utcnow()`` 写入）：created_at / updated_at / last_seen_at /
#      bound_at / expires_at / ack_at / ts / replied_at / ...
#   B. 北京墙上时间（naive，不带偏移）：reminder_events 的 due_at / fired_at /
#      confirmed_at。这一组由 ``schedule.next_occurrence()`` 产生（aware +08:00），
#      写进 SQLite 时 tzinfo 被丢弃，读回来就是「北京钟点的字面量」。
#      之所以不改成 UTC：这两个值还与**固件**交换（设备离线自建实例、回传
#      确认），固件那边按 localtime_r 的北京钟点算，改了要对刷固件；而它们
#      的语义就是"老人看到的那个钟点"，存墙上值反而少一层换算。
#
# ⚠️ 为什么必须显式标出来（2026-10-21 时区统一）：
# 这两类值在 JSON 里**长得一模一样**（``"2026-10-22T08:00:00"``，都没有偏移），
# 于是手机 App / 管理后台的 ``new Date(s)`` 只能按**浏览器本地时区**猜 ——
# 猜对了是运气，猜错了整体偏 8 小时（且只在非北京时区的设备上复现）。
# 给它们各打上正确的偏移（A → ``Z``/``+00:00``，B → ``+08:00``）之后，
# ``new Date(s)`` 就完全确定，前端不需要知道任何业务约定。
LOCAL_WALL_FIELDS = frozenset({"due_at", "fired_at", "confirmed_at"})


class ORMBase(BaseModel):
    """所有出入参的基类：``from_attributes`` + **自动补时区标记**。

    序列化时给每个 datetime 补上偏移，规则见 :data:`LOCAL_WALL_FIELDS`：

    * 已在白名单里（北京墙上）→ 补 ``config.TZ``（Asia/Shanghai，+08:00）
    * 其余（UTC）→ 补 ``UTC``（输出成 ``...Z``）
    * 本来就是 aware 的（如 ``next_fire_at``、``server_time``）→ **原样不动**

    为什么放在基类而不是逐个 schema 写 ``field_serializer``：32 个 datetime
    字段散在 15 个 schema 里，逐个写必然漏 —— 而漏的那个不会报错，只会让
    某个页面上的时间差 8 小时。白名单集中一处，漏了也只需改一行。

    为什么入参不受影响：``model_dump()`` 主要被 router 用来 ``Model(**dump)``
    构造 ORM 行。带上 tzinfo 后 SQLAlchemy 写入 SQLite 取的仍是同一个墙上
    字面量（``+08:00`` 的北京墙上 = 原来的 naive 值），**值不变**，只是多了
    一个本来就成立的标注。
    """

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("*", when_used="always")
    def _attach_timezone(self, value: Any, info: Any) -> Any:
        """通配序列化器：给每个 datetime 补上正确的偏移（规则见上）。

        ⚠️ 必须用 ``field_serializer("*")`` 而不是 ``model_serializer(wrap)``：
        后者拿到的已经是**JSON 化之后的字符串**（``"2026-10-22T08:00:00"``），
        ``isinstance(value, datetime)`` 恒为 False ⇒ 整段逻辑静默不执行，
        而输出看起来"正常"（只是少了偏移），最容易被当成"改好了"。

        ⚠️ 白名单里的字段**优先读同一行上的 ``tz``**（2026-10-21 跨时区改造）。
        以前一律补 ``config.TZ``，那在"所有规则都在北京"的年代成立；现在一条
        纽约规则的 ``due_at`` 是纽约钟点，补 +08:00 就是**错 12 小时的标注**，
        而错标注比没有标注更坏 —— 前端会拿它当权威去换算。

        ⚠️ 取 ``tz`` 只能走 ``self``（实例属性）。``info`` 是
        ``SerializationInfo``，**没有** ``data`` 字段 —— 那是校验期的
        ``ValidationInfo`` 才有的。写成 ``info.data`` 会在**序列化时才炸**
        （``AttributeError``），而校验、单元测试里直接构造对象都不会触发，
        表现是"某个接口一返回 500"。
        """
        if isinstance(value, datetime) and value.tzinfo is None:
            if info.field_name not in LOCAL_WALL_FIELDS:
                return value.replace(tzinfo=timezone.utc)
            return value.replace(tzinfo=_zone_of(getattr(self, "tz", "")))
        return value


def _zone_of(name: Any):
    """``tz`` 字符串 → ZoneInfo；非法/空串一律退回 ``config.TZ``。"""
    from .geo import valid_timezone

    valid = valid_timezone(name)
    return ZoneInfo(valid) if valid else TZ


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
    # 硬件型号（"ESP32-S3" 等）。**带默认值**：老固件不上报时为空串，
    # 老客户端（不读该键）无感；UI 兜底「未知」。
    hardware_model: str = ""
    # 绑定时间（当前归属的起始时刻）。**带默认值 None**：存量设备为 NULL，
    # UI 显示「—」；重新绑定会更新（与 created_at 语义区分，见 Device.bound_at）。
    bound_at: datetime | None = None
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


# ================================================ 已配网设备「改 WiFi」
# 契约唯一真源：build-out/_wifi-spec.md §一。三端（服务端 / 固件 / APP）共用
# 同一套字段名，改字段必须同时改那份文档并通知另外两端。
class DeviceWifiIn(ORMBase):
    """家属提交一次改 WiFi 请求（``POST /api/devices/{pk}/wifi``）。"""

    ssid: str
    # 空密码 = 开放网络（无加密 AP）。**不是**「没填」，所以不做 min_length：
    # 老人家里确实存在不加密的路由器（或把密码当成「不用填」的旧设备），
    # 拒绝空密码等于让这类设备永远改不了网。
    password: str = ""

    @field_validator("ssid")
    @classmethod
    def _check_ssid(cls, v: str) -> str:
        """SSID 非空且 ≤32 **字节**。

        ⚠️ 上限按**字节**不是按字符：802.11 的 SSID 字段是 32 字节，
        中文 SSID 一个汉字占 3 字节（UTF-8），"我家WiFi" 是 10 字节但 4 个字符。
        按字符判会放过 11 个汉字（33 字节）—— 固件那边写 NVS 时被截断，
        设备去连一个**前缀**，表现是「密码明明对，就是连不上」。
        """
        v = v.strip()
        if not v:
            raise ValueError("SSID 不能为空")
        if len(v.encode("utf-8")) > 32:
            raise ValueError("SSID 不能超过 32 字节（一个汉字算 3 字节）")
        return v

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        """空 = 开放网络；非空则 8..63 字符（WPA2-PSK 的合法长度区间）。

        ⚠️ 这里**不能 strip**：WiFi 密码允许首尾空格（有些路由器真的这么设），
        strip 掉就会让「密码正确却连不上」变成一个无法解释的故障。
        长度按**字符**判（不是字节）：WPA2 的口令是 8..63 个 ASCII 字符，
        而本项目允许中文密码（固件侧按 UTF-8 字节写 NVS，不受这个区间约束），
        取字符数是对两端都安全的下界。
        """
        if not v:
            return v
        if not 8 <= len(v) <= 63:
            raise ValueError("密码长度应为 8~63 个字符（开放网络可留空）")
        return v


class DeviceWifiOut(ORMBase):
    """家属端读当前改 WiFi 状态（``GET /api/devices/{pk}/wifi``）。

    同一个模型也用作 ``POST /api/devices/{pk}/wifi`` 的响应 —— 契约 §一 里
    POST 的响应带 ``ok: true``，而 GET 的响应不带；多出来的 ``ok`` 对 GET 的
    调用方是无害的（老客户端只读自己认识的键），所以不为此再定义一个模型。
    """

    #: 提交是否被受理。只有 POST 会看它；GET 恒为 True（读到就是成功）。
    ok: bool = True
    #: ``''``/pending/delivered/applied/rolled_back/failed，取值集合见
    #: models.Device.wifi_state。``''`` = 从未改过。
    state: str = ""
    #: 0 = 无待下发（哨兵值，见 models.Device.wifi_req_id）。
    req_id: int = 0
    #: 目标 SSID。设备回报终态前**不清**（APP 靠它显示「正在切换到 xxx」）。
    ssid: str = ""
    #: 给 APP 直接展示的一句话。**由服务端按 state 派生**，不是设备原文 ——
    #: 设备回报的 ``reason`` 只进日志（见 device_api.report_wifi_result），
    #: 因为它可能是固件内部错误码/英文，直接显示给老人家属毫无意义。
    message: str = ""
    #: 最后一次状态变更时刻（取 wifi_req_at 与 wifi_result_at 的较晚者）。
    updated_at: datetime | None = None


class DeviceWifiResultIn(ORMBase):
    """设备回报改 WiFi 结果（``POST /api/device/wifi-result``，device token）。"""

    req_id: int
    #: True = 试连成功已生效；False = 试连失败已回滚到原配置。
    ok: bool
    #: 设备实际连上的 SSID（仅用于日志对账；服务端**不以它为准**改状态）。
    ssid: str = ""
    #: 失败原因（固件自由文本）。只进日志，不进库、不下发给 APP。
    reason: str = ""


class DeviceSyncIn(ORMBase):
    """``POST /api/device/sync`` 的**可选**请求体。

    ⚠️ 这个类存在的唯一理由是 ``wifi_result``：固件把「改 WiFi 终态」塞在心跳
    请求体里一起发（断网时进 NVS 队列、联网后随下一轮心跳补发，见
    ``firmware/components/elder/src/sync.c`` 拼 ``body`` 的那一段），而不是单独调
    ``POST /api/device/wifi-result``。

    ⚠️ **2026-10-31 之前这里是个真缺陷**：固件一直在发，而 ``/sync`` 从头到尾
    没读过 request body（它只用 ``Request`` 拿 IP 判时区）。于是 200 返回、固件
    认为送达并**删掉本地结果** ⇒ 改 WiFi 的终态永久丢失：APP 永远停在「正在
    切换」，明文密码也一直留在库里。判据是 ``tools/smoke_wifi_change.py`` 的 C11
    （走心跳带上结果的那条路径），**不是**只看 ``/wifi-result`` 的 C5/C9 ——
    那两条一直是绿的，正是它们让这个缺陷活了整整一天。
    """

    wifi_result: DeviceWifiResultIn | None = None


class DeviceWifiChangeOut(ORMBase):
    """随 ``POST /api/device/sync`` 下发给固件的改 WiFi 指令。
    ⚠️ 只在 ``wifi_state == "pending"`` 时出现，下发后服务端立刻把 state 置为
    ``delivered`` —— 所以**同一条指令不会下两次**（重复下发 = 设备反复试连、
    反复断网重连，老人家里会看到 WiFi 图标闪一整天）。

    ⚠️ 老固件收到这个未知字段必须安全忽略（JSON 解析器跳过多余的 key），
    这是跨端不变量之一，见 _wifi-spec.md §四。
    """

    req_id: int
    ssid: str
    #: 明文下发（走 HTTPS/局域网）。开放网络的密码就是空串。
    password: str = ""


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


# ================================================================ 声纹说话人（F3）
class VoiceSpeakerIn(ORMBase):
    """登记一位说话人。

    ``speaker_key`` 必须与**外部声纹服务**上已注册的 ID 一致 —— 向量在那边，
    这边只做"key → 是谁"的映射。填一个服务上不存在的 key 不会报错，
    只会永远认不出人（服务端返回的 id 不在我们问过的列表里）。
    """

    speaker_key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=255)
    enabled: bool = True

    @field_validator("speaker_key", "name")
    @classmethod
    def _strip(cls, v: str) -> str:
        # 首尾空白会让 "张三" 和 "张三 " 变成两个说话人，而声纹服务那头
        # 只认我们发过去的 speaker_ids 字符串 —— 不一致时表现为"认不出"。
        v = v.strip()
        if not v:
            raise ValueError("不能为空白")
        return v

    @field_validator("speaker_key")
    @classmethod
    def _no_comma(cls, v: str) -> str:
        # speaker_ids 是逗号分隔发给服务端的：key 里带逗号会把一个说话人
        # 裂成两个，服务端返回的 id 就永远对不上我们的登记。
        if "," in v:
            raise ValueError("speaker_key 不能包含逗号")
        return v


class VoiceSpeakerOut(VoiceSpeakerIn):
    id: int
    owner_id: int
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
        # 空 kind 先放行到 model_validator：本地自建服务风格
        # （local_asr / local_embed）允许「只选风格就保存」，kind 由保存时的
        # 归一化补齐（见 routers/providers._normalize_local_service）。
        # 在**字段校验器**里做不了这个例外：api_style 声明在 kind 之后，
        # 字段校验器拿不到还没校验的后续字段（info.data 里没有它）。
        if not v:
            return ""
        if v not in KIND_LIST:
            raise ValueError(f"kind 只能是 {'/'.join(KIND_LIST)} 之一")
        return v

    @model_validator(mode="after")
    def _kind_required_unless_local_service(self) -> "ProviderIn":
        """空 kind 只对本地自建风格放行，其余一律拒绝（角色走 /api/roles）。

        为什么例外只有这两个风格：需求是「下拉选中本地 ASR / 本地向量化时，
        其它什么都不填，点保存就能用」。而空 kind 对其它任何风格都是残缺
        数据 —— 那种行存得进、resolve 时却落到别的用途上，属于
        「接口能存进去但查不出来」的玄学故障（见 model_catalog 模块说明）。

        ⚠️ 判据用 ``api_style`` 而不是 ``provider``：风格才是「走哪条协议」，
        手填 local_asr 的人未必会同时把 provider 手填成 funasr_local。
        """
        if not (self.kind or "").strip():
            style = (self.api_style or "").strip()
            if style not in LOCAL_SERVICE_DEFAULTS:
                raise ValueError(
                    "kind 必填。创建角色请用 POST /api/roles；"
                    "本地 ASR / 本地向量化可只选风格保存")
        return self

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
    # embed 用：待向量化的文本。**允许 list（批量）**：embed() 本就支持，
    # 而本地 FastEmbed 的批量报文（texts[]）与单条（input）是两条不同路径
    # （见 llm_gateway.embed 的 local_embed 分支）—— 不让它进来，
    # 批量那半条路就永远没有请求能覆盖。
    text: str | list[str] = ""
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


class PresetCreateIn(ORMBase):
    """新建平台人格预设（2026-10-19 用户报「只有一个人格，无法新增」）。

    与 :class:`PresetPatchIn` 同源同构：``extra="forbid"`` 是白名单式锁的
    前提（Pydantic v2 默认 ``extra="ignore"`` 会把未知字段**静默丢掉**，
    那等于没有锁）；归属类字段（``record_type`` / ``owner_id`` /
    ``parent_id`` / ``kind``）一律不收，由端点按常量写死 ——
    创建入口不该有能力决定"这行归谁、是不是别人的角色"。

    与 PATCH 的差别只在 ``name`` **必填**（PATCH 里可选，因为"改名"不是
    新建的语义），其余全部缺省走 catalog 出厂值，与 ``_seed()`` 建的
    默认预设逐字段一致：运营可以"先建一个和小智一样的，再慢慢调"，
    不必从零填 10 个性格档位 —— 从零填的结果通常是半途而废。

    ⚠️ 不含 ``guardrails``：硬红线是代码常量，由 ``merge_stored(None)``
    在创建时按当前 ``GUARDRAIL_VERSION`` 注入，不接受外部传入。
    """

    name: str = Field(min_length=1, max_length=64)
    description: str | None = None
    # 人格提示词（软性）。红线由代码常量注入，不在这里。
    system_prompt: str | None = None
    tts_voice: str | None = None
    # 整体覆写；缺省 = 出厂 10 维。写入前逐项 is_valid 校验，非法值 400 且不落库。
    traits: dict[str, Any] | None = None

    # 见类注释：extra="forbid" 是「白名单式 schema 锁」成立的前提
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class PresetResetIn(ORMBase):
    """恢复出厂默认。``fields`` 缺省 = 全部可重置字段。

    ⚠️ 可重置的**只有 3 个**：``system_prompt`` / ``traits`` / ``tts_voice``。
    不含 ``name``（运营的品牌标识，"恢复出厂"不该把它改回"小智（平台预设）"）、
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


class MedicineTodayItemOut(ORMBase):
    """当天**某一个服药时间点**的明细（家属端要能分清"是哪种药没吃"）。

    为什么必须逐条返回、而不是只给 ``pending_times`` 的 "HH:MM"：
    家属看到"还有 1 次没吃"时，下一步一定是问"哪一次、什么药"。只有钟点
    没有药品名时，这个问题只能靠人去对规则列表，而老人一天往往有 3~5 条
    规则 —— 对不上就等于没告诉。

    ``state`` / ``confirm_method`` 直接透传 :class:`EventState` /
    :class:`ConfirmMethod` 的枚举值，同时给一份 ``*_text``：前端做颜色/图标
    要的是枚举值，直接展示要的是中文，两边都不该各自硬编码一份文案。

    ⚠️ ``tz`` 不是给前端看的，是 :class:`ORMBase` 的序列化器要读它来给
    ``due_at`` / ``confirmed_at`` 补**正确**的偏移（见 ORMBase 的注释）。
    缺了它这两个字段会一律被标成 ``config.TZ`` —— 美西家庭的 08:00 会被
    标成北京时间 08:00，错 12 小时且不报错。
    """

    reminder_id: int
    #: 规则标题（药品名 / 日程名）。规则被删后仍保留实例，标题会是空串。
    title: str = ""
    due_at: datetime
    #: 这一实例的钟点基准（IANA 名），见类注释
    tz: str = ""
    #: EventState：0 待触发 / 1 已响铃 / 2 已确认 / 3 超时未确认 / 4 已跳过
    state: int
    state_text: str = ""
    #: ConfirmMethod：0 无 / 1 语音 / 2 按键 / 3 手机端 / 4 自动 / 5 语音+按键
    confirm_method: int = 0
    confirm_method_text: str = ""
    confirmed_at: datetime | None = None
    #: 这条属于哪台设备（2026-10-31 加）。
    #:
    #: 为什么必须带：``_medicine_today`` 是**多台设备合并成一份**统计的，而
    #: 一个家庭可能既有客厅那台也有卧室那台。合并之后家属看到"08:00 降压药
    #: 待确认"，根本不知道要去哪台设备前提醒老人 —— 更糟的是点了「代确认」
    #: 也说不清确认的是哪一台。
    #: ``device_name`` 是快照（设备改名后历史条目仍显示当时的名字），
    #: 与 ``HomeDeviceOut.name`` 同源。
    device_id: int = 0
    device_name: str = ""


class MedicineTodayOut(ORMBase):
    """当天服药概览（服务端时区 TZ 的自然日）。"""

    # 待服药次数：当天排了但还没确认的（含已过点未确认的漏服）
    pending_count: int = 0
    # 待服药的时间点，"HH:MM"，升序
    pending_times: list[str] = Field(default_factory=list)
    confirmed_count: int = 0
    total_count: int = 0
    #: 当天逐条明细（**同时包含待确认与已确认**，按 due_at 升序）。
    #: 与上面三个计数字段并存而不是替代它们：老前端只读计数，删字段会让
    #: 它们整块变空；新前端读 items 拿药品名与确认方式。
    items: list[MedicineTodayItemOut] = Field(default_factory=list)


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
    #: 这条规则的钟点基准（IANA 名）。空串 = 历史数据 = ``config.TZ``，
    #: 也 = 「由服务端按设备 IP 判定」（见 routers/reminders._reminder_tz）。
    #:
    #: 字段名刻意用 ``tz`` 而不是 ``timezone``：``model_dump()`` 会被 router
    #: 直接展开成 ``models.Reminder(**dump)``，字段名必须与**列名**逐字相同，
    #: 否则是 ``TypeError: 'timezone' is an invalid keyword argument``。
    tz: str = ""

    @field_validator("times_of_day")
    @classmethod
    def _check_times(cls, v: list[str]) -> list[str]:
        for t in v:
            if parse_hhmm(t) is None:
                raise ValueError(f"时间格式非法: {t}，应为 HH:MM")
        return v

    @field_validator("tz")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        """非法时区**直接拒绝**，不静默丢弃。

        静默丢弃看起来更"宽容"，但后果是：客户端以为设成了纽约时间，
        实际落库的是北京时间 —— 老人早上 8 点的药变成晚上 8 点响，
        而 API 返回 201、界面上看不出任何异常。宁可 422 让它当场暴露。
        """
        from .geo import valid_timezone

        if (v or "").strip() and not valid_timezone(v):
            raise ValueError(f"时区名非法: {v}，应为 IANA 名（如 Asia/Shanghai）")
        return v.strip()

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
    # ---- 软删除（2026-10-30）----
    # ⚠️ 这四个字段**只能加在 ReminderOut，绝不能加进 ReminderIn**：
    # ``ReminderIn.model_dump()`` 会被 router 直接展开成
    # ``models.Reminder(**dump)``，而 ``is_deleted`` 根本不是数据库列 ——
    # 落进 **dump 就会在创建提醒时抛 ``TypeError: 'is_deleted' is an invalid
    # keyword argument``（每个新建提醒的请求都 500）。
    # ``deleted_*`` 是真实列，技术上可以进 ReminderIn，但那样客户端就能
    # 通过 POST/PATCH 自己伪造"删除"—— 删除只该走 DELETE 接口。
    deleted_at: datetime | None = None
    deleted_by: int | None = None
    deleted_via: str = ""
    #: 已删除标记（前端加「已删除」标签用）。由 router 从 deleted_at 推导，
    #: **不是列** —— 派生值存列就多了一份会漂移的真源。
    is_deleted: bool = False


class ReminderAuditOut(ORMBase):
    """一条提醒操作流水（GET /api/reminders/audits 的元素）。

    为什么继承 ``ORMBase`` 而不是裸 ``BaseModel``
    --------------------------------------------
    任务书写的是 ``BaseModel``，但裸 BaseModel **不会**给 naive datetime
    补偏移：SQLite 读回来的 ``created_at`` 是 naive UTC，直接序列化会输出
    ``2026-10-30T12:00:00``（无偏移），前端当本地时间显示就整体差 8 小时。
    本仓库已有唯一约定：``ORMBase`` 的 ``field_serializer("*")`` 负责补偏移
    （``due_at`` 等墙上时间走白名单、其余按 UTC）—— 照抄 EventOut 的做法，
    就是让 created_at 走「非白名单 = 补 UTC」那条分支。
    """

    id: int
    reminder_id: int
    device_id: int
    action: str
    #: 中文标签：新增/修改/删除/恢复/暂停/确认（映射表在 app.reminder_audit）
    action_label: str = ""
    actor_type: str = ""
    actor_name: str = ""
    via: str = ""
    #: 中文标签：网页/手机 APP/语音/管理后台/设备
    via_label: str = ""
    title: str = ""
    summary: str = ""
    #: 带时区偏移（见类注释）
    created_at: datetime
    #: 该条流水对应的提醒**当前**是否处于已删除状态（前端据此决定显不显示
    #: 「恢复」按钮）。注意它是"现在"的状态，不是"当时"的状态 —— 一条
    #: delete 流水在恢复之后会变成 False，这是有意的：按钮要能反映现状。
    can_restore: bool = False


class EventOut(ORMBase):
    id: int
    reminder_id: int
    device_id: int
    #: 这一实例的钟点基准（IANA 名）。序列化器通过 ``self.tz`` 读它，
    #: 所以它**必须在模型上有默认值** —— 老版本客户端/缺字段时不至于 AttributeError。
    tz: str = ""
    due_at: datetime
    fired_at: datetime | None
    confirmed_at: datetime | None
    state: int
    confirm_method: int
    fired_count: int
    escalated: bool
    note: str
    #: 该实例所属提醒的标题（药品名 / 日程名）。默认空串：老调用方不认这个键
    #: 也不受影响；``routers.devices.device_events`` 会 join Reminder 填上。
    reminder_title: str = ""


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
class RemoteConfirmOut(BaseModel):
    """一条「子女在手机端代确认」的记录，随心跳下发给设备。

    为什么需要它：家属在手机端点了「代确认」，服务端只写库（``state`` /
    ``confirm_method`` / ``confirmed_at``），而设备那边什么都没收到 ——
    于是灯一直闪、屏幕还停在"该吃药了"，直到老人自己去按一次。设备侧拿到
    这个数组后把对应实例标成本地 DONE，症状才消失。

    ⚠️ **刻意继承 ``BaseModel`` 而不是 :class:`ORMBase`**：ORMBase 会给
    naive datetime 补时区标记，而这里要的根本不是字符串（见下面的序列化器）。

    ⚠️⚠️ **``due_at`` 在 JSON 里是 unix 秒（整数），不是 ISO 字符串。**
    固件侧 ``firmware/components/elder/src/sync.c`` 的 ``apply_remote_confirms``
    用 ``cJSON_IsNumber(jdue)`` 判定这条记录是否可用，非数字直接计入 bad 并
    丢弃 —— 发 ISO 字符串的话整个功能**一条都落不下去，而且服务端 200、
    固件只打一条 WARN**，是最难查的那种静默失效。固件同时用 ``fired_due_at``
    （本地 epoch）与它做 ±60 秒比对，所以这里必须是**绝对时刻**的 epoch，
    不是"规则时区的墙上钟点"。
    因此：入参传 ``schedule.event_due_utc(ev)``（aware UTC），由下面的
    ``field_serializer`` 转成 ``int(dt.timestamp())``。
    """

    reminder_id: int
    due_at: datetime
    #: ConfirmMethod.APP（3）。透传而不是写死，将来出现"远程跳过"也能复用。
    confirm_method: int

    @field_serializer("due_at")
    def _due_at_epoch(self, v: datetime) -> int:
        """datetime → unix 秒。契约见类注释（固件只认数字）。"""
        return int(v.timestamp())


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
    # 设备该用哪个时区来解释「几点响」（2026-10-21 起下发，见 DEPLOY.md §1.15）
    #
    # 为什么必须由服务端下发：固件原本把 TZ 写死 `CST-8`，设备在海外时它
    # 用北京钟点解释 `times_of_day`（"早上八点吃药"），与规则自己的时区
    # 差好几个小时 —— 而规则时区是服务端按**设备 IP** 判定的，固件判不出来
    # （它算不出自己的 IP 归属地），所以只能服务端告诉它。
    #
    # 两个字段分工：
    #   * ``timezone``：IANA 名（如 ``America/New_York``），只给日志/排查/UI 看；
    #   * ``tz_offset_minutes``：真正给固件用的东西。newlib **没有 tzdata**，
    #     装不下 IANA 名，只能按 POSIX TZ 定一个固定偏移。
    #
    # ⚠️ ``tz_offset_minutes`` 缺省为 ``None`` 而不是 0 —— 0 会被固件当成
    # "UTC"，把设备整体推到格林尼治时间；None 让固件明确"服务端没说"，
    # 继续用它 NVS 里的旧值（默认 +480，与改造前逐字一致）。
    timezone: str = ""
    tz_offset_minutes: int | None = None
    # 改 WiFi 指令（2026-10-30）：仅当 wifi_state == "pending" 时非 None，
    # 下发后服务端立刻置 delivered ⇒ 同一条指令不会下第二次。
    #
    # ⚠️ **必须默认 None**：老固件不认识这个键，None 序列化成 ``null``
    # （而不是省略），固件侧解析到 null 按「无待下发」处理即可；
    # 若把它做成必填，老固件之外的所有既有调用方都会在反序列化时炸。
    # 与 quota / tts_voice 同例：新增字段一律带默认值。
    wifi_change: DeviceWifiChangeOut | None = None
    #: 近 24 小时内「子女在手机端代确认」的实例（2026-10-31）。
    #: **必须是带默认空列表的顶层数组**：老固件按 key 取值，不认识就忽略，
    #: 不需要动协议版本；写成必填会让所有既有调用方反序列化失败。
    remote_confirms: list[RemoteConfirmOut] = Field(default_factory=list)


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
    # 按用途分组的**调用次数**（by_usage 是 token 数，两者不是一回事）。
    #
    # 为什么必须单独一个字段：ASR / TTS 按次或按秒计费，**tokens 恒为 0**，
    # 只有 by_usage 的话这两类在界面上永远显示 0 —— 家属会以为「语音识别
    # 一次都没用过」，而实际上每轮对话都在用。次数才是这两类唯一有意义的口径。
    by_usage_calls: dict[str, int] = Field(default_factory=dict)
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
    # 用户端展示的「模型类别」（「正式对话模型」「语音识别」…）。
    #
    # 从 ``models.ModelUsage.kind_label``（property）取，映射表在
    # ``model_catalog.USAGE_KIND_LABELS``。这里**只给类别不给模型名**是有意的：
    # 家属看得懂"正式对话模型"，看不懂 DeepSeek-V4-Flash；而 model 字段
    # 仍然返回给管理端对账用（用户端前端不展示它即可）。
    kind_label: str = ""
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
