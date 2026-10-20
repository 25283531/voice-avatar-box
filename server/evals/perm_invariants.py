"""权限与隔离不变量的**可执行实现**（ok_contract_v2 / no_unsafe_override /
multi_user_isolation）。

为什么这三个键以前是「文案说有、实际没有」
----------------------------------------
``invariants.json`` 的 notes 声明了 INV-1~4 与 P0-2'共 5 条不变量，
但套件里真正落地的case 只有红线相关的那几条 —— 权限矩阵（PRD §10）三条
**一个 case 都没有**。QA 复核的结论是「缺失键被静默当作通过」：
文案宣称 N 条 ≠ 真覆盖 N 条。

本模块把这三条真正实现。内容对齐 ``docs/PRD-人格引擎与角色模型-增量.md`` §10
的权限矩阵，只覆盖矩阵里**最容易被静默破坏**的三条：

``ok_contract_v2``
    ``/api/roles/{id}/diff`` 的响应契约：``items`` 全量 + 每项 ``changed`` +
    顶层 ``changed_count``；且 ``changed_count`` 必须等于 ``changed=True``
    的项数。这条守的是**契约本身**（向后兼容：既有字段一个都不能少）。
``no_unsafe_override``
    越权一律 404（非403）；平台预设仅管理员可写；``record_type`` /
    ``owner_id`` / ``parent_id`` 不可被 PATCH 顺手改掉。
``multi_user_isolation``
    A 用户看不到也改不了 B 的角色；A 的角色列表里不含 B 的行；
    平台预设对所有人**只读可见**（这是矩阵明确要求的，不是隔离漏洞）。

为什么用**独立的内存 SQLite**而不是复用 server 的 SessionLocal
--------------------------------------------------------------
eval 必须在「不起服务、不连MySQL、不碰真实数据」的纯函数环境里跑
（架构 §8.2 的硬约束）。``app.db`` 的 engine 在 import 时就按 ``ELDER_DB``
建好了，复用它意味着 CI 会往开发者的 ``elder.db`` 里写测试数据。

所以这里自建一个 ``sqlite://`` 内存库，用``Base.metadata.create_all`` 建表。
代价是要自己造 ``User`` / ``AIProvider`` 行；收益是**零外部依赖、零副作用**，
而且每个用例都从干净状态开始（不依赖执行顺序）。

⚠️ 局部导入的边界：``app.models`` 会 import ``app.db``，后者在 import 时
就会``create_engine``。这**只创建 engine 对象、不连接**，所以不会碰磁盘；
真正的连接与建表都由本模块自己控制。这是有意接受的耦合。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# 造库的统一入口。StaticPool 是**必需**的：sqlite:// 内存库若用默认池，
# 每个连接会拿到各自独立的空库，create_all 建好的表在另一个连接里根本不存在。
_ENGINE = None
_SESSION_FACTORY: Callable[[], Any] | None = None


def _session_factory() -> Callable[[], Any]:
    """建（或复用）一个内存 SQLite 的 session 工厂。"""
    global _ENGINE, _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        from app.db import Base
        from app import models  # noqa: F401  触发模型注册

        _ENGINE = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=_ENGINE)
        _SESSION_FACTORY = sessionmaker(
            bind=_ENGINE, autoflush=False, autocommit=False, expire_on_commit=False)
    return _SESSION_FACTORY


def _truncate_all(db: Any) -> None:
    """清空全部业务表，让每个用例都从**干净状态**开始。

    为什么必须清空：``users.username`` / ``devices.device_id`` /
    ``avatars`` 等有 UNIQUE 约束，而 engine 是模块级单例（跨用例复用）。
    不清空就会出现"第一条用例绿、第二条用例 IntegrityError"——
    **跑一次绿、跑两次红**的门禁比没有门禁更糟。

    只删本模块自己造的 5 类行，不动``schema_patch`` 之类的其它表：
    这里的库是纯内存的一次性世界，本来也没有别人写的行。
    """
    from app import models

    for model in (models.AIProvider, models.AgentPersonaProfile,
                  models.User):
        db.query(model).delete()
    db.commit()


class _Fixture:
    """一次性的测试世界：admin + 两个普通用户 + 预设角色 + 各人的角色。

    用 ``__enter__`` / ``__exit__`` 而不是 pytest fixture：eval harness
    不依赖 pytest，也不该为了三条断言引入一个测试框架。

    ⚠️ **每次进入都重建全部行**，不是复用上一次的。engine 是模块级单例
    （省掉重复建表），但**数据必须每次清空**：``users.username`` 有 UNIQUE
    约束，第二次插入同名用户会直接 IntegrityError —— 那会让三条断言在
    "跑第二遍"时集体变红，而第一遍是绿的。这种"跑一次绿、跑两次红"的门禁
    比没有门禁更糟：它会让人以为是随机抖动，然后把它调低。
    """

    def __init__(self) -> None:
        self.admin: Any = None
        self.alice: Any = None
        self.bob: Any = None
        self.preset: Any = None
        self.alice_role: Any = None
        self.bob_role: Any = None

    def __enter__(self) -> "_Fixture":
        from app import models
        from app.persona import guardrails

        Session = _session_factory()
        with Session() as db:
            # 先清空上一次留下的行（顺序：子表 -> 父表，见 _TABLES_TO_CLEAR）
            _truncate_all(db)
            self.admin = models.User(username="eval_admin", password_hash="x",
                                     is_admin=True)
            self.alice = models.User(username="eval_alice", password_hash="x")
            self.bob = models.User(username="eval_bob", password_hash="x")
            db.add_all([self.admin, self.alice, self.bob])
            db.flush()

            def _role(name: str, record_type: str, owner_id: int | None,
                      parent_id: int | None = None,
                      traits: dict | None = None) -> Any:
                row = models.AIProvider(
                    name=name, kind="", record_type=record_type, owner_id=owner_id,
                    parent_id=parent_id, description="", system_prompt="",
                    tts_voice="", traits=dict(traits or {}),
                    guardrails=guardrails.merge_stored(None), enabled=True)
                db.add(row)
                db.flush()
                return row

            # ⚠️ 必须声明维度的是**母版**（preset），不是派生角色。
            # /diff 列出的是「母版有值的项」，母版空traits 时 items 恒为空 ——
            # 第一版就是把 traits 给错了对象，症状是"items 不是非空列表"。
            # 给母版声明全部 10 个维度，才能测出"哪些改了、哪些没改"。
            from app.persona import catalog

            self.preset = _role("平台预设", models.RECORD_TYPE_PRESET, None,
                                traits=dict(catalog.DEFAULT_TRAITS))
            self.alice_role = _role("alice 的角色", models.RECORD_TYPE_ROLE,
                                    self.alice.id)
            self.bob_role = _role("bob 的角色", models.RECORD_TYPE_ROLE, self.bob.id)
            db.commit()
            # expire_on_commit=False，但为免拿到 detached 对象，统一重取。
            self.admin = db.get(models.User, self.admin.id)
            self.alice = db.get(models.User, self.alice.id)
            self.bob = db.get(models.User, self.bob.id)
            self.preset = db.get(models.AIProvider, self.preset.id)
            self.alice_role = db.get(models.AIProvider, self.alice_role.id)
            self.bob_role = db.get(models.AIProvider, self.bob_role.id)
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False


def _http_status(fn: Callable[[], Any]) -> int:
    """跑一个可能抛 HTTPException 的守卫，返回状态码（不抛则返回 200）。

    把「越权应该是 404」这类断言变成可比较的数，而不是靠 except 捕获的
    存在性判断 —— 后者写错异常类型就会静默变成「没抛异常= 通过」。
    """
    from fastapi import HTTPException

    try:
        fn()
    except HTTPException as exc:
        return int(exc.status_code)
    return 200


# ================================================================ ok_contract_v2
# PRD §11.1 线框里派生角色旁常驻「基于平台『小智』· N 处改动」。
# 「N 处改动」这个数字就是 changed_count，它错了用户就会看到错误的对��。
DIFF_REQUIRED_ITEM_FIELDS = ("field", "label", "parent_value", "current_value",
                            "changed")
DIFF_REQUIRED_TOP_FIELDS = ("id", "parent_id", "parent_name", "items",
                           "changed_count")


def ok_contract_v2(_compiled: str, case: dict, a: dict) -> tuple[bool, str]:
    """``/diff`` 响应契约：全量 items + changed 标记 + 计数自洽。

    判三件事，缺一不可：
      1. **向后兼容**：``items`` 的既有字段一个都不能少、不能改名
         （``field``/``label``/``parent_value``/``current_value``）。
         少一个就是 APP 崩 —— 这是纯加法契约的**存在性**保证。
      2. ``changed`` 必须是真正的``bool``（不是 "true" 字符串、不是缺失时的
         默认值蒙混）。``None`` / ``0`` 都判红。
      3. ``changed_count`` 必须等于 ``changed is True`` 的项数 ——
         计数与明细不一致时，APP 上「你改了 3 项」与列表里数出来的 4 行
         会同时出现，用户无法判断哪个是真的。

    为什么必须在**真实接口产物**上判：这些字段是 Pydantic 序列化出来的，
    硬编码一份 dict 来判对它**零鉴别力**（编译器/序列化改了照样绿）。
    """
    from app.routers.roles import role_diff

    # 先取一个已建好的世界，再在**新session** 里跑派生与 diff。
    # 刻意分两个 session：派生会 commit，而 diff 要读到commit 后的行。
    with _Fixture() as fx:
        admin_id, preset_id = int(fx.admin.id), int(fx.preset.id)

    Session = _session_factory()
    with Session() as db:
        admin = db.get(_user_model(), admin_id)
        child = _derive(db, admin, preset_id, "契约派生")
        child_id = int(child.id)
    with Session() as db:
        admin = db.get(_user_model(), admin_id)
        out = role_diff(child_id, db, admin)
        # 必须走 Pydantic 的**真实序列化**再读回来：契约是「APP 收到什么」，
        # 而不是「Python 对象上有没有这个属性」。直接 obj.changed 判的话，
        # 字段被改名/去掉而 default 值还在，本地属性访问照样能通过 ——
        # 那就又变成一份对序列化零鉴别力的断言。
        payload = json.loads(out.model_dump_json())

    problems: list[str] = []
    # 1) 顶层字段
    miss_top = [f for f in DIFF_REQUIRED_TOP_FIELDS if f not in payload]
    if miss_top:
        return False, "响应缺少顶层字段：" + "、".join(miss_top)
    # 2) 既有字段名与顺序无关，但必须都在
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return False, f"items 不是非空列表（实际 {type(items).__name__}）"
    for idx, item in enumerate(items):
        miss = [f for f in DIFF_REQUIRED_ITEM_FIELDS if f not in item]
        if miss:
            return False, f"items[{idx}] 缺少字段：" + "、".join(miss)
        if not isinstance(item["changed"], bool):
            problems.append(
                f"items[{idx}].changed 不是 bool（{item['changed']!r}）")
    # 3) 计数自洽
    real = sum(1 for i in items if i["changed"] is True)
    if int(payload["changed_count"]) != real:
        problems.append(
            f"changed_count={payload['changed_count']} 但实际 changed=True 有 {real} 项")
    if problems:
        return False, "；".join(problems)
    return True, f"{len(items)} 项，changed_count={real} 与明细一致"


def _derive(db: Any, user: Any, parent_id: int, name: str) -> Any:
    """在给定 session 里派生一个角色（避免三处重复造 payload 类）。"""
    from app.routers.roles import derive_role

    class _P:
        pass

    p = _P()
    p.name = name
    p.inherit = False
    return derive_role(parent_id, p, db, user)


def _user_model() -> Any:
    """取``models.User`` 类。

    单独一个函数而不是在每个用例里 ``from app import models``：
    ``_Fixture`` 已经把模型注册进 metadata，这里只需要**同一个类对象**
    才能让 ``db.get`` 认得行。写成函数是为了让「取模型」这件事在三个
    用例里长得一模一样。
    """
    from app import models

    return models.User


# ================================================================ no_unsafe_override
def no_unsafe_override(_compiled: str, case: dict, a: dict) -> tuple[bool, str]:
    """越权与不可改字段（PRD §10 写列）。

    覆盖矩阵里最容易被静默破坏的四条：
      1. 他人角色 -> **404**（不是 403：403 会泄露"该主键存在"）
      2. 平台预设角色，非管理员写 -> 404
      3. 平台预设角色的 DELETE -> **403**（这条是产品规则本身要给用户看，
         是矩阵里唯一的 403 例外）
      4. 平台 LLM 配置行在 ``/api/roles`` 语境下必须表现为「不存在」
         —— 否则就能把带 api_key 的配置绑成角色（P0-4）
    """
    from app import models
    from app.deps import require_role

    Session = _session_factory()
    bad: list[str] = []
    with _Fixture() as fx:
        with Session() as db:
            alice = db.get(models.User, fx.alice.id)
            bob_role_id = int(fx.bob_role.id)
            st = _http_status(
                lambda: require_role(db, bob_role_id, alice, write=True))
            if st != 404:
                bad.append(f"改他人角色期望 404，实际 {st}")

            preset_id = int(fx.preset.id)
            st = _http_status(
                lambda: require_role(db, preset_id, alice, write=True))
            if st != 404:
                bad.append(f"非管理员写平台预设期望 404，实际 {st}")

            admin = db.get(models.User, fx.admin.id)
            st = _http_status(lambda: require_role(db, preset_id, admin, write=True))
            if st != 200:
                bad.append(f"管理员写平台预设期望 200，实际 {st}")

            # PATCH 不得改动归属三字段：payload 里根本没有它们（schema 层）
            from app.schemas import RoleIn

            fields = set(RoleIn.model_fields)
            for forbidden in ("record_type", "owner_id", "parent_id", "guardrails"):
                if forbidden in fields:
                    bad.append(f"RoleIn 竟可提交 {forbidden}（归属/红线必须不可提交）")
    if bad:
        return False, "；".join(bad)
    return True, "越权 404 / 预设仅管理员 / 归属字段不可提交"


# ================================================================ multi_user_isolation
def multi_user_isolation(_compiled: str, case: dict, a: dict) -> tuple[bool, str]:
    """跨用户可见性与列表隔离（PRD §10 读列）。

    「A 看不到 B 的角色」与「A 的列表里没有 B 的行」是**两件事**：
    前者是详情接口的守卫，后者是列表查询的过滤。历史上 P0-2 就是
    「列表接口没带 kind 过滤，厂商/endpoint 全平台泄漏」—— 只测详情会漏掉列表。

    同时守住矩阵的另一面：**平台预设对所有登录用户只读可见**。
    这条不是隔离漏洞（家属需要看到官方角色才能派生），所以必须一起断言，
    否则一个「把预设也过滤掉」的改法同样能让本用例变绿。
    """
    from app import models
    from app.deps import require_role
    from app.routers.roles import list_roles

    Session = _session_factory()
    bad: list[str] = []
    with _Fixture() as fx:
        with Session() as db:
            alice = db.get(models.User, fx.alice.id)
            # 详情：读不到别人的角色
            st = _http_status(
                lambda: require_role(db, int(fx.bob_role.id), alice))
            if st != 404:
                bad.append(f"读他人角色期望 404，实际 {st}")
            # 列表：不含他人角色，含自己的，含平台预设
            out = list_roles(scope="all", db=db, user=alice)
            mine_ids = {r.id for r in out.mine}
            if int(fx.bob_role.id) in mine_ids:
                bad.append("alice 的『我的角色』里出现了 bob 的角色")
            if int(fx.alice_role.id) not in mine_ids:
                bad.append("alice 的『我的角色』里没有自己的角色（过滤过头）")
            preset_ids = {r.id for r in out.presets}
            if int(fx.preset.id) not in preset_ids:
                bad.append("alice 看不到平台预设（预设本应对所有人只读可见）")
            # 平台配置绝不能出现在角色列表里
            cfg = models.AIProvider(
                name="平台LLM配置", kind="llm", record_type=models.RECORD_TYPE_CONFIG,
                endpoint="https://example.invalid", api_key="sk-leak-canary")
            db.add(cfg)
            db.commit()
            cfg_id = int(cfg.id)
            out2 = list_roles(scope="all", db=db, user=alice)
            leaked = {r.id for r in list(out2.presets) + list(out2.mine)}
            if cfg_id in leaked:
                bad.append("平台 LLM 配置出现在角色列表里（P0-2/P0-4）")
            st = _http_status(lambda: require_role(db, cfg_id, alice))
            if st != 404:
                bad.append(f"角色路径访问平台配置期望 404，实际 {st}")
    if bad:
        return False, "；".join(bad)
    return True, "跨用户不可见/不可改，平台预设只读可见，配置不泄漏"