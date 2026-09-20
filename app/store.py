"""线程安全的内存存储。

只负责数据的安放、取出和序列化，所有业务规则在 :mod:`app.service`。
所有方法调用都应在 ``store.lock`` 内进行（服务层在多步操作期间持锁）。
"""

import itertools
import threading

from . import timeutil


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self._counters = itertools.count(1)

        # 基础台账
        self.teams = {}          # team_id -> {id, name, home_region}
        self.members = {}        # member_id -> {...}
        self.hosts = {}          # host_id -> {id, name, kind, contact}
        self.offerings = {}      # offering_id -> 申报时段（学校/企业/市集）
        self.fleets = {}         # fleet_id -> 车队申报
        self.competitions = {}   # competition_id -> 正式比赛（不可移动）

        # 队伍赛后可用窗口
        self.team_windows = {}   # team_id -> [{id, start, end, note}]

        # 行程与环节
        self.plans = {}          # plan_id -> {...}
        self.items = {}          # item_id -> {...}

        # 确认、通知、损失、签到、扰动
        self.confirmations = {}  # confirmation_id -> {...}
        self.notifications = {}  # notification_id -> {...}
        self.losses = {}         # loss_id -> {...}
        self.checkins = {}       # checkin_id -> {...}
        self.incidents = {}      # incident_id -> {...}

        # 资源占用台账：ref_id -> [booking, ...]
        self.offering_bookings = {}
        self.fleet_bookings = {}

        # 去重索引
        self._checkin_index = set()  # (item_id, credential_ref)

    # ------------------------------------------------------------------ id
    def next_id(self, prefix):
        return f"{prefix}_{next(self._counters):04d}"

    # ------------------------------------------------------------ serializers
    def member_public(self, m):
        """对其他参与方可见的最小信息（不含饮食、影像意愿等敏感项）。"""
        return {
            "member_id": m["id"],
            "alias": m["alias"],
            "team_id": m["team_id"],
        }

    def member_detail(self, m):
        return {
            **self.member_public(m),
            "credential_ref": m["credential_ref"],
            "dietary_tags": list(m["dietary_tags"]),
            "accessibility_needs": list(m["accessibility_needs"]),
            "consents": [dict(c) for c in m["consents"]],
        }

    def team_window_dict(self, w):
        return {"id": w["id"], "start": timeutil.iso(w["start"]),
                "end": timeutil.iso(w["end"]), "note": w.get("note", "")}

    def competition_dict(self, c):
        return {
            "id": c["id"],
            "label": c["label"],
            "team_ids": list(c["team_ids"]),
            "start": timeutil.iso(c["start"]),
            "end": timeutil.iso(c["end"]),
            "immovable": True,
            "source": c.get("source", "fixture"),
        }

    def offering_dict(self, o):
        return {
            "id": o["id"],
            "host_id": o["host_id"],
            "kind": o["kind"],
            "title": o.get("title", o["kind"]),
            "location": o.get("location", ""),
            "zone": o.get("zone", ""),
            "provides_meal": o.get("provides_meal", False),
            "windows": [{"start": timeutil.iso(w[0]),
                         "end": timeutil.iso(w[1])} for w in o["windows"]],
            "capacity": o["capacity"],
            "languages": list(o["languages"]),
            "accessibility": list(o["accessibility"]),
            "confirm_deadline": timeutil.iso(o["confirm_deadline"]),
            "seats_remaining": self.offering_seats_remaining(o["id"]),
        }

    def fleet_dict(self, f):
        return {
            "id": f["id"],
            "host_id": f["host_id"],
            "name": f.get("name", f["id"]),
            "windows": [{"start": timeutil.iso(w[0]),
                         "end": timeutil.iso(w[1])} for w in f["windows"]],
            "seats_per_vehicle": f["seats_per_vehicle"],
            "vehicle_count": f["vehicle_count"],
            "wheelchair": f["wheelchair"],
            "zones": list(f.get("zones", [])),
            "confirm_deadline": timeutil.iso(f["confirm_deadline"]),
            "seats_remaining": self.fleet_seats_remaining(f["id"]),
        }

    def item_dict(self, it):
        return {
            "id": it["id"],
            "plan_id": it["plan_id"],
            "seq": it["seq"],
            "kind": it["kind"],
            "ref_id": it["ref_id"],
            "title": it.get("title", ""),
            "location": it.get("location", ""),
            "start": timeutil.iso(it["start"]),
            "end": timeutil.iso(it["end"]),
            "seats": it["seats"],
            "languages": list(it.get("languages", [])),
            "accessibility": list(it.get("accessibility", [])),
            "status": it["status"],
            "frozen": it["status"] in ("enroute", "in_progress", "completed"),
            "confirmations": {k: dict(v) for k, v in it["confirmations"].items()},
            "required_parties": list(it["required_parties"]),
            "confirm_by": timeutil.iso(it.get("confirm_by")),
            "superseded_by": it.get("superseded_by"),
            "supersedes": it.get("supersedes"),
        }

    def plan_dict(self, p, items=None):
        out = {
            "id": p["id"],
            "team_id": p["team_id"],
            "label": p.get("label", ""),
            "roster": [self.member_public(self.members[m]) for m in p["member_ids"]
                       if m in self.members],
            "status": p["status"],
            "created_at": timeutil.iso(p["created_at"]),
            "locked_at": timeutil.iso(p.get("locked_at")),
        }
        if items is not None:
            out["items"] = [self.item_dict(i) for i in items]
        return out

    def confirmation_dict(self, c):
        return dict(c)

    def notification_dict(self, n):
        return dict(n)

    def loss_dict(self, l):
        return dict(l)

    def checkin_dict(self, c):
        return dict(c)

    def incident_dict(self, inc):
        out = {k: v for k, v in inc.items() if k != "impact"}
        out["at"] = timeutil.iso(inc["at"])
        if "blocked_window" in inc:
            out["blocked_window"] = {
                "start": timeutil.iso(inc["blocked_window"][0]),
                "end": timeutil.iso(inc["blocked_window"][1])}
        if "fleet_unavailable_until" in inc:
            out["fleet_unavailable_until"] = \
                timeutil.iso(inc["fleet_unavailable_until"])
        if "impact" in inc:
            out["impact"] = inc["impact"]
        return out

    # ------------------------------------------------------------- bookings
    def offering_seats_remaining(self, offering_id):
        o = self.offerings[offering_id]
        # 容量按每个时间窗内的并发占用计算；返回所有窗口中最紧的剩余量
        remaining = o["capacity"]
        for w in o["windows"]:
            used = sum(b["seats"] for b in self.offering_bookings.get(offering_id, [])
                       if timeutil.overlaps(w["start"], w["end"], b["start"], b["end"]))
            remaining = min(remaining, o["capacity"] - used)
        return remaining

    def fleet_seats_remaining(self, fleet_id):
        f = self.fleets[fleet_id]
        cap = f["seats_per_vehicle"] * f["vehicle_count"]
        remaining = cap
        for w in f["windows"]:
            used = sum(b["seats"] for b in self.fleet_bookings.get(fleet_id, [])
                       if timeutil.overlaps(w["start"], w["end"], b["start"], b["end"]))
            remaining = min(remaining, cap - used)
        return remaining

    def add_offering_booking(self, item):
        self.offering_bookings.setdefault(item["ref_id"], []).append({
            "item_id": item["id"],
            "start": item["start"],
            "end": item["end"],
            "seats": item["seats"],
        })

    def remove_booking(self, bookings, ref_id, item_id):
        lst = bookings.get(ref_id, [])
        bookings[ref_id] = [b for b in lst if b["item_id"] != item_id]

    def add_fleet_booking(self, item):
        self.fleet_bookings.setdefault(item["ref_id"], []).append({
            "item_id": item["id"],
            "start": item["start"],
            "end": item["end"],
            "seats": item["seats"],
        })

    # ------------------------------------------------------------- check-ins
    def has_checkin(self, item_id, credential_ref):
        return (item_id, credential_ref) in self._checkin_index

    def remember_checkin(self, item_id, credential_ref):
        self._checkin_index.add((item_id, credential_ref))

    def item_unique_checkins(self, item_id):
        return [c for c in self.checkins.values() if c["item_id"] == item_id]

    def plan_unique_checkins(self, plan_id):
        return [c for c in self.checkins.values() if c["plan_id"] == plan_id]
