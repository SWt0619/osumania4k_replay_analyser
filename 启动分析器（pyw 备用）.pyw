# -*- coding: utf-8 -*-
"""备用启动器：双击用 pythonw 跑（没有控制台窗口）。

和 启动分析器.vbs 做的是同一件事：起本地服务 → 等服务就绪 → 打开一个网页。
放在这里当备用的原因：VBS 的健康检查依赖 COM 组件（WinHTTP/XMLHTTP），
在某些机器上会被系统代理或组件策略影响；这条路只用标准库，行为可预测。

日志同样写在 _tools\\_launcher.log。
要停服务：任务管理器里结束跑 app\\server.py 的 python，或用「停止分析器.vbs」。
"""

import os
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(ROOT, "app")
CACHE = os.path.join(APP, "_cache")
PORT_FILE = os.path.join(CACHE, "port.txt")
LOG_FILE = os.path.join(ROOT, "_tools", "_launcher.log")
DEFAULT_PORT = 8806

# 不走系统代理：本机 127.0.0.1 的请求如果被代理拦下来，就会误判成"服务没起来"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S") + "  [pyw] " + msg + "\n")
    except Exception:
        pass


def alive(port, timeout=1.0):
    try:
        r = OPENER.open("http://127.0.0.1:%d/api/health" % port, timeout=timeout)
        return r.status == 200
    except Exception:
        return False


def find_python():
    """优先用当前解释器（双击 .pyw 时就是 pythonw），它一定带得动这个项目。"""
    exe = sys.executable or ""
    if exe and os.path.isfile(exe):
        # pythonw 不能带起子进程跑服务（它没有控制台，但 server 只 print，没关系；
        # 不过 pythonw 的 sys.executable 是 pythonw.exe，用它起服务也完全可以）
        return exe
    for cand in ("python", "py"):
        try:
            subprocess.run([cand, "--version"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5)
            return cand
        except Exception:
            continue
    return ""


def main():
    log("=" * 40)
    log("备用启动器运行")

    if alive(DEFAULT_PORT):
        log("8806 上已经有服务在跑，直接打开网页")
        open_page(DEFAULT_PORT)
        return 0

    py = find_python()
    log("python = %s" % py)
    if not py:
        log("没找到 Python")
        return 1

    os.makedirs(CACHE, exist_ok=True)
    if os.path.isfile(PORT_FILE):
        try:
            os.remove(PORT_FILE)
        except OSError:
            pass

    cmd = [py, os.path.join(APP, "server.py"), "--port", str(DEFAULT_PORT), "--no-open"]
    log("启动服务：%s" % " ".join(cmd))
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | \
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    try:
        subprocess.Popen(cmd, cwd=ROOT, creationflags=flags,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception as e:
        log("启动失败：%r" % e)
        return 1

    port = DEFAULT_PORT
    for _ in range(30):                       # 等端口文件（最多 15 秒）
        if os.path.isfile(PORT_FILE):
            try:
                txt = open(PORT_FILE, encoding="ascii").read().strip()
                if txt:
                    port = int(txt)
                    break
            except Exception:
                pass
        time.sleep(0.5)
    log("port.txt -> %d" % port)

    ok = False
    for _ in range(30):                       # 等健康检查（最多 15 秒）
        if alive(port):
            ok = True
            break
        time.sleep(0.5)
    log("健康检查 %s" % ("通过" if ok else "超时"))

    open_page(port)
    return 0 if ok else 1


def open_page(port):
    url = "http://127.0.0.1:%d/" % port
    log("打开 %s" % url)
    if os.environ.get("LAUNCHER_NO_OPEN") == "1":
        log("（测试模式：不真的开浏览器）")
        return
    try:
        webbrowser.open(url, new=1, autoraise=True)
    except Exception as e:
        log("打不开浏览器：%r" % e)


if __name__ == "__main__":
    sys.exit(main())
