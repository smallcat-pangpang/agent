"""断言注册表：类型名 → 实现，以及「类型 → 默认维度」映射。

新增一种断言的步骤：
1. 在 :mod:`atk.assertions.checks` 里实现一个带 ``type`` / ``dimension`` 的类；
2. 在下面的 ``_REGISTRY`` 里注册；
3. 把类型名加进 ``case.schema.json`` 的 ``auto_check.type`` 枚举。

注册表是**唯一**的断言清单，``loader`` 与 CLI 的 ``validate`` 都从这里取，
避免出现「Schema 说支持、代码没实现」的错位。
"""

from __future__ import annotations

from .base import AssertContext, Check, CheckRegistry
from .checks import (
    CostBudgetCheck,
    ExplainabilityCheck,
    FileExistsCheck,
    ForbiddenScanCheck,
    FormatSchemaCheck,
    LatencyBudgetCheck,
    NoHallucinationCheck,
    StateChangeCheck,
    ToolArgsCheck,
    ToolCalledCheck,
)

#: 全局注册表实例
_REGISTRY = CheckRegistry()

for _impl in (
    FileExistsCheck(),
    ToolCalledCheck(),
    ToolArgsCheck(),
    NoHallucinationCheck(),
    FormatSchemaCheck(),
    StateChangeCheck(),
    ForbiddenScanCheck(),
    LatencyBudgetCheck(),
    CostBudgetCheck(),
    ExplainabilityCheck(),
):
    _REGISTRY.register(_impl)


def get_registry() -> CheckRegistry:
    return _REGISTRY


def get_check(check_type: str) -> Check:
    return _REGISTRY.get(check_type)


#: 已实现的断言类型清单（loader/CLI 用于校验用例）
CHECK_TYPES: tuple[str, ...] = tuple(_REGISTRY.types())

#: 类型 → 默认维度（用例未显式声明 dimension 时使用）
DEFAULT_DIMENSIONS: dict[str, str] = {
    "file_exists": "completeness",
    "format_schema": "completeness",
    "tool_called": "correctness",
    "tool_args": "correctness",
    "no_hallucination": "correctness",
    "state_change": "correctness",
    "explainability": "correctness",
    "forbidden_scan": "safety",
    "latency_budget": "efficiency",
    "cost_budget": "efficiency",
}

__all__ = [
    "AssertContext",
    "CHECK_TYPES",
    "DEFAULT_DIMENSIONS",
    "get_check",
    "get_registry",
]
