"""断言框架的基础设施：上下文、协议、注册表。

设计原则（规范 1.3）：

- **规则优先，Judge 兜底**：能规则化的绝不用 LLM 判；
- **状态为真相**：工具断言一律查「调用账本」，不查 Agent 自述；
- **黑名单优先**：``forbidden_scan`` 由框架强制运行，不依赖用例作者是否写；
- **短路省钱**：规则硬失败时不再调用 Judge（``rule_grader`` 里实现）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..config import Config
from ..envs.sandbox import Sandbox
from ..envs.tool_mock import ToolMockRegistry
from ..model import AutoCheck, CaseRun, RuleResult, TestCase


@dataclass
class AssertContext:
    """一次断言的执行上下文。

    断言只能通过它拿数据，禁止直接读全局状态 —— 这样断言可以被单测
    （构造一个假 ctx 即可），也保证了并发安全。
    """

    case: TestCase
    run: CaseRun
    #: 沙箱可能为空（例如只对历史记录做「离线复判」）
    sandbox: Sandbox | None = None
    registry: ToolMockRegistry | None = None
    #: 工程根目录，用于解析 fixture 相对路径
    base_dir: str = ""
    config: Config | None = None
    #: 断言过程中的附加信息（不参与判定，供报告展示）
    notes: list[str] = field(default_factory=list)

    # -- 便捷访问 ---------------------------------------------------------

    @property
    def metrics(self):
        return self.run.metrics

    @property
    def trajectory(self):
        return self.run.trajectory

    @property
    def invocations(self):
        if self.registry is not None:
            return self.registry.invocations
        return self.run.invocations

    def calls_of(self, tool: str):
        return [inv for inv in self.invocations if inv.tool == tool]

    def tool_sequence(self) -> list[str]:
        """真实调用序列（账本口径）。"""
        return [inv.tool for inv in self.invocations]

    def success_sequence(self) -> list[str]:
        return [inv.tool for inv in self.invocations if inv.ok]

    def resolve_path(self, path: str) -> str:
        """解析 fixture 相对路径：先工程根，再当前目录。"""
        import os

        if os.path.isabs(path):
            return path
        if self.base_dir:
            candidate = os.path.join(self.base_dir, path)
            if os.path.exists(candidate):
                return candidate
        return os.path.abspath(path)


@runtime_checkable
class Check(Protocol):
    """断言实现协议。"""

    #: 断言类型名（与 Schema 的 enum 对应）
    type: str
    #: 默认归属维度
    dimension: str

    def __call__(self, check: AutoCheck, ctx: AssertContext) -> RuleResult:
        ...


class CheckRegistry:
    """断言注册表：type -> 实现。"""

    def __init__(self) -> None:
        self._checks: dict[str, Check] = {}

    def register(self, check: Check) -> None:
        if check.type in self._checks:
            raise ValueError(f"断言类型重复注册: {check.type}")
        self._checks[check.type] = check

    def get(self, check_type: str) -> Check:
        if check_type not in self._checks:
            raise KeyError(f"未实现的断言类型: {check_type}")
        return self._checks[check_type]

    def types(self) -> list[str]:
        return sorted(self._checks)

    def run(self, check: AutoCheck, ctx: AssertContext) -> RuleResult:
        """执行单条断言，并把异常转成「失败」而不是让整轮跑批崩掉。

        断言本身写错（KeyError/TypeError）应该暴露为 ``error`` 信号，
        进入人工队列，而不是中断其它用例。
        """
        impl = self.get(check.type)
        dimension = check.dimension or getattr(impl, "dimension", "correctness")
        try:
            result = impl(check, ctx)
        except Exception as exc:  # noqa: BLE001 - 断言异常必须收敛
            return RuleResult(
                check_type=check.type,
                name=check.name,
                passed=False,
                detail=f"断言执行异常: {type(exc).__name__}: {exc}",
                signal="assertion.error",
                dimension=dimension,
                required=check.required,
            )
        # 用用例里的配置覆盖实现返回的默认值，保证「用例说了算」。
        # 例外：forbidden_scan 的维度由「命中的黑名单项」决定
        # （安全项归安全，谎报完成这类归正确性），不能被 check.dimension 一律盖成安全。
        if check.type != "forbidden_scan":
            result.dimension = check.dimension or result.dimension or dimension
        result.required = check.required
        result.name = check.name or result.name
        return result


def ok_result(
    check: AutoCheck,
    dimension: str,
    detail: str,
    evidence: dict[str, Any] | None = None,
) -> RuleResult:
    """构造一条「通过」的结果。"""
    return RuleResult(
        check_type=check.type,
        name=check.name,
        passed=True,
        detail=detail,
        signal="",
        dimension=dimension,
        required=check.required,
        evidence=evidence or {},
    )


def fail_result(
    check: AutoCheck,
    dimension: str,
    detail: str,
    signal: str,
    evidence: dict[str, Any] | None = None,
) -> RuleResult:
    """构造一条「失败」的结果；``signal`` 会被归因分类器使用。"""
    return RuleResult(
        check_type=check.type,
        name=check.name,
        passed=False,
        detail=detail,
        signal=signal,
        dimension=dimension,
        required=check.required,
        evidence=evidence or {},
    )
