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
1. **串行 + 最小间隔**：镜像站是公共资源，并发和连发都很容易被限流甚至拉黑。
2. **失败也记账**：网络波动、镜像临时缺图都很常见，但如果不记，用户每打开一次
   回放就会重试一次。失败条目 7 天后才允许重试。
3. **只信任本地能验证的东西**：下下来的包必须真的含这张谱面 MD5 的 .osu 才接受，
   镜像站返回 200 但内容不对（换图、重传版本）时不写入缓存。
4. **不碰用户目录**：只往 `_cache\\audio\\` 写，不往曲库或游戏目录放任何文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import zipfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from . import config_store
from .beatmap import AUDIO_EXTS, parse_osu_file
from ..version import __version__

# 镜像按"国内可达性 + 稳定性"排序：sayobot 优先，其次两个国际站
MIRRORS = [
    ("sayobot", "https://dl.sayobot.cn/beatmaps/download/novideo/{sid}",
     {"Referer": "https://sayobot.cn/"}),
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
FETCH_VERSION = 2

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


def load_index(cache_dir: str) -> dict:
    try:
        with open(_index_path(cache_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items")
        return items if isinstance(items, dict) else {}
    except (OSError, ValueError):
        return {}


def save_index(cache_dir: str, items: dict) -> None:
    path = _index_path(cache_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "items": items}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def cached_audio(cache_dir: str, md5: str) -> str:
    """缓存里已有的音频（顺手校验文件还在，用户可能清过缓存目录）。"""
    key = (md5 or "").lower()
    if not key:
        return ""
    item = load_index(cache_dir).get(key) or {}
    name = item.get("file") or ""
    if not name:
        return ""
    path = os.path.join(cache_dir, "audio", name)
    return path if os.path.isfile(path) else ""


# --------------------------------------------------------------------- 网络
def _get(url: str, extra_headers: dict | None = None, sink: str = "",
         on_bytes=None) -> bytes:
    """GET 一个地址。sink 非空时直接流到文件（谱面包可能几十 MB）。

    on_bytes(已下载, 总大小) 每读一块回调一次，给界面报进度；总大小拿不到时是 0。
    """
    _throttle()
    headers = {"User-Agent": _user_agent(), "Accept": "*/*"}
    if extra_headers:
        headers.update(extra_headers)
    req = Request(url, headers=headers)
    with urlopen(req, timeout=TIMEOUT) as resp:
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


def _verify_osz(osz_path: str, md5: str) -> bool:
    """包里的 .osu 里必须有一份能和这张回放的 MD5 对上，否则说明镜像给错了图。

    osu! 的谱面 MD5 有两种口径（原样 / 换行归一），这里都算一遍再比。
    """
    want = (md5 or "").lower()
    if not want:
        return True
    try:
        with zipfile.ZipFile(osz_path) as z:
            for name in z.namelist():
                if not name.lower().endswith(".osu"):
                    continue
                data = z.read(name)
                if hashlib.md5(data).hexdigest() == want:
                    return True
                lf = data.replace(b"\r\n", b"\n")
                if hashlib.md5(lf).hexdigest() == want:
                    return True
                if hashlib.md5(lf.replace(b"\n", b"\r\n")).hexdigest() == want:
                    return True
    except (OSError, zipfile.BadZipFile, KeyError):
        return False
    return False


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


def _download_from_mirrors(key: str, set_id: str, audio_name: str,
                           audio_dir: str) -> tuple[str, str, str]:
    """依次问每个镜像要一次包，成功返回（音频路径, 镜像名, ""），全失败返回（"", "", 错误）。

    每个镜像都走同一条验收：体积像样 → 包里真有这张谱面 → 真能抽出音频。
    校验顺序不能反：先抽音频再校验的话，会给错图写进一份"看起来正常"的缓存。
    """
    # 临时文件名带 pid 与线程号：多开一个程序实例时也不会写到同一个文件里
    tag = "%d.%d" % (os.getpid(), threading.get_ident())
    tmp_osz = os.path.join(audio_dir, key + "." + tag + ".osz.part")
    last_err = ""
    started = time.time()
    for attempt, (name, tpl, headers) in enumerate(MIRRORS, 1):
        # 总预算：三个镜像全挂时别让用户对着"正在补音频"等一分半
        if time.time() - started > BUDGET:
            return "", "", last_err or "镜像都没能在 %d 秒内给回应" % BUDGET
        # 先把"正在连 sayobot（第 1 个镜像）"写出去：DNS + TLS 握手 + 等响应
        # 可能有一两秒完全没有字节，那一段最容易让人以为卡死。
        _progress_mark(key, stage="正在从 %s 下载" % name, mirror=name,
                       attempt=attempt, done=0, total=0)

        def _on_bytes(written, total, _name=name):
            _progress_mark(key, done=written, total=total, stage="正在从 %s 下载" % _name)

        try:
            _get(tpl.format(sid=set_id), headers, sink=tmp_osz, on_bytes=_on_bytes)
            _progress_mark(key, stage="正在校验 %s 给的包" % name)
            # 低于这个尺寸的只可能是错误页或空响应；真正的有效性靠下面的 _verify_osz
            if not os.path.isfile(tmp_osz) or os.path.getsize(tmp_osz) < 128:
                last_err = "%s 返回的是空包" % name
                continue
            if not _verify_osz(tmp_osz, key):
                last_err = "%s 给的包里没有这张谱面" % name
                continue
            _progress_mark(key, stage="正在抽取音频")
            out = extract_audio(tmp_osz, audio_name, os.path.join(audio_dir, key))
            if not out:
                last_err = "%s 的包里没有音频" % name
                continue
            return out, name, ""
        except Exception as e:  # noqa: BLE001
            last_err = "%s：%s" % (name, e)
        finally:
            try:
                if os.path.isfile(tmp_osz):
                    os.remove(tmp_osz)
            except OSError:
                pass
    return "", "", last_err or "所有镜像都失败了"


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

    audio_name, set_id = _target_of(beatmap_path)

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
    out, source, last_err = _download_from_mirrors(key, set_id, audio_name, audio_dir)
    if out:
        items[key] = {
            "v": FETCH_VERSION,
            "file": os.path.basename(out),
            "source": source,
            "at": time.time(),
            "setId": set_id,
            "size": os.path.getsize(out),
        }
        save_index(cache_dir, items)
        _progress_mark(key, stage="完成", finished=True, ok=True)
        return {"ok": True, "path": out, "source": source, "error": "", "cached": False}

    items[key] = {"v": FETCH_VERSION, "failed": True, "at": time.time(), "error": last_err}
    save_index(cache_dir, items)
    _progress_mark(key, stage="没补到", finished=True, ok=False, error=last_err)
    return {"ok": False, "path": "", "source": "", "error": last_err, "cached": False}


def test_mirrors() -> list[dict]:
    """量一次本机到各镜像站的响应时间（毫秒），给设置页的「测试连接」用。

    对同一个下载地址发 HEAD：要的是"连得上、回话多快"，不是把整包拉下来。
    服务器回 4xx / 5xx 也算连通 —— DNS、TLS、HTTP 这一路是通的，
    真正能不能下到包由补音频自己的校验决定。
    三个站并行问，总耗时约等于最慢的那个。
    """
    results: list[dict] = []
    lock = threading.Lock()
    started_all = time.perf_counter()

    def probe(name: str, tpl: str, extra: dict) -> None:
        started = time.perf_counter()
        ok, error, status = False, "", 0
        headers = {"User-Agent": _user_agent(), "Accept": "*/*"}
        if extra:
            headers.update(extra)
        try:
            req = Request(tpl.format(sid="1"), headers=headers, method="HEAD")
            with urlopen(req, timeout=LATENCY_TIMEOUT) as resp:
                status = int(getattr(resp, "status", 0) or 200)
            ok = True
        except HTTPError as e:
            status = int(e.code or 0)
            ok = True
        except Exception as e:  # noqa: BLE001 - 测速失败只是结果之一，不该抛给调用方
            error = str(e) or type(e).__name__
        ms = int(round((time.perf_counter() - started) * 1000))
        with lock:
            results.append({"name": name, "ms": ms, "ok": ok, "status": status, "error": error})

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
                    "name": name, "ms": waited, "ok": False, "status": 0,
                    "error": "等待回应超时（DNS 解析或握手没能完成）",
                })
    # 按 MIRRORS 的顺序返回：谁先回来不该影响结果表的顺序
    order = {name: i for i, (name, _, _) in enumerate(MIRRORS)}
    results.sort(key=lambda item: order.get(item["name"], 99))
    return results


def stats(cache_dir: str) -> dict:
    items = load_index(cache_dir)
    files = [v for v in items.values() if v.get("file")]
    return {
        "count": len(files),
        "bytes": sum(int(v.get("size") or 0) for v in files),
        "failed": len([v for v in items.values() if v.get("failed")]),
        "enabled": is_enabled(cache_dir),
    }
