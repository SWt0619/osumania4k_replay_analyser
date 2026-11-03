"""运行路径：源码运行与便携版（解压即用）共用同一套。

两种发布形态的目录布局是一样的：资源与可写目录都在程序根目录下。
以前还支持 PyInstaller 打出来的单文件 exe，那时资源在临时解压目录、缓存必须写到
exe 旁边；单文件 exe 已经不再发行，这层分支也就一起删掉了。
"""

from __future__ import annotations

import os

# app 包所在目录
APP_DIR = os.path.dirname(os.path.abspath(__file__))
# 资源根：web / vendor / data 都在它下面
ROOT = os.path.dirname(APP_DIR)
# 可写根：源码模式与便携版都是项目根
DATA_ROOT = ROOT

WEB_DIR = os.path.join(APP_DIR, "web")
LOCAL_REPLAY_DIR = os.path.join(DATA_ROOT, "_replays")
REPORT_DIR = os.path.join(DATA_ROOT, "报告")
# 导出的成绩卡 / B50 默认落在这里（前端生成 PNG 后 POST 回来写盘）
CARD_DIR = os.path.join(DATA_ROOT, "card")


def _writable(path: str) -> bool:
    """真去建一下、再写一个文件试试 —— 只看 os.access 在 Windows 上会误判。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write-test")
        with open(probe, "w", encoding="ascii") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except (OSError, ValueError):
        return False


def _in_temp_dir(path: str) -> bool:
    """这个目录是不是在系统临时目录底下。

    判断它是因为一种很常见的用法：直接从压缩包里双击 exe。Windows 会先把整个包解压到
    `%TEMP%\\Temp1_xxx` 再运行，于是"程序目录"每次都是一个新临时目录 —— 配置写在那儿，
    关掉窗口就跟着没了，用户看到的就是"每次重启都要重填一遍"。
    """
    import tempfile

    try:
        tmp = os.path.realpath(tempfile.gettempdir()).lower().rstrip("\\/")
        target = os.path.realpath(path).lower().rstrip("\\/")
        return target == tmp or target.startswith(tmp + os.sep)
    except (OSError, ValueError):
        return False


def _pick_cache_dir() -> tuple[str, bool]:
    """可写的缓存目录。装到只读位置时退到 %TEMP%，别让 import 就把进程带崩。

    这一层必须在模块导入时做完：config_store 等模块是按值 `from ..paths import CACHE_DIR`
    拿走的，等到 server 起来再改就晚了。
    """
    primary = os.path.join(ROOT, "app", "_cache")
    if _writable(primary) and not _in_temp_dir(primary):
        return primary, False
    import tempfile

    # 程序目录在临时目录里（压缩包直接运行的典型情况）：换个固定的地方存，
    # 下次再解压到别的临时目录也还读得到同一份配置。
    if _in_temp_dir(primary):
        stable = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                              "osumania_replay_analyser")
        if _writable(stable):
            return stable, True
    fallback = os.path.join(tempfile.gettempdir(), "osumania_replay_analyser")
    if _writable(fallback):
        return fallback, True
    return primary, False          # 两边都写不了：保持原样，后面报错会带真实路径


CACHE_DIR, CACHE_FALLBACK = _pick_cache_dir()

PORT_FILE = os.path.join(CACHE_DIR, "port.txt")
PID_FILE = os.path.join(CACHE_DIR, "server.pid")
LOG_PATH = os.path.join(CACHE_DIR, "server.log")
