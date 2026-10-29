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
from . import config_store, dan, patterns

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

# 后加的字段：老库直接 ALTER 补上，不重建表 —— 重建会丢掉所有历史。
# 版本升级只改代码、不动 `_cache/history.db`，所以这一步在每次连接时都跑一遍（很快）。
MIGRATIONS = (
    ("dan_value", "ALTER TABLE sessions ADD COLUMN dan_value REAL"),
    ("skill", "ALTER TABLE sessions ADD COLUMN skill TEXT"),
    ("pattern_label", "ALTER TABLE sessions ADD COLUMN pattern_label TEXT"),
    # 谁打的这一局：name 存回放头里的玩家名，is_me 是"是不是本人"（1/0，NULL 表示认不出来）
    ("player", "ALTER TABLE sessions ADD COLUMN player TEXT"),
    ("is_me", "ALTER TABLE sessions ADD COLUMN is_me INTEGER"),
    # 谱面大类：rc / ln / hybrid。成长评估只吃 rc，LN 与 Hybrid 的准确率口径不同
    ("map_class", "ALTER TABLE sessions ADD COLUMN map_class TEXT"),
)


def class_from_patterns(share) -> str:
    """没有现成分类时，按键型构成推一个。

    阈值和 patterns.dominant_class 一致：LN 占 70% 以上是 LN 图，
    30%~70% 是 Hybrid，其余按 RC 处理。没有构成数据就当作 RC。
    """
    try:
        total = sum(float(x.get("share") or 0) for x in (share or []))
        ln = sum(float(x.get("share") or 0) for x in (share or []) if x.get("key") == "ln")
    except (TypeError, ValueError):
        return "rc"
    if total <= 0:
        return "rc"
    part = ln / total
    if part >= 0.70:
        return "ln"
    if part >= 0.30:
        return "hybrid"
    return "rc"


def configured_player(cache_dir: str | None = None) -> str:
    """用户在设置里填的 osu! 名字（或 ID）。没填时返回空串。"""
    raw = config_store.read(cache_dir).get("playerName")
    return str(raw or "").strip()


def _is_me(player: str, owner: str) -> int | None:
    """1 = 本人，0 = 别人，None = 回放里没写名字、判断不了。"""
    if not player or not owner:
        return None
    return 1 if player.strip().casefold() == owner.strip().casefold() else 0



# 段位刻度的版本号（存在 sqlite 的 user_version 里）。
# 1 = 旧刻度：10 段以上 0.5 一跳，Alpha 记作 10.5；
# 2 = 新刻度：10 段以上 1 段一跳，Alpha 就是 11 段。
DAN_SCALE_VERSION = 2


# 谱面大类的修复版本。老记录里 map_class 有两种坏值：
#   cut / stack / spam / tech —— 那是"细类"，评估只认 rc，于是正经 RC 图被当成
#                                LN/Hybrid 排除了；
#   1.5.x 写下的 rc        —— 那时还没有 hybrid 判定，40% 长条的图也是 rc，反过来混进评估。
# 4 = 已按存下来的键型构成 + 谱面文件里的长条比例重算过大类
#     （这一版把长条音符门槛从 35% 降到 30%，30% 的图要跟着改成 hybrid）。
MAP_CLASS_VERSION = 4


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    for name, sql in MIGRATIONS:
        if name not in cols:
            conn.execute(sql)
    _migrate_dan_scale(conn)
    _migrate_map_class(conn)
    _migrate_skill_spread(conn)


def _migrate_map_class(conn: sqlite3.Connection) -> int:
    """把历史记录的谱面大类重算成 rc / ln / hybrid，只做一次。

    能重算的原因是这两样都存着：键型构成在 `patterns` 里，
    谱面文件还能按 `beatmap_md5` 去曲库找回（长条比例需要它）。
    找不回谱面时退回"只按键型构成判断"，虽然会低估长条（块里稀疏），
    但也比沿用错的分类好。
    """
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error:
        return 0
    if version >= MAP_CLASS_VERSION:
        return 0

    lib = None
    try:
        from .library import get_library

        lib = get_library()
    except Exception:  # noqa: BLE001 - 曲库不可用也要把版本号推进去
        lib = None

    fixed = 0
    for row in conn.execute("SELECT id, beatmap_md5, patterns, map_class FROM sessions"):
        try:
            share = json.loads(row["patterns"] or "[]")
        except (TypeError, ValueError):
            share = []
        group = class_from_patterns(share)
        if lib is not None and row["beatmap_md5"]:
            entry = lib.find(row["beatmap_md5"])
            if entry is not None:
                try:
                    from .beatmap import parse_osu_file

                    bm = parse_osu_file(entry.path)
                    notes = bm.note_count()
                    if notes:
                        holds = sum(1 for o in bm.objects if o.is_hold)
                        weights = {str(x.get("key")): float(x.get("share") or 0) / 100.0
                                   for x in share if isinstance(x, dict)}
                        group = patterns.class_group(weights, holds / notes)
                except Exception:  # noqa: BLE001 - 单张解析失败就用构成推的值
                    pass
        if group == "rc":
            if str(row["map_class"] or "") != group:
                conn.execute("UPDATE sessions SET map_class = ? WHERE id = ?", (group, row["id"]))
                fixed += 1
        else:
            # LN/Hybrid 不归到 RC 练法，旧的"技术/速度"标签要一起清掉。
            # 分类没变但标签还留着的也要清（之前只改 map_class，漏了 skill）。
            conn.execute(
                "UPDATE sessions SET map_class = ?, skill = NULL, "
                "pattern_label = 'LN / Hybrid 图，不参与 RC 练法评估' WHERE id = ?",
                (group, row["id"]),
            )
            fixed += 1
    conn.execute("PRAGMA user_version = %d" % MAP_CLASS_VERSION)
    return fixed


def _migrate_skill_spread(conn: sqlite3.Connection) -> int:
    """把"没有主导键型、却被归了练法"的历史记录清掉，只做一次。

    能就地重算是因为键型构成早就存在 `patterns` 列里，不用找回谱面。
    旧规则在最高键型不到 55% 时直接拿 MSD 的最高项兜底，段位谱这种
    六成键型都是别的、只占三成叠的图就被塞进了"叠"的 b10。
    """
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error:
        return 0
    if version >= SKILL_SPREAD_VERSION:
        return 0

    fixed = 0
    for row in conn.execute("SELECT id, patterns, skill FROM sessions").fetchall():
        try:
            share = [x for x in json.loads(row["patterns"] or "[]") if isinstance(x, dict)]
        except (TypeError, ValueError):
            continue
        total = sum(float(x.get("share") or 0) for x in share)
        if total <= 0:
            continue
        top = max(share, key=lambda x: float(x.get("share") or 0))
        part = float(top.get("share") or 0) / total
        if part < SKILL_SPREAD_MIN and row["skill"]:
            conn.execute(
                "UPDATE sessions SET skill = NULL, pattern_label = ? WHERE id = ?",
                ("键型分散（最高 %s %.0f%%），不参与单键型评估"
                 % (top.get("label") or top.get("key"), part * 100), row["id"]),
            )
            fixed += 1
    conn.execute("PRAGMA user_version = %d" % SKILL_SPREAD_VERSION)
    return fixed


def _migrate_dan_scale(conn: sqlite3.Connection) -> int:
    """把旧刻度的 dan_value 换算到新刻度，只做一次。

    老库里的 10.5 是 Alpha、12 是 Delta；新刻度下 Alpha 是 11、Delta 是 14。
    两者在 10 段以下是同一个值，10 段以上是 new = 10 + (old - 10) * 2。
    迁移过就写 user_version，重复打开不会再算一遍。
    """
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error:
        return 0
    if version >= DAN_SCALE_VERSION:
        return 0
    fixed = conn.execute(
        "UPDATE sessions SET dan_value = 10 + (dan_value - 10) * 2 WHERE dan_value > 10"
    ).rowcount
    conn.execute("PRAGMA user_version = %d" % DAN_SCALE_VERSION)
    return fixed or 0


# 四类练法：叠、技术、速度、耐力。键型构成只分得出前三类，
# 耐力要看 MSD 的技能雷达，两处合起来判断。
# 键型构成到练法的映射。cut 这一类在 patterns.py 里的标签就是 Stamina
# （长连打＝耐力），映射成 speed 是错的：Signal of Abydos 的 Stamina 占 67.6%，
# 之前被判成了速度。spam 才是 Speed。
SKILL_KEYS = {"stack": "jack", "tech": "tech", "cut": "stamina", "spam": "speed"}
# 键名必须和 msd.calc() 返回的 skillsets 一致（全小写）。
# 之前这里写的是 C 结构体的驼峰名（Stamina/Stream…），一个都匹配不上，
# 结果所有图都只能靠键型 55% 判定，判不出来就空着。
MSD_SKILL_KEYS = {
    "jackspeed": "jack", "chordjack": "jack",
    "technical": "tech",
    "stream": "speed", "jumpstream": "speed", "handstream": "speed",
    "stamina": "stamina",
}

# 最高键型连这个比例都不到，就认为这张图没有主导键型（段位谱、综合考核图都是这样）：
# 只进综合 b50，不参与单键型 b10。专项图普遍在 55% 以上，段位谱实测 30~40%，中间是空的。
SKILL_SPREAD_MIN = 0.45
# 历史记录里已经有按旧规则写死的 skill，需要重算一次；用 user_version 保证只跑一遍。
SKILL_SPREAD_VERSION = 5


def classify_skill(payload: dict) -> tuple[str | None, str]:
    """这张图算哪一类练法；返回 (skill, 依据文字)。

    先看键型构成：某一类占到 55% 以上才算"归属明显"，这样纯切、纯叠、纯乱能被认出来。
    键型拉不开差距时看 MSD 的技能雷达，取最高的一项。

    但有一个下限：最高的一类连 45% 都不到，说明这张图根本没有主导键型 ——
    段位谱就是典型（Extra 3/4/5 的 Jack 只占 32% / 40% / 30%，其余六成是别的），
    拿它去评"叠"会把这一类的 b10 搅浑。这种图只进综合 b50，不参与单键型评估。

    阈值 45% 是照着实测分布定的：专项图普遍在 56% 以上（Alpha 系列最低 56%，
    Signal of Abydos 67.6%），而段位谱最高只到 39.7%，中间是空的。
    """
    share = payload.get("patternShare") or []
    total = sum(float(x.get("share") or 0) for x in share)
    if total > 0:
        top = max(share, key=lambda x: float(x.get("share") or 0))
        part = float(top.get("share") or 0) / total
        if part >= 0.55:
            skill = SKILL_KEYS.get(str(top.get("key")))
            if skill:
                return skill, "%s 占 %.0f%%" % (top.get("label") or top.get("key"), part * 100)
        if part < SKILL_SPREAD_MIN:
            return None, "键型分散（最高 %s %.0f%%），不参与单键型评估" % (
                top.get("label") or top.get("key"), part * 100)

    msd = payload.get("msd") or {}
    skills = msd.get("skillsets") or {}
    pairs = [(float(v), MSD_SKILL_KEYS[k]) for k, v in skills.items() if k in MSD_SKILL_KEYS]
    pairs.sort(reverse=True)
    if pairs:
        top_v, top_k = pairs[0]
        # 直接取最高的一项。原来要求"领先第二名 8%"才算数，可 stamina 和
        # jumpstream/handstream 本来就只有零点几的差距，这个门槛等于把所有
        # 耐力型谱面都判成了"看不出来"。宁可归到最该练的那一类，也别留空。
        return top_k, "MSD 技能 %.2f 最高" % top_v
    return None, ""


def db_path() -> str:
    return os.path.join(paths.CACHE_DIR, DB_NAME)


def _connect(path: str | None = None) -> sqlite3.Connection:
    target = path or db_path()
    os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
    conn = sqlite3.connect(target, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
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

    # 段位与练法在写入时就算好：段位只跟谱面有关，练法只跟键型构成有关，
    # 两者都不依赖页面，早点落盘以后就不用为了补字段重扫历史。
    msd_value = _msd_value(payload)
    dan_value = dan.estimate(msd_value).get("dan") if msd_value else None
    player = str(replay.get("player") or "").strip()
    owner = configured_player()
    # 存大类（rc / ln / hybrid），不是细类：细类里的 stack/spam/tech 也是 RC，
    # 评估只认 "rc"，以前把这些存进 map_class 等于把正经 RC 图排除在外。
    map_class = str(((payload.get("mapClass") or {}).get("group") or "")).strip().lower()
    if map_class not in ("rc", "ln", "hybrid"):
        map_class = class_from_patterns(payload.get("patternShare"))
    skill, skill_basis = classify_skill(payload)
    if map_class != "rc":
        # 长条的准确率口径和 RC 不一样，硬说"这是张技术图"只会误导 —— 不如不给结论
        skill, skill_basis = None, "LN / Hybrid 图，不参与 RC 练法评估"

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
        msd_value,
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
        float(dan_value) if dan_value is not None else None,
        skill,
        skill_basis,
        player or None,
        _is_me(player, owner),
        map_class,
    )

    try:
        with _db(path) as conn:
            conn.execute(
                """INSERT INTO sessions
                   (key, played_at, recorded_at, beatmap_md5, beatmap_id, beatmap_set_id,
                    artist, title, difficulty, dan, msd, rate, mods, score, accuracy, ur,
                    max_combo, counts, categories, patterns, app_version,
                    dan_value, skill, pattern_label, player, is_me, map_class)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
        return True
    except sqlite3.IntegrityError:
        # 同一份回放再次导入：保留第一次那行，但把"分析结论"更新成这次算的。
        # 分类规则改过好几轮（hybrid 是 1.6 才有的，map_class 以前还存过 stack/spam
        # 这类细类），不刷新的话老行会一直沿用旧结论：hybrid 图被当成 RC 混进评估。
        # 段位（dan_value）不在这里动 —— 那个由页面用 Roxy 回填，覆盖会把结果打回 MSD 口径。
        try:
            with _db(path) as conn:
                conn.execute(
                    "UPDATE sessions SET map_class = ?, skill = ?, pattern_label = ?, "
                    "patterns = COALESCE(?, patterns), app_version = ? WHERE key = ?",
                    (map_class, skill, skill_basis, _json(payload.get("patternShare")),
                     str(payload.get("version") or ""), key),
                )
                if player:
                    # 老版本没存玩家名，重新导入是最省事的补法
                    conn.execute(
                        "UPDATE sessions SET player = ?, is_me = ? WHERE key = ? "
                        "AND (player IS NULL OR TRIM(player) = '')",
                        (player, _is_me(player, owner), key),
                    )
        except sqlite3.Error:
            pass
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


def update_dan_value(key: str, value: float, path: str | None = None) -> bool:
    """把估算器算出的段位写回这一局。

    写入时用的是 MSD 标定值；谱面难度卡片用的是 Sunny → Azusa → Roxy 那一套，
    两者会差一到两段。成长页折合要以卡片上显示的数为准，所以页面算完要回填。
    """
    if not key:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if v <= 0:
        return False
    try:
        with _db(path) as conn:
            cur = conn.execute("UPDATE sessions SET dan_value = ? WHERE key = ?", (v, key))
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


def same_beatmap(md5: str, rate: float | None = None, limit: int = 10,
                 path: str | None = None, only_me: bool = True,
                 owner: str | None = None,
                 exclude_key: str | None = None) -> list[dict]:
    """同一张谱面的历史对局，按游玩时间正序（最老的在前），给报告的走势图用。

    只取同一倍率的记录：1.0× 和 1.2× 的准确率放到一条线上比没有意义，
    UR 更没法比。倍率传 None 时不过滤（调用方自己保证口径一致）。

    也只取**自己的**记录：别人发来的回放同样会入库，混进来就成了「上次 100%，
    这次 97%」这种根本不是你打的对照。判定按 player 列比名字（.osr 里只有名字，
    没有账号 id）；player 为空的行是回放里没写名字的旧记录，判断不了就留着，
    真正要挡的是明确写了别人名字的那些。owner 没给时取设置里的玩家名，
    两者都没有就不过滤——没名字时任何过滤都只是瞎猜。
    """
    key = (md5 or "").strip().lower()
    if not key:
        return []
    me = (configured_player() if owner is None else owner) or ""
    me = me.strip()
    sql = ("SELECT id, played_at, accuracy, ur, max_combo, counts, rate, score, "
           "pattern_label, categories FROM sessions WHERE lower(beatmap_md5) = ?")
    args: list = [key]
    if rate is not None:
        sql += " AND abs(COALESCE(rate, 1.0) - ?) < 0.001"
        args.append(float(rate))
    if only_me and me:
        sql += " AND (player IS NULL OR TRIM(player) = '' OR LOWER(TRIM(player)) = LOWER(?))"
        args.append(me)
    if exclude_key:
        # 同一份回放被再次导入时，别把自己算成「上一次」——走势图会多出一个
        # 一模一样（或者因当前口径不同而数字不同）的点。
        sql += " AND key <> ?"
        args.append(str(exclude_key).strip())
    sql += " ORDER BY played_at DESC, id DESC LIMIT ?"
    args.append(max(1, int(limit)))
    try:
        with _db(path) as conn:
            rows = conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for row in reversed(rows):          # 反转让最老的排前面，折线图左→右是时间顺序
        d = _row_to_dict(row)
        out.append({
            "at": d.get("played_at"),
            "acc": round(float(d.get("accuracy") or 0), 2),
            "ur": round(float(d.get("ur") or 0), 1),
            "maxCombo": int(d.get("max_combo") or 0),
            "score": int(d.get("score") or 0),
            "counts": d.get("counts") or {},
            "categories": d.get("categories") or {},
        })
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


def all_sessions(limit: int = 5000, path: str | None = None) -> list[dict]:
    """全部存档（最多 limit 条），成长评估用。"""
    return recent(limit, path)


def backfill(path: str | None = None) -> int:
    """给老库补上 dan_value / skill。

    这两个字段是后加的，之前的行是 NULL。不补的话成长页会把它们当成"没有段位"
    全部跳过，主人之前打的那些局就白存了。只补空值，已经有值的不动。
    """
    try:
        with _db(path) as conn:
            rows = conn.execute(
                "SELECT id, msd, patterns FROM sessions "
                "WHERE dan_value IS NULL AND msd IS NOT NULL"
            ).fetchall()
            fixed = 0
            for row in rows:
                info = dan.estimate(row["msd"])
                value = info.get("dan")
                try:
                    share = json.loads(row["patterns"]) if row["patterns"] else []
                except ValueError:
                    share = []
                skill, _basis = classify_skill({"patternShare": share})
                conn.execute(
                    "UPDATE sessions SET dan_value = ?, skill = ? WHERE id = ?",
                    (float(value) if value is not None else None, skill, row["id"]),
                )
                fixed += 1

            # 分类是后来才加的一列，老行同样是 NULL：从存下来的键型构成推回去
            for row in conn.execute(
                "SELECT id, patterns FROM sessions WHERE map_class IS NULL"
            ).fetchall():
                try:
                    share = json.loads(row["patterns"]) if row["patterns"] else []
                except ValueError:
                    share = []
                conn.execute(
                    "UPDATE sessions SET map_class = ? WHERE id = ?",
                    (class_from_patterns(share), row["id"]),
                )
                fixed += 1
        return fixed
    except sqlite3.Error:
        return 0


def set_player(name: str, path: str | None = None) -> dict:
    """设定本人名字，并重算每一局的归属。

    改名也走这里：旧名字的判断不会被保留，全部按新名字重来。
    回放里没写名字的行（player 为空）记成"未知"，不当作本人。
    """
    name = (name or "").strip()
    try:
        with _db(path) as conn:
            if not name:
                conn.execute("UPDATE sessions SET is_me = NULL")
            else:
                conn.execute(
                    "UPDATE sessions SET is_me = CASE "
                    "WHEN player IS NULL OR TRIM(player) = '' THEN NULL "
                    "WHEN LOWER(TRIM(player)) = LOWER(?) THEN 1 ELSE 0 END",
                    (name,),
                )
            row = conn.execute(
                "SELECT SUM(CASE WHEN is_me = 1 THEN 1 ELSE 0 END) AS mine, "
                "SUM(CASE WHEN is_me = 0 THEN 1 ELSE 0 END) AS others, "
                "SUM(CASE WHEN is_me IS NULL THEN 1 ELSE 0 END) AS unknown FROM sessions"
            ).fetchone()
        return {"ok": True, "mine": int(row["mine"] or 0), "others": int(row["others"] or 0),
                "unknown": int(row["unknown"] or 0)}
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e), "mine": 0, "others": 0, "unknown": 0}


def backfill_players(path: str | None = None) -> int:
    """从留存的回放里把玩家名补回来。

    早年写库时没存玩家名，光看数据库分不清是谁打的。好在导入过的回放还留在
    `_replays` 里，读一遍头部就能按回放 MD5 把名字对回去。
    """
    replay_dir = paths.LOCAL_REPLAY_DIR
    if not os.path.isdir(replay_dir):
        return 0
    from . import osr  # 只在补数据时用得上，放函数里免得启动就加载

    mapping: dict[str, str] = {}
    for fn in os.listdir(replay_dir):
        if not fn.lower().endswith(".osr"):
            continue
        try:
            rep = osr.parse_osr(os.path.join(replay_dir, fn))
        except Exception:  # noqa: BLE001 - 坏文件跳过，别拖垮启动
            continue
        md5 = (getattr(rep, "replay_md5", "") or "").strip()
        player = (getattr(rep, "player", "") or "").strip()
        if md5 and player:
            mapping[md5] = player
    if not mapping:
        return 0

    fixed = 0
    try:
        with _db(path) as conn:
            rows = conn.execute(
                "SELECT id, key FROM sessions WHERE player IS NULL OR TRIM(player) = ''"
            ).fetchall()
            for row in rows:
                player = mapping.get(row["key"])
                if player:
                    conn.execute("UPDATE sessions SET player = ? WHERE id = ?", (player, row["id"]))
                    fixed += 1
    except sqlite3.Error:
        return 0
    return fixed


def warmup(path: str | None = None) -> dict:
    """服务启动时调一次：建表、补字段、补老数据、重算归属、读一下总数。

    这件事放在启动阶段，是为了让"换版本之后历史还在"当场就能确认，
    而不是等用户点开成长页才发现字段缺了。
    """
    try:
        fixed = backfill(path)
        players = backfill_players(path)
        owner = configured_player()
        counts = set_player(owner, path) if owner else {"mine": 0, "others": 0, "unknown": 0}
        with _db(path) as conn:
            cols = len({r["name"] for r in conn.execute("PRAGMA table_info(sessions)")})
            n = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        return {"ok": True, "sessions": int(n), "columns": cols, "backfilled": fixed,
                "players": players, "owner": owner,
                "mine": counts.get("mine", 0), "others": counts.get("others", 0),
                "unknown": counts.get("unknown", 0)}
    except sqlite3.Error as e:
        return {"ok": False, "sessions": 0, "error": str(e)}


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
