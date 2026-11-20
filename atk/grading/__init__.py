"""打分体系：规则断言 → Judge → 校准。

三者分工（规范 5.1）：

- :mod:`atk.grading.rules`：规则/状态断言，最可靠最便宜；
- :mod:`atk.grading.judge`：LLM Judge，只判语义、必须盲评、必须带 evidence；
- :mod:`atk.grading.calibration`：Judge 的可信度证据（Kappa/偏差/反向红队）。
"""

from __future__ import annotations

from .calibration import (
    CalibrationItem,
    CalibrationReport,
    CalibrationSet,
    PairItem,
    calibrate,
    load_calibration_set,
    save_report,
)
from .judge import (
    HeuristicJudge,
    JudgeClient,
    JudgeOutput,
    JudgeRequest,
    JudgeScore,
    OpenAICompatJudge,
    build_blind_request,
    build_judge,
    merge_judge_scores,
    verify_blind,
)
from .rules import Achievement, GradeOutcome, RuleGrader, grade_case

__all__ = [
    "Achievement",
    "GradeOutcome",
    "RuleGrader",
    "grade_case",
    "HeuristicJudge",
    "JudgeClient",
    "JudgeOutput",
    "JudgeRequest",
    "JudgeScore",
    "OpenAICompatJudge",
    "build_blind_request",
    "build_judge",
    "merge_judge_scores",
    "verify_blind",
    "CalibrationItem",
    "CalibrationReport",
    "CalibrationSet",
    "PairItem",
    "calibrate",
    "load_calibration_set",
    "save_report",
]
