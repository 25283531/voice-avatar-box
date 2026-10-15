"""判定 smoke_models / smoke_roles 的失败是否与本轮门禁改动有关。

方法：在**沙箱的 pristine 快照**上（即本轮改动之前的字节）跑同两个脚本。
pristine 是 QA-3 开工时冻结的 server/，之后所有变异都已还原。
若 pristine 上同样失败 -> 与本轮无关，是既有问题（回归）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

SB = Path(r"C:\Users\Administrator\AppData\Local\Temp\qa3_sandbox")
PRISTINE = SB.parent / "qa3_pristine"
PY = r"C:/Users/Administrator/.workbuddy/binaries/python/envs/elder/Scripts/python.exe"


def run(script: str) -> tuple[int, str]:
    t = tempfile.mkdtemp()
    env = dict(os.environ, TMPDIR=t, TEMP=t, TMP=t, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([PY, f"tools/{script}"], cwd=str(SB), capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=900)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


for script in ("smoke_models.py", "smoke_roles.py"):
    code, out = run(script)
    fails = [ln.strip() for ln in out.splitlines() if "[FAIL]" in ln]
    print("=" * 74)
    print(f"{script}  →  exit={code}   失败 {len(fails)} 项")
    print("=" * 74)
    for f in fails[:12]:
        print("   ", f[:150])
    if not fails and "Traceback" in out:
        print("   [无 [FAIL] 标记，但有异常]")
        print("   " + out[-700:])
    print()
