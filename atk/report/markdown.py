"""Markdown 报告：按规范第 11 章的模板输出「一页能决策」的结论。

报告结构固定为五段，少一段都不行：

1. 概览（六行：安全/正确性/完整性/效率/P95/成本）
2. 新增退化（按影响排序，带首个出错 step）
3. 新增修复（**必须有**，否则团队只看到坏消息，会抵触测试体系）
4. 归因分布（六分类）
5. 建议（可执行，不含「加强测试」这种空话）

另外补充：pass^k 稳定性、维度覆盖、运行元信息、以及 Judge 的可信度说明。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..config import Config
from ..grading.calibration import CalibrationReport
from ..metrics.aggregate import RunSummary
from ..metrics.stats import required_sample_size
from ..model import DIMENSION_LABELS, FAILURE_LABELS
from ..pipeline.gate import GateResult
from ._common import fmt_pp, icon_for

TEMPLATE_TITLE = "Agent 版本评估报告"


def build_markdown(
    current: RunSummary,
    baseline: RunSummary | None,
    gate: GateResult,
    config: Config | None = None,
    judge_info: str = "",
    calibration: CalibrationReport | None = None,
    extra_notes: list[str] | None = None,
) -> str:
    """生成完整 Markdown 报告。"""
    config = config or Config()
    lines: list[str] = []
    quad_cur = current.version_quad
    quad_base = baseline.version_quad if baseline else None

    # ---- 标题与结论 ----
    lines.append("# Agent 版本评估报告")
    lines.append("")
    version_line = _version_line(quad_cur, quad_base)
    lines.append(f"**【版本】** {version_line}")
    lines.append(f"**【结论】** {gate.summary_line()}")
    if gate.blocked:
        top = gate.blocks()[0]
        lines.append(f"> 阻断原因：{top.message}")
    elif gate.warnings():
        lines.append(f"> 主要风险：{gate.warnings()[0].message}")
    lines.append("")
    lines.append(
        f"**运行信息**：`{current.run_id}` ｜ 档位 `{current.profile or '-'}` ｜ "
        f"用例 {current.case_count} 条 ｜ 采样 {sum(c.samples for c in current.cases.values())} 次 ｜ "
        f"生成于 {_fmt_time(current.generated_at)}"
    )
    lines.append("")

    # ---- 一、概览 ----
    lines.append("## 一、概览")
    lines.append("")
    lines.append("| 维度 | 状态 | 本次 | 基线 | 变化（95%CI） | 样本 |")
    lines.append("|---|---|---|---|---|---|")
    rows = {r["dimension"]: r for r in gate.dimensions}
    for dim in ("safety", "correctness", "completeness", "efficiency"):
        metric = current.dimension(dim)
        row = rows.get(dim, {})
        label = DIMENSION_LABELS.get(dim, dim)
        if metric.total_samples == 0:
            lines.append(f"| {label} | ⚪ | 未覆盖 | - | - | 0 |")
            continue
        base_metric = baseline.dimension(dim) if baseline else None
        base_text = (
            f"{base_metric.pass_rate:.1%}" if base_metric and base_metric.total_samples else "-"
        )
        if row.get("compared"):
            delta_text = (
                f"{fmt_pp(row['delta'])}（{row['ci_low'] * 100:+.1f}pp ~ "
                f"{row['ci_high'] * 100:+.1f}pp，"
                f"{'显著' if row['significant'] else '不显著'}）"
            )
        else:
            delta_text = "-"
        icon = icon_for(row, config)
        lines.append(
            f"| {label} | {icon} | {metric.pass_rate:.1%} | {base_text} | {delta_text} | "
            f"{metric.total_samples} |"
        )

    # 非功能指标
    perf = gate.performance
    lines.append(
        f"| P95 时延 | {_perf_icon(perf, 'latency')} | {perf.get('latency_p95_current', 0):.0f}ms | "
        f"{_fmt_opt(perf.get('latency_p95_baseline'), 'ms')} | {_ratio_text(perf.get('latency_ratio'))} | - |"
    )
    lines.append(
        f"| 平均成本 | {_perf_icon(perf, 'cost')} | ${perf.get('avg_cost_current', 0):.4f} | "
        f"{_fmt_opt(perf.get('avg_cost_baseline'), 'usd')} | {_ratio_text(perf.get('cost_ratio'))} | - |"
    )
    lines.append("")
    lines.append(
        f"**口径**：成功率为单次采样口径（pass@1）；`pass^k` 见附录。"
        f"「完成」定义为：最终结论正确 **且** 交付物可被打开/解析。"
    )
    lines.append("")

    # ---- 二、新增退化 ----
    lines.append("## 二、新增退化（按影响排序）")
    lines.append("")
    if gate.new_regressions:
        lines.append("| 优先级 | 用例 | 标题 | 失败归因 | 首个出错 step | 步数变化 |")
        lines.append("|---|---|---|---|---|---|")
        for item in gate.new_regressions[:20]:
            classes = ", ".join(
                FAILURE_LABELS.get(k, k) for k in (item.get("failure_classes") or {})
            )
            step = item.get("failure_step")
            lines.append(
                f"| {item.get('priority', '')} | `{item['case_id']}` | {item.get('title', '')} | "
                f"{classes or '-'} | {step if step is not None else '-'} | "
                f"{item.get('step_delta', 0):+.1f} |"
            )
        lines.append("")
        lines.append("**失败详情**：")
        lines.append("")
        for item in gate.new_regressions[:5]:
            lines.append(
                f"- `{item['case_id']}` {item.get('reason', '')}"
                f"（基线 {item.get('baseline_rate', 0):.0%} → 本次 {item.get('current_rate', 0):.0%}）"
            )
    else:
        lines.append("无新增退化。")
    lines.append("")

    # ---- 三、新增修复 ----
    lines.append("## 三、新增修复")
    lines.append("")
    if gate.new_fixes:
        for item in gate.new_fixes[:20]:
            lines.append(
                f"- `{item['case_id']}` {item.get('title', '')}"
                f"（基线 {item.get('baseline_rate', 0):.0%} → 本次 100%）"
            )
    else:
        lines.append(
            "本次无新增修复（或未提供基线）。"
            if baseline is None
            else "本次无新增修复：基线中失败的用例没有在本次转通过。"
        )
    lines.append("")

    # ---- 四、归因分布 ----
    lines.append("## 四、归因分布")
    lines.append("")
    distribution = current.failure_distribution
    if distribution:
        total = sum(distribution.values())
        lines.append("| 归因类 | 数量 | 占比 |")
        lines.append("|---|---|---|")
        for cls, count in distribution.items():
            lines.append(
                f"| {FAILURE_LABELS.get(cls, cls)} | {count} | {count / total:.0%} |"
            )
        lines.append("")
        unknown = distribution.get("unknown", 0)
        if unknown:
            lines.append(
                f"> ⚠️ 有 {unknown} 条失败未自动归因，已进入人工队列；"
                "若长期 >10%，说明归因规则需要补充。"
            )
    else:
        lines.append("本次无失败，归因分布为空。")
    lines.append("")

    # ---- 五、建议 ----
    lines.append("## 五、建议")
    lines.append("")
    for index, suggestion in enumerate(
        _suggestions(current, baseline, gate, config, calibration), start=1
    ):
        lines.append(f"{index}. {suggestion}")
    lines.append("")

    # ---- 附录 ----
    lines.append("## 附录 A. 稳定性（pass^k）")
    lines.append("")
    lines.append("| k | 总体 pass^k | 参与用例数 | 说明 |")
    lines.append("|---|---|---|---|")
    for k in range(1, 6):
        value, covered = current.overall_pass_k(k)
        if covered == 0:
            lines.append(f"| {k} | — | 0 | 采样次数不足，无法计算（需每用例 ≥{k} 次） |")
            continue
        note = "单次通过率" if k == 1 else f"{k} 次全过才算通过"
        if covered < current.case_count:
            note += f"（{current.case_count - covered} 条因采样不足被排除）"
        lines.append(f"| {k} | {value:.1%} | {covered} | {note} |")
    lines.append("")

    lines.append("## 附录 B. 维度覆盖与数据质量")
    lines.append("")
    for dim in ("correctness", "completeness", "efficiency", "safety"):
        metric = current.dimension(dim)
        label = DIMENSION_LABELS.get(dim, dim)
        if metric.total_samples == 0:
            lines.append(f"- ⚪ {label}：**未覆盖**（没有断言/用例覆盖该维度，不能当作满分）")
        else:
            lines.append(
                f"- {label}：覆盖 {metric.cases} 条用例 / {metric.total_samples} 次采样，"
                f"平均分 {metric.mean_score:.2f}"
            )
    if current.flaky_cases:
        lines.append(f"- ⚠️ Flaky 隔离区：{', '.join(current.flaky_cases)}（不计入门禁）")
    if not current.flaky_cases:
        lines.append("- Flaky：本次未发现同用例多轮结果不一致")
    lines.append("")

    lines.append("## 附录 C. Judge 可信度")
    lines.append("")
    if judge_info:
        lines.append(f"- 本次 Judge：`{judge_info}`")
    if calibration is not None:
        status = "可用于门禁" if calibration.gate_eligible else "**不得用于门禁**"
        if calibration.ineligible_dims:
            status += f"（禁用维度：{', '.join(calibration.ineligible_dims)}）"
        lines.append(
            f"- 校准结论：{status}（Kappa {calibration.overall_kappa:.2f}，"
            f"反向红队被骗率 {calibration.fooled_rate:.1%}）"
        )
        if calibration.ineligible_dims:
            lines.append(
                f"- ⚠️ Judge 分禁用维度：{', '.join(calibration.ineligible_dims)}；"
                "这些维度以规则断言为准，语义分仅作参考"
            )
        weak = [
            dim
            for dim, row in calibration.per_dimension.items()
            if row.get("kappa", 0) < 0.6
        ]
        if weak:
            lines.append(
                f"- ⚠️ 以下维度 Judge 一致性不足：{weak}；"
                "这些维度的结论请以规则断言为准，Judge 分只作参考。"
            )
        for reason in calibration.reasons[:3]:
            lines.append(f"- {reason}")
    else:
        lines.append(
            "- 未提供校准报告：Judge 分只能作为参考，**不得单独作为门禁依据**。"
            "运行 `atk calibrate` 生成 Kappa 证据。"
        )
    lines.append("")

    if extra_notes:
        lines.append("## 附录 D. 运行备注")
        lines.append("")
        for note in extra_notes:
            lines.append(f"- {note}")
        lines.append("")

    lines.append("---")
    lines.append(
        f"*由 atk 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}；"
        f"版本四元组：{quad_cur.model} / {quad_cur.prompt} / {quad_cur.tool_schema} / {quad_cur.dataset}*"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _version_line(current, baseline) -> str:
    """版本行：只展示发生变化的字段，避免一屏全是 unknown。"""
    if baseline is None:
        return (
            f"model `{current.model}` ｜ prompt `{current.prompt}` ｜ "
            f"tools `{current.tool_schema}` ｜ dataset `{current.dataset}`（无基线）"
        )
    parts: list[str] = []
    for label, attr in (
        ("model", "model"),
        ("prompt", "prompt"),
        ("tools", "tool_schema"),
        ("dataset", "dataset"),
    ):
        old = getattr(baseline, attr)
        new = getattr(current, attr)
        if old != new:
            parts.append(f"{label} `{old}` → `{new}`")
        else:
            parts.append(f"{label} `{new}`")
    return " ｜ ".join(parts)


def _ratio_text(ratio: float | None) -> str:
    if ratio is None:
        return "-"
    delta = ratio - 1
    return f"{delta:+.1%}"


def _fmt_opt(value: float | None, unit: str) -> str:
    if value is None:
        return "-"
    if unit == "ms":
        return f"{value:.0f}ms"
    return f"${value:.4f}"


def _perf_icon(perf: dict[str, Any], kind: str) -> str:
    ratio = perf.get(f"{kind}_ratio")
    if ratio is None:
        return "⚪"
    if ratio > (1.2 if kind == "latency" else 1.15):
        return "🔴"
    if ratio > 1.05:
        return "🟡"
    return "🟢"


def _fmt_time(value: str) -> str:
    if not value:
        return "-"
    return value.replace("T", " ").replace("+00:00", " UTC")


def _suggestions(
    current: RunSummary,
    baseline: RunSummary | None,
    gate: GateResult,
    config: Config,
    calibration: CalibrationReport | None,
) -> list[str]:
    """根据门禁结论生成**可执行**建议。

    原则：每条建议都要能落到「谁、改什么、怎么验证」，不写空话。
    """
    suggestions: list[str] = []
    blocks = gate.blocks()

    if blocks:
        seen_safety: set[str] = set()
        for issue in blocks[:3]:
            if issue.code == "SAFETY_FAILED":
                seen_safety.update(issue.case_ids)
                suggestions.append(
                    f"**立即阻断**：{', '.join(issue.case_ids[:5])} 安全用例失败，"
                    "修复前不得发布；同时复核同类攻击面是否还有未覆盖变体。"
                )
            elif issue.code == "FORBIDDEN_HIT":
                # 与安全失败重复时只保留一条，避免建议栏刷屏
                if set(issue.case_ids) <= seen_safety:
                    continue
                suggestions.append(
                    f"**立即阻断**：{', '.join(issue.case_ids[:5])} 命中禁止行为黑名单，"
                    "优先排查权限边界与确认机制，而不是调 Prompt。"
                )
            elif issue.code.startswith("DIM_"):
                dim = issue.dimension or "correctness"
                suggestions.append(
                    f"**{DIMENSION_LABELS.get(dim, dim)}显著退化**："
                    f"按归因分布定位到首个出错 step 后回滚对应改动；"
                    f"若归因为「规划」，优先检查 Prompt/工具描述而不是换模型。"
                )
            elif issue.code == "LATENCY_DEGRADED":
                suggestions.append(
                    "**时延劣化**：定位到最慢的单步（`atk report` 的 trace 里按 latency 排序），"
                    "常见原因是串行工具调用增加或重试风暴。"
                )
            elif issue.code == "COST_DEGRADED":
                suggestions.append(
                    "**成本劣化**：对比 token 与工具调用次数，区分是 Prompt 变长还是步数变多；"
                    "成本上涨 15% 以上通常不是模型单价的锅。"
                )
    else:
        suggestions.append("门禁通过，可进入灰度：先影子模式双跑对比，再 1% → 5% → 全量。")

    # 硬失败：即使安全没挂，也要给出「先修什么」的排序依据
    hard = [i for i in gate.issues if i.code == "HARD_FAILURE"]
    if hard:
        count = len(hard[0].case_ids)
        top_class, top_count = next(
            iter(current.failure_distribution.items()), ("unknown", 0)
        )
        suggestions.append(
            f"**硬失败优先修复**：{count} 条用例存在必失败断言或黑名单命中；"
            f"按归因分布，{FAILURE_LABELS.get(top_class, top_class)}类最多（{top_count} 次采样），"
            "建议先修这一类，通常能一次消掉最多失败。"
        )

    # 不显著退化 → 扩样
    for issue in gate.issues:
        if issue.code.endswith("_NS"):
            need = required_sample_size(p=0.8, target_mde=0.05)
            suggestions.append(
                f"**扩样复核**：{issue.message}。"
                f"按 MDE 估算，要稳定检测 5pp 的差异，每组约需 {need} 条用例"
                "（p=0.8，α=0.05，power=0.8）。"
            )
            break

    if current.flaky_cases:
        suggestions.append(
            f"**Flaky 复核**：{', '.join(current.flaky_cases[:5])} 已进隔离区，"
            "本周内确认是环境抖动还是模型本身不稳定；后者按真实缺陷修，不要长期隔离。"
        )

    if calibration is None or not calibration.gate_eligible:
        suggestions.append(
            "**校准 Judge**：当前 Judge 未通过校准（Kappa/红队），"
            "请先跑 `atk calibrate`，否则语义类结论只能当参考。"
        )

    missing_dims = [
        DIMENSION_LABELS[d]
        for d in ("correctness", "completeness", "efficiency", "safety")
        if current.dimension(d).total_samples == 0
    ]
    if missing_dims:
        suggestions.append(
            f"**补覆盖**：{', '.join(missing_dims)} 维度没有断言覆盖，"
            "门禁在该维度上等于没有防护。"
        )

    # 归因分布偏斜 → 指向系统性改进
    distribution = current.failure_distribution
    if distribution:
        top_class, top_count = next(iter(distribution.items()))
        total = sum(distribution.values())
        if total and top_count / total >= 0.5 and top_class != "unknown":
            suggestions.append(
                f"**系统性改进**：{FAILURE_LABELS.get(top_class, top_class)}类失败占 {top_count / total:.0%}，"
                "属于机制问题而非偶发，建议单独立项。"
            )

    if baseline is None:
        suggestions.append(
            "**建立基线**：本次没有基线，无法判断退化/修复。请把这版结果作为下次对比的基线。"
        )

    return suggestions
