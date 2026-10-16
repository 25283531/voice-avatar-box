"""设备管理 + 激活码绑定。"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import get_current_user, require_device_owner, require_role
from ..schemas import (
    ActivationCodeOut,
    BindIn,
    BindOut,
    DeviceAgentIn,
    DeviceAgentOut,
    DeviceCreate,
    DeviceOut,
    DeviceUpdate,
    EventOut,
)
from ..security import (
    code_expires_at,
    format_code,
    generate_activation_code,
    issue_device_token,
    normalize_code,
    verify_activation_code,
)

router = APIRouter(prefix="/api/devices", tags=["devices"])


@router.post("", response_model=DeviceOut, status_code=status.HTTP_201_CREATED)
def create_device(payload: DeviceCreate, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    """登记一台设备（通常由 APP 在扫码后调用，设备标识取 eFuse MAC）。"""
    exists = db.scalar(select(models.Device).where(models.Device.device_id == payload.device_id))
    if exists:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该设备已登记")
    dev = models.Device(device_id=payload.device_id, name=payload.name, owner_id=user.id)
    db.add(dev)
    db.commit()
    db.refresh(dev)
    return dev


@router.get("", response_model=list[DeviceOut])
def list_devices(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    q = select(models.Device)
    if not user.is_admin:
        q = q.where(models.Device.owner_id == user.id)
    return list(db.scalars(q.order_by(models.Device.id.desc())).all())


@router.get("/{device_pk}", response_model=DeviceOut)
def get_device(device_pk: int, db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    return require_device_owner(device_pk, user, db)


@router.patch("/{device_pk}", response_model=DeviceOut)
def update_device(device_pk: int, payload: DeviceUpdate, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    dev = require_device_owner(device_pk, user, db)
    if payload.name is not None:
        dev.name = payload.name
    if payload.avatar_id is not None:
        dev.avatar_id = payload.avatar_id
    if payload.settings is not None:
        dev.settings = {**(dev.settings or {}), **payload.settings}
    db.commit()
    db.refresh(dev)
    return dev


@router.delete("/{device_pk}", status_code=status.HTTP_204_NO_CONTENT)
def delete_device(device_pk: int, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    dev = require_device_owner(device_pk, user, db)
    db.delete(dev)
    db.commit()


@router.post("/{device_pk}/agent", response_model=DeviceAgentOut)
def bind_agent(device_pk: int, payload: DeviceAgentIn, db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    """给设备绑定一个 agent 角色（一台设备同时只生效一个）。

    越权处理与 require_device_owner 略有不同：别人的设备这里统一返回 404
    而不是 403 —— 403 等于告诉调用方「这台设备存在，只是不是你的」，
    会把设备主键是否存在这一信息泄露出去。角色同理（deps.require_role）。

    ⚠️ **P0-4 修复**：原先这里只做 ``db.get(AIProvider, role_id)`` 存在性校验，
    于是**任何平台 LLM 配置都能被绑成角色**（kind 判空根本没查）。
    后果是 build_persona_prompt 拿到一条空 system_prompt —— 设备"绑上了角色"
    却按没有人格的方式说话，而且不报错，只是静默劣化。
    现在统一走 deps.require_role：必须是角色行、且对当前用户可见。

    TODO: 目前只把 agent_id 落库。设备心跳（/api/device/sync）还没有把角色
    下发给固件，等端侧支持「按角色切换人格 / 音色」时，在 DeviceSyncOut 里
    加一个 agent 字段、复用这里取角色的逻辑即可，不必改本接口。
    """
    dev = db.get(models.Device, device_pk)
    if dev is None or (not user.is_admin and dev.owner_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在")

    # 必须是**角色**且当前用户可见：平台配置在这里表现为"不存在"
    role = require_role(db, payload.role_id, user)
    if role.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "角色不存在")

    dev.agent_id = role.id
    db.commit()
    return DeviceAgentOut(device_pk=dev.id, role_id=role.id, role_name=role.name)


# ============================================================ 激活码
@router.post("/{device_pk}/code", response_model=ActivationCodeOut)
def issue_code(device_pk: int, db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    """为该设备生成一枚 10 分钟有效的激活码，屏显后由 APP 输入完成绑定。"""
    dev = require_device_owner(device_pk, user, db)
    for _ in range(8):
        code = generate_activation_code()
        clash = db.scalar(select(models.ActivationCode).where(models.ActivationCode.code == code))
        if not clash:
            break
    else:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "激活码生成失败，请重试")

    row = models.ActivationCode(
        code=code,
        device_id=dev.device_id,
        expires_at=code_expires_at(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return ActivationCodeOut(code=code, display=format_code(code), expires_at=row.expires_at,
                             device_id=dev.device_id)


@router.post("/bind", response_model=BindOut)
def bind_device(payload: BindIn, db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    """APP 侧：输入激活码绑定设备，返回设备 token。

    激活码采用 1 位字母 + 12 位数字（末位 Luhn 校验位），
    输入时允许带空格或短横线，字母不区分大小写。
    """
    code = normalize_code(payload.code)
    if len(code) != 13:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "激活码应为 13 位（1 字母 + 12 数字）")
    if not verify_activation_code(code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "激活码校验失败，请检查是否输错")

    row = db.scalar(select(models.ActivationCode).where(models.ActivationCode.code == code))
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "激活码不存在")
    if row.used:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "激活码已使用")
    if row.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "激活码已过期，请在设备上重新获取")

    dev = db.scalar(select(models.Device).where(models.Device.device_id == (row.device_id or payload.device_id or "")))
    if not dev:
        # 通用码：设备尚未登记，此处按 APP 上报的 device_id 现场建档
        if not payload.device_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "缺少设备标识")
        dev = models.Device(device_id=payload.device_id, name=payload.alias or "我的陪伴助手")
        db.add(dev)
        db.flush()

    dev.owner_id = user.id
    if payload.alias:
        dev.name = payload.alias
    row.used = True
    row.used_by = user.id
    db.commit()
    db.refresh(dev)

    return BindOut(device_id=dev.device_id, device_pk=dev.id,
                   device_token=issue_device_token(dev.id, dev.device_id), name=dev.name)


@router.get("/{device_pk}/events", response_model=list[EventOut])
def device_events(device_pk: int, limit: int = 100, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    """查看该设备的提醒回执（是否按时确认）。"""
    require_device_owner(device_pk, user, db)
    rows = db.scalars(
        select(models.ReminderEvent)
        .where(models.ReminderEvent.device_id == device_pk)
        .order_by(models.ReminderEvent.due_at.desc())
        .limit(limit)
    ).all()
    return list(rows)
