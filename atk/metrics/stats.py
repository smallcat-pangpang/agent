"""统计工具：pass^k、Wilson 区间、显著性检验、MDE、Kappa。

规范 3.3 的要求逐条落地：

- 成功率必须说清是「单次」还是 ``pass^k``；
- 置信区间用 Wilson，**不用**正态近似（小样本会给出负下限）；
- 版本对比必须做显著性检验，结论要写「提升 3%（95%CI −2%~+8%，不显著）」；
- 样本量规划用 MDE（最小可检测差异）。

全部用标准库实现，结果可复核；没有引入 scipy/numpy。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Sequence


# ---------------------------------------------------------------------------
# 基础描述统计
# ---------------------------------------------------------------------------


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def percentile(values: Sequence[float], q: float) -> float:
    """线性插值分位数（q 取 0~1）。"""
    if not values:
        return 0.0
    data = sorted(values)
    if len(data) == 1:
        return float(data[0])
    pos = (len(data) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(data[lo])
    return float(data[lo] + (data[hi] - data[lo]) * (pos - lo))


def p95(values: Sequence[float]) -> float:
    return percentile(values, 0.95)


def stdev(values: Sequence[float]) -> float:
    """样本标准差（n-1 分母）。"""
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


# ---------------------------------------------------------------------------
# pass^k：非确定性场景的核心指标
# ---------------------------------------------------------------------------


def pass_at_k(successes: int, samples: int, k: int) -> float:
    """单用例的 ``pass^k`` 无偏估计：``C(c, k) / C(n, k)``。

    含义：跑 n 次、成功 c 次，随机抽 k 次「全都成功」的概率。
    n=3 时 pass^3 就是「三次全对」，比 pass@1 严苛得多，能暴露不稳定。
    """
    if k <= 0 or samples <= 0 or k > samples:
        return 0.0
    if successes < k:
        return 0.0
    return math.comb(successes, k) / math.comb(samples, k)


def pass_hat_k(case_results: dict[str, tuple[int, int]], k: int) -> float:
    """总体 ``Pass^k = mean(p_k over cases)``。

    Args:
        case_results: ``{case_id: (成功次数, 采样次数)}``。
        k: k 次全过才算通过。

    Returns:
        0~1 的总体通过率；没有用例时返回 0。
    """
    if not case_results:
        return 0.0
    values = [pass_at_k(c, n, k) for c, n in case_results.values()]
    return mean(values)


def pass_k_table(case_results: dict[str, tuple[int, int]], max_k: int = 5) -> dict[int, float]:
    """一次算出 pass^1 ~ pass^max_k，报告里的「稳定性阶梯」。"""
    return {k: round(pass_hat_k(case_results, k), 4) for k in range(1, max_k + 1)}


# ---------------------------------------------------------------------------
# Wilson 置信区间
# ---------------------------------------------------------------------------


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 得分区间。

    小样本（n<30）时正态近似会给出负下限，Wilson 不会；
    这也是规范明确要求用它的原因。
    """
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return (max(0.0, center - margin), min(1.0, center + margin))


def diff_ci_wilson(
    s1: int, n1: int, s2: int, n2: int, z: float = 1.96
) -> tuple[float, float]:
    """两个比例的差的置信区间（Newcombe 混合 Wilson 法）。

    比「直接相减」严谨：能直接回答「这次退化是不是噪声」。
    """
    p1 = s1 / n1 if n1 else 0.0
    p2 = s2 / n2 if n2 else 0.0
    l1, u1 = wilson_ci(s1, n1, z)
    l2, u2 = wilson_ci(s2, n2, z)
    delta = p1 - p2
    lower = delta - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    upper = delta + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return (max(-1.0, lower), min(1.0, upper))


# ---------------------------------------------------------------------------
# 显著性检验
# ---------------------------------------------------------------------------


@dataclass
class ProportionTest:
    """两个比例差异的检验结果。"""

    delta: float
    ci_low: float
    ci_high: float
    p_value: float
    method: str
    significant: bool
    n1: int
    n2: int

    def describe(self) -> str:
        """规范要求的结论表述：带置信区间与显著性，而不是裸百分比。"""
        mark = "显著" if self.significant else "不显著"
        return (
            f"{self.delta:+.1%}（95%CI {self.ci_low:+.1%} ~ {self.ci_high:+.1%}，"
            f"{mark}，{self.method}），n={self.n1}/{self.n2}"
        )


def _log_hypergeom(a: int, b: int, c: int, d: int) -> float:
    """超几何分布的精确对数概率（Fisher 精确检验用）。"""
    n = a + b + c + d
    return (
        math.lgamma(a + b + 1)
        + math.lgamma(c + d + 1)
        + math.lgamma(a + c + 1)
        + math.lgamma(b + d + 1)
        - math.lgamma(n + 1)
        - math.lgamma(a + 1)
        - math.lgamma(b + 1)
        - math.lgamma(c + 1)
        - math.lgamma(d + 1)
    )


def fisher_exact(s1: int, n1: int, s2: int, n2: int) -> float:
    """Fisher 精确检验（双尾）的 p 值。

    表格::

               成功   失败
        组1     s1   n1-s1
        组2     s2   n2-s2
    """
    a, b = s1, n1 - s1
    c, d = s2, n2 - s2
    if min(a, b, c, d) < 0:
        return 1.0
    row1, col1, total = a + b, a + c, a + b + c + d
    if total == 0:
        return 1.0
    observed = _log_hypergeom(a, b, c, d)
    p_obs = math.exp(observed)
    p_value = 0.0
    lo = max(0, row1 - (total - col1))
    hi = min(row1, col1)
    for x in range(lo, hi + 1):
        p = math.exp(_log_hypergeom(x, row1 - x, col1 - x, total - col1 - row1 + x))
        # 数值容差，避免浮点误差漏掉与观测值等概率的表
        if p <= p_obs * 1.0000001:
            p_value += p
    return min(1.0, p_value)


def _gammaincc(a: float, x: float) -> float:
    """正则化上不完全伽马函数 Q(a, x)，用于卡方分布尾概率。"""
    if x < 0 or a <= 0:
        return 1.0
    if x == 0:
        return 1.0
    if x < a + 1:
        # 级数展开计算 P(a,x)，再用 1-P
        term = 1.0 / a
        total = term
        ap = a
        for _ in range(500):
            ap += 1
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-12:
                break
        return max(0.0, 1.0 - total * math.exp(-x + a * math.log(x) - math.lgamma(a)))
    # 连分式（Lentz 法）
    tiny = 1e-300
    b = x + 1 - a
    c = 1 / tiny
    d = 1 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-12:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def chi2_sf(stat: float, df: int = 1) -> float:
    """卡方分布尾概率。"""
    if stat <= 0:
        return 1.0
    return _gammaincc(df / 2.0, stat / 2.0)


def compare_proportions(
    s1: int, n1: int, s2: int, n2: int, z: float = 1.96
) -> ProportionTest:
    """比较两个比例（组1 - 组2），自动选 Fisher 或卡方。

    选型规则：样本量小（n<40）或任一格期望频数 <5 → Fisher 精确检验；
    否则用带连续性校正的卡方检验。
    """
    if n1 <= 0 or n2 <= 0:
        return ProportionTest(0.0, 0.0, 0.0, 1.0, "无样本", False, n1, n2)

    p1, p2 = s1 / n1, s2 / n2
    delta = p1 - p2
    low, high = diff_ci_wilson(s1, n1, s2, n2, z)

    a, b = s1, n1 - s1
    c, d = s2, n2 - s2
    total = n1 + n2
    expected = [
        (a + b) * (a + c) / total,
        (a + b) * (b + d) / total,
        (c + d) * (a + c) / total,
        (c + d) * (b + d) / total,
    ]
    use_fisher = total < 40 or min(expected) < 5

    if use_fisher:
        p_value = fisher_exact(s1, n1, s2, n2)
        method = "Fisher 精确检验"
    else:
        # 带 Yates 校正的卡方
        num = total * (abs(a * d - b * c) - total / 2) ** 2
        denom = (a + b) * (c + d) * (a + c) * (b + d)
        stat = num / denom if denom else 0.0
        p_value = chi2_sf(stat, 1)
        method = "卡方检验(Yates)"

    # 显著 = p<0.05 且置信区间不跨 0
    significant = p_value < 0.05 and (low > 0 or high < 0)
    return ProportionTest(
        delta=round(delta, 4),
        ci_low=round(low, 4),
        ci_high=round(high, 4),
        p_value=round(p_value, 6),
        method=method,
        significant=significant,
        n1=n1,
        n2=n2,
    )


def mde(p: float = 0.8, n: int = 50, z: float = 1.96, power_z: float = 0.84) -> float:
    """最小可检测差异 ``MDE ≈ (z + z_power) * sqrt(2p(1-p)/n)``。

    默认 z=1.96、power=0.8 时系数约 2.8 —— 与规范给的速算一致。
    n=50、p=0.8 时约 15%，意思是：这个样本量下，「差 10%」根本测不出来，
    不要拿它做发布决策。
    """
    if n <= 0:
        return 1.0
    return (z + power_z) * math.sqrt(2 * p * (1 - p) / n)


def required_sample_size(
    p: float = 0.8, target_mde: float = 0.05, z: float = 1.96, power_z: float = 0.84
) -> int:
    """反解：要检测出 ``target_mde`` 的差异，每组至少需要多少用例。"""
    if target_mde <= 0:
        return 0
    return math.ceil(((z + power_z) ** 2) * 2 * p * (1 - p) / (target_mde**2))


# ---------------------------------------------------------------------------
# 相关性与一致性
# ---------------------------------------------------------------------------


def pearson(x: Sequence[float], y: Sequence[float]) -> float:
    """皮尔逊相关系数（Judge 长度偏差诊断用）。"""
    n = min(len(x), len(y))
    if n < 2:
        return 0.0
    mx, my = mean(x[:n]), mean(y[:n])
    num = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    den = math.sqrt(
        sum((x[i] - mx) ** 2 for i in range(n)) * sum((y[i] - my) ** 2 for i in range(n))
    )
    return num / den if den else 0.0


def _rank(values: Sequence[float]) -> list[float]:
    """平均秩（处理并列）。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    n = min(len(x), len(y))
    if n < 2:
        return 0.0
    return pearson(_rank(x[:n]), _rank(y[:n]))


def cohens_kappa(labels_a: Sequence[Any], labels_b: Sequence[Any]) -> float:
    """Cohen's Kappa：扣掉「碰巧一致」后的一致性。

    ``κ = (Po - Pe) / (1 - Pe)``。规范要求 Judge 与人工对齐 κ≥0.6 可用、≥0.8 良好。
    """
    n = min(len(labels_a), len(labels_b))
    if n == 0:
        return 0.0
    pairs = list(zip(labels_a[:n], labels_b[:n]))
    po = sum(1 for a, b in pairs if a == b) / n
    values = {a for a, _ in pairs} | {b for _, b in pairs}
    pe = sum(
        (sum(1 for a, _ in pairs if a == v) / n) * (sum(1 for _, b in pairs if b == v) / n)
        for v in values
    )
    if pe >= 1:
        return 1.0 if po >= 1 else 0.0
    return (po - pe) / (1 - pe)


def bootstrap_ci(
    values: Sequence[float], n_boot: int = 2000, alpha: float = 0.05, seed: int = 42
) -> tuple[float, float]:
    """均值差/连续指标的 bootstrap 置信区间（非参数，无需正态假设）。"""
    if not values:
        return (0.0, 1.0)
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(n_boot):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[int((1 - alpha / 2) * n_boot) - 1]
    return (round(lo, 6), round(hi, 6))


def flaky_rate(rounds: Sequence[Sequence[bool]]) -> float:
    """Flaky 率：同一批用例在多轮之间结果不一致的比例。

    Args:
        rounds: 每一轮每个用例的通过与否。

    Returns:
        至少切换过一次结果的用例占全部用例的比例。
    """
    total = min((len(r) for r in rounds), default=0)
    if len(rounds) < 2 or total == 0:
        return 0.0
    unstable = 0
    for i in range(total):
        outcomes = {bool(r[i]) for r in rounds}
        if len(outcomes) > 1:
            unstable += 1
    return unstable / total
