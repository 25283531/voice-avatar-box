"""RoleSpec：把一行 ai_providers 装配成编译器的输入，并做 traits 值域校验。

职责边界（架构 §1.1）：人格编译是**纯函数式算法**，输入 spec + profile，
输出 prompt 字符串。它不依赖 FastAPI、也不碰 DB 会话细节，因此放在
``app/persona/`` 而不是塞进 ``routers/`` 或 ``memory/``。本模块就是那个
"输入"的形状定义与装配逻辑 —— DB 行在这里被读一次，之后全程是普通对象。

⚠️ validate() 为什么必须**抛异常**而不是返回错误列表：
traits 的值会**逐字进 system prompt**。如果非法值只是被"忽略"，用户会看到
"我设了很啰嗦但没用"却找不到原因；更糟的是非法值若被静默替换成默认值，
用户以为自己改了性格，实际在按别人的设定说话。宁可整次编译失败
（fail-closed，与 guardrails 同一 philosophy），也不要静默降级。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import catalog
from .catalog import RECORD_TYPE_PRESET


class TraitValueError(ValueError):
    """trait 取值非法。单独一个类型，便于接口层把它映射成 400 而不是 500。"""


@dataclass
class RoleSpec:
    """一个角色的编译输入。

    ``traits_declared`` 是**声明**（角色自己设定的性格），
    ``traits_overlay`` 是与母版的差集。编译时用 overlay 覆盖 declared 得到
    生效值；差集存在的意义是「还原为预设」只需清空 overlay 一行。
    """
    role_id: int
    name: str
    system_prompt: str = ""
    description: str = ""
    tts_voice: str = ""
    traits_declared: dict[str, Any] = field(default_factory=dict)
    traits_overlay: dict[str, Any] = field(default_factory=dict)
    guardrails_hard: list[dict[str, Any]] = field(default_factory=list)
    guardrails_soft: list[dict[str, Any]] = field(default_factory=list)
    # True = 平台预设。预设的 traits 是完整声明（没有母版可覆盖）。
    is_preset: bool = False
    parent_id: int | None = None

    def validate(self) -> "RoleSpec":
        """校验所有 trait 值；非法即抛 TraitValueError。

        只校验**会被写进 prompt 的那份**（即生效值）。overlay 里存了非法值
        而生效值恰好合法的情况不校验 —— 那不影响编译产物，而且 overlay
        可能来自未使用的历史数据，为此拒绝整次编译是过度反应。
        """
        effective = self.traits_effective()
        for name, value in effective.items():
            spec = catalog.TRAIT_SPECS.get(name)
            if spec is None:
                raise TraitValueError(
                    f"未知的性格维度 {name!r}；合法取值：{'/'.join(catalog.TRAIT_NAMES)}"
                )
            if not spec.is_valid(value):
                raise TraitValueError(
                    f"性格维度 {name!r}（{spec.label}）取值非法：{value!r}；"
                    f"合法范围 {spec.describe_range()}"
                )
        return self

    def traits_effective(self) -> dict[str, Any]:
        """生效的traits：母版声明被 overlay 覆盖。

        overlay 里的键若不在 TRAIT_SPECS 中也照样带出来 —— 由 validate()
        负责报错。这里不静默丢弃，是为了让"非法值"这件事**可见**。
        """
        merged = dict(self.traits_declared or {})
        merged.update(self.traits_overlay or {})
        return merged

    def trait_sentences(self) -> list[str]:
        """把生效 traits 渲染成一句条行为描述（校验通过后调用）。

        顺序按 TRAIT_SPECS 的定义顺序（Python dict 保序），而不是用户设置的
        顺序 —— 让同一个角色每次编译出的 prompt 完全一致，
        否则 eval 的字符串断言会因为无关的顺序抖动而假红。
        """
        effective = self.traits_effective()
        out: list[str] = []
        for name in catalog.TRAIT_NAMES:
            if name not in effective:
                continue
            spec = catalog.TRAIT_SPECS[name]
            text = spec.to_prompt_sentence(effective[name])
            if text:
                out.append(text)
        return out


def _traits_dict(value: Any) -> dict[str, Any]:
    """把 traits 列归一成 dict；非 dict 当空 dict。

    与 ``guardrails.as_dict`` 同一口径，但这里**刻意不 import guardrails**：
    guardrails 不依赖 spec，反向 import 会成环。重复这四行好过引入一个
    跨模块循环依赖 —— 归一规则本身简单到不值得为它牺牲依赖方向。

    为什么需要：JSON 列可以存合法但非 dict 的值（``[1,2,3]`` / ``42``），
    ``dict(...)`` 对它们会抛 TypeError，而这里在**每次编译**的入口 ——
    一次脏数据就会让这个角色的每一轮对话都 500。
    """
    return dict(value) if isinstance(value, dict) else {}


def known_valid_traits(traits: Any) -> tuple[dict[str, Any], list[str]]:
    """挑出「维度已知且取值合法」的项，返回 ``(可用项, 被丢弃项说明)``。

    为什么需要这条（QA 第四轮实测的真缺口）
    --------------------------------------
    ``_traits_dict`` 只挡「**不是 dict**」的脏值（``[1,2]`` / ``42`` / ``"str"``）。
    但 ``{"x":1}`` / ``{"humor":"nope"}`` 这类是**结构合法**的 dict，
    它们会一路活到 :meth:`RoleSpec.validate` 才抛 ``TraitValueError``。

    而 ``validate`` 抛出的异常在接口层被映射成 **400** ——
    于是**一行脏 traits 会让整个 ``GET /api/roles`` 返回 400**。
    角色是 preset 时更糟：它是全平台共享的一行，**所有用户**的角色列表
    都打不开。QA 实测确认（每轮只改一行、轮间复位，排除交叉污染）：

        ``'"str"'`` / ``'[1,2]'`` / ``'42'`` / ``null``  -> 200（已被挡住）
        ``'{"x":1}'``                                   -> **400 整表挂**
        ``'{"humor":"nope"}'``                          -> **400 整表挂**

    与 ``validate`` 的关系：**不是**放松校验，而是给「列表视图」一条
    降级路径 —— 单角色详情仍应 400（用户能改、能看懂那个提示），
    但**列表必须能开**（一行脏数据不该让全平台看不到角色）。

    :param traits: 原始列值（任何类型）。
    :return: ``(可用项 dict, 丢弃说明列表)``；丢弃说明供调用方记日志。
    """
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for name, value in _traits_dict(traits).items():
        spec = catalog.TRAIT_SPECS.get(name)
        if spec is None:
            dropped.append(f"未知维度 {name!r}")
        elif not spec.is_valid(value):
            dropped.append(f"{name}={value!r}（合法范围 {spec.describe_range()}）")
        else:
            kept[name] = value
    return kept, dropped


def sanitize_traits(traits: Any) -> dict[str, Any]:
    """列表视图用的宽松版 traits：丢掉未知维度与非法取值，只留能用的。

    ⚠️ **只给「聚合/列表」路径用**（``GET /api/roles``、``/presets``）。
    单角色详情、prompt 预览、diff 仍走 :meth:`RoleSpec.validate` 的严格路径 ——
    那里 400 是**可操作的**（用户改掉那个值就好了），而列表必须能开。

    丢弃时**必须留痕**（调用方负责打 warning）：静默丢弃与
    「用户本来就没配」在页面上长得一模一样，出问题查不到。
    完整理由见 :func:`known_valid_traits`。
    """
    return known_valid_traits(traits)[0]


def from_row(row: Any, parent_traits: dict[str, Any] | None = None) -> RoleSpec:
    """把一行 ``AIProvider`` 装配成 RoleSpec。

    ``parent_traits`` 是母版的**声明** traits；传了就说明这是一份派生角色，
    它的 traits 列存的是 overlay，需要叠加在母版声明之上。
    不传（原创角色 / 平台预设）则 traits 列本身就是完整声明，overlay 为空。

    两种情况下都做归一：老库的 JSON 列可能是 NULL（MySQL 的 JSON
    列不允许 DEFAULT，见 schema_patch），也可能是人工改库留下的非 dict 值；
    而下游一律按 dict 用，不归一会在某个很远的地方炸一个
    ``NoneType has no attribute get`` 或 ``TypeError``。
    """
    stored = _traits_dict(getattr(row, "traits", None))
    is_preset = getattr(row, "record_type", "") == RECORD_TYPE_PRESET
    if parent_traits is None:
        declared, overlay = stored, {}
    else:
        declared, overlay = _traits_dict(parent_traits), stored
    guard = _traits_dict(getattr(row, "guardrails", None))
    # ⚠️ hard 段在这里就过滤掉未知键，而不是等到编译那一刻才由
    # regenerate_locked 兜底。理由与guardrails.filter_known_hard 一致：
    # 让「spec 里看到的」恒等于「常量目录里的」，未经审核的条目不会在内存里
    # 跟着 spec 游走到某个将来会直接读它的展示/审计代码里。
    # 这里**只过滤内存副本、不回写数据库** —— 脏数据的清理由写路径
    # （guardrails.merge_stored）负责，避免「库里有、读出来没了」。
    # 局部导入：guardrails 不依赖 spec，但 persona/__init__ 会在包层面
    # 导出两者，模块层直接 import 会让 spec 与 guardrails 的加载顺序耦合。
    from .guardrails import filter_known_hard

    return RoleSpec(
        role_id=int(row.id),
        name=str(row.name or ""),
        system_prompt=str(row.system_prompt or ""),
        description=str(row.description or ""),
        tts_voice=str(row.tts_voice or ""),
        traits_declared=declared,
        traits_overlay=overlay,
        guardrails_hard=filter_known_hard(guard.get("hard")),
        guardrails_soft=list(guard.get("soft") or []),
        is_preset=is_preset,
        parent_id=getattr(row, "parent_id", None),
    )


def default_spec(name: str = catalog.PRESET_ROLE_NAME,
                 role_id: int = 0) -> RoleSpec:
    """构造一个「纯代码」的 spec，不依赖任何 DB 行。

    两个用途：
      1. 全新部署时 seed 平台预设角色；
      2. **eval 套件**在没有任何库的情况下编译 prompt 做断言 ——
         医疗红线是否齐全这类不变量，必须能在 CI 的纯函数层验证，
         不能依赖"先把库跑起来"。
    """
    return RoleSpec(
        role_id=role_id,
        name=name,
        system_prompt=catalog.DEFAULT_PRESET_PROMPT,
        traits_declared=dict(catalog.DEFAULT_TRAITS),
        guardrails_hard=[],
        guardrails_soft=[],
        is_preset=True,
    )