"""HTML 报告：把 Markdown 渲染成可分享的单文件页面。

为什么自己写一个 Markdown 渲染器：报告要能直接进邮件/工单/静态站点，
而本项目坚持零第三方依赖。只实现报告用到的语法子集（标题/表格/列表/
加粗/行内代码/链接/引用/分隔线），渲染结果可预期、无脚本注入风险。

安全：所有文本先 HTML 转义，再按语法生成标签；**不执行**任何原始 HTML。
"""

from __future__ import annotations

import html
import re

_CSS = """
:root { color-scheme: light; }
body {
  font-family: -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  max-width: 1040px; margin: 32px auto; padding: 0 24px; color: #1f2328;
  line-height: 1.65; background: #fff;
}
h1 { font-size: 28px; border-bottom: 2px solid #e5e7eb; padding-bottom: 8px; }
h2 { font-size: 21px; margin-top: 32px; border-left: 4px solid #2563eb; padding-left: 10px; }
h3 { font-size: 17px; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 14px; }
th, td { border: 1px solid #e5e7eb; padding: 7px 10px; text-align: left; vertical-align: top; }
th { background: #f8fafc; font-weight: 600; }
tr:nth-child(even) td { background: #fcfcfd; }
code { background: #f3f4f6; padding: 1px 5px; border-radius: 4px; font-size: 13px;
       font-family: ui-monospace, Consolas, monospace; }
blockquote { margin: 12px 0; padding: 8px 14px; background: #fff8e6;
             border-left: 4px solid #f0b429; color: #5c4813; }
ul, ol { padding-left: 22px; }
li { margin: 3px 0; }
hr { border: none; border-top: 1px solid #e5e7eb; margin: 28px 0; }
.meta { color: #6b7280; font-size: 13px; }
.badge { display: inline-block; padding: 3px 10px; border-radius: 999px;
         font-size: 13px; font-weight: 600; }
.badge-pass { background: #dcfce7; color: #166534; }
.badge-warn { background: #fef3c7; color: #92400e; }
.badge-block { background: #fee2e2; color: #991b1b; }
.badge-report { background: #e0e7ff; color: #3730a3; }
footer { margin-top: 40px; color: #9ca3af; font-size: 12px; }
@media print { body { max-width: none; } h2 { break-after: avoid; } table { break-inside: avoid; } }
"""

_BADGE = {
    "PASS": ("badge-pass", "✅ PASS"),
    "WARN": ("badge-warn", "⚠️ WARN"),
    "BLOCK": ("badge-block", "❌ BLOCK"),
    "REPORT": ("badge-report", "📄 REPORT"),
}


def render_markdown(md: str) -> str:
    """把报告 Markdown 渲染成 HTML 片段（只支持报告用到的语法）。"""
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 空行
        if not stripped:
            i += 1
            continue

        # 分隔线
        if stripped in ("---", "***", "___"):
            out.append("<hr>")
            i += 1
            continue

        # 标题
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue

        # 表格：当前行是 |...|，下一行是分隔行
        if stripped.startswith("|") and i + 1 < n and _is_table_sep(lines[i + 1]):
            table_lines = [stripped]
            i += 1
            while i < n and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            out.append(_render_table(table_lines))
            continue

        # 引用
        if stripped.startswith(">"):
            quote: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append(f"<blockquote>{_inline(' '.join(quote))}</blockquote>")
            continue

        # 列表（有序/无序）
        if re.match(r"^([-*]|\d+\.)\s+", stripped):
            ordered = bool(re.match(r"^\d+\.", stripped))
            items: list[str] = []
            while i < n and re.match(r"^([-*]|\d+\.)\s+", lines[i].strip()):
                content = re.sub(r"^([-*]|\d+\.)\s+", "", lines[i].strip())
                items.append(f"<li>{_inline(content)}</li>")
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue

        # 普通段落（连续多行合并）
        paragraph: list[str] = [stripped]
        i += 1
        while i < n and lines[i].strip() and not _starts_block(lines[i].strip()):
            paragraph.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(paragraph))}</p>")

    return "\n".join(out)


def _starts_block(line: str) -> bool:
    return bool(
        re.match(r"^(#{1,6})\s+", line)
        or line.startswith("|")
        or line.startswith(">")
        or re.match(r"^([-*]|\d+\.)\s+", line)
        or line in ("---", "***", "___")
    )


def _is_table_sep(line: str) -> bool:
    stripped = line.strip().strip("|")
    if not stripped:
        return False
    cells = [c.strip() for c in stripped.split("|")]
    return all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c != "")


def _render_table(table_lines: list[str]) -> str:
    def cells(row: str) -> list[str]:
        row = row.strip()
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|"):
            row = row[:-1]
        return [c.strip() for c in row.split("|")]

    header = cells(table_lines[0])
    body_rows = table_lines[2:] if len(table_lines) > 1 else []
    html_parts = ["<table>", "<thead><tr>"]
    for cell in header:
        html_parts.append(f"<th>{_inline(cell)}</th>")
    html_parts.append("</tr></thead><tbody>")
    for row in body_rows:
        values = cells(row)
        html_parts.append("<tr>")
        for cell in values:
            html_parts.append(f"<td>{_inline(cell)}</td>")
        html_parts.append("</tr>")
    html_parts.append("</tbody></table>")
    return "".join(html_parts)


def _inline(text: str) -> str:
    """行内语法：先转义，再替换标记，避免 XSS。"""
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", escaped)
    escaped = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{_safe_url(m.group(2))}">{m.group(1)}</a>',
        escaped,
    )
    return escaped


def _safe_url(url: str) -> str:
    lowered = url.strip().lower()
    if lowered.startswith(("http://", "https://", "mailto:", "/", "#", "./")):
        return html.escape(url, quote=True)
    return "#"


def build_html(
    markdown_text: str,
    decision: str,
    title: str = "Agent 版本评估报告",
    subtitle: str = "",
    dashboard_link: str | None = None,
) -> str:
    """把 Markdown 报告包装成完整 HTML 页面。"""
    badge_class, badge_text = _BADGE.get(decision, ("badge-report", decision))
    body = render_markdown(markdown_text)
    link_html = (
        f'<p><a href="{html.escape(dashboard_link)}">查看趋势看板 →</a></p>'
        if dashboard_link
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<p><span class="badge {badge_class}">{badge_text}</span></p>
{f'<p class="meta">{html.escape(subtitle)}</p>' if subtitle else ''}
{body}
{link_html}
<footer>本报告由 atk（Agent TestKit）自动生成。指标口径见《指标口径说明》。</footer>
</body>
</html>
"""


def save_html(content: str, path: str) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path
