"""执行器：用例 × 采样 → 环境 → Agent → 打分 → 归因 → 落盘。

一次采样完整走完规范第 7.1 节的流水线：

.. code-block:: text

    用例 → 建沙箱/冻结时钟/工具 Mock → 跑 Agent → 规则断言（短路）
         → Judge（仅当规则通过）→ 四维融合 → 自动归因 → 记录

几个工程决策：

- **短路**：规则硬失败时不跑 Judge。省钱、快，而且避免「规则说错、Judge 说对」
  这种自相矛盾的结论（规范 14.2 第 7 条）。
- **并发但确定性**：多线程跑采样，结果按 (case_id, sample_idx) 排序后落盘，
  保证同一批输入产出稳定的 records.jsonl（便于 diff）。
- **失败也要留档**：Agent 崩溃/超时同样生成一条 error 记录，
  否则「跑了 100 条只有 98 条记录」这种问题会被忽略。
"""

from __future__ import annotations

import dataclasses
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Sequence

from .. import FRAMEWORK_NAME, __version__
from ..agents import AgentError, create_agent
from ..assertions import AssertContext
from ..config import Config, Profile, load_config
from ..envs.clock import FrozenClock, utcnow_iso
from ..envs.sandbox import Sandbox
from ..envs.tool_mock import ToolMockRegistry
from ..grading.judge import (
    JudgeClient,
    build_blind_request,
    build_judge,
    merge_judge_scores,
    verify_blind,
)
from ..grading.rules import RuleGrader
from ..loader import load_cases
from ..metrics.aggregate import RunSummary, aggregate_runs
from ..model import (
    Attribution,
    CaseRun,
    CaseSnapshot,
    RunMetrics,
    TestCase,
    Trajectory,
    Verdict,
    VersionQuad,
)
from ..pipeline.attribution import classify
from ..pipeline.flaky import QuarantineStore, apply_quarantine
from ..trace import RunStore
from ..trace.redact import Redactor

ProgressFn = Callable[[int, int, str], None]


# ---------------------------------------------------------------------------
# 选项与结果
# ---------------------------------------------------------------------------


@dataclass
class RunOptions:
    """一次跑批的输入参数（CLI 与 CI 都通过它驱动）。"""

    case_paths: list[str] = field(default_factory=lambda: ["cases/golden"])
    agent: str = "replay:baseline"
    profile: str = "nightly"
    samples: int | None = None
    out_dir: str = "reports"
    run_id: str | None = None
    config_path: str | None = None
    judge: str = "heuristic"
    judge_base_url: str = ""
    judge_api_key: str = ""
    max_workers: int = 4
    keep_sandboxes: bool = True
    base_dir: str = "."
    case_ids: list[str] = field(default_factory=list)
    layers: list[str] = field(default_factory=list)
    limit: int | None = None
    include_quarantined: bool = False
    auto_quarantine: bool = True
    #: 只跑 impact 分析选出的用例（由 CLI 传入 case_ids 即可，这里保留开关语义）
    reason: str = ""


@dataclass
class RunResult:
    """一次跑批的产出句柄。"""

    run_id: str
    run_dir: str
    summary: RunSummary
    records: list[CaseRun] = field(default_factory=list)
    index: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0
    #: 非阻断的运行期提示（如超出档位时间预算）
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 用例选择
# ---------------------------------------------------------------------------


def select_cases(
    cases: Sequence[TestCase],
    profile: Profile,
    *,
    case_ids: Sequence[str] | None = None,
    layers: Sequence[str] | None = None,
    limit: int | None = None,
    quarantine: QuarantineStore | None = None,
    include_quarantined: bool = False,
) -> list[TestCase]:
    """按执行档位 + 过滤条件选择用例。

    排序规则：优先级（P0→P2）→ 分层（底层优先）→ case_id，
    这样 ``--limit`` 截断时优先跑最关键的用例（PR 档的冒烟逻辑）。
    """
    layer_order = {name: i for i, name in enumerate(
        ["L1", "L2", "L2.5", "L3", "L4", "L4.5", "L5", "L5.5", "L6", "L7"]
    )}
    priority_order = {"P0": 0, "P1": 1, "P2": 2}

    selected: list[TestCase] = []
    for case in cases:
        if not profile.matches(case):
            continue
        if case_ids and case.case_id not in case_ids:
            continue
        if layers and case.layer not in layers:
            continue
        if quarantine and not include_quarantined and quarantine.is_quarantined(case.case_id):
            continue
        selected.append(case)

    # 冒烟补充：PR 档额外纳入 N 条 L5/L6（按优先级取最关键的）
    if profile.smoke_layers and profile.smoke_limit:
        extras = [
            c
            for c in cases
            if c.layer in profile.smoke_layers
            and c not in selected
            and (not case_ids or c.case_id in case_ids)
            and (not quarantine or include_quarantined or not quarantine.is_quarantined(c.case_id))
        ]
        extras.sort(key=lambda c: (priority_order.get(c.priority, 9), c.case_id))
        selected.extend(extras[: profile.smoke_limit])

    selected.sort(
        key=lambda c: (
            priority_order.get(c.priority, 9),
            layer_order.get(c.layer, 99),
            c.case_id,
        )
    )
    effective_limit = limit if limit is not None else profile.limit
    if effective_limit:
        selected = selected[: int(effective_limit)]
    return selected


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------


class Runner:
    """跑批执行器。"""

    def __init__(self, options: RunOptions) -> None:
        self.options = options
        self.config: Config = load_config(options.config_path)
        self.base_dir = os.path.abspath(options.base_dir)
        self.profile: Profile = self._resolve_profile()
        self.judge: JudgeClient | None = self._build_judge()
        self.quarantine = QuarantineStore(
            os.path.join(options.out_dir, "quarantine.json")
        )
        self.grader = RuleGrader(self.config)
        self.progress: ProgressFn | None = None

    # -- 初始化 -----------------------------------------------------------

    def _resolve_profile(self) -> Profile:
        """解析档位；``--profile all`` 表示不限分层（本地全量调试用）。"""
        if self.options.profile in ("all", "full", ""):
            profile = Profile(
                name="all",
                description="全量（不受分层矩阵约束）",
                samples=self.options.samples or self.config.default_samples,
                blocking="full",
            )
            return profile
        profile = self.config.profile(self.options.profile)
        if self.options.samples is not None:
            profile.samples = int(self.options.samples)
            profile.p0_samples = int(self.options.samples)
        return profile

    def _build_judge(self) -> JudgeClient | None:
        if not self.config.judge.enabled:
            return None
        try:
            return build_judge(
                self.options.judge,
                self.config.judge,
                api_key=self.options.judge_api_key,
                base_url=self.options.judge_base_url,
            )
        except ValueError:
            # Judge 配置错误不应阻断整轮跑批：降级为规则判定，并在报告里标注
            return None

    def _run_id(self) -> str:
        if self.options.run_id:
            return self.options.run_id
        from datetime import datetime, timezone

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        agent = self.options.agent.replace(":", "-").replace("/", "-").replace("\\", "-")
        return f"{stamp}_{agent}_{self.profile.name}"

    # -- 主流程 -----------------------------------------------------------

    def run(self, progress: ProgressFn | None = None) -> RunResult:
        started = time.perf_counter()
        self.progress = progress
        load = load_cases(self.options.case_paths, config=self.config, strict=False)
        if not load.cases:
            raise AgentError(
                "没有可用用例：\n" + "\n".join(load.errors[:5] or ["用例目录为空"])
            )

        cases = select_cases(
            load.cases,
            self.profile,
            case_ids=self.options.case_ids,
            layers=self.options.layers,
            limit=self.options.limit,
            quarantine=self.quarantine,
            include_quarantined=self.options.include_quarantined,
        )
        if not cases:
            raise AgentError(f"档位 {self.profile.name!r} 下没有匹配的用例")

        run_id = self._run_id()
        run_dir = os.path.join(self.options.out_dir, "runs", run_id)
        sandbox_root = os.path.join(run_dir, "sandboxes")
        os.makedirs(run_dir, exist_ok=True)

        jobs: list[tuple[TestCase, int]] = []
        for case in cases:
            samples = self.config.samples_for(self.profile, case.priority)
            if self.options.samples is not None:
                samples = int(self.options.samples)
            for idx in range(1, max(1, samples) + 1):
                jobs.append((case, idx))

        records: list[CaseRun] = []
        total = len(jobs)
        done = 0
        workers = max(1, min(self.options.max_workers, 16))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._run_one, case, idx, run_id, sandbox_root): (case, idx)
                for case, idx in jobs
            }
            for future in as_completed(futures):
                case, idx = futures[future]
                try:
                    record = future.result()
                except Exception as exc:  # noqa: BLE001 - 单条失败不能拖垮跑批
                    record = self._error_record(case, idx, run_id, sandbox_root, exc)
                records.append(record)
                done += 1
                if self.progress:
                    self.progress(done, total, case.case_id)

        # 排序保证落盘稳定
        records.sort(key=lambda r: (r.case_id, r.sample_idx))

        summary = aggregate_runs(
            records,
            run_id=run_id,
            generated_at=utcnow_iso(),
            profile=self.profile.name,
            dimension_threshold=self.config.dimension_pass_threshold,
            flaky_rounds=self.config.flaky_rounds,
        )
        # Flaky 标记写回记录，并维护隔离区
        flaky_set = set(summary.flaky_cases)
        for record in records:
            record.flaky = record.case_id in flaky_set
        if self.options.auto_quarantine:
            self.quarantine.update_from_summary(
                summary, flaky_rounds=self.config.flaky_rounds
            )
        apply_quarantine(summary, self.quarantine)

        # 落盘（脱敏）
        warnings: list[str] = []
        redactor = Redactor(self.config.privacy) if self.config.privacy.enabled else None
        if redactor is not None and redactor.has_insecure_salt:
            warnings.append(
                "隐私脱敏仍在使用仓库内置示例盐值：请在生产环境用环境变量 "
                "ATK_PRIVACY_SALT 注入随机长串，否则假名可被暴力反查"
            )
        store = RunStore(run_dir=run_dir, redactor=redactor)
        for record in records:
            store.write_record(record)
        store.write_summary(summary)

        index = {
            "run_id": run_id,
            "framework": f"{FRAMEWORK_NAME}-{__version__}",
            "generated_at": utcnow_iso(),
            "profile": self.profile.name,
            "profile_description": self.profile.description,
            "agent": self.options.agent,
            "judge": self.options.judge if self.judge else "off",
            "options": {
                k: v
                for k, v in dataclasses.asdict(self.options).items()
                if k != "judge_api_key"
            },
            "case_count": len(cases),
            "sample_count": len(records),
            "cases": [
                {
                    "case_id": c.case_id,
                    "title": c.title,
                    "layer": c.layer,
                    "priority": c.priority,
                    "difficulty": c.difficulty,
                    "samples": sum(1 for r in records if r.case_id == c.case_id),
                }
                for c in cases
            ],
            "samples_per_case": {
                c.case_id: self.config.samples_for(self.profile, c.priority) for c in cases
            },
            "pass_k_coverage": summary.pass_k_coverage,
            "flaky_cases": summary.flaky_cases,
            "quarantined_cases": summary.quarantined_cases,
            "redaction": (redactor.stats() if redactor else {}),
            "privacy_salt_insecure": bool(redactor and redactor.has_insecure_salt),
            "load_errors": load.errors[:50],
            "load_warnings": load.warnings[:50],
            "duration_s": round(time.perf_counter() - started, 2),
        }
        # 档位时间预算：超了不中断（避免半途丢数据），但必须看得见，
        # 否则「PR 档 ≤8min」这类约定永远只是配置里的装饰。
        budget_min = int(getattr(self.profile, "time_budget_min", 0) or 0)
        duration_s = index["duration_s"]
        if budget_min > 0 and duration_s > budget_min * 60:
            msg = (
                f"档位 {self.profile.name} 耗时 {duration_s / 60:.1f}min "
                f"超出预算 {budget_min}min；建议拆分档位或启用影响面分析，"
                "否则团队会绕过门禁"
            )
            warnings.append(msg)
            index["time_budget_exceeded"] = True
            index["time_budget_min"] = budget_min
        index["warnings"] = warnings

        store.write_index(index)

        return RunResult(
            run_id=run_id,
            run_dir=run_dir,
            summary=summary,
            records=records,
            index=index,
            duration_s=duration_s,
            warnings=warnings,
        )

    # -- 单次采样 ---------------------------------------------------------

    def _run_one(
        self,
        case: TestCase,
        sample_idx: int,
        run_id: str,
        sandbox_root: str,
    ) -> CaseRun:
        started = time.perf_counter()
        sandbox = Sandbox.create(
            base_dir=sandbox_root,
            case_id=case.case_id,
            sample_idx=sample_idx,
            frozen_time=case.env.frozen_time,
            seed=case.env.seed,
        )
        # 附件复制进沙箱（只读输入）
        for turn in case.input_turns:
            for rel in turn.attachments:
                source = rel
                if not os.path.isabs(source):
                    candidate = os.path.join(self.base_dir, rel)
                    source = candidate if os.path.exists(candidate) else rel
                if os.path.exists(source):
                    sandbox.stage_attachment(source, os.path.basename(rel))
        sandbox.mark_initial()

        clock = FrozenClock(case.env.frozen_time)
        import random

        rng = random.Random(case.env.seed + sample_idx)
        registry = ToolMockRegistry(
            env=case.env,
            sandbox=sandbox,
            clock=clock,
            base_dir=self.base_dir,
            rng=rng,
        )

        adapter = create_agent(
            self.options.agent,
            base_dir=self.base_dir,
            strict=False,
        )
        from ..agents.base import AgentRequest

        request = AgentRequest(
            case=case,
            turns=case.input_turns,
            sandbox=sandbox,
            registry=registry,
            clock=clock,
            rng=rng,
            tools=registry.tool_names(),
            sample_idx=sample_idx,
        )

        verdict: Verdict
        trajectory = Trajectory()
        usage_prompt = usage_completion = usage_tool = 0
        ttft_ms = 0
        meta: dict[str, Any] = {}
        error_note: str | None = None

        try:
            result = adapter.run(request)
            trajectory = result.trajectory
            usage_prompt = result.usage.prompt_tokens
            usage_completion = result.usage.completion_tokens
            usage_tool = result.usage.tool_tokens
            ttft_ms = result.ttft_ms
            meta = result.meta
        except AgentError as exc:
            error_note = str(exc)
        except Exception as exc:  # noqa: BLE001 - 适配器 bug 也要留档
            error_note = f"{type(exc).__name__}: {exc}"

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        steps = len(trajectory.steps)
        # 时延口径：取「真实墙钟」与「轨迹里记录的耗时」的较大者。
        # 回放场景墙钟只有几毫秒，而录制里记录了真实工具耗时；
        # 只有两者取大，才能既覆盖真实运行，又让回放中的时延断言有意义。
        simulated_ms = sum(s.latency_ms for s in trajectory.steps) + sum(
            inv.latency_ms for inv in registry.invocations
        )
        latency_ms = max(elapsed_ms, simulated_ms)

        # token 兜底估算：适配器没报用量时按字符数粗估（4 字符 ≈ 1 token）
        if usage_prompt + usage_completion == 0:
            text = trajectory.text()
            usage_prompt = max(1, len(text) // 4)
            usage_completion = max(1, len(trajectory.final_output) // 4) if trajectory.final_output else 0
        if usage_tool == 0:
            usage_tool = max(
                0,
                sum(len(str(inv.result)) // 4 for inv in registry.invocations),
            )

        metrics = RunMetrics(
            steps=steps,
            prompt_tokens=usage_prompt,
            completion_tokens=usage_completion,
            tool_tokens=usage_tool,
            model_cost_usd=self.config.cost.model_cost(usage_prompt, usage_completion),
            tool_cost_usd=self.config.cost.tool_cost(len(registry.invocations)),
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            tool_calls=len(registry.invocations),
            retries=registry.retry_count(),
            max_steps_hit=steps >= case.env.max_steps,
        )

        version = VersionQuad(
            model=str(meta.get("model", case.meta.notes or "unknown")),
            prompt=str(meta.get("prompt", "unknown")),
            tool_schema=str(meta.get("tool_schema", "unknown")),
            dataset=case.dataset_version,
            framework=f"{FRAMEWORK_NAME}-{__version__}",
        )

        if error_note:
            verdict = Verdict(
                auto="error",
                final="error",
                notes=[f"执行失败: {error_note}"],
            )
            attribution: Attribution | None = Attribution(
                failure_class="env_data",
                confidence=0.5,
                reasons=[f"[执行失败] {error_note}"],
                snippet="",
            )
        else:
            ctx = AssertContext(
                case=case,
                run=CaseRun(
                    run_id=run_id,
                    case_id=case.case_id,
                    sample_idx=sample_idx,
                    version_quad=version,
                    trajectory=trajectory,
                    invocations=registry.invocations,
                    metrics=metrics,
                    verdict=Verdict(),
                    sandbox_dir=sandbox.root,
                    case=CaseSnapshot.from_case(case),
                ),
                sandbox=sandbox,
                registry=registry,
                base_dir=self.base_dir,
                config=self.config,
            )
            outcome = self.grader.grade(case, ctx.run, ctx)
            verdict = outcome.verdict
            # 关键：把打分结论挂回 ctx.run，后续 Judge/归因读到的才是真实结论
            ctx.run.verdict = verdict

            # Judge：只在「规则没有硬失败」时才跑（短路）
            judge_enabled = (
                self.judge is not None
                and case.grade.judge is not None
                and case.grade.judge.enabled
            )
            if judge_enabled and not verdict.rules_failed:
                judge_scores = self._judge(case, ctx, registry, trajectory, metrics)
                if judge_scores:
                    merge_judge_scores(verdict, judge_scores, case)
                    self._recompute_final(case, verdict)
                else:
                    verdict.judge_skipped = True
            else:
                verdict.judge_skipped = True

            attribution = None
            if verdict.final != "pass":
                attribution = classify(
                    ctx.run,
                    case,
                    expected_min_steps=case.expect.expected_min_steps,
                    expected_max_steps=case.expect.expected_max_steps,
                )
                verdict.failure_class = attribution.failure_class
                # 片段长度由 config.snippet_chars 控制，避免 trace 把报告撑爆
                limit = max(80, int(self.config.snippet_chars))
                if attribution.snippet and len(attribution.snippet) > limit:
                    attribution.snippet = attribution.snippet[:limit] + "…（已截断）"
                snippet = attribution.snippet or verdict.failure_snippet
                verdict.failure_snippet = snippet[:limit]

        record = CaseRun(
            run_id=run_id,
            case_id=case.case_id,
            sample_idx=sample_idx,
            version_quad=version,
            trajectory=trajectory,
            invocations=registry.invocations,
            metrics=metrics,
            verdict=verdict,
            started_at=utcnow_iso(),
            duration_ms=elapsed_ms,
            artifacts=sandbox.artifacts(),
            sandbox_dir=sandbox.root,
            case=CaseSnapshot.from_case(case),
            attribution=attribution,
            env_fingerprint={
                "sandbox": sandbox.fingerprint(),
                "tools": registry.fingerprint(),
                "seed": case.env.seed,
                "frozen_time": case.env.frozen_time,
            },
            agent_name=self.options.agent,
        )

        if not self.options.keep_sandboxes and not error_note:
            # 只在成功时清理沙箱；失败现场保留下来供人复核
            sandbox.cleanup()
        return record

    # -- Judge ------------------------------------------------------------

    def _judge(
        self,
        case: TestCase,
        ctx: AssertContext,
        registry: ToolMockRegistry,
        trajectory: Trajectory,
        metrics: RunMetrics,
    ) -> dict:
        """调用 Judge 并返回维度分；任何异常都降级为「跳过 Judge」。"""
        if self.judge is None:
            return {}
        run = ctx.run
        run.trajectory = trajectory
        run.invocations = registry.invocations
        run.metrics = metrics
        run.artifacts = ctx.sandbox.artifacts() if ctx.sandbox else []
        dimensions = (
            case.grade.judge.dimensions if case.grade.judge else ["correctness", "completeness"]
        )
        request = build_blind_request(case, run, dimensions)

        if self.config.judge.blind:
            violations = verify_blind(request, case)
            if violations:
                # 盲评被破坏是框架 bug，宁可跳过 Judge 也不能产出被污染的分数
                run.verdict.notes.append(f"盲评校验失败，已跳过 Judge: {violations[:3]}")
                return {}

        # 自我偏好检查：Judge 与主模型同源时告警（不阻断，但报告里要可见）
        if self.judge.model and self.judge.model in run.version_quad.model:
            run.verdict.notes.append(
                f"Judge({self.judge.model}) 与被测模型同源，存在自我偏好风险"
            )

        try:
            output = self.judge.score(request)
        except Exception as exc:  # noqa: BLE001 - Judge 故障不应让跑批失败
            run.verdict.notes.append(f"Judge 调用失败，已降级为规则判定: {exc}")
            return {}
        return output.scores

    def _recompute_final(self, case: TestCase, verdict: Verdict) -> None:
        """Judge 融合后重算总分与结论（安全仍一票否决）。"""
        if verdict.forbidden_hits:
            verdict.final = "fail"
            return
        if verdict.rules_failed:
            verdict.final = "fail"
            return
        weights = {
            d: w for d, w in case.expect.rubric.items() if d in verdict.dimension_scores
        }
        if not weights:
            return
        total = sum(verdict.dimension_scores[d] * w for d, w in weights.items()) / sum(
            weights.values()
        )
        verdict.final = "pass" if total >= case.expect.min_score else "fail"

    # -- 错误记录 ---------------------------------------------------------

    def _error_record(
        self,
        case: TestCase,
        sample_idx: int,
        run_id: str,
        sandbox_root: str,
        exc: BaseException,
    ) -> CaseRun:
        """把「执行器自身捕获到的异常」也变成一条可审计的记录。"""
        return CaseRun(
            run_id=run_id,
            case_id=case.case_id,
            sample_idx=sample_idx,
            version_quad=VersionQuad(dataset=case.dataset_version),
            trajectory=Trajectory(),
            invocations=[],
            metrics=RunMetrics(),
            verdict=Verdict(
                auto="error",
                final="error",
                notes=[f"执行异常: {type(exc).__name__}: {exc}"],
            ),
            started_at=utcnow_iso(),
            sandbox_dir=os.path.join(sandbox_root, case.case_id, f"s{sample_idx}"),
            case=CaseSnapshot.from_case(case),
            attribution=Attribution(
                failure_class="env_data",
                confidence=0.4,
                reasons=[f"[执行异常] {type(exc).__name__}: {exc}"],
            ),
            agent_name=self.options.agent,
        )


def run_cases(options: RunOptions, progress: ProgressFn | None = None) -> RunResult:
    """函数式入口。"""
    return Runner(options).run(progress=progress)
