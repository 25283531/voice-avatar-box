"""数据库引擎 / 会话 / 基类。"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL, DB_ECHO, DB_POOL_PRE_PING

_is_sqlite = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    echo=DB_ECHO,
    pool_pre_ping=DB_POOL_PRE_PING,
    # SQLite 不支持队列池参数
    **({} if _is_sqlite else {"pool_size": 10, "max_overflow": 20, "pool_recycle": 1800}),
    **({"connect_args": {"check_same_thread": False}} if _is_sqlite else {}),
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def get_db():
    """FastAPI 依赖：每个请求一个会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """建表（首次启动 / 开发环境）。生产建议用 Alembic 迁移。"""
    from . import models  # noqa: F401  触发模型注册

    Base.metadata.create_all(bind=engine)
