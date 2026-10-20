"""失误归因：把每个「有瑕疵的击打」判成 分神 / 节奏感弱 / 超出能力范围。

设计原则
--------
1. **只用可观测、可复现的量**：误差、局部密度、pattern、邻域行为、同类片段的历史表现。
   没有黑箱；每条判据都能在报告里说清"凭什么这么判"。
2. **三类的判别边界**：
   - 超出能力：失误集中在高密度 / 高难度 pattern 上，**同类片段里他反复吃亏**，
     且错误率随密度上升；成片出现、不像是"掉了一下线"。
   - 节奏感弱：误差**方向稳定地偏**（同一段一直偏早或偏晚），换到不同密度也照样偏；
     失误散布而不是成簇。
   - 分神：失误**成簇突发**、伴随长时间没有输入、难度不高、之后立刻恢复；
     同一类片段平时并不失误。
3. **互相制衡**：三类各自算分，取最高者；证据不足时置信度低，报告里如实标注。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from . import patterns
from .match import JudgedNote, MatchResult
from .metrics import (
    clamp,
    density_error_profile,
    mean,
    median,
    runs_test,
    stdev,
    trend_significance,
)

CATEGORY_LABEL = {
    "ability": "超出能力范围",
    "rhythm": "节奏感弱",
    "distraction": "分神",
    # 第四类：三类签名都不成立的孤立小失误。它必须单独存在 —— 真实回放里最常见
    # 的瑕疵就是"手抖了一下"，把它硬塞进另外三类，会让高手局的分布全是"分神"。
    "slip": "随机失手",
}

# ---------------------------------------------------------------------------
# 可重拟合参数
#
# 这几个数不是"看着像"定的，是在「真实谱面 + 已知真相」的合成集上做坐标搜索选出来的
# （目标：三类等权的 macro-F1）。改动它们之前先重跑一次归因评测，否则很容易
# 把某一类调好、另一类调坏。
# ---------------------------------------------------------------------------
P_BIN_EXCESS = 0.10        # 绝对失误率要比全谱基线高出多少，才算"这一档是墙"
P_REL_ATTEN = 0.30         # 没到自己的水位时，相对分位项保留多少
P_NO_SIG_PENALTY = 0.45    # 分神没有任何"断片签名"时的衰减
P_RHYTHM_BG = 0.0          # 失误不成簇时的背景分（重拟合结果：给分会挤掉分神类）

CATEGORY_COLOR = {
    "ability": "#ff9f6e",
    "rhythm": "#6ee7ff",
    "distraction": "#c792ff",
    "slip": "#8b93b5",
}


@dataclass
class ErrorFeature:
    note: JudgedNote
    ctx: patterns.NoteContext | None
    density: float = 0.0
    density_pct: float = 0.0
    difficulty: float = 0.0
    difficulty_pct: float = 0.0
    pattern: str = "散音"
    # 所属大类的名字（叠 / 乱 / 切 / tech / 长条 / 过渡），给"同类片段"这类说法用
    category: str = ""
    subdivision: float = 99.0

    errors_near_1_5s: int = 0
    errors_near_4s: int = 0
    gap_before: float = 0.0
    gap_before_ratio: float = 0.0     # gap_before / 该处的典型间隔
    gap_after: float = 0.0

    recent_mean: float = 0.0
    recent_abs_median: float = 0.0
    baseline_abs: float = 0.0        # 这个玩家的常态离散（全局 |误差| 中位），用于判断"突然偏很远"
    recent_stdev: float = 0.0
    recent_sign_consistency: float = 0.5
    passage_bias: float = 0.0
    passage_ur: float = 0.0
    passage_error_rate: float = 0.0   # ±4s 窗口内的失误比例

    density_bin_err: float = 0.0      # 与它同密度档的失误率（局部能力墙的直接证据）
    dens_corr: float = 0.0            # 局部窗口内 |误差| 与密度的相关系数（能力墙的经典指纹）
    out_frac: float = 0.0             # 局部窗口内的离群误差占比（分神的经典指纹）
    sig_err_rate: float = 0.0
    sig_count: int = 0
    sig_rel: float = 0.0              # 同类片段失误率 / 全局失误率（>1 表示这是他的常规弱点）

    recovery: float = 0.0

    # ---- 「能力崩盘」的两个直接指纹（用户指定必须优先判能力） ----
    jack_run_len: int = 0             # 本失误所在同列连打链的长度（>=3 才算）
    jack_run_pos: int = 0             # 链内位置（0 = 第一个音）
    jack_collapse: bool = False       # 链的后半程才爆 200/漏：前面的音都打干净了
    late_streak: int = 0              # 漏接前连续偏晚（>10ms）的击打数
    late_collapse: bool = False       # 连续偏晚之后直接漏接


@dataclass
class Attribution:
    note: JudgedNote
    category: str
    scores: dict[str, float]
    confidence: float
    reasons: list[str] = field(default_factory=list)
    secondary: str | None = None      # 次要成因（两类都很显著时给出来，避免"单一标签"的误导）

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "label": CATEGORY_LABEL.get(self.category, self.category),
            "color": CATEGORY_COLOR.get(self.category, "#999"),
            "confidence": round(self.confidence, 2),
            "scores": {k: round(v, 3) for k, v in self.scores.items()},
            "reasons": self.reasons,
            "secondary": self.secondary,
            "secondaryLabel": CATEGORY_LABEL.get(self.secondary, "") if self.secondary else "",
        }


# --------------------------------------------------------------------------- 特征


def _rolling_stats(notes: list[JudgedNote], k: int = 8):
    out_mean = [0.0] * len(notes)
    out_absmed = [0.0] * len(notes)
    out_std = [0.0] * len(notes)
    for i in range(len(notes)):
        seg = [n.delta for n in notes[max(0, i - k) : i] if n.delta is not None]
        if seg:
            out_mean[i] = mean(seg)
            out_absmed[i] = median([abs(d) for d in seg])
            out_std[i] = stdev(seg)
    return out_mean, out_absmed, out_std


def _build_jack_chains(
    notes: list[JudgedNote], by_obj: dict[int, patterns.NoteContext]
) -> tuple[list[int], dict[int, list[int]]]:
    """同列连打链：同列、相邻间隔够快（`patterns.is_jack_gap`，按各自拍长判断）的连续段。

    判定序列已按时间排好，逐列记住上一条即可 O(n) 建链——
    逐条往回扫会在同列密集的谱面上退化成 O(n²)。

    阈值用谱面上下文里的拍长而不是固定毫秒：慢歌的 1/8 与快歌的 1/16
    在绝对时间上可以相等，只有按拍长算才能判断"这是不是同一档手速"。
    """
    prev_same_col = [-1] * len(notes)
    last_in_col: dict[int, int] = {}
    for i, n in enumerate(notes):
        j = last_in_col.get(n.column, -1)
        if j >= 0:
            ctx = by_obj.get(n.obj_index)
            gap = n.note_time - notes[j].note_time
            if patterns.is_jack_gap(gap, ctx.row_gap if ctx else 0.0,
                                   ctx.beat_length if ctx else 0.0,
                                   ctx.chord_size if ctx else 1):
                prev_same_col[i] = j
        last_in_col[n.column] = i

    chain_id = [-1] * len(notes)
    chain_members: dict[int, list[int]] = {}
    for i in range(len(notes)):
        if prev_same_col[i] == -1:
            chain_id[i] = i
            chain_members[i] = [i]
        else:
            chain_id[i] = chain_id[prev_same_col[i]]
            chain_members[chain_id[i]].append(i)
    return chain_id, chain_members


def _late_streaks(notes: list[JudgedNote]) -> list[int]:
    """漏接之前连续偏晚（>10ms）的击打数。

    从每个 miss 往回数——"被越拖越晚、最后一格没按到"这个形状只有往回看才看得见。
    "整局本来就偏晚"的情况由 _fill_fingerprints 里的 drift 门槛排除，不在这里处理：
    这里只回答"漏接之前连了几次偏晚"，答案是否构成能力指纹要结合整局基线判断。
    """
    out = [0] * len(notes)
    for i, n in enumerate(notes):
        if n.judgement != "miss":
            continue
        k = i - 1
        while k >= 0:
            d = notes[k].delta
            if d is None or d <= 10.0:
                break
            out[i] += 1
            k -= 1
    return out


def _signature_stats(notes: list[JudgedNote], by_obj: dict, sig_edges: list[float]) -> dict[tuple, list[int]]:
    """同类片段的历史表现：签名 → [总条数, 失误条数]。"""
    stats: dict[tuple, list[int]] = {}
    for n in notes:
        c = by_obj.get(n.obj_index)
        if c is None:
            continue
        bucket = stats.setdefault(patterns.signature(c, sig_edges), [0, 0])
        bucket[0] += 1
        if n.is_flaw:
            bucket[1] += 1
    return stats


class _WindowStats:
    """±8 秒窗口里「|误差| 与密度的相关性」和「离群误差占比」。

    这两个量描述的是"附近这一片"，按 500ms 分桶缓存即可：失误密集时同一秒会重合很多次，
    逐个失误重扫窗口在 4000+ 失误的谱面上要 800ms，缓存后降到几十毫秒。
    """

    def __init__(self, notes: list[JudgedNote], note_times: list[float], by_obj: dict):
        self._notes = notes
        self._times = note_times
        self._by_obj = by_obj
        self._cache: dict[int, tuple[float, float]] = {}

    def at(self, t: float) -> tuple[float, float]:
        key = int(t // 500)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        center = key * 500 + 250
        lo = bisect.bisect_left(self._times, center - 8000)
        hi = bisect.bisect_right(self._times, center + 8000)
        pairs = []
        for x in self._notes[lo:hi]:
            cx = self._by_obj.get(x.obj_index)
            if cx is not None and x.delta is not None:
                pairs.append((cx.density_1s, abs(x.delta)))

        corr_v = 0.0
        out_v = 0.0
        if len(pairs) >= 10:
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            mx, my = mean(xs), mean(ys)
            sx = stdev(xs)
            sy = stdev(ys)
            if sx > 1e-6 and sy > 1e-6:
                cov = sum((a - mx) * (b - my) for a, b in pairs) / len(pairs)
                corr_v = clamp(cov / (sx * sy), -1.0, 1.0)
            med = median(ys)
            mad = median([abs(v - med) for v in ys]) or 1.0
            rsd = mad / 0.6745 or 1.0
            out_v = sum(1 for v in ys if abs(v - med) > 3.0 * rsd) / len(ys)
        self._cache[key] = (corr_v, out_v)
        return corr_v, out_v


@dataclass
class _FeatureContext:
    """算特征时全谱范围的共享数据。

    这些量对每个失误都一样，循环里重算一遍既慢又容易写出两套口径，
    所以统一在 build_features 里算一次，用这个对象传下去。
    """

    notes: list[JudgedNote]
    by_obj: dict
    presses: list[float]
    note_times: list[float]
    flaw_times: list[float]
    recent_mean: list[float]
    recent_absmed: list[float]
    recent_std: list[float]
    sig_edges: list[float]
    sig_stats: dict[tuple, list[int]]
    global_rate: float
    global_bias: float
    global_abs_median: float
    late_fraction: float
    density_sorted: list[float]
    weight_sorted: list[float]
    weight_by_index: dict[int, float]
    profile_bins: list[dict]
    chain_id: list[int]
    chain_members: dict[int, list[int]]
    late_streak: list[int]
    window: _WindowStats

    def pct(self, value: float, vals: list[float]) -> float:
        if not vals:
            return 0.0
        return bisect.bisect_left(vals, value) / len(vals)


def _build_feature_context(match: MatchResult, contexts: list[patterns.NoteContext], profile_bins: list[dict]) -> _FeatureContext:
    notes = match.notes
    by_obj = {c.index: c for c in contexts}
    # 同一个纯函数算两遍（分位排序 + 逐条赋值）会得到两份可能不同步的数，所以只算一遍
    weight_by_index = {c.index: patterns.difficulty_weight(c) for c in contexts}
    recent_mean, recent_absmed, recent_std = _rolling_stats(notes)
    sig_edges = patterns.signature_edges(contexts, n_bins=3) if contexts else []
    chain_id, chain_members = _build_jack_chains(notes, by_obj)
    return _FeatureContext(
        notes=notes,
        by_obj=by_obj,
        presses=sorted(n.press_time for n in notes if n.press_time is not None),
        note_times=[n.note_time for n in notes],
        flaw_times=[n.note_time for n in notes if n.is_flaw],
        recent_mean=recent_mean,
        recent_absmed=recent_absmed,
        recent_std=recent_std,
        sig_edges=sig_edges,
        sig_stats=_signature_stats(notes, by_obj, sig_edges),
        global_rate=sum(1 for n in notes if n.is_flaw) / max(1, len(notes)),
        # 整局中位偏差：用来区分"整局都在偏"（节奏）和"这一带比平常更晚"（被拖垮）
        global_bias=median([n.delta for n in notes if n.delta is not None]),
        global_abs_median=median([abs(n.delta) for n in notes if n.delta is not None]),
        # 整局有多大比例的音"明显偏晚"。用来区分"整局都晚"（节奏）和"某一段被拖垮"（能力）
        late_fraction=(
            sum(1 for n in notes if n.delta is not None and n.delta > 10.0)
            / max(1, sum(1 for n in notes if n.delta is not None))
        ),
        density_sorted=sorted(c.density_1s for c in contexts) if contexts else [],
        weight_sorted=sorted(weight_by_index.values()),
        weight_by_index=weight_by_index,
        profile_bins=profile_bins,
        chain_id=chain_id,
        chain_members=chain_members,
        late_streak=_late_streaks(notes),
        window=_WindowStats(notes, [n.note_time for n in notes], by_obj),
    )


def _fill_context_feature(f: ErrorFeature, ctx: _FeatureContext) -> None:
    """这一击"处在什么难度的什么地方"。"""
    c = f.ctx
    if c is None:
        return
    f.density = c.density_1s
    f.density_pct = ctx.pct(c.density_1s, ctx.density_sorted)
    f.difficulty = ctx.weight_by_index.get(c.index, patterns.difficulty_weight(c))
    f.difficulty_pct = ctx.pct(f.difficulty, ctx.weight_sorted)
    f.pattern = patterns.pattern_label(c)
    f.category = patterns.CATEGORY_LABEL.get(c.coarse, "") or f.pattern
    f.subdivision = c.subdivision


def _fill_neighborhood(f: ErrorFeature, i: int, ctx: _FeatureContext) -> None:
    """这一击前后的行为：失误密度、停顿、滚动偏差、局部窗口、之后是否恢复。"""
    notes = ctx.notes
    n = notes[i]
    t = n.note_time

    f.errors_near_1_5s = bisect.bisect_right(ctx.flaw_times, t + 1500) - bisect.bisect_left(ctx.flaw_times, t - 1500) - 1
    f.errors_near_4s = bisect.bisect_right(ctx.flaw_times, t + 4000) - bisect.bisect_left(ctx.flaw_times, t - 4000) - 1

    idx = bisect.bisect_left(ctx.presses, t)
    prev_press = ctx.presses[idx - 1] if idx > 0 else None
    next_press = ctx.presses[idx] if idx < len(ctx.presses) else None
    has_prev = prev_press is not None
    f.gap_before = (t - prev_press) if has_prev else 0.0
    f.gap_after = (next_press - t) if next_press is not None else 4000.0
    typical = 1000.0 / max(1.0, f.density)
    # 没有"上一次按键"时不认为存在空白（开局第一击不能算走神）
    f.gap_before_ratio = (f.gap_before / max(60.0, typical)) if has_prev else 0.0

    f.recent_mean = ctx.recent_mean[i]
    f.recent_abs_median = ctx.recent_absmed[i]
    # 分神窗口里连续几击都偏得很远时，"最近几击的中位"本身已经被污染了，
    # 拿它当基准会让窗口内的每一击都不算"突然"。取两者较小值当常态基线。
    f.baseline_abs = min(f.recent_abs_median, ctx.global_abs_median) if f.recent_abs_median else ctx.global_abs_median
    f.recent_stdev = ctx.recent_std[i]

    seg = [x.delta for x in notes[max(0, i - 8) : i] if x.delta is not None]
    if seg and n.delta is not None:
        f.recent_sign_consistency = sum(1 for d in seg if (d < 0) == (n.delta < 0)) / len(seg)

    lo = bisect.bisect_left(ctx.note_times, t - 4000)
    hi = bisect.bisect_right(ctx.note_times, t + 4000)
    win = notes[lo:hi]
    wd = [x.delta for x in win if x.delta is not None]
    if wd:
        f.passage_bias = median(wd)
        f.passage_ur = stdev(wd) * 10
    if win:
        f.passage_error_rate = sum(1 for x in win if x.is_flaw) / len(win)

    for b in ctx.profile_bins:
        if b["low"] <= f.density <= b["high"]:
            f.density_bin_err = b["errorRate"]
            break

    if f.ctx is not None:
        key = patterns.signature(f.ctx, ctx.sig_edges)
        total, errs = ctx.sig_stats.get(key, (0, 0))
        f.sig_count = total
        f.sig_err_rate = (errs / total) if total else 0.0
        f.sig_rel = (f.sig_err_rate / ctx.global_rate) if ctx.global_rate > 0 else 0.0

    f.dens_corr, f.out_frac = ctx.window.at(t)

    after = notes[i + 1 : i + 7]
    if after:
        f.recovery = sum(1 for x in after if not x.is_flaw) / len(after)


def _fill_fingerprints(f: ErrorFeature, i: int, ctx: _FeatureContext) -> None:
    """两个"能力崩盘"的直接指纹（用户要求这两种表现优先判能力）。"""
    # 指纹 1：同列连打链「后半程才崩」
    # 链 >=3、这条不是链头、本身爆 200 或漏，且链里排在它前面的音全部干净（severity 0）。
    # 前面都打得到、打到最后跟不上 —— 手指耐力不足的直接证据。
    members = ctx.chain_members.get(ctx.chain_id[i], [i])
    run_len = len(members)
    run_pos = members.index(i)
    # ≥4 连、且至少第 3 个才崩：3 连里"前面只有 1 个干净音"不构成"耐力跟不上"，
    # 那更可能只是这一下没打准（实测 3 连会在整局偏早的回放里被大量误触发）
    if run_len >= 4 and run_pos >= 2 and f.note.judgement in ("good", "miss"):
        head = members[:run_pos]
        if all(ctx.notes[j].severity == 0 for j in head):
            f.jack_run_len = run_len
            f.jack_run_pos = run_pos
            f.jack_collapse = True

    # 指纹 2：连续偏晚之后漏接
    # 手已经被越拖越晚、最后一格直接没按到。这是跟不上的表现，
    # 而不是"偏了一个固定值"的节奏问题（固定偏移不会以漏接收尾）。
    #
    # 关键限定：必须是**这一带比整局更晚**，且是逐次变晚（见 _late_streaks）。
    # 整局均匀偏晚 70ms 的人，每个漏接前面都"连续偏晚"——那是节奏的形状，
    # 不是能力上限的形状。没有这个限定，系统性偏移反而会被判成能力。
    f.late_streak = ctx.late_streak[i]
    # 什么算"整局都偏晚"：既要有足够多的音偏晚（≥50%），偏移本身也要够大（≥25ms）。
    # 两个条件缺一不可 —— 小样本里"4 个音里 3 个偏 15ms"不是整局偏晚，
    # 而真正系统性偏 50ms 的局里，每个漏接前面都"连续偏晚"，那是节奏的形状。
    global_late = ctx.late_fraction >= 0.5 and abs(ctx.global_bias) >= 25.0
    f.late_collapse = f.late_streak >= 3 and not global_late


def build_features(
    match: MatchResult, contexts: list[patterns.NoteContext], profile_bins: list[dict] | None = None
) -> list[ErrorFeature]:
    """给每个"有瑕疵"的击打算一组可解释的特征（完美击打不需要归因，直接跳过）。"""
    if not match.notes:
        return []
    ctx = _build_feature_context(match, contexts, profile_bins or [])

    feats: list[ErrorFeature] = []
    for i, n in enumerate(ctx.notes):
        if not n.is_flaw:
            continue
        f = ErrorFeature(note=n, ctx=ctx.by_obj.get(n.obj_index))
        _fill_context_feature(f, ctx)
        _fill_neighborhood(f, i, ctx)
        _fill_fingerprints(f, i, ctx)
        feats.append(f)
    return feats


# --------------------------------------------------------------------------- 打分


def _calm_rate(profile: dict) -> float:
    """这个人"打得顺"时的失误率：取样本足够的密度档里最低的那一档。

    用它当能力墙的基线，而不是整局平均失误率 —— 后者会被墙本身抬高。
    """
    bins = [b for b in (profile.get("density_profile") or []) if int(b.get("count") or 0) >= 10]
    if not bins:
        return float(profile.get("flaw_rate", 0.0) or 0.0)
    return min(float(b.get("errorRate", 0.0) or 0.0) for b in bins)


def _ability_density_terms(f: ErrorFeature, profile: dict, reasons: list[str]) -> tuple[float, float, float]:
    """能力证据之一：绝对密度有没有越过自己的稳定上限、这一档失误率高出多少。

    返回（这部分得分, structural, bin_err）。后两个值后面还要用：
    structural 描述"这一档的高失误率是结构性的、还是几秒走神临时抬起来的"，
    bin_err 是"这一档确实比平时难"的强度，第 7 条拿它当门。
    """
    s = 0.0
    # 1) 绝对密度是否越过"自己的稳定上限"
    ceiling = float(profile.get("ceiling", 0.0) or 0.0)
    excess = clamp((f.density - ceiling) / max(2.0, ceiling * 0.6), 0.0, 1.0) if ceiling > 0 else 0.0
    s += 0.26 * excess
    if excess >= 0.35:
        # 措辞上刻意不写「你的稳定上限是 X NPS」：那句话暗含"换个谱也成立"，
        # 而单张图推不出这个结论（同 NPS 不同键型难度能差一大截）。
        # 这里只说"本局"——这局的密度-失误剖面确实在这里拐了个弯。
        reasons.append("这一段密度 %.1f NPS，超过了本局的稳定水位（约 %.1f NPS）" % (f.density, ceiling))

    # 2) 这一密度档的失误率相对全谱平均高多少（倍数，而不是绝对百分比）
    #    用绝对阈值（>=10%）等于假设"这局一定有大量失误"：99% 准确率的局里
    #    最差的一档也就 3%，永远拿不到分，能力证据被系统性埋掉。
    #    两种口径取大者：高失误率的局看绝对档（50% 就是墙），
    #    高准确率的局看倍数（3% vs 全谱 1.2% 同样是墙）。
    #
    #    但绝对档必须先**减掉全谱基线**：整局系统性偏 50ms 的局，90% 的音都是
    #    瑕疵，每一档的失误率都是 0.9——那不是"这一档特别难"，而是"整局都这样"。
    #    不减基线的话，每一条失误都能白拿 0.22，节奏问题会被整段判成能力墙。
    # 基线的选择很关键：不能用"整局平均失误率"——能力墙会把平均值一起抬高，
    # 于是墙越高、基线越高、lift 越接近 1，最后什么都测不出来（实测 ability_dense
    # 的召回从 0.8 掉到 0.42 就是这个原因）。用**最轻松那一档**的失误率当基线：
    # 它代表"这个人打得顺的时候有多准"，墙是相对它立起来的。
    floor_rate = _calm_rate(profile)
    base_rate = max(0.004, floor_rate)
    bin_lift = f.density_bin_err / base_rate
    excess = f.density_bin_err - base_rate
    bin_abs = clamp((excess - P_BIN_EXCESS) / 0.30, 0.0, 1.0)
    # 相对倍数才是主判据：能力墙的形状是"高密度档的失误率明显高于其它档"，
    # 不管整局是 3% 还是 40% 的失误都成立。1.4 倍起步、2.6 倍封顶。
    bin_rel = clamp((bin_lift - 1.4) / 1.2, 0.0, 1.0)
    bin_err = max(bin_abs, bin_rel)
    # 但"这一档失误率高"也可能是**几秒走神**造成的：走神窗口会把它覆盖的那些
    # 密度档一起抬高，制造出一个假的能力墙。区分点是时间局部性 ——
    # 附近这一小段明显比整局烂，那是断片；整局各处都烂，才是墙。
    global_flaw = max(0.01, float(profile.get("flaw_rate", 0.0) or 0.0))
    spike = clamp((f.passage_error_rate - 2.0 * global_flaw) / max(0.05, global_flaw), 0.0, 1.0)
    structural = 1.0 - 0.6 * spike
    s += 0.22 * bin_err * structural
    if bin_lift >= 2.0:
        reasons.append("由于谱面密度较大导致的失误")
    return s, structural, bin_err


def _ability_pattern_terms(f: ErrorFeature, profile: dict, reasons: list[str]) -> float:
    """相对分位、同类片段、键型要求、全谱趋势、误差随密度上升，这五项都不需要 structural。"""
    s = 0.0

    # 3) 相对分位（只在"整体没崩、且这一处确实顶到自己的水位"时有意义）。
    #    单看分位是危险的：任何一张图上总有 45% 的音处在"相对更难"的位置，
    #    无条件加分等于给每条失误都发一份能力分。所以用 ceiling 做门：
    #    没接近自己的稳定上限时，这两项只保留三成。
    ceiling = float(profile.get("ceiling", 0.0) or 0.0)
    dens = clamp((f.density_pct - 0.55) / 0.45, 0.0, 1.0)
    diff = clamp((f.difficulty_pct - 0.55) / 0.45, 0.0, 1.0)
    at_ceiling = ceiling <= 0 or f.density >= ceiling * 0.85
    s += (0.14 * dens + 0.10 * diff) * (1.0 if at_ceiling else P_REL_ATTEN)
    if f.density_pct >= 0.85 and at_ceiling:
        reasons.append("密度处在全谱 %d%% 分位" % round(f.density_pct * 100))

    # 4) 同类片段是不是他的常规弱点（全局失误率不高时才有区分度）
    if f.sig_count >= 5:
        rel = clamp((f.sig_rel - 1.1) / 2.2, 0.0, 1.0)
        s += 0.16 * rel
        if f.sig_rel >= 2.0 and f.sig_err_rate >= 0.12:
            reasons.append("同类片段（%s）失误率 %.0f%%，是全局平均的 %.1f 倍" % (
                f.category or f.pattern, f.sig_err_rate * 100, f.sig_rel))

    # 5) pattern 本身的要求
    if f.ctx is not None:
        pat = 0.0
        if f.ctx.is_jack:
            pat = 1.0
        elif f.ctx.is_trill:
            pat = 0.75
        elif f.ctx.chord_size >= 3:
            pat = 0.55
        elif f.ctx.is_stream:
            pat = 0.45
        s += 0.10 * pat
        if pat >= 0.75:
            reasons.append("这里是%s，对速度要求最高" % f.pattern)

    # 6) 整局的密度—失误趋势
    trend = profile.get("trend", {})
    if trend.get("slope_score", 0) > 0.1:
        s += 0.08 * clamp(trend["slope_score"], 0.0, 1.0)
        if trend.get("jump", 0) > 0.10:
            reasons.append("高密度段的失误率比低密度段高 %.0f 个百分点" % (trend["jump"] * 100))

    # 6.5) |误差| 随密度上升（能力墙的经典指纹）
    corr_gain = clamp((f.dens_corr - 0.22) / 0.35, 0.0, 1.0)
    s += 0.14 * corr_gain
    if f.dens_corr >= 0.35:
        reasons.append("这一带误差大小随难度上升（相关 %.2f），是典型的能力墙特征" % f.dens_corr)
    return s


def _ability_collapse_terms(f: ErrorFeature, reasons: list[str]) -> float:
    """两个「能力崩盘」指纹：同列链后半程才崩、连续偏晚之后漏接。

    这两种形状只在"手跟不上"时出现，所以单项权重给得高；
    _decide() 里还会把它们抬成能力类的地板分。
    """
    s = 0.0
    # 8) 同列连打链「后半程才崩」（用户指定优先判能力）
    #    同列连打对的是**手指爆发与耐力**：链开头还能跟上、拉到一半开始爆 200/漏，
    #    说明手上限就在这条链附近——这跟"偏了一个固定值"的节奏问题是两回事。
    if f.jack_collapse:
        s += 0.28
        reasons.append(
            "纵连（%d 连）打到最后才崩：前面 %d 个都打干净了，这一下直接%s，"
            "是手指耐力跟不上这条链，不是拍子没对齐"
            % (f.jack_run_len, f.jack_run_pos, "漏了" if f.note.judgement == "miss" else "爆了 200")
        )

    # 9) 连续偏晚之后漏接（用户指定优先判能力）
    #    被越拖越晚、最后一格彻底没按到——固定偏移不会以"漏"收尾，这只能是跟得上/跟不上的问题。
    if f.late_collapse:
        s += 0.26
        reasons.append(
            "漏接之前连续 %d 次击打都偏晚（>10ms），手已经被拖出节奏、最后一格没按到，属于速度上限" % f.late_streak
        )
    return s


def _score_ability(f: ErrorFeature, profile: dict) -> tuple[float, list[str]]:
    """超出能力范围：密度越线、这一档失误率高、同类片段反复吃亏、崩盘指纹。

    拆成三组是因为证据来源不同（单点密度 / 全谱结构 / 局部形状）。
    每组按原有顺序往同一个 reasons 列表里追加，报告里取前三条时不会换位。
    """
    reasons: list[str] = []
    s, structural, bin_err = _ability_density_terms(f, profile, reasons)
    s += _ability_pattern_terms(f, profile, reasons)

    # 7) 持续性：周围一大片也在错（用 bin_err 而不是原始失误率：
    #    整局都烂的时候"附近也在错"不含任何信息）
    if f.errors_near_4s >= 4 and bin_err >= 0.10:
        s += 0.08 * structural
        reasons.append("前后 4 秒内错了 %d 个，是一整段跟不上" % (f.errors_near_4s + 1))

    # 7b) 1.5 秒内连着错、而且中间手没停过：这一段整体跟不上。
    #     成串不是"断片"的形状——手一直在动却一直在错，是密度/速度越过了上限。
    #     （如果错之前手停过，那是分神/被打断，归分神类。）
    if f.errors_near_1_5s >= 2 and not (f.gap_before_ratio >= 2.5 and f.gap_before >= 900):
        s += 0.30
        reasons.append("1.5 秒内连着错了 %d 个（中间手没停），是这一段整体跟不上" % (f.errors_near_1_5s + 1))

    if f.recent_abs_median > 16:
        s += 0.06 * clamp((f.recent_abs_median - 16) / 30.0, 0.0, 1.0)

    # 7c) 耐力不足：整首的后 1/3 失误率明显爬升（并不是"这里难"，见 _stamina_profile）。
    #     只在**确实落到后段**的失误上给分，而且这一条本身四道闸门都过了才会出现在 profile 里，
    #     所以权重不必给太高——它解释的是"为什么这时候会错"，不是"这一击有多难"。
    st = profile.get("stamina") or {}
    if st.get("drop") and f.note.note_time >= st.get("lateFrom", float("inf")):
        s += 0.18
        reasons.append(st.get("reason") or "歌曲后段的失误明显增多，是耐力问题")

    s += _ability_collapse_terms(f, reasons)
    return clamp(s, 0.0, 1.0), reasons


def _score_rhythm(f: ErrorFeature, profile: dict) -> tuple[float, list[str]]:
    reasons: list[str] = []
    s = 0.0

    # 越难的地方，"整体偏晚"越可能是跟不上（能力），而不是拍子没对齐。
    # 但只按难度打折会把"整局都偏 90ms"这种真节奏问题也压下去，
    # 所以还要求"失误确实集中在这一档"（bin_lift 高）才打折：
    # 整局均匀偏移 → bin_lift ≈ 1 → 不打折；某一档特别容易错 → 才把偏移判给能力。
    hard = clamp((f.difficulty_pct - 0.45) / 0.55, 0.0, 1.0)
    base_rate = max(0.004, float(profile.get("flaw_rate", 0.0) or 0.0))
    bin_lift = f.density_bin_err / base_rate
    conc = clamp((bin_lift - 1.2) / 1.8, 0.0, 1.0)
    bias_gate = 1.0 - 0.55 * hard * conc

    bias = max(abs(f.passage_bias), abs(f.recent_mean) * 0.8)
    s += 0.26 * clamp(bias / 26.0, 0.0, 1.0) * bias_gate
    if abs(f.passage_bias) >= 10:
        reasons.append("这一带击打整体%s约 %.0fms" % ("偏晚" if f.passage_bias > 0 else "偏早", abs(f.passage_bias)))

    magnitude_gate = clamp(abs(f.recent_mean) / 12.0, 0.0, 1.0)
    cons = clamp((f.recent_sign_consistency - 0.62) / 0.38, 0.0, 1.0) * magnitude_gate
    s += 0.20 * cons * (1.0 - 0.35 * hard)
    if cons >= 0.7:
        reasons.append("最近 8 次击打几乎都在%s（%d%% 同向，均值 %.0fms）" % (
            "偏晚" if f.recent_mean > 0 else "偏早",
            round(f.recent_sign_consistency * 100),
            abs(f.recent_mean)))

    # 方差不大但均值偏移大 —— "拍子对不上"而不是"手抖"
    if f.recent_stdev > 0 and f.recent_abs_median > 0:
        ratio = clamp(f.recent_abs_median / max(4.0, f.recent_stdev), 0.0, 1.4) / 1.4
        s += 0.16 * ratio

    if f.ctx is not None and f.subdivision <= 0.26:
        s += 0.12 * max(magnitude_gate, clamp(bias / 26.0, 0.0, 1.0))
        if magnitude_gate >= 0.5:
            reasons.append("%s，试试再控一下速度" % ("打晚了" if f.recent_mean > 0 else "打早了"))

    # 全局偏移是「整局」层面的证据，它同时出现在每一个失误上，
    # 权重给高了会变成"只要整局有点偏，所有失误都算节奏"。
    glob = abs(profile.get("global_bias", 0.0))
    if glob >= 8:
        s += 0.08 * clamp(glob / 30.0, 0.0, 1.0)
        reasons.append("整局平均%s %.0fms" % ("偏晚" if profile.get("global_bias", 0) > 0 else "偏早", glob))

    # 失误是"散步"而不是"成簇" —— 支持节奏类而不是分神类。
    # 但这只是"背景"，不是这一击的证据：无条件加分会让每条失误都白拿 0.06，
    # 所以并进门控里（成簇时节奏本来就该让位给分神）。
    if not profile.get("runs", {}).get("clustered"):
        s += P_RHYTHM_BG

    return clamp(s, 0.0, 1.0), reasons


def _score_distraction(f: ErrorFeature, profile: dict) -> tuple[float, list[str]]:
    """分神：要有"注意力断了一下"的**形状**，而不是"这一击打得不好"。

    三类里最容易误判的就是它：失误之后恢复得好、附近有离群误差、这一段不难——
    这几条在**任何人**的失误上都成立（高准确率的局里尤其明显，因为剩下的失误本来就
    稀疏）。所以先判有没有签名，再谈打分：

      * 停顿：出错前手停了明显久于该处的正常间隔
      * 断片：这一击突然偏很远，紧接着又恢复正常
    三者全无时，分数整体打折（0.45），避免"什么都像分神"。

    注意「成串出错」不在签名之列：手一直在动却连着错，是跟不上这一段（能力），
    不是注意力断了。签名只剩「停过手」和「偏一下又回正」两种。
    """
    reasons: list[str] = []
    s = 0.0

    burst_n = f.errors_near_1_5s
    paused = f.gap_before_ratio >= 2.5 and f.gap_before >= 900
    jump = 0.0
    if f.note.delta is not None and f.baseline_abs > 0:
        jump = abs(abs(f.note.delta) - f.baseline_abs)
    broke = jump >= 40.0 and f.recovery >= 0.7

    # 注意：「1.5 秒内连着错几个」**不是**分神的证据。
    # 手一直在动却连着错，说明这一段跟不上（能力）；真正的分神要看"手停过"——
    # 停顿之后再错，或者突然偏很远然后立刻回正。把成串的错判成分神，
    # 等于把"能力不足"说成"走神"，两者的建议方向完全相反。
    has_signature = paused or broke

    # 停顿 + 成串：这才是"注意力断了一下"的完整形状
    if paused:
        s += 0.20
        s += 0.12 * clamp(burst_n / 3.0, 0.0, 1.0)
        reasons.append("出错前手停了 %.1f 秒（这里本来只隔 %.0fms 一个音）" % (
            f.gap_before / 1000.0, 1000.0 / max(1.0, f.density)))

    # 不难的地方却错了，而且同类片段平时不出错 —— 只有在有签名时才算证据
    if f.density_pct <= 0.5 and f.difficulty_pct <= 0.5:
        s += (0.16 if has_signature else 0.05)
        if f.sig_count >= 5 and f.sig_rel <= 1.2:
            s += (0.06 if has_signature else 0.02)
            if has_signature:
                reasons.append("这一处并不难，同类片段平时也基本不出错")

    # 突然偏一下，然后马上回正（本身就是签名）
    if broke:
        s += 0.14 * clamp(jump / 45.0, 0.0, 1.0)
        if jump >= 40 and f.recovery >= 0.7:
            reasons.append("这一击突然偏了 %.0fms，平时离散只有 %.0fms" % (
                abs(f.note.delta), f.baseline_abs))

    # "之后立刻恢复"只在**有断片签名**时才有信息量：孤立的 200 之后大家都会恢复
    if has_signature and burst_n >= 1:
        s += 0.12 * clamp((f.recovery - 0.6) / 0.4, 0.0, 1.0)
        if f.recovery >= 0.9:
            reasons.append("之后 6 个音立刻恢复正常")

    # 离群误差占比高 = 手本身稳、只是偶尔断片（窗口级证据，不要求本击是断片）
    s += 0.12 * clamp((f.out_frac - 0.02) / 0.08, 0.0, 1.0)
    if f.out_frac >= 0.05:
        reasons.append("这一带出现大量「突然偏很远」的单点，手本身是稳的")

    if profile.get("runs", {}).get("clustered"):
        s += 0.04

    if not has_signature:
        s *= P_NO_SIG_PENALTY
    return clamp(s, 0.0, 1.0), reasons


# --------------------------------------------------------------------------- 随机失手


P_SLIP_PRIOR = 0.20   # "随机失手"的先验：真实回放里它是常态，不是例外


def _score_slip(f: ErrorFeature, profile: dict) -> tuple[float, list[str]]:
    """随机失手：三类签名都不成立的**残差**。

    为什么必须有这一类：一次 200 可能只是手抖了一下 —— 没有停顿、没有成簇、
    方向也不固定、难度也不高。把它塞进另外三类里任何一类，都会让那一类的占比虚高
    （实测：不加这一类时，世界第 1 的 99.4% 局有 82% 的失误被判"分神"）。

    打分刻意是"扣分制"：先给先验，再看另外三类有没有签名；谁的签名出现，谁就不再是它。
    """
    reasons: list[str] = []
    ceiling = float(profile.get("ceiling", 0.0) or 0.0)
    calm_density = ceiling <= 0 or f.density < ceiling * 0.85

    s = P_SLIP_PRIOR
    # 孤立：1.5 秒内没有同伴（有同伴就更像某一类系统性问题）
    s += 0.18 * (1.0 - clamp(f.errors_near_1_5s / 2.0, 0.0, 1.0))
    # 幅度：偏得越小越像"手抖一下"；偏 80ms 以上基本不是随机的
    mag = abs(f.note.delta) if f.note.delta is not None else 60.0
    s += 0.16 * (1.0 - clamp(mag / 80.0, 0.0, 1.0))
    # 不在自己的水位上
    if calm_density:
        s += 0.10
    # 没有停顿
    if f.gap_before_ratio < 2.0:
        s += 0.08
    # 这一带整体是稳的
    s += 0.10 * (1.0 - clamp(f.passage_error_rate / 0.15, 0.0, 1.0))

    # 扣减：另外三类的签名出现时，随机失手让位
    if f.errors_near_1_5s >= 2 or (f.gap_before_ratio >= 2.5 and f.gap_before >= 900):
        s -= 0.20
    if abs(f.recent_mean) >= 12.0 and f.recent_sign_consistency >= 0.70:
        s -= 0.18
    if not calm_density and f.density_bin_err >= 0.12:
        s -= 0.15

    if s >= 0.40:
        reasons.append("似乎是偶发的失误")
    return clamp(s, 0.0, 1.0), reasons


# --------------------------------------------------------------------------- 画像


def _stamina_profile(
    notes: list[JudgedNote],
    density_of: dict[int, float],
    density_profile: list[dict],
) -> dict:
    """耐力：把谱面按**时间**三等分，看后 1/3 的失误率有没有明显爬升。

    为什么必须谨慎：谱面后段本来就常更难，高手局的失误又稀少，
    "前面一个 200 都没出、后面出一个"完全可能是正常波动。所以四道闸门：

      1. 样本量：全曲 >= 400 个音符，后段 >= 40 个音符且 >= 8 个失误；
      2. 绝对水位：后段失误率 >= 12%（否则从 1% 翻到 3% 没有意义）；
      3. 相对涨幅：后段 >= 前段 1.45 倍，且 >= 中段 1.25 倍；
      4. 难度校正：后段实际失误率 >= 该段按密度档**预期**失误率的 1.5 倍。
         闸门 4 是关键——它把"后面本来就更难"从"耐力掉了"里分出去。

    四条全中才算耐力不足；任一不满足就 drop=False。宁可漏判，也不要把
    "结尾难"或"偶然一个 200"说成体力问题。
    """
    empty = {
        "drop": False, "lateFrom": float("inf"),
        "early": 0.0, "mid": 0.0, "late": 0.0, "expected": 0.0, "reason": "",
    }
    if len(notes) < 400:
        return empty

    t0 = notes[0].note_time
    t1 = notes[-1].note_time
    span = t1 - t0
    if span <= 0:
        return empty
    cut_early = t0 + span / 3.0
    cut_late = t0 + 2.0 * span / 3.0

    seg = {"early": [0, 0], "mid": [0, 0], "late": [0, 0]}  # [音符数, 失误数]
    late_expected: list[float] = []
    for n in notes:
        if n.note_time >= cut_late:
            key = "late"
        elif n.note_time >= cut_early:
            key = "mid"
        else:
            key = "early"
        seg[key][0] += 1
        if n.is_flaw:
            seg[key][1] += 1
        if key == "late":
            d = density_of.get(n.obj_index)
            if d is not None:
                for b in density_profile:
                    if b["low"] <= d <= b["high"]:
                        late_expected.append(b["errorRate"])
                        break

    counts = {k: v[0] for k, v in seg.items()}
    rates = {k: (v[1] / v[0] if v[0] else 0.0) for k, v in seg.items()}
    expected = mean(late_expected) if late_expected else 0.0

    # 闸门 1：样本量
    if counts["late"] < 40 or seg["late"][1] < 8 or counts["early"] < 60:
        return {**empty, "lateFrom": cut_late, "early": round(rates["early"], 3),
                "mid": round(rates["mid"], 3), "late": round(rates["late"], 3),
                "expected": round(expected, 3)}

    late_rate = rates["late"]
    # 闸门 2：绝对水位
    if late_rate < 0.12:
        return {**empty, "lateFrom": cut_late, "early": round(rates["early"], 3),
                "mid": round(rates["mid"], 3), "late": round(late_rate, 3),
                "expected": round(expected, 3)}

    # 闸门 3：相对涨幅（前段和中段都要比过）
    if late_rate < rates["early"] * 1.45 or late_rate < rates["mid"] * 1.25:
        return {**empty, "lateFrom": cut_late, "early": round(rates["early"], 3),
                "mid": round(rates["mid"], 3), "late": round(late_rate, 3),
                "expected": round(expected, 3)}

    # 闸门 4：难度校正——后段失误率要明显高于"按这一档密度本来就该有的失误率"
    if expected <= 0 or late_rate < expected * 1.5:
        return {**empty, "lateFrom": cut_late, "early": round(rates["early"], 3),
                "mid": round(rates["mid"], 3), "late": round(late_rate, 3),
                "expected": round(expected, 3)}

    return {
        "drop": True,
        "lateFrom": cut_late,
        "early": round(rates["early"], 3),
        "mid": round(rates["mid"], 3),
        "late": round(late_rate, 3),
        "expected": round(expected, 3),
        "reason": "后 1/3 失误率 %.0f%%，前两段只有 %.0f%% / %.0f%%（同密度的预期是 %.0f%%），"
                  "是耐力撑不住了" % (late_rate * 100, rates["early"] * 100,
                                      rates["mid"] * 100, expected * 100),
    }


def build_profile(match: MatchResult, contexts: list[patterns.NoteContext]) -> dict:
    notes = match.notes
    deltas = match.deltas()
    bits_severe = [1 if n.is_error else 0 for n in notes]
    runs = runs_test(bits_severe)

    samples: list[tuple[float, bool]] = []
    by_obj = {c.index: c for c in contexts}
    for n in notes:
        c = by_obj.get(n.obj_index)
        if c is not None:
            samples.append((c.density_1s, n.is_flaw))
    prof = density_error_profile(samples, n_bins=6)
    trend = trend_significance(prof)
    density_bins = [
        {
            "low": round(b.low, 2),
            "high": round(b.high, 2),
            "count": b.count,
            "errors": b.errors,
            "errorRate": round(b.error_rate, 3),
        }
        for b in prof
    ]
    # 耐力：后 1/3 的失误率有没有在"同密度"的前提下明显爬升（判据见 _stamina_profile）
    stamina = _stamina_profile(
        notes, {c.index: c.density_1s for c in contexts}, density_bins
    )

    # 稳定上限：失误率第一次明显超过基线的那一档的下边界
    ceiling = 0.0
    usable = [b for b in prof if b.count >= 10]
    if len(usable) >= 3:
        base = usable[0].error_rate
        for i, b in enumerate(usable):
            if i > 0 and b.error_rate > max(0.12, base * 2.5):
                ceiling = usable[i - 1].high
                break
        else:
            ceiling = usable[-1].high

    return {
        "ceiling": ceiling,
        "global_bias": median(deltas) if deltas else 0.0,
        "global_ur": stdev(deltas) * 10 if deltas else 0.0,
        "flaw_rate": (sum(1 for n in notes if n.is_flaw) / len(notes)) if notes else 0.0,
        "severe_rate": (sum(bits_severe) / len(notes)) if notes else 0.0,
        "runs": runs.as_dict(),
        "density_profile": density_bins,
        "stamina": stamina,
        "trend": trend,
    }


def _decide(f: ErrorFeature, profile: dict) -> Attribution:
    """给一条失误定类别：三类各自打分 → 互相制衡 → 归一成概率与置信度。"""
    a_s, a_r = _score_ability(f, profile)
    b_s, b_r = _score_rhythm(f, profile)
    c_s, c_r = _score_distraction(f, profile)
    d_s, d_r = _score_slip(f, profile)

    # 「能力崩盘」的两个指纹（同列链后半程崩 / 连续偏晚后漏）一旦成立：
    #   * 分神的分数减半 —— 成串崩掉时"这一下是断片"的解释力下降；
    #   * 能力分抬到不低于 0.45 的地板（用户要求这两种表现**优先判能力**）。
    #
    # 但**不能无条件压节奏**：整局稳定偏早/偏晚的人，纵连末端爆 200 也是同一个
    # 偏移造成的。节奏分已经很高（>=0.6）时说明整局方向一致，那才是主因，
    # 这时只抬能力、不压节奏——否则 0.85 的节奏证据会被腰斩成 0.42。
    if f.jack_collapse or f.late_collapse:
        b_s *= 0.5
        c_s *= 0.5
        d_s *= 0.3
        a_s = max(a_s, 0.45)

    # 顺序很关键（研究结论）：如果"难"已经能解释这一处，那么"断片"证据的可信度要打折，
    # 否则异方差会被误当成"手突然抖了一下"。
    ability_gate = clamp((a_s - 0.35) / 0.40, 0.0, 1.0)
    c_s = c_s * (1.0 - 0.45 * ability_gate)

    scores = {"ability": a_s, "rhythm": b_s, "distraction": c_s, "slip": d_s}
    floor = 0.04
    weights = {k: v + floor for k, v in scores.items()}
    total = sum(weights.values())
    probs = {k: v / total for k, v in weights.items()}
    ordered = sorted(probs.items(), key=lambda kv: -kv[1])
    best_k, best_p = ordered[0]
    second_p = ordered[1][1]
    # 置信度：既要领先足够多，也要自身有绝对证据量。
    # 四类之后每类的先验上限降到 0.8 左右，系数相应上调一点，避免整体"看起来都没把握"。
    margin = best_p - second_p
    confidence = clamp(margin * 1.8 + best_p * 0.62, 0.05, 0.97)

    reasons_all = {"ability": a_r, "rhythm": b_r, "distraction": c_r, "slip": d_r}
    # 次要成因只报"另一类成因"，不把"随机失手"当次要成因（它不是一种成因）
    runners = [(k, v) for k, v in ordered[1:] if k != "slip"] or [ordered[1]]
    secondary = runners[0][0] if runners[0][1] >= 0.26 and best_p < 0.62 and best_k != "slip" else None
    return Attribution(
        note=f.note,
        category=best_k,
        scores=probs,
        confidence=confidence,
        reasons=reasons_all[best_k][:3],
        secondary=secondary,
    )


def classify_all(match: MatchResult, contexts: list[patterns.NoteContext]) -> tuple[list[Attribution], dict]:
    profile = build_profile(match, contexts)
    feats = build_features(match, contexts, profile.get("density_profile", []))
    out = [_decide(f, profile) for f in feats]
    return out, summarize(out, profile)


def summarize(atts: list[Attribution], profile: dict) -> dict:
    counts = {"ability": 0, "rhythm": 0, "distraction": 0, "slip": 0}
    conf_sum = {"ability": 0.0, "rhythm": 0.0, "distraction": 0.0, "slip": 0.0}
    for a in atts:
        counts[a.category] = counts.get(a.category, 0) + 1
        conf_sum[a.category] += a.confidence
    total = max(1, len(atts))
    return {
        "total": len(atts),
        "counts": counts,
        "share": {k: round(v / total, 3) for k, v in counts.items()},
        "avgConfidence": {k: round(conf_sum[k] / counts[k], 2) if counts[k] else 0.0 for k in counts},
        "profile": profile,
    }
