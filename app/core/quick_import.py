"""快捷导入：从 osu! 刚导出的那一份回放里找最新的一份。

为什么只认 `Replays` 与 lazer 的 `exports`
----------------------------------------
stable 在结算屏按 F2 会把回放导出成「玩家 - 曲名 [难度] (日期) OsuMania.osr」，
lazer 的导出落在 `osu\\exports\\`，名字同样可读。这两个目录里**一眼就能看出哪份是最新的**，
不需要解析内容、不需要猜"这个文件是不是刚打完那一局"。

stable 另外还会把每局自动存进 `Data\\r\\`（加密文件名），那里文件更全，但混着别人的回放、
名字也认不出是谁的。所以这条路径只用来"救急"，不作为常规入口——用户在结算屏按一下 F2，
成本极低，换来的是这里可以完全不猜。

判断"刚导出"的口径
------------------
按文件修改时间倒序，只认最近 `FRESH_WINDOW` 秒内的那些。超过这个窗口就当作"不是刚打完的"，
宁可让用户再按一次 F2，也不要悄悄分析一份十分钟前的旧回放。
"""

from __future__ import annotations

import os
import time

from . import osu_dirs

# 多久以内算"刚导出的"。十分钟足够覆盖"打完看结算、再想起来分析"的间隔。
FRESH_WINDOW = 10 * 60


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def replay_dirs() -> list[str]:
    """可能装着导出回放的目录（已去重，只保留真实存在的）。

    从曲库根反推安装目录，而不是重新扫注册表：`<安装目录>\\Songs` 的上一级就是安装目录，
    同级下的 `Replays`（stable）与 `exports`（lazer）就是导出目录。
    这一层只读目录名，成本可以忽略。
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if not path or not os.path.isdir(path):
            return
        key = _norm(path)
        if key in seen:
            return
        seen.add(key)
        out.append(path)

    for songs in osu_dirs.default_songs_roots():
        install = os.path.dirname(os.path.normpath(songs))
        if os.path.basename(install).lower() == "songs":
            install = os.path.dirname(install)
        add(os.path.join(install, "Replays"))
        add(os.path.join(install, "exports"))
    for extra in osu_dirs.default_export_roots():
        add(extra)
    return out


def candidates(limit: int = 8) -> list[dict]:
    """按修改时间倒序列出最近的导出回放（只 stat，不读内容）。"""
    rows: list[dict] = []
    for directory in replay_dirs():
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith(".osr"):
                continue
            path = os.path.join(directory, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            if info.st_size <= 0:
                # 0 字节说明还在写，或者写到一半失败；等下一次按键再试
                continue
            rows.append({
                "path": path,
                "name": name,
                "size": info.st_size,
                "mtime": info.st_mtime,
                "dir": directory,
                "source": "exports" if os.path.basename(directory).lower() == "exports" else "Replays",
            })
    rows.sort(key=lambda row: row["mtime"], reverse=True)
    return rows[:limit]


def latest(within: float = FRESH_WINDOW) -> dict | None:
    """最年轻的一份导出回放；超出时间窗口时返回 None。"""
    now = time.time()
    for item in candidates(limit=20):
        if now - item["mtime"] <= within:
            return item
    return None


def label(item: dict) -> str:
    """给人看的一行说明：文件名去掉扩展名，再补上来源目录。"""
    if not item:
        return ""
    name = os.path.splitext(str(item.get("name") or ""))[0]
    return "%s（%s）" % (name, item.get("source") or "")
