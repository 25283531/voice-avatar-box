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
| `app/` | 手机端（uni-app v3 + Vue3，复用 xiaozhi `manager-mobile`），出 H5 / 微信小程序 / App |
| `firmware/` | ESP32-S3 固件（`components/elder` 为本项目新增组件，编译前需合入 xiaozhi 源码树） |
| `tools/` | 辅助脚本：`smoke_server.py` 后端冒烟、`make_firmware.py` 合并烧录 bin、`idfwin.py` / `winrun.py` Windows 下的 IDF 包装 |
| `docs/` | 烧录指南、手机 APP 开发说明 |
| `老人陪伴提醒助手-技术方案-v1.md` | 完整技术方案（硬件、形象引擎、激活码算法、风险清单） |

## 端口与地址约定

| 项 | 值 | 备注 |
| --- | --- | --- |
| 容器内监听 | `8080` | `ELDER_PORT` 可覆盖 |
| **对外发布** | **`8064`** | 镜像只发布这一个端口，`-p 8064:8080` |
| 生产域名 | `https://voice.250886.xyz` | nginx 反代到 `127.0.0.1:8064`，手机端已固化此地址 |
| 管理后台 | `https://voice.250886.xyz/` | 默认账号 `admin` / `admin123`，登录后请立即改密 |
| 接口文档 | `https://voice.250886.xyz/docs` | FastAPI 自动生成 |

## GitHub Actions 工作流

| 工作流 | 触发 | 作用 |
| --- | --- | --- |
| `server-ci.yml` | push/PR 改动 `server/`、`tools/`；可手动 | 安装依赖跑 `tools/smoke_server.py`（21 项业务闭环）+ Dockerfile 语法检查 |
| `docker-build.yml` | push/PR 改动 `server/`；可手动 | 构建镜像 → 起容器校验 `/api/health`；手动触发勾选 `publish` 才推送 `ghcr.io/25283531/voice-avatar-box` |
| `app-build.yml` | push/PR 改动 `app/`；可手动 | 构建 H5 / 微信小程序并上传产物；校验后端地址已内联 |

手动产出最终产物：**Actions → 对应工作流 → Run workflow**。

- `docker-build`：勾选 `publish`、填 `tag`（如 `v0.1.0`）→ 推送镜像，产物在 ghcr 包列表
- `app-build`：选 `both` / `h5` / `mp-weixin` → 产物在 Actions 运行页的 Artifacts

## 服务器部署

```bash
cd server
cp .env.example .env            # 至少改 ELDER_JWT_SECRET、MYSQL_ROOT_PASSWORD
docker compose up -d            # 对外 http://<host>:8064
docker compose --profile xiaozhi up -d   # 额外拉起小智语音链路（8000/8003/8002）
```

nginx 反代片段：

```nginx
server {
    listen 443 ssl;
    server_name voice.250886.xyz;
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

- 后端地址固定 `https://voice.250886.xyz`（`app/env/.env` 的 `VITE_ELDER_BASEURL`）
- Android / iOS 原生包请用 HBuilderX「发行 → 原生App云打包」，不走 CI
- 页面入口：`src/pages/elder/`（绑定设备、提醒列表与编辑、统计、形象选择）

## 固件

`firmware/components/elder` 已实现：RGB 指示灯与情绪同步、长按 3s + 30s 内短按的服药确认状态机、
离线提醒（DS3231 + NVS）、13 位激活码（1 字母 + 11 位数字 + Luhn 校验位，与后端算法一致）。

**尚未编译**：需先合入 xiaozhi-esp32 v2.0.5 源码树（ESP-IDF v5.5.1）并补齐 EAF 形象播放、
提醒与后端同步、离线提示音，完成后再统一编译，详见 `docs/烧录指南.md`。
