"""运行路径：源码运行与 PyInstaller 打包后都能找到资源、也能找到可写目录。

两种形态的差别只有一处：
  * 源码运行：`_MEIPASS` 不存在，资源与可写目录都在项目根；
  * exe 运行：资源在 PyInstaller 的临时解压目录（`sys._MEIPASS`，只读、退出即删），
    缓存、端口号、报告这些必须写到 exe 旁边，否则重启一次就全丢了。
"""

from __future__ import annotations

import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))

# app 包所在目录；打包后是 _MEIPASS\app
APP_DIR = os.path.dirname(os.path.abspath(__file__))
# 资源根：web / vendor / data 都在它下面
ROOT = os.path.dirname(APP_DIR)
# 可写根：exe 所在目录，或源码模式下的项目根
DATA_ROOT = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else ROOT

WEB_DIR = os.path.join(APP_DIR, "web")
CACHE_DIR = os.path.join(DATA_ROOT, "_cache") if FROZEN else os.path.join(ROOT, "app", "_cache")
LOCAL_REPLAY_DIR = os.path.join(DATA_ROOT, "_replays")
REPORT_DIR = os.path.join(DATA_ROOT, "报告")

PORT_FILE = os.path.join(CACHE_DIR, "port.txt")
PID_FILE = os.path.join(CACHE_DIR, "server.pid")
LOG_PATH = os.path.join(CACHE_DIR, "server.log")
