"""框架统一的异常类型。

把所有异常收敛成少数几个类型，方便 CLI 在最外层统一转成「用户可读的中文报错」，
而不是抛出几十行 traceback 让使用者去猜（对应文档 14.2 第 11 条：失败必须可定位）。
"""

from __future__ import annotations


class AtkError(Exception):
    """框架所有异常的基类。"""


class YamlError(AtkError):
    """YAML 解析失败（本框架使用内置的子集解析器，见 yamlite.py）。"""


class CaseError(AtkError):
    """用例本身不合法（Schema 校验失败、字段缺失、引用不存在等）。

    Attributes:
        path: 出问题的用例文件路径，便于直接跳转修改。
        detail: 具体是哪个字段、错在哪。
    """

    def __init__(self, path: str, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"[用例不合法] {path}: {detail}")


class ConfigError(AtkError):
    """框架配置（config/atk.yaml）不合法。"""


class RunError(AtkError):
    """执行期错误（Agent 崩溃、超时、产物缺失等）。"""


class GateError(AtkError):
    """门禁无法计算（如缺少基线、样本量不足）。"""


class CalibrationError(AtkError):
    """Judge 校准流程失败（校准集缺失、人工标签缺失等）。"""
