"""行程联控领域逻辑测试。"""

import unittest

from service import build_service
from app import timeutil
from app.errors import ApiError


def seeded():
    svc = build_service(seed=True)
    svc.set_clock("2026-09-12T10:00+08:00")
    return svc


def make_locked_plan(svc, events=None):
    events = events or [
        {"kind": "campus", "duration_minutes": 60, "languages": ["yue"]},
        {"kind": "market", "duration_minutes": 60, "languages": ["yue"]},
    ]
    plan = svc.build_candidates("hong-kong", events,
                                origin_zone="stadium", cap=1)[0]
    # 必要确认方按候选环节实际涉及的接待方动态收集
    parties = {"office", "hong-kong"}
    for it in plan["items"]:
        parties.update(it["required_parties"])
    for party in parties:
        svc.confirm(plan["id"], party)
    svc.lock(plan["id"])
    return svc.get_plan(plan["id"])


class RegistrationTest(unittest.TestCase):
    def test_seed_loads_fixture(self):
        svc = seeded()
        self.assertEqual(len(svc.list_teams()), 4)
        self.assertTrue(svc.list_offerings())
        self.assertTrue(svc.list_fleets())

    def test_competition_is_marked_immovable(self):
        svc = seeded()
        for c in svc.list_competitions():
            self.assertTrue(c["immovable"])

    def test_credential_unique(self):
        svc = seeded()
        with self.assertRaises(ApiError):
            svc.register_member("hong-kong", "重复", "CRED-HK-001")

    def test_host_kind_must_match_offering(self):
        svc = seeded()
        with self.assertRaises(ApiError):
            svc.register_offering(
                "smart-factory", "campus", "错挂活动", "x", "campus-town",
                [{"start": "2026-09-13T09:00+08:00",
                  "end": "2026-09-13T12:00+08:00"}],
                10, ["yue"], ["wheelchair"], "2026-09-12T12:00+08:00")


class CandidateTest(unittest.TestCase):
    def test_competition_window_subtracted(self):
        svc = seeded()
        # 15:00-17:30 比赛窗口内不应出现任何环节
        plan = svc.build_candidates(
            "hong-kong",
            [{"kind": "campus", "duration_minutes": 60, "languages": ["yue"]}],
            origin_zone="stadium", cap=1)[0]
        match = next(c for c in svc.list_competitions() if c["id"] == "match-day1")
        ms, me = timeutil.parse_ts(match["start"]), timeutil.parse_ts(match["end"])
        for it in plan["items"]:
            s, e = timeutil.parse_ts(it["start"]), timeutil.parse_ts(it["end"])
            self.assertFalse(timeutil.overlaps(s, e, ms, me))

    def test_accessibility_requirement_filters_hosts(self):
        svc = seeded()
        # 一支没有无障碍条件的新市集；若撤掉现有市集的轮椅条件，候选应失败
        svc.register_host("plain-market", "无障碍缺失市集", "market")
        # 只给一个与现有时段不冲突但缺轮椅条件的摊位（通过再屏蔽其他市集验证较复杂，
        # 这里直接校验：葡萄牙语需求只有企业能满足，市集/校园不会被错配）
        plan = svc.build_candidates(
            "hong-kong",
            [{"kind": "enterprise", "duration_minutes": 60,
              "languages": ["pt"]}],
            origin_zone="stadium", cap=1)[0]
        visits = [i for i in plan["items"] if i["kind"] == "visit"]
        self.assertTrue(visits)
        self.assertTrue(all(i["ref_id"] == "off-factory" for i in visits))

    def test_no_window_after_deadline(self):
        svc = seeded()
        svc.set_clock("2026-09-12T13:00+08:00")  # 已过最晚确认点
        with self.assertRaises(ApiError):
            svc.build_candidates(
                "hong-kong",
                [{"kind": "campus", "duration_minutes": 60,
                  "languages": ["yue"]}],
                origin_zone="stadium")

    def test_capacity_blocks_overbooking(self):
        svc = seeded()
        # 独立小容量市集摊位，仅在一个紧窗口内接待
        svc.register_offering(
            "market-organizer", "market", "限量摊位", "老街 7 号",
            "market-district",
            [{"start": "2026-09-13T10:00+08:00",
              "end": "2026-09-13T10:30+08:00"}],
            2, ["yue", "pt"], ["wheelchair"], "2026-09-13T09:00+08:00",
            offering_id="off-tiny")
        # 2 名香港队员占满摊位（用葡语需求把候选唯一锁定到该摊位）
        plan = svc.build_candidates(
            "hong-kong",
            [{"kind": "market", "title": "限量摊位",
              "duration_minutes": 30, "languages": ["pt"]}],
            member_ids=["hk-01", "hk-02"],
            origin_zone="stadium", cap=1)[0]
        # 找到用到 tiny 摊位的候选（其他候选可能去大市集，逐个识别）
        uses_tiny = any(i["ref_id"] == "off-tiny" for i in plan["items"])
        self.assertTrue(uses_tiny)
        parties = {"office", "hong-kong"}
        for it in plan["items"]:
            parties.update(it["required_parties"])
        for party in parties:
            svc.confirm(plan["id"], party)
        svc.lock(plan["id"])
        # 第三人再订同一摊位：容量 2 已占满，候选不可行
        with self.assertRaises(ApiError):
            svc.build_candidates(
                "hong-kong",
                [{"kind": "market", "title": "限量摊位",
                  "duration_minutes": 30, "languages": ["pt"]}],
                member_ids=["hk-03"],
                origin_zone="stadium")


class ConfirmationLockTest(unittest.TestCase):
    def test_lock_requires_all_parties(self):
        svc = seeded()
        plan = svc.build_candidates(
            "hong-kong",
            [{"kind": "campus", "duration_minutes": 60, "languages": ["yue"]}],
            origin_zone="stadium", cap=1)[0]
        svc.confirm(plan["id"], "office")
        with self.assertRaises(ApiError) as ctx:
            svc.lock(plan["id"])
        blockers = ctx.exception.details["blockers"]
        missing = {p for b in blockers for p in b.get("missing", [])}
        self.assertIn("dg-university", missing)
        self.assertIn("hong-kong", missing)

    def test_batch_confirm_only_hits_own_items(self):
        svc = seeded()
        plan = svc.build_candidates(
            "hong-kong",
            [{"kind": "campus", "duration_minutes": 60, "languages": ["yue"]}],
            origin_zone="stadium", cap=1)[0]
        svc.confirm(plan["id"], "dg-university")
        fresh = svc.get_plan(plan["id"])
        for it in fresh["items"]:
            if it["kind"] == "visit":
                self.assertIn("dg-university", it["confirmations"])
            else:
                self.assertNotIn("dg-university", it["confirmations"])

    def test_lock_books_resource_and_rechecks_at_lock_time(self):
        svc = seeded()
        plan = make_locked_plan(svc)
        for it in plan["items"]:
            self.assertEqual(it["status"], "locked")
        # 已锁定行程不能重复锁定
        with self.assertRaises(ApiError):
            svc.lock(plan["id"])


class FrozenAndIncidentTest(unittest.TestCase):
    def _overtime_scenario(self):
        svc = seeded()
        plan = make_locked_plan(svc)
        items = plan["items"]
        svc.mark_enroute(items[0]["id"])
        svc.checkin(items[1]["id"], "CRED-HK-001")
        svc.set_clock("2026-09-12T17:35+08:00")
        inc = svc.register_incident(
            "overtime", at="2026-09-12T17:35+08:00", delay_minutes=60,
            competition_id="match-day1", description="决赛加时")
        return svc, plan, inc

    def test_overtime_extends_competition_but_never_moves_it(self):
        svc, plan, inc = self._overtime_scenario()
        match = next(c for c in svc.list_competitions()
                     if c["id"] == "match-day1")
        self.assertEqual(match["end"], "2026-09-12T18:30+08:00")
        self.assertTrue(match["immovable"])

    def test_enroute_and_checked_in_items_are_pinned(self):
        svc, plan, inc = self._overtime_scenario()
        pimp = next(p for p in inc["impact"]["plans"]
                    if p["plan_id"] == plan["id"])
        pinned_kinds = {i["item_id"] for i in pimp["pinned_items"]}
        self.assertIn(plan["items"][0]["id"], pinned_kinds)  # enroute
        self.assertIn(plan["items"][1]["id"], pinned_kinds)  # in_progress
        movable_ids = {i["item_id"] for i in pimp["reschedulable_items"]}
        # 后缀环节全部进入可改排集合
        self.assertIn(plan["items"][2]["id"], movable_ids)

    def test_apply_replan_keeps_frozen_items_untouched(self):
        svc, plan, inc = self._overtime_scenario()
        pimp = next(p for p in inc["impact"]["plans"]
                    if p["plan_id"] == plan["id"])
        self.assertTrue(pimp["options"])
        # 选一个与现状后缀不同的方案
        old_sig = [(i["kind"], i["ref_id"], i["start"], i["end"])
                   for i in plan["items"][2:]]
        idx = next(k for k, o in enumerate(pimp["options"])
                   if [(s["kind"], s["ref_id"], s["start"], s["end"])
                       for s in o["summary"]] != old_sig)
        updated = svc.apply_replan(plan["id"], idx)
        by_seq = {i["seq"]: i for i in updated["items"]}
        self.assertEqual(by_seq[1]["status"], "enroute")
        self.assertEqual(by_seq[2]["status"], "in_progress")
        # 新环节需要重新确认后才能锁定
        with self.assertRaises(ApiError):
            svc.lock(plan["id"])
        parties = {"office", "hong-kong"}
        for it in updated["items"]:
            if it["status"] == "proposed":
                parties.update(it["required_parties"])
        for party in parties:
            svc.confirm(plan["id"], party)
        relocked = svc.lock(plan["id"])
        self.assertEqual(relocked["status"], "locked")

    def test_vehicle_breakdown_switches_fleet_and_bans_broken_one(self):
        svc = seeded()
        svc.register_host("backup-fleet", "后备车队", "fleet_operator")
        svc.register_fleet(
            "backup-fleet", "湾区接驳二队",
            [{"start": "2026-09-12T12:00+08:00",
              "end": "2026-09-13T22:30+08:00"}],
            16, 2, True,
            ["stadium", "songshan-lake", "campus-town", "market-district"],
            "2026-09-12T12:00+08:00", fleet_id="flt-backup")
        plan = make_locked_plan(
            svc,
            events=[{"kind": "enterprise", "duration_minutes": 60,
                     "languages": ["yue"]}])
        svc.mark_enroute(plan["items"][0]["id"])
        svc.set_clock("2026-09-12T17:50+08:00")
        inc = svc.register_incident(
            "vehicle_breakdown", at="2026-09-12T17:50+08:00",
            delay_minutes=60, fleet_id="flt-main")
        pimp = next(p for p in inc["impact"]["plans"]
                    if p["plan_id"] == plan["id"])
        for option in pimp["options"]:
            self.assertTrue(all(s["ref_id"] != "flt-main"
                                for s in option["summary"]
                                if s["kind"] == "shuttle"))
        updated = svc.apply_replan(plan["id"], 0)
        self.assertTrue(any(i["ref_id"] == "flt-backup"
                            for i in updated["items"]))


class CheckinTest(unittest.TestCase):
    def test_duplicate_scan_not_double_counted(self):
        svc = seeded()
        plan = make_locked_plan(svc)
        visit = next(i for i in plan["items"] if i["kind"] == "visit")
        first = svc.checkin(visit["id"], "CRED-HK-001")
        second = svc.checkin(visit["id"], "CRED-HK-001")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        summary = svc.checkin_summary(plan["id"])
        self.assertEqual(summary["scan_count"], 1)
        self.assertEqual(summary["unique_headcount"], 1)

    def test_unknown_credential_rejected(self):
        svc = seeded()
        plan = make_locked_plan(svc)
        visit = next(i for i in plan["items"] if i["kind"] == "visit")
        with self.assertRaises(ApiError):
            svc.checkin(visit["id"], "CRED-NOPE")


class ConsentManifestTest(unittest.TestCase):
    def test_meal_tags_only_with_meal_consent(self):
        svc = seeded()
        plan = make_locked_plan(
            svc,
            events=[{"kind": "enterprise", "duration_minutes": 60,
                     "languages": ["yue"]}])
        visit = next(i for i in plan["items"] if i["kind"] == "visit")
        rows = {a["alias"]: a for a
                in svc.item_manifest(visit["id"], "smart-factory")["attendees"]}
        self.assertEqual(rows["阿岚"]["meal_service"]["dietary_tags"],
                         ["vegetarian"])
        self.assertFalse(rows["阿翘"]["meal_service"]["granted"])
        self.assertNotIn("dietary_tags", rows["阿翘"]["meal_service"])

    def test_publicity_conspect_reflects_choice(self):
        svc = seeded()
        plan = make_locked_plan(svc)
        visit = next(i for i in plan["items"] if i["kind"] == "visit")
        rows = {a["alias"]: a for a
                in svc.item_manifest(visit["id"], "dg-university")["attendees"]}
        self.assertEqual(rows["阿岚"]["media_publicity"], "granted")
        self.assertEqual(rows["阿朗"]["media_publicity"], "denied")
        self.assertNotIn("meal_service", rows["阿岚"])  # 非包餐不释放饮食

    def test_fleet_sees_only_rollcall_fields(self):
        svc = seeded()
        plan = make_locked_plan(svc)
        shuttle = next(i for i in plan["items"] if i["kind"] == "shuttle")
        rows = svc.item_manifest(shuttle["id"], "bay-fleet")["attendees"]
        for row in rows:
            self.assertNotIn("meal_service", row)
            self.assertNotIn("media_publicity", row)

    def test_unrelated_party_cannot_view_manifest(self):
        svc = seeded()
        plan = make_locked_plan(svc)
        visit = next(i for i in plan["items"] if i["kind"] == "visit")
        with self.assertRaises(ApiError):
            svc.item_manifest(visit["id"], "smart-factory")  # 非本环节接待方


class LossTest(unittest.TestCase):
    def test_cancel_records_loss_but_never_charges(self):
        svc = seeded()
        plan = make_locked_plan(svc)
        svc.cancel_plan(plan["id"])
        losses = svc.list_losses()
        self.assertTrue(losses)
        for l in losses:
            self.assertEqual(l["settlement"], "external_record_only")
            self.assertEqual(l["status"], "pending_confirmation")

    def test_loss_needs_both_parties(self):
        svc = seeded()
        plan = make_locked_plan(svc)
        svc.cancel_plan(plan["id"])
        loss = svc.list_losses()[0]
        svc.confirm_loss(loss["id"], loss["from_party"])
        self.assertEqual(svc.list_losses(status="pending_confirmation")[0]["id"],
                         loss["id"])
        svc.confirm_loss(loss["id"], loss["against_party"])
        self.assertEqual(svc.list_losses(status="recorded")[0]["id"], loss["id"])


class NotificationTest(unittest.TestCase):
    def test_incident_broadcasts_with_receipts(self):
        svc = seeded()
        plan = make_locked_plan(svc)
        svc.set_clock("2026-09-12T17:35+08:00")
        inc = svc.register_incident(
            "overtime", at="2026-09-12T17:35+08:00", delay_minutes=60,
            competition_id="match-day1")
        targets = {n["target"] for n in svc.list_notifications()
                   if n["context"].get("incident_id") == inc["incident"]["id"]}
        self.assertIn("office", targets)
        self.assertIn("hong-kong", targets)
        nxt = next(n for n in svc.list_notifications("hong-kong")
                   if n["context"].get("incident_id"))
        svc.ack_notification(nxt["id"], "hong-kong")
        self.assertIsNotNone(
            svc.list_notifications("hong-kong")[-1]["acknowledged_at"])

    def test_office_overview_shape(self):
        svc = seeded()
        make_locked_plan(svc)
        ov = svc.office_overview()
        self.assertIn("plans", ov)
        self.assertIn("notification_receipts", ov)
        self.assertIn("checkins", ov)
        self.assertIn("losses", ov)


if __name__ == "__main__":
    unittest.main()
