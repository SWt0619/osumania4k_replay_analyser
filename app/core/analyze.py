"""把「回放 + 谱面」跑成一份完整的分析 JSON（前端只吃这一份）。

对外只有一个入口 `analyze()`，返回可直接 json.dumps 的 dict。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from dataclasses import dataclass

from types import SimpleNamespace

from . import anchor, attribution, dan, history, msd, patterns, report
from .beatmap import JUDGEMENT_LABEL, Beatmap, lazer_windows, long_path, parse_osu_file
from .library import get_library
from . import lazer_engine
from .match import (
    V1_WEIGHTS,
    V2_ACC_VALUE,
    MatchResult,
    compare_with_header,
    infer_random_order,
    match_replay,
    match_replay_frames,
    _scaled_windows,
    max_combo_from,
    v1_weight,
)
from .metrics import histogram, median, percentile, stdev, unstable_rate
from .osr import (LAZER_VERSION, MOD_MR, MOD_RANDOM, Replay, detect_judge_mode, extract_events,
                  judge_source, mods_human, parse_osr)
from ..version import __version__

JUDGEMENT_CODE = {"perfect": 0, "great": 1, "good": 2, "ok": 3, "meh": 4, "miss": 5}


def _weight(note, mode: str = "v1") -> float:
    """当前口径下一击在准确率里的权重（0..1）。

    V1 用官方 ScoreV1 权重（MAX 与 300 同权）；V2 用判定分 ÷ 305，
    所以 V2 下只有全 MAX 才是 100%。分开写是为了时间轴、分段、总评都用同一把尺子。
    """
    if mode in ("v2", "classic"):
        return V2_ACC_VALUE.get(note.judged(mode), 0) / 305.0
    return v1_weight(note)

# 时间轴分段：每段目标约 6 个音符（按密度反推时长），并夹在 250ms~2000ms 之间。
# 固定 2 秒一段的话，慢歌上粗糙到看不出起伏、12 NPS 上又会把 24 个音压成一个点。
TIMELINE_TARGET_NOTES = 6
TIMELINE_BUCKET_MIN = 250.0
TIMELINE_BUCKET_MAX = 2000.0
TIMELINE_MAX_BUCKETS = 2000

# NPS 单独一条曲线，固定 2 秒一格 —— 它和准确率的采样频率必须分开：
# 准确率要"每段约 6 个音"才灵敏，可 NPS 说的是"每秒几个音"，
# 跟着自适应段走的话，同一张图的密度曲线会随分段粒度变形（快歌上全是尖刺）。
NPS_WINDOW_MS = 2000.0


# 只开放 4K：键型判据、段位标尺、MSD 的键型分布都只按 4 键调过，
# 5K 以上的谱面照样能解析，但结论没有依据。所以入口处一律拒绝，而不是给一份看着像
# 那么回事、实际不可信的报告。
SUPPORTED_KEYS = 4


class AnalyzeError(Exception):
    """分析失败（给用户看的错误）。"""


def unsupported_keys_message(beatmap: Beatmap) -> str:
    """非 4K 谱面的统一提示语（服务端、exe 拖拽、页面提示共用这一份措辞）。"""
    return (
        "本工具目前只支持 4K（4 键）谱面。这张谱面是 %dK：%s - %s [%s]，无法分析。"
        "请换用同一首曲子的 4K 难度，或者另找一张 4K 谱。"
        % (beatmap.keys, beatmap.artist, beatmap.title, beatmap.difficulty_name)
    )


def ensure_supported(beatmap: Beatmap) -> None:
    """挡住 4K 以外的谱面。所有导入路径最终都经过这里。"""
    if beatmap.keys != SUPPORTED_KEYS:
        raise AnalyzeError(unsupported_keys_message(beatmap))


# 上一次"找不到谱面"时强制重扫的时间。用户连点几次导入时，
# 不该每次都花一两秒重扫同一份曲库。
_last_force_scan = [0.0]
_FORCE_SCAN_GAP = 10.0


def _find_beatmap(replay: Replay, beatmap_path: str | None, songs_root: str | None) -> tuple[Beatmap, str]:
    if beatmap_path:
        if not os.path.isfile(beatmap_path):
            raise AnalyzeError("指定的谱面文件不存在：%s" % beatmap_path)
        return parse_osu_file(beatmap_path), "manual"

    lib = get_library(songs_root)
    if not lib.entries:
        # 连曲库都没建起来：可能是刚装的客户端，也可能路径指向了空目录。
        lib = get_library(songs_root, force=True)
        if not lib.entries:
            raise AnalyzeError(
                "没找到 osu! 曲库，无法自动匹配谱面。请在页面下方手动选择对应的 .osu 文件"
                "（或者告诉我你的 Songs 目录）。回放里的谱面 MD5 是 %s" % replay.beatmap_md5
            )

    entry = lib.find(replay.beatmap_md5)
    if entry is None:
        # 走这一步的典型场景：用户在 lazer 里下载一张新图、打完直接导出回放拖进来。
        # 这张图在 files/<x>/<xx>/ 里，Songs 一点没变，索引还是启动时建的那份。
        # 与其让用户对着"曲库里没有这张谱面"重试，不如就地重扫一次再找。
        # 带缓存的重扫只走目录，一两秒；扫完再找不到才是真的没有。
        if time.time() - _last_force_scan[0] > _FORCE_SCAN_GAP:
            _last_force_scan[0] = time.time()
            lib = get_library(songs_root, force=True)
            entry = lib.find(replay.beatmap_md5)
    if entry is None:
        raise AnalyzeError(
            "曲库里没有 MD5 为 %s 的谱面（已重新扫描过曲库，可能确实没下载这张图）。"
            "请手动选择对应的 .osu 文件。" % replay.beatmap_md5
        )
    return parse_osu_file(entry.path), "auto"


def _pick_bucket_ms(notes) -> float:
    """按密度决定时间轴的分段粒度（毫秒）。

    "每段约 6 个音"比"每段固定 2 秒"更合理：慢段不至于粗到看不出变化，
    快速段也不会把二十几个音压成一个点。取整到 50ms，刻度与读数好读。
    """
    if len(notes) < 2:
        return TIMELINE_BUCKET_MAX
    span = max(1.0, notes[-1].note_time - min(0.0, notes[0].note_time))
    nps = len(notes) / (span / 1000.0)
    ms = 1000.0 * TIMELINE_TARGET_NOTES / max(0.5, nps)
    ms = round(ms / 50.0) * 50.0
    return float(min(TIMELINE_BUCKET_MAX, max(TIMELINE_BUCKET_MIN, ms)))


def _peak_nps(notes, window_ms: float = 1000.0) -> float:
    """1 秒滑动窗口里的最大音符数。

    不能拿时间轴的桶去算：段长现在随密度变，小段会把一个和弦放大成一串高峰，
    峰值会跟着分段粒度漂移。固定窗口才和谱面本身有关、和分段无关。
    """
    if not notes:
        return 0.0
    times = [n.note_time for n in notes]
    best = 0
    j = 0
    for i in range(len(times)):
        while times[i] - times[j] >= window_ms:
            j += 1
        if i - j + 1 > best:
            best = i - j + 1
    return float(best)


def _ur_from_sums(n: int, s: float, q: float) -> float:
    """由 Σx / Σx² 直接算总体标准差 × 10（等价于 metrics.unstable_rate 的两遍算法）。

    时间轴是逐桶累加的，必须用在线公式；精度上 deltas 是毫秒量级、n 最多几千，
    Σx² 与均值的量级差得不多，不存在灾难性抵消。
    """
    if n < 2:
        return 0.0
    mu = s / n
    var = (q / n) - mu * mu
    return math.sqrt(var if var > 0.0 else 0.0) * 10.0


def _nps_series(notes, t0: float, t1: float, window_ms: float = NPS_WINDOW_MS) -> dict:
    """NPS 曲线：固定 2 秒一格，与准确率/UR 的自适应分段彻底分开。

    准确率那条线要按"每桶约 6 个音"切才灵敏；但 NPS 是密度，采样频率一变，
    同一条曲线就会在快歌上满是尖刺、慢歌上糊成一片。两者各按各的频率采样。
    """
    span = max(1.0, t1 - t0)
    n = max(1, int(span // window_ms) + 1)
    counts = [0] * n
    for item in notes:
        idx = int((item.note_time - t0) // window_ms)
        if 0 <= idx < n:
            counts[idx] += 1
    per_sec = window_ms / 1000.0
    return {
        "bucket": window_ms,
        "points": [{"t": round(t0 + i * window_ms), "v": round(c / per_sec, 2)} for i, c in enumerate(counts)],
    }


def _timeline_span(match: MatchResult, bucket_ms: float | None) -> tuple[float, float, float, int]:
    """确定时间轴的分段粒度与段数，返回 (t0, t1, bucket_ms, 段数)。

    段数超上限时把段拉长，而不是把尾巴截掉——截掉会让长图的末段从时间轴上消失。
    """
    notes = match.notes
    bucket_ms = bucket_ms or _pick_bucket_ms(notes)
    t0 = min(0.0, notes[0].note_time)
    t1 = notes[-1].note_time
    span = max(1.0, t1 - t0)
    if span / bucket_ms > TIMELINE_MAX_BUCKETS:
        bucket_ms = math.ceil(span / TIMELINE_MAX_BUCKETS / 50.0) * 50.0
    return t0, t1, bucket_ms, max(1, int(span // bucket_ms) + 1)


def _units_of(note, mode: str) -> list[float]:
    """这一击在准确率里占的几个"判定单位"（每个单位 0..1）。

    V1 按物件算一次；V2 / Classic 的长条头尾各判一次，所以要给两个权重。
    时间轴的累计准确率必须跟 `MatchResult.accuracy()` 用同一套单位，
    否则长条一多，折线终点就和头部的准确率对不上——之前正是差在这里：
    分子分母都按物件算，V2 下漏掉了 1734 个尾判，末端显示 94.24 而实际是 95.35。
    """
    if mode not in ("v2", "classic"):
        return [v1_weight(note)]
    if mode == "classic":
        head, tail = note.head_judgement_classic, note.tail_judgement_classic
    else:
        head, tail = note.head_judgement, note.tail_judgement
    out = [V2_ACC_VALUE.get(head, 0) / 305.0]
    if note.is_hold:
        out.append(V2_ACC_VALUE.get(tail or head, 0) / 305.0)
    return out


@dataclass
class _BucketAccum:
    """逐桶的原始计数（还没折算成前端要的比例与累计量）。

    七个列表必须一一对齐、按同样的下标读写：以前用 7 元组返回、靠位置解包，
    多加一个统计量就要同时改签名、返回值和调用处三处，漏改只会静默错位。
    """

    notes: list[int]
    errors: list[int]
    cats: list[dict]
    deltas: list[list[float]]
    v1: list[float]      # 累计权重（分子）
    units: list[int]     # 判定单位数（分母）：V2 下长条算两个
    miss: list[int]
    breaks: list[int]


def _accumulate_buckets(
    notes, n_buckets: int, t0: float, bucket_ms: float, cat_by_note, mode: str = "v1"
) -> _BucketAccum:
    """把每个物件投进它所属的桶，得到逐桶的原始计数（还不含累计量）。"""
    acc = _BucketAccum(
        notes=[0] * n_buckets,
        errors=[0] * n_buckets,
        cats=[{"ability": 0, "rhythm": 0, "distraction": 0, "slip": 0} for _ in range(n_buckets)],
        deltas=[[] for _ in range(n_buckets)],
        v1=[0.0] * n_buckets,
        units=[0] * n_buckets,
        miss=[0] * n_buckets,
        breaks=[0] * n_buckets,
    )

    for n in notes:
        idx = int((n.note_time - t0) // bucket_ms)
        if idx < 0 or idx >= n_buckets:
            continue
        acc.notes[idx] += 1
        if n.is_error_for(mode):
            acc.errors[idx] += 1
        # 单独记「漏接」和「长条断连」：时间轴上只给这两类画节点，其余判定的点太密看不出东西
        broken = n.body_break if mode in ("v2", "classic") else n.dropped
        if n.is_hold and broken:
            acc.breaks[idx] += 1
        elif n.judged(mode) == "miss":
            acc.miss[idx] += 1
        if cat_by_note:
            cat = cat_by_note.get(id(n))
            if cat in ("ability", "rhythm", "distraction", "slip"):
                acc.cats[idx][cat] += 1
        if n.delta is not None:
            acc.deltas[idx].append(n.delta)
        for weight in _units_of(n, mode):
            acc.v1[idx] += weight
            acc.units[idx] += 1
    return acc


def _bucket_rows(
    acc: _BucketAccum,
    t0: float,
    bucket_ms: float,
    rate: float,
) -> list[dict]:
    """逐桶落成前端可读的行，并把「从开头累计到此刻」的量一并算出来。"""
    n_buckets = len(acc.notes)
    buckets = []
    cum_w = 0.0     # 从开头累计到当前桶的权重和
    cum_u = 0       # 从开头累计到当前桶的判定单位数（V2 下长条算两个）
    # 累计 UR 的在线统计量：n / Σx / Σx²，够算总体标准差，不必留着所有误差
    run_n = 0
    run_s = 0.0
    run_q = 0.0
    rate_norm = rate if rate and rate > 0 else 1.0
    for i in range(n_buckets):
        cnt = acc.notes[i]
        ds = acc.deltas[i]
        mean_delta = (sum(ds) / len(ds)) if ds else None
        cum_w += acc.v1[i]
        cum_u += acc.units[i]
        run_n += len(ds)
        if ds:
            run_s += sum(ds)
            run_q += sum(x * x for x in ds)
        buckets.append(
            {
                "t": round(t0 + i * bucket_ms),
                "notes": cnt,
                "errors": acc.errors[i],
                "miss": acc.miss[i],
                "breaks": acc.breaks[i],
                "nps": round(cnt / (bucket_ms / 1000.0), 2),
                "meanDelta": round(mean_delta, 2) if mean_delta is not None else None,
                # ur = **从开头累计到这一桶结束**的实时 UR（误差总体标准差 ×10 ÷ 倍率），
                # 和 accV1 同一个口径：玩家此刻的稳定度，而不是"这一小段的平均"。
                # 每桶各算各的会一路锯齿；累计值才连贯，最后一桶必然等于 summary.ur。
                # 样本不足 2 个时给 None（前端断线），别填 0 —— 0 会被读成"极其稳定"。
                "ur": round(_ur_from_sums(run_n, run_s, run_q) / rate_norm, 1) if run_n > 1 else None,
                # accV1 = **从开头累计到这一桶结束**的准确率，也就是玩家此刻在游戏里
                # 看到的那个数（0:05 打到 99.74 就是 99.74，不是"这一小段自己的平均"）。
                # 分母按判定单位算（V2 / Classic 的长条头尾各一次），和 summary.accuracy
                # 同一套账；字段名沿用历史的 accV1，值其实是"当前口径"的准确率。
                # 每桶各算各的会让曲线一路锯齿；累计值才连贯，且最后一桶必然收敛到全局 acc。
                "accV1": round(cum_w / cum_u * 100, 2) if cum_u else None,
                # acc 保留给老数据：它是"1 − 严重失误率"（只数 100/50/漏接），对 200 不敏感
                "acc": round((1 - acc.errors[i] / cnt) * 100, 1) if cnt else None,
                "cats": acc.cats[i],
                "flaws": sum(acc.cats[i].values()),
            }
        )
    return buckets



def _density_timeline(contexts, errors: list[dict], window_ms: int = 4000) -> list[dict]:
    """按固定窗口聚合密度与失误，给报告定位「哪几段最密」。

    密度取窗口内每个音符自己的 density_1s 均值——那是「这一下前后 1 秒有多密」，
    比按窗口数音符更接近手感。窗口 4 秒：两分钟的图约 30 个点，够定位段落，
    又把 payload 控制在几 KB。
    """
    if not contexts:
        return []
    bins: dict[int, dict] = {}
    for c in contexts:
        idx = int(c.time // window_ms)
        b = bins.get(idx)
        if b is None:
            b = bins[idx] = {"t0": idx * window_ms, "notes": 0, "dens": 0.0, "errors": 0}
        b["notes"] += 1
        b["dens"] += float(getattr(c, "density_1s", 0) or 0)
    for e in errors:
        t = e.get("t")
        if not isinstance(t, (int, float)):
            continue
        b = bins.get(int(t // window_ms))
        if b is not None:
            b["errors"] += 1
    end = max(c.time for c in contexts)
    out = []
    for idx in sorted(bins):
        b = bins[idx]
        if not b["notes"]:
            continue
        out.append({
            "t0": b["t0"],
            "t1": round(min(end, b["t0"] + window_ms), 1),
            "density": round(b["dens"] / b["notes"], 1),
            "notes": b["notes"],
            "errors": b["errors"],
        })
    return out


def _timeline(match: MatchResult, bucket_ms: float | None = None, cat_by_note=None,
              rate: float = 1.0, mode: str = "v1") -> dict:
    """时间轴：按时间分段统计 NPS / 失误 / 准确率 / UR。

    bucket_ms 传 None 时按密度自适应（见 _pick_bucket_ms）。
    """
    notes = match.notes
    if not notes:
        return {
            "bucket": bucket_ms or _pick_bucket_ms(notes),
            "buckets": [],
            "npsSeries": {"bucket": NPS_WINDOW_MS, "points": []},
        }
    t0, t1, bucket_ms, n_buckets = _timeline_span(match, bucket_ms)
    acc = _accumulate_buckets(notes, n_buckets, t0, bucket_ms, cat_by_note, mode)
    buckets = _bucket_rows(acc, t0, bucket_ms, rate)
    return {
        "bucket": bucket_ms,
        "buckets": buckets,
        # NPS 单独一条曲线（固定 2 秒），不跟上面的自适应分段走
        "npsSeries": _nps_series(notes, t0, t1 + bucket_ms),
    }


def _offset_histogram(match: MatchResult, lo: float = -200.0, hi: float = 200.0,
                      bins: int = 80, mode: str = "v1") -> dict:
    deltas = match.deltas(mode)
    return {
        "lo": lo,
        "hi": hi,
        "bins": bins,
        "counts": histogram(deltas, lo, hi, bins),
        "count": len(deltas),
    }


def _column_stats(match: MatchResult, mode: str = "v1") -> list[dict]:
    keys = max(1, match.beatmap.keys)
    out = []
    for col in range(keys):
        seg = [n for n in match.notes if n.column == col]
        ds = [n.delta for n in seg if n.delta is not None]
        errors = sum(1 for n in seg if n.is_error_for(mode))
        out.append(
            {
                "column": col,
                "notes": len(seg),
                "errors": errors,
                "errorRate": round(errors / len(seg), 4) if seg else 0.0,
                "meanDelta": round(sum(ds) / len(ds), 2) if ds else None,
                "absMedian": round(median([abs(d) for d in ds]), 2) if ds else None,
                "stdev": round(stdev(ds), 2) if len(ds) > 1 else None,
            }
        )
    return out


def _section_stats(match: MatchResult, rate: float = 1.0, parts: int = 3,
                   mode: str = "v1") -> list[dict]:
    notes = match.notes
    if not notes:
        return []
    times = [n.note_time for n in notes]
    t0, t1 = times[0], times[-1]
    span = max(1.0, t1 - t0)
    out = []
    for i in range(parts):
        lo = t0 + span * i / parts
        hi = t0 + span * (i + 1) / parts
        seg = [n for n in notes if lo <= n.note_time < hi or (i == parts - 1 and n.note_time == hi)]
        if not seg:
            continue
        ds = [n.delta for n in seg if n.delta is not None]
        errors = sum(1 for n in seg if n.is_error_for(mode))
        # 漏接数和头部「MISS」一个口径：V1 一件算一次，V2 / Classic 长条头尾各算一次，
        # 否则三段的 MISS 加起来对不上顶部总数。
        if mode in ("v2", "classic"):
            classic = mode == "classic"
            miss = 0
            for n in seg:
                head = ((n.head_judgement_classic or n.judgement) if classic
                        else (n.head_judgement or n.judgement))
                tail = ((n.tail_judgement_classic if classic else n.tail_judgement) or head)
                miss += 1 if head == "miss" else 0
                if n.is_hold and tail == "miss":
                    miss += 1
        else:
            miss = sum(1 for n in seg if n.judgement == "miss")
        # 与顶部准确率、时间轴用同一把尺子：V2/Classic 的长条头尾各算一个判定单位。
        # 早先这里一件算一份，面图上每段都会被压出几个百分点，凭空多出"这段明显跟不上"。
        units = [u for n in seg for u in _units_of(n, mode)]
        seg_acc = sum(units) / len(units) * 100.0 if units else 0.0
        out.append(
            {
                "index": i,
                "start": round(lo),
                "end": round(hi),
                "notes": len(seg),
                "errors": errors,
                "miss": miss,
                "acc": round(seg_acc, 2),
                "meanDelta": round(sum(ds) / len(ds), 2) if ds else None,
                # 与时间轴/全局同一个口径：DT/HT 下 UR 要除以倍率
                "ur": round(stdev(ds) * 10 / (rate if rate and rate > 0 else 1.0), 1)
                if len(ds) > 1 else None,
            }
        )
    return out


def _combo_breaks(match: MatchResult, limit: int = 200,
                  mode: str = "v1") -> tuple[list[dict], int]:
    """断连明细（最多 limit 条）与总次数。

    两个值分开给：明细要控制体积，评语里的"全曲断了 N 次"必须是真实总数。
    早先直接拿列表长度当次数，整局崩掉时永远显示 200。
    """
    out = []
    total = 0
    for n in match.notes:
        reason = ""
        if mode in ("v2", "classic"):
            head = n.head_judgement_classic if mode == "classic" else n.head_judgement
            tail = n.tail_judgement_classic if mode == "classic" else n.tail_judgement
            if head == "miss":
                reason = "漏接"
            elif tail == "miss":
                reason = "长按尾判失误"
            elif n.body_break:
                reason = "长按提前松手"
        elif n.judgement == "miss" or n.dropped:
            reason = "漏接" if n.judgement == "miss" else "长按提前松手"
        if not reason:
            continue
        total += 1
        if len(out) >= limit:
            continue          # 明细收够了，但总数还得数完
        out.append(
            {
                "t": round(n.note_time),
                "column": n.column,
                "reason": reason,
                "delta": round(n.delta, 1) if n.delta is not None else None,
            }
        )
    return out, total


def _warnings(replay: Replay, beatmap: Beatmap, presses) -> str:
    """键数或 MD5 对不上时给出提示：这两件事都意味着"可能配错了图"，结果不可信但不必中断。"""
    max_col = max((p.column for p in presses), default=-1) + 1
    warn = ""
    if presses and max_col > beatmap.keys:
        warn += "回放里出现第 %d 列，但谱面只有 %d 键，可能配错了谱面。" % (max_col, beatmap.keys)
    if replay.beatmap_md5 and beatmap.md5 and replay.beatmap_md5.lower() != beatmap.md5.lower():
        warn += "回放的谱面 MD5 与所选谱面不一致（%s vs %s），结果可能不准。" % (
            replay.beatmap_md5[:8],
            beatmap.md5[:8],
        )
    return warn


def _compact_notes(match: MatchResult, atts: list, atts_v2: list | None = None,
                   atts_classic: list | None = None) -> list[dict]:
    """播放器与热力图用的紧凑 notes。

    键名压到一个字母是刻意的：完整 payload 里这一项占体积的大头（几千条 × 每条约 80 字节），
    短键名能把它砍掉一半以上，而前端只读这几个字段。

    归因类别给三份（cat / cat2 / catc）：三套判定下的"失误"集合不同，切换口径时
    失误清单的分类也要跟着变。时间、列、误差这些共用，不重复存。
    """
    att_by_note = {id(a.note): a for a in atts}
    att2_by_note = {id(a.note): a for a in (atts_v2 or [])}
    att3_by_note = {id(a.note): a for a in (atts_classic or [])}
    out = []
    for n in match.notes:
        a = att_by_note.get(id(n))
        a2 = att2_by_note.get(id(n))
        a3 = att3_by_note.get(id(n))
        out.append(
            {
                "t": round(n.note_time, 2),
                "c": n.column,
                "j": JUDGEMENT_CODE.get(n.judgement, 5),
                # V2 / Classic 判定：切口径时播放器读这个，颜色与失误过滤都用它
                "j2": JUDGEMENT_CODE.get(n.judgement_v2 or n.judgement, 5),
                "jc": JUDGEMENT_CODE.get(n.judgement_classic or n.judgement, 5),
                "d": round(n.delta, 2) if n.delta is not None else None,
                "h": round(n.end_time, 2) if n.is_hold and n.end_time is not None else None,
                # 松手偏差（ms）：回放页要按它画出"实际松手的位置"，只有长按才有
                "rd": round(n.release_delta, 2) if n.is_hold and n.release_delta is not None else None,
                "e": 1 if n.is_flaw else 0,
                "sev": n.severity,
                "e2": 1 if n.is_flaw_for("v2") else 0,
                "sev2": n.severity_for("v2"),
                "bb": 1 if n.body_break else 0,
                "cat": a.category if a else None,
                "cat2": a2.category if a2 else None,
                "catc": a3.category if a3 else None,
            }
        )
    return out


def _error_payload(atts: list, ctx_by_index: dict[int, patterns.NoteContext],
                   stamina: dict | None = None, mode: str = "v1") -> list[dict]:
    """每条失误的完整说明：判定、误差、所在 pattern、归因与"凭什么这么判"。

    这里叠了 a.as_dict()，所以 category/scores/reasons 都在同一层，前端不必再嵌套取值。
    stamina：只给耐力窗口里"该提醒"的失误带短标记（失误清单只显示第一条理由，
    整句说明放不下，短标记 + 悬停全文更合适）。哪些算该提醒由归因那边按冷却时间决定，
    整段每条都带的话一屏里会重复十几次。
    """
    st = stamina or {}
    in_stamina = bool(st.get("drop")) and st.get("lateFrom") is not None
    hint = ""
    if in_stamina:
        hint = "「%s」段 %.0f%%→%.0f%%" % (
            st.get("label", ""), (st.get("baseRate") or 0) * 100,
            (st.get("testRate") or 0) * 100,
        )
    out = []
    for a in atts:
        n = a.note
        ctx = ctx_by_index.get(n.obj_index)
        judged = n.judged(mode)
        out.append(
            {
                "t": round(n.note_time, 2),
                "column": n.column,
                "judgement": judged,
                "judgementLabel": JUDGEMENT_LABEL.get(judged, judged),
                "delta": round(n.delta, 2) if n.delta is not None else None,
                "pattern": patterns.pattern_label(ctx) if ctx else "散音",
                # 机器可读的键型 key（longjack / jumpstream / chordjack…），中文名见 patterns.PATTERN_LABEL
                "patternKey": patterns.classify(ctx),
                # 所属大块（cut/stack/spam/tech/ln），按段匹配要按大类统计，细分太碎
                "coarse": ctx.coarse if ctx else None,
                "density": round(ctx.density_1s, 2) if ctx else None,
                "chord": ctx.chord_size if ctx else None,
                "isHold": n.is_hold,
                "dropped": (n.body_break if mode in ("v2", "classic") else n.dropped),
                "bodyBreak": n.body_break,
                "severity": n.severity_for(mode),
                # 只有"该提醒"的那几条带标记（15 秒冷却，见 attribution._mark_stamina_hints）
                "stamina": (hint if in_stamina and attribution.is_stamina_hint(st, n.obj_index)
                            else None),
                **a.as_dict(),
            }
        )
    return out


def _summary(match: MatchResult, replay: Replay, atts: list, combo_breaks: list[dict],
             mode: str = "v1", combo_break_count: int | None = None) -> dict:
    """头部数字块。

    deltas 只取一次：它在下文被用了七八次，每次都重新扫全表在 5000 物件上要多花几十毫秒。
    """
    deltas = match.deltas(mode)
    notes = match.notes
    v2_parts = match.score_v2_parts(mode)
    # ScoreV2：osu! 现行计分，两段式（连击分 + 准度分），长按算头尾两次判定。
    # stable 的 ScoreV2 mod 是 20 万连击 / 80 万准度；lazer 默认是 1 万 / 99 万，两个都给。
    score_v2, grade_v2 = match.score_v2(mode=mode)
    score_v2_lazer, _ = match.score_v2(lazer=True, mode=mode)
    return {
        "accuracy": round(match.accuracy(mode), 2),
        # UR 官方口径：误差总体标准差 ×10，DT/HT 下还要除以倍率
        # （lazer UnstableRate.cs 把 timeOffset 先 ÷ GameplayRate；这里等价于对 UR 除以 rate）
        "ur": round(unstable_rate(deltas) / (replay.rate or 1.0), 1) if len(deltas) > 1 else 0.0,
        "meanDelta": round(sum(deltas) / len(deltas), 2) if deltas else 0.0,
        "medianDelta": round(median(deltas), 2) if deltas else 0.0,
        "absMedian": round(median([abs(d) for d in deltas]), 2) if deltas else 0.0,
        "p10": round(percentile(deltas, 0.10), 1) if deltas else 0.0,
        "p90": round(percentile(deltas, 0.90), 1) if deltas else 0.0,
        "counts": match.counts(mode),
        "maxCombo": match.max_combo(mode),
        "errors": sum(1 for n in notes if n.is_error_for(mode)),
        "flaws": len(atts),
        "errorRate": round(len(atts) / max(1, len(notes)) * 100, 2),
        "severeRate": round(sum(1 for n in notes if n.is_error_for(mode)) / max(1, len(notes)) * 100, 2),
        "ghosts": len(match.ghosts),
        "comboBreaks": combo_break_count if combo_break_count is not None else len(combo_breaks),
        "scoreV2": score_v2,
        "scoreV2Lazer": score_v2_lazer,
        "gradeV2": grade_v2,
        "accV2": round(v2_parts["accuracy"] * 100, 2),   # ScoreV2 口径准确率（MAX=305 满分）
        "v2Judgements": v2_parts["judgements"],          # 判定次数（长按算 2 次）
    }


def _beatmap_summary(beatmap: Beatmap, source: str, peak_nps: float) -> dict:
    bm_notes = beatmap.note_count()
    duration = beatmap.duration
    return {
        "title": beatmap.title,
        "artist": beatmap.artist,
        "difficulty": beatmap.difficulty_name,
        "creator": beatmap.meta.get("Creator", ""),
        "keys": beatmap.keys,
        "od": beatmap.od,
        "hp": beatmap.hp,
        "bpm": round(beatmap.primary_bpm(), 2),
        "bpmRange": [round(x, 2) for x in beatmap.bpm_range()],
        "duration": round(duration),
        "notes": bm_notes,
        "holds": sum(1 for o in beatmap.objects if o.is_hold),
        "nps": round(bm_notes / (duration / 1000.0), 2) if duration else 0.0,
        "peakNps": round(peak_nps, 1),
        "columns": beatmap.column_counts(),
        "source": source,
        "hasAudio": bool(beatmap.audio_path),
        "hasBackground": bool(beatmap.background_path),
        "firstObject": round(beatmap.first_time),
        "lastObject": round(beatmap.last_time),
        "md5": beatmap.md5,
        # 谱面自己的 ID：推荐时要靠它把"这张图自己"排掉。
        # 不能只比曲名——.osu 里 Title 与 TitleUnicode 常常是两个名字
        # （例如 Title=Mach space journey / TitleUnicode=音速宇宙旅行），
        # 而镜像站的索引里只有前者，光比名字会漏掉自己。
        "beatmapId": _meta_int(beatmap, "BeatmapID") or None,
        "beatmapSetId": _meta_int(beatmap, "BeatmapSetID") or None,
        # 另一种写法：上面的 title/artist 取的是 Unicode 那版（给页面显示用），
        # 而镜像站的索引里记的往往是 Title / Artist 那版，只带显示名就对不上。
        "titleAlt": _alt_name(beatmap.meta, "Title", "TitleUnicode", beatmap.title),
        "artistAlt": _alt_name(beatmap.meta, "Artist", "ArtistUnicode", beatmap.artist),
    }


def _meta_int(beatmap: Beatmap, key: str) -> int:
    try:
        return int(str(beatmap.meta.get(key, "") or 0).strip())
    except (TypeError, ValueError):
        return 0


def _alt_name(meta: dict, plain_key: str, unicode_key: str, shown: str) -> str:
    """曲名/作者的另一种写法（与页面显示的那个不同）。没有别的写法时返回空串。"""
    for key in (plain_key, unicode_key):
        name = str(meta.get(key, "") or "").strip()
        if name and name != shown:
            return name
    return ""


# ScoreV1（mania）的官方计分表。来源：osu! wiki「osu!mania scoring system」。
# 每个判定的四项：HitValue / HitBonusValue / HitBonus / HitPunishment。
# 分数 = Σ [ unit×HitValue/320 + unit×HitBonusValue×sqrt(Bonus)/320 ]，
# 其中 unit = 1,000,000 × ModMultiplier × 0.5 / 物件数，Bonus 初始 100、夹在 [0,100]。
V1_SCORE_TABLE = {
    "perfect": (320.0, 32.0, 2.0, 0.0),
    "great": (300.0, 32.0, 1.0, 0.0),
    "good": (200.0, 16.0, 0.0, 8.0),
    "ok": (100.0, 8.0, 0.0, 24.0),
    "meh": (50.0, 4.0, 0.0, 44.0),
    "miss": (0.0, 0.0, 0.0, float("inf")),
}

# 降低难度的 Mod → 分数倍率；提高难度的 Mod → 惩罚除数（wiki 的 ModMultiplier / ModDivider）
V1_MOD_MULTIPLIER = {"EZ": 0.5, "NF": 0.5, "HT": 0.5}
V1_MOD_DIVIDER = {"HR": 1.08, "DT": 1.1, "NC": 1.1, "FI": 1.06, "HD": 1.06, "FL": 1.06}

# 预期成绩的口径：三类各只修「10%（至少 1 个）」。
#
# 早先版本把「分神 + 节奏」全部当成能修好，结果动辄 +1.5% 准确率、+3 万分 —— 太乐观了：
# 现实中一局能立刻改善的就是一小撮。现在三类都算（能力问题也能靠练进步一点），
# 但每类只取 10%，优先挑「损失最小」的那些（200 比漏接好修）。
PROJECTION_FIX_RATE = 0.10
PROJECTION_CATEGORIES = ("distraction", "rhythm", "ability")


def _v1_score(judgements: list[str], mods: list[str] | None = None) -> int:
    """按官方公式算 ScoreV1（0~1,000,000）。

    这是**精确公式**，不是拟合：每个音符贡献
        基础分 unit × HitValue/320
      + 奖励分 unit × HitBonusValue × sqrt(Bonus) / 320
    其中 unit = 1,000,000 × ModMultiplier × 0.5 / 物件数；
    Bonus 初始 100，每个音符按 HitBonus / HitPunishment 增减，夹在 [0, 100]。
    300 与 MAX 的 HitValue 差 20（320 vs 300），奖励侧也差（32√100 vs 32√100，靠 HitBonus 2/1 拉开）。
    """
    total = len(judgements)
    if not total:
        return 0
    mods = mods or []
    multiplier = 1.0
    divider = 1.0
    for m in mods:
        if m in V1_MOD_MULTIPLIER:
            multiplier *= V1_MOD_MULTIPLIER[m]
        if m in V1_MOD_DIVIDER:
            divider *= V1_MOD_DIVIDER[m]

    unit = 1000000.0 * multiplier * 0.5 / total
    bonus = 100.0
    score = 0.0
    for j in judgements:
        hit_value, bonus_value, hit_bonus, hit_punish = V1_SCORE_TABLE.get(j, V1_SCORE_TABLE["miss"])
        # 顺序按 wiki 原文：Bonus = 上一个 Bonus + HitBonus - HitPunishment，再拿它算奖励分。
        # 先算分再更新会让每张图系统性偏低（实测 5 份回放都低 0.04~0.9%），反过来只差 0.01~0.7%。
        if hit_punish == float("inf"):
            bonus = 0.0
        else:
            bonus = min(100.0, max(0.0, bonus + hit_bonus - hit_punish / divider))
        score += unit * (hit_value / 320.0)
        score += unit * (bonus_value * (bonus ** 0.5) / 320.0)
    return round(score)


_MSD_CACHE: dict[str, dict] = {}
_MSD_CACHE_PATH = ""
# 服务端是多线程的（ThreadingHTTPServer），而 MSD 缓存是进程级共享状态：
# 两个标签页同时打开同一张图时，一个线程正在 json.dump、另一个往 dict 里插键，
# 会直接抛 "dictionary changed size during iteration"。读写都串行，落盘用快照。
_MSD_LOCK = threading.Lock()


def set_msd_cache_path(path: str) -> None:
    global _MSD_CACHE_PATH, _MSD_CACHE
    with _MSD_LOCK:
        _MSD_CACHE_PATH = path
        _MSD_CACHE = {}
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    _MSD_CACHE = json.load(f)
            except (OSError, ValueError):
                _MSD_CACHE = {}


def _save_msd_cache() -> None:
    """落盘用快照：持锁期间别的线程改不到这份 dict。调用方需持有 _MSD_LOCK。"""
    if not _MSD_CACHE_PATH:
        return
    snapshot = dict(_MSD_CACHE)
    try:
        os.makedirs(os.path.dirname(_MSD_CACHE_PATH), exist_ok=True)
        with open(_MSD_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
    except OSError:
        pass


def msd_for(beatmap_md5: str, beatmap) -> dict:
    """算这张谱的 MSD（MinaCalc）。按谱面 MD5 缓存 —— 同一张图只算一次。

    MSD 只由谱面决定、与回放无关，所以缓存键用 beatmap md5。
    缓存命中不在锁外直接返回：dict 在另一线程里可能正被替换（改缓存路径）。
    """
    key = (beatmap_md5 or "").lower()
    if key:
        with _MSD_LOCK:
            cached = _MSD_CACHE.get(key)
        if cached is not None:
            return cached
    ok, why = msd.available()
    if not ok:
        return {"available": False, "error": why}
    result = msd.calc(beatmap)
    if result is None:
        return {"available": False, "error": msd.last_error() or "MinaCalc 没能算出结果"}
    payload = result.as_dict()
    payload["available"] = True
    payload["dan"] = dan.estimate(payload["overall"])
    payload["scale"] = dan.scale_table()
    if key:
        with _MSD_LOCK:
            _MSD_CACHE[key] = payload
            _save_msd_cache()
    return payload


def _pick_fixable(atts, mode: str = "v1") -> set[int]:
    """挑出"修一下就能好"的击打，返回它们的 id(note) 集合。

    口径：三类成因各只修 10%（至少 1 个），且优先修「损失最小」的那些
    （200 比漏接好修；同样一档里偏得少的更好纠正）。
    """
    by_cat: dict[str, list] = {}
    for a in atts:
        if a.category in PROJECTION_CATEGORIES:
            by_cat.setdefault(a.category, []).append(a)
    to_fix: set[int] = set()
    for items in by_cat.values():
        items.sort(key=lambda a: (a.note.severity_for(mode), abs(a.note.delta or 0.0)))
        take = max(1, round(len(items) * PROJECTION_FIX_RATE))
        for a in items[:take]:
            to_fix.add(id(a.note))
    return to_fix


def _projected_judgements(match: MatchResult, to_fix: set[int], mode: str):
    """按"修好的那几条算 300、其余照旧"重算判定序列，返回（原判定, 新判定, 原权重和, 新权重和, 修了几条）。

    单独一段是因为它要被两处共用：准确率与连击用新序列直接算，
    分数还要把两条序列都送进 ScoreV1 公式，两份序列必须是同一次遍历的结果。
    """
    fix_weight = (V2_ACC_VALUE["great"] / 305.0) if mode in ("v2", "classic") else V1_WEIGHTS["great"]
    cur_js: list[str] = []
    new_js: list[str] = []
    cur_w = 0.0
    new_w = 0.0
    fixed = 0
    for n in match.notes:
        judged = n.judged(mode)
        cur_js.append(judged)
        w = _weight(n, mode)
        cur_w += w
        if id(n) in to_fix:
            fixed += 1
            new_js.append("great")
            new_w += fix_weight
        else:
            new_js.append(judged)
            new_w += w
    return cur_js, new_js, cur_w, new_w, fixed


def _projection(match: MatchResult, atts, actual_score: int = 0, mods: list[str] | None = None,
                mode: str = "v1") -> dict:
    """把"容易解决的失误"当成已经修好，重算准确率与 v1 分数。

    口径要说清楚，否则就是个画饼的数字：
      * 三类成因各只修 **10%（至少 1 个）**，且优先修「损失最小」的那些。
      * 修好 = 那一击按 300（great）算，而不是 320 —— 保守一点，别拿满分当预期。
      * 连击按修好的那几条重算，断连补上会带来额外分数。
      * 准确率按当前判定口径算；分数涨幅仍用 ScoreV1 公式，V2 下只看准确率变化。

    """
    total = len(match.notes)
    if not total:
        return {}
    to_fix = _pick_fixable(atts, mode)
    cur_js, new_js, cur_w, new_w, fixed = _projected_judgements(match, to_fix, mode)

    cur_acc = cur_w / total * 100.0
    new_acc = new_w / total * 100.0
    cur_combo = max_combo_from(cur_js)
    new_combo = max_combo_from(new_js)
    # 分数用官方的 ScoreV1 公式（基础分 + 奖励分）直接算，两端同一把尺子，不做任何比例校准。
    # 回放头里的官方分数单独放在 officialScore 里，只作对照。
    # 「现在」的分数按状态机的判定顺序算（改判之前，顺序与 notes 一致的情况极少）；
    # 修好 N 条之后是假设值，只能按物件顺序近似
    cur_model = _v1_score(match.judge_order or cur_js, mods)
    new_model = _v1_score(new_js, mods)
    # 基准分用回放里那个**真实分数**（如果合法），涨幅用官方公式算出的差值：
    # 这样"现在"和游戏里一字不差，涨幅也不是凭空的比例系数，而是公式给的增量。
    if 0 < actual_score <= 1000000:
        cur_score = actual_score
        new_score = min(1000000, actual_score + (new_model - cur_model))
    else:
        cur_score, new_score = cur_model, min(1000000, new_model)
    return {
        "fixable": fixed,
        "current": {"accuracy": round(cur_acc, 2), "score": cur_score, "maxCombo": cur_combo},
        "projected": {"accuracy": round(new_acc, 2), "score": new_score, "maxCombo": new_combo},
        "gain": {"accuracy": round(new_acc - cur_acc, 2), "score": new_score - cur_score},
        "officialScore": actual_score if 0 < actual_score <= 1000000 else None,
        "note": "分数按官方 ScoreV1 公式（基础分 + 奖励分）计算；officialScore 是回放头里的实际分数，用于对照",
    }


def _pattern_breakdown(contexts) -> list[dict]:
    """键型构成：大类占比 + 每个大类下的细分占比。

    按音符计（不是按块），这样各大类加起来正好是 100%。
    细分只保留前四项，够用且不会把卡片撑爆。
    """
    per: dict[str, dict[str, int]] = {}
    for c in contexts:
        cat = c.coarse or "x"
        key = patterns.classify(c)
        bucket = per.setdefault(cat, {})
        bucket[key] = bucket.get(key, 0) + 1
    total = sum(sum(m.values()) for m in per.values()) or 1
    out = []
    for cat, m in sorted(per.items(), key=lambda kv: -sum(kv[1].values())):
        n = sum(m.values())
        out.append({
            "key": cat,
            "label": patterns.CATEGORY_LABEL.get(cat, cat),
            "share": round(n / total * 100, 1),
            "items": [
                {"key": k, "label": patterns.PATTERN_LABEL.get(k, k), "share": round(v / n * 100, 1)}
                for k, v in sorted(m.items(), key=lambda kv: -kv[1])[:4]
            ],
        })
    return out


def _map_class(pattern_detail: list[dict], holds: int = 0, notes: int = 0) -> dict:
    """整张图归到哪一类。Hybrid = RC 与 LN 都不占压倒性（见 patterns.dominant_class）。

    返回三样东西，别混用：
      key       —— 细类（cut/stack/spam/tech/ln/hybrid），展示用；
      group     —— 大类（rc/ln/hybrid），成长评估只认这个；
      lnShare   —— **长条音符**占全部音符的比例，界面上"长条占 X%"就是它。
    """
    share = {x["key"]: float(x["share"]) / 100.0 for x in pattern_detail}
    hold_ratio = (float(holds) / notes) if notes else 0.0
    key = patterns.dominant_class(share, hold_ratio)
    return {
        "key": key,
        "group": "rc" if key in patterns.RC_CLASSES else key,
        "label": patterns.CATEGORY_LABEL.get(key, key),
        # 用音符比例，不是键型块比例：一张 RC1373 / LN920 的图，
        # 块级只能数出 29%（长条在块里稀疏），而玩家看到的是 920/2293 = 40%。
        "lnShare": round(hold_ratio * 100, 1),
        "lnBlockShare": round(share.get("ln", 0.0) * 100, 1),
    }


def _ln_release_stats(match: MatchResult) -> dict:
    """hybrid / LN 图的松手偏差：四个轨道各自的平均 + 总平均。

    正值表示松手晚于长条尾部，负值表示提前；单位毫秒。按住到结束的 0.0 也计入，
    因为它代表尾判没有偏差，不该被悄悄排除。
    """
    holds = [n for n in match.notes if n.is_hold and n.release_delta is not None]

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 2) if values else None

    columns = []
    all_values: list[float] = []
    for col in range(4):
        values = [n.release_delta for n in holds if n.column == col]
        all_values.extend(values)
        columns.append({"column": col, "count": len(values), "mean": mean(values)})
    return {"total": len(all_values), "mean": mean(all_values), "columns": columns}


def _pattern_stats(match: MatchResult, ctx_by_index: dict[int, patterns.NoteContext],
                   mode: str = "v1") -> list[dict]:
    """每种键型：有多少物件、失误率多少、相对全谱平均"吃亏几倍"。

    为什么必须给 lift（相对倍数）而不是"失误条数占比"：
    一张 80% 都是纵连的图，纵连当然占 80% 的失误 —— 这只说明它在图里多，
    不说明你怕纵连。判断"哪种键型最吃亏"要用「该键型失误率 ÷ 全谱失误率」。
    """
    total = len(match.notes)
    if not total:
        return []
    buckets: dict[str, list[int]] = {}
    for n in match.notes:
        key = patterns.classify(ctx_by_index.get(n.obj_index))
        b = buckets.setdefault(key, [0, 0])
        b[0] += 1
        if n.is_flaw_for(mode):
            b[1] += 1

    global_rate = sum(b[1] for b in buckets.values()) / total
    rows = []
    for key, (cnt, flaws) in buckets.items():
        rate = (flaws / cnt) if cnt else 0.0
        rows.append(
            {
                "key": key,
                "label": patterns.PATTERN_LABEL.get(key, key),
                "notes": cnt,
                "flaws": flaws,
                "share": round(cnt / total * 100, 1),
                "errorRate": round(rate * 100, 1),
                # lift > 1 表示这种键型比全谱平均更容易出错
                "lift": round(rate / global_rate, 2) if global_rate > 0 else 0.0,
            }
        )
    rows.sort(key=lambda r: (-r["lift"], -r["notes"]))
    return rows


def _file_hash(path: str) -> str:
    """整个 .osr 的 sha1（前 16 位够用，碰撞概率可以忽略）。读不到就给空串。"""
    try:
        h = hashlib.sha1()
        with open(long_path(path), "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return ""


def _replay_block(replay: Replay, verify: dict, column_order: list[int] | None = None) -> dict:
    """回放元信息块（玩家、Mod、头部判定数、解析自检）。

    身份字段（做"同一张谱的两次游玩对比"必须靠它们）：
      * `beatmapMd5` —— 哪张谱；
      * `replayMd5`  —— 游戏写进 .osr 头的回放内容哈希，**每次游玩唯一**；
      * `fileHash`   —— .osr 文件字节的 sha1。有些老回放的 replayMd5 是空串或全 0，
                        这时用它兜底，保证"任何两个文件都能区分开"。
    """
    return {
        "file": os.path.basename(replay.path),
        "beatmapMd5": replay.beatmap_md5,
        "replayMd5": replay.replay_md5,
        "fileHash": _file_hash(replay.path),
        "player": replay.player or "(未知玩家)",
        "score": replay.score,
        "maxCombo": replay.max_combo,
        "mods": mods_human(replay.mods),
        "modsList": replay.mod_names,
        "rate": replay.rate,
        "date": replay.timestamp.strftime("%Y-%m-%d %H:%M") if replay.timestamp else "",
        "headerCounts": replay.header_judgements,
        "frames": len(replay.frames),
        # Random mod 推断出的列序（谱面列 → 按键列）；没开 Random 时为 null。
        # 放在回放块里，是因为它描述的是"这一局怎么打的"，而不是谱面本身。
        "columnOrder": column_order,
        "verified": verify,
    }


class _Stopwatch:
    """分段计时：每调一次 mark(key) 就把「距上一次」的毫秒数记进 timings。

    这些数字只用于诊断（页面与日志里能看到哪一段慢），但要保证 key 出现的位置
    与实际阶段一一对应，所以「上次时刻」不能散在 analyze 里手动维护。
    """

    def __init__(self) -> None:
        self.started = time.time()
        self.timings: dict[str, float] = {}
        self._last = time.time()

    def mark(self, key: str) -> None:
        now = time.time()
        self.timings[key] = round((now - self._last) * 1000, 1)
        self._last = now

    def elapsed_ms(self) -> int:
        return round((time.time() - self.started) * 1000)


def _load_replay(replay_path: str) -> Replay:
    """读回放并挡掉「不是 mania」「一个事件都没有」这两种必须中断的情况。"""
    replay = parse_osr(replay_path)
    if replay.mode != 3:
        raise AnalyzeError(
            "这个回放是 %s 模式，不是 osu!mania（本工具只支持 mania）。"
            % {0: "osu!standard", 1: "osu!taiko", 2: "osu!catch", 3: "mania"}.get(replay.mode, str(replay.mode))
        )
    if not replay.frames:
        # 没有事件 ≠ 全部漏接。真实回放至少会有帧；零帧基本是文件没写完/已损坏。
        raise AnalyzeError("回放里没有任何事件（文件可能没写完或已损坏），换一份完整的回放再试。")
    return replay


def _apply_random_order(order: list[int], presses, releases) -> None:
    """按推断出的列序，把回放里的按键列翻回谱面列。

    翻按键而不是改谱面：分列统计、键型、MSD 都按谱面原本的列号说话，
    这一点和 Mirror 的处理保持一致。
    """
    inverse = [0] * len(order)
    for note_col, press_col in enumerate(order):
        inverse[press_col] = note_col
    for ev in presses:
        if 0 <= ev.column < len(inverse):
            ev.column = inverse[ev.column]
    for ev in releases:
        if 0 <= ev.column < len(inverse):
            ev.column = inverse[ev.column]


def _random_notice(order: list[int], best: float, second: float) -> str:
    """把推断结果说清楚：列序是什么、依据多强、哪些数字会受影响。"""
    mapping = "、".join("%d→%d" % (note_col, press_col) for note_col, press_col in enumerate(order))
    text = (
        "检测到 Random mod：回放里的按键列被打乱过，已按这一局按键与音符的吻合度推断出列序"
        "（谱面列→按键列：%s；按这套列序重算的准确率是 %.1f%%）。分列统计仍按谱面原本的列号。"
        % (mapping, best * 100)
    )
    if best < 0.5:
        text += "推断的把握不大（重算准确率偏低），请确认这份回放是不是配错了谱面。"
    elif best - second < 0.05:
        text += ("但这张图的音符在时间上分不出列（换一套列序也有 %.1f%%），"
                 "列序仅供参考，整体准确率不受影响。" % (second * 100))
    return text


def _flip_mirror(beatmap: Beatmap, presses, releases) -> None:
    """Mirror：把按键列号翻回谱面坐标系。

    回放里记的是「玩家看到的」列号，而谱面文件没变。不翻回来的话，第 i 列的音符
    会去配第 i 列的按键，实际该配的是 keys-1-i 列——每一击都错位，准确率会掉到离谱
    （实测 99.5% 变 67%）。只在按键侧翻转、谱面保持原样，这样分列统计仍是
    「谱面第几列」的口径。
    """
    flip = beatmap.keys - 1
    for ev in presses:
        ev.column = flip - ev.column
    for ev in releases:
        ev.column = flip - ev.column


def _prepare_events(replay: Replay, beatmap_path: str | None, songs_root: str | None):
    """定位谱面、抽按键事件、处理 Random / Mirror。

    返回（谱面, 来源, 按键, 松手, 提示语, 推断出的列序或 None）。
    """
    beatmap, source = _find_beatmap(replay, beatmap_path, songs_root)
    # 键数检查放在抽事件之前：4K 以外的谱面不必再算配对与键型，直接给结论
    ensure_supported(beatmap)
    presses, releases = extract_events(replay.frames)

    warn = ""
    column_order = None
    if replay.mods & MOD_RANDOM:
        # Random 会重排四列，而重排结果不在 .osr 里，只能从这一局反推。
        # 推断出的列序已经包含 Mirror 的效果（两者叠加仍是一个列序），所以不再单独翻镜像。
        order, best, second = infer_random_order(beatmap, presses, releases)
        _apply_random_order(order, presses, releases)
        warn += _random_notice(order, best, second)
        column_order = order
    elif replay.mods & MOD_MR:
        _flip_mirror(beatmap, presses, releases)

    warn += _warnings(replay, beatmap, presses)
    if not presses:
        warn += (
            "这份回放里没有任何按键事件，所有物件都会记为漏接。"
            "如果这不是你打的那一局，文件可能没写完或已损坏。"
        )
    return beatmap, source, presses, releases, warn, column_order


def _column_map(replay: Replay, column_order: list[int] | None, keys: int) -> list[int] | None:
    """「谱面列 → 回放列」的映射，给逐帧状态机用。

    Random 的列序已经推断好了（order 就是 谱面列→回放列）；Mirror 是整体翻转。
    两者都没有时返回 None，让状态机按原列号读掩码。
    """
    if column_order:
        return list(column_order)
    if replay.mods & MOD_MR:
        return [keys - 1 - c for c in range(keys)]
    return None


def _judge_model(replay: Replay) -> str:
    """按客户端选判定模型，没有手动开关。

    lazer 和 stable 不是「同一套规则的两个参数」，而是两套判定：
    stable 逐帧推进状态机、长条头尾合并；lazer 按下判头、松手判尾，窗口还带 +0.5。
    拿 stable 的规则去算 lazer 的回放，面条图上会越算越偏，所以只能由回放的
    客户端版本决定——手选留着只会让人选错。
    """
    return "lazer" if int(replay.version or 0) >= LAZER_VERSION else "stable"


def _merge_converted_v1(laz: MatchResult, stable: MatchResult) -> None:
    """把 stable 口径的 Classic 结果搬进 lazer 的配对结果。

    V1 不搬：lazer 回放没有「V1 / ScoreV2」之分，回放头记的 300/100/50 就是
    perfect+great / good / ok+meh 的合并（compare_with_header 也是这么比的），
    直接用 lazer 的判定合并就能对上。拿 stable 的逐帧机器重算反而会多出几个
    漏接——实测同一局 lazer 回放，头记 38 漏，stable 机器算出 42 漏。
    """
    by_obj = {n.obj_index: n for n in stable.notes}
    for note in laz.notes:
        src = by_obj.get(note.obj_index)
        if src is None:
            continue
        note.head_judgement_classic = src.head_judgement_classic
        note.tail_judgement_classic = src.tail_judgement_classic
        note.judgement_classic = src.judgement_classic


def _pair(beatmap: Beatmap, presses, releases, algo: str = "C",
          mods: list[str] | None = None, frames=None,
          column_map: list[int] | None = None,
          model: str = "stable") -> MatchResult:
    if frames:
        stable = match_replay_frames(beatmap, frames, mods=mods, column_map=column_map)
        if model != "lazer":
            match = stable
        else:
            # lazer：事件驱动 + lazer 窗口。它没有逐帧状态机，长条是「按下判头、
            # 松手判尾」，用 stable 的机器只会把松手时机算成 break/miss。
            # note_lock：lazer 的 OrderedHitPolicy（下一个音符起点一到，前一个就不可打），
            # 密集单点上归属与结算都靠它对齐，stable 没有这层锁。
            # lazer 的判定粒度与 stable 不同（头 / 尾 / body 三个单元，尾判在最后一次
            # 松手时结算），所以走 lazer_engine 的逐帧状态机，而不是事件式配对。
            win = _scaled_windows(lazer_windows(beatmap.od), mods)
            match = lazer_engine.match_lazer(beatmap, frames, win, column_map=column_map)
            _merge_converted_v1(match, stable)
    else:
        match = match_replay(beatmap, presses, releases, algo=algo, mods=mods)
    if not match.notes:
        raise AnalyzeError("谱面里没有可分析的物件，或者回放与谱面完全不匹配。")
    return match


def _build_contexts(beatmap: Beatmap):
    """每音符上下文 + 分块粗分类 + 段落级细分键型。

    必须先分块再 classify：先分块定大方向（叠 / 乱 / 切 / tech），
    再让每个音符在所属大类内挑细分，最后用块内众数把细分抬到段落级——
    玩家的键型说法是「这段是 jumpstream」，不是「这个音符是 jump」。
    """
    contexts = patterns.build_context(beatmap)
    blocks = patterns.build_blocks(contexts, beatmap.keys, [o.is_hold for o in beatmap.objects])
    patterns.assign_coarse(contexts, blocks)
    patterns.assign_subpattern(contexts, blocks)
    return contexts, blocks


def _verify_against_header(match: MatchResult, replay: Replay, warn: str) -> tuple[dict, str]:
    """把重算的判定数与回放头对一遍，差太多时补一句"这份结果仅供参考"。

    不拿"任何一项不相等"当条件 —— DT/HT 的判定窗口口径本来就有出入，那样会频繁误报。
    """
    verify = compare_with_header(match, replay.header_judgements)
    header_miss = verify["header"]["miss"]
    mine_miss = verify["mine"]["miss"]
    header_total = verify["total_header"]
    if header_total and abs(mine_miss - header_miss) > max(10, header_total * 0.05):
        warn += (
            "重算出的漏接数（%d）与回放头记录（%d）差得较多，"
            "文件可能不完整或与谱面不匹配，这份结果仅供参考。" % (mine_miss, header_miss)
        )
    return verify, warn


def analyze_chart(osu_path: str) -> dict:
    """只分析谱面：难度、段位、键型构成与分段，完全不碰回放。

    给「导入 .osu 看难度」用。字段刻意与完整分析保持同名（beatmap / msd /
    patternShare / patternDetail / mapClass / blocks），前端可以用同一套渲染函数。
    """
    beatmap = parse_osu_file(osu_path)
    ensure_supported(beatmap)
    if not beatmap.objects:
        raise AnalyzeError("这份谱面里没有任何物件，确认一下选的是不是 mania 难度")

    contexts, blocks = _build_contexts(beatmap)
    detail = _pattern_breakdown(contexts)
    # _peak_nps 只读 note_time，这里没有 JudgedNote，用轻量对象代替
    peak = _peak_nps([SimpleNamespace(note_time=o.time) for o in beatmap.objects])
    bm = _beatmap_summary(beatmap, "上传的 .osu", peak)
    # 前端要跑和回放页同一套估算链（Sunny → Azusa → Roxy），它只认 .osu 原文。
    # 这份文件本来就在刚上传的请求体里，回传一次比再开一个「取谱面原文」的接口简单。
    try:
        with open(long_path(osu_path), "rb") as f:
            osu_text = f.read().decode("utf-8", "replace")
    except OSError:
        osu_text = ""
    return {
        "ok": True,
        "chartOnly": True,
        "version": __version__,
        "beatmap": bm,
        "osuText": osu_text,
        "msd": msd_for(beatmap.md5, beatmap),
        "patternShare": [
            {"key": x["key"], "label": x["label"], "share": x["share"]} for x in detail
        ],
        "patternDetail": detail,
        "mapClass": _map_class(detail, bm["holds"], bm["notes"]),
        "anchor": anchor.compute(contexts, anchor.kind_of(detail)),
        "blocks": [
            {
                "start": round(b.start_time),
                "end": round(b.end_time),
                "category": b.category,
                "label": b.label,
                "notes": b.notes,
                "nps": round(b.nps, 2),
            }
            for b in blocks
        ],
    }


def _beatmap_facts(beatmap: Beatmap, blocks, pattern_detail: list[dict],
                   beatmap_payload: dict, replay: Replay, contexts=None) -> dict:
    """与判定口径无关的谱面侧内容：分段、键型构成、整图大类、MSD。

    单独一段是因为这些字段只依赖谱面，两套判定视图共用同一份；
    混在 analyze() 的大字典里时，改其中一个字段要先翻过十几个无关项。
    """
    return {
        # 分段的粗分类（叠 / 乱 / 切 / tech）：键型判断先看整段再挑细分，
        # 前端可以在时间轴上把这条带画出来，让人一眼看出这首歌由哪几段构成。
        "blocks": [
            {
                "start": round(b.start_time),
                "end": round(b.end_time),
                "category": b.category,
                "label": b.label,
                "notes": b.notes,
                # 段内平均密度与段长：按段匹配要用它把"失误段"换成可比较的特征
                "nps": round(b.nps, 2),
                "jack": b.jack_ratio,
                "chord": b.chord_ratio,
                "regular": b.regularity,
            }
            for b in blocks
        ],
        # 整首歌的键型构成（按块内的物件数加权），给界面上一眼看出"这是叠图还是切图"
        "patternShare": [
            {"key": x["key"], "label": x["label"], "share": x["share"]} for x in pattern_detail
        ],
        # 每个大类下的细分键型（纵连 / 小叠 / 多押叠 · 单双三四押切 …）
        "patternDetail": pattern_detail,
        # 整图大类（Stream / Jack / Speed / Tech / LN / Hybrid）——界面上直接告诉用户"这是什么图"
        "mapClass": _map_class(pattern_detail, beatmap_payload["holds"], beatmap_payload["notes"]),
        # 谱面难度（MinaCalc MSD）：与回放无关，纯看谱面；页面上画成雷达图
        "msd": msd_for(replay.beatmap_md5, beatmap),
        # 锚度只在叠 / 切两个大类下有意义，其它键型给 None
        "anchor": anchor.compute(contexts, anchor.kind_of(pattern_detail)) if contexts else None,
    }


def _build_views(match: MatchResult, replay: Replay, contexts: list,
                 ctx_by_index: dict, blocks, modes: list[str]) -> tuple[dict, dict]:
    """按判定口径各算一份统计视图，返回（views, 每个口径的归因列表）。

    两套判定视图：V1（stable 合并口径）与 V2（头尾分判）。归因、时间轴、失误清单、
    评语都按各自口径重算一遍，这样页面切换时只是换一份数据，
    不会出现"判定变了、准确率没变"的矛盾。
    """
    views: dict[str, dict] = {}
    atts_by_mode: dict[str, list] = {}
    for mode in modes:
        atts, cat_summary = attribution.classify_all(match, contexts, blocks, mode)
        atts_by_mode[mode] = atts
        combo_breaks, combo_break_total = _combo_breaks(match, mode=mode)
        cat_by_note = {id(a.note): a.category for a in atts}
        stamina = (cat_summary.get("profile") or {}).get("stamina")
        errors = _error_payload(atts, ctx_by_index, stamina, mode)
        views[mode] = {
            "summary": _summary(match, replay, atts, combo_breaks, mode, combo_break_total),
            "timeline": _timeline(match, None, cat_by_note, replay.rate, mode),
            "offsetHistogram": _offset_histogram(match, mode=mode),
            "sections": _section_stats(match, replay.rate, 3, mode),
            "columnStats": _column_stats(match, mode),
            "errors": errors,
            # 密度时间线只给报告定位「哪几段最密」，比 sections 细、比逐音符粗
            "densityTimeline": _density_timeline(contexts, errors),
            "patterns": _pattern_stats(match, ctx_by_index, mode),
            "comboBreaks": combo_breaks,
            "categories": cat_summary,
            # 分数投影仍按 ScoreV1 公式；V2 下只留准确率变化，避免两个计分口径混在一起
            "projection": _projection(match, atts, replay.score, replay.mod_names, mode)
            if mode == "v1" else {},
        }
    return views, atts_by_mode


def _ln_stats(match: MatchResult, map_class: dict) -> dict | None:
    """松手偏差只在 hybrid / LN 图上才有意义：纯 RC 图的长条太少，平均值没有代表性。"""
    if str((map_class or {}).get("key") or "").lower() not in ("hybrid", "ln"):
        return None
    return _ln_release_stats(match)


def _recent_same_beatmap(replay: Replay) -> dict | None:
    """同一张图、同一倍率、同一个人的最近几局。没有记录时返回 None。

    查询必须在写库之前：本局是接口返回后才落库的，否则会把自己算成「上一次」。
    只取同一倍率 —— 1.0× 与 1.2× 的准确率/UR 不在同一把尺子上；
    还要对玩家 —— 别人发来的回放也会入库，混进来就成了拿别人的 100% 当自己的「上次」。
    优先用设置里的玩家名，没设置就按这一局回放里的名字。
    """
    try:
        owner = history.configured_player() or (replay.player or "")
        past = history.same_beatmap(replay.beatmap_md5, replay.rate, limit=10, owner=owner,
                                    exclude_key=replay.replay_md5)
    except Exception:  # noqa: BLE001 - 历史是附加功能，读失败不能影响分析
        return None
    if not past:
        return None
    return {"rate": round(float(replay.rate or 1.0), 3), "items": past}


def _truncated_warning(replay: Replay) -> str:
    """回放文件没写完（压缩块被截断）时的提示，完整文件返回空串。

    不提示的话，用户会以为"这一局就这么几个音符"，把残卷当成真实成绩。
    """
    if not getattr(replay, "truncated", False):
        return ""
    return ("这个回放文件不完整（多半是在游戏保存的那一刻被读取的），只分析了能解出的 %d 个事件。"
            "等游戏完全退出后重新拖入可以拿到完整结果。" % len(replay.frames))


def analyze(
    replay_path: str,
    beatmap_path: str | None = None,
    songs_root: str | None = None,
) -> dict:
    watch = _Stopwatch()
    replay = _load_replay(replay_path)
    beatmap, source, presses, releases, warn, column_order = _prepare_events(
        replay, beatmap_path, songs_root)
    watch.mark("parse")

    # 逐帧状态机直接吃回放帧（绝对掩码），presses/releases 只留给「没有帧」的兜底路径
    frames = [(f.time, f.mask) for f in replay.frames]
    column_map = _column_map(replay, column_order, max(1, beatmap.keys))
    model = _judge_model(replay)
    match = _pair(beatmap, presses, releases, "C", replay.mod_names,
                  frames=frames, column_map=column_map, model=model)
    watch.mark("match")

    contexts, blocks = _build_contexts(beatmap)
    watch.mark("context")

    # 按 obj_index 建一次索引：后面逐条失误都要取上下文，用 next() 扫全表是 O(n²)
    ctx_by_index = {c.index: c for c in contexts}
    verify, warn = _verify_against_header(match, replay, warn)
    beatmap_payload = _beatmap_summary(beatmap, source, _peak_nps(match.notes))
    pattern_detail = _pattern_breakdown(contexts)

    # 这些与判定口径无关：谱面、回放头、键型构成、MSD、分段。两套视图共用。
    common = {
        "ok": True,
        "version": __version__,
        "warnings": [w for w in [warn] if w],
        # 判定用的是哪套模型：页面据此给 V1 页签加「换算」提示
        "judgeModel": model,
        "replay": _replay_block(replay, verify, column_order),
        "beatmap": beatmap_payload,
        **_beatmap_facts(beatmap, blocks, pattern_detail, beatmap_payload, replay, contexts),
    }

    recent = _recent_same_beatmap(replay)
    if recent:
        common["history"] = recent

    judge_default = detect_judge_mode(replay.version, replay.mods, replay.score_info)
    modes = ["v1", "v2"]
    if judge_default == "classic":
        modes.append("classic")
    views, atts_by_mode = _build_views(match, replay, contexts, ctx_by_index, blocks, modes)
    watch.mark("classify")

    # notes 只存一份：时间/列/误差共用，判定与归因各留两套短字段
    common["notes"] = _compact_notes(
        match, atts_by_mode["v1"], atts_by_mode["v2"], atts_by_mode.get("classic")
    )
    for mode in modes:
        # judgeMode 必须一起传：评语里的“准确率 (v1/v2)”徽章与正文都靠它，
        # 漏了就会在 V2 视图里继续写 v1。
        # 提速练习的判断按 v1 口径（用户要求），切到 v2 / classic 时这个数不能跟着变
        v1_acc = views["v1"]["summary"]["accuracy"]
        view = {**common, **views[mode], "judgeMode": mode, "accuracyV1": v1_acc}
        views[mode]["evaluation"] = report.build_evaluation(view)

    payload = {
        **common,
        # 顶层保留 V1 字段，旧版前端（以及单文件报告）不用改就能继续读
        **views["v1"],
        # /api/report/advice 直接拿这份顶层 payload 重算建议，也要能读到 v1 准确率
        "accuracyV1": views["v1"]["summary"]["accuracy"],
        "modes": views,
        "judge": {
            "default": judge_default,
            "source": judge_source(replay.version, replay.mods, replay.score_info),
            "modes": modes,
            # 实际用的判定模型（由回放版本决定），页面据此标注口径来源
            "model": model,
        },
        "elapsedMs": watch.elapsed_ms(),
        "timings": watch.timings,
    }
    payload["lnStats"] = _ln_stats(match, common.get("mapClass") or {})
    # 回放文件没写完（压缩块被截断）时，前端要明确告诉用户"这是残卷"，别让人以为数据就是这么多
    warning = _truncated_warning(replay)
    if warning:
        payload["warning"] = warning

    # 相似谱面不在这里算：难度要用前端 Sunny 算出的段位，等页面拿到段位后调 /api/recommend
    return payload
