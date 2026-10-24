"""随机找图：按星数区间和键型，从随附索引里抽几张谱面出来。

数据只有 `data/similar_index.json`（44k 张 2020 年后的 4K 图，随程序发布），
所以这个功能完全离线：不查 osu! API、不需要登录。封面和下载走外链，点开才联网。

筛选口径：
  * 星数用 **Sunny 星级**（索引里的 `star`），和页面其它地方说的"几星"是同一把尺子；
    长条占比 ≥45% 的图用的是 LN 星，其余用 RC 星 —— 两套星混用会得出"6 星的图比 8 星还难"。
  * 键型用五类占比之和：选了"叠+技"就要求 `share_stack + share_tech ≥ 0.5`，
    也就是这张图至少一半是你要的东西。多选比单选更宽松，这是有意的。
  * 条件太窄时按阶梯放宽（阈值 0.5→0.3、星数区间 ±2），并把实际用的范围回给前端，
    免得用户对着"没有结果"反复调。
"""

from __future__ import annotations

import json
import os
import random

from .. import paths

CATS = ("cut", "stack", "spam", "tech", "ln")
LABELS = {"cut": "Stream", "stack": "Jack", "spam": "Speed", "tech": "Tech", "ln": "LN"}

# 封面走 osu! 官方 CDN（按图集 id 拼，不需要鉴权）；下载默认走 sayobot（国内快），
# 备用 nerinyan —— 两个都是"点开就下 .osz"，不跳中间页。
COVER = "https://assets.ppy.sh/beatmaps/{sid}/covers/cover.jpg"
DOWNLOAD = "https://dl.sayobot.cn/beatmaps/download/novideo/{sid}"
DOWNLOAD_ALT = "https://api.nerinyan.moe/d/{sid}?nv=1"

# 阶梯放宽：先试 (阈值, 星数外扩) 的组合，池子够大就停
LADDER = ((0.50, 0.0), (0.45, 0.5), (0.40, 1.0), (0.35, 1.5), (0.30, 2.0))
MIN_POOL_FACTOR = 2      # 池子至少是请求数的 2 倍，随机才有意义

_INDEX = None            # [(tuple, {cat: share}), ...]
_COLS = None             # 字段名 → 下标


def index_path() -> str:
    return os.path.join(paths.ROOT, "data", "similar_index.json")


def _load():
    global _INDEX, _COLS
    if _INDEX is not None:
        return _INDEX
    try:
        with open(index_path(), encoding="utf-8") as f:
            payload = json.load(f)
        fields = payload.get("fields") or []
        rows = payload.get("rows") or []
    except (OSError, ValueError):
        _INDEX, _COLS = [], {}
        return _INDEX
    cols = {name: i for i, name in enumerate(fields)}
    built = []
    for row in rows:
        try:
            if float(row[cols["star"]]) <= 0:
                continue          # 没算过星数的图不参与（多是数据没抓全）
            length = float(row[cols["length"]] or 0)
            if 0 < length < 30:
                continue          # 十几秒的练习碎片不算"一张图"，找图时只会碍事
            share = {c: float(row[cols["share_" + c]]) for c in CATS}
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        built.append((row, share))
    _COLS = cols
    _INDEX = built
    return built


def _fmt_length(seconds: float) -> str:
    sec = int(round(float(seconds or 0)))
    if sec <= 0:
        return "—"
    return "%d:%02d" % (sec // 60, sec % 60)


def _card(row, share, picked) -> dict:
    c = _COLS
    star = float(row[c["star"]])
    sid = int(row[c["sid"]])
    # 卡片上只写"主键型 + 占比"：五类都列出来太挤，而用户是按主键型找图的
    top = max(CATS, key=lambda k: share.get(k, 0.0))
    return {
        "sid": sid,
        "bid": int(row[c["bid"]]),
        "title": row[c["title"]] or "",
        "artist": row[c["artist"]] or "",
        "version": row[c["version"]] or "",
        "creator": row[c["creator"]] or "",
        "star": round(star, 2),
        "msd": row[c["msd"]],
        "dan": row[c["dan"]] or "",
        "length": _fmt_length(row[c["length"]]),
        "seconds": row[c["length"]],
        "notes": row[c["notes"]],
        "bpm": row[c["bpm"]],
        "pattern": LABELS.get(top, top),
        "patternKey": top,
        "patternShare": round(share.get(top, 0.0) * 100),
        "matchShare": round(sum(share.get(k, 0.0) for k in picked) * 100) if picked else None,
        "cover": COVER.format(sid=sid),
        "download": DOWNLOAD.format(sid=sid),
        "downloadAlt": DOWNLOAD_ALT.format(sid=sid),
        "page": "https://osu.ppy.sh/beatmapsets/%d" % sid,
    }


def _pool(rows, star_min, star_max, cats, thr):
    out = []
    for row, share in rows:
        try:
            star = float(row[_COLS["star"]])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if star < star_min or star > star_max:
            continue
        if cats and sum(share.get(k, 0.0) for k in cats) < thr:
            continue
        out.append((row, share))
    return out


def pick(star_min: float, star_max: float, cats, limit: int = 8,
         exclude=(), rng: random.Random | None = None) -> dict:
    """抽 limit 张图。exclude 是已经给过的图集 id（点"再找一次"时传回来）。"""
    rows = _load()
    limit = max(1, min(24, int(limit or 8)))
    cats = [c for c in (cats or []) if c in CATS]
    star_min, star_max = sorted((float(star_min), float(star_max)))
    star_min = max(0.0, star_min)
    star_max = min(20.0, star_max)

    picked_pool, used = [], (LADDER[-1][0], LADDER[-1][1], star_min, star_max)
    for thr, pad in LADDER:
        lo = max(0.0, star_min - pad)
        hi = min(20.0, star_max + pad)
        candidate = _pool(rows, lo, hi, cats, thr)
        # 只留"当前最宽的那一档"：条件太窄时宁可放宽，也不给用户一个空列表
        if len(candidate) >= len(picked_pool):
            picked_pool, used = candidate, (thr, pad, lo, hi)
        if len(candidate) >= limit * MIN_POOL_FACTOR:
            break

    relaxed = used[0] < 0.5 or used[1] > 0
    rnd = rng or random.Random()
    order = picked_pool[:]
    rnd.shuffle(order)

    skip_sids = {int(s) for s in exclude or []}
    items, seen_sids = [], set()
    for row, share in order:
        sid = int(row[_COLS["sid"]])
        if sid in skip_sids or sid in seen_sids:
            continue           # 同一图集只给一个难度，也别重复给刚看过的
        seen_sids.add(sid)
        items.append(_card(row, share, cats))
        if len(items) >= limit:
            break

    return {
        "ok": True,
        "items": items,
        "total": len(picked_pool),
        "relaxed": relaxed,
        "used": {
            "starMin": round(used[2], 2),
            "starMax": round(used[3], 2),
            "share": round(used[0] * 100),
            "cats": [LABELS.get(c, c) for c in cats],
        },
    }
