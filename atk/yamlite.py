"""内置 YAML 子集解析器（零依赖）。

为什么自己写：评测框架要求「拿到就能跑」，不该因为 ``pip install pyyaml`` 失败而
卡住整条评估流水线（很多 CI 沙箱是断网的）。这里实现用例 Schema 需要的 YAML 子集，
不支持 YAML 的高级特性（锚点/标签/复杂多行折叠），遇到不支持写法会明确报错，
而不是静默解析成错误结构。

支持范围
--------
1. 块状映射：``key: value``，可任意嵌套（缩进表达层级）；
2. 块状序列：``- item``，以及 ``- key: value`` 形式的「对象数组」；
3. 流式集合：``[a, b]``、``{k: v}``（可嵌套）；
4. 单引号 / 双引号字符串（含 ``\\n``、``\\t``、``\\uXXXX`` 等转义）；
5. 块标量：``|``、``|-``、``|+``、``>``、``>-``、``>+``（多行文本，如 Prompt）；
6. 标量类型推断：``true/false``、``null/~``、整数、浮点，其余一律为字符串；
7. 注释（``#``，引号内不算注释）与空行；
8. 文档分隔符 ``---`` / ``...``（只取第一个文档）。

不支持（会抛 :class:`atk.errors.YamlError`）
--------------------------------------------
- 锚点 ``&`` / 别名 ``*``、显式类型标签 ``!!str``；
- 多行纯文本标量（不带引号跨行写长句）——请改用 ``|`` 块标量；
- 复杂键（``? key`` 语法）。

用法::

    from atk.yamlite import loads, load_file
    data = loads("case_id: AGT-001\\nlayer: L2\\n")
    data = load_file("cases/golden/AGT-001.yaml")
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import YamlError

# ---------------------------------------------------------------------------
# 词法预处理：把物理行转换成一串「有效行」（去注释/去空行/记录缩进）
# ---------------------------------------------------------------------------

#: 整数字面量（含正负号）
_INT_RE = re.compile(r"^[+-]?\d+$")
#: 浮点字面量（含科学计数法、正负号、.5 这种简写）
_FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")


@dataclass(frozen=True)
class _Line:
    """一个「有意义」的物理行。

    Attributes:
        indent: 行首空格数（缩进层级）。
        text: 去掉行首缩进与行尾注释后的内容（不含块标量正文）。
        raw: 原始行（含缩进），块标量需要用它来保留内部空白。
        raw_idx: 在原始物理行列表中的下标，块标量据此向后取行。
        lineno: 1 起始的行号，用于报错。
    """

    indent: int
    text: str
    raw: str
    raw_idx: int
    lineno: int


def _strip_comment(line: str) -> str:
    """去掉行尾注释，但保留字符串内部的 ``#``。

    规则与 YAML 一致：``#`` 只有出现在「行首或空白之后」才算注释起点。
    例如 ``content: "#话题"`` 里的 ``#`` 在引号内，不裁剪；
    ``tags: [a, b]  # 备注`` 里的 ``#`` 前有空格，裁剪掉。
    """
    out: list[str] = []
    quote: str | None = None
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if quote is not None:
            out.append(ch)
            # 双引号内的反斜杠转义：跳过下一个字符，避免把 \" 当成结束引号
            if ch == "\\" and quote == '"' and i + 1 < n:
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
            continue
        # 注释起点：行首，或前面是空白
        if ch == "#" and (not out or out[-1] in " \t"):
            break
        out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _tokenize(text: str) -> tuple[list[_Line], list[str]]:
    """把文本切成有效行序列，同时返回原始物理行列表（供块标量使用）。"""
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    tokens: list[_Line] = []
    for idx, raw in enumerate(raw_lines):
        # 制表符缩进是 YAML 明令禁止的，这里直接报错，避免出现「看起来对齐其实没对齐」
        if raw.startswith("\t"):
            raise YamlError(f"第 {idx + 1} 行: 不允许使用 Tab 缩进，请改用空格")
        stripped = raw.lstrip(" ")
        indent = len(raw) - len(stripped)
        content = _strip_comment(stripped)
        if not content.strip():
            continue
        # 文档分隔符：只解析第一个文档。
        # 文件开头允许出现 "---"（跳过）；解析到内容之后再遇到 "---"/"..." 即结束。
        if indent == 0 and content.strip() in ("---", "..."):
            if content.strip() == "..." or tokens:
                break
            continue
        tokens.append(
            _Line(
                indent=indent,
                text=content.strip(),
                raw=raw,
                raw_idx=idx,
                lineno=idx + 1,
            )
        )
    return tokens, raw_lines


# ---------------------------------------------------------------------------
# 标量解析
# ---------------------------------------------------------------------------


def _unquote_double(s: str) -> str:
    """解析双引号字符串的转义序列。"""
    body = s[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(body):
            raise YamlError("双引号字符串以孤立反斜杠结尾")
        esc = body[i + 1]
        mapping = {
            "n": "\n",
            "t": "\t",
            "r": "\r",
            "0": "\0",
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
        }
        if esc in mapping:
            out.append(mapping[esc])
            i += 2
        elif esc == "u":
            hexs = body[i + 2 : i + 6]
            if len(hexs) != 4 or not all(c in "0123456789abcdefABCDEF" for c in hexs):
                raise YamlError(f"非法的 \\u 转义: {hexs!r}")
            out.append(chr(int(hexs, 16)))
            i += 6
        else:
            raise YamlError(f"不支持的转义字符: \\{esc}")
    return "".join(out)


def _parse_scalar(s: str) -> Any:
    """把一段纯量文本转换成 Python 值（字符串/布尔/None/数字）。"""
    s = s.strip()
    if s == "":
        return None
    if len(s) >= 2 and s[0] == s[-1] == '"':
        return _unquote_double(s)
    if len(s) >= 2 and s[0] == s[-1] == "'":
        # 单引号内唯一转义是 '' 表示一个 '
        return s[1:-1].replace("''", "'")
    low = s.lower()
    if low in ("null", "~"):
        return None
    if low in ("true", "false"):
        return low == "true"
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)
    # 其余保持字符串；注意 ISO 时间戳 "2026-11-18T10:00:00+08:00" 会原样保留，
    # 不做日期推断（框架内部统一按字符串处理，由 frozen_clock 负责解释）
    return s


def _split_flow_items(inner: str) -> list[str]:
    """按顶层逗号切分流式集合的内容，忽略引号/嵌套括号内的逗号。"""
    items: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(inner):
        ch = inner[i]
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(inner):
                buf.append(inner[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    return items


def _parse_flow(s: str) -> Any:
    """解析流式集合 ``[...]`` / ``{...}``；不是流式集合则按标量处理。"""
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_parse_flow(x) for x in _split_flow_items(inner)]
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        if not inner:
            return {}
        result: dict[Any, Any] = {}
        for item in _split_flow_items(inner):
            key, rest = _split_key(item)
            if key is None:
                raise YamlError(f"流式映射项缺少冒号: {item!r}")
            result[key] = _parse_flow(rest) if rest else None
        return result
    return _parse_scalar(s)


def _split_key(text: str) -> tuple[str | None, str]:
    """在「顶层」定位 ``key: value`` 的分隔冒号。

    必须是「冒号 + 空白/行尾」才算分隔符，这样 ``frozen_time: 2026-11-18T10:00:00+08:00``
    这种值里带冒号的场景不会被误切。返回 (键, 值文本)；找不到分隔符时返回 (None, 原文)。
    """
    quote: str | None = None
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote is not None:
            if ch == "\\" and quote == '"' and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            nxt = text[i + 1] if i + 1 < n else ""
            if nxt == "" or nxt in " \t":
                key = text[:i].strip()
                if len(key) >= 2 and key[0] == key[-1] and key[0] in "'\"":
                    key = str(_parse_scalar(key))
                return key, text[i + 1 :].strip()
        i += 1
    return None, text


# ---------------------------------------------------------------------------
# 语法解析
# ---------------------------------------------------------------------------

#: 块标量的指示符集合
_BLOCK_SCALAR_MARKS = {"|", "|-", "|+", ">", ">-", ">+"}


class _Parser:
    """递归下降解析器：按缩进确定层级，逐行消费。"""

    def __init__(self, tokens: list[_Line], raw_lines: list[str]) -> None:
        self.tokens = tokens
        self.raw_lines = raw_lines
        self.i = 0

    # -- 基础操作 ---------------------------------------------------------

    def _peek(self) -> _Line | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _next(self) -> _Line:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    @staticmethod
    def _is_seq(text: str) -> bool:
        """判断该行是否是序列项（``-`` 或 ``- xxx``）。"""
        return text == "-" or text.startswith("- ")

    def parse(self) -> Any:
        if not self.tokens:
            return None
        first = self.tokens[0]
        value = self._parse_block(first.indent)
        # 解析完仍有剩余行，说明缩进结构有歧义，必须报错而不是丢弃数据
        if self.i < len(self.tokens):
            tok = self.tokens[self.i]
            raise YamlError(f"第 {tok.lineno} 行: 缩进或结构异常（无法归入任何层级）")
        return value

    # -- 块解析 -----------------------------------------------------------

    def _parse_block(self, indent: int) -> Any:
        tok = self._peek()
        if tok is None or tok.indent < indent:
            return None
        if tok.indent > indent:
            raise YamlError(f"第 {tok.lineno} 行: 缩进不一致（期望 {indent} 个空格）")
        if self._is_seq(tok.text):
            return self._parse_seq(indent)
        return self._parse_map(indent)

    def _parse_seq(self, indent: int) -> list[Any]:
        """解析块状序列。"""
        items: list[Any] = []
        while True:
            tok = self._peek()
            if tok is None or tok.indent != indent or not self._is_seq(tok.text):
                break
            self._next()
            rest = tok.text[1:].lstrip()  # 去掉 "-" 与其后空格
            if rest == "":
                # "-" 独占一行：值在更深缩进的下一块
                nxt = self._peek()
                if nxt is not None and nxt.indent > indent:
                    items.append(self._parse_block(nxt.indent))
                else:
                    items.append(None)
                continue
            key, _ = _split_key(rest)
            if key is not None and not rest.startswith(("[", "{")):
                # "- key: value" 是「对象数组」写法：
                # 把该行虚拟成缩进更深的映射首行，交给 _parse_map 继续吃后续同级行。
                # 偏移量 = 该行实际列位置（"- " 的宽度），保证续行对齐判断正确。
                child_indent = tok.indent + (len(tok.text) - len(rest))
                self.tokens.insert(
                    self.i,
                    _Line(
                        indent=child_indent,
                        text=rest,
                        raw=tok.raw,
                        raw_idx=tok.raw_idx,
                        lineno=tok.lineno,
                    ),
                )
                items.append(self._parse_map(child_indent))
            else:
                items.append(_parse_flow(rest))
        return items

    def _parse_map(self, indent: int) -> dict[str, Any]:
        """解析块状映射。"""
        result: dict[str, Any] = {}
        while True:
            tok = self._peek()
            if tok is None or tok.indent != indent or self._is_seq(tok.text):
                break
            key, rest = _split_key(tok.text)
            if key is None:
                raise YamlError(
                    f"第 {tok.lineno} 行: 期望 'key: value' 结构，实际为 {tok.text!r}"
                )
            if key in result:
                # 重复键往往是复制粘贴改漏，静默覆盖会埋坑，直接报错
                raise YamlError(f"第 {tok.lineno} 行: 键 {key!r} 重复定义")
            self._next()

            if rest == "":
                nxt = self._peek()
                if nxt is not None and nxt.indent > indent:
                    result[key] = self._parse_block(nxt.indent)
                else:
                    result[key] = None
            elif rest in _BLOCK_SCALAR_MARKS:
                result[key] = self._block_scalar(rest, indent, tok)
            else:
                result[key] = _parse_flow(rest)
        return result

    # -- 块标量 -----------------------------------------------------------

    def _block_scalar(self, marker: str, parent_indent: int, key_tok: _Line) -> str:
        """解析 ``|`` / ``>`` 块标量（多行文本，常用于 Prompt 与长描述）。"""
        literal = marker.startswith("|")
        chomp = marker[1:] if len(marker) > 1 else ""

        # 1) 从键所在行之后，收集所有「缩进大于父级」或「空行」的原始行
        collected: list[tuple[int, str]] = []  # (缩进, 去缩进后的文本)
        idx = key_tok.raw_idx + 1
        block_indent: int | None = None
        last_raw_idx = key_tok.raw_idx
        while idx < len(self.raw_lines):
            raw = self.raw_lines[idx]
            if raw.strip() == "":
                collected.append((-1, ""))  # 空行占位，稍后按折叠规则处理
                idx += 1
                continue
            cur_indent = len(raw) - len(raw.lstrip(" "))
            if cur_indent <= parent_indent:
                break
            if block_indent is None:
                block_indent = cur_indent
            # 比块基准缩进更深的行，保留相对缩进（用于嵌套列表/代码块）
            text = raw[block_indent:] if len(raw) >= block_indent else raw.lstrip(" ")
            collected.append((cur_indent, text))
            last_raw_idx = idx
            idx += 1

        # 2) 丢弃尾部空行（它们只影响 chomping，不影响内容）
        while collected and collected[-1][1] == "":
            collected.pop()

        # 3) 让 token 指针跳过被块标量吃掉的物理行
        while self.i < len(self.tokens) and self.tokens[self.i].raw_idx <= last_raw_idx:
            self.i += 1

        if not collected:
            return ""

        if literal:
            body = "\n".join(text for _, text in collected)
        else:
            # 折叠：相邻非空行合并成一行；空行作为段落分隔
            parts: list[str] = []
            prev_blank = True
            for _, text in collected:
                if text == "":
                    parts.append("\n")
                    prev_blank = True
                else:
                    if parts and not prev_blank:
                        parts.append(" ")
                    parts.append(text)
                    prev_blank = False
            body = "".join(parts)

        # 4) 处理 chomping（结尾换行策略）
        if chomp == "-":  # strip：不加结尾换行
            return body.rstrip("\n")
        if chomp == "+":  # keep：原样保留
            return body + "\n"
        return body.rstrip("\n") + "\n"  # clip：保留一个结尾换行


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------


def loads(text: str) -> Any:
    """解析 YAML 文本（仅第一个文档）。"""
    tokens, raw_lines = _tokenize(text)
    return _Parser(tokens, raw_lines).parse()


def load_file(path: str) -> Any:
    """读取并解析 YAML 文件（UTF-8）。"""
    try:
        # utf-8-sig：兼容 Windows 上带 BOM 的 YAML（编辑器/记事本常见）
        with open(path, "r", encoding="utf-8-sig") as fh:
            return loads(fh.read())
    except OSError as exc:
        raise YamlError(f"无法读取文件 {path}: {exc}") from exc


def dumps_simple(data: Any) -> str:
    """把简单结构序列化成 YAML（仅用于生成给人看的配置片段，不保证全特性）。

    只支持 dict/list/标量，且只输出块状风格；主要用于 :mod:`atk.cli` 的
    ``atk init`` 生成模板。
    """
    return _dump_block(data, 0)


def _dump_block(value: Any, indent: int) -> str:
    pad = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(_dump_block(v, indent + 2).rstrip("\n"))
            else:
                lines.append(f"{pad}{k}: {_dump_scalar(v)}")
        return "\n".join(lines) + "\n"
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)) and item:
                block = _dump_block(item, indent + 2).rstrip("\n")
                block_lines = block.split("\n")
                # 第一个元素改成 "- " 开头
                first = block_lines[0].lstrip()
                lines.append(f"{pad}- {first}")
                lines.extend(block_lines[1:])
            else:
                lines.append(f"{pad}- {_dump_scalar(item)}")
        return "\n".join(lines) + "\n"
    return f"{pad}{_dump_scalar(value)}\n"


def _dump_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    # 需要引号的场景：含特殊字符、看起来像数字/布尔、首尾空格
    need_quote = (
        s == ""
        or s.strip() != s
        or any(c in s for c in ":#{}[],&*!|>'\"%@`\n")
        or s.lower() in ("true", "false", "null", "~", "yes", "no", "on", "off")
        or _INT_RE.match(s) is not None
        or _FLOAT_RE.match(s) is not None
    )
    if need_quote:
        escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return s
