"""Judge 校准流程测试（用离线启发式 Judge，不依赖网络）。"""

from __future__ import annotations

import os
import shutil
import unittest

from atk.config import JudgeConfig
from atk.grading.calibration import calibrate, load_calibration_set, save_report
from atk.grading.judge import (
    HeuristicJudge,
    build_blind_request,
    verify_blind,
)
from atk.loader import load_cases

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, ".atk-tmp")


class TestCalibrationSet(unittest.TestCase):
    def test_set_shape(self):
        calibration = load_calibration_set(os.path.join(ROOT, "cases", "calibration"))
        self.assertGreaterEqual(len(calibration.items), 20)
        self.assertGreaterEqual(calibration.adversarial_count, 5)
        self.assertTrue(calibration.pairs)
        self.assertEqual(calibration.check(), [])

    def test_calibrate_runs(self):
        calibration = load_calibration_set(os.path.join(ROOT, "cases", "calibration"))
        report = calibrate(HeuristicJudge(), calibration, JudgeConfig())
        self.assertGreaterEqual(report.overall_kappa, -1.0)
        self.assertLessEqual(report.overall_kappa, 1.0)
        self.assertIn("correctness", report.per_dimension)
        self.assertGreaterEqual(report.adversarial_count, 5)
        self.assertLessEqual(report.fooled_rate, 1.0)
        # 离线判官不应被简单的「编造数字」骗过
        self.assertLess(report.fooled_rate, 0.5)

    def test_weak_dimension_is_marked_ineligible(self):
        """P0-2：整体 Kappa 合格不能掩盖单维度不可信。"""
        calibration = load_calibration_set(os.path.join(ROOT, "cases", "calibration"))
        report = calibrate(HeuristicJudge(), calibration, JudgeConfig())
        safety_kappa = report.per_dimension["safety"]["kappa"]
        self.assertLess(safety_kappa, 0.6)
        self.assertFalse(report.dimension_eligible["safety"])
        self.assertIn("safety", report.ineligible_dims)
        self.assertIn("禁用维度", report.to_markdown())
        self.assertIn("dimension_eligible", report.to_dict())
        # 报告必须点名，避免「✅ 可用于门禁」被单独引用
        self.assertTrue(report.gate_eligible)

    def test_save_report(self):
        calibration = load_calibration_set(os.path.join(ROOT, "cases", "calibration"))
        report = calibrate(HeuristicJudge(), calibration, JudgeConfig())
        out = os.path.join(TMP, "calib-out")
        shutil.rmtree(out, ignore_errors=True)
        os.makedirs(out, exist_ok=True)
        try:
            json_path, md_path = save_report(report, out)
            self.assertTrue(os.path.exists(json_path))
            with open(md_path, "r", encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("Judge 校准报告", content)
            self.assertIn("Kappa", content)
        finally:
            shutil.rmtree(out, ignore_errors=True)


class TestBlindJudge(unittest.TestCase):
    def test_request_never_contains_expect(self):
        load = load_cases([os.path.join(ROOT, "cases", "golden")], strict=False)
        case = next(c for c in load.cases if c.case_id == "AGT-ORD-0031")
        # 用一条最小运行记录构造请求
        from atk.model import CaseRun, RunMetrics, Trajectory, VersionQuad, Verdict

        run = CaseRun(
            run_id="r",
            case_id=case.case_id,
            sample_idx=1,
            version_quad=VersionQuad(),
            trajectory=Trajectory(steps=[], final_output="已完成"),
            invocations=[],
            metrics=RunMetrics(),
            verdict=Verdict(),
        )
        request = build_blind_request(case, run, ["correctness"])
        self.assertEqual(verify_blind(request, case), [])
        payload = str(request.to_payload())
        for item in case.expect.must_achieve:
            self.assertNotIn(item.text, payload)
        for item in case.expect.forbidden:
            self.assertNotIn(item.text, payload)


if __name__ == "__main__":
    unittest.main()
