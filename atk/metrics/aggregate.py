"""聚合：把「一堆单次采样记录」变成「可做决策的指标」。

三个必须区分的口径（规范 3.2 强调「口径歧义是最大的坑」）：

1. **单次成功率**（pass@1）：所有采样里通过的比例；
2. **用例级 pass^k**：同用例跑 n 次、k 次全过的概率，按用例平均；
3. **维度覆盖率**：某维度只在「有用例覆盖」的样本上统计，
   绝不把「没测」当成「满分」——否则总分会被稀释成好看的数字。

所有指标都带样本量与 Wilson 置信区间，报告里禁止只写一个裸百分比。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..model import DIMENSIONS, CaseRun, VersionQuad
from .stats import mean, pass_at_k, pass_hat_k, p95, percentile, wilson_ci


# ---------------------------------------------------------------------------
# 单用例聚合
# ---------------------------------------------------------------------------


@dataclass
class CaseAggregate:
    """一个用例在本次运行中的多次采样汇总。"""

    case_id: str
    title: str = ""
    layer: str = ""
    priority: str = "P1"
    difficulty: str = ""
    samples: int = 0
    passes: int = 0
    errors: int = 0
    #: 硬失败样本数（命中必失败断言/黑名单），与「分数不够」区分
    hard_failures: int = 0
    #: 命中 block 级禁止行为的样本数（供 FORBIDDEN_HIT 精确判定，不再用得分==0 猜）
    forbidden_hits: int = 0
    #: 每维度的通过样本数 / 有效样本数（只统计覆盖该维度的样本）
    dimension_pass: dict[str, int] = field(default_factory=dict)
    dimension_samples: dict[str, int] = field(default_factory=dict)
    #: 每维度的平均分（含 Judge 融合后的分数）
    mean_score: dict[str, float] = field(default_factory=dict)
    mean_steps: float = 0.0
    mean_cost_usd: float = 0.0
    mean_latency_ms: float = 0.0
    failure_classes: dict[str, int] = field(default_factory=dict)
    flaky: bool = False
    quarantined: bool = False
    last_status: str = "pass"
    failure_step: int | None = None
    failure_reason: str = ""

    # -- 便捷指标 ---------------------------------------------------------

    @property
    def pass_rate(self) -> float:
        return self.passes / self.samples if self.samples else 0.0

    def pass_k(self, k: int) -> float:
        return pass_at_k(self.passes, self.samples, k)

    def dimension_rate(self, dim: str) -> float | None:
        n = self.dimension_samples.get(dim, 0)
        if not n:
            return None
        return self.dimension_pass.get(dim, 0) / n

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "layer": self.layer,
            "priority": self.priority,
            "difficulty": self.difficulty,
            "samples": self.samples,
            "passes": self.passes,
            "errors": self.errors,
            "hard_failures": self.hard_failures,
            "forbidden_hits": self.forbidden_hits,
            "dimension_pass": self.dimension_pass,
            "dimension_samples": self.dimension_samples,
            "mean_score": self.mean_score,
            "mean_steps": round(self.mean_steps, 2),
            "mean_cost_usd": round(self.mean_cost_usd, 6),
            "mean_latency_ms": round(self.mean_latency_ms, 1),
            "failure_classes": self.failure_classes,
            "flaky": self.flaky,
            "quarantined": self.quarantined,
            "last_status": self.last_status,
            "failure_step": self.failure_step,
            "failure_reason": self.failure_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaseAggregate":
        return cls(
            case_id=str(data.get("case_id", "")),
            title=str(data.get("title", "")),
            layer=str(data.get("layer", "")),
            priority=str(data.get("priority", "P1")),
            difficulty=str(data.get("difficulty", "")),
            samples=int(data.get("samples", 0)),
            passes=int(data.get("passes", 0)),
            errors=int(data.get("errors", 0)),
            hard_failures=int(data.get("hard_failures", 0)),
            # 兼容旧版 summary.json（没有该字段时用硬失败数近似）
            forbidden_hits=int(
                data.get("forbidden_hits", data.get("hard_failures", 0))
            ),
            dimension_pass={str(k): int(v) for k, v in (data.get("dimension_pass") or {}).items()},
            dimension_samples={
                str(k): int(v) for k, v in (data.get("dimension_samples") or {}).items()
            },
            mean_score={str(k): float(v) for k, v in (data.get("mean_score") or {}).items()},
            mean_steps=float(data.get("mean_steps", 0)),
            mean_cost_usd=float(data.get("mean_cost_usd", 0)),
            mean_latency_ms=float(data.get("mean_latency_ms", 0)),
            failure_classes={
                str(k): int(v) for k, v in (data.get("failure_classes") or {}).items()
            },
            flaky=bool(data.get("flaky", False)),
            quarantined=bool(data.get("quarantined", False)),
            last_status=str(data.get("last_status", "pass")),
            failure_step=data.get("failure_step"),
            failure_reason=str(data.get("failure_reason", "")),
        )


# ---------------------------------------------------------------------------
# 维度聚合
# ---------------------------------------------------------------------------


@dataclass
class DimensionMetric:
    """单个维度的统计结果。"""

    name: str
    cases: int = 0
    passed_samples: int = 0
    total_samples: int = 0
    ci_low: float = 0.0
    ci_high: float = 1.0
    pass_k: dict[int, float] = field(default_factory=dict)
    mean_score: float = 0.0

    @property
    def pass_rate(self) -> float:
        return self.passed_samples / self.total_samples if self.total_samples else 0.0

    def describe(self) -> str:
        if self.total_samples == 0:
            return "未覆盖"
        return (
            f"{self.pass_rate:.1%}（95%CI {self.ci_low:.1%}~{self.ci_high:.1%}，"
            f"{self.total_samples} 次采样）"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "cases": self.cases,
            "passed_samples": self.passed_samples,
            "total_samples": self.total_samples,
            "pass_rate": round(self.pass_rate, 4),
            "ci_low": round(self.ci_low, 4),
            "ci_high": round(self.ci_high, 4),
            "pass_k": {str(k): round(v, 4) for k, v in self.pass_k.items()},
            "mean_score": round(self.mean_score, 4),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DimensionMetric":
        return cls(
            name=str(data.get("name", "")),
            cases=int(data.get("cases", 0)),
            passed_samples=int(data.get("passed_samples", 0)),
            total_samples=int(data.get("total_samples", 0)),
            ci_low=float(data.get("ci_low", 0)),
            ci_high=float(data.get("ci_high", 1)),
            pass_k={int(k): float(v) for k, v in (data.get("pass_k") or {}).items()},
            mean_score=float(data.get("mean_score", 0)),
        )


# ---------------------------------------------------------------------------
# 总体聚合
# ---------------------------------------------------------------------------


@dataclass
class RunSummary:
    """一次完整运行的汇总（报告与门禁的输入）。"""

    run_id: str = ""
    generated_at: str = ""
    profile: str = ""
    agent: str = ""
    version_quad: VersionQuad = field(default_factory=VersionQuad)
    cases: dict[str, CaseAggregate] = field(default_factory=dict)
    dimensions: dict[str, DimensionMetric] = field(default_factory=dict)
    samples: int = 0
    #: 采样次数不足 k 的用例数（pass^k 的覆盖率，报告里要披露）
    pass_k_coverage: dict[int, int] = field(default_factory=dict)
    failure_distribution: dict[str, int] = field(default_factory=dict)
    quarantined_cases: list[str] = field(default_factory=list)
    flaky_cases: list[str] = field(default_factory=list)
    #: 原始样本状态，用于 Flaky 判定与逐轮对照
    sample_status: dict[str, list[str]] = field(default_factory=dict)

    # -- 核心指标 ---------------------------------------------------------

    @property
    def case_count(self) -> int:
        return len(self.cases)

    @property
    def pass_rate(self) -> float:
        """单次成功率（pass@1），门禁默认用它做维度对比。"""
        total = sum(c.samples for c in self.cases.values())
        passed = sum(c.passes for c in self.cases.values())
        return passed / total if total else 0.0

    def pass_ci(self, z: float = 1.96) -> tuple[float, float]:
        total = sum(c.samples for c in self.cases.values())
        passed = sum(c.passes for c in self.cases.values())
        return wilson_ci(passed, total, z)

    def overall_pass_k(self, k: int) -> tuple[float, int]:
        """总体 pass^k 与参与计算的用例数（采样不足 k 的用例被排除）。"""
        eligible = {
            cid: (c.passes, c.samples) for cid, c in self.cases.items() if c.samples >= k
        }
        if not eligible:
            return 0.0, 0
        return pass_hat_k(eligible, k), len(eligible)

    def dimension(self, name: str) -> DimensionMetric:
        return self.dimensions.get(name, DimensionMetric(name=name))

    @property
    def latency_p95(self) -> float:
        values = [
            c.mean_latency_ms for c in self.cases.values() if c.mean_latency_ms > 0
        ]
        return p95(values)

    @property
    def latency_p50(self) -> float:
        values = [
            c.mean_latency_ms for c in self.cases.values() if c.mean_latency_ms > 0
        ]
        return percentile(values, 0.5)

    @property
    def avg_cost_usd(self) -> float:
        return mean([c.mean_cost_usd for c in self.cases.values()])

    @property
    def avg_steps(self) -> float:
        return mean([c.mean_steps for c in self.cases.values()])

    @property
    def flaky_rate(self) -> float:
        return len(self.flaky_cases) / self.case_count if self.case_count else 0.0

    @property
    def safety_failures(self) -> list[str]:
        """安全维度出现失败的用例（门禁零容忍项）。"""
        out: list[str] = []
        for cid, case in self.cases.items():
            rate = case.dimension_rate("safety")
            if rate is not None and rate < 1.0:
                out.append(cid)
        return out

    def failed_cases(self) -> list[CaseAggregate]:
        return [c for c in self.cases.values() if c.passes < c.samples]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "profile": self.profile,
            "agent": self.agent,
            "version_quad": self.version_quad.to_dict(),
            "samples": self.samples,
            "case_count": self.case_count,
            "pass_rate": round(self.pass_rate, 4),
            "pass_ci": [round(x, 4) for x in self.pass_ci()],
            "pass_k": {
                str(k): round(self.overall_pass_k(k)[0], 4) for k in range(1, 6)
            },
            "pass_k_coverage": {str(k): v for k, v in self.pass_k_coverage.items()},
            "dimensions": {k: v.to_dict() for k, v in self.dimensions.items()},
            "cases": {k: v.to_dict() for k, v in self.cases.items()},
            "failure_distribution": self.failure_distribution,
            "quarantined_cases": self.quarantined_cases,
            "flaky_cases": self.flaky_cases,
            "latency_p50_ms": round(self.latency_p50, 1),
            "latency_p95_ms": round(self.latency_p95, 1),
            "avg_cost_usd": round(self.avg_cost_usd, 6),
            "avg_steps": round(self.avg_steps, 2),
            "sample_status": self.sample_status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunSummary":
        summary = cls(
            run_id=str(data.get("run_id", "")),
            generated_at=str(data.get("generated_at", "")),
            profile=str(data.get("profile", "")),
            agent=str(data.get("agent", "")),
            version_quad=VersionQuad.from_dict(data.get("version_quad") or {}),
            cases={
                cid: CaseAggregate.from_dict(item)
                for cid, item in (data.get("cases") or {}).items()
            },
            dimensions={
                name: DimensionMetric.from_dict(item)
                for name, item in (data.get("dimensions") or {}).items()
            },
            samples=int(data.get("samples", 0)),
            pass_k_coverage={
                int(k): int(v) for k, v in (data.get("pass_k_coverage") or {}).items()
            },
            failure_distribution={
                str(k): int(v) for k, v in (data.get("failure_distribution") or {}).items()
            },
            quarantined_cases=[str(x) for x in data.get("quarantined_cases") or []],
            flaky_cases=[str(x) for x in data.get("flaky_cases") or []],
            sample_status={
                str(k): [str(s) for s in v]
                for k, v in (data.get("sample_status") or {}).items()
            },
        )
        return summary


# ---------------------------------------------------------------------------
# 从运行记录构建汇总
# ---------------------------------------------------------------------------


def aggregate_runs(
    runs: Sequence[CaseRun],
    *,
    run_id: str = "",
    generated_at: str = "",
    profile: str = "",
    dimension_threshold: float = 0.6,
    flaky_rounds: int = 3,
    max_k: int = 5,
) -> RunSummary:
    """把同一次运行的所有采样记录聚合成 :class:`RunSummary`。

    Args:
        runs: 同一 run_id 下的全部采样记录。
        dimension_threshold: 维度得分达到多少算「该维度通过」。
        flaky_rounds: 连续几轮结果不一致算 Flaky（规范 3.3）。
        max_k: pass^k 计算到几。
    """
    summary = RunSummary(run_id=run_id, generated_at=generated_at, profile=profile)
    if not runs:
        return summary

    summary.version_quad = runs[0].version_quad
    summary.agent = runs[0].agent_name or ""
    summary.samples = len({r.sample_idx for r in runs}) or len(runs)

    # 按用例分组（保持稳定顺序）
    grouped: dict[str, list[CaseRun]] = {}
    for run in runs:
        grouped.setdefault(run.case_id, []).append(run)

    for case_id in sorted(grouped):
        samples = sorted(grouped[case_id], key=lambda r: r.sample_idx)
        agg = CaseAggregate(
            case_id=case_id,
            samples=len(samples),
            flaky=any(r.flaky for r in samples),
            quarantined=any(r.quarantined for r in samples),
        )
        statuses: list[str] = []
        scores: dict[str, list[float]] = {d: [] for d in DIMENSIONS}
        steps: list[float] = []
        costs: list[float] = []
        latencies: list[float] = []

        for run in samples:
            snap = run.case
            agg.title = agg.title or snap.title
            agg.layer = agg.layer or snap.layer
            agg.priority = snap.priority or agg.priority
            agg.difficulty = agg.difficulty or snap.difficulty
            statuses.append(run.verdict.final)

            if run.verdict.final == "pass":
                agg.passes += 1
            if run.verdict.final == "error":
                agg.errors += 1
            # 硬失败：命中必失败断言或黑名单（与「分数不够」区分，门禁策略不同）
            if run.verdict.rules_failed or run.verdict.forbidden_hits:
                agg.hard_failures += 1
            if run.verdict.forbidden_hits:
                agg.forbidden_hits += 1

            # 维度：只统计该样本真正覆盖到的维度
            for dim in DIMENSIONS:
                covered = dim in run.verdict.dimension_scores or (
                    dim == "safety" and run.verdict.forbidden_hits
                )
                if not covered:
                    continue
                agg.dimension_samples[dim] = agg.dimension_samples.get(dim, 0) + 1
                if run.verdict.passed_dimension(dim, dimension_threshold):
                    agg.dimension_pass[dim] = agg.dimension_pass.get(dim, 0) + 1
                if dim in run.verdict.dimension_scores:
                    scores[dim].append(run.verdict.dimension_scores[dim])

            steps.append(run.metrics.steps)
            costs.append(run.metrics.total_cost_usd)
            latencies.append(run.metrics.latency_ms)

            if run.verdict.final != "pass":
                cls = (run.attribution.failure_class if run.attribution else None) or (
                    run.verdict.failure_class or "unknown"
                )
                agg.failure_classes[cls] = agg.failure_classes.get(cls, 0) + 1
                if not agg.failure_reason:
                    agg.failure_reason = self_reason(run)
                    agg.failure_step = run.verdict.failure_step

        agg.mean_score = {
            dim: round(mean(values), 4) for dim, values in scores.items() if values
        }
        agg.mean_steps = mean(steps)
        agg.mean_cost_usd = mean(costs)
        agg.mean_latency_ms = mean(latencies)
        agg.last_status = statuses[-1] if statuses else "pass"
        # Flaky：同版本同用例多轮结果不一致
        agg.flaky = agg.flaky or (
            len(set(statuses)) > 1 and len(statuses) >= flaky_rounds
        )

        summary.cases[case_id] = agg
        summary.sample_status[case_id] = statuses
        if agg.flaky:
            summary.flaky_cases.append(case_id)
        if agg.quarantined:
            summary.quarantined_cases.append(case_id)

    # 维度聚合
    for dim in DIMENSIONS:
        metric = DimensionMetric(name=dim)
        for agg in summary.cases.values():
            n = agg.dimension_samples.get(dim, 0)
            if not n:
                continue
            metric.cases += 1
            metric.total_samples += n
            metric.passed_samples += agg.dimension_pass.get(dim, 0)
        if metric.total_samples:
            metric.ci_low, metric.ci_high = wilson_ci(
                metric.passed_samples, metric.total_samples
            )
            metric.pass_k = {
                k: round(
                    pass_hat_k(
                        {
                            cid: (
                                c.dimension_pass.get(dim, 0),
                                c.dimension_samples.get(dim, 0),
                            )
                            for cid, c in summary.cases.items()
                            if c.dimension_samples.get(dim, 0) >= k
                        },
                        k,
                    ),
                    4,
                )
                for k in range(1, max_k + 1)
            }
            score_values = [
                c.mean_score[dim] for c in summary.cases.values() if dim in c.mean_score
            ]
            metric.mean_score = round(mean(score_values), 4)
        summary.dimensions[dim] = metric

    # pass^k 覆盖率
    for k in range(1, max_k + 1):
        summary.pass_k_coverage[k] = sum(
            1 for c in summary.cases.values() if c.samples >= k
        )

    # 归因分布（按失败采样计）
    distribution: dict[str, int] = {}
    for run in runs:
        if run.verdict.final == "pass":
            continue
        cls = (run.attribution.failure_class if run.attribution else None) or (
            run.verdict.failure_class or "unknown"
        )
        distribution[cls] = distribution.get(cls, 0) + 1
    summary.failure_distribution = dict(
        sorted(distribution.items(), key=lambda kv: -kv[1])
    )

    return summary


def self_reason(run: CaseRun) -> str:
    """把一条失败记录压缩成一句话，供报告「新增退化」栏使用。"""
    if run.attribution and run.attribution.reasons:
        return run.attribution.reasons[0]
    if run.verdict.rules_failed:
        return run.verdict.rules_failed[0].detail
    return "未知原因"


def compare_dimensions(
    baseline: RunSummary, current: RunSummary
) -> list[dict[str, Any]]:
    """逐维度对比基线，返回结构化差异（门禁与报告共用）。"""
    from .stats import compare_proportions

    rows: list[dict[str, Any]] = []
    for dim in DIMENSIONS:
        base = baseline.dimension(dim)
        cur = current.dimension(dim)
        if base.total_samples == 0 or cur.total_samples == 0:
            rows.append(
                {
                    "dimension": dim,
                    "compared": False,
                    "reason": "基线或本次未覆盖该维度",
                    "baseline_rate": base.pass_rate if base.total_samples else None,
                    "current_rate": cur.pass_rate if cur.total_samples else None,
                }
            )
            continue
        test = compare_proportions(
            cur.passed_samples, cur.total_samples, base.passed_samples, base.total_samples
        )
        rows.append(
            {
                "dimension": dim,
                "compared": True,
                "baseline_rate": round(base.pass_rate, 4),
                "current_rate": round(cur.pass_rate, 4),
                "delta": test.delta,
                "ci_low": test.ci_low,
                "ci_high": test.ci_high,
                "p_value": test.p_value,
                "significant": test.significant,
                "method": test.method,
                "describe": test.describe(),
                "baseline_n": base.total_samples,
                "current_n": cur.total_samples,
            }
        )
    return rows
