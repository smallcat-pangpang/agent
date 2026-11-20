"""评估环境：冻结时钟、沙箱、工具 Mock。

三者共同构成规范 6.1 的「可复现」底座：

.. code-block:: text

    FrozenClock ──┐
    Sandbox ──────┼──> ToolMockRegistry ──> 调用账本（真相来源）
    EnvSpec ──────┘

每次采样都新建一套，互不影响（对应「并发隔离」测试面）。
"""

from __future__ import annotations

from .clock import FrozenClock, utcnow_iso
from .sandbox import Sandbox
from .tool_mock import ERROR_MESSAGES, ToolMockRegistry, ToolResponse

__all__ = [
    "FrozenClock",
    "Sandbox",
    "ToolMockRegistry",
    "ToolResponse",
    "ERROR_MESSAGES",
    "utcnow_iso",
]
