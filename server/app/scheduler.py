"""后台定时任务：排期补全 + 超时升级 + 离线清扫。

- 排期补全：为所有设备预生成未来 N 小时的提醒实例；
- 超时升级：到点后超过 escalate_after_min 仍未确认 -> 标记 MISSED 并置 escalated，
  设备下次心跳会看到该状态，从而保持红灯常亮（需求 11）；
- 离线清扫：超过在线窗口（5 分钟，见 device_state.ONLINE_WINDOW）没有心跳的设备置 offline，
  否则 devices.online 只增不减，设备断电后仍会显示在线。

⚠️ 这里曾有两步「会员订单过期」与「会员到期标记」，随会员制取消一并删除 ——
它们服务的对象（membership_orders / memberships）已无 ORM 映射。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import SCHEDULER_INTERVAL_SEC, SCHEDULE_AHEAD_HOURS, TZ
from .db import SessionLocal
from .device_state import mark_offline_devices
from .models import Device, EventState, Reminder, ReminderEvent
from .schedule import event_due_utc, local_now, materialize_events

log = logging.getLogger("elder.scheduler")

# 周期任务的健康快照，供 GET /api/health/scheduler 读取。
#
# 为什么单独开一个端点而不是塞进 /api/health：
#   Dockerfile 的 HEALTHCHECK 每 30 秒打一次 /api/health，它一旦从 200 变成非 200，
#   容器就会被判定 unhealthy —— 可此时进程还在、8064 上的业务 API 也照样能用，
#   只是后台的排期补全 / 离线清扫之类没跑成。为了后台任务把整个容器判死，连带代价太大。
#   所以「探活」和「后台任务是否健康」必须分开：/api/health 只表达进程活着，
#   运维要看后台任务就查这个专门的端点（它不进任何探活链路）。
_SCHEDULER_HEALTH: dict[str, object] = {
    "last_ok_at": None,       # 最近一次完整跑完的时刻（带时区 ISO 字符串），从未成功过为 None
    "consecutive_failures": 0,  # 连续失败次数，成功一轮即归零
    "last_error": None,       # 最近一次异常的字符串，成功时为 None
}


def scheduler_health() -> dict[str, object]:
    """返回周期任务健康快照的副本（只读，避免调用方改到内部状态）。"""
    return dict(_SCHEDULER_HEALTH)


# ================================================================ 天气预热
#: 预热的独立节奏（秒）。**不跟随 SCHEDULER_INTERVAL_SEC**：
#: 后者是给「排期补全/超时升级」用的，通常 60 秒一轮；天气缓存 TTL 是 60 分钟，
#: 每 60 秒去扫一遍候选城市纯属空转（而且日志会变成噪音）。10 分钟一轮
#: 相对 60 分钟 TTL 有 6 次机会，足够覆盖一次上游抖动。
WARM_INTERVAL_SEC = 600

#: 上次预热尝试的时刻（wall clock）。模块级状态：scheduler 是单进程单循环。
_last_warm_at: float = 0.0


def _warm_weather(session: Session, *, force: bool = False) -> None:
    """按需预热天气缓存。绝不遍历全国，只续「最近被问过且快过期」的城市。

    ⚠️ ``import`` 放函数内：天气是可选子系统，它的依赖（PyJWT/cryptography）
    不该成为**服务启动**的必要条件。放在这里，最坏情况是预热这一步报错，
    提醒排期/超时升级照常跑（``run_forever`` 对每轮异常都有兜底）。
    """
    global _last_warm_at
    now_ts = time.time()
    if not force and now_ts - _last_warm_at < WARM_INTERVAL_SEC:
        return
    _last_warm_at = now_ts

    try:
        from .weather import load_config
        from .weather import service as weather_service

        cfg = load_config()
        if not cfg.configured:
            # 未配置是常态（很多人先只用提醒功能），打日志会每 10 分钟刷一条
            return
        weather_service.warm_recent(session, cfg=cfg)
    except Exception as exc:  # noqa: BLE001  预热失败绝不能拖垮 scheduler
        log.warning("天气预热出错（不影响提醒）：%s: %s", type(exc).__name__, exc)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return

    # 顺带清理很久没人问的缓存行（同一节奏，一天最多删几十行）
    try:
        from .weather import cache as weather_cache

        removed = weather_cache.purge_stale(session)
        if removed:
            log.info("天气缓存清理：删除 %d 条长期未使用的记录", removed)
    except Exception as exc:  # noqa: BLE001
        log.debug("天气缓存清理失败（忽略）：%s", exc)


def _tick(session: Session) -> None:
    now = local_now()

    # 1) 排期补全
    devices = session.scalars(select(Device.id)).all()
    for dev_id in devices:
        try:
            materialize_events(session, dev_id, hours=SCHEDULE_AHEAD_HOURS)
        except Exception as exc:  # noqa: BLE001
            log.warning("materialize failed dev=%s: %s", dev_id, exc)
            session.rollback()

    # 2) 超时升级
    overdue = session.scalars(
        select(ReminderEvent).where(
            ReminderEvent.state.in_([int(EventState.PENDING), int(EventState.FIRED)])
        )
    ).all()
    for ev in overdue:
        rule = session.get(Reminder, ev.reminder_id)
        # 软删除（2026-10-30）：已删除的规则**不许**再升级。
        # 删除时只清了 PENDING 实例，已经 FIRED 的那条还留着（那是"那天响过"
        # 的历史事实）；但它的升级链（红灯常亮 + 通知子女）必须停 —— 否则
        # 家属删掉提醒后还会收到"老人没吃药"的告警，去问老人却发现提醒早就
        # 删了。`is_alive()` 是 models.Reminder 上与 Avatar 同风格的判据。
        if not rule or not rule.is_alive() or not rule.require_confirm:
            continue
        # due_at 存的是**规则所在时区**的裸钟点（跨时区之后不再是"服务端钟点"），
        # 直接和 now（aware）比较会抛 TypeError，而且就算补了 TZ 也只有北京规则
        # 是对的。统一走 schedule.event_due_utc() 还原成绝对时刻。
        # ⚠️ 异常若是顺着冒到 run_forever，会把这一轮后面的所有步骤
        # （含离线清扫）一起带走，所以这里也刻意不做 try 之外的花活。
        due = event_due_utc(ev)
        deadline = due + timedelta(minutes=max(1, rule.escalate_after_min))
        if now > deadline:
            ev.state = int(EventState.MISSED)
            ev.escalated = True
            ev.note = ev.note or "超时未确认"
    session.commit()

    # 3) 离线清扫：devices.online 只在设备上报时被置 True，
    #    设备断电 / 换 Wi-Fi 之后不会再上报，不清扫就会「永远在线」。
    #    阈值与首页展示共用 device_state.ONLINE_WINDOW（5 分钟，一个心跳周期 + 余量）。
    try:
        n = mark_offline_devices(session)
        if n:
            log.info("marked %d device(s) offline", n)
    except Exception as exc:  # noqa: BLE001
        log.warning("mark offline failed: %s", exc)
        session.rollback()

    # 4) 天气按需预热（内部自带 10 分钟节流；未配置天气时直接跳过）
    _warm_weather(session)


async def run_forever() -> None:
    """每 SCHEDULER_INTERVAL_SEC 秒跑一轮，异常不中断。"""
    log.info("scheduler started, interval=%ss", SCHEDULER_INTERVAL_SEC)
    while True:
        await asyncio.sleep(SCHEDULER_INTERVAL_SEC)
        try:
            with SessionLocal() as s:
                await asyncio.to_thread(_tick, s)
        except Exception as exc:  # noqa: BLE001
            log.exception("scheduler tick failed: %s", exc)
            # 记账本身绝不能再抛异常，否则会把 run_forever 整个带走
            try:
                _SCHEDULER_HEALTH["consecutive_failures"] = \
                    int(_SCHEDULER_HEALTH["consecutive_failures"]) + 1
                _SCHEDULER_HEALTH["last_error"] = str(exc)
            except Exception:  # noqa: BLE001
                pass
            continue
        # 成功一轮：刷新成功时刻并把连续失败次数清零
        try:
            _SCHEDULER_HEALTH["last_ok_at"] = local_now().isoformat()
            _SCHEDULER_HEALTH["consecutive_failures"] = 0
            _SCHEDULER_HEALTH["last_error"] = None
        except Exception:  # noqa: BLE001
            pass
