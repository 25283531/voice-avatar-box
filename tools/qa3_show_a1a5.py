"""抓取 A1 / A5 的完整 eval 输出（此前解析器漏掉了「不变量」段的失败行）。"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

SANDBOX = Path(r"C:\Users\Administrator\AppData\Local\Temp\qa3_sandbox")
PY = r"C:/Users/Administrator/.workbuddy/binaries/python/envs/elder/Scripts/python.exe"
PRISTINE = SANDBOX.parent / "qa3_pristine"

CATALOG = SANDBOX / "server/app/persona/catalog.py"


def restore() -> None:
    for rel in ("app/persona/catalog.py", "app/persona/guardrails.py",
                "evals/redline_baseline.json", "evals/redline_baseline.py",
                "evals/suites/xiaozhi.json"):
        shutil.copy2(PRISTINE / "server" / rel, SANDBOX / "server" / rel)


def run() -> tuple[int, str]:
    t = tempfile.mkdtemp()
    env = dict(os.environ, TMPDIR=t, TEMP=t, TMP=t, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([PY, "tools/eval_persona.py"], cwd=str(SANDBOX),
                       capture_output=True, text=True, encoding="utf-8",
                       env=env, timeout=900)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def a1() -> None:
    src = CATALOG.read_text(encoding="utf-8")
    old = '"一律不给结论、不做判断、不推荐具体疗法或药品。"'
    assert old in src
    CATALOG.write_text(src.replace(
        old, '"一律给出结论、给出判断、推荐具体疗法或药品。"', 1), encoding="utf-8")


def a5() -> None:
    src = CATALOG.read_text(encoding="utf-8")
    i = src.index('id="no_medical_advice"')
    j = src.index('id="no_dose_or_adjust"')
    start = src.rindex("HardGuardrail(", 0, i)
    CATALOG.write_text(src[:start] + src[j:], encoding="utf-8")


for label, fn in (("A1 语义反转", a1), ("A5 整条红线删除", a5)):
    restore()
    fn()
    code, out = run()
    print("=" * 76)
    print(f"{label}  →  eval exit={code}")
    print("=" * 76)
    print(out)
    print()
restore()
print("[已还原]")
