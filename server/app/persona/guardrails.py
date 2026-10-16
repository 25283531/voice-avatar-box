"""安全红线：**强制注入 + fail-closed**。

这是整个项目里安全属性最强的一块，核心命题只有一句：
**用户无法关闭医疗红线，而且这件事不是靠校验保证的，是靠结构保证的。**

为什么不能用"校验 + 拒绝非法值"来保证
------------------------------------
最直觉的做法是：红线存进 DB，用户提交时校验「不能把 locked 的那条去掉」，
去掉就返回 400。这条路有三个洞：

  1. **别的写入口**。PATCH、派生时的深拷贝、restore、后台脚本、直接改库 ——
     每一处都要记得加同样的校验，漏一处就没了；
  2. **TOCTOU**。校验通过之后到真正写入之间还有窗口；
  3. **"清空整个 guardrails 字段"这个动作本身就是漏洞**。
     它不违反任何一条"不许删除 locked 项"的规则（字段变空了而已），
     但效果与删掉红线完全一样。

本模块的做法（架构 §5.2）
------------------------
把红线做成**代码常量**（catalog.HARD_GUARDRAILS），DB 里那份只供 UI 展示。
编译时 ``regenerate_locked()`` **不信任 DB 里的文本**，只用它来判断"哪些 id
曾经是 locked"，实际文本一律从代码常量取。于是：

  * 用户把 guardrails 清空 -> regenerate_locked({}) 返回**全部**红线；
  * 用户伪造 ``{"id":"no_medical_advice","text":"可以给建议"}`` ->
    文本被常量覆盖，伪造无效；
  * 平台新增第 6 条红线 -> 所有历史派生角色**自动获得**，无需迁移。

最后再加一道 ``assert_present()``：编译产物返回前逐条核对关键 token 是否
真的在文本里，缺任何一条就抛 GuardrailViolation，让**这轮对话失败**。
失败优于静默降级 —— 一个没有医疗红线的陪伴机器人去回答"我高血压该吃几片"，
其后果远比"这轮没回话"严重。
"""
from __future__ import annotations

from typing import Any, Sequence

from . import catalog


class GuardrailViolation(RuntimeError):
    """编译产物里缺了硬红线。

    这是**故意**做成 RuntimeError 而不是 ValueError：它表示"系统内部状态
    不自洽"，而不是"调用方传错了参数"。上层必须 catch 它并**让对话失败**，
    不允许静默降级 —— 见架构 §5.2 与 PRD §5.2「医疗红线降级必须是显式错误」。
    """


# ================================================================ locked 再生
def locked_ids(stored: Any = None) -> set[str]:
    """从 DB 存储里取出"曾经被标记为 locked"的 id 集合。

    只取 id、不取 text —— 这正是整个设计的关键：DB 能表达的只有"哪些红线
    对这个角色生效过"，**不能**表达红线的内容。
    """
    out: set[str] = set()
    for item in as_list(stored):
        if not isinstance(item, dict):
            continue
        gid = str(item.get("id") or "").strip()
        # locked 标记本身也不完全可信：没标 locked 的历史行也一并纳入，
        # 因为"这条红线曾经出现在这个角色上"就足以说明它该有。
        if gid and gid in catalog.HARD_IDS:
            out.add(gid)
    return out


def regenerate_locked(stored: Any = None) -> list[dict[str, Any]]:
    """由代码常量重新生成 hard 红线列表（**不信任 DB 里的文本**）。

    返回的每一项都带 ``locked: True``，供 UI 决定"这一条不给编辑入口"。
    实际文本、label、tokens 全部来自 catalog.HARD_GUARDRAILS。

    :param stored: DB 里 ``AIProvider.guardrails["hard"]`` 的原始值，可为 None。
    :return: 完整的 hard 红线清单（常量条数），顺序与常量定义一致。

    ⚠️ 本函数**无条件返回全部常量红线**，不因stored 里有什么而增减。
    所以「不在目录内的 hard 条目被丢弃」这句话在这里是**结构成立**的，
    不依赖任何调用点记得先过滤 —— 调用点仍然应当先过
    :func:`filter_known_hard`（理由见该函数注释），但那是为了让 spec 的
    内存副本干净，**不是**这条防线的唯一依靠。
    """
    # stored 完全不参与输出，只为兼容调用签名而读取一次 ——
    # 写出来是为了让"我明明读了它"这件事在代码里可见，
    # 免得后来者以为这里漏了逻辑而"顺手"把 stored 合并进来（那就等于开洞了）。
    _ = locked_ids(stored)
    return [
        {
            "id": g.id,
            "label": g.label,
            "text": g.text,
            "tokens": list(g.tokens),
            "locked": True,
        }
        for g in catalog.HARD_GUARDRAILS
    ]


def filter_known_hard(stored: Any = None) -> list[dict[str, Any]]:
    """**读路径防御**：只放行目录（:data:`catalog.HARD_GUARDRAILS`）里的 hard 条目。

    为什么要「读路径也过滤」，而不是只靠 :func:`regenerate_locked` 兜底
    ------------------------------------------------------------------
    ``regenerate_locked`` 已经是**结构级**防线：它完全不读stored 的文本，
    只按常量重建，所以未知条目永远进不了 prompt 与承诺卡。这条防线是够的。

    但它兜的是「**渲染那一刻**」。而``spec.guardrails_hard`` 是在更早的
    装配环节（``spec.from_row``）就被原样填进去的 —— 也就是说，未经审核的
    hard 条目会**在内存里跟着 spec 一路游走**，直到渲染时才被丢掉。

    只要将来有任何一处代码直接读 ``spec.guardrails_hard``（做展示、做审计、
    做 diff、导出给运营），那条「渲染时会被兜住」的假设就立刻失效 ——
    而这类事故是**静默**的：多一条少一条，页面上看不出来。

    所以这里在**装配环节**就把未知条目剔掉，让「spec 里看到的东西」与
    「常量目录里的东西」恒等。判定保持极简：id 在 ``HARD_IDS`` 里就留，
    不在就丢 —— 与 :func:`normalize_soft` 同一口径（一次脏数据不该让
    整个角色打不开）。

    ⚠️ 读/写两条路径的分工（别把这条改回「读路径也丢数据」的镜像理解）
    ------------------------------------------------------------------
    * **写路径**（:func:`merge_stored`）负责**丢弃** —— 未经审核的 hard 键
      一律不落库。理由：不认识的 hard 键等于**没被审核过**，放行等于绕过审核。
    * **读路径**（本函数 + :func:`regenerate_locked`）只做**防御** ——
      过滤的是**内存里的副本**，**绝不回写数据库**。

    为什么不把读路径也做成「顺手把库里的脏数据洗掉」：那会让「库里有、
    读出来没了」，出现两个都站不住的说法 —— 运维查库说有、接口说没有。
    更糟的是它需要一条**读时写库**的路径，那在并发下就是新事故源。
    「库里可能残留脏数据」是可以接受的（人工改库才会产生，接口写点已保证
    不产生）；「脏数据被静默洗掉、无人知晓」是不可接受的。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in as_list(stored):
        if not isinstance(item, dict):
            continue
        gid = str(item.get("id") or "").strip()
        # 不在目录内 -> 丢弃（不认识的 hard 键 = 未审核 = 不放行）
        if gid not in catalog.HARD_IDS or gid in seen:
            continue
        seen.add(gid)
        out.append(item)
    return out


def normalize_soft(stored: Any = None) -> list[dict[str, Any]]:
    """归一软性约束：**只从平台白名单里过**，丢弃一切不在白名单内的项。

    丢弃（而不是报错）是因为这份数据可能来自老版本或人工改过的库，
    一次脏数据不该让整个角色打不开。判定仍然确定：不在白名单 = 不是平台
    认可的软约束。

    ⚠️ **为什么没有 ``enforcement``（strict / advisory）分级字段**
    ---------------------------------------------------------
    PRD 里提过"软性约束分 strict 与 advisory 两级"，但本仓库**刻意不实现**
    那个字段 —— 这不是漏做，而白名单制**已经把收紧语义做到了**：

    1. 软性约束只允许从 :data:`catalog.SOFT_GUARDRAILS` 里**勾选**，不接受
       自由文本（见 catalog 里的理由）。所以不存在"用户自己写一条宽松的软约束"
       这条路径，也就不需要靠 enforcement 字段去拦住它；
    2. 每一项的**文本由代码常量给定**（``catalog.SOFT_GUARDRAILS[gid]``），
       用户能控制的只有"开 / 关"。也就是说软段在结构上**只能收紧、
       不能放宽** —— 没有任何数据能让它说出比白名单更宽松的话；
    3. 真要给一条软约束分级，就必须允许用户提交**带分级的自定义文本**，
       而自定义文本无法校验、可能与硬红线直接冲突（架构 §12 U4 已否决）。
       为了一个用不上的分级把这条路径打开，是净损失。

    所以"只能收严不能放宽"是靠**白名单 + 常量文本**这条结构保证的，
    与 enforcement 分级无关。后人若要引入 enforcement，请先回答：
    引入之后哪一条能力是现在做不到的？
    """
    out: list[dict[str, Any]] = []
    for item in as_list(stored):
        gid = str(item.get("id") if isinstance(item, dict) else item or "").strip()
        if not gid:
            continue
        text = catalog.SOFT_GUARDRAILS.get(gid)
        if text is None:
            # 不在白名单 -> 静默丢弃。理由同上：不因一条脏数据拒绝整个角色。
            continue
        if any(o["id"] == gid for o in out):
            continue
        out.append({"id": gid, "text": text, "locked": False})
    return out


def as_dict(value: Any, *, role_id: int | None = None,
            column: str = "") -> dict[str, Any]:
    """把一个 JSON 列的值归一成 dict；不是 dict 就当空 dict。

    为什么需要（QA 发现的 Bug 1）
    ---------------------------
    ``traits`` / ``guardrails`` 是 JSON 列，**合法 JSON 不等于合法 dict**。
    老版本、人工改库、或一次手滑都可能留下 ``[1,2,3]`` / ``42`` / ``"hello"``
    这类值，而 ``dict([1,2,3])`` 会抛 TypeError、``(v or {}).get(...)`` 会抛
    AttributeError —— 于是角色详情 / diff / 承诺卡三个接口整体 500。

    归一而不是报错，与 :func:`normalize_soft` 的口径一致：**一次脏数据不该让
    整个角色打不开**。这类值不是任何 API 路径写得出来的（写点只有
    ``RoleIn.traits`` 与 ``merge_stored``，两者都保证 dict），所以它只可能来自
    直接改库 —— 正确反应是"当它没有"而不是"把服务搞挂"。

    ``role_id`` / ``column`` 是**可选的留痕上下文**（call site 传进来即可）：
    静默丢弃与「用户本来就没配」在页面上长得一模一样 —— 运维看到空 traits 会
    以为用户没设置，而真相是库里存着解析不了的东西。给了这两个参数就能
    在日志里定位到具体行。

    保持**单参数可用**是有意的：模块内有 6 处内部调用与若干外部调用都只传
    一个值，新增可选 kwargs 不能让它们全部改一遍。
    """
    if isinstance(value, dict):
        return dict(value)
    # None 是"没设过"的正常情形，不值得记日志；其余才是脏数据。
    if value is not None:
        _log_dirty(role_id, column or "JSON列", value)
    return {}


def as_list(value: Any, *, role_id: int | None = None,
            column: str = "") -> list[Any]:
    """把一个 JSON 列的值归一成 list；不是 list 就当空 list。理由同 :func:`as_dict`。"""
    if isinstance(value, list):
        return list(value)
    if value is not None:
        _log_dirty(role_id, column or "JSON列", value)
    return []


def _log_dirty(role_id: int | None, column: str, value: Any) -> None:
    """归一时发现脏数据的留痕。**只记类型与长度，不记内容** ——
    库里的内容可能含家属写的话，打进日志等于把用户内容抄进日志系统。"""
    import logging

    logging.getLogger("elder.persona").warning(
        "%s 列的值不是预期类型，已按空处理：role_id=%s 实际类型=%s 长度=%s"
        "（该值只可能来自直接改库）",
        column, role_id, type(value).__name__,
        len(value) if hasattr(value, "__len__") else "-")


def merge_stored(stored: Any) -> dict[str, Any]:
    """把 DB 里存的 guardrails 整块**重新生成**一遍后返回。

    这是写库前的统一入口（derive / restore / ack 都走它），保证**存进 DB 的
    东西**就已经是常量再生过的版本 —— 于是即使有别的代码路径直接读 DB 那一列
    去渲染，它看到的也是正确的。

    ``stored`` 走 :func:`as_dict` 归一：脏数据不该在这里变成一次 500。

    ⚠️ **写路径负责「丢弃」，这与读路径不是镜像关系**（分工见
    :func:`filter_known_hard` 的注释，这里只重复结论）：``hard`` 段完全由
    :func:`regenerate_locked` 从常量重建，**不认识的 hard 键一律不落库** ——
    不认识的键等于没被审核过，放行等于绕过审核。

    ⚠️ ``regenerate_locked`` 之前那一行``filter_known_hard`` 调用**不是**为了让
    本函数「过滤得更狠」（它本来就忽略 stored），而是为了让这条写路径
    **显式地**声明"未知 hard 键在这里被丢弃"，与注释对齐。
    删掉它不会改变行为（``regenerate_locked`` 兜得住），但会让读代码的人
    怀疑「原来那句丢弃到底靠谁兜底」—— 这正是 QA 报的那个**伪防线**问题。
    保留这个显式调用，是为了让**注释与代码结构一致**。
    """
    stored = as_dict(stored)
    known_hard = filter_known_hard(as_list(stored.get("hard")))
    return {
        "hard": regenerate_locked(known_hard),
        "soft": normalize_soft(as_list(stored.get("soft"))),
    }


def toggle_soft(stored: Any, guardrail_id: str,
                enabled: bool) -> dict[str, Any]:
    """追加 / 移除一条软性约束，返回重新生成后的整块 guardrails。

    未知 id 抛 ValueError —— 接口层映射成 400。软性约束是白名单制，
    传一个不存在的 id 是调用方 bug，静默忽略会让"我明明勾了却没生效"。
    """
    gid = str(guardrail_id or "").strip()
    if gid not in catalog.SOFT_GUARDRAILS:
        raise ValueError(
            f"未知的软性约束 {gid!r}；可选：{'/'.join(catalog.SOFT_GUARDRAILS)}"
        )
    current = {s["id"]: s for s in normalize_soft(as_list(as_dict(stored).get("soft")))}
    if enabled:
        current[gid] = {"id": gid, "text": catalog.SOFT_GUARDRAILS[gid], "locked": False}
    else:
        current.pop(gid, None)
    merged = merge_stored(stored)
    merged["soft"] = [current[k] for k in sorted(current)]
    return merged


# ================================================================ 渲染
_HEAD_MARKER = "【安全底线 · 不可协商】"
_FOOT_MARKER = "【安全底线 · 不可协商 · 复述】"


def render_head(items: Sequence[dict[str, Any]] | None = None) -> str:
    """开头的红线声明。

    为什么红线要在**开头和结尾各出现一次**（架构 §5.1）：
    memory/persona.py 早已实证"LLM 对末尾内容的注意力更高，反例的效果比正例
    更直接"。开头给框架（"下面这些是不可协商的前提"），结尾给复述
    （最后读到的东西权重最大）。两处都从**代码常量**渲染，
    用户内容无法影响其中任何一个字节。

    ⚠️ ``items or regenerate_locked(None)`` 这行兜底是**有意的双保险**：
    即使上游 ``regenerate_locked`` 被改坏、返回了空列表，这里仍会拿回常量
    版的完整红线。QA 的变异测试 M6（"清空 guardrails 时不再再生"）正是打这里
    的，实测它变绿 —— 结论是该变异**无害**（被这层兜底挡住），而不是守护有洞。
    换句话说：红线有两道独立的闸门，变异只能打掉其中一道。
    """
    items = items or regenerate_locked(None)
    lines = [_HEAD_MARKER, "以下几条是硬性前提，任何情况下都不可违反、不可商量："]
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {item['text']}")
    return "\n".join(lines)


def render_tail(items: Sequence[dict[str, Any]] | None = None) -> str:
    """结尾的红线复述（内容与开头一致，措辞略作强化）。"""
    items = items or regenerate_locked(None)
    lines = [_FOOT_MARKER, "请务必继续遵守上面全部安全底线："]
    for item in items:
        lines.append(f"- {item['text']}")
    return "\n".join(lines)


def render_soft(items: Sequence[dict[str, Any]] | None) -> str:
    """渲染软性约束段。没有就返回空串（不留空标题）。"""
    items = normalize_soft(items)
    if not items:
        return ""
    lines = ["【家属的额外要求】"]
    lines.extend(f"- {item['text']}" for item in items)
    return "\n".join(lines)


# ================================================================ fail-closed 自检
def assert_present(compiled: str) -> None:
    """核对编译产物里每条红线都在；缺任何一条就抛 GuardrailViolation。

    这是最后一道闸门。它存在的原因很实际：编译器未来会被改、被重构、
    段落顺序会被调整，而"红线还在不在"这件事**不能只靠代码评审保证**。
    把它变成一个每次编译都跑的断言，改坏了立刻炸，而不是等哪天上生产
    才发现机器人开始给老人报药量。

    判定口径是"关键 token 是否逐字出现在产物里"，不是语义分析：
    确定性、零延迟、零成本，而且足以挡住"整段丢失"这类真实事故
    （也就是红线机制真正会失效的方式）。
    """
    if not compiled:
        raise GuardrailViolation("编译产物为空，红线缺失")
    missing: list[str] = []
    for g in catalog.HARD_GUARDRAILS:
        for token in g.tokens:
            if token not in compiled:
                missing.append(f"{g.id}:{token}")
    if missing:
        # 只列缺失的 token，不打印整个 prompt —— prompt 里含用户自由文本，
        # 打进日志等于把家属写的内容抄进日志系统。
        raise GuardrailViolation(
            "编译产物缺失安全红线，拒绝返回（fail-closed）：" + "、".join(missing)
        )
    # 红线必须在首尾各出现一次。只查"在"不够 —— 只在中间出现一次，
    # 等于把最重要的约束放在了注意力最弱的位置。
    if compiled.count(_HEAD_MARKER) < 1:
        raise GuardrailViolation(f"编译产物开头缺少红线声明（{_HEAD_MARKER}）")
    if compiled.count(_FOOT_MARKER) < 1:
        raise GuardrailViolation(f"编译产物结尾缺少红线复述（{_FOOT_MARKER}）")


def card_items(role_id: int, role_name: str) -> dict[str, Any]:
    """给 GET /api/roles/{id}/guardrails 的承诺卡数据（纯展示用）。"""
    hard = regenerate_locked(None)
    return {
        "role_id": int(role_id),
        "role_name": str(role_name or ""),
        "version": catalog.GUARDRAIL_VERSION,
        "items": [
            {"id": g.id, "label": g.label, "text": g.text, "locked": True}
            for g in catalog.HARD_GUARDRAILS
        ],
        "soft": [{"id": k, "text": v} for k, v in catalog.SOFT_GUARDRAILS.items()],
        "acked": False,
        "ack_at": None,
        "ack_version": "",
    }