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
import time
import zipfile
from urllib.request import Request, urlopen

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
MAX_BYTES = 200 * 1024 * 1024      # 再大就不像谱面包了
MIN_INTERVAL = 2.0                 # 两次请求之间至少隔这么久
FAIL_TTL = 7 * 24 * 3600           # 失败条目多久后允许重试
# 取音频的逻辑一旦改动就 +1：旧的失败记录自动失效，
# 不然修好了 bug 用户还得等满 7 天（或者手动删索引）
FETCH_VERSION = 2

_last_request = 0.0
_lock_hint = None


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
def config_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, "config.json")


def load_config(cache_dir: str) -> dict:
    try:
        with open(config_path(cache_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_enabled(cache_dir: str) -> bool:
    """默认开启：这是「没声音」这个问题的正解，默认关掉等于没做。

    关掉的办法是配置里显式写 autoFetchAudio: false（页面上有开关）。
    """
    return bool(load_config(cache_dir).get("autoFetchAudio", True))


def set_enabled(cache_dir: str, flag: bool) -> None:
    data = load_config(cache_dir)
    data["autoFetchAudio"] = bool(flag)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = config_path(cache_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, config_path(cache_dir))
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
def _get(url: str, extra_headers: dict | None = None, sink: str = "") -> bytes:
    """GET 一个地址。sink 非空时直接流到文件（谱面包可能几十 MB）。"""
    _throttle()
    headers = {"User-Agent": _user_agent(), "Accept": "*/*"}
    if extra_headers:
        headers.update(extra_headers)
    req = Request(url, headers=headers)
    with urlopen(req, timeout=TIMEOUT) as resp:
        if not sink:
            return resp.read()
        written = 0
        with open(sink, "wb") as f:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_BYTES:
                    raise ValueError("包体超过 %d MB，放弃" % (MAX_BYTES // 1024 // 1024))
                f.write(chunk)
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
            tmp = out + ".part"
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


def _md5_of(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(256 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


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

    items = load_index(cache_dir)
    prev = items.get(key) or {}
    stale = prev.get("v") != FETCH_VERSION
    if not force and not stale and prev.get("failed") and (time.time() - float(prev.get("at") or 0)) < FAIL_TTL:
        return {"ok": False, "path": "", "source": "", "error": prev.get("error") or "上次没取到", "cached": False}

    audio_name = ""
    set_id = ""
    try:
        bm = parse_osu_file(beatmap_path)
        audio_name = bm.audio_filename
        set_id = str(bm.meta.get("BeatmapSetID") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    if set_id in ("0", "-1"):
        set_id = ""

    # 没有图集 id 就到此为止，不做「按 MD5 猜图集」。
    # 曾经试过 sayobot 的 ?K=<md5>，实测它根本不按 MD5 查：随便编一个 md5 都会返回
    # 一张不相干的图集（连 status 都是 0），拿它去下载等于白下几十 MB 还可能配错音频。
    # nerinyan /mino 的 md5 接口要么 530 要么 403，没有免鉴权可用的。
    if not set_id:
        err = "这张谱面没记录图集 id（自制图或还没上传到官网），无法在线补音频"
        items[key] = {"v": FETCH_VERSION, "failed": True, "at": time.time(), "error": err}
        save_index(cache_dir, items)
        return {"ok": False, "path": "", "source": "", "error": err, "cached": False}

    audio_dir = os.path.join(cache_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    tmp_osz = os.path.join(audio_dir, key + ".osz.part")
    last_err = ""
    started = time.time()
    for name, tpl, headers in MIRRORS:
        # 总预算：三个镜像全挂时别让用户对着"正在补音频"等一分半
        if time.time() - started > BUDGET:
            last_err = last_err or "镜像都没能在 %d 秒内给回应" % BUDGET
            break
        try:
            _get(tpl.format(sid=set_id), headers, sink=tmp_osz)
            # 低于这个尺寸的只可能是错误页或空响应；真正的有效性靠下面的 _verify_osz
            if not os.path.isfile(tmp_osz) or os.path.getsize(tmp_osz) < 128:
                last_err = "%s 返回的是空包" % name
                continue
            if not _verify_osz(tmp_osz, key):
                last_err = "%s 给的包里没有这张谱面" % name
                continue
            out = extract_audio(tmp_osz, audio_name, os.path.join(audio_dir, key))
            if not out:
                last_err = "%s 的包里没有音频" % name
                continue
            items[key] = {
                "v": FETCH_VERSION,
                "file": os.path.basename(out),
                "source": name,
                "at": time.time(),
                "setId": set_id,
                "size": os.path.getsize(out),
            }
            save_index(cache_dir, items)
            return {"ok": True, "path": out, "source": name, "error": "", "cached": False}
        except Exception as e:  # noqa: BLE001
            last_err = "%s：%s" % (name, e)
        finally:
            try:
                if os.path.isfile(tmp_osz):
                    os.remove(tmp_osz)
            except OSError:
                pass

    items[key] = {"v": FETCH_VERSION, "failed": True, "at": time.time(),
                  "error": last_err or "所有镜像都失败了"}
    save_index(cache_dir, items)
    return {"ok": False, "path": "", "source": "", "error": last_err or "所有镜像都失败了", "cached": False}


def stats(cache_dir: str) -> dict:
    items = load_index(cache_dir)
    files = [v for v in items.values() if v.get("file")]
    return {
        "count": len(files),
        "bytes": sum(int(v.get("size") or 0) for v in files),
        "failed": len([v for v in items.values() if v.get("failed")]),
        "enabled": is_enabled(cache_dir),
    }
