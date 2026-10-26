"""锚度：同轨连续结构的密度。

只对**叠（stack）与切（cut）**两个大类有意义——别的键型里同轨连打不是难点所在。

两套判据，因为两类图的"同轨连续"不是一回事：

* **叠**：同列紧邻（16 分/8 分层的每行回按），以及 1/8 层的隔行回按。
  这里必须限定层：16 分行里的隔行回按是切图的锚，不是叠。
* **切**：只认 8 分回按（同列隔两行）。16 分紧邻属于局部小叠，整段剔除，
  不算进切的锚度。

原始值（两套公式不同）拿到后，按同类参照集的百分位换算成 0~100 的锚度——
所以"锚度 73"读作"比 73% 的同类谱面更锚"，跨类不可比。

参照数据在 `data/anchor.json`：分位表 + 几张参考图，由离线脚本生成。
"""

from __future__ import annotations

import bisect
import json
import os
import threading
from collections import defaultdict

from .. import paths

# 长连续（≥4 个同键位）加成：每多一个 +5%，封顶 +30%
def _boost(length: int) -> float:
    return 1.0 + 0.05 * min(6, max(0, length - 3))


_DATA: dict | None = None
_LOCK = threading.Lock()


def _data() -> dict:
    global _DATA
    if _DATA is not None:
        return _DATA
    with _LOCK:
        if _DATA is None:
            path = os.path.join(paths.ROOT, "data", "anchor.json")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    _DATA = json.load(f)
            except (OSError, ValueError):
                _DATA = {}
    return _DATA


def _chains(ctxs, kind: str) -> list[int]:
    """按列串链，返回每条链的长度。判据见模块开头。"""
    by_col: dict[int, list] = defaultdict(list)
    for c in ctxs:
        by_col[c.column].append(c)

    if kind == "cut":
        # 切：先把 16 分紧邻（局部小叠）整段剔除，剩下的才归切管
        drop = set()
        for items in by_col.values():
            for i in range(1, len(items)):
                a, b = items[i - 1], items[i]
                if (b.time - a.time) / (b.row_gap or 1e-6) <= 1.2:
                    drop.add(a.index)
                    drop.add(b.index)
        by_col = {k: [c for c in v if c.index not in drop] for k, v in by_col.items()}

    out: list[int] = []
    for items in by_col.values():
        if not items:
            continue
        cur = [items[0]]
        for i in range(1, len(items)):
            a, b = items[i - 1], items[i]
            ratio = (b.time - a.time) / (b.row_gap or 1e-6)
            layer = (b.row_gap / b.beat_length) if b.beat_length else 0.0
            ok = False
            if kind == "stack":
                if ratio <= 1.2:
                    if layer <= 0.34:
                        ok = True
                    elif 0.42 <= layer <= 0.58:
                        # 8 分叠：每行回同列，但必须是单键或同一组和弦在反复
                        ok = (b.chord_size <= 1 or b.repeat_chord
                              or b.row_overlap >= 0.5)
                elif 0.42 <= layer <= 0.58 and 1.6 <= ratio <= 2.6 and b.chord_size <= 2:
                    ok = True
            else:
                if 1.7 <= ratio <= 2.4 and not b.is_trill:
                    ok = True
            if ok:
                cur.append(b)
            else:
                if len(cur) >= 2:
                    out.append(len(cur))
                cur = [b]
        if len(cur) >= 2:
            out.append(len(cur))
    return out


def _metric(lens: list[int], notes: int, kind: str) -> float:
    if not lens:
        return 0.0
    if kind == "cut":
        # 切：同轨率，按链里"多出来的重复次数"算
        return 100.0 * sum((l - 1) * _boost(l) for l in lens) / max(1, notes)
    # 叠：覆盖率 × 链深，两个都要（只看链深会被一条超长链带偏）
    weights = [l * _boost(l) for l in lens]
    cov = sum(weights) / float(max(1, notes))
    depth = sum(l * l * _boost(l) for l in lens) / sum(weights) - 1.0
    return 100.0 * cov * depth


def _percentile(cuts: list[float], value: float) -> int:
    if not cuts:
        return 0
    return max(0, min(100, bisect.bisect_right(cuts, value) - 1))


def compute(ctxs, kind: str) -> dict | None:
    """算一张谱的锚度。kind 只接受 stack / cut，其它返回 None。

    返回：锚度（百分位）、原始值、顺位、链统计、是否 vibro。
    参照数据缺失时返回带 error 的字典——和「键型不适用」区分开，
    否则用户看到「不适用」会以为是自己的图有问题。
    """
    if kind not in ("stack", "cut") or not ctxs:
        return None
    data = _data().get(kind) or {}
    cuts = data.get("cuts") or []
    total = int(data.get("total") or 0)
    if not cuts:
        return {"kind": kind, "score": None, "error": "参照数据缺失"}

    lens = _chains(ctxs, kind)
    notes = len(ctxs)
    value = _metric(lens, notes, kind)
    score = _percentile(cuts, value)
    avg = (sum(lens) / len(lens)) if lens else 0.0
    longest = max(lens) if lens else 0

    # vibro：同键位超高速反复，锚度会极端偏高，数字没有参考价值
    vibro = bool(lens) and (avg >= 12 or longest >= 60)

    # 顺位按百分位折算成"第 N / 总数"；总数不到时给 1
    rank = max(1, int(round(total * (1 - score / 100.0)))) if total else 1

    return {
        "kind": kind,
        "score": score,
        "value": round(value, 1),
        "rank": rank,
        "total": total,
        "chains": len(lens),
        "coverage": round(100.0 * sum(lens) / max(1, notes)),
        "avgLen": round(avg, 2),
        "longest": longest,
        "vibro": vibro,
    }


def kind_of(share: list[dict]) -> str:
    """从键型构成里取主导大类；只有 stack / cut 才给锚度。"""
    if not share:
        return ""
    top = max(share, key=lambda x: float(x.get("share") or 0))
    key = str(top.get("key") or "")
    return key if key in ("stack", "cut") else ""
