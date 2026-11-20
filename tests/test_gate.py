"""门禁判定测试：安全零容忍、分维度阈值、显著性、性能护栏、隔离区。

门禁是「可决策」的最后一公里：算错了要么拦住正常发版，要么放过事故版本。
"""

from __future__ import annotations

import unittest

from atk.config import Config, Profile
from atk.metrics.aggregate import CaseAggregate, DimensionMetric, RunSummary
from atk.model import VersionQuad
from atk.pipeline.gate import evaluate_gate


def make_summary(
    *,
    run_id: str = "run",
    safety=(10, 10),
    correctness=(10, 10),
    completeness=(10, 10),
    latency: float = 1000.0,
    cost: float = 0.01,
    cases: dict | None = None,
    flaky: list[str] | None = None,
) -> RunSummary:
    summary = RunSummary(
        run_id=run_id,
        version_quad=VersionQuad(model="m", prompt="p", tool_schema="t", dataset="d"),
        samples=1,
    )
    for dim, (passed, total) in (
        ("safety", safety),
        ("correctness", correctness),
        ("completeness", completeness),
    ):
        summary.dimensions[dim] = DimensionMetric(
            name=dim, passed_samples=passed, total_samples=total, cases=total
        )
        summary.dimensions[dim].ci_low, summary.dimensions[dim].ci_high = (0.0, 1.0)
    summary.dimensions["efficiency"] = DimensionMetric(name="efficiency")
    summary.cases = cases or {
        "C1": CaseAggregate(
            case_id="C1",
            samples=1,
            passes=1,
            mean_latency_ms=latency,
            mean_cost_usd=cost,
            dimension_pass={"safety": safety[0], "correctness": correctness[0]},
            dimension_samples={"safety": safety[1], "correctness": correctness[1]},
        )
    }
    summary.flaky_cases = flaky or []
    return summary


class TestSafetyGate(unittest.TestCase):
    def test_safety_failure_always_blocks(self):
        current = make_summary(run_id="new", safety=(9, 10))
        baseline = make_summary(run_id="old")
        gate = evaluate_gate(current, baseline, Config())
        self.assertEqual(gate.decision, "BLOCK")
        self.assertIn("SAFETY_FAILED", [i.code for i in gate.blocks()])

    def test_safety_blocks_even_in_report_only(self):
        current = make_summary(run_id="new", safety=(9, 10))
        gate = evaluate_gate(current, make_summary(), Config(), mode="report_only")
        # report_only 不阻断，但仍然要留告警（否则周报会看不到安全问题）
        self.assertEqual(gate.decision, "REPORT")
        self.assertTrue(gate.warnings())


class TestDimensionGate(unittest.TestCase):
    @staticmethod
    def no_power_guard() -> Config:
        """只测显著性逻辑时关掉统计功效护栏（样本量小是构造数据，不是真实评估集）。"""
        config = Config()
        config.thresholds.min_dimension_samples = 0
        return config

    def test_significant_regression_blocks_full_mode(self):
        # 10/10 -> 2/10：差距足够大，Fisher 显著且 CI 不跨 0
        current = make_summary(run_id="new", correctness=(2, 10))
        baseline = make_summary(run_id="old", correctness=(10, 10))
        gate = evaluate_gate(current, baseline, self.no_power_guard(), mode="full")
        self.assertEqual(gate.decision, "BLOCK")
        self.assertIn("DIM_CORRECTNESS", [i.code for i in gate.blocks()])

    def test_significant_regression_only_warns_in_hard_mode(self):
        current = make_summary(run_id="new", correctness=(2, 10))
        baseline = make_summary(run_id="old", correctness=(10, 10))
        gate = evaluate_gate(current, baseline, self.no_power_guard(), mode="hard")
        self.assertEqual(gate.decision, "WARN")
        self.assertIn("DIM_CORRECTNESS", [i.code for i in gate.warnings()])

    def test_low_power_dimension_only_warns(self):
        """样本数 < min_dimension_samples 时，即使显著也只告警（P2-6）。"""
        current = make_summary(run_id="new", correctness=(2, 10))
        baseline = make_summary(run_id="old", correctness=(10, 10))
        config = Config()
        config.thresholds.min_dimension_samples = 30
        gate = evaluate_gate(current, baseline, config, mode="full")
        self.assertEqual(gate.decision, "WARN")
        self.assertIn("DIM_CORRECTNESS_LOWPOWER", [i.code for i in gate.warnings()])
        self.assertFalse(gate.blocks())

    def test_small_nonsignificant_regression_warns(self):
        current = make_summary(run_id="new", correctness=(19, 20))
        baseline = make_summary(run_id="old", correctness=(20, 20))
        gate = evaluate_gate(current, baseline, Config(), mode="full")
        # 退化 5pp > 2pp 阈值，但样本小、不显著 → 只告警
        self.assertEqual(gate.decision, "WARN")
        self.assertTrue(any(code.endswith("_NS") for code in [i.code for i in gate.issues]))

    def test_no_baseline_passes(self):
        gate = evaluate_gate(make_summary(), None, Config())
        self.assertEqual(gate.decision, "PASS")


class TestPerformanceGate(unittest.TestCase):
    def test_latency_degradation_blocks(self):
        current = make_summary(run_id="new", latency=2000)
        baseline = make_summary(run_id="old", latency=1000)
        gate = evaluate_gate(current, baseline, Config())
        self.assertIn("LATENCY_DEGRADED", [i.code for i in gate.blocks()])

    def test_cost_degradation_blocks(self):
        current = make_summary(run_id="new", cost=0.02)
        baseline = make_summary(run_id="old", cost=0.01)
        gate = evaluate_gate(current, baseline, Config())
        self.assertIn("COST_DEGRADED", [i.code for i in gate.blocks()])

    def test_acceptable_overhead_passes(self):
        current = make_summary(run_id="new", latency=1100, cost=0.0105)
        baseline = make_summary(run_id="old", latency=1000, cost=0.01)
        gate = evaluate_gate(current, baseline, Config())
        self.assertEqual(gate.decision, "PASS")


class TestRegressionDiffAndQuarantine(unittest.TestCase):
    def test_new_regression_reported(self):
        base_case = CaseAggregate(case_id="C1", samples=1, passes=1, title="用例1")
        cur_case = CaseAggregate(
            case_id="C1",
            samples=1,
            passes=0,
            title="用例1",
            failure_reason="工具未调用",
            failure_step=3,
            failure_classes={"planning": 1},
        )
        current = make_summary(run_id="new", cases={"C1": cur_case})
        baseline = make_summary(run_id="old", cases={"C1": base_case})
        gate = evaluate_gate(current, baseline, Config())
        self.assertEqual(len(gate.new_regressions), 1)
        self.assertEqual(gate.new_regressions[0]["case_id"], "C1")
        self.assertIn("NEW_REGRESSION", [i.code for i in gate.issues])

    def test_new_fix_reported(self):
        base_case = CaseAggregate(case_id="C1", samples=1, passes=0)
        cur_case = CaseAggregate(case_id="C1", samples=1, passes=1)
        current = make_summary(run_id="new", cases={"C1": cur_case})
        baseline = make_summary(run_id="old", cases={"C1": base_case})
        gate = evaluate_gate(current, baseline, Config())
        self.assertEqual(len(gate.new_fixes), 1)

    def test_quarantined_case_excluded(self):
        bad = CaseAggregate(case_id="C1", samples=1, passes=0, quarantined=True)
        current = make_summary(run_id="new", cases={"C1": bad}, safety=(0, 0))
        gate = evaluate_gate(current, None, Config())
        # 隔离区用例不计入分子分母 → 不应因它触发安全阻断
        self.assertNotIn("SAFETY_FAILED", [i.code for i in gate.blocks()])
        self.assertIn("C1", gate.excluded_cases)


class TestProfileMode(unittest.TestCase):
    def test_profile_blocking_mode_used(self):
        profile = Profile(name="weekly", blocking="report_only")
        current = make_summary(run_id="new", safety=(9, 10))
        gate = evaluate_gate(current, None, Config(), profile=profile)
        self.assertEqual(gate.blocking_mode, "report_only")
        self.assertEqual(gate.decision, "REPORT")


if __name__ == "__main__":
    unittest.main()
