"""每局摘要的本地存档。

这一版只负责"把数据存下来"，不做趋势、不做对比、不在页面上展示。
用标准库 sqlite3，数据库放在 `_cache/history.db`，和曲库索引、日志同目录。

为什么要单独存一份：分析结果本身只在页面会话里活着，关掉标签页就没了。
以后要做"同一张图的成绩曲线""最近哪类失误在减少"，得先有按局落盘的原始记录。
表结构按"一次游玩一行"设计，字段只留摘要级（准确率、UR、判定数、键型构成），
不存每个音符，避免数据库跟着回放数量一起膨胀。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime

from .. import paths

DB_NAME = "history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT NOT NULL UNIQUE,   -- 回放内容哈希：同一份回放只记一次
    played_at     TEXT NOT NULL,          -- 回放头里记的游玩时间（没有就用写入时间）
    recorded_at   TEXT NOT NULL,          -- 本机写入时间
    beatmap_md5   TEXT,
    beatmap_id    INTEGER,
    beatmap_set_id INTEGER,
    artist        TEXT,
    title         TEXT,
    difficulty    TEXT,
    dan           TEXT,                   -- 段位由前端算，写入时通常为空，后续可补
    msd           REAL,
    rate          REAL,
    mods          TEXT,
    score         INTEGER,
    accuracy      REAL,
    ur            REAL,
    max_combo     INTEGER,
    counts        TEXT,                   -- JSON: perfect/great/good/ok/meh/miss
    categories    TEXT,                   -- JSON: 失误归因汇总
    patterns      TEXT,                   -- JSON: 五类键型构成
    app_version   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_played ON sessions(played_at);
CREATE INDEX IF NOT EXISTS idx_sessions_map ON sessions(beatmap_md5);
"""


def db_path() -> str:
    return os.path.join(paths.CACHE_DIR, DB_NAME)


def _connect(path: str | None = None) -> sqlite3.Connection:
    target = path or db_path()
    os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
    conn = sqlite3.connect(target, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def _db(path: str | None = None):
    """用完即关。sqlite3 的 with 只管事务、不管关连接，长时间跑的服务会攒句柄。"""
    conn = _connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _json(value) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def _played_at(payload: dict) -> str:
    raw = str((payload.get("replay") or {}).get("date") or "").strip()
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M").isoformat(timespec="seconds")
        except ValueError:
            pass
    return datetime.now().isoformat(timespec="seconds")


def _msd_value(payload: dict) -> float | None:
    msd = payload.get("msd")
    if isinstance(msd, dict):
        msd = msd.get("overall") or msd.get("value") or msd.get("msd")
    try:
        return round(float(msd), 2)
    except (TypeError, ValueError):
        return None


def record(payload: dict, path: str | None = None) -> bool:
    """写入一局摘要。重复回放、字段缺失、数据库不可写都返回 False，绝不抛给调用方。"""
    if not isinstance(payload, dict) or not payload.get("ok"):
        return False
    replay = payload.get("replay") or {}
    beatmap = payload.get("beatmap") or {}
    summary = payload.get("summary") or {}

    key = str(replay.get("replayMd5") or replay.get("fileHash") or "").strip()
    if not key:
        return False

    row = (
        key,
        _played_at(payload),
        datetime.now().isoformat(timespec="seconds"),
        replay.get("beatmapMd5") or None,
        beatmap.get("beatmapId") or None,
        beatmap.get("beatmapSetId") or None,
        beatmap.get("artist") or "",
        beatmap.get("title") or "",
        beatmap.get("difficulty") or "",
        None,
        _msd_value(payload),
        float(replay.get("rate") or 1.0),
        str(replay.get("mods") or ""),
        int(replay.get("score") or 0),
        float(summary.get("accuracy") or 0.0),
        float(summary.get("ur") or 0.0),
        int(summary.get("maxCombo") or 0),
        _json(summary.get("counts")),
        _json(payload.get("categories")),
        _json(payload.get("patternShare")),
        str(payload.get("version") or ""),
    )

    try:
        with _db(path) as conn:
            conn.execute(
                """INSERT INTO sessions
                   (key, played_at, recorded_at, beatmap_md5, beatmap_id, beatmap_set_id,
                    artist, title, difficulty, dan, msd, rate, mods, score, accuracy, ur,
                    max_combo, counts, categories, patterns, app_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
        return True
    except sqlite3.IntegrityError:
        # 同一份回放再次导入：保留第一次那行，不覆盖
        return False
    except sqlite3.Error:
        return False


def update_dan(key: str, dan: str, path: str | None = None) -> bool:
    """补写段位。段位要前端用 Sunny 算，写入时还没有，页面算完之后可以回填。"""
    if not key or not dan:
        return False
    try:
        with _db(path) as conn:
            cur = conn.execute("UPDATE sessions SET dan = ? WHERE key = ?", (str(dan), str(key)))
        return cur.rowcount > 0
    except sqlite3.Error:
        return False


def _row_to_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    for name in ("counts", "categories", "patterns"):
        raw = out.get(name)
        if raw:
            try:
                out[name] = json.loads(raw)
            except ValueError:
                out[name] = None
    return out


def recent(limit: int = 50, path: str | None = None) -> list[dict]:
    """最近几局，按游玩时间倒序。只给后续功能用，当前没有页面入口。"""
    try:
        with _db(path) as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY played_at DESC, id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except sqlite3.Error:
        return []


def get(key: str, path: str | None = None) -> dict | None:
    try:
        with _db(path) as conn:
            row = conn.execute("SELECT * FROM sessions WHERE key = ?", (str(key),)).fetchone()
        return _row_to_dict(row) if row else None
    except sqlite3.Error:
        return None


def stats(path: str | None = None) -> dict:
    try:
        with _db(path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MIN(played_at) AS first, MAX(played_at) AS last FROM sessions"
            ).fetchone()
        return {"sessions": row["n"], "first": row["first"], "last": row["last"]}
    except sqlite3.Error:
        return {"sessions": 0, "first": None, "last": None}


def record_safely(payload: dict, path: str | None = None) -> bool:
    """给服务端用的薄壳：任何异常都吞掉。

    历史记录是附加功能，写失败不能让一次分析整体失败——这是"存数据"这一层
    唯一需要守住的边界。
    """
    try:
        return record(payload, path)
    except Exception:  # noqa: BLE001
        return False
