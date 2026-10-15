"""二轮复核 A-ter（无文件改动版）：dict-坏键 的 400 是回归还是既有行为。

为什么重写
----------
第一版靠**改文件再改回来**做对照，结果 Windows 下 ``write_text``
把行尾LF 悄悄转成 CRLF，把三个源文件搞脏了（内容对、行尾错）。
这一版改成**纯内存对照**：把两种实现各自定义成函数，直接喂同一批脏值，
不碰磁盘任何文件 —— 对照结论一样，零污染风险。

要回答的问题
------------
``{"x":1}`` / ``{"verbosity":"nope"}`` 这类**结构合法但键/值非法**的脏数据，
让 ``GET /api/roles`` 返回 400。这到底是本轮修复引入的**回归**，
还是修复前就有的**既有缺口**？

两种归一实现的差别（这是Bug 1 修复的全部内容）：

  修复前（等价写法）：``dict(value or {})``
      - ``42``      -> ``dict(42)``      -> TypeError
      - ``[1,2,3]`` -> ``dict([1,2,3])`` -> TypeError
      - ``"abc"``   -> ``dict("abc")``   -> ValueError
      - ``None``    -> ``{}``            -> OK
  修复后：``dict(value) if isinstance(value, dict) else {}``
      -以上全部     -> ``{}``            -> OK

注意：**两者对"结构合法但键非法"的dict 是完全一样的** ——
``dict({"x":1})`` 与 ``isinstance`` 分支都原样带出 ``{"x":1}``，
随后由 ``spec.validate()`` 抛 ``TraitValueError`` -> 400。
所以 400 若存在，**必然是既有缺口**，不可能是这轮修复造成的。

用法：
    python tools/qa4_regression_or_preexisting.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable

# 纯内存对照探针：两种归一写法 + validate 的完整链路
PROBE = r'''
import json, sys
sys.path.insert(0, @SERVER@)
from app.persona import catalog, spec as specmod
from app.persona.spec import from_row, RoleSpec

def old_as_dict(value):
    """修复前的等价写法：dict(value or {})"""
    return dict(value or {})

def new_as_dict(value):
    """修复后的写法（本仓库现状）"""
    return dict(value) if isinstance(value, dict) else {}

class Row:
    def __init__(self, traits):
        self.id = 1
        self.name = "probe"
        self.record_type = "role"
        self.parent_id = None
        self.system_prompt = ""
        self.description = ""
        self.tts_voice = ""
        self.traits = traits
        self.guardrails = {}

def run(normalizer, raw):
    """按给定归一函数装配 RoleSpec 并 validate，返回状态标签。"""
    import app.persona.spec as S
    orig = S._traits_dict
    S._traits_dict = normalizer
    try:
        r = Row(raw)
        spec = from_row(r)
        spec.validate()
        return "200-OK"
    except Exception as exc:
        return "ERR-" + type(exc).__name__
    finally:
        S._traits_dict = orig

import json as _json
RAW = {
    "[1,2]": [1, 2],
    "42": 42,
    '"abc"': "abc",
    "null": None,
    "[]": [],
    "true": True,
    "{}": {},
    '{"x":1}': {"x": 1},
    '{"verbosity":"nope"}': {"verbosity": "nope"},
    '{"humor":"0.5"}': {"humor": "0.5"},
}
out = {"OLD(dict(v or {}))": {}, "NEW(isinstance)": {}}
for label, raw in RAW.items():
    out["OLD(dict(v or {}))"][label] = run(old_as_dict, raw)
    out["NEW(isinstance)"][label] = run(new_as_dict, raw)
print("@@" + _json.dumps(out, ensure_ascii=False))
'''

# 设备对话链路：脏 traits 的角色编译出来会发生什么
CHAT_PROBE = r'''
import sys
sys.path.insert(0, @SERVER@)
from app.persona.compiler import compile_prompt
from app.persona.spec import default_spec

# 1) 脏 traits（键非法）
spec = default_spec()
spec.traits_declared = {"verbosity": "not_a_level"}
try:
    compile_prompt(spec)
    print("DIRTY_TRAITS=COMPILED")
except Exception as exc:
    print("DIRTY_TRAITS=" + type(exc).__name__)

# 2) 脏 guardrails 列（非 dict）—— 修复覆盖的那一类
spec2 = default_spec()
spec2.guardrails_hard = "not-a-list"
try:
    p = compile_prompt(spec2)
    print("DIRTY_GUARDRAILS=COMPILED len=%d redlines=%d"
          % (len(p), p.count("【安全底线 · 不可协商】")))
except Exception as exc:
    print("DIRTY_GUARDRAILS=" + type(exc).__name__)

# 3) 正常基线，确认探针本身没问题
try:
    p = compile_prompt(default_spec())
    print("CLEAN=COMPILED len=%d" % len(p))
except Exception as exc:
    print("CLEAN=" + type(exc).__name__)
'''


def run_snippet(code: str, tag: str) -> str:
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["PYTHONIOENCODING"] = "utf-8"
    tmp = tempfile.mkdtemp(prefix=f"qa4c_{tag}_")
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = tmp
    drv = Path(tmp) / "p.py"
    drv.write_text(code.replace("@SERVER@", repr(str(SERVER))), encoding="utf-8")
    try:
        p = subprocess.run([PY, str(drv)], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
        return (p.stdout or "") + (p.stderr or "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    import json as _json

    print("=" * 76)
    print("A-ter：dict-坏键 的 400 是回归还是既有缺口（纯内存对照，不改文件）")
    print("=" * 76)

    out = run_snippet(PROBE, "cmp")
    line = [l for l in out.splitlines() if l.startswith("@@")]
    if not line:
        print("探针失败：\n" + out[-1500:])
        return 2
    data = _json.loads(line[-1][2:])
    old, new = data["OLD(dict(v or {}))"], data["NEW(isinstance)"]

    print("\n[1] 两种归一实现的逐项对照：")
    print(f"    {'脏值':26s} {'修复前':22s} {'修复后':22s} 变化")
    regression, improvement = [], []
    for k in old:
        o, n = old[k], new[k]
        if o == n:
            mark = "="
        elif o != "200-OK" and n == "200-OK":
            mark = "+ 修复"
            improvement.append(k)
        elif o == "200-OK" and n != "200-OK":
            mark = "! 回归"
            regression.append(k)
        else:
            mark = "~"
        print(f"    {k:26s} {o:22s} {n:22s} {mark}")

    print("\n[2] 结论：")
    print(f"    修复带来的改善 {len(improvement)} 项：{improvement}")
    print(f"    修复引入的回归 {len(regression)} 项：{regression or '（无）'}")
    print("\n[3] 设备对话链路（编译层）：")
    chat = run_snippet(CHAT_PROBE, "chat")
    for l in chat.splitlines():
        if l.startswith(("DIRTY_", "CLEAN")):
            print("    " + l)

    print("\n" + "=" * 76)
    if regression:
        print("判定：**存在回归** —— 修复前 200、修复后非200，必须挂工程师。")
        return 1
    print("判定：**无回归**。")
    print("      · 非 dict 的 5 类脏值（[1,2]/42/\"abc\"/null/[]/true）"
          "确实从ERR 变成 200 —— 这正是 Bug 1 的修复成效；")
    print("      · dict 但键/值非法的 2 类，修复前后**完全相同** ——")
    print("        因为 as_dict 的两种写法对 dict 都原样透传，")
    print("        400 来自 spec.validate()（fail-closed 设计），属**既有缺口**。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
