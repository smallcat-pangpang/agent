"""零依赖的 xlsx 读写（含「真的打开验证」）。

为什么自己实现：交付物断言要求「产物落盘后真去打开」（规范第 9 章第 11 条），
而 pandas/openpyxl 不是必装依赖。这里的读取器直接解 OOXML 包（zip + XML），
校验的是**文件本身的结构与内容**，而不是「文件存在」这种弱断言。

支持：
- 写入：inlineStr 单元格（Excel / LibreOffice / pandas 均可读）；
- 读取：``t="s"`` 共享字符串、``t="inlineStr"``、``t="str"``、数值、布尔；
- 读取时返回 ``SheetData(rows, sheet_names)``，便于报表展示。

限制：不处理公式求值、合并单元格、样式。评估场景只需要「值」。
"""

from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Sequence

#: OOXML 主命名空间
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

#: 单元格引用中的列字母，如 "AB12" -> "AB"
_COL_RE = re.compile(r"^([A-Z]+)")


class XlsxError(ValueError):
    """xlsx 结构非法（用于识别「模型生成的畸形文件」）。"""


def _col_to_index(letters: str) -> int:
    """把 Excel 列字母转成 0 基下标：A->0, Z->25, AA->26。"""
    value = 0
    for ch in letters:
        value = value * 26 + (ord(ch) - ord("A") + 1)
    return value - 1


def _xml_escape(text: str) -> str:
    """XML 文本转义，并剔除 XML 1.0 不允许的控制字符。"""
    cleaned = "".join(ch for ch in text if ch in "\t\n\r" or ord(ch) >= 0x20)
    return (
        cleaned.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write_xlsx(
    path: str,
    rows: Sequence[Sequence[Any]],
    sheet_name: str = "Sheet1",
    columns: Sequence[str] | None = None,
) -> str:
    """写一个最小但合法的 xlsx 文件。

    Args:
        path: 目标路径。
        rows: 二维数据；数字写成数值单元格，其余写成 inlineStr。
        sheet_name: 工作表名。
        columns: 表头；若提供且 rows 首行与之不同，会自动插到最前面。

    Returns:
        写入的路径。
    """
    data: list[list[Any]] = [list(r) for r in rows]
    if columns is not None:
        header = [str(c) for c in columns]
        if not data or [str(c) for c in data[0]] != header:
            data.insert(0, list(columns))

    # 1) 工作表 XML
    row_xml: list[str] = []
    for r_idx, row in enumerate(data, start=1):
        cells: list[str] = []
        for c_idx, value in enumerate(row):
            ref = f"{_index_to_col(c_idx)}{r_idx}"
            if isinstance(value, bool):
                cells.append(f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>')
            elif isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            elif value is None:
                continue
            else:
                text = _xml_escape(str(value))
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>')
        row_xml.append(f'<row r="{r_idx}">{"".join(cells)}</row>')

    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_NS[1:-1]}">'
        f'<dimension ref="A1:{_index_to_col(max((len(r) for r in data), default=1) - 1)}'
        f'{max(len(data), 1)}"/>'
        f"<sheetData>{''.join(row_xml)}</sheetData></worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_NS[1:-1]}" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{_xml_escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return path


def _index_to_col(index: int) -> str:
    """0 基下标转列字母：0->A, 25->Z, 26->AA。"""
    letters = ""
    n = index + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


@dataclass
class SheetData:
    """读出的表格内容。"""

    rows: list[list[str]] = field(default_factory=list)
    sheet_names: list[str] = field(default_factory=list)

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    def header(self) -> list[str]:
        return self.rows[0] if self.rows else []

    def column_values(self, name: str) -> list[str]:
        """按表头名取一整列（用于「订单号/金额/状态 三列」这类断言）。"""
        header = self.header()
        if name not in header:
            return []
        idx = header.index(name)
        return [row[idx] if idx < len(row) else "" for row in self.rows[1:]]

    def to_table(self, limit: int = 20) -> str:
        """转成可放进报告的 Markdown 表格。"""
        rows = self.rows[:limit]
        if not rows:
            return "(空表)"
        header = rows[0]
        lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        for row in rows[1:]:
            cells = [str(c) for c in row] + [""] * (len(header) - len(row))
            lines.append("| " + " | ".join(cells[: len(header)]) + " |")
        return "\n".join(lines)


def read_xlsx(path: str) -> SheetData:
    """读取 xlsx（第一个工作表），结构非法时抛 :class:`XlsxError`。"""
    try:
        zf = zipfile.ZipFile(path, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise XlsxError(f"不是合法的 xlsx（zip 结构损坏）: {exc}") from exc

    with zf:
        names = set(zf.namelist())
        if "xl/workbook.xml" not in names:
            raise XlsxError("缺少 xl/workbook.xml，不是 xlsx 文件")
        # 1) 工作表名
        sheet_names: list[str] = []
        try:
            wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
            for sheet in wb_root.iter(f"{_NS}sheet"):
                sheet_names.append(sheet.get("name", ""))
        except ET.ParseError as exc:
            raise XlsxError(f"workbook.xml 解析失败: {exc}") from exc

        # 2) 共享字符串表
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            try:
                ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in ss_root.iter(f"{_NS}si"):
                    shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
            except ET.ParseError as exc:
                raise XlsxError(f"sharedStrings.xml 解析失败: {exc}") from exc

        # 3) 第一个工作表
        sheet_path = "xl/worksheets/sheet1.xml"
        if sheet_path not in names:
            candidates = sorted(n for n in names if n.startswith("xl/worksheets/sheet"))
            if not candidates:
                raise XlsxError("找不到任何工作表")
            sheet_path = candidates[0]
        try:
            root = ET.fromstring(zf.read(sheet_path))
        except ET.ParseError as exc:
            raise XlsxError(f"{sheet_path} 解析失败: {exc}") from exc

        rows: list[list[str]] = []
        for row_el in root.iter(f"{_NS}row"):
            cells: dict[int, str] = {}
            for cell in row_el.iter(f"{_NS}c"):
                ref = cell.get("r", "")
                match = _COL_RE.match(ref)
                col = _col_to_index(match.group(1)) if match else len(cells)
                ctype = cell.get("t")
                if ctype == "inlineStr":
                    text = "".join(t.text or "" for t in cell.iter(f"{_NS}t"))
                elif ctype == "s":
                    raw = cell.find(f"{_NS}v")
                    index = int(raw.text) if raw is not None and raw.text else -1
                    text = shared[index] if 0 <= index < len(shared) else ""
                elif ctype == "str":
                    raw = cell.find(f"{_NS}v")
                    text = raw.text if raw is not None and raw.text else ""
                elif ctype == "b":
                    raw = cell.find(f"{_NS}v")
                    text = "TRUE" if raw is not None and raw.text == "1" else "FALSE"
                else:
                    raw = cell.find(f"{_NS}v")
                    text = raw.text if raw is not None and raw.text else ""
                cells[col] = text
            if cells:
                width = max(cells) + 1
                rows.append([cells.get(i, "") for i in range(width)])

    return SheetData(rows=rows, sheet_names=sheet_names)


def peek_deliverable(path: str) -> dict[str, Any]:
    """通用交付物体检：类型识别 + 能否真正解析。

    返回 ``{"kind": "xlsx", "ok": True, "rows": 12, "cols": 3, "detail": "..."}``。
    供 ``file_exists`` 断言与报告使用。
    """
    lower = path.lower()
    result: dict[str, Any] = {"kind": "unknown", "ok": False, "detail": ""}
    try:
        if lower.endswith((".xlsx", ".xlsm")):
            result["kind"] = "xlsx"
            data = read_xlsx(path)
            result.update(ok=True, rows=data.n_rows, cols=data.n_cols, detail="xlsx 可解析")
            result["header"] = data.header()
        elif lower.endswith(".csv"):
            result["kind"] = "csv"
            import csv

            with open(path, "r", encoding="utf-8", newline="") as fh:
                rows = list(csv.reader(fh))
            result.update(ok=bool(rows), rows=len(rows), cols=max((len(r) for r in rows), default=0))
            result["header"] = rows[0] if rows else []
            result["detail"] = "csv 可解析"
        elif lower.endswith((".json",)):
            import json

            with open(path, "r", encoding="utf-8") as fh:
                value = json.load(fh)
            result.update(ok=True, detail="json 可解析")
            result["value"] = value
        elif lower.endswith((".md", ".txt", ".log")):
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
            result.update(ok=True, detail="文本可读取", chars=len(text))
        elif lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            result["kind"] = "image"
            with open(path, "rb") as fh:
                head = fh.read(16)
            magic_ok = (
                head.startswith(b"\x89PNG")
                or head.startswith(b"\xff\xd8\xff")
                or head.startswith(b"GIF8")
                or head.startswith(b"RIFF")
            )
            result.update(ok=magic_ok, detail="图片魔数正确" if magic_ok else "图片头非法")
        elif lower.endswith(".pdf"):
            result["kind"] = "pdf"
            with open(path, "rb") as fh:
                head = fh.read(5)
            result.update(ok=head.startswith(b"%PDF"), detail="pdf 头正确")
        else:
            with open(path, "rb") as fh:
                head = fh.read(1)
            result.update(ok=True, detail="未知类型，按字节可读处理", kind="binary")
    except (OSError, ValueError) as exc:
        result["detail"] = f"无法打开: {exc}"
    return result
