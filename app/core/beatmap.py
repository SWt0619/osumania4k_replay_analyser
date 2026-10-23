"""osu! beatmap (.osu) 解析 —— 只解析分析需要的部分。

设计目标：
  * 容错：真实谱面文件里存在 BOM、大小写不一的 section、缺字段、多余字段。
  * 只读：不改文件。
  * 可测试：纯函数，无全局状态。

mania 相关事实（来自 osu! 官方文件格式文档与 lazer 源码）：
  * [Difficulty] CircleSize == 键数（4K/7K/10K ... 可能是 4.0 这样的浮点）。
  * [HitObjects] 列号 = floor(x * keys / 512)，越界要夹取。
  * type 的第 7 位（128）代表 mania 的长按（hold），此时 objectParams 为 "endTime:hitSample"。
"""

from __future__ import annotations

import math

import hashlib
import os
import re
from dataclasses import dataclass, field
from urllib.parse import unquote


# --------------------------------------------------------------------------- 数据结构


def long_path(path: str) -> str:
    """Windows 上超过 260 字符的路径要用 \\\\?\\ 前缀才能访问。

    真实谱面库里确实存在超长路径（日文曲名 + 难度名），不加前缀会 FileNotFoundError。
    """
    if os.name != "nt":
        return path
    if path.startswith("\\\\?\\"):
        return path
    if len(path) < 240:
        return path
    try:
        return "\\\\?\\" + os.path.abspath(path)
    except OSError:
        return path


def _isfile(path: str) -> bool:
    return os.path.isfile(long_path(path))


def _listdir(path: str) -> list[str]:
    try:
        return os.listdir(long_path(path))
    except OSError:
        return []


# 同捆音频可能的扩展名。谱面里写的是 .mp3，实际文件常常被换成 .ogg（省体积）
# 或 lazer 常见的 .oga / .opus，所以必须按扩展名集合去找，而不是照抄 AudioFilename。
AUDIO_EXTS = (".mp3", ".ogg", ".oga", ".opus", ".wav", ".flac", ".m4a", ".aac")


def resolve_audio(base: str, declared_name: str = "") -> str:
    """在一个目录里找谱面的同捆音频，找不到返回空串。

    顺序：AudioFilename 原样 → URL 解码 → 同名换扩展名 → 目录里任意音频。
    真实库里 AudioFilename 写 audio.mp3 但文件其实叫 audio.ogg 的情况很常见，
    只按字面找会漏掉一大半。
    """
    if not base:
        return ""
    listing = _listdir(base)
    if not listing:
        return ""

    def hit(fname: str) -> str:
        p = os.path.join(base, fname)
        return p if _isfile(p) else ""

    lower = {f.lower(): f for f in listing}
    if declared_name:
        for cand in (declared_name, unquote(declared_name)):
            bare = os.path.basename(cand.replace("/", os.sep))
            got = lower.get(bare.lower())
            if got:
                p = hit(got)
                if p:
                    return p
        # 同名、不同扩展名
        stem = os.path.splitext(os.path.basename(declared_name.replace("/", os.sep)))[0].lower()
        if stem:
            for f in sorted(listing):
                s, ext = os.path.splitext(f)
                if s.lower() == stem and ext.lower() in AUDIO_EXTS:
                    p = hit(f)
                    if p:
                        return p

    # 目录里就一个音频：多半就是它；有多个时取名字最短的（最像主音频，而不是 hitSound 之类的）
    found = [f for f in listing if os.path.splitext(f)[1].lower() in AUDIO_EXTS]
    if not found:
        return ""
    found.sort(key=lambda f: (len(f), f))
    return hit(found[0])


@dataclass
class TimingPoint:
    time: float
    beat_length: float          # uninherited: 每拍毫秒；inherited: 负数（-100/SV）
    meter: int = 4
    uninherited: bool = True

    @property
    def bpm(self) -> float | None:
        if self.uninherited and self.beat_length > 0:
            return 60000.0 / self.beat_length
        return None


@dataclass
class HitObject:
    """mania 的一个物件。

    time      : 判定时间（毫秒，float 以保留精度，谱面本身一般是整数）
    column    : 列号，0 起
    end_time  : 长按的结束时间；单点为 None
    """

    index: int
    time: float
    column: int
    end_time: float | None = None
    new_combo: bool = False

    @property
    def is_hold(self) -> bool:
        return self.end_time is not None and self.end_time > self.time


@dataclass
class Beatmap:
    path: str
    md5: str
    keys: int
    od: float
    hp: float
    ar: float
    objects: list[HitObject]
    timing_points: list[TimingPoint] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    general: dict = field(default_factory=dict)
    events: list = field(default_factory=list)

    # ---- 便捷属性 -------------------------------------------------------
    @property
    def title(self) -> str:
        m = self.meta
        return m.get("TitleUnicode") or m.get("Title") or "(未知曲目)"

    @property
    def artist(self) -> str:
        m = self.meta
        return m.get("ArtistUnicode") or m.get("Artist") or "(未知艺术家)"

    @property
    def difficulty_name(self) -> str:
        return self.meta.get("Version") or "(未知难度)"

    @property
    def audio_filename(self) -> str:
        return self.general.get("AudioFilename", "") or ""

    @property
    def audio_path(self) -> str:
        """音频的绝对路径。找不到文件时返回空串。

        只在谱面自己的目录里找（"同捆"）。lazer 的 files 存储把每个文件单独按哈希存，
        目录里没有同捆音频 —— 那种情况要交给曲库的跨谱面兜底（Library.find_audio）。
        """
        return resolve_audio(os.path.dirname(self.path), self.audio_filename)

    @property
    def background_path(self) -> str:
        for ev in self.events:
            if len(ev) >= 3 and ev[0] == "0" and ev[1] == "0":
                name = (ev[2] or "").strip()
                if not name:
                    continue
                if not name.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
                    continue
                p = os.path.join(os.path.dirname(self.path), name.replace("/", os.sep))
                if _isfile(p):
                    return p
        return ""

    # ---- 时间轴 / 密度 --------------------------------------------------
    @property
    def first_time(self) -> float:
        return self.objects[0].time if self.objects else 0.0

    @property
    def last_time(self) -> float:
        """考虑长按结束时间的结束时刻。"""
        if not self.objects:
            return 0.0
        return max(o.end_time if o.end_time is not None else o.time for o in self.objects)

    @property
    def duration(self) -> float:
        return max(0.0, self.last_time - self.first_time)

    def primary_bpm(self) -> float:
        uninherited = [tp for tp in self.timing_points if tp.uninherited and tp.beat_length > 0]
        if not uninherited:
            return 0.0
        # 取持续时间最长的那一档更合理，但为免过度计算，取第一个（绝大多数谱面只有一个）
        return uninherited[0].bpm or 0.0

    def bpm_range(self) -> tuple[float, float]:
        bpms = sorted({round(tp.bpm, 3) for tp in self.timing_points if tp.uninherited and tp.bpm})
        if not bpms:
            return (0.0, 0.0)
        return (bpms[0], bpms[-1])

    def column_counts(self) -> list[int]:
        counts = [0] * self.keys
        for o in self.objects:
            if 0 <= o.column < self.keys:
                counts[o.column] += 1
        return counts

    def note_count(self) -> int:
        return len(self.objects)


# --------------------------------------------------------------------------- 解析


def compute_md5_variants(path: str) -> dict[str, str]:
    """返回若干种"谱面 MD5"候选算法。

    osu! 的谱面哈希在不同实现里有细微差别（原始字节 / 换行归一化），
    这里全部算出来，匹配回放时逐个比对即可，避免猜错。
    """
    with open(long_path(path), "rb") as f:
        raw = f.read()
    out = {"raw": hashlib.md5(raw).hexdigest()}
    # 统一换行到 \n
    norm_lf = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    out["lf"] = hashlib.md5(norm_lf).hexdigest()
    # 统一换行到 \r\n
    norm_crlf = norm_lf.replace(b"\n", b"\r\n")
    out["crlf"] = hashlib.md5(norm_crlf).hexdigest()
    return out


_KV_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*:\s*(.*?)\s*$")


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp936", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _parse_events_line(line: str) -> list[str] | None:
    """把 [Events] 的一行切成字段，尊重双引号内的逗号。"""
    if ":" in line and '"' not in line and "," not in line:
        return None
    fields: list[str] = []
    buf: list[str] = []
    in_quote = False
    for ch in line:
        if ch == '"':
            in_quote = not in_quote
            continue
        if ch == "," and not in_quote:
            fields.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    fields.append("".join(buf).strip())
    if len(fields) < 2:
        return None
    return fields


def _parse_timing_point(line: str) -> TimingPoint | None:
    """`time,beatLength,meter,...` —— 字段缺失或不是数字时返回 None（真实谱面里确实有）。"""
    parts = line.split(",")
    if len(parts) < 2:
        return None
    try:
        time = float(parts[0])
        beat_length = float(parts[1])
    except ValueError:
        return None
    meter = 4
    if len(parts) > 2:
        try:
            meter = int(float(parts[2]))
        except ValueError:
            pass
    # 第 7 个字段是 1/0：0 表示继承点（SV），其余为普通计时点
    uninherited = True
    if len(parts) > 6:
        uninherited = parts[6].strip() not in ("0", "")
    return TimingPoint(time, beat_length, meter, uninherited)


def _parse_hit_object(line: str, index: int) -> tuple[HitObject, float] | None:
    """`x,y,time,type,...` —— 返回（物件, 原始 x）。列号要等 keys 读出来才能换算。"""
    parts = line.split(",")
    if len(parts) < 4:
        return None
    try:
        x = float(parts[0])
        time = float(parts[2])
        type_bits = int(parts[3])
    except ValueError:
        return None
    end_time = None
    if type_bits & 128 and len(parts) >= 6:   # 128 = mania 长按，第 6 段是 endTime:hitSample
        tail = parts[5].split(":")
        try:
            end_time = float(tail[0])
        except (ValueError, IndexError):
            end_time = None
    obj = HitObject(
        index=index,
        time=time,
        column=-1,  # 需 keys 才能换算，下面统一填
        end_time=end_time,
        new_combo=bool(type_bits & 4),
    )
    return obj, x


def _scan_sections(
    text: str,
) -> tuple[dict[str, str], dict[str, str], dict[str, str], list[TimingPoint], list[list[str]], list[HitObject], list[float]]:
    """逐行读 .osu，按 section 分发给对应的行解析器。

    返回（General, Metadata, Difficulty, 计时点, 事件, 物件, 原始 x）。
    列号要等读完整张图（拿到 CircleSize）才能换算，所以这里先把 x 原样带出来。
    未知 section（Colours / Editor 之外的扩展段）一律跳过：真实谱面里出现新段
    不该让整张图解析失败。
    """
    section = ""
    general: dict[str, str] = {}
    meta: dict[str, str] = {}
    difficulty: dict[str, str] = {}
    timing_points: list[TimingPoint] = []
    events: list[list[str]] = []
    objects: list[HitObject] = []
    pending_x: list[float] = []   # 与 objects 一一对应的原始 x

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue

        if section in ("General", "Metadata", "Difficulty", "Editor"):
            m = _KV_RE.match(line)
            if not m:
                continue
            key, value = m.group(1), m.group(2)
            if section == "General":
                general[key] = value
            elif section == "Metadata":
                meta[key] = value
            elif section == "Difficulty":
                difficulty[key] = value
            continue

        if section == "TimingPoints":
            tp = _parse_timing_point(line)
            if tp is not None:
                timing_points.append(tp)
            continue

        if section == "Events":
            parsed = _parse_events_line(line)
            if parsed:
                events.append(parsed)
            continue

        if section == "HitObjects":
            got = _parse_hit_object(line, len(objects))
            if got is None:
                continue
            objects.append(got[0])
            pending_x.append(got[1])
            continue

    return general, meta, difficulty, timing_points, events, objects, pending_x


def _keys_from(difficulty: dict[str, str]) -> int:
    """CircleSize 就是键数（可能是 "4.0" 这类浮点）；读不到时按 4K 处理。"""
    try:
        keys = int(round(float(difficulty.get("CircleSize", "4"))))
    except (TypeError, ValueError):
        keys = 4
    return max(1, min(18, keys))


def _assign_columns(objects: list[HitObject], pending_x: list[float], keys: int) -> None:
    """列号 = floor(x * keys / 512)，越界夹取（改过键数的老图会越界）。"""
    for obj, x in zip(objects, pending_x):
        col = int((x * keys) // 512)
        obj.column = 0 if col < 0 else (keys - 1 if col >= keys else col)


def _float_field(fields: dict[str, str], key: str, default: float) -> float:
    """[Difficulty] 里的数值字段：缺失或写成非数字时退回默认值。"""
    try:
        return float(fields.get(key, default))
    except (TypeError, ValueError):
        return default


def _file_md5(path: str) -> str:
    """谱面文件的原始字节 MD5（回放头里记的就是它）。"""
    if not path or not os.path.isfile(path):
        return ""
    try:
        return compute_md5_variants(path)["raw"]
    except OSError:
        return ""


def parse_osu_text(text: str, path: str = "") -> Beatmap:
    general, meta, difficulty, timing_points, events, objects, pending_x = _scan_sections(text)

    keys = _keys_from(difficulty)
    _assign_columns(objects, pending_x, keys)

    objects.sort(key=lambda o: (o.time, o.column))
    for i, obj in enumerate(objects):
        obj.index = i

    return Beatmap(
        path=path,
        md5=_file_md5(path),
        keys=keys,
        od=_float_field(difficulty, "OverallDifficulty", 5.0),
        hp=_float_field(difficulty, "HPDrainRate", 5.0),
        ar=_float_field(difficulty, "ApproachRate", 5.0),
        objects=objects,
        timing_points=sorted(timing_points, key=lambda tp: tp.time),
        meta=meta,
        general=general,
        events=events,
    )


def parse_osu_file(path: str) -> Beatmap:
    with open(long_path(path), "rb") as f:
        data = f.read()
    return parse_osu_text(_decode(data), path)


# --------------------------------------------------------------------------- 判定窗口


def judgement_windows(od: float, score_v2: bool = False) -> dict[str, float]:
    """mania 六档判定窗口（半窗，毫秒）。来自官方 wiki「osu!mania judgement system」：

        PERFECT  16            （固定，不随 OD 变）
        GREAT    64 - 3×OD
        GOOD     97 - 3×OD
        OK       127 - 3×OD
        MEH      151 - 3×OD
        MISS     188 - 3×OD

    ScoreV2 只改 PERFECT：OD ≤ 5 时 22.4 - 0.6×OD，OD ≥ 5 时 24.9 - 1.1×OD，
    其余四档照旧。尾判窗口再额外放大 1.5 倍（见 match 里的尾判）。

    两条容易被忽略的细节（wiki 明写）：
      * 「最大打击误差**向下取整**」——OD 8.5 时 GREAT 是 38.5 → 38，不是 38.5；
      * 「打击误差**四舍五入**」——见 judge_offset()。
    这两条决定了边界上那几个音的判定归属，也是准确率能不能和游戏对齐的关键。
    """
    od = max(0.0, min(10.0, od))
    perfect = 16.0
    if score_v2:
        raw = 22.4 - 0.6 * od if od <= 5 else 24.9 - 1.1 * od
        # 别直接 floor(raw)：OD 9 时 raw 在二进制浮点里是 14.999999999999998，
        # floor 成 14 会整整少给 1ms，v2 的 320 判定就比游戏里严一档。
        perfect = float(math.floor(raw + 1e-9))
    return {
        "perfect": perfect,
        "great": float(math.floor(64.0 - 3.0 * od + 1e-9)),
        "good": float(math.floor(97.0 - 3.0 * od + 1e-9)),
        "ok": float(math.floor(127.0 - 3.0 * od + 1e-9)),
        "meh": float(math.floor(151.0 - 3.0 * od + 1e-9)),
        "miss": float(math.floor(188.0 - 3.0 * od + 1e-9)),
    }


def judge_with_windows(offset_ms: float, windows: dict[str, float], scale: float = 1.0) -> str:
    """按给定的窗口判档。scale 用来放大尾判窗口（ScoreV2 是 1.5）。"""
    d = round(abs(offset_ms))
    for name in ("perfect", "great", "good", "ok", "meh"):
        if d <= windows[name] * scale:
            return name
    return "miss"


JUDGEMENT_LABEL = {
    "perfect": "MAX",
    "great": "300",
    "good": "200",
    "ok": "100",
    "meh": "50",
    "miss": "MISS",
}


def judge_offset(offset_ms: float, od: float) -> str:
    """按误差（ms，负数=偏早）判档。误差四舍五入后再和窗口比（官方 wiki 口径）。"""
    d = round(abs(offset_ms))
    w = judgement_windows(od)
    if d <= w["perfect"]:
        return "perfect"
    if d <= w["great"]:
        return "great"
    if d <= w["good"]:
        return "good"
    if d <= w["ok"]:
        return "ok"
    if d <= w["meh"]:
        return "meh"
    return "miss"
