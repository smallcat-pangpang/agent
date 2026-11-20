"""Judge 校准工程（规范 5.2：不是可选项）。

校准回答一个致命问题：**裁判的尺子准不准**。流程：

1. Judge 跑校准集（人工精标、与评估集不重叠）；
2. 与人工逐条比对，算 Cohen's Kappa（κ≥0.6 可用，≥0.8 良好）；
3. 混淆矩阵：重点是「人工判错/Judge 判对」（过宽）与「人工判对/Judge 判错」（过严）；
4. 偏差诊断：长度偏差（长答案更易判对）、位置偏差（偏好先出现者）、自我偏好；
5. 反向红队：造「看似正确实则错误」的样本，被骗率 >15% 的 Judge 不得用于门禁。

校准集文件格式（YAML）::

    items:
      - calib_id: CAL-0001
        title: 订单导出结果正确
        turns: ["把本周订单导出成 Excel 并发到运营群"]
        final_output: "已导出 12 条订单并发送到运营群"
        tool_outputs: ["{\\"orders\\": [12 条]}"]
        trajectory_digest: ["step2 调用 orders_api", "step3 返回 成功"]
        artifacts: ["orders.xlsx"]
        human: {correctness: 0.9, completeness: 0.9, efficiency: 0.8, safety: 1.0}
        adversarial: false
        note: 标准正例
    pairs:
      - id: P1
        a: {final_output: "...", artifacts: []}
        b: {final_output: "...", artifacts: []}
        human_winner: a
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import JudgeConfig
from ..metrics.stats import cohens_kappa, spearman
from ..model import DIMENSIONS
from ..yamlite import load_file
from .judge import JudgeClient, JudgeRequest

#: 人工分/判官分转二分类时的阈值（与维度过线阈值保持一致）
DEFAULT_BINARY_THRESHOLD = 0.6


@dataclass
class CalibrationItem:
    """一条校准样本。"""

    calib_id: str
    title: str = ""
    turns: list[str] = field(default_factory=list)
    final_output: str = ""
    tool_outputs: list[str] = field(default_factory=list)
    trajectory_digest: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    human: dict[str, float] = field(default_factory=dict)
    adversarial: bool = False
    note: str = ""

    @property
    def dimensions(self) -> list[str]:
        return [d for d in DIMENSIONS if d in self.human] or list(DIMENSIONS)

    def request(self) -> JudgeRequest:
        """转成盲评请求（同样不含 expect —— 校准集也没有 expect）。"""
        return JudgeRequest(
            case_id=self.calib_id,
            title=self.title,
            turns=[{"role": "user", "content": t} for t in self.turns],
            final_output=self.final_output,
            trajectory_digest=list(self.trajectory_digest),
            artifacts=list(self.artifacts),
            dimensions=list(self.human),
            tool_outputs=list(self.tool_outputs),
        )

    def human_labels(self, threshold: float) -> dict[str, str]:
        return {
            dim: ("pass" if score >= threshold else "fail")
            for dim, score in self.human.items()
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CalibrationItem":
        human_raw = data.get("human") or {}
        if isinstance(human_raw, (int, float)):
            human_raw = {"correctness": float(human_raw)}
        return cls(
            calib_id=str(data.get("calib_id") or data.get("id") or ""),
            title=str(data.get("title", "")),
            turns=[str(x) for x in data.get("turns", []) or []],
            final_output=str(data.get("final_output", "")),
            tool_outputs=[str(x) for x in data.get("tool_outputs", []) or []],
            trajectory_digest=[str(x) for x in data.get("trajectory_digest", []) or []],
            artifacts=[str(x) for x in data.get("artifacts", []) or []],
            human={str(k): float(v) for k, v in human_raw.items()},
            adversarial=bool(data.get("adversarial", False)),
            note=str(data.get("note", "")),
        )


@dataclass
class PairItem:
    """成对比较样本（位置偏差诊断用）。"""

    pair_id: str
    a: dict[str, Any]
    b: dict[str, Any]
    human_winner: str = ""   # "a" / "b" / "tie"

    def request(self, side: str) -> JudgeRequest:
        data = self.a if side == "a" else self.b
        return JudgeRequest(
            case_id=f"{self.pair_id}-{side}",
            title=str(data.get("title", "")),
            turns=[{"role": "user", "content": str(t)} for t in data.get("turns", [])],
            final_output=str(data.get("final_output", "")),
            artifacts=[str(x) for x in data.get("artifacts", []) or []],
            dimensions=["correctness"],
            tool_outputs=[str(x) for x in data.get("tool_outputs", []) or []],
        )


@dataclass
class CalibrationSet:
    """完整校准集。"""

    items: list[CalibrationItem] = field(default_factory=list)
    pairs: list[PairItem] = field(default_factory=list)
    path: str = ""
    version: str = ""

    @property
    def adversarial_count(self) -> int:
        return sum(1 for i in self.items if i.adversarial)

    def check(self) -> list[str]:
        """校准集自检（避免拿一份不合格的校准集得出「Judge 很准」的假结论）。"""
        problems: list[str] = []
        if len(self.items) < 20:
            problems.append(f"校准集只有 {len(self.items)} 条，建议 50~100 条（最低 20 条）")
        if self.adversarial_count < 5:
            problems.append(f"反向红队样本只有 {self.adversarial_count} 条，建议 ≥5 条")
        ids = [i.calib_id for i in self.items]
        if len(ids) != len(set(ids)):
            problems.append("calib_id 存在重复")
        for item in self.items:
            if not item.human:
                problems.append(f"{item.calib_id} 缺少 human 标注")
            # 注意：空的 final_output 是合法样本（「模型什么都没答」），不能当成缺字段
        covered = {d for item in self.items for d in item.human}
        missing = [d for d in DIMENSIONS if d not in covered]
        if missing:
            problems.append(f"校准集未覆盖维度: {missing}")
        return problems


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------


def load_calibration_set(path: str) -> CalibrationSet:
    """从文件或目录加载校准集。"""
    files: list[str] = []
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if name.lower().endswith((".yaml", ".yml", ".json")):
                files.append(os.path.join(path, name))
    else:
        files = [path]

    result = CalibrationSet(path=path)
    for file_path in files:
        if file_path.lower().endswith(".json"):
            with open(file_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        else:
            data = load_file(file_path)
        if not isinstance(data, dict):
            continue
        if data.get("version"):
            result.version = str(data["version"])
        for raw in data.get("items", []) or []:
            result.items.append(CalibrationItem.from_dict(raw))
        for raw in data.get("pairs", []) or []:
            result.pairs.append(
                PairItem(
                    pair_id=str(raw.get("id", f"P{len(result.pairs) + 1}")),
                    a=dict(raw.get("a") or {}),
                    b=dict(raw.get("b") or {}),
                    human_winner=str(raw.get("human_winner", "")),
                )
            )
    return result


# ---------------------------------------------------------------------------
# 校准计算
# ---------------------------------------------------------------------------


@dataclass
class CalibrationReport:
    """校准结论。"""

    judge_model: str = ""
    judge_name: str = ""
    set_path: str = ""
    generated_at: str = ""
    item_count: int = 0
    adversarial_count: int = 0
    threshold: float = DEFAULT_BINARY_THRESHOLD
    #: 每维度的 Kappa / 混淆矩阵 / 一致率
    per_dimension: dict[str, dict[str, Any]] = field(default_factory=dict)
    overall_kappa: float = 0.0
    accuracy: float = 0.0
    confusion: dict[str, int] = field(default_factory=dict)
    length_bias: float = 0.0
    position_consistency: float | None = None
    first_position_rate: float | None = None
    self_preference_risk: bool = False
    fooled_count: int = 0
    fooled_rate: float = 0.0
    #: 整体准入结论（保留布尔值，供 selfcheck / 报告 / --strict 兼容使用）
    gate_eligible: bool = False
    #: 分维度准入：只有标注为 True 的维度，其 Judge 分才允许进门禁
    dimension_eligible: dict[str, bool] = field(default_factory=dict)
    #: 禁用维度列表（写入报告，避免「整体合格」掩盖单个维度不可信）
    ineligible_dims: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "judge_model": self.judge_model,
            "judge_name": self.judge_name,
            "set_path": self.set_path,
            "generated_at": self.generated_at,
            "item_count": self.item_count,
            "adversarial_count": self.adversarial_count,
            "threshold": self.threshold,
            "per_dimension": self.per_dimension,
            "overall_kappa": round(self.overall_kappa, 4),
            "accuracy": round(self.accuracy, 4),
            "confusion": self.confusion,
            "length_bias": round(self.length_bias, 4),
            "position_consistency": self.position_consistency,
            "first_position_rate": self.first_position_rate,
            "self_preference_risk": self.self_preference_risk,
            "fooled_count": self.fooled_count,
            "fooled_rate": round(self.fooled_rate, 4),
            "gate_eligible": self.gate_eligible,
            "dimension_eligible": self.dimension_eligible,
            "ineligible_dims": self.ineligible_dims,
            "reasons": self.reasons,
            "problems": self.problems,
            "details": self.details,
        }

    def to_markdown(self) -> str:
        """生成人能看懂的校准报告（CI artifact 直接用）。"""
        icon = "✅ 可用于门禁" if self.gate_eligible else "❌ 不得用于门禁"
        if self.ineligible_dims:
            icon += f"（禁用维度：{', '.join(self.ineligible_dims)}）"
        lines = [
            f"# Judge 校准报告：{self.judge_name} / {self.judge_model}",
            "",
            f"- 校准集：`{self.set_path}`（{self.item_count} 条，其中反向红队 {self.adversarial_count} 条）",
            f"- 生成时间：{self.generated_at}",
            f"- 结论：**{icon}**",
            f"- 总体 Kappa：**{self.overall_kappa:.3f}**，一致率 {self.accuracy:.1%}",
            f"- 反向红队被骗率：**{self.fooled_rate:.1%}**（上限 15%）",
        ]
        if self.ineligible_dims:
            lines += [
                "",
                f"> ⚠️ **以下维度未通过校准，其 Judge 分不得用于门禁**："
                f"{', '.join(self.ineligible_dims)}。"
                "这些维度一律以规则断言为准；安全维度尤其不允许用语义分兜底。",
            ]
        lines += [
            "",
            "## 分维度一致性",
            "",
            "| 维度 | Kappa | 一致率 | 人工判对/Judge判错 | 人工判错/Judge判对 | 样本 | 可进门禁 |",
            "|---|---|---|---|---|---|---|",
        ]
        for dim, row in self.per_dimension.items():
            eligible = self.dimension_eligible.get(dim)
            mark = "✅" if eligible else ("❌ 禁用" if eligible is False else "—")
            lines.append(
                f"| {dim} | {row['kappa']:.3f} | {row['accuracy']:.1%} | "
                f"{row['false_negative']} | {row['false_positive']} | {row['n']} | {mark} |"
            )

        lines += [
            "",
            "## 偏差诊断",
            "",
            f"- 长度偏差（输出长度 vs 判官分，Spearman）：{self.length_bias:+.3f}"
            + (
                "（|ρ|>0.3 视为存在明显长度偏好）"
                if abs(self.length_bias) > 0.3
                else ""
            ),
        ]
        if self.position_consistency is not None:
            lines.append(
                f"- 位置一致性：{self.position_consistency:.1%}"
                f"（首次位置被选中率 {self.first_position_rate:.1%}，"
                "接近 100% 说明存在位置偏差）"
            )
        else:
            lines.append("- 位置一致性：未配置成对样本（pairs），无法诊断")
        lines.append(
            f"- 自我偏好风险：{'是（Judge 与被测模型同源）' if self.self_preference_risk else '否'}"
        )

        if self.reasons:
            lines += ["", "## 判定依据", ""]
            lines += [f"- {r}" for r in self.reasons]
        if self.problems:
            lines += ["", "## 校准集问题", ""]
            lines += [f"- {p}" for p in self.problems]

        if self.details:
            lines += [
                "",
                "## 逐条明细（前 20 条）",
                "",
                "| 样本 | 维度 | 人工 | Judge | 分数 | 红队 |",
                "|---|---|---|---|---|---|",
            ]
            for row in self.details[:20]:
                lines.append(
                    f"| {row['calib_id']} | {row['dimension']} | {row['human_label']} | "
                    f"{row['judge_label']} | {row['judge_score']:.2f} | "
                    f"{'是' if row['adversarial'] else ''} |"
                )
        return "\n".join(lines) + "\n"


def calibrate(
    judge: JudgeClient,
    calibration: CalibrationSet,
    config: JudgeConfig,
    threshold: float = DEFAULT_BINARY_THRESHOLD,
    model_under_test: str = "",
) -> CalibrationReport:
    """跑校准集，输出可信度证据。"""
    report = CalibrationReport(
        judge_model=judge.model,
        judge_name=judge.name,
        set_path=calibration.path,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        item_count=len(calibration.items),
        adversarial_count=calibration.adversarial_count,
        threshold=threshold,
        problems=calibration.check(),
    )

    human_all: list[str] = []
    judge_all: list[str] = []
    lengths: list[float] = []
    overall_scores: list[float] = []
    confusion = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}

    # 每个维度单独累计
    per_dim_pairs: dict[str, tuple[list[str], list[str]]] = {}

    for item in calibration.items:
        output = judge.score(item.request())
        human_labels = item.human_labels(threshold)
        lengths.append(len(item.final_output))
        overall_scores.append(output.overall())

        for dim, human_label in human_labels.items():
            score = output.scores.get(dim)
            judge_score = score.score if score else 0.0
            judge_label = "pass" if judge_score >= threshold else "fail"
            human_all.append(human_label)
            judge_all.append(judge_label)
            per_dim_pairs.setdefault(dim, ([], []))
            per_dim_pairs[dim][0].append(human_label)
            per_dim_pairs[dim][1].append(judge_label)

            report.details.append(
                {
                    "calib_id": item.calib_id,
                    "dimension": dim,
                    "human_label": human_label,
                    "human_score": item.human.get(dim),
                    "judge_label": judge_label,
                    "judge_score": round(judge_score, 3),
                    "adversarial": item.adversarial,
                    "evidence": (score.evidence if score else []),
                }
            )

            # 混淆矩阵：pass 为正类
            if human_label == "pass" and judge_label == "pass":
                confusion["tp"] += 1
            elif human_label == "fail" and judge_label == "pass":
                confusion["fp"] += 1
            elif human_label == "fail" and judge_label == "fail":
                confusion["tn"] += 1
            else:
                confusion["fn"] += 1

            # 反向红队：人工判 fail、Judge 判 pass → 被骗
            if item.adversarial and human_label == "fail" and judge_label == "pass":
                pass  # 统一在下面按样本统计

    # 分维度 Kappa
    for dim, (human, judged) in per_dim_pairs.items():
        n = len(human)
        if n == 0:
            continue
        kappa = cohens_kappa(human, judged)
        accuracy = sum(1 for h, j in zip(human, judged) if h == j) / n
        report.per_dimension[dim] = {
            "n": n,
            "kappa": round(kappa, 4),
            "accuracy": round(accuracy, 4),
            "false_positive": sum(1 for h, j in zip(human, judged) if h == "fail" and j == "pass"),
            "false_negative": sum(1 for h, j in zip(human, judged) if h == "pass" and j == "fail"),
        }

    report.overall_kappa = cohens_kappa(human_all, judge_all)
    report.accuracy = (
        sum(1 for h, j in zip(human_all, judge_all) if h == j) / len(human_all)
        if human_all
        else 0.0
    )
    report.confusion = confusion

    # 长度偏差
    report.length_bias = spearman(lengths, overall_scores)

    # 反向红队被骗率：**按样本计**，看整体结论是否被放过。
    # 只要 Judge 判「整体通过」而人工判「整体失败」，就算被欺骗；
    # 单个维度偶然判错不算被骗（那由 Kappa/混淆矩阵反映）。
    adversarial_items = [i for i in calibration.items if i.adversarial]
    if adversarial_items:
        fooled = 0
        for item in adversarial_items:
            output = judge.score(item.request())
            human_pass = all(score >= threshold for score in item.human.values())
            judge_pass = bool(output.scores) and all(
                score.score >= threshold for score in output.scores.values()
            )
            if (not human_pass) and judge_pass:
                fooled += 1
        report.fooled_count = fooled
        report.fooled_rate = fooled / len(adversarial_items)

    # 位置偏差（成对比较）
    if calibration.pairs and hasattr(judge, "compare"):
        consistent = 0
        first_chosen = 0
        total = 0
        for pair in calibration.pairs:
            forward = judge.compare(pair.request("a"), pair.request("b"))
            backward = judge.compare(pair.request("b"), pair.request("a"))
            mapped = {"a": "b", "b": "a", "tie": "tie"}[backward]
            total += 1
            if forward == mapped:
                consistent += 1
            if forward == "a":
                first_chosen += 1
        if total:
            report.position_consistency = consistent / total
            report.first_position_rate = first_chosen / total

    # 自我偏好：模型名互相包含，或命中配置里的同源禁用名单
    if judge.model:
        judge_model = judge.model.lower()
        same_as_config = [
            name
            for name in config.forbidden_same_as
            if str(name).lower() in judge_model
        ]
        same_as_subject = bool(
            model_under_test
            and (
                judge_model in model_under_test.lower()
                or model_under_test.lower() in judge_model
            )
        )
        report.self_preference_risk = bool(same_as_config or same_as_subject)
        if same_as_config:
            report.reasons.append(
                f"Judge 模型命中配置禁用的同源名单：{same_as_config}"
            )
    if report.overall_kappa >= config.good_kappa:
        report.reasons.append(
            f"总体 Kappa {report.overall_kappa:.2f} ≥ {config.good_kappa:.2f}，一致性良好"
        )

    # 结论：全局阻断项 + 分维度准入
    min_kappa = config.min_kappa
    global_ok = (
        report.overall_kappa >= min_kappa
        and report.fooled_rate <= config.max_fooled_rate
        and report.adversarial_count >= 5
        and not report.self_preference_risk
    )
    report.dimension_eligible = {
        dim: bool(global_ok and row["kappa"] >= min_kappa)
        for dim, row in report.per_dimension.items()
    }
    report.ineligible_dims = sorted(
        dim for dim, ok in report.dimension_eligible.items() if not ok
    )
    weak_dims = [
        dim for dim, row in report.per_dimension.items() if row["kappa"] < min_kappa
    ]
    if weak_dims:
        report.reasons.append(
            f"以下维度 Kappa 低于 {min_kappa}: {weak_dims}（总体 Kappa {report.overall_kappa:.2f}）；"
            "这些维度的 Judge 分已被禁止进入门禁，只保留规则断言结论"
        )
    if report.fooled_rate > config.max_fooled_rate:
        report.reasons.append(
            f"反向红队被骗率 {report.fooled_rate:.1%} > {config.max_fooled_rate:.0%}，Judge 可被「看似正确」的样本欺骗"
        )
    if report.adversarial_count < 5:
        report.reasons.append("反向红队样本不足 5 条，未能验证抗欺骗能力")
    if report.self_preference_risk:
        report.reasons.append("Judge 与被测模型同源，存在自我偏好风险（规范明确禁止）")
    if abs(report.length_bias) > 0.3:
        report.reasons.append(
            f"存在明显长度偏差（Spearman {report.length_bias:+.2f}）：长答案更容易被判对"
        )

    # gate_eligible 表示「整体准入」；单维度是否可用请看 dimension_eligible。
    # 只要有维度被禁用，结论里必须点名，避免「✅ 可用于门禁」被单独引用。
    report.gate_eligible = global_ok
    if report.gate_eligible:
        detail = (
            f"，仅 {', '.join(report.ineligible_dims)} 维度禁用"
            if report.ineligible_dims
            else "，全维度可用"
        )
        report.reasons.append(
            f"Kappa {report.overall_kappa:.2f} ≥ {min_kappa} 且被骗率 "
            f"{report.fooled_rate:.1%} ≤ {config.max_fooled_rate:.0%}，可用于门禁{detail}"
        )
    return report


def save_report(report: CalibrationReport, out_dir: str) -> tuple[str, str]:
    """把校准报告写到磁盘，返回 (json 路径, md 路径)。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = os.path.join(out_dir, f"calibration_{stamp}")
    os.makedirs(target, exist_ok=True)
    json_path = os.path.join(target, "calibration.json")
    md_path = os.path.join(target, "calibration.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(report.to_markdown())
    return json_path, md_path
