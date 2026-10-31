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
from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import TZ
from ..db import get_db
from ..deps import get_current_user
from ..device_state import is_online
from ..models import ReminderType
from ..schedule import event_instant, local_now, occurrences_on, reminder_zone
from ..schemas import (
    HomeDeviceOut,
    HomeSummaryOut,
    MedicineTodayItemOut,
    MedicineTodayOut,
)

log = logging.getLogger("elder.home")

router = APIRouter(prefix="/api/home", tags=["home"])


# ---------------------------------------------------------------- 文案映射
# 枚举值 → 中文。放在服务端而不是让前端各自硬编码：APP（uni-app）、
# 手机网页（user.html）两套 UI 都在展示同一批实例，文案写两份必然漂移，
# 而漂移的那一份会让家属看到"同一次吃药，两个页面说法不一样"。
_STATE_TEXT = {
    int(models.EventState.PENDING): "未响铃",
    int(models.EventState.FIRED): "待确认",
    int(models.EventState.CONFIRMED): "已确认",
    # 超时未确认对家属来说仍然是"还没确认"，与 FIRED 同一个动作诉求
    # （去点「代确认」），所以文案统一成「待确认」，不用「超时」吓人。
    int(models.EventState.MISSED): "待确认",
    int(models.EventState.SKIPPED): "已跳过",
}

_CONFIRM_TEXT = {
    int(models.ConfirmMethod.NONE): "—",
    int(models.ConfirmMethod.VOICE): "语音确认",
    int(models.ConfirmMethod.BUTTON): "按键确认",
    int(models.ConfirmMethod.APP): "子女代确认",
    int(models.ConfirmMethod.AUTO): "自动确认",
    int(models.ConfirmMethod.VOICE_AND_BUTTON): "语音+按键确认",
}


# ---------------------------------------------------------------- 服药统计
def _medicine_today(db: Session, device_ids: list[int],
                    device_names: dict[int, str] | None = None) -> MedicineTodayOut:
    """统计当天服药情况。

    ⚠️ 「当天」是**每条规则自己时区**的自然日（2026-10-21 跨时区改造）。
    以前统一用 ``config.TZ``，于是美西家庭 8 点的药会被算到北京那一天的
    另一个格子里 —— 首页数字对不上、家属还以为老人没吃药。
    这里刻意**不**跟随请求方的时区：服药记录属于老人那台设备，
    "今天吃了几次"不该因为看的人在北京还是纽约而变。

    数据来源是「规则实时推算 ∪ 排期实例」，见模块头部第 3 条。
    只统计 rtype=0（服药），日程 / 喝水 / 运动等不算，避免首页数字被稀释。
    """
    empty = MedicineTodayOut()
    if not device_ids:
        return empty
    # 设备名快照。为什么不在这里现查：调用方 home_summary 本来就已经把
    # Device 全查出来了，再查一遍纯属浪费；而且这里只拿到 id 列表，
    # 现查就是一次额外往返。
    names = device_names or {}

    try:
        # 1) 按规则实时推算当天应触发的服药时间点，默认都是「待服药」
        #    (reminder_id, "HH:MM") -> 这一格的明细
        #
        #    槽里存**整条明细**而不是只存 state：家属端要显示药品名与确认方式，
        #    光有 state 拼不出来。title 直接取自上面这次规则查询（Reminder 行
        #    本来就在手上），**不要**逐槽 db.get(Reminder, rid) —— 老人一天 3~5
        #    条规则、多条设备合并时那就是标准的 N+1。
        slots: dict[tuple[int, str], dict] = {}
        rule_zone: dict[int, object] = {}
        rules = db.scalars(
            select(models.Reminder).where(
                models.Reminder.device_id.in_(device_ids),
                models.Reminder.rtype == int(ReminderType.MEDICINE),
                models.Reminder.enabled.is_(True),
                # 软删除（2026-10-30）：已删除的服药规则不该再计入"今天该吃
                # 几次"。漏了它 ⇒ 家属删掉一条提醒后，首页永远显示"还有 1 次
                # 没吃"，而这个数字是家属判断老人有没有漏药的唯一依据。
                models.Reminder.deleted_at.is_(None),
            )
        ).all()
        by_id = {r.id: r for r in rules}
        for r in rules:
            zone = reminder_zone(r)
            rule_zone[r.id] = zone
            today = datetime.now(zone).date()
            for ts in occurrences_on(r, today):
                slots[(r.id, ts.strftime("%H:%M"))] = {
                    "reminder_id": r.id,
                    "title": r.title or "",
                    # 规则时区的**裸钟点**，与实例表 due_at 同口径（见下面第 2 条）
                    "due_at": ts.replace(tzinfo=None),
                    "tz": r.tz or "",
                    "state": int(models.EventState.PENDING),
                    "confirm_method": int(models.ConfirmMethod.NONE),
                    "confirmed_at": None,
                    "device_id": r.device_id,
                    "device_name": names.get(r.device_id, ""),
                }

        # 2) 用实例表覆盖真实状态。为什么按 (提醒, HH:MM) 对齐而不是按完整时间：
        #    SQLite 不保留 tzinfo，due_at 读回来是"规则时区的裸钟点"，而
        #    occurrences_on 推算出的 HH:MM 是同一个时区的钟点 —— 两边语义一致。
        #
        #    范围粗筛不能用"今天的零点"：due_at 的钟点基准逐规则不同，
        #    SQL 里没法统一。取 ±2 天（已远大于任何时区与 UTC 的差）粗筛，
        #    再用每条规则自己的"今天"精确判定。
        now_wall = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = db.scalars(
            select(models.ReminderEvent)
            .join(models.Reminder, models.Reminder.id == models.ReminderEvent.reminder_id)
            .where(
                models.ReminderEvent.device_id.in_(device_ids),
                models.Reminder.rtype == int(ReminderType.MEDICINE),
                # 软删除（2026-10-30）：与上面规则查询同口径。这里的 join
                # 原本只是为了拿 rtype，但已删除规则的实例会污染 rule_zone
                # 之外的统计口径（虽然下面有 `rid not in rule_zone` 兜着，
                # 那是"碰巧没算错"，不是"保证算对"）。
                models.Reminder.deleted_at.is_(None),
                models.ReminderEvent.due_at >= now_wall - timedelta(days=2),
                models.ReminderEvent.due_at < now_wall + timedelta(days=2),
            )
        ).all()
        for ev in rows:
            rid = ev.reminder_id
            if rid not in rule_zone:
                continue
            zone = rule_zone[rid]
            if event_instant(ev.due_at).astimezone(zone).date() != datetime.now(zone).date():
                continue
            key = (rid, ev.due_at.strftime("%H:%M"))
            slot = slots.get(key)
            if slot is None:
                # 规则的时间点被改过（实例是旧时刻），或规则今天已不再触发这一次。
                # **仍然要保留这条实例**：它代表"老人今天确实吃过这一次"，
                # 丢掉就等于把已确认的记录从页面上抹掉。
                rule = by_id.get(rid)
                slots[key] = {
                    "reminder_id": rid,
                    "title": (rule.title if rule is not None else "") or "",
                    "due_at": ev.due_at,
                    "tz": ev.tz or "",
                    "state": int(ev.state),
                    "confirm_method": int(ev.confirm_method or 0),
                    "confirmed_at": ev.confirmed_at,
                    "device_id": ev.device_id,
                    "device_name": names.get(ev.device_id, ""),
                }
                continue
            slot["due_at"] = ev.due_at
            slot["tz"] = ev.tz or slot["tz"]
            slot["state"] = int(ev.state)
            slot["confirm_method"] = int(ev.confirm_method or 0)
            slot["confirmed_at"] = ev.confirmed_at

        confirmed = sum(1 for s in slots.values()
                        if s["state"] == int(models.EventState.CONFIRMED))
        # 主动跳过（SKIPPED）不算待服药：家属明确点了「这次不吃了」
        pending_slots = sorted(
            hhmm for (_, hhmm), s in slots.items()
            if s["state"] not in (int(models.EventState.CONFIRMED), int(models.EventState.SKIPPED))
        )
        items = [
            MedicineTodayItemOut(
                reminder_id=s["reminder_id"],
                title=s["title"],
                due_at=s["due_at"],
                tz=s["tz"],
                state=s["state"],
                state_text=_STATE_TEXT.get(s["state"], ""),
                confirm_method=s["confirm_method"],
                confirm_method_text=_CONFIRM_TEXT.get(s["confirm_method"], ""),
                confirmed_at=s["confirmed_at"],
                device_id=s["device_id"],
                device_name=s["device_name"],
            )
            for s in slots.values()
        ]
        # 按**绝对时刻**排序：混时区时按裸钟点排会把纽约的 08:00 排到北京的
        # 09:00 前面。event_instant 是全仓唯一做这个换算的地方。
        items.sort(key=lambda it: event_instant(it.due_at, it.tz))
        return MedicineTodayOut(
            pending_count=len(pending_slots),
            pending_times=pending_slots,
            confirmed_count=confirmed,
            total_count=len(slots),
            items=items,
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
            # ⚠️ 必须带 UTC 偏移：这里不走 Pydantic schema（手写 isoformat），
            # 而 last_seen_at 是库里的 naive UTC。裸 isoformat 会产出无偏移的
            # "2026-10-21T14:00:00"，前端 new Date() 按浏览器本地时区解释 ⇒
            # 手机不在东八区就整体差 8 小时（时区统一前的真实症状）。
            # 用 models.iso_utc 而不是手写 as_utc(...).isoformat()：后者产出
            # "+00:00"，而 schema 序列化的同一个时刻是 "Z" —— 同一份数据里
            # 两种写法并存，前端和断言都得跟着猜。
            device_time=models.iso_utc(d.last_seen_at),
            agent_name=(agent.name if agent else None),
            avatar_name=(avatar.name if avatar else None),
        ))

    return HomeSummaryOut(
        devices=items,
        medicine_today=_medicine_today(db, [d.id for d in devices],
                                       {d.id: d.name for d in devices}),
    )
