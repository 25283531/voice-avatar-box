"""把存量角色 / 形象的 tts_voice 归一化到**当前生效的 TTS 厂商**。

为什么需要一次性改存量
--------------------
生产库现在真实躺着失效值：角色 #8（家庭自建角色）与全部平台预设都存着
``zh-CN-XiaoxiaoNeural``（Edge 的音色名），而本部署的 TTS 走**腾讯云**
（``tts_voice='601009'``，priority 6）。

``asr_tts._shape_ok_voice()`` 发现形态与目标厂商不符时会**静默置空** ——
这些音色从来没生效过。写路径（``roles._clamp_voice``）已经修好，但**存量行
不会自己变**：用户每次打开下拉，前端都只能提示「已自动改为默认」，
而那条提示本身就是"你的选择从来没生效"的另一种说法。所以一次性改掉。

在容器里跑（库路径由容器内的 app.db 解析，即 ``/data/elder.db``）：
    docker exec -i elder-server python - < build-out/normalize_voices.py

默认**只预览**，不改任何数据。加 `CONFIRM=1` 才真写：
    docker exec -e CONFIRM=1 -i elder-server python - < build-out/normalize_voices.py

判据**复用** ``app.routers.roles._clamp_voice``，不在这里另写一份：
两处判据分叉的后果是「脚本说改好了、接口又改回去」（或反过来），
而那类问题最难查 —— 因为两个地方单独看都是对的。
"""
from __future__ import annotations

import json
import os

from sqlalchemy import select

from app import models
from app.db import SessionLocal
from app.routers.roles import _clamp_voice
from app.voice import catalog as voice_catalog

CONFIRM = os.getenv("CONFIRM") == "1"

#: 容器里预期落在的库（仅用于把"跑错库"这件事在输出里点出来）
EXPECTED_DB = "/data/elder.db"


def _row_dict(row) -> dict:
    """整行导出（备份用）。``default=str`` 兜住 datetime 这类不可序列化值。"""
    return {c.name: getattr(row, c.name, None) for c in row.__table__.columns}


def _collect(db) -> list[tuple[str, object, str, str]]:
    """扫「预设 + 角色」与「形象」两类行，返回 ``[(标签, 行, 原值, 新值)]``。

    只含**需要改**的行。标签用 ``presets`` / ``roles`` / ``avatars`` ——
    前两者物理上同在 ``ai_providers`` 表，但对运维来说是两种东西，
    混成一个计数会让人看不出"平台预设也被改了"。
    """
    todo: list[tuple[str, object, str, str]] = []

    rows = db.scalars(
        select(models.AIProvider)
        .where(models.AIProvider.record_type.in_(
            # ⚠️ 平台预设与用户角色**同在 ai_providers 表**，只按 role 过滤会漏掉
            # 预设 —— 而预设的音色恰恰是出厂值（Edge 名），在腾讯云部署下同样失效，
            # 且它会被 derive 复制给每一个新角色。漏掉它 = 改了一半。
            (models.RECORD_TYPE_PRESET, models.RECORD_TYPE_ROLE)))
        .order_by(models.AIProvider.id)).all()
    for row in rows:
        old = str(row.tts_voice or "")
        new = _clamp_voice(db, old)
        if new != old:
            label = ("presets" if row.record_type == models.RECORD_TYPE_PRESET
                     else "roles")
            todo.append((label, row, old, new))

    avatars = db.scalars(select(models.Avatar).order_by(models.Avatar.id)).all()
    for row in avatars:
        old = str(row.tts_voice or "")
        new = _clamp_voice(db, old)
        if new != old:
            todo.append(("avatars", row, old, new))

    return todo


def main() -> int:
    db = SessionLocal()
    try:
        db_path = str(db.get_bind().url.database or "")
        opts = voice_catalog.options_for(db)
        print(f"库：{db_path}" + ("" if db_path == EXPECTED_DB else
                                 f"   ⚠️ 预期是 {EXPECTED_DB}，请确认没跑错库"))
        print(f"生效 TTS 厂商：{opts.get('vendor') or '（自建/认不出）'}"
              f"  默认音色：{opts.get('default_voice') or '（无）'}")
        if opts.get("allow_custom"):
            print("当前是自建 / 认不出厂商的 TTS：音色名无从校验，**本脚本不会改动任何行**。")
            return 0

        todo = _collect(db)
        counts = {k: sum(1 for t, *_ in todo if t == k)
                  for k in ("presets", "roles", "avatars")}
        summary = " / ".join(f"{k} {v} 条" for k, v in counts.items() if v)
        print(f"\n合计 {len(todo)} 条待归一化（{summary or '无'}）\n")

        if not todo:
            print("没有需要改的行。")
            return 0

        print("── 逐条（表 / 主键 / 名称 / 原值 → 新值）──")
        for table, row, old, new in todo:
            print(f"  {table} #{row.id}「{row.name or ''}」  {old!r} -> {new!r}")

        backup = [{"table": t, "row": _row_dict(row), "old": old, "new": new}
                  for t, row, old, new in todo]
        print("\n── 备份 JSON（回滚依据：改前的完整行）──")
        print(json.dumps(backup, ensure_ascii=False, indent=1, default=str))

        if not CONFIRM:
            print("\n[预览模式] 没有改动任何数据。要真改请加 CONFIRM=1。")
            print("（日志里那些 '已回落到默认' 的 WARNING 来自共用的 "
                  "_clamp_voice 判据，预览模式下**并未写入**。）")
            return 0

        for _table, row, _old, new in todo:
            row.tts_voice = new
        db.commit()
        print(f"\n已写入 {len(todo)} 条（{summary}）。")

        left = _collect(db)
        print(f"复核：仍有 {len(left)} 条不一致"
              + ("（应为 0；若不为 0 说明写后又被别的东西改回去了）" if left else ""))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
