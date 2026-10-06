"""名单与封存/揭晓的写流程（状态检查、名单读写必须在同一个 IMMEDIATE 事务内）。

每个 ``*_tx`` 函数只负责在调用方给定的事务（``db.transaction(conn)``）内完成
“重新读取轮次状态 → 读名单 → 校验 → 写数据”，自身不提交、不回滚：

- HTTP 路由在请求事务中提交，请求间由 IMMEDIATE 写锁串行；
- 确定性交错测试可以用两个真实 SQLite 连接精确安排两边的加锁/提交先后。
"""
import json
import secrets

from . import rules
from .db import now_iso


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra or {}


MAX_PRIZE = 10000


def get_round(conn, round_id: str):
    return conn.execute(
        "SELECT * FROM rounds WHERE id=?", (round_id,)
    ).fetchone()


def participant_rows(conn, round_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT entry_id, position FROM participants WHERE round_id=? "
        "ORDER BY position, entry_id",
        (round_id,),
    ).fetchall()
    return [{"id": r["entry_id"], "position": r["position"]} for r in rows]


def create_round_tx(conn, title: str, prize_count, commitment: str):
    """创建轮次。调用方已做基础类型校验，这里保留事务内的最终校验。"""
    title = (title or "").strip()
    commitment = (commitment or "").strip().lower()
    if not title:
        raise ApiError("请填写轮次名称")
    if not isinstance(prize_count, int) or isinstance(prize_count, bool):
        raise ApiError("中奖名额必须是整数")
    if not 1 <= prize_count <= MAX_PRIZE:
        raise ApiError(f"中奖名额必须在 1 到 {MAX_PRIZE} 之间")
    if not rules.is_hex64(commitment):
        raise ApiError("承诺必须是 64 位十六进制 SHA-256 摘要")

    round_id = secrets.token_hex(6)
    conn.execute(
        "INSERT INTO rounds (id,title,prize_count,commit_hex,created_at) "
        "VALUES (?,?,?,?,?)",
        (round_id, title, prize_count, commitment, now_iso()),
    )
    return get_round(conn, round_id)


def _require_open_tx(conn, round_id: str):
    """事务内重新读取轮次并要求 open；封存/揭晓后的修改一律 409。"""
    row = get_round(conn, round_id)
    if row is None:
        raise ApiError("轮次不存在", 404)
    if row["status"] != "open":
        raise ApiError("名单已封存，此后拒绝任何修改", 409)
    return row


def replace_participants_tx(conn, round_id: str, raw) -> tuple[list[dict], int]:
    """整体替换名单：全部校验与删除/插入在同一事务内。"""
    _require_open_tx(conn, round_id)
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ApiError("participants 必须是字符串数组")

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        entry = rules.normalize_entry(item)
        if not entry:
            continue
        if entry in seen:
            raise ApiError(f"编号重复：{entry}")
        if "\n" in entry or "\r" in entry:
            raise ApiError(f"编号不能包含换行符：{entry}")
        if len(entry) > 200:
            raise ApiError(f"编号过长（>200 字符）：{entry}")
        seen.add(entry)
        cleaned.append(entry)

    conn.execute("DELETE FROM participants WHERE round_id=?", (round_id,))
    conn.executemany(
        "INSERT INTO participants (round_id,entry_id,position,added_at) "
        "VALUES (?,?,?,?)",
        [(round_id, e, i, now_iso()) for i, e in enumerate(cleaned)],
    )
    return participant_rows(conn, round_id), len(cleaned)


def add_participant_tx(conn, round_id: str, raw_id) -> list[dict]:
    """新增编号：状态检查、查重、插入在同一事务内。"""
    _require_open_tx(conn, round_id)
    entry = rules.normalize_entry(raw_id or "")
    if not entry:
        raise ApiError("请输入编号")
    if "\n" in entry or "\r" in entry or len(entry) > 200:
        raise ApiError("编号不合法")

    exists = conn.execute(
        "SELECT 1 FROM participants WHERE round_id=? AND entry_id=?",
        (round_id, entry),
    ).fetchone()
    if exists:
        raise ApiError(f"编号已存在：{entry}", 409)
    pos = conn.execute(
        "SELECT COALESCE(MAX(position)+1,0) FROM participants WHERE round_id=?",
        (round_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO participants (round_id,entry_id,position,added_at) "
        "VALUES (?,?,?,?)",
        (round_id, entry, pos, now_iso()),
    )
    return participant_rows(conn, round_id)


def delete_participant_tx(conn, round_id: str, entry_id: str) -> list[dict]:
    """删除编号：状态检查与删除在同一事务内。"""
    _require_open_tx(conn, round_id)
    conn.execute(
        "DELETE FROM participants WHERE round_id=? AND entry_id=?",
        (round_id, entry_id),
    )
    return participant_rows(conn, round_id)


def seal_round_tx(conn, round_id: str):
    """封存名单。

    在写锁事务内重新读取状态与名单：人数按“变更提交后的最新名单”复核，
    规范快照、摘要、人数在同一事务一次写入；重复封存直接返回既有快照，
    不重算、不覆盖。
    返回 (row, already_sealed)。
    """
    row = get_round(conn, round_id)
    if row is None:
        raise ApiError("轮次不存在", 404)
    if row["status"] == "revealed":
        raise ApiError("轮次已揭晓，无法封存", 409)
    if row["status"] == "sealed":
        return row, True

    # 关键：拿到写锁后再读名单，确保封存包含此前已提交的所有名单变更。
    entries = [p["id"] for p in participant_rows(conn, round_id)]
    if len(entries) < row["prize_count"]:
        raise ApiError(
            f"人数不足：当前 {len(entries)} 人，中奖名额 {row['prize_count']} 个，"
            "封存要求人数不少于名额"
        )

    snapshot = rules.canonical_snapshot(entries)
    digest = rules.sha256_hex(snapshot.encode("utf-8"))
    conn.execute(
        "UPDATE rounds SET status='sealed', snapshot=?, list_digest_hex=?, "
        "participant_count=?, sealed_at=? WHERE id=?",
        (snapshot, digest, len(entries), now_iso(), round_id),
    )
    return get_round(conn, round_id), False


def reveal_round_tx(conn, round_id: str, raw_seed: str):
    """揭晓：状态检查、承诺核对与结果写入在同一事务内。

    返回 dict，由路由组装响应；承诺不符时轮次保持 sealed，仅累加失败计数。
    """
    row = get_round(conn, round_id)
    if row is None:
        raise ApiError("轮次不存在", 404)
    if row["status"] == "revealed":
        return {"already_revealed": True, "row": row}

    if row["status"] != "sealed":
        raise ApiError("轮次尚未封存", 409)

    seed = (raw_seed or "").strip().lower()
    if not rules.seed_matches_commit(seed, row["commit_hex"]):
        conn.execute(
            "UPDATE rounds SET failed_attempts=failed_attempts+1 WHERE id=?",
            (round_id,),
        )
        return {"commit_mismatch": True, "row": get_round(conn, round_id)}

    entries = row["snapshot"].split("\n") if row["snapshot"] else []
    winners = rules.draw_winners(entries, seed, row["prize_count"])
    conn.execute(
        "UPDATE rounds SET status='revealed', seed_hex=?, winners_json=?, "
        "revealed_at=? WHERE id=?",
        (seed, json.dumps(winners, ensure_ascii=False), now_iso(), round_id),
    )
    return {"revealed": True, "winners": winners,
            "row": get_round(conn, round_id)}
