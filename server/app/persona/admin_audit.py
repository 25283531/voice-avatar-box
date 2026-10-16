"""管理端**人格预设**操作审计：记录的组装与写入。

⚠️ 本模块**只负责审计记录的组装与写入，不含任何业务判定**
----------------------------------------------------------------------
「某个字段能不能改」「这个值合不合法」「这次操作要不要写审计」——一律由
调用方裁决。本模块只做三件事：

1. 比对调用方给的 ``before`` / ``after``，算出**逐字段 from→to**；
2. 按固定口径格式化（traits 拆到维度、软约束只记 id、长文本截断）；
3. ``db.add()`` 这条审计记录。

**刻意不做的事**
----------------
* **不 commit。** 审计必须与业务写**同一个事务**（同一 ``db.add`` + 一次
  ``commit``）。分两次 commit 会出现"业务成功但审计丢了"，而审计丢一次就
  永远丢了 —— 那等于没有审计。commit 由调用方在写完业务行之后统一做。
* **不判定字段是否可改。** 这里不做白名单之外的"顺手检查"，因为
  「哪些字段可改」是**产品规则**，它的真源是 API 的入参 schema
  （``PresetPatchIn`` 只有 5 个字段）。审计层再抄一份白名单，等于多一个
  可能与 schema 漂移的副本。
* **不抛异常打断业务。** 审计写不进去（比如 diff 里出现了不可序列化的值）
  必须让业务一起失败，而不是留下"改成功了但没记录" —— 那比失败更坏。
  真要序列化失败，宁可让整个请求 500，也不要静默丢审计。

================================ 本表绝不允许出现的东西 ================================
这张表是**运营可读面**（管理端有审计抽屉），所以下面几类东西一律不进：

* **红线 ``tokens``**（``catalog.HARD_GUARDRAILS[*].tokens``）：它是
  ``guardrails.assert_present()`` 做 fail-closed 自检的**唯一判定基准**。
  写进来 = 把"门禁怎么判"写进一张可被读走并随意篡改的表。
* **硬红线正文**：红线不在可改字段集合里，diff 里天然不会有；但要守住
  —— 防止将来有人"顺手把展示用的 hard 段一起存进来"。
* **任何用户派生角色的内容**：派生角色带 ``owner_id``。抄进来，这张表就
  成了跨用户数据表，而它只受 ``get_current_admin`` 保护 —— 比
  ``ai_providers`` 的行级可见性保护更弱。
* **编译产物全文**（画像摘要、偏好示例、家属写的话）：那是**运行时数据**，
  含用户与家属的私密内容。审计表绝不能成为用户内容的副本。

结构性保障：:data:`TRACKED_FIELDS` 是**白名单**，``record()`` 只格式化
白名单里的键 —— 不是"约定不要传"，而是"传了也不会被记录"。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import models
from ..persona import catalog

#: 审计**只**记录这些字段。刻意用白名单而非黑名单：
#: 新增一个可改字段时，这里必须显式加一项 —— 那样"审计里到底有什么"
#: 就是一份可grep 的清单，而不是"调用方当时传了什么"。
TRACKED_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "system_prompt",
    "tts_voice",
    "traits",
    "soft_guardrails",
    "archived_at",
)

#: ``system_prompt`` 记全文但设上限。审计表是"变化流水"，不是"内容仓库"：
#: 无限长会让"翻最近 20 条审计"这个最常见的操作变慢。
MAX_TEXT_LEN = 2000


def _text(value: Any) -> Any:
    """把值转成**可JSON 序列化**的形式，并在超长时截断。

    两种处理，缺一不可：

    1. **datetime → ISO 8601 字符串**。``archived_at`` 是下架审计要记的字段，
       而它是 ``datetime``：直接塞进 ``diff`` 会让 ``db.commit()`` 时的 JSON
       序列化抛 ``TypeError: Object of type datetime is not JSON serializable``
       —— 端点变成 **500**，下架功能整体不可用（第一次实跑就撞上了，
       根因是只考虑了"长文本要截断"，没考虑"非 JSON 原生类型要转换"）。
    2. **超长 str 截断**并在 diff 项上打 ``_truncated``。

    只对这两类生效；``dict`` / ``list`` / 数字 / None / bool 原样返回
    （traits 与软约束另有格式化路径）。
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and len(value) > MAX_TEXT_LEN:
        return {"_truncated": True, "_len": len(value),
                "text": value[:MAX_TEXT_LEN]}
    return value


def _soft_ids(value: Any) -> list[str]:
    """软约束只记 **id 列表**，不记文案。

    文案是 ``catalog.SOFT_GUARDRAILS`` 的**代码常量**，把它抄进可变表，
    就多了一份"常量与表不一致"的可能，且对"改了什么"的回答毫无帮助。
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


def build_diff(before: dict[str, Any] | None,
               after: dict[str, Any] | None) -> tuple[list[str], dict[str, Any]]:
    """比对前后值，返回 ``(changed_fields, diff)``。

    - 标量字段：``{"字段名": {"from": 旧值, "to": 新值}}``
    - ``traits``：**拆到维度**，键是 ``traits.<name>``。整块记的话，
      "只改了话多程度"与"10 项全改"在审计里长得一样，无法定位。
    - ``soft_guardrails``：整块记 id 列表（勾选型操作，逐项 diff 是噪音）。
    - 值相同的字段**不进 diff**（changed_fields 只列真正变了的）。

    ⚠️ ``before`` / ``after`` 里不在 :data:`TRACKED_FIELDS` 的键一律忽略。
    """
    before = before or {}
    after = after or {}
    changed: list[str] = []
    diff: dict[str, Any] = {}

    for field in TRACKED_FIELDS:
        if field == "traits":
            old_traits = before.get("traits") if isinstance(before.get("traits"), dict) else {}
            new_traits = after.get("traits") if isinstance(after.get("traits"), dict) else {}
            for name in sorted(set(old_traits) | set(new_traits)):
                old_v = old_traits.get(name)
                new_v = new_traits.get(name)
                if old_v != new_v:
                    key = f"traits.{name}"
                    changed.append(key)
                    diff[key] = {"from": old_v, "to": new_v}
            continue

        if field == "soft_guardrails":
            old_ids = _soft_ids(before.get("soft_guardrails"))
            new_ids = _soft_ids(after.get("soft_guardrails"))
            if old_ids != new_ids:
                changed.append(field)
                diff[field] = {"from": old_ids, "to": new_ids}
            continue

        if field not in before and field not in after:
            continue
        old_v = before.get(field)
        new_v = after.get(field)
        if old_v == new_v:
            continue
        changed.append(field)
        diff[field] = {"from": _text(old_v), "to": _text(new_v)}

    return changed, diff


def snapshot(row: models.AIProvider) -> dict[str, Any]:
    """取一行当前的**可审计快照**（调用方在改之前先存一份）。

    刻意只取 :data:`TRACKED_FIELDS` 里的字段：软约束要从 ``guardrails`` 列
    里归一出来，而红线段（hard）**根本不取** —— 它是代码常量、与行无关，
    取了只会给人"审计能记红线"的错觉。
    """
    from ..persona import guardrails

    stored = guardrails.as_dict(getattr(row, "guardrails", None))
    soft = [str(item.get("id")) for item in
            guardrails.normalize_soft(guardrails.as_list(stored.get("soft")))
            if isinstance(item, dict) and item.get("id")]
    return {
        "name": str(row.name or ""),
        "description": str(row.description or ""),
        "system_prompt": str(row.system_prompt or ""),
        "tts_voice": str(row.tts_voice or ""),
        "traits": dict(row.traits) if isinstance(row.traits, dict) else {},
        "soft_guardrails": soft,
        "archived_at": row.archived_at,
    }


def record(db, admin: models.User, row: models.AIProvider, action: str,
           before: dict[str, Any] | None,
           after: dict[str, Any] | None) -> models.AdminPresetAudit | None:
    """组装并 ``db.add()`` 一条审计。**不 commit**（理由见模块注释）。

    :param action: ``update`` / ``reset`` / ``archive``
    :param before: 改动前的 :func:`snapshot`
    :param after: 改动后的 :func:`snapshot`
    :returns: 审计行；**没有任何字段真的变化时返回 None** —— 不制造空动作
        记录（"恢复动作不算变更"是日志系统的思路，不是审计的思路；但反过来，
        一条什么都没变的审计只是噪音，会把真审计淹掉）。

    ⚠️ ``admin_username`` 记的是**快照**：用户改名后，审计仍要能回答
    "当时是谁改的"。
    """
    changed, diff = build_diff(before, after)
    if not changed:
        return None
    audit = models.AdminPresetAudit(
        preset_id=int(row.id),
        admin_id=int(admin.id),
        admin_username=str(admin.username or ""),
        action=action,
        changed_fields=changed,
        diff=diff,
        guardrail_version=catalog.GUARDRAIL_VERSION,
    )
    db.add(audit)
    return audit