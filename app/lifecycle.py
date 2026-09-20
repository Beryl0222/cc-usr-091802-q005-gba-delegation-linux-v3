"""行程生命周期：建单 → 参与方确认 → 锁定资源 → 出发/签到/完成；
以及取消与物料/空驶损失的双方确认记录。

关键不变量：

* 资源（时段容量、车辆座位）只在**全部必需参与方确认后**锁定；
* 已出发（``departed``）或已签到（``in_progress``）的环节不可取消、
  不可被系统强行改派；
* 损失只生成记录，从不产生扣款。
"""

from .errors import Conflict, DomainError, NotFound
from .notify import notify_many
from .planning import options_for_leg
from .timeutil import now_iso, parse_ts


def team_actor(team_id):
    return f"team:{team_id}"


def party_actor(party_id):
    return f"party:{party_id}"


def _required_actors(leg):
    return [team_actor(leg["team_id"]), party_actor(leg["party_id"])]


# --------------------------------------------------------------------------
# 建单
# --------------------------------------------------------------------------

def create_itinerary(store, team_id, payload, clock):
    if team_id not in store.teams:
        raise NotFound("队伍", team_id)
    selections = payload.get("selections")
    if not selections:
        raise DomainError("empty_selection", "请为每条环节选定一个候选时段")

    iid = store.gen_id("itin")
    itinerary = {
        "id": iid,
        "team_id": team_id,
        "status": "draft",
        "created_at": now_iso(clock),
        "legs": [],
    }
    store.itineraries[iid] = itinerary

    requests = payload.get("requests", [])
    req_by_kind = {r["kind"]: r for r in requests}
    leg_ids = []
    for sel in selections:
        option = _resolve_option(store, team_id, sel, req_by_kind.get(sel["kind"]), clock)
        leg = _new_leg(store, itinerary, option, clock)
        store.legs[leg["id"]] = leg
        leg_ids.append(leg["id"])
    itinerary["legs"] = leg_ids

    actors = {a for lid in leg_ids for a in _required_actors(store.legs[lid])}
    notify_many(store, actors, "itinerary_proposed",
                {"itinerary_id": iid, "team_id": team_id, "legs": len(leg_ids)},
                clock, about=iid)
    store.record_audit("itinerary_created", itinerary_id=iid, team_id=team_id,
                       legs=leg_ids)
    return itinerary


def _resolve_option(store, team_id, sel, req_override, clock):
    """根据 slot/时间选择从实时候选中取出指定选项，确保不使用过期结果。"""
    req = dict(req_override or {})
    req["kind"] = sel["kind"]
    req.setdefault("headcount", sel.get("headcount"))
    req.setdefault("duration_min", sel.get("duration_min", 0))
    if sel.get("from"):
        req["from"] = sel["from"]
        req["to"] = sel["to"]
    result = options_for_leg(store, team_id, req, clock)
    for opt in result["options"]:
        if opt["slot_id"] == sel["slot_id"]:
            if sel.get("start") and parse_ts(opt["start"]) != parse_ts(sel["start"]):
                continue
            return opt
    raise Conflict("option_unavailable",
                   "所选候选已不可用，请重新生成候选",
                   {"slot_id": sel.get("slot_id"), "kind": sel["kind"]})


def _new_leg(store, itinerary, option, clock):
    lid = store.gen_id("leg")
    return {
        "id": lid,
        "itinerary_id": itinerary["id"],
        "team_id": itinerary["team_id"],
        "kind": option["kind"],
        "offer_id": option["offer_id"],
        "slot_id": option["slot_id"],
        "party_id": option["party_id"],
        "window": {"start": option["start"], "end": option["end"]},
        "headcount": option["headcount"],
        "status": "proposed",
        "locked": False,
        "confirmations": {},
        "required_actors": _required_actors({"team_id": itinerary["team_id"],
                                             "party_id": option["party_id"]}),
        "checkins": [],
        "checkin_scans": 0,
        "created_at": now_iso(clock),
        "departed_at": None,
        "started_at": None,
        "completed_at": None,
        "cancelled_at": None,
        "version": 1,
        "replaced_by": None,
        "disruption_id": None,
    }


def get_itinerary(store, iid):
    it = store.itineraries.get(iid)
    if it is None:
        raise NotFound("行程", iid)
    return it


def get_leg(store, lid):
    leg = store.legs.get(lid)
    if leg is None:
        raise NotFound("环节", lid)
    return leg


# --------------------------------------------------------------------------
# 确认与锁定
# --------------------------------------------------------------------------

def confirm_leg(store, leg_id, actor, clock):
    leg = get_leg(store, leg_id)
    if leg["status"] not in ("proposed", "proposed-locked"):
        raise Conflict("leg_not_open",
                       f"环节当前状态 {leg['status']}，不可确认",
                       {"status": leg["status"]})
    if actor not in leg["required_actors"]:
        raise DomainError("not_a_party", "只有该环节的参与方可以确认", 403)
    if actor in leg["confirmations"]:
        raise Conflict("already_confirmed", "该参与方已确认")
    leg["confirmations"][actor] = {"at": now_iso(clock)}
    store.record_audit("leg_confirmed", leg_id=leg_id, actor=actor)

    if set(leg["confirmations"]) == set(leg["required_actors"]):
        _lock(store, leg)
        leg["status"] = "confirmed"
        store.itineraries[leg["itinerary_id"]]["status"] = "active"
        notify_many(store, leg["required_actors"], "leg_confirmed_locked",
                    {"leg_id": leg_id, "start": leg["window"]["start"]},
                    clock, about=leg["itinerary_id"])
    return leg


def _lock(store, leg):
    offer = store.offers[leg["offer_id"]]
    slot = next(s for s in offer["slots"] if s["id"] == leg["slot_id"])
    from .registry import slot_locked_qty
    locked = slot_locked_qty(store, slot)
    if locked + leg["headcount"] > slot["capacity"]:
        raise Conflict("capacity_exhausted",
                       f"容量已满: 已锁 {locked} / 容量 {slot['capacity']}",
                       {"slot_id": slot["id"], "capacity": slot["capacity"],
                        "locked": locked})
    slot["locks"].append({"leg_id": leg["id"], "qty": leg["headcount"]})
    leg["locked"] = True
    store.record_audit("capacity_locked", leg_id=leg["id"],
                       offer_id=offer["id"], slot_id=slot["id"],
                       qty=leg["headcount"])


def _release(store, leg):
    if not leg["locked"]:
        return
    offer = store.offers[leg["offer_id"]]
    slot = next(s for s in offer["slots"] if s["id"] == leg["slot_id"])
    slot["locks"] = [l for l in slot["locks"] if l["leg_id"] != leg["id"]]
    leg["locked"] = False
    store.record_audit("capacity_released", leg_id=leg["id"],
                       slot_id=slot["id"], qty=leg["headcount"])


# --------------------------------------------------------------------------
# 出发 / 签到 / 完成
# --------------------------------------------------------------------------

def depart(store, leg_id, clock, actor=None):
    leg = get_leg(store, leg_id)
    if leg["status"] != "confirmed":
        raise Conflict("not_confirmed", "只有已确认并锁定的环节可以发车/出发",
                       {"status": leg["status"]})
    leg["status"] = "departed"
    leg["departed_at"] = now_iso(clock)
    store.record_audit("leg_departed", leg_id=leg_id, actor=actor)
    notify_many(store, [leg["party_id"]], "leg_departed",
                {"leg_id": leg_id, "team_id": leg["team_id"]}, clock,
                about=leg["itinerary_id"])
    return leg


def check_in(store, leg_id, records, clock):
    """现场签到。records 为 [{pid, badge, channel}]；

    同一凭证在同一环节重复签到只计一次；汇总口径见
    :func:`attendance_summary`。
    """
    leg = get_leg(store, leg_id)
    if leg["status"] not in ("confirmed", "departed", "in_progress"):
        raise Conflict("leg_not_running", "该环节尚未锁定或已结束，无法签到",
                       {"status": leg["status"]})
    team = store.teams[leg["team_id"]]
    by_badge = {p["badge"]: p for p in team["roster"]}
    by_pid = {p["pid"]: p for p in team["roster"]}
    existing = {c["badge"] for c in leg["checkins"]}
    accepted, duplicates = [], []
    for rec in records:
        badge = rec.get("badge")
        person = by_badge.get(badge) or by_pid.get(rec.get("pid"))
        if person is None:
            raise NotFound("人员", badge or rec.get("pid"))
        leg["checkin_scans"] += 1  # 每次扫码都计数，便于核对重复签到
        if badge in existing or person["badge"] in existing:
            duplicates.append({"badge": person["badge"], "pid": person["pid"]})
            continue
        entry = {"pid": person["pid"], "alias": person["alias"],
                 "badge": person["badge"], "leg_id": leg_id,
                 "ts": now_iso(clock), "channel": rec.get("channel", "gate")}
        store.checkins.append(entry)
        leg["checkins"].append(entry)
        existing.add(person["badge"])
        accepted.append(entry)
    if leg["status"] != "in_progress" and leg["checkins"]:
        leg["status"] = "in_progress"
        leg["started_at"] = leg["started_at"] or now_iso(clock)
    store.record_audit("checkin", leg_id=leg_id, accepted=len(accepted),
                       duplicates=len(duplicates))
    return {"leg_id": leg_id, "accepted": accepted, "duplicates": duplicates,
            "unique_count": len(leg["checkins"])}


def complete(store, leg_id, clock):
    leg = get_leg(store, leg_id)
    if leg["status"] not in ("departed", "in_progress", "confirmed"):
        raise Conflict("leg_not_running", f"环节状态 {leg['status']}，不可完成")
    leg["status"] = "completed"
    leg["completed_at"] = now_iso(clock)
    store.record_audit("leg_completed", leg_id=leg_id)
    return leg


def attendance_summary(store, team_id=None, leg_id=None):
    """汇总签到人数，按凭证去重；可按队伍/环节过滤。"""
    legs = list(store.legs.values())
    if leg_id:
        legs = [l for l in legs if l["id"] == leg_id]
    if team_id:
        legs = [l for l in legs if l["team_id"] == team_id]
    unique_badges, per_leg = set(), []
    for leg in legs:
        badges = {c["badge"] for c in leg["checkins"]}
        unique_badges |= badges
        per_leg.append({"leg_id": leg["id"], "kind": leg["kind"],
                        "party_id": leg["party_id"],
                        "scans": leg.get("checkin_scans", len(leg["checkins"])),
                        "checkins": len(leg["checkins"]),
                        "unique_persons": len(badges)})
    return {"team_id": team_id, "leg_id": leg_id,
            "legs": per_leg, "total_unique_persons": len(unique_badges)}


# --------------------------------------------------------------------------
# 取消
# --------------------------------------------------------------------------

IMMUTABLE_STATES = ("departed", "in_progress", "completed")


def cancel_leg(store, leg_id, reason, clock, actor="office"):
    leg = get_leg(store, leg_id)
    if leg["status"] in IMMUTABLE_STATES:
        raise Conflict("leg_immutable",
                       "环节已出发或已签到，系统不得强行取消或改派",
                       {"status": leg["status"], "departed_at": leg["departed_at"]})
    if leg["status"] == "cancelled":
        raise Conflict("already_cancelled", "环节已取消")
    _release(store, leg)
    leg["status"] = "cancelled"
    leg["cancelled_at"] = now_iso(clock)
    leg["cancel_reason"] = reason
    notify_many(store, leg["required_actors"], "leg_cancelled",
                {"leg_id": leg_id, "reason": reason}, clock,
                about=f"cancel:{leg_id}")
    store.record_audit("leg_cancelled", leg_id=leg_id, reason=reason, actor=actor)
    return leg


# --------------------------------------------------------------------------
# 物料 / 空驶损失：双方确认记录，不自动扣款
# --------------------------------------------------------------------------

def record_loss(store, payload, clock):
    leg = get_leg(store, payload["leg_id"])
    ltype = payload["type"]
    if ltype not in ("material", "vehicle_empty"):
        raise DomainError("bad_loss_type", "损失类型必须是 material 或 vehicle_empty")
    amount = payload.get("amount")
    loss = {
        "id": store.gen_id("loss"),
        "leg_id": leg["id"],
        "itinerary_id": leg["itinerary_id"],
        "team_id": leg["team_id"],
        "party_id": leg["party_id"],
        "type": ltype,
        "description": payload.get("description", ""),
        "amount": amount,
        "currency": payload.get("currency", "CNY"),
        "status": "pending_confirmation",
        "confirmations": {},
        "required_actors": ["office", leg["party_id"]],
        "created_at": now_iso(clock),
        "settled": False,  # 系统只记录，绝不自动扣款
    }
    store.losses[loss["id"]] = loss
    notify_many(store, ["office", leg["party_id"]], "loss_recorded",
                {"loss_id": loss["id"], "type": ltype, "amount": amount},
                clock, about=f"loss:{loss['id']}")
    store.record_audit("loss_recorded", loss_id=loss["id"], leg_id=leg["id"],
                       ltype=ltype, amount=amount)
    return loss


def confirm_loss(store, loss_id, actor, clock):
    loss = store.losses.get(loss_id)
    if loss is None:
        raise NotFound("损失记录", loss_id)
    if actor.startswith("party:"):
        actor = actor.split(":", 1)[1]
    if actor not in loss["required_actors"]:
        raise DomainError("not_a_party", "只有承办方与接待方可以确认该记录", 403)
    if actor in loss["confirmations"]:
        raise Conflict("already_confirmed", "该方已确认")
    loss["confirmations"][actor] = {"at": now_iso(clock)}
    store.record_audit("loss_confirmed", loss_id=loss_id, actor=actor)
    if set(loss["confirmations"]) == set(loss["required_actors"]):
        loss["status"] = "confirmed_record"
    return loss
