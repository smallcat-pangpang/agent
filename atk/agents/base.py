"""Agent 适配器接口。

被测对象是「Agent 产品」，形态千差万别（自研框架、LangGraph、Dify、托管 API…）。
框架只约定一件事：**给我一条轨迹 + 你的版本号，我给你一份判定**。

适配器需要做的事：
1. 接收多轮输入（``AgentRequest.turns``）；
2. 只能通过 :class:`ToolMockRegistry` 调用工具（这样才有账本）；
3. 交付物写进 ``sandbox.work_dir``；
4. 返回 :class:`AgentResult`（轨迹 + token 用量）。

对应实现：
- :class:`atk.agents.replay.ReplayAgent` —— 录制回放，用于跑批与回归；
- :class:`atk.agents.demo.DemoAgent` —— 演示用参考实现（可注入缺陷）；
- :class:`atk.agents.http_agent.HttpAgent` —— 真实 Agent 的 HTTP 适配器。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..envs.clock import FrozenClock
from ..envs.sandbox import Sandbox
from ..envs.tool_mock import ToolMockRegistry
from ..model import TestCase, Trajectory, Turn


@dataclass
class TokenUsage:
    """token 用量。模型成本与工具成本要分开报（规范 3.2）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_tokens: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.tool_tokens += other.tool_tokens


@dataclass
class AgentRequest:
    """一次采样的输入上下文。"""

    case: TestCase
    turns: list[Turn]
    sandbox: Sandbox
    registry: ToolMockRegistry
    clock: FrozenClock
    rng: random.Random
    #: 可用工具名（顺序稳定，便于 Agent 构造 prompt 时缓存命中）
    tools: list[str] = field(default_factory=list)
    #: 当前是第几次采样；录制回放可据此选择不同的录制（用于稳定性演示）
    sample_idx: int = 0


@dataclass
class AgentResult:
    """一次采样的输出。"""

    trajectory: Trajectory
    usage: TokenUsage = field(default_factory=TokenUsage)
    ttft_ms: int = 0
    #: 适配器自己的元信息（模型版本、prompt 版本、耗时明细），进 version_quad
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AgentAdapter(Protocol):
    """Agent 适配器协议（结构化子类型，无需继承）。"""

    name: str
    version: str

    def run(self, request: AgentRequest) -> AgentResult:
        """执行一条用例的一次采样。"""
        ...


class AgentError(RuntimeError):
    """被测 Agent 执行失败（不是框架 bug，需在报告里按「错误」而非「失败」区分）。"""


def create_agent(spec: str, **kwargs: Any) -> AgentAdapter:
    """按字符串创建适配器。

    支持的写法::

        replay:baseline                  # 录制回放，变体=baseline
        replay:baseline:fixtures/rec      # 指定录制根目录
        http:https://agent.example/run    # 真实 Agent HTTP 接口

    Args:
        spec: 适配器描述字符串。
        kwargs: 透传给具体实现的参数（如 ``base_dir``）。
    """
    from .http_agent import HttpAgent
    from .replay import ReplayAgent

    parts = spec.split(":")
    kind = parts[0].strip().lower()

    if kind == "replay":
        variant = parts[1] if len(parts) > 1 else "baseline"
        root = parts[2] if len(parts) > 2 else kwargs.pop("recording_dir", "fixtures/recordings")
        return ReplayAgent(variant=variant, root=root, **kwargs)
    if kind == "http":
        if len(parts) < 2:
            raise AgentError("http 适配器需要提供 URL，例如 http:https://agent.example/run")
        url = ":".join(parts[1:])
        # BaseAdapter 专属参数（如 strict/base_dir）不能透传给 HttpAgent
        kwargs.pop("strict", None)
        kwargs.pop("base_dir", None)
        kwargs.pop("recording_dir", None)
        return HttpAgent(url=url, **kwargs)
    raise AgentError(f"未知适配器 {spec!r}，可选：replay / http")
