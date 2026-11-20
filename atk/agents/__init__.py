"""Agent 适配器集合。

- :class:`ReplayAgent`：录制回放，跑批/回归/框架自测的主力；
- :class:`HttpAgent`：真实 Agent 的 HTTP 接入；
- :func:`create_agent`：按 ``--agent replay:baseline`` 这类字符串创建适配器。
"""

from __future__ import annotations

from .base import (
    AgentAdapter,
    AgentError,
    AgentRequest,
    AgentResult,
    TokenUsage,
    create_agent,
)
from .http_agent import HttpAgent
from .replay import ReplayAgent, list_variants, load_variant_meta

__all__ = [
    "AgentAdapter",
    "AgentError",
    "AgentRequest",
    "AgentResult",
    "TokenUsage",
    "create_agent",
    "HttpAgent",
    "ReplayAgent",
    "list_variants",
    "load_variant_meta",
]
