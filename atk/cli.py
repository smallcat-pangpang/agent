"""命令行入口。

设计上把「执行、判定、出报告」拆成独立子命令，也提供一条龙用法：

.. code-block:: bash

    atk validate cases/golden           # 用例体检（Schema + 语义）
    atk run --profile nightly --agent replay:baseline --samples 3
    atk gate --run reports/runs/<id> --baseline auto
    atk report --run reports/runs/<id> --format md,html
    atk calibrate --judge heuristic --set cases/calibration
    atk dashboard
    atk impact --git-base HEAD~1
    atk selfcheck --write docs/自检报告.md

退出码约定（CI 直接用）：

- ``0``：通过
- ``1``：门禁阻断 / 校验失败 / 自检未达标
- ``2``：用法错误或框架异常
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from typing import Sequence

from . import __version__
from .config import load_config
from .errors import AtkError
from .grading.calibration import calibrate, load_calibration_set, save_report
from .grading.judge import build_judge
from .loader import load_cases
from .metrics.aggregate import RunSummary
from .pipeline.gate import evaluate_gate
from .pipeline.impact import ImpactGitError, ImpactMap, analyze, git_changed_files
from .pipeline.runner import RunOptions, Runner
from .report import build_dashboard, generate_reports
from .trace.store import list_runs, load_summary

EXIT_OK = 0
EXIT_BLOCK = 1
EXIT_ERROR = 2


# ---------------------------------------------------------------------------
# 公共参数
# ---------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/atk.yaml）")
    parser.add_argument(
        "--base-dir", default=".", help="工程根目录，用于解析 fixture 相对路径"
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出，便于 CI 消费")


def _load_config(args: argparse.Namespace):
    if args.config:
        # 显式路径不存在时由 load_config 抛错，不静默回落
        return load_config(args.config)
    path = os.path.join(args.base_dir, "config", "atk.yaml")
    return load_config(path if os.path.exists(path) else None)


# ---------------------------------------------------------------------------
# validate / list
# ---------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    load = load_cases(args.paths, config=_load_config(args), strict=False)
    if args.json:
        print(
            json.dumps(
                {
                    "ok": load.ok(),
                    "cases": len(load.cases),
                    "errors": load.errors,
                    "warnings": load.warnings,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for warning in load.warnings:
            print(f"⚠️  {warning}")
        for error in load.errors:
            print(f"❌ {error}")
        print(f"\n共 {len(load.cases)} 条用例，{len(load.errors)} 个错误，{len(load.warnings)} 个告警")
    return EXIT_OK if load.ok() else EXIT_BLOCK


def cmd_list(args: argparse.Namespace) -> int:
    load = load_cases(args.paths, config=_load_config(args), strict=False)
    cases = load.cases
    if args.layer:
        cases = [c for c in cases if c.layer in args.layer]
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "case_id": c.case_id,
                        "title": c.title,
                        "layer": c.layer,
                        "priority": c.priority,
                        "difficulty": c.difficulty,
                        "tags": c.tags,
                        "checks": [ch.type for ch in c.grade.auto_checks],
                    }
                    for c in cases
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_OK

    print(f"{'ID':<18}{'层':<7}{'优先级':<7}{'难度':<6}{'断言数':<6}标题")
    print("-" * 96)
    for case in sorted(cases, key=lambda c: (c.layer, c.case_id)):
        print(
            f"{case.case_id:<18}{case.layer:<7}{case.priority:<7}"
            f"{case.difficulty:<6}{len(case.grade.auto_checks):<6}{case.title}"
        )
    print("-" * 96)
    print(f"共 {len(cases)} 条")
    for error in load.errors:
        print(f"❌ {error}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    options = RunOptions(
        case_paths=args.cases,
        agent=args.agent,
        profile=args.profile,
        samples=args.samples,
        out_dir=args.out,
        run_id=args.run_id,
        config_path=args.config,
        judge=args.judge,
        judge_base_url=args.judge_base_url,
        judge_api_key=args.judge_api_key or os.environ.get("ATK_JUDGE_API_KEY", ""),
        max_workers=args.workers,
        keep_sandboxes=not args.clean,
        base_dir=args.base_dir,
        case_ids=args.case_id or [],
        layers=args.layer or [],
        limit=args.limit,
        include_quarantined=args.include_quarantined,
        auto_quarantine=not args.no_quarantine,
    )

    def progress(done: int, total: int, case_id: str) -> None:
        if args.quiet:
            return
        width = 28
        filled = int(width * done / max(total, 1))
        bar = "█" * filled + "·" * (width - filled)
        print(f"\r[{bar}] {done}/{total}  {case_id:<18}", end="", flush=True)
        if done == total:
            print()

    print(f"▶ 执行档位 {options.profile} ｜ Agent {options.agent} ｜ 输出 {options.out_dir}")
    runner = Runner(options)
    result = runner.run(progress=progress)

    summary = result.summary
    print()
    print(f"运行 ID：{summary.run_id}")
    print(f"用例 {summary.case_count} 条，采样 {sum(c.samples for c in summary.cases.values())} 次，"
          f"耗时 {result.duration_s:.1f}s")
    for warning in result.warnings:
        print(f"⚠️  {warning}")
    print(f"单次通过率：{summary.pass_rate:.1%}（95%CI {summary.pass_ci()[0]:.1%}~{summary.pass_ci()[1]:.1%}）")
    for k in (1, 2, 3):
        value, covered = summary.overall_pass_k(k)
        if covered:
            print(f"  pass^{k} = {value:.1%}（{covered} 条用例参与）")
    for dim in ("safety", "correctness", "completeness", "efficiency"):
        metric = summary.dimension(dim)
        print(f"  {dim:<13}{metric.describe()}")

    # 基线对比 + 门禁 + 报告
    if not args.no_gate:
        baseline = _load_baseline(args.baseline, args.out, result.run_dir)
        config = runner.config
        gate = evaluate_gate(
            summary,
            baseline,
            config,
            profile=runner.profile if args.profile not in ("all", "full") else None,
        )
        print()
        print(f"门禁结论：{gate.summary_line()}")
        for issue in gate.blocks():
            print(f"  ❌ {issue.message}")
        for issue in gate.warnings()[:5]:
            print(f"  ⚠️  {issue.message}")

        if not args.no_report:
            dashboard = os.path.join(args.out, "dashboard.html")
            calibration = _load_calibration(config, args.base_dir)
            reports = generate_reports(
                summary,
                baseline,
                gate,
                out_dir=result.run_dir,
                config=config,
                judge_info=_judge_info(args.judge),
                calibration=calibration,
                extra_notes=[
                    note
                    for note in (
                        f"Agent: {options.agent}",
                        f"门禁模式: {gate.blocking_mode}",
                        f"隔离区用例: {gate.excluded_cases}" if gate.excluded_cases else "",
                    )
                    if note
                ],
                dashboard_path=os.path.relpath(dashboard, result.run_dir),
            )
            print(f"报告：{reports['markdown']}")
            print(f"      {reports['html']}")
            try:
                runs = list_runs(os.path.join(args.out, "runs"))
                build_dashboard(runs, dashboard)
                print(f"看板：{dashboard}")
            except Exception as exc:  # noqa: BLE001 - 看板失败不影响主流程
                print(f"（看板生成失败，不影响结论：{exc}）")

        if args.json:
            print(json.dumps(gate.to_dict(), ensure_ascii=False, indent=2))
        return EXIT_BLOCK if gate.blocked else EXIT_OK

    return EXIT_OK


def _judge_info(spec: str) -> str:
    return {
        "heuristic": "heuristic-v1（离线启发式，仅兜底）",
        "off": "未启用",
    }.get(spec, spec)


def _load_baseline(spec: str | None, out_dir: str, current_dir: str) -> RunSummary | None:
    """解析基线：``auto`` 取**本次之前**最近的一次运行，``none`` 表示不比较。

    注意不能直接取「最近一次」：如果当前运行不是最新的（例如补跑历史版本），
    会拿未来的运行当基线，结论方向就反了。
    """
    if spec in ("none", "no", "off"):
        return None
    runs_root = os.path.join(out_dir, "runs")

    if spec and spec != "auto":
        if os.path.isdir(spec):
            try:
                return load_summary(spec)
            except FileNotFoundError:
                return None
        return None

    runs = list_runs(runs_root)
    dirs = [os.path.abspath(r["dir"]) for r in runs]
    current_abs = os.path.abspath(current_dir)
    if current_abs in dirs:
        earlier = dirs[: dirs.index(current_abs)]
    else:
        earlier = [d for d in dirs if d != current_abs]
    if not earlier:
        return None
    try:
        return load_summary(earlier[-1])
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# gate / report
# ---------------------------------------------------------------------------


def cmd_gate(args: argparse.Namespace) -> int:
    config = _load_config(args)
    current = load_summary(args.run)
    baseline = _load_baseline(args.baseline, os.path.dirname(os.path.dirname(args.run)), args.run)
    gate = evaluate_gate(current, baseline, config, mode=args.mode)

    if args.json:
        print(json.dumps(gate.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"门禁结论：{gate.summary_line()}")
        print(f"当前运行：{current.run_id}" + (f" ｜ 基线：{baseline.run_id}" if baseline else " ｜ 无基线"))
        print()
        for row in gate.dimensions:
            if not row.get("compared"):
                print(f"  {row['dimension']:<14}{row.get('reason', '')}")
                continue
            print(f"  {row['dimension']:<14}{row['describe']}")
        if gate.performance:
            perf = gate.performance
            print(
                f"  P95 时延 {perf.get('latency_p95_current')}ms "
                f"(基线 {perf.get('latency_p95_baseline')})，"
                f"成本 ${perf.get('avg_cost_current')} (基线 ${perf.get('avg_cost_baseline')})"
            )
        print()
        for issue in gate.blocks():
            print(f"❌ [{issue.code}] {issue.message}")
            if issue.case_ids:
                print(f"    用例：{', '.join(issue.case_ids[:8])}")
        for issue in gate.warnings():
            print(f"⚠️  [{issue.code}] {issue.message}")

    if gate.blocked:
        return EXIT_BLOCK
    if gate.warnings() and args.fail_on_warn:
        return EXIT_BLOCK
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    config = _load_config(args)
    current = load_summary(args.run)
    baseline = _load_baseline(args.baseline, os.path.dirname(os.path.dirname(args.run)), args.run)
    gate = evaluate_gate(current, baseline, config, mode=args.mode)

    calibration = _load_calibration(config, args.base_dir)
    out_dir = args.out_dir or args.run
    dashboard = os.path.join(os.path.dirname(os.path.dirname(args.run)), "dashboard.html")
    paths = generate_reports(
        current,
        baseline,
        gate,
        out_dir=out_dir,
        config=config,
        judge_info=args.judge_info,
        calibration=calibration,
        dashboard_path=os.path.relpath(dashboard, out_dir),
    )
    if args.dashboard:
        runs = list_runs(os.path.join(args.base_dir, "reports", "runs"))
        build_dashboard(runs, dashboard)

    print(f"Markdown：{paths['markdown']}")
    print(f"HTML：{paths['html']}")
    print(f"结论：{gate.summary_line()}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# calibrate
# ---------------------------------------------------------------------------


def cmd_calibrate(args: argparse.Namespace) -> int:
    config = _load_config(args)
    calibration_set = load_calibration_set(args.set)
    if not calibration_set.items:
        print(f"❌ 校准集为空：{args.set}", file=sys.stderr)
        return EXIT_ERROR

    problems = calibration_set.check()
    for problem in problems:
        print(f"⚠️  {problem}")

    judge = build_judge(
        args.judge,
        config.judge,
        api_key=args.api_key or os.environ.get("ATK_JUDGE_API_KEY", ""),
        base_url=args.base_url,
    )
    if judge is None:
        print("❌ 需要可用的 Judge（heuristic 或 openai:<model>）", file=sys.stderr)
        return EXIT_ERROR

    report = calibrate(
        judge,
        calibration_set,
        config.judge,
        threshold=args.threshold,
        model_under_test=args.model_under_test,
    )
    json_path, md_path = save_report(report, args.out)

    print(f"Judge：{report.judge_name} / {report.judge_model}")
    print(f"校准集：{report.item_count} 条（红队 {report.adversarial_count} 条）")
    print(f"总体 Kappa：{report.overall_kappa:.3f} ｜ 一致率 {report.accuracy:.1%}")
    for dim, row in report.per_dimension.items():
        print(
            f"  {dim:<14}κ={row['kappa']:.2f}  一致率={row['accuracy']:.1%}  "
            f"过严={row['false_negative']}  过宽={row['false_positive']}"
        )
    print(f"长度偏差 Spearman：{report.length_bias:+.3f}")
    print(f"反向红队被骗率：{report.fooled_rate:.1%}（{report.fooled_count}/{report.adversarial_count}）")
    verdict_text = "✅ 可用于门禁" if report.gate_eligible else "❌ 不得用于门禁"
    if report.ineligible_dims:
        verdict_text += f"（禁用维度：{', '.join(report.ineligible_dims)}）"
    print(f"结论：{verdict_text}")
    if report.ineligible_dims:
        print(
            "  ⚠️ 上述维度的 Judge 分不得用于门禁，一律以规则断言为准"
        )
    for reason in report.reasons:
        print(f"  - {reason}")
    print(f"\n报告：{json_path}\n      {md_path}")

    if args.strict and not report.gate_eligible:
        return EXIT_BLOCK
    return EXIT_OK


def _load_calibration(config, base_dir: str):
    """加载最近的校准报告（报告附录要用）。"""
    import glob

    paths = sorted(
        glob.glob(os.path.join(base_dir, "reports", "calibration_*", "calibration.json"))
    )
    if not paths:
        return None
    from .grading.calibration import CalibrationReport

    try:
        with open(paths[-1], "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    report = CalibrationReport(
        judge_model=data.get("judge_model", ""),
        judge_name=data.get("judge_name", ""),
        set_path=data.get("set_path", ""),
        generated_at=data.get("generated_at", ""),
        item_count=int(data.get("item_count", 0)),
        adversarial_count=int(data.get("adversarial_count", 0)),
        threshold=float(data.get("threshold", 0.6)),
        per_dimension=data.get("per_dimension", {}),
        overall_kappa=float(data.get("overall_kappa", 0)),
        accuracy=float(data.get("accuracy", 0)),
        confusion=data.get("confusion", {}),
        length_bias=float(data.get("length_bias", 0)),
        position_consistency=data.get("position_consistency"),
        first_position_rate=data.get("first_position_rate"),
        self_preference_risk=bool(data.get("self_preference_risk", False)),
        fooled_count=int(data.get("fooled_count", 0)),
        fooled_rate=float(data.get("fooled_rate", 0)),
        gate_eligible=bool(data.get("gate_eligible", False)),
        dimension_eligible=dict(data.get("dimension_eligible", {})),
        ineligible_dims=[str(x) for x in data.get("ineligible_dims", [])],
        reasons=data.get("reasons", []),
        problems=data.get("problems", []),
    )
    return report


# ---------------------------------------------------------------------------
# dashboard / impact / quarantine / selfcheck
# ---------------------------------------------------------------------------


def cmd_dashboard(args: argparse.Namespace) -> int:
    runs = list_runs(args.runs)
    if not runs:
        print(f"没有历史运行记录：{args.runs}", file=sys.stderr)
        return EXIT_ERROR
    path = build_dashboard(runs, args.out, max_points=args.limit)
    print(f"看板已生成：{path}（{len(runs)} 次运行）")
    return EXIT_OK


def cmd_impact(args: argparse.Namespace) -> int:
    config = _load_config(args)
    load = load_cases(args.cases, config=config, strict=False)
    changed = args.changed_files or []
    if args.git_base:
        try:
            changed.extend(git_changed_files(args.git_base, cwd=args.base_dir))
        except ImpactGitError as exc:
            print(f"❌ 影响面分析取不到改动文件：{exc}", file=sys.stderr)
            return EXIT_ERROR
    if not changed:
        if args.git_base:
            print(
                "ℹ️  git 未发现改动文件（工作区干净且与基线一致）；"
                "本次无需按影响面跑批。",
                file=sys.stderr,
            )
            # 仍然写出空结果，CI 才能用 case_ids 长度做条件判断
            if args.write:
                with open(args.write, "w", encoding="utf-8") as fh:
                    json.dump(
                        {
                            "case_ids": [], "reasons": {}, "matched_rules": [],
                            "unmatched_paths": [], "fallback_used": False,
                            "changed_files": [],
                        },
                        fh,
                        ensure_ascii=False,
                        indent=2,
                    )
            return EXIT_OK
        print(
            "❌ 需要 --changed-files 或 --git-base；"
            "也可以直接给文件清单：atk impact --changed-files atk/pipeline/gate.py",
            file=sys.stderr,
        )
        return EXIT_ERROR

    impact_map = ImpactMap.load(os.path.join(args.base_dir, config.impact_map))
    result = analyze(changed, load.cases, impact_map)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"改动文件 {len(changed)} 个，命中规则 {result.matched_rules or '无'}")
        if result.fallback_used:
            print("⚠️  未命中任何影响面规则，已退化到核心层")
        for cid in result.case_ids:
            print(f"  {cid}  ← {result.reasons[cid]}")
        if result.unmatched_paths:
            print(f"⚠️  未覆盖的改动：{result.unmatched_paths[:10]}")
    if args.write:
        with open(args.write, "w", encoding="utf-8") as fh:
            json.dump(result.to_dict(), fh, ensure_ascii=False, indent=2)
        print(f"已写入 {args.write}")
    return EXIT_OK


def cmd_quarantine(args: argparse.Namespace) -> int:
    from .pipeline.flaky import QuarantineStore

    store = QuarantineStore(args.store)
    if args.action == "list":
        if not store.entries:
            print("隔离区为空")
        for entry in store.entries.values():
            print(f"{entry.case_id}  自 {entry.since}  {entry.reason}")
        return EXIT_OK
    if args.action == "add":
        store.add(args.case_id, args.reason or "人工加入隔离区")
        store.save()
        print(f"已隔离 {args.case_id}")
        return EXIT_OK
    if args.action == "remove":
        ok = store.remove(args.case_id, args.reason or "")
        store.save()
        print(f"{'已解除' if ok else '未找到'}：{args.case_id}")
        return EXIT_OK
    if args.action == "review":
        store.review(args.case_id, args.reason or "人工复核")
        store.save()
        print(f"已记录复核：{args.case_id}")
        return EXIT_OK
    return EXIT_ERROR


def cmd_selfcheck(args: argparse.Namespace) -> int:
    from .selfcheck import run_selfcheck

    result = run_selfcheck(
        base_dir=args.base_dir,
        config_path=args.config,
        cases_paths=args.cases,
        calibration_path=args.calibration_set,
    )
    markdown = result.to_markdown()
    print(markdown)
    if args.write:
        path = args.write
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(markdown)
        print(f"\n已写入 {path}")
    if args.json:
        print(result.to_json())
    return EXIT_OK if result.ok else EXIT_BLOCK


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atk",
        description="Agent 产品测试/评估框架（零第三方依赖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"atk {__version__}")
    parser.add_argument("--debug", action="store_true", help="异常时打印完整堆栈")
    sub = parser.add_subparsers(dest="command", required=True)

    # validate
    p = sub.add_parser("validate", help="校验用例（Schema + 语义）")
    p.add_argument("paths", nargs="*", default=["cases/golden"])
    _add_common(p)
    p.set_defaults(func=cmd_validate)

    # list
    p = sub.add_parser("list", help="列出用例")
    p.add_argument("paths", nargs="*", default=["cases/golden"])
    p.add_argument("--layer", action="append", help="按分层过滤（可多次指定）")
    _add_common(p)
    p.set_defaults(func=cmd_list)

    # run
    p = sub.add_parser("run", help="执行跑批")
    p.add_argument("--cases", nargs="+", default=["cases/golden"], help="用例路径")
    p.add_argument("--agent", default="replay:baseline", help="适配器：replay:<variant> / http:<url>")
    p.add_argument("--profile", default="nightly", help="执行档位：pr/smoke/nightly/release/weekly/impact/all")
    p.add_argument("--samples", type=int, default=None, help="覆盖采样次数")
    p.add_argument("--out", default="reports", help="输出根目录")
    p.add_argument("--run-id", default=None)
    p.add_argument("--judge", default="heuristic", help="Judge：heuristic / off / openai:<model>")
    p.add_argument("--judge-base-url", default="", help="OpenAI 兼容接口 base url")
    p.add_argument("--judge-api-key", default="", help="Judge API Key（建议用环境变量 ATK_JUDGE_API_KEY）")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--case-id", action="append", help="只跑指定用例（可多次）")
    p.add_argument("--layer", action="append", help="只跑指定分层（可多次）")
    p.add_argument("--limit", type=int, default=None, help="最多跑多少条（冒烟）")
    p.add_argument("--baseline", default="auto", help="基线：auto / none / 运行目录")
    p.add_argument("--no-gate", action="store_true", help="只跑批，不做门禁")
    p.add_argument("--no-report", action="store_true")
    p.add_argument("--no-quarantine", action="store_true", help="不自动维护隔离区")
    p.add_argument("--include-quarantined", action="store_true")
    p.add_argument("--clean", action="store_true", help="成功后删除沙箱")
    p.add_argument("--quiet", action="store_true")
    _add_common(p)
    p.set_defaults(func=cmd_run)

    # gate
    p = sub.add_parser("gate", help="对已有运行执行门禁判定")
    p.add_argument("--run", required=True, help="运行目录")
    p.add_argument("--baseline", default="auto")
    p.add_argument("--mode", default=None, help="hard / full / report_only")
    p.add_argument("--fail-on-warn", action="store_true", help="告警也返回非零退出码")
    _add_common(p)
    p.set_defaults(func=cmd_gate)

    # report
    p = sub.add_parser("report", help="生成 Markdown/HTML 报告")
    p.add_argument("--run", required=True)
    p.add_argument("--baseline", default="auto")
    p.add_argument("--out-dir", default=None)
    p.add_argument("--mode", default=None)
    p.add_argument("--judge-info", default="heuristic-v1")
    p.add_argument("--dashboard", action="store_true", help="同时刷新趋势看板")
    _add_common(p)
    p.set_defaults(func=cmd_report)

    # calibrate
    p = sub.add_parser("calibrate", help="Judge 校准（Kappa/偏差/红队）")
    p.add_argument("--set", default="cases/calibration")
    p.add_argument("--judge", default="heuristic")
    p.add_argument("--base-url", default="")
    p.add_argument("--api-key", default="")
    p.add_argument("--threshold", type=float, default=0.6)
    p.add_argument("--model-under-test", default="", help="被测模型名，用于自我偏好检查")
    p.add_argument("--out", default="reports")
    p.add_argument("--strict", action="store_true", help="未通过校准时返回非零")
    _add_common(p)
    p.set_defaults(func=cmd_calibrate)

    # dashboard
    p = sub.add_parser("dashboard", help="生成趋势看板")
    p.add_argument("--runs", default="reports/runs")
    p.add_argument("--out", default="reports/dashboard.html")
    p.add_argument("--limit", type=int, default=12)
    _add_common(p)
    p.set_defaults(func=cmd_dashboard)

    # impact
    p = sub.add_parser("impact", help="影响面分析")
    p.add_argument("--changed-files", nargs="*", default=[])
    p.add_argument("--git-base", default=None, help="如 HEAD~1")
    p.add_argument("--cases", nargs="+", default=["cases/golden"])
    p.add_argument("--write", default=None, help="把结果写到 JSON")
    _add_common(p)
    p.set_defaults(func=cmd_impact)

    # quarantine
    p = sub.add_parser("quarantine", help="Flaky 隔离区管理")
    p.add_argument("action", choices=["list", "add", "remove", "review"])
    p.add_argument("--case-id", default="")
    p.add_argument("--reason", default="")
    p.add_argument("--store", default="reports/quarantine.json")
    _add_common(p)
    p.set_defaults(func=cmd_quarantine)

    # selfcheck
    p = sub.add_parser("selfcheck", help="框架完备性自检（附录 A）")
    p.add_argument("--cases", nargs="+", default=["cases/golden"])
    p.add_argument("--calibration-set", default="cases/calibration")
    p.add_argument("--write", default=None, help="把报告写到文件")
    _add_common(p)
    p.set_defaults(func=cmd_selfcheck)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Windows 控制台默认编码可能不是 UTF-8，统一一下避免中文报错
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except AtkError as exc:
        print(f"\n❌ {exc}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - CLI 最外层兜底
        print(f"\n❌ 未处理异常: {type(exc).__name__}: {exc}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
