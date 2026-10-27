"""逐帧重演 stable 的 mania 判定状态机。

为什么需要它：原先的配对器是「先把按键和音符全局配一遍、再补长条尾判」，而 stable
是**每帧推进一次**的状态机——每列每帧最多消费一个物件、物件到期先于按键边沿、长条
松手当场结算。两套模型在面条图上差得很远：同样是「松手早了 3ms」，状态机记的是
「break，之后再按回来还能结算」，全局配对记的是「尾判 miss」，一个物件差一级就会
连带后面的按键全部错位（实测一张 2057 根长条的图，漏接数多出 38 个）。

规则按 osu!stable 的反编译结果还原，逐帧对齐的要点：

  * 每帧 = 一次 tick：先把这一帧的绝对按键掩码写进各列，再按 StartTime 升序扫一遍
    活动物件，逐个走状态机。
  * 物件到期（`now >= endTime + W100`）先于按键边沿判定；到期即判 miss 或结算尾判。
  * 每列每帧只消费一个物件（`consumed`），同一帧里第二个物件要等下一帧。
  * 长条在体中松手只记 break（不写判定），之后再按回来仍可结算；提前松到
    `endTime - W50` 之外最终是 miss。
  * 回放结束后按到期时间逐个补判，剩下的直接判 miss。

对外只暴露 `simulate()`：给它谱面物件、回放帧、判定窗，它返回每个物件的判定与
按下/松手时刻。V1 与 ScoreV2 两条分支都在这里，区别只在长条。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

# 判定名：顺序就是 stable 的档位高低，越靠前越好
GRADES = ("perfect", "great", "good", "ok", "meh", "miss")
_RANK = {name: i for i, name in enumerate(GRADES)}

# 长条体中每 100ms 一个 tick（值 7）。只加连击、不加判定、不加分
LN_TICK_MS = 100


def _worse(a: str, b: str) -> str:
    return a if _RANK.get(a, 9) >= _RANK.get(b, 9) else b


@dataclass
class NoteState:
    """一个物件在状态机里的全部状态。字段名对齐 stable 的 ManiaNote / ManiaLongNote。"""

    index: int                      # 谱面里的原始序号
    column: int
    start: int
    end: int
    is_hold: bool
    is_judged: bool = False
    judgement: str = ""             # 空串 = 还没产生判定（stable 里是 0）
    is_held: bool = False
    press_time: int = 0
    press_recorded: bool = False
    release_time: int = 0
    release_recorded: bool = False
    head_judged: bool = False
    head_judgement: str = ""        # 只有 ScoreV2 会真正产生头判
    was_broken: bool = False
    is_dimmed: bool = False
    last_tick: int = -1
    ticks: int = 0

    @property
    def delta(self) -> int | None:
        return self.press_time - self.start if self.press_recorded else None

    @property
    def release_delta(self) -> int | None:
        return self.release_time - self.end if self.release_recorded else None


@dataclass
class Simulation:
    notes: list[NoteState]
    ghosts: list[tuple[int, int]] = field(default_factory=list)   # (时间, 列)
    windows: dict[str, float] = field(default_factory=dict)
    # 判定**产生**的顺序（物件下标）。分数里的 bonus 是逐个累积的，
    # 同一时刻的和弦里谁先判会改变后续每一下的 bonus，必须按状态机的顺序算分。
    sequence: list[int] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {name: 0 for name in GRADES}
        for n in self.notes:
            out[n.judgement or "miss"] += 1
        return out


def _grade_by(err: float, windows: dict[str, float]) -> str:
    """普通音符的档位：|误差| 依次对 perfect / great / good / ok / meh 窗口。"""
    err = abs(float(err))
    for name in GRADES[:-1]:
        if err <= windows[name]:
            return name
    return "miss"


class _Engine:
    def __init__(self, objects, frames, windows, score_v2=False, keys=4, column_map=None):
        self.win = windows
        self.v2 = score_v2
        self.keys = keys
        # Random / Mirror：回放里的按键列号不是谱面列号，按映射翻回来再喂状态机。
        # column_map[谱面列] = 回放列；None 表示两者一致。
        self.column_map = list(column_map) if column_map else None
        self.w50 = float(windows["meh"])
        self.w100 = float(windows["ok"])
        self.early = float(windows.get("miss") or windows["meh"])   # 早按窗口（188-3OD）
        # 帧必须按时间推进：状态机靠「上一帧按没按」判断边沿，乱序会一路错位。
        # 正常回放本来就是有序的，这里只是兜底（手工构造的样本未必排过序）。
        self.frames = sorted(frames, key=lambda f: f[0])
        self.ghosts: list[tuple[int, int]] = []
        self.sequence: list[int] = []

        # stable 按 StartTime 有序插入，同一个 StartTime 的后来者排在后面
        ordered = sorted(
            (
                NoteState(
                    index=i, column=int(o["column"]), start=int(round(o["time"])),
                    end=int(round(o["end"] if o["end"] is not None else o["time"])),
                    is_hold=bool(o["hold"]),
                )
                for i, o in enumerate(objects)
            ),
            key=lambda n: n.start,
        )
        self.notes = ordered
        self.by_col: list[list[NoteState]] = [[] for _ in range(keys)]
        for n in ordered:
            if 0 <= n.column < keys:
                self.by_col[n.column].append(n)
        self.starts: list[list[int]] = [[n.start for n in col] for col in self.by_col]
        self.cursor = [0] * keys
        self.held = [False] * keys
        self.was_held = [False] * keys
        # 该列最近一次「已判定」物件的 StartTime。官方每帧重扫 activeObjects：
        # 只要该列有一个已判物件还停在未来（StartTime > now），整列就 consumed，
        # 这一帧的按键谁也判定不了。ours 用 cursor 线性推进，越过之后再也见不到它，
        # 于是提前按下的那一下会把同一列更靠后的音符判死（实测一段密集连打里
        # 连锁错位 6 个音符，漏接 4 个、完美少 3 个）。
        self.last_judged_start = [-10 ** 9] * keys

    # ---- 判定 -----------------------------------------------------------
    def _judge_rice(self, note: NoteState) -> None:
        """ManiaNote::judge —— 只有「判定时还按着」才按误差分档，否则 miss。"""
        if note.is_judged:
            return
        note.is_judged = True
        self.last_judged_start[note.column] = max(
            self.last_judged_start[note.column], note.start)
        note.judgement = _grade_by(note.press_time - note.start, self.win) if note.is_held else "miss"
        self.sequence.append(note.index)

    def _judge_hold_head(self, note: NoteState) -> None:
        """ManiaLongNote::judgeLongNoteHead —— 非 ScoreV2 不产生判定（V1 头尾合并）。"""
        if note.is_held:
            note.last_tick = note.press_time
        if self.v2 and not note.was_broken and not note.head_judged:
            note.head_judgement = (
                _grade_by(note.press_time - note.start, self.win) if note.is_held else "miss"
            )
        note.head_judged = True

    def _dim_check(self, note: NoteState, now: int) -> None:
        """变暗是视觉效果，但会挡住后续的 judge 调用，所以必须一起模拟。"""
        if note.judgement == "miss":
            dim = True
        elif note.is_held:
            dim = False
        elif now <= note.start + self.w50:
            dim = False
        else:
            dim = not note.is_judged
        note.is_dimmed = dim

    def _judge_hold(self, note: NoteState, now: int) -> str:
        """ManiaLongNote::judge —— 返回 "judged" / "break" / "wait"。"""
        note.last_tick = -1
        self._dim_check(note, now)
        if note.is_judged:
            return "judged"

        # 守卫 1：还没到 StartTime + W50 且从未按下 ⇒ 不判
        if now < note.start + self.w50 and not note.press_recorded:
            return "wait"
        # 守卫 2：提前松手 ⇒ 记 break，不写判定（可以重复触发）
        if now < note.end - self.w50:
            if note.is_held:
                return "wait"
            note.was_broken = True
            return "break"
        # 守卫 3：还按着、也没到 EndTime + W50 ⇒ 继续等
        if now < note.end + self.w50 and note.is_held:
            return "wait"

        note.is_judged = True
        self.last_judged_start[note.column] = max(
            self.last_judged_start[note.column], note.start)
        self.sequence.append(note.index)
        if self.v2:
            note.judgement = _grade_by((note.release_time - note.end) / 1.5, self.win)
            if note.judgement != "miss" and (note.was_broken or not note.head_judged):
                note.judgement = _worse(note.judgement, "meh")
        else:
            note.judgement = self._v1_hold_grade(note, now)
        return "judged"

    def _v1_hold_grade(self, note: NoteState, now: int) -> str:
        """V1 的合并口径：头尾误差各自镜像/折叠后再按「头 ≤ 窗×1.2、合并 ≤ 窗×2.4」降档。

        effStart 把「提前按下的量」镜像到尾部，effEnd 把「晚松的量」镜像回尾部，
        所以同一个方向的偏差不会被算两遍 —— 这就是官方那句「整体误差」的来历。
        """
        press = note.press_time
        if press < note.start - self.w50:
            eff_start = note.end - 1
        elif press < note.start:
            eff_start = note.start + (note.start - press)
        else:
            eff_start = press

        if note.is_held:
            eff_end = now
        elif note.release_time > note.end:
            eff_end = note.end - (note.release_time - note.end)
        else:
            eff_end = note.release_time

        head_err = abs(eff_start - note.start)
        tail_err = abs(eff_end - note.end)
        combined = head_err + tail_err

        if eff_end < note.end - self.w50:
            grade = "miss"
        else:
            grade = "meh"
            for name, head_mul, comb_mul in (
                ("perfect", 1.2, 2.4), ("great", 1.1, 2.2),
                ("good", 1.0, 2.0), ("ok", 1.0, 2.0),
            ):
                if head_err <= self.win[name] * head_mul and combined <= self.win[name] * comb_mul:
                    grade = name
                    break
        # 破 LN 封顶 200（ScoreV2 才封 50）
        if note.was_broken and _RANK[grade] < _RANK["good"]:
            grade = "good"
        return grade

    def _expire(self, note: NoteState, now: int) -> bool:
        """到期：`now >= endTime + W100`（ScoreV2 的长条 W100 ×1.5）。到点直接判。

        单点这里用 100 窗口而不是 50：实测把界限放到 50 窗口（晚 20 多毫秒仍可打）
        会让一张 5080 物件的图多出 13 个 meh（20 vs 6），反而更远。stable 在
        100 窗口就把「已经打不中高分」的音符结算掉了。
        """
        w100 = self.w100
        if self.v2 and note.is_hold:
            w100 *= 1.5
            if now >= note.start + self.w100 and not note.head_judged:
                self._judge_hold_head(note)
        if float(now) < float(note.end) + w100:
            return False
        if not note.is_judged:
            if note.is_hold:
                self._judge_hold(note, now)
            else:
                self._judge_rice(note)
            return True
        return False

    def _preselect(self, now: int) -> list:
        """ScoreV2 的「每列预选」：这一帧的按下只归选中那个物件，别的都不吃。

        为什么需要它：V1 靠 consumed 让「一列一帧一个物件」自然成立，ScoreV2 换成
        了显式选择。官方的做法是从 activeObjects（EndTime 落在
        `[now - ApproachTime - 200, now + 300]` 的切片）**逆序**扫——也就是从最晚的
        音符往回——每列只决定一次：

          * 遇到已判物件就定案（该列本帧不再考虑）；
          * 还没到它起点（now < Start）就一直往前换成更早的那个，玩家提前按应给最近
            的「尚未到点」音符；
          * 一旦 now 越过了某个音符的起点，就用 limit 判断要不要把这一击给它：
            `limit = min(Start + W50, 更晚者.Start - 1) + max(0, 更晚者.Start - Start - 2*W50)`

        差这一次预选，后果不只是「按给谁」：140520 那一击在官方眼里归了 140590 的
        单点（limit 140462 < now，更早的长条 139999 被排除），长条因此从没被重新按住，
        140328 提前松手的 releaseTime 保留下来，到期结算时按 -201ms 判漏。我们原来
        把这一击给了长条、把它重新按住，尾判就成了 meh（实测整张图唯一一处差异）。

        下界用 EndTime 而不是 StartTime（官方的 rangeStartForEndTimeKey 就是按 EndTime
        索引的），这里放宽到 4 秒足够：更早的未判物件只可能是长条，而它们要么已到期
        被 `_expire` 判掉，要么本来就该参与。
        """
        sel: list[NoteState | None] = [None] * self.keys
        w50 = self.w50
        for c in range(self.keys):
            objs = self.by_col[c]
            starts = self.starts[c]
            hi = bisect.bisect_right(starts, now + 300) - 1
            cur: NoteState | None = None
            for j in range(hi, -1, -1):
                note = objs[j]
                if note.end < now - 4000:
                    break
                if note.is_judged:
                    break            # 官方 decided=1：更早的物件本帧不再考虑
                if cur is None or now < note.start:
                    cur = note
                    continue
                limit = min(note.start + w50, cur.start - 1)
                limit += max(0, cur.start - note.start - w50 - w50)
                if now <= limit:
                    cur = note
                break
            sel[c] = cur
        return sel

    def _tick_note(self, note: NoteState, now: int, col: int, consumed: list[bool],
                   sel: NoteState | None = None) -> None:
        if consumed[col]:
            return
        if now < note.start - self.early:
            return
        if self._expire(note, now):
            return

        held, was = self.held[col], self.was_held[col]
        if held and not was:
            # ScoreV2 只认预选出来的那一个：这一击没选中它就不能按（官方的
            # `if (scoreV2 && selected[col] != note) continue;`）。
            if self.v2 and sel is not note:
                return
            if not note.is_hold:
                note.is_held = True
                note.press_time = now
                note.press_recorded = True
                consumed[col] = True
                self._judge_rice(note)
                return
            if note.is_held:
                return
            note.press_time = now
            note.press_recorded = True
            consumed[col] = True
            note.is_held = True
            self._judge_hold_head(note)
            return

        if held and was:
            if not note.is_hold or not note.is_held:
                return
            consumed[col] = True
            if note.last_tick != -1 and note.start <= now <= note.end:
                if now - note.last_tick >= LN_TICK_MS:
                    note.last_tick += LN_TICK_MS
                    note.ticks += 1
            return

        # 已松开
        if held:
            return
        if not note.is_hold:
            return
        if not note.is_held:
            if not note.is_dimmed:
                self._judge_hold(note, now)
            return
        note.is_held = False
        note.release_time = now
        note.release_recorded = True
        self._judge_hold(note, now)

    def _tick_column(self, col: int, now: int, consumed: list[bool],
                     sel: NoteState | None = None) -> None:
        """推进一列。和官方一样**不因单个物件停住**：到期但还按着的长条这一帧不判，
        要跳过它去看后面的物件（以前这里 break，cursor 就卡在那个长条上，
        直到它松手，期间同一列的按键全部错过——实测一段连打漏判 3 个音符）。
        """
        objs = self.by_col[col]
        cur = self.cursor[col]
        # 官方 (A)：已判物件若 StartTime > now，封锁整列。非 ScoreV2 才有这层。
        if not self.v2 and self.last_judged_start[col] > now:
            consumed[col] = True
            return
        j = cur
        while j < len(objs):
            note = objs[j]
            if note.is_judged:
                # 未来时间的已判物件封锁该列（stable 的 consumed 分支）
                if not self.v2 and note.start > now:
                    consumed[col] = True
                j += 1
                continue
            if consumed[col]:
                break
            if now < note.start - self.early:
                break          # 后面的起点更晚，这一帧都不用看
            self._tick_note(note, now, col, consumed, sel)
            j += 1             # 判没判都往后看：没判只是「还按着，等下一帧」
        # cursor 只推进到第一个还没判的物件
        while cur < len(objs) and objs[cur].is_judged:
            cur += 1
        self.cursor[col] = cur

    def _settle(self) -> None:
        """回放结束后按到期时间逐个补判，直到没有待判物件。"""
        for _ in range(1 << 20):
            pending = [n for n in self.notes if not n.is_judged]
            if not pending:
                return
            n = min(pending, key=lambda x: x.end + (self.w100 * 1.5 if self.v2 and x.is_hold else self.w100))
            when = int(n.end + (self.w100 * 1.5 if self.v2 and n.is_hold else self.w100))
            when = max(when, n.end + int(self.w50) + 1, n.start + int(self.w50) + 1)
            # 结算时列状态保持原样（官方的 resyncColumns 只做 wasHeld = isHeld）：
            # 把它当成「松手」会把 releaseTime 改成本次结算时刻，V2 的尾判就凭空
            # 变好一档——实测一根 140328 提前松手、之后再没按回来的长条，
            # 官方按 -201ms 判漏，我们因为这一行算成了 meh。
            for c in range(self.keys):
                self.was_held[c] = self.held[c]
            before = n.is_judged
            if n.is_hold:
                self._judge_hold(n, when)
            else:
                self._judge_rice(n)
            if n.is_judged == before:
                n.is_judged = True
                n.judgement = "miss"

    def run(self) -> Simulation:
        keys = self.keys
        cmap = self.column_map
        prev_extra = 0
        for time_ms, mask in self.frames:
            now = int(time_ms)
            bits = int(mask)
            for c in range(keys):
                self.was_held[c] = self.held[c]
                src = cmap[c] if cmap else c
                self.held[c] = bool(bits & (1 << src))
            consumed = [False] * keys
            # ScoreV2 的按下归属要在本帧开始前一次性定好（官方 updateInputJudgments
            # 的前半段），判定过程中物件状态会变，边判边选就对不上了。
            selected = self._preselect(now) if self.v2 else None
            for c in range(keys):
                self._tick_column(c, now, consumed, selected[c] if selected else None)
            # 空敲：这一帧按下去了，但没有任何物件被这次按下消费
            for c in range(keys):
                if self.held[c] and not self.was_held[c] and not consumed[c]:
                    self.ghosts.append((now, c))
            # 超出谱面键数的按键：状态机里没有这一列，但玩家确实按了，算空敲
            extra = bits & ~((1 << keys) - 1)
            for c in range(keys, bits.bit_length() + 1):
                if extra & (1 << c) and not (prev_extra & (1 << c)):
                    self.ghosts.append((now, c))
            prev_extra = extra
        self._settle()
        for n in self.notes:
            if not n.judgement:
                n.judgement = "miss"
                self.sequence.append(n.index)
        return Simulation(self.notes, self.ghosts, self.win, self.sequence)


def simulate(objects, frames, windows, score_v2: bool = False, keys: int = 4,
             column_map=None) -> Simulation:
    """跑一遍状态机。

    objects：可迭代的 dict，字段 column / time / end / hold（end 对普通音符等于 time）。
    frames ：(时间, 绝对按键掩码) 序列，与 .osr 里的帧一一对应。
    windows：判定窗（perfect/great/good/ok/meh/miss），整数毫秒。
    column_map：Random / Mirror 下的「谱面列 → 回放列」映射；不传则认为一致。
    """
    return _Engine(objects, frames, windows, score_v2=score_v2, keys=keys,
                   column_map=column_map).run()
