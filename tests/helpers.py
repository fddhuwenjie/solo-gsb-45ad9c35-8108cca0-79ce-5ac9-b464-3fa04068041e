"""测试公共工具：临时数据库的 Flask 应用与真实 SQLite 连接。"""
import hashlib
import os
import sqlite3
import tempfile


def make_app():
    """创建指向临时数据库的 Flask 应用，返回 (app, db_path)。"""
    fd, db_path = tempfile.mkstemp(prefix="lottery-test-", suffix=".db")
    os.close(fd)
    os.remove(db_path)  # create_app 的建表逻辑会重新创建
    os.environ["LOTTERY_DB"] = db_path

    # 延迟导入，确保 LOTTERY_DB 已指向临时路径
    from app import create_app

    app = create_app()
    return app, db_path


def demo_commitment(seed: str | None = None) -> tuple[str, str]:
    if seed is None:
        seed = "0123456789abcdef" * 4
    digest = hashlib.sha256(("COMMIT:" + seed).encode("utf-8")).hexdigest()
    return seed, digest


def open_conn(db_path: str) -> sqlite3.Connection:
    """打开与生产配置一致的 autocommit 连接（每个线程用自己的连接）。"""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def create_round(client, title="交错测试", prize_count=3, commitment=None):
    if commitment is None:
        _, commitment = demo_commitment()
    resp = client.post(
        "/api/rounds",
        json={"title": title, "prizeCount": prize_count,
              "commitment": commitment},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["round"]["id"]


def replace(client, round_id, entries):
    resp = client.put(
        f"/api/rounds/{round_id}/participants",
        json={"participants": entries},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()
