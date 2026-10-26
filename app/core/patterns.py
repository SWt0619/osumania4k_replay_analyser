"""为谱面的每个物件算出「局部上下文」：密度、和弦、连打、交错、节拍位置等。

这些特征是「失误归因」的输入：同样是打偏 40ms，
  发生在 2 NPS 的散音上  → 大概率是分神
  发生在 12 NPS 的 1/4 连打里 → 更可能是能力上限

键型命名沿用 osu!mania 的通用说法（中英对照与判据见 PATTERN_LABEL 与 classify()）：
stream / jumpstream / handstream / chordjack / trill / jumptrill / jack / minijack / anchor。
"""

from __future__ import annotations

import bisect
import collections
import math
from dataclasses import dataclass

from .beatmap import Beatmap
from .metrics import quantile_edges
from . import ln as ln_patterns

# 键型：机器可读 key → 中文名。
# 顺序即分类优先级（叠 > 交互 > 切换类 > 单次多押 > 锚 > 散音），见 classify()。
PATTERN_LABEL = {
    "chordjack": "Chordjack",
    "longjack": "Longjack",
    "minijack": "Minijack",
    "jumptrill": "Jumptrill",
    "trill": "Trill",
    "quadstream": "Quadstream",     # 仅 5 键以上；4K 里四押就是四键全按
    "handstream": "Handstream",
    "jumpstream": "Jumpstream",
    "stream": "Stream",
    "spamstream": "Speedstream",    # Speed 段里的等间隔连打（不是 Stream）
    "quad": "Quad",
    "hand": "Hand",
    "jump": "Jump",
    "anchor": "Anchor",
    "single": "Single",
    "longnote": "LN",               # 长条为主的段落，不套用 RC 的四大类
    # 长条段的写法细分，口径见 app/core/ln.py
    "lnrelease": "Release",
    "lndensity": "Density",
    "lncoordination": "Coordination",
    "lnmixed": "Mixed LN",
}

# 判断"这一段里掺了多大的押"的阈值（占窗口内物件的比例）。
# 取这些值是因为：纯切里 share 都是 0；双押切里双押物件约占 1/2~2/3；
# 三押切里三押物件约占 1/2~3/4。留足余量，宁可少判也不要把纯切误判成双押切。
_QUAD_SHARE = 0.06
_HAND_SHARE = 0.30
_JUMP_SHARE = 0.30
# 块里四押物件的占比超过它就按"叠"处理（见 _block_category 里的实测分布）。
QUAD_STACK_SHARE = 0.15
# 「乱」与「切」的分界：切是按 1/16 音组织的写法；乱是等效 1/24、1/32 音的高速单键。
# subdivision 是"与上一行的间隔 / 每拍毫秒"，所以 1/16 音 = 0.25、1/24 音 = 0.167、1/32 音 = 0.125。
# 取 0.20 当分界（比 1/16 明显更密才算乱），并要求这类物件在块里过半。
FAST_SUBDIVISION = 0.20
FAST_SHARE_MIN = 0.50

# 「技术」= 复杂节奏、节奏在快速变化（例如 12 分、16 分、24 分掺着来）。
# 判断这件事不能只数"混了几种细分"（那是静态构成），要看间隔序列**有没有在变**：
# 用短周期重复率——把行间隔序列和它往前挪 2/3/4/6 行的自己做比较，落在同一档的比例越高，
# 说明节奏越"有型"（一整段同一种细分，或者固定图样在循环），越低说明节奏一直在换。
# 实测（整段）：均匀 1/32 墙 1.00、纯 16 分 1.00、Speed 段 0.88 / 0.67、
# Technical 段 0.61 / 0.55；全库块级分位 p50 0.73、p25 0.58。
# 门槛取两类样本的正中 0.64：乱的样本（0.67）仍留在乱，技术的样本（0.61、0.55）进技术。
RHYTHM_PERIODS = (2, 3, 4, 6)
RHYTHM_REPEAT_TECH = 0.64
# 判定"同一档"的容差：绝对 6ms（短间隔时的量化误差）或相对 10%，取大的那个。
RHYTHM_TOL_MS = 6.0
RHYTHM_TOL_RATIO = 0.10


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
    # 邻域内「相邻时间点的中位间隔」——也就是这张图当前一段的行密度基准。
    # 叠的判据要用它当尺子：同样 333ms 的间隔，在 16 分连打里是正常轮转，
    # 在 8 分 chordjack 里就是同一根手指连按。
    row_gap: float = 9999.0
    # 本列在 ±500ms 里占的份额是否够高（≥0.40）——锚点的真正含义是"别的列在动、它被反复用"。
    column_dominant: bool = False
    # 所属大块的四类粗分类（stack/spam/cut/tech），由 assign_coarse 填。
    # classify 只在所属大类内部挑细分键型，保证大方向不出错。
    coarse: str = ""
    # 本行和弦是否与前面一到两行完全相同（同一组和弦在反复）。
    # 叠块里的多押叠靠它认：和弦在反复时，那几击是同一组手指连按，不该记成"单次双押"。
    repeat_chord: bool = False
    # 周围 ±500ms 里"双押 / 三押 / 四押"物件各占多少（0..1）。
    # 用来把切换类分成 单押切 / 双押切 / 三押切 / 四押切 —— 只看自己那一瞬间的
    # chord_size 是不够的：双押切里的单键那一刻也是单键。
    chord_mix: tuple[float, float, float] = (0.0, 0.0, 0.0)
    # 这张谱有几个键。4K 与 7K 对"四押"的含义完全不同：4K 里四押就是四键全按，
    # 不存在 quadstream 那种"四押连打"的说法，所以细分键型要看这个。
    keys: int = 4
    # 本行有几个列在上一行也出现过（占本行比例）。叠段的本质就是"同一组手指被反复按下"，
    # 这个比例比"同列间隔是否 ≤1/16 拍"更贴近实际——1/4 间隔的 chordjack 也能认出来。
    row_overlap: float = 0.0
    # 这一击是不是长条的音头。calm 段没有稳定键型，只能按押数描述，
    # 但里面的长条要单独标 LN，否则会混进 single/jump。
    is_hold: bool = False
    # 长条的结束时刻（毫秒）；米为 0。长条的分支写法（release / density /
    # coordination）全靠"尾"来判，只有音头信息一项都算不出来。
    hold_end: float = 0.0
    # 细分键型（段落级结论，由 assign_subpattern 写回）。
    # 空表示还没分块，classify 退回按这一击的动作本身判断。
    sub: str = ""


def _beat_marks(beatmap: Beatmap) -> tuple[list[float], list[float]]:
    """红线的（时间, 每拍毫秒）两张表，供逐音符查拍长。

    只取 uninherited 且 beat_length > 0 的点（继承点是 SV，拍长是负数）。
    """
    times: list[float] = []
    lengths: list[float] = []
    for tp in beatmap.timing_points:
        if tp.uninherited and tp.beat_length > 0:
            times.append(tp.time)
            lengths.append(tp.beat_length)
    return times, lengths


def _beat_length_from(marks: tuple[list[float], list[float]], t: float) -> float:
    """t 时刻的每拍毫秒；第一条红线之前按第一条算，读不到给 500。"""
    times, lengths = marks
    if not times:
        return 500.0
    idx = bisect.bisect_right(times, t) - 1
    bl = lengths[max(0, idx)]
    return bl if bl > 0 else 500.0


def _count_in(times: list[float], t: float, half_width: float) -> int:
    """±half_width 内的物件数（times 已按时间排好，二分即可）。"""
    return bisect.bisect_right(times, t + half_width) - bisect.bisect_left(times, t - half_width)


def _median(values: list[float]) -> float:
    """中位数（不引 statistics：这里只用到这一个统计量）。"""
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


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


@dataclass
class _Grid:
    """逐音符测量要用的全谱索引（只跟谱面有关，与回放无关）。

    放在一个对象里，是为了让「量一个音符」的函数只收一个参数；
    这些表以前在循环里各自重算，慢且容易算出两套口径。
    """

    times: list[float]
    cols: list[int]
    uniq_times: list[float]
    ut_index: dict[float, int]
    col_times: list[list[float]]
    row_gaps: dict[float, float]
    row_cols: dict[float, set]
    marks: tuple[list[float], list[float]]

    def beat_length(self, t: float) -> float:
        return _beat_length_from(self.marks, t)


def _build_grid(beatmap: Beatmap) -> _Grid:
    objs = beatmap.objects
    times = [o.time for o in objs]
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

    # 每个时间点的「局部行间隔」：±500ms 内相邻时间点间隔的中位数。
    # 双押/三押在同一时刻，所以按去重后的时间点算，不能按物件序列算（会得到 0）。
    row_gaps: dict[float, float] = {}
    for t in uniq_times:
        lo = bisect.bisect_left(uniq_times, t - 500.0)
        hi = bisect.bisect_right(uniq_times, t + 500.0)
        gaps = [uniq_times[j + 1] - uniq_times[j] for j in range(lo, max(lo, hi - 1))]
        row_gaps[t] = _median(gaps) if gaps else 9999.0

    row_cols: dict[float, set] = {}
    for o in objs:
        row_cols.setdefault(o.time, set()).add(o.column)

    return _Grid(times=times, cols=[o.column for o in objs], uniq_times=uniq_times,
                 ut_index=ut_index, col_times=col_times, row_gaps=row_gaps,
                 row_cols=row_cols, marks=_beat_marks(beatmap))


def _context_of(i: int, obj, grid: _Grid, keys: int = 4) -> NoteContext:
    """量第 i 个物件：密度、拍长、细分、连打/交互/锚点/叠。"""
    t = obj.time
    times = grid.times
    density_1s = float(_count_in(times, t, 500.0))
    density_local = _count_in(times, t, 150.0)
    chord_size = _count_in(times, t, 32.0)

    # 用「上一个时间点」而不是「上一个物件」：同一时刻的双押/三押里，
    # 第二个物件的间隔是 0，会被当成"无限快的细分"，进而误判成节奏问题。
    uti = grid.ut_index[t]
    interval_prev = (t - grid.uniq_times[uti - 1]) if uti > 0 else 9999.0

    same_prev = 9999.0
    column_load = 0
    arr = grid.col_times[obj.column] if 0 <= obj.column < len(grid.col_times) else []
    if arr:
        k = bisect.bisect_left(arr, t)
        if k > 0:
            same_prev = t - arr[k - 1]
        column_load = bisect.bisect_right(arr, t + 1000.0) - bisect.bisect_left(arr, t - 1000.0)

    bl = grid.beat_length(t)
    subdivision = interval_prev / bl if bl > 0 else 9999.0

    rg = grid.row_gaps[t]
    # 本行与上一行的列重合度：叠段（chordjack）的核心特征
    overlap = 0.0
    if uti > 0 and chord_size >= 2:
        cur = grid.row_cols.get(t) or set()
        prev = grid.row_cols.get(grid.uniq_times[uti - 1]) or set()
        if cur:
            overlap = len(cur & prev) / len(cur)
    # 锚点：本列在邻域里占了足够大的份额（≥40%）、且确实被反复用（≥3 次）。
    # 以前只看"同列 ±200ms 出现 3 次"，密集段里随手一个单键就满足，于是锚成了垃圾桶。
    dominant = (
        bool(arr)
        and column_load >= 3
        and density_1s > 0
        and _count_in(arr, t, 500.0) / density_1s >= 0.40
    )

    return NoteContext(
        index=i,
        time=t,
        column=obj.column,
        density_1s=density_1s,
        density_local=density_local,
        chord_size=chord_size,
        column_load=column_load,
        beat_length=bl,
        subdivision=min(subdivision, 99.0),
        is_stream=_is_stream_at(grid.uniq_times, uti, len(grid.uniq_times), bl),
        is_trill=_is_trill_at(times, grid.cols, i, bl),
        is_anchor=dominant,
        is_jack=is_jack_gap(same_prev, rg, bl, chord_size),
        row_gap=rg,
        column_dominant=dominant,
        keys=keys,
        row_overlap=overlap,
        is_hold=bool(getattr(obj, "is_hold", False)),
        hold_end=float(getattr(obj, "end_time", None) or 0.0),
    )


def build_context(beatmap: Beatmap) -> list[NoteContext]:
    """给每个物件算一份局部上下文；算完再统一补「和弦占比 / 连打链 / 和弦重复」。"""
    objs = beatmap.objects
    if not objs:
        return []

    grid = _build_grid(beatmap)
    out = [_context_of(i, o, grid, beatmap.keys) for i, o in enumerate(objs)]

    _mark_chord_mix(out, grid.times)
    _mark_jack_runs(out, grid.times, max(1, beatmap.keys))
    _mark_repeat_chords(out)
    return out


def _mark_repeat_chords(contexts: list[NoteContext]) -> None:
    """标记「这一行的和弦与前面一到两行完全相同」的物件。

    叠（多押叠）的本质是同一组手指被反复按下，和弦在反复；
    而切是每行换一组。只看单个音符的同列间隔分不出这两者，所以单独标出来。
    """
    rows: dict[float, list[NoteContext]] = {}
    for c in contexts:
        rows.setdefault(c.time, []).append(c)
    order = sorted(rows)
    sets = [frozenset(c.column for c in rows[t]) for t in order]
    for i in range(len(order)):
        if len(sets[i]) < 2:
            continue
        same = (i >= 1 and sets[i] == sets[i - 1]) or (i >= 2 and sets[i] == sets[i - 2])
        if same:
            for c in rows[order[i]]:
                c.repeat_chord = True


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


# 「叠」的判据：**同一列在两个相邻的 16 分位置上各按一次**。
# 切（stream / jumpstream / handstream）的 16 分位置是在不同列之间轮转的，
# 同一列连着吃两个 16 分位置的情况几乎不出现——实测三张大切图分别是 0.0% / 0.2% / 0.0%，
# 而带叠的图是 17~20%。所以判据要同时满足两条：
#   1) 同列间隔不超过「局部行间隔」的 1.2 倍（就是紧邻的下一行）；
#   2) 这一段的行间隔本身落在 16 分那一层（≤ 0.34 拍）。
# 注意不能只看 1)：大和弦里相邻两行本来就共用列（三押共用两列），
# 单看同列覆盖率会把整张切图判成叠（实测 0.64~0.66）。
JACK_ROW_RATIO = 1.2
JACK_LAYER_MAX = 0.34
JACK_MIN_MS = 45.0
# 1/8 层的叠：同列每两行回一次（1.6~2.6 倍行间隔），且不在大和弦里
JACK_EIGHTH_LAYER = (0.42, 0.58)
JACK_EIGHTH_RATIO = (1.6, 2.6)

# 同一组和弦连续重复的判定门槛（1/8 层的夹叠图靠这一条）。
# 切图的块级分布中位 0.08~0.10、P90 0.44~0.46；夹叠图 Purple 中位 0.46、P25 0.35。
SAME_GROUP_MIN = 0.40

# 节奏型混杂的判定：块内细分档位分布的归一化熵。
# 切图实测 0.34~0.67（Collapse 0.40、Re:Dive 0.50、Betwixt 0.67），
# 典型 tech 图 0.87。门槛取 0.82：落在切图上界与 tech 样本之间，两边都留出余量，
# 只让真正"几种节奏型混着来"的段落进 tech。
# 这一条要排在叠之前：节奏本身就在变的块，先归 tech 比归叠更接近玩家的说法。
TECH_ENTROPY = 0.82

# 分块：每块约 64 个物件，相邻块重叠 16 个（25%），避免键型在块边界被腰斩。
BLOCK_NOTES = 64
BLOCK_OVERLAP = 16

# 四类粗分类的中文名。键型细分在各自大类内部进行（见 classify）。
# 另加一个"长条"大类：面（LN）图的难点在长条本身，套用 RC 的四大类只会给出误导性的结论。
# 键型一律用英文术语：中文的「切 / 叠 / 乱」在不同人群里指的东西不一样
# （比如"乱"有人当成 vibro、有人当成 LN），英文术语没有歧义。
#   cut   → Stream   （以 1/16 音组织的连打，含 jumpstream / handstream）
#   stack → Jack     （同一组手指被反复按下，含 minijack / chordjack）
#   spam  → Speed    （等效 1/24、1/32 音的高速单键）
CATEGORY_LABEL = {
    "ln": "LN",
    "calm": "Calm",
    "stack": "Jack",
    "spam": "Speed",
    "cut": "Stamina",   # 长连打一类，按用户口径叫 Stamina（键名保持 cut，不动索引格式）
    "tech": "Tech",
    "hybrid": "Hybrid",
}

# 「Hybrid」不是块级判出来的，是整张图的结论：RC 与 LN 谁都不占压倒性优势。
# 为什么要有：以前只有 LN 一类，一张 40% 长条 + 60% 双押的图会被归到 LN 或 Jack，
# 两种说法都只讲了一半。实测 7 张公认的 hybrid 图，LN 占比是 32%~68%，
# 而全库 LN 占比的分位是 p85 = 0.30、p90 = 0.48、p95 = 0.80——
# 取 [0.30, 0.70) 这一段正好是"两边都有得打"的区间，约占全库 8.6%。
HYBRID_LN_MIN = 0.30
HYBRID_LN_FULL = 0.70
# 第二条通道：长条**音符**占比高，但块级没凑够 45%（块里全是长条才判 LN 块）。
# 一局里 38% 的音是长条、手感上一半时间在按长条，按块算却只有 23% —— 这类图
# 归到 Jack 只讲了一半。门槛与块级那条一致取 30%：本机 46 局实测里，
# 长条占比不是 0~4%（纯 RC）就是 30% 以上（真的两边都有），中间是空的，
# 卡在 35% 只会把恰好 30% 的那几张漏成 RC。
HYBRID_HOLD_MIN = 0.30
HYBRID_HOLD_LN_MIN = 0.15
RC_CLASSES = ("cut", "stack", "spam", "tech")


def dominant_class(share: dict, hold_ratio: float = 0.0) -> str:
    """整张图归到哪一大类。share 的取值是 0~1（不是百分比）。

    LN 占压倒性（≥0.70）就是 LN 图；LN 与 RC 都成规模（0.30~0.70）就是 Hybrid；
    长条音符本身过半（hold_ratio ≥0.35 且 LN 块占比 ≥0.15）也算 Hybrid ——
    长条在块里"稀疏"，只按块算会低估它。
    再往下才按 RC 里最大的那一类比。所以 hybrid 是一个**整图结论**，
    五类占比仍然照原样给（界面上的堆叠条还能看出 LN 到底占多少）。
    """
    ln = float(share.get("ln") or 0.0)
    if ln >= HYBRID_LN_FULL:
        return "ln"
    if ln >= HYBRID_LN_MIN:
        return "hybrid"
    if float(hold_ratio or 0.0) >= HYBRID_HOLD_MIN and ln >= HYBRID_HOLD_LN_MIN:
        return "hybrid"
    return max(RC_CLASSES, key=lambda k: float(share.get(k) or 0.0))


def class_group(share: dict, hold_ratio: float = 0.0) -> str:
    """rc / ln / hybrid —— 这张图属于哪一大类，不细分 RC 的键型。

    和 `dominant_class` 的分工：那个用来展示（"这张图主要是切/叠/乱"），
    这个用来判断"能不能按 RC 的准确率口径去比"。
    RC 图这里一律是 "rc" —— 以前把 stack/spam/tech 直接当 map_class 存下来，
    评估那边只认 "rc"，等于把一大批正经 RC 图当成 LN/Hybrid 排除了。
    """
    key = dominant_class(share, hold_ratio)
    return key if key in ("ln", "hybrid") else "rc"


@dataclass
class Block:
    """谱面的一小段（约 64 个物件）与它的粗分类。

    为什么要分块：按单个音符的局部窗口独立判断，很容易把「双押切」里的一个单键
    判成散音、把叠图里的过渡段判成切。先看这一大段整体像什么，再在类内挑细分，
    大方向才稳。块与块之间重叠 25%，避免键型被边界切断。
    """

    start_index: int
    end_index: int
    start_time: float
    end_time: float
    notes: int
    jack_ratio: float
    chord_ratio: float
    regularity: float
    snap_entropy: float
    nps: float = 0.0
    same_group: float = 0.0
    # 四押（4K：四键全按）物件在本块里占的比例。四押在连续段落里必然形成叠的配置。
    quad_share: float = 0.0
    # 比 1/16 音更密（≤ FAST_SUBDIVISION）的物件占比：用来区分「乱」（speed）与「切」。
    fast_share: float = 0.0
    # 节奏的短周期重复率（0..1）。1 = 间隔序列在按 2/3/4/6 行循环（节奏可预测），
    # 越小说明节奏一直在变——那是「技术」的核心特征，不是「乱」（乱只是快而已）。
    rhythm_repeat: float = 1.0
    category: str = ""

    @property
    def label(self) -> str:
        return CATEGORY_LABEL.get(self.category, self.category)


def _snap_bucket(subdivision: float) -> str:
    """把细分值归到最近的常见档位，用于衡量「节奏型混了几种」。

    档名口径：`1/N 音` 指一小节（4/4）里有 N 个，也就是间隔 = 4/N 拍。
    所以 0.25 拍是 1/16 音、0.125 拍是 1/32 音；三连音那几档按同一个口径算，
    0.667 拍是 1/6 音、0.333 拍是 1/12 音、0.167 拍是 **1/24** 音
    （早先这三档写成了 1/3、1/6、1/12，正好差一半，和玩家说的「24 分音」对不上）。
    """
    for name, v in (("1/4", 1.0), ("1/6", 0.667), ("1/8", 0.5), ("1/12", 0.333),
                    ("1/16", 0.25), ("1/24", 0.167), ("1/32", 0.125)):
        if abs(subdivision - v) <= v * 0.18:
            return name
    return "other"


def _entropy(counts: dict, total: int) -> float:
    """归一化熵（0 = 只有一种，1 = 均匀分布）。"""
    if total <= 0 or len(counts) <= 1:
        return 0.0
    h = -sum((c / total) * math.log(c / total + 1e-12) for c in counts.values())
    return h / math.log(len(counts))


def _same_group_ratio(seg: list[NoteContext]) -> float:
    """同一组和弦连续重复的比例（含隔行重复）。

    用来补 16 分口径够不到的情况：1/8 层的切里和弦在换，但"同一组双押/三押
    连着出现"是叠。实测：1/8 的夹叠图 Purple 是 0.39~0.5，而切图只有 0.10~0.19。
    隔行也算，是因为常见的写法是 A、B、A、B 交替重复同一组和弦。
    """
    rows: dict[float, set] = {}
    for c in seg:
        rows.setdefault(c.time, set()).add(c.column)
    keys = list(rows.values())
    hit = total = 0
    for i in range(1, len(keys)):
        if len(keys[i]) < 2:
            continue
        total += 1
        if keys[i] == keys[i - 1] or (i >= 2 and keys[i] == keys[i - 2]):
            hit += 1
    return hit / total if total else 0.0


def _rhythm_repeat(seg: list[NoteContext]) -> float:
    """节奏的短周期重复率（0..1）：间隔序列有多"可预测"。

    做法：把行间隔序列和它往前挪 k 行（k = 2/3/4/6）的自己逐项对比，落在同一档（±10% 或
    ±6ms）的比例取最大。1 = 每隔几行就重复同一种间隔——均匀连打、固定图样循环都算；
    越小说明间隔一直在换档，也就是「节奏在变」。

    为什么要这个：只数"混了几种细分"是静态构成，分不出「16 分夹 8 分」这种规整的跳押
    和「12 分、16 分、24 分掺着来」的技术段——前者的间隔序列是 55/55/110 循环，
    后者根本找不到短周期。技术段的定义落在这上面，判据才和玩家的感受对得上。
    """
    times = sorted({c.time for c in seg})
    gaps = [b - a for a, b in zip(times, times[1:])]
    if len(gaps) < 6:
        return 1.0
    best = 0.0
    for k in RHYTHM_PERIODS:
        hit = total = 0
        for i in range(k, len(gaps)):
            total += 1
            if abs(gaps[i] - gaps[i - k]) <= max(RHYTHM_TOL_MS, RHYTHM_TOL_RATIO * gaps[i]):
                hit += 1
        if total:
            best = max(best, hit / total)
    return best


def _regularity(seg: list[NoteContext]) -> float:
    """节奏规则度：相邻时间点的间隔集中在某一档的比例。

    只看时间点（不是物件），否则双押/三押里的 0 间隔会把"规则"这一维拉歪。
    """
    times = sorted({c.time for c in seg})
    gaps = [b - a for a, b in zip(times, times[1:])]
    if not gaps:
        return 0.0
    bucket: dict[float, int] = {}
    for g in gaps:
        k = round(g / 5.0) * 5.0
        bucket[k] = bucket.get(k, 0) + 1
    modal = max(bucket.values())
    near = sum(v for k, v in bucket.items()
               if any(abs(k - m) <= m * 0.20 for m, mv in bucket.items() if mv == modal))
    return near / len(gaps)


def _calm_share(seg: list[NoteContext]) -> float:
    """轻音占比：密度低、不成押、也不是连打/叠的物件。

    用它判过渡段比用块的平均 NPS 灵敏——过渡段通常只有几秒，
    64 个物件的块会把它稀释掉。
    """
    return sum(
        1 for c in seg
        if c.density_1s < 8.0 and c.chord_size == 1 and not c.is_jack and not c.is_stream
    ) / len(seg)


def _block_category(hold_ratio: float, calm_share: float, snap_ent: float, jack: float,
                    same_group: float, regularity: float, chord: float,
                    quad: float = 0.0, fast: float = 0.0, repeat: float = 1.0,
                    layer: float = 0.0) -> str:
    """块的粗分类。顺序即优先级。

    长条最优先——面图的难点不在 RC 键型上；其次是低密度且无明确键型的过渡段。
    layer 是块内行间隔 / 拍长，只在 same_group 那一条用得上。
    """
    if hold_ratio >= 0.45:
        return "ln"
    if calm_share >= 0.40:
        return "calm"
    # 四押（4K 里就是四键全按）在连续段落里必然形成叠的配置：一行四押之后，
    # 下一行不管怎么写都会复用其中的列，同一组手指被反复按下。实测"切"块的四押占比
    # 90 分位只有 0.03，而真正的四押段在 0.3~0.6，所以门槛取 0.15。
    # 这一条排在乱/技术之前：四押段几乎必然伴随节奏起伏，但它的难点是"叠"，
    # 不是节奏——不先拦下来，和弦满地的叠图会被"节奏在变"抢走（实测 Envy - Observation）。
    if quad >= QUAD_STACK_SHARE and chord >= 0.50:
        return "stack"
    # 「乱」= 高速单键：等效 1/24、1/32 音一级（比 1/16 更密）。切是按 1/16 音组织的写法，
    # 所以先按密度分流，别把 speed 图一路塞进切 / tech。
    # 和弦太多的不算乱（那是叠），门槛见 chord < 0.55：speed 图里夹的双押三押不影响。
    # 还要求节奏本身不起伏（repeat 高）：同样是高速，间隔一直在换档的是「技术」，不是「乱」。
    if fast >= FAST_SHARE_MIN and chord < 0.55 and repeat > RHYTHM_REPEAT_TECH:
        return "spam"
    # 「技术」的主判据是节奏在变——不是"快"，也不是"混了几种细分"。
    # 12 分、16 分、24 分掺着来的段落，行间隔序列找不到短周期，repeat 会明显偏低。
    # 同列连按很明显的块仍然算叠（叠图里也常有节奏变化，主因是手指连按）。
    if repeat <= RHYTHM_REPEAT_TECH and jack < 0.12:
        return "tech"
    # 节奏型本身就杂（几种细分混着来）：排在叠之前——
    # 但同列连按很明显的块仍然算叠。
    if snap_ent >= TECH_ENTROPY and jack < 0.12:
        return "tech"
    # 叠还有两条路：16 分层的同列连按（jack），或者同一组和弦连续重复（same_group）。
    # jack 的门槛取 0.12：更低的块里同列回按只是零星巧合，够不上"这一段的写法是叠"。
    # same_group 只在 1/8 层（行间隔 ≈ 0.5 拍）才算叠：16 分行的双押切图写出来就是
    # 12-34-12-34，和弦组天然在重复，但那是切不是叠（Exile 的 16 分同轨只占 1%，
    # 却因为 same_group 高达 0.65 被整段判成叠）。加了这一层后，20 张 Reform 7~10
    # 的切图判定全部不变，只是叠占比回落。
    if jack >= 0.12:
        return "stack"
    if same_group >= SAME_GROUP_MIN and 0.42 <= layer <= 0.58:
        return "stack"
    if regularity < 0.45:
        return "tech"
    if chord >= 0.30:
        return "cut"
    return "spam"


def _make_block(seg: list[NoteContext], start: int, keys: int, holds: list[bool] | None = None,
                wide: list[NoteContext] | None = None) -> Block:
    n = len(seg)
    jack = sum(1 for c in seg if c.is_jack) / n
    chord = sum(1 for c in seg if c.chord_size >= 2) / n
    same_group = _same_group_ratio(seg)
    regularity = _regularity(seg)

    subs: dict[str, int] = {}
    for c in seg:
        b = _snap_bucket(c.subdivision)
        subs[b] = subs.get(b, 0) + 1
    snap_ent = _entropy(subs, n)

    nps = sum(c.density_1s for c in seg) / n
    hold_ratio = (sum(1 for h in holds[start : start + n] if h) / n) if holds else 0.0
    quad = sum(1 for c in seg if c.chord_size >= keys) / n
    fast = sum(1 for c in seg if c.subdivision <= FAST_SUBDIVISION) / n
    repeat = _rhythm_repeat(wide if wide else seg)
    # 块的行间隔层：same_group 判叠只在 1/8 层有效，16 分行的和弦重复是切。
    row_gap = _median([c.row_gap for c in seg])
    beat = _median([c.beat_length for c in seg])
    layer = (row_gap / beat) if beat > 0 and row_gap < 9999 else 0.0
    category = _block_category(hold_ratio, _calm_share(seg), snap_ent,
                               jack, same_group, regularity, chord, quad, fast, repeat,
                               layer)

    return Block(
        start_index=start,
        end_index=start + n - 1,
        start_time=seg[0].time,
        end_time=seg[-1].time,
        notes=n,
        jack_ratio=round(jack, 3),
        chord_ratio=round(chord, 3),
        regularity=round(regularity, 3),
        snap_entropy=round(snap_ent, 3),
        nps=round(nps, 1),
        same_group=round(same_group, 3),
        quad_share=round(quad, 3),
        fast_share=round(fast, 3),
        rhythm_repeat=round(repeat, 3),
        category=category,
    )


def build_blocks(contexts: list[NoteContext], keys: int = 4, holds: list[bool] | None = None) -> list[Block]:
    """把谱面切成互相重叠的小段，并给每段一个粗分类。

    holds 与 contexts 一一对应（True = 长条）。有它才能把面图单独划出来，
    否则一张纯 LN 图会被硬塞进叠/乱/切里。
    """
    n = len(contexts)
    if n == 0:
        return []
    step = max(1, BLOCK_NOTES - BLOCK_OVERLAP)
    bounds: list[tuple[int, int]] = []
    i = 0
    while i < n:
        end = min(i + BLOCK_NOTES, n)
        # 尾部太短就并进上一块，不单独成块（半个块的特征不稳）
        if end - i < BLOCK_NOTES // 2 and bounds:
            bounds[-1] = (bounds[-1][0], n)
            break
        bounds.append((i, end))
        if end >= n:
            break
        i += step
    return [_make_block(contexts[a:b], a, keys, holds,
                        # 节奏变化要看得比一个块更长：64 个物件只有 3 秒左右，
                        # 短窗口里"换档"很容易被巧合带偏。前后各放宽半个块。
                        contexts[max(0, a - BLOCK_NOTES // 2):min(n, b + BLOCK_NOTES // 2)])
            for a, b in bounds]


def assign_coarse(contexts: list[NoteContext], blocks: list[Block]) -> None:
    """把块的粗分类写回每个音符；重叠区以后一块为准。"""
    for b in blocks:
        for c in contexts[b.start_index : b.end_index + 1]:
            c.coarse = b.category


# 「这一击按了几个键」——描述打击构成，不是段落键型。
# 它们只该出现在 calm 段（那一段本来就没有稳定写法），或者整块都没稳定写法时兜底。
_HIT_KINDS = frozenset(("single", "jump", "hand", "quad"))

# 块内投票平票时的取舍：名字越具体的越优先。
# 平票意味着两种写法在块里交替出现，这时报更"有特征"的那个，
# 比随机取字典序靠前的那个更接近玩家的说法。
_SUB_PRIORITY = {
    "chordjack": 9, "minijack": 8, "longjack": 8,
    "jumpstream": 7, "handstream": 7, "quadstream": 7,
    "jumptrill": 6, "trill": 6, "spamstream": 5, "stream": 4, "anchor": 3,
}


def assign_subpattern(contexts: list[NoteContext], blocks: list[Block]) -> None:
    """把细分键型从「逐音符」改成「逐段」：一块只出一个名字。

    玩家的说法是「这是一张 jumpstream 图」，不是「这张图 40% 的音符是
    jumpstream、12% 是 jump」——那 12% 的 jump 本来就是 jumpstream 的组成部分。
    所以块内投票取众数，押数型（single / jump / hand / quad）不参与投票。

    calm 段例外：它本来就是没有稳定写法的过渡段，逐音符按押数描述才准确；
    段里的长条单独标 LN，不然会被混进 single / jump。

    必须在 assign_coarse 之后调用（类内判定要用 ctx.coarse）。
    """
    for b in blocks:
        seg = contexts[b.start_index : b.end_index + 1]
        if not seg:
            continue
        if b.category == "ln":
            # 长条段的写法按「松手时机 / 密度 / 同时按住」三选一，
            # 口径见 app/core/ln.py（官方 wiki 的 release / density / coordination）
            kind = ln_patterns.label_of(seg)
            for c in seg:
                c.sub = kind
            continue
        if b.category == "calm":
            for c in seg:
                c.sub = "longnote" if c.is_hold else _by_chord(c.chord_size)
            continue
        votes = collections.Counter(
            _classify_with(b.category, c) for c in seg
        )
        cand = {k: n for k, n in votes.items() if k not in _HIT_KINDS}
        if cand:
            top = max(cand.items(), key=lambda kv: (kv[1], _SUB_PRIORITY.get(kv[0], 0)))[0]
            for c in seg:
                c.sub = top
        else:
            # 整块只有单次多押、没有稳定写法：保留逐音符描述，至少不说谎
            for c in seg:
                c.sub = _classify_with(b.category, c)


def is_jack_gap(gap_ms: float, row_gap: float, beat_length: float = 0.0, chord_size: int = 1) -> bool:
    """同列上一次按键是不是「叠」。

    分两条路，对应两种真实写法：

    * **16 分层**（行间隔 ≤ 0.34 拍）：同一列紧邻下一行再按一次。
      切图的 16 分位置在不同列之间轮转，同列连着吃两个位置的情况几乎不出现。
    * **1/8 层**（行间隔 ≈ 0.5 拍）：同一列**每两行回一次**（也就是 1/4 间隔），
      而且那一击不属于大和弦。这是"左右手交替里某根手指被反复用"的写法，
      实测 Rampage 是 34.6%，而切图只有 0.5%~3.2%、Betwixt 8.5%。
      注意 1/8 层**每行**回同列不算——那是和弦轮转时共用的列（Re:Dive 就是这种）。
    """
    if gap_ms >= 9999.0:
        return False
    if row_gap <= 0 or row_gap >= 9999.0:
        return gap_ms <= 180.0
    layer = (row_gap / beat_length) if beat_length > 0 else 0.0
    if layer <= JACK_LAYER_MAX:
        return gap_ms <= max(JACK_MIN_MS, row_gap * JACK_ROW_RATIO)
    if JACK_EIGHTH_LAYER[0] <= layer <= JACK_EIGHTH_LAYER[1]:
        ratio = gap_ms / row_gap
        return JACK_EIGHTH_RATIO[0] <= ratio <= JACK_EIGHTH_RATIO[1] and chord_size <= 2
    return False


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
                                          contexts[i].row_gap, contexts[i].beat_length,
                                          contexts[i].chord_size):
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


def _stream_family(ctx: NoteContext) -> str:
    """按周围的多押构成，把切分为 单押切 / 双押切 / 三押切 / 四押切。

    4K 里没有 quadstream：四个键全按就是"四押"，它出现的地方按 handstream 归；
    真正成段的四押在块级别已经判成"叠"了（见 QUAD_STACK_SHARE）。
    quadstream 只留给 5 键以上的谱面。
    """
    j2, j3, j4 = ctx.chord_mix
    if ctx.keys >= 5 and j4 >= _QUAD_SHARE:
        return "quadstream"
    if j3 >= _HAND_SHARE or j4 >= _QUAD_SHARE:
        return "handstream"
    if j2 >= _JUMP_SHARE:
        return "jumpstream"
    return "stream"


def _by_chord(chord: int) -> str:
    """按一次按下的键数给最朴素的键型名。"""
    if chord >= 4:
        return "quad"
    if chord == 3:
        return "hand"
    if chord == 2:
        return "jump"
    return "single"


def _classify_ln(ctx: NoteContext) -> str:
    """长条段不参与 RC 的键型细分：这一段的难点在长条本身。"""
    return "longnote"


def _classify_calm(ctx: NoteContext) -> str:
    """过渡段：密度低、没有明确键型，按押数简单标注即可。"""
    return _by_chord(ctx.chord_size)


def _classify_stack(ctx: NoteContext) -> str:
    """叠段：重点是「同一组手指被反复按下」，不是单次多押。"""
    chord = ctx.chord_size
    # 只要这一击属于「和弦在反复」或「同列紧邻回按」，就算多押叠。
    # 否则玩家会看到一段明明是叠的谱，细分里写着"双押/四押"，那是单次多押的说法。
    if chord >= 2 and (ctx.is_jack or ctx.repeat_chord or ctx.row_overlap >= 0.5):
        return "chordjack"
    if ctx.is_jack:
        return "longjack" if ctx.jack_run_len >= 3 else "minijack"
    if ctx.repeat_chord:
        return "chordjack"
    if chord >= 2:
        return _by_chord(chord)
    # 叠段里的单键就是这根手指在连按，归"小叠"；"散音"是给没有键型的段落用的，
    # 一段明明判成叠、细分里却写散音，玩家会觉得前后矛盾。
    return "anchor" if ctx.column_dominant else "minijack"


def _classify_cut(ctx: NoteContext) -> str:
    """切段：按周围的多押构成分单押切 / 双押切 / 三押切 / 四押切。"""
    if ctx.is_trill and ctx.chord_size >= 2:
        return "jumptrill"
    return _stream_family(ctx)


def _classify_spam(ctx: NoteContext) -> str:
    """乱段：没有稳定键型的密集单键，细看是不是交互或连打。"""
    if ctx.is_trill and ctx.chord_size == 1:
        return "trill"
    if ctx.is_stream:
        # 这里是"乱"块里的连打，不能再叫"单押切"——块的大类是乱，细分写切会自相矛盾
        return "spamstream"
    return _by_chord(ctx.chord_size)


def _classify_tech(ctx: NoteContext) -> str:
    """tech 段：节奏型在换，先看手型（交互 / 叠 / 连打），再退回押数。"""
    if ctx.is_trill:
        return "jumptrill" if ctx.chord_size >= 2 else "trill"
    if ctx.is_jack and ctx.chord_size >= 2:
        return "chordjack"
    if ctx.is_stream:
        return _stream_family(ctx)
    return _by_chord(ctx.chord_size)


def _classify_by_motion(ctx: NoteContext) -> str:
    """还没分块时（单测或上游漏了 assign_coarse）退回按动作本身判断。"""
    if ctx.is_jack:
        if ctx.chord_size >= 2:
            return "chordjack"
        return "longjack" if ctx.jack_run_len >= 3 else "minijack"
    if ctx.is_trill:
        return "jumptrill" if ctx.chord_size >= 2 else "trill"
    if ctx.is_stream:
        return _stream_family(ctx)
    return _by_chord(ctx.chord_size)


# 粗分类 → 类内判定。键就是 Block.category 的取值。
_BY_CATEGORY = {
    "ln": _classify_ln,
    "calm": _classify_calm,
    "stack": _classify_stack,
    "cut": _classify_cut,
    "spam": _classify_spam,
    "tech": _classify_tech,
}


def _classify_in_category(ctx: NoteContext) -> str:
    """只看这一击所在的大类，在该类内部挑细分（不考虑段落级结论）。"""
    return _classify_with(ctx.coarse, ctx)


def _classify_with(category: str, ctx: NoteContext) -> str:
    """按指定的粗分类挑细分。

    `assign_subpattern` 投票时必须用块自己的 category，不能用 ctx.coarse：
    块之间有 25% 重叠，ctx.coarse 是"最后一个覆盖它的块"的分类，
    拿它投票会让相邻块的判定串进来（实测会让 LN 段的长条混进 tech 块的票里）。
    """
    handler = _BY_CATEGORY.get(category)
    return handler(ctx) if handler else _classify_by_motion(ctx)


def classify(ctx: NoteContext | None) -> str:
    """判定这个物件属于哪种键型，返回机器可读的 key（中文名见 PATTERN_LABEL）。

    分三层：先看所属大块的四类粗分类（叠 / 乱 / 切 / tech），再在**类内**挑细分，
    最后由 `assign_subpattern` 用块内众数盖掉——玩家的键型说法是段落级的，
    一段 jumpstream 里的单键仍然属于 jumpstream，不该单独记成 stream。

    锚不再当兜底：只有在「叠」类块里、且这一列在邻域占 ≥40% 时才给。
    以前它是单键的默认分支，密集段里随手一个单键就成锚，等于垃圾桶。
    """
    if ctx is None:
        return "single"
    return ctx.sub or _classify_in_category(ctx)


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
    """把上下文压缩成一个可比较的签名，用于"同类片段里他是不是也失误"。

    键型这一维用**大类**（叠 / 乱 / 切 / tech …）而不是细分：
    细分太细，一张谱上每类只有几十个音符，统计没有意义；
    而"他在叠段里是不是常规吃亏"这种说法，用大类反而更稳、更好懂。
    """
    dens_bin = bisect.bisect_right(density_edges, ctx.density_1s)
    if ctx.coarse:
        pat = ctx.coarse
    else:
        pat = "chord" if ctx.chord_size >= 3 else ("jack" if ctx.is_jack else ("trill" if ctx.is_trill else "single"))
    return (
        dens_bin,
        pat,
        "fast" if ctx.subdivision <= 0.26 else ("mid" if ctx.subdivision <= 0.52 else "slow"),
    )


def signature_edges(contexts: list[NoteContext], n_bins: int = 5) -> list[float]:
    return quantile_edges([c.density_1s for c in contexts], n_bins)
