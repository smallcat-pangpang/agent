"""极简 JSON Schema 校验器（零依赖）。

为什么不用 ``jsonschema`` 库：本项目要求「零安装即可跑」，而用例 Schema 只用到
JSON Schema 的一个很小子集。这里实现该子集，错误信息使用「中文 + JSON 路径」，
直接告诉用例作者改哪一行。

支持的关键字
------------
``type`` / ``required`` / ``properties`` / ``additionalProperties`` /
``patternProperties`` / ``items`` / ``enum`` / ``const`` / ``minimum`` /
``maximum`` / ``exclusiveMinimum`` / ``exclusiveMaximum`` / ``minLength`` /
``maxLength`` / ``pattern`` / ``minItems`` / ``maxItems`` / ``uniqueItems`` /
``oneOf`` / ``anyOf`` / ``allOf`` / ``not`` / ``$ref``（仅 ``#/definitions/*``
与 ``#/$defs/*``）/ ``format``（date-time / date / uri）。

不支持的关键字会被忽略（``title``/``description``/``default`` 等注解类关键字本就无校验语义）。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

#: JSON 类型名 → Python 类型判断函数
_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class SchemaValidator:
    """JSON Schema 子集校验器。

    使用方式::

        v = SchemaValidator(schema)
        errors = v.validate(instance)   # 返回中文错误列表，空列表表示通过
    """

    def __init__(self, schema: dict[str, Any]) -> None:
        self.root = schema

    # -- 对外入口 ---------------------------------------------------------

    def validate(self, instance: Any) -> list[str]:
        """校验实例，返回全部错误（不短路，方便一次改完所有问题）。"""
        errors: list[str] = []
        self._validate(instance, self.root, "$", errors)
        return errors

    # -- 内部实现 ---------------------------------------------------------

    def _resolve(self, ref: str) -> dict[str, Any]:
        """解析本地 ``$ref``（只支持文档内部引用）。"""
        if not ref.startswith("#/"):
            raise ValueError(f"仅支持文档内 $ref，收到: {ref}")
        node: Any = self.root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise ValueError(f"$ref 指向不存在的节点: {ref}")
            node = node[part]
        return node

    def _validate(self, value: Any, schema: Any, path: str, errors: list[str]) -> None:
        # 布尔 Schema：true 表示放行，false 表示一律失败
        if isinstance(schema, bool):
            if not schema:
                errors.append(f"{path}: 该位置不允许出现任何值")
            return
        if not isinstance(schema, dict):
            return

        # 1) $ref：解引用后继续校验（本项目不存在循环引用）
        if "$ref" in schema:
            self._validate(value, self._resolve(schema["$ref"]), path, errors)
            return

        # 2) 组合关键字
        if "allOf" in schema:
            for sub in schema["allOf"]:
                self._validate(value, sub, path, errors)
        if "anyOf" in schema:
            if not any(not self._probe(value, sub) for sub in schema["anyOf"]):
                errors.append(f"{path}: 不满足 anyOf 中任意一个分支")
        if "oneOf" in schema:
            hits = sum(1 for sub in schema["oneOf"] if not self._probe(value, sub))
            if hits != 1:
                errors.append(f"{path}: 必须且只能命中 oneOf 的 1 个分支（实际命中 {hits} 个）")
        if "not" in schema:
            if not self._probe(value, schema["not"]):
                errors.append(f"{path}: 命中了 not 禁止的结构")

        # 3) type
        if "type" in schema:
            types = schema["type"]
            types = [types] if isinstance(types, str) else list(types)
            if not any(_TYPE_CHECKS.get(t, lambda _v: True)(value) for t in types):
                errors.append(
                    f"{path}: 类型应为 {'/'.join(types)}，实际为 {type(value).__name__}"
                )
                return  # 类型都不对，后面的约束没有意义

        # 4) enum / const
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{path}: 取值必须是 {schema['enum']} 之一，实际为 {value!r}")
        if "const" in schema and value != schema["const"]:
            errors.append(f"{path}: 取值必须等于 {schema['const']!r}")

        # 5) 数值约束
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self._number_rules(value, schema, path, errors)

        # 6) 字符串约束
        if isinstance(value, str):
            self._string_rules(value, schema, path, errors)

        # 7) 数组约束
        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                errors.append(f"{path}: 元素个数至少 {schema['minItems']}，实际 {len(value)}")
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                errors.append(f"{path}: 元素个数最多 {schema['maxItems']}，实际 {len(value)}")
            if schema.get("uniqueItems") and len({_freeze(x) for x in value}) != len(value):
                errors.append(f"{path}: 数组元素必须唯一，存在重复项")
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for i, item in enumerate(value):
                    self._validate(item, item_schema, f"{path}[{i}]", errors)
            elif isinstance(item_schema, list):
                # 元组式校验（旧草案写法）
                for i, (item, sub) in enumerate(zip(value, item_schema)):
                    self._validate(item, sub, f"{path}[{i}]", errors)

        # 8) 对象约束
        if isinstance(value, dict):
            self._object_rules(value, schema, path, errors)

    def _number_rules(
        self, value: float, schema: dict[str, Any], path: str, errors: list[str]
    ) -> None:
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: 不能小于 {schema['minimum']}，实际 {value}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: 不能大于 {schema['maximum']}，实际 {value}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: 必须大于 {schema['exclusiveMinimum']}，实际 {value}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: 必须小于 {schema['exclusiveMaximum']}，实际 {value}")

    def _string_rules(
        self, value: str, schema: dict[str, Any], path: str, errors: list[str]
    ) -> None:
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: 长度至少 {schema['minLength']}，实际 {len(value)}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: 长度最多 {schema['maxLength']}，实际 {len(value)}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: 不匹配正则 {schema['pattern']!r}，实际 {value!r}")
        fmt = schema.get("format")
        if fmt == "date-time":
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                errors.append(f"{path}: 不是合法的 date-time: {value!r}")
        elif fmt == "date":
            try:
                date.fromisoformat(value)
            except ValueError:
                errors.append(f"{path}: 不是合法的 date: {value!r}")
        elif fmt == "uri":
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
                errors.append(f"{path}: 不是合法的 URI: {value!r}")

    def _object_rules(
        self, value: dict[str, Any], schema: dict[str, Any], path: str, errors: list[str]
    ) -> None:
        props: dict[str, Any] = schema.get("properties", {})
        required: list[str] = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{path}: 缺少必填字段 {key!r}")
        # 先按 properties 校验已知字段
        for key, sub in props.items():
            if key in value:
                self._validate(value[key], sub, f"{path}.{key}", errors)
        # 再处理 patternProperties
        patterns = schema.get("patternProperties", {})
        for key, item in value.items():
            matched = [p for p in patterns if re.search(p, key)]
            for p in matched:
                self._validate(item, patterns[p], f"{path}.{key}", errors)
        # 最后处理未声明字段
        additional = schema.get("additionalProperties", True)
        if additional is not True:
            for key in value:
                if key in props or any(re.search(p, key) for p in patterns):
                    continue
                if additional is False:
                    errors.append(f"{path}: 不允许出现未声明字段 {key!r}")
                elif isinstance(additional, dict):
                    self._validate(value[key], additional, f"{path}.{key}", errors)

    def _probe(self, value: Any, schema: Any) -> list[str]:
        """静默地校验一个分支，返回错误列表（不写入主错误列表）。"""
        errs: list[str] = []
        self._validate(value, schema, "$", errs)
        return errs


def _freeze(value: Any) -> str:
    """把任意 JSON 值转成可哈希字符串，用于 uniqueItems 判断。"""
    if isinstance(value, dict):
        return "{" + ",".join(f"{k}:{_freeze(v)}" for k, v in sorted(value.items())) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_freeze(v) for v in value) + "]"
    return repr(value)


def validate(instance: Any, schema: dict[str, Any]) -> list[str]:
    """函数式便捷入口。"""
    return SchemaValidator(schema).validate(instance)
