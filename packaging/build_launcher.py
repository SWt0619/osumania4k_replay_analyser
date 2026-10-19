"""把启动器与停止器编译成两个无窗口的小 exe。

用 Windows 自带的 .NET Framework 编译器（csc.exe）直接编译：不用装 Visual Studio、
不用联网，产物只有十几 KB。源码在 packaging/launcher/ 下，改完重跑本脚本即可。

用法：
    python packaging/build_launcher.py            # 编译到项目根目录
    python packaging/build_launcher.py --check    # 只检查能不能编译，不动文件
    python packaging/build_launcher.py --out DIR  # 换个输出目录
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC_DIR = os.path.join(HERE, "launcher")

START_NAME = "启动分析器.exe"
STOP_NAME = "停止分析器.exe"

# 只用到 .NET Framework 里一直存在的两个程序集；System.dll 默认也带，显式写出来更稳
REFS_COMMON = ["System.dll"]
REFS_STOP = ["System.dll", "System.Management.dll"]


def find_csc() -> str:
    """找系统自带的 C# 编译器（.NET Framework 4.x 一直带着）。"""
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for framework in ("Framework64", "Framework"):
        path = os.path.join(windir, "Microsoft.NET", framework, "v4.0.30319", "csc.exe")
        if os.path.isfile(path):
            return path
    return ""


def compile_one(csc: str, source: str, target: str, refs: list[str]) -> None:
    """调用 csc 编译一个源码文件。target 是 winexe，双击不会有控制台窗口。"""
    lib_dir = os.path.dirname(csc)
    cmd = [
        csc,
        "/nologo",
        "/target:winexe",
        "/optimize+",
        "/codepage:65001",      # 源码是 UTF-8，注释里有中文
        "/out:" + target,
        "/lib:" + lib_dir,
    ]
    for ref in refs:
        cmd.append("/r:" + ref)
    cmd.append(os.path.join(SRC_DIR, source))

    print("  编译 " + os.path.basename(target) + " ……")
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.isfile(target):
        print("  [失败] " + (result.stdout or "") + (result.stderr or ""))
        raise SystemExit(3)
    size = os.path.getsize(target) / 1024
    print("  完成：%s（%.1f KB）" % (os.path.basename(target), size))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=ROOT, help="输出目录，默认项目根目录")
    ap.add_argument("--check", action="store_true", help="只检查编译环境")
    args = ap.parse_args()

    csc = find_csc()
    if not csc:
        print("没找到 .NET Framework 自带的 C# 编译器（csc.exe）。")
        print("它本来随 Windows 一起装好，位置是：")
        print(r"  %WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
        print("如果确实没有，可以安装「.NET Framework 4.x 运行时/开发者包」后再试。")
        return 2

    print("编译器：" + csc)
    if args.check:
        print("环境可用。")
        return 0

    for name in ("Launcher.cs", "Stopper.cs"):
        if not os.path.isfile(os.path.join(SRC_DIR, name)):
            print("缺少源码：" + name)
            return 2

    os.makedirs(args.out, exist_ok=True)
    start_exe = os.path.join(args.out, START_NAME)
    stop_exe = os.path.join(args.out, STOP_NAME)

    print("开始编译启动器：")
    compile_one(csc, "Launcher.cs", start_exe, REFS_COMMON)
    compile_one(csc, "Stopper.cs", stop_exe, REFS_STOP)

    # 自检：只看路径和运行时在不在，不起服务、不弹窗
    print("自检：")
    rc = subprocess.call([start_exe, "--selftest"], cwd=ROOT, timeout=120)
    if rc == 0:
        print("  [OK] 启动器自检通过（找得到 python 与 app\\server.py）")
    elif rc == 2:
        print("  [提示] 启动器能跑，但没找到 python\\python.exe 或 app\\server.py")
        print("         完整程序包里应该有这两样，缺了的话重新解压一份")
    else:
        print("  [提示] 自检返回码 %s，详情见 _tools\\_launcher.log" % rc)

    print("\n完成。双击「%s」启动，「%s」停止服务。" % (START_NAME, STOP_NAME))
    return 0


if __name__ == "__main__":
    sys.exit(main())
