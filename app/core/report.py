"""把分析数据写成「人话」评价：等级、总评、亮点、短板、改进建议。

原则：
  * 每个结论都要有数据支撑，不给空话。
  * 建议要具体可做（降速多少、练什么 pattern、看哪一段）。
  * 不美化：差就是差，但要说清差在哪。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .labels import CATEGORY_LABEL


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


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
    return ("D", "建议先降速，把准确度做出来")


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


# 只有中位偏差超过这个值才动「改 offset」的念头。
#
# 理由很重要：单看一张图的偏差不足以断定是延迟问题 —— 一个人的手感、当天状态、
# 甚至这一张图的鼓点都能造成几毫秒的整体偏移。贸然按这张图去改 universal offset，
# 很可能在别的图上反而打得更偏。15ms 是个「几乎不可能是手感随机波动」的量级，
# 到了这个数才值得动手，且改完必须用另一张 2 分钟短图复核。
MIN_OFFSET_ADVICE_MS = 15.0


def _bias_sentence(median_delta: float) -> tuple[str, bool]:
    """返回（描述, 是否值得动 offset）。"""
    d = median_delta
    if abs(d) < 4:
        return ("击打几乎完全贴着节拍（中位偏差 %.1fms）。" % d, False)
    direction = "偏晚" if d > 0 else "偏早"
    if abs(d) < 10:
        return ("整体略微%s（中位 %.1fms），属于可接受范围。" % (direction, d), False)
    if abs(d) < MIN_OFFSET_ADVICE_MS:
        return (
            "整体%s（中位 %.1fms），方向一致但幅度不大——单凭一张图还不足以判定是延迟问题，"
            "先别动 offset。" % (direction, d),
            False,
        )
    return (
        "整局系统性%s（中位 %.1fms），方向一致且幅度不小——这不是手抖，更像是判定时机没对齐，"
        "可以考虑调 offset（改完务必用另一张短图复核）。" % (direction, d),
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
    text = "最吃亏的是「%s」：占全谱 %.0f%% 的物件，失误率 %.0f%%，是全谱平均（%.0f%%）的 %.1f 倍。" % (
        worst.get("label", "?"),
        _f(worst.get("share")),
        _f(worst.get("errorRate")),
        _f(payload.get("summary", {}).get("errorRate")),
        _f(worst.get("lift")),
    )
    return (text, str(worst.get("label", "")))


def _density_ceiling(payload: dict) -> str:
    """密度与失误的关系（只说这张图，"最密那段"具体在哪由调用方另说）。

    这里刻意不说「你的稳定上限是 X NPS」：同样的 NPS、键型不同难度可以差一大截，
    拿 NPS 当尺子等于用错尺子（用户明确要求不按 NPS 给建议）。所以只描述
    这张图自己的密度分布，并把落点指向「最密的那一段」这种具体的东西。
    """
    worst = _worst_density_bin(payload)
    usable = [b for b in _density_bins(payload) if b.get("count", 0) >= 10]
    if not worst or len(usable) < 2:
        return ""
    calm = min(usable, key=lambda b: _f(b.get("high")))
    w_rate = _f(worst.get("errorRate"))
    c_rate = _f(calm.get("errorRate"))
    if w_rate - c_rate < 0.05:
        # 各档失误率差不多 —— 说明这张图不是"越密越崩"，这句话就没信息量，别硬写
        return ""
    return (
        "失误跟着密度走：这张图最密的段落失误率 %.0f%%，最缓的段落只有 %.0f%%。"
        "把最密的那段单独抽出来降速练（0.85 倍起，连过 3 遍不掉再回原速），比整图重复更省时间。"
        % (w_rate * 100, c_rate * 100)
    )


def _fmt_mmss(t: float) -> str:
    t = max(0, int(t // 1000))
    return "%d:%02d" % (t // 60, t % 60)


def _density_bins(payload: dict) -> list[dict]:
    """读密度—失误分档（键名由 attribution 的画像决定，是下划线风格）。"""
    prof = payload.get("categories", {}).get("profile", {})
    return [b for b in (prof.get("density_profile") or []) if isinstance(b, dict)]


def _worst_density_bin(payload: dict) -> dict | None:
    """失误率最高的密度档（要求样本够）。没有足够数据返回 None。"""
    usable = [b for b in _density_bins(payload) if b.get("count", 0) >= 10 and b.get("errorRate", 0) > 0]
    if not usable:
        return None
    return max(usable, key=lambda b: b["errorRate"])


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
    (90.0, "准确率（v1）%.2f%%，基本掌握这张图。"),
]


def _acc_strength(acc: float, tag: str = "v1") -> str:
    for floor, fmt in _ACC_TIERS:
        if acc >= floor:
            # 文案表按 v1 写的；切到 v2 时只换括号里的口径名，句子其余部分不动
            return (fmt % acc).replace("（v1）", "（" + tag + "）")
    return ""


def _advice_ability(share: float) -> dict | None:
    """能力类建议。阈值 25%：低于它时样本里的噪声比信号大，给专项建议等于瞎指方向。"""
    if share < 0.25:
        return None
    return {
        "tag": "ability",
        "title": "降速练，先把手感稳下来",
        "body": "用 0.85~0.9 倍速把这张图过顺，能稳定到 99% 再回原速。目标是先把「稳」抬上去，"
        "而不是硬冲——硬冲只会把随机误差练成肌肉记忆。",
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
            "title": "把 offset 校掉这 %.0fms" % abs(median_delta),
            "body": "整局中位%s %.1fms 且换密度也不改方向，这是判定时机没对齐而不是手抖。"
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
        "body": "整局中位%s %.1fms，方向一致但幅度只有个位数——一张图不足以判定是延迟问题，"
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


def _category_narrative(payload: dict) -> tuple[str, str, list[dict]]:
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
        "ability": "主要卡在速度/pattern 上：失误集中在密度高、要求快的地方，同一类片段他反复吃亏。",
        "rhythm": "主要是节奏对不齐：误差方向一致地偏，换到密度不同的地方也还是偏，属于「拍子没咬住」而不是手不稳。",
        "distraction": "主要是注意力掉线：失误成簇出现、前后都正常，还伴随几次手停下来的空白。",
        "slip": "主要是随机失手：这些失误彼此孤立、幅度不大、方向和难度上都看不出系统性——"
                "不是能力不够，也不是拍子没咬住，更像是手感/注意力的一次短暂抖动。",
    }[main]

    if total < 10:
        # 失误个位数时给「专项建议」等于瞎指方向：样本太小，占比一两条就跳到 25%
        return (text, detail, [])

    # 建议必须带上「这份回放里的具体数字」，否则就是放在谁的报告里都成立的空话。
    # 注意：一律不提 NPS —— 同样的 NPS、键型不同难度可以差一大截，
    # 按 NPS 给建议等于用错尺子。要说「练什么」就按 pattern 和具体片段说。
    advice = [
        a
        for a in (
            _advice_ability(counts.get("ability", 0) / total),
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

    评价由八块互不相干的判断拼成（准度、失误结构、分段、左右手、pattern、密度、
    断连、offset），每块往同样的四个列表里写。以前它们全挤在一个 200 行函数里，
    改其中一条建议要滑三屏才能确认有没有和别处打架——分成一人一块之后，
    顺序仍由调用顺序决定，输出不变。
    """

    payload: dict
    acc: float
    ur: float
    median_delta: float
    has_bias: bool
    judge_mode: str = "v1"
    paragraphs: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    advice: list[dict] = field(default_factory=list)

    # 1) 准度与稳定性
    #    「准确率」标明 v1 口径（旁边还有 ScoreV2 的准确率，不标容易被当成同一个数）
    def add_accuracy(self) -> None:
        s = self.payload.get("summary", {})
        bm = self.payload.get("beatmap", {})
        self.paragraphs.append(
            "这张 %s 键的「%s」难度，你打出了 %.2f%% 的准确率（%s 口径）、UR %.0f。"
            "击打偏差中位 %.1fms，离散程度属于「%s」。"
            % (bm.get("keys", "?"), bm.get("difficulty", "?"), self.acc, self.judge_mode, self.ur,
               _f(s.get("absMedian")), _stability_word(self.ur))
        )
        bias_text, _ = _bias_sentence(self.median_delta)
        self.paragraphs.append(bias_text)

        # 条件放宽到「说得出口就写」，别让这一栏常年只有一条破折号
        notes_total = max(1, _f(bm.get("notes"), 1))
        max_combo = _f(s.get("maxCombo"))
        acc_strength = _acc_strength(self.acc, self.judge_mode)
        if acc_strength:
            self.strengths.append(acc_strength)
        else:
            # 90% 以下不再是亮点：这张图对他来说是超纲的，硬刷只会固化错误手感
            self.weaknesses.append(
                "准确率（%s）%.2f%% 在 90%% 以下，这张图目前超出你的能力承受范围：建议先降速（0.85 倍）"
                "或换低一档难度，把准确率做到 93%% 以上再回来。" % (self.judge_mode, self.acc)
            )
        if self.ur and self.ur < 110:
            self.strengths.append("稳定性好（UR %.0f），说明手指控制已经成型。" % self.ur)
        if abs(self.median_delta) < 5 and (self.ur > 0 or self.acc > 0):
            self.strengths.append("拍子咬得准（中位偏差 %+.1fms），节奏骨架没有问题。" % self.median_delta)
        if max_combo / notes_total >= 0.7:
            self.strengths.append(
                "最长连击 %d（占全部物件的 %.0f%%），中途基本没崩。" % (int(max_combo), max_combo / notes_total * 100)
            )
        fast_bin = _best_fast_bin(self.payload)
        if fast_bin is not None:
            self.strengths.append("最快的那段失误率也只有 %.0f%%，速度不是当前的瓶颈。" % (fast_bin["errorRate"] * 100))
        if self.ur > 180:
            self.weaknesses.append("偏差波动大（UR %.0f），同一处会一会儿偏早一会儿偏晚。" % self.ur)

    # 2) 失误结构
    def add_categories(self) -> None:
        cat_text, cat_detail, cat_advice = _category_narrative(self.payload)
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
        if worst_sec and worst_sec["acc"] < self.acc - 1.5:
            self.weaknesses.append(
                "第 %d 段（%s–%s）明显掉准（%.1f%%），这一段值得单独复练。"
                % (worst_sec["index"] + 1, _fmt_mmss(worst_sec["start"]), _fmt_mmss(worst_sec["end"]), worst_sec["acc"])
            )
            self.advice.append(
                {
                    "tag": "ability",
                    "title": "单独复练 %s–%s 这一段" % (_fmt_mmss(worst_sec["start"]), _fmt_mmss(worst_sec["end"])),
                    "body": "这一段准确率只有 %.1f%%（全曲 %.1f%%）。把它单独循环：先 0.9 倍速过 3 遍找准手感，"
                    "再回原速；连续 3 次原速不掉连再往下走。" % (worst_sec["acc"], self.acc),
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
            "还有一段值得单独说：%s 起连续 %d 段都是「%s」，前 6 段的失误率是 %.0f%%，"
            "之后升到 %.0f%%（多 %.0f 个百分点，同期密度没有明显上升）。"
            "同样的写法打着打着掉准，是耐力撑不住，不是这一段突然变难。"
            % (start, st.get("blocks", 0), st.get("label", ""),
               _f(st.get("baseRate")) * 100, _f(st.get("testRate")) * 100,
               _f(st.get("rise")) * 100)
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
                "第 %d 列失误率 %.1f%%，是全谱最不稳的落点。" % (col["column"] + 1, _f(col.get("errorRate")) * 100)
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
            "Quadstream": "Quadstream（四押切）罕见，先降速确保四指同时落键，别用整只手砸键。",
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
        density_text = _density_ceiling(self.payload)
        if density_text:
            self.paragraphs.append(density_text)

    # 7) 组合断点
    def add_combo(self) -> None:
        breaks = self.payload.get("comboBreaks", [])
        if not breaks:
            self.strengths.append("全程没有断连。")
            return
        first = breaks[0]
        self.paragraphs.append(
            "全曲断了 %d 次组合，第一次断在 %d:%02d（第 %d 列）。"
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
                "body": "整局中位偏差 %.1fms 且方向一致，先在 osu! 里用 universal offset 微调（或打一张短图确认手感），"
                "比硬练更省时间。" % self.median_delta,
            },
        )


def build_evaluation(payload: dict) -> dict:
    s = payload.get("summary", {})
    acc = _f(s.get("accuracy"))
    ur = _f(s.get("ur"))
    median_delta = _f(s.get("medianDelta"))

    draft = _Draft(
        payload=payload,
        acc=acc,
        ur=ur,
        median_delta=median_delta,
        has_bias=_bias_sentence(median_delta)[1],
        judge_mode=str(payload.get("judgeMode") or "v1"),
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
                "body": "这一档已经很稳了，可以去试难度高 0.5 星左右的图；把「稳定」和「上限」分开练。",
            }
        )

    grade, grade_desc = _grade(acc, ur, _f(s.get("errorRate")))
    headline_map = {
        "ability": "速度是当前的天花板",
        "rhythm": "拍子没咬住，手其实是稳的",
        "distraction": "注意力比手先掉线",
        "slip": "手是稳的，掉的是零星几下",
    }
    cats = payload.get("categories", {}).get("counts", {})
    main_cat = max(cats.items(), key=lambda kv: kv[1])[0] if cats else "ability"
    headline = headline_map[main_cat]
    if grade in ("S", "A+"):
        headline = "这一局很干净，" + headline.replace("是当前的天花板", "还有余量")

    return {
        "grade": grade,
        "gradeDesc": grade_desc,
        "headline": headline,
        "paragraphs": [p for p in draft.paragraphs if p],
        "strengths": draft.strengths[:5],
        "weaknesses": draft.weaknesses[:4],
        "advice": advice[:5],
        "badges": [
            # 顶部的准确率跟着当前判定口径走；旁边还挂着 ScoreV2 的独立评分，
            # 不标口径的话两个数字很容易被当成同一个。
            {"key": "acc", "label": "准确率 (%s)" % draft.judge_mode, "value": "%.2f%%" % acc},
            {"key": "ur", "label": "UR", "value": "%.0f" % ur},
            {"key": "bias", "label": "中位偏差", "value": "%+.1fms" % median_delta},
            {"key": "combo", "label": "最大连击", "value": str(s.get("maxCombo", 0))},
        ],
    }
