"""缓存目录的用量统计与清理。

为什么单独一个模块：`audio` / `chunks` / `uploads` / `downloads` 四个目录的
清理规则不一样，散在接口层里最容易漏掉其中一个。这里只负责「算多大」和「删什么」，
HTTP 那边只把结果转成 JSON。

音频目录是唯一会长期长大的一个：每补一次音频就从镜像下一个整包、把音频抽出来留下，
一个包几 MB 到几十 MB，玩上一年几个 GB 很正常。所以设置页必须有个能看见、能清掉的地方。
"""

from __future__ import annotations

import os

# 顺序就是设置页里的显示顺序；label 直接给页面用，免得前端再维护一份文案
TARGETS = (
    ("audio", "音频缓存（补音频时下载的整包）"),
    ("chunks", "上传分片（中断后留下的临时片）"),
    ("uploads", "分析临时文件（回放与谱面副本）"),
    ("downloads", "谱面包下载的临时文件"),
)

_NAMES = {name for name, _ in TARGETS}


def _folder(cache_dir: str, name: str) -> str:
    return os.path.join(cache_dir, name)


def folder_size(folder: str) -> tuple[int, int]:
    """（字节数, 文件数）。读不到就当 0：设置页不该因为某个目录权限问题整块报错。"""
    total = 0
    count = 0
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                        count += 1
                except OSError:
                    continue
    except OSError:
        pass
    return total, count


def stats(cache_dir: str) -> dict:
    """各项占用与总计，给设置页画列表用。"""
    items = []
    for name, label in TARGETS:
        size, files = folder_size(_folder(cache_dir, name))
        items.append({"name": name, "label": label, "bytes": size, "files": files})
    return {"items": items, "total": sum(item["bytes"] for item in items)}


def _remove_tree(folder: str) -> tuple[int, int]:
    """删掉目录下的所有文件（含子目录），返回（删除字节, 删除文件数）。"""
    freed = 0
    removed = 0
    try:
        with os.scandir(folder) as it:
            entries = list(it)
    except OSError:
        return 0, 0

    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                sub_freed, sub_removed = _remove_tree(entry.path)
                freed += sub_freed
                removed += sub_removed
                try:
                    os.rmdir(entry.path)
                except OSError:
                    pass
            else:
                info = entry.stat(follow_symlinks=False)
                os.remove(entry.path)
                freed += info.st_size
                removed += 1
        except OSError:
            # 单个文件被别的进程占着（正在下载）就跳过，剩下的照删
            continue
    return freed, removed


def clean(cache_dir: str, names) -> dict:
    """清掉指定的几项，返回释放的字节数与文件数。

    `audio` 连 `_index.json` 一起删：索引里记的都是已经删掉的音频，
    留着它只会在下次补音频时多一次无效查找。音频本身下次要用会重新下载。
    """
    wanted = [name for name in names if name in _NAMES]
    freed = 0
    removed = 0
    for name in wanted:
        size, files = _remove_tree(_folder(cache_dir, name))
        freed += size
        removed += files
    return {"names": wanted, "freed": freed, "removed": removed}


def _is_part(name: str) -> bool:
    """下载碎片：`xxx.osz.part`，以及并发分片的 `xxx.p0`~`xxx.p15`。"""
    low = name.lower()
    if low.endswith(".part"):
        return True
    head, sep, tail = low.rpartition(".p")
    return bool(sep) and bool(head) and tail.isdigit() and len(tail) <= 2


def sweep_parts(cache_dir: str) -> int:
    """启动时清掉音频目录里的下载碎片，返回删掉的文件数。

    正常流程在 finally 里会自己删；进程被任务管理器关掉时 finally 跑不到，
    而碎片名里带着进程号与线程号，以后永远不会被复用，只能靠这里收尾。
    """
    folder = _folder(cache_dir, "audio")
    dropped = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    for name in names:
        if not _is_part(name):
            continue
        try:
            os.remove(os.path.join(folder, name))
            dropped += 1
        except OSError:
            pass
    return dropped
