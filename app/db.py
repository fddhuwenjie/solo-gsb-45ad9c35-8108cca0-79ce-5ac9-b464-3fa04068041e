"""SQLite 连接与建表。

轮次状态机：
    open     可登记/修改名单
    sealed   名单已封存，等待提交种子；承诺不符仍停留在此状态
    revealed 种子匹配承诺，中奖结果一次性写入，永久拒绝重抽

并发约定：
    所有“读状态—改数据—提交”的写流程必须使用 ``transaction()``（BEGIN IMMEDIATE）。
    IMMEDIATE 在事务开始时即取得 RESERVED 写锁，因此名单变更与封存互斥串行：
    后到的事务要么在加锁时阻塞直到前者提交，要么在拿到锁后重新读到对方已提交的
    状态与名单，不会出现“封存读到旧名单、变更通过旧状态检查”的交错。
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
    winners_json     TEXT,                      -- 揭晓时一次写入
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
            # 关闭 sqlite3 模块的隐式事务：事务边界一律由 transaction() 显式管理，
            # 避免“SELECT 后 DML 悄悄开启延迟事务”导致锁获取时机不可控。
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        g.db = conn
    return g.db


@contextmanager
def transaction(conn: sqlite3.Connection):
    """立即写锁事务：BEGIN IMMEDIATE … commit / 异常 rollback。

    事务体内读取到的轮次状态与名单在该事务的一致性快照内有效，且写锁保证
    不会有另一个名单/封存事务在中途插入提交。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    with app.app_context():
        get_db().executescript(SCHEMA)
        get_db().commit()
