"""长条段的写法分类：release / density / coordination / mixed。

名字取 osu! wiki 的谱面标签（`style/LN release` / `density` / `coordination` / `mixed`），
但判定按**官方那句话的字面意思**做，不照抄社区工具的权重——那套公式里
「跨列重叠」几乎在所有长条图上都接近 100%，结果全图都被判成 coordination。

    density       "dense long note streams without breaks"
                  → 长条头一个接一个（间隔 ≤ 半拍）+ 长条覆盖的时间占比高

    release       "different long note holds ending"
                  → 错位尾巴（同刻结束、起点不同）+ 同起异终 + 短长条（松手要卡点）

    coordination  "holding multiple long notes simultaneously while hitting other patterns"
                  → 重点是"按住多根的同时还要打米"：同时按住 ≥2 根长条的时段里，
                    有多少米落在其中

三个分数各算各的，取最大；最大与次大拉不开差距报 mixed。
"""

from __future__ import annotations

from collections import defaultdict

# 三个池的名字
POOLS = ("release", "density", "coordination")

# 对外用的键名带 ln 前缀：段落的粗分类是 ln，细分不能跟 RC 的键型撞名
KEY_PREFIX = "ln"

# 块内长条少于这个数时不给结论：两三个长条算出来的比例纯属噪声
MIN_HOLDS = 3

# 长条占全部音符不到这个比例时也不给结论。
# 实测一张 2553 音符的纯米图只有 4 根四押长条，按 MIN_HOLDS 就会算出
# 「density 100%」——这种图在玩家眼里根本不是长条图，不该有 LN 写法。
MIN_LN_SHARE = 0.05

# 三个分量天然不在同一量纲上：长条图的长条头本来就连得密（density 中位 0.69），
# 而「错位尾巴」和「按住多根还要打米」在多数图里只占两三成（release 0.28 / coordination 0.22）。
# 直接比大小的话 density 会永远赢，所以先各自除以自己的中位数把量纲拉平，
# 再比谁更突出。数值来自 40 张 LN 图的实测中位；参照集变化后应重新标定。
BASE = {"density": 0.69, "release": 0.28, "coordination": 0.22}

# 领先次名多少倍才算「这一种写法主导」。1.15 对应「比另一项突出 15%」，
# 低于它说明两三种写法在混用，按官方口径就是 mixed。
LEAD_RATIO = 1.15


def _holds(contexts) -> list:
    return [c for c in contexts if c.hold_end and c.hold_end > c.time]


def _beat_of(holds) -> float:
    return sum(c.beat_length for c in holds) / max(1, len(holds))


# 同一个「长条头行」的判定窗口。谱面时间戳一般对齐到毫秒，四押同时起手偶尔
# 会差 1~2ms（编辑器手放/程序生成），按 5ms 合并才不会把一次四押拆成四个事件。
ROW_TOLERANCE = 5.0


def _head_rows(holds) -> list[tuple[float, list[int]]]:
    """把长条头按时间合并成"行"，返回 [(起手时刻, [列…])]。

    为什么要合并：同一时刻四根长条起手，按单个长条算的话「头间隔」是 0，
    density 的连打比例会虚高到 1.0；而且和弦、和弦锁定这些量都要以"行"为单位。
    """
    rows: list[tuple[float, list[int]]] = []
    for c in sorted(holds, key=lambda x: x.time):
        if rows and c.time - rows[-1][0] <= ROW_TOLERANCE:
            rows[-1][1].append(c.column)
        else:
            rows.append((c.time, [c.column]))
    return rows


def density_score(contexts) -> float:
    """密度：长条头一个接一个，长条把时间轴铺满。"""
    holds = _holds(contexts)
    n = len(holds)
    if n < MIN_HOLDS:
        return 0.0
    beat = _beat_of(holds)

    # 头间隔按「行」算，不按单个长条算：四押同时起手是一行，不是一个 0ms 的间隔
    times = [t for t, _ in _head_rows(holds)]
    tight = sum(1 for i in range(1, len(times)) if times[i] - times[i - 1] <= beat / 2 + 1)
    tight_ratio = tight / max(1, len(times) - 1)

    # 覆盖率：四列各自被长条占住的时间比例，取平均。
    # 用列维度而不是整体跨度——同一时刻四个长条压满和只按一根，密度感受完全不同。
    span = max(c.hold_end for c in holds) - min(c.time for c in holds)
    by_col: dict[int, float] = defaultdict(float)
    for c in holds:
        by_col[c.column] += c.hold_end - c.time
    cover = (sum(by_col.values()) / (span * max(1, len(by_col)))) if span > 0 else 0.0

    return 0.6 * tight_ratio + 0.4 * min(1.0, cover)


def release_score(contexts) -> float:
    """释放：长条要在不同时机松手。"""
    holds = _holds(contexts)
    n = len(holds)
    if n < MIN_HOLDS:
        return 0.0
    beat = _beat_of(holds)
    short = beat / 4.0                      # 16 分音

    by_end: dict[float, list] = defaultdict(list)
    by_start: dict[float, list] = defaultdict(list)
    for c in holds:
        by_end[round(c.hold_end, 1)].append(c)
        by_start[round(c.time, 1)].append(c)

    # 错位尾巴：同一时刻结束、起点却不同 → 各根要分别卡松手
    staggered = 0
    for group in by_end.values():
        if len(group) >= 2 and len({round(g.time, 1) for g in group}) >= 2:
            staggered += len(group)
    # 同起异终：一起按下去、分开松
    split_end = 0
    for group in by_start.values():
        if len({round(g.hold_end, 1) for g in group}) >= 2:
            split_end += len(group)
    # 短长条：按住时间很短，松手窗口也窄
    taps = sum(1 for c in holds if c.hold_end - c.time <= short)

    return (0.5 * staggered / n + 0.3 * split_end / n + 0.2 * taps / n)


def coordination_score(contexts) -> float:
    """协调：按住多根长条的同时还要打米。"""
    holds = _holds(contexts)
    if len(holds) < MIN_HOLDS:
        return 0.0

    taps = [c for c in contexts if not (c.hold_end and c.hold_end > c.time)]
    if not taps:
        # 整段没有米：长条再多也谈不上"边按边打"，协调性无从体现
        return 0.0

    # 按时间点聚合「按住根数」的变化。同一时刻四押起手要一次加 4，
    # 逐根加会经过 1/2/3 这些中间状态，凭空切出几段只有一两根在按的时段。
    delta: dict[float, int] = defaultdict(int)
    for c in holds:
        delta[round(c.time, 1)] += 1
        delta[round(c.hold_end, 1)] -= 1

    segments: list[tuple[float, float, int]] = []   # (起, 止, 期间按住的根数)
    active = 0
    prev = None
    for t in sorted(delta):
        if prev is not None and active >= 2 and t > prev:
            segments.append((prev, t, active))
        active += delta[t]
        prev = t

    if not segments:
        return 0.0

    # 米落在「按住 k 根」期间值多少：2 根 1.0、3 根 1.5、4 根以上 1.5。
    # 按住四根再打米明显比两根吃协调，但不该线性放大——四根已经占满手指了。
    hit = 0.0
    for c in taps:
        for a, b, k in segments:
            if a <= c.time < b:
                hit += min(3, k) / 2.0
                break
    return min(1.0, hit / len(taps))


def overlay_events(contexts) -> int:
    """「起手时已经有长条在按」的次数——同一时刻的多根只算一次。

    首尾相接（前一根刚松手、后一根同刻起手）不算重叠，所以先结算结束再算开始；
    同一时刻四押起手也只记一次重叠，否则一根四押会让计数翻四倍。
    """
    holds = _holds(contexts)
    if not holds:
        return 0
    starts: dict[float, int] = defaultdict(int)
    ends: dict[float, int] = defaultdict(int)
    for c in holds:
        starts[round(c.time, 1)] += 1
        ends[round(c.hold_end, 1)] += 1

    count = 0
    active = 0
    for t in sorted(set(starts) | set(ends)):
        active -= ends.get(t, 0)
        if active >= 1 and starts.get(t):
            count += 1
        active += starts.get(t, 0)
    return count


def scores(contexts) -> dict:
    """三个池的分数（0~1）。长条太少、或长条占比太低时返回空字典。"""
    holds = _holds(contexts)
    if len(holds) < MIN_HOLDS:
        return {}
    if len(holds) / float(max(1, len(contexts))) < MIN_LN_SHARE:
        return {}
    return {
        "release": release_score(contexts),
        "density": density_score(contexts),
        "coordination": coordination_score(contexts),
    }


def balanced(contexts) -> dict:
    """把三个分量除各自的基准中位数，得到「这条图的哪种写法更突出」。"""
    s = scores(contexts)
    if not s:
        return {}
    return {k: v / BASE[k] for k, v in s.items()}


def label_of(contexts) -> str:
    """这一段的写法：lnrelease / lndensity / lncoordination / lnmixed。

    长条太少、或者三种写法咬得太紧，都返回 lnmixed——官方对 mixed 的定义就是
    「多种长条排列风格混用」，勉强挑一个最高分反而误导。
    """
    rel = balanced(contexts)
    if not rel:
        return KEY_PREFIX + "mixed"
    ranked = sorted(rel.items(), key=lambda kv: -kv[1])
    top, second = ranked[0], ranked[1]
    if second[1] > 0 and top[1] / second[1] < LEAD_RATIO:
        return KEY_PREFIX + "mixed"
    return KEY_PREFIX + top[0]


def describe(contexts) -> dict:
    """给界面用：三个池的相对占比 + 结论。

    rows / overlay 是诊断用的原始量（长条头行数、重叠次数），界面上暂时不显示，
    但排查分类问题时比百分比有用。
    """
    rel = balanced(contexts)
    if not rel:
        return {}
    total = sum(rel.values()) or 1.0
    holds = _holds(contexts)
    return {
        "label": label_of(contexts),
        "release": round(100 * rel["release"] / total),
        "density": round(100 * rel["density"] / total),
        "coordination": round(100 * rel["coordination"] / total),
        "holds": len(holds),
        "rows": len(_head_rows(holds)),
        "overlay": overlay_events(contexts),
    }
