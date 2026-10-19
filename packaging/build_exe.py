"""构建单文件 exe（Windows x64）。

需要先装 PyInstaller：`pip install pyinstaller`。
用法：python packaging/build_exe.py [--out 输出目录] [--onefile|--onedir]
默认输出到项目下的 dist\\，构建中间文件在 build\\（两者都在 .gitignore 里）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NAME = "osumania_replay_analyser"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--work", default=os.path.join(ROOT, "build"))
    ap.add_argument("--onedir", action="store_true", help="目录形态（启动快，但必须整个文件夹一起发）")
    args = ap.parse_args()

    py = sys.executable
    try:
        subprocess.run([py, "-m", "PyInstaller", "--version"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        print("没装 PyInstaller：先执行  pip install pyinstaller")
        return 2

    sep = ";"  # Windows 的 --add-data 分隔符
    cmd = [
        py, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir" if args.onedir else "--onefile",
        "--noconsole",
        "--name", NAME,
        "--paths", ROOT,
        "--collect-submodules", "app",
        "--add-data", os.path.join(ROOT, "app", "web") + sep + "app/web",
        "--add-data", os.path.join(ROOT, "vendor", "minacalc") + sep + "vendor/minacalc",
        "--add-data", os.path.join(ROOT, "data", "dan_scale.json") + sep + "data",
        "--distpath", args.out,
        "--workpath", args.work,
        "--specpath", args.work,
    ]
    # MinaCalc 需要 VC 运行库；打包机上有、目标机不一定有，直接带上
    for dll in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        p = os.path.join(ROOT, "python", dll)
        if os.path.isfile(p):
            cmd += ["--add-binary", p + sep + "."]
        else:
            print("提醒：没找到 python\\%s，目标机缺少 VC 运行库时 MSD 会不可用" % dll)
    cmd.append(os.path.join(HERE, "exe_launcher.py"))

    print("构建中……（第一次会比较慢）")
    subprocess.run(cmd, check=True, cwd=ROOT)

    exe = os.path.join(args.out, NAME + (".exe" if not args.onedir else os.sep + NAME + ".exe"))
    if os.path.exists(exe):
        print("完成：%s（%.1f MB）" % (exe, os.path.getsize(exe) / 1024 / 1024))
    # 顺手清掉 spec（PyInstaller 每次都会重写，留着只会让目录变乱）
    spec = os.path.join(args.work, NAME + ".spec")
    if os.path.isfile(spec):
        os.remove(spec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
