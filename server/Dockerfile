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
    ELDER_DB=sqlite:////data/elder.db

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
