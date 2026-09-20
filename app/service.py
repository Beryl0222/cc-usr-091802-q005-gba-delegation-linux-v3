"""行程联控核心服务。

关键不变量：
* 正式比赛（competition）是外部约束，任何候选/锁定行程都必须让开；
  加时只更新比赛的实际结束时间，比赛本身从不接受系统改期。
* 资源（接待容量、车辆座位）只在环节 ``locked`` 后通过占用台账锁定；
  候选行程不占资源，锁定瞬间重新校验容量与最晚确认点。
* 环节一旦 ``enroute`` / ``in_progress`` / ``completed`` 即冻结，
  任何改排都只能动尚未出发的环节。
* 损失记录只做双方确认留痕，不产生扣款。
"""

from datetime import datetime, timedelta
from itertools import permutations

from . import timeutil
from .errors import bad_request, conflict, not_found
from .store import Store

GRID_MIN = 15
VISIT_KINDS = ("campus", "enterprise", "market")
ITEM_FROZEN_STATUS = ("enroute", "in_progress", "completed")


class DispatchService:
    def __init__(self, store=None, clock=None):
        self.store = store or Store()
        self._clock = clock
        # 区间车程分钟数：(zone_a, zone_b) -> minutes
        self.travel_matrix = {}
        # 通知通道：默认全部落库并视为送达，可替换为真实网关
        self.transports = [self._inapp_transport]

    # ------------------------------------------------------------- clock
    def now(self):
        if self._clock is not None:
            value = self._clock()
            return value if isinstance(value, datetime) else timeutil.parse_ts(value)
        return datetime.now(timeutil.DEFAULT_TZ)

    def set_clock(self, clock):
        """clock 可以是可调用对象，也可以是时间字符串/datetime 常量。"""
        if callable(clock):
            self._clock = clock
        else:
            fixed = clock if isinstance(clock, datetime) \
                else timeutil.parse_ts(clock)
            self._clock = lambda: fixed

    def travel_minutes(self, zone_a, zone_b):
        if not zone_a or not zone_b or zone_a == zone_b:
            return 10
        return self.travel_matrix.get((zone_a, zone_b)) or \
            self.travel_matrix.get((zone_b, zone_a)) or 30

    # ====================================================== 基础台账申报
    def register_team(self, team_id, name, home_region):
        with self.store.lock:
            team = self.store.teams.setdefault(
                team_id, {"id": team_id, "name": name, "home_region": home_region})
            team.update(name=name, home_region=home_region)
            return dict(team)

    def register_member(self, team_id, alias, credential_ref, member_id=None,
                        dietary_tags=None, accessibility_needs=None,
                        consents=None):
        with self.store.lock:
            if team_id not in self.store.teams:
                raise not_found(f"队伍 {team_id} 不存在")
            for m in self.store.members.values():
                if m["credential_ref"] == credential_ref and m["id"] != member_id:
                    raise conflict(f"凭证 {credential_ref} 已被占用")
            mid = member_id or self.store.next_id("mb")
            member = {
                "id": mid,
                "team_id": team_id,
                "alias": alias,
                "credential_ref": credential_ref,
                "dietary_tags": list(dietary_tags or []),
                "accessibility_needs": list(accessibility_needs or []),
                "consents": [
                    {"purpose": c["purpose"], "granted": bool(c.get("granted", False))}
                    for c in (consents or [])
                ],
            }
            self.store.members[mid] = member
            return self.store.member_detail(member)

    def register_host(self, host_id, name, kind, contact=None):
        if kind not in ("campus", "enterprise", "market", "fleet_operator"):
            raise bad_request(f"未知接待方类型 {kind}")
        with self.store.lock:
            host = self.store.hosts.setdefault(
                host_id, {"id": host_id, "name": name, "kind": kind,
                          "contact": contact})
            host.update(name=name, kind=kind, contact=contact)
            return dict(host)

    def register_competition(self, comp_id, label, team_ids, start, end,
                             source="external"):
        s, e = timeutil.parse_ts(start), timeutil.parse_ts(end)
        if e <= s:
            raise bad_request("比赛结束时间必须晚于开始时间")
        with self.store.lock:
            for t in team_ids:
                if t not in self.store.teams:
                    raise not_found(f"队伍 {t} 不存在")
            comp = {"id": comp_id, "label": label, "team_ids": list(team_ids),
                    "start": s, "end": e, "source": source}
            self.store.competitions[comp_id] = comp
            return self.store.competition_dict(comp)

    def register_team_window(self, team_id, start, end, note=""):
        s, e = timeutil.parse_ts(start), timeutil.parse_ts(end)
        if e <= s:
            raise bad_request("可用窗口结束必须晚于开始")
        with self.store.lock:
            if team_id not in self.store.teams:
                raise not_found(f"队伍 {team_id} 不存在")
            w = {"id": self.store.next_id("win"), "start": s, "end": e,
                 "note": note}
            self.store.team_windows.setdefault(team_id, []).append(w)
            return self.store.team_window_dict(w)

    def register_offering(self, host_id, kind, title, location, zone, windows,
                          capacity, languages, accessibility, confirm_deadline,
                          provides_meal=False, media_requested=False,
                          material_cost_per_seat=None, offering_id=None):
        if kind not in VISIT_KINDS:
            raise bad_request(f"接待活动类型必须是 {VISIT_KINDS} 之一")
        parsed_windows = self._parse_windows(windows)
        deadline = timeutil.parse_ts(confirm_deadline)
        with self.store.lock:
            host = self._require_host(host_id)
            if host["kind"] != kind:
                raise bad_request(
                    f"接待方 {host_id} 类型为 {host['kind']}，不能申报 {kind} 活动")
            oid = offering_id or self.store.next_id("off")
            offering = {
                "id": oid, "host_id": host_id, "kind": kind, "title": title,
                "location": location, "zone": zone, "windows": parsed_windows,
                "capacity": int(capacity), "languages": list(languages or []),
                "accessibility": list(accessibility or []),
                "confirm_deadline": deadline, "provides_meal": provides_meal,
                "media_requested": media_requested,
                "material_cost_per_seat": material_cost_per_seat,
            }
            self.store.offerings[oid] = offering
            return self.store.offering_dict(offering)

    def register_fleet(self, host_id, name, windows, seats_per_vehicle,
                       vehicle_count, wheelchair, zones, confirm_deadline,
                       deadhead_fee=None, fleet_id=None):
        parsed_windows = self._parse_windows(windows)
        deadline = timeutil.parse_ts(confirm_deadline)
        with self.store.lock:
            self._require_host(host_id)
            fid = fleet_id or self.store.next_id("flt")
            fleet = {
                "id": fid, "host_id": host_id, "name": name,
                "windows": parsed_windows,
                "seats_per_vehicle": int(seats_per_vehicle),
                "vehicle_count": int(vehicle_count),
                "wheelchair": bool(wheelchair), "zones": list(zones or []),
                "confirm_deadline": deadline, "deadhead_fee": deadhead_fee,
            }
            self.store.fleets[fid] = fleet
            return self.store.fleet_dict(fleet)

    def _parse_windows(self, windows):
        out = []
        for w in windows:
            s, e = timeutil.parse_ts(w["start"]), timeutil.parse_ts(w["end"])
            if e <= s:
                raise bad_request("时段结束必须晚于开始")
            out.append((s, e))
        return out

    def _require_host(self, host_id):
        host = self.store.hosts.get(host_id)
        if not host:
            raise not_found(f"接待方 {host_id} 不存在")
        return host

    # ====================================================== 候选行程生成
    def _effective_windows(self, team_id):
        """队伍申报窗口扣除其正式比赛占用（外部不可移动约束）。"""
        windows = [(w["start"], w["end"])
                   for w in self.store.team_windows.get(team_id, [])]
        blocked = [(c["start"], c["end"]) for c in self.store.competitions.values()
                   if team_id in c["team_ids"]]
        for bs, be in blocked:
            windows = _subtract_interval(windows, bs, be)
        return windows

    def build_candidates(self, team_id, events, member_ids=None,
                         origin_zone=None, include_endpoint_transfer=True,
                         cap=6):
        """依据队伍赛后可用窗口与接待/车队申报，枚举候选行程。

        候选只做可行性展示，不占用任何资源。
        """
        with self.store.lock:
            if team_id not in self.store.teams:
                raise not_found(f"队伍 {team_id} 不存在")
            if not events:
                raise bad_request("至少需要一个交流活动")
            roster = self._resolve_roster(team_id, member_ids)
            windows = self._effective_windows(team_id)
            if not windows:
                raise conflict("队伍当前没有任何赛后可用窗口（或全部被比赛占用）")
            needs_access = {a for m in roster for a in m["accessibility_needs"]}
            reqs = [self._normalize_event_request(e) for e in events]

            plans = []
            keys = set()
            orderings = permutations(range(len(reqs))) if len(reqs) <= 4 \
                else [tuple(range(len(reqs)))]
            for order in orderings:
                ordered = [reqs[i] for i in order]
                for spec in self._search_itineraries(
                        team_id, roster, ordered, windows, needs_access,
                        origin_zone if include_endpoint_transfer else None,
                        cap=cap - len(plans)):
                    key = tuple((x["kind"], x["ref_id"], timeutil.iso(x["start"]))
                                for x in spec)
                    if key in keys:
                        continue
                    keys.add(key)
                    plans.append(self._persist_candidate(team_id, roster, spec))
                    if len(plans) >= cap:
                        return plans
            if not plans:
                raise conflict("当前申报容量、语言、无障碍或窗口下没有可行候选行程")
            return plans

    def _normalize_event_request(self, e):
        kind = e.get("kind")
        if kind not in VISIT_KINDS:
            raise bad_request(f"活动类型必须是 {VISIT_KINDS} 之一")
        return {
            "kind": kind,
            "duration": timedelta(minutes=int(e.get("duration_minutes", 90))),
            "languages": set(e.get("languages", [])),
            "title": e.get("title"),
        }

    def _resolve_roster(self, team_id, member_ids):
        pool = [m for m in self.store.members.values() if m["team_id"] == team_id]
        if member_ids is None:
            return pool
        roster = []
        for mid in member_ids:
            m = self.store.members.get(mid)
            if not m or m["team_id"] != team_id:
                raise bad_request(f"成员 {mid} 不属于队伍 {team_id}")
            roster.append(m)
        return roster

    def _search_itineraries(self, team_id, roster, reqs, windows, needs_access,
                            origin_zone, cap, earliest=None, start_zone=None,
                            prefer_offering=None, prefer_fleet=None,
                            banned_fleet=None, banned_until=None,
                            enforce_deadline=True):
        """深度优先贪心枚举：每个活动尝试不同接待方/出发时刻，接驳串联。

        enforce_deadline=False 用于事故后的紧急改排：首次锁定的最晚确认点
        不再适用，改排环节以"开始前完成重新确认"为准。
        """
        seats = len(roster)
        now = self.now()
        results = []

        def fleet_allowed(fid, s, e):
            if banned_fleet == fid:
                # banned_until 为空表示该运力退出本行程剩余环节
                if banned_until is None:
                    return False
                if timeutil.overlaps(s, e, now, banned_until):
                    return False
            return True

        def visit_ok(ref, s, e, temp):
            o = self.store.offerings[ref]
            if enforce_deadline and o["confirm_deadline"] <= now:
                return False
            if not timeutil.fits_in(s, e, o["windows"]):
                return False
            used = sum(b["seats"] for b
                       in self.store.offering_bookings.get(ref, [])
                       if timeutil.overlaps(s, e, b["start"], b["end"]))
            used += sum(b["seats"] for b in temp.get(ref, [])
                        if timeutil.overlaps(s, e, b["start"], b["end"]))
            return used + seats <= o["capacity"]

        def shuttle_ok(fid, dep, arr, s_temp):
            f = self.store.fleets[fid]
            if enforce_deadline and f["confirm_deadline"] <= now:
                return False
            if not fleet_allowed(fid, dep, arr):
                return False
            if not timeutil.fits_in(dep, arr, f["windows"]):
                return False
            used = sum(b["seats"] for b
                       in self.store.fleet_bookings.get(fid, [])
                       if timeutil.overlaps(dep, arr, b["start"], b["end"]))
            used += sum(b["seats"] for b in s_temp.get(fid, [])
                        if timeutil.overlaps(dep, arr, b["start"], b["end"]))
            return used + seats <= f["seats_per_vehicle"] * f["vehicle_count"]

        def matching_offerings(req):
            out = []
            for o in self.store.offerings.values():
                if o["kind"] != req["kind"]:
                    continue
                if req["languages"] - set(o["languages"]):
                    continue
                if needs_access - set(o["accessibility"]):
                    continue
                out.append(o)
            out.sort(key=lambda o: 0 if o["id"] == prefer_offering else 1)
            return out[:6]

        def pick_fleet(from_zone, to_zone, dep, arr, s_temp):
            cands = []
            for f in self.store.fleets.values():
                zones = set(f["zones"])
                if from_zone and from_zone not in zones:
                    continue
                if to_zone and to_zone not in zones:
                    continue
                if needs_access and not f["wheelchair"]:
                    continue
                if not shuttle_ok(f["id"], dep, arr, s_temp):
                    continue
                cands.append(f)
            cands.sort(key=lambda f: 0 if f["id"] == prefer_fleet else 1)
            return cands[0] if cands else None

        def dfs(depth, cur_time, cur_zone, acc, v_temp, f_temp):
            if len(results) >= cap:
                return
            if depth == len(reqs):
                # 回原点的收尾接驳
                if origin_zone and cur_zone:
                    travel = timedelta(minutes=self.travel_minutes(cur_zone, origin_zone))
                    dep, arr = cur_time, cur_time + travel
                    f = pick_fleet(cur_zone, origin_zone, dep, arr, f_temp) if \
                        timeutil.fits_in(dep, arr, windows) else None
                    if not f:
                        return
                    acc = acc + [self._shuttle_spec(f, cur_zone, origin_zone, dep, arr, seats)]
                results.append(list(acc))
                return
            req = reqs[depth]
            branches = 0
            for o in matching_offerings(req):
                for ws, we in o["windows"]:
                    # 与队伍窗口的交集
                    for tstart, tend in windows:
                        lo = max(ws, tstart, cur_time)
                        hi = min(we, tend)
                        s = _ceil_grid(lo)
                        starts_tried = 0
                        while s + req["duration"] <= hi and starts_tried < 24:
                            starts_tried += 1
                            e = s + req["duration"]
                            local_acc = list(acc)
                            local_vt = {k: list(v) for k, v in v_temp.items()}
                            local_ft = {k: list(v) for k, v in f_temp.items()}
                            # 与上一站之间的接驳
                            next_time = s
                            next_zone = cur_zone
                            if cur_zone:
                                travel = timedelta(minutes=self.travel_minutes(cur_zone, o["zone"]))
                                dep = s - travel
                                if dep < cur_time:
                                    s += timedelta(minutes=GRID_MIN)
                                    continue
                                f = pick_fleet(cur_zone, o["zone"], dep, s, local_ft)
                                if not timeutil.fits_in(dep, s, windows) or not f:
                                    s += timedelta(minutes=GRID_MIN)
                                    continue
                                local_acc.append(
                                    self._shuttle_spec(f, cur_zone, o["zone"], dep, s, seats))
                                local_ft.setdefault(f["id"], []).append(
                                    {"start": dep, "end": s, "seats": seats})
                            if not visit_ok(o["id"], s, e, local_vt):
                                s += timedelta(minutes=GRID_MIN)
                                continue
                            visit = {
                                "kind": "visit", "ref_id": o["id"],
                                "title": req.get("title") or o["title"],
                                "location": o["location"], "zone": o["zone"],
                                "start": s, "end": e, "seats": seats,
                                "languages": list(req["languages"]),
                                "accessibility": list(needs_access),
                            }
                            local_acc.append(visit)
                            local_vt.setdefault(o["id"], []).append(
                                {"start": s, "end": e, "seats": seats})
                            dfs(depth + 1, e, o["zone"], local_acc,
                                local_vt, local_ft)
                            branches += 1
                            if len(results) >= cap or branches >= 8:
                                return
                            s += timedelta(minutes=GRID_MIN)

        start = earliest or _ceil_grid(min(w[0] for w in windows))
        zone = start_zone or origin_zone
        dfs(0, start, zone, [], {}, {})
        return results

    def _shuttle_spec(self, fleet, from_zone, to_zone, dep, arr, seats):
        return {
            "kind": "shuttle", "ref_id": fleet["id"],
            "title": f"{fleet['name']} {from_zone}→{to_zone}",
            "location": f"{from_zone} → {to_zone}",
            "zone": to_zone, "from_zone": from_zone, "to_zone": to_zone,
            "start": dep, "end": arr, "seats": seats,
            "accessibility": ["wheelchair"] if fleet["wheelchair"] else [],
        }

    def _persist_candidate(self, team_id, roster, specs):
        now = self.now()
        plan_id = self.store.next_id("plan")
        plan = {
            "id": plan_id, "team_id": team_id,
            "label": f"{team_id} 候选行程",
            "member_ids": [m["id"] for m in roster],
            "status": "candidate", "created_at": now, "locked_at": None,
            "origin_zone": next((s.get("from_zone") for s in specs
                                 if s["kind"] == "shuttle"), None),
        }
        self.store.plans[plan_id] = plan
        items = []
        for seq, spec in enumerate(specs, start=1):
            it = self._make_item(plan_id, seq, spec)
            items.append(it)
        for it in items:
            self._notify_required(it, "行程待确认",
                                  f"候选行程 {plan_id} 有新环节需要贵方确认")
        return self.store.plan_dict(plan, items)

    def _make_item(self, plan_id, seq, spec, confirm_by=None):
        ref_id = spec["ref_id"]
        if spec["kind"] == "visit":
            host_id = self.store.offerings[ref_id]["host_id"]
            required = ["office", self.store.plans[plan_id]["team_id"], host_id]
            default_deadline = self.store.offerings[ref_id]["confirm_deadline"]
        else:
            host_id = self.store.fleets[ref_id]["host_id"]
            required = ["office", self.store.plans[plan_id]["team_id"], host_id]
            default_deadline = self.store.fleets[ref_id]["confirm_deadline"]
        item = {
            "id": self.store.next_id("item"), "plan_id": plan_id, "seq": seq,
            "kind": spec["kind"], "ref_id": ref_id,
            "title": spec.get("title", ""), "location": spec.get("location", ""),
            "zone": spec.get("zone", ""),
            "from_zone": spec.get("from_zone"), "to_zone": spec.get("to_zone"),
            "start": spec["start"], "end": spec["end"], "seats": spec["seats"],
            "languages": spec.get("languages", []),
            "accessibility": spec.get("accessibility", []),
            "status": "proposed", "confirmations": {},
            "required_parties": required,
            "confirm_by": confirm_by or default_deadline,
        }
        self.store.items[item["id"]] = item
        return item

    # ====================================================== 确认与锁定
    def confirm(self, plan_id, party, item_id=None):
        with self.store.lock:
            plan = self._require_plan(plan_id)
            if item_id is None:
                # 批量确认：只落到该方作为必要确认方的环节
                targets = [it for it in self._plan_items(plan_id)
                           if it["status"] == "proposed"
                           and party in it["required_parties"]]
                if not targets:
                    raise conflict(f"{party} 在该行程中没有待确认环节")
            else:
                it = self._require_item(plan_id, item_id)
                if party not in it["required_parties"]:
                    raise conflict(f"{party} 不是环节 {it['id']} 的必要确认方",
                                   {"required_parties": it["required_parties"]})
                targets = [it]
            at = self.now()
            for it in targets:
                it["confirmations"][party] = {"at": timeutil.iso(at), "party": party}
            return self.store.plan_dict(plan, self._plan_items(plan_id))

    def lock(self, plan_id):
        """所有必要方确认后锁定资源；锁定瞬间复核容量、窗口、最晚确认点。"""
        with self.store.lock:
            plan = self._require_plan(plan_id)
            if plan["status"] not in ("candidate", "partially_replanned"):
                raise conflict(f"行程状态 {plan['status']} 不可锁定")
            items = self._plan_items(plan_id)
            proposed = [it for it in items if it["status"] == "proposed"]
            if not proposed:
                raise conflict("没有待锁定的环节")
            problems = []
            for it in proposed:
                missing = [p for p in it["required_parties"]
                           if p not in it["confirmations"]]
                if missing:
                    problems.append({"item_id": it["id"], "missing": missing,
                                     "reason": "confirmation_outstanding"})
                    continue
                deadline = self._item_deadline(it)
                if deadline <= self.now():
                    problems.append({"item_id": it["id"],
                                     "deadline": timeutil.iso(deadline),
                                     "reason": "past_confirm_deadline"})
            if problems:
                raise conflict("存在未完成确认或已过最晚确认点的环节",
                               {"blockers": problems})
            # 容量与窗口复核（候选期间资源未被占用）
            for it in proposed:
                if not self._resource_available(it):
                    problems.append({"item_id": it["id"],
                                     "reason": "capacity_no_longer_available"})
            if problems:
                raise conflict("锁定时容量复核失败", {"blockers": problems})
            for it in proposed:
                self._book(it)
                it["status"] = "locked"
            plan["status"] = "locked"
            plan["locked_at"] = self.now()
            for it in proposed:
                self._notify_required(it, "行程已锁定",
                                      f"环节 {it['id']} 已锁定资源")
            return self.store.plan_dict(plan, items)

    def _item_deadline(self, it):
        return it.get("confirm_by") or (
            self.store.offerings.get(it["ref_id"]) or
            self.store.fleets.get(it["ref_id"]))["confirm_deadline"]

    def _resource_available(self, it, ignore_item=None):
        if it["kind"] == "visit":
            o = self.store.offerings[it["ref_id"]]
            if not timeutil.fits_in(it["start"], it["end"], o["windows"]):
                return False
            used = sum(b["seats"] for b
                       in self.store.offering_bookings.get(it["ref_id"], [])
                       if b["item_id"] != ignore_item
                       and timeutil.overlaps(it["start"], it["end"],
                                             b["start"], b["end"]))
            return used + it["seats"] <= o["capacity"]
        f = self.store.fleets[it["ref_id"]]
        if not timeutil.fits_in(it["start"], it["end"], f["windows"]):
            return False
        used = sum(b["seats"] for b
                   in self.store.fleet_bookings.get(it["ref_id"], [])
                   if b["item_id"] != ignore_item
                   and timeutil.overlaps(it["start"], it["end"],
                                         b["start"], b["end"]))
        return used + it["seats"] <= f["seats_per_vehicle"] * f["vehicle_count"]

    def _book(self, it):
        if it["kind"] == "visit":
            self.store.add_offering_booking(it)
        else:
            self.store.add_fleet_booking(it)

    def _release(self, it):
        if it["kind"] == "visit":
            self.store.remove_booking(self.store.offering_bookings,
                                      it["ref_id"], it["id"])
        else:
            self.store.remove_booking(self.store.fleet_bookings,
                                      it["ref_id"], it["id"])

    # ====================================================== 现场状态推进
    def mark_enroute(self, item_id):
        """车辆/队伍出发，环节冻结，系统此后不得强行改派。"""
        with self.store.lock:
            it = self._require_locked_item(item_id)
            it["status"] = "enroute"
            it["departed_at"] = self.now()
            return self.store.item_dict(it)

    def complete(self, item_id):
        with self.store.lock:
            it = self.store.items.get(item_id)
            if not it:
                raise not_found("环节不存在")
            if it["status"] not in ("locked", "enroute", "in_progress"):
                raise conflict(f"环节状态 {it['status']} 不可完成")
            it["status"] = "completed"
            it["completed_at"] = self.now()
            if all(i["status"] in ("completed", "superseded", "canceled")
                   for i in self._plan_items(it["plan_id"])):
                self.store.plans[it["plan_id"]]["status"] = "completed"
            return self.store.item_dict(it)

    def _require_locked_item(self, item_id):
        it = self.store.items.get(item_id)
        if not it:
            raise not_found("环节不存在")
        if it["status"] != "locked":
            raise conflict(f"环节状态 {it['status']}，仅 locked 环节可执行此操作")
        return it

    # ====================================================== 签到（去重）
    def checkin(self, item_id, credential_ref, alias=None):
        """按 (环节, 凭证) 幂等签到，重复扫描绝不重复计数。"""
        with self.store.lock:
            it = self.store.items.get(item_id)
            if not it:
                raise not_found("环节不存在")
            if it["kind"] != "visit":
                raise bad_request("只有接待活动可以签到")
            member = next((m for m in self.store.members.values()
                           if m["credential_ref"] == credential_ref), None)
            if not member:
                raise not_found(f"凭证 {credential_ref} 无法识别")
            if member["team_id"] != self.store.plans[it["plan_id"]]["team_id"]:
                raise conflict("该凭证不属于本行程队伍")
            if self.store.has_checkin(item_id, credential_ref):
                existing = next(c for c in self.store.item_unique_checkins(item_id)
                                if c["credential_ref"] == credential_ref)
                return {"duplicate": True, "checkin": self.store.checkin_dict(existing),
                        "unique_count": len(self.store.item_unique_checkins(item_id))}
            record = {
                "id": self.store.next_id("ci"),
                "item_id": item_id, "plan_id": it["plan_id"],
                "member_id": member["id"], "alias": member["alias"],
                "credential_ref": credential_ref, "at": timeutil.iso(self.now()),
            }
            self.store.checkins[record["id"]] = record
            self.store.remember_checkin(item_id, credential_ref)
            if it["status"] in ("locked", "enroute"):
                it["status"] = "in_progress"
                it["started_at"] = self.now()
            return {"duplicate": False, "checkin": record,
                    "unique_count": len(self.store.item_unique_checkins(item_id))}

    def checkin_summary(self, plan_id=None):
        """汇总人数：同一凭证跨多个环节只算一次。"""
        with self.store.lock:
            plans = [self._require_plan(plan_id)] if plan_id \
                else list(self.store.plans.values())
            out = []
            for plan in plans:
                records = self.store.plan_unique_checkins(plan["id"])
                unique = {r["credential_ref"] for r in records}
                per_item = {}
                for it in self._plan_items(plan["id"]):
                    if it["kind"] == "visit":
                        per_item[it["id"]] = len(
                            self.store.item_unique_checkins(it["id"]))
                out.append({"plan_id": plan["id"], "team_id": plan["team_id"],
                            "scan_count": len(records),
                            "unique_headcount": len(unique),
                            "per_item_unique": per_item})
            return out[0] if plan_id else out

    # ====================================================== 授权与最小可见
    def item_manifest(self, item_id, viewer):
        """接待方视图：只返回完成服务所必需的信息，按用途授权放行。"""
        with self.store.lock:
            it = self.store.items.get(item_id)
            if not it:
                raise not_found("环节不存在")
            plan = self.store.plans[it["plan_id"]]
            host = self.store.offerings.get(it["ref_id"]) or \
                self.store.fleets.get(it["ref_id"])
            host_id = host["host_id"]
            if viewer not in ("office", host_id, plan["team_id"]):
                raise conflict("该参与方无权查看本环节名单")
            roster = [self.store.members[m] for m in plan["member_ids"]
                      if m in self.store.members]
            attendees = []
            for m in roster:
                row = {"alias": m["alias"], "team_id": m["team_id"]}
                if it["kind"] == "visit":
                    offering = self.store.offerings[it["ref_id"]]
                    # 无障碍筹备是场地服务必需
                    if m["accessibility_needs"]:
                        row["accessibility_needs"] = list(m["accessibility_needs"])
                    # 团体饮食：仅包餐活动、且成员就 meal_service 用途授权后，
                    # 才释放饮食禁忌；拒绝授权时只给出占位状态，不暴露标签
                    if offering["provides_meal"]:
                        meal_grant = next((c["granted"] for c in m["consents"]
                                           if c["purpose"] == "meal_service"), None)
                        if meal_grant and m["dietary_tags"]:
                            row["meal_service"] = {
                                "granted": True,
                                "dietary_tags": list(m["dietary_tags"])}
                        elif meal_grant is False:
                            row["meal_service"] = {"granted": False}
                    # 影像：仅申报了公开传播用途的接待方看到对应授权状态
                    if offering["media_requested"]:
                        grant = next((c["granted"] for c in m["consents"]
                                      if c["purpose"] == "publicity"), None)
                        row["media_publicity"] = {True: "granted", False: "denied",
                                                  None: "unset"}[grant]
                else:
                    # 车队只需要核载与无障碍登车信息，不看饮食与影像
                    row["accessibility_needs"] = list(m["accessibility_needs"])
                attendees.append(row)
            return {
                "item_id": item_id, "viewer": viewer,
                "view_purpose": {"visit": "host_service",
                                 "shuttle": "transport_rollcall"}[it["kind"]],
                "seats": it["seats"], "attendee_count": len(attendees),
                "attendees": attendees,
            }

    # ====================================================== 扰动与改排
    def register_incident(self, inc_type, at=None, delay_minutes=None,
                          competition_id=None, fleet_id=None, description=""):
        """录入加时赛/车辆故障/接驳延误，立即生成影响面与通知。"""
        with self.store.lock:
            at = timeutil.parse_ts(at) if at else self.now()
            inc = {
                "id": self.store.next_id("inc"), "type": inc_type,
                "at": at, "delay_minutes": delay_minutes,
                "competition_id": competition_id, "fleet_id": fleet_id,
                "description": description,
            }
            if inc_type == "overtime":
                comp = self.store.competitions.get(competition_id)
                if not comp:
                    raise not_found("比赛不存在")
                delta = timedelta(minutes=delay_minutes or 0)
                inc["blocked_window"] = (comp["end"], comp["end"] + delta)
                # 比赛仍是外部约束：只记录实际延后的结束，不允许人工拖动
                comp["end"] = comp["end"] + delta
                comp["actual_extended"] = True
            elif inc_type in ("vehicle_breakdown", "traffic_delay"):
                if not fleet_id or fleet_id not in self.store.fleets:
                    raise not_found("车队不存在")
                inc["fleet_unavailable_until"] = at + timedelta(
                    minutes=delay_minutes or 60)
            else:
                raise bad_request(f"未知扰动类型 {inc_type}")
            self.store.incidents[inc["id"]] = inc
            impact = self._impact_analysis(inc)
            inc["impact"] = {"affected_plan_ids": [p["plan_id"] for p in impact["plans"]]}
            self._broadcast_incident(inc, impact)
            return {"incident": self.store.incident_dict(inc), "impact": impact}

    def _impact_analysis(self, inc):
        plans_out = []
        affected_parties = {"office"}
        for plan in self.store.plans.values():
            if plan["status"] not in ("locked", "partially_replanned"):
                continue
            items = self._plan_items(plan["id"])
            active = [i for i in items if i["status"] not in
                      ("superseded", "canceled")]
            pinned, movable = self._classify_impact(inc, plan, active)
            if not pinned and not movable:
                continue
            affected_parties.add(plan["team_id"])
            for i in pinned + [m[0] for m in movable]:
                ref = self.store.offerings.get(i["ref_id"]) or \
                    self.store.fleets.get(i["ref_id"])
                affected_parties.add(ref["host_id"])
            options = []
            if movable:
                options = self._replan_options(plan, inc, pinned, movable)
            plans_out.append({
                "plan_id": plan["id"], "team_id": plan["team_id"],
                "pinned_items": [self._impact_item(i, "frozen_no_force_change")
                                  for i in pinned],
                "reschedulable_items": [self._impact_item(i, reason)
                                        for i, reason in movable],
                "options": options,
            })
        return {
            "incident_id": inc["id"], "at": timeutil.iso(inc["at"]),
            "affected_parties": sorted(affected_parties),
            "plans": plans_out,
            "notifications": self._receipts_snapshot(),
        }

    def _classify_impact(self, inc, plan, active):
        pinned, movable = [], []
        items = sorted(active, key=lambda i: i["seq"])
        first_hit = None
        for idx, it in enumerate(items):
            hit, reason = self._item_hit_by(inc, it)
            if not hit:
                continue
            if it["status"] in ITEM_FROZEN_STATUS:
                pinned.append(it)
            else:
                movable.append((it, reason))
            if first_hit is None:
                first_hit = idx
        # 首当其冲的环节之后所有未出发环节进入待改排后缀（时间链联动）
        if first_hit is not None:
            for it in items[first_hit + 1:]:
                if it["status"] in ITEM_FROZEN_STATUS:
                    if it not in pinned:
                        pinned.append(it)
                elif not any(m[0]["id"] == it["id"] for m in movable):
                    movable.append((it, "chain_shift"))
        return pinned, movable

    def _item_hit_by(self, inc, it):
        if inc["type"] == "overtime":
            bs, be = inc["blocked_window"]
            team_ids = self.store.competitions[inc["competition_id"]]["team_ids"]
            if self.store.plans[it["plan_id"]]["team_id"] in team_ids and \
                    timeutil.overlaps(it["start"], it["end"], bs, be):
                return True, "competition_overtime"
            return False, None
        if inc["fleet_id"] and it["kind"] == "shuttle" and \
                it["ref_id"] == inc["fleet_id"]:
            if it["status"] in ITEM_FROZEN_STATUS:
                return True, "enroute_vehicle_delay"
            if timeutil.overlaps(it["start"], it["end"], inc["at"],
                                 inc["fleet_unavailable_until"]):
                return True, "vehicle_unavailable"
        return False, None

    def _impact_item(self, it, reason):
        return {"item_id": it["id"], "kind": it["kind"], "title": it["title"],
                "start": timeutil.iso(it["start"]), "end": timeutil.iso(it["end"]),
                "status": it["status"], "reason": reason,
                "ref_id": it["ref_id"]}

    def _replan_options(self, plan, inc, pinned, movable):
        """只重排未出发后缀；冻结环节作为时间锚点。"""
        team_id = plan["team_id"]
        roster = [self.store.members[m] for m in plan["member_ids"]]
        windows = self._effective_windows(team_id)
        if pinned:
            anchor = max(pinned, key=lambda i: i["end"])
            earliest = max(inc["at"] + timedelta(minutes=inc.get("delay_minutes") or 0),
                           anchor["end"])
            start_zone = anchor.get("to_zone") or anchor.get("zone")
        else:
            earliest = inc["at"] + timedelta(minutes=inc.get("delay_minutes") or 0)
            start_zone = plan.get("origin_zone")
        movable_sorted = sorted(movable, key=lambda x: x[0]["seq"])
        reqs, prefer_offering, prefer_fleet = [], None, None
        for it, _ in movable_sorted:
            if it["kind"] == "visit":
                reqs.append({"kind": self.store.offerings[it["ref_id"]]["kind"],
                             "duration": it["end"] - it["start"],
                             "languages": set(it["languages"]),
                             "title": it["title"]})
                prefer_offering = it["ref_id"]
            else:
                prefer_fleet = it["ref_id"]
        needs_access = {a for m in roster for a in m["accessibility_needs"]}
        banned = inc["fleet_id"] if inc["type"] in \
            ("vehicle_breakdown", "traffic_delay") else None
        # 抛锚车辆退出本行程剩余环节；交通拥堵只在影响时段内暂时不可用
        banned_until = None if inc["type"] == "vehicle_breakdown" \
            else inc.get("fleet_unavailable_until")
        specs_list = self._search_itineraries(
            team_id, roster, reqs, windows, needs_access,
            origin_zone=plan.get("origin_zone"), cap=3, earliest=earliest,
            start_zone=start_zone,
            prefer_offering=prefer_offering, prefer_fleet=prefer_fleet,
            banned_fleet=banned, banned_until=banned_until,
            enforce_deadline=False)
        options = []
        for specs in specs_list:
            options.append({
                "summary": [{"kind": s["kind"], "ref_id": s["ref_id"],
                             "start": timeutil.iso(s["start"]),
                             "end": timeutil.iso(s["end"]),
                             "title": s["title"]} for s in specs],
                "specs": [self._spec_json(s) for s in specs],
            })
        return options

    def _spec_json(self, s):
        return {
            "kind": s["kind"], "ref_id": s["ref_id"],
            "title": s.get("title", ""), "location": s.get("location", ""),
            "zone": s.get("zone", ""), "from_zone": s.get("from_zone"),
            "to_zone": s.get("to_zone"),
            "start": timeutil.iso(s["start"]), "end": timeutil.iso(s["end"]),
            "seats": s["seats"], "languages": s.get("languages", []),
            "accessibility": s.get("accessibility", []),
        }

    def apply_replan(self, plan_id, option_index):
        """办公室选定改排方案：旧环节作废留痕、释放资源，新环节需重新确认。"""
        with self.store.lock:
            plan = self._require_plan(plan_id)
            inc = self._latest_incident_for_plan(plan_id)
            if inc is None:
                raise conflict("当前没有影响该行程的扰动")
            impact = next((p for p in self._impact_analysis(inc)["plans"]
                           if p["plan_id"] == plan_id), None)
            if not impact or not impact["options"]:
                raise conflict("当前没有可应用的改排方案")
            if option_index < 0 or option_index >= len(impact["options"]):
                raise bad_request("改排方案序号越界")
            specs_json = impact["options"][option_index]["specs"]
            old_movable = sorted(
                (self.store.items[i["item_id"]]
                 for i in impact["reschedulable_items"]), key=lambda i: i["seq"])
            # 冻结环节绝不触碰
            for old in old_movable:
                if old["status"] in ITEM_FROZEN_STATUS:
                    raise conflict("存在已出发/签到环节，不能改派")
            new_items = []
            append_seq = max(i["seq"] for i in self._plan_items(plan_id))
            appended = 0
            for idx, sj in enumerate(specs_json):
                new_start = timeutil.parse_ts(sj["start"])
                new_end = timeutil.parse_ts(sj["end"])
                old = old_movable[idx] if idx < len(old_movable) else None
                # 同资源、同时段的环节原样保留（含既有锁定与确认）
                if old is not None and old["kind"] == sj["kind"] and \
                        old["ref_id"] == sj["ref_id"] and \
                        old["start"] == new_start and old["end"] == new_end:
                    continue
                spec = dict(sj)
                spec["start"] = new_start
                spec["end"] = new_end
                # 紧急改排：新环节以出发前为最晚确认点
                new_it = self._make_item(
                    plan_id, old["seq"] if old else append_seq + appended + 1,
                    spec, confirm_by=new_start)
                new_items.append(new_it)
                if old is None:
                    appended += 1
                if old is not None:
                    old["status"] = "superseded"
                    old["superseded_by"] = new_it["id"]
                    new_it["supersedes"] = old["id"]
                    self._release(old)
                    self._loss_if_dropped(old, new_it)
            # 新方案比原后缀短：多余的旧环节取消并留痕
            for old in old_movable[len(specs_json):]:
                old["status"] = "canceled"
                self._release(old)
                self._loss_if_dropped(old, None)
            if not new_items and len(specs_json) == len(old_movable):
                # 选定方案与现状完全一致：无需改派，保持原锁定
                return self.store.plan_dict(plan, self._plan_items(plan_id))
            plan["status"] = "partially_replanned"
            for it in new_items:
                self._notify_required(it, "改排待确认",
                                      f"行程 {plan_id} 因扰动改排，需要重新确认")
            return self.store.plan_dict(plan, self._plan_items(plan_id))

    def cancel_plan(self, plan_id):
        """取消未出发环节；在途/签到环节拒绝取消，并生成损失确认草稿。"""
        with self.store.lock:
            plan = self._require_plan(plan_id)
            items = self._plan_items(plan_id)
            cancellable = [i for i in items if i["status"] in
                           ("proposed", "locked")]
            frozen = [i for i in items if i["status"] in ITEM_FROZEN_STATUS]
            if frozen and not cancellable:
                raise conflict("所有环节均已出发或完成，无法取消",
                               {"frozen_item_ids": [i["id"] for i in frozen]})
            for it in cancellable:
                if it["status"] == "locked":
                    self._release(it)
                it["status"] = "canceled"
                self._loss_if_dropped(it, None)
                self._notify_required(it, "行程取消",
                                      f"环节 {it['id']} 已取消")
            if frozen:
                plan["status"] = "partially_replanned"
            else:
                plan["status"] = "canceled"
            return self.store.plan_dict(plan, self._plan_items(plan_id))

    def _loss_if_dropped(self, old, new):
        """物料损失/车辆空驶只登记待确认记录，绝不自动扣款。"""
        if new is not None and new["ref_id"] == old["ref_id"] and \
                new["kind"] == old["kind"]:
            return  # 同一接待方/车队改期，物料与运力可沿用，无损失
        if old["kind"] == "visit":
            o = self.store.offerings[old["ref_id"]]
            amount = None if o["material_cost_per_seat"] is None \
                else o["material_cost_per_seat"] * old["seats"]
            loss_type, counterparty = "material", o["host_id"]
            desc = f"活动 {old['title']} 取消，已备物料损失待双方核定"
        else:
            f = self.store.fleets[old["ref_id"]]
            amount = f.get("deadhead_fee")
            loss_type, counterparty = "deadhead", f["host_id"]
            desc = f"接驳 {old['title']} 取消，车辆空驶费用待双方核定"
        record = {
            "id": self.store.next_id("loss"), "plan_id": old["plan_id"],
            "item_id": old["id"], "type": loss_type,
            "from_party": counterparty,
            "against_party": self.store.plans[old["plan_id"]]["team_id"],
            "amount": amount, "currency": "CNY" if amount is not None else None,
            "description": desc,
            "status": "pending_confirmation",
            "confirmations": {}, "settlement": "external_record_only",
            "created_at": timeutil.iso(self.now()),
        }
        self.store.losses[record["id"]] = record
        self.notify(counterparty, "loss_record",
                    "损失记录待确认", desc, {"loss_id": record["id"]})
        self.notify(record["against_party"], "loss_record",
                    "损失记录待确认", desc, {"loss_id": record["id"]})

    def confirm_loss(self, loss_id, party, amount=None, note=None):
        with self.store.lock:
            loss = self.store.losses.get(loss_id)
            if not loss:
                raise not_found("损失记录不存在")
            if party not in (loss["from_party"], loss["against_party"], "office"):
                raise conflict("仅当事双方可确认本记录")
            if amount is not None:
                loss["amount"] = amount
            if note:
                loss.setdefault("notes", []).append(
                    {"party": party, "note": note, "at": timeutil.iso(self.now())})
            loss["confirmations"][party] = timeutil.iso(self.now())
            if loss["from_party"] in loss["confirmations"] and \
                    loss["against_party"] in loss["confirmations"]:
                loss["status"] = "recorded"
            return self.store.loss_dict(loss)

    # ====================================================== 通知与回执
    def _inapp_transport(self, n):
        # 模拟网关即时送达；真实部署替换为短信/IM 网关，失败回写 failed
        n["status"] = "delivered"
        n["receipt_at"] = timeutil.iso(self.now())
        return True

    def notify(self, target, channel, subject, body, context=None):
        with self.store.lock:
            n = {"id": self.store.next_id("ntf"), "target": target,
                 "channel": channel, "subject": subject, "body": body,
                 "context": context or {}, "status": "pending",
                 "sent_at": timeutil.iso(self.now()),
                 "receipt_at": None, "acknowledged_at": None}
            self.store.notifications[n["id"]] = n
            for transport in self.transports:
                transport(n)
            return self.store.notification_dict(n)

    def _notify_required(self, it, subject, body):
        for party in it["required_parties"]:
            self.notify(party, "plan", subject, body,
                        {"plan_id": it["plan_id"], "item_id": it["id"]})

    def _broadcast_incident(self, inc, impact):
        for party in impact["affected_parties"]:
            self.notify(party, "incident",
                        f"扰动 {inc['type']} 已录入",
                        inc.get("description") or inc["type"],
                        {"incident_id": inc["id"]})

    def _receipts_snapshot(self):
        return [{"id": n["id"], "target": n["target"], "status": n["status"],
                 "acknowledged": n["acknowledged_at"] is not None}
                for n in list(self.store.notifications.values())[-50:]]

    def ack_notification(self, notification_id, party):
        with self.store.lock:
            n = self.store.notifications.get(notification_id)
            if not n:
                raise not_found("通知不存在")
            if party != n["target"] and party != "office":
                raise conflict("仅接收方可以回执")
            n["acknowledged_at"] = timeutil.iso(self.now())
            return self.store.notification_dict(n)

    # ====================================================== 办公室视图
    def office_overview(self):
        with self.store.lock:
            plans = []
            for plan in self.store.plans.values():
                items = self._plan_items(plan["id"])
                pending = []
                for it in items:
                    if it["status"] == "proposed":
                        missing = [p for p in it["required_parties"]
                                   if p not in it["confirmations"]]
                        if missing:
                            pending.append({"item_id": it["id"], "missing": missing,
                                            "deadline": timeutil.iso(self._item_deadline(it))})
                plans.append({"plan_id": plan["id"], "team_id": plan["team_id"],
                              "status": plan["status"],
                              "items": len(items),
                              "pending_confirmations": pending})
            receipts = [n for n in self.store.notifications.values()]
            return {
                "now": timeutil.iso(self.now()),
                "plans": plans,
                "incidents": [self.store.incident_dict(i)
                              for i in self.store.incidents.values()],
                "checkins": self.checkin_summary(),
                "notification_receipts": {
                    "total": len(receipts),
                    "delivered": sum(1 for n in receipts if n["status"] == "delivered"),
                    "acknowledged": sum(1 for n in receipts if n["acknowledged_at"]),
                    "pending": [{"id": n["id"], "target": n["target"],
                                 "subject": n["subject"]}
                                for n in receipts if not n["acknowledged_at"]][:20],
                },
                "losses": [self.store.loss_dict(l)
                           for l in self.store.losses.values()],
            }

    def incident(self, incident_id):
        with self.store.lock:
            inc = self.store.incidents.get(incident_id)
            if not inc:
                raise not_found("扰动不存在")
            return {"incident": self.store.incident_dict(inc),
                    "impact": self._impact_analysis(inc)}

    def _latest_incident_for_plan(self, plan_id):
        best = None
        for inc in self.store.incidents.values():
            for p in self._impact_analysis(inc)["plans"]:
                if p["plan_id"] == plan_id:
                    if best is None or inc["at"] > best["at"]:
                        best = inc
                    break
        return best

    # ====================================================== 查询辅助
    def list_teams(self):
        with self.store.lock:
            return [{"id": t["id"], "name": t["name"],
                     "home_region": t["home_region"],
                     "windows": [self.store.team_window_dict(w)
                                 for w in self.store.team_windows.get(t["id"], [])],
                     "members": [self.store.member_detail(m)
                                 for m in self.store.members.values()
                                 if m["team_id"] == t["id"]]}
                    for t in self.store.teams.values()]

    def list_hosts(self):
        with self.store.lock:
            return list(self.store.hosts.values())

    def list_offerings(self):
        with self.store.lock:
            return [self.store.offering_dict(o)
                    for o in self.store.offerings.values()]

    def list_fleets(self):
        with self.store.lock:
            return [self.store.fleet_dict(f) for f in self.store.fleets.values()]

    def list_competitions(self):
        with self.store.lock:
            return [self.store.competition_dict(c)
                    for c in self.store.competitions.values()]

    def list_plans(self):
        with self.store.lock:
            return [self.store.plan_dict(p, self._plan_items(p["id"]))
                    for p in self.store.plans.values()]

    def get_plan(self, plan_id):
        with self.store.lock:
            return self.store.plan_dict(self._require_plan(plan_id),
                                        self._plan_items(plan_id))

    def list_losses(self, status=None):
        with self.store.lock:
            return [self.store.loss_dict(l) for l in self.store.losses.values()
                    if status is None or l["status"] == status]

    def list_notifications(self, target=None):
        with self.store.lock:
            return [self.store.notification_dict(n)
                    for n in self.store.notifications.values()
                    if target is None or n["target"] == target]

    def _require_plan(self, plan_id):
        plan = self.store.plans.get(plan_id)
        if not plan:
            raise not_found("行程不存在")
        return plan

    def _require_item(self, plan_id, item_id):
        it = self.store.items.get(item_id)
        if not it or it["plan_id"] != plan_id:
            raise not_found("环节不存在或不属于该行程")
        return it

    def _plan_items(self, plan_id):
        return [it for it in self.store.items.values() if it["plan_id"] == plan_id]


def _subtract_interval(windows, bs, be):
    """从若干窗口中挖掉 [bs, be)。"""
    out = []
    for s, e in windows:
        if be <= s or bs >= e:
            out.append((s, e))
            continue
        if s < bs:
            out.append((s, bs))
        if be < e:
            out.append((be, e))
    return out


def _ceil_grid(dt, minutes=GRID_MIN):
    floor = dt.replace(minute=(dt.minute // minutes) * minutes, second=0,
                       microsecond=0)
    return floor if floor == dt else floor + timedelta(minutes=minutes)
