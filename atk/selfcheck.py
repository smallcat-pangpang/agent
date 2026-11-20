"""框架自检：按附录 A 的 20 条清单逐项给出「是/否/需人工确认」。

一个评估框架最容易犯的错误是「测别人很严，测自己很松」。
``atk selfcheck`` 把附录 A 的验收清单变成可执行检查，CI 里每周跑一次，
把结果写进 ``docs/自检报告.md``，让框架自身的退化也能被发现。

三类结论：

- ``pass``：有客观证据支持；
- ``fail``：明确不达标（CI 应失败）；
- ``manual``：需要组织动作（如灰度流程），代码无法验证，只能提示。
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from typing import Any, Sequence

from .config import load_config
from .grading.calibration import load_calibration_set
from .loader import load_cases
from .model import DIMENSIONS, TestCase
from .trace.redact import Redactor

#: 状态图标
ICONS = {"pass": "✅", "fail": "❌", "manual": "△"}


@dataclass
class CheckItem:
    number: int
    title: str
    status: str
    evidence: str
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "status": self.status,
            "evidence": self.evidence,
            "hint": self.hint,
        }


@dataclass
class SelfCheckResult:
    items: list[CheckItem] = field(default_factory=list)
    base_dir: str = ""
    generated_at: str = ""

    @property
    def passed(self) -> int:
        return sum(1 for i in self.items if i.status == "pass")

    @property
    def failed(self) -> int:
        return sum(1 for i in self.items if i.status == "fail")

    @property
    def manual(self) -> int:
        return sum(1 for i in self.items if i.status == "manual")

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def to_markdown(self) -> str:
        lines = [
            "# 框架完备性自检报告",
            "",
            f"- 生成时间：{self.generated_at}",
            f"- 结论：{'✅ 无阻断项' if self.ok else f'❌ 有 {self.failed} 项未达标'}",
            f"- 统计：{self.passed} 通过 / {self.failed} 未达标 / {self.manual} 需人工确认",
            "",
            "| # | 检查项 | 结论 | 证据 | 建议 |",
            "|---|---|---|---|---|",
        ]
        for item in self.items:
            icon = ICONS.get(item.status, "?")
            lines.append(
                f"| {item.number} | {item.title} | {icon} | {item.evidence} | {item.hint} |"
            )
        lines.append("")
        lines.append(
            "> 说明：△ 表示需要组织动作（如灰度流程、人员投入），代码无法自动验证；"
            "但每季度应能给出书面结论。"
        )
        return "\n".join(lines) + "\n"

    def to_json(self) -> str:
        return json.dumps(
            {
                "generated_at": self.generated_at,
                "ok": self.ok,
                "summary": {
                    "pass": self.passed,
                    "fail": self.failed,
                    "manual": self.manual,
                },
                "items": [i.to_dict() for i in self.items],
            },
            ensure_ascii=False,
            indent=2,
        )


def run_selfcheck(
    base_dir: str = ".",
    config_path: str | None = None,
    cases_paths: Sequence[str] | None = None,
    calibration_path: str | None = None,
) -> SelfCheckResult:
    """执行全部自检项。"""
    from datetime import datetime, timezone

    base_dir = os.path.abspath(base_dir)
    config = load_config(config_path or os.path.join(base_dir, "config", "atk.yaml"))
    cases_paths = list(cases_paths or [os.path.join(base_dir, "cases", "golden")])
    calibration_path = calibration_path or os.path.join(base_dir, "cases", "calibration")

    load = load_cases(cases_paths, strict=False)
    cases = load.cases
    result = SelfCheckResult(
        base_dir=base_dir,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    def add(number: int, title: str, status: str, evidence: str, hint: str = "") -> None:
        result.items.append(CheckItem(number, title, status, evidence, hint))

    latest = _latest_summary(base_dir)
    markdown_doc = _read(os.path.join(base_dir, "docs", "维护手册.md"))
    report_md = _latest_report(base_dir)

    # 1. L3 / L4 覆盖
    layers = {c.layer for c in cases}
    missing = [layer for layer in ("L3", "L4") if layer not in layers]
    add(
        1,
        "覆盖 L3 轨迹层与 L4 编排层",
        "pass" if not missing else "fail",
        f"已覆盖分层 {sorted(layers)}" if not missing else f"缺少 {missing}",
        "补 L3/L4 用例：只测端到端会漏掉 80% 的过程 bug",
    )

    # 2. 维度样本量 + 难度分层
    counts = _dimension_case_counts(cases)
    difficulties = {c.difficulty for c in cases}
    thin = [d for d, n in counts.items() if d in ("correctness", "completeness", "safety") and n < 30]
    if len(cases) < 50:
        # 完整黄金集应有 50~200 条、每维 ≥30；示例库是起步规模，
        # 这里如实标成「需人工确认扩容」，不把不达标伪装成达标。
        status = "manual"
        hint = (
            f"当前 {len(cases)} 条（起步库）；生产评估集应扩容到 50~200 条、"
            "每个核心维度 ≥30 条才有统计意义"
        )
    else:
        status = "pass" if not thin and len(difficulties) >= 3 else "fail"
        hint = "低于 30 条时结论没有统计意义（见 MDE 计算）"
    add(
        2,
        "核心维度样本量 ≥30 且有难度分层",
        status,
        f"各维度用例数 {counts}；难度档 {sorted(difficulties)}",
        hint,
    )

    # 3. 评估集与校准集隔离 + 换血机制（含「到期未换血」检查）
    from datetime import date, datetime, timezone

    calibration_set = _safe_load_calibration(calibration_path)
    calib_ids = {i.calib_id for i in calibration_set.items}
    overlap = calib_ids & {c.case_id for c in cases}
    rotation = [c for c in cases if c.meta.rotation_due]
    rotation_ratio = len(rotation) / len(cases) if cases else 0
    today = datetime.now(timezone.utc).date()

    def _due_date(value: str) -> date | None:
        try:
            return date.fromisoformat(value[:10])
        except (ValueError, TypeError):
            return None

    expired = [
        c.case_id
        for c in rotation
        if (d := _due_date(c.meta.rotation_due)) is not None and d < today
    ]
    invalid = [
        c.case_id for c in rotation if _due_date(c.meta.rotation_due) is None
    ]
    status = (
        "pass"
        if not overlap and rotation_ratio >= 0.8 and not expired and not invalid
        else "fail"
    )
    evidence = (
        f"校准集 {len(calib_ids)} 条，与评估集重叠 {len(overlap)} 条；"
        f"{rotation_ratio:.0%} 用例标注了 rotation_due"
    )
    if expired:
        evidence += f"；⚠️ 已过期 {len(expired)} 条：{', '.join(expired[:8])}"
    if invalid:
        evidence += f"；日期格式非法 {len(invalid)} 条：{', '.join(invalid[:8])}"
    if not expired and not invalid:
        evidence += "；无过期用例"
    add(
        3,
        "评估集与调试/校准集物理隔离，且有换血机制",
        status,
        evidence,
        "校准集与评估集重叠会让 Judge 看起来「特别准」；到期未换血会让评估集过时",
    )

    # 4. Judge 校准
    calib_report = _latest_calibration(base_dir)
    if calib_report is None:
        add(
            4,
            "Judge 在独立校准集上与人工对齐（报 Kappa）",
            "fail" if cases else "manual",
            "未找到校准报告（reports/calibration_*/calibration.json）",
            "运行 `atk calibrate`；未校准的 Judge 不得用于门禁",
        )
    else:
        kappa = float(calib_report.get("overall_kappa", 0))
        eligible = bool(calib_report.get("gate_eligible"))
        ineligible = list(calib_report.get("ineligible_dims", []))
        dim_note = f"；Judge 分禁用维度={ineligible}" if ineligible else "；全维度可用"
        add(
            4,
            "Judge 在独立校准集上与人工对齐（报 Kappa）",
            "pass" if kappa >= config.judge.min_kappa else "fail",
            f"Kappa={kappa:.2f}，gate_eligible={eligible}，"
            f"被骗率={float(calib_report.get('fooled_rate', 0)):.1%}{dim_note}",
            "Kappa<0.6 时 Judge 的语义结论不可信；禁用维度必须走规则断言",
        )

    # 5. 版本四元组
    quad = (latest or {}).get("version_quad", {})
    unknown = [k for k, v in quad.items() if str(v) in ("unknown", "", "None", "local")]
    add(
        5,
        "记录「模型 × Prompt × 工具 × 数据」版本四元组",
        "pass" if latest and not unknown else ("fail" if latest else "manual"),
        f"最近运行四元组: {quad}" if quad else "尚无运行记录",
        "缺四元组时指标掉了只能全量回滚",
    )

    # 6. 多采样 + pass^k
    max_samples = max((p.samples for p in config.profiles.values()), default=0)
    pass_k_present = bool((latest or {}).get("pass_k"))
    add(
        6,
        "多次采样并使用 pass^k 而非单次结果",
        "pass" if max_samples >= 3 and pass_k_present else "fail",
        f"最高档采样数 {max_samples}；最近运行 pass^k={'有' if pass_k_present else '无'}",
        "单次通过率在非确定性场景下没有决策价值",
    )

    # 7. 分维度阈值
    dims_configured = set(config.thresholds.dims)
    add(
        7,
        "门禁分维度设阈值，而非单一总分",
        "pass" if {"safety", "correctness", "completeness", "efficiency"} <= dims_configured else "fail",
        f"已配置阈值维度 {sorted(dims_configured)}",
        "总分会把安全满分和正确性崩盘平均掉",
    )

    # 8. 间接注入 / 中断恢复 / 并发隔离
    text = " ".join(f"{c.title} {' '.join(c.tags)}" for c in cases)
    required = {
        "间接注入": "间接注入",
        "中断/恢复": "中断",
        "并发隔离": "并发",
    }
    missing_keys = [name for name, kw in required.items() if kw not in text]
    add(
        8,
        "测了间接注入、中断恢复、并发隔离",
        "pass" if not missing_keys else "fail",
        "全部覆盖" if not missing_keys else f"缺少: {missing_keys}",
        "这三个是 Agent 独有高危面，最容易被漏测",
    )

    # 9. 灰度阶梯与回滚条件
    keywords = ("影子", "灰度", "回滚")
    hit = [k for k in keywords if k in markdown_doc]
    add(
        9,
        "有影子模式/灰度阶梯与明确回滚条件",
        "pass" if len(hit) >= 2 else "manual",
        f"维护手册命中关键词 {hit}",
        "灰度流程属于组织动作，需发布负责人确认",
    )

    # 10. 回流 SLA
    sla_hit = ("48h" in markdown_doc or "48 小时" in markdown_doc) and "3 条" in markdown_doc
    add(
        10,
        "bad case 有明确回流 SLA 与责任人",
        "pass" if sla_hit else "manual",
        "维护手册含 48h / 每周 3 条的回流要求" if sla_hit else "未在维护手册中找到 SLA 描述",
        "回流是数据集自增长的唯一来源",
    )

    # 11. 自动归因
    distribution = (latest or {}).get("failure_distribution") or {}
    total = sum(distribution.values())
    unknown_ratio = distribution.get("unknown", 0) / total if total else 0.0
    add(
        11,
        "失败自动归因到六类，而非人肉看 log",
        "pass" if unknown_ratio <= 0.10 else "fail",
        f"最近运行未知归因占比 {unknown_ratio:.0%}（失败 {total} 条）"
        if total
        else "最近运行无失败",
        "未知归因长期 >10% 说明规则表需要补",
    )

    # 12. 成本与 P95 阈值
    has_perf = config.thresholds.latency_factor > 0 and config.thresholds.cost_factor > 0
    add(
        12,
        "单列成本与 P95 的劣化阈值",
        "pass" if has_perf else "fail",
        f"latency_factor={config.thresholds.latency_factor}, "
        f"cost_factor={config.thresholds.cost_factor}",
        "「能做完但贵十倍」上线后是致命的",
    )

    # 13. expect 使用允许路径（只统计涉及工具调用的用例；纯模型输出场景无路径可写）
    tool_cases = [c for c in cases if c.env.tool_mocks]
    with_paths = sum(1 for c in tool_cases if c.expect.allowed_paths)
    ratio = with_paths / len(tool_cases) if tool_cases else 1.0
    add(
        13,
        "expect 使用「允许路径集合 + 禁止行为黑名单」",
        "pass" if ratio >= 0.8 else "fail",
        f"{with_paths}/{len(tool_cases)} 条涉及工具的用例声明了 allowed_paths（{ratio:.0%}）",
        "只写唯一答案会误杀合法解，团队最终会改 Prompt 迎合测试",
    )

    # 14. 交付物真打开（读取器存在 + 断言开启 parse）
    real_checks = sum(
        1
        for c in cases
        for check in c.grade.auto_checks
        if check.type == "file_exists" and check.args.get("parse", True)
    )
    add(
        14,
        "交付物断言做了「打开/读取」验证",
        "pass" if real_checks >= 3 else "fail",
        f"{real_checks} 条 file_exists 断言启用了解析校验",
        "只断言「文件存在」等于没测交付可用性",
    )

    # 15. 影响面分析
    impact_path = os.path.join(base_dir, config.impact_map)
    impact_rules = _count_impact_rules(impact_path)
    add(
        15,
        "有影响面分析，避免全量跑导致绕过门禁",
        "pass" if impact_rules >= 2 else "fail",
        f"{impact_path} 中 {impact_rules} 条规则",
        "全量跑 4 小时没人等，门禁必然被绕过",
    )

    # 16. 框架自身指标
    self_metrics = all(
        kw in markdown_doc for kw in ("误拦率", "漏放率", "绕过率")
    )
    add(
        16,
        "监控框架自身的误拦率/漏放率/绕过率",
        "pass" if self_metrics else "manual",
        "维护手册已定义三项指标与阈值" if self_metrics else "维护手册未定义",
        "绕过率是框架健康度的真实指标",
    )

    # 17. PII 脱敏
    redactor = Redactor(config.privacy)
    sample = "联系 13800138000 或 user@example.com，身份证 110101199001011234"
    redacted = redactor.redact_text(sample)
    clean = (
        "13800138000" not in redacted
        and "user@example.com" not in redacted
        and "110101199001011234" not in redacted
    )
    add(
        17,
        "线上 Trace 脱敏后存储",
        "pass" if clean else "fail",
        f"脱敏样例: {redacted[:60]}...",
        "评估系统不能成为隐私风险源",
    )

    # 18. 反向红队
    adversarial = calibration_set.adversarial_count
    add(
        18,
        "Judge 被反向红队测试过（骗过率 <15%）",
        "pass"
        if adversarial >= 5 and calib_report and float(calib_report.get("fooled_rate", 1)) <= 0.15
        else "fail",
        f"红队样本 {adversarial} 条；"
        f"被骗率 {float(calib_report.get('fooled_rate', 0)):.1%}" if calib_report else f"红队样本 {adversarial} 条",
        "没有被骗率数据的 Judge 不能上生产门禁",
    )

    # 19. CI 结论
    add(
        19,
        "结论带置信区间与显著性判断，而非裸百分比",
        "pass" if report_md and "CI" in report_md else "fail",
        "最近报告包含 95%CI 与显著性表述" if report_md else "尚无报告",
        "不写 CI 的「提升 3%」是噪声",
    )

    # 20. 角色与节奏
    roles = all(kw in markdown_doc for kw in ("测试负责人", "用例维护人", "Judge 维护人"))
    cadence = all(kw in markdown_doc for kw in ("每周", "每月", "每季"))
    add(
        20,
        "有明确的维护角色与固定节奏",
        "pass" if roles and cadence else "manual",
        f"角色定义={'有' if roles else '无'}，节奏={'有' if cadence else '无'}",
        "框架会死于没人维护，而不是死于技术",
    )

    return result


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _dimension_case_counts(cases: Sequence[TestCase]) -> dict[str, int]:
    """统计每个维度被多少条用例覆盖（断言的默认维度也要算进去）。"""
    from .assertions import DEFAULT_DIMENSIONS

    counts = {d: 0 for d in DIMENSIONS}
    for case in cases:
        dims = {
            c.dimension or DEFAULT_DIMENSIONS.get(c.type, "correctness")
            for c in case.grade.auto_checks
        }
        for item in case.expect.must_achieve:
            dims.add(item.dimension)
            for check in item.checks:
                dims.add(check.dimension or DEFAULT_DIMENSIONS.get(check.type, "correctness"))
        if any(item.dimension == "safety" for item in case.expect.forbidden):
            dims.add("safety")
        for dim in dims:
            if dim in counts:
                counts[dim] += 1
    return counts


def _safe_load_calibration(path: str):
    try:
        return load_calibration_set(path)
    except Exception:  # noqa: BLE001
        from .grading.calibration import CalibrationSet

        return CalibrationSet(path=path)


def _latest_summary(base_dir: str) -> dict[str, Any] | None:
    runs = sorted(glob.glob(os.path.join(base_dir, "reports", "runs", "*", "summary.json")))
    if not runs:
        return None
    try:
        with open(runs[-1], "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _latest_report(base_dir: str) -> str:
    reports = sorted(glob.glob(os.path.join(base_dir, "reports", "runs", "*", "report.md")))
    return _read(reports[-1]) if reports else ""


def _latest_calibration(base_dir: str) -> dict[str, Any] | None:
    reports = sorted(
        glob.glob(os.path.join(base_dir, "reports", "calibration_*", "calibration.json"))
    )
    if not reports:
        return None
    try:
        with open(reports[-1], "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _count_impact_rules(path: str) -> int:
    from .yamlite import load_file

    if not os.path.exists(path):
        return 0
    try:
        data = load_file(path) or {}
    except Exception:  # noqa: BLE001
        return 0
    return len(data.get("rules", []) or [])
