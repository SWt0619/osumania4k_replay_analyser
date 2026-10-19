"""统计工具：全部手写（不依赖 numpy / scipy），便于测试与解释。

这里的每个函数都对应一个可解释的统计量，报告里出现某个结论时能追溯到它。
只保留实际被分析链路用到的部分——多留一个"也许以后有用"的函数，
下次改口径时就多一处需要同步的地方。
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass


# --------------------------------------------------------------------------- 基础


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def stdev(values: list[float], population: bool = True) -> float:
    """标准差。默认按总体（除以 n）—— osu 的 UR 口径即如此。"""
    n = len(values)
    if n < 2:
        return 0.0
    mu = mean(values)
    var = sum((v - mu) ** 2 for v in values) / (n if population else (n - 1))
    return math.sqrt(max(0.0, var))


def unstable_rate(deltas: list[float]) -> float:
    """UR = 误差标准差 × 10（osu 的 unstable rate 口径）。"""
    return stdev(deltas) * 10.0


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * max(0.0, min(1.0, p))
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


# --------------------------------------------------------------------------- 游程检验


@dataclass
class RunsTest:
    """Wald–Wolfowitz 游程检验：判断"失误是否成簇"。

    把完整对局按时间切成 0/1 序列（1=失误），统计连续段（游程）数量 R。
    若 R 显著少于期望，说明失误聚集成簇，而不是均匀随机分布 —— 这是"分神"
    这类突发性失误的关键证据。
    """

    runs: int
    expected: float
    z: float
    p_two_sided: float
    clustered: bool

    def as_dict(self) -> dict:
        return {
            "runs": self.runs,
            "expected": round(self.expected, 2),
            "z": round(self.z, 2),
            "p": round(self.p_two_sided, 5),
            "clustered": self.clustered,
        }


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def runs_test(bits: list[int]) -> RunsTest:
    """bits: 0/1 序列（按时间排序）。"""
    n = len(bits)
    n1 = sum(1 for b in bits if b)
    n0 = n - n1
    # 正态近似只在两类计数都 > 10 时可靠（NIST 1.3.5.13）；样本太少直接判"无结论"
    if n < 24 or n1 <= 10 or n0 <= 10:
        return RunsTest(0, 0.0, 0.0, 1.0, False)

    runs = 1
    for i in range(1, n):
        if bits[i] != bits[i - 1]:
            runs += 1

    expected = 1.0 + (2.0 * n1 * n0) / n
    var = (2.0 * n1 * n0 * (2.0 * n1 * n0 - n)) / (n * n * (n - 1))
    if var <= 1e-9:
        return RunsTest(runs, expected, 0.0, 1.0, False)
    z = (runs - expected) / math.sqrt(var)
    p = 2.0 * (1.0 - _norm_cdf(abs(z)))
    return RunsTest(runs, expected, z, p, clustered=(z < -1.64 and p < 0.10))


# --------------------------------------------------------------------------- 分布 / 分箱


def histogram(values: list[float], lo: float, hi: float, bins: int) -> list[int]:
    out = [0] * bins
    if not values or hi <= lo:
        return out
    width = (hi - lo) / bins
    for v in values:
        idx = int((v - lo) / width)
        if idx < 0:
            idx = 0
        if idx >= bins:
            idx = bins - 1
        out[idx] += 1
    return out


def quantile_edges(values: list[float], n_bins: int) -> list[float]:
    """按分位数切出等量的桶边界（去重，避免大量重复值导致空桶）。"""
    if not values:
        return []
    edges = [percentile(values, i / n_bins) for i in range(1, n_bins)]
    out = []
    for e in edges:
        if not out or e > out[-1] + 1e-9:
            out.append(e)
    return out


def bucketize(value: float, edges: list[float]) -> int:
    """返回 value 落在第几个桶（edges 为内部边界）。"""
    return bisect.bisect_right(edges, value)


@dataclass
class BucketStat:
    low: float
    high: float
    count: int
    errors: int

    @property
    def error_rate(self) -> float:
        return (self.errors / self.count) if self.count else 0.0


def density_error_profile(
    samples: list[tuple[float, bool]], n_bins: int = 6, min_count: int = 12
) -> list[BucketStat]:
    """samples: [(局部密度, 是否失误)]，按密度分箱统计错误率。

    这是判断"超出能力范围"的核心数据：如果错误率随密度单调上升、且在高密度箱
    明显跳升，说明玩家撞到了自己的速度上限。
    """
    if len(samples) < min_count * 2:
        return []
    densities = [s[0] for s in samples]
    edges = quantile_edges(densities, n_bins)
    if not edges:
        return []
    buckets: dict[int, list[bool]] = {}
    for d, err in samples:
        idx = bucketize(d, edges)
        buckets.setdefault(idx, []).append(err)
    lows = [min(densities)] + edges
    highs = edges + [max(densities)]
    stats: list[BucketStat] = []
    for i in sorted(buckets):
        seg = buckets[i]
        stats.append(BucketStat(low=lows[i], high=highs[i], count=len(seg), errors=sum(1 for s in seg if s)))
    return stats


def trend_significance(profile: list[BucketStat]) -> dict:
    """粗略判断"错误率是否随密度上升"。

    用相邻桶错误率差 + 端点对比，给出一个 0..1 的"陡峭度"评分，避免引入回归库。
    """
    usable = [b for b in profile if b.count >= 5]
    if len(usable) < 3:
        return {"slope_score": 0.0, "monotonic": False, "jump": 0.0}
    rates = [b.error_rate for b in usable]
    first, last = rates[0], rates[-1]
    steps = [rates[i + 1] - rates[i] for i in range(len(rates) - 1)]
    rising = sum(1 for s in steps if s > 0)
    base = max(1e-6, first)
    return {
        "slope_score": clamp((last - first) / max(0.05, 0.5), 0.0, 1.0),
        "monotonic": rising >= len(steps) - 1,
        "jump": last - first,
        "ratio": last / base if base > 0 else 0.0,
        "rates": [round(r, 4) for r in rates],
    }
