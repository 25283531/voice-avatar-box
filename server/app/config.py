"""运行时配置（全部可用环境变量覆盖）。"""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


def _int_env(name: str, default: int) -> int:
    """读一个整数型环境变量。

    ⚠️ 解析不出整数时**回落到 default 而不是 0**，且负/零一律夹到 0：
    限速类变量写错的后果必须是"更严格"或"保持默认"，绝不能是"限速器消失"。
    （这段被调用方在「登录失败限速」一节里依赖，方向别改反。）
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else 0


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
#       token 消耗另有闸门：只有**未绑定设备**才受每日时长上限约束
#       （见 chat_quota 的授权判定），已授权设备一律无限量，
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
#    改 AI 配置 / 看全部用量的权限。这不是竞态 bug 那么简单，
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

# ---------------------------------------------------------------- 种子管理员口令
# ⚠️ 被换掉的老行为：空库首次启动时**无条件**用硬编码常量建管理员::
#
#       password_hash=hash_password("admin123")
#
#   本服务是公开部署的（生产地址 https://152.136.230.132，IP 直连；APP 把
#   BASE_URL 写死成它），
#   于是任何人用 admin/admin123 登录即成为**平台管理员** —— 能改 AI 配置
#   （含厂商 API Key 的读写口径）、查看全部用户用量与记忆。
#
# ⚠️⚠️ **改种子救不了存量库**：那条路径只在**空库**时执行，所以已经跑起来的
#   数据库（含正在运行的生产库）里那个 admin 的口令**目前就是 admin123**。
#   存量处置靠下面两个东西：``ELDER_RESET_ADMIN_PASSWORD``（一次性轮换）+
#   每次启动的体检告警（见 ``main._admin_password_audit``）。
#
# 取值规则（``main._seed()`` 是唯一读点）：
#   * **未设置**（默认）-> ``secrets.token_urlsafe(16)`` 随机生成，并在启动日志里
#     以 **WARNING** 级别打印一次，文案含「这是唯一的初始口令，请立即保存并登录后
#     修改」。为什么必须打日志而不是只写文档：运维的第一现场是 ``docker logs``，
#     随机口令不落日志等于**没人拿得到初始口令**，服务直接不可登。
#   * **设置了** -> 用它。典型用途是 CI：8 个冒烟脚本硬依赖 admin/admin123 登录，
#     CI runner 是一次性且不可公网访问的，那里用弱口令没有现实风险
#     （理由写在 .github/workflows/server-ci.yml 的 smoke job 里）。
#
# ⚠️ 配了它的部署**不会**默认安全：若给的值本身就是 admin123，启动体检每次都会告警。
SEED_ADMIN_PASSWORD = os.getenv("ELDER_SEED_ADMIN_PASSWORD", "").strip()

# 存量管理员口令的**一次性轮换开关**。
#
# 设置后，**下次启动时**把所有「口令仍是已知默认口令」的管理员轮换成这个值。
# 这是给「已经跑起来的存量库」准备的处置手段 —— 那批库里的 admin 口令至今是
# admin123，而改种子对它们完全无效。
#
# ⚠️ 幂等性（为什么不会每次 boot 都改一遍）
# ----------------------------------------
# 触发条件**本身**就是「口令还是默认口令」：轮换成功后该条件不再成立，
# 于是后续每次启动自然不再动手 —— 不需要额外的"已处理"标记位。
# 再加一道护栏：若配置值自己就是已知默认口令（比如手滑写成 admin123），
# **拒绝轮换**，否则会退化成每次 boot 都改一次的配置漂移（极难排查）。
#
# ⚠️ 用完即删：轮换成功后把它从 .env / compose 里删掉。留着它等于把新口令
# 的明文长期留在配置文件里，而且下次有人的口令又变回默认值时会被静默改写。
RESET_ADMIN_PASSWORD = os.getenv("ELDER_RESET_ADMIN_PASSWORD", "").strip()

# 「已知默认口令」清单：启动体检与一次性轮换**都**以它为准。
#
# 判定**必须**走 ``security.verify_password``（PBKDF2 + 随机盐的比对），
# 不能拿 ``password_hash`` 做字符串比较：hash 自带 16 字节随机盐，
# 同一个口令两次 hash 出来的字符串**完全不同**，字符串比对会**恒 False**
# -> 全员漏报 -> 体检形同虚设（这正是本类漏洞最难发现的失效形态）。
DEFAULT_ADMIN_PASSWORDS: tuple[str, ...] = ("admin123",)

# ---------------------------------------------------------------- 登录失败限速
# 主理人 2026-10-17 拍板的三条规则（**全部只针对登录失败**）：
#   1. 密码错一次之后，该主体必须等 5 秒才能再试，期间尝试直接拒绝；
#   2. 每分钟最多 6 次失败；
#   3. 每小时最多 20 次失败。
#
# 实现与取舍写在 server/app/ratelimit.py 的模块 docstring 里，运维口径写在
# docs/DEPLOY.md §5.3。这里只解释**为什么必须是环境变量**：
#
#   1. 默认值（5 / 6 / 20）是给"面向公网的单人私有化部署"定的，**内网 / 办公
#      网 / NAT 出口后面有很多人**的部署会被误伤 —— 那种场景要把 ELDER_LOGIN_FAIL_PER_HOUR
#      调大，改代码等于逼人开分支；
#   2. tools/ 下 8 个冒烟脚本会在几秒内连续登录同一台服务，其中 smoke_mail /
#      smoke_home 还会**故意输错一次密码**。冒烟必须能把限额调大或整个关掉，
#      否则限速器会把 CI 打红 —— 但**默认值仍必须是 5/6/20**，
#      关掉只能靠显式设 ELDER_LOGIN_RATE_LIMIT=0。
#
# ⚠️ 取值规则（安全侧）：**解析不出整数时回落到默认值**，而默认值是**最严**的
#   那一档。也就是说把变量写错（写成空串、写成 abc）得到的是"更严格"，
#   不是"限速器消失" —— 这跟注册开关那类"写错就敞开"的变量正好相反，
#   方向必须选对。
LOGIN_RATE_LIMIT_ENABLED = (
    os.getenv("ELDER_LOGIN_RATE_LIMIT", "1").strip().lower()
    not in ("0", "false", "no", "off", "")
)
#: 失败后必须等待的秒数（规则 1）。``0`` = 不冷却。
LOGIN_FAIL_COOLDOWN_SEC = _int_env("ELDER_LOGIN_FAIL_COOLDOWN_SEC", 5)
#: 每个主体每分钟允许的失败次数（规则 2）。``0`` = 该窗口不生效。
LOGIN_FAIL_PER_MINUTE = _int_env("ELDER_LOGIN_FAIL_PER_MINUTE", 6)
#: 每个主体每小时允许的失败次数（规则 3）。``0`` = 该窗口不生效。
LOGIN_FAIL_PER_HOUR = _int_env("ELDER_LOGIN_FAIL_PER_HOUR", 20)

# 限速表最多记多少个"主体"（IP / 用户名）的失败记录。
#
# ⚠️ 为什么必须封顶：用户名键是**请求里带什么就建什么**，攻击者可以拿
#     一万个不存在的用户名各错一次，把表撑爆（内存里的 dict 无上限增长 =
#     一条不需要口令的 OOM 路径）。封顶后按 LRU 淘汰最久没动静的键，
#     代价只是"某个很久没失败的主体重新计数"，不会锁死任何人。
LOGIN_RATE_LIMIT_MAX_KEYS = _int_env("ELDER_LOGIN_RATE_LIMIT_MAX_KEYS", 10000)

# ---------------------------------------------------------------- 客户端 IP 与反代
# 生产是「Docker + 域名 + Nginx」，服务端看到的 TCP 对端是 **Nginx**，
# 真实客户端 IP 只在 X-Forwarded-For 里。但盲目信任这个头等于限速器形同虚设
# —— 攻击者随便换个头就是一个新主体。所以规则是：
#
#   * **默认（本变量为空）谁都不信**：只认 ``request.client.host``（TCP 对端）。
#     直连部署 / 容器直接暴露端口就是这个形态，限速照常生效。
#   * **只有显式列出**的上一级地址才被当成"可信代理"，此时才去解析 XFF，
#     并且**从右往左**扫、跳过所有可信代理，取第一个不可信的地址 ——
#     攻击者伪造的那几段是被**Nginx 追加在左边**的，从右往左扫正好跳过它们。
#
# 填法：逗号分隔的 IP 或 CIDR。Nginx 与应用同一台机器 -> ``127.0.0.1``；
# Nginx 在另一个容器 -> 那个容器在 docker 网里的地址，或整段 ``172.18.0.0/16``。
# 详见 docs/DEPLOY.md §5.3。
TRUSTED_PROXY_IPS = os.getenv("ELDER_TRUSTED_PROXY_IPS", "").strip()

# ---------------------------------------------------------------- 激活码
# 与固件 elder_activation.c 保持完全一致：
#   [0]     1 位字母（剔除易混淆的 I / O）
#   [1..11] 11 位随机数字
#   [12]    1 位 Luhn 校验位（由 [1..11] 计算）
ACT_CODE_LEN = 13
ACT_CODE_TTL_MINUTES = int(os.getenv("ELDER_ACT_TTL_MIN", "10"))
LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"

# ---------------------------------------------------------------- 设备 OTA 固件分发
# 两个变量**都非空**时，OTA 响应（已绑定 / 未绑定两条路径）才带
# ``"firmware": {"version": ..., "url": ...}``；任一为空 = 不返回该段
# = 设备永远不 OTA（**默认行为逐字不变**，既有部署升级不受影响）。
#
# 为什么是「两个都非空」而不是「配了 url 就返回」：只配一半等于配置不完整，
# 下一个缺字段的段会让固件白做一次版本比较，不如干脆不返回让设备保持现状。
#
# 取值口径：
#   * ``version`` 的真源是 ``firmware/xiaozhi/CMakeLists.txt`` 的 ``PROJECT_VER``
#     （固件侧 ``ota.cc`` 用自己的 current_version_ 与服务端下发的版本比较，
#     服务端**不判断**设备当前版本，只报告最新可用版本）；
#   * ``url`` 指向的必须是**单 app 镜像**（CI 产物 ``xiaozhi.bin``），
#     **不是** ``merged-flash.bin`` —— ``Ota::Upgrade()`` 用 ``esp_ota_write``
#     写 OTA 分区，只吃单 app 镜像；
#   * 二进制**不由本服务托管**：地址由部署方提供（Nginx 静态目录 / OSS / CDN），
#     且必须是设备可达的地址（内网设备给内网地址）。GitHub Actions artifact
#     有保留期限，生产要放稳定位置。
# 完整运维口径见 docs/DEPLOY.md §1.10。
FIRMWARE_VERSION = os.getenv("ELDER_FIRMWARE_VERSION", "").strip()
FIRMWARE_URL = os.getenv("ELDER_FIRMWARE_URL", "").strip()

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

# ---------------------------------------------------------------- 部署模式与对外地址
# 三种拓扑（决策表与"我该选哪种"见 docs/DEPLOY.md §4.2）：
#
#   direct —— 公网 IP:端口直暴露，或前置反代能保证 scheme 正确的场景。
#             **按原方案**：从 request.url.scheme + Host 推导 ws/wss 与 OTA 地址。
#   tunnel —— 「TLS 在前置终止、源站只看到明文」的形态。
#             ⚠️ **不是「用了 Cloudflare Tunnel」才选它**——判据是拓扑，
#             不是工具。**Nginx / Caddy / OpenResty 在前置做 TLS 终止、
#             后端只收到明文 HTTP 的，一律属于这一类**（本项目生产早期就是
#             Cloudflare Tunnel，但那只是 tunnel 的一种实现，不是它的定义）。
#             **必须**有一个显式对外地址，否则**启动直接失败**（见下）。
#   auto   —— 未显式设置 ELDER_DEPLOY_MODE 时的默认值：推导逻辑与 direct
#             完全相同（= 历史行为），但启动日志会说明推断依据，便于排障。
#
# 判据速查（一句话）
# ----------------------------------------
#   「我的后端容器收到的是 http:// 还是 https://？」
#       收到 https://        → direct（或 auto，按请求推导就够）
#       收到 http:// 但对外是域名 HTTPS → tunnel（**必须**配显式地址）
#   「我有没有前置反代做 TLS 终止？」
#       有（Nginx / Caddy / Cloudflare Tunnel 都算）→ 通常是 tunnel
#
# ⚠️ 为什么 tunnel 缺地址是**启动失败**而不是告警后继续跑
# ----------------------------------------------------
# 缺地址时服务端一切"正常"：心跳成功、管理台能开、日志一片干净，只有设备
# 一开口就断（拿到 ws://域名 打 443）。这种故障在**运行期没有任何信号**，
# 等发现时老人已经不会说话了。而它的修法只是加一行环境变量。
# 用一次启动失败换掉一次线上静默故障，这就是"能早失败就早失败"的全部理由。
#
# 为什么这样不会伤到存量部署：tunnel 是**显式 opt-in** 的新变量，
# 不设 ELDER_DEPLOY_MODE 就是 auto = 今天的行为，既有部署升级不受影响。
# 反过来，如果用 WARNING 而不是失败：容器会继续跑（心跳确实通），
# 告警混在启动日志里很可能没人注意 —— 那正是本特性要消灭的失效形态。
#
# ⚠️ auto/direct 下**故意不校验**旧变量 ELDER_VOICE_PUBLIC_BASE 的 scheme：
# 那个变量历史上不校验，加了校验就改了存量部署的语义（本次要求逐字保持）。
# 它在新变量 ELDER_PUBLIC_BASE_URL 上始终校验（新变量没有历史语义要保）。
# ----------------------------------------------------------------

#: 旧的逃生舱变量（``ELDER_VOICE_PUBLIC_BASE``）。**语义逐字未变**：仍然只做
#: ``rstrip("/")``，仍然同时决定 HTTP/OTA 与 WS 地址（WS 由它换 scheme 推导）。
#: 它是被下面 ``PUBLIC_BASE_URL`` 合并的输入之一，不再是 device_api 的直接读点。
VOICE_PUBLIC_BASE = os.getenv("ELDER_VOICE_PUBLIC_BASE", "").rstrip("/")

#: 部署模式（``ELDER_DEPLOY_MODE``）：``direct`` / ``tunnel`` / ``auto``。
#: 大小写不敏感、忽略首尾空白；未设置 = ``auto``。
DEPLOY_MODE = os.getenv("ELDER_DEPLOY_MODE", "").strip().lower() or "auto"

#: HTTP/OTA 对外地址（``ELDER_PUBLIC_BASE_URL``）。必须是 http(s)://，启动期校验。
_PUBLIC_BASE_URL = os.getenv("ELDER_PUBLIC_BASE_URL", "").strip().rstrip("/")

#: WebSocket 单独域名（``ELDER_WS_PUBLIC_BASE_URL``，可选）。必须是 ws(s)://。
#: Cloudflare Tunnel 可以给同一源站配多个 hostname，WS 走独立域名时用它；
#: 不配则 WS 由 HTTP 地址换 scheme 推导（与旧变量时代的口径一致）。
_WS_PUBLIC_BASE_URL = os.getenv("ELDER_WS_PUBLIC_BASE_URL", "").strip().rstrip("/")

_DEPLOY_MODES = ("auto", "direct", "tunnel")
_MODE_DESC = {
    "auto": "auto（未显式设置 ELDER_DEPLOY_MODE，按请求现场推导）",
    "direct": "direct（公网直暴露 / 反代后置，按请求现场推导）",
    "tunnel": "tunnel（TLS 在前置终止：Cloudflare Tunnel / Nginx / Caddy 都适用，"
              "判据是拓扑不是工具）",
}

_deploy_log = logging.getLogger("elder.deploy")


def _ensure_deploy_log_handler() -> None:
    """保证启动摘要必然可见。

    ``main.py`` 的 ``logging.basicConfig`` 在 **import 完本模块之后**才执行
    （import 顺序决定的），而启动自检发生在 import 期 —— 彼时 root 上还没有
    handler，INFO 会被 ``logging.lastResort``（只打 WARNING+）吞掉。
    这里在无 handler 时自建一个 StreamHandler，格式与 main.py 逐字一致；
    随后 main.py 的 basicConfig 因 root 已有 handler 而成为 no-op，
    日志格式与去向都不变。
    """
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _validate_base(value: str, var: str, allowed: tuple[str, ...]) -> None:
    """校验一个对外地址变量；不合法**直接启动失败**（SystemExit）。

    为什么用 SystemExit 而不是 raise Exception：它不会被调用链上常见的
    ``except Exception`` 吞掉，且 Python 会把消息打到 stderr 后以非零码退出 ——
    ``docker compose up`` 里一眼可见，不需要翻 traceback。
    """
    parsed = urlparse(value)
    if parsed.scheme not in allowed or not parsed.netloc:
        raise SystemExit(
            f"【启动失败】{var} 取值非法：{value!r}\n"
            f"    scheme 必须是 {' 或 '.join(allowed)} 且要带主机名，"
            f"例如 https://152.136.230.132。\n"
            f"    这个地址是设备心跳时下发的，写错 = 设备拿到连不上的地址，"
            f"所以在启动期直接失败，不静默降级。"
        )


def _resolve_public_bases() -> tuple[str, str, str]:
    """合并新旧变量，返回 ``(HTTP/OTA 地址, 来源说明, WS 地址)``。

    新旧变量优先级（**不静默改语义**）：
      * HTTP/OTA 地址：``ELDER_PUBLIC_BASE_URL`` 优先；只有旧变量时用它
        （行为与今天逐字一致）。两者都配时以新变量为准并 WARNING 提醒，
        避免「改了一处漏一处」。
      * WS 地址：``ELDER_WS_PUBLIC_BASE_URL`` 优先；未配则由 HTTP 地址换
        scheme 推导（https→wss / http→ws，与旧变量时代逐字一致）。
    """
    http_base, http_source = "", ""
    if _PUBLIC_BASE_URL:
        _validate_base(_PUBLIC_BASE_URL, "ELDER_PUBLIC_BASE_URL", ("http", "https"))
        http_base, http_source = _PUBLIC_BASE_URL, "ELDER_PUBLIC_BASE_URL"
        if VOICE_PUBLIC_BASE:
            _deploy_log.warning(
                "【部署】ELDER_PUBLIC_BASE_URL 与旧变量 ELDER_VOICE_PUBLIC_BASE "
                "同时配置：本次以新变量为准（%s），旧变量被忽略。建议删掉旧变量 —— "
                "两处并存只会在下次换域名时变成「改了一处、漏了一处」的坑。",
                _PUBLIC_BASE_URL)
    elif VOICE_PUBLIC_BASE:
        http_base = VOICE_PUBLIC_BASE
        http_source = ("ELDER_VOICE_PUBLIC_BASE（旧变量；建议迁移到 "
                       "ELDER_PUBLIC_BASE_URL，见 docs/DEPLOY.md §4.2）")

    if _WS_PUBLIC_BASE_URL:
        _validate_base(_WS_PUBLIC_BASE_URL, "ELDER_WS_PUBLIC_BASE_URL", ("ws", "wss"))
        if not http_base:
            # WS 单独域名是"锦上添花"的那一半：OTA 检查仍然需要一个 http(s)
            # 地址。只配一半 = 配置自相矛盾，启动期挑明，不静默忽略。
            raise SystemExit(
                f"【启动失败】配了 ELDER_WS_PUBLIC_BASE_URL（{_WS_PUBLIC_BASE_URL}）"
                f"却没有配置 HTTP/OTA 对外地址。\n"
                f"    WS 单独域名用于「业务与语音分两个 hostname」的部署"
                f"（Cloudflare Tunnel 可配多个 hostname），但 OTA 检查"
                f"（/api/voice/ota）仍然需要一个 http(s) 地址。\n"
                f"    请补上 ELDER_PUBLIC_BASE_URL=https://<域名>"
                f"（或旧变量 ELDER_VOICE_PUBLIC_BASE）。"
            )
    return http_base, http_source, _WS_PUBLIC_BASE_URL


#: 生效的 HTTP/OTA 对外地址（新变量或旧变量合并后的结果，留空 = 按请求推导）。
PUBLIC_BASE_URL: str
#: 上面的地址是哪个变量给的（"" / 变量名），启动摘要与排障时用它说明来源。
PUBLIC_BASE_SOURCE: str
#: 生效的 WS 对外地址（留空 = 由 HTTP 地址换 scheme 推导）。
WS_PUBLIC_BASE_URL: str


def _deploy_startup_check() -> None:
    """启动自检：把部署拓扑里的配置矛盾在**启动期**暴露出来，并打摘要。

    本函数在 config 被 import 时执行 —— 对 uvicorn 来说这就是启动的第一刻，
    比任何 lifespan 钩子都早。
    """
    _ensure_deploy_log_handler()

    if DEPLOY_MODE not in _DEPLOY_MODES:
        raise SystemExit(
            f"【启动失败】ELDER_DEPLOY_MODE 取值非法：{DEPLOY_MODE!r}。\n"
            f"    合法取值：{' / '.join(_DEPLOY_MODES)}（大小写不敏感）。\n"
            f"    这个变量的全部意义就是「显式声明拓扑」——写错时猜一个默认值，\n"
            f"    等于把显式变回隐式，那正是本特性要消灭的故障形态。"
        )

    if DEPLOY_MODE == "tunnel":
        if not PUBLIC_BASE_URL:
            raise SystemExit(
                "【启动失败】ELDER_DEPLOY_MODE=tunnel 但没有配置对外地址。\n"
                "    tunnel 形态（Cloudflare Tunnel / Nginx / Caddy 等 TLS 前置\n"
                "    终止的场景）的源站看到的永远是明文 HTTP：不配对外地址，\n"
                "    设备心跳会拿到 ws://<域名>/... 去打 443 —— 能心跳、不能说话，\n"
                "    且运行期零报错。\n"
                "    修法只需一行（二选一）：\n"
                "        ELDER_PUBLIC_BASE_URL=https://152.136.230.132\n"
                "        ELDER_VOICE_PUBLIC_BASE=https://152.136.230.132   （旧变量）\n"
                "    WS 想用单独域名，再加 ELDER_WS_PUBLIC_BASE_URL=wss://<WS域名>。\n"
                "    （若你的后端容器确实直接收到 https:// 请求，用 auto 即可。）"
            )
        if PUBLIC_BASE_SOURCE.startswith("ELDER_VOICE_PUBLIC_BASE"):
            # 旧变量在 auto/direct 下故意不校验（保持历史语义），但 tunnel 模式
            # 是显式声明的生产拓扑，这里必须严格：拿 wss:// 当 HTTP 地址用，
            # OTA 会下发一个设备连不上的地址。
            parsed = urlparse(PUBLIC_BASE_URL)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise SystemExit(
                    f"【启动失败】tunnel 模式下 ELDER_VOICE_PUBLIC_BASE 必须是 "
                    f"http(s):// 地址，当前是 {PUBLIC_BASE_URL!r}。\n"
                    f"    旧变量在 auto/direct 下不校验 scheme（那是历史宽松语义，\n"
                    f"    本次刻意未改）；tunnel 显式声明了拓扑，这里必须严格。\n"
                    f"    建议直接迁移到新变量 ELDER_PUBLIC_BASE_URL（任何模式下都校验）。"
                )

    _log_deploy_summary()


def _log_deploy_summary() -> None:
    """启动摘要 —— 排障第一现场，写成人能一眼看懂的话。"""
    lines = [f"【部署】模式：{_MODE_DESC[DEPLOY_MODE]}"]
    if PUBLIC_BASE_URL:
        ws_show = WS_PUBLIC_BASE_URL or PUBLIC_BASE_URL.replace(
            "https://", "wss://").replace("http://", "ws://")
        ws_src = ("（来源 ELDER_WS_PUBLIC_BASE_URL）" if WS_PUBLIC_BASE_URL
                  else "（由 HTTP 地址换 scheme 推导）")
        lines.append(f"【部署】   HTTP/OTA 对外地址：{PUBLIC_BASE_URL}"
                     f"（来源 {PUBLIC_BASE_SOURCE}）")
        lines.append(f"【部署】   WS 对外地址：{ws_show} {ws_src}")
        lines.append(f"【部署】   设备心跳将拿到：ws={ws_show}{VOICE_WS_PATH}"
                     f"  ota={PUBLIC_BASE_URL}{VOICE_OTA_PATH}")
    else:
        lines.append("【部署】   HTTP/OTA 对外地址：未配置 → 按请求推导："
                     f"http(s)://<请求Host>{VOICE_OTA_PATH}")
        lines.append("【部署】   WS 对外地址：未配置 → 按请求推导："
                     f"ws(s)://<请求Host>{VOICE_WS_PATH}")
        lines.append("【部署】   设备拿到的 ws 地址由请求 scheme 决定。镜像未传 "
                     "--proxy-headers，uvicorn 只信任来自 127.0.0.1 的 "
                     "X-Forwarded-Proto；生产里对端是 Docker 网桥网关、不在信任名单"
                     " ⇒ scheme 恒为 http ⇒ 下发 ws://")
        if DEPLOY_MODE == "auto":
            lines.append("【部署】   ⇒ 若部署在 Cloudflare Tunnel / Nginx(TLS) 后面，"
                         "请显式设 ELDER_DEPLOY_MODE=tunnel + "
                         "ELDER_PUBLIC_BASE_URL=https://<域名>（WS 单独域名再加 "
                         "ELDER_WS_PUBLIC_BASE_URL），否则设备「能心跳不能说话」")
    for line in lines:
        _deploy_log.info(line)


PUBLIC_BASE_URL, PUBLIC_BASE_SOURCE, WS_PUBLIC_BASE_URL = _resolve_public_bases()
_deploy_startup_check()

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
# 调太大 = 老人说完要干等；调太小 = 会被句子间的停顿误切。
# **2026-10-17 调档 1500 → 2000ms**：固件默认走 realtime 模式
# （sdkconfig.defaults:82 CONFIG_USE_DEVICE_AEC=y ⇒ application.cc:279
# 选 kListeningModeRealtime），服务端这个阈值是**唯一的断句判据**。
# 老人语速慢、句中思考停顿长（自发口语中超过 1.5s 的停顿常见），
# 1500ms 会把"话说一半"判成说完 —— 内容直接丢失且不可恢复；
# 而调大的代价只是老人说完后多等半秒。**两类错误的代价不对称：
# 宁可多等，不可截断。** 另有按停顿历史自适应的上限（见下）。
VOICE_SILENCE_MS = int(os.getenv("ELDER_VOICE_SILENCE_MS", "2000"))
# 自适应阈值上限（毫秒）。本轮对话里若出现过"停顿后又继续说"的长停顿，
# 说明这位老人说话慢、停顿长，断句阈值按 VOICE_PAUSE_FACTOR 放大，
# 但不超过这个上限 —— 否则一句不停地说下去会让阈值无限膨胀。
VOICE_SILENCE_MAX_MS = int(os.getenv("ELDER_VOICE_SILENCE_MAX_MS", "6000"))
# 停顿自适应系数：观察到的最长"句中停顿"（停顿后又继续说话的那次）P 毫秒，
# 则本轮阈值 = max(VOICE_SILENCE_MS, min(VOICE_SILENCE_MAX_MS, P*系数+500))。
# 1.5 的依据：VAD 端点判决要留出"停顿后还会接着说"的余量，取实测停顿的
# 1.5 倍 + 500ms 固定余量；调成 1.0 等于没有自适应。
VOICE_PAUSE_FACTOR = float(os.getenv("ELDER_VOICE_PAUSE_FACTOR", "1.5"))
# 多长的"停顿后又继续"才记入自适应（毫秒）。低于它的是词间间隙，
# 不是思考停顿，不拿来做依据。
VOICE_PAUSE_MIN_MS = int(os.getenv("ELDER_VOICE_PAUSE_MIN_MS", "600"))
# 触发兜底前至少要听到这么多语音，防止"一句都没说就静音触发"把环境噪声
# 当成一整句送去 ASR（结果是识别出一串乱码，老人莫名其妙）。
VOICE_MIN_SPEECH_MS = int(os.getenv("ELDER_VOICE_MIN_SPEECH_MS", "300"))
# 单轮收听的安全上限（毫秒）：老人说着说着离开/卡住时，兜底 finalize，
# 避免会话永远挂在 listening。60s 足够任何正常单句，又不会无限挂住。
VOICE_MAX_LISTEN_MS = int(os.getenv("ELDER_VOICE_MAX_LISTEN_MS", "60000"))

# ---- 语音能量门限（老人轻声 / 远场）----
# 旧实现只有一个门限 _SILENCE_RMS=0.01（-40dBFS）：高于它算语音。
# 两个失效模式：① 轻声老人/离设备远，整句 RMS 都在 0.01 以下 ⇒
# 一句都不会被算作语音 ⇒ min_speech 门永远不过 ⇒ 整句被丢弃；
# ② 门限一刀切到底噪以下又会让房间噪声被当成语音 ⇒ 静音永远攒不够 ⇒
# 会话挂死。所以改成"迟滞 + 底噪跟随"两件套：
#   * enter 门限：判定"开口了"用（高门限，别被底噪触发）；
#   * keep  门限：已经开口之后，判定"还在说"用（低门限，句中换气/
#     轻声音节不掉出语音态），但只保留 VOICE_SOFT_GRACE_MS 毫秒 ——
#     不设期限的话，刚好落在两个门限之间的持续底噪会让会话永远挂住；
#   * 底噪跟随：按帧估计房间底噪（快降慢升），门限 = max(绝对下限, 底噪×系数)。
#     安静房间里轻声老人也能被检测到；嘈杂房间里门限自动抬高，
#     代价是那种房间里的耳语分辨不出（诚实的降级，见报告）。
# 绝对下限 0.005（-46dBFS）：高于实测底噪 0.002-0.004 的上限，
# 低于旧门限 0.01 —— 把"轻声但可闻"的老人从"整句被丢"里救回来。
VOICE_SPEECH_RMS = float(os.getenv("ELDER_VOICE_SPEECH_RMS", "0.005"))
# keep 门限的绝对下限（-52dBFS）。句中轻声音节/远场衰减后的语音
# 落在 enter 与 keep 之间时，靠它续住语音态。
VOICE_SPEECH_RMS_KEEP = float(os.getenv("ELDER_VOICE_SPEECH_RMS_KEEP", "0.0025"))
# 软语音（enter 与 keep 之间的能量）最长续多久（毫秒）。锚点是
# **最后一次高能量帧**，软帧本身不会刷新它 —— 否则持续底噪会无限续期。
VOICE_SOFT_GRACE_MS = int(os.getenv("ELDER_VOICE_SOFT_GRACE_MS", "2000"))

# LLM 工具调用（提醒等）单轮最多几轮。防 LLM 反复调用同一工具形成死循环：
# 每轮都是一次真实的 token 花费，不设上限等于把账单交给模型的坏心情。
VOICE_TOOL_MAX_ROUNDS = int(os.getenv("ELDER_VOICE_TOOL_MAX_ROUNDS", "3"))

# 会话内保留的历史轮数上限。语音链路每轮都要塞人格 + 记忆召回，窗口比
# 纯文本更紧，且老人话题跳跃少、短会话即可；超过就丢最早的。
VOICE_HISTORY_TURNS = int(os.getenv("ELDER_VOICE_HISTORY_TURNS", "4"))

# ---------------------------------------------------------------- 提醒调度
# 服务端预生成提醒实例的提前量（小时）。设备离线时靠本地 RTC 兜底触发，
# 联网时以服务端生成的实例为准做对账。
SCHEDULE_AHEAD_HOURS = int(os.getenv("ELDER_SCHEDULE_AHEAD_HOURS", "36"))
SCHEDULER_INTERVAL_SEC = int(os.getenv("ELDER_SCHEDULER_INTERVAL", "60"))

# ---------------------------------------------------------------- 其它
# ⚠️ 这里曾有一整段会员核销配置（MEMBERSHIP_ORDER_TTL_HOURS /
# MEMBERSHIP_ORDER_RATE_* / AMOUNT_*_LIMIT_CENTS），随会员制取消整体删除。
# 陪聊时长上限本身没删，但它是 ``chat_quota_config`` 表里的**数据**而不是环境变量，
# 见 app/chat_quota.py。
CORS_ORIGINS = os.getenv("ELDER_CORS", "*").split(",")
