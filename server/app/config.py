"""运行时配置（全部可用环境变量覆盖）。"""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- 基础
APP_NAME = "老人陪伴提醒助手 · 业务后端"
APP_VERSION = "0.1.0"

# 服务监听
HOST = os.getenv("ELDER_HOST", "0.0.0.0")
PORT = int(os.getenv("ELDER_PORT", "8080"))

# ---------------------------------------------------------------- 时区
# 服药提醒是时间强相关业务，服务端、设备端、展示层必须统一到同一个时区，
# 否则「早上 8 点吃药」会整体漂移 8 小时。
TZ = ZoneInfo(os.getenv("ELDER_TZ", "Asia/Shanghai"))

# ---------------------------------------------------------------- 数据库
# 默认**内置 SQLite**：`docker compose up -d` 开箱即用，不需要任何外部数据库，
# 也不再默认拉一个 MySQL 容器。想用 MySQL 就显式设 ELDER_DB（或叠加
# docker-compose.mysql.yml），见 docs/DEPLOY.md。
#
# 默认文件位置的选择依据：
#   1. 镜像里 WORKDIR 是 /app，直接放 ./elder.db 会落在容器可写层，
#      容器一重建数据就没了 —— 所以优先放持久化目录；
#   2. docker-compose.yml 把命名卷 elder-data 挂到 /data，
#      那里才是真正的持久化落点，故容器内的首选路径是 /data/elder.db；
#   3. 但同一个默认值也要能在本机（Windows / macOS 裸跑）直接跑起来，
#      这些机器上不存在 /data，此时退回当前工作目录下的 ./elder.db。
# 因此这里做一次「存在且可写」的判断，而不是写死某一个路径。
# 持久化目录本身可用 ELDER_DATA_DIR 覆盖（例如挂到 NAS / 宿主机目录时）。
def _default_sqlite_url() -> str:
    """选出默认 SQLite 库文件位置，返回 SQLAlchemy 连接串。"""
    data_dir = Path(os.getenv("ELDER_DATA_DIR", "/data"))
    if data_dir.is_dir() and os.access(data_dir, os.W_OK):
        # sqlite:////data/elder.db 的四条斜杠：前两条是 scheme 分隔，后两条才是绝对路径的 /
        return f"sqlite:///{data_dir.as_posix()}/elder.db"
    # 退回当前工作目录（uvicorn 启动时所在的目录），相对路径的 sqlite:///./elder.db
    return "sqlite:///./elder.db"


DATABASE_URL = os.getenv("ELDER_DB", _default_sqlite_url())
DB_ECHO = os.getenv("ELDER_DB_ECHO", "0") == "1"
DB_POOL_PRE_PING = True

# 启动时等待数据库可用的最长时间（秒）。
# Docker Compose 下 elder-db 虽然带 healthcheck，但在 Windows/WSL2 上 MySQL 8
# 首次初始化数据目录可能耗时 30~90s，且 depends_on 的健康判定与 mysqld 真正
# 开始接受查询之间存在窗口期——客户端会拿到
#   pymysql.err.OperationalError (2013, 'Lost connection to MySQL server during query')
# 因此启动阶段必须自带重试，而不是依赖编排层的时序。
DB_WAIT_SEC = float(os.getenv("ELDER_DB_WAIT_SEC", "180"))

# ---------------------------------------------------------------- 安全
JWT_SECRET = os.getenv("ELDER_JWT_SECRET", "please-change-this-secret-in-production")
JWT_ALG = "HS256"
USER_TOKEN_TTL = timedelta(days=int(os.getenv("ELDER_USER_TOKEN_DAYS", "30")))
DEVICE_TOKEN_TTL = timedelta(days=int(os.getenv("ELDER_DEVICE_TOKEN_DAYS", "365")))

# ---------------------------------------------------------------- 注册与管理员归属
# 见 docs/DEPLOY.md §「注册开关与初始管理员」。这里只解释**为什么这两个值必须是
# 环境变量**，以及各自的默认值是怎么选出来的。
#
# ⚠️ 背景：`POST /api/auth/register` 从来就**没有任何鉴权装饰器** —— 它是匿名
# 可达的。这个服务公开部署在生产域名上，所以「能不能注册」与「注册出来是不是
# 管理员」是两件必须由**配置**决定的事，不能靠前端有没有那个按钮来决定
# （前端不调用 ≠ 接口不存在，那是 security by obscurity）。
#
# 为什么两者都要放在 config 而不是各自散到路由里：router 里只有一处读点倒还好，
# 但「默认值是什么」这件事必须能被**一眼查到**，运维改错端口可以重试，把注册
# 开关理解反了是要出权限事故的。
ALLOW_PUBLIC_REGISTER = (
    os.getenv("ELDER_ALLOW_PUBLIC_REGISTER", "1").strip().lower()
    not in ("0", "false", "no", "off", "")
)
#
# 默认值为什么是**开启**（``"1"``）
#   1. 本产品的主要部署形态是「给家里老人私有化部署一套」，那里就一个人注册，
#      关掉等于每次部署都要先教一遍环境变量；
#   2. 关掉默认值会让**全部既有部署**在下一次升级后悄悄失去自建账号的能力
#       token 消耗另有闸门：非会员有每日时长配额（见 membership 的配额配置），
#       "能不能注册"不该被当成唯一的成本控制点。
#   ⚠️ **但**：部署在公网、且不愿承担他人消耗 AI token 的场景，
#   必须显式设 ``ELDER_ALLOW_PUBLIC_REGISTER=0``。届时路由返回 403 + 中文说明。
#
# 注：这里刻意接受多种"假值"（0/false/no/off/空串）。
# 同文件里 DB_ECHO 用的是 ``== "1"``，那种写法下把变量写成 ``false`` 会被当成
# **开启** —— 对一个"开关"类变量来说，把明显想关掉它的写法读成开启，
# 是最不该犯的一类误判。
#: 关闭注册时返回给调用方的原文。前端直接弹它，所以必须是**中文整句**，
#: 且要能让人一眼区分「策略关闭」与「接口坏了」（后者会是 404/500）。
REGISTER_CLOSED_DETAIL = "当前未开放自助注册，请联系管理员开通账号"

# 初始管理员（bootstrap）的用户名。
#
# ⚠️ 被替换掉的老规则是「第一个注册的人自动成为管理员」，代码形如::
#
#       is_admin = db.scalar(select(models.User).limit(1)) is None
#
#   在公开部署下这条规则等价于**「先到先得的管理员」**：任意一个先点开注册页的
#    **「先到先得的管理员」**：任意一个先点开注册页的陌生人就拿到了
#    改 AI 配置 / 核销会员订单 / 看全部用量的权限。这不是竞态 bug 那么简单，
#    是**规则本身就是错的** —— 管理员身份不能由"到达顺序"授予。
#
# 新规则：**只有用户名与本变量逐字相等的那个注册者**才是管理员。
#   * 未设置（默认空串）-> **任何人注册都不是管理员**。此时管理员由启动期
#     种子数据（``main._seed()``）或运维脚本创建。这是最安全的默认。
#   * 设置了 -> 匹配者获得管理员，且**引导完成后应当把这个变量去掉**
#     （见 DEPLOY.md 的迁移步骤）：留着它等于长期留一张"猜对用户名就给管理员"的票。
#
# 为什么用"用户名匹配"而不是" invites / 邀请码 / 审批"：
#   前者零额外状态、零新增表、零新增接口，对一个以私有化单机部署为主的产品，
#   多一张邀请码表就要多一条清理路径；而它解决的问题（防止陌生人成为管理员）
#   已经解决到"必须知道运维**特意配的那个名字**"这一层。
#   缺点要说清：**它不是一个有时限的窗口**，配了就一直有效 —— 所以文档里
#   把它写成"开箱步骤"而不是"长期配置"。
BOOTSTRAP_ADMIN_USERNAME = os.getenv("ELDER_BOOTSTRAP_ADMIN_USERNAME", "").strip()

# ---------------------------------------------------------------- 激活码
# 与固件 elder_activation.c 保持完全一致：
#   [0]     1 位字母（剔除易混淆的 I / O）
#   [1..11] 11 位随机数字
#   [12]    1 位 Luhn 校验位（由 [1..11] 计算）
ACT_CODE_LEN = 13
ACT_CODE_TTL_MINUTES = int(os.getenv("ELDER_ACT_TTL_MIN", "10"))
LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"

# ---------------------------------------------------------------- 自研语音链路
# ⚠️ 这里**不再是** xiaozhi-esp32-server。
#
# 历史沿革：早期为快速验证直接复用了小智的服务端容器，导致角色、音色、记忆、
# 大模型调用全跑在外部进程里，属于「直接依赖第三方服务」，与本项目「自成一体」
# 的目标相悖。现已改为**自研 WebSocket 服务端**（server/app/voice/），对外只保留
# 同一套公开的 JSON-over-WebSocket 协议（hello/listen/stt/tts/mcp/abort + Opus），
# 固件侧无需改动 —— 协议是公开的，谁都可以自己实现服务端，不绑定任何厂商。
#
# 设备配网后拿到的就是下面这两个地址，由本镜像自己提供。
VOICE_WS_PATH = os.getenv("ELDER_VOICE_WS_PATH", "/api/voice/v1/ws")
VOICE_OTA_PATH = os.getenv("ELDER_VOICE_OTA_PATH", "/api/voice/ota")

# 对外通告的地址：留空时由服务端按请求 Host 自动推导（ws/wss 与 http/https 同源）。
# 生产部署在 HTTPS 域名后面时要显式配成 wss://<域名>，否则浏览器/设备会拒绝混合内容。
VOICE_PUBLIC_BASE = os.getenv("ELDER_VOICE_PUBLIC_BASE", "").rstrip("/")

# ---------------------------------------------------------------- 语音链路（自研）
# 下行音频的采样率：握手里**声明 24000**，固件用自己声明的这个速率解码
# （audio_service.cc:375-390 有"按服务端声明速率解码 -> 重采样到板子输出"的
# 路径）。刻意不跟随设备上行的 16000：ASR 收 16k 就够，而 TTS 用 24k 出声
# 对老人更清楚，两端各按自己的速率走，不必为了迁就一方而牺牲另一方。
VOICE_OUTPUT_SAMPLE_RATE = int(os.getenv("ELDER_VOICE_SAMPLE_RATE", "24000"))
# 帧长（毫秒）。Opus 只支持 2.5/5/10/20/40/60，60 是固件 audio_service.cc
# 里写死的 OPUS_FRAME_DURATION_MS，改它等于换一套帧协商。
VOICE_FRAME_DURATION_MS = int(os.getenv("ELDER_VOICE_FRAME_DURATION_MS", "60"))
# ASR 语言。默认中文：本项目面向中文老人，且多数厂商不传会用默认（英文）。
VOICE_ASR_LANGUAGE = os.getenv("ELDER_VOICE_ASR_LANGUAGE", "zh")

# 服务端静音兜底阈值：连续静音超过它、且累计语音已达 VOICE_MIN_SPEECH_MS，
# 就自动 finalize（等价于设备端 VAD 断句）。
# 为什么需要它：① realtime 模式固件不做 VAD、等我们断句，而我们没有 AEC，
#    只能靠能量判停；② auto 模式下设备若漏发 listen stop（丢包/重启），
#    这一轮会永远挂在 listening 态，用户等不到回复。
# 调太大 = 老人说完要干等；调太小 = 会被句子间的停顿误切。1500ms 是
# 一句自然停顿的下界附近，实测 Sentence 之间极少有超过 1.2s 的停顿。
VOICE_SILENCE_MS = int(os.getenv("ELDER_VOICE_SILENCE_MS", "1500"))
# 触发兜底前至少要听到这么多语音，防止"一句都没说就静音触发"把环境噪声
# 当成一整句送去 ASR（结果是识别出一串乱码，老人莫名其妙）。
VOICE_MIN_SPEECH_MS = int(os.getenv("ELDER_VOICE_MIN_SPEECH_MS", "300"))

# 会话内保留的历史轮数上限。语音链路每轮都要塞人格 + 记忆召回，窗口比
# 纯文本更紧，且老人话题跳跃少、短会话即可；超过就丢最早的。
VOICE_HISTORY_TURNS = int(os.getenv("ELDER_VOICE_HISTORY_TURNS", "4"))

# ---------------------------------------------------------------- 提醒调度
# 服务端预生成提醒实例的提前量（小时）。设备离线时靠本地 RTC 兜底触发，
# 联网时以服务端生成的实例为准做对账。
SCHEDULE_AHEAD_HOURS = int(os.getenv("ELDER_SCHEDULE_AHEAD_HOURS", "36"))
SCHEDULER_INTERVAL_SEC = int(os.getenv("ELDER_SCHEDULER_INTERVAL", "60"))

# ---------------------------------------------------------------- 会员（人工核销）
# 订单号 / 收款金额的占用有效期（小时）：超时后订单置为 expired，号段与金额随之释放。
MEMBERSHIP_ORDER_TTL_HOURS = int(os.getenv("ELDER_ORDER_TTL_HOURS", "48"))
# 下单频率限制：同一用户在该窗口内最多创建这么多笔订单，避免刷号占满 4 位号段
MEMBERSHIP_ORDER_RATE_WINDOW_SEC = int(os.getenv("ELDER_ORDER_RATE_WINDOW", "60"))
MEMBERSHIP_ORDER_RATE_MAX = int(os.getenv("ELDER_ORDER_RATE_MAX", "3"))
# 金额区间合法边界（整数分）：至少 1 元，最多 999.99 元
AMOUNT_MIN_LIMIT_CENTS = 100
AMOUNT_MAX_LIMIT_CENTS = 99999

# ---------------------------------------------------------------- 其它
CORS_ORIGINS = os.getenv("ELDER_CORS", "*").split(",")
