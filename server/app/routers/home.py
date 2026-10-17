"""首页聚合接口。

APP 首页要展示两块信息：绑定的设备及其状态、当天服药情况。
如果让 APP 分别调「设备列表 + 提醒列表 + 回执列表」三个接口再自己拼，
一次进首页就是三次往返，移动端弱网下体验很差，而且「当天待服药」这类逻辑
放在端侧还要重复实现一遍时区与重复规则。所以这里在服务端一次算完。

⚠️ 这里曾聚合第三块「会员状态」，随会员制取消删除（见 HomeSummaryOut 的注释）。

设计取舍
--------
1. **设备时间**：固件没有把本地 RTC 时间回传上来（心跳只带固件版本与 IP），
   服务端也拿不到设备的实时时钟，因此用设备最近一次心跳时间 last_seen_at
   作为「设备上的时间」返回，语义是「最后一次看到这台设备的时间」。
2. **在线判定**：心跳周期是 300 秒（见 schemas.DeviceSyncOut.sync_interval_sec），
   在线窗口取 5 分钟 = 一个心跳周期 + 一倍余量，既能容忍一次丢包或固件略晚上报，
   又不会把已经掉线十几分钟的设备还显示成在线。注意这里以心跳时间为准重新计算，
   而不是直接读 devices.online 字段 —— 那个字段只在设备上报时被置 True，
   从来没有人把它置回 False，直接读会得到「永远在线」。
3. **服药统计**：服务端只预生成未来 36 小时的排期实例（SCHEDULE_AHEAD_HOURS），
   当天早些时候的服药点很可能压根没有落库，所以**先按规则实时推算**当天应触发的
   时间点，再用实例表里的真实状态去覆盖，二者取并集，避免只查实例表然后返回 0。
4. **容错**：服药统计失败时降级为 0，设备列表照常返回 ——
   首页是老人家属打开 APP 的第一屏，统计模块出故障不应该让整页白屏。
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import TZ
from ..db import get_db
from ..deps import get_current_user
from ..device_state import is_online
from ..models import ReminderType
from ..schedule import local_now, occurrences_on
from ..schemas import (
    HomeDeviceOut,
    HomeSummaryOut,
    MedicineTodayOut,
)

log = logging.getLogger("elder.home")

router = APIRouter(prefix="/api/home", tags=["home"])


# ---------------------------------------------------------------- 服药统计
def _medicine_today(db: Session, device_ids: list[int]) -> MedicineTodayOut:
    """统计当天（服务端时区 TZ 的自然日）服药情况。

    数据来源是「规则实时推算 ∪ 排期实例」，见模块头部第 3 条。
    只统计 rtype=0（服药），日程 / 喝水 / 运动等不算，避免首页数字被稀释。
    """
    empty = MedicineTodayOut()
    if not device_ids:
        return empty

    try:
        today = local_now().date()
        day_start = datetime.combine(today, time(0, 0), tzinfo=TZ)
        day_end = day_start + timedelta(days=1)

        # 1) 按规则实时推算当天应触发的服药时间点，默认都是「待服药」
        #    （reminder_id, "HH:MM"）-> 状态
        slots: dict[tuple[int, str], int] = {}
        rules = db.scalars(
            select(models.Reminder).where(
                models.Reminder.device_id.in_(device_ids),
                models.Reminder.rtype == int(ReminderType.MEDICINE),
                models.Reminder.enabled.is_(True),
            )
        ).all()
        for r in rules:
            for ts in occurrences_on(r, today):
                slots[(r.id, ts.strftime("%H:%M"))] = int(models.EventState.PENDING)

        # 2) 用实例表覆盖真实状态。为什么按 (提醒, HH:MM) 对齐而不是按完整时间：
        #    SQLite 存的是不带时区的墙上时间（见 db 层的 DateTime(timezone=True) 行为），
        #    而规则推算出的是带 TZ 的时间，直接比对象会踩时区坑；HH:MM 两边语义一致。
        rows = db.scalars(
            select(models.ReminderEvent)
            .join(models.Reminder, models.Reminder.id == models.ReminderEvent.reminder_id)
            .where(
                models.ReminderEvent.device_id.in_(device_ids),
                models.Reminder.rtype == int(ReminderType.MEDICINE),
                models.ReminderEvent.due_at >= day_start,
                models.ReminderEvent.due_at < day_end,
            )
        ).all()
        for ev in rows:
            slots[(ev.reminder_id, ev.due_at.strftime("%H:%M"))] = int(ev.state)

        confirmed = sum(1 for s in slots.values() if s == int(models.EventState.CONFIRMED))
        # 主动跳过（SKIPPED）不算待服药：家属明确点了「这次不吃了」
        pending_slots = sorted(
            hhmm for (_, hhmm), s in slots.items()
            if s not in (int(models.EventState.CONFIRMED), int(models.EventState.SKIPPED))
        )
        return MedicineTodayOut(
            pending_count=len(pending_slots),
            pending_times=pending_slots,
            confirmed_count=confirmed,
            total_count=len(slots),
        )
    except Exception as exc:  # noqa: BLE001  统计失败不应拖垮整个首页
        log.warning("服药统计失败，降级为 0: %s", exc)
        return empty


# ---------------------------------------------------------------- 接口
@router.get("/summary", response_model=HomeSummaryOut)
def home_summary(db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    """首页聚合：设备状态 + 当天服药，一次返回。"""
    devices = list(db.scalars(
        select(models.Device)
        .where(models.Device.owner_id == user.id)
        .order_by(models.Device.id.desc())
    ).all())

    items: list[HomeDeviceOut] = []
    for d in devices:
        # 在线判定与后台清扫共用 device_state.is_online，口径一致
        online = is_online(d.last_seen_at)
        avatar = db.get(models.Avatar, d.avatar_id) if d.avatar_id else None
        agent = db.get(models.AIProvider, d.agent_id) if d.agent_id else None
        items.append(HomeDeviceOut(
            id=d.id,
            name=d.name,
            online=online,
            last_seen_at=d.last_seen_at,
            ip=d.ip or "",
            fw_version=d.fw_version or "",
            device_time=(d.last_seen_at.isoformat() if d.last_seen_at else None),
            agent_name=(agent.name if agent else None),
            avatar_name=(avatar.name if avatar else None),
        ))

    return HomeSummaryOut(
        devices=items,
        medicine_today=_medicine_today(db, [d.id for d in devices]),
    )
