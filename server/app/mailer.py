"""邮件发送层：平台 SMTP 配置 + 发信 + 本机「伪装发信」模式。

设计要点
--------
1. **只用标准库** ``smtplib`` + ``email``，不新增第三方依赖：本项目 requirements
   里没有邮件库，为一个低频功能（找回密码 / 工单通知）引入新包不值得，
   标准库的 smtplib 覆盖 SSL / STARTTLS / 鉴权已经够用。

2. **真发信失败绝不把异常抛给路由层**。SMTP 配错的概率极高（端口与加密方式
   不匹配、用了登录密码而不是授权码、云厂商禁了 25 端口……），而邮件在本项目里
   永远是旁路：它失败不应该让「提交工单」「申请验证码」整个 500。
   所以 ``send_mail()`` 统一返回 ``MailResult(ok, error)``，由调用方决定
   是记日志、还是给管理员显式报错。

3. **伪装模式（ELDER_MAIL_FAKE=1）**：本机与 CI 连不到外网 SMTP，但测试又必须
   拿到验证码内容才能验证「请求 -> 校验 -> 重置」这条链路。伪装模式下邮件
   不进网络，只 append 到内存 ``OUTBOX``，再由管理端 GET /api/admin/mail/outbox
   读出来（该接口仅在伪装模式下可用，避免生产环境把验证码泄露给管理员页面）。

4. 时间统一存 UTC aware（与全库 models.utcnow() 一致）。SQLite 的
   ``DateTime(timezone=True)`` 读回来是 **naive** 的（实测：存 Asia/Shanghai 的
   本地时间会被原样写成墙上时间串，读回后若当 UTC 处理会差 8 小时），
   所以存储一律 UTC，只有**写邮件正文**给人看时才转成 config.TZ。

5. **SMTP 全链路有超时，且超时是一种「失败」而不是「挂死」**。这是真实部署里
   最容易被忽略的坑：管理员把 host 填错、或防火墙静默丢包时，TCP 握手可能
   一直挂着不返回；连接建好之后，等对端的 SMTP 应答（220/250/235…）同样可能
   无限期等不到 —— 一个「accept 了连接但一句话不说」的中转设备就能做到。
   连接超时与读超时是两个独立的失败模式（``smtp_connect_timeout`` /
   ``smtp_read_timeout``），本模块两者都设、并在 socket 层显式兜一道，
   让「超时」成为自身保证的不变量。任何超时/异常都在 ``send_mail`` 内被吃成
   ``MailResult(ok=False)``，绝不冒泡成接口 500（设计要点 2）。
"""
from __future__ import annotations

import logging
import os
import secrets
import smtplib
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from typing import Iterable

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from . import models
from .config import TZ

log = logging.getLogger("elder.mail")

# ---------------------------------------------------------------- 可调参数
# 这几个常量没有放进 config.py（那个文件本轮不改动），统一在这里用环境变量覆盖。
# 真要调到 config.py 里时，把名字与默认值原样搬过去即可。
RESET_CODE_TTL_MIN = int(os.getenv("ELDER_RESET_CODE_TTL_MIN", "15"))
RESET_CODE_MAX_ATTEMPTS = int(os.getenv("ELDER_RESET_CODE_MAX_ATTEMPTS", "5"))
# 重发冷却：防止「找回密码」接口被当成邮件轰炸器（不停拿别人的邮箱刷验证码）
RESET_RESEND_COOLDOWN_SEC = int(os.getenv("ELDER_RESET_RESEND_COOLDOWN", "60"))
# 过期验证码的清理缓冲（秒）：purge_expired_reset_codes() 只删「过期超过这个缓冲」的行
RESET_CODE_PURGE_BUFFER_SEC = int(os.getenv("ELDER_RESET_PURGE_BUFFER", "3600"))
# 管理员邮箱：APP 在「该账号没绑邮箱」时要把它显示给用户（用户要求里的
# 「请联系管理员…zxh@250886.xyz」）。放服务端而不写死在前端，是为了
# 换地址时只改一处，也避免前端改版漏掉。
RESET_ADMIN_EMAIL = os.getenv("ELDER_ADMIN_EMAIL", "zxh@250886.xyz")
# 直接寄新密码时生成的随机口令长度。12 位含大小写与数字，
# 熵足够（约 71 bit），同时老人能在电话里念出来、抄到纸上。
RESET_GENERATED_PWD_LEN = 12
# 单次 SMTP 会话超时（秒）。宁可早点失败也别让请求挂住：
# 这个发送是同步阻塞在请求线程里的，超时太长会把工作线程拖死
# （FastAPI 的同步 def 端点跑在固定大小的线程池里，挂住几个请求
#  整个服务对前端就不可用了）。
#
# ⚠️ **连接超时与读超时是两件事，必须分开设**：
#   * ``connect``：TCP 三次握手 + （SSL 场景）TLS 握手。host 写错、防火墙
#     静默丢包、网络黑洞都卡在这里。
#   * ``read``：连接**已经建立**，在等对端回 SMTP 应答（220/250/235…）。
#     一个「accept 了连接但一句话不说」的服务器（或中间设备丢弃回程流量）
#     只会卡在这里。
# 合并成一个值也能兜住（实测 smtplib 会把 timeout 留在 socket 上），
# 但两者量级不同：TLS 握手要跟公网往返多次，读取则要求「尽快拿到下一句」，
# 分开才能各自调优；更重要的是分开表述能让「到底该配哪个」一目了然。
#
# 两者都刻意取保守值：正常发信是毫秒级的，10 秒还连不上基本就是配错了。
# 两者都可单独用 ELDER_SMTP_CONNECT_TIMEOUT / ELDER_SMTP_READ_TIMEOUT 覆盖。
SMTP_CONNECT_TIMEOUT_SEC = float(os.getenv("ELDER_SMTP_CONNECT_TIMEOUT", "10"))
SMTP_READ_TIMEOUT_SEC = float(os.getenv("ELDER_SMTP_READ_TIMEOUT", "10"))


def _env_float(name: str, default: float) -> float:
    """读一个正浮点环境变量，非法/非正值一律回落到默认值。

    为什么不让非法值直接抛：这些变量是运维在容器编排文件里手写的，
    写错一个值不应该让**整个服务起不来**（甚至连带邮件这种旁路功能一起挂），
    回落到默认值 + 一条 warning 才是合理的降级。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        log.warning("环境变量 %s=%r 不是合法数字，回落到 %s", name, raw, default)
        return default
    if val <= 0:
        log.warning("环境变量 %s=%r 不是正数，回落到 %s", name, raw, default)
        return default
    return val


def smtp_connect_timeout() -> float:
    """建连（含 TLS 握手）超时秒数。每次调用都重读环境变量，便于测试调整。"""
    return _env_float("ELDER_SMTP_CONNECT_TIMEOUT", SMTP_CONNECT_TIMEOUT_SEC)


def smtp_read_timeout() -> float:
    """已建连后的读超时秒数。每次调用都重读环境变量，便于测试调整。

    单独暴露这个函数是为了让测试能只把读超时压到极小值，从而在秒级内
    复现「连接成功但对端不回话」这个只有读超时能救的场景。
    """
    return _env_float("ELDER_SMTP_READ_TIMEOUT", SMTP_READ_TIMEOUT_SEC)


def _apply_socket_timeout(server: smtplib.SMTP, timeout: float) -> None:
    """把读超时显式压到 smtplib 底层的 socket 上（幂等，starttls 之后要再调一次）。

    实测结论（本地「只 accept 不回话」的黑色服务器，见 tools/ 下的反向验证）：
    ``smtplib.SMTP(host, port, timeout=T)`` 里的 T 经 ``socket.create_connection``
    落到 socket 上后**会保留**，所以纯文本收发与 TLS 握手其实已经被 T 兜住了
    （实测分别在 T+0.02s / T+0.06s 抛 TimeoutError）。

    那为什么还要显式再压一道？三点理由：
      1. **可测性/ 可调粒度**：连接与读取是两个独立的失败模式（见上面的常量
         注释），要分别给不同的值就必须能在建连之后单独改 socket；
      2. **不依赖实现细节**：``create_connection`` 保留 timeout 是当前 CPython
         的行为，不是 ``smtplib`` 的文档契约；``starttls()`` 还会把 socket 换成
         TLS 包装对象。显式设置让「超时」成为本模块自己保证的不变量，
         而不是「恰好 smtplib 顺手也做了」。
      3. **失败要可见**：压不上超时直接 raise，绝不静默进入「可能永久阻塞」的状态。
    """
    sock = getattr(server, "sock", None)
    if sock is None:
        return
    try:
        sock.settimeout(timeout)
    except OSError as exc:  # pragma: no cover - 底层 socket 异常极少发生
        # 压不上超时不能静默放行：那正是「可能永久挂死」的状态，必须让调用方知道
        log.warning("设置 SMTP socket 超时失败（timeout=%s）: %s", timeout, exc)
        raise



def fake_mode() -> bool:
    """是否处于伪装发信模式（每次调用都重读环境变量，便于测试临时切换）。"""
    return os.getenv("ELDER_MAIL_FAKE", "0").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- 伪装发件箱
@dataclass
class FakeMail:
    to: list[str]
    subject: str
    body: str
    from_addr: str
    at: str


# 只在伪装模式下增长；进程重启即清空（测试进程与服务进程各一份，
# 所以测试是通过管理端接口读服务进程里的这份，而不是直接读本模块变量）。
OUTBOX: list[FakeMail] = []


def outbox_items(limit: int = 50) -> list[dict]:
    """最近若干封伪装邮件（倒序，最新在前），供管理端排查与冒烟测试取验证码。"""
    items = OUTBOX[-limit:][::-1]
    return [
        {"to": m.to, "subject": m.subject, "body": m.body,
         "from": m.from_addr, "at": m.at}
        for m in items
    ]


def clear_outbox() -> None:
    OUTBOX.clear()


# ---------------------------------------------------------------- 配置读写
def now_utc() -> datetime:
    """当前时刻（aware UTC）。

    与 models.utcnow() 完全等价，单独在这里再定义一次是为了让 mailer
    不反向依赖 models 的私有工具函数命名。
    """
    return datetime.now(timezone.utc)


def get_smtp_config(db: Session) -> models.SmtpConfig:
    """取 SMTP 配置（单行表，没有就建一条 enabled=False 的空配置）。"""
    cfg = db.get(models.SmtpConfig, 1)
    if cfg is None:
        cfg = models.SmtpConfig(id=1)
        db.add(cfg)
        db.flush()
    return cfg


def mask_secret(raw: str) -> str:
    """机密字段掩码：``abcdefgh`` -> ``ab******gh``。

    只保留首尾各 2 位。之所以保留首尾而不是全部打星：管理员排查「这个密码
    是不是填错了」时，能认出「哦是我那个 ab..gh 结尾的授权码」就够了，
    而 2 位字符泄露的实际信息量可以忽略。空值返回空串（由 has_password 表达
    「有没有配」）。
    """
    if not raw:
        return ""
    if len(raw) <= 4:
        return "*" * len(raw)
    return f"{raw[:2]}{'*' * 6}{raw[-2:]}"


def sender_addr(cfg: models.SmtpConfig) -> str:
    """实际发件地址：from_addr 没填时回落到 username（多数服务商要求两者一致）。"""
    return (cfg.from_addr or cfg.username or "").strip()


def notify_addr(cfg: models.SmtpConfig) -> str:
    """工单通知的收件地址：notify_to -> from_addr -> username。"""
    return (cfg.notify_to or cfg.from_addr or cfg.username or "").strip()


def smtp_ready(cfg: models.SmtpConfig) -> tuple[bool, str]:
    """配置是否「看起来能发信」。返回 (是否可用, 不可用的原因)。"""
    if not cfg.enabled:
        return False, "SMTP 未启用"
    if not (cfg.host or "").strip():
        return False, "未填写 SMTP 服务器地址"
    if not sender_addr(cfg):
        return False, "未填写发件邮箱（from_addr / username）"
    if not cfg.port:
        return False, "未填写端口"
    return True, ""


# ---------------------------------------------------------------- 发送
@dataclass
class MailResult:
    ok: bool
    error: str = ""
    fake: bool = False
    to: list[str] = field(default_factory=list)


def _is_timeout(exc: BaseException) -> bool:
    """判断一个异常是不是「超时」（含被 smtplib 重新包装过的）。

    为什么不能只判类型：``smtplib.SMTP.readline()`` 里是

        except OSError as e:
            self.close()
            raise SMTPServerDisconnected("Connection unexpectedly closed: " + str(e))

    而 ``socket.timeout`` 在 Py3.10+ 就是 ``TimeoutError``（``OSError`` 子类），
    所以**读超时会被 smtplib 抓走并改写成 SMTPServerDisconnected**，
    异常类型丢失、只剩 message 里那句 "timed out"（实测确认）。
    结果就是：只判 ``isinstance(exc, TimeoutError)`` 会漏掉「连接建好了但对端
    不回话」这个最常见的挂起形态 —— 恰恰是最该被标出来的那种失败。

    所以这里三层判定：类型（连接/TLS 握手阶段，异常原样冒泡） ->
    原因链（``__cause__`` / ``__context__``，smtplib 用的是 raise 而不是
    raise ... from，链上可能有）-> 消息文本（最后兜底）。
    """
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    # smtplib 包装时保留的原始异常可能挂在 cause/context 上
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, (socket.timeout, TimeoutError)):
            return True
        cur = cur.__cause__ or cur.__context__
    # 文本兜底：跨 Python 版本 / 未来 smtplib 改写措辞时仍尽量认得出
    return "timed out" in str(exc).lower()


def send_mail(
    db: Session,
    to: str | Iterable[str],
    subject: str,
    body: str,
) -> MailResult:
    """发一封纯文本邮件。**永不抛异常**，失败原因放在 ``MailResult.error``。

    to 可以是单个地址或地址列表；非法/空地址直接按失败返回（不发信）。
    """
    to_list = [a.strip() for a in ([to] if isinstance(to, str) else list(to)) if a and a.strip()]
    if not to_list:
        return MailResult(False, "收件人为空", to=[])

    cfg = get_smtp_config(db)
    ready, why = smtp_ready(cfg)
    if not ready:
        # 不发信不是异常，是配置状态；调用方据此决定给用户/管理员什么反馈
        return MailResult(False, why, to=to_list)

    from_addr = sender_addr(cfg)

    if fake_mode():
        # ---- 伪装模式：只记内存，不碰网络（本机/冒烟测试用）----
        OUTBOX.append(FakeMail(
            to=to_list, subject=subject, body=body, from_addr=from_addr,
            at=now_utc().astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        ))
        log.info("[FAKE] 伪装发信 -> %s | %s", ",".join(to_list), subject)
        return MailResult(True, "", fake=True, to=to_list)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((cfg.from_name or "老人陪伴提醒助手", from_addr))
    msg["To"] = ", ".join(to_list)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    connect_timeout = smtp_connect_timeout()
    read_timeout = smtp_read_timeout()
    server: smtplib.SMTP | smtplib.SMTP_SSL | None = None
    try:
        if cfg.use_ssl:
            # timeout 只作用于建连（含 TLS 握手）；读超时在下面 socket 层补
            server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=connect_timeout)
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=connect_timeout)
        _apply_socket_timeout(server, read_timeout)
        server.ehlo()
        if cfg.use_tls:
            # STARTTLS：587 端口的标准姿势。use_ssl 与 use_tls 同时为真时
            # 以 use_ssl 优先（已经是 SSL 连接了，再套一层没有意义）
            server.starttls()
            # starttls 会换掉底层 socket，必须重新压一次读超时
            _apply_socket_timeout(server, read_timeout)
            server.ehlo()
        if cfg.username:
            server.login(cfg.username, cfg.password)
        server.sendmail(from_addr, to_list, msg.as_string())
        log.info("邮件已发送 -> %s | %s", ",".join(to_list), subject)
        return MailResult(True, "", to=to_list)
    except Exception as exc:  # noqa: BLE001  任何 SMTP 异常都按「发送失败」处理
        # ⚠️ 这一段是「邮件是旁路」的最后一道防线：连接超时、读超时、TLS
        # 证书错误、SMTP 4xx/5xx 拒收、编码异常……全部收敛成 MailResult，
        # 绝不允许冒泡到路由层变成 500（否则一次配置错误就能让
        # 「提交工单」「找回密码」整条链路不可用）。
        # ⚠️ 不要把 cfg.password 打进日志
        err = f"{type(exc).__name__}: {exc}"
        if _is_timeout(exc):
            # 单独标注：超时是最需要管理员看见的一类失败（他配的地址不可达）。
            # 注意读超时会被 smtplib 包装成 SMTPServerDisconnected，
            # 所以判定走 _is_timeout 而不是 isinstance。
            err = f"SMTP 超时（{connect_timeout:g}s 连接 / {read_timeout:g}s 读）：{err}"
        log.warning("邮件发送失败 -> %s | %s | %s", ",".join(to_list), subject, err)
        return MailResult(False, err, to=to_list)
    finally:
        if server is not None:
            try:
                # quit() 会再走一次网络往返；此时连接可能已经半死，
                # 所以这里统一用 close()（纯本地关闭，不发任何报文），
                # 避免清理阶段又引入一次挂起。
                server.close()
            except Exception:  # noqa: BLE001  关闭失败无所谓，邮件已经发出去了
                pass


# ---------------------------------------------------------------- 邮件正文
def generate_password(length: int = RESET_GENERATED_PWD_LEN) -> str:
    """生成随机口令（用于「直接寄新密码」那条链路）。

    ⚠️字符集刻意**不含** 0/O、1/l/I 等易混字符：老人要把它抄到纸上，
    抄错一位就登不进来，而他们往往不会去看邮件里的原文。
    大小写与数字保留（熵约 71 bit / 12 位），只排除 6 个易混字符。
    """
    # 去掉了 0/O/1/l/I；字母表大小写都保留但排除了 o/O（易与 0 混）
    letters = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"
    digits = "23456789"
    alphabet = letters + digits
    return "".join(secrets.choice(alphabet) for _ in range(max(8, length)))


def build_new_password_mail(username: str, new_password: str) -> tuple[str, str]:
    """把**新密码本身**发到用户邮箱的邮件（2026-10-19 用户要求的那条链路）。

    与 build_reset_mail（发验证码、用户自己设密码）的分工：
      * 验证码版：明文口令从不出现在邮件里，邮箱被窃听也拿不到密码；
      * 本版：老人不用自己想密码，服务端生成一串足够强的随机口令直接寄过去。

    ⚠️ 正文里必须写清「这是新口令、请尽快登录后修改」——
    口令会留在收件箱里（转发、备份、代理服务器都可能留存），
    不提示用户去改就等于长期暴露。
    """
    subject = "【老人陪伴提醒助手】你的新密码"
    body = (
        f"你好{f'，{username}' if username else ''}：\n"
        "\n"
        "你刚刚申请了重置登录密码。**新的密码是：**\n"
        "\n"
        f"    {new_password}\n"
        "\n"
        "请用它登录。登录后请尽快在「我的 → 修改密码」里改成只有你知道的密码。\n"
        "\n"
        f"如果这不是你本人的操作，请忽略本邮件（密码不会被自动使用），"
        f"或联系管理员（{RESET_ADMIN_EMAIL}）核实账号安全。\n"
    )
    return subject, body


def find_user_by_username(db: Session, username: str) -> models.User | None:
    """按用户名找用户（大小写不敏感）。与 find_user_by_email 同理用 func.lower。"""
    target = username.strip()
    return db.scalar(
        select(models.User).where(func.lower(models.User.username) == target.lower())
    )


def build_reset_mail(username: str, code: str, ttl_min: int) -> tuple[str, str]:
    """找回密码邮件的 (主题, 正文)。有效期按 config.TZ 展示，用户才对得上时间。"""
    expire_text = (now_utc().astimezone(TZ).replace(microsecond=0)
                   .strftime("%Y-%m-%d %H:%M"))
    subject = "【老人陪伴提醒助手】找回密码验证码"
    body = (
        f"你好{f'，{username}' if username else ''}：\n"
        "\n"
        f"你正在通过邮箱找回密码，验证码是：{code}\n"
        "\n"
        f"验证码 {ttl_min} 分钟内有效，最多可尝试 {RESET_CODE_MAX_ATTEMPTS} 次，"
        "超时或超次需要重新获取。\n"
        f"（本次验证码大约在 {expire_text} 过期）\n"
        "\n"
        "如果这不是你本人的操作，请忽略本邮件，密码不会被修改。\n"
    )
    return subject, body


def build_ticket_notify_mail(ticket: models.SupportTicket, username: str) -> tuple[str, str]:
    """新工单通知管理员的 (主题, 正文)。"""
    subject = f"【新工单 #{ticket.id}】{ticket.title}"
    body = (
        f"有用户通过 APP 提交了新问题（工单号 #{ticket.id}）：\n"
        "\n"
        f"分类：{models.TicketCategory.TEXTS.get(ticket.category, ticket.category)}\n"
        f"标题：{ticket.title}\n"
        f"联系邮箱：{ticket.email}\n"
        f"账号：{username or '（匿名提交）'}\n"
        f"设备：{ticket.device_id or '（未填）'}\n"
        f"时间：{ticket.created_at}\n"
        "\n"
        "内容：\n"
        f"{ticket.content}\n"
        "\n"
        "请登录管理端处理并回复。\n"
    )
    return subject, body


def build_ticket_reply_mail(ticket: models.SupportTicket, reply: str) -> tuple[str, str]:
    """管理员回复工单时通知提交者的 (主题, 正文)。"""
    subject = f"【工单回复 #{ticket.id}】{ticket.title}"
    body = (
        "你好：\n"
        "\n"
        f"你提交的问题（工单号 #{ticket.id}「{ticket.title}」）有了新回复：\n"
        "\n"
        f"{reply}\n"
        "\n"
        "如果问题已经解决，可以不用再处理；仍未解决可再次提交工单说明情况。\n"
    )
    return subject, body


# ---------------------------------------------------------------- 查询辅助
def latest_reset_code(db: Session, email: str) -> models.PasswordResetCode | None:
    """该邮箱最近一次申请的验证码（不管是否过期/已用），用于冷却判断。"""
    return db.scalar(
        select(models.PasswordResetCode)
        .where(models.PasswordResetCode.email == email.strip().lower())
        .order_by(models.PasswordResetCode.id.desc())
        .limit(1)
    )


def purge_expired_reset_codes(db: Session, now: datetime | None = None) -> int:
    """清理已经不可能再用到的验证码行，返回删除行数。

    为什么必须清理：这张表是「只增不减」的 —— 成功用掉的、超次作废的，以及
    **为了账号枚举防护给未注册邮箱也插的那一行**（见 routers/mail.py 的
    request_reset）都会留在库里。被人扫号时后面这类会激增，跑几个月就是一张
    没什么用的大表。

    删除三类行：
      1. 过期且已超过缓冲期（``expires_at < now - 缓冲``）；
      2. 已消费（``used_at`` 非空）—— 一次性，用掉就没有留着的必要；
      3. 尝试次数已用尽（``attempts >= max_attempts``）。

    为什么第 1 类要留 1 小时缓冲而不是「过期即删」：
      * **请求进行中**：用户点下「验证」时验证码还没过期，但清理任务恰好在
        这一刻扫过，把码删了，这次验证就变成莫名其妙的「验证码错误或已过期」；
      * **时钟偏差**：多个容器/进程的时钟不可能完全对齐，服务端与数据库时间
        也可能差几秒，卡在边界删容易误伤；
      * 留一小时窗口也方便排查「用户说收不到/验证码不对」这类问题。

    ⚠️ 本函数**只导出、不在这里调用**：调用点由后台周期任务（scheduler 的
    _tick）统一挂，避免各模块各自起定时器。函数保持同步（_tick 在线程池里跑，
    不能是 async）。
    """
    base = now or now_utc()
    cutoff = base - timedelta(seconds=RESET_CODE_PURGE_BUFFER_SEC)
    stmt = delete(models.PasswordResetCode).where(
        or_(
            models.PasswordResetCode.expires_at < cutoff,
            models.PasswordResetCode.used_at.is_not(None),
            models.PasswordResetCode.attempts >= models.PasswordResetCode.max_attempts,
        )
    )
    deleted = db.execute(stmt).rowcount or 0
    db.commit()
    if deleted:
        log.info("清理过期/失效的找回密码验证码 %d 行", deleted)
    return int(deleted)


def find_user_by_email(db: Session, email: str) -> models.User | None:
    """按邮箱找用户（大小写不敏感）。

    用 func.lower 而不是直接等值比较：老数据里可能存了大写字母，
    而用户找回时填的写法未必和当初一致。等值比较在 MySQL 默认排序规则下
    虽然也不区分大小写，但 SQLite 下是区分的，两边行为不一致会埋坑。
    """
    target = email.strip().lower()
    return db.scalar(
        select(models.User).where(func.lower(models.User.email) == target)
    )
