"""osu! 客户端在本机的目录位置：曲库（Songs）、导出（exports）、文件库（files）。

为什么要单独一个模块：这三处路径的猜法原先在 `library.py` 和 `skin.py` 里各写了一份，
改了一边就会表现成"曲库能找到谱面、皮肤素材却找不到"——只在某些机器上复现，
排查时很难想到是两份常量走了样。目录探测是纯环境问题，与"索引"和"皮肤匹配"
都无关，放在这里让两边共用同一份答案。
"""

from __future__ import annotations

import os

from .beatmap import long_path


def _appdata() -> str:
    return os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")


def _existing(paths: list[str]) -> list[str]:
    return [p for p in paths if os.path.isdir(long_path(p))]


def default_export_roots() -> list[str]:
    r"""lazer「导出」的图集目录（osu\exports\<图集名>\*.osu）。"""
    return _existing([os.path.join(_appdata(), "osu", "exports")])


def default_lazer_file_roots() -> list[str]:
    r"""lazer 的内部文件库（osu\files\<xx>\<sha256>，无扩展名，靠内容辨认）。"""
    return _existing([os.path.join(_appdata(), "osu", "files")])


def default_songs_roots() -> list[str]:
    """猜测本机 osu! 曲库位置（lazer 与 stable 都覆盖）。"""
    home = os.path.expanduser("~")
    appdata = _appdata()
    localappdata = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    candidates = [
        os.path.join(appdata, "osu", "Songs"),
        os.path.join(appdata, "osu!", "Songs"),
        os.path.join(localappdata, "osu!", "Songs"),
        os.path.join(home, "osu", "Songs"),
        r"D:\osu!\Songs",
        r"C:\osu!\Songs",
    ]
    return _existing(candidates)
