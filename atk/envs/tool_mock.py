"""工具 Mock 与「调用账本」。

三件事必须同时成立，评估才可信（规范 5.1「状态检查（副作用）」）：

1. **可复现**：所有外部依赖（订单 API、IM、搜索、数据库）都走 fixture，
   禁止真实网络；
2. **状态为真相**：工具是否真被调用、调了几次、参数是什么，由本模块记账，
   **不采信 Agent 的自述**（trajectory 里写「我调用了 im_send」不算数）；
3. **可注入故障**：``fail_times`` / ``error`` / ``deny`` 用于测重试、降级、越权。

fixture 文件格式（JSON/YAML）支持两种写法::

    # 写法一：直接就是返回数据
    {"orders": [{"id": "A-1", "amount": 100}]}

    # 写法二：带元信息的信封（推荐，可同时声明副作用与耗时）
    response: {"orders": []}
    side_effects: {db_orders_read: 1}
    latency_ms: 120
    error_code: null

副作用（``side_effects``）会被累加，供 ``state_change`` 断言与「越权发送」这类
黑名单判定使用。
"""

from __future__ import annotations

import json
import os
import random
import threading
from dataclasses import dataclass, field
from typing import Any

from ..model import EnvSpec, ToolInvocation
from ..yamlite import load_file
from .clock import FrozenClock
from .sandbox import Sandbox

#: 框架约定的错误码 → 可读中文（工具返回给 Agent 的错误必须可读，规范 L2 要求）
ERROR_MESSAGES: dict[str, str] = {
    "PERMISSION_DENIED": "当前账号无权调用该工具",
    "TIMEOUT": "工具调用超时，请稍后重试",
    "503": "服务暂时不可用（503）",
    "500": "服务内部错误（500）",
    "SCHEMA_INVALID": "参数校验失败：字段类型或必填项不符合工具 Schema",
    "EMPTY_RESULT": "查询成功但结果为空",
    "UNKNOWN_TOOL": "工具不存在",
    "RATE_LIMITED": "调用频率超限，请退避后重试",
}

#: fixture 信封里可能出现的键；出现任意一个就按信封解析
_ENVELOPE_KEYS = {"response", "side_effects", "latency_ms", "error_code", "mode"}


@dataclass
class ToolResponse:
    """一次工具调用的返回值（Agent 看到的内容）。"""

    tool: str
    ok: bool
    data: Any = None
    error_code: str | None = None
    error_message: str = ""
    latency_ms: int = 0
    denied: bool = False
    side_effects: dict[str, Any] = field(default_factory=dict)
    attempt: int = 1

    def to_payload(self) -> dict[str, Any]:
        """转成给 Agent 的结构化结果。

        故意保留 ``ok`` 与 ``error`` 字段：真实产品里工具失败必须以结构化形式
        回传，否则 Agent 无法正确降级（这是 L2 的断言点之一）。
        """
        payload: dict[str, Any] = {"tool": self.tool, "ok": self.ok}
        if self.ok:
            payload["data"] = self.data
        else:
            payload["error"] = {
                "code": self.error_code,
                "message": self.error_message,
            }
        if self.side_effects:
            payload["side_effects"] = self.side_effects
        return payload

    def to_text(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False)


class ToolMockRegistry:
    """工具 Mock 注册表 + 调用账本。

    线程安全：Runner 可能并发跑多个用例，每次采样有独立的 Registry 实例，
    但同一次采样内 Agent 也可能并发调用工具，因此对账本加锁。
    """

    def __init__(
        self,
        env: EnvSpec,
        sandbox: Sandbox,
        clock: FrozenClock,
        base_dir: str = "",
        rng: random.Random | None = None,
    ) -> None:
        self.env = env
        self.sandbox = sandbox
        self.clock = clock
        self.base_dir = base_dir
        self.rng = rng or random.Random(env.seed)
        self.specs = dict(env.tool_mocks)
        self.invocations: list[ToolInvocation] = []
        self.side_effect_totals: dict[str, float] = {}
        self._attempts: dict[str, int] = {}
        self._fixture_cache: dict[str, Any] = {}
        self._lock = threading.Lock()

    # -- 元信息 -----------------------------------------------------------

    def tool_names(self) -> list[str]:
        return sorted(self.specs)

    def has(self, tool: str) -> bool:
        return tool in self.specs

    def fingerprint(self) -> dict[str, Any]:
        """环境指纹：工具名 + 模式 + 种子 + 冻结时间。

        出问题时对比两份指纹，能立刻区分「环境变了」还是「模型变了」。
        """
        return {
            "tools": {name: spec.mode for name, spec in sorted(self.specs.items())},
            "seed": self.env.seed,
            "frozen_time": self.env.frozen_time,
            "network": self.env.network,
            "denied_tools": sorted(self.env.denied_tools),
        }

    # -- 调用 -------------------------------------------------------------

    def call(
        self,
        tool: str,
        args: dict[str, Any] | None = None,
        step: int | None = None,
    ) -> ToolResponse:
        """执行一次 Mock 调用，并把调用写进账本。"""
        args = dict(args or {})
        with self._lock:
            attempt = self._attempts.get(tool, 0) + 1
            self._attempts[tool] = attempt
            spec = self.specs.get(tool)
            response = self._invoke(tool, args, spec, attempt)
            invocation = ToolInvocation(
                tool=tool,
                args=args,
                ok=response.ok,
                latency_ms=response.latency_ms,
                result=response.data,
                error_code=response.error_code,
                error_message=response.error_message,
                invoked_at=self.clock.iso(),
                attempt=attempt,
                denied=response.denied,
                side_effects=dict(response.side_effects),
            )
            self.invocations.append(invocation)
            for key, value in response.side_effects.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.side_effect_totals[key] = self.side_effect_totals.get(key, 0) + value
        return response

    def _invoke(
        self,
        tool: str,
        args: dict[str, Any],
        spec: Any,
        attempt: int,
    ) -> ToolResponse:
        """根据 Mock 配置决定这一次调用返回什么。"""
        # 1) 工具不存在：这本身就是要断言的错误（工具幻觉）
        if spec is None:
            return ToolResponse(
                tool=tool,
                ok=False,
                error_code="UNKNOWN_TOOL",
                error_message=ERROR_MESSAGES["UNKNOWN_TOOL"],
            )

        # 2) 权限拒绝（越权测试）
        if tool in self.env.denied_tools or spec.mode == "deny":
            return ToolResponse(
                tool=tool,
                ok=False,
                error_code="PERMISSION_DENIED",
                error_message=ERROR_MESSAGES["PERMISSION_DENIED"],
                denied=True,
            )

        # 3) 故障注入：前 N 次失败，用于测重试/幂等
        if spec.fail_times and attempt <= spec.fail_times:
            code = spec.error_code or "503"
            return ToolResponse(
                tool=tool,
                ok=False,
                error_code=code,
                error_message=ERROR_MESSAGES.get(code, spec.error_code or "工具调用失败"),
                latency_ms=spec.latency_ms,
                attempt=attempt,
            )

        # 4) 永久故障：模拟下游整体不可用（降级兜底测试）
        if spec.mode == "error":
            code = spec.error_code or "503"
            return ToolResponse(
                tool=tool,
                ok=False,
                error_code=code,
                error_message=ERROR_MESSAGES.get(code, "工具调用失败"),
                latency_ms=spec.latency_ms,
                attempt=attempt,
            )

        # 5) 正常返回：fixture / 内联 response / record_only 的假回执
        data, extra = self._load_payload(spec, attempt)
        if isinstance(data, dict) and "error_code" in data and data.get("error_code"):
            # fixture 里也可以声明「这次返回错误」，例如混合成功/失败的序列
            code = str(data["error_code"])
            return ToolResponse(
                tool=tool,
                ok=False,
                error_code=code,
                error_message=ERROR_MESSAGES.get(code, code),
                latency_ms=spec.latency_ms + int(extra.get("latency_ms", 0)),
                attempt=attempt,
            )

        if spec.mode == "record_only":
            data = {
                "recorded": True,
                "receipt_id": f"rcpt-{self.rng.randint(10000, 99999)}",
                "echo": args,
            }

        side_effects = dict(spec.side_effects)
        side_effects.update(extra.get("side_effects", {}) or {})

        # 记录耗时并推进冻结时钟，让轨迹内时间自洽
        latency = spec.latency_ms + int(extra.get("latency_ms", 0))
        self.clock.advance(latency)
        return ToolResponse(
            tool=tool,
            ok=True,
            data=data,
            latency_ms=latency,
            side_effects=side_effects,
            attempt=attempt,
        )

    def _load_payload(self, spec: Any, attempt: int) -> tuple[Any, dict[str, Any]]:
        """加载 fixture，返回 (数据, 信封额外信息)。

        支持 fixture 是「数组」的情况：按调用次序取元素，用于模拟
        「第一次返回 A、第二次返回 B」的多步场景；越界则取最后一个。
        """
        raw: Any = None
        if spec.fixture:
            raw = self._read_fixture(spec.fixture)
        elif spec.response is not None:
            raw = spec.response

        if isinstance(raw, list):
            if not raw:
                return None, {}
            index = min(attempt - 1, len(raw) - 1)
            raw = raw[index]

        if isinstance(raw, dict) and _ENVELOPE_KEYS & set(raw):
            return raw.get("response"), dict(raw)
        return raw, {}

    def _read_fixture(self, path: str) -> Any:
        """读取 fixture（带缓存）。相对路径先用工程根，再用当前工作目录。"""
        if path in self._fixture_cache:
            return self._fixture_cache[path]

        candidates = [path]
        if not os.path.isabs(path):
            if self.base_dir:
                candidates.insert(0, os.path.join(self.base_dir, path))
            candidates.append(os.path.abspath(path))
        resolved = next((p for p in candidates if os.path.exists(p)), None)
        if resolved is None:
            # fixture 缺失属于「环境/数据」类失败，必须显式暴露而不是静默返回空
            raise FileNotFoundError(f"fixture 不存在: {path}")
        if resolved.lower().endswith(".json"):
            with open(resolved, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        elif resolved.lower().endswith((".yaml", ".yml")):
            data = load_file(resolved)
        else:
            # .md / .html / .txt 等纯文本 fixture 原样读入（网页正文、OCR 文本）
            with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read()
        self._fixture_cache[path] = data
        return data

    # -- 账本查询 ---------------------------------------------------------

    def calls_of(self, tool: str) -> list[ToolInvocation]:
        return [inv for inv in self.invocations if inv.tool == tool]

    def sequence(self) -> list[str]:
        """真实的工具调用序列（去重前），用于路径匹配。"""
        return [inv.tool for inv in self.invocations]

    def success_sequence(self) -> list[str]:
        return [inv.tool for inv in self.invocations if inv.ok]

    def failures(self) -> list[ToolInvocation]:
        return [inv for inv in self.invocations if not inv.ok]

    def denied_calls(self) -> list[ToolInvocation]:
        return [inv for inv in self.invocations if inv.denied]

    def retry_count(self) -> int:
        """重试次数 = 同一工具的第 2 次及以后的尝试总数。"""
        return sum(1 for inv in self.invocations if inv.attempt > 1)

    def side_effect(self, name: str) -> float:
        return float(self.side_effect_totals.get(name, 0))

    def max_attempts(self, tool: str) -> int:
        return max((inv.attempt for inv in self.calls_of(tool)), default=0)

    def ledger(self) -> list[dict[str, Any]]:
        return [inv.to_dict() for inv in self.invocations]

    def reset(self) -> None:
        with self._lock:
            self.invocations.clear()
            self.side_effect_totals.clear()
            self._attempts.clear()

    def import_invocation(self, invocation: ToolInvocation) -> None:
        """并入外部上报的调用记录（HTTP 适配器用于接收真实系统的 Trace）。

        只有真实观测层上报的账本才允许走这里 —— 模型自述永远不进账本。
        """
        with self._lock:
            self.invocations.append(invocation)
            for key, value in invocation.side_effects.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.side_effect_totals[key] = self.side_effect_totals.get(key, 0) + value
            # 同步尝试次数，保证后续同工具调用的 attempt 递增正确
            self._attempts[invocation.tool] = max(
                self._attempts.get(invocation.tool, 0), invocation.attempt
            )
