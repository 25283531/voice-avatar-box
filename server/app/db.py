"""数据库引擎 / 会话 / 基类。"""
from __future__ import annotations

import logging
import time

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL, DB_ECHO, DB_POOL_PRE_PING, DB_WAIT_SEC

log = logging.getLogger(__name__)

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


def wait_for_db(max_wait: float | None = None) -> None:
    """阻塞直到数据库真正可以执行查询（指数退避）。

    为什么需要：即使 docker-compose 里配了 `depends_on: service_healthy`，
    `mysqladmin ping` 通过 ≠ mysqld 已能接受业务查询（初始化数据目录后
    mysqld 还会重启一次；Windows/WSL2 上这个窗口尤其宽）。在这段时间里
    客户端拿到的典型报错就是：

        pymysql.err.OperationalError: (2013, 'Lost connection to
        MySQL server during query')

    发生在 pymysql 读取握手包的 `_get_server_information()` 阶段。
    这里采用「先重试、后放弃」，超时仍不可用才抛错退出，
    避免容器反复崩溃重启却始终起不来。
    """
    if _is_sqlite:
        return

    budget = DB_WAIT_SEC if max_wait is None else max_wait
    deadline = time.monotonic() + budget
    delay = 1.0
    attempt = 0
    while True:
        attempt += 1
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            if attempt > 1:
                log.info("数据库已就绪（第 %d 次尝试）", attempt)
            return
        except Exception as exc:  # noqa: BLE001  连接期任何异常都应重试
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.error("等待数据库超时（共尝试 %d 次，%.0fs）：%s", attempt, budget, exc)
                raise
            wait = min(delay, remaining)
            log.warning(
                "数据库尚不可用（第 %d 次尝试，已等待 %.0fs/%.0fs）：%s；%.1fs 后重试",
                attempt, budget - remaining, budget, str(exc).splitlines()[0][:160], wait,
            )
            time.sleep(wait)
            delay = min(delay * 1.5, 5.0)


def init_db() -> None:
    """建表（首次启动 / 开发环境）。生产建议用 Alembic 迁移。"""
    from . import models  # noqa: F401  触发模型注册

    wait_for_db()
    Base.metadata.create_all(bind=engine)
