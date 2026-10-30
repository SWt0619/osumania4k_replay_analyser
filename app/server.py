"""本地服务：给页面提供 API + 静态资源 + 音频（带 Range，方便拖进度条）。

只监听 127.0.0.1，不对外。
启动：python app/server.py [--port 8806] [--no-open]
"""

from __future__ import annotations

import argparse
import base64
import io
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
import urllib.request
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from app import paths  # noqa: E402
from app.core import (  # noqa: E402
    audio_fetch,
    cache_admin,
    config_store,
    crashreport,
    dan,
    finder,
    history,
    hotkey,
    json_safe,
    map_download,
    osu_dirs,
    practice,
    progress,
    quick_import,
    recommend,
    release_notes,
    replay_store,
    report,
)
from app.core.analyze import (  # noqa: E402
    SUPPORTED_KEYS,
    AnalyzeError,
    analyze,
    analyze_chart,
    set_msd_cache_path,
    unsupported_keys_message,
)
from app.core.beatmap import long_path, parse_osu_file, parse_osu_text  # noqa: E402
from app.core.library import (  # noqa: E402
    default_songs_roots,
    get_library,
    last_build_at,
    progress_snapshot,
    reset_library,
    set_cache_path,
)
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

# 页面关掉/刷新/拖进度条时连接会被对端掐断，写响应就会抛这几个异常。
# 它们不是服务端故障，不该写 crashreport，也不该在日志里标成"失败"。
CLIENT_GONE = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)

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

LOG_MAX_BYTES = 5 * 1024 * 1024


def log(msg: str) -> None:
    line = "%s %s\n" % (time.strftime("%H:%M:%S"), msg)
    try:
        # 超过 5MB 就轮转成 .1，只留上一份：这份日志每个请求写一行，
        # 不轮转的话开几个月就是几十兆，出问题时反而不好翻
        if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


# 备用通道：把 .osr 放进这个目录，服务端直接读，完全不经过浏览器的上传
LOCAL_REPLAY_DIR = paths.LOCAL_REPLAY_DIR
try:
    os.makedirs(LOCAL_REPLAY_DIR, exist_ok=True)
except OSError as _e:      # 只读目录：这一栏用不了，但不该让整个服务起不来
    log("[startup] 备用通道目录建不出来：%r" % (_e,))

# 分片上传的中转目录：直接上传被本机安全软件/代理吞掉时的自动兜底通道
CHUNK_DIR = os.path.join(CACHE_DIR, "chunks")
CHUNK_MAX = 64 * 1024  # 单片上限，与前端约定一致


def _write_err(text: str) -> None:
    """往 stderr 写一行 —— exe（--noconsole）里 stderr 是 None，直接写会再抛一次。"""
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write(text if text.endswith("\n") else text + "\n")
    except (OSError, ValueError, AttributeError):
        pass


def _report_crash(event: str, exc: BaseException, context: dict | None = None) -> str | None:
    """把异常写进日志与 crashreport，返回报告路径（写不出来为 None）。

    顺序是有讲究的：先落日志、再落报告、最后才往 stderr 打字。
    stderr 在打包形态下不存在，放最后能保证前两步一定执行过。
    """
    try:
        trace = traceback.format_exc()
    except Exception:  # noqa: BLE001
        trace = repr(exc)
    log("[%s] %s" % (event, crashreport.summary(exc)))
    path = None
    try:
        path = crashreport.write(event, exc, context=context)
    except Exception:  # noqa: BLE001 - 报告模块自己也不该炸，但兜一层
        path = None
    if path:
        log("[%s] 崩溃报告：%s" % (event, path))
    _write_err(trace)
    return path


# 只接受本机来源：Host 必须指向回环地址（挡 DNS rebinding），
# Origin 如果存在，主机部分也必须是回环（挡"别的网站借浏览器读本机数据"）。
_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1", "[::1]")


def _host_ok(value: str) -> bool:
    text = (value or "").strip().lower()
    if not text:
        return True                       # HTTP/1.0 客户端可能不带 Host
    if text.startswith("["):              # [::1]:8806
        name = text.partition("]")[0] + "]"
    else:
        name = text.partition(":")[0]
    return name in _LOOPBACK_NAMES


def _origin_ok(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return True                       # 同源 GET、curl、离线报告都不带 Origin
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return False
    return (parts.scheme in ("http", "https")
            and (parts.hostname or "").lower() in ("127.0.0.1", "localhost", "::1"))


def _version_key(text: str) -> tuple[int, ...]:
    """把 1.5.0 / v1.5.0-beta 变成可比较的元组（只取前三段数字）。"""
    parts = re.findall(r"\d+", str(text or ""))
    return tuple(int(x) for x in parts[:3]) or (0,)


def _field(req: dict, key: str) -> str:
    """取一个字符串字段。类型不对就当没填 —— 直接 .strip() 会打出一个 500。"""
    value = req.get(key)
    return value.strip() if isinstance(value, str) else ""

class Watchdog:
    """「页面关了就退服务」的实现。

    之所以要做：这是个从浏览器打开的本地工具，用户关掉标签页之后进程还挂着，
    下次再启动就是第二个实例，还得手动去结束进程——体验很差。

    判据是页面定期发心跳：
      * 页面每 10 秒 GET /api/alive?c=<每次打开页面生成的随机 id>；
      * 关闭/刷新时用 sendBeacon 发 /api/bye?c=<id>。

    两个时间参数都是按浏览器的实际行为定的：
      * BEAT_TTL 75 秒 —— 后台标签页会把定时器降到约 1 分钟一次，留够余量，
        否则用户一切到别的窗口服务就自己死了；
      * BYE_GRACE 8 秒 —— 刷新会先 bye 再重新加载，新页面几乎立刻报到，
        这个窗口既够它报到，又让"真的关掉"在 8 秒内收尾。
    """

    BEAT_TTL = 75.0
    BYE_GRACE = 8.0

    def __init__(self) -> None:
        self._clients: dict[str, float] = {}   # id -> 到期时刻
        self._lock = threading.Lock()
        self.armed = False                     # 至少有过一次心跳才启用，避免"没人打开过"就退出

    def beat(self, cid: str) -> None:
        if not cid:
            return
        with self._lock:
            self.armed = True
            self._clients[cid] = time.time() + self.BEAT_TTL

    def bye(self, cid: str) -> None:
        if not cid:
            return
        with self._lock:
            now = time.time()
            deadline = now + self.BYE_GRACE
            old = self._clients.get(cid)
            self._clients[cid] = deadline if old is None else min(old, deadline)

    def any_alive(self) -> bool:
        now = time.time()
        with self._lock:
            for cid, exp in list(self._clients.items()):
                if exp <= now:
                    self._clients.pop(cid, None)
            return bool(self._clients)


WATCHDOG = Watchdog()


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

# 快捷导入的"最近一次"：页面靠长轮询等 token 变化，几 MB 的 notes 只在变化时传一次。
# busy 单独一个字段：热键一按下就置位并叫醒等待中的请求，页面据此立刻显示"正在分析"，
# 不用等分析跑完才给反馈。
QUICK: dict = {"token": 0, "session": "", "payload": None, "path": "", "mtime": 0.0,
               "error": "", "busy": False}
QUICK_LOCK = threading.Lock()
QUICK_COND = threading.Condition(QUICK_LOCK)
QUICK_BUSY = threading.Lock()


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


def _is_under(path: str, root: str) -> bool:
    """path 是否真的在 root 里面（按目录边界比，不是按字符串前缀）。

    只写 startswith 的话，`app/web_evil/x` 会被当成 `app/web` 下的文件放出去；
    不同盘符时 commonpath 会抛 ValueError，一律当"不在下面"。
    """
    try:
        return os.path.normcase(os.path.commonpath([os.path.abspath(path), os.path.abspath(root)])) \
            == os.path.normcase(os.path.abspath(root))
    except ValueError:
        return False


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


def _range_bounds(header: str | None, size: int) -> tuple[int, int, bool] | None:
    """解析 Range 请求头，返回（起始字节, 结束字节, 是否为部分响应）；不可满足时返回 None。

    抽成纯函数是因为这里的边界最多：后缀写法、显式结尾、越界夹取、416 判定。
    写在发送流程里时，每一支都要和响应头对着读才能确认，很容易漏掉一条。
    """
    if not header:
        return 0, size - 1, False
    m = re.match(r"bytes=(\d*)-(\d*)", header.strip())
    if not m:
        return 0, size - 1, False
    first, last = m.group(1), m.group(2)
    if not first and last:
        # 后缀 Range：bytes=-N 表示"最后 N 个字节"。
        # 早先按 0..N 处理，下载器或播放器拖到末尾时会拿到开头的内容。
        # N 为 0（bytes=-0）是空区间，按 RFC 9110 要回 416 —— 直接返回的话
        # 会算出 start=size、end=size-1，发一个 Content-Range 起止颠倒的 206。
        if int(last) <= 0:
            return None
        return size - min(int(last), size), size - 1, True
    start = int(first) if first else 0
    if last:
        end = int(last)
    else:
        # 只给了起点（或什么都没给）：一直发到文件末尾
        end = size - 1
    # 显式给了结尾也不能越过文件末尾：Content-Length 会按这个值算，
    # 不夹的话响应头写着 92 字节、实际只发得出 2 字节，
    # 播放器/下载器读到一半就报 IncompleteRead（或一直等）。
    if end > size - 1:
        end = size - 1
    if start < 0 or start > end or start >= size:
        return None
    return start, end, True


def _save_base64_upload(req: dict, key: str, suffix: str) -> tuple[str, str]:
    """把请求体里的 base64 文件字段落盘，返回（路径, 错误说明）。

    两个「选谱面」的入口（预检元数据 / 只分析谱面）做的是同一件事，
    提示语也必须一致——分开写时一边说"没有收到谱面文件"、另一边说"谱面解码失败"，
    用户只会以为自己选错了文件。
    """
    text = req.get(key) or ""
    if not text:
        return "", "没有收到谱面文件"
    try:
        # validate=True：否则 "!!!" 这类垃圾会被静默解成空字节，
        # 然后返回一份「未知艺术家 - 未知曲目」的假元数据，调用方还以为读到了谱面。
        raw = base64.b64decode(text, validate=True)
    except Exception:  # noqa: BLE001
        return "", "谱面文件的 base64 解码失败"
    if not raw:
        return "", "谱面文件是空的"
    return _write_temp(os.path.join(CACHE_DIR, "uploads"), suffix, raw), ""


class _SessionView:
    """会话存储的取用点。

    mixin 里所有「按 sid 拿会话」都从这里走，而不是直接读模块级 SESSIONS。
    这样既保留一处全局状态（本来就需要跨请求共享），又给测试留了一个替换点：
    继承这个类并覆盖 sessions 就能塞一份假会话，不必启动完整服务。
    """

    @property
    def sessions(self) -> "SessionStore":
        return SESSIONS

    def _session(self, sid: str) -> dict | None:
        """按 sid 取会话；取不到时调用方负责给出「会话已过期」。"""
        return self.sessions.get(sid) if sid else None


class _AssetMixin(_SessionView):
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
        if not _is_under(full, WEB_DIR):
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
        sess = self._session(sid)
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
        bounds = _range_bounds(self.headers.get("Range"), size)
        if bounds is None:
            self.close_connection = True
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.end_headers()
            return
        start, end, partial = bounds
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

    def _send_download(self, path: str, name: str, job_id: str = ""):
        """把下好的谱面包交给浏览器存盘。

        `Content-Disposition: attachment` 是关键：页面导航到这个地址只会触发下载，
        当前页不会被顶掉，所以前端可以直接 `location.assign`。
        """
        size = os.path.getsize(path)
        ascii_name = re.sub(r"[^A-Za-z0-9._ ()\[\]-]", "_", name) or "beatmap.osz"
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition",
            'attachment; filename="%s"; filename*=UTF-8\'\'%s'
            % (ascii_name, urllib.parse.quote(name)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                remaining = size
                while remaining > 0:
                    data = f.read(min(256 * 1024, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        # 整包发完就删掉临时文件：用户已经拿到手，没必要在缓存里再占几十 MB。
        # 中途断开的保留，重新点一次封面还能直接发。
        if remaining <= 0 and job_id:
            map_download.drop(job_id)


def _mask_user_path(path: str) -> str:
    """把路径里的本机账号名换成占位词。

    设置页的「例如 …」很容易被截图发到群里、issue 里；带着自己的 Windows 用户名
    等于顺手把账号名送出去了。示例只要**形状**对就够用，不需要是真的。
    """
    home = os.path.expanduser("~")
    if not path or not home:
        return path
    if path.lower().startswith(home.lower()):
        return r"C:\Users\你的用户名" + path[len(home):]
    return path


class _ApiMixin(_SessionView):
    """曲库与谱面查询接口：只读本机文件系统与曲库索引，不碰分析流程。

    依赖传输层的原语（_read_body / _json / _error，由 Handler 提供），
    换 HTTP 框架时这一层可以整体搬走，业务代码不用改。
    """

    # ---- 曲库 -----------------------------------------------------------
    def _library_info(self):
        roots = default_songs_roots()
        lib = get_library(roots[0] if roots else None)
        info = lib.stats()
        info["roots"] = roots
        info["ok"] = True
        return self._json(info)

    def _history_dan(self, req: dict):
        """把页面估算出的段位写回这一局。

        段位卡片和成长页必须是同一个数：卡片显示 Roxy 11.17，折合却按 MSD 标定的
        12.39 去算，用户会以为折合虚高。所以页面算完就回填，成长页直接读库。
        """
        key = str(_field(req, "key") or "").strip()
        # dan 是 JSON 数字，_field 只认字符串，这里直接取原值
        try:
            value = float(req.get("dan"))
        except (TypeError, ValueError):
            return self._error("dan 必须是数字")
        if not key:
            return self._error("缺少回放标识")
        old = (history.get(key) or {}).get("dan_value")
        # 页面在分析结果里带了 historyNew：为真说明这一局是本次刚写进库的
        fresh = bool(req.get("fresh"))
        updated = history.update_dan_value(key, value)
        log("[dan] %s -> %.2f (%s)" % (key[:8], value, "更新" if updated else "没有对应记录"))
        # 成长页的综合段位是服务端折合的，所以"这一局让段位动了多少"也在这里算：
        # 前端拿到就画在总览页，不用自己再抄一遍折合公式。
        return self._json({
            "ok": True,
            "updated": updated,
            "change": _dan_change(key, old, fresh),
        })

    def _history_replay(self, req: dict):
        """按回放 MD5 找回 .osr 并重跑一遍分析：成长页点一格就回到那一局的页面。

        找不到文件就是找不到 —— 回放被删掉、被移走、或者当初根本没留底，
        都返回 404 让页面提示"回放不存在"。这里不猜、不降级成只看摘要，
        因为摘要里没有逐音数据，画不出回放页。
        """
        key = re.sub(r"[^0-9a-fA-F]", "", _field(req, "key"))[:40].lower()
        # 16 位是 sha1 前缀（replayMd5 为空的老回放），放宽到这一档才找得回
        if len(key) < 16:
            return self._error("回放编号不完整")
        src = replay_store.find(key)
        if not src:
            return self._error("回放不存在：原文件可能已被删除或移走", 404)
        log("[history-replay] %s <- %s" % (key[:8], os.path.basename(src)))
        # store=False：库里已经有这一局，重算只给页面看，不再改写存档
        return self._json(_analyze_and_store(src, None, None, store=False))

    # ---- 游戏目录（自动探测失败时让玩家在网页里手动指定） ----------------
    def _paths_info(self):
        roots = default_songs_roots()
        lib = get_library(roots[0] if roots else None)
        appdata = os.environ.get("APPDATA") or ""
        return self._json({
            "ok": True,
            "stableDir": osu_dirs.config_stable_dir(),
            "lazerDir": osu_dirs.config_lazer_dir(),
            "songsRoot": roots[0] if roots else "",
            "songsRoots": roots,
            # 配置文件到底写在哪。存不住的时候这行是最快的判据：
            # 如果它每次都指向不同的临时目录，说明程序是从压缩包里直接跑的。
            "configPath": config_store.path_of(CACHE_DIR),
            "configShared": bool(paths.CACHE_FALLBACK),
            "count": len(lib.entries),
            # 给输入框做占位提示：告诉玩家该选到哪一级。
            # 这里只给"形状"，不放本机的真实路径 —— 截图外发时不会带出账号名。
            "examples": {
                "stable": r"D:\osu!",
                "lazer": _mask_user_path(os.path.join(appdata, "osu")) if appdata
                         else r"C:\Users\你的用户名\AppData\Roaming\osu",
            },
        })

    def _paths_unchanged(self):
        """两栏都空时的响应：保持原设置，形状与保存成功时一致。

        以前这里走 remove 分支，把已经存好的路径删掉：页面一刷新/一重启就发现
        "路径又没了"，其实是这个空提交干的。空表单不该有副作用，只有显式带 clear
        才真的清除。
        """
        roots = default_songs_roots()
        lib = get_library(roots[0] if roots else None)
        log("[paths] 空表单 -> 保持原设置不变")
        return self._json({
            "ok": True,
            "unchanged": True,
            "songsRoot": roots[0] if roots else "",
            "songsRoots": roots,
            "count": len(lib.entries),
            "buildMs": 0,
            "notes": [],
            "hint": "两栏都空着，已保留原来的设置。要清掉手动目录请点「清除手动路径」。",
        })

    def _paths_save(self, req: dict):
        stable = _field(req, "stableDir").strip('"').strip()
        lazer = _field(req, "lazerDir").strip('"').strip()

        if not stable and not lazer and not req.get("clear"):
            return self._paths_unchanged()

        notes = []
        if stable:
            ok, msg = osu_dirs.inspect_stable_dir(stable)
            if not ok:
                return self._error("稳定版目录有问题：" + msg)
            notes.append("稳定版：" + msg)
        if lazer:
            ok, msg = osu_dirs.inspect_lazer_dir(lazer)
            if not ok:
                return self._error("lazer 目录有问题：" + msg)
            notes.append("lazer：" + msg)

        changes = {}
        remove = []
        for key, val in (("stableDir", stable), ("lazerDir", lazer)):
            if val:
                changes[key] = val
            else:
                remove.append(key)
        if stable or lazer:
            # 手动指定了游戏目录，就把更粗略的 songsRoot 清掉，免得两套配置打架
            remove.append("songsRoot")
        elif req.get("clear"):
            log("[paths] 按请求清除手动指定的目录")
        try:
            config_store.merge(changes, remove, cache_dir=CACHE_DIR)
        except OSError as e:
            return self._error("保存配置失败：%s" % e, 500)

        # 配置变了：清探测缓存 + 丢进程内索引，按新路径重建（可能要几秒）
        osu_dirs.reset_cache()
        reset_library()
        roots = default_songs_roots()
        t0 = time.time()
        lib = get_library(roots[0] if roots else None, force=True)
        ms = round((time.time() - t0) * 1000, 1)
        log("[paths] stable=%s lazer=%s -> roots=%s (%d 张, %.0fms)"
            % (stable or "-", lazer or "-", roots, len(lib.entries), ms))
        count = len(lib.entries)
        if count:
            hint = ""
        elif not roots:
            # 目录能打开，但没有可以当"根"的地方：稳定版少了 Songs，也没有
            # osu!.<用户名>.cfg 指向别的目录。这时确实该让人去改路径。
            hint = ("这个目录里没有可扫描的曲库：稳定版要选到含 Songs 的那一级，"
                    "lazer 要选到含 files 的数据目录。")
        else:
            # 根找到了、也扫过了，只是里面没有谱面。别再说"目录选错了"——
            # 那会让人反复去改一个本来就对的路径（issue #7 就是这么绕进去的）。
            hint = ("目录读到了，但里面还没有谱面：先在游戏里导入这张图再回来试，"
                    "或者直接在弹窗里手动选 .osu。")
        return self._json({
            "ok": True,
            "songsRoot": roots[0] if roots else "",
            "songsRoots": roots,
            "count": count,
            "buildMs": ms,
            "notes": notes,
            "hint": hint,
        })

    # ---- 谱面查询 -------------------------------------------------------
    def _beatmap_search(self):
        # 搜索是"边打边搜"的：请求体坏掉时退回空查询，比弹一个错更不打扰
        req = _parse_json(self._read_body()) or {}
        # 刚下好的图也要能搜到（索引有 1 小时 TTL，不能等它自己过期）
        _refresh_library()
        roots = default_songs_roots()
        lib = get_library(_field(req, "songsRoot") or (roots[0] if roots else None))
        hits = lib.search(_field(req, "query"), limit=40)
        out = []
        hidden = 0
        for e in hits:
            try:
                bm = parse_osu_file(e.path)
            except Exception as e:  # noqa: BLE001
                # 单张谱面坏了不该让整次搜索失败，但也不能悄悄消失 ——
                # 之前这里只 continue，用户只会觉得"曲库里怎么没这张图"。
                log("[search] 跳过解析失败的谱面：%s（%s）" % (os.path.basename(e.path), e))
                continue
            # 非 4K 的谱面不进列表：选了也会在分析时被拒，列出来只是让人白点一次。
            # 但要如实告诉用户"隐藏了几张"，否则会以为曲库里没有这张图。
            if bm.keys != SUPPORTED_KEYS:
                hidden += 1
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
        return self._json({"ok": True, "count": len(out), "items": out, "hidden": hidden})

    def _beatmap_meta(self):
        req = _parse_json(self._read_body())
        if req is None:
            return self._error("请求体不是合法 JSON")
        path, problem = _save_base64_upload(req, "beatmap", ".osu")
        if problem:
            return self._error(problem)
        try:
            bm = parse_osu_file(path)
        except Exception as e:  # noqa: BLE001
            return self._error("谱面解析失败：%s" % e)
        if not bm.objects:
            return self._error("这份谱面里没有任何物件，确认一下选的是不是 mania 难度")
        # 键数在"选谱面"这一步就挡住：等回放重新上传一遍再报错太绕，
        # 而且用户多半是选错了同一图集里的 7K 难度。
        if bm.keys != SUPPORTED_KEYS:
            return self._error(unsupported_keys_message(bm))
        return self._json(
            {
                "ok": True,
                "label": "%s - %s [%s]" % (bm.artist, bm.title, bm.difficulty_name),
                "keys": bm.keys,
                "od": bm.od,
                "notes": len(bm.objects),
            }
        )

    # ---- 只分析谱面（不看成绩） -----------------------------------------
    # 用户手里只有 .osu、没有回放时也能看难度与键型构成。
    # 不做成绩分析，所以不需要会话、音频、媒体路由。
    def _chart_only(self):
        req = _parse_json(self._read_body())
        if req is None:
            return self._error("请求体不是合法 JSON")
        path, problem = _save_base64_upload(req, "osu", ".osu")
        if problem:
            return self._error(problem)
        try:
            return self._json(analyze_chart(path))
        except AnalyzeError as e:
            return self._error(str(e))
        except Exception as e:  # noqa: BLE001
            return self._error("谱面解析失败：%s" % e)

    # ---- 检查更新 --------------------------------------------------------
    # 只有用户打开「关于」页时才会请求，且只问 GitHub 的 latest release。
    # ---- 快捷导入的快捷键 ------------------------------------------------
    def _hotkey_info(self):
        st = hotkey.status()
        return self._json({
            "ok": True,
            "combo": st.get("combo") or "",
            "active": bool(st.get("ok")),
            "error": st.get("error") or "",
            "supported": sys.platform == "win32",
        })

    def _hotkey_save(self):
        req = _parse_json(self._read_body())
        if req is None:
            return self._error("请求体不是合法 JSON")
        ok, why = _apply_hotkey(_field(req, "combo"))
        if not ok:
            return self._error(why, 400)
        return self._hotkey_info()

    # ---- 成长评估 --------------------------------------------------------
    def _progress_info(self):
        """把本地的每局存档折成「离下一段还有多远」。"""
        profile = _profile_from_config()
        sessions = history.all_sessions()
        data = progress.evaluate(sessions, profile)
        data["history"] = history.stats()
        # 没填本人名字时不算"有多少局"：这时候还算不出该用哪些局
        data["needPlayer"] = not bool(profile.get("player"))
        return self._json(data)

    def _changelog(self):
        """关于页的更新说明：直接读仓库根的 CHANGELOG.md，不另存一份。"""
        return self._json({"ok": True, "versions": release_notes.load()})

    def _profile_save(self):
        req = _parse_json(self._read_body())
        if req is None:
            return self._error("请求体不是合法 JSON")

        player = str(req.get("player") or "").strip()
        if not player:
            return self._error("先填自己的 osu! 名字或 ID：只有本人的成绩才算进成长评估")
        if len(player) > 64:
            return self._error("名字太长了，确认一下填的是 osu! 用户名")

        kind = _field(req, "type") or "reform"
        if kind not in ("reform", "malody"):
            return self._error("段位类型只能是 reform 或 malody")
        try:
            # JSON 里的数字会被 _field 当成非字符串丢掉，这里直接读原值再转
            value = float(req.get("value"))
        except (TypeError, ValueError):
            return self._error("段位数值不对")
        if kind == "reform":
            if not -3 <= value <= 17:
                return self._error("REFORM 段位范围是 INTRO-1 到 Eta")
            dan_value, label = value, progress.dan_label(value)
        else:
            if not 0 <= value <= 20:
                return self._error("Malody 段位范围是常规 0~10、Extra 1~9 或 Extra Final")
            conv = progress.malody_to_reform(value)
            if not conv:
                return self._error("这个 Malody 段位还没有标定数据")
            dan_value, label = conv["dan"], conv["label"]

        prof = {"type": kind, "value": value, "dan": dan_value, "label": label,
                "scale": progress.DAN_SCALE}
        if kind == "malody":
            prof["malodyLabel"] = progress.malody_label(value)
            # measured = 这个换算是把段位谱丢进混合算法测出来的；False 表示用的是老标定兜底
            prof["measured"] = bool(conv.get("measured"))
        try:
            config_store.merge({"danProfile": prof, "playerName": player}, cache_dir=CACHE_DIR)
        except OSError as e:
            return self._error("保存失败：%s" % e)

        # 名字一改，全部历史立刻按新名字重算归属：别人打的那几局当场退出评估
        counts = history.set_player(player)
        return self._json({"ok": True, "profile": prof, "player": player, "counts": counts})

    def _check_update(self):
        # 调试用：本地版本通常已经是最新，想测「发现新版本」分支时设这个环境变量。
        # 例如 MANIA_FAKE_LATEST=9.9.9 会假装 GitHub 上有个 9.9.9；不设就走真实请求。
        fake = (os.environ.get("MANIA_FAKE_LATEST") or "").strip().lstrip("vV")
        if fake:
            return self._json({
                "ok": True,
                "current": __version__,
                "latest": fake,
                "hasUpdate": _version_key(fake) > _version_key(__version__),
                "url": ("https://github.com/SWt0619/osumania4k_replay_analyser"
                        "/releases/latest"),
                "notes": "调试用的假更新信息（MANIA_FAKE_LATEST）",
            })
        api = ("https://api.github.com/repos/SWt0619/osumania4k_replay_analyser"
               "/releases/latest")
        try:
            req = urllib.request.Request(api, headers={
                "User-Agent": "osumania-replay-analyser/" + __version__,
                "Accept": "application/vnd.github+json",
            })
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            latest = str(data.get("tag_name") or "").lstrip("vV")
            return self._json({
                "ok": True,
                "current": __version__,
                "latest": latest,
                "hasUpdate": _version_key(latest) > _version_key(__version__),
                "url": data.get("html_url") or (
                    "https://github.com/SWt0619/osumania4k_replay_analyser/releases/latest"),
                "notes": (data.get("body") or "")[:600],
            })
        except Exception as e:  # noqa: BLE001 - 没网/被墙/限流都不该影响页面
            return self._json({"ok": False, "current": __version__, "error": str(e)[:160]})

    # ---- 段位估算用的谱面原文 -------------------------------------------
    # 段位估算是前端算的（算法是 JS），它要的是谱面原文而不是我们解析后的 JSON；
    # 所以单独开一个接口，按会话把 .osu 文本给过去。页面只在打开难度页时才请求。
    def _beatmap_text(self, qs):
        # 两种取法：按分析会话（页面上刚导入的那份），或按谱面 MD5（历史记录里
        # 只有哈希，补算段位时要靠它去曲库里找回 .osu）。
        md5 = (qs.get("md5") or [""])[0].strip().lower()
        if md5:
            entry = get_library().find(md5)
            if not entry:
                return self._error("曲库里找不到这张谱面，无法估算段位", 404)
            path = entry.path
        else:
            sid = (qs.get("session") or [""])[0]
            sess = self._session(sid)
            if not sess:
                return self._error("会话已过期，请重新分析", 404)
            path = sess.get("beatmapPath") or ""
        if not path or not os.path.isfile(path):
            return self._error("找不到这张谱面的文件，无法估算段位", 404)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            return self._error("读取谱面失败：%s" % e, 500)
        if len(text) > 4 * 1024 * 1024:
            return self._error("谱面文件异常大，已放弃估算", 413)
        return self._json({"ok": True, "name": os.path.basename(path), "text": text})


    # ---- 导出的图片 ------------------------------------------------------
    def card_save(self, qs):
        """把成绩卡 / B50 存进程序目录的 card/。

        前端画在 canvas 上，本来只能走浏览器下载、落到「下载」文件夹里还得自己翻。
        本机程序不如直接写回自己目录：成绩卡按「序号_曲名_日期」命名，B50 按
        「B50_玩家_日期」命名；万一撞名（同一天导两次 B50）再补 (2)。
        """
        try:
            body = self._read_body(limit=16 * 1024 * 1024)
        except BodyTooLarge as e:
            return self._error(str(e), 413)
        if not body:
            return self._error("没有收到图片数据", 400)
        if not body.startswith(b"\x89PNG\r\n\x1a\n"):
            return self._error("只接受 PNG 图片", 400)
        kind = ((qs.get("kind") or [""])[0] or "").strip().lower()
        if kind in ("score", "b50"):
            # 序号只在成绩卡上用：B50 是同一天的汇总图，没有逐张递增的含义
            name = _card_name(kind,
                              title=(qs.get("title") or [""])[0],
                              ident=(qs.get("id") or [""])[0],
                              seq=_card_seq(paths.CARD_DIR),
                              judge=(qs.get("judge") or [""])[0])
        else:
            # 老前端只传 name，保留这条路径，升级到一半也不会存不进去
            name = _card_file_name((qs.get("name") or [""])[0])
        try:
            os.makedirs(paths.CARD_DIR, exist_ok=True)
            dest = _unique_path(os.path.join(paths.CARD_DIR, name))
            with open(dest, "wb") as f:
                f.write(body)
        except OSError as e:
            return self._error("保存图片失败：%s" % e, 500)
        return self._json({
            "ok": True,
            "dir": os.path.basename(paths.CARD_DIR),
            "name": os.path.basename(dest),
        })

    def _cache_clean(self):
        """清掉设置页勾选的缓存项，返回释放了多少并带上新的占用。

        只认 cache_admin 里登记过的名字：拼错或老前端传了别的键就忽略，
        免得一个手滑的参数把整个 _cache 删掉。
        """
        req = _parse_json(self._read_body())
        if req is None:
            return self._error("请求体不是合法 JSON")
        targets = req.get("targets")
        if not isinstance(targets, list):
            return self._error("targets 需要是数组")
        result = cache_admin.clean(CACHE_DIR, [str(t) for t in targets])
        log("[cache] 清理 %s：释放 %.1f MB / %d 个文件"
            % (",".join(result["names"]) or "（无匹配项）",
               result["freed"] / 1048576.0, result["removed"]))
        result["ok"] = True
        result["stats"] = cache_admin.stats(CACHE_DIR)
        return self._json(result)


class _AnalyzeApiMixin(_SessionView):
    """分析接口：上传（整包 / 分片 / 本地目录）→ analyze() → 建会话 → 带 media URL 的 JSON。

    会话在这里创建，媒体 URL 也在这里拼：两者必须用同一个 sid，
    分开写会拼出「会话存在、URL 指向另一个 id」这种只在播放时才暴露的问题。
    """

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
            # 与 _beatmap_meta 用同一口径：不校验的话 "!!!" 会被静默解成空字节，
            # 后面报的是「文件是空的」，用户看不出真正的问题是这段请求体坏了。
            replay_bytes = base64.b64decode(replay_b64, validate=True)
        except Exception:  # noqa: BLE001
            return self._error("回放文件的 base64 解码失败")

        tmp_dir = os.path.join(CACHE_DIR, "uploads")
        replay_path = _write_temp(tmp_dir, ".osr", replay_bytes)

        beatmap_path = None
        # 允许直接指定曲库里的 .osu 路径（本机工具，只校验后缀与存在性）
        requested_path = _field(req, "beatmapPath")
        if requested_path:
            # 用 long_path 判存在：真实曲库里确实有超过 260 字符的路径，
            # 直接 isfile 会把工具自己刚索引到的谱面判成"不存在"
            if not requested_path.lower().endswith(".osu") or not os.path.isfile(long_path(requested_path)):
                return self._error("指定的谱面路径无效：%s" % requested_path)
            beatmap_path = requested_path

        beatmap_b64 = req.get("beatmap")
        if beatmap_b64:
            try:
                bm_bytes = base64.b64decode(beatmap_b64)
            except Exception:  # noqa: BLE001
                return self._error("谱面文件的 base64 解码失败")
            beatmap_path = _write_temp(tmp_dir, ".osu", bm_bytes)

        songs_root = _field(req, "songsRoot") or None
        return self._finish_analyze(replay_path, beatmap_path, songs_root)

    def _finish_analyze(self, replay_path: str, beatmap_path: str | None, songs_root: str | None):
        """分析已落盘的回放并返回 JSON（直接上传 / 分片上传 / 本地文件夹共用）。"""
        return self._json(_analyze_and_store(replay_path, beatmap_path, songs_root))

    # ---- 快捷导入（先按 F2 导出，再按 Shift+F2）--------------------------
    def _quick_import(self):
        """手动触发一次「分析最近导出的回放」。热键走的也是同一条逻辑。"""
        req = _parse_json(self._read_body()) or {}
        payload, problem = _quick_payload(bool(req.get("force")))
        if problem:
            return self._error(problem, 404)
        with QUICK_LOCK:
            return self._json({"ok": True, "token": QUICK["token"],
                               "session": payload.get("session", "")})

    def _quick_pending(self, qs):
        """页面长轮询：等到"有新结果"或"开始分析了"再回。

        `wait` 是这次最多等几秒（页面给 25 秒）。等待期间不占 CPU——线程挂在条件变量上，
        热键一按下就 `notify_all` 叫醒，所以"按下 → 页面显示加载"几乎没有延迟。
        """
        try:
            since = int((qs.get("since") or ["0"])[0])
        except ValueError:
            since = 0
        try:
            wait = float((qs.get("wait") or ["0"])[0])
        except ValueError:
            wait = 0.0
        wait = max(0.0, min(wait, 25.0))
        deadline = time.time() + wait
        with QUICK_COND:
            while wait > 0 and QUICK["token"] <= since and not QUICK["busy"]:
                left = deadline - time.time()
                if left <= 0:
                    break
                QUICK_COND.wait(left)
            token = QUICK["token"]
            busy = bool(QUICK["busy"])
            fresh = token > since
            data = QUICK["payload"] if fresh else None
            error = (QUICK.get("error") or "") if fresh else ""
        # 先放锁再写响应：大 payload 落盘/发送要几毫秒，不能让别的请求跟着等
        return self._json({
            "ok": True,
            "token": token,
            "busy": busy,
            "data": data,
            "error": error,
            "combo": hotkey.status().get("combo") or "",
            "hotkey": hotkey.status().get("ok"),
        })

    # ---- 相似谱面 -------------------------------------------------------
    # 难度轴是"段位"，而段位由前端用 Sunny 算（后端没有 JS 运行时），
    # 所以这里接收前端传来的段位标签，用会话里已有的键型构成去索引里找。
    def _recommend(self):
        req = _parse_json(self._read_body()) or {}
        sid = _field(req, "session")
        sess = self._session(sid)
        if not sess:
            return self._error("会话已过期，请重新分析", 404)
        dan = _field(req, "dan")
        try:
            items = recommend.for_session(sess.get("analysis") or {}, dan, limit=5)
        except Exception as e:  # noqa: BLE001 - 推荐失败不能影响分析结果
            log("[recommend] 失败：%r" % (e,))
            items = []
        return self._json({"ok": True, "items": items})

    # ---- 随机找图 -------------------------------------------------------
    def _random_charts(self, qs):
        """按段位与键型随机抽几张图。全部读随附索引，不联网。"""
        def one(key, default=""):
            # 默认值可能传的是数字（num() 就是这么用的），不转字符串会在 .strip() 上炸
            return str((qs.get(key) or [default])[0] or default).strip()

        def num(key, default):
            try:
                return float(one(key, default))
            except (TypeError, ValueError):
                return default

        exclude = []
        for part in one("exclude").split(","):
            part = part.strip()
            if part.isdigit():
                exclude.append(int(part))
        try:
            # mode 决定用常规段位（rc）还是面条段位（ln），两者互斥
            payload = finder.pick(one("mode", "rc"), num("dan", 6.0), one("cat"),
                                  int(num("n", 8)), exclude)
        except Exception as e:  # noqa: BLE001 - 找图失败不该把页面带崩
            log("[finder] 失败：%r" % (e,))
            return self._error("找图出错了：%s" % (e,), 500)
        return self._json(payload)

    # ---- 备用通道：直接读 _replays 目录 ---------------------------------
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
        name = os.path.basename(_field(req, "name"))
        if not name.lower().endswith(".osr"):
            return self._error("请选择 _replays 目录里的 .osr 文件")
        replay_path = os.path.join(LOCAL_REPLAY_DIR, name)
        if not os.path.isfile(replay_path):
            return self._error("找不到这个文件：" + name)

        # 这条通道以前只能靠曲库自己认谱面；曲库里没有时，用户选了 .osu 也没处传，
        # 只能看着「没有收到回放文件」。现在两种指定方式都收：
        # beatmapPath 是曲库里的路径，beatmap 是从本机选的 .osu（base64）。
        beatmap_path = None
        beatmap_b64 = req.get("beatmap") or ""
        if beatmap_b64:
            try:
                bm_bytes = base64.b64decode(beatmap_b64, validate=True)
            except Exception:  # noqa: BLE001
                return self._error("谱面文件的 base64 解码失败")
            beatmap_path = _write_temp(os.path.join(CACHE_DIR, "uploads"), ".osu", bm_bytes)
        requested_path = _field(req, "beatmapPath")
        if requested_path:
            if not requested_path.lower().endswith(".osu") or not os.path.isfile(long_path(requested_path)):
                return self._error("指定的谱面路径无效：%s" % requested_path)
            beatmap_path = requested_path

        t0 = time.time()
        log("[analyze-local] " + name)
        resp = self._finish_analyze(
            replay_path, beatmap_path, _field(req, "songsRoot") or None
        )
        log("[analyze-local] 完成 用时 %.0f ms" % ((time.time() - t0) * 1000))
        return resp


class _AudioApiMixin(_SessionView):
    """在线补音频接口。

    只在 lazer 里下载过的谱面，音频和谱面各自按内容哈希存放、彼此没有关联信息，
    本地永远配不上音频。这里按会话里记的谱面路径去镜像站取一次 .osz，只把音频留下。
    """

    def _audio_fetch_status(self, qs):
        """补音频的实时进度，给页面轮询用。

        补音频本身是一个同步 POST，要下整包、最长 45 秒；页面靠这个只读接口
        把"正在下 3.2 / 12.4 MB"写出来，否则那 45 秒里界面一动不动。
        只读内存里的记录，不碰网络也不拿下载锁 —— 拿到锁的话会和正在下载的
        那个线程排在一起，轮询就成了第二个卡住的东西。
        """
        sid = ((qs.get("session") or [""])[0] or "").strip()
        sess = self._session(sid)
        if not sess:
            return self._json({"ok": False, "error": "会话已过期"})
        md5 = (sess.get("beatmapMd5") or "").strip()
        return self._json({"ok": True, "progress": audio_fetch.progress(md5)})

    def _audio_fetch(self):
        req = _parse_json(self._read_body())
        if req is None:
            return self._error("请求体不是合法 JSON")
        sid = _field(req, "session")
        sess = self._session(sid)
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
            self.sessions.set_audio(sid, result["path"])
            result["url"] = "/api/media/%s/audio" % sid
            log("[audio] 已补上：%s（%s）" % (os.path.basename(result["path"]), result.get("source")))
        else:
            log("[audio] 没补到：%s" % result.get("error"))
        return self._json(result)

    def _mirror_test(self):
        """测一次本机到各镜像站的延迟（设置页的「测试连接」）。

        每次点击真的发三个 HEAD：结果只代表"此刻这条线路"，
        不做缓存，也不在后台偷偷定期跑 —— 那是往公共镜像站白送请求。
        """
        try:
            results = audio_fetch.test_mirrors()
        except Exception as e:  # noqa: BLE001 - 测速本身不该把页面带崩
            log("[mirror] 测速失败：%r" % (e,))
            return self._error("测试镜像站连接失败：%s" % (e,), 500)
        log("[mirror] 延迟：" + "，".join(
            "%s %s" % (r["name"], ("%d ms" % r["ms"]) if r.get("ok") else "失败")
            for r in results))
        return self._json({"ok": True, "results": results})

    def _audio_pref(self):
        req = _parse_json(self._read_body()) or {}
        if "enabled" in req:
            audio_fetch.set_enabled(CACHE_DIR, bool(req.get("enabled")))
            log("[audio] 在线补音频 = %s" % audio_fetch.is_enabled(CACHE_DIR))
        return self._json({"ok": True, "enabled": audio_fetch.is_enabled(CACHE_DIR),
                           "stats": audio_fetch.stats(CACHE_DIR)})


# ---------------------------------------------------------------- 练习谱面
# 一次「练这一段」的中间结果：算好的 .osu 文本 + 音频路径，等浏览器把变速后的 WAV
# 送回来再打包。它是临时票据不是缓存，所以只活一小时、最多留 20 条。
_CUTS: dict[str, dict] = {}
# 票据表会被多个请求线程同时读写（裁切在 POST 线程、取音频在另一个 GET 线程）
_CUTS_LOCK = threading.Lock()
_CUT_TTL = 3600
_CUT_MAX = 20
# 服务端接受的范围比 practice.cut 内部（0.1~2.0）窄：再往外就不是"练习"了，
# 生成的谱面在游戏里也没法正常打
_CUT_RATE_MIN = 0.5
_CUT_RATE_MAX = 2.0


def _cut_params(req: dict) -> tuple[float, float, float, bool]:
    """解析裁切请求，返回（起点, 终点, 倍率, 是否全曲）。

    倍率由前端从建议里的 rates 取，默认 0.9：慢练 0.85~0.95、冲速度 1.05~1.1
    都走这一条路径。服务端只管把范围夹住。
    """
    try:
        t0 = float(req.get("t0"))
        t1 = float(req.get("t1"))
    except (TypeError, ValueError):
        raise ValueError("缺少要裁切的时间段")
    try:
        rate = float(req.get("rate") or 0.9)
    except (TypeError, ValueError):
        rate = 0.9
    if not _CUT_RATE_MIN <= rate <= _CUT_RATE_MAX:
        raise ValueError("倍率超出范围（%.1f~%.1f）" % (_CUT_RATE_MIN, _CUT_RATE_MAX))
    return t0, t1, rate, bool(req.get("full"))


def _cut_media(text: str, path: str) -> tuple[str, str, str]:
    """找出裁切后要用的音频与背景，返回（音频, 背景路径, 背景文件名）。

    音频优先取同目录同捆的那份；lazer 的 files 存储会把它拆走，
    这时退到补音频缓存（同一图包下过整包的音频也在那里）。
    找不到音频时返回空串，由调用方决定怎么提示。
    """
    audio = ""
    md5 = ""
    bg_path = ""
    bg_name = ""
    try:
        bm = parse_osu_text(text, path)
        md5 = bm.md5 or ""
        audio = bm.audio_path
        # 切片要能显示背景：谱面里那行坐标还指着原图，包里没这张图就会是一片空白
        bg_name = bm.background_filename
        bg_path = bm.background_path
    except Exception:  # noqa: BLE001 - 读不出音频信息不该让裁切整体失败
        audio = ""
    if not audio:
        try:
            audio = audio_fetch.cached_audio(CACHE_DIR, md5)
        except Exception:  # noqa: BLE001
            audio = ""
    if not audio or not os.path.isfile(audio):
        return "", bg_path, bg_name
    return audio, bg_path, bg_name


def _card_file_name(name: str) -> str:
    """把前端给的文件名压成一层安全的名字。

    只取基名、再替掉 Windows 的保留字符：这张图会写到 card/ 下，
    名字里带 `..` 或盘符就不能让它跑到别的目录去。
    """
    base = os.path.basename(str(name or "").replace("\\", "/"))
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .")
    if not base:
        base = "card"
    if not base.lower().endswith(".png"):
        base += ".png"
    return base[:120]


def _card_text(value: str, fallback: str) -> str:
    """把曲名 / 玩家名压成能进文件名的样子。

    这里特意不取 basename：曲名里带 `/`（「1/2」这种写法）很常见，
    取了基名会把前半截丢掉，换成下划线更接近原意。
    """
    # 连同替换点两侧的空格一起吃掉：「Destiny / Instrumental」出来是
    # 「Destiny_Instrumental」，而不是带着一串空格的「Destiny _ Instrumental」
    text = re.sub(r'\s*[<>:"/\\|?*\x00-\x1f]\s*', "_", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" ._")
    return text or fallback


def _card_seq(folder: str) -> int:
    """card/ 里下一个序号：已有「001_」前缀的最大值 +1。

    只认三位以上、后面紧跟下划线的名字：说明.txt 与 B50_… 不参与编号，
    用户手动塞进这个文件夹的其它图片也不会把序号顶飞。
    """
    top = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 1
    for name in names:
        m = re.match(r"^(\d{3,4})_", name)
        if m:
            top = max(top, int(m.group(1)))
    return top + 1


# 判定口径在文件名里的写法：圈内习惯叫 ScoreV1 / ScoreV2，缩写成 SV1 / SV2。
# classic 是 stable 老回放那套混合口径，没有对应的社区叫法，就不往名字里塞标记。
_JUDGE_TAGS = {"v1": "SV1", "v2": "SV2"}


def _card_name(kind: str, title: str = "", ident: str = "", seq: int = 1,
               today: str = "", judge: str = "") -> str:
    """按类型拼文件名：成绩卡「序号_曲名_SV1|SV2_日期」，B50「B50_玩家_日期」。

    日期默认取本机当天，测试可以传 today 固定下来。
    """
    date = today or time.strftime("%Y-%m-%d")
    if kind == "b50":
        name = "B50_%s_%s.png" % (_card_text(ident, "玩家"), date)
    else:
        parts = ["%03d" % max(1, int(seq)), _card_text(title, "成绩")]
        tag = _JUDGE_TAGS.get((judge or "").strip().lower(), "")
        if tag:
            parts.append(tag)
        parts.append(date)
        name = "_".join(parts) + ".png"
    if len(name) > 120:
        # 曲名过长时优先保前缀和日期，Windows 的整条路径还有 260 字符的限制
        name = name[:-4][:116] + ".png"
    return name


def _unique_path(path: str) -> str:
    """同名文件已存在时加 (2)、(3)…，别把上一张悄悄覆盖掉。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for i in range(2, 1000):
        cand = "%s (%d)%s" % (stem, i, ext)
        if not os.path.exists(cand):
            return cand
    return path


def _prune_cuts_locked() -> None:
    """按 TTL 与条数上限清理票据。调用方必须已持有 _CUTS_LOCK。"""
    now = time.time()
    for key in [k for k, v in _CUTS.items() if now - v["at"] > _CUT_TTL]:
        _CUTS.pop(key, None)
    while len(_CUTS) > _CUT_MAX:
        oldest = min(_CUTS.items(), key=lambda kv: kv[1]["at"])[0]
        _CUTS.pop(oldest, None)


def _put_cut(cid: str, item: dict) -> None:
    """存一张裁切票据，并把表压回上限。

    插入和清理必须进同一把锁：服务端是多线程的，一边插入一边遍历会让字典
    在扩容时抛 RuntimeError，表现是偶发的 500，且只在有人连着点两次时出现。
    """
    with _CUTS_LOCK:
        _CUTS[cid] = item
        _prune_cuts_locked()


class _PracticeMixin(_SessionView):
    """练习谱面：谱面在服务端裁，音频变速在浏览器里做。

    为什么音频不在这里做：本项目没有解码 mp3 / ogg 的能力（纯标准库、不带 ffmpeg），
    而浏览器天生能解。服务端只负责算好谱面与取音区间、收 WAV、打成 .osz。
    """

    def report_advice(self):
        """按页面上那个段位重算建议。

        分析接口返回时估算还没跑（估算器是前端 JS），建议里的练习倍率只能先按
        MSD 兜底；前端算完 Sunny → Azusa → Roxy 把标签传回来，这里只重算 advice，
        不动报告其余部分。
        """
        req = _parse_json(self._read_body()) or {}
        sess = self._session(str(req.get("session") or ""))
        if not sess:
            return self._error("会话已过期，请重新分析", 404)
        payload = sess.get("analysis") or {}
        if not payload:
            return self._error("这份会话没有分析结果", 409)
        label = str(req.get("label") or "").strip()
        value = dan.from_label(label) if label else None
        acc_v1 = payload.get("accuracyV1")
        try:
            ev = report.build_evaluation(payload, value)
        except Exception as e:  # noqa: BLE001 - 重算失败就保持首屏那份
            return self._error("重算建议失败：%s" % e, 500)
        return self._json({
            "ok": True,
            "label": label,
            "dan": value,
            "simple": bool(ev.get("simple")),
            "playalong": bool(ev.get("playalong")),
            # 段位跨过 3.6 这条线时整份评价会换写法（简易 / 完整），
            # 光换建议会留下「新手标题 + 高手短板」这种半新半旧的组合，所以文本一起回传。
            "headline": ev.get("headline") or "",
            "paragraphs": ev.get("paragraphs") or [],
            "strengths": ev.get("strengths") or [],
            "weaknesses": ev.get("weaknesses") or [],
            "rates": report.practice_rates(payload, value, acc_v1),
            "advice": ev.get("advice") or [],
        })

    def practice_cut(self):
        req = _parse_json(self._read_body()) or {}
        sess = self._session(str(req.get("session") or ""))
        if not sess:
            return self._error("会话已过期，请重新分析", 404)
        path = sess.get("beatmapPath") or ""
        if not path or not os.path.isfile(path):
            return self._error("找不到这张谱面的文件", 404)
        try:
            t0, t1, rate, full = _cut_params(req)
        except ValueError as e:
            return self._error(str(e), 400)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            return self._error("读取谱面失败：%s" % e, 500)
        try:
            out = practice.cut(text, t0, t1, rate=rate, full=full)
        except ValueError as e:
            return self._error(str(e), 400)

        audio, bg_path, bg_name = _cut_media(text, path)
        if not audio:
            return self._error("这张谱面的音频不在本地，无法变速；先在设置里补一次音频再试", 409)

        cid = uuid.uuid4().hex[:16]
        _put_cut(cid, {"text": out["text"], "audio": audio, "meta": out,
                       "bg": (bg_path, bg_name), "at": time.time()})
        payload = {k: v for k, v in out.items() if k != "text"}
        payload.update({
            "ok": True,
            "cutId": cid,
            "osuText": out["text"],
            "audioUrl": "/api/practice/audio?cut=" + cid,
            "packUrl": "/api/practice/pack?cut=" + cid,
        })
        log("[practice] 裁切 %s → %d 个物件，%s–%s" % (
            os.path.basename(path), out["objects"], int(t0), int(t1)))
        return self._json(payload)

    def practice_audio(self, qs):
        cid = (qs.get("cut") or [""])[0]
        item = _CUTS.get(cid)
        if not item:
            return self._error("这次生成的练习谱面已过期，请重新生成", 404)
        try:
            with open(item["audio"], "rb") as f:
                body = f.read()
        except OSError as e:
            return self._error("读取音频失败：%s" % e, 500)
        ctype = mimetypes.guess_type(item["audio"])[0] or "application/octet-stream"
        self._send(200, body, ctype)

    def practice_pack(self, qs):
        cid = (qs.get("cut") or [""])[0]
        item = _CUTS.get(cid)
        if not item:
            return self._error("这次生成的练习谱面已过期，请重新生成", 404)
        wav = self._read_body(limit=64 * 1024 * 1024)
        # 只看前四个字节不够：一个 60 字节的 "RIFF"+零 也能过，打进去的包在 osu 里没有声音。
        # 真 WAV 的第 8~12 字节一定是 "WAVE"。
        if len(wav) < 44 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
            return self._error("音频数据无效（不是 WAV）", 400)
        meta = item["meta"]
        buf = io.BytesIO()
        bg_path, bg_name = item.get("bg") or ("", "")
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            z.writestr(meta["fileName"][:-4] + ".osu", item["text"].encode("utf-8"))
            z.writestr(meta["audioName"], wav)
            # 背景图跟着一起打包，否则游戏里这张图是一片底色（谱面那行 Events 还指着它）
            if bg_name and bg_path and os.path.isfile(bg_path):
                z.write(bg_path, bg_name)
        body = buf.getvalue()
        log("[practice] 打包 %s（%.1f MB）" % (meta["fileName"], len(body) / 1048576.0))
        self._send(200, body, "application/octet-stream")

    # ---- 谱面包下载 -----------------------------------------------------
    # 为什么不让浏览器直接分段取：浏览器到镜像之间若挂着会走 h2 的本地代理，
    # 分段请求会被复用成一条连接，反而比单连接更慢。分段放在 Python 这边做。
    def map_download_start(self):
        req = _parse_json(self._read_body()) or {}
        try:
            job = map_download.start(str(req.get("url") or ""),
                                     str(req.get("name") or ""), CACHE_DIR)
        except ValueError as e:
            return self._error(str(e), 400)
        log("[download] 开始 %s（%s）" % (job["name"], job["id"]))
        return self._json({"ok": True, **job})

    def map_download_status(self, qs):
        info = map_download.status((qs.get("id") or [""])[0])
        if info is None:
            return self._error("这次下载已结束或过期，请重新点一次", 404)
        return self._json({"ok": True, **info})

    def map_download_file(self, qs):
        job_id = (qs.get("id") or [""])[0]
        path, name = map_download.path_of(job_id)
        if not path:
            return self._error("文件还没准备好", 404)
        return self._send_download(path, name, job_id)


class _ChunkUploadMixin:
    """分片上传（兜底通道）：把回放切成小片逐个发。

    为什么需要这条通道：直接上传 /api/analyze 时，个别机器上的安全软件/代理会把较大的
    POST 体整包吞掉（server.log 里表现为 health 之后没有任何 POST 到达）。每片都是独立的
    小请求，能绕开这种「整包拦截」，还能逐片重试。

    与路由分开的另一个原因：这里的状态全在磁盘上（`_cache/chunks/<sid>/`），
    与 Handler 的收发逻辑没有交集，混在一起时最难查的是「目录里到底剩了哪些片」。
    """

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

    @staticmethod
    def _collect_chunks(directory: str) -> tuple[list[str], str]:
        """按序号收齐分片，返回（文件名列表, 错误说明）。

        必须按**序号**排（文件名字典序会把第 10 片排到第 2 片前面），
        并且要求 0..n-1 连续：缺片时直接把剩下的拼起来，会得到一份"能解出前半段"的残卷，
        结果看起来正常、实际只分析了其中一部分。
        """
        numbered = []
        for fn in os.listdir(directory):
            if not fn.endswith(".part"):
                continue
            stem = fn[: -len(".part")]
            if stem.isdigit():
                numbered.append((int(stem), fn))
        numbered.sort()
        if not numbered:
            return [], "分片数据为空，请重新上传"
        idxs = [i for i, _ in numbered]
        if idxs != list(range(len(idxs))):
            missing = sorted(set(range(max(idxs) + 1)) - set(idxs))[:5]
            return [], "分片不连续（缺第 %s 片），请重新上传" % "、".join(str(i) for i in missing)
        return [fn for _, fn in numbered], ""

    def _analyze_chunked(self):
        req = _parse_json(self._read_body())
        if req is None:
            return self._error("请求体不是合法 JSON")
        sid = re.sub(r"[^0-9a-zA-Z]", "", _field(req, "sid"))[:40]
        d = os.path.join(CHUNK_DIR, sid)
        if not sid or not os.path.isdir(d):
            return self._error("分片数据丢失，请重新上传")
        parts, problem = self._collect_chunks(d)
        if problem:
            return self._error(problem)

        replay_path = _write_temp(os.path.join(CACHE_DIR, "uploads"), ".osr", b"")
        with open(replay_path, "wb") as out:
            for fn in parts:
                with open(os.path.join(d, fn), "rb") as f:
                    out.write(f.read())
        try:
            return self._finish_analyze(
                replay_path, beatmap_path=None,
                songs_root=_field(req, "songsRoot") or None,
            )
        finally:
            try:
                for fn in parts:
                    os.remove(os.path.join(d, fn))
                os.rmdir(d)
            except OSError:
                pass


class Handler(_ChunkUploadMixin, _AnalyzeApiMixin, _ApiMixin, _AudioApiMixin, _PracticeMixin,
              _AssetMixin, BaseHTTPRequestHandler):
    """HTTP 传输层：路由、响应头、错误码、日志。

    业务分在六个 mixin 里，这个类只负责"怎么收发"与"往哪儿转"：
      * _ApiMixin          曲库、游戏目录、谱面查询、更新检查
      * _AnalyzeApiMixin   分析、会话、media URL
      * _AudioApiMixin     在线补音频
      * _PracticeMixin     裁切练习谱面、打包 .osz
      * _AssetMixin        静态文件与媒体（Range）
      * _ChunkUploadMixin  分片上传兜底通道
    所有 mixin 共用的底层原语只有 _read_body / _json / _error / _send；
    媒体相关的 _serve_file / _send_range / _send_download 留在 _AssetMixin 里，
    因为只有它会碰 Range 和 Content-Disposition。
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
    def log_message(self, fmt, *args):
        """只把 5xx 打到 stderr（4xx 已经在 server.log 里，属于正常反馈）。

        http.server 的调用形式是 log_message('"%s" %s %s', 请求行, 状态码, 长度)，
        所以状态码在 args[1]，不是 args[0]（args[0] 永远是格式串）。
        """
        code = str(args[1]) if len(args) > 1 else ""
        if code.startswith("5"):
            _write_err("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))

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
        # 这里刻意不发 Access-Control-Allow-Origin：页面是同源打开的，本来不需要；
        # 发了 "*" 等于允许任意网站读本机数据（见 _guard）。只有 OPTIONS 会按需回显。
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # 页面在响应发完之前被关掉/刷新（WinError 10054 也走这里）。
            # 这不是服务端故障，不该在 crashreport 里记一笔。
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json_safe.dumps(obj, default=_json_default).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, msg: str, code: int = 400, **extra):
        self._json({"ok": False, "error": msg, **extra}, code)

    def _read_body(self, limit: int = MAX_BODY_BYTES) -> bytes:
        # 浏览器不会发非数字的 Content-Length，但代理、脚本、写到一半的连接会。
        # 直接 int() 会抛 ValueError，冒到兜底就变成"服务器内部错误 + 一份崩溃报告"。
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > limit:
            raise BodyTooLarge(
                "上传内容太大（%d 字节，上限 %d）——回放文件一般只有几百 KB，确认一下选的是 .osr"
                % (length, limit)
            )
        return self.rfile.read(length) if length > 0 else b""

    # ---- 路由 -----------------------------------------------------------
    def _guard(self) -> bool:
        """挡住"别的网站借用户浏览器读本机数据"。

        两条就够：Host 必须是回环地址（否则 DNS rebinding 能把任意域名解析到 127.0.0.1，
        连 CORS 都绕过去）；Origin 如果带了，主机部分也必须是回环。
        """
        if not _host_ok(self.headers.get("Host") or ""):
            self._error("只接受本机访问（Host 不是回环地址）", 403)
            return False
        origin = self.headers.get("Origin") or ""
        if not _origin_ok(origin):
            log("[guard] 拒绝跨站请求：Origin=%s" % origin[:80])
            self._error("拒绝跨站请求：这个页面不在本机", 403)
            return False
        return True

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if not self._guard():
            return
        try:
            return self._route_get(path, qs)
        except CLIENT_GONE as e:
            # 页面在响应发完前就断了（关标签页、刷新、音频拖到末尾）：
            # 这是正常操作，不是请求处理失败。
            log("[req] 页面提前断开（GET %s）：%s" % (path, type(e).__name__))
            return
        except Exception as e:  # noqa: BLE001
            report = _report_crash("请求处理失败（GET）", e, self._crash_context())
            return self._error(self._internal_error(e, report), 500)

    def _route_get(self, path: str, qs):
        """GET 路由：静态资源、页面、只读接口。

        分支顺序有意义：`/api/media/` 是前缀匹配，必须排在精确匹配之后。
        """
        if path == "/favicon.ico":
            # 浏览器每次开页面都会要它，没有就会在控制台刷一条 404
            return self._send(200, FAVICON, "image/svg+xml")
        if path in ("/", "/index.html"):
            return self._serve_file(os.path.join(WEB_DIR, "index.html"))
        if path.startswith("/static/"):
            return self._serve_file(os.path.join(WEB_DIR, path[len("/static/"):]))
        if path == "/api/beacon":
            log("[beacon] %s  %s" % ((qs.get("ev") or ["?"])[0][:40],
                                     (qs.get("data") or [""])[0][:600]))
            return self._no_content()
        if path == "/api/alive":
            WATCHDOG.beat((qs.get("c") or [""])[0][:64])
            return self._no_content()
        if path == "/api/bye":
            WATCHDOG.bye((qs.get("c") or [""])[0][:64])
            log("[watchdog] 页面关闭，%d 秒后若无新页面就退出" % WATCHDOG.BYE_GRACE)
            return self._no_content()
        if path == "/api/health":
            return self._json({"ok": True, "version": __version__, "time": time.time(),
                               "assetV": self._asset_version(),
                               # stale=进程比代码旧（改完 .py 没重启）：页面据此提示用户重启
                               "stale": stale_code(),
                               # 快捷导入的热键状态：没注册上时页面要能说清原因
                               "hotkey": hotkey.status()})
        if path == "/api/practice/audio":
            return self.practice_audio(qs)
        if path == "/api/map/download/status":
            return self.map_download_status(qs)
        if path == "/api/map/download/file":
            return self.map_download_file(qs)
        if path == "/api/beatmap-text":
            return self._beatmap_text(qs)
        if path == "/api/paths":
            return self._paths_info()
        if path == "/api/library":
            return self._library_info()
        if path == "/api/library/progress":
            # 只读快照，不触发构建：页面在建索引期间轮询它画进度条
            return self._json(progress_snapshot())
        if path == "/api/local-replays":
            return self._local_replays()
        if path == "/api/quick/pending":
            return self._quick_pending(qs)
        if path == "/api/hotkey":
            return self._hotkey_info()
        if path == "/api/random-charts":
            return self._random_charts(qs)
        if path == "/api/audio-fetch/status":
            return self._audio_fetch_status(qs)
        if path == "/api/progress":
            return self._progress_info()
        if path == "/api/changelog":
            return self._changelog()
        if path == "/api/cache/stats":
            return self._json(cache_admin.stats(CACHE_DIR))
        if path == "/api/check-update":
            return self._check_update()
        if path == "/api/open-replays":
            return self._open_replays_dir()
        if path.startswith("/api/media/"):
            return self._serve_media(path[len("/api/media/"):])
        return self._error("未知路径: " + path, 404)

    def _no_content(self):
        """回 204 空响应：心跳 / bye / beacon 只表示"收到了"，没有正文可给。"""
        self.close_connection = True
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def _open_replays_dir(self):
        """在资源管理器里打开 _replays 目录（页面上的「打开文件夹」按钮）。"""
        try:
            os.makedirs(LOCAL_REPLAY_DIR, exist_ok=True)
            os.startfile(LOCAL_REPLAY_DIR)  # noqa: S606 - 本地工具，打开自己的目录
        except Exception as e:  # noqa: BLE001
            return self._error("打不开文件夹：%r" % (e,))
        return self._json({"ok": True, "dir": LOCAL_REPLAY_DIR})

    def _crash_context(self) -> dict:
        """报告里"这次请求长什么样"，够定位就行，不记 body 内容（可能有回放数据）。"""
        ctx = {"请求": "%s %s" % (self.command or "?", (self.path or "?").split("?")[0])}
        try:
            ctx["请求体"] = "%s 字节" % (self.headers.get("Content-Length") or 0)
        except Exception:  # noqa: BLE001
            pass
        return ctx

    def _internal_error(self, exc: BaseException, report: str | None) -> str:
        tail = "（详情见 crashreport\\%s）" % os.path.basename(report) if report else ""
        return "服务器内部错误：%s%s" % (crashreport.summary(exc), tail)

    def do_OPTIONS(self):
        origin = self.headers.get("Origin") or ""
        # 只有本机来源才回显 CORS 头；别的网站连预检都过不去
        if not _host_ok(self.headers.get("Host") or "") or not _origin_ok(origin):
            return self._error("拒绝跨站请求", 403)
        self.close_connection = True
        self.send_response(204)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if not self._guard():
            return
        # 一到达就记一行：这样"请求有没有跑到后端"一眼能看出来
        declared = self.headers.get("Content-Length")
        log("[req] POST %s  Content-Length=%s" % (path, declared))
        try:
            if path == "/api/bye":
                # 页面关闭时用 sendBeacon 告别，而 sendBeacon 固定走 POST。
                # 只处理 GET 的话这里会 404，进程只能等 75 秒心跳过期才退出。
                WATCHDOG.bye((qs.get("c") or [""])[0][:64])
                return self._json({"ok": True})
            if path == "/api/analyze":
                return self._analyze()
            if path == "/api/upload-chunk":
                return self._upload_chunk()
            if path == "/api/analyze-chunked":
                return self._analyze_chunked()
            if path == "/api/beatmap-search":
                return self._beatmap_search()
            if path == "/api/paths":
                return self._paths_save(_parse_json(self._read_body()) or {})
            if path == "/api/history/dan":
                return self._history_dan(_parse_json(self._read_body()) or {})
            if path == "/api/history/replay":
                return self._history_replay(_parse_json(self._read_body()) or {})
            if path == "/api/beatmap-meta":
                return self._beatmap_meta()
            if path == "/api/report/advice":
                return self.report_advice()
            if path == "/api/card/save":
                return self.card_save(qs)
            if path == "/api/cache/clean":
                return self._cache_clean()
            if path == "/api/practice/cut":
                return self.practice_cut()
            if path == "/api/practice/pack":
                return self.practice_pack(qs)
            if path == "/api/map/download":
                return self.map_download_start()
            if path == "/api/chart":
                return self._chart_only()
            if path == "/api/recommend":
                return self._recommend()
            if path == "/api/analyze-local":
                return self._analyze_local()
            if path == "/api/quick-import":
                return self._quick_import()
            if path == "/api/hotkey":
                return self._hotkey_save()
            if path == "/api/profile":
                return self._profile_save()
            if path == "/api/audio-fetch":
                return self._audio_fetch()
            if path == "/api/audio-pref":
                return self._audio_pref()
            if path == "/api/mirror-test":
                return self._mirror_test()
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
        except CLIENT_GONE as e:
            log("[req] 页面提前断开（POST %s）：%s" % (path, type(e).__name__))
            return
        except Exception as e:  # noqa: BLE001
            report = _report_crash("请求处理失败（POST）", e, self._crash_context())
            return self._error(self._internal_error(e, report), 500)


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


# --------------------------------------------------------------------- 快捷导入
#
# 交互是「先在结算屏按 F2 导出，再按我们的组合键」。多按一下换来的是：
# 只需要看 Replays / exports 里最新的那份文件，不必去猜 Data\r 里哪个加密文件是刚打完的，
# 也不会把"只想导出"的人误判成"想做分析"。

def _profile_from_config() -> dict:
    """读用户选的当前段位与本人名字。"""
    raw = config_store.read(CACHE_DIR).get("danProfile") or {}
    if not isinstance(raw, dict):
        raw = {}
    # 旧版本存的是旧刻度（Alpha 记作 10.5）：读到就换算一次并存回去，
    # 否则一个 Alpha 玩家会被当成 10.5 段，和迁移后的谱面段位对不上。
    if raw and raw.get("scale") != progress.DAN_SCALE:
        try:
            raw = dict(raw)
            raw["dan"] = progress.rescale_dan(raw.get("dan") or 0)
            if raw.get("type") == "reform" and raw.get("value") is not None:
                raw["value"] = progress.rescale_dan(raw.get("value"))
            raw["scale"] = progress.DAN_SCALE
            config_store.merge({"danProfile": raw}, cache_dir=CACHE_DIR)
        except (TypeError, ValueError):
            pass
    try:
        dan_value = float(raw.get("dan"))
    except (TypeError, ValueError):
        dan_value = 0.0
    player = history.configured_player(CACHE_DIR)
    if not dan_value and not player:
        return {}
    return {
        "type": raw.get("type") or "reform",
        "value": raw.get("value"),
        "dan": dan_value or None,
        "label": raw.get("label") or (progress.dan_label(dan_value) if dan_value else ""),
        "player": player,
        "malodyLabel": raw.get("malodyLabel") or "",
        "measured": bool(raw.get("measured")),
    }


def _is_fallback_dan(session: dict, value) -> bool:
    """这条记录是不是还停在入库时那个 MSD 标定值上。

    页面算完 Roxy 会回填真正的估算段位；要是那次回填没成功（分析完直接关页面、
    回填请求失败），库里就只剩 MSD 值。重导入时拿它当「导入前」的基线，
    会凭空算出一段掉段 —— 那种卡片比不显示更误导。
    """
    try:
        msd_value = float(session.get("msd") or 0)
        current = float(value)
    except (TypeError, ValueError):
        return False
    if not msd_value or current <= 0:
        return False
    try:
        fallback = float((dan.estimate(msd_value) or {}).get("dan"))
    except (TypeError, ValueError):
        return False
    return abs(current - fallback) < 0.005


def _dan_change(key: str, old_value, fresh: bool = False) -> dict | None:
    """这一局让成长页的综合段位动了多少；没动就返回 None。

    折合在服务端做（页面只显示结果），所以这里拿两份存档各算一次：
    当前库是「导入之后」，另一份是「导入之前」。

    新建的那一局要按"库里根本没有这一局"还原，不能拿它的旧 dan_value：
    入库时写的是 MSD 兜底值，页面回填的是 Sunny → Azusa → Roxy 的结果，
    两者能差一两段；拿兜底值当基线，等于把这次成绩自己的估算差额算进了段位变动，
    新成绩很容易一律显示成掉段。只有重新导入老回放时，旧值才是真的"上一版"。
    两边都得有足够样本才给结论 —— 样本不够时成长页本来就不显示段位。
    """
    profile = _profile_from_config()
    sessions = history.all_sessions()
    after = progress.evaluate(sessions, profile).get("overall") or {}
    if not after.get("visible"):
        return None
    patched = []
    for s in sessions:
        if s.get("key") == key:
            s = dict(s)
            # None = 这一局之前不存在，等于不计入。两种情况下按"不存在"处理：
            # 本次刚入库（fresh），以及库里那条只剩 MSD 兜底值（从没回填过）。
            drop = fresh or _is_fallback_dan(s, old_value)
            s["dan_value"] = None if drop else old_value
        patched.append(s)
    before = progress.evaluate(patched, profile).get("overall") or {}
    if not before.get("visible"):
        return None
    try:
        to_value = float(after.get("dan"))
        from_value = float(before.get("dan"))
    except (TypeError, ValueError):
        return None
    # 显示用两位小数，差不到 0.01 就当没动：每次都弹一张 +0.00 的卡只是噪音
    delta = round(to_value - from_value, 2)
    if not delta:
        return None
    return {
        "from": round(from_value, 2),
        "to": round(to_value, 2),
        "delta": delta,
        "fromLabel": progress.dan_label(from_value),
        "toLabel": progress.dan_label(to_value),
        "count": after.get("count", 0),
    }


def _refresh_library() -> None:
    """分析/搜索前看一眼曲库有没有变化（新下载的谱面）。

    索引默认有 1 小时 TTL，用户"下载推荐图 → 打完回放拖回来"通常落在这个窗口内，
    不刷新就会一直提示"曲库里没有这张谱面"。这里只做一次目录指纹比对，
    没变化时几乎不花时间；真有新图才重扫（本机约 2~4 秒，一次就够）。

    之所以放在 _analyze_and_store 里：上传、分片、本地目录、快捷导入四条入口
    最后都汇到它，挨个加一遍迟早漏一条（快捷导入就漏过）。
    """
    try:
        roots = default_songs_roots()
        lib = get_library(roots[0] if roots else None)
        if lib.ensure_fresh():
            log("[library] 检测到曲库有变化，已重新扫描（%d 个谱面）" % len(lib.entries))
    except Exception:  # noqa: BLE001 - 刷新失败不该挡住分析
        log("[library] 刷新失败：\n" + traceback.format_exc())


def _analyze_and_store(replay_path: str, beatmap_path: str | None,
                       songs_root: str | None, store: bool = True) -> dict:
    """跑完分析、建会话、记历史，返回完整结果。HTTP 入口与热键共用这一份。

    store=False 用于"从成长页点回来重看"：回放要重新分析，但库里已经有这一局了，
    再把摘要写一遍只会把 recorded_at 和 dan_value 刷成新值，属于不该有的副作用。

    判定模型由回放里的客户端版本决定（stable 逐帧状态机 / lazer 事件式），
    没有手动开关：两套规则混用只会算错，选项留着也只会让人选错。
    """
    _refresh_library()
    before = last_build_at()
    try:
        payload = analyze(replay_path, beatmap_path=beatmap_path, songs_root=songs_root)
    finally:
        # 重扫恰好发生在"找不到这张图"这条路上，而那条路是会抛异常的，
        # 所以日志必须写在 finally 里，否则最需要它的场景反而没有记录。
        if last_build_at() > before:
            lib = get_library(songs_root)
            log("[library] 找不到谱面后重扫了曲库：%d 张，用时 %.0fms"
                % (len(lib.entries), lib.build_ms))
    audio, background, note_skin = _resolve_media(payload, beatmap_path, songs_root)

    # 谱面路径要在这里定下来：上传回放时 beatmap_path 是空的，真正的路径是
    # analyze 内部按 MD5 从曲库解析出来的。不把它解析出来存进会话，
    # 后面「在线补音频」就拿不到 .osu，也就读不出图集 id。
    resolved_bm = beatmap_path or _resolve_bm(payload, songs_root)
    sid = SESSIONS.create(payload, audio, background, note_skin,
                          beatmap_path=resolved_bm or "",
                          beatmap_md5=(payload.get("replay") or {}).get("beatmapMd5") or "")
    payload["session"] = sid
    # 回放本体也留一份（`_cache/replays/<回放MD5>.osr`）：成长页的 B50 点开时要靠它重放。
    # .osr 只有几十 KB，写失败也不影响分析，所以不看返回值。
    replay_info = payload.get("replay") or {}
    replay_store.save(str(replay_info.get("replayMd5") or replay_info.get("fileHash") or ""),
                      replay_path)
    # 每局摘要落一份本地存档（`_cache/history.db`）。这一层只存不分析，
    # 写失败也不影响本次分析，所以走 record_safely。
    # 返回值要带给页面：段位变动要拿"库里有没有这一局"当基线，
    # 新建那行的 dan_value 只是 MSD 兜底，不是上一版估算值。
    #
    # 配错图（MD5 对不上）的不记账：那种结果通常是一整排漏接，进了历史会污染
    # 成长页和「本图走势」，而且下次正确导入同一份回放还会撞上这条脏记录。
    mismatch = bool(payload.get("md5Mismatch"))
    if store and not mismatch:
        payload["historyNew"] = bool(history.record_safely(payload))
    else:
        payload["historyNew"] = False
    payload["historySkipped"] = bool(mismatch and store)
    cols = note_skin.get("columns") or []
    payload["media"] = {
        "audio": bool(audio),
        "background": bool(background),
        # 每列的 note 素材 URL（没找到的列为 null，前端回退到色块）
        "noteImages": [("/api/media/%s/note%d" % (sid, i)) if p else None for i, p in enumerate(cols)],
        "lnBody": ("/api/media/%s/lnbody" % sid) if note_skin.get("lnBody") else None,
        "lnTail": ("/api/media/%s/lntail" % sid) if note_skin.get("lnTail") else None,
    }
    return payload


def _quick_payload(force: bool = False) -> tuple[dict | None, str]:
    """分析最近一次导出的回放。返回（结果, 错误说明），出错时结果为 None。

    同一份文件连按两次不会重复分析：路径与修改时间都没变时直接给上一次的结果，
    页面那边 token 不变，也就不会重新渲染一遍。
    """
    item = quick_import.latest()
    if not item:
        combo = hotkey.status().get("combo") or "Shift+F2"
        return None, ("没找到刚导出的回放。先在结算屏按 F2 导出，再按 %s；"
                      "也可以直接把 .osr 拖进页面。" % combo)

    with QUICK_BUSY:
        with QUICK_COND:
            same = (QUICK["session"] and QUICK["path"] == item["path"]
                    and abs(QUICK["mtime"] - item["mtime"]) < 1e-6)
            cached = QUICK["payload"] if (same and not force) else None
            if cached is not None:
                # 同一份回放再按一次：不重新分析，但**要让页面有反应**。
                # 只返回旧 token 的话，用户看到的就是"按了没动静"——
                # 尤其是刷新页面后第一次按，最容易以为是坏的。
                QUICK.update(token=QUICK["token"] + 1, error="", busy=False)
                QUICK_COND.notify_all()
                return cached, ""
            # 先把"开始干活了"喊出去：页面在等这个信号，用来立刻显示加载提示。
            QUICK.update(busy=True, error="")
            QUICK_COND.notify_all()
        try:
            payload = _analyze_and_store(item["path"], None, None)
        except Exception:
            with QUICK_COND:
                QUICK["busy"] = False
                QUICK_COND.notify_all()
            raise
        payload["quick"] = {
            "name": item["name"], "dir": item["dir"],
            "source": item["source"], "mtime": item["mtime"],
        }
        with QUICK_COND:
            QUICK.update(token=QUICK["token"] + 1, session=payload.get("session", ""),
                         payload=payload, path=item["path"], mtime=item["mtime"],
                         error="", busy=False)
            QUICK_COND.notify_all()
        log("[quick] %s -> %s" % (item["name"], payload.get("session", "")))
        return payload, ""


def _on_quick_hotkey() -> None:
    """热键按下：分析最近一次导出。结果（或失败原因）留给页面轮询取走。"""
    try:
        _payload, problem = _quick_payload()
    except Exception as e:  # noqa: BLE001 - 热键是后台线程，任何异常都不能把服务带崩
        problem = "这一份分析失败：%s" % e
        log("[quick] 热键处理失败：\n" + traceback.format_exc())
        _report_crash("快捷导入失败", e, {})
    if problem:
        log("[quick] " + problem)
        with QUICK_COND:
            QUICK.update(token=QUICK["token"] + 1, payload=None, error=problem, busy=False)
            QUICK_COND.notify_all()


def _save_hotkey(combo: str) -> None:
    """把快捷键写进 config.json。写失败只记日志：这一次已经生效了，不该因此报错。"""
    try:
        config_store.merge({"quickHotkey": combo}, cache_dir=CACHE_DIR)
    except OSError as e:
        log("[hotkey] 保存失败：%r" % (e,))


def _apply_hotkey(combo: str) -> tuple[bool, str]:
    """换快捷键：先校验 → 注销旧的 → 注册新的；新键注册失败就把旧的装回去。

    combo 为空表示关闭快捷导入。
    """
    combo = (combo or "").strip()
    if combo:
        # 先校验再动手：注册失败才发现输入有错的话，用户旧的那个键已经被注销了
        try:
            hotkey.parse_combo(combo)
        except ValueError as e:
            return False, str(e)
    previous = hotkey.status().get("combo") or ""
    was_active = bool(hotkey.status().get("ok"))
    if not combo:
        hotkey.stop(clear_combo=True)
        _save_hotkey("")
        log("[hotkey] 已关闭快捷导入")
        return True, ""
    hotkey.stop()
    ok, why = hotkey.start(_on_quick_hotkey, combo)
    if not ok:
        if was_active and previous:
            hotkey.start(_on_quick_hotkey, previous)
        return False, why
    _save_hotkey(hotkey.status().get("combo") or combo)
    log("[hotkey] 快捷键改为 %s" % (hotkey.status().get("combo") or combo))
    return True, ""


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

    # 谱面包下载的临时产物：留一小时，够用户点一次存盘
    try:
        map_download.cleanup(CACHE_DIR)
    except OSError:
        pass

    # 音频目录里的下载碎片（.part / .pN）：只有进程被硬关掉才会留下，正常流程自己会删
    try:
        dropped = cache_admin.sweep_parts(CACHE_DIR)
        if dropped:
            log("[startup] 清掉 %d 个下载碎片" % dropped)
    except OSError:
        pass


def _probe_port(port: int) -> bool:
    """这个端口上是不是已经跑着本程序（而不是别的什么服务）。"""
    if port <= 0:
        return False
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=0.5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - 连不上、超时、返回不是 JSON，都当"不是本程序"
        return False
    # 只认自家 /api/health 的形状：有 ok 且有 assetV（别家服务恰好有 /api/health 也不会误判）
    return bool(data.get("ok")) and "assetV" in data


def _running_instance(port: int) -> int | None:
    """已有一个实例在跑就返回它的端口。

    同时看两处：这次要用的端口，以及 port.txt 里记的那个（端口被占时程序会往后挪，
    所以"上次在 8807"和"这次想用 8806"完全可以同时成立）。
    """
    try:
        with open(PORT_FILE, encoding="ascii") as f:
            last = int(f.read().strip())
    except (OSError, ValueError):
        last = 0
    for candidate in (last, port):
        if _probe_port(candidate):
            return candidate
    return None


def _parse_args():
    ap = argparse.ArgumentParser(description="mania 回放分析器本地服务")
    ap.add_argument("--port", type=int, default=8806)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--songs", default="", help="手动指定 osu! Songs 目录")
    ap.add_argument("--keep-alive", action="store_true",
                    help="页面关掉后也不退出服务（调试用）")
    ap.add_argument("--allow-multi", action="store_true",
                    help="即使已经有实例在跑也照常启动（调试用，正常使用不需要）")
    ap.add_argument("--no-hotkey", action="store_true",
                    help="不注册快捷导入的全局热键（调试用）")
    ap.add_argument("--hotkey", default="",
                    help="快捷导入的组合键，覆盖设置里的值（至少带一个修饰键）")
    return ap.parse_args()


def _pick_port(start: int) -> int:
    """从 start 往后找第一个空端口（最多试 20 个）。"""
    port = start
    for _ in range(20):
        if _port_free(port):
            break
        port += 1
    return port


def _bind_server(port: int):
    """绑定端口并返回 server；绑不上时写崩溃报告、返回 None。"""
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        report = crashreport.write("启动失败：端口绑定不上", e, context={"端口": port})
        print("[mania-replay-lab] 启动失败：%r%s" % (
            e, "\n报告：%s" % report if report else ""), flush=True)
        log("[startup] 端口 %d 绑定失败：%r" % (port, e))
        return None
    httpd.daemon_threads = True
    return httpd


def _write_runtime_files(port: int) -> None:
    """写出真实端口与进程号。

    端口被占时会往后挪，写出去的是这次真正用的那个，免得打开的是上一次的旧服务；
    进程号给 exe 版实现"再运行一次 → 停止服务"。
    """
    try:
        with open(PORT_FILE, "w", encoding="ascii") as f:
            f.write(str(port))
        with open(PID_FILE, "w", encoding="ascii") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def _warm_library() -> None:
    """后台预热曲库索引（不阻塞启动）。"""
    roots = default_songs_roots()
    if not roots:
        return
    try:
        lib = get_library(roots[0])
        st = lib.stats()
        print("[mania-replay-lab] 曲库索引完成：%d 张谱面（新算 %d / 复用 %d，%.0f ms）"
              % (len(lib.entries), st["hashed"], st["reused"], st["buildMs"]), flush=True)
        log("[startup] 曲库索引 %d 张（新算 %d 复用 %d，%.0f ms）"
            % (len(lib.entries), st["hashed"], st["reused"], st["buildMs"]))
    except Exception as e:  # noqa: BLE001
        # 预热失败不影响主流程（第一次搜索会重扫），但不能一点痕迹都不留
        log("[startup] 曲库预热失败：%r" % (e,))


def _warm_history() -> None:
    """后台打开历史存档：建表、补字段、迁移老库。

    以前这件事排在绑定端口之前，而升级老库要重建曲库索引、逐行解析谱面，
    几十秒都正常；启动器只等 20 秒端口，等不到就弹「服务没能启动」并退出进程，
    迁移事务跟着回滚，下次启动又从头来一遍。现在先让端口通，再在后台慢慢迁。
    """
    try:
        warm = history.warmup()
        if warm.get("ok"):
            log("[startup] 历史存档就绪：%d 局，%d 个字段" % (warm["sessions"], warm["columns"]))
        else:
            log("[startup] 历史存档打开失败：%s" % warm.get("error"))
    except Exception:  # noqa: BLE001
        log("[startup] 历史存档初始化异常：\n" + traceback.format_exc())


def _watch_pages(httpd) -> None:
    """页面关掉就退出：每 5 秒看一次有没有活着的页面。"""
    while True:
        time.sleep(5.0)
        if WATCHDOG.armed and not WATCHDOG.any_alive():
            log("[watchdog] 没有任何页面在线，服务退出")
            print("[mania-replay-lab] 页面已关闭，服务退出。", flush=True)
            httpd.shutdown()
            return


def _reuse_running_instance(args) -> bool:
    """已经有一个实例在跑就打开它并返回 True。

    多开检测的意义：两个实例会各占一份内存、各扫一遍曲库，port.txt 还会被后起的覆盖，
    结果是"先起的那个对启动器隐身"——用户点停止也停不掉它。
    """
    running = None if args.allow_multi else _running_instance(args.port)
    if not running:
        return False
    url = "http://127.0.0.1:%d/" % running
    print("[mania-replay-lab] 已经有实例在 %d 端口运行，这次不再启动：%s" % (running, url), flush=True)
    log("[startup] 检测到已有实例（端口 %d），本次启动跳过" % running)
    if not args.no_open:
        webbrowser.open(url)
    return True


def main() -> int:
    args = _parse_args()

    if args.songs:
        os.environ["MANIA_SONGS_ROOT"] = args.songs

    if paths.CACHE_FALLBACK:
        # 只读安装位置（例如被放进 Program Files、或只读共享盘）：
        # 缓存退到 %TEMP%，否则 import 阶段就会 PermissionError 起不来。
        print("[mania-replay-lab] 程序目录不可写，缓存改到：%s" % paths.CACHE_DIR, flush=True)
        log("[startup] 程序目录不可写，缓存目录改为 %s" % paths.CACHE_DIR)

    _clean_chunks()

    if _reuse_running_instance(args):
        return 0

    port = _pick_port(args.port)
    httpd = _bind_server(port)
    if httpd is None:
        return 1
    url = "http://127.0.0.1:%d/" % port
    _write_runtime_files(port)
    print("[mania-replay-lab] 服务已启动：%s" % url, flush=True)
    if port != args.port:
        print("[mania-replay-lab] 提示：%d 端口被占用，改用了 %d（旧实例可以先关掉）" % (args.port, port), flush=True)
    print("[mania-replay-lab] 曲库：%s" % (", ".join(default_songs_roots()) or "未找到"), flush=True)

    threading.Thread(target=_warm_library, daemon=True).start()
    threading.Thread(target=_warm_history, daemon=True).start()

    if not args.no_hotkey:
        # 设置里存过就以设置为准（存空串表示用户关掉了），没存过才用默认值；
        # 命令行 --hotkey 优先级最高，方便临时试另一个键。
        saved = config_store.read(CACHE_DIR)
        combo = args.hotkey or (str(saved.get("quickHotkey") or "")
                                if "quickHotkey" in saved else "Shift+F2")
        if not combo:
            print("[mania-replay-lab] 快捷导入已关闭（设置页填一个快捷键可重新开启）", flush=True)
            log("[startup] 快捷导入已关闭")
        else:
            started, why = hotkey.start(_on_quick_hotkey, combo)
            if started:
                print("[mania-replay-lab] 快捷导入已就绪：先在结算屏按 F2，再按 %s"
                      % hotkey.status().get("combo", combo), flush=True)
                log("[startup] 快捷导入热键 %s 已注册" % hotkey.status().get("combo", combo))
            else:
                print("[mania-replay-lab] 快捷导入的热键没注册上：%s" % why, flush=True)
                log("[startup] 快捷导入热键注册失败：%s" % why)

    if not args.keep_alive:
        threading.Thread(target=_watch_pages, args=(httpd,), daemon=True).start()

    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hotkey.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
