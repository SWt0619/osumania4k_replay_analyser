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

from . import attribution, dan, msd, patterns, report
from .beatmap import JUDGEMENT_LABEL, Beatmap, long_path, parse_osu_file
from .library import get_library
from .match import (
    V1_WEIGHTS,
    MatchResult,
    compare_with_header,
    infer_random_order,
    match_replay,
    max_combo_from,
    v1_weight,
)
from .metrics import histogram, median, percentile, stdev, unstable_rate
from .osr import MOD_MR, MOD_RANDOM, Replay, extract_events, mods_human, parse_osr
from ..version import __version__

JUDGEMENT_CODE = {"perfect": 0, "great": 1, "good": 2, "ok": 3, "meh": 4, "miss": 5}

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


def _find_beatmap(replay: Replay, beatmap_path: str | None, songs_root: str | None) -> tuple[Beatmap, str]:
    if beatmap_path:
        if not os.path.isfile(beatmap_path):
            raise AnalyzeError("指定的谱面文件不存在：%s" % beatmap_path)
        return parse_osu_file(beatmap_path), "manual"

    lib = get_library(songs_root)
    if not lib.entries:
        raise AnalyzeError(
            "没找到 osu! 曲库，无法自动匹配谱面。请在页面下方手动选择对应的 .osu 文件"
            "（或者告诉我你的 Songs 目录）。回放里的谱面 MD5 是 %s" % replay.beatmap_md5
        )

    entry = lib.find(replay.beatmap_md5)
    if entry is None:
        raise AnalyzeError(
            "曲库里没有 MD5 为 %s 的谱面（可能没下载这张图）。请手动选择对应的 .osu 文件。"
            % replay.beatmap_md5
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
    v1: list[float]
    miss: list[int]
    breaks: list[int]


def _accumulate_buckets(
    notes, n_buckets: int, t0: float, bucket_ms: float, cat_by_note
) -> _BucketAccum:
    """把每个物件投进它所属的桶，得到逐桶的原始计数（还不含累计量）。"""
    acc = _BucketAccum(
        notes=[0] * n_buckets,
        errors=[0] * n_buckets,
        cats=[{"ability": 0, "rhythm": 0, "distraction": 0, "slip": 0} for _ in range(n_buckets)],
        deltas=[[] for _ in range(n_buckets)],
        v1=[0.0] * n_buckets,
        miss=[0] * n_buckets,
        breaks=[0] * n_buckets,
    )

    for n in notes:
        idx = int((n.note_time - t0) // bucket_ms)
        if idx < 0 or idx >= n_buckets:
            continue
        acc.notes[idx] += 1
        if n.is_error:
            acc.errors[idx] += 1
        # 单独记「漏接」和「长条断连」：时间轴上只给这两类画节点，其余判定的点太密看不出东西
        if n.is_hold and n.dropped:
            acc.breaks[idx] += 1
        elif n.judgement == "miss":
            acc.miss[idx] += 1
        if cat_by_note:
            cat = cat_by_note.get(id(n))
            if cat in ("ability", "rhythm", "distraction", "slip"):
                acc.cats[idx][cat] += 1
        if n.delta is not None:
            acc.deltas[idx].append(n.delta)
        acc.v1[idx] += v1_weight(n)
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
    cum_w = 0.0     # 从开头累计到当前桶的 v1 权重和
    cum_n = 0       # 从开头累计到当前桶的判定数
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
        cum_n += cnt
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
                # accV1 = **从开头累计到这一桶结束**的 v1 准确率，也就是玩家此刻在游戏里
                # 看到的那个数（0:05 打到 99.74 就是 99.74，不是"这一小段自己的平均"）。
                # 每桶各算各的会让曲线一路锯齿；累计值才连贯，且最后一桶必然收敛到全局 acc。
                "accV1": round(cum_w / cum_n * 100, 2) if cum_n else None,
                # acc 保留给老数据：它是"1 − 严重失误率"（只数 100/50/漏接），对 200 不敏感
                "acc": round((1 - acc.errors[i] / cnt) * 100, 1) if cnt else None,
                "cats": acc.cats[i],
                "flaws": sum(acc.cats[i].values()),
            }
        )
    return buckets



def _timeline(match: MatchResult, bucket_ms: float | None = None, cat_by_note=None, rate: float = 1.0) -> dict:
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
    acc = _accumulate_buckets(notes, n_buckets, t0, bucket_ms, cat_by_note)
    buckets = _bucket_rows(acc, t0, bucket_ms, rate)
    return {
        "bucket": bucket_ms,
        "buckets": buckets,
        # NPS 单独一条曲线（固定 2 秒），不跟上面的自适应分段走
        "npsSeries": _nps_series(notes, t0, t1 + bucket_ms),
    }


def _offset_histogram(match: MatchResult, lo: float = -200.0, hi: float = 200.0, bins: int = 80) -> dict:
    deltas = match.deltas()
    return {
        "lo": lo,
        "hi": hi,
        "bins": bins,
        "counts": histogram(deltas, lo, hi, bins),
        "count": len(deltas),
    }


def _column_stats(match: MatchResult) -> list[dict]:
    keys = max(1, match.beatmap.keys)
    out = []
    for col in range(keys):
        seg = [n for n in match.notes if n.column == col]
        ds = [n.delta for n in seg if n.delta is not None]
        errors = sum(1 for n in seg if n.is_error)
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


def _section_stats(match: MatchResult, rate: float = 1.0, parts: int = 3) -> list[dict]:
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
        errors = sum(1 for n in seg if n.is_error)
        # 这里必须是 v1 加权准确率，和顶部「准确率 (v1)」、报告正文用同一把尺子。
        # 以前算的是"1 − 严重失误率"，吃了 200 仍显示 100% —— 同一屏上两个不同的
        # "准确率"（比如图里 66.7%、面板 94.5%）会让人以为工具算错了。
        seg_acc = sum(v1_weight(n) for n in seg) / len(seg) * 100.0
        out.append(
            {
                "index": i,
                "start": round(lo),
                "end": round(hi),
                "notes": len(seg),
                "errors": errors,
                "acc": round(seg_acc, 2),
                "meanDelta": round(sum(ds) / len(ds), 2) if ds else None,
                # 与时间轴/全局同一个口径：DT/HT 下 UR 要除以倍率
                "ur": round(stdev(ds) * 10 / (rate if rate and rate > 0 else 1.0), 1)
                if len(ds) > 1 else None,
            }
        )
    return out


def _combo_breaks(match: MatchResult, limit: int = 200) -> list[dict]:
    out = []
    for n in match.notes:
        if n.judgement == "miss" or n.dropped:
            out.append(
                {
                    "t": round(n.note_time),
                    "column": n.column,
                    "reason": "漏接" if n.judgement == "miss" else "长按提前松手",
                    "delta": round(n.delta, 1) if n.delta is not None else None,
                }
            )
            if len(out) >= limit:
                break
    return out


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


def _compact_notes(match: MatchResult, atts: list) -> list[dict]:
    """播放器与热力图用的紧凑 notes。

    键名压到一个字母是刻意的：完整 payload 里这一项占体积的大头（几千条 × 每条约 80 字节），
    短键名能把它砍掉一半以上，而前端只读这几个字段。
    """
    att_by_note = {id(a.note): a for a in atts}
    out = []
    for n in match.notes:
        a = att_by_note.get(id(n))
        out.append(
            {
                "t": round(n.note_time, 2),
                "c": n.column,
                "j": JUDGEMENT_CODE.get(n.judgement, 5),
                "d": round(n.delta, 2) if n.delta is not None else None,
                "h": round(n.end_time, 2) if n.is_hold and n.end_time is not None else None,
                # 松手偏差（ms）：回放页要按它画出"实际松手的位置"，只有长按才有
                "rd": round(n.release_delta, 2) if n.is_hold and n.release_delta is not None else None,
                "e": 1 if n.is_flaw else 0,
                "sev": n.severity,
                "cat": a.category if a else None,
            }
        )
    return out


def _error_payload(atts: list, ctx_by_index: dict[int, patterns.NoteContext],
                   stamina: dict | None = None) -> list[dict]:
    """每条失误的完整说明：判定、误差、所在 pattern、归因与"凭什么这么判"。

    这里叠了 a.as_dict()，所以 category/scores/reasons 都在同一层，前端不必再嵌套取值。
    stamina：落在耐力下滑区间里的失误多带一个短标记（失误清单只显示第一条理由，
    整句说明放不下，短标记 + 悬停全文更合适）。
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
        out.append(
            {
                "t": round(n.note_time, 2),
                "column": n.column,
                "judgement": n.judgement,
                "judgementLabel": JUDGEMENT_LABEL.get(n.judgement, n.judgement),
                "delta": round(n.delta, 2) if n.delta is not None else None,
                "pattern": patterns.pattern_label(ctx) if ctx else "散音",
                # 机器可读的键型 key（longjack / jumpstream / chordjack…），中文名见 patterns.PATTERN_LABEL
                "patternKey": patterns.classify(ctx),
                "density": round(ctx.density_1s, 2) if ctx else None,
                "chord": ctx.chord_size if ctx else None,
                "isHold": n.is_hold,
                "dropped": n.dropped,
                "severity": n.severity,
                # 落在耐力窗口里才给：null 表示这条与耐力无关
                "stamina": hint if in_stamina and n.note_time >= st["lateFrom"] else None,
                **a.as_dict(),
            }
        )
    return out


def _summary(match: MatchResult, replay: Replay, atts: list, combo_breaks: list[dict]) -> dict:
    """头部数字块。

    deltas 只取一次：它在下文被用了七八次，每次都重新扫全表在 5000 物件上要多花几十毫秒。
    """
    deltas = match.deltas()
    notes = match.notes
    v2_parts = match.score_v2_parts()
    # ScoreV2：osu! 现行计分，两段式（连击分 + 准度分），长按算头尾两次判定。
    # stable 的 ScoreV2 mod 是 20 万连击 / 80 万准度；lazer 默认是 1 万 / 99 万，两个都给。
    score_v2, grade_v2 = match.score_v2()
    score_v2_lazer, _ = match.score_v2(lazer=True)
    return {
        "accuracy": round(match.accuracy(), 2),
        # UR 官方口径：误差总体标准差 ×10，DT/HT 下还要除以倍率
        # （lazer UnstableRate.cs 把 timeOffset 先 ÷ GameplayRate；这里等价于对 UR 除以 rate）
        "ur": round(unstable_rate(deltas) / (replay.rate or 1.0), 1) if len(deltas) > 1 else 0.0,
        "meanDelta": round(sum(deltas) / len(deltas), 2) if deltas else 0.0,
        "medianDelta": round(median(deltas), 2) if deltas else 0.0,
        "absMedian": round(median([abs(d) for d in deltas]), 2) if deltas else 0.0,
        "p10": round(percentile(deltas, 0.10), 1) if deltas else 0.0,
        "p90": round(percentile(deltas, 0.90), 1) if deltas else 0.0,
        "counts": match.counts(),
        "maxCombo": match.max_combo(),
        "errors": sum(1 for n in notes if n.is_error),
        "flaws": len(atts),
        "errorRate": round(len(atts) / max(1, len(notes)) * 100, 2),
        "severeRate": round(sum(1 for n in notes if n.is_error) / max(1, len(notes)) * 100, 2),
        "ghosts": len(match.ghosts),
        "comboBreaks": len(combo_breaks),
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


def _projection(match: MatchResult, atts, actual_score: int = 0, mods: list[str] | None = None) -> dict:
    """把"容易解决的失误"当成已经修好，重算准确率与 v1 分数。

    口径要说清楚，否则就是个画饼的数字：
      * 三类成因各只修 **10%（至少 1 个）**，且优先修「损失最小」的那些。
      * 修好 = 那一击按 300（great）算，而不是 320 —— 保守一点，别拿满分当预期。
      * 连击按修好的那几条重算，断连补上会带来额外分数。
    """
    total = len(match.notes)
    if not total:
        return {}
    # 每类先按「好修程度」排序：200 最好修，漏接最难（补一个漏掉的音比纠正一个偏掉的音难）
    by_cat: dict[str, list] = {}
    for a in atts:
        if a.category in PROJECTION_CATEGORIES:
            by_cat.setdefault(a.category, []).append(a)
    to_fix = set()
    for cat, items in by_cat.items():
        items.sort(key=lambda a: (a.note.severity, abs(a.note.delta or 0.0)))
        take = max(1, round(len(items) * PROJECTION_FIX_RATE))
        for a in items[:take]:
            to_fix.add(id(a.note))

    cur_js: list[str] = []
    new_js: list[str] = []
    cur_w = 0.0
    new_w = 0.0
    fixed = 0
    for n in match.notes:
        cur_js.append(n.judgement)
        w = v1_weight(n)
        cur_w += w
        if id(n) in to_fix:
            fixed += 1
            new_js.append("great")
            new_w += V1_WEIGHTS["great"]
        else:
            new_js.append(n.judgement)
            new_w += w

    cur_acc = cur_w / total * 100.0
    new_acc = new_w / total * 100.0
    cur_combo = max_combo_from(cur_js)
    new_combo = max_combo_from(new_js)
    # 分数用官方的 ScoreV1 公式（基础分 + 奖励分）直接算，两端同一把尺子，不做任何比例校准。
    # 回放头里的官方分数单独放在 officialScore 里，只作对照。
    cur_model = _v1_score(cur_js, mods)
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


def _pattern_stats(match: MatchResult, ctx_by_index: dict[int, patterns.NoteContext]) -> list[dict]:
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
        if n.is_flaw:
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


def _pair(beatmap: Beatmap, presses, releases) -> MatchResult:
    match = match_replay(beatmap, presses, releases)
    if not match.notes:
        raise AnalyzeError("谱面里没有可分析的物件，或者回放与谱面完全不匹配。")
    return match


def _build_contexts(beatmap: Beatmap):
    """每音符上下文 + 分块粗分类。

    必须先分块再 classify：先分块定大方向（叠 / 乱 / 切 / tech），
    再让每个音符在所属大类内挑细分键型，单个音符的抖动就改不了结论。
    """
    contexts = patterns.build_context(beatmap)
    blocks = patterns.build_blocks(contexts, beatmap.keys, [o.is_hold for o in beatmap.objects])
    patterns.assign_coarse(contexts, blocks)
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

    match = _pair(beatmap, presses, releases)
    watch.mark("match")

    contexts, blocks = _build_contexts(beatmap)
    watch.mark("context")

    # 按 obj_index 建一次索引：后面逐条失误都要取上下文，用 next() 扫全表是 O(n²)
    ctx_by_index = {c.index: c for c in contexts}
    atts, cat_summary = attribution.classify_all(match, contexts, blocks)
    watch.mark("classify")

    verify, warn = _verify_against_header(match, replay, warn)
    combo_breaks = _combo_breaks(match)
    cat_by_note = {id(a.note): a.category for a in atts}
    timeline = _timeline(match, None, cat_by_note, replay.rate)
    sections = _section_stats(match, replay.rate)
    columns = _column_stats(match)
    histo = _offset_histogram(match)
    beatmap_payload = _beatmap_summary(beatmap, source, _peak_nps(match.notes))
    watch.mark("assemble")

    pattern_detail = _pattern_breakdown(contexts)

    payload = {
        "ok": True,
        "version": __version__,
        "warnings": [w for w in [warn] if w],
        "replay": _replay_block(replay, verify, column_order),
        "beatmap": beatmap_payload,
        "summary": _summary(match, replay, atts, combo_breaks),
        "timeline": timeline,
        "offsetHistogram": histo,
        "sections": sections,
        "columnStats": columns,
        "notes": _compact_notes(match, atts),
        "errors": _error_payload(atts, ctx_by_index, (cat_summary.get("profile") or {}).get("stamina")),
        "patterns": _pattern_stats(match, ctx_by_index),
        # 分段的粗分类（叠 / 乱 / 切 / tech）：键型判断先看整段再挑细分，
        # 前端可以在时间轴上把这条带画出来，让人一眼看出这首歌由哪几段构成。
        "blocks": [
            {
                "start": round(b.start_time),
                "end": round(b.end_time),
                "category": b.category,
                "label": b.label,
                "notes": b.notes,
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
        "comboBreaks": combo_breaks,
        "categories": cat_summary,
        "projection": _projection(match, atts, replay.score, replay.mod_names),
        # 谱面难度（MinaCalc MSD）：与回放无关，纯看谱面；页面上画成雷达图
        "msd": msd_for(replay.beatmap_md5, beatmap),
        "elapsedMs": watch.elapsed_ms(),
        "timings": watch.timings,
    }
    # 回放文件没写完（压缩块被截断）时，前端要明确告诉用户"这是残卷"，别让人以为数据就是这么多
    if getattr(replay, "truncated", False):
        payload["warning"] = (
            "这个回放文件不完整（多半是在游戏保存的那一刻被读取的），只分析了能解出的 %d 个事件。"
            "等游戏完全退出后重新拖入可以拿到完整结果。" % len(replay.frames)
        )

    payload["evaluation"] = report.build_evaluation(payload)
    # 相似谱面不在这里算：难度要用前端 Sunny 算出的段位，等页面拿到段位后调 /api/recommend
    return payload
