# voice-avatar-box · 老人陪伴 & 提醒助手

面向老年人的语音陪伴与服药提醒助手，硬件基于 **立创·实战派 ESP32-S3**，
后续移植 **ESP32-S3-BOX-3 / ESP-VoCat**。语音与 AI 链路复用开源
[xiaozhi-esp32](https://github.com/xinnan-tech/xiaozhi-esp32) 与
[xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)，
本项目只做「业务面」：设备绑定、提醒与回执、形象/提示词、AI 平台配置、交互统计、手机端与管理后台。

## 目录

| 路径 | 说明 |
| --- | --- |
| `server/` | FastAPI 业务后端 + WEB 管理后台（单页，离线可用）+ Docker 部署文件 |
| `app/` | 手机端家属端 APP（uni-app v3 + Vue3），四个栏目：首页 / 设备 / 日程 / 我的；出 H5 / 微信小程序 / Android APK |
| `android/` | Android 原生壳工程（WebView + `WebViewAssetLoader`），把 H5 打成 APK，不依赖 DCloud |
| `firmware/` | ESP32-S3 固件（`components/elder` 为本项目新增组件，编译前需合入 xiaozhi 源码树） |
| `tools/` | 辅助脚本：`smoke_server.py` 后端冒烟、`make_firmware.py` 合并烧录 bin、`idfwin.py` / `winrun.py` Windows 下的 IDF 包装 |
| `docs/` | 烧录指南、手机 APP 开发说明 |
| `老人陪伴提醒助手-技术方案-v1.md` | 完整技术方案（硬件、形象引擎、激活码算法、风险清单） |

## 端口与地址约定

| 项 | 值 | 备注 |
| --- | --- | --- |
| 容器内监听 | `8080` | `ELDER_PORT` 可覆盖 |
| **对外发布** | **`8064`** | 镜像只发布这一个端口，`-p 8064:8080` |
| 生产域名 | `https://voice.2508866.xyz` | nginx 反代到 `127.0.0.1:8064`，手机端已固化此地址 |
| 管理后台 | `https://voice.2508866.xyz/` | 默认账号 `admin` / `admin123`，登录后请立即改密 |
| 接口文档 | `https://voice.2508866.xyz/docs` | FastAPI 自动生成 |

## GitHub Actions 工作流

| 工作流 | 触发 | 作用 |
| --- | --- | --- |
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

## 服务器部署

默认**内置 SQLite**，一个容器就能跑，不需要任何外部数据库：

```bash
cd server
cp .env.example .env            # 至少改 ELDER_JWT_SECRET
docker compose up -d            # 对外 http://<host>:8064
docker compose --profile xiaozhi up -d   # 额外拉起语音链路（8000/8003/8002）
```

改用 MySQL（含字符集/时区要求、排障与备份）：见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

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
