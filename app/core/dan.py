"""把 MSD 换算成「段位」参照。

段位本身没有官方定义，这里用的是**实测标尺**：把 REFORM 1st/2nd 包与 Malody
4K Regular Dans v3 的 0~10 段全部跑一遍 MinaCalc，得到「段位 → MSD 综合值」的
对应关系（见 data/dan_scale.json，含原始测量值）。

对外只暴露两件事：
  * estimate(msd)  → 这个 MSD 大约相当于几段（带小数），以及落在哪个命名层级
  * scale_table()  → 整条标尺，供页面画刻度
"""

from __future__ import annotations

import json
import os

_HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCALE_PATH = os.path.join(_HERE, "data", "dan_scale.json")


def _load() -> dict:
    try:
        with open(SCALE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"anchors": [], "raw": [], "malody": {}}


_DATA = _load()
ANCHORS = _DATA.get("anchors", [])


def scale_table() -> list[dict]:
    return ANCHORS


def estimate(msd: float | None) -> dict:
    """把 MSD 综合值映射到段位。返回 {label, dan, next, gap, tier}。

    tier：intro（INTRO-1 以下）/ normal（1~10 段）/ extra（Alpha 及以上）
    """
    if not ANCHORS or msd is None or msd <= 0:
        return {}
    first, last = ANCHORS[0], ANCHORS[-1]
    if msd < first["msd"]:
        return {
            "label": "低于 " + first["label"],
            "dan": None,
            "tier": "intro",
            "next": first["label"],
            "gap": round(first["msd"] - msd, 2),
        }
    if msd >= last["msd"]:
        return {
            "label": last["label"] + " 以上",
            "dan": last["dan"],
            "tier": "extra",
            "next": None,
            "gap": None,
        }

    # 找到夹住 msd 的两个锚点，按 MSD 线性插值出小数段位
    for i in range(1, len(ANCHORS)):
        lo, hi = ANCHORS[i - 1], ANCHORS[i]
        if lo["msd"] <= msd <= hi["msd"]:
            span = hi["msd"] - lo["msd"]
            frac = 0.0 if span <= 0 else (msd - lo["msd"]) / span
            dan = lo["dan"] + (hi["dan"] - lo["dan"]) * frac
            # 命名：以更接近的一端为准，但给一个"x.x 段"的连续读数
            near = lo if frac < 0.5 else hi
            if near["dan"] >= 10:
                label = near["label"]
                tier = "extra"
            else:
                label = ("%.1f 段" % dan) if dan > 0 else near["label"]
                tier = "normal" if dan >= 0.9 else "intro"
            return {
                "label": label,
                "dan": round(dan, 2),
                "nearest": near["label"],
                "next": hi["label"],
                "gap": round(hi["msd"] - msd, 2),
                "tier": tier,
            }
    return {}
