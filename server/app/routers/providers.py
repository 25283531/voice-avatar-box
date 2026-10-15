"""多平台 AI API 接入配置（ASR / LLM / TTS / VAD）。

本服务只做「配置的托管与下发」，真正调用仍由 xiaozhi-esp32-server 完成；
管理页改完配置后可一键导出成小智的 config.yaml 片段。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import get_current_user
from ..schemas import ProviderIn, ProviderOut, ProviderSafeOut

router = APIRouter(prefix="/api/providers", tags=["providers"])

VALID_KINDS = ("asr", "llm", "tts", "vad")


@router.post("", response_model=ProviderOut, status_code=status.HTTP_201_CREATED)
def create_provider(payload: ProviderIn, db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    p = models.AIProvider(**payload.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@router.get("", response_model=list[ProviderOut])
def list_providers(kind: str | None = None, db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    q = select(models.AIProvider)
    if kind:
        q = q.where(models.AIProvider.kind == kind)
    return list(db.scalars(q.order_by(models.AIProvider.kind, models.AIProvider.priority)).all())


@router.get("/safe", response_model=list[ProviderSafeOut])
def list_providers_safe(db: Session = Depends(get_db)):
    """脱敏列表（不含 api_key），供设备与 APP 拉取。"""
    rows = db.scalars(
        select(models.AIProvider)
        .where(models.AIProvider.enabled.is_(True))
        .order_by(models.AIProvider.kind, models.AIProvider.priority)
    ).all()
    return list(rows)


@router.patch("/{provider_pk}", response_model=ProviderOut)
def update_provider(provider_pk: int, payload: ProviderIn, db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    p = db.get(models.AIProvider, provider_pk)
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "配置不存在")
    for k, v in payload.model_dump().items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return p


@router.delete("/{provider_pk}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider(provider_pk: int, db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    p = db.get(models.AIProvider, provider_pk)
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "配置不存在")
    db.delete(p)
    db.commit()


@router.get("/export/xiaozhi")
def export_for_xiaozhi(db: Session = Depends(get_db),
                       user: models.User = Depends(get_current_user)):
    """导出成小智 server 的 selected_module 片段，便于把配置同步过去。

    只导出真正的平台配置（kind 属于 asr/llm/tts/vad）：ai_providers 里还存着
    APP 自建的 agent 角色（kind 为空），它们没有厂商和模型，混进导出片段里
    会把真实的 LLM/ASR 配置顶掉。
    """
    out: dict[str, dict] = {}
    rows = db.scalars(
        select(models.AIProvider)
        .where(models.AIProvider.enabled.is_(True), models.AIProvider.kind.in_(VALID_KINDS))
        .order_by(models.AIProvider.kind, models.AIProvider.priority)
    ).all()
    for r in rows:
        out.setdefault(r.kind, {})
        if r.kind not in out[r.kind] or "name" not in out[r.kind]:
            out[r.kind] = {
                "name": r.provider,
                "model": r.model,
                "endpoint": r.endpoint,
                "params": r.params or {},
            }
    return {"selected_module": out}
