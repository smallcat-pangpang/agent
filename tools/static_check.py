"""零依赖静态体检：未定义名称 / 未使用导入 / 类型注解里的悬空名字。

为什么不用 ruff / mypy：

- 框架承诺「断网机器 python -m atk 直接跑」，CI 的 sanity 阶段不该强依赖联网装包；
- 但 `from __future__ import annotations` 会把注解变成字符串，**运行期永远不报错**，
  像 ``stats.py`` 里用了 ``Any`` 却没导入这种问题，105 个单测全绿也发现不了；
- 这个脚本用标准库 AST 专门抓这一类「运行期抓不到」的问题。

用法::

    python tools/static_check.py            # 只报告
    python tools/static_check.py --strict   # 有问题返回非零（CI 用）

ruff / mypy 的配置写在 pyproject.toml，本地可选执行，不作为强制门禁。
"""

from __future__ import annotations

import argparse
import ast
import builtins
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET_DIRS = ("atk", "tests", "tools")
ROOT_FILES = ("main.py", "demo.py")

#: 模块级隐式可用名（不是导入进来的）
IMPLICIT = {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__all__", "__path__", "__cached__",
    "annotations",
}


def _collect(tree: ast.AST) -> tuple[dict[str, int], set[str], set[str]]:
    imported: dict[str, int] = {}
    defined: set[str] = set(IMPLICIT) | set(dir(builtins))
    used: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported[alias.asname or alias.name.split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported[alias.asname or alias.name] = node.lineno
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            defined.update(node.names)
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                defined.add(node.id)
            else:
                used.add(node.id)
        elif isinstance(node, ast.Attribute):
            # obj.attr：只用到 obj，attr 不是独立名字
            pass

    # 注解里的名字（含字符串化注解之外的普通注解；future 注解仍是 AST 节点）
    for node in ast.walk(tree):
        annotations: list[ast.AST] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations += [a.annotation for a in node.args.args + node.args.kwonlyargs if a.annotation]
            if node.args.vararg and node.args.vararg.annotation:
                annotations.append(node.args.vararg.annotation)
            if node.args.kwarg and node.args.kwarg.annotation:
                annotations.append(node.args.kwarg.annotation)
            if node.returns:
                annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign) and node.annotation:
            annotations.append(node.annotation)
        for ann in annotations:
            used.update(n.id for n in ast.walk(ann) if isinstance(n, ast.Name))

    return imported, defined, used


def check_file(path: pathlib.Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return [f"[解析失败] {path}: {exc}"]

    imported, defined, used = _collect(tree)
    issues: list[str] = []
    is_package_init = path.name == "__init__.py"

    for name in sorted(used):
        if name not in defined and name not in imported:
            issues.append(f"[未定义名称] {path}: {name}")

    if not is_package_init:
        for name, lineno in imported.items():
            # `from __future__ import annotations` 是特性开关，不是普通名字
            if name == "annotations" or name in used:
                continue
            issues.append(f"[未使用导入] {path}:{lineno} {name}")

    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="atk 零依赖静态体检")
    parser.add_argument("--strict", action="store_true", help="发现问题返回非零退出码")
    args = parser.parse_args()

    files: list[pathlib.Path] = []
    for dirname in TARGET_DIRS:
        files.extend(sorted((ROOT / dirname).rglob("*.py")))
    for name in ROOT_FILES:
        path = ROOT / name
        if path.exists():
            files.append(path)

    issues: list[str] = []
    for path in files:
        issues.extend(check_file(path))

    for issue in issues:
        print(issue)
    blocking = [i for i in issues if i.startswith("[未定义名称]") or i.startswith("[解析失败]")]
    advisory = [i for i in issues if i not in blocking]
    print(f"\n检查 {len(files)} 个 Python 文件：")
    print(f"  阻断项（运行期抓不到的悬空名字）：{len(blocking)}")
    print(f"  提示项（未使用导入，可选清理）：{len(advisory)}")
    if blocking and args.strict:
        print("❌ 静态体检未通过：存在未定义名称", file=sys.stderr)
        return 1
    print("✅ 静态体检通过（阻断项 0）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
