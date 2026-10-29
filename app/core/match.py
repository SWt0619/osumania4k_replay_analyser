"""把回放和谱面配上，得出每一击的判定与误差。

这是整个分析的地基：所有误差、判定、失误都从这里来，所以规则要写清楚、可解释、可测试。

**主路径是逐帧状态机**（`match_replay_frames` → `mania_engine`）：回放本身就是一串
「时间 + 绝对按键掩码」，按帧推进、每列独立走状态机，和游戏里的结算顺序一致。
物件到期先于按键边沿、每列每帧只消费一个物件、长条松手当场结算——这三条决定了
面条图上判定能不能对上，全局配对做不到。

`match_replay` 保留的是另一套「先全局配对、再补尾判」的算法：它不依赖帧，只用
按下/松手事件，适合没有帧数据的手工样本与对照实验。两套算法给出的 `JudgedNote`
结构相同，上层（归因、时间轴、报告）不用区分。

长按的三套口径（都写在 `JudgedNote` 里）：
  * **ScoreV1**（stable）：整条只出一个判定，头尾合并误差按官方那套「头 ≤ 窗×1.2、
    合并 ≤ 窗×2.4」降档；体中松手封顶 200。
  * **ScoreV2**：头尾各判一次，尾窗口放大 1.5 倍，长按算两次判定；体中松手封顶 50。
  * **Classic**（lazer + CL）：stable 的窗口配 V2 的分判，只用于展示。
"""

from __future__ import annotations

import bisect
import itertools
import math
from dataclasses import dataclass, field

from . import mania_engine
from .beatmap import Beatmap, HitObject, judge_offset, judgement_windows, judge_with_windows
from .osr import PressEvent, ReleaseEvent

# ScoreV1 的判定权重：300/MAX 同权，200=2/3，100=1/3，50=1/6，MISS=0。
# 用精确分数而不是 0.667/0.333/0.167 —— 后者在大谱面上会累积出千分之几的偏差，
# 而官方公式就是 200/300、100/300、50/300。
V1_WEIGHTS = {"perfect": 1.0, "great": 1.0, "good": 2 / 3, "ok": 1 / 3, "meh": 1 / 6, "miss": 0.0}

# 长条体中的连击 tick 间隔（毫秒）：与 mania_engine 的模拟共用同一个值，
# 两处各写一个数迟早会分叉，连击和判定就对不上了。
LN_TICK_MS = mania_engine.LN_TICK_MS

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

# 长条尾判的时序：
#   "first" —— 官方 lazer DrawableHoldNote.OnReleased：第一次有效松手就结算尾判，
#              之后按回来不会重判（ScoreV2 / lazer 默认行为）。
#   "last"  —— 旧实现：取最后一次松手，体中松手后再按回可以拿到更好的尾判。
# 出问题需要回退时，把这里改成 "last" 即可，两套代码都保留。
HOLD_TAIL_POLICY = "first"


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
    # Classic（lazer + CL）：stable 的判定窗口 + V2 的头尾分判 / 305 计分
    head_judgement_classic: str = ""
    tail_judgement_classic: str | None = None
    judgement_classic: str = ""
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
        if not self.head_judgement_classic:
            self.head_judgement_classic = self.judgement
        if not self.judgement_classic:
            self.judgement_classic = self.judgement

    def judged(self, mode: str = "v1") -> str:
        """当前口径下的判定名。mode 只认 "v1" / "v2" / "classic"。"""
        if mode == "classic":
            return self.judgement_classic or self.judgement
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
    # 判定**产生**的顺序（判定名）。分数里的 bonus 是逐条累积的，同一时刻的和弦里
    # 谁先判会改变后面每一下的 bonus —— 按物件顺序算分，实测中位差 219 分；
    # 按这个顺序算，中位差 0。所以算分要用它，不要用 notes 的顺序。
    judge_order: list[str] = field(default_factory=list)

    # ---- 汇总 -----------------------------------------------------------
    def counts(self, mode: str = "v1") -> dict[str, int]:
        """判定数量。V1 按物件数，V2 / Classic 按判定单位数（长条算头尾两次）。"""
        out = {k: 0 for k in ("perfect", "great", "good", "ok", "meh", "miss")}
        if mode in ("v2", "classic"):
            for _t, j, _b in self.units(mode):
                out[j] = out.get(j, 0) + 1
            return out
        for n in self.notes:
            out[n.judgement] = out.get(n.judgement, 0) + 1
        return out

    def accuracy(self, mode: str = "v1") -> float:
        """加权准确率（百分比）。

        V1 用官方 ScoreV1 公式：
            (300*(MAX+300) + 200*GOOD + 100*OK + 50*MEH) / (300 * 总物件数)
        V2 / Classic 用官方 ScoreV2 口径：MAX 比 300 多 5 分，长条算头尾两个判定。
        """
        if mode in ("v2", "classic"):
            units = self.units(mode)
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

    def units(self, mode: str = "v2") -> list[tuple[float, str, bool]]:
        """ScoreV2 / Classic 的判定单位序列，按时间升序。

        每个元素 = (判定时刻, 判定名, 是否断连)。长按展开成头 + 尾两个单位；
        体中层松过手会把尾判封顶在 MEH，并且断连，但**不算 MISS**（lazer 的 hold break 口径）。
        Classic 只是把窗口换成 stable 的，结构仍是头尾分判。
        """
        units: list[tuple[float, str, bool]] = []
        classic = mode == "classic"
        for n in self.notes:
            head = (
                (n.head_judgement_classic or n.judgement)
                if classic
                else (n.head_judgement or n.judgement)
            )
            units.append((n.note_time, head, head == "miss"))
            if not n.is_hold:
                continue
            tail = (
                (n.tail_judgement_classic or head)
                if classic
                else (n.tail_judgement or head)
            )
            t = n.end_time if n.end_time is not None else n.note_time
            units.append((t, tail, n.body_break or tail == "miss"))
        units.sort(key=lambda u: u[0])
        return units

    def score_v2_parts(self, mode: str = "v2") -> dict:
        """ScoreV2 的两段（准度 / 连击比例），stable 与 lazer 两种配比共用。"""
        units = self.units(mode)
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

    def score_v2(self, lazer: bool = False, mode: str = "v2") -> tuple[int, str]:
        """ScoreV2 总分与评级。

        lazer=False（默认）：osu!stable 的 ScoreV2 —— 20 万连击 + 80 万准度。
        lazer=True：osu!lazer 的标准分 —— 1 万连击 + 99 万准度。
        两者共用同一套准确率与连击比例，只是配比不同。
        """
        p = self.score_v2_parts(mode)
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
        """最长连击。V1 按物件、V2 / Classic 按头尾两个单位。

        V2 / Classic 下体中层松手会断连（即使尾判是 MEH），所以不能只看判定名。

        长条**体中每 100ms 一个 tick 也计入连击**（官方 ManiaHoldNote 的 combo tick）：
        只算头尾的话，面条图的最大连击会比游戏少一两千——实测一张 96% 长条的图
        算 485、游戏是 708；补上 tick 之后 707。tick 从按下的位置起算，到松手或
        长条结尾为止，中途断过的长条不补（不知道断点在哪，宁少不多）。
        """
        events: list[tuple[float, bool]] = []   # (时刻, 是否续上连击)
        # 连击按**物件**算：一个长条是 1 个 combo（不是头尾两个），体中 tick 再逐个加。
        # lazer 的 ScoreV2 虽然把长条拆成两次判定，但连击仍然一次——实测 1122 物件
        # 508 长条的图，按头尾算会多出 500 多。
        for n in self.notes:
            if mode == "classic":
                judgement = n.judgement_classic
            elif mode == "v2":
                judgement = n.judgement_v2 or n.judgement
            else:
                judgement = n.judgement
            events.append((n.note_time, judgement != "miss"))

        for n in self.notes:
            if not n.is_hold or n.press_time is None or n.end_time is None:
                continue
            head = (n.head_judgement_classic or n.judgement) if mode == "classic" else (
                n.head_judgement or n.judgement)
            if head == "miss" or n.body_break:
                continue
            stop = n.end_time
            if n.release_delta is not None:
                stop = min(stop, n.end_time + n.release_delta)
            t = n.press_time + LN_TICK_MS
            while t < stop:
                events.append((t, True))
                t += LN_TICK_MS

        events.sort(key=lambda e: (e[0], e[1]))   # 同一时刻先断后连，宁可少算
        best = cur = 0
        for _t, ok in events:
            if not ok:
                cur = 0
                continue
            cur += 1
            best = max(best, cur)
        return best

    def deltas(self, mode: str = "v1") -> list[float]:
        """击打误差序列。V2 / Classic 把长条尾判的误差也算进来（lazer 的 UR 口径）。"""
        out = [n.delta for n in self.notes if n.delta is not None]
        if mode in ("v2", "classic"):
            classic = mode == "classic"
            out += [
                n.release_delta for n in self.notes
                if n.is_hold and n.release_delta is not None
                and (n.tail_judgement_classic if classic else n.tail_judgement) != "miss"
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
    windows_v2: dict[str, float],
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
            item = _make_hit(note, pt, delta, judgement)
            # 普通音符也要按 V2 自己的 PERFECT 窗口重判一次：OD 不等于 5 时，
            # V2 的 MAX 窗口和 stable 并不一样，直接复用 V1 判定会少算/多算 MAX。
            if not item.is_hold:
                j2 = judge_with_windows(delta, windows_v2)
                item.judgement_v2 = j2
                item.head_judgement = j2
            out.append(item)
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
                    windows: dict[str, float], body_break: bool = False,
                    algo: str = "A") -> str:
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

    body_break：长条体中松过手。官方把它封顶在 200（V1 分支），不直接算 MISS ——
    松手后按回来仍然能拿到 200，这一点以前的实现会误判成 MISS。

    algo 参数只为兼容旧的调用点保留：A/B/C/D 现在都走这一套（stable 口径）。
    """
    if head_delta is None or tail_delta is None:
        return "miss"
    head = abs(head_delta)
    combined = head + abs(tail_delta)

    meh = windows["meh"]
    ok = windows["ok"]
    # stable 会把"松手太晚"镜像回 end - (release - end) 再算误差，所以早松晚松用的是
    # 同一把尺子：超过 W50 才算 MISS。以前晚松只允许到 W100，104~130ms 的尾巴会被
    # 误判成漏接 —— 实测 28 份 stable 回放，漏接数因此多出 119 个。
    if tail_delta < -meh or tail_delta > meh:
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
    # 破 LN 在 V1 里封顶 200（ScoreV2 才封顶 50，见 _settle_hold 里的尾判）。
    if body_break and result != "miss":
        result = _worse(result, "good")
    return result


_JUDGE_RANK = {"perfect": 5, "great": 4, "good": 3, "ok": 2, "meh": 1, "miss": 0}


def _worse(a: str, b: str) -> str:
    """两个判定里更差的那个。完美与 300 的 severity 相同，所以不能只看 SEVERITY。"""
    return b if _JUDGE_RANK.get(b, -1) < _JUDGE_RANK.get(a, -1) else a


def _hold_without_release(item: JudgedNote, windows: dict[str, float],
                          windows_v2: dict[str, float], algo: str) -> None:
    """没有松手记录：按住到回放结束，尾判跟随头判。

    这是"回放到结尾还按着"的正常情况（长条没结束就存档），不是漏接。
    """
    item.release_delta = 0.0
    item.tail_judgement = item.head_judgement
    item.judgement_v2 = item.head_judgement
    item.head_judgement_classic = (
        "miss" if item.delta is None else judge_with_windows(item.delta, windows)
    )
    item.tail_judgement_classic = item.head_judgement_classic
    item.judgement_classic = item.head_judgement_classic
    item.judgement = _hold_judgement(item.delta, 0.0, windows, algo=algo)
    item.dropped = item.judgement == "miss"


def _hold_with_release(item: JudgedNote, tail_rel: float, body_break: bool,
                       windows: dict[str, float], windows_v2: dict[str, float],
                       algo: str) -> None:
    """有松手记录：V1 走合并口径，V2 / Classic 走头尾分判。"""
    # V1：合并口径；体中松手封顶 MEH
    item.judgement = _hold_judgement(item.delta, item.release_delta, windows, body_break, algo=algo)
    item.dropped = item.judgement == "miss"

    # V2：尾判窗口 1.5 倍；"first" 口径下按第一次松手判
    tail = judge_with_windows(tail_rel - item.end_time, windows_v2, 1.5)
    # 官方 DrawableHoldNoteTail.GetCappedResult：头没打中或体中断开过，
    # 尾判再准也最高只能到 MEH。面条图里这条很关键，否则断开的尾巴会被算成 300。
    if (item.head_judgement == "miss" or body_break) and tail != "miss":
        tail = "meh"
    item.tail_judgement = tail
    item.judgement_v2 = _worse(item.head_judgement, tail)

    # Classic：窗口用 stable 的（windows），头尾仍然分开判，尾窗口也放大 1.5 倍
    head_classic = (
        "miss" if item.delta is None else judge_with_windows(item.delta, windows)
    )
    tail_classic = judge_with_windows(tail_rel - item.end_time, windows, 1.5)
    if (head_classic == "miss" or body_break) and tail_classic != "miss":
        tail_classic = "meh"
    item.head_judgement_classic = head_classic
    item.tail_judgement_classic = tail_classic
    item.judgement_classic = _worse(head_classic, tail_classic)


def _apply_hold_tails(
    notes: list[JudgedNote],
    presses_by_col: list[list[float]],
    release_by_col: list[list[float]],
    windows: dict[str, float],
    windows_v2: dict[str, float],
    algo: str = "A",
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
            _hold_without_release(item, windows, windows_v2, algo)
            continue

        tail_rel = after[0] if HOLD_TAIL_POLICY == "first" else after[-1]
        # 体中松手：在尾部 MEH 早窗口之前松过。这个字段仍用于归因与连击
        body_break = any(r < item.end_time - windows["meh"] for r in after[:-1])
        if tail_rel < item.end_time - windows["meh"]:
            body_break = True
        # V1 合并判定用最后一次松手，release_delta 也留给页面显示
        item.release_delta = after[-1] - item.end_time
        item.body_break = body_break
        _hold_with_release(item, tail_rel, body_break, windows, windows_v2, algo)


def _settle_hold(obj: HitObject, press_time: float, head: str,
                 release_delta: float | None, windows: dict[str, float],
                 windows_v2: dict[str, float],
                 body_break: bool | None = None) -> JudgedNote:
    """长条松开时结算判定（C 算法用）。

    release_delta 为 None 表示回放里没有松手记录（一直按到结尾）：按 0 处理，
    但仍然是"尾判跟随头判"的意思，不是"偏了 0 毫秒"的巧合。

    体中层松过手会把尾判封顶在 MEH（对应官方 DrawableHoldNoteTail.GetCappedResult），
    并记 body_break；面条图里这条很关键，否则断开的尾巴会被算成 300。
    """
    if release_delta is None:
        release_delta = 0.0
    delta = press_time - obj.time
    tail = judge_with_windows(release_delta, windows_v2, 1.5)
    if body_break is None:
        body_break = release_delta < -windows["meh"]
    if (head == "miss" or body_break) and tail != "miss":
        tail = "meh"
    head_classic = judge_with_windows(delta, windows)
    tail_classic = judge_with_windows(release_delta, windows, 1.5)
    if (head_classic == "miss" or body_break) and tail_classic != "miss":
        tail_classic = "meh"
    item = JudgedNote(
        index=obj.index, obj_index=obj.index, column=obj.column, note_time=obj.time,
        is_hold=True, end_time=obj.end_time,
        judgement=_hold_judgement(delta, release_delta, windows, body_break),
        delta=delta, press_time=press_time,
        release_delta=release_delta,
        head_judgement=head, tail_judgement=tail,
        judgement_v2=_worse(head, tail),
        head_judgement_classic=head_classic,
        tail_judgement_classic=tail_classic,
        judgement_classic=_worse(head_classic, tail_classic),
        body_break=body_break,
    )
    item.dropped = item.judgement == "miss"
    return item


def _match_state_machine(
    beatmap: Beatmap,
    presses: list[PressEvent],
    releases: list[ReleaseEvent],
    od_value: float,
    windows: dict[str, float],
    windows_v2: dict[str, float],
    note_lock: bool = False,
) -> MatchResult:
    """C 算法：按列逐事件模拟 DrawableHoldNote 的按住状态。

    和 A 口径的差别在头判：A 先做全局贪心配对，再补尾判；这里按键事件来一个处理一个，
    当前按住的键只属于一个长条，松开就结算它的尾判。连续/重叠长条时松手归属更接近官方，
    但同帧多事件的先后顺序仍然还原不了（回放里没有这个信息）。

    note_lock：lazer 的 OrderedHitPolicy。一个音符只有在「下一个音符的起点还没到」时
    才可打——玩家在 A、B 之间按下时，只要还没越过 B 的起点就归 A（哪怕 A 的偏差已经
    一百多毫秒），越过了就顺延给 B，并把 A 强制判漏。stable 的事件机没有这层锁。
    """
    keys = max(1, beatmap.keys)
    keys = max(1, beatmap.keys)
    # 分组规则与 A 口径共用一份：各写一遍的话，「超出键数的按键算不算空敲」这种细节迟早分叉
    by_col, press_by_col, rel_by_col, ghosts = _group_columns(beatmap, presses, releases, keys)

    out: list[JudgedNote] = []
    for col in range(keys):
        items, extra = _match_column(by_col[col], press_by_col[col], rel_by_col[col], col,
                                     windows, windows_v2, note_lock)
        out.extend(items)
        ghosts.extend(extra)

    out.sort(key=lambda x: (x.note_time, x.column))
    for i, item in enumerate(out):
        item.index = i
    ghosts.sort(key=lambda g: g.time)
    return MatchResult(notes=out, ghosts=ghosts, beatmap=beatmap, od=od_value, windows=windows)


def _first_alive(objs: list[HitObject], alive: list[bool], start: int) -> int:
    """从 start 起第一个还没判定的物件下标（可能等于 len(objs)）。

    note lock 会跳过一些音符，所以"下一个要判的"不能直接用下标自增推。
    """
    j = start
    while j < len(objs) and not alive[j]:
        j += 1
    return j


def _pick_note_locked(objs: list[HitObject], alive: list[bool], idx: int,
                      press_time: float, windows: dict[str, float]
                      ) -> tuple[int, list[HitObject]]:
    """lazer 的 OrderedHitPolicy：这一次按下该落到哪个音符上。

    官方规则是「一个音符只有在下一个音符的起点还没到时才可打」：玩家在 A、B 之间
    按下时，只要还没越过 B 的起点就归 A（哪怕 A 的偏差已经一百多毫秒），越过了就
    顺延给 B，并把 A 强制判漏。stable 的事件机没有这层锁。

    返回（音符下标, 期间被强制判漏的物件）；下标 -1 表示这一击没有对应音符。
    这里会就地修改 alive，调用方负责把这些物件记成 miss。
    """
    j = _first_alive(objs, alive, idx)
    forced: list[HitObject] = []
    while j < len(objs):
        nxt = _first_alive(objs, alive, j + 1)
        if nxt < len(objs) and press_time >= objs[nxt].time:
            alive[j] = False
            forced.append(objs[j])
            j = nxt
            continue
        if abs(press_time - objs[j].time) > windows["meh"]:
            # 超出命中档但还在 Miss 窗内：官方 ResultFor 会返回 Miss 并**消费**这一击
            # （DrawableNote.CheckForResult 直接 ApplyResult(Miss)）。当成空敲留给
            # 下一次按键，会让这个音符被后面的按键「救回来」，那正是误差的来源。
            if abs(press_time - objs[j].time) <= windows["miss"]:
                return j, forced
            # 连 Miss 窗都过了：ResultFor 返回 None，官方返回 false 继续问下一个音符
            j = _first_alive(objs, alive, j + 1)
            continue
        return j, forced
    return -1, forced


def _match_column(
    objs: list[HitObject],
    press_times: list[float],
    release_times: list[float],
    col: int,
    windows: dict[str, float],
    windows_v2: dict[str, float],
    note_lock: bool,
) -> tuple[list[JudgedNote], list[GhostPress]]:
    """单列的事件流模拟，返回（这一列的判定, 这一列的空按）。

    列与列之间没有共享状态：长条只能被同一列的按键按住，跨列互不影响。
    拆成独立函数后，状态（idx / alive / active）的生命周期就限定在一次调用里。
    """
    objs = sorted(objs, key=lambda o: o.time)
    # 同一时刻先处理按下再处理松手：长条的开头总在结尾之前
    events = [(t, 0) for t in press_times] + [(t, 1) for t in release_times]
    events.sort(key=lambda e: (e[0], e[1]))

    out: list[JudgedNote] = []
    ghosts: list[GhostPress] = []
    idx = 0
    alive = [True] * len(objs)        # 还没产生判定；note lock 会跳过一些音符，不能只看 idx
    active: HitObject | None = None   # 当前按住的这条长条
    active_press = 0.0                # 它的按下时刻
    active_head = "miss"              # 它的头判（尾判要按头判封顶）

    def drain(until: float) -> None:
        """把 until 之前已经不可能再命中的物件记成漏接。

        用 meh 当界限是安全的：过了 meh 就超出命中窗口，之后无论怎么按都只能判漏，
        只是「判漏的时刻」被提前到这次事件，不影响最终判定。
        """
        nonlocal idx
        limit = until - windows["meh"]
        while idx < len(objs) and objs[idx].time < limit:
            if alive[idx]:
                alive[idx] = False
                out.append(_make_miss(objs[idx]))
            idx += 1
        idx = _first_alive(objs, alive, idx)

    for t, kind in events:
        drain(t)
        if kind == 0:
            # 新的一次按下：上一条还按着的长条到此为止（没有松手记录，按到结尾算）
            if active is not None:
                out.append(_settle_hold(active, active_press, active_head, None,
                                        windows, windows_v2))
                active = None
            if note_lock:
                pick, forced = _pick_note_locked(objs, alive, idx, t, windows)
                out.extend(_make_miss(o) for o in forced)
                if pick < 0:
                    ghosts.append(GhostPress(time=t, column=col))
                    continue
                k = idx
                while k < pick:
                    if alive[k]:
                        alive[k] = False
                        out.append(_make_miss(objs[k]))
                    k += 1
                obj = objs[pick]
                delta = t - obj.time
                alive[pick] = False
                idx = _first_alive(objs, alive, idx)
            else:
                if idx >= len(objs):
                    ghosts.append(GhostPress(time=t, column=col))
                    continue
                obj = objs[idx]
                delta = t - obj.time
                if abs(delta) > windows["meh"]:
                    ghosts.append(GhostPress(time=t, column=col))
                    continue
                alive[idx] = False
                idx = _first_alive(objs, alive, idx + 1)
            head = judge_with_windows(delta, windows_v2)
            if obj.is_hold and obj.end_time is not None:
                active, active_press, active_head = obj, t, head
            else:
                # 用传进来的 windows（可能已按 mod 缩放），不能重新按 OD 推一遍
                item = _make_hit(obj, t, delta, judge_with_windows(delta, windows))
                item.head_judgement = head
                item.judgement_v2 = head
                out.append(item)
        elif active is not None:
            out.append(_settle_hold(active, active_press, active_head,
                                    t - active.end_time, windows, windows_v2))
            active = None

    drain(float("inf"))
    if active is not None:
        out.append(_settle_hold(active, active_press, active_head, None,
                                windows, windows_v2))
    return out, ghosts


def _scaled_windows(windows: dict[str, float], mods: list[str] | None,
                    v2: bool = False) -> dict[str, float]:
    """按 stable 的顺序缩放判定窗：HR ÷1.4（否则 EZ ×1.4），DT/NC ×1.5（否则 HT ×0.75），
    最后向零截断。

    回放帧和谱面都在 audio clock 上，DT 下同样的手感误差在时间轴上会被放大 1.5 倍，
    所以窗口也得跟着放大。这里不缩放的话，DT/HT 的成绩会被系统性算严/算松。
    """
    mods = set(mods or [])
    scale = 1.0
    if "HR" in mods:
        scale /= 1.4
    elif "EZ" in mods:
        scale *= 1.4
    if "DT" in mods or "NC" in mods:
        scale *= 1.5
    elif "HT" in mods:
        scale *= 0.75
    if scale == 1.0:
        return windows
    out = {k: float(int(v * scale)) for k, v in windows.items()}
    if v2:
        # ScoreV2 的 EarlyInputWindow（「提前按能识别」的下界）是裸值，官方的
        # computeManiaWindowSet 只在另外五档上套 adjustManiaHitWindowFor。HR/DT 下
        # 缩了它会把「早按但还在窗口内」的音符判成空敲。
        out["miss"] = windows["miss"]
    return out


def match_replay_frames(
    beatmap: Beatmap,
    frames,
    mods: list[str] | None = None,
    column_map: list[int] | None = None,
    od: float | None = None,
) -> MatchResult:
    """逐帧状态机的配对入口（与 stable 同构，见 mania_engine）。

    为什么不用 presses/releases：判定依赖「这一帧按住了几列、上一帧是什么状态」，
    而 events 只有按下/松手两个时刻，还原不出「按住期间物件到期」这类分支。
    回放帧本身就是绝对掩码，直接喂给状态机最省事、也最接近游戏。

    跑两遍：V1 一遍、ScoreV2 一遍。两套判定窗只在 V2 的 PERFECT 与长条尾窗上不同，
    但状态机里「头判是否产生」「破 LN 封顶 50 还是 200」都跟着口径走，只能分开跑。
    """
    od_value = beatmap.od if od is None else od
    windows = _scaled_windows(judgement_windows(od_value), mods)
    windows_v2 = _scaled_windows(judgement_windows(od_value, score_v2=True), mods, v2=True)
    objs = [
        {"column": o.column, "time": o.time,
         "end": o.end_time if o.is_hold else o.time, "hold": o.is_hold}
        for o in beatmap.objects
    ]
    keys = max(1, beatmap.keys)
    sim_v1 = mania_engine.simulate(objs, frames, windows, score_v2=False,
                                   keys=keys, column_map=column_map)
    sim_v2 = mania_engine.simulate(objs, frames, windows_v2, score_v2=True,
                                   keys=keys, column_map=column_map)

    by_index_v2 = {n.index: n for n in sim_v2.notes}
    out: list[JudgedNote] = []
    for state in sim_v1.notes:
        obj = beatmap.objects[state.index]
        v2 = by_index_v2.get(state.index)
        # 头判：长条只有 ScoreV2 才真的产生头判（V1 是头尾合并），单点则直接取
        # V2 那一遍的结果。不能拿 V1 的 `state.judgement` 顶替——两套状态机不只是
        # 窗口不同，ScoreV2 还有「每帧每列预选」，同一击的归属都可能不一样。
        # 实测一张 ScoreV2 的图：V1 把 140520 那一下给了更早的长条，140590 的单点
        # 因此判漏；V2 按预选给了它、判 good，而这里若沿用 V1 就会把这一个报成 miss。
        if v2 and state.is_hold:
            head = v2.head_judgement or "miss"
        else:
            head = v2.judgement if v2 else state.judgement
        tail = v2.judgement if state.is_hold and v2 else None
        if state.is_hold and v2:
            judgement_v2 = _worse(head, tail or "miss")
        else:
            judgement_v2 = v2.judgement if v2 else state.judgement

        # Classic：stable 的窗口 + 头尾分判（我们自己的一种展示口径，不是官方模式）
        delta = state.delta
        release_delta = state.release_delta
        head_classic = "miss" if delta is None else judge_with_windows(delta, windows)
        tail_classic = None
        if state.is_hold:
            tail_classic = judge_with_windows(release_delta or 0.0, windows, 1.5)
            if (head_classic == "miss" or state.was_broken) and tail_classic != "miss":
                tail_classic = "meh"
            judgement_classic = _worse(head_classic, tail_classic)
        else:
            judgement_classic = head_classic

        item = JudgedNote(
            index=state.index, obj_index=state.index, column=obj.column,
            note_time=obj.time, is_hold=obj.is_hold, end_time=obj.end_time,
            judgement=state.judgement,
            delta=float(delta) if delta is not None else None,
            press_time=float(state.press_time) if state.press_recorded else None,
            release_delta=float(release_delta) if release_delta is not None else None,
            head_judgement=head,
            tail_judgement=tail,
            judgement_v2=judgement_v2,
            head_judgement_classic=head_classic,
            tail_judgement_classic=tail_classic,
            judgement_classic=judgement_classic,
            body_break=state.was_broken,
        )
        item.dropped = item.judgement == "miss"
        out.append(item)

    out.sort(key=lambda x: (x.note_time, x.column))
    for i, item in enumerate(out):
        item.index = i
    ghosts = [GhostPress(time=float(t), column=c) for t, c in sim_v1.ghosts]
    order = [sim_v1.notes[i].judgement for i in sim_v1.sequence]
    return MatchResult(notes=out, ghosts=ghosts, beatmap=beatmap, od=od_value,
                       windows=windows, judge_order=order)


def match_replay(
    beatmap: Beatmap,
    presses: list[PressEvent],
    releases: list[ReleaseEvent],
    od: float | None = None,
    extra_window: float = 0.0,
    algo: str = "A",
    mods: list[str] | None = None,
    windows: dict[str, float] | None = None,
    windows_v2: dict[str, float] | None = None,
    note_lock: bool = False,
) -> MatchResult:
    """执行配对。

    extra_window：可选的额外宽容（ms），用于"如果放宽一点会怎样"的对照实验。
    algo：C = 头判尾判都走事件流（服务端固定用这一条）；其余值走"A 口径"——
    先全局贪心配对、再补尾判，供测试与对照实验用。
    mods：回放里带的 mod 名（HR/EZ/DT/NC/HT），用来缩放判定窗。
    windows / windows_v2：直接指定判定窗（lazer 口径要用它绕开 stable 的表）。
    """
    od_value = beatmap.od if od is None else od
    if windows is None:
        windows = _scaled_windows(judgement_windows(od_value), mods)
    if windows_v2 is None:
        windows_v2 = _scaled_windows(judgement_windows(od_value, score_v2=True), mods, v2=True)
    if algo == "C":
        return _match_state_machine(beatmap, presses, releases, od_value, windows, windows_v2,
                                    note_lock=note_lock)
    # 配对窗口 = 命中窗口（meh）。超过它官方就已经判漏，按键必须留给后面的物件。
    hit_window = windows["meh"] + max(0.0, extra_window)

    keys = max(1, beatmap.keys)
    notes_by_col, presses_by_col, release_by_col, ghosts = _group_columns(beatmap, presses, releases, keys)

    result: list[JudgedNote] = []
    for col in range(keys):
        _pair_column(col, notes_by_col[col], presses_by_col[col], hit_window, od_value,
                     windows_v2, result, ghosts)

    _apply_hold_tails(result, presses_by_col, release_by_col, windows, windows_v2, algo=algo)

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
