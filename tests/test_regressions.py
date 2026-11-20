"""v1.0.1 修复项的回归测试（P0/P1/P2）。

这些用例专门锁住「文档说有、代码其实没有」和「运行期抓不到」的两类问题。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from atk.config import Config, ConfigError, load_config
from atk.grading.judge import JudgeScore, RULE_ONLY_DIMENSIONS, merge_judge_scores
from atk.metrics.aggregate import CaseAggregate, DimensionMetric, RunSummary
from atk.model import Verdict, VersionQuad
from atk.pipeline.gate import evaluate_gate
from atk.pipeline.impact import ImpactGitError, git_changed_files

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class TestConfigWiring(unittest.TestCase):
    def test_explicit_missing_path_raises(self):
        """P1-5：显式 --config 指向不存在的文件必须报错，不能静默用默认值。"""
        with self.assertRaises(ConfigError):
            load_config("config/this-file-does-not-exist.yaml")

    def test_default_missing_path_still_runs(self):
        """开箱可跑的语义保持不变。"""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                self.assertEqual(load_config().thresholds.dims["safety"], 0.0)
            finally:
                os.chdir(cwd)

    def test_dead_keys_are_loaded(self):
        """P1-4：这些键以前定义了没人读，现在必须影响行为。"""
        config = load_config(os.path.join(ROOT, "config", "atk.yaml"))
        self.assertIsInstance(config.thresholds.require_significance, bool)
        self.assertEqual(config.thresholds.min_dimension_samples, 30)
        self.assertEqual(config.snippet_chars, 600)
        self.assertEqual(config.judge.good_kappa, 0.80)

    def test_require_significance_false_blocks_without_significance(self):
        """P1-4：false 时退化即拦，不再被硬编码的显著性判断吃掉。"""
        def make(correctness):
            s = RunSummary(
                run_id="r",
                version_quad=VersionQuad(model="m", prompt="p", tool_schema="t", dataset="d"),
                samples=1,
            )
            for dim, (passed, total) in (
                ("safety", (10, 10)),
                ("correctness", correctness),
                ("completeness", (10, 10)),
            ):
                s.dimensions[dim] = DimensionMetric(
                    name=dim, passed_samples=passed, total_samples=total, cases=total
                )
            s.cases = {"C1": CaseAggregate(case_id="C1", samples=1, passes=1)}
            return s

        baseline, current = make((10, 10)), make((9, 10))
        config = Config()
        config.thresholds.min_dimension_samples = 0
        config.thresholds.require_significance = False
        gate = evaluate_gate(current, baseline, config, mode="full")
        self.assertIn("DIM_CORRECTNESS", [i.code for i in gate.blocks()])


class TestRuleOnlyDimensions(unittest.TestCase):
    def test_judge_cannot_rewrite_safety_score(self):
        """P0-2：安全维度只信规则断言。"""
        verdict = Verdict(dimension_scores={"safety": 0.0, "correctness": 1.0})
        merge_judge_scores(
            verdict,
            {
                "safety": JudgeScore(dimension="safety", score=1.0),
                "correctness": JudgeScore(dimension="correctness", score=0.0),
            },
        )
        self.assertIn("safety", RULE_ONLY_DIMENSIONS)
        self.assertEqual(verdict.dimension_scores["safety"], 0.0)
        self.assertIn("safety", verdict.judge_ignored_dims)
        self.assertLess(verdict.dimension_scores["correctness"], 1.0)


class TestImpactGitErrors(unittest.TestCase):
    def test_non_repo_reports_actionable_error(self):
        """P0-3：非 git 目录不再返回空列表 + 误导性报错。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ImpactGitError) as ctx:
                git_changed_files("HEAD~1", cwd=tmp)
            self.assertIn("不是 git 仓库", str(ctx.exception))


class TestValidateCounting(unittest.TestCase):
    """P2-5：validate 汇总必须说清「扫描了几个文件」，而不是拿成功加载数当总数。"""

    def test_parse_failure_counted_in_total_files(self):
        from atk.loader import load_cases

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.yaml"
            bad.write_text(
                "case_id: AGT-TST-9001\ntitle: 坏用例\nlayer: L9\n"
                "input:\n  turns: []\n  env: {}\nexpect: {}\ngrade: {}\n",
                encoding="utf-8",
            )
            load = load_cases([tmp], strict=False)
        self.assertEqual(load.total_files, 1)
        self.assertEqual(len(load.cases), 0)
        self.assertGreater(len(load.errors), 0)


class TestCalibrateBaseUrlAlias(unittest.TestCase):
    """P2-7：calibrate 与 run 的 Judge 地址参数同名；--base-url 保留为兼容别名。"""

    def test_both_names_map_to_same_field(self):
        from atk.cli import build_parser

        parser = build_parser()
        new_name = parser.parse_args(["calibrate", "--judge-base-url", "http://a"])
        legacy = parser.parse_args(["calibrate", "--base-url", "http://b"])
        self.assertEqual(new_name.base_url, "http://a")
        self.assertEqual(legacy.base_url, "http://b")


if __name__ == "__main__":
    unittest.main()
