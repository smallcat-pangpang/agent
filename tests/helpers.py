"""测试辅助：构造用例、沙箱、工具账本与运行记录的工厂。

框架自身的测试必须**不依赖真实模型与网络**，全部用手工构造的轨迹，
这样断言库的每条规则都能被独立验证（而不是靠演示录制「自证」）。
"""

from __future__ import annotations

import os
import shutil
import uuid

from atk.envs.clock import FrozenClock
from atk.envs.sandbox import Sandbox
from atk.envs.tool_mock import ToolMockRegistry
from atk.model import (
    AutoCheck,
    CaseMeta,
    CaseRun,
    CaseSnapshot,
    EnvSpec,
    Expect,
    ExpectItem,
    ForbiddenItem,
    GradeSpec,
    JudgeSpec,
    RunMetrics,
    Step,
    TestCase,
    Trajectory,
    Verdict,
    VersionQuad,
)


def make_case(
    *,
    checks: list[AutoCheck] | None = None,
    forbidden: list[ForbiddenItem] | None = None,
    must: list[ExpectItem] | None = None,
    tool_mocks: dict | None = None,
    rubric: dict | None = None,
    layer: str = "L5",
    max_steps: int = 20,
) -> TestCase:
    """构造一条最小可用用例（默认 L5，带四维 rubric）。"""
    return TestCase(
        case_id="AGT-TST-0001",
        title="测试用例",
        layer=layer,
        input_turns=[],
        env=EnvSpec(
            tool_mocks=tool_mocks or {},
            seed=1,
            frozen_time="2026-11-18T10:00:00+08:00",
            max_steps=max_steps,
        ),
        expect=Expect(
            must_achieve=must or [],
            allowed_paths=[],
            forbidden=forbidden or [],
            rubric=rubric or {"correctness": 0.5, "completeness": 0.3, "efficiency": 0.2},
        ),
        grade=GradeSpec(
            auto_checks=checks or [],
            judge=JudgeSpec(enabled=False),
        ),
        meta=CaseMeta(),
    )


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
#: 测试临时目录放在项目根（Windows 上系统临时目录/随机 mkdtemp 目录可能被安全策略拒绝写入）
TMP_ROOT = os.path.join(ROOT, ".atk-tmp")


class Harness:
    """一次性测试环境：临时沙箱 + 冻结时钟 + 工具账本。"""

    def __init__(self, case: TestCase) -> None:
        self.case = case
        os.makedirs(TMP_ROOT, exist_ok=True)
        # 不用 tempfile.mkdtemp：Windows 上部分安全策略会给它打上受限 ACL，
        # 导致在目录内无法再创建文件；这里用普通 makedirs + 随机后缀。
        self.tmpdir = os.path.join(TMP_ROOT, f"case-{uuid.uuid4().hex[:10]}")
        os.makedirs(self.tmpdir, exist_ok=True)
        self.sandbox = Sandbox.create(self.tmpdir, case.case_id, 1, case.env.frozen_time, 1)
        self.clock = FrozenClock(case.env.frozen_time)
        self.registry = ToolMockRegistry(
            env=case.env, sandbox=self.sandbox, clock=self.clock, base_dir="."
        )

    def call(self, tool: str, args: dict | None = None):
        return self.registry.call(tool, args or {})

    def write(self, name: str, content: str = "x") -> str:
        return self.sandbox.write_text(name, content)

    def cleanup(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def make_run(
    case: TestCase,
    steps: list[Step] | None = None,
    *,
    final_output: str = "",
    invocations=None,
    metrics: RunMetrics | None = None,
    artifacts: list[str] | None = None,
) -> CaseRun:
    return CaseRun(
        run_id="run-test",
        case_id=case.case_id,
        sample_idx=1,
        version_quad=VersionQuad(model="m", prompt="p", tool_schema="t", dataset="d"),
        trajectory=Trajectory(
            steps=steps or [], final_output=final_output, artifacts=artifacts or []
        ),
        invocations=list(invocations or []),
        metrics=metrics or RunMetrics(),
        verdict=Verdict(),
        case=CaseSnapshot.from_case(case),
    )


def executed_case(
    case: TestCase,
    harness: Harness,
    steps: list[Step],
    final_output: str,
    metrics: RunMetrics | None = None,
) -> CaseRun:
    """把「手工执行的轨迹」变成一条运行记录（账本来自 harness）。"""
    return make_run(
        case,
        steps,
        final_output=final_output,
        invocations=harness.registry.invocations,
        metrics=metrics,
        artifacts=harness.sandbox.artifacts(),
    )


def call_step(index: int, tool: str, args: dict) -> Step:
    return Step(index=index, type="tool_call", tool=tool, args=args)


def result_step(index: int, tool: str, ok: bool = True, content: str = "") -> Step:
    return Step(index=index, type="tool_result", tool=tool, ok=ok, content=content)
