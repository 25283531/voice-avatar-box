"""声纹说话人登记（F3）。

端点一览::

    POST   /api/voice/speakers             登记一位说话人（归属当前用户）
    GET    /api/voice/speakers             列表（本人 / 管理员全部）
    GET    /api/voice/speakers/{pk}        单条
    PATCH  /api/voice/speakers/{pk}        改名字 / 介绍 / key / 停用
    DELETE /api/voice/speakers/{pk}        软删
    GET    /api/voice/voiceprint/health    外部声纹服务可达性（不吐 URL/key）

## 两个刻意的设计选择
1. **没有"注册音频"的端点**。声纹向量是在外部服务上注册的（上游原话
   "enrolled out-of-band by the service itself"），本服务连音频都不该存。
   要加族人，先去声纹服务那边录，再来这里登记 key ↔ 名字的映射。
   假装提供一个 enroll 代理端点、背后却连不上任何已标准化的接口，
   比没有端点更坏（见 DEPLOY.md §1.14 的已知缺口）。
2. **health 不返回 URL 与 key**。任何登录用户都能问"声纹现在工作吗"，
   但不该因此知道我们配了哪台机器、哪个密钥。key 一旦从这里出去，
   就等于把"冒充任意设备身份"的凭据发给了每个注册用户。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config, models
from ..db import get_db
from ..deps import get_current_user, require_voice_speaker
from ..schemas import VoiceSpeakerIn, VoiceSpeakerOut
from ..voice import voiceprint

router = APIRouter(prefix="/api/voice", tags=["voice"])


def _duplicate_alive(db: Session, owner_id: int, speaker_key: str,
                     exclude_pk: int | None = None) -> bool:
    """同 owner 下是否已有**未删**行占着这个 key。

    只数未删行是刻意的：删掉再重建同一个 key（重新在声纹服务录过音）
    必须能成功，否则"删了想加回来"会被 409 挡住，而删行的人正是想重置它。
    代价是没有数据库层唯一约束，并发双建可能都穿过这道检查 —— 可接受：
    后果是同 key 两行，识别时任一行命中都映射到同一个名字，且 409 重试
    是这套系统里既有的模式（激活码撞码同款处理）。
    """
    q = select(models.VoiceSpeaker.id).where(
        models.VoiceSpeaker.owner_id == owner_id,
        models.VoiceSpeaker.speaker_key == speaker_key,
        models.VoiceSpeaker.deleted_at.is_(None))
    if exclude_pk is not None:
        q = q.where(models.VoiceSpeaker.id != exclude_pk)
    return db.scalar(q) is not None


@router.post("/speakers", response_model=VoiceSpeakerOut,
             status_code=status.HTTP_201_CREATED)
def create_speaker(payload: VoiceSpeakerIn, db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    """登记一位说话人。归属当前用户（这台设备的主人）。

    家属自己在 APP 里就能加；管理员能看见全部但**不能替别人建** ——
    "谁的家人"只有谁自己说了算。
    """
    if _duplicate_alive(db, user.id, payload.speaker_key):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "这个 speaker_key 已经登记过了")
    row = models.VoiceSpeaker(**payload.model_dump(), owner_id=user.id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/speakers", response_model=list[VoiceSpeakerOut])
def list_speakers(db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    """说话人列表：过滤已软删的；非管理员只看自己的。"""
    q = select(models.VoiceSpeaker).where(
        models.VoiceSpeaker.deleted_at.is_(None))
    if not user.is_admin:
        q = q.where(models.VoiceSpeaker.owner_id == user.id)
    return list(db.scalars(q.order_by(models.VoiceSpeaker.id.desc())).all())


@router.get("/voiceprint/health")
def voiceprint_health(db: Session = Depends(get_db),
                      user: models.User = Depends(get_current_user)):
    """外部声纹服务的可达性 + 本人登记数。

    ``enabled`` = 服务端配了 URL 且阈值像样；``healthy`` = 刚探过活
    （带 TTL 缓存，见 voiceprint.provider().health）。两者都 False 时
    识别不会发生，但陪聊一切正常 —— 这是设计目标，不是降级事故。
    """
    p = voiceprint.provider()
    q = select(models.VoiceSpeaker.id).where(
        models.VoiceSpeaker.deleted_at.is_(None))
    if not user.is_admin:
        q = q.where(models.VoiceSpeaker.owner_id == user.id)
    count = len(list(db.scalars(q).all()))
    return {
        "enabled": p is not None,
        "healthy": bool(p.health()) if p is not None else False,
        "threshold": config.VOICEPRINT_THRESHOLD,
        "timeout_sec": config.VOICEPRINT_TIMEOUT_SEC,
        "speakers": count,
    }


@router.get("/speakers/{speaker_pk}", response_model=VoiceSpeakerOut)
def get_speaker(speaker_pk: int, db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    return require_voice_speaker(db, speaker_pk, user)


@router.patch("/speakers/{speaker_pk}", response_model=VoiceSpeakerOut)
def update_speaker(speaker_pk: int, payload: VoiceSpeakerIn,
                   db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    """改说话人。speaker_key 也允许改（去声纹服务重新录过之后换 key）。

    归属不接受请求体覆写 —— 与 avatars 同一条理由：能改归属就等于
    "把我的说话人送给你"或"把别人的说话人抢过来"，两者都必须走显式路径。
    """
    row = require_voice_speaker(db, speaker_pk, user, write=True)
    if (payload.speaker_key != row.speaker_key
            and _duplicate_alive(db, row.owner_id, payload.speaker_key,
                                 exclude_pk=row.id)):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "这个 speaker_key 已经登记过了")
    for k, v in payload.model_dump().items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/speakers/{speaker_pk}",
               status_code=status.HTTP_204_NO_CONTENT)
def delete_speaker(speaker_pk: int, db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    """软删：行留着（审计 + 同 key 可重建），不再参与识别。"""
    row = require_voice_speaker(db, speaker_pk, user, write=True)
    row.deleted_at = models.utcnow()
    db.commit()
