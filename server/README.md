# 老人陪伴提醒助手 · 业务后端

Docker 一键部署的业务后端，负责**设备绑定、提醒规则与回执、形象与提示词、AI 供应商配置、交互统计**。
语音 / AI 链路（ASR / LLM / TTS / OTA）**自研**，实现在 `app/voice/`，对外兼容 xiaozhi 公开协议。

> **不依赖任何第三方服务器。** 早期版本曾用 `profiles: xiaozhi` 复用
> xiaozhi-esp32-server 的官方容器，把角色、音色、记忆、大模型调用都放在第三方进程里；
> 现已整体自研，该 profile 与其容器、端口**全部移除**，不要试图启动它。

## 1. 快速开始

默认形态**内置 SQLite**，只起一个容器，不需要任何外部数据库：

```bash
cd server
cp .env.example .env          # 可选；至少改 ELDER_JWT_SECRET
docker compose up -d          # 只起业务后端（内置 SQLite）
```

打开 <http://localhost:8064> 进入管理后台（用户端在 `/user`）。

⚠️ **默认 `admin/admin123` 已移除**（commit `bb5ca98`）。首次启动必须显式提供种子口令：

```bash
# .env 里加一行，值换成自己的
ELDER_SEED_ADMIN_PASSWORD=<你的强口令>
```

不提供则种子口令**随机化**，你也登不进去。
⚠️ **已在跑的生产库不受种子改动影响**，存量口令需另行处置：
登录后走 `/api/auth/password` 改密，或设`ELDER_RESET_ADMIN_PASSWORD` 重启一次
（日志出现 `ELDER-ADMIN-PASSWORD-ROTATED` 即成功）**并立刻删掉该变量**。

容器内监听 8080，对外只发布 8064（`ELDER_HTTP_PORT` 可改）。

数据落在命名卷 `elder-data` 的 `/data/elder.db`。`docker compose down` 不会删卷，
但 **`docker compose down -v` 会连数据一起删**，请先备份。

改用 MySQL（自带容器）：

```bash
docker compose -f docker-compose.yml -f docker-compose.mysql.yml up -d
```

可选：叠加本地语音服务（FunASR ASR + Silero VAD，**纯 CPU，不需要显卡**）。

这是一个**纯叠加文件**，本身不含 `profiles`——加载即启动其中全部服务：

```bash
cd server
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d
#  8064  本服务（业务面 + 管理后台 + 自研语音链路，容器内 8080）
# 10095  本地 ASR（仅 127.0.0.1）
#  8081  本地 VAD（容器内 8080，仅 127.0.0.1）
```

⚠️ 两个端口**只绑 `127.0.0.1`**（服务本身无鉴权，暴露到公网等于把麦克风内容敞开）。
主服务在同一 compose 网络内走 `http://local-asr:10095` 访问，不经宿主机端口。

⚠️ **刻意不提供本地中文 TTS**：生产是 iStoreOS 软路由、**无独显**，
CosyVoice 2 这类模型需要 8GB 显存起步，跑不动。这是硬件约束下的裁剪，不是遗漏。

> 完整的部署文档（接入 MySQL 的完整步骤、SQLite→MySQL 迁移、Nginx 反代与 HTTPS、
> 环境变量对照表、排障、备份恢复）见 **[../docs/DEPLOY.md](../docs/DEPLOY.md)**。

## 2. 本地开发（不依赖 Docker）

```bash
pip install -r requirements.txt
export ELDER_DB="sqlite:///./elder.db"      # 默认就是 SQLite，这行只是显式指定位置
export ELDER_JWT_SECRET="dev-secret"
uvicorn app.main:app --reload --port 8080
```

> **不设 `ELDER_DB` 时库落在哪**：由 `app/dbpath.py` 裁决（唯一真源，纯函数，
> 被 `tools/smoke_db_path.py` 守着）。本机裸跑默认落到**用户数据目录**
> —— Windows `%LOCALAPPDATA%\voice-avatar-box\elder.db`、
> macOS `~/Library/Application Support/voice-avatar-box/elder.db`、
> Linux `~/.local/share/voice-avatar-box/elder.db`；容器里仍是 `/data/elder.db`
> （compose 显式设了 `ELDER_DB`，语义不变）。**不再默认 `./elder.db`**：那会把
> 运行库写进 git 工作树，被 `*.db` 藏起来之后既不会被 review、又会被
> `git clean -xdf` 一起删掉。
>
> 如果你目录里**已经有** `./elder.db` 或 `server/elder.db` **且有数据**（大小 > 0），
> 服务会**继续用它**（不自动迁移，回滚成本才为 0），并在启动日志打一条 WARNING。
> 启动日志里的 `【数据库】<路径>（来源=<source>）` 就是「我现在连的是哪个库」的答案。

冒烟测试（自动起服务跑完整业务闭环）：

```bash
python tools/smoke_server.py 8100
```

各冒烟脚本的默认端口**互不重复**，因此可以放心并行跑（每个脚本记得配独立的
`TMPDIR`，否则临时 SQLite 库会互相抢占）：

```bash
python tools/smoke_server.py 8100 & \
python tools/smoke_roles.py 8101 & \
python tools/smoke_home.py 8102 & \
python tools/smoke_memory.py 8103 & \
python tools/smoke_models.py 8104 & \
python tools/smoke_mail.py 8105 & \
python tools/smoke_quota.py 8106 & \
wait
```

⚠️ **本地跑冒烟必须显式带种子口令**：

```bash
export ELDER_SEED_ADMIN_PASSWORD=admin123
```

因为 `admin/admin123` 硬编码已移除（见第 1 节），不提供时种子口令会随机化，
依赖管理员登录的脚本就会红。**这是预期行为，不是回归。**

完整脚本清单见 `tools/smoke_*.py`，端口表的**唯一真源**是 `tools/smoke_ports.py`；
端口可用位置参数或 `--port N` 覆盖（CI 就是这么用的）。查看当前端口表与占用情况：

```bash
python tools/smoke_ports.py --check
```

## 3. 架构边界

| 能力 | 归属 |
| --- | --- |
| WebSocket 语音对话、ASR/LLM/TTS 编排、OTA | **本服务自研**（`app/voice/`） |
| 提醒工具调用（增删查改提醒） | 本服务服务端 function calling（`app/voice/tools.py`） |
| Opus 编解码 | ctypes 直连系统 libopus（零新增 Python 依赖） |
| 设备注册与激活码绑定 | 本服务 |
| 提醒规则编排、排期实例、回执对账 | 本服务 |
| 形象 / 系统提示词 / 音色 / 情绪灯效 | 本服务 |
| 多平台 AI 供应商配置托管 | 本服务 |
| 交互次数统计 | 本服务 |

设备通过 `POST /api/device/sync` 一次拉全：服务器时间、提醒规则与排期、当前形象、AI 供应商、WS/OTA 地址。
OTA 只给**已绑定设备**签发 token；未绑定只返激活码。

## 4. 关键设计

### 4.1 离线优先
提醒规则**下发到设备**，设备靠本地 RTC 自行响铃，联网后把回执补齐（`/api/device/events`）。
服务端同时预排 36 小时实例做对账，两边以 `(reminder_id, due_at)` 对齐。

### 4.2 激活码
7 位 = 6 位随机数字 + 1 位 Luhn 校验位（全数字），与固件 `activation.c` 逐位一致；
显示为 `123-4567`，降低小屏抄写错误率。输入时允许带空格/短横线，校验位错当场报错。

⚠️ 空间只有 10^6，安全靠三件事：绑定接口限速（`ratelimit.bind_gate`，
每 IP 5 次/分钟、40 次/小时失败）、单码失败上限（`ActivationCode.attempts`
默认 5 次）、码与 MAC 强绑定（`bind_device` 校验 `device_id`）。
校验位只防手误、不提供抗猜测能力，详见决策记录 D42。

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
| POST | `/api/devices/{id}/code` | 生成 7 位激活码 |
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
| GET | `/api/providers/catalog` | 音色目录 + 三家 TTS 厂商预置（Edge / 火山 / 阿里百炼） |
| GET | `/api/providers/readiness` | 服务就绪度（哪些已配、哪些缺、缺了会怎样） |
| POST | `/api/providers/test-voice` | **真打一次** TTS 合成，返回音频字节与时长 |
| GET | `/api/providers/export/xiaozhi` | 导出为小智 config 片段 |
| GET | `/api/usage/summary` `/api/usage/rows` | token 用量（用户看自己设备 / 管理端看全量） |
| GET | `/api/stats/overview` | 交互统计概览 |
| POST | `/api/device/sync` | 设备心跳，拉全量配置 |
| POST | `/api/device/events` | 提醒回执上报 |
| POST | `/api/device/interactions` | 交互事件上报 |

完整接口文档见 `/docs`（FastAPI 自动生成）。

## 6. 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ELDER_DB` | 内置 SQLite 路径 | **留空即用内置 SQLite**；填 `mysql+pymysql://...` 切 MySQL |
| `ELDER_DATA_DIR` | 见下行 | 内置 SQLite 的持久化目录：容器里是 `/data`（命名卷）；**非容器默认用平台用户数据目录**（`%LOCALAPPDATA%\voice-avatar-box` / `~/Library/Application Support/voice-avatar-box` / `~/.local/share/voice-avatar-box`）。显式指定时目录不存在会**建出来**，建不出来则启动失败 |
| `ELDER_HTTP_PORT` | 8064 | 宿主机对外端口（容器内固定 8080） |
| `ELDER_JWT_SECRET` | 弱默认值 | **生产必须改** |
| `ELDER_SEED_ADMIN_PASSWORD` | 无 | **首次启动必填**，不填则种子口令随机化 |
| `ELDER_DEPLOY_MODE` | `auto` | `auto` / `direct` / `tunnel`。**用 Cloudflare Tunnel 必须设 `tunnel`**，否则缺对外地址不会被发现 |
| `ELDER_PUBLIC_BASE_URL` | 空 | 对外地址（推荐）。**任何模式下都校验 scheme** |
| `ELDER_VOICE_PUBLIC_BASE` | 空 | 旧变量，向后兼容。auto/direct 下不校验 scheme，**tunnel 下强制 https** |
| `ELDER_TRUSTED_PROXY_IPS` | 空 | 可信反代 IP。⚠️ **绝不要用 `--forwarded-allow-ips=*`**，会取可伪造的 XFF 最左段 |
| `ELDER_LOGIN_RATE_LIMIT` | 1 | 登录限速开关（`0` 关闭）。只计失败；口令正确直接发证不看配额 |
| `ELDER_LOGIN_RATE_LIMIT_MAX_KEYS` | 10000 | 限速器键容量，超出按 LRU 淘汰 |
| `ELDER_CORS` | `*` | 跨域来源，逗号分隔 |
| `ELDER_TZ` | `Asia/Shanghai` | 业务时区，影响提醒时间计算 |
| `ELDER_DB_WAIT_SEC` | 180 | 启动等待数据库秒数（仅 MySQL 生效） |
| `ELDER_ACT_TTL_MIN` | 10 | 激活码有效分钟数 |
| `ELDER_SCHEDULE_AHEAD_HOURS` | 36 | 预排期提前量 |
| `ELDER_VOICE_SILENCE_MS` | 2000 | 老人说话静音判定阈值。**默认值已是改善**，一般不用调 |

完整清单（约 39 个变量，含全部 `ELDER_VOICE_*` 语音参数）见
[../docs/DEPLOY.md](../docs/DEPLOY.md) 第 5 节。

完整清单与接入 MySQL 的步骤见 [../docs/DEPLOY.md](../docs/DEPLOY.md)。

## 7. 生产注意事项

- `ELDER_JWT_SECRET` 必须换成随机长字符串
- SQLite 数据落在卷 `elder-data` 的 `elder.db`（`docker compose down -v` 会删），MySQL 落在 `elder-db-data`，都要定期备份
- 管理后台与 API 同端口，建议用 Nginx 反代并开 HTTPS（见 docs/DEPLOY.md）
- 表结构目前靠 `create_all` 自动建，多实例滚动升级前请引入 Alembic 迁移
- SQLite 适合单机；多人共用或数据量上来后按 docs/DEPLOY.md 切 MySQL
