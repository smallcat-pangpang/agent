"""运行记录的落盘与读取。

目录结构（一次运行一个目录，天然可归档、可对比）::

    reports/runs/<run_id>/
      ├─ records.jsonl     # 每条采样一行（已脱敏）
      ├─ traces/           # 逐条采样的人可读 trace（已脱敏）
      ├─ summary.json      # 聚合指标（门禁与报告的数据源）
      ├─ index.json        # 运行元信息：参数、用例清单、耗时、脱敏统计
      └─ report.md/html    # 由 report 模块生成

用 JSONL 而不是一个巨大的 JSON：追加写、单条坏行不影响整体、
``jq/grep`` 直接可查。这是评估系统能被长期维护的细节之一。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from ..metrics.aggregate import RunSummary
from ..model import CaseRun
from .redact import Redactor


@dataclass
class RunStore:
    """一次运行的存储句柄。"""

    run_dir: str
    redactor: Redactor | None = None
    _records_written: int = 0

    @property
    def records_path(self) -> str:
        return os.path.join(self.run_dir, "records.jsonl")

    @property
    def summary_path(self) -> str:
        return os.path.join(self.run_dir, "summary.json")

    @property
    def index_path(self) -> str:
        return os.path.join(self.run_dir, "index.json")

    @property
    def trace_dir(self) -> str:
        return os.path.join(self.run_dir, "traces")

    # -- 写入 -------------------------------------------------------------

    def write_record(self, run: CaseRun) -> None:
        """追加一条采样记录（脱敏后写入）。"""
        os.makedirs(self.run_dir, exist_ok=True)
        payload = run.to_dict()
        if self.redactor is not None:
            payload = self.redactor.redact_obj(payload)
        with open(self.records_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._records_written += 1

        # 同时写一份人可读 trace，方便直接打开排查
        os.makedirs(self.trace_dir, exist_ok=True)
        trace_path = os.path.join(self.trace_dir, f"{run.case_id}_s{run.sample_idx}.json")
        with open(trace_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    def write_summary(self, summary: RunSummary) -> None:
        os.makedirs(self.run_dir, exist_ok=True)
        with open(self.summary_path, "w", encoding="utf-8") as fh:
            json.dump(summary.to_dict(), fh, ensure_ascii=False, indent=2)

    def write_index(self, index: dict[str, Any]) -> None:
        os.makedirs(self.run_dir, exist_ok=True)
        payload = index
        if self.redactor is not None:
            payload = self.redactor.redact_obj(dict(index))
            # 参数里的 key 会被当敏感字段整值替换，这里恢复为占位符
            if "options" in payload and isinstance(payload["options"], dict):
                payload["options"].pop("judge_api_key", None)
        with open(self.index_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    def write_text(self, name: str, content: str) -> str:
        os.makedirs(self.run_dir, exist_ok=True)
        path = os.path.join(self.run_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def load_records(run_dir: str) -> list[CaseRun]:
    """读取一次运行的全部采样记录。"""
    path = os.path.join(run_dir, "records.jsonl")
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到运行记录: {path}")
    records: list[CaseRun] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(CaseRun.from_dict(json.loads(line)))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno} 不是合法 JSON: {exc}") from exc
    return records


def load_summary(run_dir: str) -> RunSummary:
    """读取聚合结果。"""
    path = os.path.join(run_dir, "summary.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到运行汇总: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return RunSummary.from_dict(json.load(fh))


def load_index(run_dir: str) -> dict[str, Any]:
    path = os.path.join(run_dir, "index.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def list_runs(root: str) -> list[dict[str, Any]]:
    """列出历史运行（按时间正序），用于趋势看板与基线选择。"""
    runs: list[dict[str, Any]] = []
    if not os.path.isdir(root):
        return runs
    for name in sorted(os.listdir(root)):
        run_dir = os.path.join(root, name)
        if not os.path.isdir(run_dir):
            continue
        summary_path = os.path.join(run_dir, "summary.json")
        if not os.path.exists(summary_path):
            continue
        index = load_index(run_dir)
        try:
            with open(summary_path, "r", encoding="utf-8") as fh:
                summary = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        runs.append(
            {
                "run_id": summary.get("run_id", name),
                "dir": run_dir,
                "generated_at": summary.get("generated_at", ""),
                "profile": summary.get("profile", ""),
                "agent": summary.get("agent", ""),
                "version_quad": summary.get("version_quad", {}),
                "pass_rate": summary.get("pass_rate", 0.0),
                "dimensions": {
                    k: v.get("pass_rate", 0.0)
                    for k, v in (summary.get("dimensions") or {}).items()
                },
                "latency_p95_ms": summary.get("latency_p95_ms", 0),
                "avg_cost_usd": summary.get("avg_cost_usd", 0),
                "case_count": summary.get("case_count", 0),
                "index": index,
            }
        )
    return runs


def find_latest_run(root: str, exclude: str | None = None) -> str | None:
    """找最近一次运行目录（用作默认基线）。"""
    candidates = [r for r in list_runs(root) if r["dir"] != exclude]
    if not candidates:
        return None
    return candidates[-1]["dir"]
