"""eval harness：人格编译与安全不变量的离线门禁。

为什么放在 ``server/evals`` 而不是 ``tools/``：它需要import ``app.persona``，
而 tools/ 在仓库根、不在 ``server/`` 的包路径里。放进server 内部才能
``from app.persona...``。对外的CLI 入口仍然是 tools/eval_persona.py。
"""
from __future__ import annotations

from .assertions import ASSERTIONS, BLOCK, REPORT, run_assertion, severity_of
from .runner import CaseResult, EvalRunner, Report, run_suites

__all__ = [
    "ASSERTIONS",
    "BLOCK",
    "REPORT",
    "run_assertion",
    "severity_of",
    "CaseResult",
    "EvalRunner",
    "Report",
    "run_suites",
]