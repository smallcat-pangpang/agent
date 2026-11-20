"""Schema 校验与用例加载测试。

用例不合法的典型表现是「跑批时才炸」，这里确保它们在加载阶段就被拦住，
并且错误信息能定位到具体字段。
"""

from __future__ import annotations

import copy
import os
import unittest

from atk.errors import CaseError
from atk.loader import load_cases
from atk.schema import load_bundled_schema
from atk.schema.validator import SchemaValidator

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def minimal_case() -> dict:
    return {
        "case_id": "AGT-TST-0001",
        "title": "最小用例",
        "layer": "L5",
        "input": {
            "turns": [{"role": "user", "content": "你好"}],
            "env": {
                "tool_mocks": {},
                "seed": 1,
                "frozen_time": "2026-11-18T10:00:00+08:00",
            },
        },
        "expect": {"rubric": {"correctness": 0.7, "completeness": 0.3}},
        "grade": {
            "auto_checks": [
                {"type": "tool_called", "args": {"tool": "x", "must": False}}
            ]
        },
    }


class TestCaseSchema(unittest.TestCase):
    def setUp(self):
        self.validator = SchemaValidator(load_bundled_schema("case"))

    def test_minimal_valid(self):
        self.assertEqual(self.validator.validate(minimal_case()), [])

    def test_missing_required(self):
        data = minimal_case()
        del data["input"]
        errors = self.validator.validate(data)
        self.assertTrue(any("缺少必填字段" in e and "input" in e for e in errors))

    def test_unknown_field_rejected(self):
        data = minimal_case()
        data["unexpected"] = 1
        errors = self.validator.validate(data)
        self.assertTrue(any("未声明字段" in e for e in errors))

    def test_bad_layer(self):
        data = minimal_case()
        data["layer"] = "L99"
        errors = self.validator.validate(data)
        self.assertTrue(any("layer" in e for e in errors))

    def test_bad_check_type(self):
        data = minimal_case()
        data["grade"]["auto_checks"][0]["type"] = "not_a_check"
        errors = self.validator.validate(data)
        self.assertTrue(any("auto_checks" in e for e in errors))

    def test_missing_frozen_time(self):
        data = minimal_case()
        del data["input"]["env"]["frozen_time"]
        errors = self.validator.validate(data)
        self.assertTrue(any("frozen_time" in e for e in errors))

    def test_bad_datetime_format(self):
        data = minimal_case()
        data["input"]["env"]["frozen_time"] = "昨天"
        errors = self.validator.validate(data)
        self.assertTrue(any("date-time" in e for e in errors))

    def test_turn_needs_content(self):
        data = minimal_case()
        del data["input"]["turns"][0]["content"]
        errors = self.validator.validate(data)
        self.assertTrue(any("content" in e for e in errors))


class TestLoader(unittest.TestCase):
    def test_loads_all_golden_cases(self):
        result = load_cases([os.path.join(ROOT, "cases", "golden")], strict=False)
        self.assertEqual(result.errors, [])
        self.assertGreaterEqual(len(result.cases), 30)
        # 覆盖 L1~L7 与安全面
        layers = {c.layer for c in result.cases}
        for required in ("L1", "L2", "L3", "L4", "L5", "L6"):
            self.assertIn(required, layers)
        self.assertTrue(any(c.is_safety for c in result.cases))

    def test_duplicate_case_id_detected(self):
        import tempfile
        import shutil

        tmp = os.path.join(os.path.dirname(HERE), ".atk-tmp", "dup-cases")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        try:
            import json

            data = minimal_case()
            for name in ("a.json", "b.json"):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False)
            result = load_cases([tmp], strict=False)
            self.assertTrue(any("重复" in e for e in result.errors))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_strict_mode_raises(self):
        tmp = os.path.join(os.path.dirname(HERE), ".atk-tmp", "bad-cases")
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        try:
            with open(os.path.join(tmp, "bad.yaml"), "w", encoding="utf-8") as fh:
                fh.write("case_id: [unclosed\n")
            with self.assertRaises(CaseError):
                load_cases([tmp], strict=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
