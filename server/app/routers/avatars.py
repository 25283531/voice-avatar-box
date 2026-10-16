"""形象管理：EAF 序列帧资源 + 默认音色 + 情绪灯效。

⚠️ 形象已**退化为纯形象**（架构 §4.2）：人格与角色级音色搬到了 AIProvider，
这里不再暴露 system_prompt。设备侧「换外观」与「换人格」从此互不影响。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import get_current_user, require_avatar
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
    """新建一个**用户形象**（归属当前用户）。

    record_type 默认 'role' + owner_id = 当前用户：新建物体一律属于创建者。
    只有管理员通过直接改库（或未来的管理端接口）才能造 preset 形象。
    """
    av = models.Avatar(**payload.model_dump())
    if not av.clips:
        av.clips = dict(DEFAULT_CLIPS)
    if not av.emotion_lights:
        av.emotion_lights = dict(DEFAULT_LIGHTS)
    av.record_type = models.RECORD_TYPE_ROLE
    av.owner_id = user.id
    db.add(av)
    db.commit()
    db.refresh(av)
    return av


@router.get("", response_model=list[AvatarOut])
def list_avatars(db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    """形象列表：过滤已软删的；非管理员只看 preset + 自己的。

    ⚠️ 顺手补上了鉴权。现状这个接口连 ``get_current_user`` 都没有 ——
    与 providers 的 P0-2 是同一类问题，一并收掉。
    """
    q = select(models.Avatar).where(models.Avatar.deleted_at.is_(None))
    if not user.is_admin:
        q = q.where(
            (models.Avatar.record_type == models.RECORD_TYPE_PRESET)
            | (models.Avatar.owner_id == user.id)
        )
    return list(db.scalars(q.order_by(models.Avatar.id.desc())).all())


@router.get("/{avatar_pk}", response_model=AvatarOut)
def get_avatar(avatar_pk: int, db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    return require_avatar(db, avatar_pk, user)


@router.patch("/{avatar_pk}", response_model=AvatarOut)
def update_avatar(avatar_pk: int, payload: AvatarIn, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    """改形象（P0-3 修复：加归属 / is_admin 守卫）。

    修复前任何登录用户都能改平台预设形象 —— 改掉 clips 或灯效映射，
    等于让全平台所有设备的屏幕表现被人为改变。
    """
    av = require_avatar(db, avatar_pk, user, write=True)
    for k, v in payload.model_dump().items():
        setattr(av, k, v)
    # 归属信息不接受请求体覆写：改了就等于"把平台形象变成我的"
    # 或"把我的形象公开"，两者都必须走显式的归属变更路径。
    av.record_type = models.RECORD_TYPE_PRESET if av.owner_id is None else models.RECORD_TYPE_ROLE
    db.commit()
    db.refresh(av)
    return av


@router.delete("/{avatar_pk}", status_code=status.HTTP_204_NO_CONTENT)
def delete_avatar(avatar_pk: int, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    """删除形象（P0-3 修复 + 改为软删）。

    为什么改软删：``devices.avatar_id`` 是指向本表的外键，真删会让已绑定
    该形象的设备留下悬空引用，心跳下发时行为不可预期。软删后读路径统一按
    "未绑定" 处理（device_api.sync 里 avatar 取不到就下发 None）。
    行的其余信息保留，审计与回溯不断。
    """
    av = require_avatar(db, avatar_pk, user, write=True)
    av.deleted_at = models.utcnow()
    db.commit()


@router.post("/{avatar_pk}/apply/{device_pk}", status_code=status.HTTP_200_OK)
def apply_avatar(avatar_pk: int, device_pk: int, db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    """把形象应用到指定设备（设备下次心跳即生效）。"""
    from ..deps import require_device_owner

    dev = require_device_owner(device_pk, user, db)
    av = require_avatar(db, avatar_pk, user)
    if av.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "形象不存在")
    dev.avatar_id = av.id
    db.commit()
    return {"ok": True, "device_id": dev.device_id, "avatar_id": av.id}