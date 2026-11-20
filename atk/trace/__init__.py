"""Trace 采集与存储。

- :mod:`atk.trace.redact`：PII 脱敏（落盘前强制走一遍）；
- :mod:`atk.trace.store`：按运行目录归档 records / summary / trace。
"""

from __future__ import annotations

from .redact import Redactor, audit, assert_clean
from .store import (
    RunStore,
    find_latest_run,
    list_runs,
    load_index,
    load_records,
    load_summary,
)

__all__ = [
    "Redactor",
    "audit",
    "assert_clean",
    "RunStore",
    "find_latest_run",
    "list_runs",
    "load_index",
    "load_records",
    "load_summary",
]
