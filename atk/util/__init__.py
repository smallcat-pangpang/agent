"""通用工具：xlsx 读写、文本相似度、哈希等。"""

from __future__ import annotations

from .xlsx import SheetData, XlsxError, peek_deliverable, read_xlsx, write_xlsx

__all__ = [
    "SheetData",
    "XlsxError",
    "peek_deliverable",
    "read_xlsx",
    "write_xlsx",
]
