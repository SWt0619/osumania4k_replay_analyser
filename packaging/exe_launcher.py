"""exe 版入口：双击起服务并开网页；把 .osr 拖到 exe 上则直接生成报告。

三种形态共用这一份代码：
  * 直接运行（无参数）—— 起本地服务，等健康检查通过后开浏览器；
  * 拖入 .osr —— 不开服务，分析完生成单文件报告并打开；
  * --stop / --selftest / --version —— 停止实例 / 打包自检 / 版本号。

打包后没有控制台，所有给用户看的错误都用系统消息框，诊断信息写 _cache\\launcher.log。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser

# 打包后资源在 sys._MEIPASS；源码运行时在项目根
BASE = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from app import paths  # noqa: E402
from app.version import __version__  # noqa: E402

DEFAULT_PORT = 8806
CREATE_NO_WINDOW = 0x08000000
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# --------------------------------------------------------------------- 小工具
def log(msg: str) -> None:
    try:
        os.makedirs(paths.CACHE_DIR, exist_ok=True)
        with open(os.path.join(paths.CACHE_DIR, "launcher.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def message(text: str, title: str = "osumania_replay_analyser", flags: int = 0x10) -> int:
    """没有控制台时的提示框。flags 默认 MB_ICONERROR。"""
    try:
        import ctypes

        return int(ctypes.windll.user32.MessageBoxW(None, text, title, flags))
    except Exception:  # noqa: BLE001
        return 0


def health(port: int, timeout: float = 1.0):
    try:
        with _OPENER.open("http://127.0.0.1:%d/api/health" % port, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None


def read_int(path: str) -> int:
    try:
        with open(path, encoding="ascii") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def running_port():
    """返回正在运行的本程序端口；没有就 None。先看端口文件，再看默认端口。"""
    cand = []
    p = read_int(paths.PORT_FILE)
    if p:
        cand.append(p)
    if DEFAULT_PORT not in cand:
        cand.append(DEFAULT_PORT)
    for p in cand:
        h = health(p)
        if h and h.get("ok"):
            return p
    return None


def stop_server() -> bool:
    pid = read_int(paths.PID_FILE)
    if not pid:
        return False
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], creationflags=CREATE_NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        for _ in range(20):
            if health(read_int(paths.PORT_FILE) or DEFAULT_PORT, 0.3) is None:
                return True
            time.sleep(0.15)
        return True
    except Exception:  # noqa: BLE001
        log("停止失败：\n" + traceback.format_exc())
        return False


# --------------------------------------------------------------------- 服务
def serve(open_browser: bool = True) -> None:
    port = running_port()
    if port:
        # 已经在跑：问一句要打开页面还是停掉，避免"再点一次没反应"
        r = message(
            "分析器已经在运行（127.0.0.1:%d）。\n\n"
            "「是」打开页面\n「否」停止服务\n「取消」什么都不做" % port,
            flags=0x03 | 0x20,  # MB_YESNOCANCEL | MB_ICONQUESTION
        )
        if r == 6:      # Yes
            webbrowser.open("http://127.0.0.1:%d/" % port)
        elif r == 7:    # No
            ok = stop_server()
            message("服务已停止。" if ok else "没能停止服务，请到任务管理器结束本程序。",
                    flags=0x40 if ok else 0x10)
        return

    from app import server

    log("启动服务，version=%s" % __version__)
    sys.argv = ["server", "--port", str(DEFAULT_PORT), "--no-open"]
    th = threading.Thread(target=server.main, name="mania-server", daemon=True)
    th.start()

    deadline = time.time() + 20
    port = 0
    while time.time() < deadline:
        if not th.is_alive():
            break
        port = read_int(paths.PORT_FILE)
        if port and health(port):
            break
        time.sleep(0.2)

    if not port or not health(port):
        tail = ""
        try:
            with open(paths.LOG_PATH, encoding="utf-8", errors="replace") as f:
                tail = "".join(f.readlines()[-8:])
        except OSError:
            pass
        log("服务启动失败，日志尾部：\n" + tail)
        message("服务没能启动。\n\n可以先看看 _cache\\server.log 和 _cache\\launcher.log。\n\n" + tail[-400:])
        return

    if open_browser:
        webbrowser.open("http://127.0.0.1:%d/" % port)
    log("服务就绪：%d" % port)
    # 主线程陪跑：进程退出时服务随之结束
    while th.is_alive():
        time.sleep(0.5)


# --------------------------------------------------------------------- 拖拽模式
def make_report(replay: str, beatmap: str | None = None, open_after: bool = True) -> tuple[bool, str]:
    from app.core.analyze import AnalyzeError, analyze
    from app.core.osr import OsrError
    from app.core.report_html import build_report_html, safe_report_name

    try:
        data = analyze(replay, beatmap_path=beatmap)
    except (AnalyzeError, OsrError) as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        log("分析异常：\n" + traceback.format_exc())
        return False, "分析时出现意外错误：%s" % e

    os.makedirs(paths.REPORT_DIR, exist_ok=True)
    base = os.path.join(paths.REPORT_DIR, "报告_%s_%s" % (safe_report_name(data), time.strftime("%Y%m%d_%H%M%S")))
    try:
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        with open(base + ".html", "w", encoding="utf-8") as f:
            f.write(build_report_html(data))
    except OSError as e:
        return False, "报告写不进 %s：%s" % (paths.REPORT_DIR, e)

    if open_after:
        try:
            os.startfile(base + ".html")  # noqa: S606 本地报告，直接打开
        except Exception:  # noqa: BLE001
            pass
    return True, base + ".html"


# --------------------------------------------------------------------- 自检 / 入口
def selftest(replay: str) -> int:
    """打包自检：跑一次完整分析，把结果写到 _cache\\selftest.json。"""
    out = os.path.join(paths.CACHE_DIR, "selftest.json")
    os.makedirs(paths.CACHE_DIR, exist_ok=True)
    info = {"version": __version__, "frozen": bool(getattr(sys, "frozen", False))}
    try:
        from app.core.analyze import analyze

        data = analyze(replay)
        info.update({
            "ok": True,
            "accuracy": data["summary"]["accuracy"],
            "grade": data["evaluation"]["grade"],
            "notes": len(data["notes"]),
            "msd": bool((data.get("msd") or {}).get("available")),
            "weights": paths.ROOT,
        })
        code = 0
    except Exception as e:  # noqa: BLE001
        info.update({"ok": False, "error": "%s: %s" % (type(e).__name__, e), "trace": traceback.format_exc()[-1200:]})
        code = 1
    with open(out, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    return code


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:]]
    no_open = "--no-open" in args
    args = [a for a in args if a != "--no-open"]

    if "--version" in args:
        return 0
    if "--stop" in args:
        return 0 if stop_server() else 1
    if args and args[0] == "--selftest" and len(args) > 1:
        return selftest(args[1])

    replay = next((a for a in args if a.lower().endswith(".osr")), None)
    if replay:
        beatmap = None
        if "--map" in args:
            i = args.index("--map")
            if i + 1 < len(args):
                beatmap = args[i + 1]
        ok, detail = make_report(replay, beatmap, open_after=not no_open)
        if not ok:
            message("这份回放没能分析：\n\n" + detail)
            return 1
        log("报告：%s" % detail)
        return 0

    serve(open_browser=not no_open)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
