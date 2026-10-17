"""设备在线状态判定（首页展示与后台清扫共用同一口径）。

为什么单独抽一个模块
--------------------
「设备在线吗」这个问题在两处被问到，两处的答案必须是同一个：
  * ``GET /api/home/summary`` 展示给家属看；
  * ``app/scheduler.py`` 的周期任务把久未心跳的设备 ``online`` 置回 False。
如果各写各的阈值，就会出现「首页显示离线、设备列表显示在线」这种自相矛盾的画面，
所以阈值与判定函数都放这里，两边只 import。

为什么是 5 分钟
--------------
设备心跳周期是 300 秒（见 schemas.DeviceSyncOut.sync_interval_sec）。
取 5 分钟 = 一个完整心跳周期 + 一倍余量：既能容忍一次丢包、固件略晚上报
（弱网 / 老人家里 Wi-Fi 抖动很常见），又不会把已经掉线十几分钟的设备
还显示成在线，导致家属以为一切正常。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .chat_quota import aware
from .models import Device, utcnow

# 在线判定窗口：心跳周期 300s + 一倍余量，注释见模块头部
ONLINE_WINDOW = timedelta(minutes=5)


def is_online(last_seen_at: datetime | None, now: datetime | None = None) -> bool:
    """按最近一次心跳时间判定设备是否在线。

    ``last_seen_at`` 是设备上报时按 UTC 写的，SQLite 读回来是 naive，
    因此统一先用 aware() 补成 UTC 再比较，避免 naive/aware 相减报错。
    """
    last = aware(last_seen_at)
    if not last:
        return False
    return (now or utcnow()) - last <= ONLINE_WINDOW


def mark_offline_devices(session: Session, now: datetime | None = None) -> int:
    """把「在线但已经超过一个心跳周期没上报」的设备置为离线，返回处理数量。

    为什么需要这条清扫：``devices.online`` 只在设备上报时被置 True，
    从来没有人把它置回 False（设备断电、搬走、Wi-Fi 换了都不会再上报），
    不清扫的话这个字段就是「一旦在线，永远在线」。
    """
    now = now or utcnow()
    cutoff = now - ONLINE_WINDOW
    rows = session.scalars(
        select(Device).where(
            Device.online.is_(True),
            # last_seen_at 为 NULL 说明设备从未上报过，也应视为离线
            or_(Device.last_seen_at.is_(None), Device.last_seen_at < cutoff),
        )
    ).all()
    for dev in rows:
        dev.online = False
    if rows:
        session.commit()
    return len(rows)
