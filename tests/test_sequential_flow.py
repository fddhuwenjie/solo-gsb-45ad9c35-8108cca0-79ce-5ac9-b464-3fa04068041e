"""顺序流程回归：沿用 README“命令行 / API”一节的操作示例与既定字段。

覆盖：创建 → 名单增删改/查重 → 人数不足拒绝封存 → 封存幂等 →
封存后修改一律 409 → 错误种子保持封存 → 正确种子揭晓（结果与 README 一致）→
重复揭晓不重抽 → 核验包导出后独立 verify.py 核验通过。
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

from app import rules

from .helpers import create_round, demo_commitment, make_app, replace

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFIER = os.path.join(ROOT_DIR, "verify", "verify.py")


class SequentialFlowTest(unittest.TestCase):
    def setUp(self):
        self.app, self.db_path = make_app()
        self.client = self.app.test_client()
        self.seed, self.commitment = demo_commitment()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.remove(path)

    def api(self, method, path, **kw):
        return self.client.open(path, method=method, **kw)

    def get_round(self, rid):
        resp = self.api("GET", f"/api/rounds/{rid}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()["round"]

    def test_readme_flow_and_verification(self):
        # 1) 创建（承诺即 README 演示种子对应的固定承诺）
        rid = create_round(
            self.client,
            title="命令行演示", prize_count=3, commitment=self.commitment,
        )

        # 2) 名单：新增、删除、查重、整体替换
        r = self.api(
            "POST", f"/api/rounds/{rid}/participants", json={"id": "A001"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()["participants"]), 1)

        r = self.api(
            "POST", f"/api/rounds/{rid}/participants", json={"id": " A002 "}
        )  # 首尾空白自动去除
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            [p["id"] for p in r.get_json()["participants"]], ["A001", "A002"]
        )

        r = self.api(
            "POST", f"/api/rounds/{rid}/participants", json={"id": "A001"}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"], "编号已存在：A001")

        r = self.api(
            "DELETE", f"/api/rounds/{rid}/participants/A002"
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            [p["id"] for p in r.get_json()["participants"]], ["A001"]
        )

        r = self.api(
            "PUT", f"/api/rounds/{rid}/participants",
            json={"participants": ["A001", "A002", "A003", "A004", "A005"]},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["count"], 5)

        # 重复编号被拒绝，且名单不被破坏
        r = self.api(
            "PUT", f"/api/rounds/{rid}/participants",
            json={"participants": ["A001", "A001"]},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "编号重复：A001")
        self.assertEqual(
            len(self.get_round(rid)["participants"]), 5
        )

        # 非字符串数组 400
        r = self.api(
            "PUT", f"/api/rounds/{rid}/participants",
            json={"participants": ["A001", 2]},
        )
        self.assertEqual(r.status_code, 400)

        # 3) 人数不足不能封存（临时替换成 2 人）
        replace(self.client, rid, ["A001", "A002"])
        r = self.api("POST", f"/api/rounds/{rid}/seal")
        self.assertEqual(r.status_code, 400)
        self.assertIn("人数不足", r.get_json()["error"])
        self.assertEqual(self.get_round(rid)["status"], "open")

        # 恢复 5 人后封存；摘要固定值与 README 一致
        replace(
            self.client, rid,
            ["A001", "A002", "A003", "A004", "A005"],
        )
        r = self.api("POST", f"/api/rounds/{rid}/seal")
        self.assertEqual(r.status_code, 200)
        sealed = r.get_json()["round"]
        self.assertEqual(sealed["status"], "sealed")
        self.assertEqual(sealed["participantCount"], 5)
        self.assertEqual(
            sealed["listDigest"],
            "f798b141dcaca51093500b25f0c442bc95f72343dc84cf42e26bdc629aa2c648",
        )
        self.assertEqual(
            sealed["snapshot"],
            "A001\nA002\nA003\nA004\nA005",
        )

        # 重复封存：返回既有快照（alreadySealed，摘要不变）
        r = self.api("POST", f"/api/rounds/{rid}/seal")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["alreadySealed"])
        self.assertEqual(
            body["round"]["listDigest"], sealed["listDigest"]
        )

        # 封存后增删/替换一律 409，参与者表不变
        for method, path, payload in [
            ("POST", f"/api/rounds/{rid}/participants", {"id": "HACKER"}),
            ("DELETE", f"/api/rounds/{rid}/participants/A001", None),
            ("PUT", f"/api/rounds/{rid}/participants",
             {"participants": ["X"]}),
        ]:
            r = self.api(method, path, json=payload) if payload is not None \
                else self.api(method, path)
            self.assertEqual(r.status_code, 409, (method, path))
            self.assertEqual(
                r.get_json()["error"], "名单已封存，此后拒绝任何修改"
            )
        self.assertEqual(
            [p["id"] for p in self.get_round(rid)["participants"]],
            ["A001", "A002", "A003", "A004", "A005"],
        )

        # 4a) 错误种子：保持封存，只累加失败计数
        r = self.api(
            "POST", f"/api/rounds/{rid}/reveal", json={"seed": "f" * 64}
        )
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["commitMismatch"])
        self.assertFalse(body["ok"])
        self.assertEqual(body["round"]["status"], "sealed")
        self.assertEqual(body["round"]["failedAttempts"], 1)

        # 4b) 正确种子揭晓：结果即 README 固定值
        r = self.api(
            "POST", f"/api/rounds/{rid}/reveal", json={"seed": self.seed}
        )
        self.assertEqual(r.status_code, 200)
        revealed = r.get_json()["round"]
        self.assertEqual(revealed["status"], "revealed")
        self.assertEqual(revealed["winners"], ["A003", "A005", "A001"])

        # 4c) 重复揭晓：不重抽
        r = self.api(
            "POST", f"/api/rounds/{rid}/reveal", json={"seed": self.seed}
        )
        body = r.get_json()
        self.assertTrue(body["alreadyRevealed"])
        self.assertEqual(
            body["round"]["winners"], ["A003", "A005", "A001"]
        )

        # 揭晓后再封存被拒
        r = self.api("POST", f"/api/rounds/{rid}/seal")
        self.assertEqual(r.status_code, 409)

        # 5) 核验包：JSON 与 ZIP 均能被独立 verify.py 核验通过
        pkg = self._export_package_json(rid)
        self.assertEqual(pkg["ruleId"], rules.RULE_ID)
        self.assertEqual(pkg["participantCount"], 5)
        self.assertEqual(
            pkg["participants"],
            ["A001", "A002", "A003", "A004", "A005"],
        )
        self._run_verifier_on_zip(rid)

    def test_sealed_package_verifies_before_reveal(self):
        """封存后、揭晓前导出的核验包：无种子，独立核验通过。"""
        rid = create_round(self.client, commitment=self.commitment)
        replace(self.client, rid, ["A001", "A002", "A003"])
        r = self.api("POST", f"/api/rounds/{rid}/seal")
        self.assertEqual(r.status_code, 200)

        r = self.api("GET", f"/api/rounds/{rid}/package.json")
        self.assertEqual(r.status_code, 200)
        pkg = json.loads(r.get_data(as_text=True))
        self.assertIsNone(pkg["round"]["seed"])
        self.assertIsNone(pkg["winners"])

        with tempfile.TemporaryDirectory() as tmp:
            pkg_path = os.path.join(tmp, "package.json")
            with open(pkg_path, "wb") as f:
                f.write(r.get_data())
            proc = subprocess.run(
                [sys.executable, VERIFIER, pkg_path],
                capture_output=True, text=True,
            )
            self.assertEqual(
                proc.returncode, 0,
                f"核验未通过：\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
            )
            self.assertIn("封存（未揭晓）状态", proc.stdout)

    # ------------------------------------------------ 核验包辅助

    def _export_package_json(self, rid):
        r = self.api("GET", f"/api/rounds/{rid}/package.json")
        self.assertEqual(r.status_code, 200)
        return json.loads(r.get_data(as_text=True))

    def _run_verifier_on_zip(self, rid):
        r = self.api("GET", f"/api/rounds/{rid}/package.zip")
        self.assertEqual(r.status_code, 200)
        data = r.get_data()
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(data)))

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "pkg.zip")
            with open(zip_path, "wb") as f:
                f.write(data)
            pkg_path = os.path.join(tmp, "package.json")
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                self.assertTrue(
                    any(n.endswith("verify.py") for n in names)
                )
                self.assertTrue(
                    any(n.endswith("RULES.md") for n in names)
                )
                pkg_name = next(
                    n for n in names if n.endswith("package.json")
                )
                with zf.open(pkg_name) as src, open(pkg_path, "wb") as dst:
                    dst.write(src.read())

            with open(pkg_path, encoding="utf-8") as f:
                pkg = json.loads(f.read())
            proc = subprocess.run(
                [
                    sys.executable, VERIFIER, pkg_path,
                    "--commitment", pkg["round"]["commitment"],
                    "--list-digest", pkg["round"]["listDigest"],
                ],
                capture_output=True, text=True,
            )
            self.assertEqual(
                proc.returncode, 0,
                f"核验未通过：\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
            )
            self.assertIn("全部核验通过", proc.stdout)


if __name__ == "__main__":
    unittest.main()
