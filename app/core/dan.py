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


# --------------------------------------------------------------- 标签 ↔ 连续段位
# 段位标签是三套估算器共用的口径：REFORM 1~10 段、INTRO（比 1 段还低），
# Alpha 以上一段一跳（Alpha=11 … CloverWisp Theta=18）。每段再分五档。
_SUB = {"low": -0.4, "mid/low": -0.2, "mid": 0.0, "mid/high": 0.2, "high": 0.4}
_EXTRA = {
    "alpha": 11, "beta": 12, "gamma": 13, "delta": 14,
    "epsilon": 15, "emik zeta": 16, "thaumiel eta": 17, "cloverwisp theta": 18,
}


def from_label(label: str | None) -> float | None:
    """把段位标签换成可比较的连续段位（Reform 5 low → 4.6）。

    和估算器自己给的 numeric 对过 4.2 万个样本，中位偏差 0.06 段 —— 说明这套
    换算和 Azusa / Roxy 的数值难度是同一个刻度，可以直接拿来筛图。
    面图的标签会带 "|| < LN 5 mid" 后缀，这里只取 RC 那半。
    """
    text = (label or "").strip().split("||")[0].strip()
    if not text or text.lower().startswith("unknown"):
        return None
    if text.startswith("<"):
        return -4.4          # 比 INTRO 1 low 还低：给一个能排序的哨兵值
    if text.startswith(">"):
        return 18.4          # 比 CloverWisp Theta high 还高
    head, _, sub = text.rpartition(" ")
    offset = _SUB.get(sub.strip().lower())
    if offset is None:
        return None
    low = head.strip().lower()
    if low.startswith("intro"):
        try:
            return int(head.split()[1]) - 4 + offset
        except (IndexError, ValueError):
            return None
    if low.startswith("reform"):
        try:
            return int(head.split()[1]) + offset
        except (IndexError, ValueError):
            return None
    if low in _EXTRA:
        return _EXTRA[low] + offset
    return None


def to_label(dan: float | None) -> str:
    """连续段位换回给人看的标签（4.6 → Reform 5 low）。

    直接拿 `data/dan_rank.json` 的完整档位表反查，比另写一套换算可靠 ——
    这张表就是页面上「段位」下拉和成长页共用的那套名字。
    """
    if dan is None:
        return ""
    value = float(dan)
    if not _LABELS:
        return "%.1f 段" % value
    best = min(_LABELS, key=lambda item: abs(item[0] - value))
    return best[1]


def short_label(dan: float | None) -> str:
    """角标用的短版本：Reform 以上给连续段位（4.6 段），INTRO 给名字（Intro 2）。

    卡片上的空间只够一行，完整标签（Reform 5 low）放在信息区；
    角标用数字是因为它要用来比较大小，而"5 low 和 5 high 差多少"看不出来。
    但 0 段以下没有"负几段"这种说法 —— 那里是 INTRO 1~3，直接说名字。
    """
    if dan is None:
        return ""
    value = float(dan)
    if value < 0.5:
        return to_label(value).rsplit(" ", 1)[0]
    return "%.1f 段" % value


# ------------------------------------------------------------------ 面条段位
# 面图在混合链里给的是 "Reform 1 low || < LN 5 mid"：前半是 RC 段位，后半才是 LN 段位。
# LN 段位是另一套标尺（LN 5 ~ LN 17，16 段叫 Hypersovae、17 段叫 Lnlism），
# 拿 RC 段位筛面条图会筛出一堆根本不打 LN 的图，所以面条要单独一列。
LN_SERIES = {"hypersovae": 16, "lnlism": 17}


def from_ln_label(label: str | None) -> float | None:
    """面条段位标签 → 连续数值（LN 6 mid → 6.0；< LN 5 mid → 4.9）。"""
    text = (label or "").strip()
    if not text or text.lower().startswith("unknown"):
        return None
    if text.startswith("<"):
        return 4.9          # 低于 LN 5 中位：放在 5 段的 ±1 范围内，但不占 5 段本身
    if text.startswith(">"):
        text = text[1:].strip()
    head, _, sub = text.rpartition(" ")
    offset = _SUB.get(sub.strip().lower())
    if offset is None:
        return None
    words = head.strip().split()
    if not words:
        return None
    try:
        number = int(words[-1])
    except ValueError:
        return None
    prefix = words[0].strip().lower()
    if prefix != "ln":
        # "Hypersovae LN 16" / "Lnlism LN 17"：数字已经是对应段位，前缀只是名字
        if prefix not in LN_SERIES:
            return None
    if not 5 <= number <= 17:
        return None
    return number + offset


def ln_label(dan: float | None) -> str:
    """面条段位的显示名（6.2 → LN 6 mid/high）。低于 5 段的统一说"不到 LN 5"。"""
    if dan is None:
        return ""
    value = float(dan)
    if value < 5:
        return "不到 LN 5"
    number = int(round(value))
    names = {16: "Hypersovae LN 16", 17: "Lnlism LN 17"}
    head = names.get(number, "LN %d" % number)
    offset = round(value - number, 2)
    sub = min(_SUB.items(), key=lambda kv: abs(kv[1] - offset))[0]
    return "%s %s" % (head, sub)


def _load_label_table() -> list[tuple[float, str]]:
    try:
        with open(os.path.join(_HERE, "data", "dan_rank.json"), encoding="utf-8") as f:
            names = json.load(f).get("labels") or []
    except (OSError, ValueError):
        return []
    out = []
    for name in names:
        value = from_label(name)
        if value is not None:
            out.append((value, name))
    return out


_LABELS = _load_label_table()


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
