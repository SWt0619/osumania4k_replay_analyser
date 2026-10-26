"""谱面包下载：服务端分段并发取回，再交给浏览器存盘。

为什么不让浏览器自己分段取
--------------------------
随机找图 / 相似谱面的封面原本是把 dl.sayobot.cn 的直链丢给浏览器，走浏览器自己的
单连接。实测单连接只有 0.35~0.45 MB/s（20MB 的包要 45 秒以上），而镜像支持 Range。
按说让页面发 8 个分段请求就行，实测却不成立：浏览器到镜像之间如果挂着会走 h2 的
本地代理，8 个请求会被复用成一条连接，吞吐反而更差——同一个 4.3MB 的包，h2 单隧道
9.8 秒，强制 http/1.1 之后 2.9 秒，而 Python 侧（每个请求各自一条连接）4.6 秒。

所以把分段放在 Python 这一侧做，页面只负责三件事：发起、看进度、收文件。
代价是包会先在 `_cache/downloads/` 落一份临时文件，一小时没人收就清掉。
"""

from __future__ import annotations

import os
import re
import threading
import time
import urllib.parse
import uuid

from . import audio_fetch

MAX_BYTES = 300 * 1024 * 1024
MAX_JOBS = 16                 # 同时保留的下载任务数
JOB_TTL = 3600                # 产物留这么久，够用户点一次存盘

# 只允许从镜像站取：这个接口是本地开的，不设白名单等于给任意网站一个
# 「让本机替我下载任意地址」的入口。
ALLOWED_HOSTS = {
    "dl.sayobot.cn", "osu.sayobot.cn", "tc1.sayobot.cn", "tc2.sayobot.cn",
    "api.nerinyan.moe", "api.nerinyan.me",
    "catboy.best", "api.catboy.best", "mino.sayobot.cn",
}
# 镜像自己的防盗链：不校验，但标对来源
HEADERS = {"dl.sayobot.cn": {"Referer": "https://osu.sayobot.cn/"}}

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
MAX_RUNNING = 2               # 同时最多几个包在下载：一次点击 8 个连接，两个就是 16 个
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _safe_name(text: str, limit: int = 80) -> str:
    s = _ILLEGAL.sub("_", str(text or "")).strip().strip(".")
    if not s:
        s = "beatmap"
    return s[:limit]


def _dir(cache_dir: str) -> str:
    path = os.path.join(cache_dir, "downloads")
    os.makedirs(path, exist_ok=True)
    return path


def validate(url: str) -> str:
    """检查下载地址。返回规范化后的 URL，不合法就抛 ValueError。"""
    url = str(url or "").strip()
    if not url:
        raise ValueError("缺少下载地址")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError("只接受 https 地址")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ValueError("不是允许的镜像站：%s" % (host or "?"))
    return url


def _prune(limit: int = MAX_JOBS) -> None:
    """任务太多时把最早的产物删掉，别让临时目录长起来。调用方持锁。"""
    if len(_JOBS) <= limit:
        return
    done = sorted((j for j in _JOBS.values() if j["state"] != "running"),
                  key=lambda j: j["started"])
    for job in done[:max(0, len(_JOBS) - limit)]:
        _JOBS.pop(job["id"], None)
        try:
            os.remove(job["path"])
        except OSError:
            pass


def start(url: str, name: str, cache_dir: str) -> dict:
    """起一个后台下载。返回 {id, name}，进度用 status() 查。"""
    url = validate(url)
    job_id = uuid.uuid4().hex[:12]
    path = os.path.join(_dir(cache_dir), job_id + ".osz")
    job = {
        "id": job_id,
        "url": url,
        "name": _safe_name(name or "beatmap") + ".osz",
        "path": path,
        "state": "running",
        "received": 0,
        "total": 0,
        "size": 0,
        "error": "",
        "started": time.time(),
    }
    with _LOCK:
        running = sum(1 for j in _JOBS.values() if j["state"] == "running")
        if running >= MAX_RUNNING:
            raise ValueError("已经有 %d 个谱面在下，等一个下完再点" % running)
        _prune()
        _JOBS[job_id] = job

    def run() -> None:
        headers = HEADERS.get(urllib.parse.urlsplit(url).hostname or "", {})

        def on_bytes(done: int, total: int) -> None:
            with _LOCK:
                job["received"] = int(done or 0)
                if total:
                    job["total"] = int(total)

        try:
            audio_fetch.download_file(url, path, on_bytes=on_bytes, extra_headers=headers)
            size = os.path.getsize(path)
            if size <= 0:
                raise ValueError("下到的文件是空的")
            if size > MAX_BYTES:
                raise ValueError("包体超过 %d MB" % (MAX_BYTES // 1024 // 1024))
            with _LOCK:
                job.update(state="done", size=size, total=size, received=size)
        except Exception as e:  # noqa: BLE001 - 失败要如实告诉页面，不能崩线程
            with _LOCK:
                job.update(state="error", error=str(e) or type(e).__name__)
            try:
                os.remove(path)
            except OSError:
                pass

    threading.Thread(target=run, name="map-download", daemon=True).start()
    return {"id": job_id, "name": job["name"], "url": url}


def status(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(str(job_id or ""))
        if not job:
            return None
        return {
            "state": job["state"],
            "name": job["name"],
            "received": job["received"],
            "total": job["total"],
            "size": job["size"],
            "error": job["error"],
        }


def path_of(job_id: str) -> tuple[str, str]:
    """取已下完的文件。返回 (路径, 显示名)，没下完或不存在就返回 ("", "")。"""
    with _LOCK:
        job = _JOBS.get(str(job_id or ""))
        if not job or job["state"] != "done":
            return "", ""
        path, name = job["path"], job["name"]
    return (path, name) if os.path.isfile(path) else ("", "")


def drop(job_id: str) -> None:
    """整包发完就清掉：用户已经拿到手，没必要在缓存里再占几十 MB。"""
    with _LOCK:
        job = _JOBS.pop(str(job_id or ""), None)
    if job:
        try:
            os.remove(job["path"])
        except OSError:
            pass


def cleanup(cache_dir: str, ttl: int = JOB_TTL) -> int:
    """清掉过期的临时包。启动时调一次，也顺手把上次没删干净的收拾掉。"""
    folder = os.path.join(cache_dir, "downloads")
    if not os.path.isdir(folder):
        return 0
    deadline = time.time() - ttl
    dropped = 0
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < deadline:
                os.remove(path)
                dropped += 1
        except OSError:
            pass
    with _LOCK:
        for job_id, job in list(_JOBS.items()):
            if job["state"] != "running" and job["started"] < deadline:
                _JOBS.pop(job_id, None)
    return dropped
