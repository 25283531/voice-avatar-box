# 老人陪伴提醒助手 · 业务后端

Docker 一键部署的业务后端，负责**设备绑定、提醒规则与回执、形象与提示词、AI 供应商配置、交互统计**。
语音 / AI 链路（ASR / LLM / TTS / OTA）复用 [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)，不重复造轮子。

## 1. 快速开始

```bash
cd server
cp .env.example .env          # 修改 ELDER_JWT_SECRET
docker compose up -d          # 启动 MySQL + 业务后端
```

打开 <http://localhost:8080> 进入管理后台，默认账号 `admin` / `admin123`（**登录后立即改密码**）。

连带语音链路一起起（可选，需联网拉官方镜像）：

```bash
docker compose --profile xiaozhi up -d
#  8000  小智 WebSocket 语音通道
#  8002  小智智控台
#  8003  OTA / 视觉分析
#  8080  本服务（业务面 + 管理后台）
```

## 2. 本地开发（不依赖 Docker）

```bash
pip install -r requirements.txt
export ELDER_DB="sqlite:///./elder.db"      # 默认走 MySQL，本地可切 SQLite
export ELDER_JWT_SECRET="dev-secret"
uvicorn app.main:app --reload --port 8080
```

冒烟测试（自动起服务跑完整业务闭环）：

```bash
python tools/smoke_server.py 8099
```

## 3. 架构边界

| 能力 | 归属 |
| --- | --- |
| WebSocket 语音对话、ASR/LLM/TTS 编排、OTA | xiaozhi-esp32-server（官方镜像，不改） |
| 设备注册与激活码绑定 | 本服务 |
| 提醒规则编排、排期实例、回执对账 | 本服务 |
| 形象 / 系统提示词 / 音色 / 情绪灯效 | 本服务 |
| 多平台 AI 供应商配置托管 | 本服务 |
| 交互次数统计 | 本服务 |

设备通过 `POST /api/device/sync` 一次拉全：服务器时间、提醒规则与排期、当前形象、AI 供应商、WS/OTA 地址。

## 4. 关键设计

### 4.1 离线优先
提醒规则**下发到设备**，设备靠本地 RTC 自行响铃，联网后把回执补齐（`/api/device/events`）。
服务端同时预排 36 小时实例做对账，两边以 `(reminder_id, due_at)` 对齐。

### 4.2 激活码
13 位 = 1 位字母（剔除易混淆的 I/O）+ 11 位随机数字 + 1 位 Luhn 校验位，
与固件 `elder_activation.c` 逐位一致；显示为 `A-3829-4471-0056` 四段式，降低小屏抄写错误率。
输入时允许带空格/短横线，字母不区分大小写；校验位错当场报错，不需要打服务端。

### 4.3 提醒规则
支持：每天 / 每周指定几天 / 每月指定几号 / 每隔 N 天 / 指定日期一次，
一天可多时刻，带生效区间、剂量、重复播报次数、超时升级分钟数、允许的确认方式。
规则变更会清掉未触发的排期并按新规则重排。

### 4.4 超时升级
后台任务每 60 秒扫描：超过 `escalate_after_min` 仍未确认的实例标记为 `MISSED` 并置 `escalated`，
设备下次心跳拿到该状态即保持红灯常亮，直到确认。

## 5. 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/register` `/api/auth/login` | 注册 / 登录 |
| GET | `/api/devices` | 设备列表 |
| POST | `/api/devices` | 登记设备 |
| POST | `/api/devices/{id}/code` | 生成 13 位激活码 |
| POST | `/api/devices/bind` | 激活码绑定，返回设备 token |
| GET | `/api/devices/{id}/events` | 提醒回执 |
| GET/POST/PATCH/DELETE | `/api/reminders` | 提醒规则 CRUD（`POST` 需 `?device_pk=`） |
| GET | `/api/reminders/upcoming` | 未来 N 小时排期 |
| POST | `/api/reminders/{id}/toggle` | 暂停 / 恢复 |
| POST | `/api/reminders/{id}/confirm` | 子女代确认 |
| GET/POST/PATCH/DELETE | `/api/avatars` | 形象管理 |
| POST | `/api/avatars/{a}/apply/{d}` | 形象应用到设备 |
| GET/POST/PATCH/DELETE | `/api/providers` | AI 平台接入配置 |
| GET | `/api/providers/safe` | 脱敏列表（不含密钥） |
| GET | `/api/providers/export/xiaozhi` | 导出为小智 config 片段 |
| GET | `/api/stats/overview` | 交互统计概览 |
| POST | `/api/device/sync` | 设备心跳，拉全量配置 |
| POST | `/api/device/events` | 提醒回执上报 |
| POST | `/api/device/interactions` | 交互事件上报 |

完整接口文档见 `/docs`（FastAPI 自动生成）。

## 6. 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ELDER_DB` | MySQL 连接串 | 也支持 `sqlite:///./elder.db` |
| `ELDER_JWT_SECRET` | 弱默认值 | **生产必须改** |
| `ELDER_PORT` | 8080 | 监听端口 |
| `ELDER_CORS` | `*` | 跨域来源，逗号分隔 |
| `ELDER_XIAOZHI_WS` | 内网地址 | 下发给设备的语音通道 |
| `ELDER_XIAOZHI_OTA` | 内网地址 | 下发给设备的 OTA 地址 |
| `ELDER_ACT_TTL_MIN` | 10 | 激活码有效分钟数 |
| `ELDER_SCHEDULE_AHEAD_HOURS` | 36 | 预排期提前量 |

## 7. 生产注意事项

- `ELDER_JWT_SECRET` 必须换成随机长字符串
- MySQL 数据落在卷 `elder-db-data`，定期备份
- 管理后台与 API 同端口，建议用 Nginx 反代并开 HTTPS
- 表结构目前靠 `create_all` 自动建，多实例滚动升级前请引入 Alembic 迁移
