"""登记域：赛事与队伍、接待方与可用时段申报、用途授权。

正式比赛在这里登记为**不可移动的外部约束**：只有赛果（实际结束时间，
如加时）可被更新，比赛时段本身不接受系统改排。
"""

from .errors import Conflict, DomainError, NotFound
from .timeutil import iso, now_iso, parse_ts, parse_window

MEAL = "meal"
IMAGE = "image"


# --------------------------------------------------------------------------
# 赛事与队伍
# --------------------------------------------------------------------------

def bootstrap(store, payload):
    store.meta = {
        "event_id": payload.get("event_id"),
        "days": list(payload.get("days", [])),
    }
    store.record_audit("bootstrap", event_id=store.meta["event_id"])
    return store.meta


def register_team(store, payload):
    tid = payload["id"]
    if tid in store.teams:
        raise Conflict("team_exists", f"队伍已存在: {tid}")
    team = {
        "id": tid,
        "name": payload.get("name", tid),
        "languages": list(payload.get("languages", ["zh-yue", "zh-hans"])),
        "roster": [],
    }
    store.teams[tid] = team
    store.team_windows.setdefault(tid, [])
    store.record_audit("team_registered", team_id=tid)
    return team


def set_roster(store, team_id, persons):
    team = store.teams.get(team_id)
    if team is None:
        raise NotFound("队伍", team_id)
    roster = []
    seen = set()
    for p in persons:
        pid = p["pid"]
        if pid in seen:
            raise DomainError("duplicate_pid", f"名单中出现重复人员: {pid}")
        seen.add(pid)
        roster.append({
            "pid": pid,
            "alias": p.get("alias", pid),
            "badge": p["badge"],  # 凭证标识，仅此一项用于现场核验
            "meal_need": list(p.get("meal_need", [])),
            "accessibility": list(p.get("accessibility", [])),
            "image": dict(p.get("image", {})),  # 按用途的公开意愿
        })
    team["roster"] = roster
    store.record_audit("roster_set", team_id=team_id, count=len(roster))
    return team


def declare_availability(store, team_id, windows):
    if team_id not in store.teams:
        raise NotFound("队伍", team_id)
    parsed = []
    for w in windows:
        start, end = parse_window(w)
        if end <= start:
            raise DomainError("bad_window", "可用时段结束时间必须晚于开始时间")
        parsed.append({"start": iso(start), "end": iso(end),
                       "source": w.get("source", "declared")})
    store.team_windows[team_id] = parsed
    store.record_audit("availability_declared", team_id=team_id, windows=len(parsed))
    return parsed


def register_game(store, payload):
    gid = payload["id"]
    if gid in store.games:
        raise Conflict("game_exists", f"比赛已存在: {gid}")
    start, end = parse_window(payload["window"])
    for t in payload["teams"]:
        if t not in store.teams:
            raise NotFound("队伍", t)
    game = {
        "id": gid,
        "day": payload["day"],
        "venue": payload.get("venue", "venue-main"),
        "window": {"start": iso(start), "end": iso(end)},
        "teams": list(payload["teams"]),
        "status": "scheduled",       # 正式比赛不可被系统移动
        "actual_end": None,
    }
    store.games[gid] = game
    store.record_audit("game_registered", game_id=gid)
    return game


# --------------------------------------------------------------------------
# 接待方与可用时段申报
# --------------------------------------------------------------------------

def register_party(store, payload):
    pid = payload["id"]
    if pid in store.parties:
        raise Conflict("party_exists", f"参与方已存在: {pid}")
    party = {
        "id": pid,
        "type": payload["type"],            # campus / industry / market / fleet
        "name": payload.get("name", pid),
        "contact_channel": payload.get("contact_channel", "phone"),
    }
    store.parties[pid] = party
    store.record_audit("party_registered", party_id=pid, type=party["type"])
    return party


def register_offer(store, payload):
    party_id = payload["party_id"]
    if party_id not in store.parties:
        raise NotFound("接待方", party_id)
    otype = payload["type"]
    if otype not in ("campus", "industry", "market", "fleet"):
        raise DomainError("bad_offer_type", f"未知活动类型: {otype}")
    oid = payload.get("id") or store.gen_id("off")
    slots = []
    for s in payload.get("slots", []):
        start, end = parse_window(s)
        if end <= start:
            raise DomainError("bad_window", "时段结束时间必须晚于开始时间")
        slots.append({
            "id": s.get("id") or store.gen_id("slot"),
            "start": iso(start),
            "end": iso(end),
            "capacity": int(s["capacity"]),
            "locks": [],   # [{leg_id, qty}]
        })
    confirm_by = payload.get("confirm_by")
    offer = {
        "id": oid,
        "party_id": party_id,
        "type": otype,
        "name": payload.get("name", oid),
        "location": payload.get("location"),
        "features": {
            "languages": list(payload.get("languages", [])),
            "accessibility": list(payload.get("accessibility", [])),
            "meal_provided": bool(payload.get("meal_provided", False)),
            "meal_tags": list(payload.get("meal_tags", [])),
        },
        "route": _parse_route(payload.get("route")),
        "vehicle_id": payload.get("vehicle_id"),
        "confirm_by": iso(parse_ts(confirm_by)) if confirm_by else None,
        "slots": slots,
        "status": "active",
    }
    store.offers[oid] = offer
    store.record_audit("offer_registered", offer_id=oid, party_id=party_id,
                       type=otype, slots=len(slots))
    return offer


def _parse_route(route):
    if not route:
        return None
    return {
        "from": route["from"],
        "to": route["to"],
        "duration_min": int(route.get("duration_min", 30)),
    }


def slot_locked_qty(store, slot):
    # 锁记录随确认建立、随取消/改排释放，故直接汇总即可
    return sum(l["qty"] for l in slot["locks"])


def slot_remaining(store, slot):
    return slot["capacity"] - slot_locked_qty(store, slot)


def suspend_offer(store, offer_id, reason=None):
    offer = store.offers.get(offer_id)
    if offer is None:
        raise NotFound("可用时段申报", offer_id)
    offer["status"] = "suspended"
    store.record_audit("offer_suspended", offer_id=offer_id, reason=reason)
    return offer


# --------------------------------------------------------------------------
# 用途授权
# --------------------------------------------------------------------------

def grant_consent(store, payload, clock):
    team_id = payload["team_id"]
    if team_id not in store.teams:
        raise NotFound("队伍", team_id)
    scope = payload["scope"]
    if scope not in (MEAL, IMAGE):
        raise DomainError("bad_scope", "授权范围必须是 meal 或 image")
    purposes = list(payload.get("purposes", []))
    if not purposes:
        raise DomainError("no_purpose", "授权必须声明具体用途")
    for party_id in payload.get("parties", []):
        if party_id not in store.parties:
            raise NotFound("接待方", party_id)
    grant = {
        "id": store.gen_id("consent"),
        "team_id": team_id,
        "scope": scope,
        "purposes": purposes,
        "parties": list(payload.get("parties", [])),
        "granted_by": payload.get("granted_by", "team-lead"),
        "granted_at": now_iso(clock),
        "revoked_at": None,
    }
    store.consents[grant["id"]] = grant
    store.record_audit("consent_granted", consent_id=grant["id"],
                       team_id=team_id, scope=scope, purposes=purposes)
    return grant


def revoke_consent(store, grant_id, clock):
    grant = store.consents.get(grant_id)
    if grant is None:
        raise NotFound("授权", grant_id)
    if grant["revoked_at"]:
        raise Conflict("already_revoked", "该授权已撤销")
    grant["revoked_at"] = now_iso(clock)
    store.record_audit("consent_revoked", consent_id=grant_id)
    return grant


def active_grants(store, team_id, scope, party_id, purpose):
    """队伍对某接待方、某用途当前有效的授权。"""
    out = []
    for g in store.consents.values():
        if (g["team_id"] == team_id and g["scope"] == scope
                and g["revoked_at"] is None
                and (not g["parties"] or party_id in g["parties"])
                and purpose in g["purposes"]):
            out.append(g)
    return out


def has_consent(store, team_id, scope, party_id, purpose):
    return bool(active_grants(store, team_id, scope, party_id, purpose))
