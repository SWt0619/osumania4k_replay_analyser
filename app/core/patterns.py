"""为谱面的每个物件算出「局部上下文」：密度、和弦、连打、交错、节拍位置等。

这些特征是「失误归因」的输入：同样是打偏 40ms，
  发生在 2 NPS 的散音上  → 大概率是分神
  发生在 12 NPS 的 1/4 连打里 → 更可能是能力上限

键型命名沿用 osu!mania 的通用说法（中英对照与判据见 PATTERN_LABEL 与 classify()）：
stream / jumpstream / handstream / chordjack / trill / jumptrill / jack / minijack / anchor。
"""

from __future__ import annotations

import bisect
import math
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
    "longnote": "长条",         # 长条为主的段落，不套用 RC 的四大类
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

    return _Grid(times=times, cols=[o.column for o in objs], uniq_times=uniq_times,
                 ut_index=ut_index, col_times=col_times, row_gaps=row_gaps,
                 marks=_beat_marks(beatmap))


def _context_of(i: int, obj, grid: _Grid) -> NoteContext:
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
    )


def build_context(beatmap: Beatmap) -> list[NoteContext]:
    """给每个物件算一份局部上下文；算完再统一补「和弦占比 / 连打链 / 和弦重复」。"""
    objs = beatmap.objects
    if not objs:
        return []

    grid = _build_grid(beatmap)
    out = [_context_of(i, o, grid) for i, o in enumerate(objs)]

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
# 典型 tech 图 0.87，取 0.75 只留下真正"几种节奏型混着来"的段落。
# 这一条要排在叠之前：节奏本身就在变的块，先归 tech 比归叠更接近玩家的说法。
TECH_ENTROPY = 0.82

# 分块：每块约 64 个物件，相邻块重叠 16 个（25%），避免键型在块边界被腰斩。
BLOCK_NOTES = 64
BLOCK_OVERLAP = 16

# 四类粗分类的中文名。键型细分在各自大类内部进行（见 classify）。
# 另加一个"长条"大类：面（LN）图的难点在长条本身，套用 RC 的四大类只会给出误导性的结论。
CATEGORY_LABEL = {
    "ln": "长条",
    "calm": "过渡",
    "stack": "叠",
    "spam": "乱",
    "cut": "切",
    "tech": "tech",
}


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
    category: str = ""

    @property
    def label(self) -> str:
        return CATEGORY_LABEL.get(self.category, self.category)


def _snap_bucket(subdivision: float) -> str:
    """把细分值归到最近的常见档位，用于衡量「节奏型混了几种」。"""
    for name, v in (("1/4", 1.0), ("1/3", 0.667), ("1/8", 0.5), ("1/6", 0.333),
                    ("1/16", 0.25), ("1/12", 0.167)):
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
                    same_group: float, regularity: float, chord: float) -> str:
    """块的粗分类。顺序即优先级。

    长条最优先——面图的难点不在 RC 键型上；其次是低密度且无明确键型的过渡段。
    """
    if hold_ratio >= 0.45:
        return "ln"
    if calm_share >= 0.40:
        return "calm"
    # 节奏型本身就杂（几种细分混着来）：排在叠之前——
    # 但同列连按很明显的块仍然算叠（叠图里也常有节奏变化，主因是手指连按）。
    if snap_ent >= TECH_ENTROPY and jack < 0.12:
        return "tech"
    # 叠有两条路：16 分层的同列连按（jack），或者同一组和弦连续重复（same_group）。
    # 后者专治 1/8 层的夹叠图——那种图同列间隔够不到 16 分，但和弦在反复。
    # jack 的门槛取 0.12：更低的块里同列回按只是零星巧合，够不上"这一段的写法是叠"；
    # 块级同组重复的分布与门槛见 SAME_GROUP_MIN 上方的实测值。
    if jack >= 0.12 or same_group >= SAME_GROUP_MIN:
        return "stack"
    if regularity < 0.45:
        return "tech"
    if chord >= 0.30:
        return "cut"
    return "spam"


def _make_block(seg: list[NoteContext], start: int, keys: int, holds: list[bool] | None = None) -> Block:
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
    category = _block_category(hold_ratio, _calm_share(seg), snap_ent,
                               jack, same_group, regularity, chord)

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
    return [_make_block(contexts[a:b], a, keys, holds) for a, b in bounds]


def assign_coarse(contexts: list[NoteContext], blocks: list[Block]) -> None:
    """把块的粗分类写回每个音符；重叠区以后一块为准。"""
    for b in blocks:
        for c in contexts[b.start_index : b.end_index + 1]:
            c.coarse = b.category


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
    """按周围的多押构成，把切分为 单押切 / 双押切 / 三押切 / 四押切。"""
    j2, j3, j4 = ctx.chord_mix
    if j4 >= _QUAD_SHARE:
        return "quadstream"
    if j3 >= _HAND_SHARE:
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
    if chord >= 2 and (ctx.is_jack or ctx.repeat_chord):
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
        return "stream"
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


def classify(ctx: NoteContext | None) -> str:
    """判定这个物件属于哪种键型，返回机器可读的 key（中文名见 PATTERN_LABEL）。

    分两层：先看所属大块的四类粗分类（叠 / 乱 / 切 / tech），再在**类内**挑细分。
    这样大方向由整段决定，单个音符的局部抖动改变不了结论。

    锚不再当兜底：只有在「叠」类块里、且这一列在邻域占 ≥40% 时才给。
    以前它是单键的默认分支，密集段里随手一个单键就成锚，等于垃圾桶。
    """
    if ctx is None:
        return "single"
    handler = _BY_CATEGORY.get(ctx.coarse)
    return handler(ctx) if handler else _classify_by_motion(ctx)


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
