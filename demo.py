"""一键演示：Judge 校准 → 基线 → 候选 → 门禁 → 框架自检。

PyCharm 里右键运行本文件即可，不需要任何参数、不需要网络。
它等价于 README「五分钟跑通」里的 ②~⑤ 步，只是想省去手动敲命令。

注意：演示用的 baseline/candidate 是**参考轨迹录制**，
用于验证流水线本身（门禁能拦住已知 bug），不代表真实模型能力。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
# 用例、fixture、reports 都按项目根目录解析，先固定工作目录
os.chdir(ROOT)

from atk.cli import main  # noqa: E402


def step(title: str, argv: list[str]) -> int:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
    try:
        return main(argv)
    except SystemExit as exc:  # 防御性：CLI 内部若 sys.exit，转成返回码
        return int(exc.code or 0)


def main_demo() -> int:
    results: dict[str, int] = {}

    results["校准 Judge"] = step(
        "① Judge 校准（Kappa / 混淆矩阵 / 偏差 / 反向红队）",
        ["calibrate", "--judge", "heuristic", "--set", "cases/calibration", "--out", "reports"],
    )
    results["基线版本"] = step(
        "② 跑基线版本（35 条用例 × 3 次采样）",
        ["run", "--profile", "all", "--agent", "replay:baseline", "--samples", "3", "--quiet"],
    )
    results["候选版本"] = step(
        "③ 跑候选版本（注入 17 处已知缺陷）",
        ["run", "--profile", "all", "--agent", "replay:candidate", "--samples", "3", "--quiet"],
    )
    results["框架自检"] = step(
        "④ 框架完备性自检（附录 A 20 项）",
        ["selfcheck", "--write", "docs/自检报告.md"],
    )

    print("\n" + "=" * 78)
    print("演示结束，各步骤返回码（0=通过，1=门禁阻断，符合预期）")
    print("=" * 78)
    for name, code in results.items():
        print(f"  {name:<10} {'✅ 通过' if code == 0 else '❌ 非零返回（阻断/未达标）'}({code})")
    print("\n产物：")
    print("  reports/runs/<run_id>/report.md / report.html   单次运行报告")
    print("  reports/dashboard.html                          版本 × 指标趋势看板")
    print("  reports/calibration_*/calibration.md            Judge 校准报告")
    print("  docs/自检报告.md                                 框架自检结果")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_demo())
