"""邮箱能力路由。

分两组：
  * ``admin_router``  /api/admin/mail     —— 管理员配置 SMTP、测试发信、查看伪装发件箱
  * ``auth_router``   /api/auth/password/reset —— 用户自助找回密码（APP 内完成）

用户的所有操作都在手机 APP 完成，没有网页端：所以「找回密码」走的是
「APP 提交邮箱 -> 收邮件拿 6 位验证码 -> 回到 APP 填验证码 + 新密码」这条
闭环，服务端不提供任何重置页面。
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import mailer
from .. import models
from ..db import get_db
from ..deps import get_current_admin
from ..mailer import (
    RESET_CODE_MAX_ATTEMPTS,
    RESET_CODE_TTL_MIN,
    RESET_RESEND_COOLDOWN_SEC,
    fake_mode,
)
from ..schemas import (
    MailTestIn,
    MailTestOut,
    PasswordResetRequestIn,
    PasswordResetRequestOut,
    PasswordResetVerifyIn,
    PasswordResetVerifyOut,
    SmtpConfigIn,
    SmtpConfigOut,
)
from ..security import hash_password, verify_password

# ============================================================ 管理端
admin_router = APIRouter(
    prefix="/api/admin/mail", tags=["admin-mail"],
    dependencies=[Depends(get_current_admin)],
)

# ============================================================ 用户端（找回密码）
auth_router = APIRouter(prefix="/api/auth/password/reset", tags=["auth"])


# ---------------------------------------------------------------- 序列化
def _smtp_out(cfg: models.SmtpConfig) -> SmtpConfigOut:
    """配置 -> 出参。password 只给掩码，明文永不出口（理由见 schemas.SmtpConfigOut）。"""
    return SmtpConfigOut(
        host=cfg.host,
        port=cfg.port,
        username=cfg.username,
        password=mailer.mask_secret(cfg.password),
        has_password=bool(cfg.password),
        use_tls=cfg.use_tls,
        use_ssl=cfg.use_ssl,
        from_addr=cfg.from_addr,
        from_name=cfg.from_name,
        notify_to=cfg.notify_to,
        enabled=cfg.enabled,
        updated_at=cfg.updated_at,
    )


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite 读回的时间是 naive（存的是 UTC），比较前补回 UTC 时区。

    与 chat_quota.aware() 同一个坑：naive 和 aware 直接相减会抛
    ``TypeError: can't subtract offset-naive and offset-aware datetimes``。
    """
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- 管理端接口
@admin_router.get("/smtp", response_model=SmtpConfigOut)
def get_smtp(db: Session = Depends(get_db)):
    """读取 SMTP 配置。密码只返回掩码。"""
    return _smtp_out(mailer.get_smtp_config(db))


@admin_router.patch("/smtp", response_model=SmtpConfigOut)
def patch_smtp(payload: SmtpConfigIn, db: Session = Depends(get_db),
               admin: models.User = Depends(get_current_admin)):
    """局部更新 SMTP 配置。

    password 是**只写**字段：传了才覆盖，不传保持原值；读取接口也拿不到明文，
    所以改端口之类的操作不需要「先读再回传」，顺带避免了明文在前端来回搬运。
    """
    cfg = mailer.get_smtp_config(db)
    for field in ("host", "port", "username", "use_tls", "use_ssl",
                  "from_addr", "from_name", "notify_to", "enabled"):
        value = getattr(payload, field)
        if value is not None:
            setattr(cfg, field, value)
    if payload.password is not None:
        cfg.password = payload.password
    cfg.updated_at = mailer.now_utc()
    db.commit()
    db.refresh(cfg)
    return _smtp_out(cfg)


@admin_router.post("/test", response_model=MailTestOut)
def test_mail(payload: MailTestIn | None = None, db: Session = Depends(get_db),
              admin: models.User = Depends(get_current_admin)):
    """发送一封测试邮件，把失败原因原样返回。

    为什么必须有这条接口：SMTP 参数（端口 / 加密方式 / 授权码）配错的概率极高，
    而配错的后果是「找回密码彻底不可用」，用户只会反馈「收不到邮件」，
    管理员没有任何自查手段，只能去翻服务日志。配完立刻发一封、当场看到
    「连不上 / 鉴权失败 / 发件人被拒」，是这套邮箱能力能否落地的关键。
    """
    cfg = mailer.get_smtp_config(db)
    to = ((payload.to if payload else "") or "").strip() or mailer.notify_addr(cfg)
    if not to:
        return MailTestOut(ok=False, error="收件人为空：请填 to，或在配置里填 notify_to / from_addr",
                           to="", fake=fake_mode(),
                           message="请先配置 SMTP")

    subject = ((payload.subject if payload else "") or "").strip() or \
        "【老人陪伴提醒助手】SMTP 配置测试邮件"
    body = (
        "这是一封测试邮件，用来确认平台的 SMTP 配置是否正确。\n"
        "\n"
        "收到这封邮件说明配置可用，「邮箱找回密码」与「工单通知」都能正常发送。\n"
        f"\n发送时间：{mailer.now_utc()}\n发信模式：{'伪装发信（未真正投递）' if fake_mode() else '真实投递'}\n"
    )
    result = mailer.send_mail(db, to, subject, body)
    return MailTestOut(
        ok=result.ok,
        error=result.error,
        to=",".join(result.to),
        fake=result.fake,
        message=("测试邮件已发出，请查收收件箱" if result.ok else f"发送失败：{result.error}"),
    )


@admin_router.get("/outbox")
def get_outbox(limit: int = Query(50, ge=1, le=200), clear: bool = False,
               db: Session = Depends(get_db),
               admin: models.User = Depends(get_current_admin)):
    """查看伪装发件箱（仅在 ELDER_MAIL_FAKE=1 时可用）。

    本机与 CI 连不通外网 SMTP，冒烟测试要靠这里把验证码掏出来才能验证
    「申请 -> 校验 -> 重置」整条链路。它只在伪装模式下开放：生产环境如果也能
    读，等于任何管理员都能拿到别人的重置验证码。
    """
    if not fake_mode():
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "当前不是伪装发信模式（ELDER_MAIL_FAKE=1），没有发件箱")
    items = mailer.outbox_items(limit)
    if clear:
        mailer.clear_outbox()
    return {"fake": True, "count": len(items), "items": items}


# ---------------------------------------------------------------- 找回密码
def _fail(message: str, attempts_left: int = 0) -> HTTPException:
    """校验失败的统一出口。

    注意这里**不区分**「邮箱没申请过验证码」「验证码错误」「验证码已过期」
    「验证码已用掉」「尝试次数用尽」—— 全部走同一句话。否则攻击者可以用
    「这个邮箱有没有在找回密码」来探测账号是否存在。attempts_left 只在本邮箱
    **已经**申请过验证码时才有意义（未申请时也是同一个默认值，见 verify_reset）。
    """
    return HTTPException(
        status.HTTP_400_BAD_REQUEST,
        {"message": message, "attempts_left": attempts_left},
    )


@auth_router.post("/request", response_model=PasswordResetRequestOut)
def request_reset(payload: PasswordResetRequestIn, db: Session = Depends(get_db)):
    """申请找回密码验证码：提交邮箱 -> 生成 6 位验证码 -> 发邮件。

    ⚠️ 账号枚举防护（本接口最重要的约束）：
      1. 响应体对「已注册邮箱」和「未注册邮箱」**完全一致**，不回显任何
         「该邮箱未注册」之类的信息 —— 否则就是一个现成的注册用户名单接口；
      2. 因此**未注册的邮箱也会插入一条验证码行**（user_id=None）：
         这样冷却时间、尝试次数、失败响应都走同一条路径，攻击者无法通过
         「429 冷却 / attempts_left 递减」来区分邮箱是否注册；
      3. 剩下的差异只有两个，都不是响应内容：
           - **发不发信**：未注册邮箱不发（也不该发，否则就是帮别人发垃圾邮件）；
           - **响应耗时**：未注册邮箱少一次 SMTP 往返，会快那么一点。
         ⚠️ 也就是说**响应体一致，但存在耗时侧信道**——理论上攻击者可以用高精度
         计时区分邮箱是否注册。彻底消除要统一延时或把发信挪到后台线程，
         成本远大于收益（6 位验证码 + 5 次上限 + 15 分钟有效期已经把实际
         利用价值压得很低），本轮不做。别把上面三条理解成「完全无侧信道」。

    另一个副作用是：未注册邮箱也会消耗一次冷却窗口，避免接口被当成
    任意邮箱的邮件轰炸器（虽然不发信，但请求本身要落库）。
    """
    email = payload.email.strip().lower()
    now = mailer.now_utc()

    # ---- 1) 重发冷却：60 秒内不重复发，防止被当成邮件轰炸器 ----
    latest = mailer.latest_reset_code(db, email)
    if latest is not None:
        created = _aware(latest.created_at) or now
        elapsed = (now - created).total_seconds()
        if elapsed < RESET_RESEND_COOLDOWN_SEC:
            wait = int(RESET_RESEND_COOLDOWN_SEC - elapsed)
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                f"验证码已发送，请 {wait} 秒后再试")

    # ---- 2) 平台是否具备发信能力：判定放在查用户之前，保证两种邮箱返回一致 ----
    cfg = mailer.get_smtp_config(db)
    ready, why = mailer.smtp_ready(cfg)
    if not ready:
        # 503 而不是 500，也不是「静默成功」：
        # 静默成功会让用户一直等一封永远不会来的邮件，比直接说「平台没配邮件」
        # 体验差得多；而这个判定与邮箱是否注册无关，不破坏枚举防护。
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"平台尚未配置可用的邮件服务（{why}），请联系管理员")

    # ---- 3) 查用户（结果只影响「发不发信」，不影响响应） ----
    user = mailer.find_user_by_email(db, email)

    # ---- 4) 生成验证码：存哈希，明文只出现在邮件里 ----
    code = "".join(secrets.choice("0123456789") for _ in range(6))
    row = models.PasswordResetCode(
        email=email,
        user_id=(user.id if user else None),
        code_hash=hash_password(code),
        expires_at=now + timedelta(minutes=RESET_CODE_TTL_MIN),
        attempts=0,
        max_attempts=RESET_CODE_MAX_ATTEMPTS,
        created_at=now,
    )
    # 同一邮箱的旧验证码全部作废：用户重新申请说明旧的那串已经不要了，
    # 留着只会给「捡到旧邮件的人」多一次机会。
    for old in db.scalars(
        select(models.PasswordResetCode)
        .where(models.PasswordResetCode.email == email,
               models.PasswordResetCode.used_at.is_(None))
    ).all():
        old.used_at = now
    db.add(row)
    db.commit()

    # ---- 5) 发信。失败不能让请求 500，也不能改变响应（否则又成了枚举侧信道）----
    if user is not None:
        subject, body = mailer.build_reset_mail(user.username, code, RESET_CODE_TTL_MIN)
        result = mailer.send_mail(db, email, subject, body)
        if not result.ok:
            # 邮件没发出去，验证码也就没意义了：直接作废，避免用户在 APP 里
            # 对着一个永远收不到的验证码反复试。
            row.used_at = mailer.now_utc()
            db.commit()
    # 未注册邮箱：不查用户、不发信，什么都不做（但上面那行验证码已经落库了）

    return PasswordResetRequestOut(
        ok=True,
        cooldown_sec=RESET_RESEND_COOLDOWN_SEC,
        expires_in_sec=RESET_CODE_TTL_MIN * 60,
    )


@auth_router.post("/verify", response_model=PasswordResetVerifyOut)
def verify_reset(payload: PasswordResetVerifyIn, db: Session = Depends(get_db)):
    """校验验证码 + 重置密码。

    安全要点：
      * 用 ``verify_password``（内部 hmac.compare_digest 恒定时间比较）比对哈希，
        不用 ``==``：``==`` 在第一个字节不同就立刻返回，耗时差可被用来逐位爆破
        6 位验证码（虽然只有 100 万种组合，但配合无限次尝试就是真漏洞）；
      * 有效期 15 分钟 + 最多 5 次尝试，超次即作废；
      * 校验通过后**立即**把该邮箱所有未用的验证码置为已用（一次性）。

    ⚠️ 已知局限：重置密码**不会**让已签发的旧 token 失效 —— JWT 是无状态的，
    服务端没有 token 版本号或黑名单，改密码这件事它感知不到。要真正做到
    「改完就强制下线」需要引入 token 版本号（users 上加一列，签发时写进去、
    每次鉴权比对），属于改动 auth 全局，本轮不做。
    当前能做的已经做了：重置成功后该用户所有已存在的验证码立即作废（不会再有
    第二个验证码被人拿去重置），风险范围就只剩「旧 token 在它剩余有效期内
    仍然可用」—— 账号真被别人拿走了，改密码后对方最多用到 token 自然过期。
    """
    email = payload.email.strip().lower()
    now = mailer.now_utc()

    row = mailer.latest_reset_code(db, email)
    if row is None:
        # 与「验证码错误」完全相同的响应，不给「你没申请过」这种提示
        raise _fail("验证码错误或已过期", RESET_CODE_MAX_ATTEMPTS)

    if row.used_at is not None:
        raise _fail("验证码错误或已过期", 0)

    if (_aware(row.expires_at) or now) <= now:
        raise _fail("验证码错误或已过期", 0)

    left = max(0, int(row.max_attempts or RESET_CODE_MAX_ATTEMPTS) - int(row.attempts or 0))
    if left <= 0:
        # 尝试次数用尽：作废，逼用户重新申请
        row.used_at = now
        db.commit()
        raise _fail("验证码错误或已过期", 0)

    # 恒定时间比较（内部 hmac.compare_digest），且比较的是哈希而不是明文
    ok = verify_password(payload.code, row.code_hash)
    if not ok:
        row.attempts = int(row.attempts or 0) + 1
        if row.attempts >= int(row.max_attempts or RESET_CODE_MAX_ATTEMPTS):
            row.used_at = now      # 最后一次也错了 -> 直接作废
        db.commit()
        raise _fail("验证码错误或已过期", max(0, left - 1))

    # user_id 为空 = 该邮箱未注册（申请时不知道，这里才知道），
    # 走与「验证码错误」完全相同的失败响应，不给任何额外信息。
    user = db.get(models.User, row.user_id) if row.user_id else None
    if user is None:
        row.attempts = int(row.attempts or 0) + 1
        row.used_at = now
        db.commit()
        raise _fail("验证码错误或已过期", max(0, left - 1))

    user.password_hash = hash_password(payload.new_password)
    # 一次性：本邮箱所有未消费的验证码一起作废（含本次）
    row.used_at = now
    for other in db.scalars(
        select(models.PasswordResetCode)
        .where(models.PasswordResetCode.email == email,
               models.PasswordResetCode.used_at.is_(None))
    ).all():
        other.used_at = now
    db.commit()

    return PasswordResetVerifyOut(ok=True, username=user.username)
