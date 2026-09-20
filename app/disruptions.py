"""扰动处置：加时赛与车辆故障。

扰动进入系统时立即产出影响面快照：受影响环节、已签到/在途而**受保护
不可强改**的人员、可行改排候选、各方确认状态与通知回执。改排只作用于
尚未出发的环节，新环节需要参与方重新确认后才锁定资源。
"""

from .errors import Conflict, DomainError, NotFound
from .lifecycle import (IMMUTABLE_STATES, _new_leg, _release, _required_actors,
                        get_itinerary, get_leg)
from .notify import notify_many, receipts_for
from .planning import RECOVERY_MIN, options_for_leg
from .timeutil import iso, mins, now_iso, overlap, parse_ts

RESCHEDULED = "rescheduled"


# --------------------------------------------------------------------------
# 扰动登记
# --------------------------------------------------------------------------

def register_overtime(store, payload, clock):
    game = store.games.get(payload["game_id"])
    if game is None:
        raise NotFound("比赛", payload["game_id"])
    actual_end = parse_ts(payload["actual_end"])
    scheduled_end = parse_ts(game["window"]["end"])
    if actual_end <= scheduled_end:
        raise DomainError("not_overtime",
                          "实际结束时间必须晚于赛程结束时间才构成加时")
    game["actual_end"] = iso(actual_end)
    game["status"] = "overtime"

    d = _create_disruption(store, {
        "type": "overtime",
        "game_id": game["id"],
        "teams": list(game["teams"]),
        "blocked_from": game["window"]["start"],
        "blocked_to": iso(actual_end + mins(RECOVERY_MIN)),
        "summary": f"比赛 {game['id']} 加时至 {iso(actual_end)}",
    }, clock)
    blocked = (parse_ts(game["window"]["start"]), actual_end + mins(RECOVERY_MIN))
    affected = []
    for team_id in game["teams"]:
        for leg in _team_legs(store, team_id):
            if leg["status"] in ("cancelled", RESCHEDULED):
                continue
            ws, we = parse_ts(leg["window"]["start"]), parse_ts(leg["window"]["end"])
            if overlap(ws, we, *blocked):
                affected.append((leg, "环节时间与加时赛后恢复窗口冲突"))
    _finalize(store, d, affected, clock)
    return d


def register_vehicle_fault(store, payload, clock):
    vehicle_id = payload.get("vehicle_id")
    offer_id = payload.get("offer_id")
    if not vehicle_id and not offer_id:
        raise DomainError("missing_vehicle", "需要提供 vehicle_id 或 offer_id")
    offers = [o for o in store.offers.values()
              if o["type"] == "fleet"
              and ((vehicle_id and o.get("vehicle_id") == vehicle_id)
                   or (offer_id and o["id"] == offer_id))]
    if not offers:
        raise NotFound("车辆/运力申报", vehicle_id or offer_id)
    until = parse_ts(payload["unavailable_until"])

    for offer in offers:
        offer["status"] = "suspended"
        offer["suspension"] = {"reason": "vehicle_fault",
                               "unavailable_until": iso(until)}

    offer_ids = {o["id"] for o in offers}
    d = _create_disruption(store, {
        "type": "vehicle_fault",
        "vehicle_id": vehicle_id,
        "offer_ids": sorted(offer_ids),
        "blocked_from": now_iso(clock),
        "blocked_to": iso(until),
        "summary": f"车辆 {vehicle_id or sorted(offer_ids)} 故障，运力暂停至 {iso(until)}",
    }, clock)
    blocked = (parse_ts(d["occurred_at"]), until)
    affected = []
    for leg in store.legs.values():
        if leg["status"] in ("cancelled", RESCHEDULED):
            continue
        if leg["offer_id"] in offer_ids:
            ws = parse_ts(leg["window"]["start"])
            if ws < until:
                affected.append((leg, "承运车辆故障，运力在该环节前无法恢复"))
    _finalize(store, d, affected, clock)
    return d


def _create_disruption(store, fields, clock):
    d = {
        "id": store.gen_id("dis"),
        "occurred_at": now_iso(clock),
        "status": "open",
        "affected_leg_ids": [],
        **fields,
    }
    store.disruptions[d["id"]] = d
    store.record_audit("disruption_registered", disruption_id=d["id"],
                       dtype=d["type"])
    return d


def _team_legs(store, team_id):
    return [l for l in store.legs.values() if l["team_id"] == team_id]


def _protected_persons(store, leg):
    """已签到或在途、系统不得改派的人数（凭证去重）。"""
    badges = {c["badge"] for c in leg["checkins"]}
    if leg["status"] == "departed" and not badges:
        return leg["headcount"]  # 在途车辆上的人员按发车名单保护
    return len(badges)


def _finalize(store, disruption, affected, clock):
    parties = set()
    for leg, _reason in affected:
        leg["disruption_id"] = disruption["id"]
        disruption["affected_leg_ids"].append(leg["id"])
        parties.update(_required_actors(leg))
    if parties:
        notify_many(store, parties, "disruption_alert",
                    {"disruption_id": disruption["id"],
                     "summary": disruption["summary"],
                     "legs": disruption["affected_leg_ids"]},
                    clock, about=disruption["id"])
    store.record_audit("disruption_impact", disruption_id=disruption["id"],
                       legs=disruption["affected_leg_ids"], parties=sorted(parties))


# --------------------------------------------------------------------------
# 影响面快照
# --------------------------------------------------------------------------

def _leg_requirement(store, leg):
    """从环节现状重建需求，并补回队伍的翻译与无障碍硬需求。"""
    team = store.teams[leg["team_id"]]
    req = {
        "kind": leg["kind"],
        "headcount": leg["headcount"],
        "duration_min": int(
            (parse_ts(leg["window"]["end"]) - parse_ts(leg["window"]["start"]))
            .total_seconds() // 60),
        # 接驳只核验无障碍乘车条件，不核验翻译
        "languages": [] if leg["kind"] == "fleet"
        else list(team.get("languages", [])),
        "accessibility": sorted({a for p in team.get("roster", [])
                                 for a in p.get("accessibility", [])}),
    }
    offer = store.offers.get(leg["offer_id"])
    if offer and leg["kind"] == "fleet" and offer.get("route"):
        req["from"] = offer["route"]["from"]
        req["to"] = offer["route"]["to"]
    return req


def _reschedule_options(store, leg, clock):
    req = _leg_requirement(store, leg)
    result = options_for_leg(store, leg["team_id"], req, clock,
                             exclude_leg_id=leg["id"])
    return result["options"]


def impact_snapshot(store, disruption_id, clock):
    d = store.disruptions.get(disruption_id)
    if d is None:
        raise NotFound("扰动事件", disruption_id)
    legs_view, parties, protected_badges = [], set(), set()
    for lid in d["affected_leg_ids"]:
        leg = store.legs.get(lid)
        if leg is None:
            continue
        immutable = leg["status"] in IMMUTABLE_STATES
        parties.update(_required_actors(leg))
        if immutable:
            protected_badges |= {c["badge"] for c in leg["checkins"]}
        view = {
            "leg_id": lid,
            "team_id": leg["team_id"],
            "party_id": leg["party_id"],
            "kind": leg["kind"],
            "window": leg["window"],
            "headcount": leg["headcount"],
            "status": leg["status"],
            "immutable": immutable,
            "protected_persons": _protected_persons(store, leg),
            "confirmations": leg["confirmations"],
            "replacement_id": leg.get("replaced_by"),
        }
        if not immutable and leg["status"] not in ("cancelled", RESCHEDULED):
            view["reschedule_options"] = _reschedule_options(store, leg, clock)
        legs_view.append(view)

    return {
        "disruption": d,
        "affected_legs": legs_view,
        "affected_parties": sorted(parties),
        "protected_unique_persons": len(protected_badges),
        "receipts": receipts_for(store, d["id"]),
    }


# --------------------------------------------------------------------------
# 改排：仅未出发环节；新环节需双方重新确认
# --------------------------------------------------------------------------

def reschedule(store, disruption_id, selections, clock):
    d = store.disruptions.get(disruption_id)
    if d is None:
        raise NotFound("扰动事件", disruption_id)
    results = {"replaced": [], "skipped": [], "errors": []}
    for sel in selections:
        leg = get_leg(store, sel["leg_id"])
        if leg["id"] not in d["affected_leg_ids"]:
            results["errors"].append({"leg_id": leg["id"],
                                      "reason": "该环节不在本次扰动影响面内"})
            continue
        if leg["status"] in IMMUTABLE_STATES:
            results["skipped"].append({
                "leg_id": leg["id"],
                "reason": "已出发或已签到，系统不得强行改派",
                "protected_persons": _protected_persons(store, leg),
            })
            continue
        if leg["status"] in ("cancelled", RESCHEDULED):
            results["skipped"].append({"leg_id": leg["id"],
                                       "reason": f"环节状态为 {leg['status']}"})
            continue
        option = _pick_option(store, leg, sel, clock)
        _apply_replacement(store, d, leg, option, clock)
        results["replaced"].append({"leg_id": leg["id"],
                                    "replacement_id": leg["replaced_by"],
                                    "new_window": option["start"] + "→" + option["end"],
                                    "requires_reconfirmation": True})
    if results["replaced"]:
        d["status"] = "rescheduling"
    return results


def _pick_option(store, leg, sel, clock):
    req = _leg_requirement(store, leg)
    req["headcount"] = sel.get("headcount", leg["headcount"])
    if sel.get("from"):
        req["from"] = sel["from"]
        req["to"] = sel["to"]
    result = options_for_leg(store, leg["team_id"], req, clock,
                             exclude_leg_id=leg["id"])
    for opt in result["options"]:
        if opt["slot_id"] == sel["slot_id"]:
            if not sel.get("start") or parse_ts(opt["start"]) == parse_ts(sel["start"]):
                return opt
    raise Conflict("no_reschedule_option",
                   "未找到满足容量/窗口/最晚确认点的改排候选",
                   {"leg_id": leg["id"], "slot_id": sel.get("slot_id")})


def _apply_replacement(store, disruption, old_leg, option, clock):
    itinerary = get_itinerary(store, old_leg["itinerary_id"])
    _release(store, old_leg)
    new_leg = _new_leg(store, itinerary, option, clock)
    new_leg["version"] = old_leg["version"] + 1
    new_leg["replaces"] = old_leg["id"]
    new_leg["disruption_id"] = disruption["id"]
    store.legs[new_leg["id"]] = new_leg
    old_leg["status"] = RESCHEDULED
    old_leg["replaced_by"] = new_leg["id"]
    itinerary["legs"].append(new_leg["id"])
    notify_many(store, _required_actors(new_leg), "leg_rescheduled",
                {"old_leg_id": old_leg["id"], "new_leg_id": new_leg["id"],
                 "start": option["start"], "end": option["end"],
                 "disruption_id": disruption["id"]},
                clock, about=disruption["id"])
    store.record_audit("leg_rescheduled", old_leg_id=old_leg["id"],
                       new_leg_id=new_leg["id"], disruption_id=disruption["id"])


def close_disruption(store, disruption_id, clock):
    d = store.disruptions.get(disruption_id)
    if d is None:
        raise NotFound("扰动事件", disruption_id)
    d["status"] = "closed"
    d["closed_at"] = now_iso(clock)
    store.record_audit("disruption_closed", disruption_id=disruption_id)
    return d


def resume_fleet_offer(store, offer_id, clock):
    """车辆修复后恢复运力申报。"""
    offer = store.offers.get(offer_id)
    if offer is None:
        raise NotFound("运力申报", offer_id)
    offer["status"] = "active"
    offer["suspension"] = None
    store.record_audit("offer_resumed", offer_id=offer_id)
    return offer
