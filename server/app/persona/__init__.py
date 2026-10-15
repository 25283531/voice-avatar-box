"""人格编译算法包（纯算法，不依赖 FastAPI / DB 会话）。

这个包与 ``app/memory/`` **平级**而不是它的子模块，理由是依赖方向：
人格编译是记忆模块的**上游调用方**（它需要记忆产出的画像与示例），
把它放进 memory/ 会暗示反过来的关系。它输入 spec + profile、输出 prompt 字符串，
是纯函数式算法，因此也不该在 routers/ 里 —— 那里的一切都绑在 HTTP 上。

一个刻意的约束：本包**不 import app.models / app.db**，所以它能在
"没有 engine、没有库"的纯函数环境里跑。eval 套件（server/evals/）正是
靠这一点做到 CI 里不需要起服务就能断言医疗红线是否齐全。
"""
from __future__ import annotations

from . import catalog, guardrails, spec
from .catalog import GUARDRAIL_VERSION, HARD_GUARDRAILS, TRAIT_SPECS
from .compiler import (
    ProfileMerger,
    PromptCompiler,
    compile_prompt,
    describe_traits,
    effective_traits,
)
from .guardrails import (
    GuardrailViolation,
    assert_present,
    filter_known_hard,
    regenerate_locked,
)
from .spec import RoleSpec, TraitValueError, default_spec

__all__ = [
    "catalog",
    "guardrails",
    "spec",
    "GUARDRAIL_VERSION",
    "HARD_GUARDRAILS",
    "TRAIT_SPECS",
    "ProfileMerger",
    "PromptCompiler",
    "compile_prompt",
    "describe_traits",
    "effective_traits",
    "assert_present",
    "regenerate_locked",
    # 读路径防御：装配 spec 时剔掉未经审核的 hard 键。
    # 导出它是为了让 tools/ 下的反向验证脚本能直接打这条防线。
    "filter_known_hard",
    "GuardrailViolation",
    "RoleSpec",
    "TraitValueError",
    "default_spec",
]