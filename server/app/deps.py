"""FastAPI 公共依赖：当前用户 / 当前设备鉴权。"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from . import models
from .db import get_db
from .security import decode_token

bearer = HTTPBearer(auto_error=False)


def get_current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> models.User:
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少凭证")
    payload = decode_token(cred.credentials)
    if not payload or payload.get("typ") != "user":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "凭证无效或已过期")
    user = db.get(models.User, int(payload["sub"]))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在")
    return user


def get_current_device(
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> models.Device:
    """设备侧鉴权：设备用绑定时签发的 device_token 调用。"""
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少设备凭证")
    payload = decode_token(cred.credentials)
    if not payload or payload.get("typ") != "device":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "设备凭证无效")
    dev = db.get(models.Device, int(payload["sub"]))
    if not dev:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "设备不存在")
    return dev


def get_current_admin(user: models.User = Depends(get_current_user)) -> models.User:
    """管理端接口统一入口：非管理员一律 403。"""
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return user


def require_device_owner(
    device_pk: int,
    user: models.User,
    db: Session,
) -> models.Device:
    """确认设备属于当前用户，否则 403。管理员可跨用户操作。"""
    dev = db.get(models.Device, device_pk)
    if not dev:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在")
    if not user.is_admin and dev.owner_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权操作该设备")
    return dev
