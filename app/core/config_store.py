"""`_cache/config.json` 的唯一读写入口。

为什么单独一个模块：这份配置文件有三个消费者 —— `osu_dirs` 读玩家手填的游戏目录、
`audio_fetch` 读写联网开关、`server` 在保存目录时把新旧字段合并回去。各写一遍的后果
是「保存了不生效」或「一次保存把另一个字段冲掉」，而且只在特定操作顺序下复现。

约定：
  * 顶层永远是对象，读到数组/标量一律当空配置；
  * 写盘走「临时文件 + os.replace」，不会让另一个线程/进程读到半个 JSON；
  * 路径默认 `app/_cache/config.json`，测试或打包形态可以传别的目录。
"""

from __future__ import annotations

import json
import os
from typing import Iterable

from ..paths import CACHE_DIR
from .beatmap import long_path


def path_of(cache_dir: str | None = None) -> str:
    return os.path.join(cache_dir or CACHE_DIR, "config.json")


def read(cache_dir: str | None = None) -> dict:
    """读配置；文件不存在、坏掉、或顶层不是对象时返回空 dict。"""
    try:
        with open(long_path(path_of(cache_dir)), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write(data: dict, cache_dir: str | None = None) -> None:
    """整体写盘。失败抛 OSError，由调用方决定是提示用户还是静默。"""
    target = path_of(cache_dir)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, target)


def merge(changes: dict, remove: Iterable[str] = (), cache_dir: str | None = None) -> dict:
    """改几个字段再写回，其余字段原样保留。返回合并后的配置。

    先删后加：同一个键同时出现在 changes 与 remove 里时，以 changes 为准。
    """
    data = read(cache_dir)
    for key in remove:
        data.pop(key, None)
    data.update(changes)
    write(data, cache_dir)
    return data
