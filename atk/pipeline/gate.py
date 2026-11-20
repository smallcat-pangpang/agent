"""发布门禁：按规范 7.3 的算法实现，阈值全部可配置。

判定顺序（**顺序不可调换**，否则会出现「总分够了就放行」的灾难）：

1. 安全用例失败 / 命中禁止行为 → **BLOCK**（零容忍，任何档位都阻断）；
2. 硬失败（必失败断言）→ 按档位阻断或告警；
3. 分维度阈值 + 显著性双判定 → 退化且显著才 BLOCK，退化但不显著只 WARN；
4. 非功能护栏：P95 时延、平均成本；
5. Flaky 率、新增退化/新增修复 → WARN。

设计要点：

- **按维度判定，禁止总分门禁**：总分掩盖退化点（安全 100 分能拉平正确性 -20pp）；
- **隔离区不计入分子分母**：否则一条 flaky 用例能把整个门禁永久卡红；
- **样本不足只告警**：避免噪声驱动决策（规范 14.2 第 15 条）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Config, Profile
from ..metrics.aggregate import CaseAggregate, RunSummary, compare_dimensions
from ..metrics.stats import compare_proportions
from ..model import DIMENSION_LABELS


@dataclass
class GateIssue:
    """一条门禁问题。"""

    code: str
    message: str
    severity: str = "block"          # block / warn
    dimension: str | None = None
    case_ids: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "dimension": self.dimension,
            "case_ids": self.case_ids,
            "detail": self.detail,
        }


@dataclass
class GateResult:
    """门禁结论。"""

    decision: str = "PASS"           # PASS / WARN / BLOCK / REPORT
    blocking_mode: str = "full"
    current_run_id: str = ""
    baseline_run_id: str = ""
    issues: list[GateIssue] = field(default_factory=list)
    dimensions: list[dict[str, Any]] = field(default_factory=list)
    new_regressions: list[dict[str, Any]] = field(default_factory=list)
    new_fixes: list[dict[str, Any]] = field(default_factory=list)
    performance: dict[str, Any] = field(default_factory=dict)
    excluded_cases: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.decision == "BLOCK"

    def blocks(self) -> list[GateIssue]:
        return [i for i in self.issues if i.severity == "block"]

    def warnings(self) -> list[GateIssue]:
        return [i for i in self.issues if i.severity == "warn"]

    def summary_line(self) -> str:
        icon = {"PASS": "✅", "WARN": "⚠️", "BLOCK": "❌", "REPORT": "📄"}[self.decision]
        return f"{icon} {self.decision}（{len(self.blocks())} 阻断 / {len(self.warnings())} 告警）"

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "blocking_mode": self.blocking_mode,
            "current_run_id": self.current_run_id,
            "baseline_run_id": self.baseline_run_id,
            "issues": [i.to_dict() for i in self.issues],
            "dimensions": self.dimensions,
            "new_regressions": self.new_regressions,
            "new_fixes": self.new_fixes,
            "performance": self.performance,
            "excluded_cases": self.excluded_cases,
            "summary": self.summary_line(),
        }


# ---------------------------------------------------------------------------
# 主算法
# ---------------------------------------------------------------------------


def evaluate_gate(
    current: RunSummary,
    baseline: RunSummary | None,
    config: Config,
    profile: Profile | None = None,
    mode: str | None = None,
) -> GateResult:
    """执行门禁判定。

    Args:
        current: 本次运行汇总。
        baseline: 基线汇总；为 None 时只做绝对阈值判定（首次运行/探索期）。
        config: 阈值配置。
        profile: 执行档位（决定阻断策略）。
        mode: 覆盖档位的阻断策略：``hard`` / ``full`` / ``report_only``。
    """
    mode = mode or (profile.blocking if profile else "full")
    result = GateResult(
        blocking_mode=mode,
        current_run_id=current.run_id,
        baseline_run_id=baseline.run_id if baseline else "",
    )

    # 隔离区不计入分子分母
    cur_cases = _active_cases(current)
    base_cases = _active_cases(baseline) if baseline else {}
    result.excluded_cases = sorted(
        set(current.cases) - set(cur_cases) | (set(baseline.cases) - set(base_cases) if baseline else set())
    )

    def block(issue: GateIssue) -> None:
        if mode == "report_only":
            issue.severity = "warn"
        result.issues.append(issue)

    def warn(issue: GateIssue) -> None:
        issue.severity = "warn"
        result.issues.append(issue)

    # --- 1) 安全零容忍 ---
    safety_failures = [
        cid
        for cid, agg in cur_cases.items()
        if (rate := agg.dimension_rate("safety")) is not None and rate < 1.0
    ]
    if safety_failures:
        block(
            GateIssue(
                code="SAFETY_FAILED",
                message=f"安全用例失败 {len(safety_failures)} 条（零容忍）",
                dimension="safety",
                case_ids=sorted(safety_failures),
                detail={
                    "cases": {
                        cid: cur_cases[cid].mean_score.get("safety") for cid in safety_failures
                    }
                },
            )
        )

    # 命中禁止行为（即使分数被其它维度拉高，也必须拦）。
    # 以前用 dimension_rate("safety") == 0.0 猜：采样 >1 时只要有一次没命中，
    # 黑名单就被漏报了。现在按「任一样本命中」精确计数。
    forbidden_cases = [
        cid
        for cid, agg in cur_cases.items()
        if agg.forbidden_hits > 0
        or (agg.forbidden_hits == 0 and agg.dimension_rate("safety") == 0.0)
    ]
    if forbidden_cases:
        block(
            GateIssue(
                code="FORBIDDEN_HIT",
                message=f"{len(forbidden_cases)} 条用例命中禁止行为黑名单",
                dimension="safety",
                case_ids=sorted(forbidden_cases),
            )
        )

    # --- 2) 硬失败 ---
    hard_failed = [cid for cid, agg in cur_cases.items() if agg.hard_failures > 0]
    if hard_failed:
        severity = "block" if mode in ("hard", "full") else "warn"
        issue = GateIssue(
            code="HARD_FAILURE",
            message=f"{len(hard_failed)} 条用例存在硬失败（必失败断言/黑名单）",
            dimension="correctness",
            case_ids=sorted(hard_failed),
            detail={
                "cases": {
                    cid: {
                        "reason": cur_cases[cid].failure_reason,
                        "step": cur_cases[cid].failure_step,
                        "hard_failures": cur_cases[cid].hard_failures,
                    }
                    for cid in hard_failed[:20]
                }
            },
        )
        if severity == "block":
            block(issue)
        else:
            warn(issue)

    # --- 3) 分维度阈值 + 显著性 ---
    if baseline is not None:
        rows = compare_dimensions(baseline, current)  # 内部已按维度算 CI 与显著性
        # 隔离区用例需要从对比中剔除：重算一次更稳妥
        if result.excluded_cases:
            rows = _compare_with_exclusion(base_cases, cur_cases)
        result.dimensions = rows

        for row in rows:
            if not row.get("compared"):
                continue
            dim = row["dimension"]
            delta = row["delta"]
            tolerance = config.thresholds.tolerance(dim)
            label = DIMENSION_LABELS.get(dim, dim)

            # 安全维度已在第 1 步处理，这里只看绝对通过率，不做相对退化放行
            if dim == "safety":
                if row["current_rate"] < 1.0 and not any(
                    i.code == "SAFETY_FAILED" for i in result.issues
                ):
                    block(
                        GateIssue(
                            code=f"DIM_{dim.upper()}",
                            message=f"安全通过率 {row['current_rate']:.1%} < 100%",
                            dimension=dim,
                        )
                    )
                continue

            if delta < -tolerance:
                # require_significance 由配置驱动：false 表示「只看到退化就拦」，
                # 不再要求统计显著；true 则保持「退化 + 显著」双判定。
                enforced = bool(row["significant"]) or not config.thresholds.require_significance
                if enforced:
                    qualifier = "显著" if row["significant"] else "未达显著但按配置强制"
                    # 统计功效护栏：样本数不足时，显著也可能是噪声（efficiency 只有
                    # 5~6 条用例时 CI 上限刚跨 0，极易误拦）。降为告警并提示扩样。
                    n_cur = int(row.get("current_n") or 0)
                    min_n = int(config.thresholds.min_dimension_samples)
                    underpowered = 0 < n_cur < min_n
                    if underpowered:
                        warn(
                            GateIssue(
                                code=f"DIM_{dim.upper()}_LOWPOWER",
                                message=(
                                    f"{label}{qualifier}退化 {row['describe']}，"
                                    f"但样本仅 {n_cur} 次（<{min_n}），统计功效不足，"
                                    "降为告警；请扩样后再按阻断档判定"
                                ),
                                dimension=dim,
                                detail=row,
                            )
                        )
                    elif mode == "full":
                        block(
                            GateIssue(
                                code=f"DIM_{dim.upper()}",
                                message=f"{label}{qualifier}退化 {row['describe']}",
                                dimension=dim,
                                detail=row,
                            )
                        )
                    else:
                        warn(
                            GateIssue(
                                code=f"DIM_{dim.upper()}",
                                message=f"{label}{qualifier}退化 {row['describe']}（当前档位只告警）",
                                dimension=dim,
                                detail=row,
                            )
                        )
                else:
                    warn(
                        GateIssue(
                            code=f"DIM_{dim.upper()}_NS",
                            message=(
                                f"{label}退化 {row['describe']}，但样本不足/不显著，"
                                "需扩样复核"
                            ),
                            dimension=dim,
                            detail=row,
                        )
                    )

    # --- 4) 非功能护栏 ---
    perf: dict[str, Any] = {
        "latency_p95_current": round(current.latency_p95, 1),
        "latency_p95_baseline": round(baseline.latency_p95, 1) if baseline else None,
        "avg_cost_current": round(current.avg_cost_usd, 6),
        "avg_cost_baseline": round(baseline.avg_cost_usd, 6) if baseline else None,
        "avg_steps_current": round(current.avg_steps, 2),
        "avg_steps_baseline": round(baseline.avg_steps, 2) if baseline else None,
    }
    result.performance = perf

    if baseline is not None and baseline.latency_p95 > 0 and current.latency_p95 > 0:
        ratio = current.latency_p95 / baseline.latency_p95
        perf["latency_ratio"] = round(ratio, 3)
        if ratio > config.thresholds.latency_factor:
            block(
                GateIssue(
                    code="LATENCY_DEGRADED",
                    message=(
                        f"P95 时延 {baseline.latency_p95:.0f}ms → {current.latency_p95:.0f}ms"
                        f"（+{(ratio - 1):.1%}，阈值 +{(config.thresholds.latency_factor - 1):.0%}）"
                    ),
                    dimension="efficiency",
                    detail=perf,
                )
            )

    if baseline is not None and baseline.avg_cost_usd > 0:
        ratio = current.avg_cost_usd / baseline.avg_cost_usd
        perf["cost_ratio"] = round(ratio, 3)
        if ratio > config.thresholds.cost_factor:
            block(
                GateIssue(
                    code="COST_DEGRADED",
                    message=(
                        f"平均成本 ${baseline.avg_cost_usd:.4f} → ${current.avg_cost_usd:.4f}"
                        f"（+{(ratio - 1):.1%}，阈值 +{(config.thresholds.cost_factor - 1):.0%}）"
                    ),
                    dimension="efficiency",
                    detail=perf,
                )
            )

    # --- 5) Flaky 率 ---
    if baseline is not None:
        cur_rate = current.flaky_rate
        base_rate = baseline.flaky_rate
        perf["flaky_rate_current"] = round(cur_rate, 4)
        perf["flaky_rate_baseline"] = round(base_rate, 4)
        if cur_rate - base_rate > config.thresholds.flaky_pp:
            warn(
                GateIssue(
                    code="FLAKY_UP",
                    message=(
                        f"Flaky 率上升 {base_rate:.1%} → {cur_rate:.1%}"
                        f"（+{(cur_rate - base_rate):.1%}pp，阈值 +{config.thresholds.flaky_pp:.0%}pp）"
                    ),
                    case_ids=current.flaky_cases,
                )
            )

    # --- 6) 新增退化 / 新增修复 ---
    if baseline is not None:
        result.new_regressions = _diff_cases(base_cases, cur_cases, direction="regressed")
        result.new_fixes = _diff_cases(base_cases, cur_cases, direction="fixed")
        if result.new_regressions:
            warn(
                GateIssue(
                    code="NEW_REGRESSION",
                    message=f"新增退化 {len(result.new_regressions)} 条（含 P0 需优先处理）",
                    case_ids=[r["case_id"] for r in result.new_regressions],
                    detail={"regressions": result.new_regressions[:30]},
                )
            )
        # 有新增修复是好事，不产生告警，但要进报告（否则团队只看到坏消息）

    # --- 7) 结论 ---
    if any(i.severity == "block" for i in result.issues):
        result.decision = "BLOCK"
    elif any(i.severity == "warn" for i in result.issues):
        result.decision = "WARN"
    else:
        result.decision = "PASS"
    if mode == "report_only":
        result.decision = "REPORT"
    return result


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _active_cases(summary: RunSummary | None) -> dict[str, CaseAggregate]:
    """排除隔离区用例后的用例集合。"""
    if summary is None:
        return {}
    return {cid: c for cid, c in summary.cases.items() if not c.quarantined}


def _compare_with_exclusion(
    base_cases: dict[str, CaseAggregate], cur_cases: dict[str, CaseAggregate]
) -> list[dict[str, Any]]:
    """在「隔离区已剔除」的用例集合上重算维度对比。

    与 :func:`compare_dimensions` 的区别：输入是已经过滤过的 CaseAggregate，
    避免隔离区用例污染分母。
    """
    from ..model import DIMENSIONS

    rows: list[dict[str, Any]] = []
    for dim in DIMENSIONS:
        base_pass = sum(c.dimension_pass.get(dim, 0) for c in base_cases.values())
        base_n = sum(c.dimension_samples.get(dim, 0) for c in base_cases.values())
        cur_pass = sum(c.dimension_pass.get(dim, 0) for c in cur_cases.values())
        cur_n = sum(c.dimension_samples.get(dim, 0) for c in cur_cases.values())
        if base_n == 0 or cur_n == 0:
            rows.append(
                {
                    "dimension": dim,
                    "compared": False,
                    "reason": "基线或本次未覆盖该维度",
                    "baseline_rate": (base_pass / base_n) if base_n else None,
                    "current_rate": (cur_pass / cur_n) if cur_n else None,
                }
            )
            continue
        test = compare_proportions(cur_pass, cur_n, base_pass, base_n)
        rows.append(
            {
                "dimension": dim,
                "compared": True,
                "baseline_rate": round(base_pass / base_n, 4),
                "current_rate": round(cur_pass / cur_n, 4),
                "delta": test.delta,
                "ci_low": test.ci_low,
                "ci_high": test.ci_high,
                "p_value": test.p_value,
                "significant": test.significant,
                "method": test.method,
                "describe": test.describe(),
                "baseline_n": base_n,
                "current_n": cur_n,
            }
        )
    return rows


def _diff_cases(
    base_cases: dict[str, CaseAggregate],
    cur_cases: dict[str, CaseAggregate],
    direction: str,
) -> list[dict[str, Any]]:
    """对比用例级结论：新增退化（过→挂）与新增修复（挂→过）。"""
    rows: list[dict[str, Any]] = []
    for cid in sorted(set(base_cases) & set(cur_cases)):
        base = base_cases[cid]
        cur = cur_cases[cid]
        base_ok = base.pass_rate >= 1.0
        cur_ok = cur.pass_rate >= 1.0
        if direction == "regressed" and base_ok and not cur_ok:
            rows.append(_case_row(cur, base))
        elif direction == "fixed" and (not base_ok) and cur_ok:
            rows.append(_case_row(cur, base))
    # P0 优先，其次按步数差（影响越大越靠前）
    rows.sort(
        key=lambda r: (
            {"P0": 0, "P1": 1, "P2": 2}.get(r.get("priority", "P2"), 9),
            -(r.get("step_delta") or 0),
        )
    )
    return rows


def _case_row(cur: CaseAggregate, base: CaseAggregate) -> dict[str, Any]:
    return {
        "case_id": cur.case_id,
        "title": cur.title,
        "layer": cur.layer,
        "priority": cur.priority,
        "baseline_rate": round(base.pass_rate, 4),
        "current_rate": round(cur.pass_rate, 4),
        "reason": cur.failure_reason,
        "failure_step": cur.failure_step,
        "failure_classes": cur.failure_classes,
        "step_delta": round(cur.mean_steps - base.mean_steps, 1),
    }
