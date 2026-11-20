"""PyCharm / 命令行通用入口。

在 PyCharm 里直接右键运行本文件即可（无参数时打印帮助）。
需要跑具体命令时，在 Run Configuration 的 Parameters 里填，例如::

    run --profile all --agent replay:baseline --samples 3

等价于命令行 ``python -m atk ...``；也可以用 ``python main.py ...``。
"""

from __future__ import annotations

import os
import sys

# 保证从任意工作目录启动都能找到 atk 包与用例（PyCharm 的 Working directory 可能不是项目根）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atk.cli import main  # noqa: E402  (必须在 sys.path 调整之后导入)

if __name__ == "__main__":
    argv = sys.argv[1:]
    # 无参数时展示帮助，而不是抛 argparse 的「缺少子命令」错误
    raise SystemExit(main(argv if argv else ["--help"]))
