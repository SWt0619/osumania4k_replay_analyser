"""osu! 曲库索引：按 MD5 自动找到回放对应的谱面（以及它的音频）。

设计要点（2026-10-17 改版，起因：用户反馈"启动后卡在解析回放很久"）：
  1. **索引落盘**：把每个 .osu 的 (大小, mtime, 三个 MD5) 缓存到 JSON，
     下次启动直接读，不再每次把所有谱面重算一遍。
  2. **增量**：只有新增/改动过的文件才重新算 MD5。
  3. **可选早退**：按 MD5 查找时可以先扫一遍"已知"的条目，再决定要不要补扫。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field

from .beatmap import AUDIO_EXTS, compute_md5_variants, long_path, parse_osu_file, resolve_audio
from .osu_dirs import (
    default_export_roots,
    default_lazer_file_roots,
    default_songs_roots,
    detected_songs_roots,
)

CACHE_VERSION = 3

# 超过 20 星的难度黑名单，由 _dataset/build_star_index.py 生成。
# 只按 (bid, Version) 精确排除，不能按图包整包排除：同一个图包里其他难度要照常可用。
_HIGH_STAR: dict[int, set[str]] | None = None


def _high_star_map() -> dict[int, set[str]]:
    global _HIGH_STAR
    if _HIGH_STAR is not None:
        return _HIGH_STAR
    out: dict[int, set[str]] = {}
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "data", "star_over20.json")
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        for item in payload.get("entries") or []:
            bid = int(item.get("bid") or 0)
            version = str(item.get("version") or "").strip().lower()
            if bid > 0 and version:
                out.setdefault(bid, set()).add(version)
    except (OSError, ValueError, TypeError, AttributeError):
        out = {}
    _HIGH_STAR = out
    return out


def _is_high_star(full: str, high: dict[int, set[str]]) -> bool:
    """这个 .osu 是否在 20 星黑名单里。读文件头 4KB 就够，不解析整张谱。"""
    if not high:
        return False
    try:
        with open(long_path(full), "r", encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return False
    bid_match = re.search(r"^BeatmapID:\s*(\d+)", head, re.M)
    ver_match = re.search(r"^Version:\s*(.*)$", head, re.M)
    if not bid_match or not ver_match:
        return False
    try:
        bid = int(bid_match.group(1))
    except ValueError:
        return False
    if bid not in high:
        return False
    return ver_match.group(1).strip().lower() in high[bid]


def _looks_like_osu(path: str) -> bool:
    """lazer 的文件库用哈希当文件名（无扩展名），靠内容辨认 .osu。"""
    try:
        with open(long_path(path), "rb") as f:
            head = f.read(64)
    except OSError:
        return False
    return b"osu file format" in head


def _listdir(path: str) -> list[str]:
    try:
        return os.listdir(long_path(path))
    except OSError:
        return []


def _in_lazer_store(path: str) -> bool:
    """是否在 lazer 的 files 存储里（哈希文件名、目录里没有同捆音频）。"""
    return os.sep + "files" + os.sep in path.lower().replace("/", os.sep)


def _audio_score(path: str) -> int:
    """这个谱面路径"能拿到音频"的打分，用于同 MD5 有多份时挑更优的那份。

    3 = 目录里就有音频；2 = 有目录结构（stable 的 Songs / lazer 导出），只是这份可能缺音频；
    1 = lazer files 存储里的哈希文件 —— 那种目录里绝不会有同捆音频。
    """
    if any(os.path.splitext(f)[1].lower() in AUDIO_EXTS for f in _listdir(os.path.dirname(path))):
        return 3
    return 1 if _in_lazer_store(path) else 2


@dataclass
class LibraryEntry:
    path: str
    md5_raw: str
    md5_lf: str
    md5_crlf: str
    size: int
    mtime: float


def _entry_for(full: str, cache: dict[str, LibraryEntry], sniff: bool = False) -> tuple[LibraryEntry | None, bool]:
    """算（或复用）一个 .osu 的索引条目。返回 (entry, 是否新算了哈希)。

    复用条件同时比大小和 mtime：只比一个的话，改图但没改大小（换难度名）会被漏掉。
    """
    try:
        st = os.stat(full)
    except OSError:
        return None, False
    hit = cache.get(full)
    if hit is not None and hit.size == st.st_size and abs(hit.mtime - st.st_mtime) < 1.0:
        return hit, False
    if sniff and not _looks_like_osu(full):
        return None, False
    try:
        hashes = compute_md5_variants(full)
    except OSError:
        return None, False
    return (
        LibraryEntry(
            path=full,
            md5_raw=hashes["raw"],
            md5_lf=hashes["lf"],
            md5_crlf=hashes["crlf"],
            size=st.st_size,
            mtime=st.st_mtime,
        ),
        True,
    )


def _scan_regular(scan_root: str, cache: dict[str, LibraryEntry]) -> tuple[list[LibraryEntry], list[bool]]:
    """普通目录树：直接按 .osu 扩展名收。"""
    found: list[LibraryEntry] = []
    hashed: list[bool] = []
    if not scan_root or not os.path.isdir(long_path(scan_root)):
        return found, hashed
    for dirpath, _dirnames, filenames in os.walk(long_path(scan_root)):
        for fn in filenames:
            if not fn.lower().endswith(".osu"):
                continue
            entry, did_hash = _entry_for(os.path.join(dirpath, fn), cache)
            if entry is None:
                continue
            found.append(entry)
            hashed.append(did_hash)
    return found, hashed


def _scan_lazer_store(store_root: str, cache: dict[str, LibraryEntry]) -> tuple[list[LibraryEntry], list[bool]]:
    """lazer 的 files 存储：哈希文件名、无扩展名，只能按内容辨认。

    超长图/新图往往只存在于这里（用户在 lazer 里玩，谱面不落在 Songs），
    不索引它的话回放永远提示"曲库里没有这张谱面"。
    """
    found: list[LibraryEntry] = []
    hashed: list[bool] = []
    if not store_root or not os.path.isdir(long_path(store_root)):
        return found, hashed
    for dirpath, _dirnames, filenames in os.walk(long_path(store_root)):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if fn.lower().endswith((".osu", ".osk", ".osr")):
                continue  # 正常命名的交给普通扫描，免得同一份文件被索引两遍
            entry, did_hash = _entry_for(full, cache, sniff=True)
            if entry is None:
                continue
            found.append(entry)
            hashed.append(did_hash)
    return found, hashed


def _resolve_md5_conflicts(entries: list[LibraryEntry]) -> dict[str, LibraryEntry]:
    """同一个 MD5 可能在「stable 的 Songs」和「lazer 的 files」里各有一份。

    files 那份是单独存放的哈希文件、目录里没有同捆音频 —— 让它占位的话，
    回放就永远拿不到音频。所以冲突时优先挑"目录里有音频"的那份。
    """
    by_md5: dict[str, LibraryEntry] = {}
    for e in entries:
        for key in (e.md5_raw, e.md5_lf, e.md5_crlf):
            prev = by_md5.get(key)
            if prev is None or _audio_score(e.path) > _audio_score(prev.path):
                by_md5[key] = e
    return by_md5


def _load_cache_file(cache_path: str, root: str) -> dict[str, LibraryEntry]:
    """读落盘索引。版本号或根目录对不上就整份作废（宁可从算，也不用可疑数据）。"""
    if not cache_path or not os.path.isfile(cache_path):
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return {}
    if data.get("version") != CACHE_VERSION or data.get("root") != root:
        return {}
    out: dict[str, LibraryEntry] = {}
    for item in data.get("files", []):
        try:
            out[item["p"]] = LibraryEntry(
                path=item["p"],
                md5_raw=item["raw"],
                md5_lf=item["lf"],
                md5_crlf=item["crlf"],
                size=int(item["size"]),
                mtime=float(item["mtime"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _save_cache_file(cache_path: str, lib: "Library") -> None:
    """整份覆盖写。先写 .tmp 再 replace，读的一方不会看到半个文件。"""
    if not cache_path:
        return
    data = {
        "version": CACHE_VERSION,
        "root": lib.root,
        "builtAt": lib.built_at,
        "files": [
            {"p": e.path, "raw": e.md5_raw, "lf": e.md5_lf, "crlf": e.md5_crlf, "size": e.size, "mtime": e.mtime}
            for e in lib.entries
        ],
    }
    tmp = cache_path + ".tmp"
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, cache_path)
    except OSError:
        pass


@dataclass
class Library:
    root: str = ""
    entries: list[LibraryEntry] = field(default_factory=list)
    built_at: float = 0.0
    scanned_files: int = 0
    reused_files: int = 0
    hashed_files: int = 0
    build_ms: float = 0.0
    _by_md5: dict[str, LibraryEntry] = field(default_factory=dict, repr=False)
    _meta_cache: dict[str, dict] = field(default_factory=dict, repr=False)
    _audio_cache: dict[str, str] = field(default_factory=dict, repr=False)
    _set_audio_cache: dict[str, str] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # 上次扫描时的曲库指纹与缓存路径：用来判断"这中间有没有下过新图"
    _stamp_at: tuple = field(default=(), repr=False)
    _cache_path: str = field(default="", repr=False)

    # ---- 构建 -----------------------------------------------------------
    def build(self, root: str, force: bool = False, cache_path: str = "") -> None:
        with self._lock:
            started = time.time()
            if not force and self.root == root and self.entries and (time.time() - self.built_at) < 3600:
                return

            cached = {} if force else _load_cache_file(cache_path, root)
            entries, stats = self._scan_all(root, cached)
            by_md5 = _resolve_md5_conflicts(entries)

            self.root = root
            self.entries = entries
            self._by_md5 = by_md5
            self._meta_cache = {}
            self._audio_cache = {}
            self._set_audio_cache = {}
            self.built_at = time.time()
            self.scanned_files, self.reused_files, self.hashed_files = stats
            self.build_ms = (time.time() - started) * 1000
            self._cache_path = cache_path
            self._stamp_at = self._stamp()
            if entries:
                _save_cache_file(cache_path, self)

    def _scan_all(self, root: str, cached: dict[str, LibraryEntry]) -> tuple[list[LibraryEntry], tuple[int, int, int]]:
        """扫多处来源，返回（条目, (总数, 复用数, 新算哈希数)）。

        除了主根，还会扫 lazer 的导出目录与 files 存储：只在 lazer 里玩的人的谱面
        根本不在 Songs 下，漏掉就会永远提示"曲库里没有这张谱面"。
        这两处与主根是**不同种类的存储**，所以无条件合并；
        而"其它 Songs 根"只在主根本身就是自动探测到的曲库时才合并（见下面的注释）。
        """
        entries: list[LibraryEntry] = []
        counts = [0, 0, 0]   # scanned / reused / hashed

        def collect(found: list[LibraryEntry], did_hashes: list[bool]) -> None:
            for entry, did_hash in zip(found, did_hashes):
                entries.append(entry)
                counts[0] += 1
                counts[2 if did_hash else 1] += 1

        collect(*_scan_regular(root, cached))
        # 同时装了 lazer 与 stable 的人，两边 Songs 是两棵完全不同的树。
        # 只扫主根的话，另一个客户端里的谱面会永远"曲库里没有这张谱面"。
        # 仅在"主根本身就是自动探测到的曲库之一"时合并：玩家手填的目录（--songs、
        # 网页里指定）不算，否则一次指定会把整机所有曲库都卷进来。
        defaults = detected_songs_roots()
        if any(os.path.abspath(root or "") == os.path.abspath(d) for d in defaults):
            for extra in defaults:
                if os.path.abspath(extra) != os.path.abspath(root or ""):
                    collect(*_scan_regular(extra, cached))
        # lazer 导出的谱面目录（osu\exports\<图集>\*.osu）也收进来
        for extra in default_export_roots():
            if os.path.abspath(extra) != os.path.abspath(root or ""):
                collect(*_scan_regular(extra, cached))
        for store in default_lazer_file_roots():
            collect(*_scan_lazer_store(store, cached))
        # 扫描阶段就把 20 星以上的观赏图排掉：精确到 (bid, 难度名)，同包其他难度不受影响。
        high = _high_star_map()
        if high:
            entries = [e for e in entries if not _is_high_star(e.path, high)]
        return entries, (counts[0], counts[1], counts[2])

    # ---- 新鲜度 ---------------------------------------------------------
    def _stamp(self) -> tuple:
        """曲库目录的指纹：顶层条目数 + 最新 mtime。

        下载完一张新图，`Songs` 下会多一个目录，这两项都会变 —— 拿它当"该重扫了"的信号，
        比定时重建便宜得多（几千个目录 stat 一遍，几十毫秒）。
        """
        if not self.root:
            return ()
        try:
            names = os.listdir(self.root)
        except OSError:
            return ()
        latest = 0.0
        for name in names:
            try:
                latest = max(latest, os.stat(os.path.join(self.root, name)).st_mtime)
            except OSError:
                continue
        return (self.root, len(names), round(latest, 1))

    def ensure_fresh(self) -> bool:
        """曲库有变化就重建索引，返回是否重建了。

        用户的真实路径：网页里推了一张图给他 → 他去下载 → 打完导出回放 → 拖回来分析，
        这时 `Songs` 里已经有这张图，但索引是 1 小时前建的（build 里有 TTL），
        于是一直提示"曲库里没有这张谱面"。分析前调一次这个就够了。
        """
        if not self.root or not self.entries:
            return False
        if self._stamp() == self._stamp_at:
            return False
        self.build(self.root, force=True, cache_path=self._cache_path)
        return True

    # ---- 查询 -----------------------------------------------------------
    def find(self, md5: str) -> LibraryEntry | None:
        if not md5:
            return None
        return self._by_md5.get(md5.lower())

    # ---- 音频 -----------------------------------------------------------
    def find_audio(self, path: str) -> str:
        """给定谱面路径，尽量找到能用的音频；找不到返回空串。

        分三步：
          1. 谱面自己的目录（同捆音频，可能是 mp3/ogg/…，扩展名不一定和 AudioFilename 一致）；
          2. 同图集（BeatmapSetID 相同）的其他谱面目录 —— lazer 的 files 存储里
             每个文件单独按哈希存放、没有同捆音频，只能去别处借；
          3. 曲名 + 艺术家相同的其他谱面目录（有些目录名不带 set id）。
        结果按路径缓存，同一次分析不会重复读盘。
        """
        if not path:
            return ""
        cached = self._audio_cache.get(path)
        if cached is not None:
            return cached
        found = ""
        try:
            bm = parse_osu_file(path)
            found = bm.audio_path
            if not found:
                set_id = str(bm.meta.get("BeatmapSetID") or "").strip()
                if set_id and set_id not in ("0", "-1"):
                    found = self._audio_of_set(set_id)
            if not found:
                found = self._audio_of_title(bm.title, bm.artist)
        except Exception:  # noqa: BLE001
            found = ""
        self._audio_cache[path] = found
        return found

    def _audio_of_set(self, set_id: str) -> str:
        """按 BeatmapSetID 找同图集里任意一张谱面的音频。

        stable/lazer 的 Songs 目录名通常以「set id + 空格 + 曲名」开头，
        所以拿目录名去匹配即可，不必把每张谱面都解析一遍。
        """
        cached = self._set_audio_cache.get(set_id)
        if cached is not None:
            return cached
        found = ""
        seen: set[str] = set()
        for e in self.entries:
            parent = os.path.basename(os.path.dirname(e.path))
            if parent in seen:
                continue
            seen.add(parent)
            if not (parent == set_id or parent.startswith(set_id + " ")):
                continue
            got = resolve_audio(os.path.dirname(e.path))
            if got:
                found = got
                break
        self._set_audio_cache[set_id] = found
        return found

    def _audio_of_title(self, title: str, artist: str) -> str:
        """按「曲名 + 艺术家」找同图集的音频（给目录名里没有 set id 的情况兜底）。"""
        key = (title or "").strip().lower() + "|" + (artist or "").strip().lower()
        if not key.strip("|"):
            return ""
        cached = self._set_audio_cache.get(key)
        if cached is not None:
            return cached
        found = ""
        seen: set[str] = set()
        for e in self.entries:
            parent = os.path.basename(os.path.dirname(e.path)).lower()
            if parent in seen:
                continue
            seen.add(parent)
            if (title or "").lower() not in parent or (artist or "").lower() not in parent:
                continue
            got = resolve_audio(os.path.dirname(e.path))
            if got:
                found = got
                break
        self._set_audio_cache[key] = found
        return found

    def _meta(self, entry: LibraryEntry) -> dict:
        """谱面的（artist/title/difficulty）元数据，带内存缓存。

        lazer 存储里的文件名是哈希，按文件名搜索完全搜不到，
        只能解析内容。这里解析一次就缓存，后续搜索不再读盘。
        """
        meta = self._meta_cache.get(entry.path)
        if meta is not None:
            return meta
        meta = {}
        try:
            bm = parse_osu_file(entry.path)
            meta = {
                "artist": (bm.artist or "").lower(),
                "title": (bm.title or "").lower(),
                "difficulty": (bm.difficulty_name or "").lower(),
                "keys": bm.keys,
            }
        except Exception:  # noqa: BLE001
            meta = {}
        self._meta_cache[entry.path] = meta
        return meta

    def search(self, query: str, limit: int = 30) -> list[LibraryEntry]:
        q = (query or "").strip().lower()
        if not q:
            return self.entries[:limit]
        hits = []
        late: list[LibraryEntry] = []
        for e in self.entries:
            name = os.path.basename(e.path).lower()
            parent = os.path.basename(os.path.dirname(e.path)).lower()
            if q in name or q in parent:
                hits.append(e)
                continue
            # 哈希文件名（lazer 存储）按不到名字，交给元数据解析（有缓存，不贵）
            if len(name) >= 40 and "." not in name:
                late.append(e)
        if len(hits) < limit:
            for e in late:
                meta = self._meta(e)
                if q in meta.get("title", "") or q in meta.get("artist", "") or q in meta.get("difficulty", ""):
                    hits.append(e)
                    if len(hits) >= limit:
                        break
        return hits[:limit]

    def stats(self) -> dict:
        return {
            "root": self.root,
            "count": len(self.entries),
            "builtAt": self.built_at,
            "scanned": self.scanned_files,
            "reused": self.reused_files,
            "hashed": self.hashed_files,
            "buildMs": round(self.build_ms, 1),
        }


# 进程内单例（服务端只跑一个曲库索引）
_LIB = Library()
_CACHE_PATH = ""


def set_cache_path(path: str) -> None:
    global _CACHE_PATH
    _CACHE_PATH = path


def get_library(root: str | None = None, force: bool = False) -> Library:
    """取曲库索引。

    root 为空时用默认根并复用单例；传入自定义根时**建一个临时实例**，
    避免一次"指定错的目录"把全局缓存和目标覆盖掉。
    """
    if root:
        # 单例还没建过（或根相同）→ 用单例，这样能享受落盘缓存；
        # 只有"另一个目录"才建临时实例，避免污染。
        if not _LIB.root or os.path.abspath(root) == os.path.abspath(_LIB.root):
            _LIB.build(root, force=force, cache_path=_CACHE_PATH)
            return _LIB
        tmp = Library()
        tmp.build(root, force=force, cache_path="")
        return tmp
    if not _LIB.entries:
        roots = default_songs_roots()
        if roots:
            _LIB.build(roots[0], cache_path=_CACHE_PATH)
    return _LIB


def reset_library() -> None:
    """丢掉进程内索引，下次 get_library 会按新的根重建。

    玩家在网页上改了游戏目录后必须调用：否则单例还指着旧目录，
    新设的路径看起来"不生效"。
    """
    global _LIB
    _LIB = Library()
