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

# ---------------------------------------------------------------- 激活码
# 与固件 elder_activation.c 保持完全一致：
#   [0]     1 位字母（剔除易混淆的 I / O）
#   [1..11] 11 位随机数字
#   [12]    1 位 Luhn 校验位（由 [1..11] 计算）
ACT_CODE_LEN = 13
ACT_CODE_TTL_MINUTES = int(os.getenv("ELDER_ACT_TTL_MIN", "10"))
LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"

# ---------------------------------------------------------------- 小智语音链路
# 语音/AI 链路复用 xiaozhi-esp32-server，本服务只做业务面，
# 这里仅记录地址，供管理页面与设备下发配置时展示/透传。
XIAOZHI_WS_URL = os.getenv("ELDER_XIAOZHI_WS", "ws://xiaozhi-server:8000/xiaozhi/v1/")
XIAOZHI_OTA_URL = os.getenv("ELDER_XIAOZHI_OTA", "http://xiaozhi-server:8003/xiaozhi/ota/")

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
