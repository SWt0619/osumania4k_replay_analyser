"""osu! 客户端在本机的目录位置：曲库（Songs）、导出（exports）、文件库（files）。

为什么要单独一个模块：这三处路径的猜法原先在 `library.py` 和 `skin.py` 里各写了一份，
改了一边就会表现成"曲库能找到谱面、皮肤素材却找不到"——只在某些机器上复现，
排查时很难想到是两份常量走了样。目录探测是纯环境问题，与"索引"和"皮肤匹配"
都无关，放在这里让两边共用同一份答案。

关于"非默认安装"：osu! 可以装在任何盘、任何目录名，也可以用便携模式（把数据放在
安装目录里）。所以不能只写死几个候选路径，按可靠度依次尝试：

  1. 稳定版的用户配置 `osu!.<用户名>.cfg` 里的 `BeatmapDirectory`——这是最准的，
     玩家自己改过曲库位置也记在这里；
  2. 注册表里 osu! 的安装位置（卸载项 / 文件关联），推出安装目录后再读第 1 条；
  3. lazer 的 `storage.ini`（玩家把数据目录挪走时会出现）；
  4. 各盘根目录扫一层，找带 `osu!.exe` 或 `osu!*.cfg` 的目录；
  5. 兜底的几个常见路径。

全部找不到时返回空列表——上层会提示玩家手动选 `.osu`，不会直接崩。
"""

from __future__ import annotations

import glob
import os

from . import config_store
from .beatmap import long_path


def _appdata() -> str:
    return os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")


def _existing(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if not p:
            continue
        q = os.path.normcase(os.path.normpath(p))
        if q in seen:
            continue
        seen.add(q)
        if os.path.isdir(long_path(p)):
            out.append(p)
    return out


# ---------------------------------------------------------------- 稳定版配置


def _read_beatmap_dir(cfg_path: str) -> str | None:
    """从 `osu!.<用户名>.cfg` 里读出 `BeatmapDirectory`。

    文件是 ini 风格、UTF-8 带 BOM，键值可能散落在 [Paths] 段里；这里只按行找键，
    不解析段，遇到同名键取第一个——osu! 自己写的配置里这个键只出现一次。
    """
    try:
        with open(cfg_path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                if key.strip().lower() != "beatmapdirectory":
                    continue
                val = val.strip().strip('"')
                # 相对路径是相对安装目录的（便携模式常见）
                if val and not os.path.isabs(val):
                    val = os.path.join(os.path.dirname(cfg_path), val)
                return val or None
    except OSError:
        return None
    return None


def _songs_from_install_dir(install_dir: str) -> list[str]:
    """给一个 osu! 安装目录，推出可能的曲库位置。"""
    if not install_dir or not os.path.isdir(long_path(install_dir)):
        return []
    out: list[str] = []
    for cfg in glob.glob(os.path.join(install_dir, "osu!*.cfg")):
        p = _read_beatmap_dir(cfg)
        if p:
            out.append(p)
    # 便携模式/默认情况下数据就在安装目录里
    out.append(os.path.join(install_dir, "Songs"))
    return out


def _registry_install_dirs() -> list[str]:
    """从注册表猜安装目录。拿不到就返回空（非 Windows 或被精简过都属正常）。"""
    try:
        import winreg
    except ImportError:
        return []

    out: list[str] = []

    def _read(hive, sub, value):
        try:
            with winreg.OpenKey(hive, sub) as k:
                v, _ = winreg.QueryValueEx(k, value)
                return str(v)
        except OSError:
            return ""

    # 文件关联：默认值形如 "C:\osu!\osu!.exe" "%1"
    for sub in (r"osu\shell\open\command", r"osu\DefaultIcon"):
        raw = _read(winreg.HKEY_CLASSES_ROOT, sub, "")
        if raw:
            exe = raw.split('"')[1] if raw.startswith('"') else raw.split(" ")[0]
            if exe.lower().endswith(".exe"):
                out.append(os.path.dirname(exe))

    # 卸载项：osu! 的安装器会写 InstallLocation / DisplayIcon
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        base = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
        try:
            with winreg.OpenKey(hive, base) as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    name = winreg.EnumKey(root, i)
                    if "osu" not in name.lower():
                        continue
                    loc = _read(hive, base + "\\" + name, "InstallLocation")
                    if loc:
                        out.append(loc.strip('"'))
        except OSError:
            continue

    return out


def _lazer_custom_data_dir() -> list[str]:
    """lazer 把数据目录挪走时，`%APPDATA%\\osu\\storage.ini` 里会写新位置。"""
    ini = os.path.join(_appdata(), "osu", "storage.ini")
    if not os.path.isfile(long_path(ini)):
        return []
    out: list[str] = []
    try:
        with open(ini, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                # lazer 自定义数据目录写在 storage.ini 的 FullPath 里
                # （osy.Game/IO/OsuStorage.cs 读的就是 StorageConfig.FullPath）；
                # 另外三个是历史/别处见过的写法，一起认。
                if key.strip().lower() in ("fullpath", "data", "songs", "beatmapdirectory"):
                    val = val.strip().strip('"')
                    if val:
                        out.append(os.path.join(val, "Songs"))
                        out.append(val)
    except OSError:
        pass
    return out


_NESTED_PARENTS = ("games", "game", "游戏", "program files", "program files (x86)")


def _scan_for_installs() -> list[str]:
    """各盘扫一层，找**含 osu!.exe 的目录**——不看目录叫什么名字。

    这一条是"非默认安装"的主力：安装目录可能叫 `OsuStable`、`osu-4k`、
    `Osu! 2024`，光按名字猜一定会漏。判据用 `osu!.exe` 这种必然存在的文件，
    每个子目录只做一次 `isfile`，三四个盘加起来也就几毫秒。

    带时间预算：映射到本机的网络盘 `listdir` 可能卡几秒，扫超过 200ms 就收手，
    宁可漏掉也要保证程序能立刻打开。
    """
    import time

    deadline = time.monotonic() + 0.2
    out: list[str] = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        if time.monotonic() > deadline:
            break
        root = letter + ":\\"
        if not os.path.isdir(root):
            continue
        try:
            children = [os.path.join(root, n) for n in os.listdir(root)]
        except OSError:
            continue
        for d in children:
            if os.path.isfile(os.path.join(d, "osu!.exe")):
                out.append(d)
            elif os.path.basename(d).lower() in _NESTED_PARENTS:
                # 装在 Games\osu! 这种子目录里的情况，只对常见父目录再下探一层
                try:
                    subs = [os.path.join(d, n) for n in os.listdir(d)]
                except OSError:
                    continue
                for s in subs:
                    if os.path.isfile(os.path.join(s, "osu!.exe")):
                        out.append(s)
    return out


# ---------------------------------------------------------------- 对外接口


def default_export_roots() -> list[str]:
    r"""lazer「导出」的图集目录（osu\exports\<图集名>\*.osu）。"""
    out = []
    lazer = config_lazer_dir()
    if lazer:
        out.append(os.path.join(lazer, "exports"))
    out.append(os.path.join(_appdata(), "osu", "exports"))
    return _existing(out)


def default_lazer_file_roots() -> list[str]:
    r"""lazer 的内部文件库（osu\files\<xx>\<sha256>，无扩展名，靠内容辨认）。"""
    out = []
    lazer = config_lazer_dir()
    if lazer:
        out.append(os.path.join(lazer, "files"))
    out.append(os.path.join(_appdata(), "osu", "files"))
    return _existing(out)


def _config() -> dict:
    """读 `_cache/config.json`（读不到就当空配置）。

    读写细节统一在 config_store 里，这里只关心「玩家手填的目录」这几个键。
    """
    return config_store.read()


def _env_songs_root() -> list[str]:
    """命令行 `--songs` 指定的曲库（由 server.main 写进环境变量）。

    这一项必须真的被读：参数在帮助里写着"手动指定 osu! Songs 目录"，
    只设环境变量而没人读的话，用户会以为指定了、实际还是扫的默认位置。
    """
    val = (os.environ.get("MANIA_SONGS_ROOT") or "").strip()
    return [val] if val else []


def _config_songs_root() -> list[str]:
    """网页里手动指定的曲库路径（`songsRoot`）。"""
    val = _config().get("songsRoot")
    return [val] if isinstance(val, str) and val.strip() else []


def config_stable_dir() -> str:
    val = _config().get("stableDir")
    return val if isinstance(val, str) and val.strip() else ""


def config_lazer_dir() -> str:
    val = _config().get("lazerDir")
    return val if isinstance(val, str) and val.strip() else ""


def inspect_stable_dir(d: str) -> tuple[bool, str]:
    """检查用户填的稳定版目录对不对。返回 (是否可用, 给用户看的话)。"""
    if not d:
        return False, "还没填路径"
    if not os.path.isdir(long_path(d)):
        return False, "这个路径不存在（检查一下盘符和文件夹名）"
    if os.path.isfile(os.path.join(long_path(d), "osu!.exe")):
        return True, "看起来是 osu! 安装目录"
    if glob.glob(os.path.join(long_path(d), "osu!*.cfg")):
        return True, "没看到 osu!.exe，但有 osu! 的配置文件，按安装目录处理"
    if os.path.isdir(os.path.join(long_path(d), "Songs")):
        return True, "这个目录里有 Songs，按安装目录处理"
    return False, "这个目录里没有 osu!.exe，请选到 osu! 的安装目录那一级"


def inspect_lazer_dir(d: str) -> tuple[bool, str]:
    """检查用户填的 lazer 数据目录对不对。"""
    if not d:
        return False, "还没填路径"
    if not os.path.isdir(long_path(d)):
        return False, "这个路径不存在（检查一下盘符和文件夹名）"
    has_songs = os.path.isdir(os.path.join(long_path(d), "Songs"))
    has_files = os.path.isdir(os.path.join(long_path(d), "files"))
    if has_songs or has_files:
        return True, "看起来是 lazer 的数据目录"
    # 允许直接填到 Songs 那一级
    if os.path.basename(os.path.normpath(d)).lower() == "songs":
        return True, "按 Songs 目录处理"
    return False, "这个目录里没有 Songs 或 files，请选到 osu!lazer 的数据目录（含 Songs、files 的那一级）"


_ROOTS_CACHE: list[str] | None = None
_DETECTED_CACHE: list[str] | None = None


def reset_cache() -> None:
    """清掉探测缓存（测试用；运行时改了 config.json 需要重启程序）。"""
    global _ROOTS_CACHE, _DETECTED_CACHE
    _ROOTS_CACHE = None
    _DETECTED_CACHE = None


def _explicit_roots() -> list[str]:
    """玩家明确指定的曲库：命令行 `--songs` 优先，其次网页里填的。

    这两项既不在注册表里也不在常见路径里，所以不能混进「自动探测」那一组 ——
    否则「传了自定义目录还去合并别的默认曲库」会把它自己也算成默认根。
    """
    out = _env_songs_root() + _config_songs_root()
    cfg_stable = config_stable_dir()
    if cfg_stable:
        out.extend(_songs_from_install_dir(cfg_stable))
    cfg_lazer = config_lazer_dir()
    if cfg_lazer:
        out.append(os.path.join(cfg_lazer, "Songs"))
    return out


def detected_songs_roots() -> list[str]:
    """自动探测到的曲库（不含玩家指定的那些）。

    单独一个函数是给 `library._scan_all` 用的：它要判断「传进来这个根是不是默认曲库之一」，
    玩家手填的目录不该算进来（否则会连带扫描旁边几个真实曲库，反而更慢）。
    """
    global _DETECTED_CACHE
    if _DETECTED_CACHE is not None:
        return list(_DETECTED_CACHE)

    home = os.path.expanduser("~")
    appdata = _appdata()
    localappdata = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")

    candidates: list[str] = []
    # 1) 安装目录 → 稳定版 cfg 里的 BeatmapDirectory
    install_dirs: list[str] = list(_registry_install_dirs())
    install_dirs.extend(_scan_for_installs())
    for d in install_dirs:
        candidates.extend(_songs_from_install_dir(d))

    # 2) lazer 的自定义数据目录
    candidates.extend(_lazer_custom_data_dir())

    # 3) 常见默认位置
    candidates.extend([
        os.path.join(appdata, "osu", "Songs"),
        os.path.join(appdata, "osu!", "Songs"),
        os.path.join(localappdata, "osu!", "Songs"),
        os.path.join(home, "osu", "Songs"),
        r"D:\osu!\Songs",
        r"C:\osu!\Songs",
    ])

    _DETECTED_CACHE = _existing(candidates)
    return list(_DETECTED_CACHE)


def default_songs_roots() -> list[str]:
    """猜测本机 osu! 曲库位置（lazer 与 stable 都覆盖，含非默认安装）。

    顺序：玩家指定的在前，自动探测的在后 —— 调用方一律取第一个当主根。

    结果按进程缓存：探测要在各盘列目录，每次请求都扫一遍没必要；
    代价是运行期间新装的 osu! 或改过的 config.json 要重启程序才生效。
    """
    global _ROOTS_CACHE
    if _ROOTS_CACHE is not None:
        return list(_ROOTS_CACHE)
    _ROOTS_CACHE = _existing(_explicit_roots() + detected_songs_roots())
    return list(_ROOTS_CACHE)
