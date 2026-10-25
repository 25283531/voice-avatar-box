"""老人陪伴提醒助手 · 业务后端入口。

职责边界
--------
- 本服务负责**业务面**：设备绑定、提醒规则与回执、形象与提示词、AI 供应商配置、交互统计。
- 语音链路是**自研**的（server/app/voice/，xiaozhi 公开协议的服务端实现），
  与业务 API 同源同端口；设备在 /api/device/sync 里拿到的 WS / OTA 地址
  指向本服务自己（推导逻辑见 routers/device_api.py 的 _voice_endpoints）。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from .config import (
    BeijingFormatter,
    LOG_FORMAT,
    APP_NAME,
    APP_VERSION,
    CORS_ORIGINS,
    DEFAULT_ADMIN_PASSWORDS,
    RESET_ADMIN_PASSWORD,
    SEED_ADMIN_PASSWORD,
    SUPPORT_EMAIL,
)
from .db import SessionLocal, get_db, init_db
from .models import Avatar
from .routers import (admin_presets, avatars, auth, chat_quota, device_api, devices,
                      geo as geo_router, home, mail, providers, reminders, roles,
                      stats, support, usage, voice_speakers, weather as weather_router)
from .schema_patch import run_all as run_schema_upgrade
from .security import hash_password, verify_password
# 记忆的 router 单独导入，**不从 .memory 包里取**：
# memory/__init__.py 是算法层门面（client/store/retrieve/persona），刻意不引 FastAPI，
# 这样任何地方想用记忆算法都不必把 HTTP 栈拖进来。
from .memory import router as memory_router
# 语音链路（自研）：import 本身**不注册任何路由**（见 voice/__init__.py 的 attach），
# 这里只把模块引进来，真正接线在 app 定义之后的那一行 _voice.attach(app)。
from . import voice as _voice

# ⚠️ 不用 basicConfig(format=...)：那会走默认 Formatter，而默认 Formatter 的
# asctime 读进程 TZ 环境变量（CI 上跑出来就是 UTC）。显式挂 config 里的
# BeijingFormatter —— 时间恒为北京时间，与 TZ 环境变量无关。
_log_handler = logging.StreamHandler()
_log_handler.setFormatter(BeijingFormatter(LOG_FORMAT))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
log = logging.getLogger("elder")

STATIC_DIR = Path(__file__).parent / "static"

# 三条安全日志的**稳定标记**：运维要 grep、冒烟要断言，改文案时这三个串不许动
# （tools/smoke_auth.py 里各有一份逐字对齐的副本，改了那边会红）。
#: 种子管理员的随机初始口令，形如 ``ELDER-INITIAL-PASSWORD=<pwd>``
SEED_PASSWORD_MARKER = "ELDER-INITIAL-PASSWORD"
#: 存量默认口令体检告警，形如 ``ELDER-DEFAULT-PASSWORD-ALERT user=admin``
DEFAULT_PASSWORD_ALERT_MARKER = "ELDER-DEFAULT-PASSWORD-ALERT"
#: 一次性轮换已执行，形如 ``ELDER-ADMIN-PASSWORD-ROTATED user=admin``
PASSWORD_ROTATED_MARKER = "ELDER-ADMIN-PASSWORD-ROTATED"

#: 平台预设角色历史上用过的默认名。2026-10-20 品牌统一为「小智」后，
#: ``_seed()`` 会把这些名字**迁移**成 ``catalog.PRESET_ROLE_NAME``。
#: 只认这几个字符串，运营改过的名字一律不动（见 _seed 里的迁移注释）。
_LEGACY_PRESET_NAMES: frozenset[str] = frozenset({
    "小暖（平台预设）",
    "小暖（默认）",
    "小暖",
})
#: 默认形象历史上的名字，迁移规则同上（``eaf_pack_name`` 是素材标识，**不改**）
_LEGACY_AVATAR_NAMES: frozenset[str] = frozenset({
    "小暖（默认）",
    "小暖",
})


def _seed_admin_password() -> tuple[str, bool]:
    """种子管理员的初始口令，返回 ``(口令, 是否由本进程随机生成)``。

    随机分支用 ``secrets.token_urlsafe(16)``（22 个字符 / 128 bit 熵），
    不用 ``random`` —— 后者是 Mersenne Twister，**可预测**，拿它发口令等于没发。
    """
    if SEED_ADMIN_PASSWORD:
        return SEED_ADMIN_PASSWORD, False
    return secrets.token_urlsafe(16), True


def _hit_default_password(password_hash: str) -> str | None:
    """该口令哈希是否命中「已知默认口令」；命中则返回命中的那个明文，否则 None。

    ⚠️ 判定**必须**用 ``verify_password``（PBKDF2 重新算一遍再 compare_digest），
    不能把 ``password_hash`` 拿去做字符串比较：hash 里带 16 字节随机盐，同一个
    口令两次 hash 出来的字符串完全不同，字符串比对会**恒 False**
    -> 存量库全员漏报 -> 这条体检等于没写（最危险的失效形态）。
    """
    for weak in DEFAULT_ADMIN_PASSWORDS:
        if verify_password(weak, password_hash):
            return weak
    return None


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
    """首次启动写入种子数据：默认管理员 + 平台预设角色（小智）+ 纯形象。

    ⚠️ 角色与形象的关系（架构 §7.1）：人格住在 ``ai_providers`` 的一行
    ``record_type='preset'`` 里；``Avatar`` 只剩"长什么样"。

    预设角色与种子形象都做成**幂等 upsert**，而不是"表为空才写"：
    运营在小智的 traits 上做的微调，不该被一次重启悄悄抹掉。
    """
    from . import models
    from .persona import catalog, guardrails
    from .routers.avatars import DEFAULT_CLIPS, DEFAULT_LIGHTS

    with SessionLocal() as db:
        if db.query(models.User).count() == 0:
            pwd, generated = _seed_admin_password()
            admin = models.User(
                username="admin",
                password_hash=hash_password(pwd),
                nickname="管理员",
                is_admin=True,
            )
            db.add(admin)
            if generated:
                # ⚠️ **必须打印**：随机口令只在这一次存在，重启后无法复现。
                # 运维的第一现场是 docker logs，把它只写进文档等于没写 ——
                # 结果是新部署没人拿得到初始口令。
                # 级别取 WARNING 而不是 INFO：docker logs 里 INFO 会被大量启动
                # 日志淹没，而这条是"不看就登不进去"的级别。
                log.warning(
                    "【安全】种子管理员已创建：用户名 admin，"
                    "%s=%s\n"
                    "    ⚠️ 这是唯一的初始口令，请立即保存并登录后修改 —— "
                    "重启不会再显示，丢了只能靠 ELDER_RESET_ADMIN_PASSWORD 重置。\n"
                    "    新部署想自己指定：部署前设 ELDER_SEED_ADMIN_PASSWORD=<口令>。",
                    SEED_PASSWORD_MARKER, pwd)
            else:
                # 口令来自配置，**刻意不回显**：它已经在 .env / compose 里躺成明文了，
                # 再往日志里抄一份只是多一个泄露面。
                log.info("seed: 已按 ELDER_SEED_ADMIN_PASSWORD 创建种子管理员"
                         " admin（口令不打印）")

        # ---- 平台预设角色（人格的唯一落点）----
        # 判定条件用 kind == ''（而不是 record_type == 'preset'）：
        # 回填规则会把老库里所有历史角色行标成 preset，用后者会导致
        # "已有角色 -> 不建预设角色"，母版就永远缺失了（见架构 §7.1 的警告）。
        preset = db.scalar(
            select(models.AIProvider).where(models.AIProvider.kind == "")
            .order_by(models.AIProvider.id).limit(1))
        # ---- 品牌改名迁移：旧名（小暖）→ 小智（2026-10-20）----
        # 只动**平台预设自己那一行**。三个判据必须同时成立：
        #   record_type == preset   —— 不是历史角色（历史角色改成预设会让它
        #                              突然变成全平台可见且只读）；
        #   owner_id is None        —— 不是某个家属自建的角色；
        #   name 命中旧默认名       —— 运营改过名的不动，那是他的品牌决策。
        # 少任何一条都可能改到用户数据，而改名是不可逆的。
        if (preset is not None
                and preset.name in _LEGACY_PRESET_NAMES
                and preset.record_type == models.RECORD_TYPE_PRESET
                and preset.owner_id is None):
            log.info("seed: 平台预设角色改名「%s」→「%s」（品牌统一为小智）",
                     preset.name, catalog.PRESET_ROLE_NAME)
            preset.name = catalog.PRESET_ROLE_NAME
            db.flush()

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

        # 品牌改名迁移（形象侧，判据与角色侧一致）。注意 eaf_pack_name
        # 保持 "xiaonuan" 不动 —— 那是素材包目录名，改了会加载不到序列帧。
        if (av is not None and av.name in _LEGACY_AVATAR_NAMES
                and av.record_type == models.RECORD_TYPE_PRESET
                and av.owner_id is None):
            log.info("seed: 默认形象改名「%s」→「小智（默认）」", av.name)
            av.name = "小智（默认）"
            db.flush()

        if av is None:
            av = Avatar(
                name="小智（默认）",
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


def _admin_password_audit() -> None:
    """启动体检 + 一次性轮换：**只针对存量部署**里口令仍是默认口令的管理员。

    为什么非有这一段不可
    --------------------
    ``_seed()`` 只在**空库**时建管理员。也就是说"把种子改成随机口令"对**已经跑
    起来的库**一点用都没有 —— 存量部署（含正在运行的生产库）里那个 admin 的口令
    **现在就是 admin123**。只改进种子 = 只修将来、不修现在，所以这里必须给出
    存量手段：**体检（每次启动告警）+ 一次性轮换**。

    两段逻辑（顺序不能反）
    ----------------------
      1. 轮换：``ELDER_RESET_ADMIN_PASSWORD`` 有值 -> 把命中者换成该值；
      2. 体检：**轮换之后**再扫一遍，命中者打 WARNING 并给出可照抄的处置命令。

    ⚠️ 只对**命中者**告警
    ----------------------
    已经改过密码的管理员、以及普通用户**完全不受影响**：不告警、不被轮换、
    能照常登录。误伤一个正常账号比漏报更难被接受 —— 那会把一次安全整改变成
    一次线上事故。

    ⚠️ 幂等性（为什么不会每次 boot 都改一遍）
    ------------------------------------------
    触发条件**本身**就是「口令还是默认口令」：轮换成功后它不再成立，后续启动
    自然不动手。额外的护栏是「配置值自己就是默认口令则拒绝轮换」—— 挡住
    ``ELDER_RESET_ADMIN_PASSWORD=admin123`` 这种手滑导致的每 boot 改写（配置漂移）。
    """
    from . import models

    with SessionLocal() as db:
        admins = db.scalars(
            select(models.User).where(models.User.is_admin.is_(True))
        ).all()

        # ---- 1) 一次性轮换 ----
        if RESET_ADMIN_PASSWORD and RESET_ADMIN_PASSWORD in DEFAULT_ADMIN_PASSWORDS:
            log.warning(
                "【安全】ELDER_RESET_ADMIN_PASSWORD 的值自己就是已知默认口令，"
                "**已拒绝轮换**（否则每次启动都会改一遍口令，属于配置漂移）。"
                "请换成一个强口令后重启。")
        elif RESET_ADMIN_PASSWORD:
            rotated: list[str] = []
            for u in admins:
                # 幂等的全部秘密在这一行：口令已经不是默认值的账号**不在**轮换范围，
                # 所以轮换成功后下次启动自然不会再改（无需额外的"已处理"标记）。
                if _hit_default_password(u.password_hash) is not None:
                    u.password_hash = hash_password(RESET_ADMIN_PASSWORD)
                    rotated.append(u.username)
            if rotated:
                db.commit()
                log.warning(
                    "【安全】%s user=%s —— 已把该管理员的口令轮换为 "
                    "ELDER_RESET_ADMIN_PASSWORD 的值。\n"
                    "    ★ 用完即删：现在就把 ELDER_RESET_ADMIN_PASSWORD 从 .env / "
                    "compose 里删掉再重启一次，否则新口令会以明文长期留在配置文件里。",
                    PASSWORD_ROTATED_MARKER, "、".join(rotated))

        # ---- 2) 体检（轮换之后，已修好的不再告警）----
        for u in admins:
            hit = _hit_default_password(u.password_hash)
            if hit is None:
                continue
            log.warning(
                "【安全】%s user=%s id=%s —— 该管理员的口令仍是已知默认口令「%s」。\n"
                "    → 任何知道这个默认值的人都能以**平台管理员**身份登录：改 AI 配置"
                "（含厂商 API Key 的读写口径）、查看全部用户用量与记忆。\n"
                "    → 处置 A（登录后自己改）：带 token 调 "
                "POST /api/auth/password  {\"old_password\":\"%s\","
                "\"new_password\":\"<强口令>\"}\n"
                "    → 处置 B（一次性轮换，适合口令已泄露/无法登录）：\n"
                "        # 在 server/.env（或 compose 的 environment）里加一行\n"
                "        ELDER_RESET_ADMIN_PASSWORD='<强口令>'\n"
                "        docker compose up -d        # 日志出现 %s 即成功\n"
                "        # ★ 然后删掉这一行，再 docker compose up -d 一次\n"
                "    → 只要它还命中，本告警**每次启动都会打**。",
                DEFAULT_PASSWORD_ALERT_MARKER, u.username, u.id, hit,
                hit, PASSWORD_ROTATED_MARKER)


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


def _seed_chat_quota() -> None:
    """陪聊计量种子数据：配额配置（空闲切断秒数）与服药关键词表。

    ⚠️ 这里曾叫 ``_seed_membership``，种子的是会员的金额区间与套餐 ——
    会员制取消后那些表不再有 ORM 映射，``create_all`` 也不会给新库建，
    所以只剩计量相关的两样要seed。
    """
    from . import chat_quota as cq

    with SessionLocal() as db:
        cq.seed_defaults(db)


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
    # ⚠️ 体检必须在 _seed() **之后**：全新库的管理员是 _seed() 这一趟刚建的，
    # 放到前面会漏掉它 —— 而「ELDER_SEED_ADMIN_PASSWORD 填的就是 admin123」
    # 恰恰是最该被告警的情形（那就是旧代码的行为）。
    # 放到 _seed_migration() 之前则不成立任何依赖，两者互不相干，顺序随意。
    _admin_password_audit()
    _seed_migration()
    _seed_chat_quota()
    # 求助通道的可用性必须在**启动期**说清（端点本身不打日志：登录页每次渲染
    # 都会调它，每请求一条 WARNING 等于没有告警）。
    _warn_support_email_once()
    from . import scheduler

    task = asyncio.create_task(scheduler.run_forever())
    # MQTT+UDP 传输（F1）：ELDER_MQTT_PORT>0 才起（默认 0 = 关，设备继续
    # 走 WebSocket）。放在 lifespan 里而不是 import 期：broker 要绑端口、
    # 要事件循环，且冒烟脚本靠这个显式控制启停。
    await _voice.start_transports()
    log.info("%s v%s started", APP_NAME, APP_VERSION)
    try:
        yield
    finally:
        await _voice.stop_transports()
        task.cancel()


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan,
              description="老人陪伴 & 提醒助手业务后端（业务 API + 管理后台 + 用户端 + 自研语音链路，同源同端口）")

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

# 展示时区判定：/api/geo/timezone。**无鉴权**（登录页与首屏都要用），
# 输出只有时区名/国家码/偏移，不含用户数据。判定逻辑在 app/geo.py。
# 口径（2026-10-21 用户定调）：用户端/设备端按各自 IP 所在国家/地区显示，
# **管理端统一北京时间**（管理员在中国，统一口径才好排查与统计）。
for r in (geo_router.router,):
    app.include_router(r)

# 陪聊时长：设备端 /api/device/quota（当日统计 + 服药关键词）与 /api/device/chat-usage（上报）。
# ⚠️ 用户端 /api/membership 与管理端 /api/admin/membership 已随会员制取消删除。
for r in (chat_quota.router,):
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

# 天气（和风）：/api/weather/now、/api/weather/daily、/api/weather/status。
# 与语音工具 get_weather **共用同一个 service 与同一张缓存表** —— 老人听到的和
# 家属在 APP 上看到的必须是同一份数据，否则"网页是新的、设备是旧的"迟早发生。
# 天气是可选子系统：没配和风凭据时接口照常可用，只是返回"还没开通"。
for r in (weather_router.router,):
    app.include_router(r)

# 语音链路（自研，server/app/voice/）：WS 会话端点 + 设备 OTA 端点。
# 这是**本服务自己的**实现，不转发、不依赖任何第三方语音服务器 ——
# 固件的 CONFIG_OTA_URL 已指向本服务，OTA 响应里的 websocket:{url,token}
# 也由我们自己签发（只给已绑定设备签，未绑定只给激活码，见 voice/__init__.py）。
# attach() 自带幂等保护，冒烟脚本可提前自行调用。
_voice.attach(app)

# 声纹说话人登记（F3）：/api/voice/speakers CRUD + /api/voice/voiceprint/health。
# 与语音链路同源同端口，但**不依赖语音链路被启用** —— 登记的是"外部声纹服务
# 那边的 key ↔ 是谁"的映射，服务没配时列表照样能建（只是永远认不出人）。
for r in (voice_speakers.router,):
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
    """后台周期任务健康快照（排期补全 / 超时升级 / 离线清扫）。

    这个端点**不参与任何探活链路**，只给运维和监控看：
      - last_ok_at           最近一次完整跑完的时刻，从未成功过为 None
      - consecutive_failures 连续失败次数，成功一轮即归零
      - last_error           最近一次异常信息，成功时为 None
    """
    # 惰性导入：scheduler 依赖 db / models，模块级直接 import
    # 会把初始化顺序钉死，放在函数里更稳。
    from .scheduler import scheduler_health

    return scheduler_health()


def _resolve_support_email(db) -> str:
    """解析「对用户公开的管理员联系邮箱」：env → DB ``from_addr`` → DB ``notify_to`` → 空串。

    ⚠️ **顺序是 from_addr 在前，notify_to 只在 from_addr 为空时兜底**（2026-10-20
    在生产库上核到实际配置后订正，别写反）：

        from_addr  = zxh@250886.xyz        ← 管理员**对外身份**，用户该看到的
        notify_to  = xiaohui0302@qq.com    ← 工单通知的**内部收件箱**（个人邮箱）

    ``notify_to`` 的语义见 ``models.SmtpConfig``：它是「工单通知收件邮箱」，
    是运维内部收信口；把它渲染到**公网登录页**上，等于不登录就能拿到一个私人
    邮箱。所以：``from_addr`` 优先，``notify_to`` 只作兜底。

    为什么允许回落到 DB 里的 SMTP 配置：生产实例的 SMTP 是管理员**在管理端配的**
    （存 ``smtp_config`` 表），代码里再写死一个地址就是第二个真源 —— 而
    2026-10-20 之前那个写死的 ``support@voice.250886.xyz`` 指向一个不可达域名
    （生产是 IP 直连、没有 DNS 记录），家属点「忘记用户名 → 发邮件给管理员」时
    邮件直接进死信箱，页面却照样渲染成一个可点的 mailto，谁都不会发现信没到。

    Args:
        db: SQLAlchemy 会话（端点用 ``Depends(get_db)``，启动自检用 ``SessionLocal()``）。

    Returns:
        邮箱字符串；解析不到返回**空串**（不是任何兜底地址）。
    """
    if SUPPORT_EMAIL:
        return SUPPORT_EMAIL
    # 读取方式**复用** mailer.get_smtp_config，不另写一份查询（那是两份真源：
    # 它还会在缺行时补一条空配置，保证「没配过」与「配了但全空」同一条路径）。
    from .mailer import get_smtp_config

    try:
        cfg = get_smtp_config(db)
        return (cfg.from_addr or cfg.notify_to or "").strip()
    except Exception as exc:  # noqa: BLE001
        # 这是个**无鉴权**的公开端点：读配置失败（并发首次建行撞主键 / 库锁 /
        # 老库缺列）只能退化成空串，**不能 500** —— 500 会把登录页的
        # 「忘记用户名」整块打崩，而那正是本功能存在的意义。
        db.rollback()
        log.warning("读取 SMTP 配置失败，求助邮箱退化为空：%s: %s",
                    type(exc).__name__, exc)
        return ""


def _warn_support_email_once() -> None:
    """启动期检查一次「求助邮箱能否解析」，解析不到就 WARNING。

    为什么不在端点里打：``/api/support-contact`` 会被登录页**每次渲染**调用，
    每请求一条 WARNING 会把日志冲成噪音，真正的告警反而被淹掉。
    """
    try:
        with SessionLocal() as db:
            email = _resolve_support_email(db)
    except Exception as exc:  # noqa: BLE001  启动自检不许因为查配置失败而崩
        log.warning("求助邮箱自检失败（不影响启动）：%s: %s", type(exc).__name__, exc)
        return
    if email:
        log.info("【求助通道】/api/support-contact 将下发 %s", email)
        return
    # 单行标题 + 多行解释：运维 grep「【求助通道】」就能定位，不需要通读日志。
    log.warning(
        "【求助通道】未解析到管理员联系邮箱：ELDER_SUPPORT_EMAIL 未设置，"
        "且管理端 SMTP 的 from_addr / notify_to 都为空。\n"
        "    后果：用户点「忘记用户名 › 发邮件给管理员」时**拿不到地址**"
        "（前端对空串不渲染 mailto），而这是忘记用户名之后唯一能解绑设备的通道。\n"
        "    修法（二选一）：管理端「邮件设置」里填好发件邮箱（from_addr）"
        "或通知收件邮箱（notify_to）；或设环境变量 ELDER_SUPPORT_EMAIL=<管理员邮箱>。"
    )


@app.get("/api/support-contact")
def support_contact(db=Depends(get_db)):
    """对用户公开的管理员联系邮箱（无鉴权，只有这一个字段）。

    为什么单独开一个端点而不是塞进 ``/api/health``：health 是容器探活的
    固定契约（Dockerfile HEALTHCHECK 每 30 秒打一次，冒烟脚本也钉着它的
    形状），往里加业务字段会让「探活响应」变成一个谁都想改的杂物间。

    邮箱从哪来见 :func:`_resolve_support_email` —— **不再有写死的默认值**，
    解析不到就是空串（前端对空串不渲染 mailto）。

    响应只有 ``{"email": ...}``，不泄露任何其他配置。
    """
    return {"email": _resolve_support_email(db)}


# ---------------------------------------------------------------- 双入口页面
# 两个入口页都必须**每次回源校验**（2026-10-25）。
#
# 起因：换版后用户报「功能没修好」，排查半天才发现浏览器拿的是旧页面。
# 原先这两个 FileResponse 只带 etag / last-modified，**没有 Cache-Control**，
# 浏览器于是按启发式规则自己决定缓存多久（Chrome 取 last-modified 的 10%
# 左右）—— 部署完当天可能一直看到旧页面，而服务端日志显示一切正常。
#
# 用 no-cache 而不是 no-store：no-cache 允许缓存但**必须**回源校验，
# 页面没变时仍是 304（不重传 160KB），变了立刻拿到新的。no-store 会让
# 每次刷新都全量重传，反而更慢。
_PAGE_CACHE_HEADERS = {"Cache-Control": "no-cache, must-revalidate"}

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # 入口（2026-10-19 用户端入口重构，docs/决策记录.md D41）：
    #   /      → 用户端（未登录即登录/注册页）—— 老人与家属是主要使用者，
    #            以使用方便为准则，最短路径给最常用的界面；
    #   /admin → 管理后台（低频功能，tab 按 is_admin 过滤）。
    # /user 已**直接删除**：不 301、不做同内容兼容渲染 —— 项目未对外公开，
    # 没有存量书签/外链，多留一条路径只是多一份要维护的东西（冒烟
    # smoke_server.py 钉住 GET /user → 404，改回渲染会让它红）。
    # 用户端与管理后台是**两个入口、两套信息架构**，不是同一页面按角色
    # 隐藏几个 tab：管理端管平台（全部用户的设备/AI/形象），用户端管自己
    # （我家的设备/提醒/记忆）。
    # ⚠️ 用户端这里曾列着「会员」tab —— 它随会员制取消（e264200）一起删除了：
    # /api/membership 与 /api/admin/membership 两组端点已下线，会员相关的
    # 五张表也由 schema_patch 在启动时清掉（见 DROPPED_TABLES）。
    @app.get("/")
    def user_console():
        return FileResponse(str(STATIC_DIR / "user.html"),
                            headers=_PAGE_CACHE_HEADERS)

    @app.get("/admin")
    def console():
        return FileResponse(str(STATIC_DIR / "index.html"),
                            headers=_PAGE_CACHE_HEADERS)
else:  # pragma: no cover
    @app.get("/")
    def console_placeholder():
        return {"message": "静态页面未打包，请访问 /docs 查看接口"}
