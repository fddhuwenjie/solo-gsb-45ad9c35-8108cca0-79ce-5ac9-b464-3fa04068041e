"""SQLite 连接与建表。

轮次状态机：
    open     可登记/修改名单
    sealed   名单已封存，等待提交种子；承诺不符仍停留在此状态
    revealed 种子匹配承诺，中奖结果一次性写入，永久拒绝重抽
"""
import sqlite3
from datetime import datetime, timezone

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    prize_count      INTEGER NOT NULL CHECK (prize_count > 0),
    commit_hex       TEXT NOT NULL CHECK (length(commit_hex) = 64),
    status           TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open','sealed','revealed')),
    list_digest_hex  TEXT,                       -- 封存时写入
    snapshot         TEXT,                       -- 封存时的规范名单快照
    participant_count INTEGER,                  -- 封存时人数
    sealed_at        TEXT,
    seed_hex         TEXT,                       -- 揭晓匹配后写入
    winners_json     TEXT,                       -- 揭晓时一次写入
    revealed_at      TEXT,
    failed_attempts  INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS participants (
    round_id  TEXT NOT NULL REFERENCES rounds(id),
    entry_id  TEXT NOT NULL,
    position  INTEGER NOT NULL,          -- 名单内顺序（仅作展示）
    added_at  TEXT NOT NULL,
    PRIMARY KEY (round_id, entry_id)
);

CREATE INDEX IF NOT EXISTS idx_participants_round ON participants(round_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DB_PATH"],
            timeout=15,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        g.db = conn
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    with app.app_context():
        get_db().executescript(SCHEMA)
        get_db().commit()
