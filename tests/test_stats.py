"""统计模块测试：pass^k、Wilson、Fisher、Kappa、MDE。

这些公式是「统计而非单点」原则的地基，算错了报告里的置信区间就是装饰品。
"""

from __future__ import annotations

import math
import unittest

from atk.metrics.stats import (
    bootstrap_ci,
    cohens_kappa,
    compare_proportions,
    diff_ci_wilson,
    fisher_exact,
    flaky_rate,
    mde,
    pass_at_k,
    pass_hat_k,
    pass_k_table,
    required_sample_size,
    spearman,
    wilson_ci,
)


class TestPassAtK(unittest.TestCase):
    def test_all_pass(self):
        self.assertAlmostEqual(pass_at_k(3, 3, 3), 1.0)

    def test_not_enough_success(self):
        self.assertEqual(pass_at_k(2, 3, 3), 0.0)

    def test_known_value(self):
        # c=2, n=3, k=2 -> C(2,2)/C(3,2) = 1/3
        self.assertAlmostEqual(pass_at_k(2, 3, 2), 1 / 3)

    def test_mean_over_cases(self):
        cases = {"a": (3, 3), "b": (2, 3)}
        expected = (1.0 + 1 / 3) / 2
        self.assertAlmostEqual(pass_hat_k(cases, 2), expected)

    def test_table(self):
        table = pass_k_table({"a": (3, 3)}, max_k=3)
        self.assertEqual(set(table), {1, 2, 3})
        self.assertAlmostEqual(table[3], 1.0)

    def test_empty(self):
        self.assertEqual(pass_hat_k({}, 3), 0.0)


class TestWilson(unittest.TestCase):
    def test_bounds_not_negative_for_small_sample(self):
        low, high = wilson_ci(0, 5)
        self.assertGreaterEqual(low, 0.0)
        self.assertLessEqual(high, 1.0)

    def test_contains_point_estimate(self):
        low, high = wilson_ci(8, 10)
        self.assertLess(low, 0.8)
        self.assertGreater(high, 0.8)

    def test_zero_n(self):
        self.assertEqual(wilson_ci(0, 0), (0.0, 1.0))

    def test_diff_ci(self):
        # 10/10 对 2/10：差距足够大，置信区间不应跨 0
        low, high = diff_ci_wilson(10, 10, 2, 10)
        self.assertGreater(low, 0)
        self.assertLess(high, 1)

    def test_compare_proportions_significant(self):
        test = compare_proportions(9, 10, 3, 10)
        self.assertTrue(test.significant)
        self.assertLess(test.p_value, 0.05)
        self.assertIn("95%CI", test.describe())

    def test_compare_proportions_not_significant(self):
        test = compare_proportions(80, 100, 78, 100)
        self.assertFalse(test.significant)


class TestFisher(unittest.TestCase):
    def test_classic_table(self):
        # 经典例子：[[8,2],[1,9]] 双尾 p ≈ 0.0055
        p = fisher_exact(8, 10, 1, 10)
        self.assertLess(p, 0.01)
        self.assertGreater(p, 0.001)

    def test_no_difference(self):
        p = fisher_exact(5, 10, 5, 10)
        self.assertAlmostEqual(p, 1.0, places=6)

    def test_empty_table(self):
        self.assertEqual(fisher_exact(0, 0, 0, 0), 1.0)


class TestKappa(unittest.TestCase):
    def test_perfect_agreement(self):
        self.assertAlmostEqual(cohens_kappa(["a", "b", "a"], ["a", "b", "a"]), 1.0)

    def test_chance_agreement(self):
        a = ["pass"] * 5 + ["fail"] * 5
        b = ["pass"] * 5 + ["fail"] * 5
        self.assertAlmostEqual(cohens_kappa(a, b), 1.0)

    def test_low_agreement(self):
        a = ["pass", "pass", "fail", "fail"]
        b = ["fail", "pass", "pass", "fail"]
        kappa = cohens_kappa(a, b)
        self.assertLess(kappa, 0.4)

    def test_empty(self):
        self.assertEqual(cohens_kappa([], []), 0.0)


class TestMde(unittest.TestCase):
    def test_known_value(self):
        # 两比例 MDE = (1.96+0.84) * sqrt(2p(1-p)/n)；p=0.8、n=50 时为 22.4%
        self.assertAlmostEqual(mde(0.8, 50), 0.224, places=3)

    def test_required_sample_size(self):
        n = required_sample_size(0.8, 0.05)
        self.assertGreater(n, 900)
        self.assertLess(n, 1100)

    def test_bootstrap_ci(self):
        low, high = bootstrap_ci([1, 2, 3, 4, 5], n_boot=200)
        self.assertLessEqual(low, high)


class TestCorrelationAndFlaky(unittest.TestCase):
    def test_spearman_monotonic(self):
        self.assertAlmostEqual(spearman([1, 2, 3], [10, 20, 30]), 1.0)

    def test_flaky_rate(self):
        rounds = [[True, True, False], [True, False, False]]
        # 第 2 个用例结果翻转 → 1/3
        self.assertAlmostEqual(flaky_rate(rounds), 1 / 3)

    def test_flaky_none(self):
        self.assertEqual(flaky_rate([[True, False], [True, False]]), 0.0)


if __name__ == "__main__":
    unittest.main()
