"""陪聊时长上报与设备端配额查询（取消会员制后保留的唯一一组端点）。

这个文件曾经是 ``routers/membership.py``，对外暴露三组路由：

* 用户端 ``/api/membership``（会员状态/套餐/下单/我的订单/收款信息）—— **已删除**；
* 管理端 ``/api/admin/membership``（订单核销/配置/授权）—— **已删除**；
* 设备端 ``/api/device``（当日配额、时长上报）—— **保留**，即本文件现在全部内容。

⚠️ **为什么设备端这两条不能跟着一起删**
------------------------------------
它们不是会员功能，是**计量功能**：

* ``POST /api/device/chat-usage`` 是 ``chat_usage`` 表的写入入口 ——
  用户与运营都要看「今天聊了多久」，删了它用量统计就断源；
* ``GET /api/device/quota`` 让设备拿到 ``idle_timeout_seconds`` 与
  ``reminder_keywords`` —— 后者是固件本地识别服药提醒、避免无谓唤醒 LLM 的依据，
  属于提醒链路，删了会连累服药提醒。

这两条的路径与出参**一个字都没改**，所以老固件无需重新烧录即可继续工作。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import models
from .. import chat_quota as svc
from ..chat_quota import local_date_str, validate_date_str
from ..db import get_db
from ..deps import get_current_device
from ..models import utcnow
from ..schemas import ChatUsageIn, ChatUsageOut, DeviceQuotaOut

# 与 device_api.py 同前缀但独立成组，避免动到 device_api.py 的既有逻辑。
router = APIRouter(prefix="/api/device", tags=["device"])


# ---------------------------------------------------------------- 设备端接口
@router.get("/quota", response_model=DeviceQuotaOut)
def device_quota(db: Session = Depends(get_db),
                 dev: models.Device = Depends(get_current_device)):
    """当日陪聊统计：已用秒数 / 空闲切断秒数 / 服药关键词表。

    ⚠️ 取消会员制后这里**不再有上限**：`unlimited` 恒为 ``True``、
    ``limit_seconds`` 恒为 ``None``（设备应优先判断 ``unlimited``）。
    ``used_seconds`` 仍然照常返回 —— 它是展示用的统计，不是闸门。
    """
    snap = svc.quota_snapshot(db, device_ids=[dev.id],
                             authorized=dev.owner_id is not None)
    return DeviceQuotaOut(**snap)


@router.post("/chat-usage", response_model=ChatUsageOut)
def report_chat_usage(payload: ChatUsageIn, db: Session = Depends(get_db),
                      dev: models.Device = Depends(get_current_device)):
    """上报一次会话时长。``is_reminder=true`` 表示服药/提醒类交互，单独累计。"""
    date = validate_date_str(payload.date) if payload.date else local_date_str()
    if not date:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "日期格式应为 YYYY-MM-DD")

    row = svc.record_chat_usage(db, dev.id, date, payload.seconds, payload.is_reminder)
    snap = svc.quota_snapshot(db, device_ids=[dev.id], date=date,
                              authorized=dev.owner_id is not None)
    remaining = None if snap["unlimited"] else max(
        0, int(snap["limit_seconds"] or 0) - int(snap["used_seconds"])
    )
    dev.last_seen_at = utcnow()
    dev.online = True
    db.commit()
    return ChatUsageOut(
        date=row.date,
        used_seconds=int(row.used_seconds or 0),
        session_count=int(row.session_count or 0),
        reminder_seconds=int(row.reminder_seconds or 0),
        limit_seconds=snap["limit_seconds"],
        unlimited=snap["unlimited"],
        remaining_seconds=remaining,
    )
