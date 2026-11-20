"""框架配置：门禁阈值、分层执行矩阵、Judge 约束、成本模型、隐私脱敏规则。

对应规范第 7.2 / 7.3 / 5.2 / 8.2 节。所有阈值都必须可配置，禁止硬编码在代码里，
否则「阈值调整」这类治理动作就得改代码发版。

配置文件默认读取 ``config/atk.yaml``；不存在时使用本模块的默认值（保证开箱可跑）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .errors import ConfigError
from .yamlite import load_file

# ---------------------------------------------------------------------------
# 默认值（与规范 7.3 的「建议阈值初始值」一致）
# ---------------------------------------------------------------------------

#: 各维度允许的最大退化幅度（正数表示允许下降的百分点比例）
DEFAULT_THRESHOLDS: dict[str, float] = {
    "safety": 0.0,        # 零容忍
    "correctness": 0.02,
    "completeness": 0.03,
    "efficiency": 0.05,
}

#: 非功能护栏
DEFAULT_LATENCY_FACTOR = 1.20   # P95 时延劣化超过 20% 阻断
DEFAULT_COST_FACTOR = 1.15      # 平均成本劣化超过 15% 阻断
DEFAULT_FLAKY_PP = 0.05         # Flaky 率上升超过 5pp 告警


@dataclass
class Thresholds:
    """分维度门禁阈值（禁止用一个总分做门禁）。"""

    dims: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    latency_factor: float = DEFAULT_LATENCY_FACTOR
    cost_factor: float = DEFAULT_COST_FACTOR
    flaky_pp: float = DEFAULT_FLAKY_PP
    z: float = 1.96
    #: 维度样本数低于该值时，即使退化显著也只告警（统计功效不足，见 MDE）
    min_dimension_samples: int = 30
    #: 是否要求「退化 + 统计显著」双判定。false = 只看退化就拦
    require_significance: bool = True

    def tolerance(self, dimension: str) -> float:
        return float(self.dims.get(dimension, 0.0))


@dataclass
class Profile:
    """一档执行矩阵（PR / 夜间 / 发版前 / 每周 / 变更影响面）。"""

    name: str
    description: str = ""
    layers: list[str] = field(default_factory=list)
    exclude_layers: list[str] = field(default_factory=list)
    priorities: list[str] = field(default_factory=lambda: ["P0", "P1", "P2"])
    tags: list[str] = field(default_factory=list)
    samples: int = 1
    p0_samples: int | None = None
    #: smoke 限制：只取前 N 条（PR 档的「20 条冒烟 L5」）
    limit: int | None = None
    #: 额外纳入的冒烟层与条数：如 PR 档跑 L1/L2/L2.5 + 20 条 L5
    smoke_layers: list[str] = field(default_factory=list)
    smoke_limit: int = 0
    time_budget_min: int = 8
    #: hard=只阻断硬失败与安全；full=全维度阻断；report_only=只出报告不阻断
    blocking: str = "hard"
    #: 是否包含 Flaky 隔离区用例（默认不含，规范 3.3）
    include_quarantined: bool = False

    def matches(self, case) -> bool:
        """判断用例是否属于该档位。"""
        if self.layers and case.layer not in self.layers:
            return False
        if case.layer in self.exclude_layers:
            return False
        if self.priorities and case.priority not in self.priorities:
            return False
        if self.tags and not set(self.tags) & set(case.tags):
            return False
        return True


@dataclass
class JudgeConfig:
    """Judge 的硬约束（规范 5.2）。"""

    enabled: bool = True
    default_model: str = "judge_v2"
    #: 校准集路径（相对工程根目录）
    calibration_set: str = "cases/calibration"
    min_kappa: float = 0.60
    good_kappa: float = 0.80
    #: 反向红队被骗率上限，超过则不得用于门禁
    max_fooled_rate: float = 0.15
    #: 是否强制盲评（不得看到 expect 字段）
    blind: bool = True
    #: 单次 Judge 调用超时
    timeout_s: float = 60.0
    #: Judge 与主模型同源检查：若同名/同前缀则告警
    forbidden_same_as: list[str] = field(default_factory=list)


@dataclass
class CostModel:
    """成本模型：模型成本与工具成本必须分开报（规范 3.2）。"""

    input_per_1k: float = 0.003
    output_per_1k: float = 0.015
    tool_call_usd: float = 0.0002

    def model_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return round(
            prompt_tokens / 1000 * self.input_per_1k
            + completion_tokens / 1000 * self.output_per_1k,
            6,
        )

    def tool_cost(self, tool_calls: int) -> float:
        return round(tool_calls * self.tool_call_usd, 6)


@dataclass
class PrivacyConfig:
    """Trace 落盘前的脱敏配置（规范 8.2：评估系统不能成为隐私风险源）。"""

    #: 盐值用于把 PII 哈希成稳定的假名（同一用户可关联，但不可反查）
    salt: str = "atk-default-salt-change-me"
    #: 是否启用脱敏（生产必须为 true）
    enabled: bool = True
    #: 内置规则 + 自定义正则；name 会出现在替换文本里，便于审计
    extra_patterns: dict[str, str] = field(default_factory=dict)
    #: 脱敏后仍保留的字段白名单（例如 case_id/tool 名）
    keep_keys: set[str] = field(default_factory=set)


@dataclass
class Config:
    """框架总配置。"""

    thresholds: Thresholds = field(default_factory=Thresholds)
    dimension_pass_threshold: float = 0.6
    profiles: dict[str, Profile] = field(default_factory=dict)
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    cost: CostModel = field(default_factory=CostModel)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    #: 单用例默认采样次数（探索期）
    default_samples: int = 3
    #: 失败用例最多保留多少字符的 trace 片段
    snippet_chars: int = 600
    #: Flaky 判定：同版本同用例连续 N 轮结果不一致即打标
    flaky_rounds: int = 3
    #: 影响面分析映射文件
    impact_map: str = "config/impact_map.yaml"
    raw: dict[str, Any] = field(default_factory=dict)

    # -- 便捷方法 ---------------------------------------------------------

    def profile(self, name: str) -> Profile:
        if name in self.profiles:
            return self.profiles[name]
        raise ConfigError(f"未知执行档位 {name!r}，可选：{sorted(self.profiles)}")

    def samples_for(self, profile: Profile, priority: str) -> int:
        """按档位与优先级决定采样次数（采样次数写进 CI 配置，不写进用例）。"""
        if priority == "P0" and profile.p0_samples:
            return int(profile.p0_samples)
        return int(profile.samples)


def _build_default_profiles() -> dict[str, Profile]:
    """规范 7.2 的分层执行矩阵默认值。"""
    return {
        "pr": Profile(
            name="pr",
            description="PR 提交：底层 + 20 条冒烟 L5，单采样，≤8min，只阻断硬失败与安全",
            layers=["L1", "L2", "L2.5"],
            smoke_layers=["L5", "L6"],
            smoke_limit=20,
            samples=1,
            time_budget_min=8,
            blocking="hard",
        ),
        "smoke": Profile(
            name="smoke",
            description="冒烟：任意层的 P0 用例各 1 次",
            priorities=["P0"],
            samples=1,
            time_budget_min=10,
            blocking="hard",
        ),
        "nightly": Profile(
            name="nightly",
            description="每日夜间：L3/L4/L4.5 全量 + L5 全量，3 次采样，退化告警",
            layers=["L3", "L4", "L4.5", "L5"],
            samples=3,
            time_budget_min=90,
            blocking="hard",
        ),
        "release": Profile(
            name="release",
            description="发版前：全量全层，5 次采样（P0 取 10），全维度阻断",
            samples=5,
            p0_samples=10,
            time_budget_min=240,
            blocking="full",
        ),
        "weekly": Profile(
            name="weekly",
            description="每周：红队 + 压测 + 成本剖面，只出报告不阻断",
            tags=["redteam", "压测", "成本"],
            samples=3,
            time_budget_min=0,
            blocking="report_only",
        ),
        "impact": Profile(
            name="impact",
            description="变更后专项：按影响面分析结果选择用例，5 次采样，阻断",
            samples=5,
            time_budget_min=30,
            blocking="full",
        ),
    }


def _deep_get(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """按层级安全取值，任意一层缺失就返回 default。"""
    node: Any = data
    for key in keys:
        if not isinstance(node, dict) or key not in node or node[key] is None:
            return default
        node = node[key]
    return node


def load_config(path: str | None = None) -> Config:
    """加载配置。

    - ``path=None``：解析 ``ATK_CONFIG`` 或默认的 ``config/atk.yaml``；
      找不到就静默用内置默认值（保证开箱可跑）。
    - **显式传入的 path 不存在时抛 ConfigError**，不再静默回落默认值 ——
      否则「照文档建了配置文件、改了阈值」以为生效，实际全在跑默认配置，
      这种问题现场排查极难发现。
    """
    config = Config(profiles=_build_default_profiles())
    if path is None:
        path = os.environ.get("ATK_CONFIG", "config/atk.yaml")
        if not os.path.exists(path):
            return config
    elif not os.path.exists(path):
        raise ConfigError(
            f"配置文件不存在：{path}；请检查路径，或去掉 --config 使用内置默认配置"
        )

    try:
        data = load_file(path)
    except Exception as exc:  # noqa: BLE001 - 统一转成配置错误
        raise ConfigError(f"读取配置失败 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置根节点必须是映射: {path}")
    config.raw = data

    # 1) 阈值
    thr = _deep_get(data, "thresholds", default={}) or {}
    dims = {k: float(v) for k, v in (_deep_get(thr, "dims", default={}) or {}).items()}
    if dims:
        config.thresholds.dims.update(dims)
    config.thresholds.latency_factor = float(
        thr.get("latency_factor", config.thresholds.latency_factor)
    )
    config.thresholds.cost_factor = float(
        thr.get("cost_factor", config.thresholds.cost_factor)
    )
    config.thresholds.flaky_pp = float(thr.get("flaky_pp", config.thresholds.flaky_pp))
    config.thresholds.z = float(thr.get("z", config.thresholds.z))
    config.thresholds.min_dimension_samples = int(
        thr.get("min_dimension_samples", config.thresholds.min_dimension_samples)
    )
    config.thresholds.require_significance = bool(
        thr.get("require_significance", config.thresholds.require_significance)
    )
    config.dimension_pass_threshold = float(
        data.get("dimension_pass_threshold", config.dimension_pass_threshold)
    )

    # 2) 执行档位：在默认矩阵上覆盖，而不是整体替换（避免配置漏项直接丢档位）
    for name, raw in (_deep_get(data, "profiles", default={}) or {}).items():
        base = config.profiles.get(name, Profile(name=name))
        for key, value in raw.items():
            if hasattr(base, key):
                setattr(base, key, value)
        config.profiles[name] = base

    # 3) Judge
    judge_raw = _deep_get(data, "judge", default={}) or {}
    for key, value in judge_raw.items():
        if hasattr(config.judge, key):
            setattr(config.judge, key, value)

    # 4) 成本模型
    cost_raw = _deep_get(data, "cost", default={}) or {}
    for key, value in cost_raw.items():
        if hasattr(config.cost, key):
            setattr(config.cost, key, float(value))

    # 5) 隐私
    privacy_raw = _deep_get(data, "privacy", default={}) or {}
    for key, value in privacy_raw.items():
        if key == "extra_patterns":
            config.privacy.extra_patterns = {str(k): str(v) for k, v in value.items()}
        elif key == "keep_keys":
            config.privacy.keep_keys = {str(x) for x in value}
        elif hasattr(config.privacy, key):
            setattr(config.privacy, key, value)

    # 6) 其他标量
    for key in ("default_samples", "snippet_chars", "flaky_rounds", "impact_map"):
        if key in data and data[key] is not None:
            setattr(config, key, data[key])

    return config
