#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CI 门禁自检脚本 —— .github/workflows/pr-gate.yml 在 CI 里跑的就是它。

本脚本刻意与工作流分离成独立文件，是为了「本地能跑同一个判据」：
在推 PR 之前，本地 ``python tools/ci_gate_check.py``  exit 0 才推。
CI 与本地跑同一份代码，才不会出现「CI 绿本地红」或反过来。

检查三项（任一失败 exit 1，全部通过 exit 0）
--------------------------------------------
1. **CODEOWNERS 存在且语法合法**：每条非注释非空行都是一条规则，
   模式合法、至少一个 owner、每个 owner 以 ``@`` 开头。
2. **关键路径覆盖断言**：硬编码一份关键路径清单（启动期写表 / 启动 seed /
   连库 / 人格红线 / 黄金副本 / CI 本体），按 CODEOWNERS 模式匹配规则
   断言每条关键路径都被**某条非通配规则**覆盖。有人删掉这些行就红。
3. **workflows 结构**：``.github/workflows/*.yml`` 逐个过 ``yaml.safe_load``，
   并断言「有 ``on`` 触发块」「``jobs`` 非空」「每个步骤都有 ``uses`` 或
   ``run``」。最后一条来自真实事故：一个悬空空步骤会让**整个 workflow
   启动失败** → jobs=0 → PR 上不产出任何检查项 → 后端业务冒烟静默失效，
   而它在 diff 里看起来只是一行无害的 ``name``。

匹配口径（与 GitHub 真实语义的关系，必须读）
--------------------------------------------
本脚本的匹配器是 GitHub CODEOWNERS 语义的**保守近似**，方向刻意取「宽」：

* 前导 ``/`` 锚定仓库根；完全不含斜杠的模式按 basename 任意深度匹配
  （gitignore 习惯）；其余含斜杠的模式同样锚定仓库根；
* 目录模式（尾部 ``/``，或不带尾部斜杠的目录名）匹配目录本身及全部后代；
* ``*`` 匹配任意字符**含 ``/``**，``**`` 同样跨目录。

「宽」是有意的trade-off：本脚本的职责是**自检锚点**，不是重新实现
GitHub。误判「未覆盖」会让门禁在合法 CODEOWNERS 上假红，是自伤；
误判「已覆盖」只可能放过未来某种 exotic 写法，而那时删行测试
（见 pr-gate.yml 注释里的反向验证）仍然有效。当前 CODEOWNERS 只用到
字面路径 + 尾部斜杠目录，两种口径下结果一致。

裸通配规则（``*`` / ``**`` / ``/*`` / ``/**``）在覆盖断言中被跳过：
它们匹配一切路径，若计入覆盖，断言就退化成永真 —— 「有人删掉显式条目
也能过」，正是本断言要挡的东西。

依赖：PyYAML（CI 工作流里显式安装；缺了会 fail-closed 报错，不静默跳过）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
CODEOWNERS_PATH = ROOT / ".github" / "CODEOWNERS"
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

# 关键路径清单：与 .github/CODEOWNERS 的显式条目一一对应。
# 改这里 = 改「门禁认为哪些路径出事就是数据损坏」—— 那是决策，不是随手
# 增删；要增删请连同 CODEOWNERS 注释里的事故链路一起改。
CRITICAL_PATHS: tuple[str, ...] = (
    "server/app/schema_patch.py",  # 启动期写表：backfill/dedupe/_patch_version
    "server/app/main.py",          # 启动 seed 人格预设
    "server/app/db.py",            # init_db / wait_for_db
    "server/app/persona/",         # 人格红线目录
    "server/evals/",               # 红线黄金副本基准
    ".github/workflows/",          # CI 本体（含本门禁自己）
)

# 裸通配规则：匹配一切，覆盖断言中不计入。
_BARE_CATCHALL = frozenset({"*", "**", "/*", "/**"})


class Rule(NamedTuple):
    """一条 CODEOWNERS 规则。"""

    pattern: str
    owners: tuple[str, ...]
    lineno: int


class CheckReport:
    """收集检查结果，统一打印。"""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passes: list[str] = []

    def ok(self, message: str) -> None:
        self.passes.append(message)
        print(f"[ OK ] {message}")

    def fail(self, message: str) -> None:
        self.failures.append(message)
        print(f"[FAIL] {message}")


# ---------------------------------------------------------------------------
# 检查 1：CODEOWNERS 存在性 + 语法
# ---------------------------------------------------------------------------

def parse_codeowners(text: str) -> tuple[list[Rule], list[str]]:
    """解析 CODEOWNERS 文本，返回 (规则列表, 语法错误列表)。

    语法错误包括：模式为空、模式以 ``!`` 开头（CODEOWNERS 不支持取反）、
    没有 owner、owner 不以 ``@`` 开头。
    """
    rules: list[Rule] = []
    errors: list[str] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        pattern = parts[0]
        owners = tuple(parts[1:])

        if not pattern:
            errors.append(f"第 {lineno} 行：模式为空")
            continue
        if pattern.startswith("!"):
            errors.append(
                f"第 {lineno} 行：模式 {pattern!r} 以 '!' 开头 —— "
                "CODEOWNERS 不支持取反，该行不会被 GitHub 采用"
            )
        if not owners:
            errors.append(
                f"第 {lineno} 行：规则 {pattern!r} 没有 owner —— "
                "GitHub 要求每条规则至少一个所有者"
            )
        for owner in owners:
            if not owner.startswith("@"):
                errors.append(
                    f"第 {lineno} 行：owner {owner!r} 不以 '@' 开头 —— "
                    "本门禁只认 GitHub 账号/团队（@user / @org/team），"
                    "邮箱所有者一律按不合法处理（所有权必须可经 GitHub 审计）"
                )
        rules.append(Rule(pattern=pattern, owners=owners, lineno=lineno))

    return rules, errors


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """把 CODEOWNERS 模式编译成正则。匹配口径见模块 docstring。"""
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    body = pattern.strip("/")
    regex_body = ""
    for i, segment in enumerate(body.split("**")):
        if i:
            regex_body += ".*"
        regex_body += re.escape(segment).replace(r"\*", ".*")
    if anchored:
        full = "^" + regex_body + r"(/.*)?$"
    else:
        full = r"(^|.*/)" + regex_body + r"(/.*)?$"
    return re.compile(full)


# ---------------------------------------------------------------------------
# 检查 2：关键路径覆盖断言
# ---------------------------------------------------------------------------

def check_critical_coverage(rules: list[Rule], report: CheckReport) -> None:
    """断言每条关键路径都被某条非通配规则覆盖。"""
    compiled: list[tuple[Rule, re.Pattern[str]]] = []
    for rule in rules:
        if rule.pattern in _BARE_CATCHALL:
            continue  # 裸通配不计入覆盖，否则断言永真
        try:
            compiled.append((rule, compile_pattern(rule.pattern)))
        except re.error as exc:  # 模式非法时编译不会抛，这里防御未来改动
            report.fail(f"第 {rule.lineno} 行：模式 {rule.pattern!r} 编译失败：{exc}")

    for path in CRITICAL_PATHS:
        normalized = path.rstrip("/")
        matched = [
            rule.pattern
            for rule, regex in compiled
            if regex.match(normalized)
        ]
        if matched:
            report.ok(
                f"关键路径 {path} 已被显式规则覆盖（{', '.join(matched)}）"
            )
        else:
            report.fail(
                f"关键路径 {path} 没有任何非通配规则覆盖 —— "
                "CODEOWNERS 的显式条目被删了？删掉这些行的后果见该文件"
                "顶部的事故链路注释"
            )


# ---------------------------------------------------------------------------
# 检查 3：workflows 结构 —— 可解析 + 有触发块 + jobs 非空 + 每个步骤非空
# ---------------------------------------------------------------------------
# 事故链路（本项目真实发生过，GitHub Actions run 22/23/24 连续三次
# conclusion=failure、jobs=0）：
#
#   mysql-smoke 末尾一个「只有 name、没有 uses/run」的悬空步骤
#     → Actions 遇到没有 uses/run 的步骤，会让**整个 workflow 启动失败**
#       （不是只红那一步）
#     → jobs = 0，PR 上**一个检查项都不产出**
#     → 后端业务冒烟（smoke_server / smoke_roles / eval_persona / qa3_*
#       与整个 MySQL job）全部静默失效
#
# 而它在 diff 里只是几行注释 + 一个孤零零的 name，看起来完全无害。
# 最危险的地方：失败形态是「PR 上没有检查项」，**看起来像通过** ——
# 比红更危险，红至少会有人去看。
#
# 「jobs 为空」「缺 on 触发块」是同一类缺陷：不产出任何检查项的 workflow
# 等于没有 workflow，而且同样表现为「什么都没发生」。
#
# ⚠️ 本检查的边界（不写清就会被当成它不是的东西）
# 它只判「结构上会不会被 Actions 拒绝」，**不判步骤会不会真的跑**：
#   * run 里的命令写错、脚本自己假绿 —— 判不出来；
#   * 触发条件写窄了（例如 paths 过滤漏掉某个目录）—— 判不出来；
#   * 步骤的语义对不对、够不够 —— 判不出来。
# 那些靠 CI 实跑与人工 review。一个只判结构、不判语义的断言不是没用的，
# 但它**不能**被当成「CI 已经覆盖住了」的证据。

def _find_trigger_key(doc: dict) -> object | None:
    """找出 workflow 的触发块键。

    ⚠️ PyYAML 遵循 YAML 1.1，会把裸键 ``on`` 解析成布尔 ``True``
    （本地实测：``yaml.safe_load("on:\\n  push:")`` 的键是 ``True`` 而非
    ``"on"``）。所以这里同时接受布尔 True 与任意大小写的字符串 "on" ——
    只认字符串会让这个断言在**每一个**合法 workflow 上假红。
    """
    for key in doc:
        if key is True:
            return key
        if isinstance(key, str) and key.lower() == "on":
            return key
    return None


def check_workflow_structure(doc: dict, rel: str, report: "CheckReport") -> None:
    """单个 workflow 的结构断言：触发块 / jobs 非空 / 每个步骤有 uses 或 run。"""
    # ---- 触发块存在 ----
    if _find_trigger_key(doc) is None:
        report.fail(f"{rel} 缺 on 触发块 —— 不产检查项的 workflow 等于没有")
    else:
        report.ok(f"{rel} 存在 on 触发块")

    # ---- jobs 非空 ----
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        report.fail(
            f"{rel} 的 jobs 为空或不是映射 —— Actions 不会为它跑出任何检查项"
        )
        return
    report.ok(f"{rel} jobs 非空（{len(jobs)} 个）")

    # ---- 每个步骤都必须有 uses 或 run ----
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            report.fail(f"{rel} 的 job {job_id!r} 不是映射")
            continue
        if "steps" not in job:
            # job 用 `uses:` 调复用工作流是合法的，没有 steps
            if job.get("uses"):
                continue
            report.fail(
                f"{rel} 的 job {job_id!r} 既没有 steps 也没有 uses —— "
                "该 job 不会执行任何东西"
            )
            continue
        steps = job["steps"]
        if not isinstance(steps, list) or not steps:
            report.fail(f"{rel} 的 job {job_id!r} 的 steps 为空")
            continue
        ok = True
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                report.fail(f"{rel} 的 job {job_id!r} 第 {index} 个步骤不是映射")
                ok = False
                continue
            if not step.get("uses") and not step.get("run"):
                name = step.get("name") or "<未命名>"
                report.fail(
                    f"{rel} 的 job {job_id!r} 第 {index} 个步骤「{name}」"
                    "既无 uses 也无 run —— 空步骤会让整个 workflow 启动失败、"
                    "PR 上无任何检查项（看起来像通过，比红更危险）"
                )
                ok = False
        if ok:
            report.ok(
                f"{rel} 的 job {job_id!r} {len(steps)} 个步骤均有 uses 或 run"
            )


def check_workflows(report: "CheckReport") -> None:
    """遍历 `.github/workflows/*.yml`：解析 + 结构断言。"""
    try:
        import yaml  # noqa: PLC0415 —— 延迟导入，缺依赖时给出可执行提示
    except ImportError:
        report.fail(
            "PyYAML 未安装：pip install pyyaml（CI 工作流里已显式安装；"
            "此处 fail-closed，绝不静默跳过 YAML 检查）"
        )
        return

    if not WORKFLOWS_DIR.is_dir():
        report.fail(f" workflows 目录不存在：{WORKFLOWS_DIR}")
        return

    files = sorted(WORKFLOWS_DIR.glob("*.yml"))
    if not files:
        report.fail(".github/workflows/ 下没有任何 .yml —— CI 没了？")
        return

    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            report.fail(f"{rel} 不是合法 YAML：{exc}")
            continue
        if not isinstance(doc, dict):
            report.fail(
                f"{rel} 顶层不是映射（解析结果为 {type(doc).__name__}）"
            )
            continue
        report.ok(f"{rel} 可通过 yaml.safe_load 解析")
        check_workflow_structure(doc, rel, report)


# ---------------------------------------------------------------------------
# 检查 4：禁止把 SQLAlchemy URL 转成会洗白口令的字符串
# ---------------------------------------------------------------------------

#: 会触发口令洗白的写法。
#:
#: ⚠️ 只收 ``str(make_url(...))`` 这一种**明确无歧义**的形态，不收裸 ``str(url)``。
#:    理由：裸 ``str(url)`` 在本仓库是合法的其它用法（``llm_gateway.py:821`` 用它把
#:    data-URL 拆出 base64），收进来就是纯误报；一条会误报的检查等于没有检查 ——
#:    真出事时它先被人加进白名单，然后永远失效。
#:
#:    真正的风险形态是「SQLAlchemy 的 ``URL`` 对象被 str() 化后再喂回
#:    ``make_url()``/``create_engine()``」，而本仓库里 ``URL`` 的来源就是
#:    ``make_url()``。所以按来源收口，既精准又不漏当前的实际模式。
_URL_STR_TRAPS: tuple[tuple[str, str], ...] = (
    (r"str\(\s*make_url\(", "str(make_url(...))"),
)

#: 本文件自己必然含这些字面量（正则与提示文案），检查器不能检自己
_SELF = Path(__file__).relative_to(ROOT).as_posix()


def check_no_url_str_trap(report: "CheckReport") -> None:
    """扫仓库里会把数据库连接串的口令洗白的写法。

    ``URL.render_as_string(hide_password=False)`` 是**允许**的，
    所以命中行里同时出现 ``hide_password`` 时放行。
    """
    hits: list[str] = []
    scanned = 0
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel == _SELF:
            continue
        if any(part in {".git", "node_modules", "build", "dist", "__pycache__",
                        "managed_components", ".venv"}
               for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern, label in _URL_STR_TRAPS:
                if not re.search(pattern, line):
                    continue
                if "hide_password" in line or line.lstrip().startswith("#"):
                    continue
                hits.append(f"{rel}:{lineno}  {label}  →  {line.strip()[:90]}")

    if hits:
        report.fail(
            "发现会把 SQLAlchemy URL 的口令洗成字面量 '***' 的写法"
            f"（扫描 {scanned} 个 .py，命中 {len(hits)} 处）"
            "—— 改用 render_as_string(hide_password=False)：\n    "
            + "\n    ".join(hits)
        )
    else:
        report.ok(f"没有 str(make_url(...)) 这类口令洗白写法（已扫 {scanned} 个 .py）")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 72)
    print("守门人自检：CODEOWNERS 覆盖 + workflows 结构")
    print("=" * 72)

    report = CheckReport()

    # ---- 检查 1 ----
    if not CODEOWNERS_PATH.is_file():
        report.fail(f"CODEOWNERS 不存在：{CODEOWNERS_PATH.relative_to(ROOT).as_posix()}")
        rules: list[Rule] = []
    else:
        text = CODEOWNERS_PATH.read_text(encoding="utf-8")
        rules, syntax_errors = parse_codeowners(text)
        if syntax_errors:
            for err in syntax_errors:
                report.fail(f"CODEOWNERS 语法：{err}")
        elif not rules:
            report.fail("CODEOWNERS 没有任何规则（连默认行都没有）")
        else:
            report.ok(f"CODEOWNERS 语法合法（{len(rules)} 条规则）")

    # ---- 检查 2（即使检查 1 有错也继续跑，一次看全）----
    if rules:
        check_critical_coverage(rules, report)

    # ---- 检查 3 ----
    check_workflows(report)

    # ---- 检查 4 ----
    check_no_url_str_trap(report)

    print("-" * 72)
    print(f"通过 {len(report.passes)} 项，失败 {len(report.failures)} 项")
    if report.failures:
        print("结论：**红** —— 上面的 FAIL 逐条处理后再推 PR")
        return 1
    print("结论：**绿**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
