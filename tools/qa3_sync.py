"""把工作区当前状态同步进沙箱（工作区是权威），并清掉 pycache。"""
import shutil
import subprocess
import sys
from pathlib import Path

WS = Path(r"G:\workbuddy\语音助手")
SB = Path(r"C:\Users\Administrator\AppData\Local\Temp\qa3_sandbox")

FILES = [
    "evals/assertions.py", "evals/redline_baseline.py",
    "evals/redline_baseline.json", "evals/perm_invariants.py",
    "evals/runner.py", "evals/suites/xiaozhi.json", "evals/suites/invariants.json",
    "app/persona/catalog.py", "app/persona/guardrails.py",
    "app/persona/compiler.py", "app/persona/spec.py",
    "app/models.py", "app/routers/roles.py", "app/schemas.py",
    "app/schema_patch.py", "app/deps.py", "app/main.py", "app/config.py",
]

for rel in FILES:
    src = WS / "server" / rel
    dst = SB / "server" / rel
    if not src.exists():
        print("缺失:", rel)
        continue
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)

for p in SB.rglob("__pycache__"):
    shutil.rmtree(p, ignore_errors=True)
pri = SB.parent / "qa3_pristine"
shutil.rmtree(pri, ignore_errors=True)

print("已同步", len(FILES), "个文件到沙箱")
