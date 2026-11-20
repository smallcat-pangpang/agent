"""框架的领域模型（全部为 dataclass，可直接序列化成 JSON）。

设计要点（对应规范第 10 章）：

1. **用例（TestCase）与执行记录（CaseRun）严格分离**：用例是「输入+期望」，
   执行记录是「一次运行的事实」。这样同一用例可跑 n 次，产出 n 条记录，
   ``pass^k`` 才有数据基础。
2. **真相来源是工具调用账本（ToolInvocation），不是 Agent 的自述**。
   Agent 的 trajectory 是「它说自己做了什么」，Invocation 才是「实际发生了什么」。
   两者不一致本身就是一类 bug（可解释性），见 assertions 中的 explainability 检查。
3. **四维分（correctness/completeness/efficiency/safety）独立存储**，
   禁止只留一个总分，否则报告无法定位退化点。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from typing import Any

#: 框架固定使用的四个评估维度（规范 5.3）
DIMENSIONS: tuple[str, ...] = ("correctness", "completeness", "efficiency", "safety")

#: 维度中文名，报告里直接使用
DIMENSION_LABELS: dict[str, str] = {
    "correctness": "正确性",
    "completeness": "完整性",
    "efficiency": "效率",
    "safety": "安全",
}

#: 分层枚举（规范第 2 章）
LAYERS: tuple[str, ...] = (
    "L1",
    "L2",
    "L2.5",
    "L3",
    "L4",
    "L4.5",
    "L5",
    "L5.5",
    "L6",
    "L7",
)

#: 失败归因六分类（规范 7.4），另有 judge_misjudge 用于人工反馈环节
FAILURE_CLASSES: tuple[str, ...] = (
    "env_data",        # 环境/数据：fixture 缺失、Mock 异常、超时集中
    "tool_interface",  # 工具/接口：Schema 校验失败、5xx、返回空
    "skill_match",     # 技能匹配：应命中未命中 / 误命中 / 多技能冲突
    "planning",        # 规划：缺子任务、顺序错、覆盖不足、提前收尾
    "execution",       # 执行/收敛：死循环、超步数、工具选错
    "model_context",   # 模型/上下文：幻觉、指令遗忘、长上下文衰减
    "orchestration",   # 编排/委派：上下文跨层丢失、SubAgent 失败被掩盖
    "judge_misjudge",  # 判定器误判：人工复核发现断言过严/过宽
    "unknown",         # 兜底：无法自动归因，进入人工队列
)

FAILURE_LABELS: dict[str, str] = {
    "env_data": "环境/数据",
    "tool_interface": "工具/接口",
    "skill_match": "技能匹配",
    "planning": "规划",
    "execution": "执行/收敛",
    "model_context": "模型/上下文",
    "orchestration": "编排/委派",
    "judge_misjudge": "判定器误判",
    "unknown": "未归因",
}


# ---------------------------------------------------------------------------
# 序列化工具
# ---------------------------------------------------------------------------


def to_jsonable(value: Any) -> Any:
    """把 dataclass / 嵌套结构转成可 ``json.dumps`` 的对象。

    这样做而不是直接用 ``dataclasses.asdict``：一是要处理 set/tuple/datetime，
    二是要在遇到未知对象时给出可读的 ``repr`` 而不是崩溃。
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _get(data: dict[str, Any], key: str, default: Any = None) -> Any:
    """安全取值：None 也视为缺省（YAML 里 ``key:`` 留空会解析成 None）。"""
    value = data.get(key, default)
    return default if value is None else value


def parse_iso(value: str) -> datetime:
    """解析 ISO 8601 时间，统一转成带时区的 datetime。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# 用例侧模型
# ---------------------------------------------------------------------------


@dataclass
class Turn:
    """一轮输入。多轮场景必须用数组表达（规范 10.1）。"""

    role: str = "user"
    content: str = ""
    attachments: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Turn":
        return cls(
            role=str(_get(data, "role", "user")),
            content=str(_get(data, "content", "")),
            attachments=[str(x) for x in _get(data, "attachments", [])],
        )


@dataclass
class ToolMockSpec:
    """单个工具的 Mock 配置。

    mode 语义：
    - ``stub``：返回 fixture / 内联 response（默认）；
    - ``record_only``：记录调用，但不产生真实副作用（如发消息只记账）；
    - ``error``：故障注入，返回错误（用于测降级与重试）；
    - ``deny``：权限拒绝（用于越权测试）。
    """

    name: str
    mode: str = "stub"
    fixture: str | None = None
    latency_ms: int = 0
    fail_times: int = 0
    error_code: str | None = None
    response: Any = None
    side_effects: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, data: Any) -> "ToolMockSpec":
        if isinstance(data, str):
            # 简写：``orders_api: fixtures/orders.json``
            if data in ("record_only", "error", "deny", "stub"):
                return cls(name=name, mode=data)
            return cls(name=name, fixture=data)
        if not isinstance(data, dict):
            raise TypeError(f"工具 {name} 的 mock 配置必须是字符串或对象")
        return cls(
            name=name,
            mode=str(_get(data, "mode", "stub")),
            fixture=_get(data, "fixture"),
            latency_ms=int(_get(data, "latency_ms", 0)),
            fail_times=int(_get(data, "fail_times", 0)),
            error_code=_get(data, "error_code"),
            response=_get(data, "response"),
            side_effects=dict(_get(data, "side_effects", {})),
        )


@dataclass
class EnvSpec:
    """用例级评估环境（规范 6.1 的强制项）。"""

    tool_mocks: dict[str, ToolMockSpec] = field(default_factory=dict)
    seed: int = 0
    frozen_time: str = "2026-01-01T00:00:00+00:00"
    network: bool = False
    fixtures: dict[str, str] = field(default_factory=dict)
    denied_tools: list[str] = field(default_factory=list)
    max_steps: int = 40
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EnvSpec":
        mocks_raw = dict(_get(data, "tool_mocks", {}))
        mocks = {
            name: ToolMockSpec.from_dict(name, spec) for name, spec in mocks_raw.items()
        }
        return cls(
            tool_mocks=mocks,
            seed=int(_get(data, "seed", 0)),
            frozen_time=str(_get(data, "frozen_time", "2026-01-01T00:00:00+00:00")),
            network=bool(_get(data, "network", False)),
            fixtures={k: str(v) for k, v in dict(_get(data, "fixtures", {})).items()},
            denied_tools=[str(x) for x in _get(data, "denied_tools", [])],
            max_steps=int(_get(data, "max_steps", 40)),
            notes=str(_get(data, "notes", "")),
        )


@dataclass
class AutoCheck:
    """一条规则断言配置。"""

    type: str
    args: dict[str, Any] = field(default_factory=dict)
    name: str = ""
    dimension: str | None = None
    weight: float = 1.0
    required: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"{self.type}"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutoCheck":
        return cls(
            type=str(_get(data, "type")),
            args=dict(_get(data, "args", {})),
            name=str(_get(data, "name", "")),
            dimension=_get(data, "dimension"),
            weight=float(_get(data, "weight", 1.0)),
            required=bool(_get(data, "required", True)),
        )


@dataclass
class ExpectItem:
    """一条 must_achieve 目标。

    ``text`` 是给人看的语义描述；``checks`` 是可选的机器判定条件
    （任一命中即达成）。若没有 checks，则交给 Judge / 关键词覆盖度判定。
    """

    text: str
    dimension: str = "correctness"
    checks: list[AutoCheck] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> "ExpectItem":
        if isinstance(data, str):
            return cls(text=data)
        return cls(
            text=str(_get(data, "text")),
            dimension=str(_get(data, "dimension", "correctness")),
            checks=[AutoCheck.from_dict(c) for c in _get(data, "checks", [])],
            keywords=[str(x) for x in _get(data, "keywords", [])],
        )


@dataclass
class ForbiddenItem:
    """一条禁止行为（黑名单，优先级最高）。"""

    text: str
    patterns: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    args: dict[str, Any] = field(default_factory=dict)
    dimension: str = "safety"
    severity: str = "block"
    semantic: bool = True

    @classmethod
    def from_dict(cls, data: Any) -> "ForbiddenItem":
        if isinstance(data, str):
            return cls(text=data)
        return cls(
            text=str(_get(data, "text")),
            patterns=[str(x) for x in _get(data, "patterns", [])],
            tools=[str(x) for x in _get(data, "tools", [])],
            args=dict(_get(data, "args", {})),
            dimension=str(_get(data, "dimension", "safety")),
            severity=str(_get(data, "severity", "block")),
            semantic=bool(_get(data, "semantic", True)),
        )


@dataclass
class Expect:
    """期望值定义：允许路径集合 + 禁止行为黑名单 + 四维权重。"""

    must_achieve: list[ExpectItem] = field(default_factory=list)
    allowed_paths: list[list[str]] = field(default_factory=list)
    forbidden: list[ForbiddenItem] = field(default_factory=list)
    rubric: dict[str, float] = field(default_factory=dict)
    min_score: float = 0.6
    expected_min_steps: int | None = None
    expected_max_steps: int | None = None
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Expect":
        rubric_raw = dict(_get(data, "rubric", {}))
        # 权重归一化：允许用例写 5/3/2 这种整数配比
        total = sum(float(v) for v in rubric_raw.values()) or 1.0
        rubric = {str(k): float(v) / total for k, v in rubric_raw.items()}
        return cls(
            must_achieve=[ExpectItem.from_dict(x) for x in _get(data, "must_achieve", [])],
            allowed_paths=[
                [str(t) for t in path] for path in _get(data, "allowed_paths", [])
            ],
            forbidden=[ForbiddenItem.from_dict(x) for x in _get(data, "forbidden", [])],
            rubric=rubric,
            min_score=float(_get(data, "min_score", 0.6)),
            expected_min_steps=_get(data, "expected_min_steps"),
            expected_max_steps=_get(data, "expected_max_steps"),
            notes=str(_get(data, "notes", "")),
        )


@dataclass
class JudgeSpec:
    """用例里的 Judge 配置。"""

    enabled: bool = True
    model: str = "judge_v2"
    calibration_set: str = "calib_default"
    dimensions: list[str] = field(default_factory=lambda: ["correctness", "completeness"])
    threshold: float = 0.6

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JudgeSpec":
        return cls(
            enabled=bool(_get(data, "enabled", True)),
            model=str(_get(data, "model", "judge_v2")),
            calibration_set=str(_get(data, "calibration_set", "calib_default")),
            dimensions=[
                str(x) for x in _get(data, "dimensions", ["correctness", "completeness"])
            ],
            threshold=float(_get(data, "threshold", 0.6)),
        )


@dataclass
class GradeSpec:
    """打分配置：规则断言为主，Judge 兜底。"""

    auto_checks: list[AutoCheck] = field(default_factory=list)
    judge: JudgeSpec | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GradeSpec":
        judge_raw = _get(data, "judge")
        return cls(
            auto_checks=[AutoCheck.from_dict(x) for x in _get(data, "auto_checks", [])],
            judge=JudgeSpec.from_dict(judge_raw) if judge_raw else None,
        )


@dataclass
class CaseMeta:
    """数据治理字段（规范 4.2：血缘、责任人、校准时间、换血时间）。"""

    source: str = "human"
    owner: str = ""
    created: str = ""
    last_calibrated: str = ""
    rotation_due: str = ""
    dataset: str = ""
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaseMeta":
        return cls(
            source=str(_get(data, "source", "human")),
            owner=str(_get(data, "owner", "")),
            created=str(_get(data, "created", "")),
            last_calibrated=str(_get(data, "last_calibrated", "")),
            rotation_due=str(_get(data, "rotation_due", "")),
            dataset=str(_get(data, "dataset", "")),
            notes=str(_get(data, "notes", "")),
        )


@dataclass
class TestCase:
    """一条完整用例。"""

    case_id: str
    title: str
    layer: str
    input_turns: list[Turn]
    env: EnvSpec
    expect: Expect
    grade: GradeSpec
    domain: str = ""
    difficulty: str = "中等"
    priority: str = "P1"
    tags: list[str] = field(default_factory=list)
    owner: str = ""
    timeout_s: float = 120.0
    meta: CaseMeta = field(default_factory=CaseMeta)
    path: str = ""

    # -- 便捷属性 ---------------------------------------------------------

    @property
    def dataset_version(self) -> str:
        """数据集版本：用例可显式声明，否则退化成目录名，保证四元组不为空。"""
        if self.meta.dataset:
            return self.meta.dataset
        return self.path.rsplit("/", 1)[0].rsplit("\\", 1)[-1] or "local"

    @property
    def is_safety(self) -> bool:
        return self.layer == "L6" or "安全" in self.tags or any(
            f.dimension == "safety" and f.severity == "block" for f in self.expect.forbidden
        )

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TestCase":
        input_raw = _get(data, "input", {})
        meta_raw = _get(data, "meta", {})
        return cls(
            case_id=str(_get(data, "case_id")),
            title=str(_get(data, "title")),
            layer=str(_get(data, "layer")),
            input_turns=[Turn.from_dict(t) for t in _get(input_raw, "turns", [])],
            env=EnvSpec.from_dict(_get(input_raw, "env", {})),
            expect=Expect.from_dict(_get(data, "expect", {})),
            grade=GradeSpec.from_dict(_get(data, "grade", {})),
            domain=str(_get(data, "domain", "")),
            difficulty=str(_get(data, "difficulty", "中等")),
            priority=str(_get(data, "priority", "P1")),
            tags=[str(x) for x in _get(data, "tags", [])],
            owner=str(_get(data, "owner", meta_raw.get("owner", ""))),
            timeout_s=float(_get(input_raw, "timeout_s", 120)),
            meta=CaseMeta.from_dict(meta_raw),
            path=str(data.get("_path", "")),
        )


# ---------------------------------------------------------------------------
# 执行侧模型
# ---------------------------------------------------------------------------


@dataclass
class Step:
    """轨迹中的一步。

    type 取值：plan / tool_call / tool_result / message / clarify / final / error
    """

    index: int
    type: str
    content: str = ""
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    ok: bool | None = None
    latency_ms: int = 0
    tokens: int = 0
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None
    error_code: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Step":
        return cls(
            index=int(_get(data, "step", data.get("index", 0))),
            type=str(_get(data, "type", "message")),
            content=str(_get(data, "content", "")),
            tool=_get(data, "tool"),
            args=dict(_get(data, "args", {})),
            ok=_get(data, "ok"),
            latency_ms=int(_get(data, "latency_ms", 0)),
            tokens=int(_get(data, "tokens", 0)),
            artifacts=[str(a) for a in _get(data, "artifacts", [])],
            error=_get(data, "error"),
            error_code=_get(data, "error_code"),
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"step": self.index, "type": self.type}
        if self.content:
            data["content"] = self.content
        if self.tool:
            data["tool"] = self.tool
        if self.args:
            data["args"] = self.args
        if self.ok is not None:
            data["ok"] = self.ok
        if self.latency_ms:
            data["latency_ms"] = self.latency_ms
        if self.tokens:
            data["tokens"] = self.tokens
        if self.artifacts:
            data["artifacts"] = self.artifacts
        if self.error:
            data["error"] = self.error
        if self.error_code:
            data["error_code"] = self.error_code
        return data


@dataclass
class ToolInvocation:
    """工具调用账本的一条记录 —— 评估的「真相来源」。

    注意：这是 Mock 注册表在**真正被调用时**写下的，不是 Agent 自述。
    对账 trajectory 里的 tool_call 与这里的记录，可以识别「说一套做一套」。
    """

    tool: str
    args: dict[str, Any]
    ok: bool
    latency_ms: int = 0
    result: Any = None
    error_code: str | None = None
    error_message: str | None = None
    invoked_at: str = ""
    attempt: int = 1
    denied: bool = False
    side_effects: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass
class Trajectory:
    """Agent 自述的完整轨迹。"""

    steps: list[Step] = field(default_factory=list)
    final_output: str = ""
    artifacts: list[str] = field(default_factory=list)

    def text(self) -> str:
        """把整条轨迹拍平成文本，供黑名单扫描/关键词判定使用。"""
        parts: list[str] = []
        for step in self.steps:
            if step.content:
                parts.append(step.content)
            if step.tool:
                parts.append(f"{step.tool} {json.dumps(step.args, ensure_ascii=False)}")
            if step.error:
                parts.append(step.error)
        if self.final_output:
            parts.append(self.final_output)
        return "\n".join(parts)

    def tool_sequence(self) -> list[str]:
        """Agent 自述的调用序列（用于路径匹配的可解释性对照）。"""
        return [s.tool for s in self.steps if s.type == "tool_call" and s.tool]

    def step_at(self, index: int | None) -> Step | None:
        if index is None:
            return None
        for step in self.steps:
            if step.index == index:
                return step
        return None

    def snippet(self, around: int | None, radius: int = 2) -> str:
        """取某个 step 前后的轨迹片段，用于失败报告（研发 30 秒看懂）。"""
        if around is None or not self.steps:
            return ""
        lines: list[str] = []
        for step in self.steps:
            if abs(step.index - around) <= radius:
                mark = ">>" if step.index == around else "  "
                desc = step.tool or step.type
                body = step.content or json.dumps(step.args, ensure_ascii=False) or ""
                if step.error:
                    body = f"{body} | 错误: {step.error}"
                lines.append(f"{mark} step{step.index} [{desc}] {body[:160]}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "final_output": self.final_output,
            "artifacts": self.artifacts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Trajectory":
        return cls(
            steps=[Step.from_dict(s) for s in _get(data, "steps", [])],
            final_output=str(_get(data, "final_output", "")),
            artifacts=[str(a) for a in _get(data, "artifacts", [])],
        )


@dataclass
class RunMetrics:
    """单次采样的效率类指标（规范 3.1 第 4 组）。"""

    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_tokens: int = 0
    model_cost_usd: float = 0.0
    tool_cost_usd: float = 0.0
    latency_ms: int = 0
    ttft_ms: int = 0
    tool_calls: int = 0
    retries: int = 0
    max_steps_hit: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens + self.tool_tokens

    @property
    def total_cost_usd(self) -> float:
        return round(self.model_cost_usd + self.tool_cost_usd, 6)

    def to_dict(self) -> dict[str, Any]:
        data = to_jsonable(self)
        data["tokens_total"] = self.total_tokens
        data["cost_usd"] = self.total_cost_usd
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunMetrics":
        return cls(
            steps=int(_get(data, "steps", 0)),
            prompt_tokens=int(_get(data, "prompt_tokens", 0)),
            completion_tokens=int(_get(data, "completion_tokens", 0)),
            tool_tokens=int(_get(data, "tool_tokens", 0)),
            model_cost_usd=float(_get(data, "model_cost_usd", 0.0)),
            tool_cost_usd=float(_get(data, "tool_cost_usd", 0.0)),
            latency_ms=int(_get(data, "latency_ms", 0)),
            ttft_ms=int(_get(data, "ttft_ms", 0)),
            tool_calls=int(_get(data, "tool_calls", 0)),
            retries=int(_get(data, "retries", 0)),
            max_steps_hit=bool(_get(data, "max_steps_hit", False)),
        )


@dataclass
class RuleResult:
    """一条规则断言的执行结果。"""

    check_type: str
    name: str
    passed: bool
    detail: str = ""
    signal: str = ""
    dimension: str = "correctness"
    required: bool = True
    evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleResult":
        return cls(
            check_type=str(_get(data, "check_type", "")),
            name=str(_get(data, "name", "")),
            passed=bool(_get(data, "passed", False)),
            detail=str(_get(data, "detail", "")),
            signal=str(_get(data, "signal", "")),
            dimension=str(_get(data, "dimension", "correctness")),
            required=bool(_get(data, "required", True)),
            evidence=dict(_get(data, "evidence", {})),
        )


@dataclass
class JudgeScore:
    """Judge 对单个维度的打分结果，必须带 evidence 引用。"""

    dimension: str
    score: float
    evidence: list[str] = field(default_factory=list)
    rationale: str = ""
    judge_model: str = ""
    latency_ms: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JudgeScore":
        return cls(
            dimension=str(_get(data, "dimension", "")),
            score=float(_get(data, "score", 0.0)),
            evidence=[str(x) for x in _get(data, "evidence", [])],
            rationale=str(_get(data, "rationale", "")),
            judge_model=str(_get(data, "judge_model", "")),
            latency_ms=int(_get(data, "latency_ms", 0)),
        )


@dataclass
class Verdict:
    """单个采样的判定结论。"""

    #: auto = 规则判定的结果（pass/fail/error），final = 综合 Judge 后的结论
    auto: str = "pass"
    final: str = "pass"
    rules_failed: list[RuleResult] = field(default_factory=list)
    rules_passed: int = 0
    judge_scores: dict[str, JudgeScore] = field(default_factory=dict)
    dimension_scores: dict[str, float] = field(default_factory=dict)
    forbidden_hits: list[str] = field(default_factory=list)
    failure_class: str | None = None
    failure_step: int | None = None
    failure_snippet: str = ""
    judge_skipped: bool = False
    #: Judge 分被明确忽略的维度（规则独占维度，见 judge.RULE_ONLY_DIMENSIONS）
    judge_ignored_dims: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.final == "pass"

    def passed_dimension(self, dim: str, threshold: float) -> bool:
        """某维度是否算「通过」：门禁按维度统计时使用。

        判定顺序：
        1. 执行错误（error）一律不算通过；
        2. 安全维度：只要没有命中 block 级安全黑名单就算通过；
        3. 该维度存在**硬失败的必失败断言** → 不算通过
           （不能因为其它维度的达成度把分数拉过阈值就当作通过）；
        4. 否则看维度得分是否达到阈值。
        """
        if self.final == "error":
            return False
        if dim == "safety":
            return not self.forbidden_hits
        if any(
            (not rule.passed) and rule.required and rule.dimension == dim
            for rule in self.rules_failed
        ):
            return False
        score = self.dimension_scores.get(dim)
        if score is None:
            # 没有该维度的信息时退化为整体结论，避免把「没测」当成「失败」
            return self.final == "pass"
        return score >= threshold

    def to_dict(self) -> dict[str, Any]:
        return {
            "auto": self.auto,
            "final": self.final,
            "rules_failed": [to_jsonable(r) for r in self.rules_failed],
            "rules_passed": self.rules_passed,
            "judge_scores": {k: to_jsonable(v) for k, v in self.judge_scores.items()},
            "dimension_scores": self.dimension_scores,
            "forbidden_hits": self.forbidden_hits,
            "failure_class": self.failure_class,
            "failure_step": self.failure_step,
            "failure_snippet": self.failure_snippet,
            "judge_skipped": self.judge_skipped,
            "judge_ignored_dims": list(self.judge_ignored_dims),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Verdict":
        return cls(
            auto=str(_get(data, "auto", "pass")),
            final=str(_get(data, "final", "pass")),
            rules_failed=[RuleResult.from_dict(x) for x in _get(data, "rules_failed", [])],
            rules_passed=int(_get(data, "rules_passed", 0)),
            judge_scores={
                k: JudgeScore.from_dict(v)
                for k, v in dict(_get(data, "judge_scores", {})).items()
            },
            dimension_scores={
                str(k): float(v) for k, v in dict(_get(data, "dimension_scores", {})).items()
            },
            forbidden_hits=[str(x) for x in _get(data, "forbidden_hits", [])],
            failure_class=_get(data, "failure_class"),
            failure_step=_get(data, "failure_step"),
            failure_snippet=str(_get(data, "failure_snippet", "")),
            judge_skipped=bool(_get(data, "judge_skipped", False)),
            judge_ignored_dims=[str(x) for x in _get(data, "judge_ignored_dims", [])],
            notes=[str(x) for x in _get(data, "notes", [])],
        )


@dataclass
class Attribution:
    """失败自动归因结果（规范 7.4）。"""

    failure_class: str = "unknown"
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    first_error_step: int | None = None
    snippet: str = ""
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Attribution":
        return cls(
            failure_class=str(_get(data, "failure_class", "unknown")),
            confidence=float(_get(data, "confidence", 0.0)),
            reasons=[str(x) for x in _get(data, "reasons", [])],
            first_error_step=_get(data, "first_error_step"),
            snippet=str(_get(data, "snippet", "")),
            signals=[str(x) for x in _get(data, "signals", [])],
        )


@dataclass
class VersionQuad:
    """版本四元组 —— 归因的前提（规范 6.2）。"""

    model: str = "unknown"
    prompt: str = "unknown"
    tool_schema: str = "unknown"
    dataset: str = "unknown"
    framework: str = "atk-1.0.0"

    def key(self) -> str:
        return f"{self.model}|{self.prompt}|{self.tool_schema}|{self.dataset}"

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VersionQuad":
        return cls(
            model=str(_get(data, "model", "unknown")),
            prompt=str(_get(data, "prompt", "unknown")),
            tool_schema=str(_get(data, "tool_schema", "unknown")),
            dataset=str(_get(data, "dataset", "unknown")),
            framework=str(_get(data, "framework", "atk")),
        )


@dataclass
class CaseSnapshot:
    """用例的轻量快照，随运行记录一起落盘。

    这样历史报告在用例被改动/删除后仍然能正确解释当时的结论。
    """

    case_id: str = ""
    title: str = ""
    layer: str = ""
    priority: str = ""
    difficulty: str = ""
    domain: str = ""
    tags: list[str] = field(default_factory=list)
    supports_pass_k: bool = True

    @classmethod
    def from_case(cls, case: TestCase) -> "CaseSnapshot":
        return cls(
            case_id=case.case_id,
            title=case.title,
            layer=case.layer,
            priority=case.priority,
            difficulty=case.difficulty,
            domain=case.domain,
            tags=list(case.tags),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaseSnapshot":
        return cls(
            case_id=str(_get(data, "case_id", "")),
            title=str(_get(data, "title", "")),
            layer=str(_get(data, "layer", "")),
            priority=str(_get(data, "priority", "")),
            difficulty=str(_get(data, "difficulty", "")),
            domain=str(_get(data, "domain", "")),
            tags=[str(x) for x in _get(data, "tags", [])],
            supports_pass_k=bool(_get(data, "supports_pass_k", True)),
        )


@dataclass
class CaseRun:
    """一次用例采样的完整执行记录（规范 10.2 的落地版本）。"""

    run_id: str
    case_id: str
    sample_idx: int
    version_quad: VersionQuad
    trajectory: Trajectory
    invocations: list[ToolInvocation]
    metrics: RunMetrics
    verdict: Verdict
    started_at: str = ""
    duration_ms: int = 0
    artifacts: list[str] = field(default_factory=list)
    sandbox_dir: str = ""
    case: CaseSnapshot = field(default_factory=CaseSnapshot)
    attribution: Attribution | None = None
    env_fingerprint: dict[str, Any] = field(default_factory=dict)
    agent_name: str = ""
    flaky: bool = False
    quarantined: bool = False
    human_label: str | None = None

    @property
    def status(self) -> str:
        return self.verdict.final

    @property
    def is_error(self) -> bool:
        return self.verdict.final == "error"

    def calls_of(self, tool: str) -> list[ToolInvocation]:
        """取某个工具的全部真实调用记录。"""
        return [inv for inv in self.invocations if inv.tool == tool]

    def to_dict(self) -> dict[str, Any]:
        """转成落盘 JSON（字段名与规范 10.2 对齐，并保留扩展字段）。"""
        return {
            "run_id": self.run_id,
            "case_id": self.case_id,
            "sample_idx": self.sample_idx,
            "version_quad": self.version_quad.to_dict(),
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "agent": self.agent_name,
            "case": to_jsonable(self.case),
            "trajectory": self.trajectory.to_dict(),
            "invocations": [inv.to_dict() for inv in self.invocations],
            "artifacts": self.artifacts,
            "metrics": self.metrics.to_dict(),
            "verdict": self.verdict.to_dict(),
            "attribution": to_jsonable(self.attribution) if self.attribution else None,
            "env_fingerprint": self.env_fingerprint,
            "sandbox_dir": self.sandbox_dir,
            "flaky": self.flaky,
            "quarantined": self.quarantined,
            "human_label": self.human_label,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaseRun":
        attr = data.get("attribution")
        return cls(
            run_id=str(_get(data, "run_id", "")),
            case_id=str(_get(data, "case_id", "")),
            sample_idx=int(_get(data, "sample_idx", 0)),
            version_quad=VersionQuad.from_dict(_get(data, "version_quad", {})),
            trajectory=Trajectory.from_dict(_get(data, "trajectory", {})),
            invocations=[
                ToolInvocation(**{k: v for k, v in inv.items() if k in _INVOCATION_FIELDS})
                for inv in _get(data, "invocations", [])
            ],
            metrics=RunMetrics.from_dict(_get(data, "metrics", {})),
            verdict=Verdict.from_dict(_get(data, "verdict", {})),
            started_at=str(_get(data, "started_at", "")),
            duration_ms=int(_get(data, "duration_ms", 0)),
            artifacts=[str(a) for a in _get(data, "artifacts", [])],
            sandbox_dir=str(_get(data, "sandbox_dir", "")),
            case=CaseSnapshot.from_dict(_get(data, "case", {})),
            attribution=Attribution.from_dict(attr) if attr else None,
            env_fingerprint=dict(_get(data, "env_fingerprint", {})),
            agent_name=str(_get(data, "agent", "")),
            flaky=bool(_get(data, "flaky", False)),
            quarantined=bool(_get(data, "quarantined", False)),
            human_label=_get(data, "human_label"),
        )


#: ToolInvocation 的合法字段集合，from_dict 时用于过滤未知字段
_INVOCATION_FIELDS = {f.name for f in fields(ToolInvocation)}
