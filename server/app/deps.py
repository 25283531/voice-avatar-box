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
    # 代次校验：管理员解绑时 `token_epoch += 1`，此处旧 token 立刻失效。
    # 没有这一行，解绑只是「清了归属」，原家属手上那枚 token 仍能读**新家庭**
    # 的数据（提醒、服药回执），等于解绑没生效（见 models.Device.token_epoch）。
    if int(payload.get("ep", 0)) != int(getattr(dev, "token_epoch", 0) or 0):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "设备凭证已失效，请重新绑定")
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


# ================================================================ 角色 / 配置
# 为什么越权一律 404 而不是 403
# -----------------------------
# 403 等于告诉调用方「这个主键**存在**，只是不属于你」。只要攻击者能区分
# 404 与 403，就能用遍历 id 的方式把整张表的主键存在性、以及"哪条平台配置
# 是哪条"全套出来 —— 这本身就是信息泄露。devices.bind_agent 与 avatars 早就
# 是这么处理的，这里保持一致。
#
# 唯一例外是**预设角色的 DELETE 返回 403**：那条规则本身就是要给用户看的
# 产品规则（"平台预设不可删，请联系管理员下架"），不是在保护什么秘密。

def require_role(
    db: Session,
    role_pk: int,
    user: models.User,
    *,
    write: bool = False,
    allow_archived: bool = False,
) -> models.AIProvider:
    """取一个当前用户可访问的角色行，否则 404。

    这是**角色接口唯一的访问守卫**（/api/roles 全套、/api/devices/{pk}/agent
    都走它）。判定逻辑刻意全部委托给 ``AIProvider`` 上的方法
    （is_role / is_visible_to / can_write），不在这里重写一遍条件 ——
    条件散落在多处必然漂移，漏一处就是一个越权洞。

    :param write: True = 需要写权限（owner 或管理员）。
    :param allow_archived: 归档的预设是否仍算存在。默认 False，因为
        下架的预设不该再被派生或绑定。
    """
    row = db.get(models.AIProvider, role_pk)
    # ⚠️ 判 is_role() 而不是「行存在即可」：一条 record_type='config' 的平台
    # LLM 配置在这里必须表现为"不存在"，否则就能把平台配置绑成角色，
    # 而 build_persona_prompt 拿到的是空 system_prompt（P0-4）。
    if row is None or not row.is_role():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "角色不存在")
    if not allow_archived and row.archived_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "角色不存在")
    # 软删的行对普通路径等同于不存在；但 owner / 管理员仍可读，
    # 否则「我删掉的角色」这条审计线索就断了（INV-4 要验证画像仍可读）。
    if row.deleted_at is not None and not user.is_admin and row.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "角色不存在")
    if write:
        if not row.can_write(user):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "角色不存在")
    elif not row.is_visible_to(user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "角色不存在")
    return row


def require_config(
    db: Session,
    provider_pk: int,
    user: models.User,
) -> models.AIProvider:
    """取一条**平台配置**，否则 404。仅管理员可用（P0-1/P0-2 的根治点）。

    三重校验，缺一不可：
      1. is_admin —— 平台配置含 api_key / endpoint，只有管理员能碰；
      2. 必须是 record_type == 'config' —— 否则角色行会被 PATCH 成配置、
         或配置行被 DELETE 掉（后者会让设备直接失去对话能力）；
      3. 行必须存在。

    顺序刻意是「先鉴权再取行」：非管理员**无论 id 是否存在**都得到 403，
    这样连"这个 id 存在吗"都探测不到。若反过来先取行，非管理员就能通过
    403 与 404 的差异把 ai_providers 的主键空间摸一遍。
    """
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    row = db.get(models.AIProvider, provider_pk)
    if row is None or not row.is_config():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "配置不存在")
    return row


def require_avatar(
    db: Session,
    avatar_pk: int,
    user: models.User,
    *,
    write: bool = False,
) -> models.Avatar:
    """取一个当前用户可访问的形象行，否则 404（P0-3 的守卫）。

    与 require_role 同构：判定全交给 Avatar 上的 is_visible_to / can_write。
    """
    row = db.get(models.Avatar, avatar_pk)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "形象不存在")
    if write:
        if not row.can_write(user):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "形象不存在")
    elif not row.is_visible_to(user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "形象不存在")
    return row
