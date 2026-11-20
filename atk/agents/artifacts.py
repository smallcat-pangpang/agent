"""交付物构造器：把「录制/脚本里的产物描述」真正写成沙箱里的文件。

录制回放与演示 Agent 都需要生成交付物，例如::

    {"path": "orders_2026w46.xlsx", "type": "xlsx",
     "columns": ["订单号", "金额", "状态"],
     "rows": [["A-1001", 128.5, "已发货"]]}

支持的类型：``xlsx`` / ``csv`` / ``json`` / ``text|md|txt`` / ``png``。
产物**必须落在 sandbox.work_dir 内**，越界会被 :meth:`Sandbox.work_path` 拒绝。
"""

from __future__ import annotations

import base64
import csv
import json
import os
import struct
import zlib
from typing import Any

from ..envs.sandbox import Sandbox
from ..util.xlsx import write_xlsx


def materialize(sandbox: Sandbox, spec: dict[str, Any]) -> str:
    """按描述生成一个交付物，返回相对路径。"""
    rel = str(spec.get("path") or spec.get("name") or "artifact.txt")
    kind = str(spec.get("type") or _guess_type(rel)).lower()
    target = sandbox.work_path(rel)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)

    if kind in ("xlsx", "excel"):
        columns = spec.get("columns")
        rows = spec.get("rows") or []
        write_xlsx(target, rows, sheet_name=str(spec.get("sheet", "Sheet1")), columns=columns)
    elif kind == "csv":
        columns = list(spec.get("columns") or [])
        rows = list(spec.get("rows") or [])
        with open(target, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            if columns:
                writer.writerow(columns)
            writer.writerows(rows)
    elif kind == "json":
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(spec.get("data"), fh, ensure_ascii=False, indent=2)
    elif kind == "png":
        _write_solid_png(target, int(spec.get("width", 320)), int(spec.get("height", 200)))
    elif kind in ("text", "md", "txt", "markdown"):
        content = spec.get("content", "")
        if isinstance(content, list):
            content = "\n".join(str(x) for x in content)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(str(content))
    elif kind == "base64":
        with open(target, "wb") as fh:
            fh.write(base64.b64decode(str(spec.get("content", ""))))
    else:
        raise ValueError(f"不支持的产物类型: {kind}")

    return rel.replace(os.sep, "/")


def _guess_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return {
        "xlsx": "xlsx",
        "csv": "csv",
        "json": "json",
        "md": "md",
        "txt": "txt",
        "png": "png",
    }.get(ext, "text")


def _write_solid_png(path: str, width: int, height: int) -> None:
    """生成一个纯色 PNG（用于多模态产物的占位）。

    不依赖 Pillow：直接按 PNG 规范拼 IHDR/IDAT/IEND。
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b""
    for y in range(height):
        raw += b"\x00" + bytes([(x * 7 + y * 3) % 256 for x in range(width * 3)])
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)
