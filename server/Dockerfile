# 老人陪伴提醒助手 · 业务后端镜像
#
# 端口约定：
#   容器内固定监听 8080（ELDER_PORT 可覆盖）
#   对外只发布一个端口，默认 8064：  -p 8064:8080
#   生产环境由 nginx 反代 https://voice.250886.xyz -> 127.0.0.1:8064
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PIP_NO_CACHE_DIR=1 \
    ELDER_PORT=8080

WORKDIR /app

# tzdata 走 pip 安装，避免在 slim 镜像里 apt-get；
# 没有它 zoneinfo.ZoneInfo("Asia/Shanghai") 会直接抛异常。
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%d/api/health' % int(os.environ.get('ELDER_PORT','8080')))"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${ELDER_PORT:-8080}"]
