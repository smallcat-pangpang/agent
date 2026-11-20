"""Schema 与校验相关工具。

- :mod:`atk.schema.validator`：零依赖 JSON Schema 子集校验器；
- ``case.schema.json``：用例的权威定义（交付物 1），任何用例都必须通过它；
- ``run_record.schema.json``：执行记录的权威定义，用于跨系统对接。
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

from .validator import SchemaValidator, validate

__all__ = ["SchemaValidator", "validate", "load_bundled_schema", "CASE_SCHEMA_PATH"]

_HERE = os.path.dirname(os.path.abspath(__file__))
CASE_SCHEMA_PATH = os.path.join(_HERE, "case.schema.json")
RUN_RECORD_SCHEMA_PATH = os.path.join(_HERE, "run_record.schema.json")


@lru_cache(maxsize=8)
def load_bundled_schema(name: str = "case") -> dict[str, Any]:
    """加载框架自带的 Schema 文件。"""
    path = CASE_SCHEMA_PATH if name == "case" else RUN_RECORD_SCHEMA_PATH
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
