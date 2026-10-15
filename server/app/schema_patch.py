"""老库补列：为「模型加了字段、但库是旧的」补齐缺失的列。

背景
----
本项目只用 ``Base.metadata.create_all`` 建表（见 app/db.py 的 init_db），
没有引入 Alembic，而 create_all **不会**给已存在的表补列。也就是说：
新版本往模型里加一个字段后，全新部署的库一切正常，但升级上来的老库会在
第一次 SELECT 时直接抛 ``no such column: xxx``。

这里不做完整迁移框架，只做一件最小的事：启动时按「表 -> 列 -> DDL 片段」
清单做一次幂等的缺列检查，缺了就 ALTER TABLE ADD COLUMN 补上。
新增模型字段时，记得同步往 NEW_COLUMNS 里登记一行（含 SQLite 与 MySQL
两种写法，避免只照顾到一种库）。

局限（有意为之，保持侵入最小）：
  * 只补列，不改列类型、不删列、不建索引；
  * 补出来的列如果有 NOT NULL 约束需要默认值，由应用层的 Python default 兜底；
  * 若某天字段多到难以维护，再考虑引入 Alembic。
"""
from __future__ import annotations

import logging
from typing import Final

from sqlalchemy import inspect, text

from .db import engine

log = logging.getLogger("elder.schema_patch")

# (表名, 列名, DDL 类型片段)。类型片段按数据库的实际方言取用。
NEW_COLUMNS: Final[list[tuple[str, str, str, str]]] = [
    # 表名, 列名, SQLite 类型, MySQL 类型
    ("users", "email", "VARCHAR(128) DEFAULT ''", "VARCHAR(128) DEFAULT ''"),
    ("devices", "agent_id", "INTEGER", "INT NULL"),
    # ai_providers 的 agent 角色字段（APP 的 AgentRole），见 models.AIProvider
    ("ai_providers", "description", "VARCHAR(255) DEFAULT ''", "VARCHAR(255) DEFAULT ''"),
    ("ai_providers", "system_prompt", "TEXT DEFAULT ''", "TEXT"),
    ("ai_providers", "tts_voice", "VARCHAR(64) DEFAULT ''", "VARCHAR(64) DEFAULT ''"),
    ("ai_providers", "is_default", "BOOLEAN DEFAULT 0", "TINYINT(1) DEFAULT 0"),
]


def _dialect() -> str:
    return engine.dialect.name


def ensure_missing_columns() -> int:
    """给已存在的表补上缺失的列，返回补的列数（幂等，可重复执行）。"""
    dialect = _dialect()
    ddl_index = 2 if dialect == "sqlite" else 3
    added = 0

    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())

    with engine.begin() as conn:
        for table, column, sqlite_ddl, mysql_ddl in NEW_COLUMNS:
            if table not in existing_tables:
                # 表都还不存在，说明等下/刚才 create_all 会按新模型建好，无需补列
                continue
            cols = {c["name"] for c in insp.get_columns(table)}
            if column in cols:
                continue
            ddl = sqlite_ddl if ddl_index == 2 else mysql_ddl
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            except Exception as exc:  # noqa: BLE001  补列失败不应该拖垮启动
                log.warning("补列失败 %s.%s: %s", table, column, exc)
                continue
            added += 1
            log.info("老库补列: %s.%s %s", table, column, ddl)

    return added
