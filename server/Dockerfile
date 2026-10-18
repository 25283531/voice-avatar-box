# 老人陪伴提醒助手 · 业务后端镜像
#
# 端口约定：
#   容器内固定监听 8080（ELDER_PORT 可覆盖）
#   对外只发布一个端口，默认 8064：  -p 8064:8080
#   生产环境由 nginx 反代 https://152.136.230.132 -> 127.0.0.1:8064（IP 直连）
FROM python:3.14-slim

# 默认用内置 SQLite，库文件放在 /data（docker-compose.yml 把命名卷挂到这里）。
# 四个斜杠 = sqlite:// + 绝对路径，别漏写；切 MySQL 时用 ELDER_DB 覆盖即可。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PIP_NO_CACHE_DIR=1 \
    ELDER_PORT=8080 \
    ELDER_DB=sqlite:////data/elder.db \
    ELDER_DEPLOY_MODE=auto

# ---------------------------------------------------------------- 部署模式
# ELDER_DEPLOY_MODE 决定服务端给设备下发哪套对外地址，三种取值（决策表与
# "我该选哪种"见 docs/DEPLOY.md §4.2；启动期校验与摘要见 app/config.py
# 的 _deploy_startup_check）：
#
#   auto   —— 默认。未显式声明拓扑：按请求现场推导 ws/OTA 地址（历史行为），
#             启动日志会说明推断依据。**只在公网 IP:端口直暴露下天然正确**：
#             镜像没有给 uvicorn 传 --proxy-headers，而 uvicorn 只信任来自
#             127.0.0.1 的 X-Forwarded-Proto；生产里对端是 Docker 网桥网关，
#             scheme 恒为 http，TLS 反代/隧道后面会下发 ws://（能心跳不能说话）。
#   direct —— 显式声明"直暴露/反代后置"：推导逻辑与 auto 完全相同，
#             值是一份写进配置的意图声明 + 启动摘要里明确的模式名。
#   tunnel —— Cloudflare Tunnel 等「TLS 在前置终止」的形态：**必须**配对外
#             地址，否则容器启动直接失败（不静默降级）。配套变量：
#               ELDER_PUBLIC_BASE_URL=https://152.136.230.132   （HTTP/OTA，必需）
#               ELDER_WS_PUBLIC_BASE_URL=wss://voice-ws.250886.xyz （WS 单独域名，可选）
#             旧变量 ELDER_VOICE_PUBLIC_BASE 仍然有效（向后兼容），新部署建议用
#             ELDER_PUBLIC_BASE_URL（任何模式下都校验 scheme）。
#
# ⚠️ 上面 ENV 里的 auto 与 config.py 的默认值一致，显式声明只是为了让
# `docker inspect` 能一眼看到当前模式；compose 的 environment 会覆盖它。
# ⚠️ 这里给 uvicorn 传 --proxy-headers，但 **--forwarded-allow-ips 绝不写 `*`**：
#   传 * 时 uvicorn 的 ProxyHeadersMiddleware.get_trusted_client_host() 走
#   `always_trust` 分支，取 X-Forwarded-For 的**最左段** —— 而那正是请求方可以
#   随便写的一段，等于让攻击者每次换个头就换一个主体，登录限速形同虚设。
#   写成具体 IP 时它走的是**从右往左跳过可信代理**，取到第一个不可信地址，
#   与 app/ratelimit.py::client_ip() 的算法逐字一致 —— 两边解析出的真实客户端
#   是同一个，限速器照常工作。详见 docs/DEPLOY.md §4.1.4。
#
# 为什么必须传（不传的代价不只是限速）
# -------------------------------------
# uvicorn 用它来决定 `request.url.scheme`。不传时只有来自 127.0.0.1 的
# X-Forwarded-Proto 被采纳，而生产里对端是 Docker 网桥网关 ⇒ scheme 恒为
# http。后果有两个，都是**运行期零报错**的那类：
#   1. FastAPI 的 redirect_slashes 生成 307 时拼出 `Location: http://域名/...`，
#      浏览器在 HTTPS 页面上跟这个重定向 → Mixed Content → "Failed to fetch"；
#   2. config.py 按请求 scheme 推导下发地址，设备拿到 ws:// 打 443，
#      表现为"能心跳不能说话"。
# 默认值 127.0.0.1 与 uvicorn 自己的默认值相同，**不改变任何现有部署的行为**；
# 反代在容器里的（Caddy / Nginx / OpenResty 与后端同 compose 网络）把
# ELDER_TRUSTED_PROXY_IPS 设成 Docker 网桥网关即可（查法见 DEPLOY.md）。

WORKDIR /app

# /data 是内置 SQLite 的落点，先建出来：即便用户没挂卷也能直接跑起来
# （挂了命名卷/bind mount 时会被挂载点覆盖，不影响）。
RUN mkdir -p /data

# tzdata 走 pip 安装，避免在 slim 镜像里 apt-get；
# 没有它 zoneinfo.ZoneInfo("Asia/Shanghai") 会直接抛异常。
#
# libopus0 走 apt 而不是 pip —— 这与 tzdata 的选择**理由不同**，别"统一风格"：
# tzdata 有纯数据等价物（pip 的 tzdata 就是同一份 IANA 数据），
# 而 Opus 是 C 扩展，pip 生态里没有"纯数据"替代，可选只有自带 vendor 的
# wheel（把同一份 C 代码装第二遍）。且 Debian slim 里 apt 的 libopus0 是
# 官方维护包，能直接吃到安全更新。装完立刻清 apt 列表，别让层变大。
#
# ⚠️ edge-tts / av（requirements.txt 里，Edge TTS 内置桥所需）**都是纯 wheel**，
# 自带 ffmpeg，不需要 apt 装 ffmpeg —— 别看着"要解 mp3"就顺手加
# ffmpeg，那会让镜像白白大几百 MB。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%d/api/health' % int(os.environ.get('ELDER_PORT','8080')))"

# ⚠️ ELDER_TRUSTED_PROXY_IPS 与 ELDER_PORT 同样用 `${VAR:-默认}` 展开，
#    由 `sh -c` 完成（exec 形式里 Docker 自身不做变量替换，交给 shell）。
#    ⚠️ 这个值**不要带空格**：未加引号展开时 "172.18.0.1, 10.0.0.5" 会被 shell
#    切成两个参数，uvicorn 直接报参数错误。逗号紧挨着写。
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${ELDER_PORT:-8080} --proxy-headers --forwarded-allow-ips ${ELDER_TRUSTED_PROXY_IPS:-127.0.0.1}"]
