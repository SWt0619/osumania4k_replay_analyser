"""为谱面的每个物件算出「局部上下文」：密度、和弦、连打、交错、节拍位置等。

这些特征是「失误归因」的输入：同样是打偏 40ms，
  发生在 2 NPS 的散音上  → 大概率是分神
  发生在 12 NPS 的 1/4 连打里 → 更可能是能力上限

键型命名沿用 osu!mania 的通用说法（中英对照与判据见 PATTERN_LABEL 与 classify()）：
stream / jumpstream / handstream / chordjack / trill / jumptrill / jack / minijack / anchor。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from .beatmap import Beatmap
from .metrics import quantile_edges

# 键型：机器可读 key → 中文名。
# 顺序即分类优先级（叠 > 交互 > 切换类 > 单次多押 > 锚 > 散音），见 classify()。
PATTERN_LABEL = {
    "chordjack": "多押叠",      # chordjack
    "longjack": "纵连",         # longjack / jack
    "minijack": "小叠",         # minijack
    "jumptrill": "对拍",        # jumptrill
    "trill": "交互",            # trill
    "quadstream": "四押切",     # quadstream
    "handstream": "三押切",     # handstream
    "jumpstream": "双押切",     # jumpstream
    "stream": "单押切",         # (single) stream
    "quad": "四押",             # quad
    "hand": "三押",             # hand / triple
    "jump": "双押",             # jump / double
    "anchor": "锚",             # anchor
    "single": "散音",           # 不算任何特殊键型的单点
}

# 判断"这一段里掺了多大的押"的阈值（占窗口内物件的比例）。
# 取这些值是因为：纯切里 share 都是 0；双押切里双押物件约占 1/2~2/3；
# 三押切里三押物件约占 1/2~3/4。留足余量，宁可少判也不要把纯切误判成双押切。
_QUAD_SHARE = 0.06
_HAND_SHARE = 0.30
_JUMP_SHARE = 0.30


@dataclass
class NoteContext:
    index: int
    time: float
    column: int
    density_1s: float        # ±500ms 内的物件数，换算成 NPS
    density_local: int       # ±150ms 内的物件数（瞬时压力）
    chord_size: int          # 同一时刻（±32ms）的物件数
    column_load: int         # 同列 ±1000ms 内的物件数
    beat_length: float       # 当时每拍毫秒
    subdivision: float       # 与上一个时间点的间隔 / 拍长（0.25 = 1/16 音符）
    is_stream: bool          # 稳定的等间隔连打
    is_trill: bool           # 两列交替
    is_anchor: bool          # 同列在短窗口内反复出现
    is_jack: bool            # 同列在一个「1/16 音符」内被再次按下（相对拍长，不是绝对毫秒）
    jack_run_len: int = 1    # 本物件所在同列连打链的长度（链 = 同列、每个相邻间隔都够快）
    jack_run_pos: int = 0    # 本物件在链里的位置（0 = 第一个）
    # 周围 ±500ms 里"双押 / 三押 / 四押"物件各占多少（0..1）。
    # 用来把切换类分成 单押切 / 双押切 / 三押切 / 四押切 —— 只看自己那一瞬间的
    # chord_size 是不够的：双押切里的单键那一刻也是单键。
    chord_mix: tuple[float, float, float] = (0.0, 0.0, 0.0)


def _beat_length_at(beatmap: Beatmap, t: float) -> float:
    pts = [tp for tp in beatmap.timing_points if tp.uninherited and tp.beat_length > 0]
    if not pts:
        return 500.0
    cur = pts[0]
    for tp in pts:
        if tp.time <= t:
            cur = tp
        else:
            break
    return cur.beat_length if cur.beat_length > 0 else 500.0


def _count_in(times: list[float], t: float, half_width: float) -> int:
    """±half_width 内的物件数（times 已按时间排好，二分即可）。"""
    return bisect.bisect_right(times, t + half_width) - bisect.bisect_left(times, t - half_width)


def _is_stream_at(times: list[float], i: int, n: int, beat_length: float) -> bool:
    """稳定的等间隔连打：前后各取两个间隔，均值落在合理区间且离散够小。

    要求"间隔稳定"是为了不把随机散音误判成连打——散音的间隔忽长忽短，
    而在归因里"连打"意味着"这里要求手速"，误判会直接影响能力类判据。
    """
    if not (1 <= i < n - 2):
        return False
    seg = [times[k + 1] - times[k] for k in range(max(0, i - 2), min(n - 1, i + 2))]
    if len(seg) < 3:
        return False
    avg = sum(seg) / len(seg)
    spread = max(seg) - min(seg)
    return 20 <= avg <= beat_length / 3.0 and spread <= max(6.0, avg * 0.28)


def _is_trill_at(times: list[float], cols: list[int], i: int, beat_length: float) -> bool:
    """两列交替：前四个音只由两列组成、当前音回到两拍前那一列、且快于半拍。"""
    if i < 4:
        return False
    prev_cols = cols[i - 4 : i]
    if not (len(set(prev_cols)) == 2 and cols[i] == prev_cols[-2] and cols[i] != prev_cols[-1]):
        return False
    gaps = [times[k + 1] - times[k] for k in range(i - 4, i)]
    return max(gaps) <= max(90.0, beat_length / 2.0) and min(gaps) >= 15.0


def build_context(beatmap: Beatmap) -> list[NoteContext]:
    objs = beatmap.objects
    n = len(objs)
    if not n:
        return []

    times = [o.time for o in objs]
    cols = [o.column for o in objs]
    keys = max(1, beatmap.keys)
    # 去重后的时间戳：双押/三押在同一时刻有多个物件，用"物件序列"算间隔会得到 0，
    # 于是带押的连打永远判不成 stream。切换类必须按"时间点序列"看节奏。
    uniq_times = sorted(set(times))
    ut_index = {t: k for k, t in enumerate(uniq_times)}

    # 每列的物件时间表，用于同列间隔
    col_times: list[list[float]] = [[] for _ in range(keys)]
    for o in objs:
        if 0 <= o.column < keys:
            col_times[o.column].append(o.time)

    out: list[NoteContext] = []
    for i, o in enumerate(objs):
        t = o.time
        density_1s = float(_count_in(times, t, 500.0))
        density_local = _count_in(times, t, 150.0)
        chord_size = _count_in(times, t, 32.0)

        # 用「上一个时间点」而不是「上一个物件」：同一时刻的双押/三押里，
        # 第二个物件的间隔是 0，会被当成"无限快的细分"，进而误判成节奏问题。
        uti = ut_index[t]
        interval_prev = (t - uniq_times[uti - 1]) if uti > 0 else 9999.0

        same_prev = 9999.0
        column_load = 0
        arr = col_times[o.column] if 0 <= o.column < keys else []
        if arr:
            k = bisect.bisect_left(arr, t)
            if k > 0:
                same_prev = t - arr[k - 1]
            column_load = bisect.bisect_right(arr, t + 1000.0) - bisect.bisect_left(arr, t - 1000.0)

        bl = _beat_length_at(beatmap, t)
        subdivision = interval_prev / bl if bl > 0 else 9999.0

        # 同列锚点：同列在 ±200ms 内出现 ≥3 次
        is_anchor = bool(arr) and _count_in(arr, t, 200.0) >= 3

        out.append(
            NoteContext(
                index=i,
                time=t,
                column=o.column,
                density_1s=density_1s,
                density_local=density_local,
                chord_size=chord_size,
                column_load=column_load,
                beat_length=bl,
                subdivision=min(subdivision, 99.0),
                is_stream=_is_stream_at(uniq_times, ut_index[t], len(uniq_times), bl),
                is_trill=_is_trill_at(times, cols, i, bl),
                is_anchor=is_anchor,
                is_jack=is_jack_gap(same_prev, bl),
            )
        )

    _mark_chord_mix(out, times)
    _mark_jack_runs(out, times, keys)
    return out


def _prefix(flags: list[int]) -> list[int]:
    acc = [0] * (len(flags) + 1)
    for i, v in enumerate(flags):
        acc[i + 1] = acc[i] + v
    return acc


def _mark_chord_mix(contexts: list[NoteContext], times: list[float]) -> None:
    """给每个物件标注周围 ±500ms 的"双押 / 三押 / 四押"物件占比。

    只按自己那一瞬间的 chord_size 分类是不够的：双押切里的单键，那一刻也是单键，
    得看它周围有没有稳定的多押构成，才能区分"纯切"和"双押切/三押切"。
    """
    two = [1 if c.chord_size == 2 else 0 for c in contexts]
    three = [1 if c.chord_size == 3 else 0 for c in contexts]
    four = [1 if c.chord_size >= 4 else 0 for c in contexts]
    p2, p3, p4 = _prefix(two), _prefix(three), _prefix(four)
    for c in contexts:
        lo = bisect.bisect_left(times, c.time - 500.0)
        hi = bisect.bisect_right(times, c.time + 500.0)
        w = max(1, hi - lo)
        c.chord_mix = ((p2[hi] - p2[lo]) / w, (p3[hi] - p3[lo]) / w, (p4[hi] - p4[lo]) / w)


# 「叠」的相对窗口：同列重复间隔 <= 拍长 × JACK_SNAP。
# 1/8 音符 = 0.5 拍、1/16 音符 = 0.25 拍，取 0.30 正好把两者分开：
#   切（stream / handstream）的连续物件是**八分音**，同列再按要等到 0.5 拍 → 不算叠；
#   叠（jack / chordjack）是**十六分音**，同列 0.25 拍就再来一次 → 算叠。
# 以前写死 180ms，在 180BPM 上 1/8 正好是 166ms，于是一堆三押切被误判成三押叠。
JACK_SNAP = 0.30


def is_jack_gap(gap_ms: float, beat_length: float) -> bool:
    '''同列上一次按键是不是「叠」那一档（相对拍长判断，避免被 BPM 带偏）。'''
    if gap_ms >= 9999.0:
        return False
    if beat_length <= 0:
        return gap_ms <= 180.0
    return gap_ms <= beat_length * JACK_SNAP


def _mark_jack_runs(contexts: list[NoteContext], times: list[float], keys: int) -> None:
    """给每个物件标注它所在的同列连打链：链长 jack_run_len、链内位置 jack_run_pos。

    链的定义：同一列里每个相邻间隔都够快（`is_jack_gap`，按各自的拍长判断）的连续段。
    归因用它来识别「链打到最后才崩」——前面几个都打干净了、
    后半程开始爆 200/漏，这是手指耐力跟不上这条链的直接证据。
    """
    n = len(contexts)
    if not n:
        return
    # 按列收集物件序号（objects 本来就按时间排，contexts 与之一一对应）
    col_order: list[list[int]] = [[] for _ in range(max(1, keys))]
    for i, c in enumerate(contexts):
        if 0 <= c.column < len(col_order):
            col_order[c.column].append(i)

    for idxs in col_order:
        m = len(idxs)
        if not m:
            continue
        # 先给链编号、标出链内位置
        chain_of: list[int] = []
        pos_in_chain: list[int] = []
        chain_members: dict[int, list[int]] = {}
        cid = -1
        for k, i in enumerate(idxs):
            if k == 0 or not is_jack_gap(times[idxs[k]] - times[idxs[k - 1]],
                                          contexts[i].beat_length):
                cid += 1
                chain_members[cid] = []
            chain_of.append(cid)
            pos = k - chain_members[cid][0] if chain_members[cid] else 0
            pos_in_chain.append(pos)
            chain_members[cid].append(k)
        for k, i in enumerate(idxs):
            members = chain_members[chain_of[k]]
            contexts[i].jack_run_len = len(members)
            contexts[i].jack_run_pos = pos_in_chain[k]


def classify(ctx: NoteContext | None) -> str:
    """判定这个物件属于哪种键型，返回机器可读的 key（中文名见 PATTERN_LABEL）。

    优先级从"最具体的动作"到"最泛的兜底"，和玩家描述谱面的习惯一致：
      叠键（同列再按）→ 交互（两列来回）→ 切换类（等间隔连打，按掺了几押再分）
      → 单次多押 → 锚 → 散音

    为什么叠键排最前：一个"双押叠"既是多押又是叠键，但玩家会说"这里卡手"，
    指的就是那根手指被连续按；把它算成"双押"会丢掉最关键的成因。
    """
    if ctx is None:
        return "single"
    if ctx.is_jack:
        if ctx.chord_size >= 2:
            return "chordjack"
        return "longjack" if ctx.jack_run_len >= 3 else "minijack"
    if ctx.is_trill:
        return "jumptrill" if ctx.chord_size >= 2 else "trill"
    if ctx.is_stream:
        j2, j3, j4 = ctx.chord_mix
        if j4 >= _QUAD_SHARE:
            return "quadstream"
        if j3 >= _HAND_SHARE:
            return "handstream"
        if j2 >= _JUMP_SHARE:
            return "jumpstream"
        return "stream"
    if ctx.chord_size >= 4:
        return "quad"
    if ctx.chord_size == 3:
        return "hand"
    if ctx.chord_size == 2:
        return "jump"
    return "anchor" if ctx.is_anchor else "single"


def pattern_label(ctx: NoteContext) -> str:
    """给人看的键型名（中文，见 PATTERN_LABEL）。"""
    return PATTERN_LABEL.get(classify(ctx), "散音")


def difficulty_weight(ctx: NoteContext) -> float:
    """一个粗粒度"这处有多难"的分数（无量纲，用于同谱面内比较）。

    构成：主场密度 + 局部密度 + 和弦 + 同列压力 + 交错/连打加成，再按间隔归一。
    """
    base = ctx.density_1s / 8.0
    local = ctx.density_local / 6.0
    chord = (ctx.chord_size - 1) * 0.35
    colpressure = ctx.column_load / 10.0
    pattern_bonus = 0.0
    if ctx.is_trill:
        pattern_bonus += 0.35
    if ctx.is_stream:
        pattern_bonus += 0.25
    if ctx.is_jack:
        pattern_bonus += 0.45
    if ctx.is_anchor:
        pattern_bonus += 0.2
    return base * 0.9 + local * 0.5 + chord + colpressure * 0.4 + pattern_bonus


def signature(ctx: NoteContext, density_edges: list[float]) -> tuple:
    """把上下文压缩成一个可比较的签名，用于"同类片段里他是不是也失误"。"""
    dens_bin = bisect.bisect_right(density_edges, ctx.density_1s)
    return (
        dens_bin,
        "chord" if ctx.chord_size >= 3 else ("jack" if ctx.is_jack else ("trill" if ctx.is_trill else "single")),
        "fast" if ctx.subdivision <= 0.26 else ("mid" if ctx.subdivision <= 0.52 else "slow"),
    )


def signature_edges(contexts: list[NoteContext], n_bins: int = 5) -> list[float]:
    return quantile_edges([c.density_1s for c in contexts], n_bins)
