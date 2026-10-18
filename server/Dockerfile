# 老人陪伴提醒助手 · 业务后端镜像
#
# 端口约定：
#   容器内固定监听 8080（ELDER_PORT 可覆盖）
#   对外只发布一个端口，默认 8064：  -p 8064:8080
#   生产环境由 nginx 反代 https://voice.250886.xyz -> 127.0.0.1:8064
FROM python:3.11-slim

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
#               ELDER_PUBLIC_BASE_URL=https://voice.2508866.xyz   （HTTP/OTA，必需）
#               ELDER_WS_PUBLIC_BASE_URL=wss://voice-ws.2508866.xyz （WS 单独域名，可选）
#             旧变量 ELDER_VOICE_PUBLIC_BASE 仍然有效（向后兼容），新部署建议用
#             ELDER_PUBLIC_BASE_URL（任何模式下都校验 scheme）。
#
# ⚠️ 上面 ENV 里的 auto 与 config.py 的默认值一致，显式声明只是为了让
# `docker inspect` 能一眼看到当前模式；compose 的 environment 会覆盖它。
# ⚠️ 刻意**不**给 CMD 的 uvicorn 加 --proxy-headers / --forwarded-allow-ips：
# 传 * 会让登录限速形同虚设（XFF 最左段可伪造），应用层按
# ELDER_TRUSTED_PROXY_IPS 名单从右往左解析，见 docs/DEPLOY.md §4.1.4。

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

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${ELDER_PORT:-8080}"]
