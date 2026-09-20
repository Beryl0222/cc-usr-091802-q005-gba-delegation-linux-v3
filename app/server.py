"""HTTP 装配：把领域模块暴露为 JSON API。

观看者身份通过 ``X-Viewer`` 头或 ``viewer`` 查询参数指定：

``office`` / ``team:<id>`` / ``party:<id>``
    决定最小可见视图与确认权限。
``X-Now`` 头（ISO 时间）用于注入当前时间，便于复盘与测试。
"""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import disruptions, lifecycle, planning, registry, views
from .errors import DomainError
from .notify import acknowledge
from .store import Store
from .timeutil import now_iso


def _qs(query, key, default=None):
    vals = parse_qs(query).get(key)
    return vals[0] if vals else default


class Api:
    """无状态外观：持有一个 :class:`Store`。"""

    def __init__(self, store=None, clock=None):
        self.store = store or Store()
        self.clock = clock  # None 表示真实时钟

    def now(self, override=None):
        if override:
            return override
        if self.clock is not None:
            return now_iso(self.clock)
        from datetime import datetime
        from .timeutil import DEFAULT_TZ
        return now_iso(lambda: datetime.now(DEFAULT_TZ))

    # -- 路由 ---------------------------------------------------------------

    def dispatch(self, method, path, body, viewer, now_override):
        m = self.MATCHES
        for pattern, routes in m:
            match = pattern.fullmatch(path)
            if match:
                handler = routes.get(method)
                if handler is None:
                    raise DomainError("method_not_allowed", "方法不允许", 405)
                return handler(self, body, viewer, now_override, **match.groupdict())
        raise DomainError("not_found_route", f"无此路由: {path}", 404)

    # -- setup --------------------------------------------------------------

    def bootstrap(self, body, viewer, now):
        return registry.bootstrap(self.store, body)

    def create_team(self, body, viewer, now):
        return registry.register_team(self.store, body)

    def set_roster(self, body, viewer, now, team_id):
        return registry.set_roster(self.store, team_id, body["persons"])

    def declare_availability(self, body, viewer, now, team_id):
        return registry.declare_availability(self.store, team_id, body["windows"])

    def create_game(self, body, viewer, now):
        return registry.register_game(self.store, body)

    def create_party(self, body, viewer, now):
        return registry.register_party(self.store, body)

    def create_offer(self, body, viewer, now):
        return registry.register_offer(self.store, body)

    def resume_offer(self, body, viewer, now, offer_id):
        return disruptions.resume_fleet_offer(self.store, offer_id, _Clock(now))

    # -- planning -----------------------------------------------------------

    def candidates(self, body, viewer, now, team_id):
        return planning.build_candidates(self.store, team_id, body, _Clock(now))

    def create_itinerary(self, body, viewer, now, team_id):
        it = lifecycle.create_itinerary(self.store, team_id, body, _Clock(now))
        return views.itinerary_view(self.store, it, "office")

    def list_itineraries(self, body, viewer, now, **_):
        viewer = viewer or "office"
        return {"itineraries": views.visible_itineraries(self.store, viewer)}

    def get_itinerary(self, body, viewer, now, itinerary_id, **_):
        it = lifecycle.get_itinerary(self.store, itinerary_id)
        view = views.itinerary_view(self.store, it, viewer or "office")
        if view is None:
            raise DomainError("forbidden", "无权查看该行程", 403)
        return view

    # -- legs ---------------------------------------------------------------

    def confirm_leg(self, body, viewer, now, leg_id):
        actor = body.get("actor") or viewer
        if not actor:
            raise DomainError("actor_required", "确认需要提供参与方身份")
        return lifecycle.confirm_leg(self.store, leg_id, actor, _Clock(now))

    def depart(self, body, viewer, now, leg_id):
        return lifecycle.depart(self.store, leg_id, _Clock(now), actor=viewer)

    def checkin(self, body, viewer, now, leg_id):
        return lifecycle.check_in(self.store, leg_id, body["records"], _Clock(now))

    def complete(self, body, viewer, now, leg_id):
        return lifecycle.complete(self.store, leg_id, _Clock(now))

    def cancel_leg(self, body, viewer, now, leg_id):
        return lifecycle.cancel_leg(self.store, leg_id,
                                    body.get("reason", ""), _Clock(now),
                                    actor=viewer or "office")

    def attendance(self, body, viewer, now, **_):
        raise NotImplementedError  # 走 GET，见 _handle 中的 /attendance

    # -- consent ------------------------------------------------------------

    def grant_consent(self, body, viewer, now):
        return registry.grant_consent(self.store, body, _Clock(now))

    def revoke_consent(self, body, viewer, now, consent_id):
        return registry.revoke_consent(self.store, consent_id, _Clock(now))

    # -- losses -------------------------------------------------------------

    def record_loss(self, body, viewer, now):
        return lifecycle.record_loss(self.store, body, _Clock(now))

    def confirm_loss(self, body, viewer, now, loss_id):
        actor = body.get("actor") or viewer
        if not actor:
            raise DomainError("actor_required", "确认需要提供参与方身份")
        return lifecycle.confirm_loss(self.store, loss_id, actor, _Clock(now))

    def list_losses(self, body, viewer, now, **_):
        items = list(self.store.losses.values())
        if viewer and viewer.startswith("party:"):
            pid = viewer.split(":", 1)[1]
            items = [l for l in items if l["party_id"] == pid]
        elif viewer and viewer.startswith("team:"):
            tid = viewer.split(":", 1)[1]
            items = [l for l in items if l["team_id"] == tid]
        return {"losses": items}

    # -- disruptions --------------------------------------------------------

    def overtime(self, body, viewer, now):
        return disruptions.register_overtime(self.store, body, _Clock(now))

    def vehicle_fault(self, body, viewer, now):
        return disruptions.register_vehicle_fault(self.store, body, _Clock(now))

    def impact(self, body, viewer, now, disruption_id, **_):
        snap = disruptions.impact_snapshot(self.store, disruption_id, _Clock(now))
        if viewer and viewer.startswith("party:"):
            pid = viewer.split(":", 1)[1]
            snap["affected_legs"] = [l for l in snap["affected_legs"]
                                     if l["party_id"] == pid]
            snap["affected_parties"] = [pid]
            snap["protected_unique_persons"] = 0
            snap["receipts"] = [r for r in snap["receipts"]
                                if r["party_id"] == pid]
        elif viewer and viewer.startswith("team:"):
            tid = viewer.split(":", 1)[1]
            snap["affected_legs"] = [l for l in snap["affected_legs"]
                                     if l["team_id"] == tid]
        return snap

    def reschedule(self, body, viewer, now, disruption_id, **_):
        return disruptions.reschedule(self.store, disruption_id,
                                      body["selections"], _Clock(now))

    def close_disruption(self, body, viewer, now, disruption_id, **_):
        return disruptions.close_disruption(self.store, disruption_id, _Clock(now))

    def list_disruptions(self, body, viewer, now, **_):
        return {"disruptions": list(self.store.disruptions.values())}

    # -- notifications ------------------------------------------------------

    def ack(self, body, viewer, now, notification_id, **_):
        return acknowledge(self.store, notification_id, _Clock(now),
                           channel=body.get("channel"))

    def list_notifications(self, body, viewer, now, party_id=None, **_):
        items = list(self.store.notifications.values())
        if viewer and viewer.startswith("party:") and not party_id:
            party_id = viewer.split(":", 1)[1]
        if party_id:
            items = [n for n in items if n["party_id"] == party_id]
        return {"notifications": items}

    def attendance_report(self, query, viewer, now):
        team_id = _qs(query, "team_id")
        leg_id = _qs(query, "leg_id")
        if viewer and viewer.startswith("party:"):
            # 接待方只能汇总自己服务环节的到场，且拿不到跨队伍去重总数
            report = lifecycle.attendance_summary(
                self.store, team_id=team_id, leg_id=leg_id)
            pid = viewer.split(":", 1)[1]
            report["legs"] = [l for l in report["legs"]
                              if l["party_id"] == pid]
            report["total_unique_persons"] = sum(
                l["unique_persons"] for l in report["legs"])
            return report
        if viewer and viewer.startswith("team:"):
            team_id = viewer.split(":", 1)[1]
        return lifecycle.attendance_summary(
            self.store, team_id=team_id, leg_id=leg_id)


class _Clock:
    """把固定时间字符串适配为领域模块所需的零参可调用时钟。"""

    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def _route(pattern):
    return re.compile("^" + pattern + "$")


def _build_matches():
    P = {
        "team": r"(?P<team_id>[A-Za-z0-9_\-]+)",
        "itin": r"(?P<itinerary_id>[A-Za-z0-9_\-]+)",
        "leg": r"(?P<leg_id>[A-Za-z0-9_\-]+)",
        "consent": r"(?P<consent_id>[A-Za-z0-9_\-]+)",
        "loss": r"(?P<loss_id>[A-Za-z0-9_\-]+)",
        "dis": r"(?P<disruption_id>[A-Za-z0-9_\-]+)",
        "offer": r"(?P<offer_id>[A-Za-z0-9_\-]+)",
        "note": r"(?P<notification_id>[A-Za-z0-9_\-]+)",
        "party": r"(?P<party_id>[A-Za-z0-9_\-]+)",
    }
    return [
        (_route(r"/admin/bootstrap"), {"POST": Api.bootstrap}),
        (_route(r"/teams"), {"POST": Api.create_team}),
        (_route(rf"/teams/{P['team']}/roster"), {"POST": Api.set_roster}),
        (_route(rf"/teams/{P['team']}/availability"),
         {"POST": Api.declare_availability}),
        (_route(rf"/teams/{P['team']}/candidates"), {"POST": Api.candidates}),
        (_route(rf"/teams/{P['team']}/itineraries"),
         {"POST": Api.create_itinerary}),
        (_route(r"/games"), {"POST": Api.create_game}),
        (_route(r"/parties"), {"POST": Api.create_party}),
        (_route(r"/offers"), {"POST": Api.create_offer}),
        (_route(rf"/offers/{P['offer']}/resume"), {"POST": Api.resume_offer}),
        (_route(r"/itineraries"), {"GET": Api.list_itineraries}),
        (_route(rf"/itineraries/{P['itin']}"), {"GET": Api.get_itinerary}),
        (_route(rf"/legs/{P['leg']}/confirm"), {"POST": Api.confirm_leg}),
        (_route(rf"/legs/{P['leg']}/depart"), {"POST": Api.depart}),
        (_route(rf"/legs/{P['leg']}/checkin"), {"POST": Api.checkin}),
        (_route(rf"/legs/{P['leg']}/complete"), {"POST": Api.complete}),
        (_route(rf"/legs/{P['leg']}/cancel"), {"POST": Api.cancel_leg}),
        (_route(r"/consents"), {"POST": Api.grant_consent}),
        (_route(rf"/consents/{P['consent']}/revoke"), {"POST": Api.revoke_consent}),
        (_route(r"/losses"), {"POST": Api.record_loss,
                              "GET": Api.list_losses}),
        (_route(rf"/losses/{P['loss']}/confirm"), {"POST": Api.confirm_loss}),
        (_route(r"/disruptions/overtime"), {"POST": Api.overtime}),
        (_route(r"/disruptions/vehicle-fault"), {"POST": Api.vehicle_fault}),
        (_route(r"/disruptions"), {"GET": Api.list_disruptions}),
        (_route(rf"/disruptions/{P['dis']}/impact"), {"GET": Api.impact}),
        (_route(rf"/disruptions/{P['dis']}/reschedule"),
         {"POST": Api.reschedule}),
        (_route(rf"/disruptions/{P['dis']}/close"), {"POST": Api.close_disruption}),
        (_route(rf"/notifications/{P['note']}/ack"), {"POST": Api.ack}),
        (_route(rf"/notifications(?:/{P['party']})?"),
         {"GET": Api.list_notifications}),
    ]


Api.MATCHES = _build_matches()


# --------------------------------------------------------------------------
# HTTP Handler
# --------------------------------------------------------------------------

def make_handler(api):
    class Handler(BaseHTTPRequestHandler):
        def _write(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/health":
                from service import health_payload
                self._write(200, health_payload())
                return
            if method == "GET" and path == "/attendance":
                with api.store.lock:
                    try:
                        viewer = self.headers.get("X-Viewer") \
                            or _qs(parsed.query, "viewer")
                        self._write(200, api.attendance_report(
                            parsed.query, viewer, api.now(_now(self))))
                    except DomainError as e:
                        self._write(e.status, {"error": e.code,
                                               "message": e.message,
                                               "details": e.details})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError) as e:
                self._write(400, {"error": "bad_json", "message": f"请求体不是有效 JSON: {e}"})
                return
            viewer = self.headers.get("X-Viewer") or _qs(parsed.query, "viewer")
            now = api.now(_now(self))
            try:
                with api.store.lock:
                    result = api.dispatch(method, path, body, viewer, now)
                self._write(200, result)
            except DomainError as e:
                self._write(e.status, {"error": e.code, "message": e.message,
                                       "details": e.details})

        def log_message(self, *_args):
            return

    return Handler


def _now(handler):
    return handler.headers.get("X-Now")


def build_server(port, api=None):
    api = api or Api()
    return ThreadingHTTPServer(("0.0.0.0", port), make_handler(api))
