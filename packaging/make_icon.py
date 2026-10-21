"""生成 Windows 图标（.ico）。

输入是 packaging/icon 下的两张 PNG：大尺寸用完整版（带内圈与数据点），
小尺寸用简化版（粗线、无内圈小点）。分开出图是因为 16px 下细节会糊成一团，
直接用一张大图缩放出不来清楚的图标。

用法：
    python packaging/make_icon.py

依赖 Pillow 与 Chrome，只在打包机上用；程序运行时仍然零第三方依赖。
Chrome 只负责把 SVG 渲染成 PNG，已经渲染好的 PNG 也留在同目录，重跑不会重新截图。
"""

from __future__ import annotations

import io
import os
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ICON_DIR = os.path.join(HERE, "icon")
BIG_PNG = os.path.join(ICON_DIR, "logo-1024.png")
SMALL_PNG = os.path.join(ICON_DIR, "logo-small-512.png")
TINY_PNG = os.path.join(ICON_DIR, "logo-tiny-512.png")
OUT_ICO = os.path.join(ICON_DIR, "app.ico")

# Windows 会按场景挑最合适的一档；20/24/40 是旧式 DPI 缩放会用到的
SIZES = (256, 128, 64, 48, 40, 32, 24, 20, 16)


def _find_chrome() -> str:
    for name in ("chrome.exe", "msedge.exe"):
        for base in (
            r"C:\Program Files\Google\Chrome\Application",
            r"C:\Program Files (x86)\Google\Chrome\Application",
            r"C:\Program Files (x86)\Microsoft\Edge\Application",
            r"C:\Program Files\Microsoft\Edge\Application",
        ):
            path = os.path.join(base, name)
            if os.path.isfile(path):
                return path
    return ""


def _render_svg(chrome: str, page: str, out_png: str, size: int) -> None:
    url = "file:///" + os.path.abspath(page).replace("\\", "/").replace(" ", "%20")
    cmd = [
        chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--default-background-color=00000000",
        "--window-size=%d,%d" % (size, size),
        "--screenshot=" + out_png,
        url,
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_pngs() -> None:
    """两张源图不在就现场渲染，方便换 logo 之后一条命令重来。"""
    chrome = _find_chrome()
    jobs = (
        ("render.html", BIG_PNG, 1024),
        ("render-small.html", SMALL_PNG, 512),
        ("render-tiny.html", TINY_PNG, 512),
    )
    for page, out, size in jobs:
        if os.path.isfile(out):
            continue
        if not chrome:
            raise SystemExit("缺少 %s，且没找到 Chrome/Edge 用来渲染 SVG" % os.path.basename(out))
        print("渲染 " + os.path.basename(out) + " ……")
        _render_svg(chrome, os.path.join(ICON_DIR, page), out, size)


def build_ico() -> None:
    from PIL import Image

    big = Image.open(BIG_PNG).convert("RGBA")
    small = Image.open(SMALL_PNG).convert("RGBA")
    tiny = Image.open(TINY_PNG).convert("RGBA")

    blobs: list[tuple[int, bytes]] = []
    for size in SIZES:
        # 三档源图：大尺寸保细节，中尺寸去掉内圈小点，24 以下只留最粗的折线
        src = big if size >= 64 else (small if size >= 32 else tiny)
        im = src.resize((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        blobs.append((size, buf.getvalue()))

    # ICO 结构：6 字节目录头 + 每档 16 字节目录项 + 各档 PNG 数据。
    # Vista 之后允许直接嵌 PNG，比 BMP 省空间，也能保住圆角的 alpha。
    out = bytearray(struct.pack("<HHH", 0, 1, len(blobs)))
    offset = 6 + 16 * len(blobs)
    for size, data in blobs:
        dim = 0 if size >= 256 else size
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _size, data in blobs:
        out += data

    with open(OUT_ICO, "wb") as f:
        f.write(out)
    print("完成：%s（%.1f KB，%d 档）" % (OUT_ICO, len(out) / 1024, len(blobs)))


def main() -> int:
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("需要 Pillow：pip install pillow（只在打包机上用）")
        return 2
    ensure_pngs()
    build_ico()
    return 0


if __name__ == "__main__":
    sys.exit(main())
