"""相似谱面推荐：按**段位**和**键型构成**找最像的几张图。

为什么难度用段位而不是 MSD：段位是拿真实段位谱拟合出来的（Sunny 星级查表），
和玩家心里的"这张图几段"是同一把尺子，两张图差几档也比 MSD 差几分更直观。
MSD 仍然随推荐结果给出，但只作参考，不参与排序。

相似度 = 0.65 × 键型相似度 + 0.35 × 段位接近度：

  * 键型相似度：五类占比的 L1 距离换算（`1 - L1/2`），0.9 就是"很像"；
  * 段位接近度：段位顺序表里相差几档，按 `exp(-Δ/3)` 衰减。
    档位从 `data/dan_rank.json` 来（由 Sunny 的 RC 区间表生成：Intro 1~3、
    Reform 1~10、Alpha…Zeta…，每段再细分 low / mid-low / mid / mid-high / high）。

三道门槛，宁可不推也不推错：长条占比差 >25 个百分点不推；键型相似度 <0.5 不推；
段位差超过 4 档不推（凑不满五张时依次放宽到 8 档、12 档，12 档那档要求键型 ≥0.65）。
同一图集只留最像的一个难度，同曲名直接排除。
"""

from __future__ import annotations

import json
import math
import os

from .. import paths

W_PATTERN = 0.65
W_DAN = 0.35
# 段位门槛（单位：档，一档 = low / mid-low / mid / mid-high / high 之一）
DAN_GATE = 4
DAN_GATE_LOOSE = 8
DAN_GATE_FALLBACK = 12
# 段位差 3 档时接近度降到约 0.37
DAN_SCALE = 3.0
LN_GATE = 0.25
MIN_PATTERN = 0.5
MIN_PATTERN_FALLBACK = 0.65

CATS = ("cut", "stack", "spam", "tech", "ln")

_INDEX = None        # [(row, dan_rank, {cat: share}), ...] 或 None
_RANKS = None        # {label: rank}


def index_path() -> str:
    return os.path.join(paths.ROOT, "data", "similar_index.json")


def _rank_table() -> dict[str, int]:
    global _RANKS
    if _RANKS is not None:
        return _RANKS
    try:
        with open(os.path.join(paths.ROOT, "data", "dan_rank.json"), encoding="utf-8") as f:
            labels = json.load(f).get("labels") or []
    except (OSError, ValueError):
        labels = []
    _RANKS = {label: i for i, label in enumerate(labels)}
    return _RANKS


def rank_of(label: str | None) -> float | None:
    """段位标签 → 顺序号（越大越难）。认不出来返回 None。"""
    if not label:
        return None
    text = str(label).split("||")[0].strip()
    table = _rank_table()
    if text in table:
        return float(table[text])
    # 区间表两端之外：estDiff2 会给 "< Intro 1 low" / "> CloverWisp Theta high"
    if text.startswith("< "):
        base = rank_of(text[2:])
        return base - 1.0 if base is not None else None
    if text.startswith("> "):
        base = rank_of(text[2:])
        return base + 1.0 if base is not None else None
    return None


def _load() -> list | None:
    """读索引，顺便把每行的段位换算成顺序号（进程内只做一次）。"""
    global _INDEX
    if _INDEX is not None:
        return _INDEX or None
    try:
        with open(index_path(), encoding="utf-8") as f:
            rows = json.load(f).get("rows") or []
    except (OSError, ValueError):
        _INDEX = []
        return None
    built = []
    for row in rows:
        rank = rank_of(row[5])
        if rank is None:
            continue                      # 认不出段位的行不参与推荐
        share = {c: float(row[12 + i]) for i, c in enumerate(CATS)}
        built.append((row, rank, share))
    _INDEX = built
    return built or None


def available() -> bool:
    return _load() is not None


def pattern_similarity(a: dict, b: dict) -> float:
    """五类占比的相似度：0 = 完全不像，1 = 构成一模一样。"""
    l1 = sum(abs(float(a.get(c) or 0.0) - float(b.get(c) or 0.0)) for c in CATS)
    return max(0.0, 1.0 - l1 / 2.0)


def recommend(dan: str | None, share: dict, ln_ratio: float = 0.0,
              artist: str = "", title: str = "", exclude_sid: int | None = None,
              limit: int = 5) -> list[dict]:
    """按段位与键型找最接近的谱面。段位认不出、或索引缺失时返回空列表。"""
    rows = _load()
    q_rank = rank_of(dan)
    if not rows or q_rank is None:
        return []

    tiers = (
        (DAN_GATE, MIN_PATTERN, False),
        (DAN_GATE_LOOSE, MIN_PATTERN, False),
        (DAN_GATE_FALLBACK, MIN_PATTERN_FALLBACK, True),
    )
    best: list[dict] = []
    for gate, min_pat, relaxed in tiers:
        got = _pick(rows, q_rank, share, artist, title, exclude_sid, limit,
                    gate, min_pat, relaxed)
        if len(got) > len(best):
            best = got
        if len(best) >= limit:
            break
    return best[:limit]


def _pick(rows: list, q_rank: float, share: dict, artist: str, title: str,
          exclude_sid: int | None, limit: int, gate: int,
          min_pat: float, relaxed: bool) -> list[dict]:
    q_ln = float(share.get("ln") or 0.0)
    scored = []
    for row, rank, cand in rows:
        if exclude_sid is not None and row[1] == exclude_sid:
            continue
        if artist and title and row[2].lower() == artist.lower() and row[3].lower() == title.lower():
            continue
        gap = abs(rank - q_rank)
        if gap > gate:
            continue
        if abs(cand["ln"] - q_ln) > LN_GATE:
            continue
        pat = pattern_similarity(share, cand)
        if pat < min_pat:
            continue
        score = W_PATTERN * pat + W_DAN * math.exp(-gap / DAN_SCALE)
        scored.append((score, pat, gap, row, cand))

    scored.sort(key=lambda x: -x[0])
    out = []
    seen_sid = set()
    for score, pat, gap, row, cand in scored:
        if row[1] in seen_sid:          # 同一图集只留最像的那个难度
            continue
        seen_sid.add(row[1])
        dominant = max(CATS, key=lambda c: cand[c])
        out.append({
            "bid": row[0],
            "sid": row[1],
            "artist": row[2],
            "title": row[3],
            "version": row[4],
            "dan": row[5],
            "msd": row[6],
            "year": row[11],
            "similarity": round(score, 3),
            "patternSimilarity": round(pat, 3),
            "danGap": int(round(gap)),
            "relaxed": bool(relaxed),
            "dominant": dominant,
            "dominantShare": round(cand[dominant], 3),
            "share": {c: round(v, 3) for c, v in cand.items()},
            "downloadUrl": "https://dl.sayobot.cn/beatmaps/download/novideo/%s" % row[1],
            # 详情给官网页面（sayobot 只有站内弹窗，没有按 sid 的路由）；下载走镜像
            "pageUrl": "https://osu.ppy.sh/beatmapsets/%s" % row[1],
        })
        if len(out) >= limit:
            break
    return out


def for_session(payload: dict, dan: str | None, limit: int = 5) -> list[dict]:
    """从一份分析结果里取查询特征（键型构成、长条占比、曲名）。"""
    from . import patterns

    bm = payload.get("beatmap") or {}
    notes = float(bm.get("notes") or 0)
    ln_ratio = (float(bm.get("holds") or 0) / notes) if notes else 0.0
    share = {x.get("key"): float(x.get("share") or 0) / 100.0
             for x in (payload.get("patternShare") or [])}
    items = recommend(dan=dan, share=share, ln_ratio=ln_ratio,
                      artist=bm.get("artist") or "", title=bm.get("title") or "",
                      exclude_sid=None, limit=limit)
    for item in items:
        item["label"] = patterns.CATEGORY_LABEL.get(item["dominant"], item["dominant"])
        # 键型构成给一句人话（中文名在前端没有第二份表，只能后端拼）
        item["shareLabel"] = " / ".join(
            "%s %d%%" % (patterns.CATEGORY_LABEL.get(c, c), round(item["share"][c] * 100))
            for c in CATS if item["share"].get(c, 0) >= 0.01
        )
    return items
