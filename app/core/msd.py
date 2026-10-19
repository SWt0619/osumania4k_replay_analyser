"""MinaCalc（Etterna 的 MSD 难度计算器）封装。

为什么要它：osu! 的 star rating 对 4K 谱面的"键型分布"几乎没有区分度，
而 MSD 会给 7 个维度的评分（stream / jumpstream / handstream / stamina /
jack / chordjack / technical），正好能画成雷达图，也能量化"这张图难在哪"。

实现方式是 ctypes 调 kangalioo/minacalc-standalone 的 C API
（MinaCalc 0.72.1 的独立构建，MIT）。DLL 放在 vendor/minacalc/ 下，
来源：Nimue-lua/MinaCalc-soundsphere 的 minacalc/bin/win64/libminacalc.dll。

注意：这里的 C API 只有 `{notes 位掩码, rowTime 秒}` 两个字段（没有 rowType），
所以长按**只按头部**参与计算 —— 与 Etterna 导出 osu 转换谱时的做法一致。
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from dataclasses import dataclass

from .beatmap import Beatmap

_HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DLL_PATH = os.path.join(_HERE, "vendor", "minacalc", "libminacalc.dll")

# Etterna 界面上的 MSD 是在 93% 上算的（score_goal），改这个值难度会整体平移
SCORE_GOAL = 0.93


class _Ssr(ctypes.Structure):
    _fields_ = [
        ("overall", ctypes.c_float),
        ("stream", ctypes.c_float),
        ("jumpstream", ctypes.c_float),
        ("handstream", ctypes.c_float),
        ("stamina", ctypes.c_float),
        ("jackspeed", ctypes.c_float),
        ("chordjack", ctypes.c_float),
        ("technical", ctypes.c_float),
    ]


class _NoteInfo(ctypes.Structure):
    _fields_ = [("notes", ctypes.c_uint), ("rowTime", ctypes.c_float)]


# 只列 key，中文名交给前端 charts.js 维护：文案属于展示层，放后端会变成两份要同步的字表
SKILL_KEYS = ("overall", "stream", "jumpstream", "handstream", "stamina", "jackspeed", "chordjack", "technical")

_LOCK = threading.Lock()
_LIB = None
_LIB_ERROR = ""
_CALC = None


def _load():
    """懒加载 DLL。失败不抛异常，返回 None 并记下原因（页面上要如实显示"不可用"）。"""
    global _LIB, _LIB_ERROR, _CALC
    if _LIB is not None or _LIB_ERROR:
        return _LIB
    if sys.platform != "win32":
        _LIB_ERROR = "MinaCalc 目前只在 Windows 上提供构建"
        return None
    if not os.path.isfile(DLL_PATH):
        _LIB_ERROR = "缺少 vendor/minacalc/libminacalc.dll"
        return None
    try:
        lib = ctypes.CDLL(DLL_PATH)
        lib.create_calc.restype = ctypes.c_void_p
        lib.destroy_calc.argtypes = [ctypes.c_void_p]
        lib.calc_ssr.restype = _Ssr
        # 注意签名末尾的 keycount：这个构建的 API 比 kangalioo 原版多一个参数，
        # 漏掉它 calc 会返回全 0（不报错，很容易误判成"算出来了"）。
        lib.calc_ssr.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_uint,
        ]
        _LIB = lib
        _CALC = lib.create_calc()
    except OSError as exc:  # 缺少 VC 运行库等
        _LIB_ERROR = "加载 libminacalc.dll 失败：%s" % exc
        _LIB = None
    return _LIB


def available() -> tuple[bool, str]:
    lib = _load()
    return (lib is not None, _LIB_ERROR)


def _rows_from_beatmap(bm: Beatmap) -> list[tuple[int, float]]:
    """把谱面折成 (列位掩码, 时间秒) 序列。同一时刻的多押合并成一行。"""
    by_time: dict[float, int] = {}
    for obj in bm.objects:
        t = round(obj.time, 3)
        if t < 0:
            continue
        by_time[t] = by_time.get(t, 0) | (1 << max(0, min(31, obj.column)))
    # 必须**按时间**排！直接 sorted((mask, t)) 是按掩码排的，会让时间倒流，
    # MinaCalc 遇到负间隔会静默返回全 0（不报错，极易误判成"算出来了"）。
    ordered = sorted(((t / 1000.0, mask) for t, mask in by_time.items()), key=lambda x: x[0])
    return [(mask, t) for t, mask in ordered]


@dataclass
class MsdResult:
    overall: float
    skillsets: dict[str, float]
    rows: int
    rate: float = 1.0

    def as_dict(self) -> dict:
        return {
            "overall": round(self.overall, 2),
            "skillsets": {k: round(v, 2) for k, v in self.skillsets.items()},
            "rows": self.rows,
            "rate": self.rate,
        }


def calc(beatmap: Beatmap, rate: float = 1.0) -> MsdResult | None:
    """算一张谱的 MSD（rate=1.0 即原速）。DLL 不可用时返回 None。"""
    lib = _load()
    if lib is None:
        return None
    rows = _rows_from_beatmap(beatmap)
    if not rows:
        return None
    arr = (_NoteInfo * len(rows))()
    for i, (mask, t) in enumerate(rows):
        arr[i].notes = mask
        arr[i].rowTime = t
    with _LOCK:
        ssr = lib.calc_ssr(_CALC, arr, len(rows), float(rate), float(SCORE_GOAL), int(beatmap.keys or 4))
    skills = {k: float(getattr(ssr, k)) for k in SKILL_KEYS if k != "overall"}
    return MsdResult(overall=float(ssr.overall), skillsets=skills, rows=len(rows), rate=rate)
