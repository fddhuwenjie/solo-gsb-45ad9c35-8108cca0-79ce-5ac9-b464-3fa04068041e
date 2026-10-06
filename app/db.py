"""SQLite 连接与建表。

轮次状态机：
    open     可登记/修改名单
    sealed   名单已封存，等待提交种子；承诺不符仍停留在此状态
    revealed 种子匹配承诺，中奖结果一次性写入，永久拒绝重抽
"""
import sqlite3
from contextlib import contextmanager
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


@contextmanager
def immediate_transaction(conn: sqlite3.Connection):
    """把“状态检查 + 名单读写 + 提交”放进同一个写事务。

    BEGIN IMMEDIATE 在事务一开始就取得 SQLite 写锁，使并发的名单变更
    与封存串行化，消除“先查状态再读写”的检查—使用竞态（TOCTOU）：

    - 名单变更先提交 → 封存在自己的事务里重新读取名单，
      快照、人数与摘要必然基于变更后的名单；
    - 封存先提交 → 名单变更在事务内复查状态时会看到 sealed，
      按约定返回 409，参与者表保持封存时内容。

    块内提前 return（如 404/409/参数校验失败）会正常提交——
    这些路径没有任何写入，提交只用于释放写锁；异常则回滚。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


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
