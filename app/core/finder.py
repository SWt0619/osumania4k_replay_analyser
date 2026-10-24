"""随机找图：按段位和键型，从随附索引里抽几张谱面出来。

数据只有 `data/similar_index.json`（44k 张 2020 年后的 4K 图，随程序发布），
所以这个功能完全离线：不查 osu! API、不需要登录。封面和下载走外链，点开才联网。

筛选口径：
  * **常规（rc）**用混合估算链的最终段位（`danValue`）：高难走 Roxy、低难走 Azusa、
    非面图走 Sunny。选一个段位，给它的上下各一段（±1）。
  * **面条（ln）**用同一份链里 `||` 后半段的 LN 段位（`lnDanValue`，LN 5~17），
    并且要求这张图真有面条成分（LN 占比 ≥15%）。拿常规段位筛面条是筛不出来的 ——
    面图的 RC 段位和 LN 段位差得远。
  * 键型用五类占比（单选）：选了"叠"就要求这张图的主键型是叠。面条模式不看键型，
    面图的主键型本来就是 LN。
  * 条件太窄时按阶梯放宽（键型阈值 0.5→0.3、段位在 ±1 之外再扩到 ±3），并把实际
    用的范围回给前端，免得用户对着"没有结果"反复调。
"""

from __future__ import annotations

import json
import os
import random

from .. import paths
from . import dan as dan_mod

CATS = ("cut", "stack", "spam", "tech", "ln")
LABELS = {"cut": "Stream", "stack": "Jack", "spam": "Speed", "tech": "Tech", "ln": "LN"}
METHOD_LABELS = {"roxy": "Roxy", "azusa": "Azusa", "sunny": "Sunny"}
MODES = ("rc", "ln")

# 面条模式要求"一半以上是长条"。0.15 只是链里判"面图"的下限，按那个筛会捞出一堆
# 以 Jack / Stream 为主、顺带几个长条的图 —— 面条玩家要的不是这种。
# 0.5 时各段位的存量仍然够用（LN 6~17 每段 32~345 张）。
LN_RATIO_FLOOR = 0.5
# 用户选一个段位，默认推荐上下各一段
DAN_SPAN = 1.0

# 封面走 osu! 官方 CDN（按图集 id 拼，不需要鉴权）；下载默认走 sayobot（国内快），
# 备用 nerinyan —— 两个都是"点开就下 .osz"，不跳中间页。
COVER = "https://assets.ppy.sh/beatmaps/{sid}/covers/cover.jpg"
DOWNLOAD = "https://dl.sayobot.cn/beatmaps/download/novideo/{sid}"
DOWNLOAD_ALT = "https://api.nerinyan.me/d/{sid}?nv=1"

# 阶梯放宽：先试 (键型阈值, 在 ±1 之外再扩几段) 的组合，池子够大就停
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
            if row[cols["danValue"]] is None:
                continue          # 没估出段位的图不参与（多是数据没抓全）
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


def _card(row, share, cat, mode) -> dict:
    c = _COLS
    sid = int(row[c["sid"]])
    # 卡片上只写"主键型 + 占比"：五类都列出来太挤，而用户是按主键型找图的
    top = max(CATS, key=lambda k: share.get(k, 0.0))
    method = row[c["danMethod"]] or ""
    if mode == "ln":
        value = row[c["lnDanValue"]]
        label = row[c["lnDanLabel"]] or dan_mod.ln_label(value)
        short = "LN " + ("%.1f" % float(value)) if value is not None else ""
    else:
        value = row[c["danValue"]]
        label = row[c["danLabel"]] or dan_mod.to_label(value)
        short = dan_mod.short_label(value)
    return {
        "sid": sid,
        "bid": int(row[c["bid"]]),
        "title": row[c["title"]] or "",
        "artist": row[c["artist"]] or "",
        "version": row[c["version"]] or "",
        "creator": row[c["creator"]] or "",
        "star": round(float(row[c["star"]] or 0), 2),
        "msd": row[c["msd"]],
        "mode": mode,
        "dan": label,
        "danValue": round(float(value), 2) if value is not None else None,
        "danShort": short,
        "danMethod": METHOD_LABELS.get(method, method),
        "danMethodKey": method,
        "lnRatio": round(float(row[c["lnRatio"]] or 0), 3),
        "length": _fmt_length(row[c["length"]]),
        "seconds": row[c["length"]],
        "notes": row[c["notes"]],
        "bpm": row[c["bpm"]],
        "pattern": LABELS.get(top, top),
        "patternKey": top,
        "patternShare": round(share.get(top, 0.0) * 100),
        "matchShare": round(share.get(cat, 0.0) * 100) if cat else None,
        "cover": COVER.format(sid=sid),
        "download": DOWNLOAD.format(sid=sid),
        "downloadAlt": DOWNLOAD_ALT.format(sid=sid),
        "page": "https://osu.ppy.sh/beatmapsets/%d" % sid,
    }


def _dan_of(row, mode) -> float | None:
    """这张图在给定模式下的段位；面条模式还会挡掉没有面条成分的图。"""
    c = _COLS
    try:
        if mode == "ln":
            value = row[c["lnDanValue"]]
            if value is None:
                return None
            if float(row[c["lnRatio"]] or 0) < LN_RATIO_FLOOR:
                return None          # 只是沾了点长条，不当面条图
            return float(value)
        value = row[c["danValue"]]
        return None if value is None else float(value)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _pool(rows, dan_min, dan_max, cat, thr, mode):
    out = []
    for row, share in rows:
        value = _dan_of(row, mode)
        if value is None or value < dan_min or value > dan_max:
            continue
        if cat and share.get(cat, 0.0) < thr:
            continue
        out.append((row, share, value))
    return out


def pick(mode: str, dan_center: float, cat: str | None, limit: int = 8,
         exclude=(), rng: random.Random | None = None) -> dict:
    """在 dan_center 上下各一段里抽 limit 张，按段位从小到大排好。

    mode: "rc" 常规 / "ln" 面条，两者互斥。
    exclude: 已经给过的图集 id（点"再找一次"时传回来）。
    """
    rows = _load()
    mode = mode if mode in MODES else "rc"
    limit = max(1, min(24, int(limit or 8)))
    cat = cat if cat in CATS else None
    try:
        center = float(dan_center)
    except (TypeError, ValueError):
        center = 6.0
    center = max(-4.4, min(18.4, center))

    picked_pool, used = [], (LADDER[-1][0], LADDER[-1][1], center - DAN_SPAN, center + DAN_SPAN)
    for thr, pad in LADDER:
        lo = center - DAN_SPAN - pad
        hi = center + DAN_SPAN + pad
        candidate = _pool(rows, lo, hi, cat, thr, mode)
        # 只留"当前最宽的那一档"：条件太窄时宁可放宽，也不给用户一个空列表
        if len(candidate) >= len(picked_pool):
            picked_pool, used = candidate, (thr, pad, lo, hi)
        if len(candidate) >= limit * MIN_POOL_FACTOR:
            break

    relaxed = used[0] < 0.5 or used[1] > 0
    rnd = rng or random.Random()

    # 按 0.5 段分桶、轮着取：否则面条 LN 5 这种"绝大多数图都压在 4.9"的区间，
    # 一批 8 张会全是同一个段位，看着像没筛。
    buckets: dict[float, list] = {}
    for row, share, value in picked_pool:
        buckets.setdefault(round(value * 2) / 2, []).append((row, share))
    for group in buckets.values():
        rnd.shuffle(group)
    order, keys = [], sorted(buckets)
    while len(order) < len(picked_pool):
        added = False
        for key in keys:
            if buckets[key]:
                order.append(buckets[key].pop())
                added = True
        if not added:
            break

    skip_sids = {int(s) for s in exclude or []}
    cards, seen_sids = [], set()
    for row, share in order:
        sid = int(row[_COLS["sid"]])
        if sid in skip_sids or sid in seen_sids:
            continue           # 同一图集只给一个难度，也别重复给刚看过的
        seen_sids.add(sid)
        cards.append(_card(row, share, cat, mode))
        if len(cards) >= limit:
            break

    # 按段位排好再给用户：随机的是"抽哪几张"，不是"怎么摆"
    cards.sort(key=lambda it: it["danValue"] if it["danValue"] is not None else 0)

    label_of = dan_mod.ln_label if mode == "ln" else dan_mod.to_label
    return {
        "ok": True,
        "items": cards,
        "mode": mode,
        "relaxed": relaxed,
        "used": {
            "mode": mode,
            "dan": round(center, 2),
            "danMin": round(used[2], 2),
            "danMax": round(used[3], 2),
            "danMinLabel": label_of(used[2]),
            "danMaxLabel": label_of(used[3]),
            "share": round(used[0] * 100),
            "cat": LABELS.get(cat, "") if cat else "",
        },
    }
