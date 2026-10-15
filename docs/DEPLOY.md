# 部署说明（Docker）

本文档覆盖 `server/` 的两种部署形态：

| 形态 | 适用场景 | 数据库 | 启动方式 |
| --- | --- | --- | --- |
| **默认（推荐先跑通）** | 单机 / 家用 / 快速验证 | **内置 SQLite**（零外部依赖，开箱即用） | `docker compose up -d` |
| MySQL | 多人共用、数据量大、需要外部运维工具 | MySQL 8（自带容器或外部实例） | 叠加 `docker-compose.mysql.yml` |

端口与域名是硬性约定，不要改：

- 容器内固定监听 **8080**，对外只发布 **8064**（`ELDER_HTTP_PORT` 可改宿主机侧端口）
- 生产域名 `https://voice.2508866.xyz`（手机 APP 在 `app/src/http/elder.ts` 里代码级写死，无设置入口）

---

## 1. 快速开始（默认内置 SQLite）

### 1.1 前置条件

- Docker 20.10+ 与 Docker Compose v2（`docker compose version` 有输出即可）
- 宿主机 8064 端口未被占用
- 需要 2 GB 以上可用磁盘（镜像 + 数据卷）

### 1.2 启动

```bash
cd server
cp .env.example .env          # 可选：不复制也能起，全部变量都有默认值
vi .env                       # 至少改 ELDER_JWT_SECRET（生产必改）
docker compose up -d
```

默认只起 **一个** 服务 `elder-server`，不会再拉数据库容器。数据库是镜像内置的 SQLite 文件，落在命名卷 `elder-data` 里的 `/data/elder.db`。

首次启动会自动建表并写入种子数据（默认管理员、默认形象、会员套餐等），日志里能看到：

```
seed: created default admin / admin123  <- 请登录后立即修改密码
```

### 1.3 访问

| 入口 | 地址 |
| --- | --- |
| 管理后台 | `http://<host>:8064/` |
| 接口文档 | `http://<host>:8064/docs` |
| 健康检查 | `http://<host>:8064/api/health`（应返回 `{"ok":true,...}`） |

默认账号 `admin` / `admin123`，**登录后立刻改密码**。

### 1.4 常用运维命令

```bash
docker compose logs -f elder-server     # 看日志（Ctrl+C 退出，不会停服务）
docker compose ps                       # 看状态与端口
docker compose restart elder-server     # 重启
docker compose stop                     # 停（容器还在，数据还在）
docker compose start                    # 再起
docker compose down                     # 停并删容器；命名卷保留，数据还在
```

> **警告**：`docker compose down -v` 会**连命名卷一起删除**，内置 SQLite 的 `elder.db` 就找不回来了。
> 执行前请先按第 8 节备份。

### 1.5 数据文件在哪（宿主机侧）

```bash
docker volume inspect server_elder-data      # 看 Mountpoint，即卷在宿主机上的目录
docker compose exec elder-server ls -l /data # 容器视角
```

### 1.6 顺带起语音链路（可选）

```bash
docker compose --profile xiaozhi up -d
#  8000  小智 WebSocket 语音通道
#  8002  小智智控台
#  8003  OTA / 视觉分析
#  8064  本服务（业务面 + 管理后台）
```

### 1.7 不用 Docker 的本地开发

```bash
pip install -r requirements.txt
export ELDER_DB="sqlite:///./elder.db"    # 相对路径 = 当前工作目录
uvicorn app.main:app --reload --port 8080
```

不设 `ELDER_DB` 时，代码会自动挑一个可写目录：`/data` 存在且可写就用 `/data/elder.db`（容器里的命名卷），否则退回当前工作目录下的 `./elder.db`（本机裸跑）。持久化目录可用 `ELDER_DATA_DIR` 覆盖。

---

## 2. 接入 MySQL（完整步骤）

### 2.1 方式 A：让 Compose 起一个 MySQL 容器（overlay 叠加）

`docker-compose.mysql.yml` 是**叠加文件**，必须和主文件一起加载，不能单独使用：

```bash
cd server
cp .env.example .env
vi .env                                  # 改 MYSQL_ROOT_PASSWORD、ELDER_JWT_SECRET
docker compose -f docker-compose.yml -f docker-compose.mysql.yml up -d
```

后续所有子命令都要带同样的两个 `-f`，否则会按默认的 SQLite 形态重建：

```bash
docker compose -f docker-compose.yml -f docker-compose.mysql.yml logs -f elder-db
docker compose -f docker-compose.yml -f docker-compose.mysql.yml ps
docker compose -f docker-compose.yml -f docker-compose.mysql.yml down
```

嫌命令长可以起个别名，或写进 `.env` 同级的 `COMPOSE_FILE`：

```bash
export COMPOSE_FILE=docker-compose.yml:docker-compose.mysql.yml
docker compose up -d          # 之后所有命令都自动带这两个文件
```

叠加文件做三件事：

1. 把 `elder-server` 的 `ELDER_DB` 从 `sqlite:////data/elder.db` 换成 MySQL 连接串；
2. 补上 `elder-db`（`mysql:8.0`）服务与 `elder-db-data` 数据卷；
3. 给 `elder-server` 加 `depends_on: condition: service_healthy`，等数据库真正能查再启动。

### 2.2 方式 B：连外部已有的 MySQL（不另起数据库容器）

这是生产上更常见的形态：数据库用云厂商 RDS 或机房里已有实例。做法是**只覆盖 `ELDER_DB`**，不要叠加 `docker-compose.mysql.yml`（那个文件会另起一个 `elder-db` 容器）。

推荐用 `.env` 覆盖（Compose 会自动读取同目录 `.env`）：

```bash
# server/.env
ELDER_DB=mysql+pymysql://elder:StrongPass@10.0.0.7:3306/elder_companion?charset=utf8mb4
```

然后照常 `docker compose up -d` 即可。也可以临时覆盖：

```bash
ELDER_DB='mysql+pymysql://elder:StrongPass@10.0.0.7:3306/elder_companion?charset=utf8mb4' \
  docker compose up -d elder-server
```

外部实例需要先手工建库（本服务只建表，不建库）：

```sql
CREATE DATABASE elder_companion
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
CREATE USER 'elder'@'%' IDENTIFIED BY 'StrongPass';
GRANT ALL PRIVILEGES ON elder_companion.* TO 'elder'@'%';
FLUSH PRIVILEGES;
```

### 2.3 连接串格式

```
mysql+pymysql://<用户名>:<密码>@<主机>:<端口>/<库名>?charset=utf8mb4
```

示例：

| 场景 | 连接串 |
| --- | --- |
| Compose 自带容器 | `mysql+pymysql://root:123456@elder-db:3306/elder_companion?charset=utf8mb4` |
| 宿主机上的 MySQL | `mysql+pymysql://elder:Pass@host.docker.internal:3306/elder_companion?charset=utf8mb4` |
| 外部 RDS | `mysql+pymysql://elder:Pass@rm-xxxx.mysql.rds.aliyuncs.com:3306/elder_companion?charset=utf8mb4` |

注意事项：

- **密码里的特殊字符必须 URL 转义**，否则会被当成连接串的分隔符：`@`→`%40`、`:`→`%3A`、`/`→`%2F`、`#`→`%23`。
  例如密码 `p@ss:w` 要写成 `mysql+pymysql://elder:p%40ss%3Aw@host:3306/db?charset=utf8mb4`。
- **`?charset=utf8mb4` 不要省**：否则中文（昵称、提醒标题、提示词）会存成乱码。
- 从容器连宿主机上的 MySQL 用 `host.docker.internal`（Linux 上需要给容器加 `extra_hosts: ["host.docker.internal:host-gateway"]`）。

### 2.4 字符集与时区要求（重要）

服药提醒是**时间强相关**业务，字符集和时区错了会出两类隐性故障：中文乱码、提醒整体漂移 8 小时。

| 项 | 要求 | 怎么保证 |
| --- | --- | --- |
| 字符集 | `utf8mb4` + `utf8mb4_unicode_ci` | 自带容器已在 `command` 里指定；外部库按 2.2 的 SQL 建 |
| 连接字符集 | `utf8mb4` | 连接串带 `?charset=utf8mb4` |
| MySQL 时区 | `+08:00` | 自带容器已加 `--default-time-zone=+08:00`；外部实例请确认 `SELECT NOW()` 与北京时间一致 |
| 容器时区 | `Asia/Shanghai` | 两个 compose 文件都设了 `TZ: Asia/Shanghai` |
| 业务时区 | `Asia/Shanghai` | 环境变量 `ELDER_TZ`，默认值即 `Asia/Shanghai` |

外部实例自检：

```sql
SELECT NOW();                                   -- 应等于北京时间
SHOW VARIABLES LIKE 'character_set%';           -- 应为 utf8mb4
```

### 2.5 验证连通

```bash
# 1) 服务自己认为健康
curl -s http://<host>:8064/api/health

# 2) 容器内直连数据库（自带容器形态）
docker compose -f docker-compose.yml -f docker-compose.mysql.yml exec elder-db \
  mysql -h 127.0.0.1 -uroot -p"$MYSQL_ROOT_PASSWORD" -e "SHOW DATABASES;"

# 3) 从应用容器连外部实例
docker compose exec elder-server python - <<'PY'
import os
from sqlalchemy import create_engine, text
e = create_engine(os.environ["ELDER_DB"])
with e.connect() as c:
    print("NOW():", c.execute(text("SELECT NOW()")).scalar())
    print("tables:", [r[0] for r in c.execute(text("SHOW TABLES"))])
PY
```

第 3 步能打印出 16 张业务表（users / devices / reminders / avatars / membership_* 等）就说明接好了。

---

## 3. 从 SQLite 迁到 MySQL

> **这不是自动迁移。** 切换 `ELDER_DB` 只会让服务在新库上重新 `create_all` 建一批**空表**，
> 老数据一行都不会跟过去。必须自己导。

### 3.1 迁移前的准备

1. 选一个停机窗口（老人在用的设备会短暂拿不到服务端配置）。
2. **先备份 `elder.db`**（见第 8 节），备份好了再动手。
3. 确认 MySQL 侧字符集/时区符合 2.4。

### 3.2 推荐路径：空表启动 + 按表搬数据

```bash
# 1) 停服，避免迁移过程中还有写入
docker compose stop elder-server
# 2) 备份 SQLite
docker run --rm -v server_elder-data:/data -v "$PWD:/backup" busybox \
  cp /data/elder.db /backup/elder.db.bak

# 3) 切到 MySQL，让服务建空表（只起一次，确认日志里建表完成后停掉）
docker compose -f docker-compose.yml -f docker-compose.mysql.yml up -d
docker compose -f docker-compose.yml -f docker-compose.mysql.yml logs -f elder-server
docker compose -f docker-compose.yml -f docker-compose.mysql.yml stop elder-server

# 4) 搬数据（一次性脚本，见下）
# 5) 正式起服务
docker compose -f docker-compose.yml -f docker-compose.mysql.yml up -d
```

第 4 步的脚本（`server/` 目录下临时放一个，用完删掉）：

```python
# migrate_sqlite_to_mysql.py —— 一次性数据搬运，用完即弃
import os
from sqlalchemy import create_engine, MetaData, select, insert, text

SRC = "sqlite:////backup/elder.db"                 # 挂载进来的 SQLite 备份
DST = os.environ["ELDER_DB"]                       # 目标 MySQL 连接串

src = create_engine(SRC, connect_args={"check_same_thread": False})
dst = create_engine(DST)
s_meta, d_meta = MetaData(), MetaData()
s_meta.reflect(bind=src)
d_meta.reflect(bind=dst)

with src.connect() as s:
    with dst.begin() as d:
        # 表之间有外键，导入期间先关掉检查，导完再开
        d.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for name, dtable in d_meta.tables.items():
            if name not in s_meta.tables:
                print("跳过（源库没有）:", name)
                continue
            rows = [dict(r) for r in s.execute(select(s_meta.tables[name]))]
            if rows:
                d.execute(insert(dtable), rows)
            print(f"{name}: {len(rows)} 行")
        d.execute(text("SET FOREIGN_KEY_CHECKS=1"))
```

跑法（用 compose 起一个一次性容器，卷直接挂进去）：

```bash
docker compose -f docker-compose.yml -f docker-compose.mysql.yml run --rm \
  -v "$PWD:/work" -v server_elder-data:/backup \
  elder-server python /work/migrate_sqlite_to_mysql.py
```

脚本里的 `SRC` 指向挂载进来的 SQLite 备份，`DST` 直接用容器里的 `ELDER_DB`（此时已是 MySQL 连接串），
两个数据库都能被同一个网络访问到。

### 3.3 迁移后要检查的点

- 行数对得上：两边 `SELECT COUNT(*)` 逐表比对。
- 时间字段：SQLite 里存的 naive datetime 是北京时间，导入 MySQL 后应保持一致；抽查几条 `reminders` 的 `due_at`。
- 自增 ID：脚本保留了原 ID，用户/设备/提醒的主键不变，设备端不需要重新绑定。
- 中文没有变成 `??` 或乱码。

### 3.4 反向迁回（MySQL → SQLite）

同样的思路倒着来：先备份 `mysqldump`，再用 SQLite 新建空库导入。**MySQL 特有的类型（如 `DATETIME(6)`、`JSON`）在 SQLite 里会退化成文本**，回迁有损，能不回就别回。

---

## 4. 反向代理 / HTTPS

手机 APP 里的后端地址是**代码级写死**的 `https://voice.2508866.xyz`（`app/src/http/elder.ts` 的 `BASE_URL`），没有网络设置入口。所以对外域名只能是这个，Nginx 负责把 443 转到容器发布的 8064。

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name voice.2508866.xyz;

    ssl_certificate     /etc/letsencrypt/live/voice.2508866.xyz/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/voice.2508866.xyz/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:8064;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;      # 管理后台长轮询
        proxy_send_timeout 300s;
    }
}

# HTTP 全量跳 HTTPS
server {
    listen 80;
    server_name voice.2508866.xyz;
    return 301 https://$host$request_uri;
}
```

注意点：

- **`ELDER_CORS` 要跟着改**：`*` 在开发时方便，上线后建议写成 `https://voice.2508866.xyz`（多个来源用逗号分隔）。
  FastAPI 的 CORS 中间件在 `allow_origins=["*"]` 时不会带 `Allow-Credentials`，若前端要传凭证必须写明确来源。
- 反向代理之后，容器内仍然认为自己是 HTTP，不需要在应用侧开 TLS。
- 8064 不需要对公网开放，只监听 `127.0.0.1` 更安全：把 compose 里的端口映射改成 `127.0.0.1:${ELDER_HTTP_PORT:-8064}:8080`。

---

## 5. 环境变量对照表

变量写在 `server/.env`（从 `server/.env.example` 复制），Compose 会自动读取。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ELDER_DB` | 自动挑选的 SQLite 路径 | 数据库连接串。**留空 = 内置 SQLite**；填 `mysql+pymysql://...` = 用 MySQL |
| `ELDER_DATA_DIR` | `/data` | 内置 SQLite 的持久化目录；不存在/不可写时退回当前工作目录 |
| `ELDER_HTTP_PORT` | `8064` | 宿主机对外端口（容器内固定 8080） |
| `ELDER_PORT` | `8080` | 容器内监听端口，一般不改 |
| `ELDER_JWT_SECRET` | `please-change-this-secret-in-production` | JWT 签名密钥，**生产必须换成随机长字符串** |
| `ELDER_CORS` | `*` | 允许的跨域来源，逗号分隔；上线建议填 `https://voice.2508866.xyz` |
| `ELDER_DB_WAIT_SEC` | `180` | 启动阶段等待数据库可用的秒数（仅 MySQL 生效，SQLite 是本地文件无需等待） |
| `ELDER_TZ` | `Asia/Shanghai` | 业务时区，改了会整体影响提醒时间计算 |
| `MYSQL_ROOT_PASSWORD` | `123456` | 仅在使用自带 `elder-db` 容器时生效，**生产必须改** |
| `ELDER_ORDER_TTL_HOURS` | `48` | 会员订单（人工核销）占用有效期，超时置 `expired` 并释放号段与金额 |
| `ELDER_XIAOZHI_WS` | `ws://xiaozhi-server:8000/xiaozhi/v1/` | 下发给设备的语音通道地址 |
| `ELDER_XIAOZHI_OTA` | `http://xiaozhi-server:8003/xiaozhi/ota/` | 下发给设备的 OTA 地址 |
| `ELDER_SCHEDULE_AHEAD_HOURS` | `36` | 服务端预生成提醒实例的提前量 |
| `ELDER_DB_ECHO` | `0` | 设为 `1` 打印所有 SQL，排查问题时开，平时关 |

完整清单以 `server/app/config.py` 为准。

---

## 6. 排障

### 6.1 `(2013, 'Lost connection to MySQL server during query')`

这是本项目**真实踩过的坑**，只在 MySQL 形态出现。

**现象**：`elder-server` 启动即崩，日志里是

```
pymysql.err.OperationalError: (2013, 'Lost connection to MySQL server during query')
```

**成因**：`depends_on: condition: service_healthy` 只是说 healthcheck 通过了，但 healthcheck 通过 ≠ mysqld 已经能接受业务查询。MySQL 8 首次初始化数据目录时 mysqld 会**再重启一次**，在这段窗口里 pymysql 读握手包（`_get_server_information()`）就会被服务端掐断。Windows / WSL2 上这个窗口尤其宽（30~90 秒）。

**处理**：

1. 加大等待时间——应用侧自带重试，把 `ELDER_DB_WAIT_SEC` 提到 `300`：

   ```bash
   # server/.env
   ELDER_DB_WAIT_SEC=300
   ```
2. 确认 healthcheck 用的是**真实查询**而不是 `mysqladmin ping`：`docker-compose.mysql.yml` 里是
   `mysql -h 127.0.0.1 -uroot -p"$$MYSQL_ROOT_PASSWORD" -e "SELECT 1"`，`mysqladmin ping` 即使认证失败也返回 0，会误判就绪。
3. 看数据库自己的日志，确认它还在不在初始化：

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.mysql.yml logs -f elder-db
   # 看到 "ready for connections" 才是真的可以查了
   ```
4. 手动验证：待 `elder-db` 起来后执行
   `docker compose exec elder-db mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "SELECT 1"`。

### 6.2 端口 8064 被占用

```bash
docker compose logs elder-server | head -20     # Address already in use
```

换个宿主机端口即可（容器内仍是 8080）：

```bash
ELDER_HTTP_PORT=18064 docker compose up -d
```

或写进 `.env`。**注意**：若换了端口，Nginx 的 `proxy_pass` 也要跟着改。

### 6.3 SQLite：`unable to open database file`

`sqlite3.OperationalError: unable to open database file` 说明 `/data` 不存在或不可写。

- 用 bind mount 挂宿主机目录时，目录必须先存在且有权限：`mkdir -p ./data && chmod 777 ./data`（容器内以 root 运行，实际看宿主机侧权限）。
- Windows 上挂载 NTFS 目录偶发权限问题，优先用**命名卷**（默认形态）而不是 bind mount。

### 6.4 中文乱码

- 连接串少了 `?charset=utf8mb4`。
- 外部库的字符集不是 `utf8mb4`，按 2.4 检查并 `ALTER DATABASE ... CHARACTER SET utf8mb4`。

### 6.5 提醒时间差 8 小时

MySQL 时区和容器时区不一致。确认 `SELECT NOW()` 是北京时间，且 `TZ=Asia/Shanghai`、`--default-time-zone=+08:00` 都设了。

### 6.6 数据卷在哪 / 想直接看数据

```bash
docker volume ls | grep elder
docker volume inspect server_elder-data        # SQLite
docker volume inspect server_elder-db-data     # MySQL（若用了自带容器）
```

### 6.7 容器反复重启

```bash
docker compose logs --tail=200 elder-server
docker inspect --format '{{.State.ExitCode}}' elder-server
```

启动阶段自重试最长 `ELDER_DB_WAIT_SEC` 秒，超时才会放弃并退出；日志里会有
`等待数据库超时（共尝试 N 次，…s）` 的明确提示。

### 6.8 升级后报 `no such column: xxx` / `Unknown column 'xxx' in 'field list'`

说明老库缺新版本新增的列。补齐动作在启动时由 `app/schema_patch.py` 的
`ensure_missing_columns()` 完成（幂等，见 8.3），按顺序排查：

1. 看启动日志有没有走到 lifespan —— 容器若是启动即崩，补列根本没机会执行。
2. 确认该字段**已登记**在 `schema_patch.NEW_COLUMNS` 里：没登记的字段不会被补，
   这是最常见的原因（每加一个模型字段都要同步登记一行）。
3. 真补不上就手工补（以 `users.email` 为例）：

   ```sql
   -- MySQL
   ALTER TABLE users ADD COLUMN email VARCHAR(128) NOT NULL DEFAULT '';
   -- SQLite
   ALTER TABLE users ADD COLUMN email VARCHAR(128) NOT NULL DEFAULT '';
   ```

4. 补完重启：`docker compose restart elder-server`。

---

## 7. 健康检查与监控

- 容器自带 `HEALTHCHECK`，每 30 秒请求 `/api/health`，`docker compose ps` 看 `(healthy)`。
- 外部监控直接探 `http://<host>:8064/api/health`，返回 `{"ok":true}` 即正常。

### 7.1 后台定时任务

排期、订单过期、会员过期、离线清扫等由 lifespan 里起的 `scheduler` 后台任务驱动
（间隔 `ELDER_SCHEDULER_INTERVAL`，默认 60 秒一轮，预排期提前量 `ELDER_SCHEDULE_AHEAD_HOURS` 默认 36 小时）。

- 它挂掉**不会**让 API 不可用，所以 `/api/health` 仍然 200 —— 监控探活探不出定时任务半瘫。
  想确认它在跑，看日志里有没有持续出现的 `scheduler` 记录，或观察提醒实例是否按期生成。
- 一轮里某一步抛异常会带走**这一轮剩下的所有步骤**，所以日志里出现 scheduler 的 traceback
  要当回事处理，不要只看接口还能通就放过。

---

## 8. 数据备份与恢复

### 8.1 SQLite（默认形态）

**必须先停服务再复制文件**，否则可能拷到写了一半的库（SQLite 有 WAL，热拷有风险）。

```bash
# 备份
docker compose stop elder-server
docker run --rm -v server_elder-data:/data -v "$PWD:/backup" busybox \
  cp /data/elder.db /backup/elder.db.$(date +%Y%m%d)
docker compose start elder-server

# 恢复（同样先停服）
docker compose stop elder-server
docker run --rm -v server_elder-data:/data -v "$PWD:/backup" busybox \
  cp /backup/elder.db.20261015 /data/elder.db
docker compose start elder-server
```

### 8.2 MySQL（自带容器形态）

```bash
# 备份
docker compose -f docker-compose.yml -f docker-compose.mysql.yml exec elder-db \
  mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" \
    --single-transaction --routines --default-character-set=utf8mb4 \
    elder_companion > elder_companion.$(date +%Y%m%d).sql

# 恢复
docker compose -f docker-compose.yml -f docker-compose.mysql.yml exec -T elder-db \
  mysql -uroot -p"$MYSQL_ROOT_PASSWORD" elder_companion < elder_companion.20261015.sql
```

`--single-transaction` 让 mysqldump 在一致性快照里跑，不需要停服。

### 8.3 备份策略建议

- SQLite：每天停服快照一次，保留 7 天；家用单机足够了。
- MySQL：每天 `mysqldump` 一次，异地留存。
- 升级版本前**务必**先备份：表结构靠 `create_all` 自动建，**没有 Alembic，升级没有回滚脚本**。
- 新增列由 `server/app/schema_patch.py` 的 `ensure_missing_columns()` 在启动时**幂等**补齐。
  当前 `NEW_COLUMNS` 登记了 6 列（SQLite 类型 / MySQL 类型）：

  | 表 | 列 | SQLite | MySQL |
  | --- | --- | --- | --- |
  | `users` | `email` | `VARCHAR(128) DEFAULT ''` | `VARCHAR(128) DEFAULT ''` |
  | `devices` | `agent_id` | `INTEGER` | `INT NULL` |
  | `ai_providers` | `description` | `VARCHAR(255) DEFAULT ''` | `VARCHAR(255) DEFAULT ''` |
  | `ai_providers` | `system_prompt` | `TEXT DEFAULT ''` | `TEXT` |
  | `ai_providers` | `tts_voice` | `VARCHAR(64) DEFAULT ''` | `VARCHAR(64) DEFAULT ''` |
  | `ai_providers` | `is_default` | `BOOLEAN DEFAULT 0` | `TINYINT(1) DEFAULT 0` |

  **以后每新增一个模型字段，都要同步往 `schema_patch.NEW_COLUMNS` 里登记一行**
  （SQLite 与 MySQL 两种 DDL 都要写），否则老库升级上来会 `no such column` —— 这个坑踩过一次就够了。
  只补列、不改类型、不删列、不建索引；需要完整迁移能力时再考虑引入 Alembic。
