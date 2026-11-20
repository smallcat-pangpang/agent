"""atk —— Agent 产品测试/评估框架（零第三方依赖）。

设计目标与文档第 0.2 节一一对应：
- 可复现：环境冻结（时钟/随机种子/网络/工具 Mock）+ 录制回放；
- 可归因：版本四元组 + 首个出错 step + 自动归因六分类；
- 可决策：分维度阈值门禁 + 显著性检验 + 一页式报告；
- 可信：Judge 盲评 + 校准集（Kappa/混淆矩阵/偏差/反向红队）；
- 可持续：用例 Schema 化、影响面分析、bad case 回流、Flaky 隔离区。

本包只依赖 Python 标准库；当环境中存在 pandas/openpyxl 时，
交付物校验会自动启用更强的解析能力（见 assertions/files.py）。
"""

__version__ = "1.0.0"

#: 框架对外暴露的版本号，会写进每份运行记录的 version_quad.framework 字段
FRAMEWORK_NAME = "atk"
