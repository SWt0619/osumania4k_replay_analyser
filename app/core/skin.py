"""从谱面目录或 lazer 皮肤导出里找 mania 的 note 素材（"小球"）。

背景：用户希望回放画面里的音符用皮肤素材（如 Notes4K/left2.png 那颗蓝色小球）
来画，而不是程序自己拼的色块。素材有两个可能的家：
  * 谱面目录（Songs / lazer 导出的图集，有的图集自带 note 皮肤）；
  * lazer 导出的**皮肤**目录（osu/exports/<皮肤名>/，里面有 Notes4K/left*.png）。

支持的命名（按优先级）：
  1. note1.png ~ noteN.png        经典方案，1 基、从左到右；
  2. left*/centre*/right*         舞台分组方案，编号从舞台中心向两侧数
                                   （这是镜像皮肤的通用约定），right 缺失时
                                   用同号 left 镜像补位；
  3. Note.png / Note2.png         lazer 风格，两图按列交替。
另外收集 LNBody.png（长按身体），前端画长条时用。
"""

from __future__ import annotations

import os

from .beatmap import long_path
from .osu_dirs import default_export_roots

IMG_EXTS = (".png", ".jpg", ".jpeg")


def _dir_files(directory: str) -> dict[str, str]:
    """目录里 小写文件名 -> 完整路径 的映射（含一层子目录的顶层不递归）。"""
    out: dict[str, str] = {}
    try:
        for fn in os.listdir(long_path(directory)):
            full = os.path.join(directory, fn)
            if os.path.isfile(full):
                out[fn.lower()] = full
    except OSError:
        pass
    return out


def _pick(files: dict[str, str], stems: list[str]) -> str:
    """按候选名找图，普通优先、@2x 兜底（@2x 缩放画即可）。"""
    for stem in stems:
        for ext in IMG_EXTS:
            p = files.get(stem + ext)
            if p:
                return p
        for ext in IMG_EXTS:
            p = files.get(stem + "@2x" + ext)
            if p:
                return p
    return ""


def _map_stage_group(files: dict[str, str], keys: int) -> list[str]:
    """left/centre/right 舞台分组方案。right 缺失时用同号 left 镜像补位。"""
    half = keys // 2
    odd = keys % 2 == 1
    centre = _pick(files, ["centre", "center"]) if odd else ""
    mapping = [""] * keys
    for i in range(keys):
        if odd and i == half:
            mapping[i] = centre
            continue
        if i < half:
            n = half - i
            mapping[i] = _pick(files, ["left%d" % n])
        else:
            n = i - half + (0 if odd else 1)
            img = _pick(files, ["right%d" % n, "right"])
            if not img:
                # 只有 left 组的对称皮肤：右边用同号 left 镜像补位
                img = _pick(files, ["left%d" % n])
            mapping[i] = img
    return mapping


def _map_columns(files: dict[str, str], keys: int) -> list[str]:
    """每个方案整体参评：只整套采用「配满所有列」的方案，避免不同方案的
    文件名互相撞车（比如 Note2.png 被误认成 note2 的经典命名）。"""
    # 方案 1：note1..noteN（1 基、从左到右）
    classic = [_pick(files, ["note%d" % (i + 1)]) for i in range(keys)]
    if all(classic):
        return classic

    # 方案 2：left/centre/right 舞台分组（用户常见的"小球"就在这组里）
    stage = _map_stage_group(files, keys)
    if all(stage):
        return stage

    # 方案 3：lazer 风格 Note.png / Note2.png 交替（奇列 Note，偶列 Note2）
    base = _pick(files, ["note"])
    alt = _pick(files, ["note2"])
    if base and alt:
        return [base if i % 2 == 0 else alt for i in range(keys)]
    if base:
        return [base] * keys

    # 都配不满：把三个方案的可用结果按优先级拼一份尽量全的
    out = [""] * keys
    for src in (stage, classic):
        for i in range(keys):
            if not out[i] and src[i]:
                out[i] = src[i]
    if base:
        for i in range(keys):
            if not out[i]:
                out[i] = base
    return out


def _score(mapping: list[str]) -> int:
    return sum(1 for x in mapping if x)


def _scan_dirs(dirs: list[str], keys: int, min_score: int) -> tuple[int, dict] | None:
    """逐个目录试配 note 素材，返回（配出的列数, 结果）里列数最多的那份。

    两个入口（谱面目录 / 皮肤导出目录）的差别只有"候选目录怎么来"和"至少配出几列"，
    挑最优的那套逻辑必须只有一份：分开写时很容易变成一处取最优、一处取第一个。
    """
    best: tuple[int, dict] | None = None
    for d in dirs:
        files = _dir_files(d)
        mapping = _map_columns(files, keys)
        score = _score(mapping)
        if score >= min_score and (best is None or score > best[0]):
            best = (
                score,
                {"columns": mapping, "lnBody": _pick(files, ["lnbody"]), "lnTail": _pick(files, ["lntail"])},
            )
        # 配满所有列、长按素材也齐了就是最优解，不必再扫后面的目录
        if best and best[0] == keys and best[1]["lnBody"] and best[1]["lnTail"]:
            break
    return best


def find_note_images(beatmap_path: str, keys: int) -> dict:
    """在谱面所在目录（及其一层子目录）里找 note 素材。

    返回 {"columns": [路径或 "" ...], "lnBody": 路径或 "", "lnTail": 路径或 ""}。
    子目录里名字带 note/skin 的优先（比如 Notes4K\），其次全部子目录逐个试，
    取配出列数最多的那份。
    """
    keys = max(1, int(keys))
    empty = {"columns": [""] * keys, "lnBody": "", "lnTail": ""}
    base = os.path.dirname(beatmap_path)
    if not base or not os.path.isdir(long_path(base)):
        return empty

    try:
        subs = [n for n in os.listdir(long_path(base)) if os.path.isdir(os.path.join(base, n))]
    except OSError:
        subs = []
    preferred = [os.path.join(base, s) for s in subs if "note" in s.lower() or "skin" in s.lower()]
    others = [os.path.join(base, s) for s in subs if os.path.join(base, s) not in preferred]
    best = _scan_dirs([base] + preferred + others, keys, min_score=0)
    return best[1] if best else empty


def find_skin_in_exports(keys: int) -> dict:
    """从 lazer 的皮肤导出目录（osu/exports/<皮肤名>/）里找 note 素材。

    用户在 lazer 里导出的皮肤是"一个图集一个文件夹"，里面可能还有 Notes4K/ 这种
    按键数分组的子目录。谱面目录里找不到素材时，用最近导出的那套皮肤兜底。
    """
    keys = max(1, int(keys))
    empty = {"columns": [""] * keys, "lnBody": "", "lnTail": ""}
    candidates: list[str] = []
    for exports_root in default_export_roots():
        try:
            for name in os.listdir(long_path(exports_root)):
                d = os.path.join(exports_root, name)
                if os.path.isdir(d):
                    candidates.append(d)
        except OSError:
            continue

    # 按修改时间挑最新的皮肤目录优先（导出多套时，最新的一般是正在用的）
    def sort_key(d: str) -> float:
        try:
            return os.path.getmtime(d)
        except OSError:
            return 0.0

    candidates.sort(key=sort_key, reverse=True)

    dirs: list[str] = []
    for d in candidates:
        # 皮肤目录里可能还有 Notes4K/ 这类按键数分组的子目录，一并参与
        dirs.append(d)
        try:
            dirs += [os.path.join(d, n) for n in os.listdir(long_path(d)) if os.path.isdir(os.path.join(d, n))]
        except OSError:
            pass

    # 只认"长得像 mania note 皮肤"的目录：至少配出两列，别把无关图集误当皮肤
    best = _scan_dirs(dirs, keys, min_score=2)
    return best[1] if best else empty
