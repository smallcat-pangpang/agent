"""报告渲染的公共小工具（图标、格式化、阈值判断）。

把「什么颜色」这类判断收敛到一处，避免 Markdown 报告和 HTML 看板给出
互相矛盾的结论 —— 报告里同一份数据两种颜色，团队就再也不信报告了。
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..model import DIMENSION_LABELS


def fmt_pp(delta: float) -> str:
    """百分点格式：0.021 -> "+2.1pp"。"""
    return f"{delta * 100:+.1f}pp"


def fmt_rate(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def icon_for(row: dict[str, Any], config: Config) -> str:
    """按「维度 + 阈值 + 显著性」给出状态灯。

    🔴 阻断 / 🟡 告警 / 🟢 正常 / ⚪ 未覆盖
    """
    if not row.get("compared"):
        return "⚪"
    dim = row.get("dimension", "")
    tolerance = config.thresholds.tolerance(dim)
    delta = float(row.get("delta", 0.0))

    if dim == "safety":
        return "🟢" if float(row.get("current_rate") or 0) >= 1.0 else "🔴"

    if delta < -tolerance:
        return "🔴" if row.get("significant") else "🟡"
    if delta < 0:
        return "🟡"
    return "🟢"


def status_icon(decision: str) -> str:
    return {"PASS": "✅", "WARN": "⚠️", "BLOCK": "❌", "REPORT": "📄"}.get(decision, "❔")


def dimension_label(name: str) -> str:
    return DIMENSION_LABELS.get(name, name)


def markdown_escape(text: str) -> str:
    """转义表格里会破坏结构的字符（竖线与换行）。"""
    return str(text).replace("|", "\\|").replace("\n", " ")
