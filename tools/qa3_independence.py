"""B 项：独立性断言 redline_baseline_is_independent 有没有鉴别力。

B1  在 redline_baseline.py 里加 `from app.persona import catalog`
      -> 断言必须变红（防门禁被改回同源自证）
B2  用 __import__("app.persona.catalog") 动态取法
      -> 断言也必须变红（防动态绕过）
B3  把 import 写成注释/文档里的字样（只改注释，不改 import）
      -> 断言必须**仍然绿**（AST 只看真 import，注释不算违规）
      这条是「鉴别力」的反向校验：确认它不是子串匹配的误报机制。
B4  在 baseline.json 里写入 "catalog" 字样
      -> 断言必须变红（静态数据里不该有运行期痕迹）
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SANDBOX = Path(r"C:\Users\Administrator\AppData\Local\Temp\qa3_sandbox")
PY = r"C:/Users/Administrator/.workbuddy/binaries/python/envs/elder/Scripts/python.exe"
PRISTINE = SANDBOX.parent / "qa3_pristine"
RB_PY = SANDBOX / "server/evals/redline_baseline.py"
BASELINE = SANDBOX / "server/evals/redline_baseline.json"

REL = ("app/persona/guardrails.py", "app/persona/catalog.py",
       "evals/redline_baseline.json", "evals/redline_baseline.py",
       "evals/suites/xiaozhi.json")


def restore() -> None:
    for rel in REL:
        shutil.copy2(PRISTINE / "server" / rel, SANDBOX / "server" / rel)


def run() -> tuple[int, str]:
    t = tempfile.mkdtemp()
    env = dict(os.environ, TMPDIR=t, TEMP=t, TMP=t, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([PY, "tools/eval_persona.py"], cwd=str(SANDBOX),
                       capture_output=True, text=True, encoding="utf-8",
                       env=env, timeout=900)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def probe_independence() -> tuple[str, str]:
    """只跑独立性断言本身，拿到它的原话。"""
    t = tempfile.mkdtemp()
    env = dict(os.environ, TMPDIR=t, TEMP=t, TMP=t, PYTHONDONTWRITEBYTECODE="1")
    code = (
        "import sys; sys.path.insert(0, r'%s')\n"
        "from evals.assertions import redline_baseline_is_independent as f\n"
        "ok, detail = f('', {}, {})\n"
        "print('OK=', ok)\n"
        "print('DETAIL=', detail)\n"
    ) % (SANDBOX / "server")
    p = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", env=env, timeout=300, cwd=str(SANDBOX))
    return (p.stdout or "").strip(), (p.stderr or "").strip()


def b1() -> str:
    src = RB_PY.read_text(encoding="utf-8")
    src = src.replace("import json\n", "import json\nfrom app.persona import catalog\n", 1)
    RB_PY.write_text(src, encoding="utf-8")
    ast.parse(RB_PY.read_text(encoding="utf-8"))
    return "redline_baseline.py 加 `from app.persona import catalog`"


def b2() -> str:
    src = RB_PY.read_text(encoding="utf-8")
    src = src.replace("import json\n",
                      'import json\n_catalog = __import__("app.persona.catalog")\n', 1)
    RB_PY.write_text(src, encoding="utf-8")
    ast.parse(RB_PY.read_text(encoding="utf-8"))
    return 'redline_baseline.py 加 `__import__("app.persona.catalog")`'


def b3() -> str:
    """只加注释里的 catalog 字样 —— 合法改动，必须仍然绿。"""
    src = RB_PY.read_text(encoding="utf-8")
    src = src.replace("import json\n",
                      "import json\n# 说明：本模块刻意不 import app.persona.catalog\n", 1)
    RB_PY.write_text(src, encoding="utf-8")
    ast.parse(RB_PY.read_text(encoding="utf-8"))
    return "redline_baseline.py 只在**注释**里提到 app.persona.catalog"


def b4() -> str:
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    data["_note"] = data.get("_note", "") + "（抄录来源见 app.persona.catalog）"
    BASELINE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return "baseline.json 的 _note 里写入 app.persona.catalog"


CASES = [
    ("B1 import catalog", b1, "红"),
    ("B2 __import__ 动态取法", b2, "红"),
    ("B3 仅注释提到 catalog", b3, "绿"),
    ("B4 json 里写入 catalog 字样", b4, "红"),
]

print("=" * 76)
print("B 项：独立性断言鉴别力")
print("=" * 76)
print("[基线] ", end="")
out, err = probe_independence()
print(out.replace("\n", " | ") or err[-300:])
print()

results = []
for name, fn, expect in CASES:
    restore()
    desc = fn()
    code, _ = run()
    p_out, _ = probe_independence()
    red = code != 0
    ok = ("红" in expect and red) or ("绿" in expect and not red)
    results.append((name, expect, red, ok, p_out))
    print(f"{name}")
    print(f"  手法：{desc}")
    print(f"  期望：{expect}    实际：{'🔴 红' if red else '🟢 绿'}   "
          f"{'✅ 符合预期' if ok else '❌ 不符合预期'}")
    print(f"  断言原话：{p_out.replace(chr(10), ' | ')}")
    print()

restore()
print("=" * 76)
bad = [r for r in results if not r[3]]
print("B 项结论：", "全部符合预期" if not bad else f"★ {len(bad)} 项不符合预期")
print("=" * 76)
sys.exit(1 if bad else 0)
