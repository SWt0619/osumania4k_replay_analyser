"""把「回放的按下事件」和「谱面的物件」配对，得出每一击的误差。

这是整个分析的地基：所有误差、判定、失误都从这里来，所以规则要写清楚、可解释、可测试。

配对规则（逐列独立）：
  * 一次按下只能命中同一列的一个物件。
  * 允许的最大偏差 = 该谱面的**命中窗口**（meh 窗口，151 - 3*OD），而不是 miss 窗口。
    这是关键：误差超过 meh 时官方就已经判漏了，那一下按键并没有打在这颗物件上，
    它很可能是**下一颗物件的稍早按键**，必须留着给后面的物件（见下面的"不消耗"规则）。
  * 按时间顺序贪心配对：先来的物件先挑，避免后面的物件抢走前面物件的按键。
  * 配不上的按键 = 空敲；配不到按键的物件 = 漏接。两者都**不消耗**对方。

长按（hold）：
  * 头部按普通物件判。
  * **ScoreV1**：整条只出一个判定。用「按下之后该列最后一次松手」算尾部误差，
    再按官方的合并口径降档（|头| ≤ 各档窗口的 1.2 / 1.1 / 1 倍，且
    |头|+|尾| ≤ 2.4 / 2.2 / 2 倍），见 _hold_judgement()。体中松过手（hold break）
    会把整条封顶在 MEH，而不是直接算 miss —— 官方只要求"按到尾部"，
    中途松手后按回来仍然能拿到 MEH。
  * **ScoreV2**：头、尾各判一次，尾窗口放大 1.5 倍，长按算两次判定、两点连击。
    体中松手同样把尾判封顶在 MEH。两套结果都留在 JudgedNote 里，
    页面可以随时切换（见 judged(mode)）。
"""

from __future__ import annotations

import bisect
import itertools
import math
from dataclasses import dataclass, field

from .beatmap import Beatmap, HitObject, judge_offset, judgement_windows, judge_with_windows
from .osr import PressEvent, ReleaseEvent

# ScoreV1 的判定权重：300/MAX 同权，200=2/3，100=1/3，50=1/6，MISS=0。
# 用精确分数而不是 0.667/0.333/0.167 —— 后者在大谱面上会累积出千分之几的偏差，
# 而官方公式就是 200/300、100/300、50/300。
V1_WEIGHTS = {"perfect": 1.0, "great": 1.0, "good": 2 / 3, "ok": 1 / 3, "meh": 1 / 6, "miss": 0.0}

# ---------------------------------------------------------------------------
# ScoreV2 常量
#
# 依据：
#   * osu! 官方 wiki「Hold note」：ScoreV2 下长按的头和尾**分别**判定，
#     中间按住不再每 tick 加连击（一个长按恰好 2 次判定、2 点连击）。
#   * osu! 官方 wiki「Scoring」：判定分表 + 连击/准度两段式结构。
#   * mania ScoreV2 专题：连击倍率 log4、准度分 80 万、连击分 20 万。
# ---------------------------------------------------------------------------

# 准度口径的判定分：MAX 比 300 多 5 分，所以只有全 MAX 才有 100% 准确率
V2_ACC_VALUE = {"perfect": 305, "great": 300, "good": 200, "ok": 100, "meh": 50, "miss": 0}
# 连击口径的判定分：这里**彩 300 和黄 300 都是 300**（只有准度口径才区分 305）
V2_COMBO_VALUE = {"perfect": 300, "great": 300, "good": 200, "ok": 100, "meh": 50, "miss": 0}

# 连击倍率的封顶：连击数超过 400 之后倍率不再增长（log4 400）
COMBO_MULT_CAP = 400


def combo_multiplier(combo: int) -> float:
    """ScoreV2 的连击倍率。

    连击数 ≤ 1 时是 0.5；2..400 是 log4(连击数)；超过 400 固定 log4(400)。
    """
    if combo <= 1:
        return 0.5
    if combo > COMBO_MULT_CAP:
        return math.log(COMBO_MULT_CAP, 4)
    return math.log(combo, 4)


def max_combo_from(judgements) -> int:
    """最长连击：mania 里任何非 miss 都保连。

    单独一个函数，是因为「什么算断连」要在两处用同一把尺子：
    汇总里的 maxCombo，以及「修掉几处失误后连击能到多少」的预估。
    以前两处各写一遍，规则一改就只改到一边。
    """
    best = 0
    run = 0
    for j in judgements:
        if j == "miss":
            run = 0
        else:
            run += 1
            if run > best:
                best = run
    return best


@dataclass
class JudgedNote:
    """一个物件的判定结果，同时保存 V1 与 V2 两套口径。

    V1（stable ScoreV1）：
      * `judgement` —— 长条按头尾合并误差算出的那一个判定；
      * 普通音符与 `head_judgement` 相同。
    V2（ScoreV2 / lazer 默认）：
      * `head_judgement` —— 头判，按普通音符窗口；
      * `tail_judgement` —— 尾判，长条才有，窗口放大 1.5 倍；
      * `judgement_v2`   —— 头尾里更差的那个，用于显示、断连与失误判断。

    两套同时留着，页面切换时不需要重算，也不会出现"切过去以后数字对不上"。
    """

    index: int              # 显示序号（按时间排序后重排）
    obj_index: int          # 对应谱面原始物件序号（做特征关联时用）
    column: int
    note_time: float
    is_hold: bool
    end_time: float | None
    judgement: str                 # ScoreV1 的最终判定（长条 = 头与尾合并）
    delta: float | None            # 按下时间 - 物件时间（ms，负数=偏早）；漏掉为 None
    press_time: float | None
    release_delta: float | None = None   # 松手时间 - 长按结束时间
    head_judgement: str = ""             # V2 头判
    tail_judgement: str | None = None    # V2 尾判
    judgement_v2: str = ""               # V2 最终判定（头尾更差者）
    body_break: bool = False             # 长条体中松过手（官方会封顶 MEH 并断连）
    dropped: bool = False                # V1 下整条掉判定（提前松到窗口外）

    SEVERITY = {"perfect": 0, "great": 0, "good": 1, "ok": 2, "meh": 2, "miss": 3}

    def __post_init__(self) -> None:
        # 普通音符没有单独的尾判：两套口径就是同一个判定。
        # 放在这里而不是每个构造点手写，避免漏填导致 V2 显示成空白。
        if not self.head_judgement:
            self.head_judgement = self.judgement
        if not self.judgement_v2:
            self.judgement_v2 = self.judgement

    def judged(self, mode: str = "v1") -> str:
        """当前口径下的判定名。mode 只认 "v1" / "v2"，其余一律按 v1。"""
        if mode == "v2":
            return self.judgement_v2 or self.judgement
        return self.judgement

    def severity_for(self, mode: str = "v1") -> int:
        return self.SEVERITY.get(self.judged(mode), 3)

    def is_error_for(self, mode: str = "v1") -> bool:
        """严重失误：100 / 50 / 漏接 / 长按掉。用于"断连、稳定度"统计。"""
        return self.severity_for(mode) >= 2

    def is_flaw_for(self, mode: str = "v1") -> bool:
        """有瑕疵：包含 200。用于归因（200 也是准确率损失，能暴露系统性偏差）。"""
        return self.severity_for(mode) >= 1

    @property
    def severity(self) -> int:
        """V1 的严重度，保留旧属性给尚未区分口径的调用点。"""
        return self.severity_for("v1")

    @property
    def is_error(self) -> bool:
        return self.is_error_for("v1")

    @property
    def is_flaw(self) -> bool:
        return self.is_flaw_for("v1")


def v1_weight(note: JudgedNote) -> float:
    """这一击在 ScoreV1 准确率里占的权重（0..1）。

    抽成函数是因为"当前这一段的准确率"（时间轴折线、此刻读数）也要用同一套权重：
    口径写两遍迟早会分叉，而 V1 的 200=2/3 这种事一旦分叉，两个数字就永远对不上。
    """
    return V1_WEIGHTS.get(note.judgement, 0.0)


@dataclass
class GhostPress:
    """没有对应物件的按下（多按 / 空敲）。"""

    time: float
    column: int


@dataclass
class MatchResult:
    notes: list[JudgedNote]
    ghosts: list[GhostPress]
    beatmap: Beatmap
    od: float
    windows: dict[str, float] = field(default_factory=dict)

    # ---- 汇总 -----------------------------------------------------------
    def counts(self, mode: str = "v1") -> dict[str, int]:
        """判定数量。V1 按物件数，V2 按判定单位数（长条算头尾两次）。"""
        out = {k: 0 for k in ("perfect", "great", "good", "ok", "meh", "miss")}
        if mode == "v2":
            for _t, j, _b in self.v2_units():
                out[j] = out.get(j, 0) + 1
            return out
        for n in self.notes:
            out[n.judgement] = out.get(n.judgement, 0) + 1
        return out

    def accuracy(self, mode: str = "v1") -> float:
        """加权准确率（百分比）。

        V1 用官方 ScoreV1 公式：
            (300*(MAX+300) + 200*GOOD + 100*OK + 50*MEH) / (300 * 总物件数)
        V2 用官方 ScoreV2 口径：MAX 比 300 多 5 分，长条算头尾两个判定。
        """
        if mode == "v2":
            units = self.v2_units()
            if not units:
                return 0.0
            total = sum(V2_ACC_VALUE.get(j, 0) for _t, j, _b in units)
            return total / (305.0 * len(units)) * 100.0
        if not self.notes:
            return 0.0
        total = sum(v1_weight(n) for n in self.notes)
        return total / len(self.notes) * 100.0

    # ---- ScoreV2 -------------------------------------------------------
    #
    # 结构（osu! 官方 wiki「Scoring」+ mania ScoreV2 专题）：
    #     总分 = 连击分 + 准度分
    #     连击分 = 200,000 × 实际连击参数 / 理论最大连击参数
    #              单个判定 = 判定数值 × 连击倍率(连击数)
    #              理论最大 = Σ 300 × 连击倍率(i)，i = 1..总判定数
    #     准度分   = 800,000 × 准确率^(2 + 2×准确率) × 已判定数/总判定数
    #     准确率   = Σ判定分 / (305 × 总判定数)
    # 与 ScoreV1 最大的结构差异：**一个长按算两次判定**（头 + 尾），
    # 所以总判定数 = 普通音符数 + 2 × 长按数，而不是物件数。
    # ------------------------------------------------------------------

    def v2_units(self) -> list[tuple[float, str, bool]]:
        """ScoreV2 的判定单位序列，按时间升序。

        每个元素 = (判定时刻, 判定名, 是否断连)。长按展开成头 + 尾两个单位；
        体中层松过手会把尾判封顶在 MEH，并且断连，但**不算 MISS**（lazer 的 hold break 口径）。
        头判与尾判是分开算的，这正是 V2 与 V1 的结构差异。
        """
        units: list[tuple[float, str, bool]] = []
        for n in self.notes:
            head = n.head_judgement or n.judgement
            units.append((n.note_time, head, head == "miss"))
            if not n.is_hold:
                continue
            tail = n.tail_judgement or head
            t = n.end_time if n.end_time is not None else n.note_time
            units.append((t, tail, n.body_break or tail == "miss"))
        units.sort(key=lambda u: u[0])
        return units

    def score_v2_parts(self) -> dict:
        """ScoreV2 的两段（准度 / 连击比例），stable 与 lazer 两种配比共用。"""
        units = self.v2_units()
        if not units:
            return {"accuracy": 0.0, "comboRatio": 0.0, "judgements": 0, "units": 0}

        total_units = len(units)
        acc_sum = sum(V2_ACC_VALUE.get(j, 0) for _t, j, _b in units)
        accuracy = acc_sum / (305.0 * total_units)

        # 实际连击参数：按时间顺序推进连击，断连处归零
        got = 0.0
        combo = 0
        for _t, j, brk in units:
            if brk:
                combo = 0
                continue
            combo += 1
            got += V2_COMBO_VALUE.get(j, 0) * combo_multiplier(combo)
        # 理论最大连击参数：全连且每一击都是 300
        ideal = 300.0 * sum(combo_multiplier(c) for c in range(1, total_units + 1))

        return {
            "accuracy": accuracy,
            "comboRatio": (got / ideal) if ideal > 0 else 0.0,
            "judgements": total_units,
            "maxCombo": combo,
        }

    def score_v2(self, lazer: bool = False) -> tuple[int, str]:
        """ScoreV2 总分与评级。

        lazer=False（默认）：osu!stable 的 ScoreV2 —— 20 万连击 + 80 万准度。
        lazer=True：osu!lazer 的标准分 —— 1 万连击 + 99 万准度。
        两者共用同一套准确率与连击比例，只是配比不同。
        """
        p = self.score_v2_parts()
        if not p["judgements"]:
            return 0, "D"
        acc = p["accuracy"]
        # 准度分的核心是 acc^(2+2·acc)：acc=1 时取满，acc=0.9 时约 0.9^3.8≈0.68，
        # 所以 ScoreV2 对准确率下降的惩罚远比线性"acc × 100 万"更狠。
        acc_portion = acc ** (2 + 2 * acc)
        combo_w, acc_w = (10_000, 990_000) if lazer else (200_000, 800_000)
        score = int(round(combo_w * p["comboRatio"] + acc_w * acc_portion))
        return score, score_v2_grade(acc)

    def max_combo(self, mode: str = "v1") -> int:
        """最长连击。V1 按物件、V2 按头尾两个单位。

        V2 下体中层松手会断连（即使尾判是 MEH），所以不能只看判定名。
        """
        if mode == "v2":
            best = cur = 0
            for _t, judgement, brk in self.v2_units():
                if brk or judgement == "miss":
                    cur = 0
                    continue
                cur += 1
                best = max(best, cur)
            return best
        return max_combo_from([n.judgement for n in self.notes])

    def deltas(self, mode: str = "v1") -> list[float]:
        """击打误差序列。V2 把长条尾判的误差也算进来（lazer 的 UR 口径）。"""
        out = [n.delta for n in self.notes if n.delta is not None]
        if mode == "v2":
            out += [
                n.release_delta for n in self.notes
                if n.is_hold and n.release_delta is not None and n.tail_judgement != "miss"
            ]
        return out


def score_v2_grade(accuracy: float) -> str:
    """ScoreV2 评级（按准确率，只有全 MAX 才能 SS）：SS / S≥95% / A≥90% / B≥80% / C≥70% / D。"""
    if accuracy >= 0.999999:
        return "SS"
    if accuracy >= 0.95:
        return "S"
    if accuracy >= 0.90:
        return "A"
    if accuracy >= 0.80:
        return "B"
    if accuracy >= 0.70:
        return "C"
    return "D"


# --------------------------------------------------------------------------- 配对


def _group_columns(
    beatmap: Beatmap, presses: list[PressEvent], releases: list[ReleaseEvent], keys: int
) -> tuple[list[list[HitObject]], list[list[float]], list[list[float]], list[GhostPress]]:
    """按列把「物件 / 按下 / 松手」分好组并按时间排序。

    配对是逐列独立的，分组之后每列就只剩一条双向指针扫描，不必全局排序比对。
    超出键数的按键在这里就记成空按：那本来就是"按到了不存在的列"。
    """
    notes_by_col: list[list[HitObject]] = [[] for _ in range(keys)]
    for obj in beatmap.objects:
        # 谱面里出现超出键数的列（改过键数的图）时直接丢弃：它永远配不到按键，
        # 留着只会让总物件数与判定数对不上
        if 0 <= obj.column < keys:
            notes_by_col[obj.column].append(obj)

    presses_by_col: list[list[float]] = [[] for _ in range(keys)]
    release_by_col: list[list[float]] = [[] for _ in range(keys)]
    ghosts: list[GhostPress] = []
    for p in presses:
        if 0 <= p.column < keys:
            presses_by_col[p.column].append(p.time)
        else:
            ghosts.append(GhostPress(time=p.time, column=p.column))
    for r in releases:
        if 0 <= r.column < keys:
            release_by_col[r.column].append(r.time)
    for lst in presses_by_col:
        lst.sort()
    for lst in release_by_col:
        lst.sort()
    return notes_by_col, presses_by_col, release_by_col, ghosts


def _pair_indices(note_times: list[float], press_times: list[float], hit_window: float):
    """纯配对：按贪心规则产出 (音符下标, 按键下标) 对。

    单独抽出来是因为「什么算配上」只有这一份定义：真正的判定用它，
    Random mod 推断列序时也用它（那边只关心配上了多少权重，不需要建对象）。
    规则说明见 `_pair_column`。
    """
    i = 0
    j = 0
    n = len(note_times)
    m = len(press_times)
    while i < n and j < m:
        if press_times[j] < note_times[i] - hit_window:
            j += 1
            continue
        if press_times[j] > note_times[i] + hit_window:
            i += 1
            continue
        yield i, j
        i += 1
        j += 1


def _pair_column(
    col: int,
    notes: list[HitObject],
    press_times: list[float],
    hit_window: float,
    od_value: float,
    out: list[JudgedNote],
    ghosts: list[GhostPress],
) -> None:
    """单列的贪心配对：谁先到谁先配，避免后面的物件抢走前面物件的按键。

    三种情况分开处理是关键：
      * 按键早于物件的命中窗口 → 空敲（按早了，配不上任何物件），只推进按键指针；
      * 按键晚于物件的命中窗口 → 物件漏接，**只推进物件指针**——这一下按键要留给
        后面的物件（它就是后面那颗物件的偏早按键，典型场景：连续十六分里第一颗漏了，
        玩家的手指其实是按在第二颗上）；
      * 落在窗口内 → 按误差判档，按键和物件一起消耗。

    第二条的"不消耗按键"是修过一个真实 bug 的：早期版本用 miss 窗口（188-3*OD）当
    配对窗口，于是"晚 164ms 的那一下"会被记成前一颗物件的 miss 并消耗掉，连锁导致
    后面两三颗明明打中了却被算成漏接，acc 能差出 0.08pp。
    """
    times = [n.time for n in notes]
    matched = dict(_pair_indices(times, press_times, hit_window))
    for i, note in enumerate(notes):
        j = matched.get(i)
        if j is None:
            out.append(_make_miss(note))
            continue
        pt = press_times[j]
        delta = pt - note.time
        judgement = judge_offset(delta, od_value)
        if judgement == "miss":
            # 只有刻意放宽（extra_window）的实验口径才会走到这里：正常口径下
            # 命中窗口就是 meh，窗口内不可能判 miss。仍记下这一下按键。
            out.append(_make_miss(note, press_time=pt, delta=delta))
        else:
            out.append(_make_hit(note, pt, delta, judgement))
    used = set(matched.values())
    for j, pt in enumerate(press_times):
        if j not in used:
            ghosts.append(GhostPress(time=pt, column=col))


def _column_weight(notes: list[HitObject], press_times: list[float],
                   hit_window: float, od_value: float) -> float:
    """把某一列音符与某一列按键配一遍，返回累计的 v1 权重（0..音符数）。

    只累加权重、不建判定对象：推断列序要把 4×4 种组合都比一遍，
    建对象纯属浪费。
    """
    times = [n.time for n in notes]
    total = 0.0
    for i, j in _pair_indices(times, press_times, hit_window):
        total += V1_WEIGHTS.get(judge_offset(press_times[j] - times[i], od_value), 0.0)
    return total


def infer_random_order(beatmap: Beatmap, presses, releases,
                       od: float | None = None) -> tuple[list[int], float, float]:
    """推断 Random mod 打乱后的列序。

    Random 会把四列的顺序重新排一遍，回放里记的是重排**之后**的列号，谱面文件却没变。
    重排结果没有存进 .osr，所以只能从这一局的数据反推：枚举所有列序，每种都按正常
    规则配一遍，取「判定权重最高」的那套。

    4 键一共 24 种列序，而配对本来就是逐列做的，所以先算 4×4 的两两权重，
    再让 24 种列序查表求和 —— 只有 16 次配对的开销。

    返回 (order, best, second)：
      order[谱面列] = 回放里的按键列；
      best / second 是按这套列序重算的 v1 准确率（0..1）里最好与次好的两个。
      两者差距很小，说明这张图的音符在时间上分不出列，推断结果不可当真。
      同分时优先取原序列（0,1,2,3），也就是"看不出被打乱就不动它"。
      超过 6 键不推断（列序数量会爆炸），直接按原序列返回。
    """
    keys = max(1, beatmap.keys)
    od_value = beatmap.od if od is None else od
    hit_window = judgement_windows(od_value)["meh"]
    notes_by_col, presses_by_col, _release_by_col, _ghost = _group_columns(
        beatmap, presses, releases, keys)
    total = sum(len(v) for v in notes_by_col)
    identity = tuple(range(keys))
    if not total or keys > 6:
        return list(identity), 0.0, 0.0

    weights = [[_column_weight(notes_by_col[c], presses_by_col[j], hit_window, od_value)
                for j in range(keys)] for c in range(keys)]
    ranked = sorted(
        ((sum(weights[c][perm[c]] for c in range(keys)), perm)
         for perm in itertools.permutations(range(keys))),
        key=lambda item: (-item[0], item[1] != identity),
    )
    best_score, best_perm = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0.0
    return list(best_perm), best_score / total, second_score / total


def _hold_judgement(head_delta: float | None, tail_delta: float | None,
                    windows: dict[str, float], body_break: bool = False) -> str:
    """长条的**一个**判定（ScoreV1 官方口径）。

    依据 osu! wiki「osu!mania judgement system」，核心是**合并误差**：
        combined = |头部误差| + |尾部误差|
    各档要求（倍率就是官方原文的 1.2/2.4、1.1/2.2、×2）：

    | 判定    | 头部误差 ≤        | 合并误差 ≤        |
    |---------|------------------|------------------|
    | PERFECT | PERFECT × 1.2    | PERFECT × 2.4    |
    | GREAT   | GREAT   × 1.1    | GREAT   × 2.2    |
    | GOOD    | GOOD             | GOOD    × 2      |
    | OK      | OK               | OK      × 2      |
    | MEH     | 其余非 MISS      |                  |

    MISS 的两种情形（wiki）：
      * 从「尾部 MEH 早窗口」到「OK 晚窗口」之间没按住键（提前松手且没按回来）；
      * 松手太晚（晚期 MEH 不可能，直接 MISS）。

    body_break：长条体中松过手。官方只把它封顶在 MEH，不直接算 MISS ——
    松手后按回来仍然能拿到 MEH，这一点以前的实现会误判成 MISS。
    """
    if head_delta is None or tail_delta is None:
        return "miss"
    head = abs(head_delta)
    combined = head + abs(tail_delta)

    meh = windows["meh"]
    ok = windows["ok"]
    if tail_delta < -meh or tail_delta > ok:
        return "miss"

    p = windows["perfect"]
    if head <= p * 1.2 and combined <= p * 2.4:
        result = "perfect"
    elif head <= windows["great"] * 1.1 and combined <= windows["great"] * 2.2:
        result = "great"
    elif head <= windows["good"] and combined <= windows["good"] * 2.0:
        result = "good"
    elif head <= ok and combined <= ok * 2.0:
        result = "ok"
    else:
        result = "meh"
    return "meh" if body_break and result != "miss" else result


_JUDGE_RANK = {"perfect": 5, "great": 4, "good": 3, "ok": 2, "meh": 1, "miss": 0}


def _worse(a: str, b: str) -> str:
    """两个判定里更差的那个。完美与 300 的 severity 相同，所以不能只看 SEVERITY。"""
    return b if _JUDGE_RANK.get(b, -1) < _JUDGE_RANK.get(a, -1) else a


def _apply_hold_tails(
    notes: list[JudgedNote],
    presses_by_col: list[list[float]],
    release_by_col: list[list[float]],
    windows: dict[str, float],
    windows_v2: dict[str, float],
) -> None:
    """给长条补上两套判定。

    尾部误差取「按下之后、下一次同列按键之前最后一次松手」：
      * 中途松手又按回来：最后一次松手才是真正的尾判，但整条要按 hold break 封顶 MEH；
      * 从体中松手后一直没按回来：尾判直接 MISS；
      * 松手太晚（超过尾部 OK 窗口）：MISS；
      * 一直按到回放结束、没有松手记录：按"按住到尾部"处理，尾判跟随头判。

    边界不能按时间窗口截断：这样"松手太晚"会因为没有候选而被当成"没松手"。
    正确的边界是「下一次同列按下」——它才意味着这条长条已经结束。
    体中按回来的那次按下不算，因为它发生在 end_time 之前。

    以前取的是第一次松手，中途松手会直接把整条算成 MISS，比官方口径严；
    而 ScoreV2 的尾判又直接拿合并判定顶替，等于没有实现头尾分判。
    """
    keys = len(release_by_col)
    for item in notes:
        if not item.is_hold or item.press_time is None or item.end_time is None:
            continue
        col = item.column
        if not (0 <= col < keys):
            continue
        rels = release_by_col[col]
        presses = presses_by_col[col] if col < len(presses_by_col) else []
        idx = bisect.bisect_left(rels, item.press_time)
        # 下一条同列物件（按下时间晚于本条尾部）才是边界；体中按回来不算
        next_idx = bisect.bisect_right(presses, item.end_time)
        limit = presses[next_idx] if next_idx < len(presses) else float("inf")
        after = [r for r in rels[idx:] if r < limit]

        item.head_judgement = (
            "miss" if item.delta is None else judge_with_windows(item.delta, windows_v2)
        )

        if not after:
            # 没有松手记录：按住到回放结束，尾判跟随头判
            item.release_delta = 0.0
            item.tail_judgement = item.head_judgement
            item.judgement_v2 = item.head_judgement
            item.judgement = _hold_judgement(item.delta, 0.0, windows)
            item.dropped = item.judgement == "miss"
            continue

        tail_rel = after[-1]
        # 体中松手：在尾部 MEH 早窗口之前松过（最后一次仍在窗口内才算"按回来了"）
        body_break = any(r < item.end_time - windows["meh"] for r in after[:-1])
        if tail_rel < item.end_time - windows["meh"]:
            body_break = True

        item.release_delta = tail_rel - item.end_time
        item.body_break = body_break

        # V1：合并口径；体中松手封顶 MEH
        item.judgement = _hold_judgement(item.delta, item.release_delta, windows, body_break)
        item.dropped = item.judgement == "miss"

        # V2：尾判窗口 1.5 倍；体中松手同样封顶 MEH
        tail = judge_with_windows(item.release_delta, windows_v2, 1.5)
        if body_break and tail != "miss":
            tail = "meh"
        item.tail_judgement = tail
        item.judgement_v2 = _worse(item.head_judgement, tail)


def match_replay(
    beatmap: Beatmap,
    presses: list[PressEvent],
    releases: list[ReleaseEvent],
    od: float | None = None,
    extra_window: float = 0.0,
) -> MatchResult:
    """执行配对。

    extra_window：可选的额外宽容（ms），用于"如果放宽一点会怎样"的对照实验。
    """
    od_value = beatmap.od if od is None else od
    windows = judgement_windows(od_value)
    windows_v2 = judgement_windows(od_value, score_v2=True)
    # 配对窗口 = 命中窗口（meh）。超过它官方就已经判漏，按键必须留给后面的物件。
    hit_window = windows["meh"] + max(0.0, extra_window)

    keys = max(1, beatmap.keys)
    notes_by_col, presses_by_col, release_by_col, ghosts = _group_columns(beatmap, presses, releases, keys)

    result: list[JudgedNote] = []
    for col in range(keys):
        _pair_column(col, notes_by_col[col], presses_by_col[col], hit_window, od_value, result, ghosts)

    _apply_hold_tails(result, presses_by_col, release_by_col, windows, windows_v2)

    result.sort(key=lambda x: (x.note_time, x.column))
    for idx, item in enumerate(result):
        item.index = idx
    ghosts.sort(key=lambda g: g.time)

    return MatchResult(
        notes=result,
        ghosts=ghosts,
        beatmap=beatmap,
        od=od_value,
        windows=windows,
    )


def _make_miss(note: HitObject, press_time: float | None = None, delta: float | None = None) -> JudgedNote:
    return JudgedNote(
        index=note.index,
        obj_index=note.index,
        column=note.column,
        note_time=note.time,
        is_hold=note.is_hold,
        end_time=note.end_time,
        judgement="miss",
        delta=delta,
        press_time=press_time,
    )


def _make_hit(note: HitObject, press_time: float, delta: float, judgement: str) -> JudgedNote:
    return JudgedNote(
        index=note.index,
        obj_index=note.index,
        column=note.column,
        note_time=note.time,
        is_hold=note.is_hold,
        end_time=note.end_time,
        judgement=judgement,
        delta=delta,
        press_time=press_time,
    )


# --------------------------------------------------------------------------- 自检


def compare_with_header(match: MatchResult, header: dict[str, int]) -> dict:
    """把重算的判定数与回放头对比（验证解析是否正确）。

    回放头口径：mania 下 count300 含 MAX，count100=good，count50=meh。
    """
    mine = match.counts()
    mine_300 = mine["perfect"] + mine["great"]
    mine_100 = mine["good"]
    mine_50 = mine["ok"] + mine["meh"]
    header_300 = header.get("great", 0) + header.get("perfect", 0)
    header_100 = header.get("good", 0)
    header_50 = header.get("meh", 0) + header.get("ok", 0)
    return {
        "mine": {"300": mine_300, "100": mine_100, "50": mine_50, "miss": mine["miss"]},
        "header": {"300": header_300, "100": header_100, "50": header_50, "miss": header.get("miss", 0)},
        "match_300": mine_300 == header_300,
        "match_100": mine_100 == header_100,
        "match_50": mine_50 == header_50,
        "match_miss": mine["miss"] == header.get("miss", 0),
        "total_mine": len(match.notes),
        "total_header": sum(header.get(k, 0) for k in ("perfect", "great", "good", "ok", "meh", "miss")),
    }
