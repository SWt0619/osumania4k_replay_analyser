"""个人成长：把历史存档折算成「离下一段还有多远」。

规则（对齐游戏里的过段线）：

  * REFORM 的过段标准是 V1 准确率 96%。所以一局的「有效段位」=
    谱面段位 + (本局准确率 − 96) × 0.5。
    打一张 7 段的图拿到 96% 就是 7.0 段，98% 是 8.0 段，94% 是 6.0 段。
  * 取最好的 50 局求平均，得到综合段位；分练法（叠 / 技术 / 速度 / 耐力）
    各取最好的 10 局。
  * 样本少的时候给「初步评估」，并明确标出样本数量，避免把噪音当结论。

段位一律用 MSD 标尺（`dan.estimate`），和回放页顶部那个混合算法的读数会有
零点几段的差：这里要的是能存档、能跨版本复算的固定口径。
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict

from . import dan

PASS_ACC = 96.0          # REFORM 过段线：V1 准确率 96%
MIN_ACC = 91.0           # 低于这个准确率的成绩没有参考价值，不进长期评估

# 准确率 -> 段位偏移的对照表（Alpha 之前的规则）。
# 不再是"每差 1 个点折算固定段数"的直线：95→96 只涨 0.3 段，而 99→100 涨 0.5 段，
# 越靠近满分越难涨，这样 99.9% 不会和 98% 被读成同一档。
# 中间按线性插值取值；91 以下不进评估，所以表从 91 开始。
ACC_CURVE: tuple[tuple[float, float], ...] = (
    (91.0, -2.0), (93.0, -1.0), (95.0, -0.3), (96.0, 0.0),
    (97.0, 0.3), (98.0, 0.8), (99.0, 1.5), (100.0, 2.0),
)
CURVE_FULL = 2.0         # 对照表两端的绝对值；Alpha 之前正好用满，往上按比例缩
TOP_PLAYS = 50           # 综合评估取最好的 50 局
SKILL_TOP = 10           # 每个练法取最好的 10 局
MIN_SESSIONS = 25        # 少于 25 局不出综合段位
FULL_SESSIONS = 50       # 满 50 局才算稳定
MIN_SKILL = 5            # 单个练法少于 5 局不出评估
FULL_SKILL = 10

SKILLS = ("jack", "tech", "speed", "stamina")
SKILL_LABEL = {"jack": "叠", "tech": "技术", "speed": "速度", "stamina": "耐力"}

# 段位阶梯：目标永远是"上一格"，不是"数值加一"。
# 希腊段之间只差 0.5，按 floor+1 算会把 Delta（12.0）的目标算成 13.0，
# 而 13.0 应该是 Zeta，不是"比 Delta 高一段"这种模糊说法。
# 段位刻度的版本号。1 = 旧刻度（Alpha 记作 10.5，10 段以上 0.5 一跳）；
# 2 = 现在这套（Alpha 记作 11 段，10 段以上 1 段一跳）。存配置和数据库时都用它判断要不要换算。
DAN_SCALE = 2


def rescale_dan(value: float) -> float:
    """把旧刻度的段位换算到当前刻度。10 段以下两套刻度是同一个值，不用动。"""
    v = float(value)
    return v if v <= 10 else round(10 + (v - 10) * 2, 2)


# 段位阶梯：10 段之后一律 1 段一跳 —— Alpha 就是 11 段、Beta 12、Gamma 13，
# 不存在"Alpha 是 10.5 段"这种半段写法。
LADDER: list[tuple[float, str]] = (
    [(-3, "INTRO-1"), (-2, "INTRO-2"), (-1, "INTRO-3")]
    + [(i, "%d 段" % i) for i in range(1, 11)]
    + [(11, "Alpha"), (12, "Beta"), (13, "Gamma"), (14, "Delta"),
       (15, "Epsilon"), (16, "Zeta"), (17, "Eta")]
)

# 到这里往上，谱面样本和 MSD 标尺都不再可靠，评估只能当参考
BEYOND_DAN = 14.0


_HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCALE_PATH = os.path.join(_HERE, "data", "dan_scale.json")


def _malody_scale() -> dict[int, float]:
    """早期一轮 MinaCalc 口径的标定（`data/dan_scale.json` 的 malody 表），只覆盖 Regular 0~10。

    留着当兜底：新的实测表缺失时（比如用户只解压了旧包）还能把常规段换算出来。
    """
    try:
        with open(SCALE_PATH, encoding="utf-8") as f:
            raw = (json.load(f) or {}).get("malody") or {}
    except (OSError, ValueError):
        return {}
    out = {}
    for key, msd in raw.items():
        try:
            out[int(key)] = float(msd)
        except (TypeError, ValueError):
            continue
    return out


def _malody_measured() -> dict[int, dict]:
    """段位谱实测表（`data/malody_dan.json`）。

    由 `_tools/calibrate_malody.py` 生成：把 Malody 的 Regular / Extra 段位谱丢进
    Sunny → Azusa → Roxy 这条链跑一遍，读到的段位就是「能过这张段位谱的人大约几段」。
    这里只负责把 JSON 读成 {段位编码: 连续段位}。
    """
    path = os.path.join(_HERE, "data", "malody_dan.json")
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f) or {}
    except (OSError, ValueError):
        return {}
    out = {}
    for key, row in (raw.get("levels") or {}).items():
        try:
            out[int(key)] = {"dan": float(row.get("dan")), "label": row.get("label") or ""}
        except (TypeError, ValueError):
            continue
    return out


_MALODY_MSD = _malody_scale()
_MALODY_MEASURED = _malody_measured()


def malody_to_reform(value: float) -> dict:
    """Malody 段位换成 REFORM 段位。

    以段位谱的实测值为准——段位谱本来就是用来定段的，把它丢进本程序的混合算法，
    得到的「这段位谱有多难」就是换算值。没有任何体感对照或插值。
    实测表缺失时才退回 MinaCalc 口径的老标定（只有 Regular 0~10）。

    数值约定：0~10 = Regular，11~19 = Extra 1~9，20 = Extra Final。
    """
    try:
        key = int(round(float(value)))
    except (TypeError, ValueError):
        return {}

    hit = _MALODY_MEASURED.get(key)
    if hit and hit.get("dan") is not None:
        return {"dan": hit["dan"], "label": dan_label(hit["dan"]), "measured": True}

    msd = _MALODY_MSD.get(key)
    if msd:
        info = dan.estimate(msd) or {}
        if info.get("dan") is not None:
            return {"dan": info["dan"],
                    "label": info.get("label") or dan_label(info["dan"]),
                    "msd": msd, "measured": False}
    return {}


def malody_label(value: float) -> str:
    """把 Malody 的选择写成玩家看得懂的名字。"""
    v = int(round(float(value)))
    if v >= 20:
        return "Extra Final"
    if v >= 11:
        return "Extra %d" % (v - 10)
    return "Regular %d" % v


def malody_view(value: float | None) -> dict:
    """把 REFORM 连续段位放回 Malody 自己的档位刻度。

    Malody 的段位是离散的（Regular 1~10、Extra 1~9、Extra Final），实测表给了每一档
    对应的 REFORM 值。这里找出当前值落在哪两档之间，算「距下一档还差多少」，
    进度条就按这一段填 —— 以前一律折成 REFORM 显示，Malody 玩家看不到自己的段位名。
    """
    if value is None or not _MALODY_MEASURED:
        return {}
    rows = sorted((float(row["dan"]), key, row["label"])
                  for key, row in _MALODY_MEASURED.items())
    v = float(value)
    if v < rows[0][0]:
        return {"label": rows[0][2], "next": rows[0][2], "nextDan": rows[0][0],
                "gap": round(rows[0][0] - v, 2), "progress": 0.0}
    for i in range(len(rows) - 1):
        lo, hi = rows[i][0], rows[i + 1][0]
        if lo <= v < hi:
            span = hi - lo
            return {
                "label": rows[i][2],
                "next": rows[i + 1][2],
                "nextDan": hi,
                "gap": round(hi - v, 2),
                "progress": round((v - lo) / span, 3) if span > 0 else 0.0,
            }
    return {"label": rows[-1][2], "next": "", "nextDan": rows[-1][0],
            "gap": 0.0, "progress": 1.0}


def curve_offset(acc: float) -> float:
    """按对照表插值出「这个准确率在 Alpha 之前值多少段偏移」。

    表是分段线性的，落在两点之间就按比例取；超出两端就取端点值，不做外推
    （100% 以上不存在，91% 以下本来就进不来）。
    """
    if acc <= ACC_CURVE[0][0]:
        return ACC_CURVE[0][1]
    if acc >= ACC_CURVE[-1][0]:
        return ACC_CURVE[-1][1]
    for i in range(1, len(ACC_CURVE)):
        x1, y1 = ACC_CURVE[i]
        if acc <= x1:
            x0, y0 = ACC_CURVE[i - 1]
            return y0 + (acc - x0) * (y1 - y0) / (x1 - x0)
    return ACC_CURVE[-1][1]


def clamp_span(base: float) -> tuple[float, float]:
    """这一局允许偏离谱面段位多少，返回 (下限, 上限)，都带符号。

    段位越高，谱面之间的可比性越差：一张 Delta 图打出 99% 可能是图太熟，
    而不是有 Delta 以上的实力，所以越往上越要压住"超出谱面"的那一头。
    Alpha 之前上下各 2 段；Alpha 之后到 Delta 之前上限 1.5、下限 2；
    Delta 及以后上限 0.8、下限 1.5。
    """
    if base >= 14.0:        # Delta（14 段）及以后
        return -1.5, 0.8
    if base > 11.0:         # Alpha（11 段）之后、Delta 之前
        return -2.0, 1.5
    return -2.0, 2.0        # Alpha 及以前


def play_value(session: dict) -> float | None:
    """一局折算成「有效段位」。谱面段位缺失、没成绩或准确率太低时返回 None。"""
    try:
        base = float(session.get("dan_value"))
        acc = float(session.get("accuracy"))
    except (TypeError, ValueError):
        return None
    if not base or base <= 0 or not acc:
        return None
    # 91% 以下说明这一局自己都没打明白，读不出"离下一段多远"，留在历史里但不进评估
    if acc < MIN_ACC:
        return None
    offset = curve_offset(acc)
    lo, hi = clamp_span(base)
    # 高段位的上下限收得更紧：正负两个方向分别按端点比例缩放对照表，
    # 所以 100% 在 Delta 图上只值 +0.8，而 91% 依然扣满 −1.5。
    scale = (hi if offset >= 0 else abs(lo)) / CURVE_FULL
    value = base + offset * scale
    return max(base + lo, min(base + hi, value))


def _play_row(session: dict, value: float) -> dict:
    return {
        "key": session.get("key"),
        # 去重用：同一张谱面（谱面 MD5 相同）只算最好的一局。老记录没有 MD5 时留空，
        # 下面按"认不出是不是同一张"处理，不参与合并。
        "md5": (session.get("beatmap_md5") or "").strip().lower(),
        "at": session.get("played_at"),
        "artist": session.get("artist") or "",
        "title": session.get("title") or "",
        "difficulty": session.get("difficulty") or "",
        "dan": round(float(session.get("dan_value") or 0), 2),
        "acc": round(float(session.get("accuracy") or 0), 2),
        "value": round(value, 2),
        "skill": session.get("skill"),
        # 说明为什么这张没进单键型 b10（键型分散 / LN Hybrid / 认不出玩家）
        "note": session.get("pattern_label") or "",
    }


# 加权平均的权重：峰值放在"前 20%"的位置（rows 已按折合值降序），从峰值往两端线性衰减。
WEIGHT_PEAK = 0.2
WEIGHT_DROP = 0.75      # 最好那端降到 0.85，最差那端降到 0.40


def _average(rows: list[dict]) -> float | None:
    """中间偏上最重、两端递减的加权平均。

    直接算术平均会被大量低分局拖低：50 局从 8.85 均匀排到 11.30，平均只有 10.07，
    比最好那局低 1.2 段，读起来不像"这人的水平"。权重峰值放在前 20%（第 10 名附近），
    好成绩说话更算数，同时也不至于只看一两局 —— 两端仍保留 0.85 / 0.40 的权重。
    """
    n = len(rows)
    if n == 0:
        return None
    if n == 1:
        return float(rows[0]["value"])
    peak = (n - 1) * WEIGHT_PEAK
    num = 0.0
    den = 0.0
    for i, r in enumerate(rows):
        w = 1.0 - WEIGHT_DROP * abs(i - peak) / (n - 1)
        num += w * float(r["value"])
        den += w
    return num / den if den else None


def next_rung(current: float) -> tuple[float, str] | None:
    """当前段位上面那一格。已经到顶时返回 None。"""
    for value, label in LADDER:
        if value > float(current) + 1e-9:
            return value, label
    return None


def _kept_sessions(sessions: list[dict], owner: str) -> tuple[list[dict], int, int, int]:
    """筛掉「不是本人」和 LN / Hybrid，返回（留下的记录, 别人的, 身份不明的, LN/Hybrid 的）。

    只排除 LN / Hybrid。老库里 map_class 还可能是 stack/spam/tech/cut ——
    那是 RC 的细类，按"不等于 rc 就排除"会误伤一大批正经 RC 图。
    """
    kept: list[dict] = []
    others = 0
    unknown = 0
    ln_hybrid = 0
    for s in sessions:
        if owner:
            flag = s.get("is_me")
            if flag == 0:
                others += 1
                continue
            if flag is None:
                unknown += 1
                continue
        cls = (s.get("map_class") or "rc").strip().lower()
        if cls in ("ln", "hybrid"):
            ln_hybrid += 1
            continue
        kept.append(s)
    return kept, others, unknown, ln_hybrid


def _play_rows(kept: list[dict]) -> tuple[list[dict], int, int]:
    """把记录折算成可比较的成绩行。

    返回（按段位降序的行, 准确率过低的条数, 因同一张谱面被合并掉的条数）。

    同一张谱面（MD5 相同）只留折算值最高的一局：否则一张拿手的图反复打，最好 50 局里
    能塞进十几条一模一样的成绩，段位就被"重复次数"刷上去了 —— 那不是水平。合成图集时
    每张图本来也只会出一份成绩。MD5 缺失的老记录分不清是不是同一张，按独立记录计入。
    """
    plays: list[dict] = []
    low_acc = 0
    for s in kept:
        v = play_value(s)
        if v is None:
            # 只统计"准确率太低"这种，段位缺失的记录本来就不该出现在评估里
            try:
                if float(s.get("accuracy") or 0) < MIN_ACC:
                    low_acc += 1
            except (TypeError, ValueError):
                pass
            continue
        plays.append(_play_row(s, v))
    plays.sort(key=lambda p: p["value"], reverse=True)

    unique: list[dict] = []
    seen: set[str] = set()
    duplicates = 0
    for p in plays:
        md5 = p.get("md5") or ""
        if md5:
            if md5 in seen:
                duplicates += 1
                continue
            seen.add(md5)
        unique.append(p)
    return unique, low_acc, duplicates


def _ln_sessions(sessions: list[dict], owner: str) -> tuple[list[dict], int, int]:
    """挑出 LN / Hybrid 的记录（仍然只认本人），给 LN 专区用。

    常规 B50 会把这两类排除在外：它们的准确率口径和 RC 的 96% 过段线不是一回事。
    但排除不等于不该看，所以单独一页按 v2 的 97% 折算。
    返回（记录, 别人的, 认不出玩家的）。
    """
    kept: list[dict] = []
    others = 0
    unknown = 0
    for s in sessions:
        if owner:
            flag = s.get("is_me")
            if flag == 0:
                others += 1
                continue
            if flag is None:
                unknown += 1
                continue
        cls = (s.get("map_class") or "rc").strip().lower()
        if cls in ("ln", "hybrid"):
            kept.append(s)
    return kept, others, unknown


# ---------------------------------------------------------------- LN 专区口径
# score v2 的判定窗比 v1 严一档：97% 相当于 v1 的 96%，所以整条对照表右移 1 个百分点。
# 折合上限固定 ±1.5 段 —— LN 图之间可比性差，不打分段位，一刀切更省事也更好读。
# score v2 的判定窗比 v1 严一档：97% 相当于 v1 的 96%，所以整条曲线以 97 为原点。
# 下限 91 正好扣满 −2、上限 100 正好加满 +2，再乘 LN_SPAN/CURVE_FULL 就落在 ±1.5 内。
# 形状沿用 v1 那套"越靠近满分越陡"：99→100 涨 0.8，比 97→98 的 0.5 更值钱。
LN_ACC_CURVE: tuple[tuple[float, float], ...] = (
    (91.0, -2.0), (93.0, -1.2), (95.0, -0.5), (96.0, -0.25),
    (97.0, 0.0), (98.0, 0.5), (99.0, 1.2), (100.0, 2.0),
)
LN_PASS_ACC = 97.0
LN_MIN_ACC = 91.0
LN_SPAN = 1.5
LN_MIN_SESSIONS = 10      # LN 图本来就少，门槛比常规 B50 低一档
LN_FULL_SESSIONS = 30


def ln_curve_offset(acc: float) -> float:
    """v2 口径的准确率偏移：97% 记 0，100% 记 +2，91% 记 −2。"""
    a = float(acc)
    if a <= LN_ACC_CURVE[0][0]:
        return LN_ACC_CURVE[0][1]
    if a >= LN_ACC_CURVE[-1][0]:
        return LN_ACC_CURVE[-1][1]
    for i in range(1, len(LN_ACC_CURVE)):
        x1, y1 = LN_ACC_CURVE[i]
        if a <= x1:
            x0, y0 = LN_ACC_CURVE[i - 1]
            return y0 + (a - x0) * (y1 - y0) / (x1 - x0)
    return LN_ACC_CURVE[-1][1]


def ln_play_value(session: dict) -> float | None:
    """一局 LN / Hybrid 折算成有效段位。

    原始段位取 `ln_dan_value`（面条标尺，Sunny 给的 LN 段位），准确率按 v2 口径。
    上下限固定 ±1.5，不再按段位分档：LN 谱面样本少，分档只会让读数更飘。
    """
    try:
        base = float(session.get("ln_dan_value") or 0)
        acc = float(session.get("accuracy") or 0)
    except (TypeError, ValueError):
        return None
    if base <= 0 or not acc:
        return None
    if acc < LN_MIN_ACC:
        return None
    offset = ln_curve_offset(acc)
    scale = LN_SPAN / CURVE_FULL
    value = base + offset * scale
    return max(base - LN_SPAN, min(base + LN_SPAN, value))


def _ln_play_row(session: dict, value: float) -> dict:
    row = _play_row(session, value)
    # 卡片上要显示"这张图本身是几段"，LN 专区用的是面条段位
    row["dan"] = round(float(session.get("ln_dan_value") or 0), 2)
    row["label"] = dan.ln_label(session.get("ln_dan_value"))
    # 倍率进键里：同一张图开 HT 与 1.0× 是两个难度，不能当成同一局合并
    row["rate"] = round(float(session.get("rate") or 1), 3)
    return row


def _ln_play_rows(kept: list[dict]) -> tuple[list[dict], int, int, int]:
    """LN 专区的成绩行，返回（行, 准确率太低, 同图合并, 缺 LN 段位）。

    去重规则和常规 B50 一样：同一张谱面只留折算值最高的一局，
    否则一张拿手的 LN 图反复打就能把专区段位刷上去。
    """
    plays: list[dict] = []
    low_acc = 0
    no_dan = 0
    for s in kept:
        try:
            acc = float(s.get("accuracy") or 0)
        except (TypeError, ValueError):
            acc = 0.0
        if acc < LN_MIN_ACC:
            low_acc += 1
            continue
        if not (float(s.get("ln_dan_value") or 0) > 0):
            # 老记录只有 RC 段位：算不出面条折合，先记一笔，页面上会提示重新导入
            no_dan += 1
            continue
        v = ln_play_value(s)
        if v is None:
            continue
        plays.append(_ln_play_row(s, v))
    plays.sort(key=lambda p: p["value"], reverse=True)

    unique: list[dict] = []
    seen: set[str] = set()
    duplicates = 0
    for p in plays:
        md5 = p.get("md5") or ""
        if md5:
            # 倍率一起进键：HT 0.75× 打出的 13 段和 1.0× 的 17 段本来就是两张不同的图，
            # 只按 MD5 合并会把开 HT 那局当成"同一张图的低分版本"直接丢掉
            key = "%s|%g" % (md5, p.get("rate") or 1)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
        unique.append(p)
    return unique, low_acc, duplicates, no_dan


def _skill_blocks(plays: list[dict]) -> dict:
    """按练法分组，各自取最好的若干局。样本不够的组只给局数，不出段位。"""
    by_skill: dict[str, list[dict]] = defaultdict(list)
    for p in plays:
        if p["skill"] in SKILLS:
            by_skill[p["skill"]].append(p)

    skills = {}
    for key in SKILLS:
        group = by_skill.get(key, [])
        rows = group[:SKILL_TOP]
        avg = _average(rows)
        skills[key] = {
            "label": SKILL_LABEL[key],
            "count": len(group),
            "used": len(rows),
            "visible": len(group) >= MIN_SKILL,
            "preliminary": MIN_SKILL <= len(group) < FULL_SKILL,
            "need": max(0, MIN_SKILL - len(group)),
            "dan": round(avg, 2) if (avg is not None and len(group) >= MIN_SKILL) else None,
            # 该练法实际用到的局（最好 10 局），点开卡片时摊给玩家看
            "plays": rows,
        }
    return skills


def _target_block(profile: dict | None, overall_dan: float | None, count: int,
                  top: list[dict]) -> tuple[dict, bool]:
    """离下一段还差多少。返回（目标块, 是否已经越过评估上限）。"""
    target: dict = {}
    beyond = False
    if not (profile and profile.get("dan")):
        return target, beyond

    current = float(profile["dan"])
    beyond = current >= BEYOND_DAN - 1e-9
    nxt = next_rung(current)
    if not nxt:
        return {"dan": None, "label": "", "progress": 1.0, "gap": 0.0,
                "status": "max", "qualified": 0, "beyond": True}, beyond

    target_dan, target_label = nxt
    span = max(0.1, target_dan - current)
    if overall_dan is None or count < MIN_SESSIONS:
        return {
            "dan": target_dan,
            "label": target_label,
            "progress": 0.0,
            "gap": None,
            "status": "unknown",
            "qualified": 0,
            "beyond": beyond,
        }, beyond

    gap = target_dan - overall_dan
    ratio = max(0.0, gap) / span
    qualified = sum(1 for p in top if p["value"] >= target_dan - span * 0.45)
    return {
        "dan": target_dan,
        "label": target_label,
        "progress": round(max(0.0, min(1.0, 1.0 - ratio)), 3),
        "gap": round(gap, 2),
        "status": ("above" if gap <= -span else
                   "ready" if ratio <= 0.15 else
                   "close" if ratio <= 0.45 else "far"),
        "qualified": qualified,
        "beyond": beyond,
    }, beyond


def evaluate(sessions: list[dict], profile: dict | None = None) -> dict:
    """把存档折成一份评估结果。profile 是用户选的当前段位（里面带本人名字）。

    只统计纯 RC 谱面：LN 与 Hybrid 的准确率口径和 96% 过段线不是一回事，
    混进来会把综合评价往奇怪的方向拉。
    """
    owner = str((profile or {}).get("player") or "").strip()
    kept, others, unknown, ln_hybrid = _kept_sessions(sessions, owner)
    plays, low_acc, duplicates = _play_rows(kept)

    top = plays[:TOP_PLAYS]
    overall_dan = _average(top)
    count = len(plays)

    overall = {
        "count": count,
        "visible": count >= MIN_SESSIONS,
        "preliminary": MIN_SESSIONS <= count < FULL_SESSIONS,
        "need": max(0, MIN_SESSIONS - count),
        "dan": round(overall_dan, 2) if (overall_dan is not None and count >= MIN_SESSIONS) else None,
        # 全部 50 局都带上：页面上的 5×10 卡片要逐张摊开，让人看清是哪些回放算进来的
        "plays": top,
    }

    target, beyond = _target_block(profile, overall_dan, count, top)

    # Malody 玩家看自己的档位：REFORM 数值仍然留在 overall.dan 里当标尺，
    # 这里额外给一份「Extra 4、距 Extra 5 还差 0.02」的视图。
    malody = {}
    if str((profile or {}).get("type") or "").lower() == "malody" and overall_dan is not None:
        malody = malody_view(overall_dan)

    # LN 专区：常规 B50 排除掉的 LN / Hybrid，在这里按 v2 口径单独折一次。
    # 两套数据一起返回，页面切换时不用重新请求。
    ln_kept, ln_others, ln_unknown = _ln_sessions(sessions, owner)
    ln_plays, ln_low, ln_dup, ln_no_dan = _ln_play_rows(ln_kept)
    ln_top = ln_plays[:TOP_PLAYS]
    ln_avg = _average(ln_top)
    ln_count = len(ln_plays)
    ln = {
        "count": ln_count,
        "visible": ln_count >= LN_MIN_SESSIONS,
        "preliminary": LN_MIN_SESSIONS <= ln_count < LN_FULL_SESSIONS,
        "need": max(0, LN_MIN_SESSIONS - ln_count),
        "dan": (round(ln_avg, 2)
                if (ln_avg is not None and ln_count >= LN_MIN_SESSIONS) else None),
        "plays": ln_top,
        "lowAcc": ln_low,
        "duplicates": ln_dup,
        # 老记录只有 RC 段位、算不出面条折合的条数
        "noDan": ln_no_dan,
        "others": ln_others,
        "unknown": ln_unknown,
        "passAcc": LN_PASS_ACC,
        "minAcc": LN_MIN_ACC,
        "span": LN_SPAN,
        "minSessions": LN_MIN_SESSIONS,
        "fullSessions": LN_FULL_SESSIONS,
    }

    return {
        "ok": True,
        "count": count,
        "profile": profile or {},
        "owner": {"name": owner, "mine": len(kept), "others": others, "unknown": unknown},
        "filters": {"others": others, "unknown": unknown, "lnHybrid": ln_hybrid,
                    "lowAcc": low_acc, "duplicates": duplicates},
        "beyond": beyond,
        "overall": overall,
        "skills": _skill_blocks(plays),
        "target": target,
        "malody": malody,
        "ln": ln,
        "minSessions": MIN_SESSIONS,
        "fullSessions": FULL_SESSIONS,
        "minSkill": MIN_SKILL,
        "passAcc": PASS_ACC,
    }

def dan_label(value: float) -> str:
    """把连续段位写成玩家看得懂的名字。"""
    if value < -3:
        return "INTRO 以下"
    if value < 0:
        # -3 / -2 / -1 就是下拉里的 INTRO-1 / INTRO-2 / INTRO-3。
        # 早先一律叫 "INTRO"，选 INTRO-1 却显示 "INTRO 以下"，自相矛盾。
        if value < -2:
            return "INTRO-1"
        if value < -1:
            return "INTRO-2"
        return "INTRO-3"
    if value < 11:
        # 10.0 也归这里：REFORM 到 10 段为止，11 才是 Alpha。
        # 早先用 < 10，10.0 掉进 extra 分支后成了 "10.0 段"，和 9.96 的 "9 段" 对不上。
        nearest = round(value)
        if abs(value - nearest) < 0.05:
            return "%d 段" % nearest
        return "%.1f 段" % value
    extra = {11: "Alpha", 12: "Beta", 13: "Gamma", 14: "Delta",
             15: "Epsilon", 16: "Zeta", 17: "Eta"}
    for k, name in extra.items():
        # 只有真正贴着这一档才报名字；差得多就老实给"11.3 段"这种读数，
        # 免得把 Gamma 和 Delta 之间的值也含糊地叫成某一个
        if abs(value - k) < 0.3:
            return name
    if value > 17:
        # REFORM 的段位到 Eta 为止；再高的读数只能这么说，不能编一个新段位出来
        return "Eta 以上"
    return "%.1f 段" % value
