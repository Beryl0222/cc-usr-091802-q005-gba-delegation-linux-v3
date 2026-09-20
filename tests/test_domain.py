"""端到端领域测试：申报 → 候选 → 确认锁定 → 签到 → 加时/故障 → 改排，
以及授权最小可见、损失双确、通知回执。
"""

import json
import unittest
from pathlib import Path

from app import disruptions, lifecycle, planning, registry, views
from app.errors import Conflict, DomainError
from app.notify import acknowledge
from app.seed import load_seed
from app.store import Store
from app.timeutil import parse_ts

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample.json"
T0 = "2026-09-12T08:30:00+08:00"


def seeded():
    store = Store()
    load_seed(store, json.loads(FIXTURE.read_text(encoding="utf-8")), clock=T0)
    return store


class CandidatePlanningTest(unittest.TestCase):
    def setUp(self):
        self.store = seeded()

    def test_post_game_window_and_hard_game_constraint(self):
        # 香港队 day1 比赛 10:00-12:00；接驳候选只能落在赛后恢复窗口
        result = planning.build_candidates(self.store, "hong-kong", {
            "legs": [{"kind": "fleet", "from": "venue-main", "to": "campus-1",
                      "headcount": 5}]
        }, lambda: T0)
        self.assertTrue(result["feasible"])
        first = result["legs"][0]["options"][0]
        self.assertEqual(first["slot_id"], "slot-bus-d1-noon")
        self.assertGreaterEqual(first["start"], "2026-09-12T12:15")

    def test_capacity_language_accessibility_and_reasons(self):
        result = planning.build_candidates(self.store, "hong-kong", {
            "legs": [
                # 企业无葡语翻译
                {"kind": "industry", "headcount": 5, "duration_min": 60,
                 "languages": ["pt"]},
                # 企业容量 12，13 人超容
                {"kind": "industry", "headcount": 13, "duration_min": 60},
                # 正常需求：校园 120 分钟，英语翻译齐备
                {"kind": "campus", "headcount": 5, "duration_min": 120,
                 "languages": ["en"]},
            ]
        }, lambda: T0)
        self.assertFalse(result["feasible"])
        industry_rejects = [
            r for leg in result["legs"][:2] for r in leg["rejected"]
            if r["offer_id"] == "off-industry"
        ]
        self.assertTrue(any("葡" in " ".join(r["reasons"]) or "pt" in " ".join(r["reasons"])
                            for r in industry_rejects))
        self.assertTrue(any("容量不足" in " ".join(r["reasons"])
                            for r in industry_rejects))
        campus = result["legs"][2]
        self.assertTrue(campus["options"])
        self.assertEqual(campus["options"][0]["start"], "2026-09-12T13:00+08:00")

    def test_past_confirm_by_is_flagged_not_hidden(self):
        # 加时后的改排由双方重新确认；过确认点只做标记，不消除候选
        result = planning.build_candidates(self.store, "hong-kong", {
            "legs": [{"kind": "campus", "headcount": 5, "duration_min": 60}]
        }, lambda: "2026-09-12T13:00:00+08:00")
        opt = result["legs"][0]["options"][0]
        self.assertTrue(opt["confirm_by_passed"])


class ConfirmationLockingTest(unittest.TestCase):
    def setUp(self):
        self.store = seeded()
        self.itin = lifecycle.create_itinerary(self.store, "hong-kong", {
            "requests": [
                {"kind": "fleet", "from": "venue-main", "to": "campus-1",
                 "headcount": 5},
                {"kind": "campus", "headcount": 5, "duration_min": 120},
                {"kind": "market", "headcount": 5, "duration_min": 60},
            ],
            "selections": [
                {"kind": "fleet", "slot_id": "slot-bus-d1-noon",
                 "start": "2026-09-12T12:15", "headcount": 5},
                {"kind": "campus", "slot_id": "slot-campus-d1",
                 "start": "2026-09-12T13:00", "headcount": 5, "duration_min": 120},
                {"kind": "market", "slot_id": "slot-market-d1",
                 "start": "2026-09-12T16:00", "headcount": 5, "duration_min": 60},
            ],
        }, lambda: T0)
        self.legs = {self.store.legs[l]["kind"]: self.store.legs[l]
                     for l in self.itin["legs"]}

    def confirm(self, leg, *actors):
        for a in actors:
            lifecycle.confirm_leg(self.store, leg["id"], a, lambda: T0)

    def test_resource_locks_only_after_all_confirm(self):
        fleet = self.legs["fleet"]
        slot = self.store.offers["off-fleet-venue-campus"]["slots"][0]
        self.confirm(fleet, "team:hong-kong")
        self.assertEqual(fleet["status"], "proposed")
        self.assertFalse(fleet["locked"])
        self.assertEqual(registry.slot_remaining(self.store, slot), 20)
        self.confirm(fleet, "party:fleet-1")
        self.assertEqual(fleet["status"], "confirmed")
        self.assertTrue(fleet["locked"])
        self.assertEqual(registry.slot_remaining(self.store, slot), 15)

    def test_capacity_cannot_be_oversubscribed(self):
        campus = self.legs["campus"]
        self.confirm(campus, "team:hong-kong", "party:campus-1")
        # 粤东 25 人恰好填满 30 人容量
        ge = lifecycle.create_itinerary(self.store, "guangdong-east", {
            "requests": [{"kind": "campus", "headcount": 25, "duration_min": 60}],
            "selections": [{"kind": "campus", "slot_id": "slot-campus-d1",
                            "start": "2026-09-12T13:00", "headcount": 25,
                            "duration_min": 60}],
        }, lambda: T0)
        ge_leg = self.store.legs[ge["legs"][0]]
        self.confirm(ge_leg, "team:guangdong-east", "party:campus-1")
        # 澳门再排 5 人：候选中该时段已无容量
        result = planning.build_candidates(self.store, "macao", {
            "legs": [{"kind": "campus", "headcount": 5, "duration_min": 60}]
        }, lambda: T0)
        self.assertTrue(all(
            "容量不足" in " ".join(r["reasons"])
            for r in result["legs"][0]["rejected"]
            if r["slot_id"] == "slot-campus-d1"))

    def test_cancel_before_departure_releases_capacity(self):
        market = self.legs["market"]
        self.confirm(market, "team:hong-kong", "party:market-1")
        slot = self.store.offers["off-market"]["slots"][0]
        self.assertEqual(registry.slot_remaining(self.store, slot), 55)
        lifecycle.cancel_leg(self.store, market["id"], "队伍申请取消", lambda: T0)
        self.assertEqual(registry.slot_remaining(self.store, slot), 60)
        self.assertEqual(market["status"], "cancelled")


class AttendanceTest(unittest.TestCase):
    def setUp(self):
        self.store = seeded()
        itin = lifecycle.create_itinerary(self.store, "hong-kong", {
            "requests": [
                {"kind": "campus", "headcount": 5, "duration_min": 60},
                {"kind": "market", "headcount": 5, "duration_min": 60},
            ],
            "selections": [
                {"kind": "campus", "slot_id": "slot-campus-d1",
                 "start": "2026-09-12T13:00", "headcount": 5, "duration_min": 60},
                {"kind": "market", "slot_id": "slot-market-d1",
                 "start": "2026-09-12T16:00", "headcount": 5, "duration_min": 60},
            ],
        }, lambda: T0)
        self.campus, self.market = [self.store.legs[l] for l in itin["legs"]]
        for leg in (self.campus, self.market):
            lifecycle.confirm_leg(self.store, leg["id"], "team:hong-kong",
                                  lambda: T0)
            lifecycle.confirm_leg(self.store, leg["id"],
                                  f"party:{leg['party_id']}", lambda: T0)

    def test_duplicate_badge_scanned_once(self):
        res = lifecycle.check_in(self.store, self.campus["id"], [
            {"badge": f"B-HK-0{i}"} for i in range(1, 6)
        ] + [{"badge": "B-HK-01", "channel": "manual"}], lambda: "2026-09-12T13:05")
        self.assertEqual(res["unique_count"], 5)
        self.assertEqual(len(res["duplicates"]), 1)
        self.assertEqual(self.campus["checkin_scans"], 6)
        self.assertEqual(self.campus["status"], "in_progress")

    def test_team_totals_deduplicate_across_legs(self):
        lifecycle.check_in(self.store, self.campus["id"],
                           [{"badge": f"B-HK-0{i}"} for i in range(1, 6)],
                           lambda: "2026-09-12T13:05")
        lifecycle.check_in(self.store, self.market["id"],
                           [{"badge": f"B-HK-0{i}"} for i in range(1, 4)],
                           lambda: "2026-09-12T16:05")
        summary = lifecycle.attendance_summary(self.store, team_id="hong-kong")
        self.assertEqual(sum(l["checkins"] for l in summary["legs"]), 8)
        self.assertEqual(summary["total_unique_persons"], 5)


class OvertimeRescheduleTest(unittest.TestCase):
    def setUp(self):
        self.store = seeded()
        itin = lifecycle.create_itinerary(self.store, "hong-kong", {
            "requests": [
                {"kind": "fleet", "from": "venue-main", "to": "campus-1",
                 "headcount": 5},
                {"kind": "campus", "headcount": 5, "duration_min": 120},
            ],
            "selections": [
                {"kind": "fleet", "slot_id": "slot-bus-d1-noon",
                 "start": "2026-09-12T12:15", "headcount": 5},
                {"kind": "campus", "slot_id": "slot-campus-d1",
                 "start": "2026-09-12T13:00", "headcount": 5, "duration_min": 120},
            ],
        }, lambda: T0)
        self.fleet, self.campus = [self.store.legs[l] for l in itin["legs"]]
        for leg in (self.fleet, self.campus):
            lifecycle.confirm_leg(self.store, leg["id"], "team:hong-kong",
                                  lambda: T0)
            lifecycle.confirm_leg(self.store, leg["id"],
                                  f"party:{leg['party_id']}", lambda: T0)
        # 车辆 12:20 发车，人员已在途
        lifecycle.depart(self.store, self.fleet["id"],
                         lambda: "2026-09-12T12:20:00+08:00")

    def test_overtime_impact_protects_enroute_and_shows_options(self):
        d = disruptions.register_overtime(self.store, {
            "game_id": "game-1",
            "actual_end": "2026-09-12T13:30:00+08:00",
        }, lambda: "2026-09-12T13:31:00+08:00")
        snap = disruptions.impact_snapshot(self.store, d["id"],
                                           lambda: "2026-09-12T13:31:00+08:00")
        by_id = {l["leg_id"]: l for l in snap["affected_legs"]}
        self.assertIn(self.fleet["id"], by_id)
        self.assertIn(self.campus["id"], by_id)

        moving = by_id[self.fleet["id"]]
        self.assertTrue(moving["immutable"])
        self.assertEqual(moving["protected_persons"], 5)  # 在途车辆按发车名单保护
        self.assertNotIn("reschedule_options", moving)

        waiting = by_id[self.campus["id"]]
        self.assertFalse(waiting["immutable"])
        self.assertTrue(waiting["reschedule_options"])
        earliest = waiting["reschedule_options"][0]
        self.assertGreaterEqual(parse_ts(earliest["start"]),
                                parse_ts("2026-09-12T13:45+08:00"))

    def test_reschedule_skips_immutable_and_reconfirms_new_leg(self):
        d = disruptions.register_overtime(self.store, {
            "game_id": "game-1",
            "actual_end": "2026-09-12T13:30:00+08:00",
        }, lambda: "2026-09-12T13:31:00+08:00")
        results = disruptions.reschedule(self.store, d["id"], [
            {"leg_id": self.fleet["id"], "slot_id": "slot-bus-d1-noon"},
            {"leg_id": self.campus["id"], "slot_id": "slot-campus-d1",
             "start": "2026-09-12T13:45"},
        ], lambda: "2026-09-12T13:32:00+08:00")
        self.assertEqual(len(results["skipped"]), 1)
        self.assertIn("不得强行改派", results["skipped"][0]["reason"])
        self.assertEqual(len(results["replaced"]), 1)

        # 旧环节释放容量、标记被替换；在途环节原封不动
        self.assertEqual(self.campus["status"], "rescheduled")
        self.assertFalse(self.campus["locked"])
        self.assertEqual(self.fleet["status"], "departed")
        slot = self.store.offers["off-campus"]["slots"][0]
        self.assertEqual(registry.slot_remaining(self.store, slot), 30)

        new_id = self.campus["replaced_by"]
        new_leg = self.store.legs[new_id]
        self.assertEqual(new_leg["window"]["start"], "2026-09-12T13:45+08:00")
        self.assertEqual(new_leg["status"], "proposed")
        self.assertFalse(new_leg["locked"])  # 改排后必须重新确认
        lifecycle.confirm_leg(self.store, new_id, "team:hong-kong",
                              lambda: "2026-09-12T13:33:00+08:00")
        lifecycle.confirm_leg(self.store, new_id, "party:campus-1",
                              lambda: "2026-09-12T13:33:00+08:00")
        self.assertEqual(new_leg["status"], "confirmed")
        self.assertEqual(registry.slot_remaining(self.store, slot), 25)

    def test_immutable_leg_cannot_be_cancelled(self):
        with self.assertRaises(Conflict) as ctx:
            lifecycle.cancel_leg(self.store, self.fleet["id"], "误操作",
                                 lambda: T0)
        self.assertEqual(ctx.exception.code, "leg_immutable")

    def test_receipts_visible_in_impact(self):
        d = disruptions.register_overtime(self.store, {
            "game_id": "game-1",
            "actual_end": "2026-09-12T13:30:00+08:00",
        }, lambda: "2026-09-12T13:31:00+08:00")
        snap = disruptions.impact_snapshot(self.store, d["id"],
                                           lambda: "2026-09-12T13:31:00+08:00")
        parties = {r["party_id"] for r in snap["receipts"]}
        self.assertIn("fleet-1", parties)
        self.assertIn("campus-1", parties)
        self.assertTrue(all(r["received"] is False for r in snap["receipts"]))
        first = snap["receipts"][0]
        acknowledge(self.store, first["notif_id"],
                    lambda: "2026-09-12T13:35:00+08:00")
        snap2 = disruptions.impact_snapshot(self.store, d["id"],
                                            lambda: "2026-09-12T13:35:00+08:00")
        acked = [r for r in snap2["receipts"] if r["received"]]
        self.assertEqual(len(acked), 1)


class VehicleFaultTest(unittest.TestCase):
    def setUp(self):
        self.store = seeded()
        itin = lifecycle.create_itinerary(self.store, "hong-kong", {
            "requests": [{"kind": "fleet", "from": "venue-main",
                          "to": "campus-1", "headcount": 5}],
            "selections": [{"kind": "fleet", "slot_id": "slot-bus-d1-noon",
                            "start": "2026-09-12T12:15", "headcount": 5}],
        }, lambda: T0)
        self.leg = self.store.legs[itin["legs"][0]]
        lifecycle.confirm_leg(self.store, self.leg["id"], "team:hong-kong",
                              lambda: T0)
        lifecycle.confirm_leg(self.store, self.leg["id"], "party:fleet-1",
                              lambda: T0)
        registry.register_party(self.store, {
            "id": "fleet-3", "type": "fleet", "name": "应急运力"})
        registry.register_offer(self.store, {
            "id": "off-fleet-spare", "party_id": "fleet-3", "type": "fleet",
            "name": "应急场馆→校园", "vehicle_id": "bus-C21",
            "accessibility": ["wheelchair"],
            "route": {"from": "venue-main", "to": "campus-1",
                      "duration_min": 30},
            "confirm_by": "2026-09-12T13:30:00+08:00",
            "slots": [{"id": "slot-spare",
                       "start": "2026-09-12T12:30:00+08:00",
                       "end": "2026-09-12T14:30:00+08:00", "capacity": 20}],
        })

    def test_reschedule_options_require_accessibility(self):
        # 香港队有轮椅队员：未声明无障碍的应急运力不得成为改排候选
        registry.register_offer(self.store, {
            "id": "off-fleet-steps", "party_id": "fleet-3", "type": "fleet",
            "name": "无踏板应急车", "vehicle_id": "bus-D31",
            "route": {"from": "venue-main", "to": "campus-1",
                      "duration_min": 30},
            "confirm_by": "2026-09-12T13:30:00+08:00",
            "slots": [{"id": "slot-steps",
                       "start": "2026-09-12T12:30:00+08:00",
                       "end": "2026-09-12T14:30:00+08:00", "capacity": 20}],
        })
        d = disruptions.register_vehicle_fault(self.store, {
            "vehicle_id": "bus-A12",
            "unavailable_until": "2026-09-12T14:00:00+08:00",
        }, lambda: "2026-09-12T11:50:00+08:00")
        snap = disruptions.impact_snapshot(self.store, d["id"],
                                           lambda: "2026-09-12T11:50:00+08:00")
        slot_ids = {o["slot_id"] for o in snap["affected_legs"][0]["reschedule_options"]}
        self.assertIn("slot-spare", slot_ids)
        self.assertNotIn("slot-steps", slot_ids)

    def test_fault_suspends_offer_and_reschedules_to_spare(self):
        d = disruptions.register_vehicle_fault(self.store, {
            "vehicle_id": "bus-A12",
            "unavailable_until": "2026-09-12T14:00:00+08:00",
        }, lambda: "2026-09-12T11:50:00+08:00")
        self.assertEqual(self.store.offers["off-fleet-venue-campus"]["status"],
                         "suspended")
        snap = disruptions.impact_snapshot(self.store, d["id"],
                                           lambda: "2026-09-12T11:50:00+08:00")
        self.assertEqual(len(snap["affected_legs"]), 1)
        opts = snap["affected_legs"][0]["reschedule_options"]
        self.assertTrue(any(o["slot_id"] == "slot-spare" for o in opts))

        results = disruptions.reschedule(self.store, d["id"], [
            {"leg_id": self.leg["id"], "slot_id": "slot-spare",
             "start": "2026-09-12T12:30"},
        ], lambda: "2026-09-12T11:51:00+08:00")
        self.assertEqual(results["replaced"][0]["replacement_id"]
                         is not None, True)
        new_leg = self.store.legs[self.leg["replaced_by"]]
        self.assertEqual(new_leg["party_id"], "fleet-3")
        self.assertEqual(self.leg["status"], "rescheduled")
        # 原运力恢复后可重新接单
        disruptions.resume_fleet_offer(
            self.store, "off-fleet-venue-campus",
            lambda: "2026-09-12T14:05:00+08:00")
        self.assertEqual(self.store.offers["off-fleet-venue-campus"]["status"],
                         "active")


class ConsentAndVisibilityTest(unittest.TestCase):
    def setUp(self):
        self.store = seeded()
        itin = lifecycle.create_itinerary(self.store, "hong-kong", {
            "requests": [{"kind": "campus", "headcount": 5,
                          "duration_min": 60, "languages": ["en"]}],
            "selections": [{"kind": "campus", "slot_id": "slot-campus-d1",
                            "start": "2026-09-12T13:00", "headcount": 5,
                            "duration_min": 60}],
        }, lambda: T0)
        self.leg = self.store.legs[itin["legs"][0]]

    def test_host_sees_only_service_necessary_fields(self):
        v = views.leg_view(self.store, self.leg, "party:campus-1")
        self.assertIn("roster", v)
        person = next(p for p in v["roster"] if p["badge"] == "B-HK-01")
        # 校园有 HK 的餐饮与影像授权
        self.assertEqual(person["meal_need"], ["halal"])
        self.assertEqual(person["image"], {"publicity": "ok"})
        # 已授权用途下，接待方需要知道谁拒绝，才能遵守其意愿
        no_image = next(p for p in v["roster"] if p["badge"] == "B-HK-02")
        self.assertEqual(no_image["image"], {"publicity": "no"})

    def test_missing_consent_hides_sensitive_fields(self):
        # 市集未获香港队餐饮/影像授权
        v = views.leg_view(self.store, self.leg, "party:market-1")
        self.assertIsNone(v)  # 不是该环节接待方，整条不可见

    def test_revocation_takes_effect_immediately(self):
        grant = next(g for g in self.store.consents.values()
                     if g["team_id"] == "hong-kong" and g["scope"] == "meal")
        registry.revoke_consent(self.store, grant["id"],
                                lambda: "2026-09-12T09:00:00+08:00")
        v = views.leg_view(self.store, self.leg, "party:campus-1")
        self.assertTrue(all("meal_need" not in p for p in v["roster"]))

    def test_fleet_view_has_no_identities_meal_or_image(self):
        itin = lifecycle.create_itinerary(self.store, "hong-kong", {
            "requests": [{"kind": "fleet", "from": "venue-main",
                          "to": "campus-1", "headcount": 5}],
            "selections": [{"kind": "fleet", "slot_id": "slot-bus-d1-noon",
                            "start": "2026-09-12T12:15", "headcount": 5}],
        }, lambda: T0)
        fleet_leg = self.store.legs[itin["legs"][0]]
        v = views.leg_view(self.store, fleet_leg, "party:fleet-1")
        self.assertEqual(v["roster"], [])
        self.assertEqual(v["accessibility_summary"], {"wheelchair": 1})
        self.assertEqual(v["route"]["from"], "venue-main")
        self.assertNotIn("meal_need", json.dumps(v, ensure_ascii=False))
        self.assertNotIn("image", json.dumps(v, ensure_ascii=False))
        self.assertNotIn("badge", json.dumps(v, ensure_ascii=False))


class LossRecordTest(unittest.TestCase):
    def test_loss_needs_both_confirmations_and_never_settles(self):
        store = seeded()
        itin = lifecycle.create_itinerary(store, "hong-kong", {
            "requests": [{"kind": "market", "headcount": 5,
                          "duration_min": 60}],
            "selections": [{"kind": "market", "slot_id": "slot-market-d1",
                            "start": "2026-09-12T16:00", "headcount": 5,
                            "duration_min": 60}],
        }, lambda: T0)
        leg = store.legs[itin["legs"][0]]
        lifecycle.confirm_leg(store, leg["id"], "team:hong-kong", lambda: T0)
        lifecycle.confirm_leg(store, leg["id"], "party:market-1", lambda: T0)
        lifecycle.cancel_leg(store, leg["id"], "加时取消", lambda: T0)

        loss = lifecycle.record_loss(store, {
            "leg_id": leg["id"], "type": "material",
            "description": "已制备的主题摊位物料", "amount": "1200.00",
        }, lambda: T0)
        self.assertEqual(loss["status"], "pending_confirmation")
        self.assertFalse(loss["settled"])  # 系统绝不自动扣款
        lifecycle.confirm_loss(store, loss["id"], "party:market-1", lambda: T0)
        self.assertEqual(loss["status"], "pending_confirmation")
        lifecycle.confirm_loss(store, loss["id"], "office", lambda: T0)
        self.assertEqual(loss["status"], "confirmed_record")
        self.assertFalse(loss["settled"])

    def test_vehicle_empty_run_recorded_not_charged(self):
        store = seeded()
        itin = lifecycle.create_itinerary(store, "hong-kong", {
            "requests": [{"kind": "fleet", "from": "venue-main",
                          "to": "campus-1", "headcount": 5}],
            "selections": [{"kind": "fleet", "slot_id": "slot-bus-d1-noon",
                            "start": "2026-09-12T12:15", "headcount": 5}],
        }, lambda: T0)
        leg = store.legs[itin["legs"][0]]
        lifecycle.confirm_leg(store, leg["id"], "team:hong-kong", lambda: T0)
        lifecycle.confirm_leg(store, leg["id"], "party:fleet-1", lambda: T0)
        lifecycle.cancel_leg(store, leg["id"], "接驳延误取消", lambda: T0)
        loss = lifecycle.record_loss(store, {
            "leg_id": leg["id"], "type": "vehicle_empty",
            "description": "车辆空驶 22 公里", "amount": "300.00",
        }, lambda: T0)
        self.assertEqual(loss["type"], "vehicle_empty")
        self.assertFalse(loss["settled"])


if __name__ == "__main__":
    unittest.main()
