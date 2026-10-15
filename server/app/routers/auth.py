"""注册 / 登录 / 当前用户。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import get_current_user
from ..schemas import (
    EmailChangeIn,
    PasswordChangeIn,
    TokenOut,
    UserCreate,
    UserLogin,
    UserOut,
)
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
        email=payload.email,
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


@router.patch("/email", response_model=UserOut)
def change_email(payload: EmailChangeIn, db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    """更换绑定邮箱。

    - 格式非法由 Pydantic 拦下 -> 422；
    - 与他人重复 -> 409（APP 据此提示「该邮箱已被其他账号使用」）；
    - 返回体与 GET /api/auth/me 完全一致，APP 直接拿返回值刷新账户卡片。
    """
    email = payload.email
    clash = db.scalar(
        select(models.User).where(models.User.email == email, models.User.id != user.id)
    )
    if clash:
        raise HTTPException(status.HTTP_409_CONFLICT, "该邮箱已被其他账号使用")
    user.email = email
    db.commit()
    db.refresh(user)
    return user


@router.post("/password")
def change_password(payload: PasswordChangeIn, db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    """修改密码。

    这里刻意把「原密码错误」返回 400 而不是 401：APP 的 401 会被请求层
    统一解释成「登录态失效」并踢回登录页，而原密码填错只是表单填错，
    让用户重新登录反而更难受。新密码长度不足 6 位由 Pydantic 拦下 -> 422。
    """
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "原密码不正确")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"ok": True}
