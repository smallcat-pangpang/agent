#!/usr/bin/env python3
"""在 push 之前，本地等价复现 ``.github/workflows/agent-eval.yml`` 的六个 job。

设计目的
--------
P0-1 修的是「CI 假绿」。修完之后如果从没在真实 runner 上跑过，这个闭环仍不算关上。
本脚本把 workflow 里每个 ``run:`` 步骤逐条在本地执行一遍，用同一套退出码口径
判定 job 会不会红，让「推送前」与「推送后」可以对照。

与 workflow 的对应关系（本地化替换，仅此三处）：

===========================  ==================================
workflow 原文                本地等价
===========================  ==================================
``atk ...``                  ``python -m atk ...``
                             （Windows 本地 pip 脚本目录不在 PATH；
                             Ubuntu runner 上两者等价）
``--out reports``            ``--out <临时目录>``
                             （避免污染仓库 reports/）
``${{ github.sha }}`` 等      ``pr-localsha`` / ``*-local``
===========================  ==================================

命令表与 workflow 的 job/step 名一一对应；修改 workflow 时请同步本文件。

用法::

    python tools/ci_local_check.py                 # 六 job 全量 + 负向断言
    python tools/ci_local_check.py --out DIR      # 指定输出目录（便于留存）
    python tools/ci_local_check.py --no-negative # 只跑正向

退出码：全部 PASS 返回 0；任一 job 失败返回 1。
``coverage`` 未安装时对应步骤标 SKIP（CI 里会 ``pip install coverage``）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
TIMEOUT_S = 300

# workflow 里被替换成本地固定值的 GitHub 表达式
SHA = "pr-localsha"


def _atk(*args: str) -> list[str]:
    return [PY, "-m", "atk", *args]


def _py(*args: str) -> list[str]:
    return [PY, *args]


class Recorder:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, int | None, str]] = []

    def run(self, job: str, step: str, cmd: list[str]) -> tuple[int | None, str]:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=TIMEOUT_S,
            )
            rc: int | None = proc.returncode
            output = (proc.stdout or "") + (proc.stderr or "")
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            rc, output = 1, f"命令无法执行：{exc}"
        self.rows.append((job, step, rc, output))
        return rc, output

    def skip(self, job: str, step: str, why: str) -> None:
        self.rows.append((job, step, None, why))

    def print_table(self) -> bool:
        print("\n" + "=" * 78)
        print(f"{'JOB':<16}{'STEP':<34}{'结果':<8}说明")
        print("-" * 78)
        ok = True
        for job, step, rc, output in self.rows:
            if rc is None:
                mark, note = "SKIP", output[:40]
            elif rc == 0:
                mark, note = "PASS", ""
            else:
                mark, note = "FAIL", f"exit={rc}"
                ok = False
                tail = [ln.strip() for ln in output.strip().splitlines() if ln.strip()]
                if tail:
                    note += "  « " + tail[-1][:60]
            print(f"{job:<16}{step:<34}{mark:<8}{note}")
        print("-" * 78)
        passed = sum(1 for _, _, rc, _ in self.rows if rc == 0)
        failed = sum(1 for _, _, rc, _ in self.rows if rc not in (0, None))
        skipped = sum(1 for _, _, rc, _ in self.rows if rc is None)
        print(f"合计：{passed} PASS / {failed} FAIL / {skipped} SKIP")
        return ok


def build_and_run(rec: Recorder, out: Path, agent: str) -> None:
    o = str(out)
    runs = str(out / "runs")

    # ---------------------------------------------------------------- sanity
    rec.run("sanity", "atk validate cases/golden", _atk("validate", "cases/golden"))
    rec.run("sanity", "unittest discover", _py("-m", "unittest", "discover", "-s", "tests", "-t", "."))
    rec.run(
        "sanity",
        "atk selfcheck --write",
        _atk("selfcheck", "--write", str(out / "selfcheck.md")),
    )
    rec.run("sanity", "static_check --strict", _py("tools/static_check.py", "--strict"))
    if importlib.util.find_spec("coverage") is None:
        rec.skip("sanity", "coverage --fail-under=65", "本地未装 coverage（CI 会 pip install）")
    else:
        rec.run(
            "sanity",
            "coverage run",
            _py("-m", "coverage", "run", "--source=atk", "-m", "unittest", "discover", "-s", "tests", "-t", "."),
        )
        rec.run("sanity", "coverage report", _py("-m", "coverage", "report", "--fail-under=65"))

    # --------------------------------------------------------------- pr-gate
    # 与 workflow 一致：run 不带 --no-gate（内部会判一次并生成报告），
    # 显式 gate 必须带 --mode hard，否则 CLI 默认 full 与 pr 档 blocking=hard 冲突。
    rec.run(
        "pr-gate",
        "atk run --profile pr",
        _atk(
            "run", "--profile", "pr", "--agent", agent,
            "--cases", "cases/golden", "--out", o, "--judge", "heuristic",
            "--workers", "4", "--run-id", SHA,
        ),
    )
    rec.run(
        "pr-gate",
        "atk gate --mode hard",
        _atk("gate", "--run", os.path.join(runs, SHA), "--baseline", "auto", "--mode", "hard"),
    )

    # ---------------------------------------------------------------- nightly
    rec.run(
        "nightly",
        "atk run --profile nightly",
        _atk(
            "run", "--profile", "nightly", "--agent", agent, "--samples", "3",
            "--cases", "cases/golden", "--out", o, "--judge", "heuristic",
            "--quiet", "--run-id", "nightly-local",
        ),
    )
    rec.run(
        "nightly",
        "atk gate --mode hard",
        _atk("gate", "--run", os.path.join(runs, "nightly-local"), "--baseline", "auto", "--mode", "hard"),
    )
    rec.run(
        "nightly",
        "atk dashboard",
        _atk("dashboard", "--runs", runs, "--out", str(out / "dashboard.html")),
    )

    # ----------------------------------------------------------- release-gate
    rec.run(
        "release-gate",
        "atk run --profile release",
        _atk(
            "run", "--profile", "release", "--agent", agent,
            "--cases", "cases/golden", "--out", o, "--judge", "heuristic",
            "--quiet", "--run-id", "release-local",
        ),
    )
    rec.run(
        "release-gate",
        "atk gate --mode full",
        _atk("gate", "--run", os.path.join(runs, "release-local"), "--baseline", "auto", "--mode", "full"),
    )

    # ---------------------------------------------------------------- weekly
    rec.run(
        "weekly",
        "atk calibrate",
        _atk("calibrate", "--judge", "heuristic", "--set", "cases/calibration", "--out", o),
    )
    rec.run(
        "weekly",
        "atk run --no-gate",
        _atk(
            "run", "--profile", "weekly", "--agent", agent, "--samples", "3",
            "--cases", "cases/golden", "--out", o, "--judge", "heuristic",
            "--quiet", "--no-gate", "--run-id", "weekly-local",
        ),
    )
    rec.run(
        "weekly",
        "atk report --dashboard",
        _atk("report", "--run", os.path.join(runs, "weekly-local"), "--baseline", "auto", "--dashboard"),
    )

    # ----------------------------------------------------------- impact-gate
    impact_json = str(out / "impact.json")
    rc, output = rec.run(
        "impact-gate",
        "atk impact --write",
        _atk("impact", "--git-base", "HEAD~1", "--cases", "cases/golden", "--write", impact_json),
    )
    case_args: list[str] = []
    if rc == 0 and os.path.exists(impact_json):
        with open(impact_json, encoding="utf-8") as fh:
            case_args = [x for cid in json.load(fh).get("case_ids", []) for x in ("--case-id", cid)]
    if case_args:
        rec.run(
            "impact-gate",
            "atk run --profile impact",
            _atk(
                "run", "--profile", "impact", "--agent", agent,
                "--cases", "cases/golden", *case_args,
                "--out", o, "--judge", "heuristic", "--run-id", "impact-local",
            ),
        )
        rec.run(
            "impact-gate",
            "atk gate --mode full",
            _atk("gate", "--run", os.path.join(runs, "impact-local"), "--baseline", "auto", "--mode", "full"),
        )
    else:
        rec.skip("impact-gate", "跑受影响用例 + 门禁", "impact.json 无选中用例（workflow 里同样跳过）")


def check_workflow(rec: Recorder) -> None:
    """静态校验 workflow 本身：触发覆盖、P0-1 残留、门禁口径对齐。

    这几项不需要 runner，但恰恰是最容易改坏、又最容易「看起来绿」的地方。
    """
    sys.path.insert(0, str(ROOT))
    from atk.yamlite import loads

    path = ROOT / ".github" / "workflows" / "agent-eval.yml"
    txt = path.read_text(encoding="utf-8")
    jobs = loads(txt).get("jobs", {})

    rec.rows.append((
        "workflow", "YAML 可解析且 6 个 job",
        0 if len(jobs) == 6 else 1, f"jobs={list(jobs)}",
    ))
    # workflow_dispatch 下拉的 5 个档位必须各有 job 命中，
    # 否则「选了档位却只有 sanity 跑」= 又一种静默假绿（P0-1 同型）
    for profile in ("pr", "nightly", "release", "weekly", "impact"):
        hit = any(f"profile == '{profile}'" in str(j.get("if", "")) for j in jobs.values())
        rec.rows.append((
            "workflow", f"dispatch:{profile} 有 job",
            0 if hit else 1, "" if hit else "该档位手动触发不会跑任何门禁",
        ))

    live = [ln for ln in txt.splitlines() if not ln.strip().startswith("#")]
    residue = [ln.strip() for ln in live if re.search(r"\|\|\s*true|--max-workers|ls -dt", ln)]
    rec.rows.append((
        "workflow", "无 || true / --max-workers / ls -dt",
        0 if not residue else 1, residue[0][:50] if residue else "",
    ))

    pr_steps = str(jobs.get("pr-gate", {}).get("steps", []))
    aligned = "--mode hard" in pr_steps
    rec.rows.append((
        "workflow", "pr-gate 判定口径对齐 hard",
        0 if aligned else 1, "" if aligned else "显式 gate 缺少 --mode hard，会按 full 变严",
    ))


def run_negative(rec: Recorder, neg_out: Path) -> None:
    """证明「该红会红」：无历史基线 + candidate，PR 门禁必须返回非 0。

    这是 P0-1 是否真正关闭的核心证据。用一个全新输出目录，
    确保 ``--baseline auto`` 找不到任何历史运行（模拟首次 PR / cache 未命中）。
    这里不复用 ``Recorder.run``：该步骤**预期返回非 0**，不能记成 FAIL。
    """
    cmd = _atk(
        "run", "--profile", "pr", "--agent", "replay:candidate",
        "--cases", "cases/golden", "--out", str(neg_out), "--judge", "heuristic",
        "--workers", "4", "--run-id", "pr-negative", "--quiet",
    )
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TIMEOUT_S,
        )
        rc, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        rc, output = 1, f"命令无法执行：{exc}"

    # run 内部打印的是中文 issue message（不带 code），code 只在 `atk gate` 输出里
    blocked = rc != 0 and "BLOCK" in output
    hard_evidence = any(k in output for k in ("安全用例失败", "命中禁止行为", "存在硬失败"))
    passed = blocked and hard_evidence
    rec.rows.append((
        "pr-gate(负向)",
        "无基线 + candidate → 变红",
        0 if passed else 1,
        f"run_exit={rc}，BLOCK={'BLOCK' in output}，硬失败证据={hard_evidence}",
    ))


def main() -> int:
    ap = argparse.ArgumentParser(description="本地复现 GitHub Actions 六个 job 的命令链")
    ap.add_argument("--out", default="", help="输出目录（默认临时目录）")
    ap.add_argument("--agent", default="replay:baseline", help="正向 job 使用的 Agent")
    ap.add_argument("--no-negative", action="store_true", help="跳过负向断言")
    ap.add_argument("--static-only", action="store_true",
                    help="只跑 workflow 静态校验（供 CI 的 sanity job 调用，不执行跑批）")
    args = ap.parse_args()

    rec = Recorder()
    check_workflow(rec)
    if not args.static_only:
        out = Path(args.out).resolve() if args.out else Path(tempfile.mkdtemp(prefix="atk_ci_"))
        out.mkdir(parents=True, exist_ok=True)
        neg_out = out / "negative"
        neg_out.mkdir(parents=True, exist_ok=True)
        print(f"复现 workflow 六 job ｜ 输出目录：{out}")
        print("等价替换：atk→python -m atk，--out reports→临时目录，${{ github.* }}→本地固定值")
        build_and_run(rec, out, args.agent)
        if not args.no_negative:
            run_negative(rec, neg_out)

    ok = rec.print_table()
    if not ok:
        print("\n❌ 存在 FAIL：这些命令在 GitHub Actions 上同样会让 job 变红，请先修复。", file=sys.stderr)
    elif args.static_only:
        print("\n✅ workflow 静态校验通过（触发覆盖 / P0-1 残留 / 门禁口径）。")
    else:
        print("\n✅ 六 job 命令链全部通过；负向断言确认「无基线 + 硬失败 → 变红」。")
        print("   注意：这仍是本地等价验证，真实 runner（Ubuntu / Python 3.11 / 从零 clone）"
              "需在 push 后再确认一次。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
