"""osu! .osr 回放解析。

字段顺序与语义来自 lazer 的 `LegacyScoreDecoder`（对照 ppy/osu 源码里的同名类即可核对）：
  byte mode(0=osu 1=taiko 2=catch 3=mania) / int32 version / string beatmapMd5 /
  string player / string replayMd5 / u16 x6 (300,100,50,geki,katu,miss) /
  int32 score / u16 maxCombo / byte perfect / int32 mods / string hpGraph /
  int64 timestamp(.NET ticks) / byte[] compressedReplay / [int64 onlineId] /
  [byte[] compressedScoreInfo，version>=30000001 才有]

回放数据是 **LZMA alone 格式**（13 字节头：5 字节属性 + 8 字节解压长度），解压后是
`-12345|x|y|button,` 这样的逗号分隔帧，**时间是增量 delta，要逐帧累加**。

mania 的关键点（源码 `ManiaReplayFrame.FromLegacy`）：**按下的键位编码在 x 字段的低 20 位**，
bit0 = 第 1 列 …… 与第 4 段的按键状态无关。y 字段无意义。
"""

from __future__ import annotations

import json
import lzma
import math
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .beatmap import long_path


class OsrError(Exception):
    """回放文件无法解析。"""


# --------------------------------------------------------------------------- 结构


@dataclass
class RawFrame:
    """回放里的原始帧（已还原为绝对时间）。"""

    time: float          # 毫秒（谱面时间轴）
    mask: int            # 键位位掩码，bit i = 第 i 列
    button: int = 0      # 第 4 段原值（mania 下无意义，仅留档）


@dataclass
class Replay:
    path: str
    mode: int
    version: int
    beatmap_md5: str
    player: str
    replay_md5: str
    count300: int
    count100: int
    count50: int
    geki: int
    katu: int
    count_miss: int
    score: int
    max_combo: int
    perfect: bool
    mods: int
    timestamp: datetime | None
    frames: list[RawFrame] = field(default_factory=list)
    online_id: int = 0
    truncated: bool = False             # 压缩块被截断/写坏，只解出前面一部分
    has_score_info: bool = False
    score_info: dict | None = None      # lazer 导出时附带的 JSON（判定统计等）

    # ---- 便捷 -----------------------------------------------------------
    @property
    def mod_names(self) -> list[str]:
        return decode_mods(self.mods)

    @property
    def rate(self) -> float:
        """速度倍率（DT/NC=1.5，HT=0.75）。

        回放帧时间与谱面时间都处在**谱面时间轴**上（audio clock），所以配对时
        **不需要**按倍率换算。它还有两个真实用途：UR 要按官方口径除以倍率
        （见 analyze._summary / _section_stats），以及页面上的「等效难度」说明。
        """
        if self.mods & MOD_DT or self.mods & MOD_NC:
            return 1.5
        if self.mods & MOD_HT:
            return 0.75
        return 1.0

    @property
    def header_judgements(self) -> dict[str, int]:
        """回放头里记录的判定数（用来校验我们自己的重算结果）。

        mania 下 lazer 的 SetCount300 会把 geki(MAX) 合并进 count300，
        所以这里给出原始值与"合计"两种口径。头里计数全为 0 时退化到 lazer 的 JSON 统计。
        """
        # mania 的 legacy 六元组顺序是 geki=MAX、count300=300、katu=200、count100=100、count50=50。
        # 以前把 count100 当成 200、把 katu 丢掉，200 与 100 两档整体错位一级：
        # 实测一份全连回放里游戏记的 2 个 100，被读成了 2 个 200。
        raw = {
            "perfect": self.geki,
            "great": max(0, self.count300),
            "good": self.katu,
            "ok": self.count100,
            "meh": self.count50,
            "miss": self.count_miss,
        }
        if sum(raw.values()) == 0 and self.score_info:
            got = judgements_from_score_info(self.score_info)
            if got:
                return got
        return raw


# --------------------------------------------------------------------------- 位定义

# legacy mods 位（与 lazer LegacyMods 一致）。只有出现在 MOD_NAMES 里的位才会被解码，
# 所以这里只保留会显示给用户的那些。
MOD_NF = 1 << 0
MOD_EZ = 1 << 1
MOD_HD = 1 << 3
MOD_HR = 1 << 4
MOD_SD = 1 << 5
MOD_DT = 1 << 6
MOD_RX = 1 << 7
MOD_HT = 1 << 8
MOD_NC = 1 << 9
MOD_FL = 1 << 10
MOD_SO = 1 << 12
MOD_AP = 1 << 13
MOD_PF = 1 << 14
# Random：mania 下把四列的顺序随机重排。回放里记的是重排之后的列号，
# 重排结果不写进 .osr，所以分析前要靠按键与音符的吻合度反推（见 match.infer_random_order）。
MOD_RANDOM = 1 << 21
# Mirror：mania 下会把整个键位镜像（第 0 列和第 keys-1 列互换）。
# 回放里记的按键是镜像后的坐标系，谱面文件本身没变，配对前必须翻回来。
MOD_MR = 1 << 30
# ScoreV2：stable 端开这个 mod 才是 V2 计分；lazer 默认就是 V2，不会带这个位。
MOD_SCORE_V2 = 1 << 29

MOD_KEY_BITS = {
    1 << 15: "1K",
    1 << 16: "4K",
    1 << 17: "5K",
    1 << 18: "6K",
    1 << 19: "7K",
    1 << 20: "8K",
    1 << 25: "9K",
    1 << 26: "10K",
    1 << 27: "2K",
    1 << 28: "3K",
}

MOD_NAMES = {
    MOD_NF: "NF",
    MOD_EZ: "EZ",
    MOD_HD: "HD",
    MOD_HR: "HR",
    MOD_SD: "SD",
    MOD_DT: "DT",
    MOD_RX: "RX",
    MOD_HT: "HT",
    MOD_NC: "NC",
    MOD_FL: "FL",
    MOD_SO: "SO",
    MOD_AP: "AP",
    MOD_PF: "PF",
    MOD_RANDOM: "RD",
    MOD_MR: "MR",
    MOD_SCORE_V2: "ScoreV2",
}

# lazer 导出的 .osr 用 30000000 起步的数据库版本号；stable 的版本号是 YYYYMMDD。
LAZER_VERSION = 30000000


def mod_acronyms(score_info: dict | None) -> set[str]:
    """lazer 回放扩展信息里的模组缩写。Classic 是 CL。"""
    if not score_info:
        return set()
    out: set[str] = set()
    for item in score_info.get("mods") or []:
        name = item.get("acronym") if isinstance(item, dict) else item
        if name:
            out.add(str(name).upper())
    return out


def detect_judge_mode(version: int, mods: int, score_info: dict | None = None) -> str:
    """按回放来源判断默认使用哪套判定。

    lazer 默认就是 ScoreV2；带 Classic 时判定窗口切回 stable，按 V1 处理。
    stable 只有开了 ScoreV2 mod 才是 V2。
    """
    if int(version or 0) >= LAZER_VERSION:
        return "classic" if "CL" in mod_acronyms(score_info) else "v2"
    if int(mods or 0) & MOD_SCORE_V2:
        return "v2"
    return "v1"


def judge_source(version: int, mods: int, score_info: dict | None = None) -> str:
    """给页面显示的来源标签：lazer / lazer-classic / stable-v2 / stable-v1。"""
    if int(version or 0) >= LAZER_VERSION:
        return "lazer-classic" if "CL" in mod_acronyms(score_info) else "lazer"
    if int(mods or 0) & MOD_SCORE_V2:
        return "stable-v2"
    return "stable-v1"

MOD_DESC = {
    "NF": "不失败",
    "EZ": "简单（判定放宽一半）",
    "HD": "隐藏",
    "HR": "困难（判定收紧）",
    "SD": "只准完美",
    "PF": "完美",
    "DT": "加速 1.5×",
    "NC": "夜核 1.5×",
    "HT": "减速 0.75×",
    "FL": "闪烁",
    "SO": "旋转",
    "AP": "自瞄",
    "RX": "自动",
    "RD": "列序随机",
}


def judgements_from_score_info(info: dict) -> dict[str, int] | None:
    """从 lazer 的 LegacyReplaySoloScoreInfo 里抽判定数。

    不同版本里形状略有差别，这里按 HitResult 的名字宽松匹配；
    匹配不到就返回 None（让上层退回"没有判定数据"的处理）。
    """
    stats = info.get("Statistics") or info.get("statistics")
    if not isinstance(stats, dict):
        return None
    alias = {
        "perfect": "perfect",
        "great": "great",
        "good": "good",
        "ok": "ok",
        "meh": "meh",
        "miss": "miss",
        "smalltick": None,
        "largetick": None,
        "sliderbreak": None,
        "ignoremiss": None,
    }
    out = {"perfect": 0, "great": 0, "good": 0, "ok": 0, "meh": 0, "miss": 0}
    hit = False
    for key, value in stats.items():
        name = str(key).strip().lower()
        if name not in alias or alias[name] is None:
            continue
        try:
            out[alias[name]] += int(value)
        except (TypeError, ValueError):
            continue
        hit = True
    return out if hit else None


def decode_mods(mods: int) -> list[str]:
    out = [name for bit, name in MOD_NAMES.items() if mods & bit]
    out += [name for bit, name in MOD_KEY_BITS.items() if mods & bit]
    return out


def mods_human(mods: int) -> str:
    names = decode_mods(mods)
    if not names:
        return "无 Mod"
    detail = [MOD_DESC.get(n, "") for n in names]
    parts = [n + ("（" + d + "）" if d else "") for n, d in zip(names, detail)]
    return " + ".join(parts)


# --------------------------------------------------------------------------- 读取


class _Reader:
    __slots__ = ("data", "pos", "truncated")

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        # 声明的长度比实际字节多：文件被截断（写到一半、传输断掉、分片少了尾巴）
        self.truncated = False

    def _take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise OsrError("文件被截断：需要 %d 字节，剩余 %d" % (n, len(self.data) - self.pos))
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def byte(self) -> int:
        return self._take(1)[0]

    def i32(self) -> int:
        return struct.unpack("<i", self._take(4))[0]

    def u16(self) -> int:
        return struct.unpack("<H", self._take(2))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self._take(8))[0]

    def uleb(self) -> int:
        result = 0
        shift = 0
        while True:
            b = self.byte()
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7
            if shift > 35:
                raise OsrError("字符串长度异常（文件结构损坏，可能不是完整的 .osr）")

    def string(self) -> str:
        """osu 的字符串是「1 字节类型标记 + 7bit 长度 + UTF-8」。

        类型标记来自 `SerializationWriter.Write(string)`：非 null 写 `ObjType.StringType`（=0x0B），
        null 写 0（`SerializationReader.ReadString` 见 0 直接返回 null）。
        真实回放里长度前缀几乎是单字节，但按 LEB128 读更稳。
        """
        tag = self.byte()
        if tag == 0:
            return ""
        n = self.uleb()
        if n == 0:
            return ""
        if n > 8192:
            raise OsrError("字符串长度异常（%d 字节），可能不是 .osr 文件" % n)
        return self._take(n).decode("utf-8", errors="replace")

    def byte_array(self, mark_truncated: bool = False) -> bytes:
        """读「int32 长度 + 字节」。

        声明的长度超过剩余字节时只读到末尾。这种情况必须让上层知道：
        截断的 LZMA 流有时能解出前半段，静默当完整数据用会得到一份"看起来正常、
        实际只分析了前几分之一"的结果。mark_truncated=True 的调用方（回放数据块）
        会把这件事记在 reader 上；score_info 之类的可选块则不必打扰用户。
        """
        n = self.i32()
        if n <= 0:
            return b""  # 有的实现用 -1 表示"读到末尾"
        if n > len(self.data) - self.pos:
            if mark_truncated:
                self.truncated = True
            n = len(self.data) - self.pos
        return self._take(n)

    def remaining(self) -> int:
        return len(self.data) - self.pos


def _lzma_filters_from_props(props: bytes) -> list[dict]:
    """把 LZMA1 的 5 字节属性还原成 Python lzma 的 filter 配置。

    属性格式：第 1 字节 = lc + lp*9 + pb*45，后 4 字节 = 字典大小（小端 u32）。
    """
    if len(props) < 5:
        raise OsrError("LZMA 属性长度不足")
    b = props[0]
    lc = b % 9
    rest = b // 9
    lp = rest % 5
    pb = rest // 5
    dict_size = struct.unpack("<I", props[1:5])[0]
    if dict_size < 4096:
        dict_size = 4096
    return [
        {
            "id": lzma.FILTER_LZMA1,
            "lc": lc,
            "lp": lp,
            "pb": pb,
            "dict_size": dict_size,
        }
    ]


def _lzma_decompress(blob: bytes) -> bytes:
    """解压回放数据。

    真实布局：`5 字节 LZMA 属性 + 8 字节原始长度 + 裸 LZMA1 流`
    （lazer 用 SharpCompress 的 `LzmaStream.Create(properties, stream, compressedSize, outSize)`，
    这正是标准 .lzma 头 = 13 字节）。

    但 Python 的 FORMAT_ALONE 对某些用 0xFFFFFFFFFFFFFFFF 表示"长度未知"的文件会报
    Corrupt input data，所以这里先试 ALONE，失败再按裸 LZMA1 解（更稳，实测这对
    ppy/osu 的测试回放有效）。
    """
    if not blob:
        return b""
    errors: list[str] = []
    for fmt in (lzma.FORMAT_ALONE, lzma.FORMAT_AUTO):
        try:
            return lzma.decompress(blob, format=fmt)
        except Exception as e:  # noqa: BLE001
            errors.append("%s:%r" % (fmt, e))

    try:
        filters = _lzma_filters_from_props(blob[:5])
        body = blob[13:] if len(blob) > 13 else b""
        if not body:
            raise OsrError("回放数据为空")
        dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
        return dec.decompress(body)
    except Exception as e:  # noqa: BLE001
        errors.append("raw:%r" % (e,))

    # 最后再试一次：属性 + 长度未知（全 F）的 13 字节头
    try:
        filters = _lzma_filters_from_props(blob[:5])
        fake_header = blob[:5] + struct.pack("<Q", 0xFFFFFFFFFFFFFFFF)
        dec = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
        return dec.decompress(fake_header + blob[13:])
    except Exception as e:  # noqa: BLE001
        errors.append("alone-unknown-size:%r" % (e,))

    raise OsrError("回放数据解压失败：" + " | ".join(errors))


def _lzma_salvage(blob: bytes, step: int = 512) -> bytes:
    """尽力从"没写完/被写坏"的压缩块里抢救已经能解出来的部分。

    典型场景：游戏还在往 .osr 里写，玩家就把文件拖进来了 —— 压缩流的前半段是好的，
    末尾是垃圾，`lzma.decompress` 会直接抛 `Corrupt input data`，一个字节都不给你。
    LZMA 是流式的，按小块喂解码器，坏块处停下，前面解出来的都是有效数据。
    """
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
    out = bytearray()
    for i in range(0, len(blob), step):
        try:
            piece = dec.decompress(blob[i:i + step])
        except Exception:  # noqa: BLE001 - 坏块就到这儿为止
            return bytes(out)
        if piece:
            out += piece
        if dec.eof:
            break
    return bytes(out)


def _parse_frames(text: str) -> list[RawFrame]:
    """带位置判断的帧解析（与 lazer 的行为一致）。

    两条容错规则都来自真实文件：
      * 第一帧常是 `-12345|...` 的种子帧，它不是按键状态（x 恒为 0），跳过；
      * 开头偶尔夹着 x=256、y=-500 的占位帧，会造成"凭空松手"，同样丢掉。
        但只允许丢开头连续的两帧 —— 中间出现同样的坐标是正常按键，不能顺手删。
    """
    frames: list[RawFrame] = []
    xs: list[float] = []
    ys: list[float] = []
    t = 0.0
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split("|")
        if len(parts) < 4:
            continue
        if parts[0] == "-12345":
            continue
        try:
            delta = float(parts[0])
            x = float(parts[1])
        except ValueError:
            continue
        # 100000 位的数字能过 float()（得到 inf），但 int(inf) 会抛 OverflowError。
        # 这种帧只可能来自损坏的文件，直接丢掉 —— 别让它把整次解析变成 500。
        if not (math.isfinite(delta) and math.isfinite(x)):
            continue
        try:
            y = float(parts[2])
        except ValueError:
            y = 0.0
        if not math.isfinite(y):
            y = 0.0
        try:
            button = int(float(parts[3]))
        except (ValueError, OverflowError):
            button = 0
        t += delta
        frames.append(RawFrame(time=t, mask=int(x), button=button))
        xs.append(x)
        ys.append(y)

    # 删除开头的占位帧（x=256, y=-500）
    keep = [i for i in range(len(frames)) if not (xs[i] == 256 and ys[i] == -500)]
    # 只允许删开头连续的两帧
    if keep and keep[0] > 2:
        keep = list(range(len(frames)))
    return [frames[i] for i in keep]


@dataclass
class _Header:
    """回放头的原始字段（还没做解压与容错处理）。"""

    mode: int
    version: int
    beatmap_md5: str
    player: str
    replay_md5: str
    count300: int
    count100: int
    count50: int
    geki: int
    katu: int
    count_miss: int
    score: int
    max_combo: int
    perfect: bool
    mods: int
    ticks: int
    compressed: bytes


def _read_header(r: _Reader) -> _Header:
    """按固定字段顺序读完文件头。字段顺序见模块说明。"""
    try:
        mode = r.byte()
        version = r.i32()
        beatmap_md5 = r.string()
        player = r.string()
        replay_md5 = r.string()
        c300 = r.u16()
        c100 = r.u16()
        c50 = r.u16()
        geki = r.u16()
        katu = r.u16()
        cmiss = r.u16()
        score = r.i32()
        max_combo = r.u16()
        perfect = bool(r.byte())
        mods = r.i32()
        _hp = r.string()
        ticks = r.i64()
        compressed = r.byte_array(mark_truncated=True)
    except OsrError:
        raise
    except Exception as e:  # noqa: BLE001
        raise OsrError("读取回放头失败：%r" % (e,)) from e
    return _Header(mode, version, beatmap_md5, player, replay_md5, c300, c100, c50,
                   geki, katu, cmiss, score, max_combo, perfect, mods, ticks, compressed)


def _read_tail(r: _Reader, version: int) -> tuple[int, bool, dict | None]:
    """读头之后的两个可选块：在线 id 与 lazer 的 scoreInfo。返回三者与"有没有 scoreInfo"。"""
    online_id = 0
    if version >= 20140721 and r.remaining() >= 8:
        online_id = r.i64()
    elif version >= 20121008 and r.remaining() >= 4:
        online_id = r.i32()

    has_score_info = False
    score_info = None
    if version >= 30000001 and r.remaining() > 0:
        try:
            blob = r.byte_array()
            has_score_info = True
            if blob:
                try:
                    text = _lzma_decompress(blob).decode("utf-8", errors="replace")
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        score_info = parsed
                except Exception:  # noqa: BLE001
                    score_info = None
        except OsrError:
            has_score_info = False
    return online_id, has_score_info, score_info


def _decode_replay_block(compressed: bytes, declared_truncated: bool) -> tuple[list[RawFrame], bool]:
    """解压 + 解析帧。返回（帧, 是否残卷）。

    文件没写完时 `lzma.decompress` 会一个字节都不给，所以还要走一遍流式抢救；
    两条路都拿不到足够事件时才算失败。
    """
    truncated = declared_truncated
    try:
        text = _lzma_decompress(compressed).decode("utf-8", errors="replace")
    except OsrError:
        # 全解失败：多半是文件还在写就被读了。能救多少算多少，别把整次分析判死。
        salvaged = _lzma_salvage(compressed)
        # 门槛放低：小回放的压缩块本来就只有一两百字节；真正的把关在下面"至少 30 个事件"
        if len(salvaged) < 256:
            raise OsrError(
                "回放数据解压失败（压缩块 %d 字节）。多半是文件还没写完——"
                "如果游戏正在保存，等它完全退出后再拖进来；也可能这不是完整的 .osr" % len(compressed)
            ) from None
        text = salvaged.decode("utf-8", errors="replace")
        truncated = True

    frames = _parse_frames(text)
    if truncated and len(frames) < 30:
        # 救出来的东西连一小段都拼不成，那就别硬着头皮分析了
        raise OsrError("回放数据解压失败（只解出 %d 个事件）。文件可能还没写完，等游戏完全退出后再试" % len(frames))
    return frames, truncated


def parse_osr(path: str) -> Replay:
    with open(long_path(path), "rb") as f:
        data = f.read()
    if not data:
        raise OsrError("文件是空的")
    r = _Reader(data)
    head = _read_header(r)
    online_id, has_score_info, score_info = _read_tail(r, head.version)
    # 头部声明的压缩块长度超过实际字节：文件在写/传输途中被截断了。
    # 即使"解压成功"，解出来的也只是前缀，必须按残卷处理。
    frames, truncated = _decode_replay_block(head.compressed, r.truncated)

    stamp = None
    if head.ticks > 0:
        try:
            stamp = datetime(1, 1, 1) + timedelta(microseconds=head.ticks // 10)
        except (OverflowError, ValueError):
            stamp = None

    return Replay(
        path=path,
        mode=head.mode,
        version=head.version,
        beatmap_md5=head.beatmap_md5,
        player=head.player,
        replay_md5=head.replay_md5,
        count300=head.count300,
        count100=head.count100,
        count50=head.count50,
        geki=head.geki,
        katu=head.katu,
        count_miss=head.count_miss,
        score=head.score,
        max_combo=head.max_combo,
        perfect=head.perfect,
        mods=head.mods,
        timestamp=stamp,
        frames=frames,
        online_id=online_id,
        truncated=truncated,
        has_score_info=has_score_info,
        score_info=score_info,
    )


# --------------------------------------------------------------------------- 按下事件


@dataclass
class PressEvent:
    """一次"按下"（列从松开变成按下）。"""

    time: float
    column: int


@dataclass
class ReleaseEvent:
    time: float
    column: int


def extract_events(frames: list[RawFrame]) -> tuple[list[PressEvent], list[ReleaseEvent]]:
    """把帧序列转成逐列的按下/松手事件。

    帧只在输入变化时出现，所以相邻帧的掩码差异就是事件。
    """
    presses: list[PressEvent] = []
    releases: list[ReleaseEvent] = []
    prev = 0
    for f in frames:
        changed = f.mask ^ prev
        if changed:
            col = 0
            bits = changed
            while bits:
                if bits & 1:
                    if f.mask & (1 << col):
                        presses.append(PressEvent(time=f.time, column=col))
                    else:
                        releases.append(ReleaseEvent(time=f.time, column=col))
                bits >>= 1
                col += 1
        prev = f.mask
    presses.sort(key=lambda e: (e.time, e.column))
    releases.sort(key=lambda e: (e.time, e.column))
    return presses, releases
