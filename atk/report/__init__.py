"""报告生成：Markdown / HTML / 趋势看板。

一行调用就能产出可交付的报告::

    paths = generate_reports(current, baseline, gate, out_dir="reports/runs/xxx")
"""

from __future__ import annotations

import os
from typing import Any

from ..config import Config
from ..grading.calibration import CalibrationReport
from ..metrics.aggregate import RunSummary
from ..pipeline.gate import GateResult
from .dashboard import build_dashboard, line_chart
from .html import build_html, render_markdown, save_html
from .markdown import build_markdown

__all__ = [
    "build_markdown",
    "build_html",
    "build_dashboard",
    "line_chart",
    "render_markdown",
    "save_html",
    "generate_reports",
]


def generate_reports(
    current: RunSummary,
    baseline: RunSummary | None,
    gate: GateResult,
    out_dir: str,
    config: Config | None = None,
    judge_info: str = "",
    calibration: CalibrationReport | None = None,
    extra_notes: list[str] | None = None,
    dashboard_path: str | None = None,
) -> dict[str, str]:
    """生成 Markdown + HTML 报告，返回路径字典。"""
    os.makedirs(out_dir, exist_ok=True)
    markdown_text = build_markdown(
        current,
        baseline,
        gate,
        config=config,
        judge_info=judge_info,
        calibration=calibration,
        extra_notes=extra_notes,
    )
    md_path = os.path.join(out_dir, "report.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(markdown_text)

    html_text = build_html(
        markdown_text,
        decision=gate.decision,
        subtitle=(
            f"{current.version_quad.model} / {current.version_quad.prompt} / "
            f"{current.version_quad.dataset} ｜ {current.run_id}"
        ),
        dashboard_link=dashboard_path,
    )
    html_path = os.path.join(out_dir, "report.html")
    save_html(html_text, html_path)

    return {"markdown": md_path, "html": html_path}
