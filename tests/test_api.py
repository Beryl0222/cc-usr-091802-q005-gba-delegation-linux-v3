"""HTTP API 端到端测试（真实端口 + JSON）。"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from app import DispatchService, Store
from app.api import build_server
from app.bootstrap import load_seed


class ApiClient:
    def __init__(self, base):
        self.base = base

    def call(self, method, path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = DispatchService(Store())
        load_seed(cls.service)
        cls.service.set_clock("2026-09-12T10:00+08:00")
        cls.server = build_server(cls.service, 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.api = ApiClient(f"http://127.0.0.1:{port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_01_health_and_seed(self):
        status, body = self.api.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "gba-delegation-control")
        status, body = self.api.call("GET", "/competitions")
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 2)

    def test_02_full_flow(self):
        # 生成候选
        status, body = self.api.call(
            "POST", "/teams/hong-kong/candidates",
            {"events": [
                {"kind": "campus", "duration_minutes": 60, "languages": ["yue"]},
                {"kind": "market", "duration_minutes": 60, "languages": ["yue"]}],
             "origin_zone": "stadium", "cap": 1})
        self.assertEqual(status, 201, body)
        plan = body["plans"][0]
        pid = plan["id"]
        self.assertTrue(plan["items"])

        # 三方确认
        for party in ("office", "hong-kong", "dg-university",
                      "market-organizer", "bay-fleet"):
            status, body = self.api.call(
                "POST", f"/plans/{pid}/confirmations", {"party": party})
            self.assertEqual(status, 200, body)

        # 锁定
        status, body = self.api.call("POST", f"/plans/{pid}/lock", {})
        self.assertEqual(status, 200, body)
        self.assertTrue(all(i["status"] == "locked" for i in body["items"]))

        # 接驳出发
        shuttle = next(i for i in body["items"] if i["kind"] == "shuttle")
        status, _ = self.api.call("POST", f"/items/{shuttle['id']}/depart", {})
        self.assertEqual(status, 200)

        # 重复签到
        visit = next(i for i in body["items"] if i["kind"] == "visit")
        for cred, expect_dup in (("CRED-HK-001", False),
                                 ("CRED-HK-002", False),
                                 ("CRED-HK-001", True)):
            status, r = self.api.call(
                "POST", f"/items/{visit['id']}/checkins",
                {"credential_ref": cred})
            self.assertEqual(status, 201, r)
            self.assertEqual(r["duplicate"], expect_dup)

        status, summary = self.api.call(
            "GET", f"/checkins/summary?plan_id={pid}")
        self.assertEqual(summary["unique_headcount"], 2)

        # 加时
        self.service.set_clock("2026-09-12T17:35+08:00")
        status, inc = self.api.call(
            "POST", "/incidents",
            {"type": "overtime", "at": "2026-09-12T17:35+08:00",
             "delay_minutes": 60, "competition_id": "match-day1"})
        self.assertEqual(status, 201, inc)
        pimp = next(p for p in inc["impact"]["plans"] if p["plan_id"] == pid)
        self.assertTrue(pimp["pinned_items"])
        self.assertTrue(pimp["options"])

        # 应用一个与现状后缀不同的改排方案并重新确认锁定
        old_sig = [(i["kind"], i["ref_id"], i["start"], i["end"])
                   for i in plan["items"][2:]]
        option_index = next(
            k for k, o in enumerate(pimp["options"])
            if [(s["kind"], s["ref_id"], s["start"], s["end"])
                for s in o["summary"]] != old_sig)
        status, replanned = self.api.call(
            "POST", f"/plans/{pid}/replan",
            {"option_index": option_index})
        self.assertEqual(status, 200, replanned)
        parties = {"office", "hong-kong"}
        for it in replanned["items"]:
            if it["status"] == "proposed":
                parties.update(it["required_parties"])
        for party in parties:
            self.api.call("POST", f"/plans/{pid}/confirmations",
                          {"party": party})
        status, relocked = self.api.call("POST", f"/plans/{pid}/lock", {})
        self.assertEqual(status, 200, relocked)

        # 接待方最小可见视图
        status, manifest = self.api.call(
            "GET", f"/items/{visit['id']}/manifest?viewer=dg-university")
        self.assertEqual(status, 200)
        self.assertEqual(manifest["attendee_count"], 3)
        for row in manifest["attendees"]:
            self.assertNotIn("credential_ref", row)
            self.assertNotIn("dietary_tags", row)  # 非包餐看不到饮食

        # 办公室总览
        status, ov = self.api.call("GET", "/office/overview")
        self.assertEqual(status, 200)
        self.assertTrue(ov["notification_receipts"]["delivered"] > 0)

    def test_03_errors_are_json(self):
        status, body = self.api.call("GET", "/no-such-path")
        self.assertEqual(status, 404)
        self.assertIn("error", body)
        status, body = self.api.call(
            "POST", "/hosts/no-host/offerings",
            {"kind": "campus", "title": "x", "location": "x",
             "zone": "z", "windows": [], "capacity": 1,
             "confirm_deadline": "2026-09-12T12:00+08:00"})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
