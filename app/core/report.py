"""把分析数据写成「人话」评价：等级、总评、亮点、短板、改进建议。

原则：
  * 每个结论都要有数据支撑，不给空话。
  * 建议要具体可做（降速多少、练什么 pattern、看哪一段）。
  * 不美化：差就是差，但要说清差在哪。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import dan
from .labels import CATEGORY_LABEL


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# 段位 → 练习倍率。高段位 0.9 太慢（读图靠速度记忆，慢到 0.9 反而对不上），
# 但 0.95 又几乎没降，所以 Alpha 以上给两档让用户自己挑：先用慢的过顺、再上快的收速度。
# Delta（14）及以上再把两档往上抬一点。
RATE_TIERS = (
    (14.0, (0.96, 0.92)),   # Delta 及以上
    (11.0, (0.95, 0.9)),    # Alpha ~ Gamma
)
DEFAULT_RATES = (0.9,)

# 成绩撑不住就降速、撑得住就提速。98.5% 是「这套已经打穿」的线：再降速只是重复劳动，
# 不如把余量用掉——注意这条线看的是 **v1 口径**，切到 v2 / classic 也不能跟着动。
FAST_ACC = 98.5
FAST_TIERS = (
    (14.0, (1.05,)),        # Delta 及以上只留 1.05，1.1 已经超出能练的范围
    (11.0, (1.05, 1.1)),    # Alpha ~ Gamma
)
FAST_DEFAULT = (1.1,)


def _practice_rates(payload: dict, dan_value: float | None = None,
                    acc_v1: float | None = None) -> list[float]:
    """练习谱面用哪几个倍率，从慢到快。

    `dan_value` 由前端把混合估算链（Sunny → Azusa → Roxy）的结果传上来——那才是
    页面上显示的段位。只有拿不到时才退回 MSD 换算：两套口径在个别图上能差半档到
    两段，用 MSD 顶替会在边界图上给出和页面不一致的倍率。
    """
    if dan_value is None:
        msd = payload.get("msd") or {}
        info = msd.get("dan")
        if not isinstance(info, dict):
            overall = _f(msd.get("overall"))
            info = dan.estimate(overall) if overall > 0 else {}
        raw = info.get("dan")
        dan_value = float(raw) if raw is not None else None
    fast = acc_v1 is not None and float(acc_v1) >= FAST_ACC
    if dan_value is None:
        return list(FAST_DEFAULT if fast else DEFAULT_RATES)
    for floor, rates in (FAST_TIERS if fast else RATE_TIERS):
        if float(dan_value) >= floor:
            return list(rates)
    return list(FAST_DEFAULT if fast else DEFAULT_RATES)


def _rates_text(rates: list[float]) -> str:
    """0.9 → "0.9"，[0.95, 0.9] → "0.9 / 0.95"（给正文用，从慢到快）。"""
    return " / ".join("%g" % r for r in sorted(rates))


def _grade(acc: float, ur: float, error_rate: float) -> tuple[str, str]:
    """返回 (等级, 等级说明)。"""
    if acc >= 99.5 and ur < 85 and error_rate < 0.6:
        return ("S", "几乎无可挑剔")
    if acc >= 98.5 and ur < 110:
        return ("A+", "非常扎实")
    if acc >= 96.5:
        return ("A", "整体很稳")
    if acc >= 92:
        return ("B", "基本掌握，还有明显提升空间")
    if acc >= 85:
        return ("C", "能打下来，但稳定性不足")
    return ("D", "这张图超出了你的能力范围")


def _stability_word(ur: float) -> str:
    if ur <= 0:
        return "无法计算"
    if ur < 80:
        return "极稳"
    if ur < 110:
        return "很稳"
    if ur < 145:
        return "中等偏稳"
    if ur < 190:
        return "有些飘"
    return "波动很大"


# 只有整局偏移超过这个值才动「改 offset」的念头。
#
# 理由很重要：单看一张图的偏差不足以断定是延迟问题 —— 一个人的手感、当天状态、
# 甚至这一张图的鼓点都能造成几毫秒的整体偏移。贸然按这张图去改 universal offset，
# 很可能在别的图上反而打得更偏。15ms 是个「几乎不可能是手感随机波动」的量级，
# 到了这个数才值得动手，且改完必须用另一张 2 分钟短图复核。
MIN_OFFSET_ADVICE_MS = 15.0


def _bias_sentence(median_delta: float) -> tuple[str, bool]:
    """返回（描述, 是否值得动 offset）。

    这里的 median_delta 是**带符号**的中位数，全文统一叫「整局偏移」：正 = 整体打晚，
    负 = 整体打早。它和「击打偏差」（每次击打偏离目标的幅度，取绝对值）不是同一个量 ——
    有早有晚时整局偏移可能接近 0，但每次击打的偏差照样有十几毫秒。
    以前两处都叫「中位偏差」，于是同一个页面里出现了 0.0ms 和 10.0ms 两个数。
    """
    d = median_delta
    if abs(d) < 4:
        return ("没有出现整体打早或打晚的情况（整局偏移 %+.1fms）。" % d, False)
    direction = "偏晚" if d > 0 else "偏早"
    if abs(d) < 10:
        return ("整体略微%s（整局偏移 %+.1fms），但还属于可接受范围。" % (direction, d), False)
    if abs(d) < MIN_OFFSET_ADVICE_MS:
        return (
            "整体%s（整局偏移 %+.1fms），方向一致但幅度不大——单凭一张图还不足以判定是延迟问题，"
            "先别动 offset。" % (direction, d),
            False,
        )
    return (
        "整局系统性%s（整局偏移 %+.1fms），可以考虑调 offset（调整完记得再打别的图试试）。" % (direction, d),
        True,
    )


def _section_sentence(sections: list[dict]) -> tuple[str, dict | None]:
    if len(sections) < 2:
        return ("", None)
    best = max(sections, key=lambda s: s["acc"])
    worst = min(sections, key=lambda s: s["acc"])
    if best is worst or abs(best["acc"] - worst["acc"]) < 0.2:
        return ("", None)
    names = {0: "前段", 1: "中段", 2: "后段"}

    def name_of(sec: dict) -> str:
        return names.get(sec.get("index", -1), "第%d段" % (sec.get("index", 0) + 1))

    text = "分段看，%s最稳（%.1f%%），%s最差（%.1f%%），落差 %.1f 个百分点。" % (
        name_of(best),
        best["acc"],
        name_of(worst),
        worst["acc"],
        best["acc"] - worst["acc"],
    )
    return (text, worst)


def _hand_sentence(columns: list[dict]) -> tuple[str, str]:
    """比较左右手的表现。返回（描述, 弱侧名）。"""
    if len(columns) < 4:
        return ("", "")
    half = len(columns) // 2
    left = [c for c in columns[:half] if c["notes"] >= 8]
    right = [c for c in columns[half:] if c["notes"] >= 8]
    if not left or not right:
        return ("", "")
    la = sum(c["errorRate"] for c in left) / len(left)
    ra = sum(c["errorRate"] for c in right) / len(right)
    ld = [c["absMedian"] for c in left if c["absMedian"] is not None]
    rd = [c["absMedian"] for c in right if c["absMedian"] is not None]
    lmean = (sum(ld) / len(ld)) if ld else 0.0
    rmean = (sum(rd) / len(rd)) if rd else 0.0
    if abs(la - ra) < 0.02 and abs(lmean - rmean) < 3:
        return ("左右手表现基本对称。", "")
    weak = "右手" if ra > la or rmean > lmean else "左手"
    text = "左右手不对称：左手失误率 %.1f%%、右手 %.1f%%（偏差中位 %.1f / %.1f ms），%s偏弱。" % (
        la * 100,
        ra * 100,
        lmean,
        rmean,
        weak,
    )
    return text, weak


def _pattern_sentence(payload: dict) -> tuple[str, str]:
    """找出"最吃亏"的键型：看失误率相对全谱平均的倍数（lift），而不是失误条数占比。

    只按"失误里有几成是它"来排会系统性偏向图里最多的键型：一张 80% 都是纵连的图，
    纵连当然占 80% 的失误，但这只说明它多、不说明你怕它。所以这里要求
    「样本够 + 失误率明显高于全谱平均」两条同时成立，否则宁可不写。
    """
    rows = payload.get("patterns") or []
    # 样本太少时任何比例都是噪声：至少 20 个物件、3 次失误
    cand = [r for r in rows if r.get("notes", 0) >= 20 and r.get("flaws", 0) >= 3]
    if not cand:
        return ("", "")
    worst = max(cand, key=lambda r: r.get("lift", 0))
    if worst.get("lift", 0) < 1.25:
        # 没有任何一种键型明显更吃亏 —— 这时候点一个名字等于编故事
        return ("", "")
    text = "最吃亏的键型是「%s」：仅占全谱 %.0f%% 的物件，但失误率达 %.0f%%，是全谱平均（%.0f%%）的 %.1f 倍。" % (
        worst.get("label", "?"),
        _f(worst.get("share")),
        _f(worst.get("errorRate")),
        _f(payload.get("summary", {}).get("errorRate")),
        _f(worst.get("lift")),
    )
    return (text, str(worst.get("label", "")))


def _density_ceiling(payload: dict) -> str:
    """密度与失误的关系。

    限定语必须写「本图内」：同一张图上「22 NPS 开始掉」不代表他的能力上限就是
    22 NPS——键型、段位、当天状态都会变，说成绝对上限会误导。这里只描述
    这张图自己的密度分布，并把落点指向「最密的那几段」这种具体的东西。
    """
    split = _density_split(payload)
    if not split:
        return ""
    segs = split["segments"]
    text = (
        "本图内 %g NPS 是你开始掉准的分界：以下失误率 %.1f%%，以上 %.1f%%。"
        % (split["threshold"], split["lowRate"] * 100, split["highRate"] * 100)
    )
    if segs:
        text += "超过它的段落有 %d 处（%s）。" % (
            split["segmentCount"],
            # 展示按时间顺序，「先练哪段」由建议里那句负责说
            "、".join("%s–%s" % (_fmt_mmss(a), _fmt_mmss(b)) for a, b in sorted(segs)),
        )
    return text


def _density_split(payload: dict) -> dict | None:
    """本图内的「掉准分界」：从低密度档往上找第一个失误率明显上升的档。

    要求低档至少 80 个音符：样本太小时最低档的失误率本身就在抖，
    拿它当基准会把整条曲线读歪。
    """
    bins = [b for b in _density_bins(payload) if _f(b.get("count")) >= 80]
    if len(bins) < 2:
        return None
    bins.sort(key=lambda b: _f(b.get("low")))
    low = bins[0]
    low_rate = _f(low.get("errorRate")) or 0.005   # 0 会让后面的倍数判据永远不成立
    cut = None
    for b in bins[1:]:
        rate = _f(b.get("errorRate"))
        if rate >= max(low_rate * 1.6, low_rate + 0.02):
            cut = b
            break
    if cut is None:
        return None
    threshold = _f(cut.get("low"))
    high = [b for b in bins if _f(b.get("low")) >= threshold - 0.01]
    notes = sum(int(_f(b.get("count"))) for b in high)
    errs = sum(int(_f(b.get("errors"))) for b in high)
    high_rate = (errs / notes) if notes else _f(cut.get("errorRate"))
    segs = _dense_segments(payload.get("densityTimeline") or [], threshold)
    return {
        "threshold": threshold,
        "lowRate": low_rate,
        "highRate": high_rate,
        "segments": segs[:3],
        "segmentCount": len(segs),
    }


def _dense_segments(timeline: list[dict], threshold: float,
                    limit: int = 8) -> list[tuple]:
    """时间线里密度 ≥ 阈值的连续窗口，返回 [(起, 止)]，按长度降序。

    空一个窗口就断开：中间那段掉到阈值以下，说明是两处独立的难点，
    合成一段会让用户去练中间其实没问题的部分。
    """
    if not timeline:
        return []
    runs: list[list[float]] = []
    cur: list[float] | None = None
    for w in timeline:
        dense = _f(w.get("density")) >= threshold
        if not dense:
            if cur:
                runs.append(cur)
                cur = None
            continue
        t0, t1 = _f(w.get("t0")), _f(w.get("t1"))
        span = max(1.0, t1 - t0)
        if cur is not None and t0 - cur[1] <= span * 1.5:
            cur[1] = t1
        else:
            if cur:
                runs.append(cur)
            cur = [t0, t1]
    if cur:
        runs.append(cur)
    runs.sort(key=lambda r: -(r[1] - r[0]))
    return [(round(a), round(b)) for a, b in runs[:limit]]


def _density_advice(payload: dict, rates: list[float] | None = None) -> dict | None:
    """按本图的密度分界给一条「只练这几段」的建议。没有明显分界就不给。"""
    rates = list(rates or DEFAULT_RATES)
    split = _density_split(payload)
    if not split or not split["segments"]:
        return None
    segs = split["segments"]
    first = segs[0]
    more = "" if split["segmentCount"] <= len(segs) else "等 %d 处" % split["segmentCount"]
    return {
        "tag": "ability",
        "title": "先单独练 %s–%s %s" % (
            _fmt_mmss(first[0]), _fmt_mmss(first[1]),
            "这一段" if split["segmentCount"] <= 1 else "这几段",
        ),
        "body": (
            "本图内 %g NPS 以上失误率 %.1f%%，以下只有 %.1f%%。"
            "超过这个密度的段落有 %d 处（%s%s），按 %s× 只循环这几段："
            "每段连着 3 次失误不超过 3 个再回原速；整图暂时不用刷。"
            % (split["threshold"], split["highRate"] * 100, split["lowRate"] * 100,
               split["segmentCount"],
               "、".join("%s–%s" % (_fmt_mmss(a), _fmt_mmss(b)) for a, b in sorted(segs)), more,
               _rates_text(rates))
        ),
        "rates": rates,
        "timeRange": [first[0], first[1]],
        # 同类里优先当主建议：它带分界、带段落、还能给出降速后的段位
        "weight": 2,
    }


def _fmt_mmss(t: float) -> str:
    t = max(0, int(t // 1000))
    return "%d:%02d" % (t // 60, t % 60)


def _density_bins(payload: dict) -> list[dict]:
    """读密度—失误分档（键名由 attribution 的画像决定，是下划线风格）。"""
    prof = payload.get("categories", {}).get("profile", {})
    return [b for b in (prof.get("density_profile") or []) if isinstance(b, dict)]


def _best_fast_bin(payload: dict) -> dict | None:
    """密度最高、失误率仍然很低的档 —— 「高密度也稳」的亮点证据。"""
    usable = [b for b in _density_bins(payload) if b.get("count", 0) >= 10]
    if not usable:
        return None
    top = sorted(usable, key=lambda b: -_f(b["high"]))[:2]
    good = [b for b in top if _f(b["errorRate"]) < 0.10]
    return good[0] if good else None


def _worst_error_cluster(payload: dict, window_ms: float = 2000) -> tuple | None:
    """最密集的失误簇：（个数, 起始 ms, 结束 ms）。不足 3 个连发不算簇。"""
    ts = sorted(e["t"] for e in payload.get("errors", []) if isinstance(e.get("t"), (int, float)))
    if len(ts) < 3:
        return None
    j = 0
    best: tuple | None = None
    for i in range(len(ts)):
        while ts[i] - ts[j] > window_ms:
            j += 1
        cnt = i - j + 1
        if best is None or cnt > best[0]:
            best = (cnt, ts[j], ts[i])
    if best is None or best[0] < 3:
        return None
    return best


def _worst_column(payload: dict) -> dict | None:
    """失误率明显偏高的那一列（左右手不对称的落点）。差距不到 2 个百分点不算。"""
    cols = [c for c in payload.get("columnStats", []) if c.get("notes", 0) >= 8]
    if len(cols) < 2:
        return None
    worst = max(cols, key=lambda c: _f(c.get("errorRate")))
    best = min(cols, key=lambda c: _f(c.get("errorRate")))
    if worst is best or _f(worst.get("errorRate")) - _f(best.get("errorRate")) < 0.02:
        return None
    return worst


# 准确率分档文案（亮点侧）。90% 以下不再是亮点，反过来要提醒难度超纲。
_ACC_TIERS = [
    (99.5, "准确率（v1）%.2f%%，几乎到顶，剩的都是细节问题。"),
    (99.0, "准确率（v1）%.2f%%，这一局打得很干净。"),
    (97.0, "准确率（v1）%.2f%%，稳定度在这张图上已经够看。"),
    (95.0, "准确率（v1）%.2f%%，骨架没问题，掉分集中在少数难点上。"),
    (93.0, "准确率（v1）%.2f%%，能完整打下来，还有明确的提升空间。"),
    (90.0, "准确率（v1）%.2f%%，需要提升的部分较多。"),
]


def _acc_strength(acc: float, tag: str = "v1") -> str:
    for floor, fmt in _ACC_TIERS:
        if acc >= floor:
            # 文案表按 v1 写的；切到 v2 时只换括号里的口径名，句子其余部分不动
            return (fmt % acc).replace("（v1）", "（" + tag + "）")
    return ""


def _advice_ability(share: float, rates: list[float] | None = None) -> dict | None:
    """能力类建议。阈值 25%：低于它时样本里的噪声比信号大，给专项建议等于瞎指方向。"""
    if share < 0.25:
        return None
    rates = list(rates or DEFAULT_RATES)
    return {
        "tag": "ability",
        "title": "降速练，先把手感稳下来",
        "body": "用 %s 倍速把这张图过顺，能稳定到 99%% 再回原速。目标是先把「稳」抬上去，"
        "而不是硬冲——硬冲只会把随机误差练成肌肉记忆。"
        % _rates_text(rates),
        # 前端逐档估算这张图降速后是什么段位，别让用户自己去算
        "rates": rates,
    }


def _advice_faster(rates: list[float], acc_v1: float) -> dict:
    """成绩够好时反过来提速练：再降速只是把已经会的东西重复一遍。"""
    return {
        "tag": "ability",
        "title": "提速练，把余量用掉",
        "body": "这一局 v1 准确率 %.2f%%，降速练已经没有意义了：用 %s 倍速把全曲过一遍，"
        "目标是在加速下也稳在 98%% 以上——做到了说明速度余量是真的有，"
        "做不到就说明余量就在这一档附近。" % (acc_v1, _rates_text(rates)),
        "rates": rates,
        # 全曲加速：不裁段，整张图重新打包
        "fullMap": True,
        "weight": 2,
    }


def _advice_rhythm(payload: dict, share: float) -> dict | None:
    """节奏类建议：偏得多才谈 offset，偏得少只谈跟拍。"""
    if share < 0.25:
        return None
    median_delta = _f(payload.get("summary", {}).get("medianDelta"))
    direction = "偏晚" if median_delta > 0 else "偏早"
    if abs(median_delta) >= MIN_OFFSET_ADVICE_MS:
        return {
            "tag": "rhythm",
            "title": "把 offset 校掉 %.0fms" % abs(median_delta),
            "body": "整局%s %.1fms 且换密度也不改方向，这是判定时机没对齐而不是手抖。"
            "在 osu! 里把 universal offset 往%s调 %.0fms 左右，再用另一张 2 分钟短图复核——"
            "**别只凭这一张图就定**，改完打别的图要确认没有更偏。"
            % (
                direction,
                abs(median_delta),
                "正" if median_delta > 0 else "负",
                abs(median_delta),
            ),
        }
    ur = _f(payload.get("summary", {}).get("ur"))
    return {
        "tag": "rhythm",
        "title": "跟拍子练，别动 offset",
        "body": "整局%s %.1fms，方向一致但幅度只有个位数——一张图不足以判定是延迟问题，"
        "这时候改 offset 很可能让别的图更偏。更稳的做法是开 hitsound 或跟节拍器打一段慢速同图，"
        "把「每次击打落在同一个偏移」这件事做出来（当前 UR %.0f）。"
        % (direction, abs(median_delta), ur),
    }


def _advice_distraction(payload: dict, share: float) -> dict | None:
    """分神类建议：能定位到具体那一簇就点名，定位不到才给通用建议。"""
    if share < 0.25:
        return None
    cluster = _worst_error_cluster(payload)
    if cluster is None:
        return {
            "tag": "distraction",
            "title": "缩短单次练习时长",
            "body": "失误成簇、且伴随手停下来的空白，说明注意力在掉。改成「每轮 2~3 分钟 + 30 秒休息」，"
            "比一口气刷 10 分钟更有效。",
        }
    return {
        "tag": "distraction",
        "title": "走神集中在 %s 前后" % _fmt_mmss(cluster[1]),
        "body": "失误最密的一簇在 %s–%s（%d 秒内连着错 %d 个），其余时间手是稳的。"
        "把练习拆成「每轮 2~3 分钟 + 30 秒休息」，并留意 %s 附近是不是难度跳变/读谱压力点——那里最容易掉线。"
        % (
            _fmt_mmss(cluster[1]),
            _fmt_mmss(cluster[2]),
            round((cluster[2] - cluster[1]) / 1000),
            cluster[0],
            _fmt_mmss(cluster[1]),
        ),
        "timeRange": [round(cluster[1]), round(cluster[2])],
    }


def _advice_slip(share: float) -> dict | None:
    """随机失手类建议：这类失误没法靠"降速练"解决，能改的是热身与节奏稳定性。"""
    if share < 0.45:
        return None
    return {
        "tag": "slip",
        "title": "先热身，再谈上限",
        "body": "这类失误彼此孤立、幅度不大，说明不是不会打，是手还没热开或注意力短暂松了一下。"
        "开打前用 0.8 倍速过 2~3 分钟同难度图（或打两遍简单图）再上正题；"
        "连续几局都这样时，先停 10 分钟——疲劳期的失手率会翻倍，硬刷只会固化。",
    }


def _category_narrative(payload: dict, dan_value: float | None = None,
                        acc_v1: float | None = None) -> tuple[str, str, list[dict]]:
    cats = payload.get("categories", {})
    counts = cats.get("counts", {})
    total = max(1, cats.get("total", 0))
    label = CATEGORY_LABEL
    if not total or sum(counts.values()) == 0:
        return ("这一局几乎没有失误，不需要归因。", "", [])
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    main, _main_n = ranked[0]
    text = "把 %d 个失误拆开看：%s %d 个（%.0f%%）、%s %d 个、%s %d 个、%s %d 个。" % (
        total,
        label["ability"],
        counts.get("ability", 0),
        counts.get("ability", 0) / total * 100,
        label["rhythm"],
        counts.get("rhythm", 0),
        label["distraction"],
        counts.get("distraction", 0),
        label["slip"],
        counts.get("slip", 0),
    )
    detail = {
        "ability": "主要卡在速度上：失误集中在密度高、要求快的地方，同一类片段反复掉分。",
        "rhythm": "主要是节奏对不齐：误差方向一致地偏，换到密度不同的地方也还是偏，属于「拍子没咬住」而不是手不稳。",
        "distraction": "主要是注意力掉线：失误成簇出现、前后都正常，还伴随几次手停下来的空白。",
        "slip": "主要是随机失手：这些失误彼此孤立、幅度不大、方向和难度上都看不出系统性——"
                "不是能力不够，也不是拍子没咬住，更像是手感/注意力的一次短暂抖动。",
    }[main]

    if acc_v1 is not None and float(acc_v1) >= FAST_ACC:
        # 98.5% 以上不再谈降速：给提速建议，不再按失误类别拆
        return (text, detail, [_advice_faster(_practice_rates(payload, dan_value, acc_v1), float(acc_v1))])

    if total < 10:
        # 失误个位数时给「专项建议」等于瞎指方向：样本太小，占比一两条就跳到 25%
        return (text, detail, [])

    # 建议必须带上「这份回放里的具体数字」，否则就是放在谁的报告里都成立的空话。
    # 注意：一律不提 NPS —— 同样的 NPS、键型不同难度可以差一大截，
    # 按 NPS 给建议等于用错尺子。要说「练什么」就按 pattern 和具体片段说。
    advice = [
        a
        for a in (
            _advice_ability(counts.get("ability", 0) / total, _practice_rates(payload, dan_value)),
            _advice_rhythm(payload, counts.get("rhythm", 0) / total),
            _advice_distraction(payload, counts.get("distraction", 0) / total),
            _advice_slip(counts.get("slip", 0) / total),
        )
        if a
    ]
    return (text, detail, advice)


@dataclass
class _Draft:
    """评价文本的逐段累积器。

    评价由九块互不相干的判断拼成（准度、失误结构、分段、耐力、左右手、pattern、
    密度、断连、offset），每块往同样的四个列表里写。以前它们全挤在一个 200 行函数里，
    改其中一条建议要滑三屏才能确认有没有和别处打架——分成一人一块之后，
    顺序仍由调用顺序决定，输出不变。
    """

    payload: dict
    acc: float
    ur: float
    median_delta: float
    has_bias: bool
    judge_mode: str = "v1"
    dan_value: float | None = None
    acc_v1: float | None = None
    paragraphs: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    advice: list[dict] = field(default_factory=list)

    @property
    def fast_mode(self) -> bool:
        """v1 准确率够高时改提速练，降速类建议整块不出现。"""
        return self.acc_v1 is not None and float(self.acc_v1) >= FAST_ACC

    @property
    def rates(self) -> list[float]:
        """这张图的练习倍率，从慢到快；提速模式下是 1.05 / 1.1。"""
        return _practice_rates(self.payload, self.dan_value, self.acc_v1)

    # 1) 准度与稳定性
    #    「准确率」标明 v1 口径（旁边还有 ScoreV2 的准确率，不标容易被当成同一个数）
    def add_accuracy(self) -> None:
        s = self.payload.get("summary", {})
        bm = self.payload.get("beatmap", {})
        self.paragraphs.append(
            "这张谱的「%s」难度，你打出了 %.2f%% 的准确率（按%s方式计算）、UR %.0f。"
            "击打偏差中位 %.1fms，离散程度属于「%s」。"
            % (bm.get("difficulty", "?"), self.acc, self.judge_mode, self.ur,
               _f(s.get("absMedian")), _stability_word(self.ur))
        )
        bias_text, _ = _bias_sentence(self.median_delta)
        self.paragraphs.append(bias_text)

        # 条件放宽到「说得出口就写」，别让这一栏常年只有一条破折号
        notes_total = max(1, _f(bm.get("notes"), 1))
        # V2 / Classic 的长条头尾各算一次连击，分母也得跟着换，
        # 否则面图上会写出"占全部物件的 135%"。V1 按物件，分母就是物件数。
        v2_mode = self.judge_mode in ("v2", "classic")
        combo_base = max(1, int(_f(s.get("v2Judgements"), 0))) if v2_mode else notes_total
        combo_unit = "判定" if v2_mode else "物件"
        max_combo = _f(s.get("maxCombo"))
        acc_strength = _acc_strength(self.acc, self.judge_mode)
        if acc_strength:
            self.strengths.append(acc_strength)
        else:
            # 90% 以下不再是亮点：这张图对他来说是超纲的，硬刷只会固化错误手感
            self.weaknesses.append(
                "准确率（%s）%.2f%% 在 90%% 以下，这张图目前超出你的能力承受范围：建议先降速（0.85 倍）"
                "或换低一档难度，把准确率做到 93%% 以上再尝试打原速。" % (self.judge_mode, self.acc)
            )
        if self.ur and self.ur < 110:
            self.strengths.append("稳定性好（UR %.0f），说明手指控制已经成型。" % self.ur)
        if abs(self.median_delta) < 5 and (self.ur > 0 or self.acc > 0):
            self.strengths.append("拍子咬得准（整局偏移 %+.1fms），节奏骨架没有问题。" % self.median_delta)
        if max_combo / combo_base >= 0.7:
            self.strengths.append(
                "最长连击 %d（占全部%s的 %.0f%%），中途基本没崩。"
                % (int(max_combo), combo_unit, min(100.0, max_combo / combo_base * 100))
            )
        fast_bin = _best_fast_bin(self.payload)
        if fast_bin is not None:
            self.strengths.append("最快的那段失误率也只有 %.0f%%，速度不是当前的瓶颈。" % (fast_bin["errorRate"] * 100))
        if self.ur > 180:
            self.weaknesses.append(
                "偏差波动大（UR %.0f），同一处会一会儿偏早一会儿偏晚，如果几乎没有miss的情况UR偏高，"
                "可能需要考虑调整下落速度或延迟。" % self.ur
            )

    # 2) 失误结构
    def add_categories(self) -> None:
        cat_text, cat_detail, cat_advice = _category_narrative(
            self.payload, self.dan_value, self.acc_v1)
        self.paragraphs.append(cat_text)
        if cat_detail:
            self.paragraphs.append(cat_detail)
        self.advice.extend(cat_advice)

    # 3) 分段
    def add_sections(self) -> None:
        sections = self.payload.get("sections", [])
        sec_text, worst_sec = _section_sentence(sections)
        if sec_text:
            self.paragraphs.append(sec_text)
        if worst_sec and worst_sec["acc"] < self.acc - 1.5 and not self.fast_mode:
            self.weaknesses.append(
                "第 %d 段（%s–%s）明显跟不上，掉了（%.1f%%），这一段值得单独复练。"
                % (worst_sec["index"] + 1, _fmt_mmss(worst_sec["start"]), _fmt_mmss(worst_sec["end"]), worst_sec["acc"])
            )
            self.advice.append(
                {
                    "tag": "ability",
                    "title": "单独复练 %s–%s 这一段" % (_fmt_mmss(worst_sec["start"]), _fmt_mmss(worst_sec["end"])),
                    "body": "这一段准确率只有 %.1f%%（全曲 %.1f%%）。把它单独循环：先 %g 倍速过 3 遍找准手感，"
                    "再回原速；连续 3 次原速不掉连再往下走。"
                    % (worst_sec["acc"], self.acc, min(self.rates)),
                    "rates": self.rates,
                    "timeRange": [round(_f(worst_sec["start"])), round(_f(worst_sec["end"]))],
                    "weight": 1,
                }
            )
        if len(sections) >= 2:
            first_acc = _f(sections[0].get("acc"))
            last_acc = _f(sections[-1].get("acc"))
            if last_acc - first_acc >= 1.0:
                self.strengths.append("后段比前段稳（+%.1f 个百分点），续航没有问题。" % (last_acc - first_acc))

    # 3.5) 耐力
    # 判定本身在 attribution 里（同一大类连续 6 段之后失误明显上升），
    # 但逐条失误的理由只留 3 条，长图上很容易被别的理由挤掉，所以这里单独写一段。
    def add_stamina(self) -> None:
        prof = (self.payload.get("categories") or {}).get("profile") or {}
        st = prof.get("stamina") or {}
        if not st.get("drop"):
            return
        start = _fmt_mmss(st.get("fromTime", 0))
        self.paragraphs.append(
            "还有一段值得单独说：%s 起连续 %d 段都是「%s」键型，前 6 段的失误率是 %.0f%%，"
            "之后升到 %.0f%%。同样的写法打着打着掉准，可能是耐力撑不住。"
            % (start, st.get("blocks", 0), st.get("label", ""),
               _f(st.get("baseRate")) * 100, _f(st.get("testRate")) * 100)
        )
        self.weaknesses.append(
            "后段（%s 起）在同样的「%s」上掉准，体力是短板。" % (start, st.get("label", ""))
        )

    # 4) 左右手
    def add_hands(self) -> None:
        hand_text, weak_hand = _hand_sentence(self.payload.get("columnStats", []))
        if hand_text:
            self.paragraphs.append(hand_text)
        if not weak_hand:
            # 对称本身就是亮点
            if hand_text == "左右手表现基本对称。":
                self.strengths.append("左右手表现基本对称，没有明显的弱侧。")
            return
        col = _worst_column(self.payload)
        if col is not None:
            self.weaknesses.append(
                "第 %d 列失误率 %.1f%%，可能由于该轨难度较大，也可能这是你的薄弱侧。"
                % (col["column"] + 1, _f(col.get("errorRate")) * 100)
            )
            self.advice.append(
                {
                    "tag": "ability",
                    "title": "补%s（第 %d 列最明显）" % (weak_hand, col["column"] + 1),
                    "body": "第 %d 列失误率 %.1f%%，明显高于其它列。找一张以%s为主的连打练习图，"
                    "先慢速把节奏走对，再逐档提速；也可以只练这一侧的交替片段。"
                    % (col["column"] + 1, _f(col.get("errorRate")) * 100, weak_hand),
                }
            )
        else:
            self.weaknesses.append("%s相对偏弱，快速连打时容易先崩。" % weak_hand)
            self.advice.append(
                {
                    "tag": "ability",
                    "title": "补%s" % weak_hand,
                    "body": "找一张以%s为主的连打练习图，先慢速把节奏走对，再逐档提速；也可以只练这一侧的交替片段。"
                    % weak_hand,
                }
            )

    # 5) pattern
    def add_pattern(self) -> None:
        pat_text, worst_pat = _pattern_sentence(self.payload)
        if not pat_text:
            return
        self.paragraphs.append(pat_text)
        self.weaknesses.append("「%s」是你目前最吃亏的键型。" % worst_pat)
        # 键型名与 osu!mania 的通用叫法一致（映射见 patterns.PATTERN_LABEL），练法也按这个分
        # 键名统一用英文（与 patterns.PATTERN_LABEL 一致）；解释仍用中文
        drills = {
            "Longjack": "Longjack（纵连）找专项图，从能全连的 BPM 开始每档 +10 慢慢抬；注意手腕别锁死、手指别停在键上。",
            "Minijack": "Minijack（小叠）靠单指快速回弹：从 1/8 的两连叠开始，注意抬指高度别太高，否则回不来。",
            "Chordjack": "Chordjack（多押叠）要同时按准和快速换手：先降速把每个和弦按实，再逐档提速。",
            "Jumpstream": "Jumpstream（双押切）的重点是两指落键的同时性：慢速练到两个音完全齐，再上速度。",
            "Handstream": "Handstream（三押切）先练三指独立发力（每根手指单独按实），再合起来提速；注意别用整只手砸。",
            "Quadstream": "Quadstream（四押切）先降速确保四指同时落键，别用整只手砸键。",
            "Stream": "Stream（单押切）掉准多半是手腕紧张：放松手腕、用指根发力，从短串（4 连）开始练。",
            "Speedstream": "Speedstream（高速连打）先降速把每一击打实，再按 0.05× 一档往上推；别一上来就追速度。",
            "Trill": "Trill（交互）重点练两指切换时力度一致，避免一边轻一边重。",
            "Jumptrill": "Jumptrill（对拍）是两对双押交替：先分手练熟，再合起来；别让一只手等另一只手。",
            "Jump": "Jump（双押）不稳通常是一指深一指浅：慢速对齐两指的落键时刻，再上速度。",
            "Hand": "Hand（三押）容易漏键就练「按实」：慢速确认每根手指都压到位，再逐步提速。",
            "Quad": "Quad（四押）先确认四指能同时落键，再谈速度。",
            "Anchor": "Anchor（锚）是同一列被反复砸：单独练这一列的回弹，别让手指停在键上。",
        }
        self.advice.append(
            {
                "tag": "ability",
                "title": "专项练「%s」" % worst_pat,
                "body": drills.get(worst_pat, "找一张以这个键型为主的练习图，从能全连的速度开始逐档提。"),
            }
        )

    # 6) 稳定区间
    def add_density(self) -> None:
        if self.fast_mode:
            return                      # 提速练习不裁段，密度分段建议没有落点
        density_text = _density_ceiling(self.payload)
        if density_text:
            self.paragraphs.append(density_text)
        drain = _density_advice(self.payload, self.rates)
        if drain:
            self.advice.append(drain)

    # 7) 组合断点
    def add_combo(self) -> None:
        breaks = self.payload.get("comboBreaks", [])
        if not breaks:
            self.strengths.append("全程没有断连。")
            return
        first = breaks[0]
        self.paragraphs.append(
            "全曲断了 %d 次，第一次断在 %d:%02d（第 %d 列）。"
            % (len(breaks), int(first["t"] // 60000), int(first["t"] % 60000 // 1000), first["column"] + 1)
        )

    # 8) 偏移建议
    def add_offset_advice(self) -> None:
        if not self.has_bias:
            return
        # 节奏类建议里若已经在说 offset，会跟这条重复——偏移本身成立时只留这条更具体的
        self.advice = [
            a for a in self.advice if not (a.get("tag") == "rhythm" and "universal offset" in a.get("body", ""))
        ]
        self.advice.insert(
            0,
            {
                "tag": "rhythm",
                "title": "先校一遍偏移",
                "body": "整局偏移 %.1fms 且方向一致，先在 osu! 里用 universal offset 微调（或打一张短图确认手感），"
                "比硬练更省时间。" % self.median_delta,
            },
        )


# 一句话结论按"占比最高的那类失误"给，四类各一句；顶部的徽章也在这里拼。
_HEADLINES = {
    "ability": "速度是当前的天花板",
    "rhythm": "拍子没咬住，手其实是稳的",
    "distraction": "注意力比手先掉线",
    "slip": "手是稳的，掉的是零星几下",
}


def _headline(grade: str, counts: dict) -> str:
    """一句话结论。S / A+ 时同一句话要换个说法：这时候说"天花板"是自相矛盾的。"""
    main_cat = max(counts.items(), key=lambda kv: kv[1])[0] if counts else "ability"
    headline = _HEADLINES[main_cat]
    if grade in ("S", "A+"):
        headline = "这一局很干净，" + headline.replace("是当前的天花板", "还有余量")
    return headline


def _badges(acc: float, ur: float, abs_median: float, max_combo, tag: str) -> list[dict]:
    """顶栏的几个数字。

    准确率跟着当前判定口径走；旁边还挂着 ScoreV2 的独立评分，
    不标口径的话两个数字很容易被当成同一个。

    这一格放「击打偏差」（绝对值）而不是带符号的整局偏移：玩家想知道的是
    「我每次偏离多远」，显示 +0.0ms 会让人以为打得完美，其实每次偏了 10ms。
    整局偏移（有没有系统性偏早/偏晚）在正文里单独说。
    """
    return [
        {"key": "acc", "label": "准确率 (%s)" % tag, "value": "%.2f%%" % acc},
        {"key": "ur", "label": "UR", "value": "%.0f" % ur},
        {"key": "bias", "label": "击打偏差", "value": "%.1fms" % abs_median},
        # V1 按物件计，面图上注定低于结算屏（那边头尾都算），标出口径免得被当成算错
        {"key": "combo", "label": "最大连击" + ("（按物件）" if tag == "v1" else ""), "value": str(max_combo)},
    ]


def _ranges_overlap(a, b, ratio: float = 0.35) -> bool:
    """两段时间是否明显重叠（重叠部分占较短那段的三分之一以上）。

    阈值压到 0.35 而不是 0.5：分段建议会横跨好几个密度段，和主建议重叠
    三成多时读起来就已经是「同一片地方」，留着只会让人以为要练两处。
    """
    try:
        a0, a1 = float(a[0]), float(a[1])
        b0, b1 = float(b[0]), float(b[1])
    except (TypeError, ValueError, IndexError):
        return False
    lo, hi = max(a0, b0), min(a1, b1)
    if hi <= lo:
        return False
    shorter = min(a1 - a0, b1 - b0)
    return shorter > 0 and (hi - lo) / shorter >= ratio


_ADVICE_WEIGHT = {"ability": 100.0, "rhythm": 80.0, "distraction": 70.0, "slip": 60.0}


def _advice_priority(item: dict, share: dict) -> float:
    """一条建议「先做」的程度：所属类别的失误占比 + 是否给了具体位置。"""
    tag = str(item.get("tag") or "ability")
    p = _ADVICE_WEIGHT.get(tag, 50.0) + 100.0 * _f(share.get(tag))
    if item.get("timeRange"):
        p += 8.0
    if item.get("rates"):
        p += 4.0
    # 校 offset 永远排第一：不改手感、只改判定对齐，而且失误是整局统一偏移，
    # 校准之后其他建议的效果才看得出来。用 "universal offset" 精确匹配——
    # 「跟拍子练，别动 offset」也含 offset，但那条明确说了别动，不能置顶。
    text = (str(item.get("title") or "") + str(item.get("body") or "")).lower()
    if "universal offset" in text:
        return 999.0
    return round(p, 2)


def _finalize_advice(items: list[dict], share: dict, limit: int = 3) -> list[dict]:
    """合并同类、按「能挽回多少失误」排序、最多留三条。

    ability 组会同时产出整图降速、复练某段、专项键型、补弱侧四条，说的
    其实是同一件事（手跟不上）。并列摆着只会让人不知道先做哪个，所以合成
    一条主建议，其余降级成子项——用户照着第一条做就行。
    """
    if not items:
        return []
    groups: dict[str, list[dict]] = {}
    for a in items:
        groups.setdefault(str(a.get("tag") or "ability"), []).append(a)

    out: list[dict] = []
    for _tag, group in groups.items():
        # 主建议优先级：校 offset > 带具体时间 > 带倍率 > 生成顺序
        group.sort(key=lambda a: (
            0 if "universal offset" in (str(a.get("title") or "") + str(a.get("body") or "")).lower() else 1,
            -int(a.get("weight") or 0),
            0 if a.get("timeRange") else 1,
            0 if a.get("rates") else 1,
        ))
        main = dict(group[0])
        main_range = main.get("timeRange")
        subs: list[str] = []
        for extra in group[1:]:
            # 和主建议指同一段的子项直接丢掉，否则会出现「主练 1:20，子项又让练 0:20」
            if _ranges_overlap(main_range, extra.get("timeRange")):
                continue
            text = str(extra.get("title") or "").strip()
            if text and text not in subs:
                subs.append(text)
        if subs:
            main["subs"] = subs[:3]
        main["priority"] = _advice_priority(main, share)
        out.append(main)

    out.sort(key=lambda a: -_f(a.get("priority")))
    return out[:limit]


def build_evaluation(payload: dict, dan_value: float | None = None) -> dict:
    s = payload.get("summary", {})
    acc = _f(s.get("accuracy"))
    # 提速与否一律看 v1 口径：切到 v2 / classic 时 acc 会变，但「这套已经打穿」的判断不该变
    acc_v1_raw = payload.get("accuracyV1")
    acc_v1 = _f(acc_v1_raw) if acc_v1_raw is not None else None
    ur = _f(s.get("ur"))
    median_delta = _f(s.get("medianDelta"))

    draft = _Draft(
        payload=payload,
        acc=acc,
        ur=ur,
        median_delta=median_delta,
        has_bias=_bias_sentence(median_delta)[1],
        judge_mode=str(payload.get("judgeMode") or "v1"),
        dan_value=dan_value,
        acc_v1=acc_v1,
    )
    # 顺序即版面顺序，改顺序会改变报告读起来的高低起伏
    draft.add_accuracy()
    draft.add_categories()
    draft.add_sections()
    draft.add_stamina()
    draft.add_hands()
    draft.add_pattern()
    draft.add_density()
    draft.add_combo()
    draft.add_offset_advice()

    advice = draft.advice
    if not advice:
        advice.append(
            {
                "tag": "ability",
                "title": "往上加一档难度",
                "body": "这一档已经很稳了，可以去试难度高一点的图；把「稳定」和「上限」分开练。",
            }
        )

    grade, grade_desc = _grade(acc, ur, _f(s.get("errorRate")))
    cats = payload.get("categories", {}).get("counts", {})
    share = payload.get("categories", {}).get("share", {})
    advice = _finalize_advice(advice, share)

    return {
        "grade": grade,
        "gradeDesc": grade_desc,
        "headline": _headline(grade, cats),
        "paragraphs": [p for p in draft.paragraphs if p],
        "strengths": draft.strengths[:5],
        "weaknesses": draft.weaknesses[:4],
        "advice": advice,
        "badges": _badges(acc, ur, _f(s.get("absMedian")), s.get("maxCombo", 0), draft.judge_mode),
    }
