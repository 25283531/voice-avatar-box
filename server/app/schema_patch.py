"""老库升级：补列 + 回填 + 补索引。

背景
----
本项目只用 ``Base.metadata.create_all`` 建表（见 app/db.py 的 init_db），
没有引入 Alembic，而 create_all **不会**给已存在的表补列。也就是说：
新版本往模型里加一个字段后，全新部署的库一切正常，但升级上来的老库会在
第一次 SELECT 时直接抛 ``no such column: xxx``。

这里不做完整迁移框架，只做一件最小的事：启动时按「表 -> 列 -> DDL 片段」
清单做一次幂等的缺列检查，缺了就ALTER TABLE ADD COLUMN 补上。
新增模型字段时，记得同步往 NEW_COLUMNS 里登记一行（含 SQLite 与 MySQL
两种写法，避免只照顾到一种库）。

⚠️ **每一行都必须带 server_default**，这不是风格问题而是功能问题：
``ALTER TABLE ADD COLUMN x VARCHAR(16) DEFAULT 'config'`` 会把默认值写给
**已有行**，而不带 DEFAULT 时老行读出的是 NULL —— 于是第一次 SELECT 就能
把 Pydantic 序列化炸掉（字段类型错误）。这个坑之前踩过一次。

本轮新增（角色模型，docs/架构设计-角色模型与人格能力.md §4）
--------------------------------------------------------------
1. ``ai_providers`` 8 列：record_type / owner_id / parent_id / avatar_id /
   traits / guardrails / deleted_at / archived_at，另加承诺卡回执 3 列；
2. ``avatars`` 3 列：record_type / owner_id / deleted_at；
3. ``agent_persona_profiles`` 1 列：agent_key（哨兵列，修 L1 全局画像的
   唯一性漏洞，见 models.AgentPersonaProfile 的注释）。

⚠️ **顺带修掉的一处既有欠账**：api_style / supports_stream / timeout_sec /
max_retries / context_window 这五列在 models 里**早就带 server_default**，
但一直没有登记进本清单。后果是：比它们更老的老库升级上来后，第一次
SELECT ai_providers 就会抛 ``no such column: api_style`` —— 服务直接起不来。
本轮验证老库升级路径时实测复现了这个崩溃，现已补登记。
这类"模型加了列但忘了登记"的坑正是本模块存在的理由，注释里那句
「必须同步把同一份默认值登记进 NEW_COLUMNS」说的就是它。

除了补列，本模块还做三件 create_all 干不了的事：
  * ``backfill_record_types()``  —— 老库 ``record_type`` 回填；
  * ``dedupe_persona_globals()`` —— 建agent_key 唯一索引**之前**必须先清重复，
    否则 CREATE UNIQUE INDEX 直接失败（老库里可能有重复的 (user, NULL) 行）；
  * ``ensure_missing_indexes()`` —— create_all 只给**新**库建索引，老库不会。

⚠️ **后两者里有两个函数会改业务数据，它们被登记进 ``patch_meta``**
--------------------------------------------------------------------
``backfill_record_types`` 与 ``dedupe_persona_globals`` 会 **UPDATE 业务列 /
DELETE 行**，所以它们**只在首次执行**，靠 :data:`PATCH_REGISTRY` +
``models.PatchMeta`` 表标记。补列与补索引**不登记**（幂等且无副作用）。

这条铁律的来源是一个**已经真实发生过**的 P0：``backfill_record_types``
曾把「``kind`` 为空 + 有 ``owner_id``」的用户派生角色刷成 ``preset``，
而 ``preset`` 对全平台可见 —— 于是一次发版就造成**跨用户信息泄漏 +
用户角色永久只读**（PATCH 刻意不改归属，没有任何接口能改回来）。

→ 铁律：**启动时改业务数据，必须只执行一次。**
自问「第二次、第一百次启动时还会改数据吗？」答案必须是「不会」。
两层防线缺一不可：**判据正确**（owner_id 参与判断）+ **只执行一次**（标记位）。
详见模块顶部的「为什么要登记」。

选判据的两条规律（写回填前必读）
--------------------------------
上面那条铁律只解决了「会不会重复执行」，**没解决「判据对不对」**。
而 P0 恰恰是判据先错、幂等后失效。以下两条规律解释了**为什么同一种写法
在四张表上一成一败**—— 记住规律比记住「记得加判据」有用得多。

**规律一：判据必须选「写入端无条件保证」的字段。**

对照两个真实修法就能看出差别：

* ``avatars`` 用 ``owner_id IS NULL`` 判定「这是平台预设」。
  ``owner_id`` 是**写入端必然要填**的字段（建预设的人一定有 owner 语义），
  所以这个判据**永远**能把「平台建的」和「用户建的」分开。
* 角色行曾用 ``kind != 'config'`` 判定「这是角色」。
  但**角色行的 ``kind`` 必然是空串**，于是「不是配置」这个条件
  对 preset 和用户角色**同样成立** —— 它压根区分不了两者。

→ 自问：**这个字段的取值，是否由写入端无条件保证，且两侧取值必然不同？**
若某一类实体的该字段恒为空/恒为默认，那么判据在这一类上**恒真**，
等于没判。「恒真」不会报错，它只会静默地把所有行判成同一类。

**规律二：判据必须与被判定对象在同一语义空间内可比较。**

``kind != 'config'`` 与 ``record_type != 'config'`` 看起来是同一件事，
都在回答「这是不是配置」，所以第一次回填就判对了、第二次不再改、
**幂等成立但错值被永久固化**。问题在于：一旦引入**设备**这类新实体，
「不是配置」不再意味着「是角色」，旧判据就失去了区分力。
它**不会抛错**，只会静默地把角色判成设备（或反过来）。

→ 自问：**我写的判据表达的是「A 还是 B」，还是「既不是 A 也不是 B」？**
  - 「既不是 A 也不是 B」= 默认拒绝优先 → **安全**，新类别出现时落到正确的else；
  - 「A 还是 B」= 二选一穷举 → **脆弱**，新增第三类时会把该类错判进某一侧。

⚠️ 与幂等断言的关系（别把两者混为一谈）
----------------------------------------
QA 实测结论：``run_all()`` 第二次全 0 这条**幂等断言对「错误的幂等」是盲的**。
把 owner 守卫去掉（复现 P0）后，第二次 run_all 仍然全 0、断言照样绿 ——
因为 V1 跑完之后值已经是对的，所以第二次确实不再改。
**幂等断言回答的是「第二次还改不改」，P0 的问题是「改得对不对」。**

真正抓住那个 P0 的是**重启不变式**（★ owner 仍能 PATCH ／ ★ 他人仍 404）——
它们**直接对着业务语义判**，而不是对着「有没有再改一次」判。

→ 所以：补幂等断言可以，但它只能定位成**辅助**防线，
**守不住「判据选错了」这一类 bug**。写断言时必须把边界写进注释，
否则下一个维护者会以为「有了幂等断言就能放心改回填逻辑」——
**不写清边界的断言是负债**：它不报错，只是给人虚假的安全感。

关于长期记忆模块的六张新表
-------------------------
``agent_memories`` / ``agent_memory_entities`` / ``chat_messages`` /
``agent_preference_examples`` / ``agent_persona_profiles`` /
``agent_persona_audits`` 是随记忆模块**一次性新增**的，
``create_all`` 会按当前模型直接建全，不存在「老库缺列」的历史包袱，
所以它们**刻意没有**登记进 NEW_COLUMNS。

将来若往这几张表**追加**字段，必须补一行登记 —— 否则全新部署正常、
升级上来的库会在第一次 SELECT 时抛 ``no such column``（正是本文要防的事故）。
本轮只给 agent_persona_profiles 补了 agent_key 一列，理由见上。

删表（会员制取消后的收尾）
------------------------
产品决策**取消会员制**后，``memberships`` / ``membership_plans`` /
``membership_orders`` / ``membership_amount_range`` / ``payment_config``
这五张表的 **ORM 映射已经删除**（见 ``models.py`` 对应位置的说明），但**表本身
还留在盘上**。它们从此变成孤儿表：``create_all`` 不认它们，代码也不再读写，
留着只会让人误以为还有会员功能。

所以本模块除了「补列」还多做一件事：启动时把这五张表**真正 DROP 掉**，
登记在 :data:`DROPPED_TABLES`，由 :func:`drop_membership_tables` 执行。
细节（两种方言、幂等、外键核查）写在那个函数里。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import inspect, text

from .db import engine

log = logging.getLogger("elder.schema_patch")

# ================================================================ 迁移登记（只登记改业务数据的）
# 为什么需要这一层：见下方「为什么要登记」的长注释与 models.PatchMeta。
#
# ⚠️ **只登记「改业务数据」的迁移**。补列（:func:`ensure_missing_columns`）与
# 补索引（:func:`ensure_missing_indexes`）**刻意不登记** —— 它们幂等且无副作用，
# 重复执行不改变任何已有行的业务含义，登记纯属负担。
# 一旦"什么都登记"，标记位就会退化成"启动时跳过一切"的开关，那才是真危险。
PATCH_REGISTRY: Final[dict[str, str]] = {
    # patch_id -> 一句话说明它改了什么业务数据（会出现在启动日志里）
    "record_type_owner_guard_v1":
        "回填 ai_providers.record_type 时要求 owner_id IS NULL"
        "（修P0：kind 为空的用户角色曾被每次启动刷成 preset）",
    "persona_agent_key_v1":
        "回填 agent_persona_profiles.agent_key 并合并重复的 L1 全局画像行",
}

# 登记表名。与 models.PatchMeta.__tablename__ 一致；不 import models 是为了
# 避免 schema_patch -> models -> db -> config 的导入链把启动顺序搞复杂。
_PATCH_META_TABLE: Final = "patch_meta"


def _patch_meta_ready(insp: Any) -> bool:
    """登记表是否可用（存在且有 patch_id 列）。

    ⚠️ **为什么要"表不存在就当没登记过"而不是"当已登记"**
    ----------------------------------------------------
    两个方向都会出错，必须选对：

    * 表不存在 -> 若当成「已登记」-> 回填**永远不执行** → 老库的
      ``record_type`` 永远是错的（老行全是默认 'config'），而服务照常启动、
      **没有任何报错**。这是静默的功能损坏。
    * 表不存在 -> 当成「没登记」-> 回填执行一次 -> 正确。

    所以降级方向是**「表在就查，表不在就当没登记过，照常执行」**：
    宁可多跑一次（幂等的），不可少跑一次（数据错）。

    调用顺序上也已保证：``main.lifespan`` 先 ``init_db()``（内含
    ``create_all``，会把 ``patch_meta`` 建出来）再 ``run_schema_upgrade()``。
    这里仍然做检查，是为了让 schema_patch 能被**单独调用**（测试脚本就是
    这么用的），不依赖调用方记得先 create_all。
    """
    if _PATCH_META_TABLE not in set(insp.get_table_names()):
        return False
    try:
        return "patch_id" in {c["name"] for c in insp.get_columns(_PATCH_META_TABLE)}
    except Exception:  # noqa: BLE001  取不到就当没有，靠上面的降级方向兜住
        return False


def _is_applied(conn: Any, patch_id: str) -> bool:
    """这个改业务数据的迁移是否已经执行过。"""
    try:
        row = conn.execute(text(
            f"SELECT patch_id FROM {_PATCH_META_TABLE} WHERE patch_id = :pid"),
            {"pid": patch_id}).first()
    except Exception as exc:  # noqa: BLE001  表缺失/方言问题 -> 当没登记过
        log.warning("读取迁移登记失败（按未登记处理）：%s", exc)
        return False
    return row is not None


def _mark_applied(conn: Any, patch_id: str) -> None:
    """登记「已执行」。失败只警告 —— 宁可下次重跑（幂等），不可静默丢标记。

    ⚠️ MySQL 方言：用 ``INSERT ... ON DUPLICATE KEY UPDATE`` 而不是
    ``INSERT OR IGNORE``（那是 SQLite 专有语法，在 MySQL 上直接语法错误）。
    这条SQL 必须**两种库都合法** —— CI 的 ``mysql-smoke`` job 会真跑它。
    """
    sql = (
        f"INSERT INTO {_PATCH_META_TABLE} (patch_id, applied_at) "
        f"VALUES (:pid, :now) ON DUPLICATE KEY UPDATE applied_at = :now"
    )
    if _is_sqlite():
        sql = (
            f"INSERT OR REPLACE INTO {_PATCH_META_TABLE} (patch_id, applied_at) "
            f"VALUES (:pid, :now)"
        )
    conn.execute(text(sql), {"pid": patch_id, "now": datetime.now(timezone.utc)})


# ================================================================ 「为什么要登记」总说明
# 把它放在模块顶部而不是散落在各函数里，因为它约束的是**将来所有人**：
#
# **启动时改业务数据是这个项目里最危险的操作。** 本轮已经因此出过**一次 P0** ——
# ``backfill_record_types`` 曾把「kind 为空 + 有 owner」的**用户角色**刷成 preset，
# 后果是双重的：用户自己的角色永久只读（can_write 对 preset 只给 admin），
# 且因为 preset **对全平台可见**，A 家人的角色设定泄露给 B 家人。
#
# 那个 bug 的本质不是「判据写错了」（虽然确实写错了），而是
# **「每次启动都执行一次不可逆的业务数据改写」**。所以铁律是：
#
#   **第二次、第一百次启动时，这条改写都不会再改任何数据。**
#
# 两层防线，缺一不可（它们是两个**独立**的性质，不能互相替代）：
#   1. **判据正确** —— ``owner_id`` 参与判断，保证「第一次就改对」；
#   2. **只执行一次** —— ``patch_meta`` 标记位，保证「第一次之后不再改」。
#
# 只修第1 层：将来有人把 WHERE 改错一处（review 里极难发现，因为改完看起来
# 完全合理），就会**重演同一个 P0**。
# 只加第 2 层：第一次执行时若判据本来就错，那个错误会被**永久固化**。
#
# → **判断标准（回答「这个迁移要不要登记」）**：
#   会改变已有行的业务含义（UPDATE 业务列 / DELETE 行）→ **登记**；
#   只增列或增索引、不碰任何已有行的业务含义 → 不登记。

# (表名, 列名, SQLite 类型, MySQL 类型)
#
# ⚠️ SQLite 与 MySQL 的类型差异要点：
#   * JSON 列：SQLAlchemy 的 JSON 类型在两边都映射为**原生 JSON 文本列**，
#     但 DDL 关键字不同 —— SQLite 只能写 TEXT，MySQL 写 JSON。
#     MySQL 的 JSON 列**不允许带 DEFAULT**（8.0.13 之前连表达式默认值都不行），
#     所以 MySQL 侧一律 "JSON"（隐式可空），读取端统一 `or {}` 归一（见架构 §13.5）。
#   * 时间列：SQLite 的 DATETIME 默认可空，MySQL 必须显式写 NULL 才可空。
NEW_COLUMNS: Final[list[tuple[str, str, str, str]]] = [
    # 表名, 列名, SQLite 类型, MySQL 类型
    ("users", "email", "VARCHAR(128) DEFAULT ''", "VARCHAR(128) DEFAULT ''"),
    ("devices", "agent_id", "INTEGER", "INT NULL"),
    # ai_providers 的 agent 角色字段（APP 的 AgentRole），见 models.AIProvider
    ("ai_providers", "description", "VARCHAR(255) DEFAULT ''", "VARCHAR(255) DEFAULT ''"),
    ("ai_providers", "system_prompt", "TEXT DEFAULT ''", "TEXT"),
    ("ai_providers", "tts_voice", "VARCHAR(64) DEFAULT ''", "VARCHAR(64) DEFAULT ''"),
    ("ai_providers", "is_default", "BOOLEAN DEFAULT 0", "TINYINT(1) DEFAULT 0"),
    # ---- 调用层元信息（**这五条是补登记的既有欠账**，见下方注释）----
    ("ai_providers", "api_style", "VARCHAR(32) DEFAULT ''", "VARCHAR(32) DEFAULT ''"),
    ("ai_providers", "supports_stream", "BOOLEAN DEFAULT 0", "TINYINT(1) DEFAULT 0"),
    ("ai_providers", "timeout_sec", "INTEGER DEFAULT 30", "INT NOT NULL DEFAULT 30"),
    ("ai_providers", "max_retries", "INTEGER DEFAULT 1", "INT NOT NULL DEFAULT 1"),
    ("ai_providers", "context_window", "INTEGER DEFAULT 0", "INT NOT NULL DEFAULT 0"),
    # ---- 角色模型新增（架构 §4.1）：归属与生命周期 ----
    # record_type 默认 'config'：老库里绝大多数行确实是平台配置，
    # 先给它们这个默认值，再由 backfill_record_types() 按 kind 精确修正。
    ("ai_providers", "record_type", "VARCHAR(16) DEFAULT 'config'",
     "VARCHAR(16) DEFAULT 'config'"),
    ("ai_providers", "owner_id", "INTEGER", "INT NULL"),
    ("ai_providers", "parent_id", "INTEGER", "INT NULL"),
    ("ai_providers", "avatar_id", "INTEGER", "INT NULL"),
    # JSON 列：SQLite 用 TEXT（带 '{}' 默认值，让老行读出{} 而不是 None），
    # MySQL 的 JSON 列不能带 DEFAULT，只能留空由读取端 `or {}` 兜。
    ("ai_providers", "traits", "TEXT DEFAULT '{}'", "JSON"),
    ("ai_providers", "guardrails", "TEXT DEFAULT '{}'", "JSON"),
    ("ai_providers", "deleted_at", "DATETIME", "DATETIME NULL"),
    ("ai_providers", "archived_at", "DATETIME", "DATETIME NULL"),
    # 承诺卡确认回执（架构 §12 U5）
    ("ai_providers", "guardrail_ack_at", "DATETIME", "DATETIME NULL"),
    ("ai_providers", "guardrail_ack_by", "INTEGER", "INT NULL"),
    ("ai_providers", "guardrail_ack_version", "VARCHAR(32) DEFAULT ''",
     "VARCHAR(32) DEFAULT ''"),
    # ---- avatars：形象归属与软删（架构 §4.2）----
    # 默认 'preset'：老库里已有的形象全是平台 seed 出来的，
    # 归为 preset 时普通用户仍可读（家属需要能看到小暖长什么样），
    # 只是改不动 —— 这是安全侧的选择。
    ("avatars", "record_type", "VARCHAR(16) DEFAULT 'preset'", "VARCHAR(16) DEFAULT 'preset'"),
    ("avatars", "owner_id", "INTEGER", "INT NULL"),
    ("avatars", "deleted_at", "DATETIME", "DATETIME NULL"),
    # ---- agent_persona_profiles：哨兵列，修 L1 全局画像唯一性（架构 §1.5）----
    ("agent_persona_profiles", "agent_key", "INTEGER DEFAULT 0", "INT NOT NULL DEFAULT 0"),
]

# 本轮新增的索引。create_all 只为**新**库建索引，老库升级上来不会有，
# 所以要在这里幂等补上（CREATE INDEX IF NOT EXISTS，SQLite 与 MySQL 8 都支持）。
# MySQL 不支持 CREATE INDEX IF NOT EXISTS，所以走"先查后建"。
NEW_INDEXES: Final[list[tuple[str, str, tuple[str, ...]]]] = [
    # 表名, 索引名, 列
    ("ai_providers", "ix_prov_record_type", ("record_type",)),
    ("ai_providers", "ix_prov_owner", ("owner_id",)),
    ("ai_providers", "ix_prov_parent", ("parent_id",)),
    ("avatars", "ix_avatars_record_type", ("record_type",)),
    ("avatars", "ix_avatars_owner", ("owner_id",)),
]

# agent_key 的唯一索引单独建：它必须在 dedupe_persona_globals() 之后建，
# 且失败要能被单独容忍（老库若有顽固重复行，不该拖垮整个启动）。
PERSONA_AGENTKEY_INDEX: Final = ("agent_persona_profiles", "uq_persona_user_agentkey",
                               ("user_id", "agent_key"))

# ================================================================ 要删掉的表（会员制收尾）
# 产品决策：取消会员制（见 commit「feat(产品决策)!: 取消会员制」）。ORM 映射已删，
# 这里把**表本身**也删掉 —— 用户已确认「还在开发阶段，没有投入使用，数据可以动」。
#
# ⚠️ **外键已核查（删表前必须做这一步）**
# ---------------------------------------
# 这五张表只有**出向**外键，没有**入向**外键：
#   * ``memberships.user_id``            -> users.id
#   * ``membership_orders.user_id``      -> users.id
#   * ``membership_orders.admin_id``     -> users.id
#   * ``membership_plans`` / ``membership_amount_range`` / ``payment_config``：无外键
# 换句话说：**没有任何表引用这五张表的 id**。所以删的是「子表」，
# 不会破坏其它表的外键约束，删除顺序也无所谓（五张表彼此之间没有引用）。
# 核查方式：``git show <删模型前>:server/app/models.py | grep ForeignKey``，
# 全部 ForeignKey 目标只有 ai_providers / avatars / devices / reminders / users /
# agent_memories，不含任何 membership / payment 表。
#
# ⚠️ 若将来有人往这里加表，**必须重做一遍上面的入向外键核查** ——
# 一旦有别的表引用了它，MySQL 上 ``DROP TABLE`` 会因为外键约束直接失败
# （SQLite 默认不强制外键，反而「删得掉」，于是两种库行为不一致，
# 这种不一致在 CI 上表现为「SQLite 绿、MySQL 黄」）。
DROPPED_TABLES: Final[tuple[str, ...]] = (
    "memberships",
    "membership_plans",
    "membership_orders",
    "membership_amount_range",
    "payment_config",
)


def _dialect() -> str:
    return engine.dialect.name


def _is_sqlite() -> bool:
    return _dialect() == "sqlite"


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


def backfill_record_types() -> int:
    """按 kind 回填 ``ai_providers.record_type`` / ``avatars.record_type``。

    回填规则（架构 §4.1）
    --------------------
        UPDATE ai_providers SET record_type = 'config' WHERE kind != '';
        -- kind == '' 的行**无法判断真实归属**（老库根本没有 owner 字段），
        -- 一律置为 'preset'；**但必须 owner_id IS NULL**。
        UPDATE ai_providers SET record_type = 'preset'
          WHERE kind = '' AND owner_id IS NULL;

    ⚠️ **为什么必须带 owner_id IS NULL**（QA 第 2 轮实测出的 P0 回归）：
    角色行的 ``kind`` 恒为空串（create_role / derive_role 都写死 kind=""，
    is_role() 也要求 kind==""），所以 ``kind == ''`` **区分不了「平台预设」和
    「用户角色」**。只按 kind 回填会让每次启动都把用户的 role 刷成 preset，
    造成「重启后自己的角色变只读 + 对全平台可见」，而且不可逆。

    ⚠️ **为什么老角色行归 preset 而不是猜一个 owner**：
    表里没存 owner，无法证明某行属于谁。把不确定的行判给「平台」是**安全侧
    选择** —— 后果是历史用户对自己的老角色**变成只读**（仍可在 APP 里看到、
    绑定，但改不了）；而"猜一个 owner 判给某个用户"可能把 A 用户的行判给
    B 用户，那是**越权**。补救路径见架构 §4.1.1：管理员按需手工转归属。
    这是一次**显式的人工动作**，不做自动归属算法 —— 任何自动算法都在猜，
    而猜错的后果是越权。

幂等：WHERE 条件与当前值无关（带owner_id 判据后），重复执行结果相同。

选判据的规律（QA 第 2 轮总结，**比记某个具体修复更有长期价值**）
--------------------------------------------------------------
回填的判据必须同时满足**两条**，缺一条就会误伤用户数据：

1. **由写入端无条件保证会填的字段**。
   ✅ ``owner_id`` —— 用户角色**必然**有它（``create_role`` / ``derive_role``
   都写死 ``owner_id=user.id``），平台预设**必然**没有。所以 ``owner_id IS NULL``
   精确等价于「这条行没有归属信息」，两个目标同时成立。
   ❌ ``kind`` —— 恰恰相反：角色行的 ``kind`` **必然**是空串（``create_role`` /
   ``derive_role`` 都写死 ``kind=""``，``is_role()`` 也要求它）。所以
   ``kind = ''`` **区分不了「平台预设」和「用户角色」**，用它当判据必然误伤。

2. **与被判定对象在同一语义空间内可比较**（QA 采纳后补充）。
   ``devices.id`` 与 ``ai_providers.id`` 是**两张表各自独立的自增序列**，
   拿 ``device.id <= preset.id`` 判「谁更早」**毫无意义** —— 实测全新库里
   两者都是 1，判据恒成立，设备照样被凭空绑上角色（``main._seed_migration``
   步骤 4 曾踩这个坑，现已改用 ``created_at``）。
   换句话说：两条判据各自的"自然直觉"都不可信，**必须确认它们指向同一语义**。

本轮踩的坑正是违反第 1 条：写入端（``roles.py`` 写 ``kind=""``）与判定端
（回填只看 ``kind``）**各改各的、没对齐**，于是每次启动都把用户的 role 刷成
preset。``avatars`` 之所以没中招，只是因为它的判定端用了 ``owner_id IS NULL``。

所以：**改动任何一处写入逻辑时，必须回头检查依赖该字段的回填/判定逻辑**，
两者的语义必须由同一个约定支撑。

为什么（曾经）暂不做「回填只执行一次」—— 以及为什么那个判断是错的
------------------------------------------------------------------
**本节已于 2026-10-16 推翻并改为实现**（主理人拍板）。原文保留，
因为它记录了「当时为什么觉得不需要」，以及**那个判断错在哪** ——
这比一开始就写对更有长期价值。

    ~~理想做法是回填只作用于「升级前就存在的行」（一次性动作）。但本项目**没有
    Alembic**，要表达「这次升级跑过没有」就得引入一张需要长期维护的状态表
    （还要考虑它在老库里怎么建、怎么补列）。而本文件当前的行为依赖「判据正确」
    而非「只跑一次」—— 上一条的规律正是为了让这个前提持续成立。~~

**错在两点**：

1. **把成本与风险当成了同一个量级**，不成比例。后者（每次启动都改业务数据）
   **已经真实发生过一次 P0**：用户的派生角色被刷成 preset → 跨用户可见 +
   永久只读，且没有任何接口能改回来。而前者只是一张 2 列的小表。
2. **混淆了两件独立的事**：「判据正确」保证*第一次*改对；「只执行一次」保证
   *第一次之后*不再改。判据正确**不能**替代只执行一次 —— 将来WHERE 被改错
   一处（review 里极难发现，因为改完看起来完全合理），P0 立刻重演。

→ 现在两层都要，见模块顶部的「为什么要登记」与 :data:`PATCH_REGISTRY`。

顺带对「幂等断言」祛魅（QA 实测，``tools/qa3_idempotency.py``）：
**幂等断言对「错误的幂等」是盲的。** 上一轮那个 P0 正是「每次都改、每次都改错
同样的东西」—— 形式上完全幂等，错得离谱。真正抓住它的是「重启不变式」
（直接对业务语义判：``record_type`` 仍是 'role'、owner 仍可写、他人仍 404），
不是幂等断言。所以门禁以重启不变式为主，``patch_meta`` 是**第二道**防线。
"""
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    meta_ready = _patch_meta_ready(insp)
    changed = 0

    with engine.begin() as conn:
        if meta_ready and _is_applied(conn, "record_type_owner_guard_v1"):
            # ⚠️ 已执行过 -> **立刻返回，一行数据都不碰**。
            # 这是「只执行一次」那层防线。它与上面的判据是**两个独立**的性质：
            # 判据保证第一次改对，这里保证第一次之后不再改。
            # 少了这一层，将来WHERE 条件被改错一处就会重演 P0 ——
            # 而那种改动在 review 里几乎看不出来（改完完全合理）。
            return 0
        if "ai_providers" in existing_tables:
            cols = {c["name"] for c in insp.get_columns("ai_providers")}
            if "record_type" in cols and "kind" in cols:
                has_owner = "owner_id" in cols
                # 先把「与 kind 明显矛盾」的行修正过来：config 必须 kind 非空。
                res = conn.execute(text(
                    "UPDATE ai_providers SET record_type = 'config' "
                    "WHERE kind IS NOT NULL AND kind != '' "
                    "AND (record_type IS NULL OR record_type != 'config')"))
                changed += int(res.rowcount or 0)
                # 再把「kind 为空」的行归为 preset（安全侧）。
                #
                # ⚠️⚠️ **必须同时要求 owner_id IS NULL**（QA 第2轮发现的 P0 回归）：
                #   角色行的 kind 恒为空串 —— create_role / derive_role 都写死 kind=""，
                #   is_role() 也要求 kind==""。所以 **`kind==''` 根本无法区分
                #   「平台预设」与「用户角色」**，光看 kind 会把用户的 role 刷成 preset。
                #
                #   后果是致命的：家属派生角色 → 服务重启（发版/容器重建）→ 角色变只读
                #   （owner 判成 preset，can_write 直接返回 False），同时因为 preset
                #   **对全平台可见**，A 家人的角色设定泄露给 B 家人。且**不可逆** ——
                #   PATCH 刻意不改归属，没有任何接口能把 record_type 改回 'role'。
                #
                #   owner_id IS NULL 才是「这条行没有归属信息」的准确判据，也正是
                #   **老库**里那些历史角色行的真实状态（老库根本没有 owner 字段）。
                #   于是条件同时满足两个目标：用户角色（有 owner_id）不被碰，
                #   老库无主行仍归 preset（安全侧）。
                #
                #   与 avatars 的回填口径一致（那边用 owner_id IS NULL 判定）。
                #   ⚠️ 老库在补列之前 owner_id 可能尚未存在，此时该判据缺失；
                #      这种情况只可能发生在「本次升级」这一次，补列先于回填执行，
                #      所以 has_owner 为 False 时退化为旧行为（可接受：老库行本就无主）。
                owner_guard = " AND owner_id IS NULL" if has_owner else ""
                res = conn.execute(text(
                    "UPDATE ai_providers SET record_type = 'preset' "
                    "WHERE (kind IS NULL OR kind = '')" + owner_guard + " "
                    "AND (record_type IS NULL OR record_type != 'preset')"))
                changed += int(res.rowcount or 0)

        if "avatars" in existing_tables:
            cols = {c["name"] for c in insp.get_columns("avatars")}
            if "record_type" in cols:
                # 已有 owner_id 的形象判为用户形象（理论上不会有，
                # 但若将来手工改过，这里给出正确的归类而不是一律 preset）。
                res = conn.execute(text(
                    "UPDATE avatars SET record_type = 'role' "
                    "WHERE owner_id IS NOT NULL AND (record_type IS NULL OR record_type != 'role')"))
                changed += int(res.rowcount or 0)
                res = conn.execute(text(
                    "UPDATE avatars SET record_type = 'preset' "
                    "WHERE owner_id IS NULL AND (record_type IS NULL OR record_type != 'preset')"))
                changed += int(res.rowcount or 0)

    if changed:
        log.info("回填 record_type：%d 行", changed)
    if meta_ready:
        # 登记放在最后：只有真正跑过才登记。若中途抛异常，
        # 事务回滚、标记也没写 —— 下次启动会重跑，这正是我们要的
        # （宁可重跑一次幂等的，也不可"以为跑过了"而漏掉）。
        with engine.begin() as conn:
            _mark_applied(conn, "record_type_owner_guard_v1")
    else:
        log.warning("迁移登记表不可用，record_type 回填将每次启动重复执行"
                    "（功能仍正确，但没有『只执行一次』那层保护）")
    return changed


def dedupe_persona_globals() -> int:
    """清理 ``agent_persona_profiles`` 里重复的 (user_id, agent_id IS NULL) 行。

    为什么必须先清再建唯一索引
    ------------------------
    ``UniqueConstraint("user_id", "agent_id")`` 不约束 ``agent_id IS NULL``
    的行（SQL 里 ``NULL != NULL``），所以老库里**可能**已经有同一用户的多个
    L1 全局画像行。直接 ``CREATE UNIQUE INDEX (user_id, agent_key)`` 会失败，
    而这个失败发生在启动路径上 —— 表现就是「升级后服务起不来」。

    合并规则（保留 id 最小的一行，其余并进去）
    ----------------------------------------
      * ``replies_count`` / ``positive_count`` / ``negative_count``：**相加**。
        这三个是纯计数，语义上可加。
      * ``avg_reply_len`` / ``avg_user_len``：**按样本数加权平均**。
        直接取某一行的值等于丢掉另一半数据；而这几个 avg 的分母就是
        positive_count / replies_count，正好可当权重。
      * ``humor_score``：同理由positive_count 加权。
      * ``address`` / ``speed``：取非空的那一个（都是单值标签，无法平均）。
      * ``address_counts`` / ``catchphrases``：**逐项相加**再取前N，
        这两个本来就是计数字典。
      * ``manual``：字典合并，已有键优先（人工覆盖优先级最高，不能被统计覆盖）。

    幂等：清完再执行是空操作。
    """
    insp = inspect(engine)
    if "agent_persona_profiles" not in set(insp.get_table_names()):
        return 0
    cols = {c["name"] for c in insp.get_columns("agent_persona_profiles")}
    if "agent_id" not in cols:
        return 0

    # ⚠️ 同backfill_record_types：这是**改业务数据**的迁移（DELETE 行 +合并
    # 计数），必须"只执行一次"。DELETE 不可逆 —— 每启动一次就重跑一次合并，
    # 虽然结果幂等，但它把"每次启动都在动业务数据"这件事变成常态，
    # 一旦判据被改坏就没有第二道防线。
    #
    # agent_key 的回填放在守卫**之外**（见下方），因为它必须每次都跑：
    # 新写入的 L2 画像行靠 models/persona 的写入端填 agent_key，
    # 但老库里可能还有没填的行，而唯一索引要求它非空且正确。
    meta_ready = _patch_meta_ready(insp)
    with engine.begin() as probe:
        if meta_ready and _is_applied(probe, "persona_agent_key_v1"):
            return 0

    removed = 0
    with engine.begin() as conn:
        # 找出有重复的 user_id（agent_id IS NULL 的那些行）
        dup_users = [int(r[0]) for r in conn.execute(text(
            "SELECT DISTINCT user_id FROM agent_persona_profiles "
            "WHERE agent_id IS NULL GROUP BY user_id HAVING COUNT(*) > 1")).all()]
        for uid in dup_users:
            rows = conn.execute(text(
                "SELECT id, replies_count, positive_count, negative_count, "
                "       avg_reply_len, avg_user_len, humor_score, address, speed, "
                "       manual, address_counts, catchphrases "
                "FROM agent_persona_profiles "
                "WHERE user_id = :uid AND agent_id IS NULL ORDER BY id ASC"),
                {"uid": uid}).mappings().all()
            if len(rows) < 2:
                continue
            keep = rows[0]
            others = rows[1:]

            def _num(row, key: str) -> float:
                v = row.get(key)
                return float(v) if v is not None else 0.0

            def _wavg(key: str, weight_key: str) -> float:
                """按 weight_key 加权平均；权重全为 0 时退化为算术平均。"""
                tw = sum(max(0.0, _num(o, weight_key)) for o in others) + \
                    max(0.0, _num(keep, weight_key))
                if tw <= 0:
                    vals = [_num(keep, key)] + [_num(o, key) for o in others]
                    return round(sum(vals) / len(vals), 3)
                acc = _num(keep, key) * max(0.0, _num(keep, weight_key))
                for o in others:
                    acc += _num(o, key) * max(0.0, _num(o, weight_key))
                return round(acc / tw, 3)

            def _merge_dicts(key: str) -> str:
                """把计数字典逐项相加，返回可直接写回 TEXT 列的 JSON 串。"""
                import json as _json

                total: dict[str, int] = {}
                for r in [keep, *others]:
                    raw = r.get(key)
                    if not raw:
                        continue
                    try:
                        parsed = _json.loads(raw) if isinstance(raw, str) else dict(raw)
                    except (ValueError, TypeError):
                        continue
                    for k, v in dict(parsed).items():
                        try:
                            total[k] = total.get(k, 0) + int(v)
                        except (TypeError, ValueError):
                            total[k] = total.get(k, 0) + 1
                if not total:
                    return "{}"
                # 只保留计数最高的 8 项 / 20 项，与写入侧的上限保持一致
                cap = 20 if key == "catchphrases" else 8
                top = dict(sorted(total.items(), key=lambda kv: -kv[1])[:cap])
                return _json.dumps(top, ensure_ascii=False)

            import json as _json

            merged_manual: dict = {}
            for r in [keep, *others]:
                raw = r.get("manual")
                if not raw:
                    continue
                try:
                    parsed = _json.loads(raw) if isinstance(raw, str) else dict(raw)
                except (ValueError, TypeError):
                    continue
                for k, v in dict(parsed).items():
                    # keep 是 id 最小的一行，优先级最高：已有键不覆盖
                    merged_manual.setdefault(k, v)

            conn.execute(text(
                "UPDATE agent_persona_profiles SET "
                "  replies_count = :rc, positive_count = :pc, negative_count = :nc, "
                "  avg_reply_len = :arl, avg_user_len = :aul, humor_score = :hs, "
                "  address = :addr, speed = :speed, "
                "  address_counts = :ac, catchphrases = :cp, manual = :manual "
                "WHERE id = :id"), {
                "rc": int(keep["replies_count"] or 0) + sum(int(o["replies_count"] or 0) for o in others),
                "pc": int(keep["positive_count"] or 0) + sum(int(o["positive_count"] or 0) for o in others),
                "nc": int(keep["negative_count"] or 0) + sum(int(o["negative_count"] or 0) for o in others),
                "arl": _wavg("avg_reply_len", "positive_count"),
                "aul": _wavg("avg_user_len", "replies_count"),
                "hs": _wavg("humor_score", "positive_count"),
                # 单值标签：取第一个非空的（keep 优先）
                "addr": (keep["address"] or next((o["address"] for o in others if o["address"]), "")) or "",
                "speed": (keep["speed"] or next((o["speed"] for o in others if o["speed"]), "")) or "normal",
                "ac": _merge_dicts("address_counts"),
                "cp": _merge_dicts("catchphrases"),
                "manual": _json.dumps(merged_manual, ensure_ascii=False),
                "id": int(keep["id"]),
            })
            # 删掉其余行：L1 全局画像的语义是「每个用户一行」，
            # 多出来的那些是历史 bug 的产物，不该继续留着干扰取值。
            for o in others:
                conn.execute(text("DELETE FROM agent_persona_profiles WHERE id = :id"),
                             {"id": int(o["id"])})
                removed += 1

        # agent_key 回填：0 = 全局，其余等于 agent_id。
        # 这一步必须在建唯一索引**之前**，否则老行的 agent_key 全是默认 0，
        # 同一个用户的多行 L2 画像会互相冲突。
        if "agent_key" in cols:
            conn.execute(text(
                "UPDATE agent_persona_profiles SET agent_key = 0 WHERE agent_id IS NULL"))
            conn.execute(text(
                "UPDATE agent_persona_profiles SET agent_key = agent_id WHERE agent_id IS NOT NULL"))

    if removed:
        log.info("合并重复的 L1 全局画像：%d 行", removed)
    if meta_ready:
        with engine.begin() as conn:
            _mark_applied(conn, "persona_agent_key_v1")
    return removed


def _quote_table(name: str) -> str:
    """按当前方言引用表名：SQLite 用双引号，MySQL 用反引号。"""
    return f'"{name}"' if _is_sqlite() else f"`{name}`"


def drop_membership_tables() -> int:
    """删掉会员制遗留的五张表，返回**已确认移除**的表数（幂等：第二次起返回 0）。

    为什么放在这里而不是「写个一次性脚本清一遍」
    ------------------------------------------
    本项目没有 Alembic，**启动时只有一个改 schema 的入口**：``main.lifespan``
    在 ``init_db()`` 之后调 :func:`run_all`。把删表登记进这个入口，才能保证
    **每一台升级上来的机器**都在启动时自动清一遍。写成一次性脚本则必然有人
    漏跑，而漏跑的后果是孤儿表永久留在生产库里 —— 没有任何报错提示。

    ⚠️ 为什么必须由 ``run_all`` 统一调度，而不是各自 import
    ----------------------------------------------------
    启动路径上改 schema 的东西散落各处 = 无法保证顺序、无法一眼看全。
    这里沿用 :data:`NEW_COLUMNS` 那套「清单 + 一个函数」的组织方式，
    不新造一条平行的迁移路径。

    两种方言怎么处理（SQLite / MySQL 的差异点在哪）
    ----------------------------------------------
    * **存在性判断**：**不用方言相关的 SQL**，统一走 SQLAlchemy 的
      ``inspector.get_table_names()``。表不在就直接跳过、**一条 DDL 都不发**。
      于是「全新部署」和「已清过的老库」都是零开销、零副作用。
    * **表名引用**：SQLite 用双引号、MySQL 用反引号 —— 靠 :func:`_quote_table`
      按方言拼。不硬写一种，是因为 MySQL 在 ``ANSI_QUOTES`` 模式下会把
      双引号当标识符引号、把反引号当语法错误，而 CI 的 ``mysql-smoke`` job
      会真跑这条 SQL，方言错一个字就是启动期崩溃。
    * **DDL**：两边都用 ``DROP TABLE IF EXISTS <表名>``。``IF EXISTS``
      两种库都支持（MySQL 一直支持；SQLite 3.3+ 支持）。

    幂等怎么保证（三层）
    --------------------
    1. ``get_table_names()`` 里没有 -> 跳过，不发 DDL，返回 0；
    2. 真要删时也带 ``IF EXISTS`` —— 因为「检查」与「执行」之间隔着一次
       连接获取，**多实例同时启动**时存在竞态：另一个实例可能刚把表删了。
       ``IF EXISTS`` 让竞态的结果变成「后跑的那次是 no-op」，
       而不是「后跑的启动失败」；
    3. 删完之后表就不在 ``get_table_names()`` 里了 -> 第二次启动连 DDL 都不发。

    ⚠️ 返回值口径：**已确认移除**（删完复查不在库里）的表数，不是 DDL 次数
    ---------------------------------------------------------------------
    竞态下 ``DROP TABLE IF EXISTS`` 是 no-op，按「发了几次 DDL」计数会把
    **别的实例删的表**记到本实例头上（日志与计数都不可信）。所以删完再查
    一次 ``get_table_names()``，只数**真正已消失**的那些；失败的（仍在库里）
    不计。详见函数末尾那段注释。

    于是 :func:`run_all` 第二次调用返回 ``membership_tables_dropped=0``，
    ``tools/qa3_migration.py`` 的「第二次 run_all 全为 0」断言照常成立。

    ⚠️ 失败处理：单表隔离，不拖垮启动
    --------------------------------
    MySQL 的 DDL 会**隐式提交**，没法像 SQLite 那样裹在事务里整体回滚。
    所以这里**不做**「要么全删要么全不删」的假设，而是每张表单独 try/except：
    某张删失败只打 warning 并继续，下次启动会再试一次（幂等）。
    这条与 :func:`ensure_missing_columns`「补列失败不拖垮启动」是同一条原则 ——
    **启动路径上任何一步的失败都不该让服务起不来**，否则一台机器的问题
    会变成整个服务不可用。

    ✅ 审计结论：**不登记进** :data:`PATCH_REGISTRY`（2026-10 补）。
    判据（见模块顶部）：只登记「改变已有行业务含义」的迁移。删表删的是
    **已经没有任何代码读写的孤儿表**，不存在「第二次改出不同结果」的问题 ——
    第一次删完，第二次连表都找不着。
    """
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    todo = [t for t in DROPPED_TABLES if t in existing]
    if not todo:
        return 0

    dropped = 0
    with engine.begin() as conn:
        for table in todo:
            try:
                conn.execute(text(f"DROP TABLE IF EXISTS {_quote_table(table)}"))
            except Exception as exc:  # noqa: BLE001  删表失败不该拖垮启动
                log.warning("删除会员遗留表失败 %s: %s（下次启动会重试）", table, exc)
                continue
            dropped += 1
            log.info("已删除会员遗留表: %s", table)

    # ⚠️ 返回值必须是「复查确认已不存在」的表数，不是「发了几次 DDL」
    # ---------------------------------------------------------------
    # 上面那句 ``DROP TABLE IF EXISTS`` 在**别的实例先删掉了这张表**时是
    # no-op（IF EXISTS 让它不报错），此时 ``dropped += 1`` 会把「别人删的」
    # 记成「本实例删的」—— 启动日志与 ``run_all`` 的计数就不可信了
    # （QA 实测：竞态下 reported=1，但本实例一条行都没删掉）。
    #
    # 所以执行完再查一次 ``get_table_names()``，以**真正已消失**的表数为返回值：
    #   * 本实例删掉的            -> 已消失，计数；
    #   * 被别的实例抢先删掉的    -> 也确实已消失，但语义是"确认移除"而非"我删的"；
    #   * 删失败的（外键/权限等）-> 仍在名单里，不计数。
    # 于是返回值恒等于「这次启动结束后确认不在库里的遗留表数」，
    # 幂等断言（第二次为 0）与故障重试都照旧成立。
    #
    # 代价只有一次 Inspector 往返，且**仅在五张表真的存在时才发生**；
    # 全新部署 / 已清过的库走的是上面的 ``if not todo: return 0``，零开销。
    # ⚠️⚠️ 必须用**新建的** Inspector 复查，不能用开头那个 ``insp``
    # ------------------------------------------------------------
    # SQLAlchemy 的 Inspector 会**缓存** ``get_table_names()`` 的结果
    # （反射开销大，默认按 Inspector 实例缓存）。复用开头那个 ``insp``
    # 读回来的是**删表之前的快照** ⇒ ``confirmed`` 恒为空 ⇒ 返回值恒为 0，
    # 而表其实已经删掉了。
    #
    # 这个坑之所以阴：它**不报错、不失败**，只是让返回值与日志永远显示 0 ——
    # 「删了但说没删」比「没删但说删了」更难发现，因为表确实没了，
    # 幂等断言（第二次全 0）也照样绿，一切看起来都对。
    # 实测复现：同一个 insp 在 DROP 之后仍认为表存在；新建的 insp 才看到真相。
    still_there = set(inspect(engine).get_table_names())
    confirmed = [t for t in DROPPED_TABLES if t not in still_there]
    if confirmed:
        log.info("已确认移除会员遗留表: %s", "、".join(confirmed))
    return len(confirmed)


def ensure_missing_indexes() -> int:
    """幂等补建本轮新增的索引，返回新建的索引数。

    为什么不放在 create_all 里：create_all 只对**不存在的表**执行 CREATE
    TABLE / CREATE INDEX，老表已存在时它**完全不看索引**。所以老库升级上来
    不会有这些索引，只能自己补。

    与补列同样的原则：**失败不拖垮启动**。索引缺失只影响查询性能，
    不影响正确性（唯一索引 uq_persona_user_agentkey 尤其特殊，它保护的是
    数据正确性，但即使建不上，退化行为也只是"和以前一样"）。

    ✅ **审计结论：不需要登记进 ``patch_meta``**（2026-10-16）。
    理由：它只``CREATE INDEX``，**不碰任何已有行的业务含义** —— 重复执行
    一百次与执行一次完全等价，正是"幂等且无副作用"那一类。
    判据（见模块顶部）：会改变已有行的业务含义才登记。

    ⚠️ 但注意它**依赖** ``dedupe_persona_globals`` 先跑（唯一索引要求数据
    已去重），所以顺序不能调换 —— 这个依赖靠 :func:`run_all` 的顺序保证，
    不靠标记位。
    """
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    created = 0

    with engine.begin() as conn:
        for table, index_name, columns in [*NEW_INDEXES, PERSONA_AGENTKEY_INDEX]:
            if table not in existing_tables:
                continue
            try:
                have = {i["name"] for i in insp.get_indexes(table)}
                # 唯一索引在部分驱动里也会出现在 unique_constraints 而非 indexes，
                # 两边都查一遍，避免重复创建。
                have |= {u["name"] for u in insp.get_unique_constraints(table)}
            except Exception:  # noqa: BLE001  取不到就当没有，靠 try 兜住重复创建
                have = set()
            if index_name in have:
                continue
            # 索引所依赖的列必须都在（列还没补上就别建索引，否则报 no such column）
            try:
                cols = {c["name"] for c in insp.get_columns(table)}
            except Exception:  # noqa: BLE001
                continue
            if not set(columns) <= cols:
                continue

            unique = "UNIQUE " if index_name == PERSONA_AGENTKEY_INDEX[1] else ""
            cols_ddl = ", ".join(columns)
            ddl = (f"CREATE {unique}INDEX IF NOT EXISTS {index_name} ON {table} ({cols_ddl})"
                   if _is_sqlite() else
                   f"CREATE {unique}INDEX {index_name} ON {table} ({cols_ddl})")
            try:
                conn.execute(text(ddl))
                created += 1
                log.info("老库补索引: %s (%s)", index_name, cols_ddl)
            except Exception as exc:  # noqa: BLE001  索引建不上不该拖垮启动
                log.warning("补索引失败 %s: %s", index_name, exc)
                continue

    return created


def run_all() -> dict[str, int]:
    """按正确顺序跑完整个升级流程，返回各步做了什么（供启动日志与测试断言）。

    顺序**不能调换**，每一步都依赖前一步的结果：
      1. 补列 —— 没有 record_type 就没法回填，没有 agent_key 就没法去重；
      2. 回填 record_type —— 必须在建索引前完成，否则索引建在错误的分类上；
      3. 去重 + 回填 agent_key —— 必须在建唯一索引前完成，否则索引直接失败；
      4. 建索引 —— 最后一步，依赖前三步的列与数据。

    删表（:func:`drop_membership_tables`）**与上面四步互不依赖**，放在最前面：
    它动的是五张已经没有任何 ORM 映射的孤儿表，先清掉再补列，
    读 ``inspector.get_table_names()`` 的结果也更干净
    （否则老库里那五张表会混进「现存表」名单，虽然不影响正确性，
    但排查时多五个噪音）。
    """
    dropped = drop_membership_tables()
    added = ensure_missing_columns()
    backfilled = backfill_record_types()
    deduped = dedupe_persona_globals()
    indexed = ensure_missing_indexes()
    return {
        "membership_tables_dropped": dropped,
        "columns_added": added,
        "record_types_backfilled": backfilled,
        "persona_globals_deduped": deduped,
        "indexes_created": indexed,
    }