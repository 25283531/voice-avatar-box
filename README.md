# voice-avatar-box · 老人陪伴 & 提醒助手

面向老年人的语音陪伴与服药提醒助手，硬件基于 **立创·实战派 ESP32-S3**，
后续移植 **ESP32-S3-BOX-3 / ESP-VoCat**。

**语音链路是自研的**（`server/app/voice/`：WebSocket 服务端 + Opus 编解码 + 会话编排 + OTA），
与业务 API 同进程同端口，运行时**不依赖任何第三方语音服务器**。
固件侧借鉴开源 [xiaozhi-esp32](https://github.com/xinnan-tech/xiaozhi-esp32) 的
**公开协议**（设备侧 WebSocket 帧格式、OTA 响应结构）与固件设计，
从而在固件零改动的前提下自研服务端——**协议兼容 ≠ 调用它的服务器**：
[xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) 只作为协议与固件设计的参考，
本项目不部署、不调用它（早期曾跑过它的容器，已删除）。设备出厂 OTA 地址默认指向本项目自己的域名。

本项目自己做的「业务面」：设备绑定、提醒与回执、形象/提示词、AI 平台配置、交互统计、手机端与管理后台。

## 目录

| 路径 | 说明 |
| --- | --- |
| `server/` | **单一镜像**的服务端：FastAPI 业务后端 + 自研语音链路 + 记忆与人格模块 + 多模型网关 + Docker 部署文件 |
| `app/` | 手机端家属端 APP（uni-app v3 + Vue3），四个栏目：首页 / 设备 / 日程 / 我的；出 H5 / 微信小程序 / Android APK |
| `android/` | Android 原生壳工程（WebView + `WebViewAssetLoader`），把 H5 打成 APK，不依赖 DCloud |
| `firmware/` | ESP32-S3 固件（`components/elder` 为本项目新增组件，编译前需合入 xiaozhi 源码树） |
| `tools/` | 辅助脚本：`smoke_server.py` 后端冒烟、`make_firmware.py` 合并烧录 bin、`idfwin.py` / `winrun.py` Windows 下的 IDF 包装 |
| `docs/` | [`架构设计.md`](docs/架构设计.md) 平台整体架构与选型 · [`DEPLOY.md`](docs/DEPLOY.md) 部署与 MySQL 接入 · [`会员功能规划.md`](docs/会员功能规划.md) 会员与人工核销 · [`决策记录.md`](docs/决策记录.md) 用户拍板的决策及其边界 · 烧录指南 · 手机 APP 开发说明 |
| `老人陪伴提醒助手-技术方案-v1.md` | 完整技术方案（硬件、形象引擎、激活码算法、风险清单） |

## 端口与地址约定

| 项 | 值 | 备注 |
| --- | --- | --- |
| 容器内监听 | `8080` | `ELDER_PORT` 可覆盖 |
| **对外发布** | **`8064`** | 镜像只发布这一个端口，`-p 8064:8080`。HTTP API、语音 WebSocket、OTA 全复用它 |
| 语音 WebSocket | `ws(s)://<域名>/api/voice/v1/ws` | **自研链路**，与 API 同源同端口，不需要额外开端口 |
| 生产域名 | `https://voice.2508866.xyz` | nginx 反代到 `127.0.0.1:8064`，手机端已固化此地址；反代需放行 WebSocket 升级头 |
| 管理后台 | `https://voice.2508866.xyz/` | 默认账号 `admin` / `admin123`，登录后请立即改密 |
| 接口文档 | `https://voice.2508866.xyz/docs` | FastAPI 自动生成 |

## GitHub Actions 工作流

| 工作流 | 触发 | 作用 |
| --- | --- | --- |
| `pr-gate.yml` | push 到 `main` + **每个 PR**（刻意不加 paths 过滤） | 常驻门禁「守门人自检」：CODEOWNERS 覆盖 + workflows 可解析。push 时自动跑，结果可见但**不再拦截推送**（见下方流程变更） |
| `server-ci.yml` | push/PR 改动 `server/`、`tools/`；可手动 | 安装依赖跑 `tools/smoke_server.py`（21 项业务闭环）+ Dockerfile 语法检查 |
| `docker-build.yml` | push/PR 改动 `server/`；可手动 | 构建镜像 → 起容器校验 `/api/health`；手动触发勾选 `publish` 才推送 `ghcr.io/25283531/voice-avatar-box` |
| `app-build.yml` | push/PR 改动 `app/`；可手动 | 构建 H5 / 微信小程序并上传产物；校验后端地址已内联 |
| `android-build.yml` | push/PR 改动 `app/`、`android/`；可手动 / 发 Release | **一次产出可安装的 Android APK**（H5 + WebView 壳，全程在 runner 内，不用 DCloud 账号） |

增量触发：每个工作流都带 `paths` 过滤，推送只跑受影响的那条
（例如只改 `docs/` 或 `firmware/` 不会触发任何构建；改 `tools/` 只跑 `server-ci`）。

手动产出最终产物：**Actions → 对应工作流 → Run workflow**。

- `docker-build`：勾选 `publish`、填 `tag`（如 `v0.1.0`）→ 推送镜像，产物在 ghcr 包列表
- `app-build`：选 `both` / `h5` / `mp-weixin` → 产物在 Actions 运行页的 Artifacts
- `android-build`：选 `self`（仓库证书签名）/ `debug` → 产物为可直接安装的 APK

### 手机 APP 栏目

APP 是给家属用的，不接入小智任何服务，业务后端地址在代码级写死
（`app/src/http/elder.ts`，域名 `https://voice.2508866.xyz`），**不提供任何网络设置入口**。

| 栏目 | 内容 |
| --- | --- |
| 首页 | 已绑定设备、设备状态（网络连接 / 设备时间）、当日服药状态（待服数量、待服时间点、已确认次数）、会员状态（是否开通 / 到期时间 / 权益） |
| 设备 | 添加设备（激活码）、删除设备、重命名、给设备配网、为每台设备绑定不同 agent 角色 |
| 日程 | 服药提醒与日程提醒：新增 / 编辑 / 暂停 / 代老人确认 / 删除 |
| 我的 | 账户名、修改密码、更换绑定邮箱、开通（续费）会员、创建自有 agent 角色、系统形象库、缓存管理、应用信息、隐私政策、用户协议 |
- `android-build`：填版本号 → 产物 Artifacts 里的 `elder-app-android-apk`

## 分支保护与 PR 流程

仓库默认分支是 **`main`**。

⚠️ **流程已变更（2026-10-16，决策 D5）**：早期是「功能分支 + PR + 评审批准」，
实测跑不通后改为**直接提交并推送到 `main`**。原因是单协作者仓库的自审被 GitHub
拒绝（`POST /pulls/{n}/reviews` 返回 422、合并返回 405「At least 1 approving
review is required」），而"过严的守门人等于没有守门人"—— 保护规则挡住了
唯一一个本该干活的人，代码因此长期停在分支上进不了 main，连部署都拿不到。

现在的流程固定为：

```
改代码 → 本地跑门禁判据（exit 0）→ commit → 直接 push 到 main
```

**main 上仍保留的保护**：禁强推、禁删分支、`enforce_admins`（管理员也不豁免）。
**已移除**：必需状态检查「守门人自检」—— 它是唯一会拦截直接 push 的规则，
留着就直接推不上去。`pr-gate` 改为 push 后运行：仍然每次都跑、结果在 PR /
commit 页可见，**只是不再拦截**。若将来恢复多人协作，把它加回来即可
（`PATCH /repos/{o}/{r}/branches/main/protection`，改名前先改 GitHub 仓库设置，
改必需状态检查名 = 保护失效）。

推 main 前，本地先跑一遍门禁判据（与 CI 同一个脚本，exit 0 才推）：

```bash
python tools/ci_gate_check.py
```

> **为什么移除状态检查后还留着 CODEOWNERS 与 pr-gate**：它们的作用从"合并前的
> 自动关卡"变成了"push 后的自动体检 + 危险路径所有权的登记簿"。危险路径清单
> 本身仍然有效（见下一节）—— 它回答的是"改这些地方时该找谁、该多想一层"，
> 这个价值与走不走 PR 无关。

### 危险路径清单（改这些路径前必须自己多想一层）

登记在 `.github/CODEOWNERS`（默认行 `* @25283531` + 下列显式条目）。
「出事就是数据损坏」不是形容词，是事故链路的第一环：

| 路径 | 为什么危险 |
| --- | --- |
| `server/app/schema_patch.py` | **启动期写表**：backfill / dedupe / `_patch_version` 登记跑在每次进程启动时，判据决定「哪些行会被改写」 |
| `server/app/main.py` | 启动 seed 人格预设：平台预设角色的唯一来源（改一次发版一次，运营无法调人格） |
| `server/app/db.py` | `init_db` / `wait_for_db`：建表与连库等待，是回填的执行环境 |
| `server/app/persona/` | 人格红线目录：5 条硬红线（一律不给结论、不做判断、不推荐具体疗法或药品等）。红线被改坏不会自己变红 |
| `server/evals/` | 红线黄金副本基准：评测判据的权威数据，与 `catalog.py` 必须同时改才自洽 |
| `.github/workflows/` | CI 本体：改这里 = 改「什么能进 main」 |

事故链路（真实发生过的 P0，不是假想）：

```
启动回填判据写错 → 重启时改写用户角色归属 → 用户派生角色被永久刷成 preset
→ 角色变只读 + 全平台可见 = 跨用户数据泄漏
```

没有报警、没有堆栈，用户侧看不出任何异常，等发现时数据已被改写多轮。

⚠️ **这些路径出事时没有任何自动信号**。流程改为直接推 main 后（D5），
「合并前有人看过 diff」这道关卡已经不存在了，所以唯一的防线变成了
**动手前的自查**。改这些文件时逐条问自己：

1. 这次改动会不会让某个**判据**（哪些行算 preset / 是否已执行过）的含义变化？
2. 如果判据写错，**重启时**会发生什么？会不会无信号地改写用户数据？
3. 有没有一条断言能在判据写错时**变红**？如果没有，先补断言再改判据。

第 3 条是本项目用真实 P0 换来的纪律：`schema_patch.py` 的启动回填曾把用户派生
角色永久刷成 preset（跨用户数据泄漏），当时**没有任何测试能发现它**。

### 守门人自检守得住什么、守不住什么

| 守得住（机械可判，CI 红） | 守不住（没有机械判据，靠人） |
| --- | --- |
| CODEOWNERS 存在且每条规则语法合法 | **同时篡改代码与基准的自洽绕过**（改 `catalog.py` 再重新生成黄金副本，diff 两处都变、门禁全绿）—— 靠人工 review 看 diff 全貌 |
| 关键危险路径仍被显式（非通配）规则覆盖，删行就红 | **语义正确性**（判据写错但语法合法）—— 靠人工 review + 变异测试「证明它会红」的纪律 |
| `.github/workflows/*.yml` 全部可被 `yaml.safe_load` 解析 | **覆盖 ≠ 真的有人看**——分支保护是否强制 review 是 GitHub 侧设置，CI 只代表文件层面完好 |

本项目哲学：**不写清边界的断言是负债**。每个断言旁边都必须写清它守不住
什么，否则下一个人会把「门禁是绿的」当成「代码是对的」。

## 服务器部署

默认**单一镜像**：业务、会员、语音链路、角色与记忆、邮件——全部平台级能力都在同一个容器里，
默认内置 SQLite，不需要任何外部数据库或额外的语音服务容器。

```bash
cd server
cp .env.example .env            # 至少改 ELDER_JWT_SECRET
docker compose up -d            # 对外 http://<host>:8064
```

> 语音链路是自研的（`server/app/voice/`），与本服务同进程、**同端口 8064**，
> 设备连 `ws(s)://<域名>/api/voice/v1/ws`，地址由服务端自动下发。
> **不再依赖任何第三方语音服务端**，镜像也不需要第二个端口。

只有在用户量上来之后才需要接 MySQL（**仅换数据库，不拆分服务**），
详见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

```bash
# MySQL 变体是叠加文件，必须和主文件一起加载
docker compose -f docker-compose.yml -f docker-compose.mysql.yml up -d
```

nginx 反代片段：

```nginx
server {
    listen 443 ssl;
    server_name voice.2508866.xyz;
    # ssl_certificate / ssl_certificate_key ...

    location / {
        proxy_pass http://127.0.0.1:8064;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;      # 管理后台长轮询
    }
}
```

## 手机端

```bash
cd app
pnpm install
pnpm build:h5          # 产物 app/dist/build/h5
pnpm build:mp-weixin   # 产物 app/dist/build/mp-weixin，用微信开发者工具上传
```

- 后端地址**代码级写死** `https://voice.2508866.xyz`（`app/src/http/elder.ts` 的 `BASE_URL` 常量）
- 页面入口：`src/pages/elder/`（绑定设备、提醒列表与编辑、统计、形象选择）

### Android APK（CI 一次出包，不依赖 DCloud）

`android-build.yml` 的流程：构建 H5（`VITE_APP_PUBLIC_BASE=./`，资源走相对路径）
→ 拷进 `android/app/src/main/assets/web` → Gradle 打 Release 包 → 上传 APK。

页面由 `android/app/src/main/java/com/elder/companion/MainActivity.kt` 里的 WebView 承载，
用 `androidx.webkit.WebViewAssetLoader` 把 assets 映射成虚拟域 `https://elder.local/assets/web/index.html`
——这样页面有正常 origin，`localStorage` 与跨域请求行为和线上 H5 完全一致。

之所以不走 uni-app 云打包：uni-app 的 App 端原生运行时是 DCloud 闭源 AAR，只有云打包能出 APK，
而且 HBuilderX 3.9+ 起强制要求登录 + 绑定手机号 + 实名认证，免费账号每月仅 5 次配额。
本项目手机端不依赖 App 原生能力（唯一用到 `plus.runtime.restart()` 的地方已有 H5 降级分支），
所以 WebView 壳即可完整运行，构建也完全在 GitHub Actions 里完成。

签名（Secret，可选；不配会自动回落到 debug 签名，依然可安装）：

| Secret | 用途 |
| --- | --- |
| `ANDROID_KEYSTORE_BASE64` | 证书文件 base64（当前这份实际是 PKCS12，Gradle 会按文件 magic 自动识别） |
| `ANDROID_KEY_ALIAS` | 别名，当前 `__uni__g9c80343` |
| `ANDROID_KEY_PASSWORD` / `ANDROID_STORE_PASSWORD` | 密钥与密钥库口令 |

跑一次：**Actions → android-build → Run workflow**，
产物在 Artifacts：`elder-app-android-apk`。

> iOS：没有 Apple 开发者证书（p12 + mobileprovision 缺一不可），暂不支持。

> `app-pack.yml`（uni-app 云打包）**已废弃**：DCloud 账号登录失败且强制实名认证，
> 现已改走 `android-build.yml` 的 H5 + WebView 壳方案，全程在 GitHub runner 内出 APK。
> 两个 Secret，且该账号必须已在 DCloud 注册并实名。

## 固件

`firmware/components/elder` 已实现：RGB 指示灯与情绪同步、长按 3s + 30s 内短按的服药确认状态机、
离线提醒（DS3231 + NVS）、13 位激活码（1 字母 + 11 位数字 + Luhn 校验位，与后端算法一致）。

**尚未编译**：需先合入 xiaozhi-esp32 v2.0.5 源码树（ESP-IDF v5.5.1）并补齐 EAF 形象播放、
提醒与后端同步、离线提示音，完成后再统一编译，详见 `docs/烧录指南.md`。
