"""用例加载与校验。

一条用例从磁盘到内存要过三关（缺一关都会让评估结果不可信）：

1. **语法关**：YAML/JSON 能解析（内置解析器报错会带行号）；
2. **Schema 关**：字段、类型、枚举、必填项符合 ``case.schema.json``；
3. **语义关**：Schema 表达不了的规则，例如「rubric 的维度必须合法」、
   「fixture 路径必须存在」、「auto_checks 的 type 必须已实现」、
   「同一 case_id 不允许出现两次」。

第 3 关很重要：只做 Schema 校验的框架，会让「引用了不存在的 fixture」这类
配置错误在跑批时才炸，浪费一整轮评估时间。
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable

from .config import Config
from .errors import CaseError
from .model import DIMENSIONS, LAYERS, TestCase, parse_iso
from .schema import load_bundled_schema
from .schema.validator import SchemaValidator
from .yamlite import YamlError, load_file

#: 自动发现的用例文件后缀
CASE_SUFFIXES = (".yaml", ".yml", ".json")


@dataclass
class LoadResult:
    """加载结果：用例 + 错误 + 告警。

    ``total_files`` 记录扫描到的文件数：解析失败的文件会被跳过、不进 ``cases``，
    只有保留扫描总数，validate 的汇总才能说清「扫了几个、成几个、挂几个」，
    而不是显示「共 0 条用例，6 个错误」这种把解析失败藏起来的口径。
    """

    cases: list[TestCase] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 实际扫描到的用例文件数（含解析失败被跳过的）
    total_files: int = 0

    def ok(self) -> bool:
        return not self.errors

    def raise_if_errors(self) -> None:
        """有错误时抛出，附前 10 条明细（错误太多时只展示前 10 条）。"""
        if not self.errors:
            return
        head = "\n".join(f"  - {e}" for e in self.errors[:10])
        more = f"\n  ... 共 {len(self.errors)} 条错误" if len(self.errors) > 10 else ""
        raise CaseError("<批量加载>", f"用例校验未通过:\n{head}{more}")


# ---------------------------------------------------------------------------
# 文件发现
# ---------------------------------------------------------------------------


def discover_case_files(paths: Iterable[str], recursive: bool = True) -> list[str]:
    """把「目录 / 文件 / 通配符」统一展开成文件列表，并去重排序。

    跳过以 ``_`` 或 ``.`` 开头的文件（框架约定的草稿/临时文件）。
    """
    found: list[str] = []
    for raw in paths:
        if any(ch in raw for ch in "*?["):
            found.extend(glob.glob(raw, recursive=True))
            continue
        if os.path.isdir(raw):
            pattern = "**/*" if recursive else "*"
            for path in glob.glob(os.path.join(raw, pattern), recursive=recursive):
                if os.path.isfile(path):
                    found.append(path)
            continue
        found.append(raw)

    result: list[str] = []
    seen: set[str] = set()
    for path in sorted(found):
        name = os.path.basename(path)
        if not path.lower().endswith(CASE_SUFFIXES):
            continue
        if name.startswith("_") or name.startswith("."):
            continue
        norm = os.path.abspath(path)
        if norm in seen:
            continue
        seen.add(norm)
        result.append(norm)
    return result


def _read_case_file(path: str) -> dict[str, Any]:
    """读取用例文件：``.json`` 用标准库，其余走内置 YAML 解析器。"""
    if path.lower().endswith(".json"):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = load_file(path)
    if not isinstance(data, dict):
        raise CaseError(path, "用例根节点必须是映射（key: value）")
    return data


# ---------------------------------------------------------------------------
# 语义校验
# ---------------------------------------------------------------------------


def _check_semantics(case: TestCase, result: LoadResult, known_checks: set[str]) -> None:
    """Schema 表达不了的规则，在这里兜底。"""

    def err(msg: str) -> None:
        result.errors.append(f"{case.case_id} ({os.path.basename(case.path)}): {msg}")

    def warn(msg: str) -> None:
        result.warnings.append(f"{case.case_id}: {msg}")

    # 1) rubric：维度必须合法，且至少有一个维度（否则无法算四维分）
    if not case.expect.rubric:
        err("expect.rubric 为空：必须给出四维权重，否则无法分维度报告")
    for dim in case.expect.rubric:
        if dim not in DIMENSIONS:
            err(f"expect.rubric 中出现未知维度 {dim!r}，合法值为 {list(DIMENSIONS)}")
    if case.expect.rubric and abs(sum(case.expect.rubric.values()) - 1.0) > 1e-6:
        # from_dict 已归一化，这里防御未来有人绕过 from_dict 直接构造
        err("expect.rubric 权重之和应为 1，请检查配置")

    # 2) forbidden：黑名单为空是常见疏漏，给出告警（规范 1.3「黑名单优先」）
    if not case.expect.forbidden:
        warn("expect.forbidden 为空：建议至少写 1 条禁止行为，黑名单比白名单更可靠")

    # 3) must_achieve 为空同样告警：没有成功标准就无法判「达成」
    if not case.expect.must_achieve:
        warn("expect.must_achieve 为空：用例缺少成功标准")

    # 3b) 涉及工具调用的用例必须声明「允许路径集合」，否则只写唯一答案会误杀合法解。
    #     这是 README §4 三条硬规矩的第 1 条，以前只在文档里说、validate 没检查。
    if case.env.tool_mocks and not case.expect.allowed_paths:
        warn(
            "expect.allowed_paths 缺失：涉及工具调用的用例应声明允许路径集合（可写多条），"
            "只写唯一答案会误杀合法解"
        )

    # 4) 每个 forbid 若声明了 tools，必须与 env.tool_mocks 对得上
    for item in case.expect.forbidden:
        for tool in item.tools:
            if tool not in case.env.tool_mocks and tool not in ("*",):
                warn(f"forbidden 引用了未 Mock 的工具 {tool!r}，该黑名单可能永远不会命中")

    # 5) allowed_paths 里的工具也应在 Mock 清单里
    for path in case.expect.allowed_paths:
        if not path:
            err("allowed_paths 中存在空路径")
        for tool in path:
            if tool not in case.env.tool_mocks and tool not in ("*", "final"):
                warn(f"allowed_paths 引用了未 Mock 的工具 {tool!r}")

    # 6) 断言类型必须已实现（否则会运行期才炸）
    for check in case.grade.auto_checks:
        if check.type not in known_checks:
            err(
                f"grade.auto_checks 中未知断言类型 {check.type!r}，"
                f"已实现：{sorted(known_checks)}"
            )
        if check.dimension is not None and check.dimension not in DIMENSIONS:
            err(f"断言 {check.type} 的 dimension 非法: {check.dimension!r}")
    # must_achieve 内嵌的 checks 同样校验
    for item in case.expect.must_achieve:
        for check in item.checks:
            if check.type not in known_checks:
                err(f"must_achieve.checks 中未知断言类型 {check.type!r}")

    # 7) 冻结时间必须可解析（可复现的最低门槛）
    try:
        parse_iso(case.env.frozen_time)
    except ValueError:
        err(f"input.env.frozen_time 不是合法 ISO 时间: {case.env.frozen_time!r}")

    # 8) 步数标注一致性
    lo, hi = case.expect.expected_min_steps, case.expect.expected_max_steps
    if lo is not None and hi is not None and lo > hi:
        err(f"expected_min_steps({lo}) 不能大于 expected_max_steps({hi})")

    # 9) fixture / 附件是否存在（相对用例目录与工程根目录都找一遍）
    base_dir = os.path.dirname(case.path)
    for name, rel in case.env.fixtures.items():
        if not _resolve_existing(rel, base_dir):
            result.warnings.append(f"{case.case_id}: fixture {name!r} 指向的文件不存在: {rel}")
    for turn in case.input_turns:
        for rel in turn.attachments:
            if not _resolve_existing(rel, base_dir):
                result.warnings.append(
                    f"{case.case_id}: 附件不存在: {rel}（运行时沙箱会缺失该文件）"
                )

    # 10) 分层与优先级的基本常识检查
    if case.layer not in LAYERS:
        err(f"未知分层 {case.layer!r}")
    if case.layer == "L5" and case.priority == "P2":
        warn("L5 端到端用例设为 P2 可能导致门禁覆盖不足")


def _resolve_existing(rel_path: str, base_dir: str) -> bool:
    """相对路径解析：先按用例目录，再按当前工作目录（工程根）。"""
    if os.path.isabs(rel_path):
        return os.path.exists(rel_path)
    if os.path.exists(os.path.join(base_dir, rel_path)):
        return True
    return os.path.exists(rel_path)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def load_cases(
    paths: Iterable[str] | str,
    *,
    config: Config | None = None,
    strict: bool = True,
) -> LoadResult:
    """加载并校验用例。

    Args:
        paths: 目录、文件或通配符；传单个字符串也可。
        config: 框架配置（当前仅用于日志/未来扩展）。
        strict: True 时，只要有一条用例不合法就整体失败；False 时跳过坏用例继续。

    Raises:
        CaseError: ``strict=True`` 且存在错误时。
    """
    # 延迟导入，避免 loader ←→ assertions 的循环依赖，同时让「已实现断言清单」
    # 永远与代码真实情况一致（不用维护两份列表）
    from .assertions import CHECK_TYPES

    if isinstance(paths, str):
        paths = [paths]
    files = discover_case_files(paths)
    result = LoadResult()
    result.total_files = len(files)

    if not files:
        result.errors.append("未找到任何用例文件（.yaml/.yml/.json）")
        if strict:
            result.raise_if_errors()
        return result

    schema = load_bundled_schema("case")
    validator = SchemaValidator(schema)
    seen_ids: dict[str, str] = {}

    for path in files:
        try:
            raw = _read_case_file(path)
        except (YamlError, json.JSONDecodeError, CaseError) as exc:
            result.errors.append(f"{path}: 解析失败 - {exc}")
            continue

        # Schema 校验：一次列出全部问题，避免「改一个跑一次」
        schema_errors = validator.validate(raw)
        if schema_errors:
            for msg in schema_errors:
                result.errors.append(f"{os.path.basename(path)}: {msg}")
            continue

        try:
            # _path 是内部字段，Schema 校验通过后再注入，避免污染用例定义
            raw["_path"] = path
            case = TestCase.from_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            result.errors.append(f"{path}: 字段类型错误 - {exc}")
            continue

        # 文件命名约定：case_id 与文件名一致，方便反查
        stem = os.path.splitext(os.path.basename(path))[0]
        if stem != case.case_id:
            result.warnings.append(
                f"{os.path.basename(path)}: 文件名与 case_id({case.case_id}) 不一致"
            )

        # case_id 全局唯一
        if case.case_id in seen_ids:
            result.errors.append(
                f"{case.case_id}: case_id 重复（{seen_ids[case.case_id]} 与 {path}）"
            )
            continue
        seen_ids[case.case_id] = path

        _check_semantics(case, result, set(CHECK_TYPES))
        result.cases.append(case)

    if strict and result.errors:
        result.raise_if_errors()
    return result
