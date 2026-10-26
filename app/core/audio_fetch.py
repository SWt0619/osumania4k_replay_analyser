"""谱面音频的在线补充：本地拿不到音频时，去镜像站取一次谱面包，只留下音频。

为什么需要这一层
----------------
lazer 把谱面和音频**各自**按内容哈希存进 `osu\\files\\`，同一张图的两个文件之间
没有任何关联信息（目录名也只是哈希分片）。所以只在 lazer 里下载过的谱面，
本地能解析出谱面、却永远配不上音频 —— 表现就是回放页没有声音。

这里的办法是：按谱面自己记的 `BeatmapSetID` 去镜像站下 `.osz`，
从压缩包里把 `AudioFilename` 指向的那个文件抽出来存进 `_cache\\audio\\`，
其余全部丢弃。下载一次，之后同一张图直接用缓存。

几条自我约束（都是被踩过的）
--------------------------
1. **同一时刻只下一个包**：镜像站是公共资源，连发容易被限流。单包内部会拆成 8 片
   Range 并发（实测单连接 0.4MB/s、8 路 2.1MB/s），但包与包之间仍然串行，
   下下来的总字节数不变，只是把一条慢连接换成几条。
2. **失败也记账**：网络波动、镜像临时缺图都很常见，但如果不记，用户每打开一次
   回放就会重试一次。失败条目 7 天后才允许重试。
3. **只信任本地能验证的东西**：下下来的包必须真的含这张谱面 MD5 的 .osu 才接受，
   镜像站返回 200 但内容不对（换图、重传版本）时不写入缓存。
4. **一次整包服务整个图集**：同一个图包里每个难度都是独立的 .osu、独立的 MD5。
   只按当前 MD5 存一份的话，换个难度就要把同一个几十 MB 的包再下一次
   （实测一个三难度的包被完整下了三次）。所以校验通过后顺手把包内每份 .osu
   指向的音频都抽出来，按音频内容去重，同包换难度直接命中。
5. **不碰用户目录**：只往 `_cache\\audio\\` 写，不往曲库或游戏目录放任何文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from . import config_store
from .beatmap import AUDIO_EXTS, _decode, parse_osu_file, parse_osu_text
from ..version import __version__

# 镜像按"国内可达性 + 稳定性"排序：sayobot 优先，其次两个国际站。
# Referer 用 osu.sayobot.cn：www 与裸域名都不是镜像站本体（前者挂的是别人的证书，
# 后者只是 nginx 默认页）。实测下载并不校验它，写上只是别把来源标错。
MIRRORS = [
    ("sayobot", "https://dl.sayobot.cn/beatmaps/download/novideo/{sid}",
     {"Referer": "https://osu.sayobot.cn/"}),
    ("nerinyan", "https://api.nerinyan.moe/d/{sid}?nv=1", {}),
    ("mino", "https://catboy.best/d/{sid}", {}),
]

UA = "osumania_replay_analyser/%s (+https://github.com/SWt0619/osumania4k_replay_analyser)" % __version__
TIMEOUT = 20
BUDGET = 45                        # 三个镜像合计最多花这么久
LATENCY_TIMEOUT = 4                # 测延迟时单个站点最多等这么久（三个并行，总耗时≈最慢的一个）
MAX_BYTES = 200 * 1024 * 1024      # 再大就不像谱面包了
MIN_INTERVAL = 2.0                 # 两次请求之间至少隔这么久
FAIL_TTL = 7 * 24 * 3600           # 失败条目多久后允许重试
# 取音频的逻辑一旦改动就 +1：旧的失败记录自动失效，
# 不然修好了 bug 用户还得等满 7 天（或者手动删索引）
# v3：索引新增 sets（图集 → 各难度用哪份音频），同包换难度不再重下整包
FETCH_VERSION = 3

# 抽整个图的音频时给自己划的线：正常图集就一两首（多难度共用或分 cut 版），
# 遇到塞了几十首的怪包就只留当前难度那份，别把用户磁盘吃满。
MAX_HARVEST_FILES = 24
MAX_HARVEST_BYTES = 300 * 1024 * 1024

_last_request = 0.0
# 同一时刻只允许一个线程去镜像站取。服务端是多线程的（ThreadingHTTPServer），
# 两个窗口同时打开同一份缺音频的回放时，没有这把锁就会各下一遍。
_fetch_lock = threading.Lock()

# ------------------------------------------------------------------ 进度
# 补音频对前端是一次同步 POST，而它要下整个谱面包（几十 MB、最长 45 秒）。
# 中间不给任何反馈时，界面上只能写一句"正在尝试补一份…"，用户会当成卡死。
# 这里在内存里记一条当前进度，服务端另外开一个只读接口让页面轮询。
_PROGRESS: dict[str, dict] = {}
_PROGRESS_LOCK = threading.Lock()
_PROGRESS_MAX = 32          # 最多留几条，长时间运行不会越攒越多
_PROGRESS_TTL = 300         # 结束的记录保留多久（留出时间让最后一次轮询读到结果）


def _progress_own(key: str, stage: str = "准备补音频") -> None:
    """把这张图的进度记录交给当前线程，并保留最初开始等的时间。"""
    now = time.time()
    with _PROGRESS_LOCK:
        prev = _PROGRESS.get(key) or {}
        started = float(prev.get("started") or now)
        _PROGRESS[key] = {
            "stage": stage, "started": started, "at": now, "owner": threading.get_ident(),
            "done": 0, "total": 0, "mirror": "", "attempt": 0,
            "finished": False, "ok": None, "error": "",
        }
        _progress_trim_locked(now)


def _progress_mark(key: str, **fields) -> None:
    """更新进度。字段只是展示用，写失败也不该影响下载。"""
    now = time.time()
    with _PROGRESS_LOCK:
        rec = _PROGRESS.get(key)
        if rec is not None:
            rec.update(fields)
            rec["at"] = now


def _progress_waiting(key: str) -> None:
    """在等全局锁：只有这张图还没有在跑的任务时才写，别盖掉别人的真实进度。"""
    now = time.time()
    with _PROGRESS_LOCK:
        rec = _PROGRESS.get(key)
        if rec and not rec.get("finished") and rec.get("owner"):
            return
        _PROGRESS[key] = {
            "stage": "排队等待（同一时间只补一份）", "started": now, "at": now,
            "owner": None, "done": 0, "total": 0, "mirror": "", "attempt": 0,
            "finished": False, "ok": None, "error": "",
        }
        _progress_trim_locked(now)


def _progress_trim_locked(now: float) -> None:
    if len(_PROGRESS) <= _PROGRESS_MAX:
        return
    for k in [k for k, v in _PROGRESS.items() if now - float(v.get("at") or 0) > _PROGRESS_TTL]:
        _PROGRESS.pop(k, None)


def progress(md5: str) -> dict:
    """给页面轮询的进度快照；没有任务时返回空 dict。"""
    key = (md5 or "").lower()
    if not key:
        return {}
    with _PROGRESS_LOCK:
        rec = _PROGRESS.get(key)
        out = dict(rec) if rec else {}
    if not out:
        return {}
    out.pop("owner", None)
    out["elapsed"] = round(max(0.0, time.time() - float(out.get("started") or time.time())), 1)
    return out


def _throttle() -> None:
    """拉平请求间隔。镜像站被压垮的话，所有人都没得用。"""
    global _last_request
    wait = MIN_INTERVAL - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.time()


def _user_agent() -> str:
    return UA


# --------------------------------------------------------------------- 配置
def load_config(cache_dir: str) -> dict:
    """读配置（薄封装，保留这个函数名给服务端与脚本用）。"""
    return config_store.read(cache_dir)


def is_enabled(cache_dir: str) -> bool:
    """默认开启：这是「没声音」这个问题的正解，默认关掉等于没做。

    关掉的办法是配置里显式写 autoFetchAudio: false。本版没有界面开关，
    只能改文件（`/api/audio-pref` 接口留着给脚本用）；这一点与 README 的说法一致。
    """
    return bool(load_config(cache_dir).get("autoFetchAudio", True))


def set_enabled(cache_dir: str, flag: bool) -> None:
    """只改联网开关这一个字段，配置里的其它项（比如手填的游戏目录）保持不变。"""
    try:
        config_store.merge({"autoFetchAudio": bool(flag)}, cache_dir=cache_dir)
    except OSError:
        pass


# --------------------------------------------------------------------- 缓存
def _index_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, "audio", "_index.json")


def _read_index(cache_dir: str) -> dict:
    """读完整索引。

    items：谱面 MD5 → 这份难度用的音频（老结构，仍按它优先命中）
    sets ：图集 id → {"at": 时间, "maps": {谱面 MD5: 音频文件}}，
           一次下载后把包里所有难度都记下来，同包换难度不必再下一遍
    """
    try:
        with open(_index_path(cache_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"items": {}, "sets": {}}
    items = data.get("items")
    sets = data.get("sets")
    return {
        "items": items if isinstance(items, dict) else {},
        "sets": sets if isinstance(sets, dict) else {},
    }


def load_index(cache_dir: str) -> dict:
    return _read_index(cache_dir)["items"]


def save_index(cache_dir: str, items: dict, sets: dict | None = None) -> None:
    """写索引。sets 传 None 表示沿用文件里已有的那份，不覆盖。"""
    path = _index_path(cache_dir)
    payload_sets = _read_index(cache_dir)["sets"] if sets is None else sets
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "items": items, "sets": payload_sets},
                      f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def cached_audio(cache_dir: str, md5: str) -> str:
    """缓存里已有的音频（顺手校验文件还在，用户可能清过缓存目录）。

    先看这份难度自己有没有记录，没有再翻图集表 —— 同一图包里别的难度下过整包时，
    这份难度的音频其实已经在磁盘上了。
    """
    key = (md5 or "").lower()
    if not key:
        return ""
    data = _read_index(cache_dir)
    name = (data["items"].get(key) or {}).get("file") or ""
    if not name:
        for rec in data["sets"].values():
            maps = rec.get("maps") if isinstance(rec, dict) else None
            if isinstance(maps, dict) and maps.get(key):
                name = maps[key]
                break
    if not name:
        return ""
    path = os.path.join(cache_dir, "audio", name)
    return path if os.path.isfile(path) else ""


# --------------------------------------------------------------------- 网络
# 单连接实测只有 0.35~0.45 MB/s（20MB 要 45 秒以上），而镜像支持 Range：
# 8 路并发能到 2.1 MB/s，16 路反而回落到 1.8。所以按 8 片拆。
# 几条自我约束：
#  - 只拆 ≥4MB 的包：小包握手比省下的时间还贵；
#  - 片与片错开 80ms 起跑，不在同一瞬间打出 8 个连接；
#  - 单请求间隔 MIN_INTERVAL 仍然只对"开始下载"生效，不逐片等；
#  - 任一片重试两次仍失败（或镜像回 403/416）就整体退回单连接，宁可慢也要拿到包；
#  - 全局仍由 _fetch_lock 串行，用户连点也只会有这 8 个连接。
PARALLEL = 8
PARALLEL_MIN_BYTES = 4 * 1024 * 1024
PART_STAGGER = 0.08
PROBE_TIMEOUT = 8


def _headers(extra: dict | None = None) -> dict:
    headers = {"User-Agent": _user_agent(), "Accept": "*/*"}
    if extra:
        headers.update(extra)
    return headers


def _content_range_total(value: str | None) -> int:
    """从 `bytes 0-0/4328410` 里取总长度。拿不到就返回 0。"""
    if not value:
        return 0
    _, _, tail = str(value).partition("/")
    return int(tail) if tail.isdigit() else 0


def _probe_total(url: str, headers: dict) -> int:
    """问一下这个包有多大、支不支持分段。返回总字节数，0 表示别拆。

    用 Range: bytes=0-0 而不是 HEAD：有的镜像对 HEAD 回 405，但对分段是支持的。
    """
    probe = dict(headers)
    probe["Range"] = "bytes=0-0"
    try:
        with urlopen(Request(url, headers=probe), timeout=PROBE_TIMEOUT) as resp:
            if getattr(resp, "status", 200) != 206:
                return 0
            total = _content_range_total(resp.headers.get("Content-Range"))
            resp.read(1)
            return total
    except Exception:  # noqa: BLE001 - 探不到就当不支持，交给单连接
        return 0


def _single(url: str, headers: dict, sink: str, on_bytes=None,
            throttle: bool = True) -> bytes:
    """单连接下载。sink 为空时读进内存。"""
    if throttle:
        _throttle()
    with urlopen(Request(url, headers=headers), timeout=TIMEOUT) as resp:
        if not sink:
            return resp.read()
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0
        written = 0
        if on_bytes:
            on_bytes(0, total)
        with open(sink, "wb") as f:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_BYTES:
                    raise ValueError("包体超过 %d MB，放弃" % (MAX_BYTES // 1024 // 1024))
                f.write(chunk)
                if on_bytes:
                    on_bytes(written, total)
        return b""


def _parts(url: str, headers: dict, sink: str, total: int, on_bytes=None) -> None:
    """把 total 字节拆成 PARALLEL 片并发取回，按顺序拼到 sink。

    进度按累计字节上报，语义和单连接一致（已下载, 总大小），前端不用改。
    """
    # 每片至少 1MB：4MB 的包拆 4 片，20MB 以上封顶 8 片
    count = int(min(PARALLEL, max(2, total // (1024 * 1024))))
    step = total // count
    spans = []
    start = 0
    for i in range(count):
        end = total - 1 if i == count - 1 else start + step - 1
        spans.append((start, end))
        start = end + 1

    parts = ["%s.p%d" % (sink, i) for i in range(count)]
    got = [0] * count
    lock = threading.Lock()
    if on_bytes:
        on_bytes(0, total)

    def worker(idx: int, a: int, b: int) -> None:
        time.sleep(idx * PART_STAGGER)
        want = b - a + 1
        headers_i = dict(headers)
        headers_i["Range"] = "bytes=%d-%d" % (a, b)
        last: Exception | None = None
        for attempt in range(3):
            got[idx] = 0
            try:
                with urlopen(Request(url, headers=headers_i), timeout=TIMEOUT) as resp:
                    if getattr(resp, "status", 200) != 206:
                        # 镜像不理 Range 时别硬拼：整包会被当成这一片写进去
                        raise ValueError("镜像不支持分段（%s）" % getattr(resp, "status", "?"))
                    size = 0
                    with open(parts[idx], "wb") as f:
                        while True:
                            chunk = resp.read(256 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                            size += len(chunk)
                            with lock:
                                got[idx] += len(chunk)
                                done = sum(got)
                            if on_bytes:
                                on_bytes(min(done, total), total)
                if size != want:
                    raise ValueError("分段长度不符（%d / %d）" % (size, want))
                return
            except HTTPError as e:
                last = e
                if e.code in (403, 416, 429):
                    # 明确拒绝或范围不合法，重试没有意义，交给单连接
                    raise
                time.sleep(0.6 * (attempt + 1))
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(0.6 * (attempt + 1))
        raise last or RuntimeError("分段下载失败")

    try:
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(worker, i, a, b) for i, (a, b) in enumerate(spans)]
            for fut in futures:
                fut.result()
        with open(sink, "wb") as out:
            for path in parts:
                with open(path, "rb") as f:
                    shutil.copyfileobj(f, out, 256 * 1024)
        if os.path.getsize(sink) != total:
            raise ValueError("拼包后大小不对")
    finally:
        for path in parts:
            try:
                os.remove(path)
            except OSError:
                pass


def _get(url: str, extra_headers: dict | None = None, sink: str = "",
         on_bytes=None) -> bytes:
    """GET 一个地址。sink 非空时直接流到文件（谱面包可能几十 MB）。

    on_bytes(已下载, 总大小) 每读一块回调一次，给界面报进度；总大小拿不到时是 0。
    走文件时优先分段并发，任何异常都退回单连接——补音频失败比慢更难受。
    """
    headers = _headers(extra_headers)
    if not sink:
        return _single(url, headers, sink, on_bytes)

    _throttle()
    total = _probe_total(url, headers)
    if total > MAX_BYTES:
        raise ValueError("包体超过 %d MB，放弃" % (MAX_BYTES // 1024 // 1024))
    if total >= PARALLEL_MIN_BYTES:
        try:
            _parts(url, headers, sink, total, on_bytes)
            return b""
        except Exception:  # noqa: BLE001
            # 已经下到的分片会被清掉，重下一遍；只发生在镜像抽风时
            pass
    return _single(url, headers, sink, on_bytes, throttle=False)


def download_file(url: str, sink: str, on_bytes=None,
                  extra_headers: dict | None = None) -> None:
    """下载一整个文件到 sink（给「点封面下载谱面包」用）。

    和补音频共用同一条链路：优先分段并发，镜像不支持就退回单连接。
    on_bytes(已下载, 总大小) 的语义与补音频一致，前端进度条不用区分来源。
    """
    _get(url, extra_headers=extra_headers, sink=sink, on_bytes=on_bytes)


# --------------------------------------------------------------------- 抽取
def _pick_audio(names: list[str], wanted: str) -> str:
    """在压缩包条目里找要的那个音频：先按名字精确匹配，再按扩展名兜底。"""
    base = os.path.basename((wanted or "").replace("\\", "/")).lower()
    if base:
        for n in names:
            if os.path.basename(n).lower() == base:
                return n
        stem = os.path.splitext(base)[0]
        for n in names:
            if os.path.splitext(os.path.basename(n).lower())[0] == stem:
                return n
    for n in names:
        if os.path.splitext(n)[1].lower() in AUDIO_EXTS:
            return n
    return ""


def extract_audio(osz_path: str, wanted: str, out_base: str) -> str:
    """从 .osz 里抽出音频写到 out_base + 扩展名，返回落盘路径（失败返回空串）。"""
    try:
        with zipfile.ZipFile(osz_path) as z:
            name = _pick_audio(z.namelist(), wanted)
            if not name:
                return ""
            ext = os.path.splitext(name)[1].lower()
            if ext not in AUDIO_EXTS:
                ext = ".mp3"
            out = out_base + ext
            tmp = out + ".%d.%d.part" % (os.getpid(), threading.get_ident())
            with z.open(name) as src, open(tmp, "wb") as dst:
                while True:
                    chunk = src.read(256 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
            os.replace(tmp, out)
            return out
    except (OSError, zipfile.BadZipFile):
        return ""


def _md5_variants(data: bytes) -> list[str]:
    """一份 .osu 的几种 MD5 口径：原样、LF 归一、CRLF 归一。

    回放里记的 MD5 可能来自 stable（原样）也可能来自 lazer（换行被归一过），
    只比对一种会把同一张谱面判成两张。
    """
    out = [hashlib.md5(data).hexdigest()]
    lf = data.replace(b"\r\n", b"\n")
    out.append(hashlib.md5(lf).hexdigest())
    out.append(hashlib.md5(lf.replace(b"\n", b"\r\n")).hexdigest())
    return out


def _verify_osz(osz_path: str, md5: str) -> bool:
    """包里的 .osu 里必须有一份能和这张回放的 MD5 对上，否则说明镜像给错了图。"""
    want = (md5 or "").lower()
    if not want:
        return True
    try:
        with zipfile.ZipFile(osz_path) as z:
            for name in z.namelist():
                if not name.lower().endswith(".osu"):
                    continue
                if want in _md5_variants(z.read(name)):
                    return True
    except (OSError, zipfile.BadZipFile, KeyError):
        return False
    return False


def _audio_filename_of(data: bytes) -> str:
    """从 .osu 原文里读出 AudioFilename。解析不了的（坏文件、残缺包）给空串。"""
    try:
        return parse_osu_text(_decode(data)).audio_filename or ""
    except Exception:  # noqa: BLE001 - 包里混进坏文件是常事，跳过即可
        return ""


def _extract_by_content(z: zipfile.ZipFile, name: str, audio_dir: str) -> tuple[str, int]:
    """抽一个音频到缓存，按内容 MD5 命名（同一首被多个难度引用时只留一份）。

    返回（文件名, 字节数）；失败返回（""，0）。
    """
    ext = os.path.splitext(name)[1].lower()
    if ext not in AUDIO_EXTS:
        ext = ".mp3"
    tmp = os.path.join(audio_dir, ".harvest.%d.%d.part" % (os.getpid(), threading.get_ident()))
    digest = hashlib.md5()
    size = 0
    try:
        with z.open(name) as src, open(tmp, "wb") as dst:
            while True:
                chunk = src.read(256 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                dst.write(chunk)
        out = os.path.join(audio_dir, digest.hexdigest() + ext)
        if os.path.isfile(out):
            os.remove(tmp)
        else:
            os.replace(tmp, out)
        return os.path.basename(out), size
    except (OSError, KeyError, zipfile.BadZipFile):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return "", 0


def _harvest_audio(osz_path: str, audio_dir: str, want_md5: str) -> tuple[bool, dict]:
    """把包里每份 .osu 指向的音频都抽出来，顺便校验这是不是我们要的图。

    返回（包里有这张谱面, {谱面 MD5: 音频文件名}）。一次整包几十 MB 只服务一个
    难度太亏，同包换难度时直接查这张表就能命中。
    """
    want = (want_md5 or "").lower()
    maps: dict[str, str] = {}
    picked: dict[str, str] = {}      # zip 内音频条目名 → 缓存文件名
    total_bytes = 0
    try:
        with zipfile.ZipFile(osz_path) as z:
            names = z.namelist()
            for name in names:
                if not name.lower().endswith(".osu"):
                    continue
                try:
                    data = z.read(name)
                except (OSError, KeyError, zipfile.BadZipFile):
                    continue
                audio_name = _audio_filename_of(data)
                if not audio_name:
                    continue
                entry = _pick_audio(names, audio_name)
                if not entry:
                    continue
                if entry not in picked:
                    if len(picked) >= MAX_HARVEST_FILES or total_bytes >= MAX_HARVEST_BYTES:
                        continue
                    picked[entry], size = _extract_by_content(z, entry, audio_dir)
                    total_bytes += size
                file_name = picked.get(entry) or ""
                if not file_name:
                    continue
                for digest in _md5_variants(data):
                    maps[digest] = file_name
    except (OSError, zipfile.BadZipFile):
        return False, {}
    return (not want) or (want in maps), maps


# --------------------------------------------------------------------- 主流程
def fetch_audio(beatmap_path: str, md5: str, cache_dir: str, force: bool = False) -> dict:
    """给一张谱面补音频。返回 {ok, path, source, error, cached}。

    调用方负责判断要不要联网（is_enabled）——这里只管干活。
    """
    key = (md5 or "").lower()
    if not key:
        return {"ok": False, "path": "", "source": "", "error": "没有谱面 MD5", "cached": False}

    hit = cached_audio(cache_dir, key)
    if hit:
        return {"ok": True, "path": hit, "source": "cache", "error": "", "cached": True}

    # 同一张图只让一个线程去取。两个窗口同时拖进同一份缺音频的回放时，
    # 以前两边会各开一遍下载、往同一个 .part 文件里写，互相把包写坏之后
    # 各自换下一个镜像重试——实测一次补全会打出 3~5 个请求，公共镜像很容易被限流。
    _progress_waiting(key)
    with _fetch_lock:
        # 等锁的这段时间里，先拿到锁的线程可能已经把音频下好写进缓存了
        hit = cached_audio(cache_dir, key)
        if hit:
            _progress_mark(key, stage="完成（缓存里已经有了）", finished=True, ok=True)
            return {"ok": True, "path": hit, "source": "cache", "error": "", "cached": True}
        return _fetch_locked(beatmap_path, key, cache_dir, force)


def _target_of(beatmap_path: str) -> tuple[str, str]:
    """读出这次要抽的音频文件名与图集 id（读不到就给空串）。"""
    audio_name = ""
    set_id = ""
    try:
        bm = parse_osu_file(beatmap_path)
        audio_name = bm.audio_filename
        set_id = str(bm.meta.get("BeatmapSetID") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    return audio_name, ("" if set_id in ("0", "-1") else set_id)


def _download_from_mirrors(key: str, set_id: str,
                           audio_dir: str) -> tuple[dict, str, str]:
    """依次问每个镜像要一次包，成功返回（各难度音频表, 镜像名, ""），全失败返回（{}, "", 错误）。

    每个镜像都走同一条验收：体积像样 → 包里真有这张谱面 → 音频真能抽出来。
    验收和抽取是同一次遍历（`_harvest_audio`），顺便把同包其它难度的音频也留下。
    """
    # 临时文件名带 pid 与线程号：多开一个程序实例时也不会写到同一个文件里
    tag = "%d.%d" % (os.getpid(), threading.get_ident())
    tmp_osz = os.path.join(audio_dir, key + "." + tag + ".osz.part")
    last_err = ""
    started = time.time()
    for attempt, (name, tpl, headers) in enumerate(MIRRORS, 1):
        # 总预算：三个镜像全挂时别让用户对着"正在补音频"等一分半
        if time.time() - started > BUDGET:
            return {}, "", last_err or "镜像都没能在 %d 秒内给回应" % BUDGET
        # 先把"正在连 sayobot（第 1 个镜像）"写出去：DNS + TLS 握手 + 等响应
        # 可能有一两秒完全没有字节，那一段最容易让人以为卡死。
        _progress_mark(key, stage="正在从 %s 下载" % name, mirror=name,
                       attempt=attempt, done=0, total=0)

        def _on_bytes(written, total, _name=name):
            _progress_mark(key, done=written, total=total, stage="正在从 %s 下载" % _name)

        try:
            _get(tpl.format(sid=set_id), headers, sink=tmp_osz, on_bytes=_on_bytes)
            _progress_mark(key, stage="正在校验 %s 给的包" % name)
            # 低于这个尺寸的只可能是错误页或空响应；真正的有效性靠下面的 _harvest_audio
            if not os.path.isfile(tmp_osz) or os.path.getsize(tmp_osz) < 128:
                last_err = "%s 返回的是空包" % name
                continue
            _progress_mark(key, stage="正在整理包里的音频")
            ok, maps = _harvest_audio(tmp_osz, audio_dir, key)
            if not ok:
                last_err = "%s 给的包里没有这张谱面" % name
                continue
            if not maps.get(key):
                last_err = "%s 的包里没有这张谱面的音频" % name
                continue
            return maps, name, ""
        except Exception as e:  # noqa: BLE001
            last_err = "%s：%s" % (name, e)
        finally:
            try:
                if os.path.isfile(tmp_osz):
                    os.remove(tmp_osz)
            except OSError:
                pass
    return {}, "", last_err or "所有镜像都失败了"


def _fetch_locked(beatmap_path: str, key: str, cache_dir: str, force: bool) -> dict:
    """真正的下载流程。调用方必须已经持有 `_fetch_lock`。"""
    _progress_own(key)
    items = load_index(cache_dir)
    prev = items.get(key) or {}
    stale = prev.get("v") != FETCH_VERSION
    if not force and not stale and prev.get("failed") and (time.time() - float(prev.get("at") or 0)) < FAIL_TTL:
        err = prev.get("error") or "上次没取到"
        _progress_mark(key, stage="上次没取到，短期内不再重试", finished=True, ok=False, error=err)
        return {"ok": False, "path": "", "source": "", "error": err, "cached": False}

    _audio_name, set_id = _target_of(beatmap_path)

    # 没有图集 id 就到此为止，不做「按 MD5 猜图集」。
    # 曾经试过 sayobot 的 ?K=<md5>，实测它根本不按 MD5 查：随便编一个 md5 都会返回
    # 一张不相干的图集（连 status 都是 0），拿它去下载等于白下几十 MB 还可能配错音频。
    # nerinyan /mino 的 md5 接口要么 530 要么 403，没有免鉴权可用的。
    if not set_id:
        err = "这张谱面没记录图集 id（自制图或还没上传到官网），无法在线补音频"
        items[key] = {"v": FETCH_VERSION, "failed": True, "at": time.time(), "error": err}
        save_index(cache_dir, items)
        _progress_mark(key, stage="无法在线补音频", finished=True, ok=False, error=err)
        return {"ok": False, "path": "", "source": "", "error": err, "cached": False}

    audio_dir = os.path.join(cache_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    maps, source, last_err = _download_from_mirrors(key, set_id, audio_dir)
    file_name = (maps or {}).get(key) or ""
    if file_name:
        path = os.path.join(audio_dir, file_name)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        items[key] = {
            "v": FETCH_VERSION,
            "file": file_name,
            "source": source,
            "at": time.time(),
            "setId": set_id,
            "size": size,
        }
        # 同一张表按图集存一份：之后换这个包里别的难度，直接命中，不再下整包
        sets = _read_index(cache_dir)["sets"]
        sets[set_id] = {"at": time.time(), "maps": maps}
        save_index(cache_dir, items, sets)
        _progress_mark(key, stage="完成", finished=True, ok=True)
        return {"ok": True, "path": path, "source": source, "error": "", "cached": False}

    items[key] = {"v": FETCH_VERSION, "failed": True, "at": time.time(), "error": last_err}
    save_index(cache_dir, items)
    _progress_mark(key, stage="没补到", finished=True, ok=False, error=last_err)
    return {"ok": False, "path": "", "source": "", "error": last_err, "cached": False}


def test_mirrors() -> list[dict]:
    """量一次本机到各镜像站的可用性与响应时间（毫秒），给设置页的「测试连接」用。

    以前发的是 HEAD：有的站不支持（405），有的站在风控直接 403，全被当成"连通"，
    界面上就显示成一个正常延迟，实际补音频根本下不了。现在按真实下载走一遍
    GET + `Range: bytes=0-1023`，只读 1KB 就断开 —— 路径和补音频完全一致，
    下不动的站会如实标出来。

    返回每项：ok（真能下）、reachable（连得上但也许被拒）、status、ms、error。
    三个站并行问，总耗时约等于最慢的那个。
    """
    results: list[dict] = []
    lock = threading.Lock()
    started_all = time.perf_counter()

    def probe(name: str, tpl: str, extra: dict) -> None:
        started = time.perf_counter()
        ok, reachable, error, status = False, False, "", 0
        headers = {"User-Agent": _user_agent(), "Accept": "*/*", "Range": "bytes=0-1023"}
        if extra:
            headers.update(extra)
        try:
            req = Request(tpl.format(sid="1"), headers=headers)
            with urlopen(req, timeout=LATENCY_TIMEOUT) as resp:
                status = int(getattr(resp, "status", 0) or 200)
                reachable = True
                ok = 200 <= status < 300
                if ok:
                    # 读了才算真给数据：只回一个空壳（Content-Length: 0）同样下不了
                    if not resp.read(1024):
                        ok, error = False, "有回应但没有数据"
                else:
                    error = "拒绝访问（HTTP %d）" % status
        except HTTPError as e:
            status = int(e.code or 0)
            reachable = True
            error = "拒绝访问（HTTP %d）" % status if status else "请求被拒绝"
        except Exception as e:  # noqa: BLE001 - 测速失败只是结果之一，不该抛给调用方
            error = str(e) or type(e).__name__
        ms = int(round((time.perf_counter() - started) * 1000))
        with lock:
            results.append({"name": name, "ms": ms, "ok": ok, "reachable": reachable,
                            "status": status, "error": error})

    threads = [
        threading.Thread(target=probe, args=(name, tpl, headers), daemon=True)
        for name, tpl, headers in MIRRORS
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(LATENCY_TIMEOUT + 1)
    # 有站点可能卡在 DNS 上，join 都回来了还没结果：补一行"没回应"，
    # 否则结果表会少一个站，用户会以为漏测了。
    with lock:
        known = {r["name"] for r in results}
        waited = int(round((time.perf_counter() - started_all) * 1000))
        for name, _, _ in MIRRORS:
            if name not in known:
                results.append({
                    "name": name, "ms": waited, "ok": False, "reachable": False, "status": 0,
                    "error": "等待回应超时（DNS 解析或握手没能完成）",
                })
    # 按 MIRRORS 的顺序返回：谁先回来不该影响结果表的顺序
    order = {name: i for i, (name, _, _) in enumerate(MIRRORS)}
    results.sort(key=lambda item: order.get(item["name"], 99))
    return results


def stats(cache_dir: str) -> dict:
    """缓存占用。同一份音频会被同包的多个难度共用，按文件名去重后再加总。"""
    data = _read_index(cache_dir)
    audio_dir = os.path.join(cache_dir, "audio")
    names: set[str] = set()
    for item in data["items"].values():
        if item.get("file"):
            names.add(item["file"])
    for rec in data["sets"].values():
        maps = rec.get("maps") if isinstance(rec, dict) else None
        if isinstance(maps, dict):
            names.update(v for v in maps.values() if v)
    total = 0
    alive = 0
    for name in names:
        try:
            total += os.path.getsize(os.path.join(audio_dir, name))
            alive += 1
        except OSError:
            continue
    return {
        "count": alive,
        "bytes": total,
        "failed": len([v for v in data["items"].values() if v.get("failed")]),
        "enabled": is_enabled(cache_dir),
    }
