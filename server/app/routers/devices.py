"""设备管理 + 激活码绑定。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..config import ACT_CODE_LEN
from ..db import get_db
from ..deps import get_current_user, require_device_owner, require_role
from ..macs import mac_variants, norm_mac
from ..ratelimit import bind_gate, clear_bind_failures, record_bind_failure
from ..schemas import (
    ActivationCodeOut,
    BindIn,
    BindOut,
    DeviceAgentIn,
    DeviceAgentOut,
    DeviceCreate,
    DeviceOut,
    DeviceUpdate,
    EventOut,
)
from ..security import (
    format_code,
    issue_device_token,
    normalize_code,
    verify_activation_code,
)

router = APIRouter(prefix="/api/devices", tags=["devices"])

log = logging.getLogger(__name__)


def _norm_mac(value: str) -> str:
    """归一化 MAC 以便**比较**：去分隔符（``:`` / ``-``）、转小写。

    实现已收敛到 ``app.macs.norm_mac``（唯一真源）。留这个别名是因为本文件
    多处调用、且注释里已引用该名字；**不要在这里另写一份规则** ——
    曾经 `voice` 与 `devices` 各持一种写法，导致同一台设备被当成两台。
    """
    return norm_mac(value)


def _find_device_row(db: Session, mac: str) -> models.Device | None:
    """按 MAC 查 ``Device`` 行，**容忍两种历史写法**（带冒号 / 不带冒号）。

    为什么不能直接 ``Device.device_id == mac``：固件给的是
    ``aa:bb:cc:dd:ee:ff``，而网页端「登记设备」表单存的是
    ``aabbccddeeff``。直接等值比较会把同一台设备当成两台 ——
    轻则列表里出现重复行，重则绑定建出第二行、设备心跳找不到自己。

    ⚠️ 用 ``IN (mac_variants(...))`` 而不是 ``lower(replace(...))``：
    函数包住列会让 ``device_id`` 上的索引失效（全表扫）。这里虽然不在
    无鉴权的 OTA 热路径上，但保持与 ``voice._find_device`` **同一形态**
    很重要 —— 两处查询口径一旦分叉，就会出现「OTA 认为已绑定、网页端
    认为没登记」这类只在特定数据上复现的故障（见 app/macs.py 模块注释）。

    ⚠️ 必须 ``order_by(id)`` + 多行告警：``mac_variants`` 现在最多返回 4~6 种
    写法，库里若同时有 ``AA:BB:..`` 与 ``aa:bb:..`` 两行，``IN`` 会命中两行，
    而 ``scalar`` 无排序时取哪行**由执行计划决定**。表现是「同一台设备时有时无」
    —— 比「查不到」更难排查。取最早那行保证可复现，同时 ``log.error`` 提示合并。
    """
    rows = db.scalars(
        select(models.Device)
        .where(models.Device.device_id.in_(mac_variants(mac)))
        .order_by(models.Device.id)
        .limit(2)
    ).all()
    if not rows:
        return None
    if len(rows) > 1:
        log.error(
            "同一 MAC 命中多台设备行（存量写法未归一化，请人工合并）：mac=%s ids=%s",
            norm_mac(mac), [r.id for r in rows])
    return rows[0]


@router.post("", response_model=DeviceOut, status_code=status.HTTP_201_CREATED)
def create_device(payload: DeviceCreate, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    """登记一台设备（通常由 APP 在扫码后调用，设备标识取 eFuse MAC）。"""
    # 查重必须容忍两种写法，否则「带冒号登记一次、不带冒号再登记一次」
    # 会让同一台设备长出两行（心跳只会认其中一行，另一行永远离线）。
    exists = _find_device_row(db, payload.device_id)
    if exists:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该设备已登记")
    # 入库统一存归一化形式：与 ActivationCode.device_id 同口径，
    # 绑定/OTA/心跳三条路才都指向同一行。
    dev = models.Device(device_id=norm_mac(payload.device_id), name=payload.name,
                        owner_id=user.id, bound_at=models.utcnow())
    # 建档即归属（owner_id 直接写死为当前用户）⇒ bound_at 必须同时写。
    # 漏写的后果：这条路径建出的设备「已归属但绑定时间显示 —」，与 bind 流程
    # 两种口径，家属看到同一平台两台设备一个有时间一个没有。
    db.add(dev)
    db.commit()
    db.refresh(dev)
    return dev


@router.get("", response_model=list[DeviceOut])
def list_devices(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    q = select(models.Device)
    if not user.is_admin:
        q = q.where(models.Device.owner_id == user.id)
    return list(db.scalars(q.order_by(models.Device.id.desc())).all())


@router.get("/{device_pk}", response_model=DeviceOut)
def get_device(device_pk: int, db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    return require_device_owner(device_pk, user, db)


@router.patch("/{device_pk}", response_model=DeviceOut)
def update_device(device_pk: int, payload: DeviceUpdate, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    dev = require_device_owner(device_pk, user, db)
    if payload.name is not None:
        dev.name = payload.name
    if payload.avatar_id is not None:
        dev.avatar_id = payload.avatar_id
    if payload.settings is not None:
        dev.settings = {**(dev.settings or {}), **payload.settings}
    db.commit()
    db.refresh(dev)
    return dev


@router.delete("/{device_pk}", status_code=status.HTTP_204_NO_CONTENT)
def delete_device(device_pk: int, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    dev = require_device_owner(device_pk, user, db)
    db.delete(dev)
    db.commit()


@router.post("/{device_pk}/unbind", status_code=status.HTTP_204_NO_CONTENT)
def admin_unbind_device(device_pk: int, db: Session = Depends(get_db),
                        user: models.User = Depends(get_current_user)):
    """**平台管理员**把设备从原账户解绑（清归属，不删设备行）。

    为什么必须存在这条路径（用户 2026-10-20 明确要求）：

        家属忘了自己注册的用户名 ⇒ 登不进原账户 ⇒ 网页/APP 里那个「删除设备」
        也点不到（它要求你是设备主人）⇒ 设备被永久锁在原账户上，而设备本身
        还在老人家里正常使用。这时**唯一可行的通道**是：家属发邮件给平台管理员，
        管理员在后台把设备解绑，设备下一次 OTA 轮询（15s）就重新变成未绑定态、
        屏幕上重新出现激活码，家属可以用**新账户**重新绑定。

    与 ``delete_device`` 的区别（两者不能互相替代）：
      * ``delete_device`` 是**用户自助**，删整行，连带提醒/回执/审计一起没；
      * 本接口是**管理员代操作**，只清 ``owner_id`` / ``bound_at``，设备行、
        历史提醒、交互记录全部保留 —— 因为真实诉求是「换个人来管这台设备」，
        不是「把这台设备从系统里抹掉」。删行会让老人正在吃的药提醒一起消失。

    ⚠️ 权限：**只允许管理员**。设备主人自己不该走这条路 —— 他有 ``delete_device``
    与后续的解绑入口，让主人也能调本接口等于给了一个「悄悄清空归属」的暗门，
    而正常解绑应当走有明确 UI 提示的路径。
    """
    if not user.is_admin:
        # 403 而不是 404：这条规则本身是产品规则（要告诉用户"你得找管理员"），
        # 不是在保护某个秘密 —— 与 deps.require_device_owner 的 404 口径不同，
        # 那里保护的是「设备主键是否存在」。
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "仅平台管理员可执行解绑，请联系平台管理员")

    dev = db.get(models.Device, device_pk)
    if dev is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在")
    if dev.owner_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该设备当前未绑定任何账户")

    old_owner = dev.owner_id
    mac = str(dev.device_id or "")
    dev.owner_id = None
    dev.bound_at = None
    # 代次 +1 ⇒ 该设备已签出的**全部** device_token 立即失效。
    # 不这样做，解绑只清了归属、没清访问权：原家属手上的 token 在 365 天
    # 有效期内仍能开 WS 读数据，等新家庭绑定后就读到**新家庭**的提醒了。
    dev.token_epoch = int(getattr(dev, "token_epoch", 0) or 0) + 1
    db.commit()

    # 解绑后设备应能立刻被重新绑定 ⇒ 作废它名下所有残留码，让设备下一轮
    # OTA（15s）拿到一枚全新的码，而不是拿到一枚「别人已经看过」的旧码。
    from ..voice import _retire_codes_for

    retired = _retire_codes_for(db, mac)

    # 审计：这是跨账户的破坏性操作，必须有可追溯记录。日志级别用 WARNING，
    # 保证在默认 INFO 之上、任何日志配置下都能落盘。
    log.warning("ADMIN-UNBIND device_pk=%s mac=%s 原归属用户=%s 操作管理员=%s(id=%s) 作废码=%d",
                dev.id, mac, old_owner, user.username, user.id, retired)


@router.post("/{device_pk}/agent", response_model=DeviceAgentOut)
def bind_agent(device_pk: int, payload: DeviceAgentIn, db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    """给设备绑定一个 agent 角色（一台设备同时只生效一个）。

    越权处理与 require_device_owner 略有不同：别人的设备这里统一返回 404
    而不是 403 —— 403 等于告诉调用方「这台设备存在，只是不是你的」，
    会把设备主键是否存在这一信息泄露出去。角色同理（deps.require_role）。

    ⚠️ **P0-4 修复**：原先这里只做 ``db.get(AIProvider, role_id)`` 存在性校验，
    于是**任何平台 LLM 配置都能被绑成角色**（kind 判空根本没查）。
    后果是 build_persona_prompt 拿到一条空 system_prompt —— 设备"绑上了角色"
    却按没有人格的方式说话，而且不报错，只是静默劣化。
    现在统一走 deps.require_role：必须是角色行、且对当前用户可见。

    TODO: 目前只把 agent_id 落库。设备心跳（/api/device/sync）还没有把角色
    下发给固件，等端侧支持「按角色切换人格 / 音色」时，在 DeviceSyncOut 里
    加一个 agent 字段、复用这里取角色的逻辑即可，不必改本接口。
    """
    dev = db.get(models.Device, device_pk)
    if dev is None or (not user.is_admin and dev.owner_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在")

    # 必须是**角色**且当前用户可见：平台配置在这里表现为"不存在"
    role = require_role(db, payload.role_id, user)
    if role.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "角色不存在")

    dev.agent_id = role.id
    db.commit()
    return DeviceAgentOut(device_pk=dev.id, role_id=role.id, role_name=role.name)


# ============================================================ 激活码
@router.post("/{device_pk}/code", response_model=ActivationCodeOut)
def issue_code(device_pk: int, db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    """（管理员）为**尚未绑定**的设备补发一枚 10 分钟有效的激活码。

    ⚠️ 2026-10-20 语义变更（用户要求「已绑定设备不再下发激活码」）：
    本接口原先对**任何**归属设备都发码，于是「已绑定」和「还有一枚可用码」
    两个状态可以同时存在 —— 那枚码就是一次重复绑定的入口。现在：

      * 设备已绑定（``owner_id`` 非空）⇒ **409**，明确告诉调用方先解绑；
      * 设备未绑定 ⇒ 走与固件完全相同的取码逻辑（``voice._ensure_activation_code``），
        复用有效码、过期换新、并作废旧码 —— **一条实现，不是第二份规则**。

    正常家庭场景根本用不到这个接口：码是设备自己开机向 OTA 端点申领的，
    屏幕上就有。它只留给管理员在排障时「替一台连不上网的设备补一枚码」。
    """
    dev = require_device_owner(device_pk, user, db)
    if dev.owner_id is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "该设备已绑定账户，不再下发激活码；如需换绑请先解绑（忘记用户名请邮件联系平台管理员）")

    # 延迟 import：voice 顶层会 import routers.device_api，顶层互引会成环。
    from ..voice import _ensure_activation_code

    row = _ensure_activation_code(db, str(dev.device_id or ""))
    return ActivationCodeOut(code=row.code, display=format_code(row.code),
                             expires_at=row.expires_at, device_id=dev.device_id)


@router.post("/bind", response_model=BindOut)
def bind_device(request: Request, payload: BindIn, db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    """APP 侧：输入激活码绑定设备，返回设备 token。

    激活码 = **6 位随机数字 + 1 位 Luhn 校验位 = 7 位全数字**（与固件
    ``firmware/components/elder/src/activation.c`` 逐位一致）。
    输入时允许带空格或短横线，屏幕上的 ``123-4567`` 可以直接粘贴。

    ⚠️ 空间只有 10^6，所以下面三道防线**缺一不可**：
      1. IP 级限速（``ratelimit.bind_gate``，每分钟 5 次 / 每小时 40 次失败）；
      2. 单码失败上限（``row.attempts`` vs ``row.max_attempts``）——
         攻击者换 IP 也不能对同一枚码无限试；
      3. 码与设备强绑定（``row.device_id`` 必须等于 APP 上报的 device_id）——
         撞到别人的码也绑不到自己名下。
    """
    code = normalize_code(payload.code)

    # 闸门放在**最前面**：格式错误的请求同样计入限速。否则攻击者可以先用
    # 一堆格式错的请求探"这枚码是否存在"，全程不触发记账。
    verdict = bind_gate(request, user.id)
    if verdict is not None:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, verdict.detail,
                            headers={"Retry-After": str(verdict.retry_after)})

    def _fail(status_code: int, detail: str) -> None:
        """记一笔失败再抛 —— 记账点只此一处，避免漏记。"""
        record_bind_failure(request, user.id)
        raise HTTPException(status_code, detail)

    if len(code) != ACT_CODE_LEN:
        _fail(status.HTTP_400_BAD_REQUEST,
              f"激活码应为 {ACT_CODE_LEN} 位数字，当前 {len(code)} 位")
    if not verify_activation_code(code):
        _fail(status.HTTP_400_BAD_REQUEST, "激活码校验失败，请核对屏幕上的 7 位数字")

    row = db.scalar(select(models.ActivationCode).where(models.ActivationCode.code == code))
    if not row:
        _fail(status.HTTP_404_NOT_FOUND, "激活码不存在，请在设备上重新获取")
    if row.used:
        _fail(status.HTTP_400_BAD_REQUEST, "激活码已使用")
    if row.attempts >= row.max_attempts:
        _fail(status.HTTP_400_BAD_REQUEST, "该激活码尝试次数过多已失效，请在设备上重新获取")
    if row.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        _fail(status.HTTP_400_BAD_REQUEST, "激活码已过期，请在设备上重新获取")

    # 码与设备强绑定：激活码是在**哪台设备的屏幕上**显示的，就只能绑哪台。
    # 这一条是 6 位码可接受的前提 —— 没有它，撞码 = 把别人的设备绑到自己名下。
    #
    # ⚠️ 比较前必须**归一化 MAC**：固件 `SystemInfo::GetMacAddress()` 产出的是
    # `aa:bb:cc:dd:ee:ff`（带冒号），而 `Device.device_id` 历史上存的是
    # `aabbccddeeff`（不带冒号，见 models.Device 注释与 smoke_server 断言）。
    # 直接字符串比会把**合法绑定判成不匹配**，用户表现为「APP 说绑定成功、
    # 设备一直要激活码」。APP 不传 device_id 时（默认流程）本检查跳过。
    if row.device_id and payload.device_id and _norm_mac(payload.device_id) != _norm_mac(row.device_id):
        row.attempts += 1
        db.commit()
        _fail(status.HTTP_400_BAD_REQUEST, "激活码与设备不匹配，请确认输入的是本机屏幕上的码")

    # ⚠️ 建档/查库一律用 **row.device_id（固件权威值）**，不用 payload：
    # 激活码是固件按自己的 MAC 申领的，只有用它建档，设备心跳/OTA 才找得到
    # 自己这一行。用 APP 传来的字符串会造出「一台设备两行」或「永远查不到」。
    wanted_id = row.device_id or payload.device_id or ""

    # ⚠️⚠️ 2026-10-20 修 P0：**全新设备经 APP 绑定必然失败**。
    # 原先这里在 `dev` 不存在时要求 `payload.device_id` 非空，否则 400
    # 「缺少设备标识」。但真实链路是：
    #     设备开机 → 打 /api/voice/ota → 服务端按 MAC 建**激活码行**（尚无 Device 行）
    #     → 家属在 APP 只输入 7 位码 → bind。
    # APP 的 doBind 只传 `{code, alias}`（它**拿不到** MAC），而全仓建 Device 行
    # 的地方只有 `create_device`（网页端「登记设备」表单）与本函数 —— 也就是说
    # **只有先走网页登记的人才能绑上，纯 APP 用户一定卡在 400**。
    # 正确判据不是「APP 有没有给 MAC」，而是「这枚码知不知道自己是哪台设备的」：
    # 码里带了 device_id（固件申领时写死）就足够建档，不需要 APP 再补一遍。
    if not wanted_id:
        # 只有「历史通用码（无 device_id）且 APP 也没给 MAC」才真的无法定位设备
        row.attempts += 1
        db.commit()
        _fail(status.HTTP_400_BAD_REQUEST,
              "该激活码未关联设备，请在设备上重新获取一枚新码")

    dev = _find_device_row(db, wanted_id)
    if not dev:
        dev = models.Device(device_id=norm_mac(wanted_id),
                            name=payload.alias or "我的陪伴助手")
        db.add(dev)
        db.flush()

    # ⚠️ 归属保护：设备已经有主人、且不是当前用户 ⇒ **拒绝**，不许静默改归属。
    # 没有这一条，任何拿到码的人（撞码 / 从 OTA 端点按 MAC 取码）都能把别人
    # 名下正在使用的设备「重新绑定」到自己账户，进而拿到设备 token、下发提醒
    # 与话术 —— 这是物理世界的冒充，不是普通的数据越权。
    # 换绑必须先由原主人解绑（/api/devices/{pk} 删除或解绑接口）。
    if dev.owner_id is not None and dev.owner_id != user.id:
        row.attempts += 1
        db.commit()
        _fail(status.HTTP_409_CONFLICT,
              "该设备已绑定其他账户，请先在原账户中解绑后再试")

    dev.owner_id = user.id
    # 绑定时间 = 当前归属的起始时刻。放在这里而不是上面「现场建档」分支里：
    # 通用码场景设备可能刚 flush 出来，重绑定（解绑再绑）也要更新时间，
    # 一个写入点覆盖两条路径。存量设备此列为 NULL，UI 显示「—」。
    dev.bound_at = models.utcnow()
    if payload.alias:
        dev.name = payload.alias
    row.used = True
    row.used_at = models.utcnow()
    row.used_by = user.id
    db.commit()

    # ⚠️ 用户要求：「已经绑定用户的设备…不再下发激活码，不允许重复绑定」。
    # 绑定成功这一刻起，这台设备名下**任何还活着的未用码都必须作废** ——
    # 否则同一台设备会同时存在「已绑定」和「还有一枚可用码」两个状态，
    # 任何人拿到那枚码就能把它再绑一次（bind_device 的 409 只拦「绑给别人」，
    # 拦不住「原主人自己被重复绑」这种无意义但会污染审计的路径）。
    #
    # 延迟 import：voice 模块会 import routers.device_api（_ws_endpoint），
    # 顶层 import 会形成环。这里只为一个纯 DB 操作付一次模块查找成本。
    from ..voice import _retire_codes_for

    retired = _retire_codes_for(db, wanted_id, keep_id=row.id)
    if retired:
        log.info("绑定成功：作废 MAC %s 名下残留激活码 %d 枚", wanted_id, retired)

    db.refresh(dev)

    # 绑定成功 ⇒ 清掉该 IP 的失败计数（家属连续绑多台不该被限速误伤）
    clear_bind_failures(request, user.id)

    return BindOut(device_id=dev.device_id, device_pk=dev.id,
                   device_token=issue_device_token(
                       dev.id, dev.device_id, int(dev.token_epoch or 0)), name=dev.name)


@router.get("/{device_pk}/events", response_model=list[EventOut])
def device_events(device_pk: int, limit: int = 100, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    """查看该设备的提醒回执（是否按时确认）。"""
    require_device_owner(device_pk, user, db)
    rows = db.scalars(
        select(models.ReminderEvent)
        .where(models.ReminderEvent.device_id == device_pk)
        .order_by(models.ReminderEvent.due_at.desc())
        .limit(limit)
    ).all()
    return list(rows)
