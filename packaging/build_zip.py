"""打便携版 zip：脚本 + 内置 Python + 资源，解压即用。

用法：python packaging/build_zip.py [--out 输出目录]
默认输出到项目下的 dist\\。会跳过缓存、报告产物、__pycache__ 和 dist/build 本身。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NAME = "osumania_replay_analyser"

sys.path.insert(0, HERE)
try:
    from build_launcher import SRC_DIR, START_NAME, STOP_NAME
except Exception:  # noqa: BLE001
    SRC_DIR = os.path.join(HERE, "launcher")
    START_NAME, STOP_NAME = "启动分析器.exe", "停止分析器.exe"
SKIP_DIRS = {"__pycache__", "dist", "build", ".git", ".venv", "node_modules", "_cache",
             "_tools", "_tmp", "_dataset",
             # 测试、构建脚本、内部调研都不该出现在发行包里
             "tests", "packaging", "docs",
             # legacy 里是早年的 pyw 备用入口，运行用不到，别塞进发行包
             "legacy",
             # 崩溃报告是本机排障产物，不该跟着包发出去
             "crashreport"}
# 回放、谱面、日志、本地配置一律不进压缩包：这些只可能是跑程序时产生的本机数据
SKIP_EXT = {".pyc", ".pyo", ".osr", ".osu", ".osz", ".osb", ".osk", ".log", ".env"}
SKIP_FILES = {"thumbs.db", "desktop.ini", "推送公开仓库.bat", "config.json", ".env",
              ".gitignore", "ROADMAP.md"}
# 仓库根目录下这些前缀的 md 是给自己看的（审查报告、待修清单、检验记录），不进发行包
SKIP_DOC_PREFIX = ("待修清单", "代码质量报告", "独立验收报告", "键型判定检验")


def version() -> str:
    sys.path.insert(0, ROOT)
    from app.version import __version__

    return __version__


def should_skip(path: str) -> bool:
    name = os.path.basename(path)
    if name.lower() in SKIP_FILES:
        return True
    # 内部工作笔记（待修清单 / 代码审查 / 检验报告）不进发行包：它们会过期，用户看了只会困惑
    if name.startswith(SKIP_DOC_PREFIX):
        return True
    if os.path.splitext(name)[1].lower() in SKIP_EXT:
        return True
    return False


def launcher_stale() -> str:
    """两个小启动器是不是落后于源码了？返回原因，空前缀表示没问题。

    这一关是为了别把「改了停止器却忘了重编译」的包发出去 —— 那种包能启动、
    却停不下来服务，而且从文件名和版本号上完全看不出来。
    """
    for src, exe in (("Launcher.cs", START_NAME), ("Stopper.cs", STOP_NAME)):
        src_path = os.path.join(SRC_DIR, src)
        exe_path = os.path.join(ROOT, exe)
        if not os.path.isfile(exe_path):
            return "%s 还没编译出来" % exe
        if os.path.isfile(src_path) and os.path.getmtime(exe_path) < os.path.getmtime(src_path):
            return "%s 比 %s 旧（改完没重新编译）" % (exe, src)
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--no-launcher", action="store_true", help="跳过启动器编译（exe 已经编好时用）")
    ap.add_argument("--allow-stale-launcher", action="store_true",
                    help="即使启动器比源码旧也照打（只在明确知道后果时用）")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    # 先编译两个小启动器（启动分析器.exe / 停止分析器.exe），再打包
    launcher_script = os.path.join(HERE, "build_launcher.py")
    if os.path.isfile(launcher_script) and not args.no_launcher:
        print("编译启动器……")
        built = subprocess.run([sys.executable, launcher_script], cwd=ROOT)
        if built.returncode != 0:
            print("提醒：启动器没编译成功（返回码 %s）。"
                  % built.returncode)
            print("     先确认系统里有 .NET Framework 自带的 csc.exe（在 %WINDIR% 下的 Microsoft.NET 目录里）。")
        print("")

    stale = launcher_stale()
    if stale and not args.allow_stale_launcher:
        print("[中止] " + stale)
        print("       包里的 exe 会和源码对不上（比如改了停止器却没重编译，那个包能启动却停不下来）。")
        print("       先跑： python packaging\\build_launcher.py  然后再打包。")
        print("       确实要照打：加 --allow-stale-launcher")
        return 3

    if not os.path.isfile(os.path.join(ROOT, "python", "python.exe")):
        print("提醒：python\\python.exe 不存在，打出来的 zip 将不带内置运行时。")
        print("     恢复方式见 .gitignore 里的注释（embeddable package + 三个 VC DLL）。")

    target = os.path.join(args.out, "%s-%s-win64.zip" % (NAME, version()))
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            rel_dir = os.path.relpath(dirpath, ROOT)
            if rel_dir.startswith(("dist", "build")):
                continue
            # 报告目录保留空目录，其余运行产物不带
            if rel_dir == "_replays":
                for fn in filenames:
                    if fn == "把回放放这里.txt":
                        continue  # 下面统一写一份，避免 zip 里出现重复条目
                    if not fn.lower().endswith(".osr") and not should_skip(os.path.join(dirpath, fn)):
                        z.write(os.path.join(dirpath, fn), os.path.join(NAME, rel_dir, fn))
                        count += 1
                continue
            if rel_dir.startswith(os.path.join("报告")):
                continue
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                if should_skip(full):
                    continue
                arc = os.path.join(NAME, rel_dir, fn) if rel_dir != "." else os.path.join(NAME, fn)
                z.write(full, arc)
                count += 1
        # 空目录也留一个占位，免得解压后找不到该把回放/报告放哪儿
        z.writestr(os.path.join(NAME, "报告", "把生成的报告放这里.txt"), "生成的报告会放在这个目录。\n")
        z.writestr(os.path.join(NAME, "_replays", "把回放放这里.txt"), "把 .osr 放到这里，页面里可以直接点文件名分析。\n")

    print("完成：%s（%d 个文件，%.1f MB）" % (target, count, os.path.getsize(target) / 1024 / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
