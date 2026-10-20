"""把「程序自己出错」写成一份标准格式的报告，落在 `crashreport/`。

只接两类事件：
  * 请求处理里没有被接住的异常（也就是会变成 HTTP 500 的那种）；
  * 启动失败（端口绑不上、目录建不出来、导入就炸）。

用户操作导致的错误（回放坏了、谱面不是 4K、文件是空的）**不写**这里 ——
那些是正常反馈，写进崩溃目录只会把真正的崩溃淹掉。

几条硬约束：
  * 写报告这件事本身绝不能再抛异常（它经常是在"已经出错"的路径上被调用）；
  * 不依赖 stderr：exe 是 `--noconsole`，那里 `sys.stderr` 是 None；
  * 同一个堆栈在一个进程里只写一次，避免用户反复点导致几百个同样的文件。
"""

from __future__ import annotations

import hashlib
import os
import platform
import sys
import time
import traceback
import unicodedata

from .. import paths
from ..version import __version__

# 同一份堆栈只落一次（进程内）。键是堆栈的哈希，不是异常文本 ——
# 文本里常带时间、随机 id，会导致去重失效。
_SEEN: set[str] = set()
# 目录里最多留这么多份，超了删最旧的。崩溃报告是给排查用的，不是归档。
KEEP = 50
LOG_TAIL_LINES = 60


def dir_path() -> str:
    return os.path.join(paths.DATA_ROOT, "crashreport")


def write(event: str, exc: BaseException | None = None, *, context: dict | None = None,
          detail: str = "") -> str | None:
    """写一份报告，返回文件路径；任何一步失败都返回 None（不抛）。"""
    try:
        text, signature = _render(event, exc, context or {}, detail)
        if signature in _SEEN:
            return None
        _SEEN.add(signature)

        target_dir = dir_path()
        os.makedirs(target_dir, exist_ok=True)
        name = "crash-%s-pid%d-%s.txt" % (
            time.strftime("%Y%m%d-%H%M%S"), os.getpid(), signature[:6])
        path = os.path.join(target_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        _prune(target_dir)
        return path
    except Exception:  # noqa: BLE001 - 报告写不出来也不能再炸一次
        return None


def summary(exc: BaseException | None) -> str:
    """一行摘要，给页面提示用：`TypeError: ...`。"""
    if exc is None:
        return ""
    try:
        last = traceback.format_exception_only(type(exc), exc)[-1].strip()
    except Exception:  # noqa: BLE001
        last = repr(exc)
    return last.splitlines()[-1] if last else repr(exc)


# ---------------------------------------------------------------- 内部
def _signature(event: str, exc: BaseException | None, tb_text: str) -> str:
    """给"同一处崩溃"算一个指纹：异常类型 + 消息 + **最内层**那一帧。

    不能用整条堆栈：同一处 bug 从两个不同调用点进来，堆栈前几帧就不一样了，
    去重会失效，用户点两下就多出两份报告。取最内层（真正出错的那一行）才稳。
    """
    key = event
    if exc is not None:
        frames = traceback.extract_tb(exc.__traceback__)
        if frames:
            last = frames[-1]
            key += "|%s:%s:%s" % (last.filename, last.lineno, last.name)
        key += "|%s|%s" % (type(exc).__name__, exc)
    else:
        key += "|" + tb_text[:400]
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()


def _render(event: str, exc: BaseException | None, context: dict,
            detail: str) -> tuple[str, str]:
    tb = ""
    if exc is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    elif detail:
        tb = detail
    signature = _signature(event, exc, tb)

    rows = [
        ("时间", time.strftime("%Y-%m-%d %H:%M:%S")),
        ("版本", __version__),
        ("运行形态", "exe（PyInstaller 冻结）" if paths.FROZEN else "源码"),
        ("Python", "%s（%s）" % (platform.python_version(), sys.executable or "?")),
        ("系统", "%s %s" % (platform.system(), platform.release())),
        ("架构", platform.machine() or "?"),
        ("进程", "pid %d" % os.getpid()),
        ("事件", event),
    ]
    for key, value in context.items():
        rows.append((key, value))
    if exc is not None:
        rows.append(("异常", summary(exc)))

    width = max(_display_width(k) for k, _ in rows)
    head = "\n".join("%s  %s" % (_pad(k, width), v) for k, v in rows)

    body = [
        "=" * 72,
        " osumania4k_replay_analyser 崩溃报告   （本文件只在本机生成，不会上传）",
        "=" * 72,
        head,
        "-" * 72,
        "堆栈",
        "-" * 72,
        tb.rstrip() or "(无)",
        "-" * 72,
        "最近日志（server.log 尾部 %d 行）" % LOG_TAIL_LINES,
        "-" * 72,
        _log_tail(),
        "=" * 72,
        "把这份文件和复现步骤一起发到 issue 即可。",
        "=" * 72,
        "",
    ]
    return "\n".join(body), signature


def _display_width(text: str) -> int:
    """东亚宽字符算 2 格 —— 否则中文标签那一栏对不齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _log_tail() -> str:
    try:
        path = paths.LOG_PATH
        if not os.path.isfile(path):
            return "(没有 server.log)"
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-LOG_TAIL_LINES:]
        return "".join(lines).rstrip() or "(日志是空的)"
    except OSError as e:
        return "(读不到日志：%r)" % (e,)


def _prune(target_dir: str) -> None:
    try:
        names = sorted(n for n in os.listdir(target_dir)
                       if n.startswith("crash-") and n.endswith(".txt"))
        for old in names[:-KEEP]:
            try:
                os.remove(os.path.join(target_dir, old))
            except OSError:
                pass
    except OSError:
        pass
