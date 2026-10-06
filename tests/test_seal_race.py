"""封存与名单变更竞态的确定性交错测试。

被复现的竞态：封存读取参与者后尚未提交时，新增/替换可能通过开放状态
检查并成功写入，封存也返回成功，最终快照与摘要却漏掉该编号。修复后
（名单变更与封存各自把“状态检查 + 名单读写 + 提交”放进同一个
BEGIN IMMEDIATE 写事务，彼此串行化），两种提交先后都必须保持一致：

- 名单变更先提交 → 封存按变更后名单重新检查人数并保存规范快照、
  人数和摘要；
- 封存先提交 → 后续名单变更按原约定返回 409，参与者表保持封存时内容。

交错不靠运气：用 mock 在封存路径的关键位置装上闸门（threading.Event），
由测试主线程精确控制两个并发请求的相对时序，因此每次运行的交错顺序
完全确定。

运行：python -m unittest discover -s tests -v
"""
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import create_app  # noqa: E402
from app import rules, routes  # noqa: E402

SEALED_ERROR = "名单已封存，此后拒绝任何修改"


def load_verifier():
    """按路径加载 verify/verify.py（独立核验脚本）。"""
    spec = importlib.util.spec_from_file_location(
        "lottery_verify", os.path.join(ROOT, "verify", "verify.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Gate:
    """确定性闸门：进入时置 entered 信号，随后阻塞直到主线程 release。"""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.armed = True

    def wait(self):
        self.entered.set()
        if not self.release.wait(timeout=15):
            raise RuntimeError("测试闸门等待超时")


def run_in_thread(app, fn):
    """在新线程里用独立的测试客户端执行请求（客户端不可跨线程共享）。"""
    result = {}

    def target():
        try:
            result["response"] = fn(app.test_client())
        except BaseException as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    return thread, result


def finish(thread, result):
    thread.join(timeout=20)
    if thread.is_alive():
        raise AssertionError("请求线程未在超时内结束（疑似死锁）")
    if "error" in result:
        raise result["error"]
    return result["response"]


class SealRaceTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)  # 交给 sqlite 自己创建
        self.app = create_app({"DB_PATH": self.db_path})
        self.client = self.app.test_client()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    # ------------------------------------------------------------ 辅助

    def create_round(self, prize_count):
        seed = "ab" * 32
        commitment = hashlib.sha256(("COMMIT:" + seed).encode()).hexdigest()
        resp = self.client.post(
            "/api/rounds",
            json={"title": "交错测试", "prizeCount": prize_count,
                  "commitment": commitment},
        )
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return resp.get_json()["round"]["id"], seed

    def put_participants(self, rid, entries):
        resp = self.client.put(
            f"/api/rounds/{rid}/participants", json={"participants": entries}
        )
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return resp

    def get_round(self, rid):
        resp = self.client.get(f"/api/rounds/{rid}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()["round"]

    @staticmethod
    def digest_of(snapshot):
        return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()

    @staticmethod
    def hold_seal_after_list_read(gate):
        """闸门装在 canonical_snapshot：封存已读取名单、尚未提交。"""
        real_snapshot = rules.canonical_snapshot

        def gated(entries):
            gate.wait()
            return real_snapshot(entries)

        return mock.patch.object(rules, "canonical_snapshot", gated)

    @staticmethod
    def hold_seal_before_transaction(gate):
        """闸门装在 immediate_transaction：封存尚未开启写事务。"""
        real_immediate = routes.immediate_transaction

        def gated(conn):
            if gate.armed:
                gate.wait()
            return real_immediate(conn)

        return mock.patch.object(routes, "immediate_transaction", gated)

    def assert_sealed_state(self, rid, snapshot):
        """封存后：快照/人数/摘要与参与者表互相一致。"""
        final = self.get_round(rid)
        entries = snapshot.split("\n")
        self.assertEqual(final["status"], "sealed")
        self.assertEqual(final["snapshot"], snapshot)
        self.assertEqual(final["participantCount"], len(entries))
        self.assertEqual(final["listDigest"], self.digest_of(snapshot))
        self.assertEqual([p["id"] for p in final["participants"]], entries)
        return final

    # ---------------------------------------- 交错一：封存先提交

    def test_seal_commits_first_then_add_gets_409(self):
        rid, _ = self.create_round(prize_count=2)
        self.put_participants(rid, ["A001", "A002", "A003"])

        gate = Gate()
        with self.hold_seal_after_list_read(gate):
            seal_thread, seal_result = run_in_thread(
                self.app, lambda c: c.post(f"/api/rounds/{rid}/seal")
            )
            self.assertTrue(gate.entered.wait(timeout=15), "封存线程未到达闸门")

            # 封存已读取名单并持有写锁；新增请求被挡在写事务之外
            add_thread, add_result = run_in_thread(
                self.app,
                lambda c: c.post(f"/api/rounds/{rid}/participants",
                                 json={"id": "A004"}),
            )
            time.sleep(0.5)  # 让新增请求抵达并阻塞在写锁上；最终断言与时序无关
            gate.release.set()

            seal_resp = finish(seal_thread, seal_result)
            add_resp = finish(add_thread, add_result)

        snapshot = "A001\nA002\nA003"
        self.assertEqual(seal_resp.status_code, 200)
        sealed = seal_resp.get_json()["round"]
        self.assertEqual(sealed["snapshot"], snapshot)
        self.assertEqual(sealed["participantCount"], 3)
        self.assertEqual(sealed["listDigest"], self.digest_of(snapshot))

        self.assertEqual(add_resp.status_code, 409)
        self.assertEqual(add_resp.get_json()["error"], SEALED_ERROR)

        self.assert_sealed_state(rid, snapshot)

    def test_seal_commits_first_then_replace_gets_409(self):
        rid, _ = self.create_round(prize_count=2)
        self.put_participants(rid, ["A001", "A002", "A003"])

        gate = Gate()
        with self.hold_seal_after_list_read(gate):
            seal_thread, seal_result = run_in_thread(
                self.app, lambda c: c.post(f"/api/rounds/{rid}/seal")
            )
            self.assertTrue(gate.entered.wait(timeout=15), "封存线程未到达闸门")

            replace_thread, replace_result = run_in_thread(
                self.app,
                lambda c: c.put(f"/api/rounds/{rid}/participants",
                                json={"participants": ["B001", "B002"]}),
            )
            time.sleep(0.5)  # 同上：让替换请求阻塞在写锁上
            gate.release.set()

            seal_resp = finish(seal_thread, seal_result)
            replace_resp = finish(replace_thread, replace_result)

        snapshot = "A001\nA002\nA003"
        self.assertEqual(seal_resp.status_code, 200)
        self.assertEqual(seal_resp.get_json()["round"]["snapshot"], snapshot)

        self.assertEqual(replace_resp.status_code, 409)
        self.assertEqual(replace_resp.get_json()["error"], SEALED_ERROR)

        self.assert_sealed_state(rid, snapshot)

    # ---------------------------------------- 交错二：名单变更先提交

    def test_add_commits_first_then_seal_uses_updated_list(self):
        rid, _ = self.create_round(prize_count=3)
        # 旧名单只有 2 人：封存若按变更前名单检查人数必然失败
        self.put_participants(rid, ["A001", "A002"])

        gate = Gate()
        with self.hold_seal_before_transaction(gate):
            seal_thread, seal_result = run_in_thread(
                self.app, lambda c: c.post(f"/api/rounds/{rid}/seal")
            )
            self.assertTrue(gate.entered.wait(timeout=15), "封存线程未到达闸门")
            gate.armed = False  # 名单变更请求直接通行

            add_resp = self.client.post(
                f"/api/rounds/{rid}/participants", json={"id": "A003"}
            )
            self.assertEqual(add_resp.status_code, 200, add_resp.get_json())

            gate.release.set()  # 新增已提交，放行封存
            seal_resp = finish(seal_thread, seal_result)

        self.assertEqual(seal_resp.status_code, 200, seal_resp.get_json())
        sealed = seal_resp.get_json()["round"]
        self.assertEqual(sealed["snapshot"], "A001\nA002\nA003")
        self.assertEqual(sealed["participantCount"], 3)
        self.assertEqual(sealed["listDigest"],
                         self.digest_of("A001\nA002\nA003"))

        self.assert_sealed_state(rid, "A001\nA002\nA003")

    def test_replace_commits_first_then_seal_uses_replaced_list(self):
        rid, _ = self.create_round(prize_count=2)
        self.put_participants(rid, ["A001", "A002", "A003"])

        gate = Gate()
        with self.hold_seal_before_transaction(gate):
            seal_thread, seal_result = run_in_thread(
                self.app, lambda c: c.post(f"/api/rounds/{rid}/seal")
            )
            self.assertTrue(gate.entered.wait(timeout=15), "封存线程未到达闸门")
            gate.armed = False

            replace_resp = self.client.put(
                f"/api/rounds/{rid}/participants",
                json={"participants": ["B001", "B002", "B003", "B004"]},
            )
            self.assertEqual(replace_resp.status_code, 200,
                             replace_resp.get_json())

            gate.release.set()  # 整体替换已提交，放行封存
            seal_resp = finish(seal_thread, seal_result)

        snapshot = "B001\nB002\nB003\nB004"
        self.assertEqual(seal_resp.status_code, 200, seal_resp.get_json())
        sealed = seal_resp.get_json()["round"]
        self.assertEqual(sealed["snapshot"], snapshot)
        self.assertEqual(sealed["participantCount"], 4)
        self.assertEqual(sealed["listDigest"], self.digest_of(snapshot))

        self.assert_sealed_state(rid, snapshot)

    def test_replace_commits_first_then_seal_rechecks_count(self):
        """变更后人数不足：封存必须按变更后名单重新检查并拒绝。"""
        rid, _ = self.create_round(prize_count=2)
        self.put_participants(rid, ["A001", "A002", "A003"])

        gate = Gate()
        with self.hold_seal_before_transaction(gate):
            seal_thread, seal_result = run_in_thread(
                self.app, lambda c: c.post(f"/api/rounds/{rid}/seal")
            )
            self.assertTrue(gate.entered.wait(timeout=15), "封存线程未到达闸门")
            gate.armed = False

            replace_resp = self.client.put(
                f"/api/rounds/{rid}/participants",
                json={"participants": ["B001"]},  # 1 人 < 名额 2
            )
            self.assertEqual(replace_resp.status_code, 200,
                             replace_resp.get_json())

            gate.release.set()
            seal_resp = finish(seal_thread, seal_result)

        self.assertEqual(seal_resp.status_code, 400)
        self.assertIn("人数不足", seal_resp.get_json()["error"])

        # 封存失败：轮次仍为 open，名单保持变更后内容
        final = self.get_round(rid)
        self.assertEqual(final["status"], "open")
        self.assertEqual([p["id"] for p in final["participants"]], ["B001"])
        self.assertIsNone(final["listDigest"])

    # ---------------------------------------- 顺序行为回归

    def test_insufficient_count_cannot_seal(self):
        rid, _ = self.create_round(prize_count=3)
        self.put_participants(rid, ["A001", "A002"])
        resp = self.client.post(f"/api/rounds/{rid}/seal")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("人数不足", resp.get_json()["error"])
        self.assertEqual(self.get_round(rid)["status"], "open")

    def test_repeat_seal_returns_existing_snapshot(self):
        rid, _ = self.create_round(prize_count=2)
        self.put_participants(rid, ["A001", "A002", "A003"])
        first = self.client.post(f"/api/rounds/{rid}/seal").get_json()["round"]
        second_resp = self.client.post(f"/api/rounds/{rid}/seal")
        self.assertEqual(second_resp.status_code, 200)
        second_body = second_resp.get_json()
        self.assertTrue(second_body["alreadySealed"])
        second = second_body["round"]
        self.assertEqual(second["snapshot"], first["snapshot"])
        self.assertEqual(second["listDigest"], first["listDigest"])
        self.assertEqual(second["participantCount"], first["participantCount"])
        self.assertEqual(second["sealedAt"], first["sealedAt"])

    def test_all_mutations_after_seal_return_409(self):
        rid, _ = self.create_round(prize_count=2)
        self.put_participants(rid, ["A001", "A002", "A003"])
        self.assertEqual(
            self.client.post(f"/api/rounds/{rid}/seal").status_code, 200
        )

        responses = [
            self.client.post(f"/api/rounds/{rid}/participants",
                             json={"id": "A004"}),
            self.client.put(f"/api/rounds/{rid}/participants",
                            json={"participants": ["B001", "B002"]}),
            self.client.delete(f"/api/rounds/{rid}/participants/A001"),
        ]
        for resp in responses:
            self.assertEqual(resp.status_code, 409, resp.get_json())
            self.assertEqual(resp.get_json()["error"], SEALED_ERROR)

        self.assert_sealed_state(rid, "A001\nA002\nA003")

    def test_reveal_and_independent_verify_still_work(self):
        rid, seed = self.create_round(prize_count=3)
        entries = ["A001", "A002", "A003", "A004", "A005"]
        self.put_participants(rid, entries)
        self.assertEqual(
            self.client.post(f"/api/rounds/{rid}/seal").status_code, 200
        )

        # 错误种子：保持封存，仅记录失败次数
        bad = self.client.post(f"/api/rounds/{rid}/reveal",
                               json={"seed": "ff" * 32})
        bad_body = bad.get_json()
        self.assertFalse(bad_body["ok"])
        self.assertTrue(bad_body["commitMismatch"])
        self.assertEqual(bad_body["round"]["status"], "sealed")
        self.assertEqual(bad_body["round"]["failedAttempts"], 1)

        # 正确种子：揭晓，结果与本地按规则重算一致
        good = self.client.post(f"/api/rounds/{rid}/reveal",
                                json={"seed": seed})
        self.assertEqual(good.status_code, 200, good.get_json())
        revealed = good.get_json()["round"]
        self.assertEqual(revealed["status"], "revealed")
        self.assertEqual(revealed["winners"],
                         rules.draw_winners(entries, seed, 3))

        # 重复揭晓：不重抽
        again = self.client.post(f"/api/rounds/{rid}/reveal",
                                 json={"seed": seed})
        self.assertTrue(again.get_json()["alreadyRevealed"])
        self.assertEqual(again.get_json()["round"]["winners"],
                         revealed["winners"])

        # 导出核验包，交给独立核验器重算校验
        pkg_resp = self.client.get(f"/api/rounds/{rid}/package.json")
        self.assertEqual(pkg_resp.status_code, 200)
        pkg = json.loads(pkg_resp.data.decode("utf-8"))
        verifier = load_verifier()
        with redirect_stdout(io.StringIO()):
            errors, checks = verifier.verify(pkg)
        self.assertEqual(errors, [])
        self.assertTrue(checks)


if __name__ == "__main__":
    unittest.main()
