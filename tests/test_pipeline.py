"""端到端流水线测试：跑批 → 落盘 → 聚合 → 门禁 → 报告。

用 replay 适配器（无需网络）跑一条真实用例，验证：
- 记录、汇总、index 都被正确写出；
- 版本四元组、环境指纹、产物清单齐全；
- 门禁能对「自己和自己」判 PASS；
- 报告包含五段结构与置信区间。
"""

from __future__ import annotations

import os
import shutil
import unittest

from atk.config import load_config
from atk.metrics.aggregate import aggregate_runs
from atk.pipeline.attribution import classify, detect_loop
from atk.pipeline.gate import evaluate_gate
from atk.pipeline.runner import RunOptions, Runner
from atk.report import build_html, build_markdown, build_dashboard, generate_reports
from atk.trace.store import load_records, load_summary, list_runs

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(ROOT, ".atk-tmp")


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shutil.rmtree(os.path.join(TMP, "reports-pipeline"), ignore_errors=True)
        cls.out = os.path.join(TMP, "reports-pipeline")
        options = RunOptions(
            case_paths=[os.path.join(ROOT, "cases", "golden")],
            agent="replay:baseline",
            profile="all",
            samples=1,
            out_dir=cls.out,
            judge="off",
            keep_sandboxes=False,
            base_dir=ROOT,
            case_ids=["AGT-ORD-0031"],
            max_workers=1,
        )
        cls.result = Runner(options).run()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def test_records_and_summary_written(self):
        self.assertTrue(os.path.exists(self.result.run_dir))
        self.assertTrue(os.path.exists(os.path.join(self.result.run_dir, "records.jsonl")))
        self.assertTrue(os.path.exists(os.path.join(self.result.run_dir, "summary.json")))
        self.assertTrue(os.path.exists(os.path.join(self.result.run_dir, "index.json")))

    def test_record_has_version_quad_and_fingerprint(self):
        records = load_records(self.result.run_dir)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.case_id, "AGT-ORD-0031")
        self.assertTrue(record.version_quad.model)
        self.assertTrue(record.version_quad.dataset)
        self.assertIn("tools", record.env_fingerprint)
        self.assertIn("orders_export.xlsx", record.artifacts)

    def test_summary_metrics(self):
        summary = load_summary(self.result.run_dir)
        self.assertEqual(summary.case_count, 1)
        self.assertEqual(summary.pass_rate, 1.0)
        self.assertGreater(summary.dimension("safety").total_samples, 0)
        self.assertGreater(summary.dimension("completeness").total_samples, 0)

    def test_gate_self_compare_passes(self):
        summary = load_summary(self.result.run_dir)
        gate = evaluate_gate(summary, summary, load_config(os.path.join(ROOT, "config", "atk.yaml")))
        self.assertEqual(gate.decision, "PASS")

    def test_report_and_dashboard(self):
        summary = load_summary(self.result.run_dir)
        gate = evaluate_gate(summary, None, load_config())
        md = build_markdown(summary, None, gate, config=load_config())
        self.assertIn("## 一、概览", md)
        self.assertIn("## 二、新增退化", md)
        self.assertIn("## 三、新增修复", md)
        self.assertIn("## 四、归因分布", md)
        self.assertIn("95%CI", md)

        html_text = build_html(md, gate.decision)
        self.assertIn("<table>", html_text)
        self.assertIn("BLOCK" if gate.blocked else "PASS", html_text)

        dashboard = os.path.join(TMP, "dashboard-test.html")
        build_dashboard(list_runs(os.path.join(self.out, "runs")), dashboard)
        self.assertTrue(os.path.exists(dashboard))
        with open(dashboard, "r", encoding="utf-8") as fh:
            self.assertIn("<svg", fh.read())

    def test_generate_reports_files(self):
        summary = load_summary(self.result.run_dir)
        gate = evaluate_gate(summary, None, load_config())
        paths = generate_reports(summary, None, gate, out_dir=self.result.run_dir)
        self.assertTrue(os.path.exists(paths["markdown"]))
        self.assertTrue(os.path.exists(paths["html"]))


class TestAttribution(unittest.TestCase):
    def test_detect_loop(self):
        from atk.model import CaseRun, RunMetrics, Trajectory, VersionQuad, ToolInvocation

        inv = [
            ToolInvocation(tool="search", args={"q": "x"}, ok=True),
            ToolInvocation(tool="search", args={"q": "x"}, ok=True),
            ToolInvocation(tool="search", args={"q": "x"}, ok=True),
        ]
        run = CaseRun(
            run_id="r",
            case_id="c",
            sample_idx=1,
            version_quad=VersionQuad(),
            trajectory=Trajectory(),
            invocations=inv,
            metrics=RunMetrics(),
            verdict=__import__("atk.model", fromlist=["Verdict"]).Verdict(final="fail"),
        )
        hit, samples = detect_loop(run)
        self.assertTrue(hit)
        self.assertEqual(len(samples), 3)


if __name__ == "__main__":
    unittest.main()
