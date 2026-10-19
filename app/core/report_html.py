"""把分析结果渲染成一个自包含的 HTML 报告（样式、脚本、数据全内嵌）。

exe 版的"把回放拖到 exe 上"用它；以后要加别的导出入口（命令行、脚本）也走这一份，
避免每个入口各拼一套 HTML，改一处样式要满仓库找。
"""

from __future__ import annotations

import html as html_mod
import json
import os
import re

from .. import paths


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _safe_js(code: str) -> str:
    """把 </script 转义掉：JS 注释/字符串里出现这串会把 <script> 提前闭合（踩过）。"""
    return code.replace("</script", "<\\/script").replace("</SCRIPT", "<\\/SCRIPT")


def _safe_css(code: str) -> str:
    return code.replace("</style", "<\\/style").replace("</STYLE", "<\\/STYLE")


def build_report_html(data: dict) -> str:
    """把 index.html 变成"没有服务也能看"的单文件报告。"""
    page = _read(os.path.join(paths.WEB_DIR, "index.html"))
    css = _read(os.path.join(paths.WEB_DIR, "css", "app.css"))
    charts = _read(os.path.join(paths.WEB_DIR, "js", "charts.js"))
    player = _read(os.path.join(paths.WEB_DIR, "js", "player.js"))
    ui = _read(os.path.join(paths.WEB_DIR, "js", "ui.js"))
    offline = _read(os.path.join(paths.WEB_DIR, "js", "offline.js"))

    # 样式内联
    page = re.sub(
        r'<link rel="stylesheet" href="[^"]*">',
        lambda m: "<style>\n" + _safe_css(css) + "\n</style>",
        page,
        count=1,
    )
    # 去掉所有外部脚本
    page = re.sub(r'<script src="[^"]*"></script>\s*', "", page)

    # 报告里不需要的东西：空状态、导入按钮、曲库状态条。
    # 只按元素起始标签做替换，避免动到别处的同名文字。
    page = page.replace('<div class="empty glass" id="emptyState">', '<div class="empty glass" id="emptyState" hidden>')
    page = page.replace('<button class="icon-btn" id="btnImport"', '<button class="icon-btn" id="btnImport" hidden')
    page = page.replace('<div class="chip" id="libChip"', '<div class="chip" id="libChip" hidden')

    payload = (
        json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    scripts = (
        "<script>window.REPORT_DATA = " + payload + ";</script>\n"
        "<script>\n" + _safe_js(charts) + "\n</script>\n"
        "<script>\n" + _safe_js(player) + "\n</script>\n"
        "<script>\n" + _safe_js(ui) + "\n</script>\n"
        "<script>\n" + _safe_js(offline) + "\n</script>\n"
    )
    page = page.replace("</body>", scripts + "</body>")

    title = "%s - %s [%s] · 回放分析" % (
        data["beatmap"]["artist"],
        data["beatmap"]["title"],
        data["beatmap"]["difficulty"],
    )
    page = re.sub(r"<title>.*?</title>", "<title>" + html_mod.escape(title) + "</title>", page, count=1)
    return page


def safe_report_name(data: dict) -> str:
    """报告文件名里的曲名部分：去掉 Windows 不允许的字符，并限长。"""
    name = "%s - %s" % (data["beatmap"]["artist"], data["beatmap"]["title"])
    return re.sub(r'[\\/:*?"<>|]', "_", name)[:60]
