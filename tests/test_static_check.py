"""静态体检自身的回归测试。

背景：``stats.py`` 曾出现 ``Sequence[Any]`` 但没导入 ``Any``。
因为文件头有 ``from __future__ import annotations``，注解只是字符串，
运行期完全正常、105 个单测全绿，只有静态检查能抓到。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.static_check import check_file


class TestStaticCheck(unittest.TestCase):
    def _check(self, source: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.py"
            path.write_text(source, encoding="utf-8")
            return check_file(path)

    def test_catches_undefined_annotation_name(self):
        issues = self._check(
            "from __future__ import annotations\n"
            "from typing import Sequence\n"
            "def f(x: Sequence[Any]) -> None: ...\n"
        )
        self.assertTrue(
            any("未定义名称" in i and "Any" in i for i in issues),
            f"应抓到未导入的 Any，实际：{issues}",
        )

    def test_clean_file_has_no_blocking_issues(self):
        issues = self._check(
            "from __future__ import annotations\n"
            "from typing import Any, Sequence\n"
            "def f(x: Sequence[Any]) -> None: ...\n"
        )
        self.assertFalse(
            [i for i in issues if "未定义名称" in i or "解析失败" in i],
            f"干净文件不该有阻断项，实际：{issues}",
        )

    def test_except_handler_name_is_defined(self):
        issues = self._check(
            "try:\n"
            "    pass\n"
            "except ValueError as exc:\n"
            "    print(exc)\n"
        )
        self.assertFalse([i for i in issues if "未定义名称" in i])


if __name__ == "__main__":
    unittest.main()
