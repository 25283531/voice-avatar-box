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
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from .config import APP_NAME, APP_VERSION, CORS_ORIGINS
from .db import SessionLocal, init_db
from .models import Avatar
from .routers import (admin_presets, avatars, auth, device_api, devices, home, mail,
                      membership, providers, reminders, roles, stats, support, usage)
from .schema_patch import run_all as run_schema_upgrade
from .security import hash_password
# 记忆的 router 单独导入，**不从 .memory 包里取**：
# memory/__init__.py 是算法层门面（client/store/retrieve/persona），刻意不引 FastAPI，
# 这样任何地方想用记忆算法都不必把 HTTP 栈拖进来。
from .memory import router as memory_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("elder")

STATIC_DIR = Path(__file__).parent / "static"


def _as_utc(dt: datetime | None) -> datetime | None:
    """把一个可能是 naive 的 datetime 归一到 **UTC aware**。

    为什么需要：SQLite 的 ``DateTime`` 列读回来是 **naive**（不带 tzinfo）的，
    而同一个对象在同一个 session 里刚写入、又可能带 tzinfo。两种表示直接比较
    要么抛 TypeError，要么退化成字符串比较 —— 后者会因格式差异给出错误结论
    （见 ``_seed_migration`` 步骤 4 的注释，那里的偶发红就是这个原因）。

    naive 一律按 **UTC** 解释：本项目所有时间戳都由 ``models.utcnow()`` 写入，
    它本来就是 UTC，只是存储层把 tzinfo 丢掉了。

    MySQL 的 DATETIME 同样不带时区，走的是同一条归一，行为一致。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _seed() -> None:
    """首次启动写入种子数据：默认管理员 + 平台预设角色（小暖）+ 纯形象。

    ⚠️ 角色与形象的关系（架构 §7.1）：人格住在 ``ai_providers`` 的一行
    ``record_type='preset'`` 里；``Avatar`` 只剩"长什么样"。

    预设角色与种子形象都做成**幂等 upsert**，而不是"表为空才写"：
    运营在小暖的 traits 上做的微调，不该被一次重启悄悄抹掉。
    """
    from . import models
    from .persona import catalog, guardrails
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

        # ---- 平台预设角色（人格的唯一落点）----
        # 判定条件用 kind == ''（而不是 record_type == 'preset'）：
        # 回填规则会把老库里所有历史角色行标成 preset，用后者会导致
        # "已有角色 -> 不建预设角色"，母版就永远缺失了（见架构 §7.1 的警告）。
        preset = db.scalar(
            select(models.AIProvider).where(models.AIProvider.kind == "")
            .order_by(models.AIProvider.id).limit(1))
        if preset is None:
            preset = models.AIProvider(
                name=catalog.PRESET_ROLE_NAME,
                description="温和陪伴风格，语速偏慢，适合老年用户",
                kind="",
                record_type=models.RECORD_TYPE_PRESET,
                owner_id=None,
                parent_id=None,
                system_prompt=catalog.DEFAULT_PRESET_PROMPT,
                # ⚠️ 常量搬家，**值逐字不变**（"zh-CN-XiaoxiaoNeural"）。
                # 为什么必须搬：运营后台的「恢复出厂默认」需要单一真源 ——
                # 出厂默认散落在 catalog 与本文件两处时，reset 就得反向依赖
                # main.py，而这里是**启动期写表**的路径，让请求路径依赖它
                # 等于把两者耦合（那条路径历史上正是 P0 的发源地）。
                # 幂等性与判据正确性论证见 docs/架构设计-人格预设运营后台.md §9.2：
                # 判据一个字符未改、建行条件未改，故**不改任何业务数据**。
                tts_voice=catalog.DEFAULT_PRESET_TTS_VOICE,
                traits=dict(catalog.DEFAULT_TRAITS),
                guardrails=guardrails.merge_stored(None),
                enabled=True,
            )
            db.add(preset)
            db.flush()
            log.info("seed: created preset role「%s」(id=%s)",
                     catalog.PRESET_ROLE_NAME, preset.id)
        elif preset.name != catalog.PRESET_ROLE_NAME:
            # 老库里的 kind=='' 行是历史角色，不是平台预设。**不改它**：
            # 改成"预设"会让某个用户的角色突然变成全平台可见且只读，
            # 也可能把别人的行判给平台（架构 §4.1 的安全侧选择）。
            # 只记一条 warning，让管理员按需手工转归属。
            log.warning(
                "seed: 已存在 kind='' 的角色 id=%s「%s」（owner=%s），"
                "按安全侧规则保持只读；如需转为某人的角色，请管理员手工调整归属",
                preset.id, preset.name, preset.owner_id)

        # ---- 纯形象（只管外观/灯效/默认音色）----
        av = db.scalar(select(Avatar).where(Avatar.deleted_at.is_(None))
                       .order_by(Avatar.id).limit(1))
        if av is None:
            av = Avatar(
                name="小暖（默认）",
                description="温和陪伴风格，语速偏慢，适合老年用户",
                record_type=models.RECORD_TYPE_PRESET,
                owner_id=None,
                eaf_pack_name="xiaonuan",
                screen_width=320,
                screen_height=240,
                clips=dict(DEFAULT_CLIPS),
                # 音色：这是**形象的默认音色**；角色自带音色时以角色的为准
                # （见 routers/device_api.py 的 voice fallback）。
                tts_voice="zh-CN-XiaoxiaoNeural",
                emotion_lights=dict(DEFAULT_LIGHTS),
            )
            db.add(av)
            log.info("seed: created default avatar")
        db.commit()


def _seed_migration() -> None:
    """把 Avatar 上的人格迁到 AIProvider（架构 §7.1 四步，**幂等 + 可回滚**）。

    为什么需要：改造前人格写在 ``Avatar.system_prompt``，而设备侧只下发
    avatar、不下发 agent。角色模型落地后编译时从角色行取 spec，
    若不迁移，存量设备的角色指向为空 -> 拿不到 prompt -> 无人格。

    四步：
      1. 建母版：不存在 kind=='' 的 AIProvider 行时，建一条 preset 角色；
      2. 迁人格：把 Avatar.system_prompt / tts_voice 写进preset 角色行
         （**仅当目标为空时**才写，保证重复启动不会覆盖后来的运营修改）；
      3. 收权：Avatar.system_prompt 置空（不是删列 —— 本项目没有 Alembic，
         schema_patch 只能 ADD COLUMN，删列做不到；停用 + 迁移是唯一安全路径）；
      4. 绑定回填：给还没有 agent_id 的存量设备补上 preset 角色。

    回滚方案：步骤 3 是置空而非删除，所以回滚只需去掉步骤 3（从备份恢复该列）。
    步骤 1 建的新行 ``record_type='preset'``，旧代码只看 ``kind == ''``
    -> 会把它当角色渲染，不会崩；且旧代码路径根本不读它，
    所以**回滚不会导致设备失去对话**。
    """
    from . import models
    from .persona import catalog, guardrails

    with SessionLocal() as db:
        # ---- 步骤 1：建母版（仅当完全没有 kind=='' 的行）----
        preset = db.scalar(
            select(models.AIProvider).where(models.AIProvider.kind == "")
            .order_by(models.AIProvider.id).limit(1))
        if preset is None:
            preset = models.AIProvider(
                name=catalog.PRESET_ROLE_NAME,
                description="温和陪伴风格，语速偏慢，适合老年用户",
                kind="",
                record_type=models.RECORD_TYPE_PRESET,
                owner_id=None,
                parent_id=None,
                system_prompt="",
                tts_voice="",
                traits=dict(catalog.DEFAULT_TRAITS),
                guardrails=guardrails.merge_stored(None),
                enabled=True,
            )
            db.add(preset)
            db.flush()
            log.info("migrate: created preset role id=%s", preset.id)

        # ---- 步骤 2：迁人格（仅当目标为空，避免覆盖后来的运营修改）----
        migrated = 0
        for av in db.scalars(select(Avatar)).all():
            prompt = (av.system_prompt or "").strip()
            if prompt and not (preset.system_prompt or "").strip():
                preset.system_prompt = prompt
                migrated += 1
            voice = (av.tts_voice or "").strip()
            if voice and not (preset.tts_voice or "").strip():
                preset.tts_voice = voice

        # ---- 步骤 4：绑定回填（先做，置空放在后面）----
        # 设备侧当前只下发 avatar（device_api.sync），不绑角色就拿不到 spec
        # -> 无人格可用。回填让存量设备平滑切到新模型。
        #
        # ⚠️⚠️ **必须只作用于「迁移之前就存在」的设备**（QA 第 2 轮同类回归）：
        # 本函数在**每次启动**都跑，而原写法是「所有 agent_id IS NULL 的设备都绑
        # preset」。于是两种用户意图都会被篡改：
        #   1. **新建的设备**（从没绑过角色）-> 重启后凭空多出一个 preset 角色；
        #   2. 用户**主动解绑**（DELETE /roles/{id} 会把 agent_id 置空）
        #      -> 每次重启又被强行绑回来，解绑等于无效。
        # 两者都是「启动流程改写用户数据」，与 schema_patch 那条回填是同一个病根。
        #
        # 判据用「设备创建时间 <= preset 创建时间」，而不是 ``device.id <= preset.id``：
        # 后者**曾经被用过一次且是错的** —— devices 与 ai_providers 是两张表、
        # **各自独立的自增序列**，跨表比 id 毫无意义（实测全新库里 device.id=1
        # 而 preset.id=1，判据失效，设备照样被凭空绑上）。
        # created_at 由写入端无条件填充，且与 preset 在同一时间轴上可比较。
        #
        # 迁移起点取 preset 行的 created_at：preset 是在 _seed() 里新建的，
        # 因此「比 preset 更早创建的设备」正是角色模型落地**之前**就存在的存量设备。
        #
        # ⚠️ **比较必须放在 Python 侧，不能放 SQL**（QA-2 复核指出这里 3 跑 1 抖）：
        # SQLite 的 DateTime 列**读回来是没有时区的 naive 值**，而同一个对象在
        # 同一 session 里刚写入又可能带 tzinfo。两种表示混在一起时，SQL 里做的
        # 其实是**字符串比较** —— naive 的 "…26.503146" 与带 +00:00 的
        # "…26.617993+00:00" 同秒时就可能误判，这正是那条断言偶发红的根因。
        # 统一归一到 UTC aware 之后再在 Python 里比，才不受存储格式影响。
        filled = 0
        boundary = _as_utc(preset.created_at)
        if boundary is not None:
            for dev in db.scalars(
                select(models.Device).where(models.Device.agent_id.is_(None))
            ).all():
                created = _as_utc(dev.created_at)
                # created 为 None 的行（老库里可能没有有效值）**不参与回填**：
                # 无法证明它是存量设备就不改动它 —— 与整体"安全侧"口径一致。
                if created is not None and created <= boundary:
                    dev.agent_id = preset.id
                    filled += 1
        if filled:
            log.info("migrate: %d 台存量设备已绑定平台预设角色 id=%s", filled, preset.id)

        # ---- 步骤 3：收权（置空 Avatar.system_prompt）----
        cleared = 0
        for av in db.scalars(select(Avatar)).all():
            if (av.system_prompt or "").strip():
                av.system_prompt = ""
                cleared += 1

        db.commit()
        if migrated or cleared or filled:
            log.info("migrate: 人格迁移完成（迁入 %s 条 / 置空 %s 条 / 回填 %s 台设备）",
                     migrated, cleared, filled)

        # 老角色归属的补救提示（架构 §4.1.1）：无法自动判断归属，
        # 因此只提示管理员按需手工处理，绝不猜一个 owner。
        orphans = db.scalars(
            select(models.AIProvider).where(
                models.AIProvider.kind == "",
                models.AIProvider.record_type == models.RECORD_TYPE_PRESET,
                models.AIProvider.id != preset.id,
            ).order_by(models.AIProvider.id)).all()
        for row in orphans:
            log.warning(
                "migrate: 历史角色 id=%s「%s」(created_at=%s) 无归属信息，"
                "已按安全侧规则置为 preset（只读）。若确属某位用户，"
                "请管理员在管理端手工转归属：kind 保持空串、record_type 改 'role'、"
                "owner_id 设为该用户",
                row.id, row.name, row.created_at)


def _seed_membership() -> None:
    """会员系统种子数据：金额区间、配额配置、默认套餐、服药关键词。"""
    from . import membership as mbs

    with SessionLocal() as db:
        mbs.seed_defaults(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 本项目没有 Alembic，create_all 既不会给老库补列，也不会给老库建索引。
    # run_all() 按「补列 -> 回填 record_type -> 去重 + 回填 agent_key -> 建索引」
    # 的顺序幂等执行；顺序不能调换，每一步都依赖前一步（详见 schema_patch）。
    upgrade = run_schema_upgrade()
    if any(upgrade.values()):
        log.info("schema 升级：%s", upgrade)
    # ⚠️ 顺序：先 _seed() 再 _seed_migration()，**不能调换**。
    # _seed() 负责建平台预设角色并写入 DEFAULT_PRESET_PROMPT；_seed_migration()
    # 的步骤 1 只在「不存在 kind=='' 的行」时才建母版。若反序执行，新装库里
    # _seed_migration 会先建出一个 system_prompt 为空的母版（人格此时还没地方可迁），
    # 随后 _seed() 看到 kind=='' 行已存在便不再补写 —— 结果是**全新部署的
    # 平台预设角色没有人格**。反序还不会报错，只是静默少一段话，很难发现。
    _seed()
    _seed_migration()
    _seed_membership()
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
          providers.router, roles.router, stats.router, device_api.router, home.router):
    app.include_router(r)

# 会员系统：用户端 /api/membership、管理端 /api/admin/membership、设备端 /api/device/quota 等
for r in (membership.router, membership.admin_router, membership.device_router):
    app.include_router(r)

# token 用量：用户端 /api/usage（只看自己设备）、管理端 /api/admin/usage（全量+聚合）。
# 计量数据由调用方（语音链路等）经 app.usage 落库，表结构见 models.ModelUsage 类注释。
for r in (usage.router, usage.admin_router):
    app.include_router(r)

# 邮箱服务：管理端 SMTP 配置 /api/admin/mail、找回密码 /api/auth/password/reset、
# 工单 /api/support/tickets（用户端）+ /api/admin/support/tickets（管理端）
for r in (mail.admin_router, mail.auth_router, support.router, support.admin_router):
    app.include_router(r)

# 长期记忆与人格：用户端 /api/memory（搜索/写入/删除、语气画像、偏好示例、审计）、
# 管理端 /api/admin/memory/stats（**只有聚合计数**，管理员看不到任何记忆正文，
# 理由见 memory/router.py 的模块注释）。
# ⚠️ 记忆模块本身只提供 memory.bridge 的两个钩子供语音链路调用；
# server/app/voice/ 尚未落地，所以这里只注册 HTTP 接口，不假装接线了语音（见 bridge.py）。
for r in (memory_router.router, memory_router.admin_router):
    app.include_router(r)

# 人格预设运营后台：/api/admin/presets（调整现有平台预设的 5 个可改字段 +
# 恢复默认 + 软约束 + 只读红线展示 + 审计）。只对**现有**平台预设开放，
# 不支持新建（设计 §2）。router 级 get_current_admin —— 非管理员 403。
app.include_router(admin_presets.router)


@app.get("/api/health")
def health():
    """容器探活端点：只表达「进程活着」，永远返回 200。

    ⚠️ 不要往这里加任何后台任务的健康状态 —— Dockerfile 的 HEALTHCHECK 每 30 秒
    打一次这里，它一旦非 200 容器就被判 unhealthy，而此时业务 API 其实还能正常服务。
    后台任务的健康状况请看 /api/health/scheduler。
    """
    return {"ok": True, "service": APP_NAME, "version": APP_VERSION}


@app.get("/api/health/scheduler")
def health_scheduler():
    """后台周期任务健康快照（排期补全 / 超时升级 / 订单与会员过期 / 离线清扫）。

    这个端点**不参与任何探活链路**，只给运维和监控看：
      - last_ok_at           最近一次完整跑完的时刻，从未成功过为 None
      - consecutive_failures 连续失败次数，成功一轮即归零
      - last_error           最近一次异常信息，成功时为 None
    """
    # 惰性导入：scheduler 依赖 db / models / membership，模块级直接 import
    # 会把初始化顺序钉死，放在函数里更稳。
    from .scheduler import scheduler_health

    return scheduler_health()


# ---------------------------------------------------------------- 管理页面
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def console():
        return FileResponse(str(STATIC_DIR / "index.html"))

    # 用户端界面：面向普通用户（设备 / 会员 / 提醒 / 记忆 / 形象 / 角色 / 用量）。
    # 与管理后台（/）是**两个入口**，不是同一页面按角色隐藏几个 tab ——
    # 两者的信息架构本来就不一致（管理端管平台，用户端管自己）。
    @app.get("/user")
    def user_console():
        return FileResponse(str(STATIC_DIR / "user.html"))
else:  # pragma: no cover
    @app.get("/")
    def console_placeholder():
        return {"message": "静态管理页面未打包，请访问 /docs 查看接口"}
