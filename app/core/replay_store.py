"""回放存档：成长页的 B50 每一格都要能点回当时的分析。

历史库里只存摘要（一局的判定数、段位、失误构成），没有回放文件本身。
所以导入时顺手把 .osr 复制一份到 `_cache/replays/`（几十 KB 一份，比摘要大不了多少），
点开时先看存档；存档没有（老记录、清过缓存）就按回放 MD5 去常见目录里找：

    _replays/            手动放进来的
    <稳定版>/Replays/     游戏自己导出的
    <lazer>/exports/     lazer 导出的

都找不到，就说明这份回放确实不在了 —— 调用方据此提示"回放不存在"。
按内容（回放 MD5）认文件而不是按文件名，所以改过名的回放照样找得到。
"""

from __future__ import annotations

import os
import re
import shutil

from .. import paths
from . import osr, osu_dirs
from .beatmap import long_path

# 回放 MD5 是 32 位；少数老回放的这个字段是空的，历史库会退回文件 sha1。
# sha1 只取前 16 位（见 analyze._file_hash），所以这里下限放到 16：
# 早先按 32 位校验，那批回放会被判成"编号不完整"，永远点不开。
_KEY = re.compile(r"^[0-9a-f]{16,40}$")

ARCHIVE_DIR = os.path.join(paths.CACHE_DIR, "replays")

# 扫目录时最多看这么多文件：真到几千份还没找到，多半是找错了，继续读下去只会卡页面。
MAX_SCAN = 5000


def archive_path(key: str) -> str:
    """这一局在存档目录里的路径；key 不合法时返回空串（顺便挡掉路径穿越）。"""
    text = (key or "").strip().lower()
    if not _KEY.match(text):
        return ""
    return os.path.join(ARCHIVE_DIR, text + ".osr")


def save(key: str, src: str) -> bool:
    """把一份回放复制进存档目录；已有同名存档就跳过。失败不抛，返回 False。"""
    dst = archive_path(key)
    if not dst or not src or not os.path.isfile(long_path(src)):
        return False
    if os.path.isfile(long_path(dst)):
        return True
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        shutil.copyfile(long_path(src), long_path(dst))
    except OSError:
        return False
    return True


def _scan_dirs() -> list[str]:
    """按"最可能在这里"排序的候选目录，只保留真实存在的。"""
    dirs = [paths.LOCAL_REPLAY_DIR]
    try:
        stable = osu_dirs.config_stable_dir()
    except Exception:  # noqa: BLE001 - 配置读不出来不该影响找文件
        stable = ""
    if stable:
        dirs.append(os.path.join(stable, "Replays"))
    try:
        dirs.extend(osu_dirs.default_export_roots())
    except Exception:  # noqa: BLE001
        pass
    out = []
    for d in dirs:
        if d and os.path.isdir(long_path(d)) and d not in out:
            out.append(d)
    return out


def find(key: str) -> str | None:
    """按回放 MD5 找文件：存档优先，其次扫常见目录。找不到返回 None。"""
    text = (key or "").strip().lower()
    if not _KEY.match(text):
        return None
    cached = archive_path(text)
    if cached and os.path.isfile(long_path(cached)):
        return cached

    seen = 0
    for folder in _scan_dirs():
        try:
            names = sorted(os.listdir(long_path(folder)))
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith(".osr"):
                continue
            seen += 1
            if seen > MAX_SCAN:
                return None
            full = os.path.join(folder, name)
            got = osr.read_replay_key(full)
            if not got or got[0] != text:
                continue
            save(text, full)   # 找到就地存一份，下次点不用再扫
            return full
    return None
