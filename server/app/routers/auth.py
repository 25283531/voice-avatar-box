"""注册 / 登录 / 当前用户。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import get_current_user
from ..schemas import TokenOut, UserCreate, UserLogin, UserOut
from ..security import hash_password, issue_user_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    exists = db.scalar(select(models.User).where(models.User.username == payload.username))
    if exists:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名已存在")
    # 第一个注册的用户自动成为管理员，方便私有化部署开箱即用
    is_admin = db.scalar(select(models.User).limit(1)) is None
    user = models.User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        nickname=payload.nickname or payload.username,
        phone=payload.phone,
        is_admin=is_admin,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenOut)
def login(payload: UserLogin, db: Session = Depends(get_db)):
    user = db.scalar(select(models.User).where(models.User.username == payload.username))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    token = issue_user_token(user.id, user.username, user.is_admin)
    return TokenOut(access_token=token, user_id=user.id, username=user.username,
                    is_admin=user.is_admin)


@router.get("/me", response_model=UserOut)
def me(user: models.User = Depends(get_current_user)):
    return user
