"""运行时配置（全部可用环境变量覆盖）。"""
from __future__ import annotations

import os
from datetime import timedelta
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
# 默认走 MySQL(Docker Compose)；本地调试可设 ELDER_DB=sqlite:///./elder.db
DATABASE_URL = os.getenv(
    "ELDER_DB",
    "mysql+pymysql://root:123456@elder-db:3306/elder_companion?charset=utf8mb4",
)
DB_ECHO = os.getenv("ELDER_DB_ECHO", "0") == "1"
DB_POOL_PRE_PING = True

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

# ---------------------------------------------------------------- 其它
CORS_ORIGINS = os.getenv("ELDER_CORS", "*").split(",")
