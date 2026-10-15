"""E4：只削弱 catalog 的 tokens（正文不动）—— 检验黄金副本是否真的兜住 token 层。

背景：黄金副本逐字钉住的是 ``text``。但 json 里也存了一份 ``tokens``，
而 E3 已证明**没有任何断言比对它**。
若有人只把 catalog 的 tokens 削弱（删掉否定式 token），text 不变 ->
golden copy 匹配、assert_present 查的也更少 -> eval 可能仍绿。
那意味着「同源自证」在 **token 层**仍未被打破。
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

SB = Path(r"C:\Users\Administrator\AppData\Local\Temp\qa3_sandbox")
PRISTINE = Path(r"C:\Users\Administrator\AppData\Local\Temp\qa3_pristine")
PY = r"C:/Users/Administrator/.workbuddy/binaries/python/envs/elder/Scripts/python.exe"
CATALOG = SB / "server/app/persona/catalog.py"

REL = ("app/persona/guardrails.py", "app/persona/catalog.py",
       "app/persona/compiler.py", "app/persona/spec.py",
       "evals/redline_baseline.json", "evals/redline_baseline.py",
       "evals/suites/xiaozhi.json", "evals/suites/invariants.json")


def restore() -> None:
    for rel in REL:
        src = PRISTINE / rel
        if src.exists():
            shutil.copy2(src, SB / "server" / rel)


def run() -> tuple[int, str]:
    t = tempfile.mkdtemp()
    env = dict(os.environ, TMPDIR=t, TEMP=t, TMP=t, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([PY, "tools/eval_persona.py"], cwd=str(SB),
                       capture_output=True, text=True, encoding="utf-8",
                       env=env, timeout=900)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


OLD_TOK = 'tokens=("不给医疗建议", "医生", "家人", "不给结论"),'
NEW_TOK = 'tokens=("提醒",),'   # 只留一个毫无约束力的词


def e4() -> str:
    src = CATALOG.read_text(encoding="utf-8")
    assert OLD_TOK in src, "E4 未命中 tokens 锚点"
    CATALOG.write_text(src.replace(OLD_TOK, NEW_TOK, 1), encoding="utf-8")
    ast.parse(CATALOG.read_text(encoding="utf-8"))
    return ("catalog：医疗红线的 tokens 从 4 个（含否定式「不给结论」）"
            "削到 1 个无约束力的「提醒」—— **正文一字未改**")


def e5() -> str:
    """再加一层：tokens 削弱 + 正文也反转（tokens 仍保留原样做样子）。"""
    src = CATALOG.read_text(encoding="utf-8")
    src = src.replace(
        '"一律不给结论、不做判断、不推荐具体疗法或药品。"',
        '"一律给出结论、给出判断、推荐具体疗法或药品。"', 1)
    src = src.replace(OLD_TOK, NEW_TOK, 1)
    CATALOG.write_text(src, encoding="utf-8")
    ast.parse(CATALOG.read_text(encoding="utf-8"))
    return "catalog：正文反转 + tokens 削成「提醒」"


print("=" * 78)
print("E4/E5：token 层是否仍会同源自证")
print("=" * 78)
code, _ = run()
print(f"[基线] eval exit={code}（期望 0）\n")

for name, fn in (("E4 只削弱 tokens（正文不变）", e4),
                 ("E5 正文反转 + tokens 削弱", e5)):
    restore()
    desc = fn()
    code, out = run()
    red = code != 0
    crashed = "Traceback" in out
    who = sorted(set(re.findall(r"\] (\w+)\s+->", out))) if not crashed else []
    print(f"{name}\n  手法：{desc}")
    print(f"  结果：{'RED' if red else 'GREEN ★(绕过成功)'}   exit={code}"
          f"{'   ★崩溃' if crashed else ''}")
    if who:
        print(f"  拦截断言：{', '.join(who)}")
    for a, d in re.findall(r"\] (\w+)\s+-> ([^\n]{0,120})", out)[:2]:
        print(f"    · [{a}] {d}")
    print()

restore()
code, _ = run()
print(f"[收尾] 已还原，eval exit={code}")
