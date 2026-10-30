"""提醒操作审计：把「谁在什么时候对哪条提醒做了什么」落成一条流水。

为什么要有这个模块（而不是在每个接口里各写一遍 ``db.add(ReminderAudit(...))``）
-----------------------------------------------------------------------------
1. **``summary`` 是给家属看的一句话**，不是调试信息。它必须与 ``action`` 一一
   对应（「暂停提醒「早上吃药」」vs「恢复提醒…」），散在六个调用点里写六遍，
   必然出现"同一个动作两种说法"——而家属看到的是两种说法，会以为是两件事；
2. **``actor_name`` 要冗余快照**：用户可能改昵称、甚至被删号，历史流水仍要能
   还原"当时是谁"。取名字的规则（用户名/邮箱里非空的第一个）只该有一份；
3. **不 commit** 这条纪律必须写在被调用的地方，而不是靠每个调用点自觉。

⚠️ 本模块**不 commit**：调用方把 ``log()`` 与业务变更放在同一个事务里提交。
   分开提交的后果是「提醒删掉了、流水丢了」（或者反过来：流水说删了、
   实际没删），而审计流水的全部价值就在于它与事实一致。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models

log = logging.getLogger("elder.reminder_audit")

#: action -> 中文标签（前端列表的「操作」列）。**唯一真源**：
#: ``summary`` 的自动生成、``ReminderAuditOut.action_label`` 都从这里取。
ACTION_LABELS: dict[str, str] = {
    "create": "新增",
    "update": "修改",
    "delete": "删除",
    "restore": "恢复",
    "toggle_on": "恢复",
    "toggle_off": "暂停",
    "confirm": "确认",
}

#: 动词（拼进 summary 用）。与 ACTION_LABELS 分开是因为中文里"操作名"和
#: "动词"不总是同一个词（「恢复」既是动作也是状态），硬合成一张表会在
#: 某一处读起来别扭。
_ACTION_VERBS: dict[str, str] = {
    "create": "新增",
    "update": "修改",
    "delete": "删除",
    "restore": "恢复",
    "toggle_on": "恢复",
    "toggle_off": "暂停",
    "confirm": "确认",
}

#: via -> 中文标签（「从哪个端操作的」）。
VIA_LABELS: dict[str, str] = {
    "web": "网页",
    "app": "手机 APP",
    "voice": "语音",
    "admin": "管理后台",
    "device": "设备",
}

#: 允许的 via 取值。``client_via`` 依赖它做白名单校验（非法值一律按 web）。
VIA_VALUES: frozenset[str] = frozenset(VIA_LABELS)


def action_label(action: str) -> str:
    """action -> 中文标签；未登记的 action 原样返回（能被发现，而不是显示空白）。"""
    key = (action or "").strip()
    return ACTION_LABELS.get(key, key)


def via_label(via: str) -> str:
    """via -> 中文标签；未登记的值原样返回。"""
    key = (via or "").strip()
    return VIA_LABELS.get(key, key)


def _display_name(actor: Any) -> str:
    """用户的显示名：用户名 / 邮箱里非空的第一个。

    刻意不用 ``nickname``：本项目的 nickname 允许为空且可随意改，而
    ``username`` 是登录名（稳定、唯一）。审计要回答"是谁"，宁可显示
    ``zhangsan`` 也不要显示一个空的昵称。
    """
    for attr in ("username", "email"):
        val = str(getattr(actor, attr, "") or "").strip()
        if val:
            return val[:64]
    return ""


#: 快照包含的字段。刻意**不含** created_at / updated_at / deleted_*：
#: 时间戳在流水行上已有（created_at），把它们塞进 before/after 只会让
#: "改了什么"淹没在噪音里。也不含 id/device_id（流水行自己有）。
_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "title", "speak_text", "rtype", "schedule_kind", "times_of_day",
    "weekdays", "month_days", "interval_days", "once_date", "start_date",
    "end_date", "dose", "require_confirm", "repeat_interval_min",
    "repeat_times", "escalate_after_min", "confirm_methods", "enabled", "tz",
)


def snapshot(reminder: models.Reminder | None) -> dict[str, Any]:
    """提醒的「业务字段」快照（写进 before_json / after_json）。"""
    if reminder is None:
        return {}
    return {f: getattr(reminder, f, None) for f in _SNAPSHOT_FIELDS}


def _dump(payload: Any) -> str:
    """dict -> JSON 字符串；None / 空 dict -> 空串。

    ``default=str``：快照里可能有 datetime / IntEnum，不能让一条流水因为
    "序列化不了"而写不进去（那会让业务变更也一起回滚，见模块头部的纪律）。
    """
    if payload is None:
        return ""
    if isinstance(payload, dict) and not payload:
        return ""
    if isinstance(payload, str):
        return payload[:4000]
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)[:4000]
    except (TypeError, ValueError) as exc:  # noqa: BLE001  兜底：宁可少存，不可丢流水
        log.warning("审计 before/after 序列化失败，按空串存：%s", exc)
        return ""


def default_summary(action: str, title: str) -> str:
    """按 action 生成一句中文话（调用方不传 summary 时用）。"""
    verb = _ACTION_VERBS.get((action or "").strip(), "操作")
    return f"{verb}提醒「{title or '（无标题）'}」"[:255]


def log(db: Session, *, reminder: models.Reminder, action: str,
        actor_type: str = "user", actor: Any = None, actor_name: str = "",
        via: str = "web", before: Any = None, after: Any = None,
        summary: str = "") -> models.ReminderAudit:
    """追加一条审计流水（**不 commit**，由调用方与业务变更同一事务提交）。

    参数
    ----
    reminder  ！必须是**本次操作后的**提醒对象：``device_id`` / ``title`` /
              ``id`` 都从它取。删除了的提醒也是它（软删除，行还在）。
    actor     传 ``models.User`` 时自动取 id 与显示名；传 None 时
              ``actor_id=None``，``actor_name`` 用入参（如「老人（语音）」）。
    before / after
              dict 时存 JSON；None 时存空串。**传快照，别传 ORM 对象** ——
              同一个 Session 里的 ORM 对象在 commit 之后会变，而流水要的是
              "当时的样子"。
    """
    name = _display_name(actor) if actor is not None else ""
    row = models.ReminderAudit(
        device_id=int(reminder.device_id),
        reminder_id=int(reminder.id),
        action=(action or "")[:16],
        actor_type=(actor_type or "user")[:16],
        actor_id=(int(getattr(actor, "id")) if actor is not None
                  and getattr(actor, "id", None) is not None else None),
        # 传了 actor 就以 actor 为准（别让调用方手填的名字覆盖真实登录名）；
        # 没传 actor（语音 / 设备）才用调用方给的名字。
        actor_name=(name or (actor_name or ""))[:64],
        via=(via or "web")[:16],
        title=(reminder.title or "")[:128],
        summary=(summary or default_summary(action, reminder.title or ""))[:255],
        before_json=_dump(before),
        after_json=_dump(after),
    )
    db.add(row)
    return row


def enabled_before_delete(db: Session, reminder_id: int) -> bool | None:
    """从**最近一条** ``delete`` 流水里还原「删除前是不是启用中」。

    为什么需要（而不是恢复时一律置 True）
    ------------------------------------
    软删除时会把 ``enabled`` 一并置 false（**第二道防线**，不是"老固件兜底"
    —— 那个说法是错的，见 ``routers/reminders.delete_reminder`` 的更正说明）。
    既然删除时改了 enabled，恢复就必须把它改回去，否则「先暂停 → 再删除 →
    恢复」会把用户明确表达过的「暂停」意图弄丢：一条本该静音的提醒恢复后
    开始每天响。

    返回 ``None`` 表示**没有依据**（没有 delete 流水 / JSON 坏了 / 字段缺失），
    调用方应按 ``True`` 处理：删除前默认是启用中的，恢复成启用态符合直觉。

    ⚠️ 排序必须 ``created_at`` + ``id`` 双键：同一秒内删两次时单键排序在
    SQLite 上结果不确定，"最近一条"会随机取到旧的那条。
    """
    raw = db.scalar(
        select(models.ReminderAudit.before_json)
        .where(models.ReminderAudit.reminder_id == int(reminder_id),
               models.ReminderAudit.action == "delete")
        .order_by(models.ReminderAudit.created_at.desc(),
                  models.ReminderAudit.id.desc())
        .limit(1)
    )
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or "enabled" not in data:
        return None
    return bool(data.get("enabled"))


def to_out(row: models.ReminderAudit, *, can_restore: bool = False) -> Any:
    """流水行 -> ``ReminderAuditOut``（补两个中文标签 + can_restore）。

    为什么在这里拼而不是让前端拼：``action_label`` / ``via_label`` 是
    「同一份映射的第二份真源」的典型入口。前端拼一遍、后端再拼一遍，
    加一个新 action 时必然有一边显示成英文 key。
    """
    from .schemas import ReminderAuditOut  # 局部导入：本模块也被 voice/tools 引用，
    # 顶层 import schemas 会把 pydantic 的模型构建提前到语音链路 import 期。

    return ReminderAuditOut(
        id=row.id,
        reminder_id=row.reminder_id,
        device_id=row.device_id,
        action=row.action,
        action_label=action_label(row.action),
        actor_type=row.actor_type,
        actor_name=row.actor_name,
        via=row.via,
        via_label=via_label(row.via),
        title=row.title,
        summary=row.summary,
        created_at=row.created_at,
        can_restore=bool(can_restore),
    )
