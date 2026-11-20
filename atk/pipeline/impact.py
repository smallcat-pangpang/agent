"""影响面分析（Change Impact Analysis）。

规范 7.2 把「影响面分析」列为必需项：改一个工具 Schema 就全量重跑，
单次 4 小时，没人愿意等 → 门禁必然被绕过。正确做法是：

    改动文件 → 规则匹配 → 受影响用例集 → 只跑这些

规则文件（``config/impact_map.yaml``）::

    rules:
      - name: 工具 Schema 变更
        paths: ["tools/im_send*", "atk/envs/tool_mock.py"]
        layers: ["L2", "L2.5"]
        tags: ["IM", "多工具"]
      - name: 文档摘要 Prompt 变更
        paths: ["prompts/doc_summary*"]
        tags: ["文档"]
    fallback_layers: ["L2", "L3"]
    max_cases: 200

匹配不到任何规则时，退化为「核心层 + 依赖该改动目录的用例」，
并在输出里明确标注 ``unmatched``，避免「静默不跑」。
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..model import TestCase
from ..yamlite import load_file


@dataclass
class ImpactRule:
    """一条影响面规则。"""

    name: str
    paths: list[str] = field(default_factory=list)
    layers: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    priorities: list[str] = field(default_factory=list)
    case_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImpactRule":
        return cls(
            name=str(data.get("name", "") or data.get("id", "")),
            paths=[str(x) for x in data.get("paths", []) or []],
            layers=[str(x) for x in data.get("layers", []) or []],
            tags=[str(x) for x in data.get("tags", []) or []],
            priorities=[str(x) for x in data.get("priorities", []) or []],
            case_ids=[str(x) for x in data.get("case_ids", []) or []],
        )

    def match_path(self, changed: str) -> bool:
        norm = changed.replace("\\", "/")
        return any(
            fnmatch.fnmatch(norm, pattern) or fnmatch.fnmatch(os.path.basename(norm), pattern)
            for pattern in self.paths
        )

    def match_case(self, case: TestCase) -> bool:
        if self.case_ids and case.case_id in self.case_ids:
            return True
        if self.layers and case.layer not in self.layers:
            return False
        if self.priorities and case.priority not in self.priorities:
            return False
        if self.tags and not (set(self.tags) & set(case.tags)):
            return False
        # 规则里只写了 paths 时，匹配到路径即认为该用例受影响（由调用方决定范围）
        return bool(self.layers or self.tags or self.priorities or self.case_ids)


@dataclass
class ImpactMap:
    rules: list[ImpactRule] = field(default_factory=list)
    fallback_layers: list[str] = field(default_factory=lambda: ["L2", "L3"])
    max_cases: int = 200

    @classmethod
    def load(cls, path: str) -> "ImpactMap":
        if not os.path.exists(path):
            return cls()
        data = load_file(path) or {}
        return cls(
            rules=[ImpactRule.from_dict(r) for r in data.get("rules", []) or []],
            fallback_layers=[str(x) for x in data.get("fallback_layers", ["L2", "L3"])],
            max_cases=int(data.get("max_cases", 200)),
        )


@dataclass
class ImpactResult:
    """影响面分析结果。"""

    case_ids: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    matched_rules: list[str] = field(default_factory=list)
    unmatched_paths: list[str] = field(default_factory=list)
    fallback_used: bool = False
    changed_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_ids": self.case_ids,
            "reasons": self.reasons,
            "matched_rules": self.matched_rules,
            "unmatched_paths": self.unmatched_paths,
            "fallback_used": self.fallback_used,
            "changed_files": self.changed_files,
        }


def analyze(
    changed_files: Iterable[str],
    cases: Sequence[TestCase],
    impact_map: ImpactMap,
) -> ImpactResult:
    """把改动文件映射到用例集。"""
    changed = [c.replace("\\", "/") for c in changed_files]
    result = ImpactResult(changed_files=changed)
    selected: dict[str, str] = {}

    for rule in impact_map.rules:
        hit_files = [f for f in changed if rule.match_path(f)]
        if not hit_files:
            continue
        matched_any = False
        for case in cases:
            if rule.match_case(case):
                selected.setdefault(
                    case.case_id,
                    f"规则「{rule.name}」命中改动 {hit_files[0]}",
                )
                matched_any = True
        if matched_any:
            result.matched_rules.append(rule.name)
        else:
            # 规则命中但没圈到用例，说明规则写得太窄，必须让人看到
            result.unmatched_paths.extend(hit_files)

    # 没有任何规则命中 → 退化到核心层，并明确标注
    if not selected:
        result.fallback_used = True
        for case in cases:
            if case.layer in impact_map.fallback_layers:
                selected[case.case_id] = (
                    f"未匹配到影响面规则，退化到核心层 {impact_map.fallback_layers}"
                )

    # 未匹配的改动文件单独提示（可能是新增目录/规则遗漏）
    covered = [f for r in impact_map.rules for f in changed if r.match_path(f)]
    result.unmatched_paths.extend([f for f in changed if f not in covered])

    ordered = sorted(selected)
    if len(ordered) > impact_map.max_cases:
        ordered = ordered[: impact_map.max_cases]
    result.case_ids = ordered
    result.reasons = {cid: selected[cid] for cid in ordered}
    result.unmatched_paths = sorted(set(result.unmatched_paths))
    return result


class ImpactGitError(RuntimeError):
    """git 侧的前提不满足（不是仓库 / 没有基线提交），调用方应给出可执行提示。"""


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=30, check=False
    )


def git_changed_files(
    base: str = "HEAD~1", cwd: str = ".", include_untracked: bool = True
) -> list[str]:
    """用 git 取改动文件列表（也可在 CI 里直接传文件清单）。

    Raises:
        ImpactGitError: 不在 git 仓库里，或仓库还没有任何提交。
            以前这两种情况都会静默返回空列表，CLI 再误报成「需要 --git-base」，
            完全看不出真正的原因。
    """
    try:
        probe = _git(["git", "rev-parse", "--is-inside-work-tree"], cwd)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ImpactGitError(
            "无法执行 git 命令（未安装或不可执行）；请改用 --changed-files 传入文件清单"
        ) from exc
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        raise ImpactGitError(
            f"{cwd!r} 不是 git 仓库：先在项目根目录执行 `git init` 并做一次提交，"
            "或改用 --changed-files 传入文件清单"
        )

    try:
        head = _git(["git", "rev-parse", "--verify", "HEAD"], cwd)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ImpactGitError("无法读取 HEAD，请确认仓库状态") from exc
    if head.returncode != 0:
        raise ImpactGitError("仓库还没有任何提交：先 `git add . && git commit -m init`")

    files: list[str] = []
    # 首次提交时 HEAD~1 不存在，退化成「已跟踪的全部文件」，避免静默不跑
    base_ok = _git(["git", "rev-parse", "--verify", base], cwd).returncode == 0
    commands = [
        ["git", "diff", "--name-only", base, "HEAD"] if base_ok
        else ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        ["git", "diff", "--name-only", "HEAD"],
    ]
    for cmd in commands:
        try:
            output = _git(cmd, cwd)
        except (OSError, subprocess.SubprocessError):
            continue
        if output.returncode != 0:
            continue
        files.extend(line.strip() for line in output.stdout.splitlines() if line.strip())

    if include_untracked:
        try:
            output = _git(["git", "ls-files", "--others", "--exclude-standard"], cwd)
            if output.returncode == 0:
                files.extend(
                    line.strip() for line in output.stdout.splitlines() if line.strip()
                )
        except (OSError, subprocess.SubprocessError):
            pass

    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique
