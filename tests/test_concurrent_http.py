"""HTTP 层并发补充：多个真实请求同时新增/替换与封存时，快照与参与者表必须一致。

这不是确定性交错（确定性用例见 test_interleave.py），而是端到端的不变量检查：
无论请求实际交错成什么样，封存成功后必然满足
    状态=sealed 且 参与者表集合 == 快照集合 == 摘要重算集合 且 人数 ≥ 名额。
"""
import os
import threading
import unittest

from app import rules

from .helpers import create_round, make_app, replace

# 工作线程先在就绪事件上集合，主线程确认全部就绪后统一起跑；
# 不使用 threading.Barrier——本环境下跨测试复用进程时曾观察到主线程单独等待的挂起。
class _Gate:
    def __init__(self, n):
        self.n = n
        self.ready = threading.Event()
        self.go = threading.Event()
        self._lock = threading.Lock()
        self._seen = 0

    def wait_ready_and_open(self):
        """主线程：等所有工作线程就绪后放行。"""
        self.ready.wait(5)
        self.go.set()

    def worker_ready_and_wait(self):
        with self._lock:
            self._seen += 1
            if self._seen == self.n:
                self.ready.set()
        self.go.wait(5)


class ConcurrentHttpTest(unittest.TestCase):
    def setUp(self):
        self.app, self.db_path = make_app()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.remove(path)

    def test_concurrent_add_and_seal(self):
        prize = 5
        base = 3
        extra = 10
        rid = create_round(self._client(), prize_count=prize)
        initial = [f"B{i:03d}" for i in range(base)]
        replace(self._client(), rid, initial)

        gate = _Gate(extra)
        errors = []

        def add(i):
            try:
                local = self.app.test_client()
                gate.worker_ready_and_wait()
                local.post(
                    f"/api/rounds/{rid}/participants",
                    json={"id": f"X{i:03d}"},
                )
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=add, args=(i,))
                   for i in range(extra)]
        for t in threads:
            t.start()
        gate.wait_ready_and_open()  # 封存与所有新增同时放行
        seal_resp = self._client().post(f"/api/rounds/{rid}/seal")
        for t in threads:
            t.join(15)
            self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])

        self.assertIn(seal_resp.status_code, (200, 400))
        body = seal_resp.get_json()
        if seal_resp.status_code == 400:
            # 只有“封存早于足够多新增提交”才可能人数不足；再封存一次必成功
            self.assertIn("人数不足", body["error"])
            seal_resp = self._client().post(f"/api/rounds/{rid}/seal")
            self.assertEqual(seal_resp.status_code, 200)
            body = seal_resp.get_json()

        sealed = body["round"]
        self.assertEqual(sealed["status"], "sealed")
        self.assertGreaterEqual(sealed["participantCount"], prize)

        snap_ids = sealed["snapshot"].split("\n")
        table_ids = [p["id"] for p in sealed["participants"]]

        # 核心不变量：表、快照、摘要三者一致，无重复编号
        self.assertEqual(len(snap_ids), len(set(snap_ids)))
        self.assertEqual(sorted(table_ids), sorted(snap_ids))
        self.assertEqual(
            sealed["listDigest"], rules.list_digest(snap_ids)
        )
        self.assertEqual(sealed["participantCount"], len(snap_ids))

        # 封存之后任何新增都必须 409，且表不再变化
        resp = self._client().post(
            f"/api/rounds/{rid}/participants", json={"id": "AFTER"}
        )
        self.assertEqual(resp.status_code, 409)
        final = self._client().get(f"/api/rounds/{rid}").get_json()["round"]
        self.assertEqual(
            sorted(p["id"] for p in final["participants"]), sorted(snap_ids)
        )

    def test_concurrent_replace_and_seal(self):
        client = self._client()
        rid = create_round(client, prize_count=3)
        # 初始名单本身满足封存门槛：两种提交先后都可能出现。
        replace(client, rid, ["A001", "A002", "A003"])

        gate = _Gate(1)
        outcomes = {}

        def do_replace():
            local = self.app.test_client()
            gate.worker_ready_and_wait()
            outcomes["replace"] = local.put(
                f"/api/rounds/{rid}/participants",
                json={"participants": ["C001", "C002", "C003", "C004"]},
            )

        t = threading.Thread(target=do_replace)
        t.start()
        gate.wait_ready_and_open()
        outcomes["seal"] = client.post(f"/api/rounds/{rid}/seal")
        t.join(15)
        self.assertFalse(t.is_alive())

        final = client.get(f"/api/rounds/{rid}").get_json()["round"]
        seal_resp = outcomes["seal"]
        replace_resp = outcomes["replace"]
        self.assertEqual(seal_resp.status_code, 200)
        self.assertEqual(final["status"], "sealed")

        snap = final["snapshot"].split("\n")
        self.assertEqual(
            sorted(p["id"] for p in final["participants"]), sorted(snap)
        )
        self.assertEqual(final["listDigest"], rules.list_digest(snap))

        if replace_resp.status_code == 409:
            # 封存先提交：替换按原约定 409，快照保持封存时的旧名单
            self.assertEqual(
                seal_resp.get_json()["round"]["snapshot"],
                "A001\nA002\nA003",
            )
            self.assertEqual(snap, ["A001", "A002", "A003"])
        else:
            # 替换先提交：封存按变更后名单生成快照
            self.assertEqual(replace_resp.status_code, 200)
            self.assertEqual(snap, ["C001", "C002", "C003", "C004"])

        # 无论谁先，封存后再改都必须 409 且表不再变化
        after = client.put(
            f"/api/rounds/{rid}/participants",
            json={"participants": ["Z001"]},
        )
        self.assertEqual(after.status_code, 409)
        final2 = client.get(f"/api/rounds/{rid}").get_json()["round"]
        self.assertEqual(
            sorted(p["id"] for p in final2["participants"]), sorted(snap)
        )

    def _client(self):
        return self.app.test_client()


if __name__ == "__main__":
    unittest.main()
