"""注册 / 登录 / 当前用户。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import (
    ALLOW_PUBLIC_REGISTER,
    BOOTSTRAP_ADMIN_USERNAME,
    REGISTER_CLOSED_DETAIL,
)
from ..db import get_db
from ..deps import get_current_user
from ..ratelimit import (
    clear_login_failures_for,
    login_gate,
    record_login_failure,
)
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


def _resolve_new_user_is_admin(username: str) -> bool:
    """本次注册是否应产出管理员 —— **只认显式配置的名字，不认到达顺序**。

    被替换掉的老规则（``select(User).limit(1) is None``，即"库里没人 -> 你就是管理员"）
    在匿名可注册的部署上等于把管理员身份发给第一个路人。这里改成：

        ``username == config.BOOTSTRAP_ADMIN_USERNAME``

    * 未配置（默认）-> 恒定 False：**公开注册永远不产出管理员**；
    * 配置了 -> 也只认这一个名字，其它名字一律 False。

    ⚠️ 为什么这里**不再查一次"库里有没有管理员"**
    ----------------------------------------------
    曾经考虑过 ``「名字匹配」且「尚无管理员」``（第二个条件像是更保险），但那会
    让"注册出来是不是管理员"取决于**调用时刻的数据库状态**：同一个配置、
    同一份请求，在空库上给管理员、在有库上不给。这种"看当时心情"的判定无法在
    冒烟里稳定复现，运维也更难推理，而它额外防住的场景（在已有管理员的库上，
    有人恰好注册了运维特意配的那个名字）本就要求攻击者**事先知道那个名字**。
    相比之下，「规则恒定、结果可预测」更值得保。

    这条路同时也保证了**对既有部署的幂等性**：本函数从不写任何人的 ``is_admin``，
    只会给"这一次新建的行"定值 —— 已有管理员不会因为别人注册而被降级。
    """
    # 未配置 -> 任何人都不是管理员（strip 过的空串 -> False）
    return bool(BOOTSTRAP_ADMIN_USERNAME) and username == BOOTSTRAP_ADMIN_USERNAME


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    # ⚠️ 策略判定必须**先于**任何数据库查询。
    # 放在用户名查重之后的话，注册关闭时仍能用「400 用户名已存在 / 403」
    # 的差异把已存在的用户名枚举出来 —— 关掉注册不该顺带变成一个探测接口。
    if not ALLOW_PUBLIC_REGISTER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, REGISTER_CLOSED_DETAIL)
    exists = db.scalar(select(models.User).where(models.User.username == payload.username))
    if exists:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名已存在")
    user = models.User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        nickname=payload.nickname or payload.username,
        phone=payload.phone,
        email=payload.email,
        is_admin=_resolve_new_user_is_admin(payload.username),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


#: 失败文案。**必须**同时覆盖"用户不存在"与"密码错"两种情况，
#: 且两种情况返回**完全相同**的响应 —— 否则这个接口就是一台用户名枚举机。
#: （限速触发时返回 429，见下面 login() 里的说明。）
LOGIN_FAILED_DETAIL = "用户名或密码错误"


@router.post("/login", response_model=TokenOut)
def login(payload: UserLogin, request: Request, db: Session = Depends(get_db)):
    """登录。

    ⚠️ 顺序是**先验口令、后过闸门**，这个顺序是有意的，别"优化"成先查配额：

    1. 口令正确 -> 发证 + 清掉该账号的失败记录，**完全不看配额**。
       ★ 这一条决定了**限速器永远不会把知道口令的人锁在外面**：
       攻击者打满 20 次/小时，真正的管理员照样进得来 ——
       否则"限速"就变成了一把把合法用户锁死的 DoS 武器。
       （``tools/smoke_mail.py:458-461`` 先故意输错一次、紧接着用新口令登录，
       反过来写会让它从 200 变 429。）
    2. 口令不对 -> 先过闸门（IP 键 + 用户名键，任一触发即拦），
       触发了就 429 + 还要等多久；没触发才记一笔失败并返回 401。
       记与不记的差别很大：**被 429 拦下的那一笔不记账**，否则攻击者持续敲
       就能把冷却期无限续期，5 秒冷却会退化成永久锁死。

    ⚠️ 为什么触发限速用 **429** 而不是 401：401 会让"密码错"和"被限速"
    在响应上无法区分，运维排障时看不出是被打了还是自己输错了；而 429 是否
    "帮攻击者确认命中限速"其实无所谓 —— 他连发几次本来就能从"突然开始被拒"
    推断出来，藏住状态码换不来任何安全性，只换来看不懂的日志。
    """
    user = db.scalar(select(models.User).where(models.User.username == payload.username))
    if user and verify_password(payload.password, user.password_hash):
        clear_login_failures_for(payload.username)
        token = issue_user_token(user.id, user.username, user.is_admin)
        return TokenOut(access_token=token, user_id=user.id, username=user.username,
                        is_admin=user.is_admin)

    verdict = login_gate(request, payload.username)
    if verdict is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            verdict.detail,
            headers={"Retry-After": str(verdict.retry_after)},
        )
    record_login_failure(request, payload.username)
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, LOGIN_FAILED_DETAIL)


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
