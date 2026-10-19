"""本地服务：给页面提供 API + 静态资源 + 音频（带 Range，方便拖进度条）。

只监听 127.0.0.1，不对外。
启动：python app/server.py [--port 8806] [--no-open]
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from app import paths  # noqa: E402
from app.core import audio_fetch  # noqa: E402
from app.core.analyze import AnalyzeError, analyze, set_msd_cache_path  # noqa: E402
from app.core.beatmap import parse_osu_file  # noqa: E402
from app.core.library import default_songs_roots, get_library, set_cache_path  # noqa: E402
from app.core.osr import OsrError  # noqa: E402
from app.core.skin import find_note_images, find_skin_in_exports  # noqa: E402
from app.version import __version__  # noqa: E402

WEB_DIR = paths.WEB_DIR
CACHE_DIR = paths.CACHE_DIR
os.makedirs(CACHE_DIR, exist_ok=True)

# 曲库索引落盘：第二次启动直接读，不再把所有 .osu 重新算一遍 MD5
set_cache_path(os.path.join(CACHE_DIR, "library_index.json"))
# MSD 只跟谱面有关，按谱面 md5 缓存；同一张图第二次打开就是 0 开销
set_msd_cache_path(os.path.join(CACHE_DIR, "msd.json"))
LOG_PATH = paths.LOG_PATH
# 实际监听的端口：端口被占时会自动往后挪，写在这里让"打开浏览器"的脚本拿到正确的地址
PORT_FILE = paths.PORT_FILE
# 进程号：exe 版靠它实现"再运行一次 → 选择停止服务"
PID_FILE = paths.PID_FILE

# 上传体积上限：正常回放几百 KB，给到 32MB 已经很宽松
MAX_BODY_BYTES = 32 * 1024 * 1024

# 进程启动时刻。Python 只在 import 时读一次模块，改完 .py 不重启的话跑的还是旧代码：
# 表现是"前端新、后端旧"，新字段悄悄消失（例如时间轴缺了 UR 那条带），却不报任何错。
# 这里拿启动时刻和磁盘上 .py 的 mtime 比一比，让页面能明确提示"去重启"，而不是让人猜。
STARTED_AT = time.time()


class _StaleCodeWatcher:
    """进程启动后有没有 .py 被改过。

    做成对象是为了把"上次检查时刻 / 上次结论"这对状态收在一处：它们必须一起更新，
    散成模块级变量时很容易只改一半（缓存时间变了、结论没刷新）。
    结果缓存 2 秒，避免每个请求都 stat 一遍。
    """

    CACHE_SECONDS = 2.0
    # mtime 比启动时刻大 0.5 秒以上才算改过：文件系统时间戳有粒度，同秒内的差异不可信
    TOLERANCE = 0.5

    def __init__(self, dirs: tuple[str, ...], started_at: float):
        self._dirs = dirs
        self._started_at = started_at
        self._checked_at = 0.0
        self._stale = False

    def _newest_mtime(self) -> float:
        newest = 0.0
        for d in self._dirs:
            try:
                for fn in os.listdir(d):
                    if fn.endswith(".py"):
                        newest = max(newest, os.path.getmtime(os.path.join(d, fn)))
            except OSError:
                continue
        return newest

    def stale(self) -> bool:
        now = time.time()
        if now - self._checked_at < self.CACHE_SECONDS:
            return self._stale
        self._checked_at = now
        self._stale = self._newest_mtime() > self._started_at + self.TOLERANCE
        return self._stale


_STALE_WATCHER = _StaleCodeWatcher((paths.APP_DIR, os.path.join(paths.APP_DIR, "core")), STARTED_AT)


def stale_code() -> bool:
    """进程启动后有没有 .py 被改过（供 /api/health 使用）。

    exe 形态下没有可改的 .py，解压时间戳还会被误判成"改过"，直接报 False。
    """
    if paths.FROZEN:
        return False
    return _STALE_WATCHER.stale()

# 站点图标（内联 SVG，省得浏览器每次开页面都在控制台刷一条 favicon 404）
FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    b'<rect width="24" height="24" rx="6" fill="#141a33"/>'
    b'<rect x="6" y="9" width="2.6" height="7" rx="1.3" fill="#7c5cff"/>'
    b'<rect x="10.7" y="5.5" width="2.6" height="10.5" rx="1.3" fill="#22d3ee"/>'
    b'<rect x="15.4" y="11" width="2.6" height="5" rx="1.3" fill="#7c5cff"/>'
    b'</svg>'
)

# 备用通道：把 .osr 放进这个目录，服务端直接读，完全不经过浏览器的上传
LOCAL_REPLAY_DIR = paths.LOCAL_REPLAY_DIR
os.makedirs(LOCAL_REPLAY_DIR, exist_ok=True)

# 分片上传的中转目录：直接上传被本机安全软件/代理吞掉时的自动兜底通道
CHUNK_DIR = os.path.join(CACHE_DIR, "chunks")
CHUNK_MAX = 64 * 1024  # 单片上限，与前端约定一致


def log(msg: str) -> None:
    line = "%s %s\n" % (time.strftime("%H:%M:%S"), msg)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass

class SessionStore:
    """分析结果与媒体路径的进程内缓存（session_id → 会话）。

    做成对象而不是裸 dict，是因为它有三个必须一起守住的约束：
    写入时要顺手清理过期项、读写要串行、媒体 URL 的拼法要和存法一致。
    散落在 Handler 里，改其中一条时很容易漏掉另外两条。
    """

    TTL_SECONDS = 3600 * 6
    # 单份分析结果就是几百 KB～1MB（含 notes/errors），不封顶的话，反复拖几十次
    # 会把内存吃到几百 MB。只保留最近这些份，更早的按"会话过期"处理（前端重传即可）。
    MAX_ITEMS = 40

    def __init__(self) -> None:
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(self, payload: dict, audio: str, background: str, note_skin: dict | None = None,
               beatmap_path: str = "", beatmap_md5: str = "") -> str:
        sid = "%x%04x" % (int(time.time() * 1000), int(time.time_ns()) % 0xFFFF)
        skin = note_skin or {"columns": [], "lnBody": "", "lnTail": ""}
        now = time.time()
        with self._lock:
            self._items[sid] = {
                "analysis": payload,
                "audio": audio,
                "background": background,
                "noteImages": list(skin.get("columns") or []),
                "lnBody": skin.get("lnBody") or "",
                "lnTail": skin.get("lnTail") or "",
                # 留着谱面路径与 MD5：本地找不到音频时，靠它们去镜像站补一份
                "beatmapPath": beatmap_path or "",
                "beatmapMd5": beatmap_md5 or "",
                "created": now,
            }
            for k in [k for k, v in self._items.items() if now - v["created"] > self.TTL_SECONDS]:
                self._items.pop(k, None)
            while len(self._items) > self.MAX_ITEMS:
                oldest = min(self._items, key=lambda k: self._items[k]["created"])
                self._items.pop(oldest, None)
        return sid

    def get(self, sid: str) -> dict | None:
        with self._lock:
            return self._items.get(sid)

    def set_audio(self, sid: str, path: str) -> bool:
        """把后来补到的音频挂回会话：媒体路由是按会话取路径的，改这里就生效。"""
        with self._lock:
            item = self._items.get(sid)
            if not item:
                return False
            item["audio"] = path
            return True


SESSIONS = SessionStore()


def _json_default(o):
    return str(o)


def _parse_json(raw: bytes):
    """请求体 → JSON 对象；不是合法 JSON 对象时返回 None。

    五个 POST 接口都要做这一步，各写一遍 try/except 的结果是"有的接口报 400、
    有的直接 500"——统一在这里，错误处理只剩一处。顶层必须是对象：
    数组或标量会让后面的 req.get(...) 直接抛 AttributeError，变成 500。
    """
    try:
        obj = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


class BodyTooLarge(Exception):
    """请求体超过上限。单独一个类型，是为了让传输层给出 413 而不是通用 500。"""


def _write_temp(directory: str, suffix: str, data: bytes) -> str:
    """把上传内容落盘，文件名保证唯一。

    早先用「秒级时间戳 + 进程号」：同一秒内的两个请求会落到同一个文件名上。
    Windows 下后一个 open("wb") 直接 PermissionError（整个请求 500）；
    更隐蔽的情况是它覆盖掉前一个请求正在读的文件，分析到一半的内容被换掉。
    本机多开一个标签页、或者前端自动重试，都能撞上。
    """
    os.makedirs(directory, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="up_", suffix=suffix, dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except Exception:  # noqa: BLE001
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path


class _AssetMixin:
    """静态文件与媒体（带 Range）的响应。

    与接口无关：不解析请求体、不建会话，只按路径找文件并写响应。
    拆出来的直接好处是改缓存头或 Range 逻辑时，不用在两百行接口代码里翻。
    """

    # ---- 静态文件 -------------------------------------------------------
    def _asset_version(self) -> str:
        """用静态目录里最新的 mtime 当版本号：改了任何前端文件，URL 就变，浏览器必然重新拉。"""
        newest = 0.0
        for dp, _dn, fns in os.walk(WEB_DIR):
            for fn in fns:
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(dp, fn)))
                except OSError:
                    pass
        return "%x" % int(newest)

    def _serve_file(self, full: str):
        full = os.path.abspath(full)
        if not full.startswith(os.path.abspath(WEB_DIR)):
            return self._error("非法路径", 403)
        if not os.path.isfile(full):
            return self._error("文件不存在", 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            body = f.read()
        if full.lower().endswith((".html", ".htm")) and b"__ASSET_V__" in body:
            body = body.replace(b"__ASSET_V__", self._asset_version().encode("ascii"))
        self._send(200, body, ctype)

    # ---- 媒体（Range） --------------------------------------------------
    def _serve_media(self, rest: str):
        parts = rest.split("/", 1)
        if len(parts) != 2:
            return self._error("非法媒体路径", 404)
        sid, kind = parts[0], parts[1]
        sess = SESSIONS.get(sid)
        if not sess:
            return self._error("会话已过期，请重新分析", 404)
        if kind in ("lnbody", "lntail"):
            # 长条的身体与尾巴是两套素材：老皮肤只有身体，靠前端自己做兜底
            path = sess.get("lnBody" if kind == "lnbody" else "lnTail") or ""
            if not path or not os.path.isfile(path):
                return self._error("素材不存在", 404)
            return self._send_range(path)
        if kind.startswith("note"):
            idx_txt = kind[4:]
            if not idx_txt.isdigit():
                return self._error("非法素材路径", 404)
            cols = sess.get("noteImages") or []
            i = int(idx_txt)
            path = cols[i] if 0 <= i < len(cols) else ""
            if not path or not os.path.isfile(path):
                return self._error("素材不存在", 404)
            return self._send_range(path)
        path = sess.get("audio") if kind == "audio" else sess.get("background") if kind == "bg" else None
        if not path or not os.path.isfile(path):
            return self._error("媒体文件不存在", 404)
        return self._send_range(path)

    def _send_range(self, path: str):
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if range_header:
            m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
            if m:
                first, last = m.group(1), m.group(2)
                if not first and last:
                    # 后缀 Range：bytes=-N 表示"最后 N 个字节"。
                    # 早先按 0..N 处理，下载器或播放器拖到末尾时会拿到开头的内容。
                    suffix = min(int(last), size)
                    start, end = size - suffix, size - 1
                else:
                    if first:
                        start = int(first)
                    if last:
                        end = int(last)
                    elif first:
                        end = size - 1
                if start < 0 or start > end or start >= size:
                    self.close_connection = True
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.end_headers()
                    return
                partial = True
        length = end - start + 1
        self.close_connection = True
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Connection", "close")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                chunk = 256 * 1024
                while remaining > 0:
                    data = f.read(min(chunk, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        except (BrokenPipeError, ConnectionAbortedError):
            pass


class _AnalysisApiMixin:
    """分析相关接口：曲库、谱面元数据、分析、会话与媒体 URL。

    只依赖传输层的四个原语（_read_body / _json / _error / _send）和模块级 SESSIONS，
    换 HTTP 框架时这一层可以整体搬走，业务代码本身不用改。
    """

    # ---- API ------------------------------------------------------------
    def _library_info(self):
        roots = default_songs_roots()
        lib = get_library(roots[0] if roots else None)
        info = lib.stats()
        info["roots"] = roots
        info["ok"] = True
        return self._json(info)

    def _analyze(self):
        raw = self._read_body()
        log("[analyze] 收到请求体 %.1f KB" % (len(raw) / 1024.0))
        req = _parse_json(raw)
        if req is None:
            return self._error("请求体不是合法 JSON")

        replay_b64 = req.get("replay") or ""
        if not replay_b64:
            return self._error("没有收到回放文件")
        try:
            replay_bytes = base64.b64decode(replay_b64)
        except Exception:  # noqa: BLE001
            return self._error("回放文件的 base64 解码失败")

        tmp_dir = os.path.join(CACHE_DIR, "uploads")
        replay_path = _write_temp(tmp_dir, ".osr", replay_bytes)

        beatmap_path = None
        # 允许直接指定曲库里的 .osu 路径（本机工具，只校验后缀与存在性）
        requested_path = (req.get("beatmapPath") or "").strip()
        if requested_path:
            if not requested_path.lower().endswith(".osu") or not os.path.isfile(requested_path):
                return self._error("指定的谱面路径无效：%s" % requested_path)
            beatmap_path = requested_path

        beatmap_b64 = req.get("beatmap")
        if beatmap_b64:
            try:
                bm_bytes = base64.b64decode(beatmap_b64)
            except Exception:  # noqa: BLE001
                return self._error("谱面文件的 base64 解码失败")
            beatmap_path = _write_temp(tmp_dir, ".osu", bm_bytes)

        songs_root = (req.get("songsRoot") or "").strip() or None
        return self._finish_analyze(replay_path, beatmap_path, songs_root)

    def _finish_analyze(self, replay_path: str, beatmap_path: str | None, songs_root: str | None):
        """分析已落盘的回放并返回 JSON（直接上传 / 分片上传 / 本地文件夹共用）。"""
        payload = analyze(replay_path, beatmap_path=beatmap_path, songs_root=songs_root)
        audio, background, note_skin = _resolve_media(payload, beatmap_path, songs_root)

        # 谱面路径要在这里定下来：上传回放时 beatmap_path 是空的，真正的路径是
        # analyze 内部按 MD5 从曲库解析出来的。不把它解析出来存进会话，
        # 后面「在线补音频」就拿不到 .osu，也就读不出图集 id。
        resolved_bm = beatmap_path or _resolve_bm(payload, songs_root)
        sid = SESSIONS.create(payload, audio, background, note_skin,
                              beatmap_path=resolved_bm or "",
                              beatmap_md5=(payload.get("replay") or {}).get("beatmapMd5") or "")
        payload["session"] = sid
        cols = note_skin.get("columns") or []
        payload["media"] = {
            "audio": bool(audio),
            "background": bool(background),
            # 每列的 note 素材 URL（没找到的列为 null，前端回退到色块）
            "noteImages": [("/api/media/%s/note%d" % (sid, i)) if p else None for i, p in enumerate(cols)],
            "lnBody": ("/api/media/%s/lnbody" % sid) if note_skin.get("lnBody") else None,
            "lnTail": ("/api/media/%s/lntail" % sid) if note_skin.get("lnTail") else None,
        }
        return self._json(payload)

    # ---- 在线补音频 -----------------------------------------------------------
    # 只在 lazer 里下载过的谱面，音频和谱面各自按内容哈希存放、彼此没有关联信息，
    # 本地永远配不上音频。这里按谱面信息去镜像站取一次 .osz，只把音频留下。
    def _audio_fetch(self):
        req = _parse_json(self._read_body())
        if req is None:
            return self._error("请求体不是合法 JSON")
        sid = (req.get("session") or "").strip()
        sess = SESSIONS.get(sid)
        if not sess:
            return self._error("会话已过期，请重新分析", 404)
        if sess.get("audio"):
            return self._json({"ok": True, "already": True, "source": "local",
                               "url": "/api/media/%s/audio" % sid})
        if not audio_fetch.is_enabled(CACHE_DIR):
            return self._json({"ok": False, "disabled": True, "error": "在线补音频已关闭"})

        md5 = (sess.get("beatmapMd5") or "").strip()
        log("[audio] 尝试补音频 %s …" % (md5[:8] or "(无 MD5)"))
        try:
            result = audio_fetch.fetch_audio(sess.get("beatmapPath") or "", md5, CACHE_DIR,
                                             force=bool(req.get("force")))
        except Exception as e:  # noqa: BLE001
            log("[audio] 异常：%s" % e)
            return self._json({"ok": False, "error": "补音频时出错：%s" % e})

        if result.get("ok"):
            SESSIONS.set_audio(sid, result["path"])
            result["url"] = "/api/media/%s/audio" % sid
            log("[audio] 已补上：%s（%s）" % (os.path.basename(result["path"]), result.get("source")))
        else:
            log("[audio] 没补到：%s" % result.get("error"))
        return self._json(result)

    def _audio_pref(self):
        req = _parse_json(self._read_body()) or {}
        if "enabled" in req:
            audio_fetch.set_enabled(CACHE_DIR, bool(req.get("enabled")))
            log("[audio] 在线补音频 = %s" % audio_fetch.is_enabled(CACHE_DIR))
        return self._json({"ok": True, "enabled": audio_fetch.is_enabled(CACHE_DIR),
                           "stats": audio_fetch.stats(CACHE_DIR)})

    def _beatmap_search(self):
        # 搜索是"边打边搜"的：请求体坏掉时退回空查询，比弹一个错更不打扰
        req = _parse_json(self._read_body()) or {}
        roots = default_songs_roots()
        lib = get_library(req.get("songsRoot") or (roots[0] if roots else None))
        hits = lib.search(req.get("query", ""), limit=40)
        out = []
        for e in hits:
            try:
                bm = parse_osu_file(e.path)
            except Exception:  # noqa: BLE001
                continue
            out.append(
                {
                    "path": e.path,
                    "label": "%s - %s [%s]" % (bm.artist, bm.title, bm.difficulty_name),
                    "keys": bm.keys,
                    "od": bm.od,
                    "notes": len(bm.objects),
                }
            )
        return self._json({"ok": True, "count": len(out), "items": out})

    def _beatmap_meta(self):
        req = _parse_json(self._read_body())
        if req is None:
            return self._error("请求体不是合法 JSON")
        b64 = req.get("beatmap") or ""
        if not b64:
            return self._error("没有收到谱面文件")
        try:
            # validate=True：否则 "!!!" 这类垃圾会被静默解成空字节，
            # 然后返回一份「未知艺术家 - 未知曲目」的假元数据，调用方还以为读到了谱面。
            raw = base64.b64decode(b64, validate=True)
        except Exception:  # noqa: BLE001
            return self._error("谱面文件的 base64 解码失败")
        if not raw:
            return self._error("谱面文件是空的")
        path = _write_temp(os.path.join(CACHE_DIR, "uploads"), ".osu", raw)
        try:
            bm = parse_osu_file(path)
        except Exception as e:  # noqa: BLE001
            return self._error("谱面解析失败：%s" % e)
        if not bm.objects:
            return self._error("这份谱面里没有任何物件，确认一下选的是不是 mania 难度")
        return self._json(
            {
                "ok": True,
                "label": "%s - %s [%s]" % (bm.artist, bm.title, bm.difficulty_name),
                "keys": bm.keys,
                "od": bm.od,
                "notes": len(bm.objects),
            }
        )

    def _local_replays(self):
        """列出备用目录里的回放（不经过浏览器上传）。"""
        items = []
        try:
            for fn in sorted(os.listdir(LOCAL_REPLAY_DIR)):
                if fn.lower().endswith(".osr"):
                    p = os.path.join(LOCAL_REPLAY_DIR, fn)
                    items.append({"name": fn, "size": os.path.getsize(p), "mtime": os.path.getmtime(p)})
        except OSError:
            pass
        return self._json({"ok": True, "dir": LOCAL_REPLAY_DIR, "items": items})

    def _analyze_local(self):
        req = _parse_json(self._read_body())
        if req is None:
            return self._error("请求体不是合法 JSON")
        name = os.path.basename((req.get("name") or "").strip())
        if not name.lower().endswith(".osr"):
            return self._error("请选择 _replays 目录里的 .osr 文件")
        replay_path = os.path.join(LOCAL_REPLAY_DIR, name)
        if not os.path.isfile(replay_path):
            return self._error("找不到这个文件：" + name)
        t0 = time.time()
        log("[analyze-local] " + name)
        resp = self._finish_analyze(replay_path, None, (req.get("songsRoot") or None))
        log("[analyze-local] 完成 用时 %.0f ms" % ((time.time() - t0) * 1000))
        return resp


class Handler(_AnalysisApiMixin, _AssetMixin, BaseHTTPRequestHandler):
    """HTTP 传输层：路由、响应头、错误码、日志，以及分片上传。

    业务接口在 _AnalysisApiMixin，静态与媒体在 _AssetMixin —— 这个类只负责"怎么收发"。
    """

    server_version = "ManiaReplayLab/" + __version__
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        try:
            peer = self.client_address[1] if self.client_address else "?"
            log("[conn] 新连接 :%s" % peer)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.connection.settimeout(120.0)
        except OSError:
            pass

    # ---- 基础设施 -------------------------------------------------------
    def log_message(self, fmt, *args):  # 安静一点，只留错误
        if args and str(args[0]).startswith("4"):
            sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def log_request(self, code="-", size="-"):
        """每个请求都进日志（含 GET 与错误码），这样"页面到底请求了什么"一目了然。"""
        try:
            log("[http] %s %s -> %s" % (self.command, self.path.split("?")[0], code))
        except Exception:  # noqa: BLE001
            pass
        super().log_request(code, size)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Asset-Version", self._asset_version())
        # API 一律带 CORS 头：万一页面是从 file:// 或别的端口打开的，POST 也不会被浏览器拦
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=_json_default).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, msg: str, code: int = 400, **extra):
        self._json({"ok": False, "error": msg, **extra}, code)

    def _read_body(self, limit: int = MAX_BODY_BYTES) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise BodyTooLarge(
                "上传内容太大（%d 字节，上限 %d）——回放文件一般只有几百 KB，确认一下选的是 .osr"
                % (length, limit)
            )
        return self.rfile.read(length) if length > 0 else b""

    # ---- 路由 -----------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/favicon.ico":
                # 浏览器每次开页面都会要它，没有就会在控制台刷一条 404
                return self._send(200, FAVICON, "image/svg+xml")
            if path in ("/", "/index.html"):
                return self._serve_file(os.path.join(WEB_DIR, "index.html"))
            if path.startswith("/static/"):
                rel = path[len("/static/"):]
                return self._serve_file(os.path.join(WEB_DIR, rel))
            if path == "/api/beacon":
                ev = (qs.get("ev") or ["?"])[0][:40]
                data = (qs.get("data") or [""])[0][:600]
                log("[beacon] %s  %s" % (ev, data))
                body = b"ok"
                self.send_response(204)
                self.send_header("Connection", "close")
                self.send_header("Content-Length", "0")
                self.end_headers()
                self.close_connection = True
                return
            if path == "/api/health":
                return self._json({"ok": True, "version": __version__, "time": time.time(),
                                   "assetV": self._asset_version(),
                                   # stale=进程比代码旧（改完 .py 没重启）：页面据此提示用户重启
                                   "stale": stale_code()})
            if path == "/api/library":
                return self._library_info()
            if path == "/api/local-replays":
                return self._local_replays()
            if path == "/api/open-replays":
                try:
                    os.makedirs(LOCAL_REPLAY_DIR, exist_ok=True)
                    os.startfile(LOCAL_REPLAY_DIR)  # noqa: S606 - 本地工具，打开自己的目录
                except Exception as e:  # noqa: BLE001
                    return self._error("打不开文件夹：%r" % (e,))
                return self._json({"ok": True, "dir": LOCAL_REPLAY_DIR})
            if path.startswith("/api/media/"):
                return self._serve_media(path[len("/api/media/"):])
            return self._error("未知路径: " + path, 404)
        except Exception:  # noqa: BLE001
            sys.stderr.write(traceback.format_exc())
            return self._error("服务器内部错误", 500)

    def do_OPTIONS(self):
        log("[req] OPTIONS %s  Origin=%s" % (self.path, self.headers.get("Origin")))
        self.close_connection = True
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        # 一到达就记一行：这样"请求有没有跑到后端"一眼能看出来
        declared = self.headers.get("Content-Length")
        log("[req] POST %s  Content-Length=%s" % (path, declared))
        try:
            if path == "/api/analyze":
                return self._analyze()
            if path == "/api/upload-chunk":
                return self._upload_chunk()
            if path == "/api/analyze-chunked":
                return self._analyze_chunked()
            if path == "/api/beatmap-search":
                return self._beatmap_search()
            if path == "/api/beatmap-meta":
                return self._beatmap_meta()
            if path == "/api/analyze-local":
                return self._analyze_local()
            if path == "/api/audio-fetch":
                return self._audio_fetch()
            if path == "/api/audio-pref":
                return self._audio_pref()
            if path == "/api/echo":
                body = self._read_body()
                return self._json({"ok": True, "received": len(body), "echo": body[:64].decode("utf-8", "replace")})
            return self._error("未知路径: " + path, 404)
        except BodyTooLarge as e:
            return self._error(str(e), 413)
        except socket.timeout:
            log("[req] 读请求体超时（浏览器没有把内容发完）")
            return self._error("没收到完整的上传内容（读取超时）。请重新拖一次文件", 200)
        except AnalyzeError as e:
            return self._error(str(e), 200, needsInput=True)
        except OsrError as e:
            return self._error("回放文件解析失败：%s" % e, 200)
        except Exception:  # noqa: BLE001
            sys.stderr.write(traceback.format_exc())
            return self._error("服务器内部错误：" + traceback.format_exc(limit=2).strip().splitlines()[-1], 500)


    # ---- 分片上传（兜底通道） -------------------------------------------
    # 直接上传 /api/analyze 时，个别机器上的安全软件/代理会把较大的 POST 体整包吞掉
    # （server.log 里表现为 health 之后没有任何 POST 到达）。把回放切成小片逐个发，
    # 每片都是独立的小请求，能绕开这种「整包拦截」，还能逐片重试。
    def _upload_chunk(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        sid = re.sub(r"[^0-9a-zA-Z]", "", (qs.get("sid") or [""])[0])[:40]
        idx = (qs.get("i") or ["0"])[0]
        if not sid or not idx.isdigit():
            return self._error("分片参数无效")
        body = self._read_body(CHUNK_MAX + 1024)
        if not body:
            return self._error("分片内容为空")
        d = os.path.join(CHUNK_DIR, sid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "%05d.part" % int(idx)), "wb") as f:
            f.write(body)
        return self._json({"ok": True, "i": int(idx), "bytes": len(body)})

    def _analyze_chunked(self):
        req = _parse_json(self._read_body())
        if req is None:
            return self._error("请求体不是合法 JSON")
        sid = re.sub(r"[^0-9a-zA-Z]", "", (req.get("sid") or ""))[:40]
        d = os.path.join(CHUNK_DIR, sid)
        if not sid or not os.path.isdir(d):
            return self._error("分片数据丢失，请重新上传")
        # 按序号排（不是按文件名字典序），并且要求 0..n-1 连续。
        # 缺片时直接把剩下的拼起来，会得到一份"能解出前半段"的残卷，
        # 结果看起来正常、实际只分析了其中一部分。
        numbered = []
        for fn in os.listdir(d):
            if not fn.endswith(".part"):
                continue
            stem = fn[: -len(".part")]
            if stem.isdigit():
                numbered.append((int(stem), fn))
        numbered.sort()
        idxs = [i for i, _ in numbered]
        if not numbered:
            return self._error("分片数据为空，请重新上传")
        if idxs != list(range(len(idxs))):
            missing = sorted(set(range(max(idxs) + 1)) - set(idxs))[:5]
            return self._error("分片不连续（缺第 %s 片），请重新上传" % "、".join(str(i) for i in missing))
        parts = [fn for _, fn in numbered]

        replay_path = _write_temp(os.path.join(CACHE_DIR, "uploads"), ".osr", b"")
        with open(replay_path, "wb") as out:
            for fn in parts:
                with open(os.path.join(d, fn), "rb") as f:
                    out.write(f.read())
        try:
            return self._finish_analyze(replay_path, beatmap_path=None, songs_root=(req.get("songsRoot") or None))
        finally:
            try:
                for fn in parts:
                    os.remove(os.path.join(d, fn))
                os.rmdir(d)
            except OSError:
                pass


def _resolve_media(payload: dict, beatmap_path: str | None, songs_root: str | None) -> tuple[str, str, dict]:
    """找出这次分析要用的音频 / 背景 / note 素材。

    全都找不到也不算错误：前端会退化成"虚拟时钟 + 色块"。所以这里吞掉异常只记日志，
    绝不能让一个坏掉的皮肤目录把整次分析变成 500。
    """
    empty_skin = {"columns": [], "lnBody": "", "lnTail": ""}
    keys = int((payload.get("beatmap") or {}).get("keys") or 4)
    bm_path = beatmap_path or _resolve_bm(payload, songs_root)
    if not bm_path:
        return "", "", empty_skin

    audio = background = ""
    parsed = False
    try:
        bm = parse_osu_file(bm_path)
        background = bm.background_path
        audio = bm.audio_path
        parsed = True
    except Exception:  # noqa: BLE001
        log("[analyze] 解析谱面（取音频/背景）失败：\n" + traceback.format_exc())

    # 音频：先在谱面自己的目录里找同捆音频（扩展名可能是 ogg 而不是 mp3）；
    # lazer 的 files 存储里没有同捆音频，这时再去同图集的其他谱面目录借。
    if parsed and not audio:
        try:
            audio = get_library(songs_root).find_audio(bm_path)
        except Exception:  # noqa: BLE001
            audio = ""
        if not audio:
            # 曲库里没有，但之前在线补过一份的话，直接拿来用 —— 第二次打开这张图
            # 就不需要再走一次「先无声、再等前端补」的来回
            md5 = (payload.get("replay") or {}).get("beatmapMd5") or ""
            audio = audio_fetch.cached_audio(CACHE_DIR, md5) or ""
        if not audio:
            log("[analyze] 没找到音频：谱面目录 %s 里没有同捆音频文件" % os.path.dirname(bm_path))

    try:
        note_skin = find_note_images(bm_path, keys)
        if not any(note_skin.get("columns") or []):
            note_skin = find_skin_in_exports(keys)
    except Exception:  # noqa: BLE001
        log("[analyze] 找 note 素材失败：\n" + traceback.format_exc())
        note_skin = empty_skin
    return audio, background, note_skin


def _resolve_bm(payload: dict, songs_root: str | None) -> str:
    """分析结果里带了 md5，尝试再定位一次谱面路径（给音频用）。"""
    md5 = (payload.get("beatmap") or {}).get("md5") or ""
    if not md5:
        return ""
    lib = get_library(songs_root)
    e = lib.find(md5)
    return e.path if e else ""


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _clean_chunks() -> None:
    """启动时清掉上次运行遗留的分片目录，以及超过一天的上传副本。

    分片正常流程用完即删；上传副本每次分析都会留一份，留着方便排查，
    但不清会一直长（一张谱几百 KB 到几 MB）。
    """
    try:
        if os.path.isdir(CHUNK_DIR):
            for name in os.listdir(CHUNK_DIR):
                d = os.path.join(CHUNK_DIR, name)
                for fn in os.listdir(d):
                    try:
                        os.remove(os.path.join(d, fn))
                    except OSError:
                        pass
                try:
                    os.rmdir(d)
                except OSError:
                    pass
    except OSError:
        pass

    try:
        cutoff = time.time() - 24 * 3600
        uploads = os.path.join(CACHE_DIR, "uploads")
        for fn in os.listdir(uploads):
            p = os.path.join(uploads, fn)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="mania 回放分析器本地服务")
    ap.add_argument("--port", type=int, default=8806)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--songs", default="", help="手动指定 osu! Songs 目录")
    args = ap.parse_args()

    if args.songs:
        os.environ["MANIA_SONGS_ROOT"] = args.songs

    _clean_chunks()

    port = args.port
    for _ in range(20):
        if _port_free(port):
            break
        port += 1

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    url = "http://127.0.0.1:%d/" % port
    # 端口被占时会往后挪，把真实端口写出去，免得打开的是上一次的旧服务
    try:
        with open(PORT_FILE, "w", encoding="ascii") as f:
            f.write(str(port))
        with open(PID_FILE, "w", encoding="ascii") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    print("[mania-replay-lab] 服务已启动：%s" % url, flush=True)
    if port != args.port:
        print("[mania-replay-lab] 提示：%d 端口被占用，改用了 %d（旧实例可以先关掉）" % (args.port, port), flush=True)
    print("[mania-replay-lab] 曲库：%s" % (", ".join(default_songs_roots()) or "未找到"), flush=True)

    # 后台预热曲库索引（不阻塞启动）
    def warm():
        roots = default_songs_roots()
        if roots:
            try:
                lib = get_library(roots[0])
                st = lib.stats()
                print("[mania-replay-lab] 曲库索引完成：%d 张谱面（新算 %d / 复用 %d，%.0f ms）"
                      % (len(lib.entries), st["hashed"], st["reused"], st["buildMs"]), flush=True)
                log("[startup] 曲库索引 %d 张（新算 %d 复用 %d，%.0f ms）"
                    % (len(lib.entries), st["hashed"], st["reused"], st["buildMs"]))
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=warm, daemon=True).start()

    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
