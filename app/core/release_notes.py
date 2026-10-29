"""把 CHANGELOG.md 拆成关于页能直接画的结构。

为什么不另存一份 JSON：更新记录本来就只有一份（仓库根目录的 CHANGELOG.md，
发行包里也带着），再抄一份到前端迟早会漏改。解析放在服务端做，前端只负责排版。

结构：
    [{"version": "1.9.0",
      "note": "",                       # 标题后面的补充说明，比如「这个版本没有 release」
      "sections": [{"title": "新增功能", "items": ["…", "…"]}],
      "prose": ["…"]}]                 # 不属于任何条目的整段话（1.8.0 结尾那段）
"""

from __future__ import annotations

import os
import re

from app import paths

HEADING = re.compile(r"^##\s+(.+?)\s*$")
SECTION = re.compile(r"^###\s+(.+?)\s*$")
BULLET = re.compile(r"^[-*]\s+(.*)$")
VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)\s*(.*)$")


def changelog_path() -> str:
    return os.path.join(paths.ROOT, "CHANGELOG.md")


def parse(text: str) -> list[dict]:
    """按 markdown 的层级切分；只认 ## / ### / - 三种行，其余都当正文。"""
    versions: list[dict] = []
    cur: dict | None = None
    sec: dict | None = None
    bullet: str | None = None

    def flush() -> None:
        nonlocal bullet
        if bullet and cur is not None and sec is not None:
            sec["items"].append(bullet)
        bullet = None

    def ensure_section() -> dict | None:
        nonlocal sec
        if cur is None:
            return None
        if sec is None:
            sec = {"title": "", "items": []}
            cur["sections"].append(sec)
        return sec

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            # 空行只结束当前条目，段落要等下一行不是 - 开头才算正文
            flush()
            continue

        m = HEADING.match(line)
        if m:
            flush()
            title = m.group(1)
            vm = VERSION.match(title)
            if vm:
                cur = {"version": vm.group(1), "note": vm.group(2).strip(),
                       "sections": [], "prose": []}
            else:
                cur = {"version": title, "note": "", "sections": [], "prose": []}
            versions.append(cur)
            sec = None
            continue

        if line.startswith("### "):
            flush()
            sm = SECTION.match(line)
            if cur is None:
                continue
            sec = {"title": sm.group(1) if sm else line[4:].strip(), "items": []}
            cur["sections"].append(sec)
            continue

        if line.startswith("#"):
            continue                       # 一级标题与其它层级都不要

        bm = BULLET.match(line)
        if bm:
            flush()
            if cur is None:
                continue
            ensure_section()
            bullet = bm.group(1).strip()
            continue

        if bullet:
            # 条目后面紧跟的一行：markdown 里属于同一条（没缩进也能看出来）。
            # 两边都是英文单词才补空格，中文直接接上。
            gap = " " if (bullet[-1].isascii() and bullet[-1].isalnum()
                          and line[0].isascii() and line[0].isalnum()) else ""
            bullet += gap + line
            continue

        if cur is None:
            continue
        cur["prose"].append(line)

    flush()
    return versions


def load(path: str | None = None) -> list[dict]:
    """读不到就返回空列表——关于页据此把整块藏起来，不让空白卡占位。"""
    target = path or changelog_path()
    try:
        with open(target, "r", encoding="utf-8") as f:
            return parse(f.read())
    except OSError:
        return []
