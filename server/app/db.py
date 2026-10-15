"""数据库引擎 / 会话 / 基类。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL, DB_ECHO, DB_POOL_PRE_PING, DB_WAIT_SEC

log = logging.getLogger(__name__)

_is_sqlite = DATABASE_URL.startswith("sqlite")


def _ensure_sqlite_parent_dir() -> None:
    """SQLite 是「一个文件就是一个库」，父目录不存在会直接报
    `sqlite3.OperationalError: unable to open database file`。

    命名卷挂进容器后 /data 一般已经存在，但下面两种情况目录可能还没有：
      1. 宿主机目录挂载（bind mount）时目标目录尚未创建；
      2. 本机裸跑 / 自定义 ELDER_DATA_DIR。
    这里提前 mkdir 兜底，避免首次启动因为少一个目录就崩掉。
    """
    if not _is_sqlite:
        return

    # ⚠️ 必须用 SQLAlchemy 自己的 make_url()，不能用 urllib.parse.urlparse()。
    # 连接串 `sqlite:///C:/Users/.../x.db`（Windows 绝对路径）经 urlparse 后
    # path 是 `/C:/Users/.../x.db`，Path() 会把它解释成 `\C:\Users\...`，
    # 再 mkdir 直接抛 `WinError 123 文件名、目录名或卷标语法不正确`。
    # make_url().database 在各种形态下都给出正确的文件位置：
    #   sqlite:///./x.db          -> ./x.db
    #   sqlite:////data/elder.db  -> /data/elder.db
    #   sqlite:///C:/a/b.db       -> C:/a/b.db
    try:
        database = make_url(DATABASE_URL).database
    except Exception:  # noqa: BLE001  连接串异常不应阻断启动，交给 create_engine 报错
        return
    if not database:
        return

    parent = Path(database).parent
    if str(parent) in ("", "."):
        return
    parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_parent_dir()

engine = create_engine(
    DATABASE_URL,
    echo=DB_ECHO,
    # pool_pre_ping：每次取连接前先探活，避免拿到被服务端踢掉的半开连接。
    # 对 MySQL 这类网络数据库是必需的，对 SQLite（进程内文件）没有意义但也无害。
    pool_pre_ping=DB_POOL_PRE_PING,
    # pool_size / max_overflow / pool_recycle 是给 MySQL 这种网络数据库调连接数的
    # （每次建连都要 TCP + 鉴权，连接必须复用、且要定期回收避免被服务端踢掉）。
    # SQLite 是进程内的本地文件：文件库的连接池语义完全不同，内存库更是走
    # SingletonThreadPool、根本不接受这几个参数，老版本 SQLAlchemy 上会直接抛
    # `TypeError: Invalid argument(s) ... sent to create_engine()`。
    # 所以按分支区分，不无脑全传。
    **({} if _is_sqlite else {"pool_size": 10, "max_overflow": 20, "pool_recycle": 1800}),
    # FastAPI 的请求可能落在任意线程上，而 sqlite3 默认只允许创建连接的那个线程使用，
    # 不关掉这个检查就会报 `sqlite3.ProgrammingError: SQLite objects created in a
    # thread can only be used in that same thread`。
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

    SQLite 分支直接返回：它是进程内的本地文件，没有「服务还没起来」这回事，
    目录缺失的问题已在模块导入时由 _ensure_sqlite_parent_dir() 兜底。
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
