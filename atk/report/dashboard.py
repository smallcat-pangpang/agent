"""趋势看板：版本 × 指标，退化一眼可见（规范 8.1「回归看板」）。

生成一个**自包含**的 HTML（内联 SVG + CSS，无外部依赖），可以直接挂到
静态站点或发到群里。图表包括：

- 总体通过率 + 正确性/完整性/安全三条维度线（同一坐标系，0~100%）；
- P95 时延与平均成本（各自坐标系）；
- 退化标注：通过率环比下降超过阈值的点标红；
- 明细表：每次运行的版本四元组、用例数、耗时、门禁结论（如有）。

SVG 全部手写，不引 echarts/plotly：评估看板不该再带一个 1MB 的前端依赖。
"""

from __future__ import annotations

import html
import os
from datetime import datetime
from typing import Any, Sequence

_CSS = """
body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
       margin: 0; padding: 28px; background: #f6f8fa; color: #1f2328; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 28px 0 10px; }
.sub { color: #6b7280; font-size: 13px; margin-bottom: 20px; }
.cards { display: flex; flex-wrap: wrap; gap: 14px; margin: 16px 0 8px; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px;
        padding: 14px 18px; min-width: 150px; box-shadow: 0 1px 2px rgba(0,0,0,.04); }
.card .k { color: #6b7280; font-size: 12px; }
.card .v { font-size: 24px; font-weight: 700; margin-top: 2px; }
.card .d { font-size: 12px; margin-top: 2px; }
.up { color: #16a34a; } .down { color: #dc2626; } .flat { color: #6b7280; }
.panel { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px;
         padding: 10px 14px; margin-bottom: 16px; overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13px; background: #fff; }
th, td { border: 1px solid #e5e7eb; padding: 7px 10px; text-align: left; }
th { background: #f8fafc; }
.legend { font-size: 12px; color: #374151; margin: 4px 0 0 8px; }
.legend span { margin-right: 16px; }
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
       margin-right: 5px; vertical-align: middle; }
footer { color: #9ca3af; font-size: 12px; margin-top: 30px; }
"""

#: 维度线颜色（与报告里的语义一致）
COLORS = {
    "overall": "#2563eb",
    "correctness": "#16a34a",
    "completeness": "#d97706",
    "safety": "#dc2626",
    "efficiency": "#7c3aed",
}


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _scale_y(value: float, y_min: float, y_max: float, top: float, height: float) -> float:
    if y_max <= y_min:
        return top + height
    ratio = (value - y_min) / (y_max - y_min)
    return top + height * (1 - ratio)


def line_chart(
    series: dict[str, list[float | None]],
    labels: Sequence[str],
    *,
    width: int = 860,
    height: int = 220,
    y_min: float = 0.0,
    y_max: float = 1.0,
    y_format: str = "percent",
    title: str = "",
    highlight_drops: bool = True,
    drop_threshold: float = 0.03,
) -> str:
    """画一张多序列折线图（返回 SVG 字符串）。

    Args:
        series: ``{序列名: [每轮的值]}``，``None`` 表示该轮缺失（断线不连）。
        labels: 横轴标签（通常是用例数/时间/版本）。
        highlight_drops: 是否把「环比下降超过阈值」的点标红。
    """
    pad_left, pad_right, pad_top, pad_bottom = 52, 16, 18, 34
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    n = max(len(labels), 1)

    def x_at(index: int) -> float:
        if n == 1:
            return pad_left + plot_w / 2
        return pad_left + plot_w * index / (n - 1)

    def y_at(value: float) -> float:
        return _scale_y(value, y_min, y_max, pad_top, plot_h)

    def fmt(value: float) -> str:
        if y_format == "percent":
            return f"{value:.0%}"
        if y_format == "ms":
            return f"{value:.0f}ms"
        if y_format == "usd":
            return f"${value:.3f}"
        return f"{value:.2f}"

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        'xmlns="http://www.w3.org/2000/svg" role="img">',
        f"<title>{_esc(title)}</title>",
    ]

    # 网格与 Y 轴刻度
    for i in range(5):
        value = y_min + (y_max - y_min) * i / 4
        y = y_at(value)
        parts.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" '
            'stroke="#e5e7eb" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_left - 8}" y="{y + 4:.1f}" font-size="10" fill="#6b7280" '
            f'text-anchor="end">{fmt(value)}</text>'
        )

    # X 轴标签（太多时抽稀）
    step = max(1, n // 8)
    for index, label in enumerate(labels):
        if index % step != 0 and index != n - 1:
            continue
        parts.append(
            f'<text x="{x_at(index):.1f}" y="{height - 12}" font-size="10" fill="#6b7280" '
            f'text-anchor="middle">{_esc(label)}</text>'
        )

    # 折线
    for name, values in series.items():
        color = COLORS.get(name, "#6b7280")
        segment: list[str] = []
        for index, value in enumerate(values[:n]):
            if value is None:
                continue
            segment.append(f"{x_at(index):.1f},{y_at(value):.1f}")
        if not segment:
            continue
        parts.append(
            f'<polyline points="{" ".join(segment)}" fill="none" stroke="{color}" '
            'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        )
        for index, value in enumerate(values[:n]):
            if value is None:
                continue
            # 退化标注：与本序列上一个有效值比较
            prev = next((v for v in reversed(values[:index]) if v is not None), None)
            dropped = (
                highlight_drops
                and prev is not None
                and (value - prev) < -drop_threshold
            )
            radius = 4 if dropped else 2.6
            fill = "#dc2626" if dropped else color
            parts.append(
                f'<circle cx="{x_at(index):.1f}" cy="{y_at(value):.1f}" r="{radius}" '
                f'fill="{fill}"><title>{_esc(name)}: {fmt(value)}</title></circle>'
            )

    parts.append("</svg>")
    return "".join(parts)


def build_dashboard(
    runs: Sequence[dict[str, Any]],
    out_path: str,
    title: str = "Agent 评估趋势看板",
    max_points: int = 12,
) -> str:
    """根据历史运行列表生成看板 HTML，返回文件路径。"""
    selected = list(runs)[-max_points:]
    labels = [
        (r.get("version_quad", {}).get("prompt") or r.get("run_id", "")[-8:]) for r in selected
    ]

    def dim_series(name: str) -> list[float | None]:
        return [
            (r.get("dimensions") or {}).get(name) if r.get("dimensions") else None
            for r in selected
        ]

    overall = [float(r.get("pass_rate") or 0.0) for r in selected]
    cards = _cards(selected)
    charts: list[str] = []

    charts.append(
        '<div class="panel">'
        + "<h2>通过率与维度趋势</h2>"
        + line_chart(
            {
                "overall": overall,
                "correctness": dim_series("correctness"),
                "completeness": dim_series("completeness"),
                "safety": dim_series("safety"),
            },
            labels,
            y_min=0,
            y_max=1,
            y_format="percent",
            title="通过率与维度趋势",
        )
        + '<div class="legend">'
        + "".join(
            f'<span><i class="dot" style="background:{COLORS[k]}"></i>{v}</span>'
            for k, v in (
                ("overall", "总体"),
                ("correctness", "正确性"),
                ("completeness", "完整性"),
                ("safety", "安全"),
            )
        )
        + "</div></div>"
    )

    # 时延图
    latency = [float(r.get("latency_p95_ms") or 0.0) for r in selected]
    if any(latency):
        ceiling = max(latency) * 1.15 or 1.0
        charts.append(
            '<div class="panel"><h2>P95 时延</h2>'
            + line_chart(
                {"overall": latency},
                labels,
                y_min=0,
                y_max=ceiling,
                y_format="ms",
                title="P95 时延",
            )
            + "</div>"
        )

    # 成本图
    cost = [float(r.get("avg_cost_usd") or 0.0) for r in selected]
    if any(cost):
        ceiling = max(cost) * 1.2 or 0.01
        charts.append(
            '<div class="panel"><h2>平均单任务成本</h2>'
            + line_chart(
                {"overall": cost},
                labels,
                y_min=0,
                y_max=ceiling,
                y_format="usd",
                title="平均成本",
            )
            + "</div>"
        )

    table = _runs_table(selected)

    body = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title><style>{_CSS}</style></head>
<body>
<h1>{_esc(title)}</h1>
<p class="sub">最近 {len(selected)} 次运行；红点表示该项指标环比明显下降。生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
{cards}
{''.join(charts)}
<h2>运行明细</h2>
<div class="panel">{table}</div>
<footer>数据来源：reports/runs/*/summary.json。每次运行都会新增一行，可直接归档对比。</footer>
</body></html>
"""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return out_path


def _cards(runs: Sequence[dict[str, Any]]) -> str:
    """顶部指标卡：最新值与环比。"""
    if not runs:
        return ""
    latest = runs[-1]
    prev = runs[-2] if len(runs) >= 2 else None

    def card(label: str, key: str, fmt: str, good: str = "up") -> str:
        value = float(latest.get(key) or 0.0)
        text = {"percent": f"{value:.1%}", "ms": f"{value:.0f}ms", "usd": f"${value:.4f}"}[fmt]
        delta_html = '<div class="d flat">—</div>'
        if prev is not None:
            old = float(prev.get(key) or 0.0)
            if old:
                delta = (value - old) / old
                cls = "up" if (delta > 0) == (good == "up") else "down"
                if abs(delta) < 0.005:
                    cls = "flat"
                delta_html = f'<div class="d {cls}">{delta:+.1%}</div>'
        return f'<div class="card"><div class="k">{_esc(label)}</div><div class="v">{text}</div>{delta_html}</div>'

    cards = [
        card("总体通过率", "pass_rate", "percent"),
        card("P95 时延", "latency_p95_ms", "ms", good="down"),
        card("平均成本", "avg_cost_usd", "usd", good="down"),
    ]
    if latest.get("case_count"):
        cards.append(
            f'<div class="card"><div class="k">用例数</div>'
            f'<div class="v">{int(latest["case_count"])}</div>'
            f'<div class="d flat">最近一次运行</div></div>'
        )
    return '<div class="cards">' + "".join(cards) + "</div>"


def _runs_table(runs: Sequence[dict[str, Any]]) -> str:
    header = (
        "<tr><th>运行</th><th>生成时间</th><th>档位</th><th>Agent</th>"
        "<th>版本（model/prompt/tools）</th><th>用例</th><th>通过率</th>"
        "<th>正确性</th><th>完整性</th><th>安全</th><th>P95</th><th>成本</th></tr>"
    )
    rows: list[str] = []
    for run in reversed(list(runs)):
        quad = run.get("version_quad") or {}
        dims = run.get("dimensions") or {}
        rows.append(
            "<tr>"
            f"<td>{_esc(run.get('run_id', ''))}</td>"
            f"<td>{_esc((run.get('generated_at') or '').replace('T', ' ')[:19])}</td>"
            f"<td>{_esc(run.get('profile', ''))}</td>"
            f"<td>{_esc(run.get('agent', ''))}</td>"
            f"<td>{_esc(quad.get('model', ''))} / {_esc(quad.get('prompt', ''))} / "
            f"{_esc(quad.get('tool_schema', ''))}</td>"
            f"<td>{int(run.get('case_count') or 0)}</td>"
            f"<td>{float(run.get('pass_rate') or 0):.1%}</td>"
            f"<td>{_fmt_rate(dims.get('correctness'))}</td>"
            f"<td>{_fmt_rate(dims.get('completeness'))}</td>"
            f"<td>{_fmt_rate(dims.get('safety'))}</td>"
            f"<td>{float(run.get('latency_p95_ms') or 0):.0f}ms</td>"
            f"<td>${float(run.get('avg_cost_usd') or 0):.4f}</td>"
            "</tr>"
        )
    return f"<table>{header}{''.join(rows)}</table>"


def _fmt_rate(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.1%}"
