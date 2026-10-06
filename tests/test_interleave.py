"""确定性交错验证：同一轮次名单变更与封存的加锁/提交先后。

两个真实 SQLite 连接 + 两个线程，手工安排：

- 线程 A 先 BEGIN IMMEDIATE 并完成写操作但暂不提交（持有 RESERVED 写锁）；
- 线程 B 随后 BEGIN IMMEDIATE —— 必然阻塞（SQLite 只允许一个写锁）；
- 确认 B 确实被挡住后，A 提交；B 解除阻塞并继续，此时只能读到 A 已提交的状态。

覆盖三类名单变更（新增 / 整体替换 / 删除）与两种提交先后：
    变更先提交 → 封存按变更后名单复核人数并生成快照；
    封存先提交 → 变更被事务内重新检查拒绝（409），参与者表保持封存时内容。
"""
import os
import threading
import time
import unittest

from app import rules
from app import services
from app.db import transaction
from app.services import ApiError

from .helpers import (
    create_round,
    demo_commitment,
    make_app,
    open_conn,
    replace,
)

# 被挡线程至少停留这么久，足以证明它真的在等写锁，而不是已经跑完。
BLOCK_GRACE = 0.3


class TxWorker(threading.Thread):
    """在独立连接/事务中执行一个 services.*_tx 调用。"""

    def __init__(self, db_path, fn):
        super().__init__(daemon=True)
        self.db_path = db_path
        self.fn = fn
        self.result = None
        self.exc = None

    def run(self):
        conn = open_conn(self.db_path)
        try:
            with transaction(conn):
                self.result = self.fn(conn)
        except BaseException as e:  # 捕获后在主线程断言，线程内不吞掉
            self.exc = e
        finally:
            conn.close()

    def wait_done(self, testcase, timeout=10):
        self.join(timeout)
        testcase.assertFalse(
            self.is_alive(), "工作线程未在预期时间内结束（可能死锁）"
        )

    def assert_blocked(self, testcase):
        time.sleep(BLOCK_GRACE)
        testcase.assertTrue(
            self.is_alive(), "工作线程应当仍在等待写锁，但已经结束"
        )
        testcase.assertIsNone(self.result)
        testcase.assertIsNone(self.exc)


class InterleaveTest(unittest.TestCase):
    def setUp(self):
        self.app, self.db_path = make_app()
        self.client = self.app.test_client()
        self.addCleanup(lambda: os.path.exists(self.db_path) and None)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.remove(path)

    # ------------------------------------------------ 辅助断言

    def _open_main_tx(self):
        conn = open_conn(self.db_path)
        conn.execute("BEGIN IMMEDIATE")
        return conn

    def _get(self, round_id):
        resp = self.client.get(f"/api/rounds/{round_id}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()["round"]

    def _assert_sealed_snapshot(self, round_id, entries):
        data = self._get(round_id)
        self.assertEqual(data["status"], "sealed")
        self.assertEqual(data["participantCount"], len(entries))
        self.assertEqual(data["snapshot"], rules.canonical_snapshot(entries))
        self.assertEqual(
            data["listDigest"], rules.list_digest(entries)
        )
        table_ids = sorted(p["id"] for p in data["participants"])
        self.assertEqual(
            table_ids,
            sorted(data["snapshot"].split("\n")),
            "参与者表必须与封存快照一致",
        )

    # ------------------------------------------------ 变更先提交

    def test_add_commits_first_seal_includes_new_id(self):
        """封存读名单前，新增已提交 → 快照/摘要/人数必须包含新编号。

        初始只有 2 人（< 名额 3），若封存按旧名单判断会误报人数不足；
        按修复后的语义，新增提交后封存必须成功且为 3 人。
        """
        rid = create_round(self.client)
        replace(self.client, rid, ["A001", "A002"])

        main = self._open_main_tx()
        services.add_participant_tx(main, rid, "A003")  # 未提交，持锁

        worker = TxWorker(
            self.db_path, lambda c: services.seal_round_tx(c, rid)
        )
        worker.start()
        worker.assert_blocked(self)

        main.commit()
        main.close()
        worker.wait_done(self)

        self.assertIsNone(worker.exc, worker.exc)
        row, already = worker.result
        self.assertFalse(already)
        self._assert_sealed_snapshot(rid, ["A001", "A002", "A003"])

    def test_replace_commits_first_seal_uses_new_list(self):
        """整体替换先提交 → 封存按替换后的名单保存快照。"""
        rid = create_round(self.client)
        replace(self.client, rid, ["A001", "A002"])

        main = self._open_main_tx()
        services.replace_participants_tx(
            main, rid, ["B001", "B002", "B003"]
        )

        worker = TxWorker(
            self.db_path, lambda c: services.seal_round_tx(c, rid)
        )
        worker.start()
        worker.assert_blocked(self)

        main.commit()
        main.close()
        worker.wait_done(self)

        self.assertIsNone(worker.exc, worker.exc)
        self._assert_sealed_snapshot(rid, ["B001", "B002", "B003"])

    def test_delete_commits_first_seal_excludes_deleted_id(self):
        """删除先提交 → 封存快照不得再含被删编号。"""
        rid = create_round(self.client)
        replace(self.client, rid, ["A001", "A002", "A003", "A004"])

        main = self._open_main_tx()
        services.delete_participant_tx(main, rid, "A004")

        worker = TxWorker(
            self.db_path, lambda c: services.seal_round_tx(c, rid)
        )
        worker.start()
        worker.assert_blocked(self)

        main.commit()
        main.close()
        worker.wait_done(self)

        self.assertIsNone(worker.exc, worker.exc)
        self._assert_sealed_snapshot(rid, ["A001", "A002", "A003"])

    # ------------------------------------------------ 封存先提交

    def test_seal_commits_first_add_gets_409(self):
        rid = create_round(self.client)
        entries = ["A001", "A002", "A003"]
        replace(self.client, rid, entries)

        seal_conn = self._open_main_tx()
        row, already = services.seal_round_tx(seal_conn, rid)
        self.assertFalse(already)

        worker = TxWorker(
            self.db_path,
            lambda c: services.add_participant_tx(c, rid, "HACKER"),
        )
        worker.start()
        worker.assert_blocked(self)

        seal_conn.commit()
        seal_conn.close()
        worker.wait_done(self)

        self.assertIsInstance(worker.exc, ApiError)
        self.assertEqual(worker.exc.status, 409)
        self._assert_sealed_snapshot(rid, entries)  # 表保持封存时内容

    def test_seal_commits_first_replace_gets_409(self):
        rid = create_round(self.client)
        entries = ["A001", "A002", "A003"]
        replace(self.client, rid, entries)

        seal_conn = self._open_main_tx()
        services.seal_round_tx(seal_conn, rid)

        worker = TxWorker(
            self.db_path,
            lambda c: services.replace_participants_tx(
                c, rid, ["X1", "X2", "X3"]
            ),
        )
        worker.start()
        worker.assert_blocked(self)

        seal_conn.commit()
        seal_conn.close()
        worker.wait_done(self)

        self.assertIsInstance(worker.exc, ApiError)
        self.assertEqual(worker.exc.status, 409)
        self._assert_sealed_snapshot(rid, entries)  # 替换未发生

    def test_seal_commits_first_delete_gets_409(self):
        rid = create_round(self.client)
        entries = ["A001", "A002", "A003"]
        replace(self.client, rid, entries)

        seal_conn = self._open_main_tx()
        services.seal_round_tx(seal_conn, rid)

        worker = TxWorker(
            self.db_path,
            lambda c: services.delete_participant_tx(c, rid, "A003"),
        )
        worker.start()
        worker.assert_blocked(self)

        seal_conn.commit()
        seal_conn.close()
        worker.wait_done(self)

        self.assertIsInstance(worker.exc, ApiError)
        self.assertEqual(worker.exc.status, 409)
        self._assert_sealed_snapshot(rid, entries)  # 删除未发生

    # ------------------------------------------------ 失败后的锁释放 / 幂等

    def test_blocked_change_rolls_back_and_releases_lock(self):
        """被 409 拒绝的变更必须回滚并释放写锁：之后重复封存/揭晓正常。"""
        seed, commitment = demo_commitment()
        rid = create_round(self.client, commitment=commitment)
        entries = ["A001", "A002", "A003"]
        replace(self.client, rid, entries)
        digest_before = rules.list_digest(entries)

        seal_conn = self._open_main_tx()
        services.seal_round_tx(seal_conn, rid)
        worker = TxWorker(
            self.db_path,
            lambda c: services.add_participant_tx(c, rid, "A004"),
        )
        worker.start()
        worker.assert_blocked(self)
        seal_conn.commit()
        seal_conn.close()
        worker.wait_done(self)
        self.assertEqual(worker.exc.status, 409)

        # 写锁已释放：重复封存返回既有快照，不重算、不覆盖。
        resp = self.client.post(f"/api/rounds/{rid}/seal")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["alreadySealed"])
        self.assertEqual(body["round"]["listDigest"], digest_before)

        # 揭晓仍可进行，且参与者表仍为封存时的 3 人。
        resp = self.client.post(
            f"/api/rounds/{rid}/reveal", json={"seed": seed}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.get_json()["round"]["winners"],
            rules.draw_winners(entries, seed, 3),
        )
        self._assert_sealed_state_participants(rid, entries)

    def _assert_sealed_state_participants(self, rid, entries):
        data = self._get(rid)
        self.assertEqual(data["status"], "revealed")
        self.assertEqual(
            sorted(p["id"] for p in data["participants"]), sorted(entries)
        )
        self.assertEqual(
            data["snapshot"].split("\n"),
            sorted(entries, key=lambda e: e.encode("utf-8")),
        )


if __name__ == "__main__":
    unittest.main()
