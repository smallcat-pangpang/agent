"""内置 YAML 子集解析器的测试。

用例库全靠它加载，一旦解析出错，整条评估流水线的结论都不可信。
这里的测试覆盖：嵌套映射/序列、对象数组、流式集合、块标量、注释、
引号与冒号歧义、以及「错误输入必须报错而不是静默解析成错误结构」。
"""

from __future__ import annotations

import os
import unittest

from atk.yamlite import YamlError, dumps_simple, loads


class TestYamlite(unittest.TestCase):
    def test_nested_map_and_seq(self):
        text = """
case_id: AGT-L1-0001
tags: [a, b, c]
input:
  turns:
    - role: user
      content: "你好"
    - role: user
      content: "再说一次"
  env:
    seed: 42
    flag: true
    nothing: null
"""
        data = loads(text)
        self.assertEqual(data["case_id"], "AGT-L1-0001")
        self.assertEqual(data["tags"], ["a", "b", "c"])
        self.assertEqual(len(data["input"]["turns"]), 2)
        self.assertEqual(data["input"]["turns"][1]["content"], "再说一次")
        self.assertEqual(data["input"]["env"]["seed"], 42)
        self.assertTrue(data["input"]["env"]["flag"])
        self.assertIsNone(data["input"]["env"]["nothing"])

    def test_colon_in_value(self):
        # 时间戳里的冒号不能被当成分隔符
        data = loads('frozen_time: "2026-11-18T10:00:00+08:00"\nurl: https://a.b/c\n')
        self.assertEqual(data["frozen_time"], "2026-11-18T10:00:00+08:00")
        self.assertEqual(data["url"], "https://a.b/c")

    def test_flow_map_and_nested(self):
        data = loads('a: {x: 1, y: [2, 3], z: {k: "v"}}\n')
        self.assertEqual(data["a"]["y"], [2, 3])
        self.assertEqual(data["a"]["z"]["k"], "v")

    def test_comments_are_stripped_but_not_in_quotes(self):
        data = loads('a: 1  # 注释\nb: "#不是注释"\n')
        self.assertEqual(data["a"], 1)
        self.assertEqual(data["b"], "#不是注释")

    def test_block_scalar_literal_and_folded(self):
        text = """
prompt: |
  第一行
  第二行
folded: >-
  这是
  一段折叠文本
"""
        data = loads(text)
        self.assertIn("第一行\n第二行", data["prompt"])
        self.assertEqual(data["folded"], "这是 一段折叠文本")

    def test_quoted_strings_and_escapes(self):
        data = loads('a: "line1\\nline2"\nb: \'it\'\'s ok\'\n')
        self.assertEqual(data["a"], "line1\nline2")
        self.assertEqual(data["b"], "it's ok")

    def test_numbers_and_bools(self):
        data = loads("a: -1\nb: 0.5\nc: 1e3\nd: false\n")
        self.assertEqual(data["a"], -1)
        self.assertEqual(data["b"], 0.5)
        self.assertEqual(data["c"], 1000.0)
        self.assertFalse(data["d"])

    def test_bad_indent_raises(self):
        with self.assertRaises(YamlError):
            loads("a:\n    b: 1\n  c: 2\n")

    def test_duplicate_key_raises(self):
        with self.assertRaises(YamlError):
            loads("a: 1\na: 2\n")

    def test_missing_colon_raises(self):
        with self.assertRaises(YamlError):
            loads("a: 1\nthis is not yaml\n")

    def test_tab_indent_raises(self):
        with self.assertRaises(YamlError):
            loads("a:\n\tb: 1\n")

    def test_document_separator(self):
        data = loads("---\na: 1\n---\nb: 2\n")
        self.assertEqual(data, {"a": 1})

    def test_dumps_simple_roundtrip(self):
        original = {"a": 1, "b": ["x", "y"], "c": {"d": True, "e": None}}
        self.assertEqual(loads(dumps_simple(original)), original)

    def test_all_real_cases_parse(self):
        """真实用例文件必须全部能解析（防止只在小样例上通过）。"""
        root = os.path.join(os.path.dirname(__file__), "..", "cases", "golden")
        if not os.path.isdir(root):
            self.skipTest("用例目录不存在")
        count = 0
        for name in sorted(os.listdir(root)):
            if name.endswith((".yaml", ".yml")):
                with open(os.path.join(root, name), "r", encoding="utf-8") as fh:
                    data = loads(fh.read())
                self.assertIn("case_id", data, name)
                count += 1
        self.assertGreaterEqual(count, 30, "黄金用例应至少 30 条")


if __name__ == "__main__":
    unittest.main()
