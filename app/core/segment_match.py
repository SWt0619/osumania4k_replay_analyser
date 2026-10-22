"""按段匹配：拿一局里失误最集中的几段，去谱面库里找"局部很像"的图。

这是功能框架，不是成品：算法、索引读取、查询特征的提取都在这里，
页面与服务端接口等数据验证过之后再接（接入点见文件末尾的说明）。

为什么按段而不是按整图：整图相似只能回答"这两张图风格像不像"，
而玩家真正要练的往往是"这张图 2:10 有一段双押切，和我刚才崩的地方一样"。
谱面库里的每张图已经有 30 秒一段的指纹（`_dataset/analyze.py` 生成），
这里只负责把指纹和"你的失误段"对上。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

# 与推荐索引、键型大类保持同一套 key；顺序固定，索引里存的是数组不是对象
CATS = ("cut", "stack", "spam", "tech", "ln")

# 查询段与候选段的相似度权重：构成比密度更能说明"是不是同一种写法"
W_SHARE = 0.65
W_NPS = 0.35
# NPS 用比例距离：8 NPS 对 10 NPS 与 16 对 20 是同一种差别
NPS_SCALE = 0.45
# 低于这个分数就不算"像"，避免把一堆弱相关的结果塞给用户
MIN_SCORE = 0.55


@dataclass
class Segment:
    """一段的特征。t 是段起点（毫秒），nps 是该段平均密度，share 是五类占比（0~1）。"""

    t: float
    nps: float
    share: dict[str, float] = field(default_factory=dict)

    def as_row(self) -> list[float]:
        return [round(self.t, 1), round(self.nps, 2)] + [
            round(float(self.share.get(c) or 0.0), 3) for c in CATS
        ]


def _norm_share(counts: dict[str, int], total: int) -> dict[str, float]:
    if total <= 0:
        return {c: 0.0 for c in CATS}
    return {c: counts.get(c, 0) / total for c in CATS}


def segments_from_payload(payload: dict, limit: int = 3, min_errors: int = 2) -> list[Segment]:
    """从一份分析结果里挑出失误最集中的几段，转成查询特征。

    口径说明：
      * 段来自 `payload["blocks"]`（约 64 个物件一段，块间重叠 25%）；
      * 每段的五类占比按落在段内的失误统计，没有失误时退回该段的主类（one-hot）；
      * 排序看"段内失误条数"，同分时看单位时间失误数——长段天然吃亏，用密度拉平。

    这样做的代价是"占比"只反映失误处的写法，不是整段的写法。等 `blocks` 直接
    输出五类构成之后（见 ROADMAP 第二阶段），把这里换成段级占比即可，接口不变。
    """
    blocks = payload.get("blocks") or []
    errors = payload.get("errors") or []
    if not blocks or not errors:
        return []

    buckets: list[dict] = []
    for b in blocks:
        start = float(b.get("start") or 0)
        end = float(b.get("end") or start)
        cat = str(b.get("category") or "")
        if cat == "calm":          # 过渡段不作为练习目标
            continue
        buckets.append({"start": start, "end": end, "cat": cat,
                        "notes": int(b.get("notes") or 0), "counts": {}, "n": 0})

    for e in errors:
        t = float(e.get("t") or 0)
        cat = e.get("coarse") or ""
        if not cat:
            continue
        for b in buckets:
            if b["start"] <= t <= b["end"]:
                b["counts"][cat] = b["counts"].get(cat, 0) + 1
                b["n"] += 1
                break

    picked = [b for b in buckets if b["n"] >= min_errors]
    if not picked:                  # 失误太散：退一步，取失误最多的那一段
        picked = sorted(buckets, key=lambda b: -b["n"])[:limit]
    picked.sort(key=lambda b: (-b["n"], -(b["n"] / max(1.0, (b["end"] - b["start"]) / 1000.0))))

    out: list[Segment] = []
    for b in picked[:limit]:
        span = max(1.0, (b["end"] - b["start"]) / 1000.0)
        nps = float(b.get("nps") or 0.0)
        if nps <= 0:
            # blocks 里没给 nps 时按段内物件数估；不能拿失误数当密度
            nps = float(b.get("notes") or 0) / span
        if b["n"]:
            share = _norm_share(b["counts"], b["n"])
        else:
            share = {c: 1.0 if c == b["cat"] else 0.0 for c in CATS}
        out.append(Segment(t=b["start"], nps=nps, share=share))
    return out


def _share_similarity(a: dict[str, float], b: list[float]) -> float:
    """五类占比的 L1 距离折成 0~1：完全一致 = 1，完全相反 = 0。"""
    l1 = sum(abs(float(a.get(c) or 0.0) - float(b[i])) for i, c in enumerate(CATS))
    return max(0.0, 1.0 - l1 / 2.0)


def segment_similarity(query: Segment, row: list[float]) -> float:
    """查询段与候选段（索引里的数组）的相似度。"""
    seg_nps = float(row[1] or 0.0)
    nps_term = math.exp(-abs(math.log((query.nps + 0.5) / (seg_nps + 0.5))) / NPS_SCALE)
    return W_SHARE * _share_similarity(query.share, row[2:]) + W_NPS * nps_term


def match(rows, queries: list[Segment], top: int = 20,
          exclude_bid: int | None = None, min_score: float = MIN_SCORE) -> list[dict]:
    """对每张谱面找最像的那一段，按相似度与命中段数排序。

    rows 的元素形如 `(bid, segments)`，segments 是 `[[t, nps, cut, stack, spam, tech, ln], ...]`。
    返回只带 bid 与匹配理由；曲名、段位、下载链接由上层用 bid 去主索引里补，
    这样这一层不必关心推荐索引的字段顺序。
    """
    if not rows or not queries:
        return []

    out: list[dict] = []
    for item in rows:
        bid, segments = item
        if exclude_bid is not None and int(bid) == int(exclude_bid):
            continue
        best = -1.0
        best_t = 0.0
        hits = 0
        for q in queries:
            local_best = -1.0
            local_t = 0.0
            for seg in segments:
                score = segment_similarity(q, seg)
                if score > local_best:
                    local_best, local_t = score, float(seg[0])
            if local_best >= min_score:
                hits += 1
                if local_best > best:
                    best, best_t = local_best, local_t
        if best < min_score:
            continue
        # 多个失误段都能在这张图里找到对应，比只命中一段更值得推荐；
        # 加分封顶，避免"十段全中"把相似度顶到 1 以上
        score = min(1.0, best + 0.04 * max(0, hits - 1))
        out.append({
            "bid": int(bid),
            "similarity": round(score, 3),
            "bestSegment": round(best, 3),
            # 索引里的 t 存的是秒（maps.jsonl 是毫秒，构建时已经除过 1000）
            "segmentTime": round(best_t, 1),
            "hits": hits,
        })

    out.sort(key=lambda x: (-x["similarity"], -x["hits"], x["bid"]))
    return out[:top]


def load_index(path: str) -> tuple[dict, list[tuple[int, list]]]:
    """读分段索引。文件不存在或版本不对时返回空表，调用方按"该功能不可用"处理。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}, []
    if data.get("version") != 1:
        return {}, []
    rows = []
    for item in data.get("rows") or []:
        try:
            rows.append((int(item[0]), item[1]))
        except (IndexError, TypeError, ValueError):
            continue
    return {"bucketSec": data.get("bucketSec") or 30}, rows


# 接入说明（留给后续，不在本框架里实现）：
#   1. `_dataset/build_segment_index.py` 生成 `_dataset/segment_index.json`；
#   2. 分析结果里已经有 `errors`（每条失误的时间与大类）与 `blocks`，直接喂
#      `segments_from_payload` 即可，不需要改 analyze；
#   3. 服务端新增 `/api/segment-recommend`，把 match() 的结果用主索引补全曲名、
#      段位、下载链接，页面在"相似谱面"卡片下面加一块"按段找"；
#   4. 索引体积可能到十几 MB，先按需加载，不要塞进发行包。
