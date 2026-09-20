"""HTTP 端到端测试：通过真实端口走完整 API，覆盖观看者视角与时间注入。"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from app.seed import load_seed
from app.server import Api, make_handler
from app.store import Store

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample.json"
T0 = "2026-09-12T08:30:00+08:00"


class ApiSession:
    def __init__(self, base):
        self.base = base

    def call(self, method, path, body=None, viewer=None, now=T0):
        data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        if viewer:
            req.add_header("X-Viewer", viewer)
        if now:
            req.add_header("X-Now", now)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def get(self, path, **kw):
        return self.call("GET", path, None, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body, **kw)


class HttpFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store = Store()
        load_seed(store, json.loads(FIXTURE.read_text(encoding="utf-8")), T0)
        cls.api = Api(store, clock=lambda: T0)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.api))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.http = ApiSession(f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_01_health(self):
        status, body = self.http.get("/health", now=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "gba-delegation-control")

    def test_02_full_flow_overtime_and_reschedule(self):
        http = self.http
        # 候选：香港队赛后接驳 + 校园交流
        status, cand = http.post("/teams/hong-kong/candidates", {
            "legs": [
                {"kind": "fleet", "from": "venue-main", "to": "campus-1",
                 "headcount": 5},
                {"kind": "campus", "headcount": 5, "duration_min": 120},
            ]
        })
        self.assertEqual(status, 200)
        self.assertTrue(cand["feasible"])

        status, itin = http.post("/teams/hong-kong/itineraries", {
            "requests": [
                {"kind": "fleet", "from": "venue-main", "to": "campus-1",
                 "headcount": 5},
                {"kind": "campus", "headcount": 5, "duration_min": 120},
            ],
            "selections": [
                {"kind": "fleet", "slot_id": "slot-bus-d1-noon",
                 "start": "2026-09-12T12:15+08:00", "headcount": 5},
                {"kind": "campus", "slot_id": "slot-campus-d1",
                 "start": "2026-09-12T13:00+08:00", "headcount": 5,
                 "duration_min": 120},
            ],
        })
        self.assertEqual(status, 200)
        iid = itin["id"]
        legs = {l["kind"]: l for l in itin["legs"]}

        # 双方确认
        for leg in legs.values():
            s, _ = http.post(f"/legs/{leg['id']}/confirm", {},
                             viewer="team:hong-kong")
            self.assertEqual(s, 200)
            s, _ = http.post(f"/legs/{leg['id']}/confirm", {},
                             viewer=f"party:{leg['party_id']}")
            self.assertEqual(s, 200)

        # 接驳车辆 12:20 发车（在途）；校园环节尚未出发
        s, _ = http.post(f"/legs/{legs['fleet']['id']}/depart", {},
                         now="2026-09-12T12:20+08:00")
        self.assertEqual(s, 200)

        # 加时到 13:30
        s, dis = http.post("/disruptions/overtime", {
            "game_id": "game-1", "actual_end": "2026-09-12T13:30+08:00",
        }, now="2026-09-12T13:31+08:00")
        self.assertEqual(s, 200)
        did = dis["id"]

        # 影响面：在途接驳受保护，校园环节给改排候选与回执状态
        s, snap = http.get(f"/disruptions/{did}/impact",
                           now="2026-09-12T13:31+08:00")
        self.assertEqual(s, 200)
        by_id = {l["leg_id"]: l for l in snap["affected_legs"]}
        self.assertTrue(by_id[legs["fleet"]["id"]]["immutable"])
        waiting = by_id[legs["campus"]["id"]]
        self.assertFalse(waiting["immutable"])
        self.assertTrue(waiting["reschedule_options"])
        unreceived = [r for r in snap["receipts"] if not r["received"]]
        self.assertEqual(len(unreceived), len(snap["receipts"]))

        # 办公室逐条回收回执
        s, _ = http.post(f"/notifications/{snap['receipts'][0]['notif_id']}/ack",
                         {}, now="2026-09-12T13:32+08:00")
        self.assertEqual(s, 200)

        # 改排：在途被跳过，校园换到 13:45
        s, res = http.post(f"/disruptions/{did}/reschedule", {
            "selections": [
                {"leg_id": legs["fleet"]["id"], "slot_id": "slot-bus-d1-noon"},
                {"leg_id": legs["campus"]["id"], "slot_id": "slot-campus-d1",
                 "start": "2026-09-12T13:45+08:00"},
            ]
        }, now="2026-09-12T13:33+08:00")
        self.assertEqual(s, 200)
        self.assertEqual(len(res["skipped"]), 1)
        self.assertEqual(len(res["replaced"]), 1)
        new_leg_id = res["replaced"][0]["replacement_id"]

        # 新环节须双方重新确认后才能签到
        s, _ = http.post(f"/legs/{new_leg_id}/confirm", {},
                         viewer="team:hong-kong", now="2026-09-12T13:34+08:00")
        self.assertEqual(s, 200)
        s, _ = http.post(f"/legs/{new_leg_id}/confirm", {},
                         viewer="party:campus-1", now="2026-09-12T13:34+08:00")
        self.assertEqual(s, 200)

        # 签到：5 人 + 1 次重复扫码
        s, ci = http.post(f"/legs/{new_leg_id}/checkin", {
            "records": [{"badge": f"B-HK-0{i}"} for i in range(1, 6)]
                       + [{"badge": "B-HK-01", "channel": "manual"}]
        }, now="2026-09-12T13:50+08:00")
        self.assertEqual(s, 200)
        self.assertEqual(ci["unique_count"], 5)
        self.assertEqual(len(ci["duplicates"]), 1)

        # 已签到环节不能再被改派/取消
        s, body = http.post(f"/legs/{new_leg_id}/cancel",
                            {"reason": "误操作"}, now="2026-09-12T13:51+08:00")
        self.assertEqual(s, 409)
        self.assertEqual(body["error"], "leg_immutable")

        # 汇总人数排除重复签到
        s, report = http.get("/attendance?team_id=hong-kong")
        self.assertEqual(s, 200)
        self.assertEqual(report["total_unique_persons"], 5)

    def test_03_viewer_minimum_visibility(self):
        http = self.http
        status, items = http.get("/itineraries", viewer="office")
        iid = items["itineraries"][0]["id"]
        # 企业不是该行程接待方：整单不可见
        s, _ = http.get(f"/itineraries/{iid}", viewer="party:industry-1")
        self.assertEqual(s, 403)
        # 校园接待方：可见签到标识，但无授权用途时拿不到影像资料
        s, view = http.get(f"/itineraries/{iid}", viewer="party:campus-1")
        self.assertEqual(s, 200)
        # 取仍在进行的校园环节（已改排的历史环节不再下发名单）
        leg = next(l for l in view["legs"]
                   if l["kind"] == "campus" and l["status"]
                   in ("confirmed", "departed", "in_progress"))
        self.assertTrue(all("badge" in p for p in leg["roster"]))

    def test_04_loss_double_confirmation(self):
        http = self.http
        # 自建一单市集行程并取消，以登记物料损失
        s, itin = http.post("/teams/hong-kong/itineraries", {
            "requests": [{"kind": "market", "headcount": 5,
                          "duration_min": 60}],
            "selections": [{"kind": "market", "slot_id": "slot-market-d1",
                            "start": "2026-09-12T16:00+08:00", "headcount": 5,
                            "duration_min": 60}],
        })
        self.assertEqual(s, 200)
        leg_id = itin["legs"][0]["id"]
        http.post(f"/legs/{leg_id}/confirm", {}, viewer="team:hong-kong")
        http.post(f"/legs/{leg_id}/confirm", {}, viewer="party:market-1")
        http.post(f"/legs/{leg_id}/cancel", {"reason": "加时取消"})
        s, loss = http.post("/losses", {
            "leg_id": leg_id, "type": "material",
            "description": "市集物料", "amount": "800.00",
        })
        self.assertEqual(s, 200)
        lid = loss["id"]
        s, _ = http.post(f"/losses/{lid}/confirm", {}, viewer="party:market-1")
        self.assertEqual(s, 200)
        s, body = http.post(f"/losses/{lid}/confirm", {}, viewer="office")
        self.assertEqual(s, 200)
        self.assertEqual(body["status"], "confirmed_record")
        self.assertFalse(body["settled"])


    def test_05_scoped_attendance_and_fleet_isolation(self):
        http = self.http
        # 校园接待方只能汇总自己环节的到场
        s, report = http.get("/attendance?team_id=hong-kong",
                             viewer="party:campus-1")
        self.assertEqual(s, 200)
        self.assertTrue(report["legs"])
        self.assertTrue(all(l["party_id"] == "campus-1" for l in report["legs"]))

        # 车队即使看到自己服务的行程，也只见接驳环节且无个人标识
        s, items = http.get("/itineraries", viewer="party:fleet-1")
        self.assertEqual(s, 200)
        blob = json.dumps(items, ensure_ascii=False)
        self.assertNotIn("badge", blob)
        self.assertNotIn("meal_need", blob)
        for it in items["itineraries"]:
            for l in it["legs"]:
                self.assertEqual(l["kind"], "fleet")
                self.assertIn("route", l)


if __name__ == "__main__":
    unittest.main()
