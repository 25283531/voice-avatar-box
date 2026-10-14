"""形象 / 角色管理：EAF 序列帧资源 + 系统提示词 + 音色 + 情绪灯效。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import get_current_user
from ..schemas import AvatarIn, AvatarOut

router = APIRouter(prefix="/api/avatars", tags=["avatars"])

# 端侧约定的语义片段名，作为默认映射，便于管理页一键填充
DEFAULT_CLIPS = {
    "idle": "idle",
    "listening": "listening",
    "thinking": "thinking",
    "mouth_open": "mouth_open",
    "mouth_half": "mouth_half",
    "smile": "smile",
    "blink": "blink",
    "sleepy": "sleepy",
    "alert": "alert",
}

DEFAULT_LIGHTS = {
    "neutral": {"rgb": "#4A90D9", "effect": "breath"},
    "happy": {"rgb": "#3AD07A", "effect": "breath"},
    "sad": {"rgb": "#5B6B8C", "effect": "solid"},
    "angry": {"rgb": "#E4573D", "effect": "blink"},
    "alert": {"rgb": "#FF2020", "effect": "solid"},
    "speaking": {"rgb": "#7FD3FF", "effect": "breath"},
    "listening": {"rgb": "#FFD24A", "effect": "breath"},
}


@router.post("", response_model=AvatarOut, status_code=status.HTTP_201_CREATED)
def create_avatar(payload: AvatarIn, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    av = models.Avatar(**payload.model_dump())
    if not av.clips:
        av.clips = dict(DEFAULT_CLIPS)
    if not av.emotion_lights:
        av.emotion_lights = dict(DEFAULT_LIGHTS)
    db.add(av)
    db.commit()
    db.refresh(av)
    return av


@router.get("", response_model=list[AvatarOut])
def list_avatars(db: Session = Depends(get_db)):
    return list(db.scalars(select(models.Avatar).order_by(models.Avatar.id.desc())).all())


@router.get("/{avatar_pk}", response_model=AvatarOut)
def get_avatar(avatar_pk: int, db: Session = Depends(get_db)):
    av = db.get(models.Avatar, avatar_pk)
    if not av:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "形象不存在")
    return av


@router.patch("/{avatar_pk}", response_model=AvatarOut)
def update_avatar(avatar_pk: int, payload: AvatarIn, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    av = db.get(models.Avatar, avatar_pk)
    if not av:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "形象不存在")
    for k, v in payload.model_dump().items():
        setattr(av, k, v)
    db.commit()
    db.refresh(av)
    return av


@router.delete("/{avatar_pk}", status_code=status.HTTP_204_NO_CONTENT)
def delete_avatar(avatar_pk: int, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    av = db.get(models.Avatar, avatar_pk)
    if not av:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "形象不存在")
    db.delete(av)
    db.commit()


@router.post("/{avatar_pk}/apply/{device_pk}", status_code=status.HTTP_200_OK)
def apply_avatar(avatar_pk: int, device_pk: int, db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    """把形象应用到指定设备（设备下次心跳即生效）。"""
    from ..deps import require_device_owner

    dev = require_device_owner(device_pk, user, db)
    av = db.get(models.Avatar, avatar_pk)
    if not av:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "形象不存在")
    dev.avatar_id = av.id
    db.commit()
    return {"ok": True, "device_id": dev.device_id, "avatar_id": av.id}
