"""QA：独立验证两件事（工程师的自测结论我不采信，直接实测）

1. **隐私**：``_log_dirty`` 声称「只记类型与长度，不记内容」——
   库里可能存着家属写的话，打进日志等于把用户内容抄进日志系统。
   这是**隐私主张**，必须实测：灌一段带可识别信息的脏值，抓日志确认没泄漏。
2. **留痕**：脏数据必须**留下可定位的痕迹**（QA-2 建议 3）——
   静默丢弃与「用户本来就没配」在页面上长得一样，运维会误判。
   所以既要「不记内容」，也要「记得住是哪一行」。
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

SECRET = "奶奶的降压药-secret-98765"
FAILS: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        FAILS.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def main() -> int:
    print("=" * 72)
    print("验证：脏值留痕是否「记类型不记内容」+ 是否可定位")
    print("=" * 72)

    # ---------- 1. 隐私：内容不得进日志 ----------
    print("\n[1] 隐私：把可识别的家属内容塞进脏值，抓日志")
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setLevel(logging.WARNING)
    lg = logging.getLogger("elder.persona")
    lg.addHandler(h)
    lg.setLevel(logging.WARNING)

    from app.persona import guardrails as g

    # 构造「合法 JSON 但类型不对、且内容含可识别信息」的值
    dirty = [SECRET, "另一句私密的话"]
    g.as_dict(dirty, role_id=42, column="traits")
    g.as_list(dirty, role_id=42, column="guardrails")
    log = buf.getvalue()
    lg.removeHandler(h)

    print(f"  日志内容：{log.strip()[:200]}")
    ok("日志不含家属原始内容（secret 未出现）", SECRET not in log)
    ok("日志不含任何私密原文", "另一句私密的话" not in log)
    ok("日志记了类型（便于判断是哪种脏）", "list" in log, "期望含 list")
    ok("日志记了长度（便于判断规模）", "长度=2" in log or "2" in log)
    ok("日志记了 role_id（可定位到行）", "42" in log)
    ok("日志记了列名（可定位到字段）", "traits" in log)

    # ---------- 2. None 不该刷日志 ----------
    print("\n[2] None 是「没设过」，不该刷日志（否则日志被无意义淹没）")
    buf2 = io.StringIO()
    h2 = logging.StreamHandler(buf2)
    lg2 = logging.getLogger("elder.persona")
    lg2.addHandler(h2)
    g.as_dict(None, role_id=1, column="traits")
    g.as_list(None, role_id=1, column="guardrails")
    g.as_dict({}, role_id=1, column="traits")
    g.as_list([], role_id=1, column="guardrails")
    log2 = buf2.getvalue()
    lg2.removeHandler(h2)
    ok("None / 空容器不刷日志", log2.strip() == "", f"日志={log2.strip()[:80]!r}")

    # ---------- 3. 归一结果正确性（不能因为留痕而改行为）----------
    print("\n[3] 归一结果必须仍是「当它没有」")
    ok("as_dict([..]) -> {}", g.as_dict([1, 2, 3]) == {})
    ok("as_dict(42) -> {}", g.as_dict(42) == {})
    ok("as_dict('x') -> {}", g.as_dict("x") == {})
    ok("as_dict(None) -> {}", g.as_dict(None) == {})
    ok("as_list({..}) -> []", g.as_list({"a": 1}) == [])
    ok("正常 dict 原样返回", g.as_dict({"verbosity": "short"}) == {"verbosity": "short"})
    ok("正常 list 原样返回", g.as_list([{"id": "no_politics"}]) == [{"id": "no_politics"}])

    # ---------- 4. 兼容性：单参数调用不能被新 kwargs 破坏 ----------
    print("\n[4] 单参数调用向后兼容（模块内有 6 处内部调用只传一个值）")
    try:
        g.as_dict({"a": 1})
        g.as_list([1])
        g.as_dict([1], role_id=1)
        g.as_dict([1], column="x")
        g.as_dict([1], role_id=1, column="x")
        ok("各种调用形态均可用", True)
    except Exception as exc:  # noqa: BLE001
        ok("各种调用形态均可用", False, f"{type(exc).__name__}: {exc}")

    print("\n" + "=" * 72)
    print(f"==== {'全部通过' if not FAILS else f'{len(FAILS)} 条失败'} ====")
    for f in FAILS:
        print(f"  FAILED: {f}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())