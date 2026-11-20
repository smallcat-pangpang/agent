"""录制回放 Agent：把一次真实运行「冻结」成可重复执行的脚本。

为什么需要它（规范 6.1「工具 Mock / 录制回放」）：

- 评估集动辄几百条 × 3~10 次采样，实时调用真实 Agent 既慢又贵，
  更致命的是**同一用例两次跑的结果不一样**，无法判断指标波动是谁造成的；
- 录制回放把「Agent 行为」固定下来，于是流水线本身的正确性可以被单独验证
  （框架自测：给一段已知有 bug 的录制，门禁必须拦住）。

录制文件格式（``fixtures/recordings/<variant>/<case_id>.json``）::

    {
      "agent": "demo-agent",
      "model": "demo-2026.11",
      "prompt": "v2.3",
      "tool_schema": "v1.7",
      "ttft_ms": 420,
      "tokens": {"prompt": 900, "completion": 260},
      "steps": [
        {"type": "plan",  "content": "先取订单，再生成表格，最后发群"},
        {"type": "tool_call", "tool": "orders_api", "args": {"range": "本周"}},
        {"type": "message", "content": "已拿到 12 条订单"},
        {"type": "final", "content": "已导出并发送"}
      ],
      "artifacts": [
        {"path": "orders.xlsx", "type": "xlsx",
         "columns": ["订单号", "金额", "状态"],
         "rows": [["A-1001", 128.5, "已发货"]]}
      ]
    }

**按采样取录制**（可选）：若需要演示非确定性，可在变体目录下放
``<case_id>_s1.json`` / ``_s2.json`` / ``_s3.json``，回放时按 ``sample_idx``
优先选择；找不到则退回 ``<case_id>.json``，旧格式完全兼容。
``replay:flaky`` 变体就是用它来展示 ``pass^1 > pass^3`` 与 Flaky 检测。

**关键点**：录制里的 ``tool_call`` 会真的打到 :class:`ToolMockRegistry`，
所以调用账本、权限拒绝、故障注入、副作用统计全部生效；
Agent 的自述与账本可以互相对账（识别「说一套做一套」）。
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any

from ..envs.tool_mock import ToolMockRegistry
from ..model import Step, Trajectory
from .artifacts import materialize
from .base import AgentError, AgentRequest, AgentResult, TokenUsage

#: 工具结果写进轨迹时的截断长度（完整结果在账本里，不丢信息）
_RESULT_PREVIEW = 800


class ReplayAgent:
    """按录制脚本回放一次采样。"""

    def __init__(
        self,
        variant: str = "baseline",
        root: str = "fixtures/recordings",
        base_dir: str = "",
        strict: bool = True,
    ) -> None:
        self.variant = variant
        self.root = root
        self.base_dir = base_dir
        self.strict = strict
        self.name = f"replay:{variant}"
        self.version = variant
        self._cache: dict[str, dict[str, Any]] = {}

    # -- 录制读取 ---------------------------------------------------------

    def _resolve_path(self, case_id: str, sample_idx: int = 0) -> str | None:
        # 按采样优先找 <case_id>_s<n>.json：同一用例的不同采样可以有不同结果，
        # 这样 pass^k / Flaky 才有意义；找不到就退回单文件录制（旧格式完全兼容）。
        stems: list[str] = []
        if sample_idx:
            stems += [f"{case_id}_s{sample_idx}.json", f"{case_id}_s{sample_idx}.yaml"]
        stems += [f"{case_id}.json", f"{case_id}.yaml"]
        candidates: list[str] = []
        for stem in stems:
            candidates.append(os.path.join(self.root, self.variant, stem))
        for stem in stems:
            candidates.append(os.path.join(self.root, stem))
        if self.base_dir:
            candidates = [os.path.join(self.base_dir, c) for c in candidates] + candidates
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    def load_recording(self, case_id: str, sample_idx: int = 0) -> dict[str, Any]:
        """加载录制（带缓存）；缺失时按 ``strict`` 决定报错还是返回空脚本。"""
        key = f"{case_id}#s{sample_idx}"
        if key in self._cache:
            return self._cache[key]
        path = self._resolve_path(case_id, sample_idx)
        if path is None:
            if self.strict:
                raise AgentError(
                    f"录制裁剪缺失：找不到用例 {case_id} 在变体 {self.variant!r} 下的录制文件"
                    f"（查找目录 {self.root}）"
                )
            recording: dict[str, Any] = {"steps": [], "artifacts": []}
        else:
            with open(path, "r", encoding="utf-8") as fh:
                recording = json.load(fh)
        self._cache[key] = recording
        return recording

    # -- 执行 -------------------------------------------------------------

    def run(self, request: AgentRequest) -> AgentResult:
        sample_idx = int(getattr(request, "sample_idx", 0) or 0)
        recording = self.load_recording(request.case.case_id, sample_idx)
        registry: ToolMockRegistry = request.registry
        steps: list[Step] = []
        artifacts: list[str] = []
        index = 0

        for raw in recording.get("steps", []):
            index += 1
            step_type = str(raw.get("type", "message"))

            if step_type == "tool_call":
                tool = str(raw.get("tool", ""))
                args = dict(raw.get("args") or {})
                # 真实打到 Mock（账本、权限、故障注入全部生效）
                try:
                    response = registry.call(tool, args, step=index)
                except FileNotFoundError as exc:
                    # fixture 缺失属于「环境/数据」类问题，直接抛出由 Runner 记为 error
                    raise AgentError(str(exc)) from exc
                steps.append(
                    Step(
                        index=index,
                        type="tool_call",
                        tool=tool,
                        args=args,
                        content=str(raw.get("content", "")),
                    )
                )
                index += 1
                steps.append(
                    Step(
                        index=index,
                        type="tool_result",
                        tool=tool,
                        ok=response.ok,
                        latency_ms=response.latency_ms,
                        content=response.to_text()[:_RESULT_PREVIEW],
                        error=response.error_message or None,
                        error_code=response.error_code,
                    )
                )
                continue

            steps.append(
                Step(
                    index=index,
                    type=step_type,
                    content=str(raw.get("content", "")),
                    tokens=int(raw.get("tokens", 0)),
                    latency_ms=int(raw.get("latency_ms", 0)),
                    artifacts=[str(a) for a in raw.get("artifacts", [])],
                )
            )

        # 产物落盘（注意：这是「Agent 的产物」，必须写进沙箱 work/）
        for spec in recording.get("artifacts", []):
            artifacts.append(materialize(request.sandbox, spec))

        final_output = str(recording.get("final_output", ""))
        if not final_output:
            for step in reversed(steps):
                if step.type == "final":
                    final_output = step.content
                    break
        if not final_output:
            # 兼容只写了 message 的录制
            final_output = next(
                (s.content for s in reversed(steps) if s.type in ("message", "final")), ""
            )

        trajectory = Trajectory(steps=steps, final_output=final_output, artifacts=artifacts)

        tokens = recording.get("tokens") or {}
        usage = TokenUsage(
            prompt_tokens=int(tokens.get("prompt", 0)),
            completion_tokens=int(tokens.get("completion", 0)),
            tool_tokens=sum(
                len(s.content) // 4 for s in steps if s.type == "tool_result"
            ),
        )

        return AgentResult(
            trajectory=trajectory,
            usage=usage,
            ttft_ms=int(recording.get("ttft_ms", 0)),
            meta={
                "model": str(recording.get("model", "replay-model")),
                "prompt": str(recording.get("prompt", "replay-prompt")),
                "tool_schema": str(recording.get("tool_schema", "replay-tools")),
                "recording": self.variant,
            },
        )


def list_variants(root: str, base_dir: str = "") -> list[str]:
    """列出录制根目录下的所有变体（CI 里用于生成基线/候选两套跑批）。"""
    candidates = [os.path.join(base_dir, root) if base_dir else root, root]
    for path in candidates:
        if os.path.isdir(path):
            return sorted(
                name
                for name in os.listdir(path)
                if os.path.isdir(os.path.join(path, name)) and not name.startswith(".")
            )
    return []


def load_variant_meta(root: str, variant: str, base_dir: str = "") -> dict[str, Any]:
    """读取变体级元信息（``_meta.json``），用于填充版本四元组。"""
    for base in ([os.path.join(base_dir, root)] if base_dir else []) + [root]:
        path = os.path.join(base, variant, "_meta.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
    return {}


def clone_recording(recording: dict[str, Any]) -> dict[str, Any]:
    """深拷贝录制，避免调用方修改污染缓存。"""
    return copy.deepcopy(recording)
