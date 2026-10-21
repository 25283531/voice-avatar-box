# 部署说明（Docker）

本文档覆盖 `server/` 的两种部署形态：

| 形态 | 适用场景 | 数据库 | 启动方式 |
| --- | --- | --- | --- |
| **默认（推荐先跑通）** | 单机 / 家用 / 快速验证 | **内置 SQLite**（零外部依赖，开箱即用） | `docker compose up -d` |
| MySQL | 多人共用、数据量大、需要外部运维工具 | MySQL 8（自带容器或外部实例） | 叠加 `docker-compose.mysql.yml` |

端口与对外地址是硬性约定，不要改：

- 容器内固定监听 **8080**，对外只发布 **8064**（`ELDER_HTTP_PORT` 可改宿主机侧端口）
- 生产地址 `https://152.136.230.132`（**IP 直连**，2026-10-18 起；域名 `voice.250886.xyz`
  未备案被厂商按 SNI 阻断，详见 [4.3 IP 直连部署](#43-ip-直连部署域名被按-sni-阻断的形态)）。
  手机 APP 与固件的地址是代码级写死的（`app/src/http/elder.ts` 的 `BASE_URL` /
  `CONFIG_OTA_URL`），没有设置入口
- ⚠️ **IP 直连的前提是服务器持有 SAN 含 `152.136.230.132` 的 IP 证书**
  （Let's Encrypt 2026-01-15 起支持，见 [4.3](#43-ip-直连部署域名被按-sni-阻断的形态)）。
  证书没就绪时，固件与 APP 会报「证书名不符」——这是预期的中间态，不是代码问题

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

首次启动会自动建表并写入种子数据（平台预设角色与其人格、默认形象「小暖」、陪聊计量配额、种子管理员 `admin` 等）。

> ⚠️ 这里曾写过种子里还有「会员套餐」。**会员制已取消（commit `e264200`）**，
> `membership_*` / `payment_config` 五张表由启动期的
> `schema_patch.drop_membership_tables()` 真正 DROP，不再是种子数据的一部分。

**种子管理员的初始口令是随机的**（`secrets.token_urlsafe(16)`），只在启动日志里出现**一次**，形如：

```
WARNING elder | 【安全】种子管理员已创建：用户名 admin，ELDER-INITIAL-PASSWORD=5B5PwvJQwCjKGCtK-LuIwQ
    ⚠️ 这是唯一的初始口令，请立即保存并登录后修改 —— 重启不会再显示，丢了只能靠 ELDER_RESET_ADMIN_PASSWORD 重置。
```

> ⚠️ **这条 WARNING 是唯一一次拿到初始口令的机会**，重启后无法复现，所以第一件事是：
> ```bash
> docker compose logs elder-server | grep ELDER-INITIAL-PASSWORD
> ```
> 想自己指定初始口令，部署前设 `ELDER_SEED_ADMIN_PASSWORD=<口令>`（见 §5.2）。
> 设了它则口令取该值，且**刻意不回显进日志**（它已经在 `.env` 里躺着，
> 再抄一份进日志只是多一个泄露面），只打一行
> `seed: 已按 ELDER_SEED_ADMIN_PASSWORD 创建种子管理员 admin（口令不打印）`。

> ⚠️ **硬编码弱口令 `admin/admin123` 已从 `main.py` 移除**（commit `bb5ca98`）——
> 现在不设 `ELDER_SEED_ADMIN_PASSWORD` 就一定是随机口令，代码里**不再存在**
> 任何 `admin123` 的兜底分支。所以「按文档装完用 admin/admin123 登进去」这条路
> 已经没有了；要么去日志里取随机口令，要么显式设该变量。

### 1.3 访问

| 入口 | 地址 |
| --- | --- |
| 管理后台 | `http://<host>:8064/` |
| 接口文档 | `http://<host>:8064/docs` |
| 健康检查 | `http://<host>:8064/api/health`（应返回 `{"ok":true,...}`） |

默认账号用户名 `admin`，口令见启动日志（§1.2）。**登录后立刻改密码**。

> ⚠️ **历史部署必看**：2026-10 之前的代码用硬编码常量 `admin123` 作种子管理员口令。
> 那段代码只在**空库**时执行，所以**已经跑起来的库里那个 admin 的口令至今仍是
> `admin123`** —— 升级代码不会自动改掉它。这一版会在每次启动时告警，
> 处置命令见 §5.2。

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

容器里库文件的路径**永远是 `/data/elder.db`**（compose 显式设了
`ELDER_DB=sqlite:////data/elder.db`，且 `/data` 是命名卷），本节下面这些新规则
**不影响容器**。

#### 不设 `ELDER_DB` 时落在哪（非容器 / 本机裸跑）

| 优先级 | 条件 | 落点 |
| --- | --- | --- |
| 1 | 显式 `ELDER_DB` | **原样使用**（不加工、不改写，生产与所有冒烟脚本都走这条） |
| 2 | 显式 `ELDER_DATA_DIR` | `<ELDER_DATA_DIR>/elder.db`；**目录不存在就建**，建不出来则启动失败（不再静默回落） |
| 3 | `/data` 已存在且可写 | `/data/elder.db`（容器形态，与历史行为逐字一致） |
| 4 | 上面都没有 | 用户数据目录：Windows `%LOCALAPPDATA%\voice-avatar-box\elder.db`；macOS `~/Library/Application Support/voice-avatar-box/elder.db`；Linux `$XDG_DATA_HOME/voice-avatar-box/elder.db`（缺省 `~/.local/share/voice-avatar-box/`） |

**本机裸跑时库不会再落在仓库目录里**（这是本次变更的目的）。但有一种情况会
**沿用**仓库里已有的旧库：`./elder.db` 或 `server/elder.db` **已经有数据**
（文件大小 > 0）时，服务继续用它，并在启动日志里打一条 WARNING：

```
WARNING elder.deploy | 【数据库】沿用仓库目录里已有的旧库（**未迁移**）：...\server\elder.db
INFO    elder.deploy | 【数据库】G:\...\server\elder.db（来源=legacy_cwd）
```

> 启动摘要里那行 `【数据库】<路径>（来源=<source>）` 是排障第一现场：
> 以前「我现在连的是哪个库」有三个来源却**没有一行日志**说最终用了哪个。
> `source` 取值：`explicit` / `data_dir` / `data_dir_default` /
> `legacy_cwd` / `legacy_server` / `new_default`。

> ⚠️ **0 字节的 `.db` 不算「有数据」**。仓库里曾经有几个 0 字节的空壳库，
> 它们会被忽略（新位置生效）。判断依据是 `size > 0`，不是「文件存在」。

#### 三种处置方式（都不丢数据）

1. **什么都不做**（推荐）：旧库原地继续被使用，只是多一条 WARNING。
   **回滚成本为 0**：把代码退回上一版，老代码看的还是那个路径。
2. **手工迁移**（想要整洁时，**仅停机时**执行）：

   ```bash
   docker compose stop elder-server                # 或停掉本机 uvicorn
   cp -p server/elder.db "$NEW_DIR/elder.db"       # 先拷，不要 mv
   # 用新路径起一次：ELDER_DB=sqlite:///$NEW_DIR/elder.db
   ```

   **验收判据不是「服务能起来」**（空库也能起来），而是三张表行数一致：

   ```bash
   for t in users devices reminders; do
     echo -n "$t old="; sqlite3 server/elder.db "select count(*) from $t"
     echo -n "$t new="; sqlite3 "$NEW_DIR/elder.db" "select count(*) from $t"
   done
   ```

   三对数字都相等后，才可以把原文件删掉。
3. **孤儿库**（`server/_lead_check.db` 之类历史手工产物）：全仓已无代码再写它们。
   处置权在数据所有者 —— 先确认「这是不是我要的」，**不要直接删**。

#### 两个都有数据的库并存 ⇒ 服务**拒绝启动**

如果你手工往新位置拷过一份库、旧位置的那份又还在，启动会直接失败并列出两条路径：

```
【启动失败】发现两个都有数据的 SQLite 库，拒绝替你猜：
    A: C:\Users\...\AppData\Local\voice-avatar-box\elder.db
    B: G:\...\server\elder.db
    二选一，显式指定后重启：
        ELDER_DB=sqlite:///G:/.../server/elder.db
```

为什么不挑「更新」的那个：挑错的后果是「服务起来了，但数据是空的」，
而且运行期零报错 —— 这比停一下服务难查得多。**显式指定**即可恢复。

### 1.6 语音链路（内置，无需额外操作）

语音链路是**本镜像自带**的（自研 WebSocket 服务端 `server/app/voice/`），
和 API 在同一个进程、**同一个端口**，随 `docker compose up -d` 一起就绪，没有第二个容器要起。

```
8064  本服务：业务 API + 管理后台 + 语音 WebSocket（/api/voice/v1/ws）+ OTA
```

> 历史版本曾额外拉一个 `xiaozhi-esp32-server` 官方容器跑语音链路（`--profile xiaozhi`），
> 那意味着角色、音色、记忆、大模型调用都在第三方进程里。**该 profile 已删除**，
> 现在只有 8064 一个端口。若你手上是旧部署，升级后请同步调整防火墙与反代规则：
> 放行 WebSocket 升级即可，不再需要 8000 / 8002 / 8003。
>
> **部署在反向代理 / Cloudflare Tunnel 后面时，还须显式声明部署模式并配对外地址**
> （`ELDER_DEPLOY_MODE=tunnel` + `ELDER_PUBLIC_BASE_URL=https://<域名>`），
> 否则设备会拿到 `ws://` 地址连不上 —— 完整解释、三种拓扑的决策表与"我该选哪种"
> 见 §4.2。旧变量 `ELDER_VOICE_PUBLIC_BASE` 仍然有效（向后兼容）。

### 1.7 设备配网（浏览器直连设备热点，不用装 APP）

设备第一次上电、或家里换了 Wi-Fi，需要给设备配网。**固件自带浏览器配网**，
不需要装 APP、也不需要本服务在线：设备没配过网时会自己开一个 Wi-Fi 热点，
并把热点名和配网网址直接打在屏幕上。

流程（让用户照做即可）：

1. **给设备通电、进入配网模式** —— 屏幕上会显示热点名和网址。
   热点名形如 `XiaoZhi-A1B2`（`XiaoZhi` 前缀 + MAC 后两位，**Z 大写**，与上游项目的
   `Xiaozhi-` 只差一个字母的大小写），网址固定是 `http://192.168.4.1`。
2. **用手机或电脑连上这个热点**（热点本身没有密码）。
3. **浏览器打开屏幕上那个网址**（`http://192.168.4.1`）。
4. **在页面里选家里的 Wi-Fi 名称、输入密码、提交**。
5. **配网成功后设备自动重启**，重启后连上家里的 Wi-Fi；随后就可以在用户端用激活码绑定它。

配网由固件完成（`78__esp-wifi-connect` 的 `wifi_configuration_ap` + `dns_server` 强制门户），
页面跑在设备自己的 `192.168.4.1` 上，**与部署的服务器无关**：
服务端此时不需要可达，日志里也不会有任何配网请求。服务端只负责配好网之后的绑定与对话。

排障：

| 现象 | 处理 |
| --- | --- |
| 连上热点后配网页面没自动弹出 | 手动在浏览器地址栏输入 `192.168.4.1`（多数手机的强制门户会自己弹，iOS / 部分 Android 需要关掉蜂窝数据再试） |
| 打不开 `192.168.4.1` | 确认连的是**设备热点**、不是家里的 Wi-Fi；确认没走代理 / VPN；换一个浏览器或换一台手机 |
| 页面上扫不到家里的 Wi-Fi | 设备只支持 **2.4GHz** Wi-Fi；把路由器双频合一（同名 2.4G/5G）暂时拆开，或让设备离路由器近一点 |
| 输完密码提交失败 / 重启后又回到配网模式 | 密码或 SSID 输错（注意大小写与特殊字符）；路由器隐藏了 SSID 或信号太弱也会这样——让设备离路由器近一点、把 SSID 显示出来再试 |
| 配网成功（连上了家里 Wi-Fi），但设备一直离线、连不上云 | 见下方两条 |

"配网成功但连不上云"通常是这两件事，和配网本身无关：

1. **固件 OTA 地址指向了别处**。设备连上 Wi-Fi 后会去 `CONFIG_OTA_URL` 换 WebSocket 地址与 token，
   出厂默认已指向本项目的 `https://152.136.230.132/api/voice/ota`。
   若刷的是早期固件，默认值可能仍指向第三方域名——OTA 请求（含 MAC / UUID / 板子信息）
   会发给第三方，而响应里的 WebSocket 地址与 token 也由第三方下发，等于把整条语音链路的控制权交出去。
   处置：改 `CONFIG_OTA_URL` 后重新编译，或写 NVS 里 `settings("wifi")` 的 `ota_url` 覆盖
   （见 `firmware/xiaozhi/main/ota.cc` 的 `GetCheckVersionUrl`，不必重新烧录）。
2. **服务端没放行 WebSocket 升级**。表现是注册、心跳都正常，一开口才断线，
   按 [6.9 设备配网正常、心跳也正常，但一开口说话就断线](#69-设备配网正常心跳也正常但一开口说话就断线) 排查反代。

### 1.8 配置邮件服务（找回密码 / 用户反馈依赖它）

> **这一步不做，用户就找不回密码。** APP 的找回密码、提交问题都走邮件，
> 而 SMTP 未配置时这两条链路会明确失败，不会静默假装成功。

SMTP **不在 compose 里配**，也不在 `.env` 里配 —— 存在数据库里，管理员登录后配一次即可：

```
管理后台 → 邮件设置 → 填 SMTP（host / port / 用户名 / 密码 / TLS / 发件人）→ 点「发送测试邮件」
```

配完**务必点一次「发送测试邮件」**。SMTP 配错的概率极高（端口 465/587 混淆、TLS 开关、
邮箱服务商要用授权码而非登录密码），这是唯一能立刻发现问题的方式。

常见组合：

| 服务商 | host | port | 加密 |
| --- | --- | --- | --- |
| QQ 邮箱 | `smtp.qq.com` | `465` | SSL |
| 163 邮箱 | `smtp.163.com` | `465` | SSL |
| 阿里云企业邮 | `smtp.qiye.aliyun.com` | `465` | SSL |
| Gmail | `smtp.gmail.com` | `587` | STARTTLS |

> ⚠️ 国内邮箱大多要用**授权码**（在邮箱网页端生成），不是你的登录密码。
> 这是最常见的配置失败原因。

**环境变量** `ELDER_MAIL_FAKE` 是给 CI / 本机测试用的：设为 `1` 时邮件不真发，
写进进程内发件箱，管理员可从 `GET /api/admin/mail/outbox` 读出来核对。
**生产必须留空或设 `0`**，否则用户点了找回密码永远收不到邮件。

排障见 [6.10 用户收不到密码重置邮件](#610-用户收不到密码重置邮件)。

### 1.9 不用 Docker 的本地开发

```bash
pip install -r requirements.txt
export ELDER_DB="sqlite:///./elder.db"    # 相对路径 = 当前工作目录
uvicorn app.main:app --reload --port 8080
```

不设 `ELDER_DB` 时**不要指望库落在当前工作目录**：落点由 §1.5 那张优先级表决定，
本机裸跑默认落到**用户数据目录**（Windows `%LOCALAPPDATA%\voice-avatar-box\elder.db`，
macOS `~/Library/Application Support/voice-avatar-box/elder.db`，
Linux `~/.local/share/voice-avatar-box/elder.db`）。
持久化目录可用 `ELDER_DATA_DIR` 覆盖（目录不存在会**建出来**，建不出来则启动失败）。

为什么不再默认 `./elder.db`：运行库写进 git 工作树后，`*.db` 被 `.gitignore` 遮住，
于是它**既不会被 review、又会被 `git clean -xdf` 一起删掉**。旧库的处理见 §1.5
（有数据就沿用，不自动迁移）。

启动日志里有一行 `【数据库】<路径>（来源=<source>）`，**先看它**再排障。

### 1.10 设备 OTA 固件分发（可选，**默认不启用**）

设备开机 OTA 检查（`/api/voice/ota`）时，服务端可以顺带告诉设备「有新版固件」。
**默认不返回 `firmware` 段**（= 设备永远不 OTA，与配置该特性之前逐字一致）；
只有下面两个变量**都配置**时才在响应里带上（已绑定 / 未绑定两条路径都会带）：

| 变量 | 说明 |
| --- | --- |
| `ELDER_FIRMWARE_VERSION` | 最新可用固件版本号，如 `2.0.5`。**真源是 `firmware/xiaozhi/CMakeLists.txt` 的 `PROJECT_VER`**，两边必须一致 |
| `ELDER_FIRMWARE_URL` | 固件下载地址（完整 URL），如 `https://152.136.230.132/firmware/xiaozhi-2.0.5.bin` |

配置示例（`server/.env` 或 compose 的 `environment`，二选一即可，改完重启服务）：

```bash
ELDER_FIRMWARE_VERSION=2.0.5
ELDER_FIRMWARE_URL=https://152.136.230.132/firmware/xiaozhi-2.0.5.bin
```

行为约定（配之前必须知道）：

1. **版本比较由固件自己做**（`firmware/xiaozhi/main/ota.cc` 的
   `IsNewVersionAvailable`），服务端只负责「报告最新可用版本」，不判断设备
   当前版本、不决定要不要升。配成旧版本号 = 设备认为无需升级，无副作用。
2. **URL 指向的必须是单 app 镜像（CI 产物 `xiaozhi.bin`），不是
   `merged-flash.bin`**。设备的 OTA 升级走 `Ota::Upgrade()` →
   `esp_ota_begin` / `esp_ota_write` 写 **OTA 分区**，只吃单 app 镜像；
   merged-flash 是整片 Flash 镜像（含 bootloader / 分区表），OTA 写不进去。
3. **二进制不放在本服务托管**。本服务只在 OTA 响应里返回一个 URL，文件放哪
   由部署方决定（Nginx 静态目录 / OSS / CDN 均可）。注意地址必须是**设备
   可达**的（内网设备给内网地址），建议 https。
4. **CI artifact 会过期**。GitHub Actions 产物有保留期限，生产环境要把
   artifact 下载后传到自己的稳定静态托管，**别把 CI 的临时 URL 配到这里** ——
   过期后设备每次开机都会尝试下载一个 404。
5. **两个变量只配一个 = 不返回 `firmware` 段**（配置不完整时宁可不返回，
   让设备保持现状，也不下一个缺字段的段让固件白做比较）。

---

### 1.11 配置和风天气（可选）

老人问"今天天气怎么样"时，服务端调和风天气取数据。**不配也能跑**，只是这
类问题会答"我查不到天气"。

原理、JWT 怎么签、缓存怎么命中，见 [和风天气接入](./和风天气接入.md)。
本节只讲**怎么配、配完怎么验、坑在哪**。

#### 1.11.1 五个必填变量

| 变量 | 值从哪来 | 说明 |
| --- | --- | --- |
| `ELDER_QWEATHER_HOST` | 控制台「设置 → API Host」 | 形如 `xxxxxx.re.qweatherapi.com`，**不要带 `https://`**（带代码会剥，但别指望） |
| `ELDER_QWEATHER_KEY_ID` | 控制台「项目管理 → 凭据 ID」 | JWT header 的 `kid` |
| `ELDER_QWEATHER_PROJECT_ID` | 控制台「项目管理 → 项目 ID」 | JWT payload 的 `sub` |
| `ELDER_QWEATHER_DEVELOPER_ID` | 控制台「账号 → 开发者 ID」 | JWT payload 的 `iss` |
| `ELDER_QWEATHER_PRIVATE_KEY_PATH` | 自己生成的 Ed25519 私钥路径 | **容器内路径**，见 1.11.2 |

可选调优（都有合理默认，不懂就别填）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ELDER_WEATHER_ENABLE` | `1` | 设 `0` 整体关闭，连定时预热都不跑 |
| `ELDER_WEATHER_TTL_MIN` | `60` | 缓存有效期（分钟）。**写 0 会被当无效值回落到 60** |
| `ELDER_WEATHER_DEFAULT_CITY` | `平顶山` | 老人没说是哪个城市、设备也没设过时用它 |
| `ELDER_WEATHER_GEO_TTL_DAYS` | `30` | 城市名 → LocationID 的缓存天数 |
| `ELDER_WEATHER_TIMEOUT_SEC` | `6` | 单次上游请求超时 |
| `ELDER_WEATHER_WARM_HOURS` | `24` | 预热只刷"最近这么多小时内被问过"的城市 |
| `ELDER_WEATHER_WARM_AHEAD_MIN` | `10` | 只刷"这么多分钟内就要过期"的城市 |

#### 1.11.2 私钥：不能放项目目录

项目目录要上传 GitHub，私钥进仓库等于把账号送人。所以：

```bash
# 1) 本地生成（只在部署机上做一次，别提交）
openssl genpkey -algorithm ed25519 -out qweather-ed25519-private.pem
openssl pkey -in qweather-ed25519-private.pem -pubout -out qweather-ed25519-public.pem

# 2) 公钥内容贴到和风控制台「项目管理 → 凭据 → 上传公钥」
cat qweather-ed25519-public.pem

# 3) 私钥传到服务器，放到项目目录**之外**
ssh root@<host> 'mkdir -p /opt/voice-avatar/secrets && chmod 700 /opt/voice-avatar/secrets'
scp qweather-ed25519-private.pem root@<host>:/opt/voice-avatar/secrets/
ssh root@<host> 'chmod 600 /opt/voice-avatar/secrets/qweather-ed25519-private.pem'
```

⚠️ **`chmod 600` 不是可选项**。私钥 world-readable 时，容器里任何一个进程
都能读走它去刷你的和风配额。

#### 1.11.3 挂载与注入（两个坑）

`docker-compose.yml` 里要做两件事，**缺一不可**：

```yaml
services:
  elder:
    volumes:
      # 坑 1：私钥必须挂进容器，且**只读**
      - /opt/voice-avatar/secrets:/app/secrets:ro
    environment:
      # 坑 2：environment 是**显式列表**，.env 里的变量不会自动进容器！
      - ELDER_QWEATHER_HOST=${ELDER_QWEATHER_HOST}
      - ELDER_QWEATHER_KEY_ID=${ELDER_QWEATHER_KEY_ID}
      - ELDER_QWEATHER_PROJECT_ID=${ELDER_QWEATHER_PROJECT_ID}
      - ELDER_QWEATHER_DEVELOPER_ID=${ELDER_QWEATHER_DEVELOPER_ID}
      - ELDER_QWEATHER_PRIVATE_KEY_PATH=/app/secrets/qweather-ed25519-private.pem
```

**坑 2 是本项目实际踩过的**：`.env` 只负责给 Compose 做变量替换，它**不会**
把变量塞进容器。表现是"`.env` 里明明写了，容器里 `printenv` 却没有"，
于是天气一直报未配置，而启动日志一切正常。

另一个常见误配：私钥路径写成宿主机路径（`/opt/voice-avatar/secrets/...`）。
容器里看到的是挂载点 `/app/secrets/...`，两者不是一回事。

#### 1.11.4 验证（两步，都要做）

接口需要登录态（`get_current_user`），先拿一个管理员 token：

```bash
TOKEN=$(curl -s -X POST http://<host>:8064/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<口令>"}' | jq -r .access_token)
```

第一步，看配置有没有生效、缓存里有什么：

```bash
curl -s http://<host>:8064/api/weather/status -H "Authorization: Bearer $TOKEN" | jq
```

期望 `configured: true`，以及 `cache_rows` / `hits` 计数（首次部署时是 0）。

第二步，**连打两次同一个城市**，看 `source` 有没有从 `api` 变成 `cache`：

```bash
for i in 1 2; do
  curl -s "http://<host>:8064/api/weather/now?city=平顶山" \
    -H "Authorization: Bearer $TOKEN" | jq '{source, stale, temp}'
done
```

- 第 1 次：`source: "api"`（真调了和风，花掉一次配额）
- 第 2 次：`source: "cache"`（命中 SQLite，**不花配额**）

**这一步是验收红线**：两次都是 `api` 说明缓存没生效，上线后会按每次问话
烧配额。反过来，第二次若 `stale: true`，说明命中的是**过期**缓存（上游挂了
的降级路径），不是正常命中。

#### 1.11.5 定时预热在做什么

调度器每 10 分钟跑一次，只做一件事：把「最近 24 小时被问过」且「10 分钟内
就要过期」的城市重新拉一遍。**不会拉全国**——没人问过的城市永远不会产生
任何 API 调用。冷门城市第一次被问时走实时调用，之后进缓存。

---

### 1.12 接入本机自建的 ASR / 向量化模型（Docker）

很多部署把语音识别与向量化放在**同一台服务器的另一个容器**里（本仓当前生产
就是这样：FunASR/SenseVoice 走 8082，FastEmbed/bge 走 8080）。它们不是
OpenAI 兼容协议，所以不是"填个 endpoint 就能用"——管理页里给了两个
**选中即可用**的预置。

#### 1.12.1 怎么配（真的只需要点一下）

管理后台 →「AI 接入」→ 新增：

1. 「平台」下拉选 **本地 ASR（FunASR/SenseVoice · 免密钥）** 或
   **本地向量化（FastEmbed/bge · 免密钥）**；
2. 点保存。

其他字段**一个都不用填**。保存时服务端会自动补齐（见下表），这些默认值的
唯一真源是 `server/app/model_catalog.py` 的 `LOCAL_SERVICE_DEFAULTS`，
前端与服务端读同一份，不会出现"前端说不用填、后端说缺地址"：

| 字段 | 自动补成什么 | 说明 |
| --- | --- | --- |
| 能力（kind） | `asr` / `embedding` | 选「本地 ASR」却停在「大模型」上是**存得进、用时报 no_config** 的经典坑，所以强制对齐 |
| 接口地址 | `http://127.0.0.1:8082` / `http://127.0.0.1:8080` | **仍然可改**，见 1.12.2 |
| 模型 | `sensevoice-small` / `bge-small-zh-v1.5` | 仅作展示：服务端按自己加载的模型工作，不看这个字段 |
| 平台（provider） | `funasr_local` / `fastembed_local` | 调用层据此选本地协议分支 |
| API Key | 清空 | 本地服务没有密钥这一说 |
| 超时 | 60s（ASR）/ 30s（向量化） | 本地模型首次推理慢，别用云端那档 30s 误判超时 |

另一种等价入口：新增时把「厂商 API 风格」选成
`本地 ASR（FunASR/SenseVoice，无需密钥）` / `本地向量化（FastEmbed/bge，无需密钥）`
—— 地址同样会自动补上。**建模时新旧两条路都试过**：smoke_models 的 I 组。

#### 1.12.2 Docker 部署的地址坑（本节的真正重点）

默认地址里的 `127.0.0.1` 是**助手容器里的 127.0.0.1**，不是宿主机的。
模型容器不在同一个 network namespace 时，三种改法按推荐顺序：

1. **同一个 compose 项目**：把接口地址改成服务名，如
   `http://funasr:8082` / `http://embedder:8080`（Docker 内网 DNS 解析）；
2. **同一宿主机、不同 compose 项目**：用桥接网关地址或宿主机在
   `docker0` 上的 IP（常见 `172.17.0.1`）；
3. **跨机部署**：填模型机的内网地址，如 `http://10.2.0.16:8082`
   （和风/向量化文档里给的就是这个形态）。

⚠️ 别填 `host.docker.internal` 之外的猜测写法，也别把整条接口地址
（`http://10.2.0.16:8082/asr`）粘进来 —— 本地协议是
`POST {接口地址}/asr`，多一级路径就是 404。

#### 1.12.3 验证（分两句说）

向量化可以立刻验：编辑那条配置 → 「试一下」→ 用途选 `文本向量化` →
返回三维度（512 维在真实服务上是 512）浮点数组即通。

语音识别**只能真机验**：test 端点刻意不真调 ASR（需要真实录音，伪造音频
只会得到空串，反而让人误以为配好了）。保存后直接对设备说一句话，
服务端日志出现 `ASR 跳过 ...` 之外没有告警、老人得到回应，即通。

想从命令行确认"选中的是哪一行"：

```bash
curl -s "http://<host>:8064/api/providers/resolve?usage=asr" \
  -H "Authorization: Bearer $TOKEN" | jq '{name, kind, endpoint, api_style}'
curl -s "http://<host>:8064/api/providers/resolve?usage=embed" \
  -H "Authorization: Bearer $TOKEN" | jq '{name, kind, endpoint, api_style}'
```

期望 `kind` 分别为 `asr` / `embedding`、`api_style` 为 `local_asr` /
`local_embed`。

#### 1.12.4 两个后续注意

- **换模型 = 可能换维度**：bge-small-zh-v1.5 是 512 维，换成 768 维模型后，
  旧记忆的语义检索会自动失效（维度不符不参与计算），需要重新入库。
  服务端不会报错，只是召回变差——这是维度混算的标准症状。
- **本地服务是串行的**：GGUF 版 FunASR 请求排队执行，高并发时延迟是
  "排队 × 单次"。老人场景一句话一轮，体感不到；若将来接多房间并发，
  要么加实例，要么把超时再调大。

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

第 3 步能打印出业务表（users / devices / reminders / avatars / ai_providers 等）就说明接好了。

> ⚠️ 文档里曾写过「membership_* 等 16 张表」。会员制已取消（commit `e264200`），
> 那五张表（`memberships` / `membership_plans` / `membership_orders` /
> `membership_amount_range` / `payment_config`）现由启动期的
> `schema_patch.drop_membership_tables()` 真正 DROP 掉。
> **迁库后请核对这些表确实不在了** —— 迁移常走 mysqldump，会把已废弃的表
> 一起搬过去；而 `schema_patch` 的删表逻辑按依赖顺序执行，在外键仍引用
> 目标表时可能需要先清 `users` 的关联数据。

---

### 2.6 可选：加本地语音服务（`docker-compose.local.yml`）

> 与 MySQL 无关，只是同属"叠加文件"这一类，所以放在这一节。
> SQLite 与 MySQL 两种形态都可以叠加它，也可以完全不叠。

想让 ASR / VAD 不出本机、且不依赖任何云厂商时，叠加本地服务文件：

```bash
cd server
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d
```

| 服务 | 是什么 | 体积 | 适合 |
| --- | --- | --- | --- |
| `local-asr` | FunASR / SenseVoice 中文识别，OpenAI 兼容端 | 约 254MB 模型，纯 CPU | 中文准确率约为 whisper-small 的 3 倍 |
| `local-vad` | Silero VAD 端点检测 | 2MB 级 ONNX | 老人句中长停顿不再被误判成「说完了」 |

**刻意不含本地中文 TTS**，这是从部署形态倒推的结论：当前生产环境是
iStoreOS 软路由（无独立显卡），而唯一「开源 + 中文强 + 可商用」的中文 TTS
（CosyVoice 2）需要 8GB 显存起。塞进软路由的实测表现会是「合成一句话要几十秒」，
对老人场景**比用云端 TTS 更糟**（设备一直不吭声）。TTS 维持 Edge TTS 零成本方案。

起容器之后仍需在管理后台「AI 接入」里手工加两行指向本地地址：

```text
kind=asr   endpoint=http://local-asr:10095/v1
kind=vad   endpoint=http://local-vad:8080
```

管理后台顶部的**服务就绪度**面板会显示当前缺哪几项、缺了会怎样，
不必数配置行来判断。首次启动要下模型，冷启动可能 30~60 秒。

> ⚠️ **就绪度面板的作用是"如实反映未就绪"，不是粉饰**。全新空库下
> asr / tts / vad / chat 四项都是未配置状态，`resolve_provider` 会返 503 ——
> **"不配置也能用"这个假设不成立**。关键项是 `asr` / `tts` / `chat`（缺任一
> 就无法提供完整服务）；VAD 不算关键项，断句已由设备端 esp-sr 承担。

本地服务的端口只绑定 `127.0.0.1`（`local-asr` 宿主 10095、`local-vad` 宿主 8081
→ 容器 8080），**不对外暴露**——这两个服务没有任何鉴权。上面的
`endpoint=http://local-vad:8080` 走的是 compose 内网（容器端口），
不是宿主机的 8081，主服务与它们在同一网络里不需要绕宿主端口。

> 这两个服务在 `docker-compose.local.yml` 里**没有 `profiles:` 键** ——
> 只要 `-f` 加载了这个叠加文件就会一起起，不需要额外的 profile 名。
> 反过来说，**不加载这个文件就一个都不会起**。

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

手机 APP 与固件里的后端地址是**代码级写死**的，没有网络设置入口：
`app/src/http/elder.ts` 的 `BASE_URL`（APP）与 `CONFIG_OTA_URL`（固件）。

**当前生产形态（2026-10-18 起）是 IP 直连**：域名 `voice.250886.xyz` 未备案，
被云厂商按 SNI 阻断，代码已全部改为 `https://152.136.230.132`。IP 直连形态的
完整部署步骤（IP 证书、Nginx 双证书、环境变量）见
[4.3 IP 直连部署](#43-ip-直连部署域名被按-sni-阻断的形态)。

下面这节是**域名形态**的配置（有域名、或备案恢复后的形态），IP 直连形态
请直接看 §4.3，两者只在证书与 `server_name` 上有区别，其余完全相同。

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name voice.250886.xyz;

    ssl_certificate     /etc/letsencrypt/live/voice.250886.xyz/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/voice.250886.xyz/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    client_max_body_size 20m;

    # ---------------------------------------------------------- 语音 WebSocket
    # ⚠️ 这一段不能省。语音链路和 API 共用 8064，设备会往
    #    /api/voice/v1/ws 发起 WebSocket 升级。Nginx 默认不转发 Upgrade 头，
    #    漏了这三行的表现是：设备能配网、能心跳（HTTP 正常），
    #    但一开口说话就断连，看日志是 200 而不是 101 —— 很容易误判成固件问题。
    #    另外 read_timeout 要放宽到远大于一次对话的长度，
    #    否则老人说到一半被 Nginx 掐掉连接。
    location /api/voice/ {
        proxy_pass http://127.0.0.1:8064;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;   # 一次语音对话可能很长；不要设成几十秒
        proxy_send_timeout 3600s;
        proxy_buffering off;        # Opus 音频不能攒着发，否则明显延迟
    }

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
    server_name voice.250886.xyz;
    return 301 https://$host$request_uri;
}
```

注意点：

- **`ELDER_CORS` 要跟着改**：`*` 在开发时方便，上线后建议写成实际对外地址（IP 直连形态是 `https://152.136.230.132`，域名形态是 `https://voice.250886.xyz`；多个来源用逗号分隔）。
  FastAPI 的 CORS 中间件在 `allow_origins=["*"]` 时不会带 `Allow-Credentials`，若前端要传凭证必须写明确来源。
- 反向代理之后，容器内仍然认为自己是 HTTP，不需要在应用侧开 TLS。
- **WebSocket 必须显式放行 Upgrade**：见上面 `location /api/voice/` 那段。这是本项目最容易踩的坑。
- **语音链路不需要单独的域名或端口**：它和 `/api/` 同源。若出于某种原因把它拆出去部署，才需要为它单独配对外地址。
- 8064 不需要对公网开放，只监听 `127.0.0.1` 更安全：把 compose 里的端口映射改成 `127.0.0.1:${ELDER_HTTP_PORT:-8064}:8080`。
  注意语音链路也随之走了反代，上面那段 WebSocket 配置就是必需的，不能偷懒直接暴露 8064。
- **反代（无论 Nginx 还是 Cloudflare Tunnel）下都要配对外地址**：当前镜像的 uvicorn 启动命令没有传 `--proxy-headers`，而 uvicorn 的 `X-Forwarded-Proto` 处理**只信任来自 127.0.0.1 的对端**（框架默认值）—— 生产里容器看到的 TCP 对端是 Docker 网桥网关（不是 127.0.0.1，查法见 §4.1.4），该头不被采纳，`request.url.scheme` 恒为 `http`，设备会拿到 `ws://` 地址。不要以为"反代传了 X-Forwarded-Proto 就没事"——完整解释见 §4.1.3，三种拓扑怎么配见 §4.2 的决策表。

---

### 4.1 Cloudflare Tunnel + iStoreOS 部署（家庭软路由形态）

> ⚠️ **本节已不是当前生产形态**：2026-10-18 起生产迁到国内 VPS 的 IP 直连
> （域名未备案被按 SNI 阻断），见 [§4.3](#43-ip-直连部署域名被按-sni-阻断的形态)。
> 本节保留作家庭软路由 + Tunnel 的完整参考，两节除了证书与 `server_name`
> 之外完全相同。
>
> 本节记录 2026-10 上线的家庭形态：**iStoreOS（OpenWrt 软路由）+ Docker + cloudflared**，
> 对外 `https://voice.250886.xyz`，源站 `http://127.0.0.1:8064`（容器内 8080）。
> 地址以仓库内配置为准（`firmware/xiaozhi/main/Kconfig.projbuild` 的 `CONFIG_OTA_URL`、
> `server/app/config.py` 的注释、`server/docker-compose.yml` 的示例），不要凭记忆改。

#### 4.1.1 链路形态

```
设备 ──wss://──> Cloudflare 边缘 ──(TLS 终止)──> cloudflared(iStoreOS 宿主机)
       ──http://──> 127.0.0.1:8064 ──> 容器 8080（uvicorn）
```

- TLS 在 Cloudflare 边缘终止；cloudflared 到源站走**明文 HTTP** 即可，源站不需要证书、不启 TLS。
- compose 端口建议只绑回环：`"127.0.0.1:${ELDER_HTTP_PORT:-8064}:8080"`。这样 8064 不对局域网开放，只有 cloudflared 能进来。
- **一个端口承载全部**：业务 API + 管理后台 + 语音 WebSocket（`/api/voice/v1/ws`）+ OTA（`/api/voice/ota`），不要开第二个端口。

#### 4.1.2 cloudflared 配置

`config.yml`（iStoreOS 上的路径以你的安装方式为准，常见 `/etc/cloudflared/config.yml`）：

```yaml
tunnel: <隧道 UUID>
credentials-file: /etc/cloudflared/<隧道 UUID>.json
ingress:
  - hostname: voice.250886.xyz
    service: http://127.0.0.1:8064
  - service: http_status:404
```

两个必须写对的点：

- **`service` 是 `http` 不是 `https`**。源站（uvicorn）监听明文，写 `https://` 会握手直接失败。
  也不要画蛇添足加 `noTLSVerify: true` —— 那是给"https 源站 + 自签名证书"用的，这里根本没有 https 源站。
- **不需要任何 WebSocket 专用开关**。cloudflared 默认转发 WebSocket 升级（101 Switching Protocols），
  没有"开 WS"这类选项。对照物是 Nginx —— 那边才需要显式 `proxy_set_header Upgrade ...`（见 §4），cloudflared 侧无此坑。

怎么确认 WS 真的过了隧道（抄走即用）：

```bash
# 1) HTTP 面：期望 {"ok":true,...}（IP 直连形态）
curl -s https://152.136.230.132/api/health

# 2) WS 升级面：不带设备 token，期望先看到 101，紧接着连接被服务端以 4401 关闭
#    （4401 = token 缺失，见 server/app/voice/session.py 的 CLOSE_UNAUTHORIZED；
#     服务端是"先 accept 再鉴权"，所以 101 一定会出现）。
#    看到 101 就证明 Nginx/cloudflared → 源站的升级链路是通的；
#    4401 是预期的，不代表配置有问题。
curl -si -N --max-time 5 \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  https://152.136.230.132/api/voice/v1/ws
```

#### 4.1.3 ⚠️ ws:// vs wss://：隧道形态下**必须**声明 `tunnel` 模式并配对外地址

设备心跳 `POST /api/device/sync` 的响应里，语音 WS 地址是服务端**现场推导**的
（`server/app/routers/device_api.py` 的 `_voice_endpoints()`）。推导逻辑由
**部署模式** `ELDER_DEPLOY_MODE` 决定（启动期校验与摘要见 `server/app/config.py`
的 `_deploy_startup_check`，三种模式的完整决策表见 §4.2）：

- `tunnel` / `direct` 且配了对外地址 -> 用配置的地址，与请求无关；
- 否则按 `request.url.scheme` 决定下发 `ws://` 还是 `wss://`，Host 取请求头。

坑在于：**uvicorn 对 `X-Forwarded-Proto` 的处理只信任来自 `127.0.0.1` 的对端**
（框架默认值 `proxy_headers=True, forwarded_allow_ips=127.0.0.1`；镜像 CMD 显式传了
`--proxy-headers --forwarded-allow-ips ${ELDER_TRUSTED_PROXY_IPS:-127.0.0.1}`，
不设该变量时与框架默认值逐字等价，存量部署升级不受影响）。本形态里容器看到的 TCP 对端是
**Docker 网桥网关**（cloudflared 在宿主机，查法见 §4.1.4），不在信任名单里
—— 即使 cloudflared 正确传了 `X-Forwarded-Proto: https`，uvicorn 眼里自己
永远是明文 HTTP。于是 `request.url.scheme` 恒为 `http`，设备拿到的是
`ws://<对外地址>/api/voice/v1/ws`：明文 WS 打到 443。表现是**设备能配网、
能心跳，一开口说话就断**，极易误判成固件问题。

解决办法（二选一，**推荐前者**）：

```bash
# ① 新变量（任何模式下都校验 scheme；WS 可单独配域名）
#    生产形态（2026-10-18 起，IP 直连）：
ELDER_DEPLOY_MODE=tunnel
ELDER_PUBLIC_BASE_URL=https://152.136.230.132
# ELDER_WS_PUBLIC_BASE_URL=wss://152.136.230.132   # 可选：WS 单独地址
#    域名形态（备案恢复后）把上面两行换成 voice.250886.xyz 即可，其余不变

# ② 旧变量（向后兼容，语义与历史上逐字一致；tunnel 模式下同样满足"有显式地址"）
# ELDER_VOICE_PUBLIC_BASE=https://152.136.230.132
```

注意：

- `ELDER_PUBLIC_BASE_URL` 必须给 `https://`（服务端会自动换成 `wss://` 下发给设备）；
  给 `ws://` / `ftp://` / 不带 scheme 会**启动直接失败**（报错点名是哪个变量）。
- `ELDER_WS_PUBLIC_BASE_URL` 必须给 `ws(s)://`；只配它不配 HTTP 地址也会启动失败
  （OTA 检查仍然需要 http(s) 地址）。
- 末尾不要带 `/`（代码会剥掉，但别依赖这个）。
- **`tunnel` 模式不配对外地址 = 容器启动失败**，这是刻意的：缺地址时服务端一切
  "正常"（心跳成功、管理台能开），只有设备一开口就断，运行期没有任何信号。
  用一次启动失败换掉一次线上静默故障。不设 `ELDER_DEPLOY_MODE` 则是 `auto`，
  行为与今天完全一致，存量部署升级不受影响。
- **同一个 `scheme` 坑还会打到管理后台**：Starlette 的 `redirect_slashes` 对
  `/api/admin/presets` 这类少结尾斜杠的请求发 307，`Location` 头的 scheme 取自
  `scope["scheme"]` —— 即上面这个被改写的值。于是浏览器在 HTTPS 页面上收到一个
  `http://` 的绝对跳转地址，**先按 Mixed Content 策略拦下来**（`Failed to fetch`），
  请求根本发不出去。处置见 §6.13。
- 改完 `docker compose up -d`（重建容器）生效；启动日志会打一行部署摘要
  （模式 / HTTP 地址 / WS 地址 / 设备将拿到的 ws URL），然后按 §4.1.2 的方法实测一次 WS。

#### 4.1.4 ⚠️ 登录限速与 X-Forwarded-For：别动 uvicorn 的 `--forwarded-allow-ips`

登录限速取客户端 IP 的逻辑在**应用层**（`server/app/ratelimit.py` 的 `client_ip()`），
默认**谁都不信**：只认 TCP 对端。只有 `ELDER_TRUSTED_PROXY_IPS` 显式列出的地址才解析
XFF，且**从右往左**跳过可信段、取第一个不可信地址（攻击者伪造的段在左侧，正好被跳过）。

对应到本形态：

- **绝不要**给 uvicorn 传 `--forwarded-allow-ips=*`。传 `*` 时 uvicorn 会把 XFF 最左段
  （请求方随便写的那段）当作对端 IP，限速器直接形同虚设。镜像 CMD 传的是
  `--forwarded-allow-ips ${ELDER_TRUSTED_PROXY_IPS:-127.0.0.1}`：只有回环对端
  （或显式填进 `ELDER_TRUSTED_PROXY_IPS` 的地址）的 XFF 会被框架采纳，其余一律交给
  应用层按可信名单从右往左解析 —— 这是刻意的最安全默认；要改 Dockerfile CMD 属于
  `server/**` 改动，需单独评审，不要在执行 docker run 时顺手加参数。
- cloudflared 跑在宿主机时，容器看到的 TCP 对端**不是 127.0.0.1**，而是 Docker 网桥网关
  （compose 网络 `server_elder-net` 的 Gateway，通常 `172.x.0.1`）。查法：

  ```bash
  docker network inspect server_elder-net -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}'
  ```

  想让限速按真实访客 IP 生效，就把该网关（或整段 CIDR）填进 `ELDER_TRUSTED_PROXY_IPS`
  （例如 `172.18.0.0/16`）。cloudflared 会在 XFF 链上追加真实客户端（以 cloudflared
  文档为准），应用层从右往左扫到的第一跳就是它；应用只解析 XFF，不读 `CF-Connecting-IP`。
  不填则所有请求共享同一个"IP"——限速退化成全局阈值（多人共用会互相误伤），
  但不会被伪造绕过。
- 若 cloudflared 跑在另一个容器里，填那个容器在 docker 网里的地址或网段，同理。

#### 4.1.5 Cloudflare 的 WebSocket 空闲超时（如实说明）

长连接空闲被 Cloudflare 侧断开是已知行为：设备长时间静默后再开口，可能发现 WS 已断。
**具体空闲阈值以 Cloudflare 官方文档为准**——本文刻意不写秒数，避免把不确定的数字
写成事实。处置方向是**重连**而不是改配置：

- 设备侧：断线后自动重连，建议指数退避（避免服务端重启时所有设备齐刷刷砸过来）。
  服务端无会话状态，重连即新会话，对老人无感知。
- 排查时先看 `docker compose logs -f elder-server` 里 WS 断开的时刻，与设备静默时长
  互相印证，再怀疑固件或网络。

#### 4.1.6 iStoreOS 特有注意点

- **路径与磁盘**：把整个 `server/` 目录放到 iStoreOS 的持久化盘上（例如 `/opt/voice-avatar-box/server`
  或你挂载的数据盘，**别放 `/tmp`**——重启会没），在该目录执行 `docker compose up -d`。
  镜像加构建缓存约 1~2 GB；SQLite 数据在命名卷 `elder-data`，`docker compose down -v`
  会连数据一起删（见 §1.4/§1.5）。
- **时区**：镜像内 `TZ=Asia/Shanghai` 且装了 tzdata（`server/Dockerfile`），容器内
  `ZoneInfo("Asia/Shanghai")` 恒为 +8，与宿主机时区无关 —— iStoreOS 默认 UTC 也不影响
  提醒时间。固件侧时间由心跳下发的 `server_time` 校准。
- **固件与服务的分工**（改地址时最容易漏）：
  - OTA 检查地址：编译期 `CONFIG_OTA_URL`（默认 `https://152.136.230.132/api/voice/ota`，
    见 `firmware/xiaozhi/main/Kconfig.projbuild`），设备运行时还可被 NVS 覆盖
    （`settings("wifi")` 的 `ota_url`，见 `firmware/xiaozhi/main/ota.cc` 的 `GetCheckVersionUrl`）。
  - 语音 WS 地址：每次心跳由服务端按 §4.1.3 的逻辑重新下发。
  - 所以换地址（域名 ↔ IP）要**三处一起改**：固件默认值或 NVS、部署模式对应的对外地址变量
    （`ELDER_PUBLIC_BASE_URL` / 旧变量 `ELDER_VOICE_PUBLIC_BASE`，
    WS 单独域名再加 `ELDER_WS_PUBLIC_BASE_URL`）、TLS 前置（Tunnel 主机名或 Nginx server）。
    漏一处的典型症状是"能心跳不能说话"或"OTA 打到旧地址"。
- cloudflared 在 iStoreOS 上的安装方式（opkg 包或官方二进制）与隧道创建
  （Cloudflare Zero Trust 后台）以各自官方文档为准，本文不复述可能过时的步骤。

---

### 4.2 三种拓扑决策表：我该选哪种（`ELDER_DEPLOY_MODE`）

同一个镜像跑在三种拓扑下，**服务端给设备下发的 ws/OTA 地址来源不同**。
`ELDER_DEPLOY_MODE` 把"当前是哪种拓扑"变成显式配置，启动时还会打一行摘要
（模式 / HTTP 地址 / WS 地址 / 设备将拿到的 ws URL）—— 排障第一现场。

#### 先看这两句，能省掉整张表的犹豫

> **`tunnel` 这个名字有误导性：它的判据是「拓扑」，不是「你用了什么工具」。**
> 真正的含义是 **TLS 在前置终止、源站只收到明文 HTTP**。
>
> **所以：你在前面挂了 Nginx / Caddy / OpenResty / Cloudflare Tunnel 任何一个，
> 哪怕完全没用 Cloudflare Tunnel，也应该用 `tunnel`。**
> 名字里带"tunnel"只是因为本项目最早用它，不代表它是唯一适用场景。

一句话判据：

```
你的后端容器收到的请求，是 http:// 还是 https://？
  收到 https://                  → direct（或 auto，按请求推导就够）
  收到 http://，但对外是域名 HTTPS → tunnel ← 必须配 ELDER_PUBLIC_BASE_URL
```

不确定就看启动日志那行摘要，它会明写「设备将拿到的 ws URL」。

| 拓扑 | `ELDER_DEPLOY_MODE` | 还要配什么 | 设备拿到的地址 | 主要坑 |
| --- | --- | --- | --- | --- |
| **TLS 在前置终止**（Cloudflare Tunnel / Nginx / Caddy / OpenResty —— **工具不限**） | `tunnel` | `ELDER_PUBLIC_BASE_URL=https://<域名>`（**必需**，缺了启动失败）；`ELDER_WS_PUBLIC_BASE_URL=wss://<WS域名>`（可选） | 配置的地址，与请求无关；WS 域名分开配时两者各自生效 | uvicorn 不信任非回环对端的 `X-Forwarded-Proto`，自动推导会给出 `ws://`；用 Cloudflare 时长连接可能被空闲断开（§4.1.5） |
| **公网直暴露**（有公网 IP，对外 `http://IP:8064`） | `direct`（或留空 = `auto`） | 不用配对外地址 | 按请求 Host 推导：`ws://IP:8064/...`、`http://IP:8064/...` | 明文 HTTP：流量、登录口令、设备 token 全部裸奔；谁扫到这个端口都能打你的 AI token（建议同时 `ELDER_ALLOW_PUBLIC_REGISTER=0`）。详见下方"直暴露的风险" |
| **有头域的反代**（自管 Nginx/Caddy，TLS 在反代上终止） | `tunnel` + 对外地址（**推荐**）；反代确实把源站当明文 HTTP 用时 `direct` 即可 | 同第一行 | 配了就用配置的；没配按请求推导 | 与隧道同源的问题：TLS 在前置终止 ⇒ scheme 推导不可信。别指望反代传了 `X-Forwarded-Proto` 就没事（§4.1.3） |

**取舍：为什么"实在解决不了就走公网直暴露"是下策**

- 隧道（当前生产形态）不暴露任何入站端口，TLS 与证书由 Cloudflare 管，
  家里没有公网 IP 也能上线 —— 这是它作为**首选**的理由。
- 公网直暴露只需要一台有公网 IP 的服务器，配置最简单，但代价是明文：
  ① 任何中间人都能看/改流量（含管理员登录口令与设备 token）；
  ② 端口一扫就能定位到服务，注册接口匿名可达（不关注册的话），
  陌生人可以注册账号消耗你的 AI token、把老人设备挤下线；
  ③ `ws://` 在部分网络环境下会被中间设备劫持或阻断。
  如果只是**临时验证**、或内网/VPN 内使用，直暴露是可接受的；
  长期给老人用，请回到隧道，或至少给直暴露套一层 TLS 反代（那就变成第三行）。
- 一条实用判据：**先试隧道**。按 §4.1 配好 cloudflared + 两行环境变量，
  启动日志会明确告诉你设备将拿到什么地址；只有隧道因网络环境（如运营商干扰）
  确实建不起来时，再退到公网服务器直暴露，并接受上面的风险。

**新旧变量优先级（不静默改语义）**

| 地址 | 来源（从左到右优先级递减） |
| --- | --- |
| HTTP/OTA 对外地址 | `ELDER_PUBLIC_BASE_URL` > `ELDER_VOICE_PUBLIC_BASE`（旧逃生舱，语义逐字未变） |
| WS 对外地址 | `ELDER_WS_PUBLIC_BASE_URL` > 由 HTTP 地址换 scheme 推导（https→wss / http→ws） |

- 两者都配时以新变量为准，并打 WARNING 提醒删掉旧变量（两处并存 = 下次换域名
  必漏一处）。**旧变量没有废弃**：存量部署原样生效；迁移路径就是把
  `ELDER_VOICE_PUBLIC_BASE=x` 改成 `ELDER_PUBLIC_BASE_URL=x`（值不变，
  区别只是新变量在任何模式下都校验 scheme、且支持 WS 单独域名）。
- `auto`（默认，不设该变量）= 历史上的行为：没配对外地址就按请求推导，
  启动日志说明推断依据。**存量部署不设任何新变量，升级后行为逐字不变**
  （回归证明见 `tools/smoke_deploy.py` 的 H 组：与 git 基线逐字比对）。

**启动期校验（配错直接失败，不静默降级）**

| 配置 | 结果 |
| --- | --- |
| `tunnel` 但没有任何对外地址 | **启动失败**，报错点名 `ELDER_DEPLOY_MODE=tunnel` 与 `ELDER_PUBLIC_BASE_URL` |
| 配了 `ELDER_WS_PUBLIC_BASE_URL` 却没配 HTTP 地址 | **启动失败**（OTA 仍需要 http(s) 地址） |
| `ELDER_DEPLOY_MODE` 写成 `cloudflare` 等 | **启动失败**，列出合法值 `auto / direct / tunnel` |
| `ELDER_PUBLIC_BASE_URL` 不是 http(s) / `ELDER_WS_PUBLIC_BASE_URL` 不是 ws(s) | **启动失败**，点名是哪个变量、哪里不合法 |
| `tunnel` 下旧变量给了 `wss://` | **启动失败**（auto/direct 下旧变量不校验，保持历史语义） |

**⚠️ 三个地址变量的校验强度不一样（最容易踩的坑就在这里）**

| 变量 | `auto` / `direct` | `tunnel` | 说明 |
| --- | --- | --- | --- |
| `ELDER_PUBLIC_BASE_URL`（新） | **校验** scheme，必须 http(s):// | **校验**，且缺了直接启动失败 | 新变量没有历史语义要保，所以**任何模式下都严格校验**。推荐用它 |
| `ELDER_VOICE_PUBLIC_BASE`（旧） | **故意不校验** scheme（逐字保持历史宽松语义） | **强制必须是 https**，给 `wss://` 直接启动失败 | 加校验就改了存量部署的语义，所以只在显式 opt-in 的 tunnel 下严格 |
| `ELDER_WS_PUBLIC_BASE_URL` | 校验 ws(s):// | 同左 | 只配它不配 HTTP 地址 = 启动失败（OTA 仍需要 http(s) 地址） |

**这张表解释的那个真实坑**：「我配了旧变量、`direct` 模式跑得好好的，
怎么一换成隧道就连不上」—— 因为旧变量在 `direct` 下**根本不检查 scheme**，
你填 `wss://voice.250886.xyz` 它照收不误、照跑不误；直到你把
`ELDER_DEPLOY_MODE` 改成 `tunnel`，才会在启动时被拦下来。
**这不是 bug，是刻意的**（不校验是为了不打断存量部署），但必须知道。

`tunnel` 缺地址是**启动失败**而不是「告警后继续跑」，也是刻意的 fail-fast：
缺地址时服务端一切「正常」（心跳成功、管理台能开、日志干净），只有设备一开口
就断，**运行期零信号**。用一次启动失败换掉一次线上静默故障。
不设 `ELDER_DEPLOY_MODE` 则是 `auto`，行为与历史逐字一致，存量部署升级不受影响。

冒烟覆盖：`tools/smoke_deploy.py`（86 条断言：三模式推导逐条断言、启动失败
必红、auto/direct 与 git 基线逐字一致、反向验证三处）。

---

## 4.3 IP 直连部署（域名被按 SNI 阻断的形态）

> **这是 2026-10-18 起的本项目生产形态。** 域名 `voice.250886.xyz` 未备案，
> 被云厂商按 SNI 阻断；APP 与固件已代码级改为直连服务器 IP
> `https://152.136.230.132`。本节讲这形态与域名形态的差异与操作步骤。

### 4.3.1 为什么是 IP 直连（证据链）

阻断不是网络层不可达，而是厂商 DPI 按 TLS ClientHello 里的 **SNI** 杀连接
（2026-10-18 在用户本机实测）：

| 探针 | 结果 | 说明 |
| --- | --- | --- |
| `Test-NetConnection 152.136.230.132 -Port 443` | **True** | TCP 层通，没有 IP 阻断 |
| `openssl s_client -connect 152.136.230.132:443`（连 IP、**无 SNI**） | **TLS 1.3 握手成功，证书链 Verification: OK** | 不带 SNI 的 TLS 连接畅通 |
| `openssl s_client -connect voice.250886.xyz:443 -servername voice.250886.xyz` | `write:errno=113`，连接被杀 | 带域名 SNI 立即被杀 |
| 浏览器访问域名 | 时通时断 | Chrome 走 HTTP/3（QUIC）时 SNI 被加密、DPI 看不见 ⇒ 通；回落到 TCP+TLS 就被杀 ⇒ 断。这个现象本身就是 SNI 判据的佐证 |

结论：**换 IP 直连（不带 SNI）确实能绕过阻断**，HTTPS 完全可用。
⚠️ 用户本机 `curl.exe` 报 `CRYPT_E_REVOCATION_OFFLINE` 与阻断无关——那是
Windows Schannel 握过手后去查证书吊销状态（CRL）连不上吊销服务器；
openssl 默认不查所以显示 OK。真实客户端（ESP32 mbedtls、Android OkHttp）
不做 CRL 检查，不受影响。

### 4.3.2 IP 证书是硬前提（不是可选项）

固件的 TLS 实现（托管组件 `78__esp-ml307` 的 `esp_ssl.cc`）用系统证书包
验证证书链、**并按 URL 主机名校验证书**。连 IP 时拿 IP 去比对域名证书的
主机名 ⇒ 必然失败。所以服务器**必须持有 SAN 含 `152.136.230.132` 的
IP 证书**，否则固件与 APP（OkHttp 同样做主机名校验）都会报「证书名不符」。

Let's Encrypt 自 2026-01-15 起正式发行 IP 证书（GA），要点：

- IPv4/IPv6 均可；**有效期只有 160 小时（约 6 天）**，必须选 `shortlived` profile
- 验证方式只支持 **http-01 / tls-alpn-01**（没有 DNS-01、没有通配符）
- Certbot 5.4+ 支持 `--ip-address`；`webroot` / `standalone` / `manual` 插件可用，
  **`--nginx` / `--apache` 插件尚不支持**，需自己配 reload hook

申请命令（80 端口要可达，LE 走 http-01 验证）：

```bash
# webroot 方式（80 已有服务在跑时用；目录要和 Nginx 的 ACME challenge location 对上）
sudo certbot certonly --preferred-profile shortlived \
  --webroot --webroot-path /var/www/certbot \
  --ip-address --key-type ecdsa \
  -d 152.136.230.132 --deploy-hook "systemctl reload nginx"
```

⚠️ 160 小时有效期 ⇒ **续期必须自动化**，`--deploy-hook` 里 reload Nginx；
忘了续期的表现是所有设备在第 6 天开始证书过期报错。续期频率建议
`--cert-name` 固定 + systemd timer 每天两次（Let's Encrypt 建议）。

### 4.3.3 Nginx：default_server + 双证书

IP 直连时客户端**不带 SNI**，TLS 握手落到 `default_server`。
所以 default_server 必须挂 **IP 证书**；域名 server（带 SNI 的请求才落到它）
继续用域名证书——两个 server 并存：

```nginx
# 不带 SNI 的连接（IP 直连的 APP/固件/curl）落到这里 ⇒ 必须是 IP 证书
server {
    listen 443 ssl default_server;
    http2 on;
    server_name 152.136.230.132;

    ssl_certificate     /etc/letsencrypt/live/152.136.230.132/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/152.136.230.132/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    client_max_body_size 20m;

    # ---------------------------------------------------------- 语音 WebSocket
    # ⚠️ 这一段不能省。语音链路和 API 共用 8064，设备会往
    #    /api/voice/v1/ws 发起 WebSocket 升级。Nginx 默认不转发 Upgrade 头，
    #    漏了这三行的表现是：设备能配网、能心跳（HTTP 正常），
    #    但一开口说话就断连，看日志是 200 而不是 101 —— 很容易误判成固件问题。
    location /api/voice/ {
        proxy_pass http://127.0.0.1:8064;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;   # 一次语音对话可能很长；不要设成几十秒
        proxy_send_timeout 3600s;
        proxy_buffering off;        # Opus 音频不能攒着发，否则明显延迟
    }

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

# 带 SNI=voice.250886.xyz 的连接（浏览器、备案恢复后的 APP/固件）落到这里 ⇒ 域名证书
server {
    listen 443 ssl;
    http2 on;
    server_name voice.250886.xyz;

    ssl_certificate     /etc/letsencrypt/live/voice.250886.xyz/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/voice.250886.xyz/privkey.pem;

    # location 段与上面完全相同（可抽成 include 文件共享）
    include /etc/nginx/elder_locations.conf;
}

# HTTP 全量跳 HTTPS（IP 直连形态下浏览器输 http://IP 也会被跳转）
server {
    listen 80 default_server;
    return 301 https://$host$request_uri;
}

# ACME http-01 验证用（IP 证书续期靠它）
location /.well-known/acme-challenge/ {
    root /var/www/certbot;
}
```

（80 server 与 ACME location 二选一即可，看 certbot 用的是 webroot 还是 standalone。）

### 4.3.4 容器侧环境变量

```bash
ELDER_DEPLOY_MODE=tunnel
ELDER_PUBLIC_BASE_URL=https://152.136.230.132
ELDER_JWT_SECRET=<随机长字符串>
ELDER_SEED_ADMIN_PASSWORD=<强口令>               # 首启必填，不填则随机化
```

⚠️ `ELDER_PUBLIC_BASE_URL` 用 **https://** 开头（不是 `wss://`、不带路径尾斜杠）。
WS 地址由它自动换 scheme 推导成 `wss://152.136.230.132/api/voice/v1/ws`。
IP 直连下 Host 头就是 IP ⇒ auto 模式按请求推导也会得到 IP 地址，但
**tunnel 模式显式配置仍是推荐做法**（推导依赖请求头，遇到畸形请求会给出错地址）。

### 4.3.5 备选方案：连 IP、按域名验证书（不申请 IP 证书）

如果暂时不想申请 IP 证书，另一个办法是让客户端「连 IP、但按域名校验证书」：

| 端 | 做法 | 代价 |
| --- | --- | --- |
| Android APP | 自定义 `HostnameVerifier`，把 `152.136.230.132` 映射回 `voice.250886.xyz` 校验 | 需要改 APP 代码并重新出包；**与 IP 证书方案互斥**（有 IP 证书时域名证书就不在 443 默认路径上了） |
| ESP32 固件 | `esp_ssl.cc` 的 esp-tls 按 URL 主机名校验，无法只改配置绕过 | **不可行**，除非改托管组件 |

结论：固件这一端没有「免 IP 证书」的选项，**IP 证书是必选项**。

### 4.3.6 备案之后怎么切回域名

域名备案恢复后，SNI 阻断解除，按以下顺序切换（**服务端先行，客户端最后**）：

1. 服务器确认域名证书就位、Nginx 域名 server 生效（`openssl s_client
   -connect voice.250886.xyz:443 -servername voice.250886.xyz` 通过）
2. 改代码三处真源回域名：`app/src/http/elder.ts` 的 `BASE_URL`、
   `firmware/xiaozhi/main/Kconfig.projbuild` 的 `CONFIG_OTA_URL`、
   `firmware/components/elder/Kconfig.projbuild` 的 `ELDER_SERVER_URL`
   （后者在 `sdkconfig.defaults` 与 `tools/merge_to_xiaozhi.py` 还有两份同源副本）
3. 重打包 APP、重烧固件（存量设备可先用 NVS `ota_url` 覆盖顶一阵）
4. `ELDER_PUBLIC_BASE_URL` 改回 `https://voice.250886.xyz`
5. CI 两条工作流的白名单已同时放行 IP 与域名（firmware-build 的 OTA 校验、
   app-build 的产物地址校验），无需改 CI

⚠️ **不要在证书没就绪前就切客户端**：固件/APP 会报证书名不符，
表现为「OTA 检查失败」而非提示证书问题，很难归因。

---

## 4.4 公网 VPS + 自管 TLS 证书（不用 Cloudflare Tunnel）

> ⚠️ **当前国内 VPS 生产形态见 §4.3（IP 直连）**：证书来源不同（IP 证书而非
> 域名证书）、Nginx 要 default_server 双证书。本节讲的是**域名形态**下的
> VPS 自管证书（备案恢复后 / 有其他域名时）。

这一形态的要点与 §4.1 相同（**都是「TLS 在前置终止」**），但证书来源不同，
所以单独写一节，避免被误以为「不部署 Tunnel 就跟文档不一样」。

> **结论先说：`ELDER_DEPLOY_MODE=tunnel` 依然要设。**
> 你有没有用 Cloudflare Tunnel，和这个值**没有任何关系**。

### 架构

```
设备 / 浏览器 ──HTTPS 443──> Caddy 或 Nginx（宿主机，做 TLS 终止）
                             └─> http://127.0.0.1:8064 ──> 容器 8080
```

后端容器收到的永远是 `http://` ⇒ **必须显式配对外地址**，否则设备拿到
`ws://<域名>` 去打 443 ⇒ **能心跳、不能说话，且日志一片干净**。

### 容器侧环境变量

```bash
ELDER_DEPLOY_MODE=tunnel
ELDER_PUBLIC_BASE_URL=https://voice.example.com   # 你自己的域名
ELDER_JWT_SECRET=<随机长字符串>
ELDER_SEED_ADMIN_PASSWORD=<强口令>               # 首启必填，不填则随机化
```

⚠️ `ELDER_PUBLIC_BASE_URL` 用 **https://** 开头（不是 `wss://`、不带路径尾斜杠）。
WS 地址由它自动换 scheme 推导成 `wss://voice.example.com/api/voice/v1/ws`。

### 证书怎么来

| 方式 | 说明 |
| --- | --- |
| **Caddy 自动签发（推荐）** | Caddy 自动完成 ACME 挑战与续期，配置里只要有域名和一份指向自己的邮箱即可。**80 端口必须可达**（ACME HTTP-01 验证要用）。 |
| Nginx + Certbot | `certbot --nginx -d voice.example.com`，证书 90 天有效期，需配续期定时任务。 |
| 自带证书 | Nginx `ssl_certificate` 指向自己的 `.crt/.key`，注意证书链要完整（缺中间证书部分客户端会报错）。 |

### 三个最容易踩的点

1. **443 证书 ≠ 容器的事**。后端容器只监听 HTTP，TLS 完全由前置代理负责。
   容器映射仍是 `8064:8080`，**不要**试图在容器里做 TLS。
2. **别用 `--forwarded-allow-ips=*`**。那会让 uvicorn 采信可伪造的
   `X-Forwarded-Proto` 最左段，登录限速器随之形同虚设（见 §5.3）。
   要让 uvicorn 信任反代，用 `ELDER_TRUSTED_PROXY_IPS` 显式列出反代 IP。
3. **80/443 都要放通**，另确认云厂商安全组与宿主防火墙两处都开了
   （只开一层是最常见的「证书签发失败」原因）。

### 验证是否配对了

启动日志里这几行就是判据（**认准 `设备心跳将拿到：ws=` 后面是 `wss://` 还是 `ws://`**）：

```
【部署】模式：tunnel（TLS 在前置终止：Cloudflare Tunnel / Nginx / Caddy 都适用）
【部署】   HTTP/OTA 对外地址：https://voice.example.com（来源 ELDER_PUBLIC_BASE_URL）
【部署】   WS 对外地址：wss://voice.example.com （由 HTTP 地址换 scheme 推导）
【部署】   设备心跳将拿到：ws=wss://voice.example.com/api/voice/v1/ws  ota=https://voice.example.com/api/voice/ota
```

看到 **`ws=wss://`** 就是配对了。若看到 `ws=ws://`（或看到「未配置 → 按请求推导」）
就是两个变量没配上。

设备侧实测：先看设备能否完成 `/api/device/sync` 心跳（能），再让老人说一句话
—— **能心跳但不能说话，就是这一节的故障**（而不是网络不通）。

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
| `ELDER_CORS` | `*` | 允许的跨域来源，逗号分隔；上线建议填实际对外地址（IP 直连 `https://152.136.230.132`，域名形态 `https://voice.250886.xyz`） |
| `ELDER_DB_WAIT_SEC` | `180` | 启动阶段等待数据库可用的秒数（仅 MySQL 生效，SQLite 是本地文件无需等待） |
| `ELDER_TZ` | `Asia/Shanghai` | 业务时区，改了会整体影响提醒时间计算 |
| `MYSQL_ROOT_PASSWORD` | `123456` | 仅在使用自带 `elder-db` 容器时生效，**生产必须改** |
| `ELDER_VOICE_PUBLIC_BASE` | 留空（自动推导） | 语音链路对外地址（**旧变量，向后兼容，语义逐字未变**）。自研链路与 API 同源同端口，未配时服务端按请求 Host 推导；反代（含 Cloudflare Tunnel）下**必须**有显式对外地址（新变量或本变量），否则设备拿到 `ws://`。与 `ELDER_PUBLIC_BASE_URL` 同时配置时以新变量为准并 WARNING。见 §4.2 |
| `ELDER_DEPLOY_MODE` | `auto`（未设置） | **部署模式**：`auto` / `direct` / `tunnel`（大小写不敏感）。⚠️ **`tunnel` 的判据是拓扑不是工具** =「TLS 在前置终止、源站只收到明文」，**Nginx / Caddy / Cloudflare Tunnel 一律适用，不用 Cloudflare Tunnel 也该设 `tunnel`**；必须配 `ELDER_PUBLIC_BASE_URL`否则启动失败。`direct` = 公网直暴露/后端直接收到 https，按请求推导；`auto` = 未声明，行为同 direct 但启动日志说明推断依据。写错取值启动失败。见 §4.2 |
| `ELDER_PUBLIC_BASE_URL` | 留空 | **HTTP/OTA 对外地址**（新变量，http/https，启动期校验 scheme）。配了就在所有模式下优先使用；tunnel 模式下必需 |
| `ELDER_WS_PUBLIC_BASE_URL` | 留空 | **WebSocket 单独域名**（可选，ws/wss，启动期校验）。Cloudflare Tunnel 可给同一源站配多个 hostname；不配则 WS 由 HTTP 地址换 scheme 推导。只配它不配 HTTP 地址会启动失败 |
| `ELDER_SCHEDULE_AHEAD_HOURS` | `36` | 服务端预生成提醒实例的提前量 |
| `ELDER_SCHEDULER_INTERVAL` | `60` | 后台定时任务间隔秒数（排期补全 / 超时升级 / 离线清扫，见 §7.1） |
| `ELDER_HOST` | `0.0.0.0` | 容器内监听地址。镜像 CMD 用它，一般不改 |
| `ELDER_USER_TOKEN_DAYS` | `30` | 用户登录令牌有效期（天） |
| `ELDER_DEVICE_TOKEN_DAYS` | `365` | 设备令牌有效期（天） |
| `ELDER_ACT_TTL_MIN` | `10` | 激活码有效期（分钟），与固件 `elder_activation.c` 的格式约定配套 |
| `ELDER_FIRMWARE_VERSION` | 空（不返回 firmware 段） | **设备 OTA 报告的最新固件版本号**。与 `ELDER_FIRMWARE_URL` **同时配置**才在 OTA 响应里下发 `firmware` 段；版本真源是 `firmware/xiaozhi/CMakeLists.txt` 的 `PROJECT_VER`，版本比较由固件自己做。见 §1.10 |
| `ELDER_FIRMWARE_URL` | 空（不返回 firmware 段） | **固件下载 URL**，**必须是单 app 镜像（`xiaozhi.bin`）不是 merged-flash.bin**（OTA 走 `esp_ota_write` 写 OTA 分区）；二进制由部署方托管，CI artifact 会过期别直接用。见 §1.10 |
| `ELDER_LIBOPUS_PATH` | 空（自动探测） | **Opus 编解码库文件的绝对路径**。**Docker 部署不需要设** —— 镜像已 `apt-get install libopus0`。只在**本机裸跑**（§1.9）且自动探测失败时才需要，见 §6.12 |
| `ELDER_QWEATHER_HOST` | 空（天气功能未配置） | **和风天气 API Host**（控制台给的裸 host，不带 `https://`）。见 §1.11 |
| `ELDER_QWEATHER_KEY_ID` | 空 | 和风**凭据 ID**，JWT header 的 `kid`。见 §1.11 |
| `ELDER_QWEATHER_PROJECT_ID` | 空 | 和风**项目 ID**，JWT payload 的 `sub`。见 §1.11 |
| `ELDER_QWEATHER_DEVELOPER_ID` | 空 | 和风**开发者 ID**，JWT payload 的 `iss`。见 §1.11 |
| `ELDER_QWEATHER_PRIVATE_KEY_PATH` | 空（回落仓库父目录的默认文件名） | **Ed25519 私钥路径（容器内路径）**。生产是 `/app/secrets/qweather-ed25519-private.pem`，**私钥不能放项目目录**（会进 GitHub）。见 §1.11.2 |
| `ELDER_QWEATHER_PRIVATE_KEY` | 空 | 私钥**内容**（PEM 字符串）。与 `_PATH` 二选一，优先用它 —— 适合 k8s Secret 注入。见 §1.11 |
| `ELDER_WEATHER_ENABLE` | `1` | 设 `0` 关闭天气子系统（连定时预热一起停） |
| `ELDER_WEATHER_TTL_MIN` | `60` | 天气缓存有效期（分钟）。非法值/`0` 回落 60 |
| `ELDER_WEATHER_DEFAULT_CITY` | `平顶山` | 没指定城市时的默认城市 |
| `ELDER_WEATHER_GEO_TTL_DAYS` | `30` | 城市名→LocationID 缓存天数 |
| `ELDER_WEATHER_TIMEOUT_SEC` | `6` | 单次上游请求超时秒数 |
| `ELDER_WEATHER_WARM_HOURS` | `24` | 预热只刷最近这么多小时内被问过的城市 |
| `ELDER_WEATHER_WARM_AHEAD_MIN` | `10` | 预热只刷这么多分钟内就要过期的条目 |
| `ELDER_DB_ECHO` | `0` | 设为 `1` 打印所有 SQL，排查问题时开，平时关 |
| `ELDER_ALLOW_PUBLIC_REGISTER` | `1`（开启） | **是否开放匿名自助注册**。设为 `0`/`false`/`no`/`off`/空串即关闭，关闭时 `POST /api/auth/register` 返回 `403` + 「当前未开放自助注册，请联系管理员开通账号」。详见 §5.1 |
| `ELDER_BOOTSTRAP_ADMIN_USERNAME` | 空（未配置） | **初始管理员（bootstrap）的用户名**。只有用户名**逐字等于**它的注册者会成为管理员；未配置时**任何人注册都不是管理员**。详见 §5.1 |
| `ELDER_SEED_ADMIN_PASSWORD` | 空（随机生成） | **空库首次启动时种子管理员 `admin` 的初始口令**。留空 = `secrets.token_urlsafe(16)` 随机生成，并以 **WARNING** 打进启动日志（唯一一次可见）；设置 = 用它。**生产不要设**，设了且值是弱口令会每次启动被体检告警。详见 §5.2 |
| `ELDER_RESET_ADMIN_PASSWORD` | 空（不轮换） | **存量管理员口令的一次性轮换开关**。设置后，下次启动把所有「口令仍是已知默认口令」的管理员轮换成该值；轮换成功后**必须删掉**。幂等：轮换后条件不再成立，后续启动不会再改。详见 §5.2 |
| `ELDER_LOGIN_RATE_LIMIT` | `1`（开启） | **登录失败限速总开关**。设为 `0`/`false`/`no`/`off`/空串即整体关闭（内网 / 跑冒烟时用）。**生产不要关**。详见 §5.3 |
| `ELDER_LOGIN_FAIL_COOLDOWN_SEC` | `5` | 密码错一次后必须等待的秒数。`0` = 不冷却。详见 §5.3 |
| `ELDER_LOGIN_FAIL_PER_MINUTE` | `6` | 每个主体（IP / 用户名）每分钟最多几次失败。`0` = 该窗口不生效。详见 §5.3 |
| `ELDER_LOGIN_FAIL_PER_HOUR` | `20` | 每个主体每小时最多几次失败。`0` = 该窗口不生效。NAT 后面人多时调大这一条。详见 §5.3 |
| `ELDER_LOGIN_RATE_LIMIT_MAX_KEYS` | `10000` | 限速表最多记多少个主体，超出按 LRU 淘汰。挡住"拿一万个假用户名各错一次"把内存撑爆。详见 §5.3 |
| `ELDER_TRUSTED_PROXY_IPS` | 空（框架层回落 `127.0.0.1`，应用层谁都不信） | **可信反代地址**（逗号分隔，支持 CIDR）。**同时驱动两层**：① 框架层 —— Dockerfile CMD 的 `--forwarded-allow-ips ${ELDER_TRUSTED_PROXY_IPS:-127.0.0.1}`，决定 uvicorn 是否采信 XFF/XFP（不设时与框架默认 `127.0.0.1` 等价）；② 应用层 —— `ratelimit.client_ip()` 只在**对端在名单里**时才解析 XFF，并**从右往左**跳过可信段取第一个不可信地址。⚠️ 两层语义必须一致：uvicorn 侧的 `*` = 信任所有人（取最左段，可伪造），应用层的 `*` = 谁都不信（解析不成网段），合起来限速器两头落空。**切勿传 `*`**。详见 §5.3、§6.13 |
| `ELDER_MAIL_FAKE` | `0` | **测试专用**。设为 `1` 时邮件不真投递、写进进程内发件箱。**生产必须为 `0`**，留空等同 `0` |

**找回密码与 SMTP 超时（`server/app/mailer.py`）** —— 这一组**不在 `config.py` 里**，
是邮件模块自己读的，但同样是运维会要调的：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ELDER_RESET_CODE_TTL_MIN` | `15` | 验证码有效期（分钟） |
| `ELDER_RESET_CODE_MAX_ATTEMPTS` | `5` | 同一验证码最多校验次数，超过即作废（防爆破） |
| `ELDER_RESET_RESEND_COOLDOWN` | `60` | 重发冷却（秒）。**防止「找回密码」被当成邮件轰炸器** —— 不限速的话谁都能拿一个邮箱地址无限刷验证码 |
| `ELDER_RESET_PURGE_BUFFER` | `3600` | 过期验证码清理缓冲（秒）：只删「过期超过这个缓冲」的行，避免删掉还能用几秒的 |
| `ELDER_SMTP_CONNECT_TIMEOUT` | `10` | 单次 SMTP 连接超时（秒） |
| `ELDER_SMTP_READ_TIMEOUT` | `10` | 单次 SMTP 读取超时（秒）。发送是**同步阻塞在请求线程里**的，超时太长会把工作线程拖死 |

> 用户反馈「收不到邮件」时，**先查超时而不是先查 DNS** ——
> `Connection refused` 通常是端口错或服务没监听（见 §6.10），
> 而「一直转圈最后才报错」通常是这两个超时在起作用。

**语音链路调参（`ELDER_VOICE_*`）** —— 老人体验的核心可调项。
**默认值就是调好的值，正常部署一格都不要动**；调错的表现是「老人听不清 /
说得太急 / 抢话 / 半天不应」，所以下表只作备案，改之前先读 `config.py` 里
每个变量上方的取舍注释：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ELDER_VOICE_SAMPLE_RATE` | `24000` | **下行 TTS 输出采样率**。客户端只吃 24kHz/mono/S16LE 裸 PCM，固件按服务端声明的速率逐帧 Opus 编码。**客户端的硬约束，改了要连固件一起改** |
| `ELDER_VOICE_ASR_LANGUAGE` | `zh` | ASR 语言。默认中文：多数厂商不传会用默认（英文），老人场景必须显式给 |
| `ELDER_VOICE_FRAME_DURATION_MS` | `60` | Opus 帧长。只支持 2.5/5/10/20/40/60，且 60 是固件 `audio_service.cc` 里写死的 `OPUS_FRAME_DURATION_MS`，**改它等于换一套帧协商** |
| `ELDER_VOICE_SILENCE_MS` | `2000` | **服务端静音兜底阈值（断句判据）**。固件默认走 realtime 模式、服务端无 AEC，这个阈值是唯一的断句依据。老人口语中超过 1.5s 的停顿常见，1500 会把「话说一半」判成说完、内容直接丢失且不可恢复。**两类错误代价不对称：宁可多等，不可截断** |
| `ELDER_VOICE_SILENCE_MAX_MS` | `6000` | 停顿自适应的上限（毫秒）。本轮出现过「停顿后又继续说」的长停顿就按比例放大断句阈值，但不超过它，否则一句不停地说下去会让阈值无限膨胀 |
| `ELDER_VOICE_PAUSE_FACTOR` | `1.5` | 停顿自适应系数：阈值 = `max(SILENCE_MS, min(SILENCE_MAX_MS, 最长句中停顿 × 1.5 + 500ms))`。取 1.5 是给「停顿后还会接着说」留余量，调成 1.0 等于没有自适应 |
| `ELDER_VOICE_PAUSE_MIN_MS` | `600` | 多长的「停顿后又继续」才记入自适应（毫秒）。低于它的是词间间隙，不是思考停顿，不拿来做依据 |
| `ELDER_VOICE_MIN_SPEECH_MS` | `300` | 触发兜底前至少要听到这么多语音，防止「一句都没说就静音触发」把环境噪声当一整句送去 ASR（老人会听到一串乱码） |
| `ELDER_VOICE_MAX_LISTEN_MS` | `60000` | 单轮收听安全上限（毫秒）。老人说着说着离开/卡住时兜底 finalize，避免会话永远挂在 listening。60s 足够任何正常单句 |
| `ELDER_VOICE_SPEECH_RMS` | `0.005` | 「判定开口了」的高门限（绝对下限，约 -46dBFS）。**调低**能救回轻声/离得远的老人，**调高**能压住嘈杂房间里的误触发 |
| `ELDER_VOICE_SPEECH_RMS_KEEP` | `0.0025` | 「还在说」的低门限（约 -52dBFS）。句中轻声音节/远场衰减落在高低门限之间时靠它续住语音态，否则话说到一半就掉出语音态 |
| `ELDER_VOICE_SOFT_GRACE_MS` | `2000` | 软语音（能量落在两门限之间）最长续多久（毫秒）。**不设期限**的话，持续底噪会让会话永远挂住 |
| `ELDER_VOICE_TOOL_MAX_ROUNDS` | `3` | LLM 工具调用（提醒的增删查改）单轮最多几轮。**防 LLM 反复调用同一工具形成死循环** —— 每轮都是一次真实的 token 花费，不设上限等于把账单交给模型的坏心情 |
| `ELDER_VOICE_HISTORY_TURNS` | `4` | 会话内保留的历史轮数。语音链路每轮都要塞人格 + 记忆召回，窗口比纯文本更紧，老人话题跳跃少，所以留得少 |
| `ELDER_VOICE_WS_PATH` | `/api/voice/v1/ws` | 语音 WebSocket 路径。换路径要同步改反代的 `location` 与固件侧预期 |
| `ELDER_VOICE_OTA_PATH` | `/api/voice/ota` | OTA 地址路径，**必须与固件 `CONFIG_OTA_URL` 一致** |

> ⚠️ **这一组里只有 `ELDER_VOICE_SAMPLE_RATE` 是"客户端硬约束"**（客户端只吃
> 24kHz/mono/S16LE 裸 PCM），其余都是纯服务端行为参数。
> 调参前先看现象：老人**说得太急**（话被截断）→ 调大 `SILENCE_MS`；
> 老人**抢话/应得太慢**（明显停顿才应答）→ 调小。**别一次改多个变量**，
> 一次只动一个、观察一天，否则分不清是哪个起的作用。


> **SMTP 不在环境变量里** —— 存数据库，在管理后台配，见 §1.8。

> ⚠️ **陪聊时长上限不在环境变量里**。它曾经和会员核销配置写在同一段代码里，
> 随会员制取消**那段配置整体删掉了，但时长上限本身没删** —— 它现在是
> `chat_quota_config` 表里的**数据**（由 `app/chat_quota.py` 管理），
> 在管理后台改，不在 `.env` 里。排查「陪聊被切断」请查这张表，不要去 `.env` 里找变量。
>
> 这张表单行两个字段（默认值见 `chat_quota.py:56-57`）：
>
> | 字段 | 默认 | 含义 |
> | --- | --- | --- |
> | `free_daily_seconds` | `3600` | 未授权设备的每日陪聊秒数。**`0` = 不限制** |
> | `idle_timeout_seconds` | `90` | 空闲多久判定对话结束（秒） |
>
> ⚠️ **`free_daily_seconds` 只对未授权（未绑定）设备有意义，已授权设备一律无限量**
> —— 这是一条硬不变量，由 `tools/smoke_quota.py` 守着，改动前先看它。
> 换句话说，「已付费开通的设备会不会被时长切断」这个问题的答案是**不会**。
> 另外这个秒数**现在只用于展示与对账，不再用于拦人**（会员制取消后失去了拦人的语义）。

> ⚠️ **已随会员制取消、不要再往 `.env` 里配的**：`ELDER_ORDER_TTL_HOURS`
> （会员订单人工核销的有效期）在 `config.py` 里**已彻底不存在** ——
> 那段会员核销配置随 commit `e264200` 整体删除，全仓没有任何代码读它。
> 早期版本的 `server/.env.example` 里有一行示例，现已一并清掉；
> **你的旧 `.env` 里如果还留着它，直接删掉，无副作用。**
> 设备付费开通走的是「线下转账 + 管理员人工核销」的设备订单，与平台会员制无关。

完整清单以 `server/app/config.py` 为准。

### 5.1 注册开关与初始管理员

`POST /api/auth/register` **从设计上就是匿名可达的**（没有任何鉴权装饰器）。
所以「谁可以注册」「注册出来是不是管理员」必须由服务端配置决定 ——
**不能靠前端有没有那个注册按钮**：按钮不显示 ≠ 接口不存在，那种做法是
security by obscurity。这两个变量的取值规则都写在 `server/app/config.py`，
后端是唯一真源。

**为什么默认值是"开启"**：主要部署形态是给家里老人私有化部署一套，往往就
一个人注册，默认关闭等于每次部署都先教一遍环境变量；而且默认翻转会让
**既有部署在升级后悄悄失去自助建号能力** —— 那种"注册怎么突然不好使了"
最难定位。

⚠️ **但部署在公网、且不想让别人消耗你的 AI token 时，必须显式关闭**：

```bash
ELDER_ALLOW_PUBLIC_REGISTER=0
```

此时注册接口返回 `403`，响应体是 `{"detail": "当前未开放自助注册，请联系管理员开通账号"}`。
刻意用 `403` + 中文整句而不是 `404`/空响应：前端可以直接把 `detail` 弹给用户，
排障时也能一眼区分「策略关了」与「接口坏了」。

**初始管理员怎么来（三种方式，任选其一）**：

1. **默认种子账号**：空库首次启动会自动建管理员 `admin`，口令**随机生成并打进日志**
   （见 §1.2）；也可在部署前用 `ELDER_SEED_ADMIN_PASSWORD` 指定。
   > ⚠️ 这里曾经是硬编码的 `admin123`（口令写死在代码里，任何知道它的人都有完整
   > 管理权限）。**已在本轮移除**，但只对**新装库**有效 —— 存量库见 §5.2。
2. **运维直接改库**：把某个现有用户的 `users.is_admin` 置为真。适合绝大多数情况。
3. **`ELDER_BOOTSTRAP_ADMIN_USERNAME`**：想让"某个特定用户名注册进来就是管理员"
   时用它。

关于方式 3，有一条**必须照做的收尾动作**：

```bash
# 1) 部署前设好（名字选一个不容易被猜到的）
ELDER_BOOTSTRAP_ADMIN_USERNAME=zhangjiashu_admin
# 2) 用这个用户名注册 -> 该账号成为管理员
# 3) ★ 注册完成后把这个变量**去掉**并重启服务
```

原因是这个变量**没有时限**：只要它还在，配的那个名字就一直是一张
"注册即得管理员"的票。它的定位是"开箱步骤"，**不是长期配置**。

> ⚠️ 这里曾经的规则是「第一个注册的用户自动成为管理员」（代码形如
> `select(User).limit(1) is None`）。在匿名可注册的公网部署上，它等于
> **把管理员发给第一个点开注册页的路人**，已在本轮移除。
> 回归网在 `tools/smoke_auth.py`（含一条专门甄别该规则的最小场景）。

### 5.1.1 管理员的操作留痕：两张审计表

管理员能改的东西里有两类是「改了别人能看见、且事后可能被质疑」的，
它们各有专门的审计表（`server/app/models.py`）。**排障时先查这两张表，
不要只翻应用日志** —— 应用日志会滚，审计表不会。

| 表 | 记什么 | 关键字段 |
| --- | --- | --- |
| `admin_preset_audits` | **管理员改平台人格预设**的逐字段审计 | `action`（`update`/`reset`/`archive`）、`changed_fields`、`diff`（`{"字段": {"from":…, "to":…}}`）、`admin_username`（**快照不 join**）、`guardrail_version` |
| `agent_persona_audits` | **人格自动演化**的每一条推断 | `signal`、`user_text`/`bot_text`/`prev_bot_text`、`inferred`（人话描述）、`valid`、**`reason`**、`discarded_at`/`discard_reason` |

运维时有三个要点：

- **「机器人为什么这么说话」查 `agent_persona_audits`**。它的设计重点不是记录
  学到了什么，而是**每条推断先判有效性再入库、判定结果原样留档** ——
  判定为无效（`valid=False`）也照记一条，家属能看到「机器人刚才差点被逗，
  还好没学」。家属作废某条学习走 `discarded_at`（**不删行**，删行就谈不上审计）。
- **「谁改了平台预设、改了什么」查 `admin_preset_audits`**。
  `admin_username` 刻意存**快照**而不是 join `users` —— 用户改名后，
  审计仍必须能回答「当时是谁改的」。
- ⚠️ **这两张表刻意不存放什么**（将来有人要加字段时必读）：
  硬红线正文与红线 tokens（那是 `guardrails.assert_present()` 做 fail-closed
  自检的唯一判定基准，写进运营可读的表等于把门禁怎么判交出去）、
  任何用户派生角色的内容（派生角色带 `owner_id`，抄进来就成了跨用户数据表，
  而这张表只受 `get_current_admin` 保护，比 `ai_providers` 的行级可见性更弱）、
  以及任何编译产物全文（画像摘要、偏好示例、家属写的话 —— 含私密内容）。
  **审计表绝不能成为用户内容的副本。**

> `preset_id` 不设 `ON DELETE`：预设只能 archive 不能硬删
> （`DELETE /api/roles/{id}` 对 preset 永远 403），所以审计不会因删除而悬空。

### 5.2 种子管理员口令与**存量**默认口令处置（P0）

#### 背景：为什么"改种子"救不了存量库

旧代码在 `main._seed()` 里写死了 `password_hash=hash_password("admin123")`。
那段代码**只在空库首次启动时执行**，而硬编码弱口令本身**已从 `main.py` 移除**
（commit `bb5ca98`），于是：

* **新装库**：本版已换成随机口令（或 `ELDER_SEED_ADMIN_PASSWORD` 指定的值）；
* **已经跑起来的库**（含正在运行的生产库）：那个 `admin` 的口令**至今仍是 `admin123`**，
  升级代码**不会**自动改掉它 —— 因为根本不会再走 `_seed()`。

本服务是公开部署的（`https://152.136.230.132`，APP 把 `BASE_URL` 写死成它），
所以存量的 `admin/admin123` 等于"任何人都能当平台管理员"：改 AI 配置（含厂商
API Key 的读写口径）、查看全部用户用量与记忆、给设备核销付费开通。

#### 一、先确认你的库是不是中招

启动日志里**每次**都会体检（只要还命中就一直喊）：

```bash
docker compose logs elder-server | grep ELDER-DEFAULT-PASSWORD-ALERT
```

命中时长这样（WARNING 级）：

```
WARNING elder | 【安全】ELDER-DEFAULT-PASSWORD-ALERT user=admin id=1 —— 该管理员的口令仍是已知默认口令「admin123」。
    → 任何知道这个默认值的人都能以**平台管理员**身份登录：...
    → 处置 A（登录后自己改）：带 token 调 POST /api/auth/password ...
    → 处置 B（一次性轮换，适合口令已泄露/无法登录）：...
```

判定用的是 `verify_password()`（PBKDF2 重算比对），**不是**字符串比对 ——
hash 带随机盐，同一个口令两次 hash 出来的字符串不同，字符串比对必然漏报。
**只对命中者告警**：已经改过口令的管理员、普通用户完全不受影响。

#### 二、处置命令（抄走即用）

**方式 A —— 能登录，直接改密码**（最推荐）

```bash
# 1) 拿 token（把 152.136.230.132 换成你的对外地址）
TOKEN=$(curl -s -X POST https://152.136.230.132/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# 2) 改密码（新口令自己换掉）
curl -X POST https://152.136.230.132/api/auth/password \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"old_password":"admin123","new_password":"换成你的强口令"}'
```

**方式 B —— 一次性轮换**（口令已泄露、或登不进去时用）

```bash
# 1) 在 server/.env（或 compose 的 environment）里**临时**加一行
echo "ELDER_RESET_ADMIN_PASSWORD='换成你的强口令'" >> server/.env

# 2) 重启一次 —— 日志出现下面这行即成功
docker compose up -d
docker compose logs elder-server | grep ELDER-ADMIN-PASSWORD-ROTATED
#    WARNING elder | 【安全】ELDER-ADMIN-PASSWORD-ROTATED user=admin —— 已把该管理员的口令轮换为 ELDER_RESET_ADMIN_PASSWORD 的值。

# 3) ★ 立刻把那一行删掉，再重启一次（否则新口令会以明文长期留在配置文件里）
sed -i '/ELDER_RESET_ADMIN_PASSWORD/d' server/.env
docker compose up -d

# 4) 确认告警消失
docker compose logs elder-server | grep ELDER-DEFAULT-PASSWORD-ALERT   # 应无输出
```

**方式 C —— 直接改库**（前两种都不可行时）

```bash
# 容器内用本项目的 hash 算法重写 password_hash，别手写 bcrypt/明文
docker compose exec elder-server python - <<'PY'
from app.security import hash_password, verify_password
from app.db import SessionLocal
from app.models import User
with SessionLocal() as db:
    u = db.query(User).filter(User.username == "admin").one()
    assert verify_password("admin123", u.password_hash), "该账号口令已不是默认值，无需处置"
    u.password_hash = hash_password("换成你的强口令")
    db.commit()
    print("admin 口令已重置")
PY
```

#### 三、幂等性与护栏（别踩）

* **幂等**：轮换的触发条件是「口令仍是已知默认口令」。轮换成功后该条件不再成立，
  所以后续每次启动**不会再改** —— 不需要额外的"已处理"标记。把
  `ELDER_RESET_ADMIN_PASSWORD` 忘在 `.env` 里不会导致反复改口令。
* **护栏**：若它的值自己就是已知默认口令（比如手滑写成 `admin123`），
  **拒绝轮换**并打印「已拒绝轮换」—— 否则会退化成每次启动改一遍的配置漂移。
* **新装库**想指定初始口令用 `ELDER_SEED_ADMIN_PASSWORD`，不要用
  `ELDER_RESET_ADMIN_PASSWORD`（后者只对**已存在**且口令是默认值的管理员生效，
  对新装库不起作用）。

> 回归网：`tools/smoke_auth.py` 场景 4~7（65 条断言，含"随机口令分支"、"存量告警"、
> "轮换幂等"、"不误伤其它账号"）。

#### 四、跑冒烟时必须显式给 `ELDER_SEED_ADMIN_PASSWORD`

`tools/` 下十余个冒烟脚本（`smoke_server` / `smoke_home` / `smoke_mail` /
`smoke_quota` / `smoke_voice_catalog` 等）都用 `admin/admin123` 登录，
`smoke_mail`、`smoke_home` 还会**故意输错一次**。硬编码弱口令从
`main.py` 移除后，这些脚本在自己起子进程时必须显式带上：

```bash
export ELDER_SEED_ADMIN_PASSWORD=admin123      # Windows Git Bash 同语法
python tools/smoke_server.py
```

**忘了带会红**（登录 401/403），这是**预期行为、不是回归**。
部分脚本（`smoke_auth` / `smoke_local_reminders` / `smoke_voice_catalog`）
已在内部按场景显式设置，不需要外部导出；其余脚本靠继承环境变量。
CI 里在 job 级设同一个值即可 —— runner 是一次性且不可公网访问，
那里用弱口令没有现实风险。

同时建议本地跑冒烟时把限速放宽，否则几秒内连续登录同一台服务会被闸门拦下：

```bash
export ELDER_LOGIN_FAIL_PER_MINUTE=9999 ELDER_LOGIN_FAIL_PER_HOUR=9999
```

三条日志标记（`ELDER-INITIAL-PASSWORD` / `ELDER-DEFAULT-PASSWORD-ALERT` /
`ELDER-ADMIN-PASSWORD-ROTATED`）是冒烟断言的锚点，**改文案会让 `smoke_auth` 转红**。

### 5.3 登录失败限速（5 秒冷却 / 每分钟 6 次 / 每小时 20 次）

主理人 2026-10-17 拍板的三条规则，**全部只针对登录失败**：

| # | 规则 | 默认值 | 环境变量 |
| --- | --- | --- | --- |
| 1 | 密码错一次后，该主体必须等 **5 秒** 才能再试，期间尝试直接拒绝 | `5` 秒 | `ELDER_LOGIN_FAIL_COOLDOWN_SEC` |
| 2 | 每分钟最多 **6 次** 失败 | `6` | `ELDER_LOGIN_FAIL_PER_MINUTE` |
| 3 | 每小时最多 **20 次** 失败 | `20` | `ELDER_LOGIN_FAIL_PER_HOUR` |

成功登录**不计入**配额。触发限速时返回 `429` + `"登录失败次数过多，请 5 秒 后再试"`
（会按量级换成"分钟 / 小时"），并带标准 `Retry-After` 响应头。

#### 一、限流的键：**IP 与用户名同时限**

只按用户名限 -> 攻击者把 `admin` 的 20 次/小时打满，**真正的管理员自己登不进去**
（限速器变成 DoS 武器）；只按 IP 限 -> 换代理/换出口 IP 就绕过，且挡不住"一堆 IP
对同一个账号各试一次"。所以两道闸门都装，任一触发即拦，取两者中更长的等待时间。

⚠️ 为什么它**锁不死**合法用户：**口令正确的请求根本不过闸门**。
`login()` 的顺序是**先验口令、后过闸门** —— 口令对了直接发证并清掉该账号的失败记录，
完全不看配额。于是"被锁死"只会作用在**不知道口令的人**身上，他本来也登不进去。
（这条同时保住了"先故意输错一次、紧接着用新口令登录"的正常用法。）

#### 二、客户端 IP 怎么取 / 怎么防伪造（生产必须读）

`X-Forwarded-For` 是**请求方可以随便写**的头。盲目信任它等于让攻击者每次换个头
就换一个主体 —— 限速器形同虚设。本项目的规则是**两层**：

**第 1 层（uvicorn 自带）** —— uvicorn 默认开启 `ProxyHeadersMiddleware`，
镜像 CMD 显式传 `--proxy-headers --forwarded-allow-ips ${ELDER_TRUSTED_PROXY_IPS:-127.0.0.1}`
（不设变量时与框架默认 `127.0.0.1` 逐字等价）：只要 TCP 对端在名单里，它会先把
`request.client` 改写成 XFF 里解析出来的地址，才轮到应用代码看到。

> ⚠️⚠️ **绝对不要传 `--forwarded-allow-ips=*`**。此时 uvicorn 取的是 XFF 的
> **最左边一段**，那正是攻击者伪造着塞进来的；`*` 等于把限速器关掉。
> 更阴的是：`*` 在 uvicorn 层是"信任所有人"，而在应用层
> （`ratelimit._parse_trusted("*")` 解析不成网段，退化成永不匹配的字面量）
> 是"谁都不信" —— 两层语义相反，合起来的效果是 uvicorn 把对端改写成攻击者
> 选的 IP，限速器再拿它当键，**两头落空**。生产必须把它写成**反代那一个地址**
> （或最小网段），例如：
>
> ```yaml
> # docker-compose.yml（Nginx 也在容器里时填容器名/网段，不要用 *）
> environment:
>   ELDER_TRUSTED_PROXY_IPS: "172.18.0.5"
> ```
>
> 反代就在本机（对端是 127.0.0.1）时保持默认值即可，不用配。

**第 2 层（本项目自己的 `server/app/ratelimit.py`）** —— 当 uvicorn 第 1 层**没有**
解析（因为对端不在它的名单里，例如 Nginx 在另一个容器、对端是 docker 网桥地址）时，
由这一层接管：

* `ELDER_TRUSTED_PROXY_IPS` **留空（默认）**：**谁都不信**，只认 TCP 对端。
  直连部署就是这个形态，限速照常生效，伪造 XFF 无效。
* `ELDER_TRUSTED_PROXY_IPS` 填了地址/CIDR（逗号分隔，支持 `172.18.0.0/16`）：
  只有**对端在名单里**时才解析 XFF，并且**从右往左**扫、跳过所有可信地址，
  取第一个不可信的地址作为客户端。

  为什么从右往左：每经过一跳可信代理就在右边追加一段，
  所以 `X-Forwarded-For: <攻击者伪造>, <真实客户端>, <可信代理>` 从右往左扫
  正好跳过伪造段 —— 取最左段的实现会被随便一个头骗过去。

> 排障：如果所有人共用一个出口 IP（公司 NAT / 家里多个人），会表现为"一个人输错
> 几次、别人也跟着被限"。此时把 `ELDER_LOGIN_FAIL_PER_HOUR` 调大，
> 或让运维确认第 1 层的 `ELDER_TRUSTED_PROXY_IPS` 是不是误配成了 `*`
> （uvicorn 侧等价的 `--forwarded-allow-ips=*` 同理）。

#### 三、计数存哪、怎么清理

**进程内存**（`server/app/ratelimit.py`），不落库。理由：限速是速度挡板不是审计，
重启归零可接受；落库会让每次失败都变成一次写（给攻击者一条刷 I/O 的放大路径），
还要再配一套清理任务。三道防线保证不膨胀：滑出 1 小时窗口的时间戳随时丢弃、
空键直接删、键总数超过 `ELDER_LOGIN_RATE_LIMIT_MAX_KEYS`（默认 10000）按 LRU 淘汰。

⚠️ 代价：**多 worker / 多副本部署时限额会变成每份各算一次**（N 副本 = 放宽 N 倍）。
本镜像是单进程 uvicorn，当前不受影响；将来要横向扩容请换成共享计数（Redis），
**不要靠调大阈值糊过去**。

#### 四、怎么临时放开

```bash
# 整体关闭（内网部署 / 跑冒烟时用；生产不要设）
ELDER_LOGIN_RATE_LIMIT=0

# 只放宽"每小时"那一条（NAT 后面人多时）
ELDER_LOGIN_FAIL_PER_HOUR=60
```

> 回归网：`tools/smoke_auth.py` 场景 8~17（42 条断言）。三条规则各自**单独起一台
> 服务**、把另外两个窗口设成 9999 隔离开 —— 否则"第 21 次被拦"会被分钟窗口
> （第 7 次就拦）抢先命中，断言即使把小时上限改成 9999 也照样绿（**假绿**）。

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

### 6.9 设备配网正常、心跳也正常，但一开口说话就断线

这是语音链路合并到单一端口之后**最常见**的部署故障。现象很有迷惑性：
注册设备、拉取提醒、`/api/device/sync` 全都成功，只有说话时 WebSocket 连不上或秒断。

按这个顺序排查：

1. **看 Nginx 有没有放行 Upgrade**：这是首要原因。旧的部署说明里语音链路走独立端口
   （8000），反代只配了普通 HTTP；现在语音复用 8064，必须加 `location /api/voice/` 那一段。
   验证方法（改完 `nginx -s reload`）：

   ```bash
   curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
        -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
        https://152.136.230.132/api/voice/v1/ws
   # 正确：HTTP/1.1 101 Switching Protocols
   # 错误：HTTP/1.1 200 或 400 —— 说明 Upgrade 头被反代吃掉了
   ```

2. **确认 `proxy_read_timeout` 没有设太小**：有限值小于一次对话时长时，
   表现是"能说第一句，说到一半断开"。建议 ≥ 3600s。

3. **`proxy_buffering off` 有没有配**：忘了开会有明显的语音延迟（音频被攒够一批才发）。

4. **确认设备拿到的地址对不对**：看启动日志里的部署摘要（`docker compose logs elder-server | grep 【部署】`），
   或调一次心跳看 `ws_url` 字段。

   走 HTTPS 时必须是 `wss://<对外地址>/api/voice/v1/ws`（IP 直连形态：
   `wss://152.136.230.132/api/voice/v1/ws`）。
   如果显示的是内网 IP 或 `ws://`（非加密），按部署形态二选一：

   ```bash
   # 隧道 / TLS 反代（推荐）：显式声明模式 + 对外地址，缺地址会启动失败
   ELDER_DEPLOY_MODE=tunnel
   ELDER_PUBLIC_BASE_URL=https://152.136.230.132
   # 旧变量仍然有效：ELDER_VOICE_PUBLIC_BASE=https://152.136.230.132
   ```

   机制说明：uvicorn 对 `X-Forwarded-Proto` **只信任来自 127.0.0.1 的对端**
   （镜像 CMD 的 `--forwarded-allow-ips ${ELDER_TRUSTED_PROXY_IPS:-127.0.0.1}`，
   默认即 127.0.0.1），生产里容器看到的对端是 Docker 网桥网关，该头不被采纳，
   scheme 推导恒为 http —— 所以**只要走了 TLS 反代（Nginx / Cloudflare Tunnel
   都一样）就必须配对外地址**，详见 §4.1.3，三种拓扑的完整决策表见 §4.2。

   > 想把网关加进信任名单、让 scheme 推导自动生效，需要同时改两层：
   > `ELDER_TRUSTED_PROXY_IPS`（应用层 ratelimit）与 Dockerfile CMD 的
   > `--forwarded-allow-ips`（框架层，读同一个变量，见 §5.3）。
   > **但不推荐用它替代对外地址** —— 名单只解决"对端是谁"，不解决
   > "该给设备发什么地址"，配错域名时照样错。

5. 若完全没有到 `/api/voice/` 的日志，检查容器的 8064 端口映射是否已发布、
   以及有没有别的进程抢占了它。

> 注：`ws` 与 `wss` **不能混用**。页面/设备在 HTTPS 下拒绝建立非加密的 WebSocket。
> 服务端默认按请求 scheme 推导，但生产对端不在 uvicorn 的 XFP 信任名单里、
> 推导结果恒为 `http`，所以**走 TLS 反代时必须配对外地址**（§4.1.3 / §4.2），
> 不要指望自动推导。

### 6.10 用户收不到密码重置邮件

按顺序排查：

1. **先在管理后台点「发送测试邮件」**。这一步能挡掉 80% 的问题，而且会**原样返回失败原因**
   （比翻日志快得多）。典型的失败：`Connection refused`（端口错）、
   `Application-specific password required`（用了登录密码而非授权码）、
   `STARTTLS required`（该用 STARTTLS 却配了 SSL，或反之）。
2. **检查端口与加密的组合**：`465` 配 SSL，`587` 配 STARTTLS。两者弄混是最常见错误。
3. **检查 `ELDER_MAIL_FAKE`**：如果被人设成了 `1`，邮件会全部进假发件箱而不投递。
   `docker compose exec elder-server env | grep MAIL_FAKE` 确认。
4. **用户侧：先让他看垃圾邮件箱**。自建 SMTP 首次发信很容易被判垃圾邮件。
5. **看服务日志**：邮件发送失败不会让请求失败（设计上不允许把 500 抛给用户），
   所以必须查日志，关键字 `mailer`。

   ```bash
   docker compose logs elder-server | grep -i mailer
   ```

6. 若验证码压根没生成，检查该用户**注册时填的邮箱是否为空** ——
   老用户（邮箱字段上线前注册的）`email` 是空串，找回密码时无法投递。这种情况引导他联系管理员后台处理。

> 另外提醒一个安全设计：`POST /api/auth/password/reset/request` 对**已注册和未注册的邮箱
> 返回完全相同的响应**，这是刻意的账号枚举防护。所以调用成功不代表那个邮箱一定注册过，
> 排查时别被这点误导。

### 6.11 设备断网 / 长时间离线后行为异常

断网下的服药提醒是本项目的硬需求（医疗事故级），运维排查按下面几条对照。

**先分清是「没响铃」还是「响错时间」** —— 固件在这两者之间选了保守的一侧：

| 现象 | 日志关键字 | 含义 |
| --- | --- | --- |
| 到点不响 | `系统时钟不可信（未校时），暂停触发提醒` | 时钟没校过，**故意不响**。见下方说明 |
| 到点不响 | `提醒引擎已就绪` 但无 `-> PENDING` | 提醒表为空：可能从未同步过服务端规则 |
| 长时间没联网 | `本地上报失败（...），下次心跳重试` | 本地自建提醒上报失败，**但本地照常响铃** |
| 联网后提醒重复 | `本地条目与后端下发重复，按后端版本保留` | 上报成功但响应丢失导致重发，属幂等兜底 |
| 服务端规则没出现 | `已合并：后端 N 条 + 本地 M 条` | 合并正常，本地条目未被覆盖 |

**关于「时钟不可信」**：`reminder.c` 在 `time(NULL) < 2020-01-01` 时拒绝触发
任何提醒。触发条件是**设备从未成功同步过服务端时间**（校时来源只有
`/api/device/sync` 返回的 `server_time`，没有 SNTP、没有外挂 RTC 驱动）。
所以：

- 刚出厂、还没联网激活的设备：不会响铃 → **必须先联网激活一次**；
- 长期断电导致 RTC 归零的设备：不会响铃，直到重新联网。

这是刻意取舍：**响错时间的服药提醒比不提醒更危险**。但要清楚它的代价 ——
「该吃的药没人提醒」也是事故。若要让设备在**永久离线**下也能提醒准点，
必须外挂 DS3231 并实现 I²C 驱动（`elder_reminder.h` 里的注释写的是设计意图，
当前代码尚未实现），详见 `docs/决策记录.md` D11。

**关于离线语音命令**：目前**断网时老人只能用按键确认**（长按 → 30 秒内短按），
说「吃过了」不生效 —— esp-sr MultiNet 未启用（分区表无 `model` 分区 +
未选 `USE_CUSTOM_WAKE_WORD`）。不要向用户承诺语音确认，详见 D11。

命令词**表本身确实已经烧进固件**（9 条，开机日志逐条打印，搜
`离线命令词已预置` 可确认），缺的只是「把识别结果喂给它」的那一层。
同一条日志后面会跟一条 `ESP_LOGW` 明确警告当前语音识别未启用 ——
这是刻意留的，不要在排障时把它当噪声删掉。

**离线回执补发**：断网期间产生的服药回执进 NVS 持久化队列
（容量 32，满了丢最旧一条），联网后 `flush_queue()` 逐条补发。
排查漏掉的服药记录时看 `补发 N 条离线回执，剩余 M`。

### 6.12 本机跑语音链路：Opus 编解码不可用 / 冒烟「假绿」

⚠️ **Docker 部署不会遇到这个问题** —— 镜像构建时已
`apt-get install -y --no-install-recommends libopus0`（`server/Dockerfile:63`，
走 apt 而不是 pip 是有意的：pip 的 opus 绑定会把同一份 C 代码再编一遍）。
**只有本机裸跑（§1.9）才可能撞上。**

**现象**：日志里一条 WARNING：

```
WARNING elder.voice.opus | libopus 未找到，语音编解码不可用（装 libopus0 或设
ELDER_LIBOPUS_PATH 后可恢复）。协议链路不受影响，音频将退化为静音/跳过。
```

**为什么这条最危险**：它**不是报错**。WS 握手、心跳、STT、下行时序全对，
设备端也认这套协议 —— 唯独音频编解码这一步不能做，结果是
**协议层一切正常、老人就是听不见声音**。这与文档里记的「静默故障」同类：
轮数用尽的兜底话术只赋给 reply 不推 speaker，也是协议正常但老人听不到。

**排查**：

```bash
# 1) 确认是不是真的缺库（Windows 上尤其常见）
python -c "from app.voice.opus_codec import libopus_available; print(libopus_available())"

# 2) 装库
#    Debian/Ubuntu: apt-get install -y libopus0
#    macOS:        brew install opus
#    Windows:      把 opus.dll 放到 PATH 或项目目录下
# 3) 还找不到就显式指路径（优先级最高，会跳过自动探测）
export ELDER_LIBOPUS_PATH=C:/lib/opus.dll
```

自动探测会依次尝试 `libopus.so.0` / `libopus.so` / `libopus-0.dll` / `opus.dll` /
`libopus.0.dylib` / `libopus.dylib`；失败时报错文案会列出**全部已试过的候选**，
照着那个列表定位最快。

> ⚠️ **「冒烟全绿」不等于编解码被验过** —— 这是本项目真实踩过的**假绿**：
> 缺可选依赖时测试会静默 SKIP 而不是 FAIL，断言根本没跑，报告却是绿的。
> 现在已加独立的 `libopus_available()` 前置断言把「跳过」变成「红」，
> 但**你自己新写的测试若依赖 libopus，仍然必须配一条前置断言**，否则会重蹈覆辙。

### 6.13 管理后台报 `Failed to fetch` / Mixed Content（HTTPS 页面里的 http 跳转）

**现象**：页面本身能打开（HTTPS 正常），但一调用某些接口就失败：

- 浏览器 Console：`Mixed Content: The page at 'https://152.136.230.132/' was loaded
  over HTTPS, but requested an insecure XMLHttpRequest endpoint
  'http://152.136.230.132/api/admin/presets/'. This request has been blocked;
  the content must be served over HTTPS.`
- 前端只显示一个没有细节的 `Failed to fetch`（fetch 被浏览器拦下，
  连 socket 都没建，所以服务端日志里**一行都没有**）。
- 常见触发路径是**少写了结尾斜杠**的接口，例如 `/api/admin/presets`
  （带斜杠的 `/api/admin/presets/` 反而是 200）。

**根因**（与 §4.1.3 的 `ws://` 是同一个 scheme 坑，换了个马甲）：

1. Starlette 的 `redirect_slashes` 对少斜杠路径发 **307**，
   `Location` 是**绝对地址**，其 scheme 取自 `scope["scheme"]`；
2. 而 `scope["scheme"]` 正是 uvicorn `ProxyHeadersMiddleware` 改写的那一环 ——
   TLS 在前置终止（Cloudflare Tunnel / Nginx / Caddy）时，容器看到的 TCP 对端是
   Docker 网桥网关，不在 `--forwarded-allow-ips` 名单里，
   `X-Forwarded-Proto: https` 不被采纳，scheme 恒为 `http`；
3. 于是浏览器在 HTTPS 页面上收到一个 `http://` 的跳转目标，被 Mixed Content
   策略**先于任何网络请求**拦掉。

**自证**（一条命令，看 `Location` 的 scheme；注意 uvicorn 发出的头名是**小写**
`location`）：

```bash
curl -sI http://<宿主机>:8064/api/admin/presets | grep -i '^location'
# 错：location: http://152.136.230.132/api/admin/presets/
# 对：location: https://152.136.230.132/api/admin/presets/
```

**处置**（按推荐顺序）：

1. **显式配对外地址**（治本，与 §4.1.3 同一套药）：
   `ELDER_DEPLOY_MODE=tunnel` + `ELDER_PUBLIC_BASE_URL=https://152.136.230.132`。
   配了之后地址与请求无关，scheme 推导退居二线，307 自然带上 `https://`。
2. **把可信反代填进 `ELDER_TRUSTED_PROXY_IPS`**（让推导自动生效）：
   填 Docker 网桥网关（查法见 §4.1.4）。注意这一个变量同时决定两层 ——
   框架层读 Dockerfile CMD 的 `--forwarded-allow-ips ${ELDER_TRUSTED_PROXY_IPS:-...}`，
   应用层读 `server/app/ratelimit.py` 的同一变量。**两层语义必须一致，
   切勿一边填网关一边把 CMD 写成 `*`**（`*` 的两层语义相反，见 §5.3）。
3. ~~前端统一补结尾斜杠~~：这只是让 307 不发生，属于绕过；换一个
   不带斜杠的接口（或将来加一个需要跳转的路由）照样复发。**不建议**作为修复。

**回归看守**：`tools/smoke_proxy_headers.py` 的 C1/C2（同一配置下 307 的
`Location` 必须是 `https://`；把 `forwarded_allow_ips` 换成不可信地址即复现事故）、
C3（`proxy_headers=False` 的反向验证）与 D 组（Dockerfile 静态核对：
`--proxy-headers` 必须在、默认值不能是 `*`）。CI 已接入。

---

## 7. 健康检查与监控

- 容器自带 `HEALTHCHECK`，每 30 秒请求 `/api/health`，`docker compose ps` 看 `(healthy)`。
- 外部监控直接探 `http://<host>:8064/api/health`，返回 `{"ok":true}` 即正常。

### 7.1 后台定时任务

排期补全、超时升级、离线清扫由 lifespan 里起的 `scheduler` 后台任务驱动
（间隔 `ELDER_SCHEDULER_INTERVAL`，默认 60 秒一轮，预排期提前量 `ELDER_SCHEDULE_AHEAD_HOURS` 默认 36 小时）。

> 这里的「订单过期」「会员到期标记」两步已随平台会员制取消删除 —— 它们服务的
> `membership_orders` / `memberships` 表已无 ORM 映射且被启动期的
> `schema_patch.drop_membership_tables()` DROP。设备付费开通（线下转账 +
> 管理员人工核销）走的是设备订单表，不在这个定时任务里。

- 它挂掉**不会**让 API 不可用，所以 `/api/health` 仍然 200 —— 监控探活探不出定时任务半瘫。
- 一轮里某一步抛异常会带走**这一轮剩下的所有步骤**，所以日志里出现 scheduler 的 traceback
  要当回事处理，不要只看接口还能通就放过。

#### 7.1.1 用 `/api/health/scheduler` 看定时任务是否在跑

```bash
curl -s http://<host>:8064/api/health/scheduler
```

```json
{
  "last_ok_at": "2026-10-15T14:03:12+08:00",
  "consecutive_failures": 0,
  "last_error": null
}
```

| 字段 | 含义 | 判读 |
| --- | --- | --- |
| `last_ok_at` | 最近一次完整跑完的时刻（带时区） | 为 `null` 表示启动后一轮都没成功过；正常情况下应随每轮推进 |
| `consecutive_failures` | 连续失败次数 | 成功一轮即归零；持续大于 0 说明定时任务半瘫 |
| `last_error` | 最近一次异常信息 | 配合日志定位，成功时为 `null` |

建议的告警条件：`consecutive_failures > 0` 持续 5 分钟，或
`last_ok_at` 早于当前时间 10 分钟以上。

**为什么单独开一个端点，而不把状态并进 `/api/health`**：
`HEALTHCHECK` 每 30 秒打一次 `/api/health`，它一旦非 200，容器就被判定
`unhealthy` —— 可此时进程还在、`8064` 上的业务 API 也照样能服务，只是后台的
排期补全 / 离线清扫没跑成。为了后台任务把整个容器判死，连带代价太大。
所以两者职责严格分开：`/api/health` 只表达「进程活着」（永远 200），
`/api/health/scheduler` 只给运维和监控看，**不参与任何探活链路**。

---

## 8. 数据备份与恢复

### 8.1 SQLite（默认形态）

**库跑在 WAL 模式**（`app/db.py::apply_sqlite_pragmas`，2026-10-20 起）。
WAL 下库旁边会有 `elder.db-wal` / `elder.db-shm`，**还没 checkpoint 的事务只在 -wal 里**
—— 直接 `cp elder.db` 会静默丢最近的事务（文件大小看着正常，数据却是旧的）。

**推荐：不停服在线备份**（SQLite 官方 backup API，拷出来的一定是一致快照）：

```bash
docker exec elder-server python -c "
import sqlite3
src=sqlite3.connect('/data/elder.db'); dst=sqlite3.connect('/data/backup.db')
src.backup(dst); dst.close(); src.close()"
docker cp elder-server:/data/backup.db ./elder.db.$(date +%Y%m%d-%H%M%S)
```

**或者停服后整目录拷**（要连 `-wal` / `-shm` 一起，只拷主文件不够）：

```bash
docker compose stop elder-server
docker run --rm -v server_elder-data:/data -v "$PWD:/backup" busybox \
  sh -c 'cp /data/elder.db* /backup/'
docker compose start elder-server
```

**恢复**（同样先停服；恢复后要把旧的 `-wal`/`-shm` 删掉，否则会和主文件对不上）：

```bash
docker compose stop elder-server
docker run --rm -v server_elder-data:/data -v "$PWD:/backup" busybox \
  sh -c 'rm -f /data/elder.db-wal /data/elder.db-shm && cp /backup/elder.db.20261015 /data/elder.db'
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
  当前 `NEW_COLUMNS` 登记了 **26 列**（SQLite 类型 / MySQL 类型），分布在 5 张表上：
  `users` 1 列、`devices` 1 列、`ai_providers` 20 列、`avatars` 3 列、
  `agent_persona_profiles` 1 列。其中最关键的几条：

  | 表 | 列 | SQLite | MySQL |
  | --- | --- | --- | --- |
  | `users` | `email` | `VARCHAR(128) DEFAULT ''` | `VARCHAR(128) DEFAULT ''` |
  | `devices` | `agent_id` | `INTEGER` | `INT NULL` |
  | `ai_providers` | `system_prompt` | `TEXT DEFAULT ''` | `TEXT` |
  | `ai_providers` | `api_style` | `VARCHAR(32) DEFAULT ''` | `VARCHAR(32) DEFAULT ''` |
  | `ai_providers` | `record_type` | `VARCHAR(16) DEFAULT 'config'` | `VARCHAR(16) DEFAULT 'config'` |
  | `ai_providers` | `traits` / `guardrails` | `TEXT DEFAULT '{}'` | `JSON`（MySQL 的 JSON 不允许带 DEFAULT） |
  | `avatars` | `record_type` | `VARCHAR(16) DEFAULT 'preset'` | `VARCHAR(16) DEFAULT 'preset'` |
  | `agent_persona_profiles` | `agent_key` | `INTEGER DEFAULT 0` | `INT NOT NULL DEFAULT 0` |

  > ⚠️ **`api_style` 这条是真实踩过的坑**：它在 `models.py` 里早就带
  > `server_default`，但一直没登记进 `NEW_COLUMNS` —— 比它更老的老库升级上来后
  > 第一次 `SELECT ai_providers` 就抛 `no such column: api_style`，服务直接起不来。
  > 同一批补登记的还有 `supports_stream` / `timeout_sec` / `max_retries` /
  > `context_window`。完整清单请直接看 `schema_patch.NEW_COLUMNS`，不要照抄本表。

  **以后每新增一个模型字段，都要同步往 `schema_patch.NEW_COLUMNS` 里登记一行**
  （SQLite 与 MySQL 两种 DDL 都要写，且**必须带 `server_default`** ——
  不带默认值时老行读出 NULL，第一次 SELECT 就会把 Pydantic 序列化炸掉），
  否则老库升级上来会 `no such column` —— 这个坑踩过一次就够了。
  只补列、不改类型、不删列、不建索引；需要完整迁移能力时再考虑引入 Alembic。
