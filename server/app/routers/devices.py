"""设备管理 + 激活码绑定。"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import ACT_CODE_LEN
from ..db import get_db
from ..deps import get_current_user, require_device_owner, require_role
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
    code_expires_at,
    format_code,
    generate_activation_code,
    issue_device_token,
    normalize_code,
    verify_activation_code,
)

router = APIRouter(prefix="/api/devices", tags=["devices"])


def _norm_mac(value: str) -> str:
    """归一化 MAC 以便**比较**：去分隔符（``:`` / ``-``）、转小写。

    固件 `SystemInfo::GetMacAddress()` 给的是 ``aa:bb:cc:dd:ee:ff``，而
    `Device.device_id` 的历史存量是 ``aabbccddeeff``（不带冒号）。
    **只用于比较，绝不用于存储** —— 存储侧保持原样，否则同一台设备会长出第二行。
    """
    return "".join(ch for ch in (value or "") if ch.isalnum()).lower()


@router.post("", response_model=DeviceOut, status_code=status.HTTP_201_CREATED)
def create_device(payload: DeviceCreate, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    """登记一台设备（通常由 APP 在扫码后调用，设备标识取 eFuse MAC）。"""
    exists = db.scalar(select(models.Device).where(models.Device.device_id == payload.device_id))
    if exists:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该设备已登记")
    dev = models.Device(device_id=payload.device_id, name=payload.name, owner_id=user.id,
                        bound_at=models.utcnow())
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
    """为该设备生成一枚 10 分钟有效的激活码，屏显后由 APP 输入完成绑定。"""
    dev = require_device_owner(device_pk, user, db)
    for _ in range(8):
        code = generate_activation_code()
        clash = db.scalar(select(models.ActivationCode).where(models.ActivationCode.code == code))
        if not clash:
            break
    else:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "激活码生成失败，请重试")

    row = models.ActivationCode(
        code=code,
        device_id=dev.device_id,
        expires_at=code_expires_at(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return ActivationCodeOut(code=code, display=format_code(code), expires_at=row.expires_at,
                             device_id=dev.device_id)


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
    dev = db.scalar(select(models.Device).where(models.Device.device_id == wanted_id))
    if not dev:
        # 设备尚未登记（极少见：码总是按 MAC 建的，但历史通用码没有 device_id）
        if not payload.device_id:
            row.attempts += 1
            db.commit()
            _fail(status.HTTP_400_BAD_REQUEST, "缺少设备标识")
        dev = models.Device(device_id=wanted_id, name=payload.alias or "我的陪伴助手")
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
    db.refresh(dev)

    # 绑定成功 ⇒ 清掉该 IP 的失败计数（家属连续绑多台不该被限速误伤）
    clear_bind_failures(request, user.id)

    return BindOut(device_id=dev.device_id, device_pk=dev.id,
                   device_token=issue_device_token(dev.id, dev.device_id), name=dev.name)


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
