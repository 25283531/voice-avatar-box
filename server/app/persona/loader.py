"""从数据库装配「这个角色此刻的」spec + 画像 —— 语音链路的人格入口。

为什么需要这一层
----------------
``persona.compiler`` 只认 ``RoleSpec`` 与画像字典，不认识数据库；而「怎么从
一行 ``ai_providers`` 装配出 spec / 画像」这段逻辑原来只存在于
``routers/roles.py``（``_parent_traits`` / ``_spec_of`` / ``_effective_profile``）。

语音链路要用同一条逻辑，但**不能 import 路由层**：routers 依赖 FastAPI、
依赖注入、鉴权，让每一次老人说话都反向依赖 HTTP 层，等于把 Web 框架的
初始化成本与副作用（router 注册、Depends 解析）绑到音频线程上。

所以这里把那段逻辑搬过来，落点是本模块；路由层那份保持原样不动（本次
任务不允许改 routers）。两边读的是同一张表、同一套 ``from_row`` /
``ProfileMerger``，唯一的实质差异是「谁有权读」：路由层用
``deps.require_role`` 判权，这里用 ``AIProvider.is_visible_to`` 的等价判据
（preset 公开 / role 仅 owner）在本地判一次。

永不抛异常
----------
取不到行、行不是角色、被软删/下架、不是这位用户的角色、traits 非法、
画像读失败 —— 一律返回 ``(None, {})``。人格是**增益**，不是对话的前提：
拿不到角色就该按基础口令照常说话，绝不能让老人对着沉默的音箱。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from .. import models
from ..memory.persona import merge_profiles
from . import guardrails
from .compiler import ProfileMerger
from .spec import from_row

log = logging.getLogger("elder.persona.loader")


def _parent_traits(db: Session, row: models.AIProvider) -> dict[str, Any]:
    """母版的**声明** traits（没有母版则为空 dict）。

    与 ``routers/roles.py:63 _parent_traits`` 同义：派生角色的 ``traits``
    列只存 overlay，要叠加在母版声明之上；原创角色与 preset 的 traits 列
    本身就是完整声明。
    """
    if not row.parent_id:
        return {}
    parent = db.get(models.AIProvider, int(row.parent_id))
    if parent is None:
        # 母版被硬删过（不该发生：preset 只能 archive 不能 delete）。
        # 按「无母版」处理，让派生角色用平台默认值而不是崩掉。
        log.warning("角色 id=%s 的母版 id=%s 不存在，按无母版处理",
                    row.id, row.parent_id)
        return {}
    return guardrails.as_dict(parent.traits)


def _visible_to_user(row: models.AIProvider, user_id: int) -> bool:
    """读权限：preset 全平台可用，role 只属于 owner。

    设备上的 ``agent_id`` 是绑定时由家属选定的，理论上不会指向别人的角色；
    但这道判据很便宜，而「用别人的角色人格跟自家老人说话」是隐私事故，
    值得在每次对话前多判一次（判不过就走基础口令，不报错）。
    """
    if row.is_preset():
        return True
    return row.owner_id is not None and int(row.owner_id) == int(user_id)


def load_agent_persona(db: Session, user_id: int, agent_id: int | None
                       ) -> tuple[Any, dict]:
    """返回 ``(spec, profile)``；取不到就返回 ``(None, {})``，绝不抛异常。

    :param db: 当前请求/轮次的会话（只读，不改任何行）。
    :param user_id: 老人所属用户（``Device.owner_id``）。
    :param agent_id: ``Device.agent_id``，可能为 ``None``（未绑角色）。
    """
    if agent_id is None or not user_id:
        return None, {}
    try:
        row = db.get(models.AIProvider, int(agent_id))
        if row is None:
            log.info("设备绑定的角色 id=%s 不存在，语音按无人格回答", agent_id)
            return None, {}
        # 必须是角色行：万一 agent_id 指到一条平台配置（record_type=config），
        # 拿它去编译人格会把 api_key/model 之类的字段当成角色设定读。
        if not row.is_role():
            log.warning("agent_id=%s 不是角色行（record_type=%r），忽略",
                        agent_id, row.record_type)
            return None, {}
        if not row.is_alive():
            log.info("角色 id=%s 已软删/下架，语音按无人格回答", agent_id)
            return None, {}
        if not _visible_to_user(row, user_id):
            log.warning("角色 id=%s 不属于 user_id=%s，拒绝使用其人格",
                        agent_id, user_id)
            return None, {}

        # 与 routers/roles.py:82 _spec_of 同一条装配路径（母版 overlay 叠加）。
        spec = from_row(row, _parent_traits(db, row) if row.parent_id else None)
        # 非法 trait 值绝不能进 prompt：validate 抛就整体回退到基础口令。
        spec.validate()

        # 画像：L1（全局）+ L2（角色级），再按 L2.manual > L1 统计 > traits 声明
        # 合并（与 routers/roles.py:294 _effective_profile 同一条优先级链）。
        try:
            layers = merge_profiles(db, int(user_id), int(agent_id))
        except Exception as exc:  # noqa: BLE001  画像读失败不该拖垮人格
            log.warning("读取角色 id=%s 的画像失败，按无画像编译：%s: %s",
                        agent_id, type(exc).__name__, exc)
            layers = {}
        profile = ProfileMerger.merge(layers.get("l1"), layers.get("l2"),
                                      spec.traits_effective())
        return spec, profile
    except Exception as exc:  # noqa: BLE001  人格装配永远不该让对话失败
        log.warning("装配角色 id=%s 的人格失败，语音按无人格回答：%s: %s",
                    agent_id, type(exc).__name__, exc)
        return None, {}
