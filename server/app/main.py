"""老人陪伴提醒助手 · 业务后端入口。

职责边界
--------
- 本服务负责**业务面**：设备绑定、提醒规则与回执、形象与提示词、AI 供应商配置、交互统计。
- 语音 / AI 链路复用 xiaozhi-esp32-server（WebSocket + OTA），本服务不重复造轮子，
  仅在 /api/device/sync 里把 WS / OTA 地址下发给设备。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import APP_NAME, APP_VERSION, CORS_ORIGINS
from .db import SessionLocal, init_db
from .models import Avatar
from .routers import avatars, auth, device_api, devices, providers, reminders, stats
from .security import hash_password

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("elder")

STATIC_DIR = Path(__file__).parent / "static"


def _seed() -> None:
    """首次启动写入种子数据：默认管理员 + 默认形象。"""
    from . import models
    from .routers.avatars import DEFAULT_CLIPS, DEFAULT_LIGHTS

    with SessionLocal() as db:
        if db.query(models.User).count() == 0:
            admin = models.User(
                username="admin",
                password_hash=hash_password("admin123"),
                nickname="管理员",
                is_admin=True,
            )
            db.add(admin)
            log.info("seed: created default admin / admin123  <- 请登录后立即修改密码")

        if db.query(Avatar).count() == 0:
            db.add(Avatar(
                name="小暖（默认）",
                description="温和陪伴风格，语速偏慢，适合老年用户",
                eaf_pack_name="xiaonuan",
                screen_width=320,
                screen_height=240,
                clips=dict(DEFAULT_CLIPS),
                system_prompt=(
                    "你是小暖，一位耐心、温和的陪伴助手，服务对象是老年人。\n"
                    "说话简短清楚，每句不超过 20 个字，语速放慢。\n"
                    "对方听不清时要主动重复，不要使用网络流行语和英文缩写。\n"
                    "涉及服药、身体不适时，提醒对方联系家人或医生，不要给出医疗建议。"
                ),
                tts_voice="zh-CN-XiaoxiaoNeural",
                emotion_lights=dict(DEFAULT_LIGHTS),
            ))
            log.info("seed: created default avatar")
        db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _seed()
    from . import scheduler

    task = asyncio.create_task(scheduler.run_forever())
    log.info("%s v%s started", APP_NAME, APP_VERSION)
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan,
              description="老人陪伴 & 提醒助手业务后端（与 xiaozhi-esp32-server 协同）")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth.router, devices.router, reminders.router, avatars.router,
          providers.router, stats.router, device_api.router):
    app.include_router(r)


@app.get("/api/health")
def health():
    return {"ok": True, "service": APP_NAME, "version": APP_VERSION}


# ---------------------------------------------------------------- 管理页面
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def console():
        return FileResponse(str(STATIC_DIR / "index.html"))
else:  # pragma: no cover
    @app.get("/")
    def console_placeholder():
        return {"message": "静态管理页面未打包，请访问 /docs 查看接口"}
