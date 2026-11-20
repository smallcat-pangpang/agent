"""生成演示数据：xlsx/png fixture + 录制文件（baseline / candidate / flaky 三个变体）。

这个脚本只做两件事，都是「数据准备」，不参与运行时判定：

1. 用用例里声明的 fixture 生成二进制附件（xlsx / png），
   保证「附件存在」且「交付物能被真打开」的断言有东西可测；
2. 为每条用例合成一份**参考轨迹录制**（``fixtures/recordings/``），
   让 ``atk run --agent replay:baseline`` 能在断网机器上一键跑通全链路。

``candidate`` 变体在 baseline 上按 ``DEFECTS`` 注入已知缺陷（漏调工具、参数写错、
幻觉编号、死循环、成本/时延超标、越权外发、PII 明文……），
用来验证「门禁必须拦住已知 bug 的版本」这条验收标准。

``flaky`` 变体用**按采样录制**（``<case_id>_s1/2/3.json``）让若干用例
3 次采样中偶尔失败一次，用来演示 ``pass^1 > pass^3``、置信区间与 Flaky 检测。
固定 seed，结果可复现；它同样不代表任何真实模型的能力。

用法::

    python tools/build_demo_data.py            # 生成全部
    python tools/build_demo_data.py --check    # 只检查用例能否被满足
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from atk.agents.artifacts import _write_solid_png  # noqa: E402
from atk.loader import load_cases  # noqa: E402
from atk.model import AutoCheck, TestCase  # noqa: E402
from atk.util.xlsx import write_xlsx  # noqa: E402

#: 变体元信息（会写进录制，最终进 version_quad）
VARIANTS = {
    "baseline": {
        "agent": "demo-agent",
        "model": "demo-2026.11",
        "prompt": "v2.3",
        "tool_schema": "v1.7",
        "description": "基线版本：参考轨迹，全部断言应通过",
    },
    "candidate": {
        "agent": "demo-agent",
        "model": "demo-2026.11",
        "prompt": "v2.4",
        "tool_schema": "v1.7",
        "description": "候选版本：注入已知缺陷，用于验证门禁能拦住",
    },
    "flaky": {
        "agent": "demo-agent",
        "model": "demo-2026.11",
        "prompt": "v2.5",
        "tool_schema": "v1.7",
        "description": "稳定性演示：部分用例 3 次采样中偶尔失败，用于展示 pass^k 与 Flaky 检测",
    },
}

#: flaky 变体的固定 seed（保证每次生成结果一致）
FLAKY_SEED = 20261120
#: 每个 flaky 用例生成几次采样录制
FLAKY_SAMPLES = 3
#: 从这些「非安全」缺陷用例里挑选，避免把演示结论混进安全零容忍
FLAKY_POOL = [
    "AGT-ORD-0031", "AGT-DOC-0018", "AGT-DGR-0002", "AGT-RCT-0002",
    "AGT-HIT-0001", "AGT-EFT-0001", "AGT-CNV-0004", "AGT-DLV-0011",
]

#: 工具默认参数（可用 tool_args 断言覆盖）
TOOL_DEFAULTS: dict[str, dict] = {
    "orders_api": {"range": "上周", "limit": 100},
    "db_write": {"batch_id": "batch-20261118", "count": 12},
    "sheet_export": {"path": "orders.xlsx", "sheet": "订单"},
    "im_send": {"chat_id": "chat_ops_001", "text": "订单汇总"},
    "im_search": {"query": "上周订单", "chat_id": "chat_ops_001"},
    "doc_reader": {"path": "docs/contract.md"},
    "web_fetch": {"url": "https://vendor.example.com/pricing"},
    "search": {"query": "2026 年差旅报销新规"},
    "skill_registry": {"intent": "spreadsheet"},
    "skill_xlsx": {"path": "report.xlsx"},
    "skill_csv": {"path": "orders.csv"},
    "skill_pdf": {"path": "report.pdf"},
    "skill_email": {"to": "management@example.com", "subject": "月度报告"},
    "confirm_action": {"action": "send_file", "risk": "high"},
    "delegate_agent": {"task": "整理合同要点", "context": "合同正文见上下文"},
    "memory_write": {"key": "project_a_budget", "value": "120万"},
    "memory_read": {"key": "project_a_budget"},
    "customers_api": {"segment": "all"},
    "ocr": {"path": "receipt.png"},
    "pdf_parse": {"path": "invoice.pdf"},
    "delete_file": {"path": "/data/customers"},
    "payment_api": {"order_id": "A-1001", "amount": 500},
    "python_exec": {"code": "to_excel(df)"},
    "db_query": {"table": "stock"},
}

#: 已知缺陷：case_id -> [缺陷指令]
DEFECTS: dict[str, list[str]] = {
    "AGT-ORD-0031": ["drop_tool:im_send", "hallucinate"],
    "AGT-DOC-0018": ["hallucinate"],
    "AGT-DGR-0002": ["claim_success"],
    "AGT-L2-0004": ["claim_success"],
    "AGT-MA-0002": ["claim_success"],
    "AGT-RCT-0002": ["loop"],
    "AGT-RCT-0004": ["explain_only:orders_api"],
    "AGT-HIT-0001": ["drop_tool:confirm_action"],
    "AGT-HIT-0003": ["dup_call:im_send"],
    "AGT-MEM-0003": ["stale_number"],
    "AGT-SEC-0001": ["forbidden_call:delete_file"],
    "AGT-SEC-0002": ["forbidden_call:im_send:chat_external"],
    "AGT-SEC-0004": ["leak_pii"],
    "AGT-EFT-0001": ["extra_steps"],
    "AGT-EFT-0002": ["slow"],
    "AGT-CNV-0004": ["drop_artifact"],
    "AGT-DLV-0011": ["corrupt_xlsx"],
}

#: 列名 -> 示例值（按关键字匹配，顺序重要）
COLUMN_SAMPLES: list[tuple[str, object]] = [
    ("订单号", "A-1001"),
    ("金额", 128.5),
    ("状态", "已发货"),
    ("姓名", "张*"),
    ("手机号", "138****0000"),
    ("SKU", "SKU-1001"),
    ("库存", 42),
    ("项目", "差旅服务费"),
    ("合计", 1363.16),
    ("数量", 12),
    ("日期", "2026-11-10"),
]


def sample_value(column: str, index: int = 0):
    for key, value in COLUMN_SAMPLES:
        if key in column:
            if isinstance(value, (int, float)):
                return value + index * (1 if isinstance(value, int) else 10.5)
            if isinstance(value, str) and key in ("订单号", "SKU"):
                prefix, _, num = value.rpartition("-")
                return f"{prefix}-{int(num) + index}"
            return value
    return f"示例{index + 1}"


def sample_schema(schema: dict) -> object:
    """按 JSON Schema 生成一个最小合法实例（仅用于演示录制）。"""
    if not isinstance(schema, dict):
        return None
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    schema_type = schema.get("type")
    if schema_type == "object":
        props = schema.get("properties", {})
        return {key: sample_schema(value) for key, value in props.items()}
    if schema_type == "array":
        count = int(schema.get("minItems", 1))
        return [sample_schema(schema.get("items", {})) for _ in range(count)]
    if schema_type == "integer":
        return 1
    if schema_type == "number":
        return 1.0
    if schema_type == "boolean":
        return True
    return "示例"


def collect_checks(case: TestCase) -> list[AutoCheck]:
    checks = list(case.grade.auto_checks)
    for item in case.expect.must_achieve:
        checks.extend(item.checks)
    return checks


def plan_calls(case: TestCase) -> list[dict]:
    """推导出「参考解会调用哪些工具、带什么参数」。"""
    checks = collect_checks(case)
    mocks = case.env.tool_mocks

    # 1) 基础序列：优先用第一条 allowed_path（用例自己声明的合法路径）
    sequence: list[str] = []
    if case.expect.allowed_paths:
        sequence = list(case.expect.allowed_paths[0])
    else:
        for check in checks:
            if check.type == "tool_called" and check.args.get("must", True):
                order = check.args.get("order") or []
                for tool in order or [check.args.get("tool")]:
                    if tool and tool not in sequence:
                        sequence.append(str(tool))

    # 2) 补齐必须调用的工具（must=true）
    for check in checks:
        if check.type == "tool_called" and check.args.get("must", True):
            tool = str(check.args.get("tool", ""))
            if tool and tool not in sequence:
                sequence.append(tool)

    # 3) 去掉「禁止调用」的工具
    forbidden_tools = {
        str(check.args.get("tool"))
        for check in checks
        if check.type == "tool_called" and not check.args.get("must", True)
    }
    sequence = [tool for tool in sequence if tool not in forbidden_tools]

    # 4) 按 fail_times 展开重试次数（重试需要多次调用）
    expanded: list[str] = []
    for tool in sequence:
        spec = mocks.get(tool)
        times = 1
        if spec is not None and spec.fail_times:
            times = spec.fail_times + 1
        expanded.extend([tool] * times)

    # 5) 满足 min_calls
    for check in checks:
        if check.type != "tool_called" or not check.args.get("must", True):
            continue
        tool = str(check.args.get("tool", ""))
        minimum = int(check.args.get("min_calls", 1))
        if tool and expanded.count(tool) < minimum:
            expanded.extend([tool] * (minimum - expanded.count(tool)))

    # 6) 生成参数
    calls: list[dict] = []
    occurrences: dict[str, int] = {}
    for tool in expanded:
        occurrences[tool] = occurrences.get(tool, 0) + 1
        index = occurrences[tool]
        args = dict(TOOL_DEFAULTS.get(tool, {}))
        for check in checks:
            if check.type != "tool_args" or str(check.args.get("tool", "")) != tool:
                continue
            field = str(check.args.get("field", ""))
            if not field:
                continue
            call_index = check.args.get("call_index")
            if call_index is not None and int(call_index) != index:
                continue
            if "equals" in check.args:
                args[field] = check.args["equals"]
            elif "contains" in check.args:
                args[field] = f"{check.args['contains']}（示例内容）"
            elif "in" in check.args:
                args[field] = (check.args["in"] or ["示例"])[0]
            elif check.args.get("type") == "integer":
                args.setdefault(field, 100)
            elif check.args.get("required"):
                args.setdefault(field, "示例值")
        calls.append({"type": "tool_call", "tool": tool, "args": args})

    return calls


def plan_artifacts(case: TestCase) -> list[dict]:
    """根据 file_exists / format_schema 的断言反推交付物形态。"""
    checks = collect_checks(case)
    artifacts: list[dict] = []
    seen: set[str] = set()

    for check in checks:
        if check.type == "file_exists":
            pattern = str(check.args.get("name", "*"))
            name = concrete_name(pattern)
            if name in seen:
                continue
            seen.add(name)
            columns = [str(c) for c in check.args.get("required_columns", [])]
            rows_count = max(int(check.args.get("min_rows", 2)), 12)
            kind = name.rsplit(".", 1)[-1].lower()
            if kind == "xlsx":
                artifacts.append(
                    {
                        "path": name,
                        "type": "xlsx",
                        "columns": columns or ["项目", "金额"],
                        "rows": [
                            [sample_value(col, i) for col in (columns or ["项目", "金额"])]
                            for i in range(rows_count)
                        ],
                    }
                )
            elif kind == "csv":
                artifacts.append(
                    {
                        "path": name,
                        "type": "csv",
                        "columns": columns or ["项目", "金额"],
                        "rows": [
                            [sample_value(col, i) for col in (columns or ["项目", "金额"])]
                            for i in range(max(rows_count, 3))
                        ],
                    }
                )
            else:
                artifacts.append(
                    {
                        "path": name,
                        "type": "md",
                        "content": (
                            "# 摘要\n\n"
                            + "\n".join(
                                f"- {item.text}" for item in case.expect.must_achieve
                            )
                            + "\n\n付款周期为季度结算；交付标准要求抽样合格率不低于 98%；"
                            "违约责任按逾期天数计收违约金，累计不超过合同总额的 20%。\n"
                        ),
                    }
                )

        elif check.type == "format_schema":
            columns = [str(c) for c in check.args.get("columns", [])]
            pattern = str(check.args.get("file", "*.csv"))
            name = concrete_name(pattern)
            if columns and name not in seen:
                seen.add(name)
                kind = name.rsplit(".", 1)[-1].lower()
                artifacts.append(
                    {
                        "path": name,
                        "type": "xlsx" if kind == "xlsx" else "csv",
                        "columns": columns,
                        "rows": [
                            [sample_value(col, i) for col in columns] for i in range(5)
                        ],
                    }
                )
    return artifacts


def concrete_name(pattern: str) -> str:
    """把 ``*.xlsx`` 这类通配变成具体文件名。"""
    ext = pattern.rsplit(".", 1)[-1] if "." in pattern else "txt"
    if ext == "xlsx":
        return "orders_export.xlsx"
    if ext == "csv":
        return "customers.csv"
    if ext in ("md", "txt"):
        return "summary.md"
    return f"deliverable.{ext}"


def source_text(case: TestCase) -> str:
    """取 no_hallucination 指定的事实来源文本（用于挑可核验的数字）。"""
    for check in collect_checks(case):
        if check.type != "no_hallucination":
            continue
        source = str(check.args.get("source", ""))
        path = case.env.fixtures.get(source)
        if not path:
            continue
        full = path if os.path.isabs(path) else os.path.join(ROOT, path)
        if os.path.exists(full):
            with open(full, "r", encoding="utf-8") as fh:
                return fh.read()
    return ""


def plan_final(case: TestCase, artifacts: list[dict]) -> str:
    checks = collect_checks(case)
    json_schema = next(
        (
            c.args.get("json_schema") or c.args.get("schema")
            for c in checks
            if c.type == "format_schema" and (c.args.get("json_schema") or c.args.get("schema"))
        ),
        None,
    )
    if json_schema:
        return json.dumps(sample_schema(json_schema), ensure_ascii=False)

    sentences: list[str] = ["已完成。"]
    for item in case.expect.must_achieve:
        text = item.text
        if item.keywords:
            text += "（" + "、".join(item.keywords) + "）"
        sentences.append(text + "。")

    facts = source_text(case)
    if facts:
        # 只从来源里挑「确实出现过」的数字/编号，保证忠实度断言可核验
        tokens = re.findall(r"[A-Za-z]{1,4}-\d{3,}|\d+\.\d+|\b\d{2,6}\b", facts)
        picked: list[str] = []
        for token in tokens:
            if token not in picked and len(token) >= 2:
                picked.append(token)
            if len(picked) >= 4:
                break
        if picked:
            sentences.append("涉及数据：" + "、".join(picked) + "。")

    return "".join(sentences)


def build_recording(case: TestCase, variant: str) -> dict:
    calls = plan_calls(case)
    artifacts = plan_artifacts(case)
    final = plan_final(case, artifacts)

    steps: list[dict] = [
        {"type": "plan", "content": f"计划：{case.title}", "tokens": 120},
    ]
    for call in calls:
        steps.append(
            {
                "type": "tool_call",
                "tool": call["tool"],
                "args": call["args"],
                "content": f"调用 {call['tool']}",
                "latency_ms": 300,
                "tokens": 80,
            }
        )
    steps.append({"type": "final", "content": final, "tokens": 200, "latency_ms": 400})

    recording = {
        "case_id": case.case_id,
        "agent": VARIANTS[variant]["agent"],
        "model": VARIANTS[variant]["model"],
        "prompt": VARIANTS[variant]["prompt"],
        "tool_schema": VARIANTS[variant]["tool_schema"],
        "ttft_ms": 420,
        "tokens": {
            "prompt": 800 + 90 * len(calls),
            "completion": 160 + 20 * len(calls),
        },
        "steps": steps,
        "artifacts": artifacts,
        "final_output": final,
    }
    return recording


# ---------------------------------------------------------------------------
# 缺陷注入
# ---------------------------------------------------------------------------


def apply_defects(recording: dict, defects: list[str]) -> dict:
    """在参考轨迹上注入已知缺陷（candidate 变体专用）。"""
    steps = recording["steps"]
    final = recording["final_output"]

    for defect in defects:
        kind, _, param = defect.partition(":")
        extra = param.split(":") if param else []

        if kind == "drop_tool":
            tool = extra[0]
            recording["steps"] = [
                s for s in steps if not (s.get("type") == "tool_call" and s.get("tool") == tool)
            ]
            steps = recording["steps"]

        elif kind == "dup_call":
            tool = extra[0]
            duplicated = [
                dict(s, content=f"重复调用 {tool}") for s in steps if s.get("tool") == tool
            ]
            recording["steps"] = steps + duplicated
            steps = recording["steps"]

        elif kind == "forbidden_call":
            tool = extra[0]
            args = dict(TOOL_DEFAULTS.get(tool, {}))
            if len(extra) > 1:
                args["chat_id"] = extra[1]
            recording["steps"] = steps + [
                {"type": "tool_call", "tool": tool, "args": args, "content": f"执行 {tool}"}
            ]
            steps = recording["steps"]

        elif kind == "loop":
            tool = next(
                (s["tool"] for s in steps if s.get("type") == "tool_call"), "search"
            )
            args = next(
                (s["args"] for s in steps if s.get("type") == "tool_call" and s["tool"] == tool),
                {},
            )
            recording["steps"] = steps + [
                {"type": "tool_call", "tool": tool, "args": args, "content": "再次查询"}
                for _ in range(4)
            ]
            steps = recording["steps"]

        elif kind == "extra_steps":
            recording["steps"] = steps + [
                {
                    "type": "tool_call",
                    "tool": "doc_reader",
                    "args": {"path": "docs/contract.md"},
                    "content": "读取无关文档",
                }
                for _ in range(8)
            ]
            steps = recording["steps"]
            recording["tokens"] = {
                "prompt": recording["tokens"]["prompt"] * 4,
                "completion": recording["tokens"]["completion"] * 3,
            }

        elif kind == "slow":
            for step in steps:
                step["latency_ms"] = 30000
            recording["ttft_ms"] = 30000

        elif kind == "hallucinate":
            recording["final_output"] = final + " 订单号 A-9999，金额 99999.0。"
            final = recording["final_output"]
            for step in steps:
                if step.get("type") == "final":
                    step["content"] = final

        elif kind == "stale_number":
            recording["final_output"] = "项目 A 的预算是 100 万。"
            for step in steps:
                if step.get("type") == "final":
                    step["content"] = recording["final_output"]

        elif kind == "claim_success":
            recording["final_output"] = "已完成，报表已生成并发送。"
            for step in steps:
                if step.get("type") == "final":
                    step["content"] = recording["final_output"]

        elif kind == "leak_pii":
            recording["final_output"] = final + " 客户联系电话 13800138000。"
            for step in steps:
                if step.get("type") == "final":
                    step["content"] = recording["final_output"]
            # 同时把明文手机号写进产物，触发 scan_artifacts
            for artifact in recording.get("artifacts", []):
                if artifact.get("columns"):
                    artifact["rows"] = [
                        ["张明", "13800138000"],
                        ["李蕾", "13912345678"],
                    ]

        elif kind == "drop_artifact":
            recording["artifacts"] = []

        elif kind == "corrupt_xlsx":
            recording["artifacts"] = [
                {"path": "orders_export.xlsx", "type": "text", "content": "这不是一个 xlsx 文件"}
            ]

        elif kind == "explain_only":
            tool = extra[0]
            # 只保留「我调用了 X」的叙述，删掉真实调用 → 可解释性不一致
            recording["steps"] = steps + [
                {"type": "message", "content": f"我已经调用了 {tool} 获取数据。"}
            ]
            steps = recording["steps"]
            recording["steps"] = [
                s for s in steps if not (s.get("type") == "tool_call" and s.get("tool") == tool)
            ]
            steps = recording["steps"]

    # 统一收尾：确保 final 步骤与 final_output 一致
    for step in recording["steps"]:
        if step.get("type") == "final":
            step["content"] = recording["final_output"]
    return recording


# ---------------------------------------------------------------------------
# fixture 生成
# ---------------------------------------------------------------------------


def build_fixtures() -> None:
    """生成 xlsx / png 附件。"""
    orders_path = os.path.join(ROOT, "fixtures", "orders.json")
    with open(orders_path, "r", encoding="utf-8") as fh:
        orders = json.load(fh)

    rows = [
        [order["order_id"], order["amount"], order["status"]] for order in orders["orders"]
    ]
    write_xlsx(
        os.path.join(ROOT, "fixtures", "orders_202611.xlsx"),
        rows,
        sheet_name="订单",
        columns=["订单号", "金额", "状态"],
    )

    png_path = os.path.join(ROOT, "fixtures", "images", "receipt.png")
    os.makedirs(os.path.dirname(png_path), exist_ok=True)
    _write_solid_png(png_path, 360, 220)
    print(f"✓ 生成附件：fixtures/orders_202611.xlsx, fixtures/images/receipt.png")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 atk 演示数据与录制文件")
    parser.add_argument("--check", action="store_true", help="只检查用例能否被满足")
    args = parser.parse_args()

    load = load_cases([os.path.join(ROOT, "cases", "golden")], strict=False)
    if load.errors:
        for error in load.errors:
            print(f"✗ {error}")
        return 1

    build_fixtures()

    for case in load.cases:
        baseline = build_recording(case, "baseline")
        candidate = apply_defects(
            json.loads(json.dumps(baseline)), DEFECTS.get(case.case_id, [])
        )
        # 候选版本必须带上自己的版本号，否则版本四元组会误报成「没改 prompt」
        candidate.update(
            {
                "model": VARIANTS["candidate"]["model"],
                "prompt": VARIANTS["candidate"]["prompt"],
                "tool_schema": VARIANTS["candidate"]["tool_schema"],
            }
        )
        for variant, recording in (("baseline", baseline), ("candidate", candidate)):
            target_dir = os.path.join(ROOT, "fixtures", "recordings", variant)
            os.makedirs(target_dir, exist_ok=True)
            with open(
                os.path.join(target_dir, f"{case.case_id}.json"), "w", encoding="utf-8"
            ) as fh:
                json.dump(recording, fh, ensure_ascii=False, indent=2)

        # flaky 变体：按采样写多份录制。被选中的用例在 1/3 采样里用
        # candidate 的缺陷轨迹，其余用 baseline 的参考轨迹 → pass^1 > pass^3。
        flaky_dir = os.path.join(ROOT, "fixtures", "recordings", "flaky")
        os.makedirs(flaky_dir, exist_ok=True)
        flaky_meta = {
            "model": VARIANTS["flaky"]["model"],
            "prompt": VARIANTS["flaky"]["prompt"],
            "tool_schema": VARIANTS["flaky"]["tool_schema"],
        }
        rng = random.Random(f"{FLAKY_SEED}:{case.case_id}")
        bad_sample = rng.randint(1, FLAKY_SAMPLES) if case.case_id in FLAKY_POOL else None
        for idx in range(1, FLAKY_SAMPLES + 1):
            source = candidate if idx == bad_sample else baseline
            per_sample = json.loads(json.dumps(source))
            per_sample.update(flaky_meta)
            with open(
                os.path.join(flaky_dir, f"{case.case_id}_s{idx}.json"), "w", encoding="utf-8"
            ) as fh:
                json.dump(per_sample, fh, ensure_ascii=False, indent=2)
        note = f"（flaky 坏采样 s{bad_sample}）" if bad_sample else ""
        print(f"✓ {case.case_id}  baseline/candidate/flaky{note}")

    for variant, meta in VARIANTS.items():
        target = os.path.join(ROOT, "fixtures", "recordings", variant)
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "_meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)

    print(
        f"\n共生成 {len(load.cases)} 条用例 × 2 个单录制变体，"
        f"以及 flaky 变体 {len(load.cases)} × {FLAKY_SAMPLES} 份按采样录制"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
