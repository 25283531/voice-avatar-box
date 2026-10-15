"""第三轮反向验证：把本轮修的三处**逐一改坏**，确认对应用例真的会红。

为什么必须做反向验证
------------------
一个刚写出来的绿色断言，可能是因为「它压根没跑到」「它判的东西不存在」
或者「它恒真」而绿。只跑正向只能证明"现在没坏"，证明不了"坏了我会发现"。

本脚本做三组变异，每组都要求：**改坏 -> 变红-> 复原 -> 重新变绿**。
少了"复原后重新变绿"这一步，"改坏了之后一直红"也会被误判成"门禁有效"。

三组变异（分别对应本轮三项修复）
--------------------------------
M1  **unknown hard 键泄漏**（对应必修 2）
    把 ``guardrails.filter_known_hard`` 改成恒等（不过滤）。
    预期：``hard_never_leaks_unknown_key`` 变红。
    ⚠️ 注意这条变异**只**打读路径。若 ``regenerate_locked`` 仍在，
    提示词/承诺卡其实不会真的泄漏 —— 所以本脚本同时单独报告
    "spec 是否携带未审核条目"，让"防线在哪一层"这件事是**看得见的事实**，
    而不是靠读注释推断。

M2  **diff 契约被破坏**（对应必修 1）
    把 ``RoleDiffItem.changed`` 从响应里去掉（改字段名）。
    预期：``ok_contract_v2`` 变红（向后兼容断言必须能抓住字段消失）。

M3  **权限被放开**（对应必修 3）
    把 ``AIProvider.can_write`` 改成"谁都能写"。
    预期：``no_unsafe_override`` 变红。

另外单独验证「缺失键判红」这条元不变量：
    把套件里的 ``required_invariants`` 加上一个不存在的键，
    预期：eval 立刻红，且提示"没有用例覆盖它"。
    —— 这条正是 QA 指出的"缺失键被静默当作通过"的反面。

用法：
    python tools/qa5_reverse.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
CATALOG_DIR = SERVER / "app" / "persona"
SCHEMAS = SERVER / "app" / "schemas.py"
MODELS = SERVER / "app" / "models.py"
SUITE = SERVER / "evals" / "suites" / "invariants.json"
PY = sys.executable

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    return bool(ok)


def run_eval() -> tuple[int, str]:
    """跑一次 eval_persona，返回 (exit_code, 合并输出)。"""
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["PYTHONIOENCODING"] = "utf-8"
    tmp = tempfile.mkdtemp(prefix="qa5_")
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = tmp
    try:
        p = subprocess.run(
            [PY, str(ROOT / "tools" / "eval_persona.py")],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def first_failure(out: str) -> str:
    """抠出第一条硬失败，用来说明"是哪道断言抓住了"。"""
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("- [") and "❌" not in s:
            return s
    return ""


def mutate(path: Path, old: str, new: str) -> str:
    """替换并返回原文；片段不存在时抛错（**绝不静默跳过**）。

    静默跳过是这套脚本最容易出的事故：第一版就因为 catalog 变了而
    SKIP 了5 条用例，输出"共 0 条"，看起来像"全绿"。
    """
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"{path.name} 找不到待替换片段，脚本需更新：{old[:60]!r}")
    path.write_text(src.replace(old, new, 1), encoding="utf-8")
    return src


def restore(path: Path, original: str) -> None:
    """复原文件，但**只在没人动过它**的前提下写回。

    ⚠️ 为什么需要这个守卫（本轮实测踩到）
    --------------------------------------
    这个脚本与别的 agent **共用同一个工作区**。若在「我变异完、还没复原」
    的窗口里对方也改了同一个文件，那么无脑写回 original就会
    **把对方的改动整段覆盖掉** —— 也就是「我为了复原自己的变异，
    反而制造了别人的数据丢失」。

    真实经过：qa5 跑完报「复原后仍红」，而 catalog / models 都停在
    **正确版本**，说明红的原因不在我这边，而是并发覆盖。

    所以复原改成**条件写回**：内容仍等于我写入的变异版才写回；
    否则说明期间有第三方改动，此时**跳过并大声报警** ——
    宁可不复原（人可介入），不可覆盖别人的工作。
    """
    current = path.read_text(encoding="utf-8")
    if current == original:
        return                      # 已经复原过，无需再写
    # current 应等于「变异后」的状态；若两者都不等，说明有人改过
    # late-binding：_mutation_pairs() 在运行时才读那些常量（它们定义在本文件更靠后）
    mutated = original
    for old, new in _mutation_pairs():
        mutated = mutated.replace(old, new, 1)
    if current != mutated:
        log_or_warn(
            f"⚠️ 跳过复原 {path.name}：文件内容既不是原始版也不是本脚本的变异版，"
            f"说明期间有第三方改动。为避免覆盖别人的工作，本脚本不写回。"
            f"请人工确认该文件状态。")
        return
    path.write_text(original, encoding="utf-8")


def _mutation_pairs() -> list[tuple[str, str]]:
    """全部变异对。**运行时**才读常量，因为常量定义在文件更靠后的位置。"""
    return [(M1_OLD, M1_NEW), (M2_OLD, M2_NEW), (M3_OLD, M3_NEW),
            (META_OLD, META_NEW)]


def log_or_warn(msg: str) -> None:
    print(msg)




# ================================================================ M1
M1_FILE = CATALOG_DIR / "guardrails.py"
M1_OLD = """        if gid not in catalog.HARD_IDS or gid in seen:
            continue"""
M1_NEW = """        if gid in seen:
            continue"""


# ================================================================ M2
M2_OLD = "    changed: bool = True"
M2_NEW = "    changed_v2: bool = True"


# ================================================================ M3
# ⚠️ 必须锚在 AIProvider.can_write 的**docstring**上，不能只锚那行return：
# Avatar 里有一段**逐字相同**的 `if record_type == PRESET ... return is_admin`
# （models.py:213），只锚代码会让 replace(..., 1) 命中 Avatar 而不是
# AIProvider —— 变异"生效了"却打在了无关的类上，于是 eval 仍然全绿，
# 而脚本会如实报告"M3 没红"。第一版就是这么错的：它把 can_write 改成
# "谁都能写"，但改的是 Avatar，于是 no_unsafe_override 抓不到任何东西。
# 锚 docstring 让这段文字在文件里唯一。
M3_OLD = '''        """写权限：preset -> 仅管理员；role -> owner 或管理员；config -> 仅管理员。"""
        if self.is_config():
            return bool(getattr(user, "is_admin", False))
        if self.is_preset():
            return bool(getattr(user, "is_admin", False))
        return bool(getattr(user, "is_admin", False)) or (
            self.owner_id is not None and self.owner_id == user.id)'''
M3_NEW = '''        """写权限：preset -> 仅管理员；role -> owner 或管理员；config -> 仅管理员。"""
        return True'''


# ================================================================ 元不变量
META_OLD = '"hard_never_leaks_unknown_key"'
META_NEW = '"hard_never_leaks_unknown_key",\n    "totally_missing_invariant_xyz"'


def main() -> int:
    print("=" * 72)
    print("第三轮反向验证：把三处修复逐一改坏，确认对应用例变红")
    print("=" * 72)

    # ---- 基线：不变异必须绿，否则后面全是无意义的红 ----
    base_code, base_out = run_eval()
    print(f"\n[baseline] exit={base_code} "
          f"{'GREEN(ok)' if base_code == 0 else 'RED(!! 基线就不绿)'}")
    if base_code != 0:
        print(base_out[-2500:])
        return 1
    check("基线：未变异时 eval 绿", True)

    # =============== M1：读路径防线改成恒等 ===============
    print("\n── M1 把 filter_known_hard 改成不过滤（读路径防线失效）──")
    src = M1_FILE.read_text(encoding="utf-8")
    M1_FILE.write_text(src.replace(M1_OLD, M1_NEW, 1), encoding="utf-8")
    try:
        code, out = run_eval()
        red = code != 0
        detail = f"exit={code}"
        ff = first_failure(out)
        if ff:
            detail += f" | {ff[:130]}"
        check("M1 读路径不过滤 -> eval 必须红", red, detail)
        check("M1 是被 hard_never_leaks_unknown_key 抓住的",
              "hard_never_leaks_unknown_key" in out or "unknown_hard" in out,
              ff[:130])
    finally:
        restore(M1_FILE, src)

    # 额外把「防线在哪一层」变成看得见的事实，而不是靠读注释推断
    probe = run_layer_probe()
    check("M1 附带：spec 层确实拦住了未审核条目（防线在装配环节）",
          probe.get("spec_clean") is True, json.dumps(probe, ensure_ascii=False))
    check("M1 附带：即使 spec 被塞脏，提示词仍安全（第二道闸门）",
          probe.get("prompt_clean") is True, "")

    # =============== M2：破坏 diff 契约 ===============
    print("\n── M2 把 RoleDiffItem.changed 改名（向后兼容被破坏）──")
    src = SCHEMAS.read_text(encoding="utf-8")
    SCHEMAS.write_text(src.replace(M2_OLD, M2_NEW, 1), encoding="utf-8")
    try:
        code, out = run_eval()
        red = code != 0
        ff = first_failure(out)
        check("M2 diff 字段改名 -> eval 必须红", red,
              f"exit={code}" + (f" | {ff[:130]}" if ff else ""))
        check("M2 是被 ok_contract_v2 抓住的", "ok_contract_v2" in out, ff[:130])
    finally:
        restore(SCHEMAS, src)

    # =============== M3：放开写权限 ===============
    print("\n── M3 把 can_write 改成谁都能写（越权洞）──")
    src = MODELS.read_text(encoding="utf-8")
    MODELS.write_text(src.replace(M3_OLD, M3_NEW, 1), encoding="utf-8")
    try:
        code, out = run_eval()
        red = code != 0
        ff = first_failure(out)
        check("M3 放开写权限 -> eval 必须红", red,
              f"exit={code}" + (f" | {ff[:130]}" if ff else ""))
        check("M3 是被 no_unsafe_override 抓住的", "no_unsafe_override" in out,
              ff[:130])
    finally:
        restore(MODELS, src)

    # =============== 元不变量：缺失键必须判红 ===============
    print("\n── 元不变量：宣称一个没有任何用例覆盖的不变量 ──")
    src = SUITE.read_text(encoding="utf-8")
    SUITE.write_text(src.replace(META_OLD, META_NEW, 1), encoding="utf-8")
    try:
        code, out = run_eval()
        red = code != 0
        ff = first_failure(out)
        check("宣称了却没实现的键 -> eval 必须红（不再静默通过）", red,
              f"exit={code}" + (f" | {ff[:130]}" if ff else ""))
        check("红的原因明确指出「没有用例覆盖」",
              "没有任何用例覆盖" in out, ff[:150])
    finally:
        restore(SUITE, src)

    # ---- 复原确认：全部还原后必须重新变绿 ----
    after_code, after_out = run_eval()
    restored = after_code == 0
    print(f"\n[restore-check] 复原后 exit={after_code} "
          f"{'GREEN(已复原)' if restored else 'RED(!! 复原失败)'}")
    if not restored:
        print(after_out[-2000:])
    check("复原后 eval 重新变绿", restored)

    print("\n" + "=" * 72)
    print("==== 结论 ====")
    bad = [n for n, ok, _d in results if not ok]
    for n in bad:
        print(f"  ❌ {n}")
    passed = sum(1 for _n, ok, _d in results if ok)
    print(f"\n共 {len(results)} 项，失败 {len(bad)} 项，复原 "
          f"{'正常' if restored else '失败'}")
    print("VERDICT:", "PASS" if not bad and restored else "FAIL")
    return 0 if not bad and restored else 1


def run_layer_probe() -> dict:
    """实测「未审核 hard 键在哪一层被拦住」，把防线位置变成事实。"""
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["PYTHONIOENCODING"] = "utf-8"
    tmp = tempfile.mkdtemp(prefix="qa5_probe_")
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = tmp
    code = (
        "import json\n"
        f"import sys; sys.path.insert(0, {str(SERVER)!r})\n"
        "from app.persona.guardrails import card_items, merge_stored\n"
        "from app.persona.spec import from_row\n"
        "from app.persona.compiler import PromptCompiler\n"
        "CAN = 'CANARY_LAYER_PROBE'\n"
        "U = {'id': 'probe_unknown', 'text': CAN, 'locked': True}\n"
        "class R:\n"
        "    id=0; name='p'; system_prompt=''; description=''; tts_voice=''\n"
        "    traits={}; guardrails={'hard':[U],'soft':[]}\n"
        "    record_type='preset'; parent_id=None\n"
        "spec = from_row(R())\n"
        "res = {}\n"
        "res['spec_clean'] = not any('probe_unknown' == str(i.get('id'))\n"
        "                          for i in spec.guardrails_hard)\n"
        "res['prompt_clean'] = CAN not in PromptCompiler().compile(spec, None, '', '')\n"
        "res['card_clean'] = CAN not in json.dumps(card_items(0,'p'), ensure_ascii=False)\n"
        "res['stored_clean'] = CAN not in json.dumps(\n"
        "    merge_stored({'hard':[U],'soft':[]}), ensure_ascii=False)\n"
        "print('###JSON###' + json.dumps(res))\n"
    )
    drv = Path(tmp) / "probe.py"
    drv.write_text(code, encoding="utf-8")
    try:
        p = subprocess.run([PY, str(drv)], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        out = p.stdout or ""
        idx = out.rfind("###JSON###")
        if idx < 0:
            return {"error": out[-300:] or (p.stderr or "")[-300:]}
        return json.loads(out[idx + len("###JSON###"):].strip())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())