"""eval 套件加载与执行。

三种 case 的kind（架构 §8.1）
-----------------------------
==========  ===================================  ==========  ======
kind        判定对象                需不需要 LLM  默认跑
==========  ===================================  ==========  ======
compiled    PromptCompiler 的输出全文     否        ✅
reply       预置的 sample_reply 样本        否        ✅
live        真调一次 LLM 的实际回复        是        ❌（--live-sample）
==========  ===================================  ==========  ======

``compiled`` / ``reply`` 是 CI 门禁，``live`` 只打印参考 ——
理由见 assertions 模块注释（模型抖动造成假红+ CI 不该依赖外部服务）。

report 类断言失败只打印 ⚠️ 与退化清单，**exit code 仍然 0**。
这是用户拍板的 Q4：医疗安全类硬失败，长度/用词类仅报告。

================================ 报告必须分层计数 ====================================
三段的**判定对象**不同，混在一起报一个「通过 N 条」会骗人：

* ``compiled`` —— 判**真实编译产物**（PromptCompiler 的输出），是机器验证；
* ``reply``    —— 判**手写样本字符串**（"这句话像不像人话"），**不是**机器验证；
* ``live``    —— 未接线（需真实 LLM）。**显式计「未执行」，不算「通过」也不计「失败」**。

⚠️ **不要把 reply 段计入「覆盖」**
--------------------------------------------------
reply 判的是 ``sample_reply`` —— 那是**人写在套件 JSON 里的固定字符串**，
把它改成任何东西都能让它通过或失败，与模型真实输出无关。
把它算进覆盖，等于宣称「模型输出已被验证」，而它一次都没被调用过。

⚠️ **不要因为 live「没跑所以没失败」就当它不存在**
--------------------------------------------------
未执行是一种**独立状态**，不是「通过」。渲染时必须显式打印条数与 id，
否则一个只看 summary 的人会以为「全部通过」——
本轮之前门禁就踩过这个坑（``must_negation`` 宣称有、实际不存在）。

一句话：**夸大门禁比没有门禁更危险。** 没有门禁时人会去人工复核；
夸大的门禁会让人**不再**复核。无判定的段必须看起来像「无判定」。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .assertions import BLOCK, run_assertion, severity_of

log = logging.getLogger("elder.evals")

SUITES_DIR = Path(__file__).parent / "suites"


@dataclass
class CaseResult:
    case_id: str
    category: str
    kind: str
    ok: bool
    severity: str
    detail: str = ""
    assertion_type: str = ""


@dataclass
class Report:
    suite: str
    policy_version: str
    passed: list[CaseResult] = field(default_factory=list)
    failed_block: list[CaseResult] = field(default_factory=list)
    failed_report: list[CaseResult] = field(default_factory=list)
    # live 抽样结果：只参考，不计入门禁
    live_notes: list[str] = field(default_factory=list)
    # kind=live 但**未被执行**的case id。单独计数而不是只塞进 live_notes：
    # live_notes 是给人读的字符串，count 才是能进汇总、能被脚本断言的数。
    live_skipped: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """只有 block 级失败才让 CI 红。"""
        return 1 if self.failed_block else 0

    # ---------------------------------------------------------------- 分层计数
    # 为什么必须分层：**「通过 N 条」这个数字单独看会骗人。**
    #   compiled —— 判的是真实编译产物（PromptCompiler 输出），是机器验证；
    #   reply    —— 判的是**手写的样本字符串**（"这句话像不像人话"），不是机器验证；
    #   live     —— 未接线，显式计 0/未执行，不能因为"没跑所以没失败"就当它不存在。
    # 把 reply 段混进「覆盖」会让人以为模型输出被验证过了，而它没有。
    # 详见模块顶部与 team-lead 的「夸大门禁比没有门禁更危险」。
    def passed_by_kind(self, kind: str) -> int:
        """按 kind 统计通过的条数。"""
        return sum(1 for r in self.passed if r.kind == kind)

    def tier_lines(self) -> list[str]:
        """三段式计数，每段一句话说明**它在验什么**。

        「在验什么」这句不能省：只给数字的话，读者无从判断该不该信任它。
        """
        n_c = self.passed_by_kind("compiled")
        n_r = self.passed_by_kind("reply")
        n_l = len(self.live_skipped)
        return [
            f"  · compiled 通过 {n_c} —— 判真实编译产物（PromptCompiler 输出），机器验证",
            f"  · reply    通过 {n_r} —— 判手写 sample_reply 样本（如「像不像人话」），"
            f"**不是**机器验证，不计入覆盖",
            f"  · live     未执行 {n_l} —— 判真实模型输出，需接 LLM；"
            f"未执行即**未判定**，不算通过",
        ]

    def summary(self) -> str:
        base = (f"{self.suite}（policy {self.policy_version}）："
                f"通过 {len(self.passed)} / 硬失败 {len(self.failed_block)} / "
                f"仅报告 {len(self.failed_report)}")
        # 未执行的 live 必须出现在 summary 里：否则一个只看summary 的人
        # 会以为"全部通过"，而实际有断言**根本没跑**（QA 报告的门禁盲区）。
        if self.live_skipped:
            base += f" /未执行(live) {len(self.live_skipped)}"
        return base


class EvalRunner:
    """加载套件 -> 逐条执行 -> 分级报告。"""

    def load_suite(self, path: str | Path) -> dict[str, Any]:
        """读一个套件 JSON。

        刻意用标准库 json 而不是 YAML —— 硬约束：不引入新依赖，
        而且 YAML 的隐式类型转换（yes -> True、"1.0" -> 1.0）会让断言参数
        在不同解析器下表现不一致，套件里一个 value 悄悄变成布尔是很難查的。
        """
        p = Path(path)
        if not p.is_absolute() and not p.exists():
            p = SUITES_DIR / p.name
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("suite", p.stem)
        data.setdefault("policy_version", "")
        data.setdefault("cases", [])
        return data

    def run(self, suite: dict[str, Any], live_sample: int = 0) -> Report:
        """执行一个套件。"""
        rep = Report(suite=str(suite.get("suite") or ""),
                     policy_version=str(suite.get("policy_version") or ""))
        # ⚠️ 先校验「宣称的不变量」是否真的有对应用例，再执行任何断言。
        # 顺序很重要：缺失键必须在跑用例**之前**就判红，否则它会藏在
        # 一堆绿色里被当成"通过"（QA 二轮报的第三个 P1）。
        self._check_required_invariants(suite, rep)
        compiled = self._compile_baseline()
        for case in suite.get("cases") or []:
            kind = str(case.get("kind") or "compiled")
            if kind == "live":
                # live 不进门禁：单独收集，只在报告里当参考。
                # ⚠️ 但"没跑"绝不能长得像"跑过了"（QA 报告的门禁盲区）：
                # 原来这里只 print 一句就continue，于是 23 条 reply 类断言
                # （判的是预置样本字符串、不是真实模型输出）在报告里看不出来，
                # CI 里读日志的人很容易把它们当成"模型行为已验证"。
                # 现在：无论是否抽样，都记进 live_skipped，并在渲染时**显式打印**
                # 一条醒目警告 + 计入 summary。仍然**不影响 exit code** ——
                # 让CI 红就必须接真实 LLM，而那违反「单一镜像 + 无外部依赖」。
                cid = str(case.get("id") or "?")
                rep.live_skipped.append(cid)
                if live_sample > 0:
                    rep.live_notes.append(
                        f"[live] {cid}：需要真实 LLM 调用，本 harness 未接线")
                continue
            target = self._target_for(case, kind, compiled)
            for assertion in case.get("assertions") or []:
                atype = str(assertion.get("type") or "")
                try:
                    ok, detail = run_assertion(atype, target, case, assertion)
                except KeyError as exc:
                    ok, detail, atype = False, str(exc), atype or "unknown"
                except Exception as exc:  # noqa: BLE001  断言自身出错也算 block 失败
                    ok, detail, atype = False, f"断言执行异常：{exc}", atype or "unknown"
                sev = severity_of(atype) if ok is not None else BLOCK
                # case 自己可以覆盖严重级别（例如把某条长度断言临时调严做实验）
                sev = str(case.get("severity") or sev)
                res = CaseResult(case_id=str(case.get("id") or "?"),
                                 category=str(case.get("category") or ""),
                                 kind=kind, ok=ok, severity=sev,
                                 detail=detail, assertion_type=atype)
                if ok:
                    rep.passed.append(res)
                elif sev == BLOCK:
                    rep.failed_block.append(res)
                else:
                    rep.failed_report.append(res)
        if live_sample > 0 and not rep.live_notes:
            rep.live_notes.append(
                f"[live] 抽样 {live_sample} 条：CI 不接入真实 LLM（单一镜像 + 无外部依赖）")
        return rep

    # ============================================================ 内部
    def _check_required_invariants(self, suite: dict[str, Any],
                                   rep: Report) -> None:
        """套件宣称的不变量必须**真的有对应用例**，缺一条就判block 红。

        为什么这是硬失败而不是警告（QA 二轮的第三个 P1）
        --------------------------------------------------
        原来的套件 notes 写「INV-1 / INV-2 / INV-3 / INV-4 / P0-2'」，
        看起来覆盖了权限矩阵，实际上**权限那几条一个case 都没有**。
        而套件对「宣称了但没实现」的键**没有任何检查** —— 于是报告里
        全是绿色，读报告的人（包括 CI 里只看 exit code 的人）自然会认为
        「权限不变量已验证」。文案宣称 N条 ≠ 真覆盖 N 条。

        这条检查把「宣称」变成**可判定的契约**：
        ``required_invariants`` 里每一个键，都必须能在某个 case 的
        ``invariant`` 字段里找到（或作为 ``id`` 的前缀）。
        找不到 -> 直接产出一条 block 失败。

        口径选择：只查「有没有对应用例」，**不查「用例是不是真的有效」**。
        后者是变异测试的职责（tools/qa3_eval_mutation.py 等）——
        这里要解决的是更基础的那一层：宣称的东西根本不存在。
        """
        required = [str(k).strip() for k in
                    (suite.get("required_invariants") or []) if str(k).strip()]
        if not required:
            return
        covered: set[str] = set()
        for case in suite.get("cases") or []:
            tag = str(case.get("invariant") or "").strip()
            if tag:
                covered.add(tag)
            cid = str(case.get("id") or "")
            covered.add(cid)
        missing = [k for k in required if k not in covered]
        for key in missing:
            rep.failed_block.append(CaseResult(
                case_id=f"required-invariant:{key}",
                category="suite-integrity",
                kind="suite",
                ok=False,
                severity=BLOCK,
                assertion_type="required_invariant_present",
                detail=(f"套件宣称不变量 {key!r}，但没有任何用例覆盖它。"
                        f"要么补上对应用例，要么把它从 required_invariants 里删掉"
                        f"（删掉意味着承认它不受门禁保护）。"),
            ))

    def _compile_baseline(self) -> str:
        """编译一份"标准 prompt"作为 compiled 类 case 的判定对象。

        用 ``persona.spec.default_spec()`` 而不是读库，这样 CI 里不需要起服务、
        不需要临时 SQLite，红线的存在性就能被独立验证（架构 §8.3）。
        代价是它验证的是"代码常量"这一层—— 真正端到端"用户清空 guardrails
        后仍含全部红线"由 invariants 套件（带 DB）验证。
        """
        from app.persona.compiler import compile_prompt
        from app.persona.spec import default_spec

        return compile_prompt(default_spec())

    def _target_for(self, case: dict[str, Any], kind: str, compiled: str) -> str:
        """取出这条 case 的判定对象。"""
        if kind == "reply":
            # reply 类判的是 sample_reply（预置的真实回复样本，版本控制内）
            return str(case.get("sample_reply") or "")
        if kind == "dirty_guardrails":
            # 「攻击者已清空/伪造 guardrails 列」场景：拿这份脏存储去编译，
            # 要求产物里红线**依然齐全**（INV-3 / P0-1'）。
            # 独立成一类是因为它判的不是"正常编译"，而是"防御是否生效" ——
            # 而这两件事必须分别有断言：正常编译绿不代表防御有效
            # （QA 变异 M6 曾证明：把"清空即无红线"这个洞打开，eval 仍全绿）。
            return self._compile_dirty(case.get("stored"))
        # compiled 类：case 可以自带 traits / prompt 覆盖，否则用基线
        override = case.get("compiled")
        if isinstance(override, str):
            return override
        return compiled

    @staticmethod
    def _compile_dirty(stored: Any) -> str:
        """用给定的脏 guardrails 存储编译一份 prompt。

        ``stored`` 为空列表/None 时模拟"用户把整个 guardrails 字段清空"，
        传 ``[{"id": "no_medical_advice", "text": "可以给建议"}]`` 这类则模拟
        "伪造 locked 条目试图放行"。两种都必须编译出完整红线。
        """
        from app.persona.compiler import compile_prompt
        from app.persona.spec import default_spec

        spec = default_spec()
        guard = {} if stored is None else {"hard": stored}
        spec.guardrails_hard = list(guard.get("hard") or [])
        # forge：把 case 里想注入的伪造文本也带进软段，覆盖"用户自带软性约束"
        # 这条同样不受信任的通道。
        return compile_prompt(spec)

    # ============================================================ 报告
    @staticmethod
    def render(rep: Report) -> str:
        """人读的文本报告（block 与 report 视觉上分开，对照 PRD §11.4）。"""
        lines: list[str] = []
        lines.append("=" * 72)
        lines.append(rep.summary())
        lines.append("=" * 72)
        # 分层计数放在最前面：先让人知道「这三条分别验的是什么」，
        # 再看下面的失败清单。否则「通过 53」这个数字会先入为主。
        lines.append("")
        lines.append("分层计数（口径不同，不可合并成一个「覆盖率」）：")
        lines.extend(rep.tier_lines())
        if rep.failed_block:
            lines.append("")
            lines.append(f"❌ 硬失败（CI 会红）{len(rep.failed_block)} 条：")
            for r in rep.failed_block:
                lines.append(f"   - [{r.case_id}] {r.assertion_type}"
                             + (f"  -> {r.detail}" if r.detail else ""))
        if rep.failed_report:
            lines.append("")
            lines.append(f"⚠️  仅报告（不影响 CI）{len(rep.failed_report)} 条退化项：")
            for r in rep.failed_report:
                lines.append(f"   - [{r.case_id}] {r.assertion_type}"
                             + (f"  -> {r.detail}" if r.detail else ""))
        if rep.live_skipped:
            lines.append("")
            lines.append(f"🚫 **未执行** {len(rep.live_skipped)} 条 kind=live 断言"
                         f"（本 harness 未接线，不计入门禁）：")
            for cid in rep.live_skipped:
                lines.append(f"   - {cid}")
            lines.append("   ⚠️ 这些 case **没有产生任何判定**。"
                         "它们的 id 不在上面的「通过」里，也不要当成通过；")
            lines.append("      live 判的是真实模型输出，需要接 LLM 才能跑"
                         "（CI 按架构 §8.2 不引外部依赖）。")
            lines.append("      已执行的 compiled / reply 类断言覆盖的是"
                         "编译产物与预置样本，不等于模型行为已验证。")
        if rep.live_notes:
            lines.append("")
            lines.append("ℹ️  live 抽样（仅参考）：")
            for n in rep.live_notes:
                lines.append(f"   - {n}")
        if not rep.failed_block and not rep.failed_report:
            lines.append("")
            if rep.live_skipped:
                # ⚠️ 有未执行的 live 时**不能说「全部通过」** ——
                # 那 N 条不是通过了，是没跑。不区分这两种状态正是「夸大门禁」
                # 的起点：读者会把「没失败」当成「已验证」。
                lines.append(f"✅ 已执行的 {len(rep.passed)} 条断言通过"
                             f"（compiled {rep.passed_by_kind('compiled')}"
                             f" / reply {rep.passed_by_kind('reply')}）")
                lines.append(f"🚫 另有 {len(rep.live_skipped)} 条 live 断言**未执行**，"
                             f"它们既没通过也没失败 —— 是**未判定**。")
            else:
                lines.append(f"✅ 全部 {len(rep.passed)} 条断言通过")
        lines.append("")
        lines.append(f"结论：{'FAIL（存在硬失败）' if rep.failed_block else 'PASS'}"
                     f"，exit code = {rep.exit_code}")
        return "\n".join(lines)


def run_suites(paths: Iterable[str | Path] | None = None,
               live_sample: int = 0) -> tuple[int, list[Report]]:
    """跑一批套件，返回 (总 exit code, 各套件报告)。"""
    if paths is None:
        paths = sorted(SUITES_DIR.glob("*.json"))
    runner = EvalRunner()
    reports: list[Report] = []
    for p in paths:
        try:
            suite = runner.load_suite(p)
        except Exception as exc:  # noqa: BLE001  套件坏了也要报出来，不能静默跳过
            print(f"❌ 套件加载失败 {p}：{exc}")
            reports.append(Report(suite=str(p), policy_version="",
                                  failed_block=[CaseResult(
                                      case_id="load", category="", kind="compiled",
                                      ok=False, severity=BLOCK, detail=str(exc))]))
            continue
        rep = runner.run(suite, live_sample=live_sample)
        reports.append(rep)
        print(EvalRunner.render(rep))
        print()
    code = 1 if any(r.exit_code for r in reports) else 0
    return code, reports