"""lazer 的 mania 判定状态机（按上游 DrawableNote / DrawableHoldNote 重建）。

为什么单开一份而不是复用 stable 的 `mania_engine`：两边的判定对象不是一个粒度。
stable 的长条是「一个物件一份判定」，头尾误差合并后再降档；lazer 拆成头、尾、
body 三个可判定单元，尾判在**最后一次松手**时结算，body 断只影响连击与封顶。
混用会把「中途抖一下手指」当成提前松手——实测一张 3108 根长条的图因此少算
14 个 100、多算 12 个 50。

规则来源（逐条对应上游）：

  * `HitWindows.ResultFor`：|offset| 自 perfect 向 miss 扫，闭区间；超出 miss 返回 None
    （这一次不受理，按键落到下一个物件）。
  * `OrderedHitPolicy.IsHittable`：下一件物件的 StartTime 一到，本物件就锁死
    （`t >= next.start` 时跳过）。
  * `DrawableHoldNote.OnPressed`：只要没到 EndTime，press 就把 `holding` 直接置真
    —— **头判过不过都置**，所以「松手又按回来」能继续把这个尾巴按完。
  * `DrawableHoldNoteTail.CheckForResult`：`(t - end) / 1.5` 再查窗口，超窗不判，
    等 update tick 判 Miss。
  * `GetCappedResult`：结果好于 Meh 且（头没打中 或 body 断）时压到 Meh。
  * update tick：rice/头 = `start + meh` 截止，尾 = `end + 1.5 × meh` 截止。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .match import GhostPress, JudgedNote, MatchResult

GRADES = ("perfect", "great", "good", "ok", "meh")
_BETTER_THAN_MEH = ("perfect", "great", "good", "ok")


def _is_hit(name: str | None) -> bool:
    return name is not None and name != "miss"


@dataclass
class _Unit:
    index: int                 # 谱面里的原始序号
    column: int
    start: int
    end: int
    is_hold: bool
    next_idx: int = -1         # 同列下一件（note lock 用），-1 = 没有
    head_judged: bool = False
    tail_judged: bool = False
    body_judged: bool = False
    head_res: str | None = None
    tail_res: str | None = None
    body_hit: bool = False
    holding: bool = False
    # 画面上的「实际按下 / 实际松手」位置只认真实输入：到期补判出来的漏接没有这一刻，
    # 留 None。以前这里存的是判定时刻，漏接会在 +130ms 左右凭空长出一根按键线，
    # 还会被算进整局偏移和 UR。
    press_t: int | None = None
    rel_t: int | None = None
    released_early: bool = False
    marked: bool = False       # 父物件记过号（IgnoreMiss）

    def all_judged(self) -> bool:
        return self.head_judged and self.tail_judged and self.body_judged


@dataclass
class _Out:
    notes: dict[int, JudgedNote] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    ghosts: list[GhostPress] = field(default_factory=list)


class _Lazer:
    def __init__(self, beatmap, frames, windows, column_map=None, keys=None):
        self.w = windows
        self.keys = int(keys or beatmap.keys or 4)
        self.column_map = list(column_map) if column_map else None
        self.frames = sorted(frames, key=lambda f: f[0])
        self.out = _Out()

        units = []
        for i, o in enumerate(beatmap.objects):
            is_hold = bool(o.is_hold and o.end_time is not None)
            units.append(_Unit(
                index=i, column=int(o.column), start=int(round(o.time)),
                end=int(round(o.end_time)) if is_hold else int(round(o.time)),
                is_hold=is_hold,
            ))
        self.units = units
        self.columns: list[list[int]] = [[] for _ in range(self.keys)]
        for i, u in enumerate(units):
            if 0 <= u.column < self.keys:
                self.columns[u.column].append(i)
        for col in self.columns:
            col.sort(key=lambda i: (self.units[i].start, i))
            for a, b in zip(col, col[1:]):
                self.units[a].next_idx = b

        # 待判集合：update tick 只扫这些，不能每帧遍历全部物件
        # （8000 物件 × 5 万帧 = 4 亿次，实测单张图要 38 秒）。
        # 到期检查走「按时间排好的指针」而不是每帧遍历全部物件：
        # 8000 物件 × 5 万帧 = 4 亿次，直接遍历单张图要 38 秒。
        self._head_order = sorted(range(len(units)), key=lambda i: units[i].start)
        self._head_ptr = 0
        self._tail_order = sorted((i for i, u in enumerate(units) if u.is_hold),
                                  key=lambda i: units[i].end)
        self._tail_ptr = 0
        self._col_cursor = [0] * self.keys

        # 判定结果按物件收口，最后统一转成 JudgedNote
        self._head: dict[int, str] = {}
        self._tail: dict[int, str] = {}

    # ---- 判定出口 ---------------------------------------------------------
    def _grade(self, offset: float) -> str | None:
        a = abs(float(offset))
        for name in GRADES:
            if a <= self.w[name]:
                return name
        if a <= self.w.get("miss", 0.0):
            return "miss"
        return None

    def _judge_head(self, u: _Unit, res: str) -> None:
        u.head_judged = True
        u.head_res = res
        self._head[u.index] = res
        self.out.order.append(res)

    def _judge_tail(self, u: _Unit, res: str) -> None:
        u.tail_judged = True
        u.tail_res = res
        self._tail[u.index] = res
        self.out.order.append(res)

    def _cap_tail(self, u: _Unit, res: str) -> str:
        """好于 Meh 且（头没打中 或 body 断）⇒ 压到 Meh；Miss 不封顶。"""
        if res not in _BETTER_THAN_MEH:
            return res
        head_hit = u.head_judged and _is_hit(u.head_res)
        body_break = u.body_judged and not u.body_hit
        if not head_hit or body_break:
            return "meh"
        return res

    def _handle_hit(self, col: int, target: int) -> None:
        """Column.OnNewResult：把同列 end 早于 target 的未判物件强制判漏。

        target 随判定单调递增，所以每列用一个游标扫过就行——不加游标时每次
        判定都从列头重扫，密集图上是 O(n²)。
        """
        col_list = self.columns[col]
        k = self._col_cursor[col]
        while k < len(col_list):
            u = self.units[col_list[k]]
            obj_end = u.end if u.is_hold else u.start
            if obj_end >= target:
                break
            k += 1
            if not u.is_hold:
                if not u.head_judged:
                    self._judge_head(u, "miss")
                continue
            if not u.marked:
                u.marked = True
                u.holding = False
            if u.start < target:
                if not u.head_judged:
                    self._judge_head(u, "miss")
                if u.end < target:
                    if not u.tail_judged:
                        self._judge_tail(u, "miss")
                    if not u.body_judged:
                        u.body_judged = True
                        u.body_hit = False
        self._col_cursor[col] = k

    # ---- 输入 -------------------------------------------------------------
    def _offer_press(self, col: int, t: int) -> None:
        for idx in self.columns[col]:
            u = self.units[idx]
            if u.all_judged() if u.is_hold else u.head_judged:
                continue
            if u.next_idx >= 0 and t >= self.units[u.next_idx].start:
                continue                      # note lock
            if u.is_hold:
                if t > u.end and (t - u.end) > self.w["meh"]:
                    continue                  # 过了尾巴还在 Meh 窗外
                if (t - u.start) >= -self.w.get("miss", 0.0):
                    u.holding = True          # 头判过不过都置：松手后按回来能续
                    # 头判可能是 miss（超出窗口但那一下确实按了），位置仍然按真实按下记
                    if u.press_t is None:
                        u.press_t = t
                if not u.head_judged:
                    res = self._grade(t - u.start)
                    if res is not None:
                        self._judge_head(u, res)
                        if _is_hit(res):
                            self._handle_hit(col, u.start)
                        return
                continue                      # 这一击没受理，落给下一个物件
            res = self._grade(t - u.start)
            if res is None:
                continue
            u.press_t = t
            self._judge_head(u, res)
            if _is_hit(res):
                self._handle_hit(col, u.start)
            return
        self.out.ghosts.append(GhostPress(time=float(t), column=col))

    def _offer_release(self, col: int, t: int) -> None:
        for idx in self.columns[col]:
            u = self.units[idx]
            if not u.is_hold or u.all_judged() or not u.holding:
                continue
            u.rel_t = t                       # 真实松手帧：尾判成 miss 也要留位置
            if t < u.end:
                u.released_early = True
            if not u.tail_judged:
                res = self._grade((t - u.end) / 1.5)
                if res is not None:
                    capped = self._cap_tail(u, res)
                    self._judge_tail(u, capped)
                    if _is_hit(capped):
                        self._handle_hit(col, u.end)
            if not u.body_judged:
                u.body_judged = True
                u.body_hit = u.tail_judged and _is_hit(u.tail_res)
            u.holding = False

    def _update_pass(self, t: int) -> None:
        limit = self.w["meh"]
        order = self._head_order
        while self._head_ptr < len(order):
            u = self.units[order[self._head_ptr]]
            if t - u.start <= limit:
                break
            if not u.head_judged:
                self._judge_head(u, "miss")
            self._head_ptr += 1
        limit = 1.5 * self.w["meh"]
        order = self._tail_order
        while self._tail_ptr < len(order):
            u = self.units[order[self._tail_ptr]]
            if t - u.end <= limit:
                break
            if not u.tail_judged:
                self._judge_tail(u, "miss")
                if not u.body_judged:
                    u.body_judged = True
                    u.body_hit = False
            self._tail_ptr += 1

    # ---- 主循环 -----------------------------------------------------------
    def run(self) -> MatchResult:
        prev = 0
        for time_ms, mask in self.frames:
            t = int(time_ms)
            bits = int(mask)
            down = bits & ~prev
            up = ~bits & prev
            for c in range(self.keys):
                src = self.column_map[c] if self.column_map else c
                if down & (1 << src):
                    self._offer_press(c, t)
            for c in range(self.keys):
                src = self.column_map[c] if self.column_map else c
                if up & (1 << src):
                    self._offer_release(c, t)
            self._update_pass(t)
            prev = bits
        # 回放结束还挂着的物件：补漏
        for u in self.units:
            if not u.head_judged:
                self._judge_head(u, "miss")
            if u.is_hold and not u.tail_judged:
                self._judge_tail(u, "miss")
                if not u.body_judged:
                    u.body_judged = True
                    u.body_hit = False

        notes: list[JudgedNote] = []
        for u in self.units:
            obj = self.beatmap_objects[u.index]
            head = self._head.get(u.index, "miss")
            tail = self._tail.get(u.index) if u.is_hold else None
            # 只有真实输入才留位置：到期补判的漏接没有「按下的那一刻」，
            # 硬塞判定时刻会让回放页画出一根不存在的按键线，还会污染整局偏移与 UR。
            press_t = u.press_t
            rel_t = u.rel_t if u.is_hold else None
            item = JudgedNote(
                index=u.index, obj_index=u.index, column=obj.column,
                note_time=obj.time, is_hold=u.is_hold,
                end_time=obj.end_time if u.is_hold else None,
                judgement=head,
                delta=float(press_t - u.start) if press_t is not None else None,
                press_time=float(press_t) if press_t is not None else None,
                release_delta=float(rel_t - u.end) if rel_t is not None else None,
                head_judgement=head,
                tail_judgement=tail,
                judgement_v2=_worse_head_tail(head, tail),
                head_judgement_classic=None, tail_judgement_classic=None,
                judgement_classic=None,
                # body 断连：尾判之前松过手（body_hit 跟着那一刻的尾判结果走），
                # 之后就算按回来、尾判是 Perfect，连击照断。
                body_break=bool(u.is_hold and u.body_judged and not u.body_hit),
            )
            item.dropped = head == "miss"
            notes.append(item)
        notes.sort(key=lambda n: (n.note_time, n.column))
        for i, n in enumerate(notes):
            n.index = i
        return MatchResult(notes=notes, ghosts=self.out.ghosts, beatmap=self.beatmap,
                           od=self.od, windows=self.w, judge_order=self.out.order)


def _worse_head_tail(head: str, tail: str | None) -> str:
    if tail is None:
        return head
    order = ("perfect", "great", "good", "ok", "meh", "miss")
    return head if order.index(head) >= order.index(tail) else tail


def match_lazer(beatmap, frames, windows, column_map=None, keys=None) -> MatchResult:
    """按 lazer 的判定重建一局。frames = [(时间, 绝对按键掩码)]，时间用谱面时基。"""
    eng = _Lazer(beatmap, frames, windows, column_map=column_map, keys=keys)
    eng.beatmap = beatmap
    eng.beatmap_objects = beatmap.objects
    eng.od = beatmap.od
    return eng.run()
