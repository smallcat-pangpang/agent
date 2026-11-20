"""指标计算：统计工具 + 聚合。

- :mod:`atk.metrics.stats`：Wilson CI、pass^k、Fisher/卡方、MDE、Kappa；
- :mod:`atk.metrics.aggregate`：把采样记录聚合成 RunSummary。
"""

from __future__ import annotations

from .aggregate import (
    CaseAggregate,
    DimensionMetric,
    RunSummary,
    aggregate_runs,
    compare_dimensions,
)
from .stats import (
    bootstrap_ci,
    cohens_kappa,
    compare_proportions,
    diff_ci_wilson,
    fisher_exact,
    flaky_rate,
    mde,
    pass_at_k,
    pass_hat_k,
    p95,
    percentile,
    required_sample_size,
    spearman,
    wilson_ci,
)

__all__ = [
    "CaseAggregate",
    "DimensionMetric",
    "RunSummary",
    "aggregate_runs",
    "compare_dimensions",
    "bootstrap_ci",
    "cohens_kappa",
    "compare_proportions",
    "diff_ci_wilson",
    "fisher_exact",
    "flaky_rate",
    "mde",
    "pass_at_k",
    "pass_hat_k",
    "p95",
    "percentile",
    "required_sample_size",
    "spearman",
    "wilson_ci",
]
