"""真实 Agent 的 HTTP 适配器（把线上/联调环境的 Agent 接进评估流水线）。

协议约定（POST，JSON）
----------------------

请求::

    {
      "run_id": "...",
      "case_id": "AGT-ORD-0031",
      "turns": [{"role": "user", "content": "...", "attachments": [...]}],
      "tools": ["orders_api", "im_send"],
      "env": {"frozen_time": "2026-11-18T10:00:00+08:00", "seed": 42, "network": false},
      "version": {"model": "...", "prompt": "...", "tool_schema": "..."}
    }

响应::

    {
      "steps": [
        {"step": 1, "type": "plan", "content": "..."},
        {"step": 2, "type": "tool_call", "tool": "orders_api", "args": {...}},
        {"step": 3, "type": "final", "content": "...", "artifacts": ["orders.xlsx"]}
      ],
      "final_output": "...",
      "usage": {"prompt_tokens": 900, "completion_tokens": 260},
      "ttft_ms": 420,
      "tool_invocations": [
        {"tool": "orders_api", "args": {...}, "ok": true, "latency_ms": 120,
         "result": {...}, "attempt": 1, "side_effects": {"db_orders_read": 1}}
      ],
      "version": {"model": "xxx-2026.11", "prompt": "v2.3", "tool_schema": "v1.7"}
    }

**关于 ``tool_invocations``**：这是被测系统上报的「真实调用账本」，
必须由被测系统的观测层（Trace 采集）提供，不能用模型自述替代。
框架会把它并入本地账本，之后所有工具断言都以它为准 —— 这是「状态为真相」
原则在真实环境下的落地方式。若对方不提供，工具类断言会自动降级为「不可验证」
并在报告里标注，而不是假装通过。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..envs.tool_mock import ToolMockRegistry
from ..model import Step, ToolInvocation, Trajectory
from .base import AgentError, AgentRequest, AgentResult, TokenUsage


class HttpAgent:
    """通过 HTTP 调用真实 Agent。"""

    def __init__(
        self,
        url: str,
        timeout_s: float = 120.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.timeout_s = timeout_s
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.name = "http"
        self.version = url

    def run(self, request: AgentRequest) -> AgentResult:
        payload = {
            "case_id": request.case.case_id,
            "turns": [
                {
                    "role": turn.role,
                    "content": turn.content,
                    "attachments": turn.attachments,
                }
                for turn in request.turns
            ],
            "tools": request.tools,
            "env": {
                "frozen_time": request.case.env.frozen_time,
                "seed": request.case.env.seed,
                "network": request.case.env.network,
            },
            "version": {
                "model": request.case.meta.notes or "",
                "prompt": "",
                "tool_schema": "",
            },
        }
        response = self._post(payload)
        return self._to_result(response, request.registry)

    # -- HTTP -------------------------------------------------------------

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers=self.headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise AgentError(f"调用被测 Agent 失败: {exc}") from exc
        except TimeoutError as exc:
            raise AgentError(f"调用被测 Agent 超时（{self.timeout_s}s）") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AgentError(f"被测 Agent 返回非法 JSON: {exc}; 前 200 字符: {raw[:200]}") from exc
        if not isinstance(data, dict):
            raise AgentError("被测 Agent 返回值必须是 JSON 对象")
        return data

    # -- 结果转换 ---------------------------------------------------------

    def _to_result(
        self, data: dict[str, Any], registry: ToolMockRegistry
    ) -> AgentResult:
        steps = [Step.from_dict(s) for s in data.get("steps", [])]

        # 并入被测系统上报的真实调用账本（真相来源）
        for raw in data.get("tool_invocations", []) or []:
            registry.import_invocation(
                ToolInvocation(
                    tool=str(raw.get("tool", "")),
                    args=dict(raw.get("args") or {}),
                    ok=bool(raw.get("ok", True)),
                    latency_ms=int(raw.get("latency_ms", 0)),
                    result=raw.get("result"),
                    error_code=raw.get("error_code"),
                    error_message=raw.get("error_message"),
                    invoked_at=str(raw.get("invoked_at", "")),
                    attempt=int(raw.get("attempt", 1)),
                    denied=bool(raw.get("denied", False)),
                    side_effects=dict(raw.get("side_effects") or {}),
                )
            )

        artifacts = [str(a) for a in data.get("artifacts", []) or []]
        if not artifacts:
            artifacts = [a for s in steps for a in s.artifacts]

        final_output = str(data.get("final_output", ""))
        if not final_output:
            final_output = next(
                (s.content for s in reversed(steps) if s.type == "final"), ""
            )

        usage_raw = data.get("usage") or {}
        usage = TokenUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
            completion_tokens=int(usage_raw.get("completion_tokens", 0)),
            tool_tokens=int(usage_raw.get("tool_tokens", 0)),
        )

        version = data.get("version") or {}
        return AgentResult(
            trajectory=Trajectory(steps=steps, final_output=final_output, artifacts=artifacts),
            usage=usage,
            ttft_ms=int(data.get("ttft_ms", 0)),
            meta={
                "model": str(version.get("model", "remote-model")),
                "prompt": str(version.get("prompt", "remote-prompt")),
                "tool_schema": str(version.get("tool_schema", "remote-tools")),
            },
        )
