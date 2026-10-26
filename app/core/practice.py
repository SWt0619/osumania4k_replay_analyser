"""把报告里的「练这一段」做成一张能直接拖进 osu! 的练习谱面。

裁切点不取建议区间的原值，而是向外扩到最近的整八拍：一段 1:23.4–2:05.1 直接切会在
半拍上开始，前奏听不出重音，练的时候找不到落点。八拍是 4/4 里最自然的呼吸单位，
多扩一点点没有副作用。

0.9 倍速是**烘进谱面**的：物件时间和红线的 beatLength 一起乘 1/rate，BPM 显示成原来的
0.9 倍，八分音符的间隔不变。玩家用原速打开就是慢速版，不依赖游戏里的 HT 档位。
绿线的 beatLength 是负值、表示流速，不能跟着缩。

音频不在这里处理：这个项目没有解码能力，交给浏览器的 Web Audio（见 app/web/js/practice.js）。
这里只负责算出「音频该从哪一秒取到哪一秒」。
"""

from __future__ import annotations

import math
import re

BEATS_PER_CUT = 8
# 长条被裁到只剩几毫秒时，在游戏里等于瞬发；按普通单点处理更符合直觉
MIN_HOLD_MS = 20.0
DEFAULT_BEAT_MS = 500.0          # 找不到红线时的兜底：120 BPM
_ILLEGAL = re.compile(r'[\\/:*?"<>|]')


def _parse_timing_point(line: str) -> dict | None:
    parts = line.split(",")
    if len(parts) < 2:
        return None
    try:
        time = float(parts[0])
        beat = float(parts[1])
    except ValueError:
        return None
    uninherited = True
    if len(parts) >= 7 and parts[6].strip() in ("0", "false", "False"):
        uninherited = False
    return {"time": time, "beat": beat, "uninherited": uninherited, "parts": parts}


def _parse_object(line: str) -> dict | None:
    parts = line.split(",")
    if len(parts) < 5:
        return None
    try:
        time = float(parts[2])
        kind = int(parts[3])
    except ValueError:
        return None
    end = None
    if kind & 128 and len(parts) >= 6:
        head = parts[5].split(":", 1)[0]
        try:
            end = float(head)
        except ValueError:
            end = None
    return {"time": time, "kind": kind, "end": end, "parts": parts}


def _grid_base(tps: list[dict], t: float) -> dict:
    """覆盖 t 的那条红线（uninherited=1）；t 在第一条之前时用第一条。"""
    base = None
    for tp in tps:
        if tp["uninherited"] and tp["time"] <= t:
            base = tp
    if base is None:
        for tp in tps:
            if tp["uninherited"]:
                base = tp
                break
    return base or {"time": 0.0, "beat": DEFAULT_BEAT_MS}


def _section(lines: list[str], name: str) -> tuple[int, int] | None:
    """返回某个段落的 [起, 止) 行号。段落名带方括号，如 [HitObjects]。"""
    start = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if start is not None:
                return start, i
            if s == name:
                start = i + 1
    if start is None:
        return None
    return start, len(lines)


def _fmt_ms(v: float) -> str:
    return str(int(round(v)))


def safe_name(text: str, limit: int = 60) -> str:
    s = _ILLEGAL.sub("_", str(text or "")).strip().strip(".")
    return (s or "practice")[:limit]


def rate_label(rate: float) -> str:
    """1.1 → "Rate1.1"，0.9 → "Slice0.9"。难度名、音频名、包名都从这里来。

    加速版与降速版分开命名：两者都是「改过速度的同一张图」，但一个是为了把速度
    余量顶上去、整曲不裁段，一个是为了拆段慢练，混用一个前缀在选歌界面里分不清。
    """
    value = float(rate)
    return ("Rate" if value > 1.0 else "Slice") + ("%g" % value)


def cut(
    osu_text: str,
    t0: float,
    t1: float,
    rate: float = 0.9,
    pad_ms: int = 3000,
    tail_ms: int = 2000,
    full: bool = False,
) -> dict:
    """把 [t0, t1] 扩到整八拍、按 rate 缩放，返回新谱面文本与时间信息。

    返回体里的 audioStart/audioEnd 是**原曲**上的取音区间（毫秒），前端按
    `(audioEnd - audioStart) / rate` 渲染，再在最前面补 pad_ms 的静音。
    """
    rate = float(rate)
    # 上限放到 2.0：成绩够好时推荐的是提速练习（1.05 ~ 1.1），不是降速
    if not 0.1 <= rate <= 2.0:
        raise ValueError("倍率超出范围（0.1~2.0）")
    label = rate_label(rate)
    t0, t1 = float(t0), float(t1)
    if t1 < t0:
        t0, t1 = t1, t0
    if not full and t1 - t0 < 1:
        raise ValueError("这一段太短，没有可裁切的内容")

    lines = osu_text.splitlines()
    tp_range = _section(lines, "[TimingPoints]")
    obj_range = _section(lines, "[HitObjects]")
    if not tp_range or not obj_range:
        raise ValueError("这张谱面缺少 [TimingPoints] 或 [HitObjects]，无法裁切")

    tps = [p for p in (_parse_timing_point(l) for l in lines[tp_range[0]:tp_range[1]]) if p]
    objs = [o for o in (_parse_object(l) for l in lines[obj_range[0]:obj_range[1]]) if o]
    if not tps or not objs:
        raise ValueError("这张谱面没有可用的节奏线或物件")

    # ---- 1. 对齐到整八拍 ------------------------------------------------
    base = _grid_base(tps, t0)
    beat = base["beat"] if base["beat"] > 0 else DEFAULT_BEAT_MS
    step = beat * BEATS_PER_CUT
    # 注意终点必须用 ceil：写成 -(-x // step) 在 Python 里等于 x // step（向下取整），
    # 段尾会落在网格线之前，切出来的长度就不是整八拍的倍数。
    if full:
        # 全曲模式不裁：从 0 到最后一个物件，也不做八拍对齐（没有段要接）
        cut_start = 0.0
        cut_end = max((o["end"] if o["end"] is not None else o["time"]) for o in objs)
    else:
        cut_start = base["time"] + math.floor((t0 - base["time"]) / step) * step
        cut_end = base["time"] + math.ceil((t1 - base["time"]) / step) * step
        # 先夹到 0 以上，再保证至少一段：顺序反了会把开头那一段压短
        cut_start = max(0.0, cut_start)
        if cut_end - cut_start < step:
            cut_end = cut_start + step
        # 段尾不能无限往后：t1 传成 1e8 时对齐会把 cutEnd 拉到荒谬的位置，
        # 音频那头根本取不到。夹到最后一个物件之后两拍，等于"到曲尾为止"。
        last_end = max((o["end"] if o["end"] is not None else o["time"]) for o in objs)
        cut_end = min(cut_end, last_end + step * 2)

    # ---- 2. 时间映射 ----------------------------------------------------
    def conv(t: float) -> float:
        return (t - cut_start) / rate + pad_ms

    # ---- 3. 节奏线 ------------------------------------------------------
    # 区间之前的那条红线（和它后面的绿线）要复制到前奏的 3000ms 处，
    # 否则段首那几秒没有 BPM / 流速，物件会按默认值显示。
    head_base = None
    head_sv = None
    for tp in tps:
        if tp["time"] > cut_start:
            continue
        if tp["uninherited"]:
            head_base = tp
            head_sv = None
        else:
            head_sv = tp

    new_tps: list[str] = []
    if head_base is not None:
        parts = list(head_base["parts"])
        parts[0] = _fmt_ms(pad_ms)
        parts[1] = "%.10f" % (head_base["beat"] / rate)
        new_tps.append(",".join(parts))
    if head_sv is not None:
        parts = list(head_sv["parts"])
        parts[0] = _fmt_ms(pad_ms)
        new_tps.append(",".join(parts))

    for tp in tps:
        if tp["time"] < cut_start or tp["time"] > cut_end:
            continue
        parts = list(tp["parts"])
        parts[0] = _fmt_ms(conv(tp["time"]))
        if tp["uninherited"] and tp["beat"] > 0:
            parts[1] = "%.10f" % (tp["beat"] / rate)
        new_tps.append(",".join(parts))

    if not new_tps:
        raise ValueError("这一段里没有任何节奏线，无法生成练习谱面")

    # ---- 4. 物件 --------------------------------------------------------
    kept: list[tuple[float, str]] = []
    seen: set[tuple[str, int]] = set()
    for obj in objs:
        start = obj["time"]
        end = obj["end"] if obj["end"] is not None else start
        if end < cut_start or start > cut_end:
            continue
        new_start = conv(max(start, cut_start))
        new_end = conv(min(end, cut_end))
        parts = list(obj["parts"])
        is_hold = bool(obj["kind"] & 128) and obj["end"] is not None
        if is_hold and new_end - new_start < MIN_HOLD_MS:
            # 裁到几乎没有长度的长条按单点处理，保留长条会在下落时闪一下。
            # 第 6 段是 "结束时间:采样"，去掉结束时间后剩下的采样信息要留着。
            parts[3] = str(obj["kind"] & ~128)
            rest = parts[5].split(":", 1)
            parts[5] = rest[1] if len(rest) > 1 else "0:0:0:0:"
            is_hold = False
        parts[2] = _fmt_ms(new_start)
        if is_hold:
            rest = parts[5].split(":", 1)
            suffix = (":" + rest[1]) if len(rest) > 1 else ""
            parts[5] = _fmt_ms(new_end) + suffix
        key = (parts[0], int(round(new_start)))
        if key in seen:
            continue
        seen.add(key)
        kept.append((new_start, ",".join(parts)))

    if not kept:
        raise ValueError("这一段里没有任何物件，无法生成练习谱面")
    kept.sort(key=lambda kv: kv[0])

    # ---- 5. 其它段落 ----------------------------------------------------
    # 曲名一个字都不动：生成的难度要和原图并进同一个图包，改了 Title 就会被
    # 拆成另一张图。只把 Version 换成 Slice0.9 / Rate1.1，音频换成本次生成的 wav。
    audio_name = "%s-%g.wav" % ("rate" if rate > 1.0 else "slice", rate)
    title = ""
    artist = ""
    meta_range = _section(lines, "[Metadata]")
    general_range = _section(lines, "[General]")
    if meta_range:
        for i in range(*meta_range):
            m = re.match(r"^\s*(Title|Artist)\s*:\s*(.*)$", lines[i])
            if m:
                if m.group(1) == "Title":
                    title = m.group(2).strip()
                else:
                    artist = m.group(2).strip()

    out: list[str] = []
    skip_ranges = [tp_range, obj_range]
    cur_section = ""
    for i, line in enumerate(lines):
        if any(a <= i < b for a, b in skip_ranges):
            continue
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur_section = s
        if general_range and general_range[0] <= i < general_range[1]:
            if s.startswith("AudioFilename:"):
                line = "AudioFilename: " + audio_name
            elif s.startswith("AudioLeadIn:"):
                line = "AudioLeadIn: 0"
            elif s.startswith("PreviewTime:"):
                line = "PreviewTime: -1"
        if meta_range and meta_range[0] <= i < meta_range[1]:
            if s.startswith("BeatmapID:") or s.startswith("BeatmapSetID:"):
                # 原图的 ID 留着会和原难度撞车，删掉让 osu 当成新难度导入
                continue
            if s.startswith("Version:"):
                line = "Version:" + label
        if cur_section == "[TimingPoints]":
            out.append(line)
            out.extend(new_tps)
            continue
        if cur_section == "[HitObjects]":
            out.append(line)
            out.extend(text for _t, text in kept)
            continue
        if cur_section in ("[Events]", "[Storyboard]"):
            # 视频和故事板跟着原曲时间轴，裁切后一定错位，直接丢掉；
            # 背景图（0,0,"bg.jpg"）没有时间轴，留着好看
            if s.startswith(("Video,", "Sprite,", "Animation,", "Sample,")):
                continue
        out.append(line)

    text = "\n".join(out) + "\n"
    return {
        "text": text,
        "audioName": audio_name,
        "audioStart": round(cut_start, 3),
        # 尾部多留一点，免得最后一个音符后面立刻静音；前端拿到真实音频长度后自己夹
        "audioEnd": round(cut_end + tail_ms, 3),
        "cutStart": round(cut_start, 3),
        "cutEnd": round(cut_end, 3),
        "rate": rate,
        "rateText": "%.2f" % rate,
        "label": label,
        "padMs": pad_ms,
        "tailMs": tail_ms,
        "bpm": round(60000.0 / (beat / rate), 2) if beat > 0 else None,
        "sourceBpm": round(60000.0 / beat, 2) if beat > 0 else None,
        "beats": BEATS_PER_CUT,
        "objects": len(kept),
        "fileName": safe_name("%s - %s [%s]" % (artist, title, label)) + ".osz",
    }
