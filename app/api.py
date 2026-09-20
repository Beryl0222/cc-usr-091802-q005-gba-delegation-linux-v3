"""HTTP 路由层：把 JSON 请求映射到 :class:`DispatchService`。

不引入第三方框架，沿用项目标准库 ``http.server`` 基线。
所有业务错误由 :class:`~app.errors.ApiError` 统一转成 JSON。
"""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import ApiError, bad_request

SERVICE_ID = "gba-delegation-control"
SERVICE_NAME = "湾区代表队行程联控"

ROUTES = [
    ("POST", r"^/admin/seed$", "seed"),
    ("GET", r"^/health$", "health"),

    ("POST", r"^/teams$", "create_team"),
    ("GET", r"^/teams$", "list_teams"),
    ("POST", r"^/teams/(?P<team_id>[\w.-]+)/members$", "create_member"),
    ("POST", r"^/teams/(?P<team_id>[\w.-]+)/windows$", "create_window"),
    ("POST", r"^/teams/(?P<team_id>[\w.-]+)/candidates$", "build_candidates"),

    ("POST", r"^/hosts$", "create_host"),
    ("GET", r"^/hosts$", "list_hosts"),
    ("POST", r"^/hosts/(?P<host_id>[\w.-]+)/offerings$", "create_offering"),
    ("POST", r"^/hosts/(?P<host_id>[\w.-]+)/fleets$", "create_fleet"),
    ("GET", r"^/offerings$", "list_offerings"),
    ("GET", r"^/fleets$", "list_fleets"),

    ("POST", r"^/competitions$", "create_competition"),
    ("GET", r"^/competitions$", "list_competitions"),

    ("GET", r"^/plans$", "list_plans"),
    ("GET", r"^/plans/(?P<plan_id>[\w.-]+)$", "get_plan"),
    ("POST", r"^/plans/(?P<plan_id>[\w.-]+)/confirmations$", "confirm"),
    ("POST", r"^/plans/(?P<plan_id>[\w.-]+)/lock$", "lock"),
    ("POST", r"^/plans/(?P<plan_id>[\w.-]+)/cancel$", "cancel_plan"),
    ("POST", r"^/plans/(?P<plan_id>[\w.-]+)/replan$", "apply_replan"),

    ("POST", r"^/items/(?P<item_id>[\w.-]+)/depart$", "mark_enroute"),
    ("POST", r"^/items/(?P<item_id>[\w.-]+)/complete$", "complete_item"),
    ("POST", r"^/items/(?P<item_id>[\w.-]+)/checkins$", "checkin"),
    ("GET", r"^/items/(?P<item_id>[\w.-]+)/manifest$", "item_manifest"),

    ("GET", r"^/checkins/summary$", "checkin_summary"),

    ("POST", r"^/incidents$", "create_incident"),
    ("GET", r"^/incidents/(?P<incident_id>[\w.-]+)$", "get_incident"),

    ("GET", r"^/losses$", "list_losses"),
    ("POST", r"^/losses/(?P<loss_id>[\w.-]+)/confirmations$", "confirm_loss"),

    ("GET", r"^/notifications$", "list_notifications"),
    ("POST", r"^/notifications/(?P<nid>[\w.-]+)/ack$", "ack_notification"),

    ("GET", r"^/office/overview$", "office_overview"),
]
COMPILED = [(method, re.compile(pattern), action)
            for method, pattern, action in ROUTES]


def make_handler(service, seed_loader=None):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise bad_request(f"请求体不是合法 JSON：{exc}")
            if not isinstance(data, dict):
                raise bad_request("请求体必须是 JSON 对象")
            return data

        def _dispatch(self, method):
            parsed = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            for route_method, pattern, action in COMPILED:
                if route_method != method:
                    continue
                match = pattern.match(parsed.path)
                if not match:
                    continue
                payload = self._read_json() if method == "POST" else {}
                try:
                    result = HANDLERS[action](service, payload,
                                              {**match.groupdict(), **query})
                except ApiError as exc:
                    self._send(exc.status, exc.to_dict())
                    return
                status, body = result if isinstance(result, tuple) else (200, result)
                self._send(status, body)
                return
            self._send(404, {"error": "not_found", "message": f"无此路由：{method} {parsed.path}"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, *_args):
            return

    return Handler


def health_payload():
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


# ---------------------------------------------------------------- handlers
def h_health(service, body, params):
    return health_payload()


def h_seed(service, body, params):
    from .bootstrap import load_seed
    count = load_seed(service, body.get("path"))
    return 201, {"loaded": count}


def h_create_team(service, b, p):
    return 201, service.register_team(b["team_id"], b["name"], b["home_region"])


def h_list_teams(service, b, p):
    return service.list_teams()


def h_create_member(service, b, p):
    return 201, service.register_member(
        p["team_id"], b["alias"], b["credential_ref"],
        member_id=b.get("member_id"), dietary_tags=b.get("dietary_tags"),
        accessibility_needs=b.get("accessibility_needs"),
        consents=b.get("consents", b.get("photo_consents", [])))


def h_create_window(service, b, p):
    return 201, service.register_team_window(
        p["team_id"], b["start"], b["end"], b.get("note", ""))


def h_build_candidates(service, b, p):
    return 201, {"plans": service.build_candidates(
        p["team_id"], b["events"], member_ids=b.get("member_ids"),
        origin_zone=b.get("origin_zone"),
        include_endpoint_transfer=b.get("include_endpoint_transfer", True),
        cap=int(b.get("cap", 6)))}


def h_create_host(service, b, p):
    return 201, service.register_host(
        b["host_id"], b["name"], b["kind"], contact=b.get("contact"))


def h_list_hosts(service, b, p):
    return service.list_hosts()


def h_create_offering(service, b, p):
    return 201, service.register_offering(
        p["host_id"], b["kind"], b["title"], b["location"], b["zone"],
        b["windows"], b["capacity"], b.get("languages", []),
        b.get("accessibility", []), b["confirm_deadline"],
        provides_meal=b.get("provides_meal", False),
        media_requested=b.get("media_requested", False),
        material_cost_per_seat=b.get("material_cost_per_seat"),
        offering_id=b.get("offering_id"))


def h_create_fleet(service, b, p):
    return 201, service.register_fleet(
        p["host_id"], b["name"], b["windows"], b["seats_per_vehicle"],
        b["vehicle_count"], b.get("wheelchair", False),
        b.get("zones", []), b["confirm_deadline"],
        deadhead_fee=b.get("deadhead_fee"), fleet_id=b.get("fleet_id"))


def h_list_offerings(service, b, p):
    return service.list_offerings()


def h_list_fleets(service, b, p):
    return service.list_fleets()


def h_create_competition(service, b, p):
    return 201, service.register_competition(
        b["competition_id"], b["label"], b["team_ids"], b["start"], b["end"],
        source=b.get("source", "external"))


def h_list_competitions(service, b, p):
    return service.list_competitions()


def h_list_plans(service, b, p):
    return service.list_plans()


def h_get_plan(service, b, p):
    return service.get_plan(p["plan_id"])


def h_confirm(service, b, p):
    return service.confirm(p["plan_id"], b["party"], item_id=b.get("item_id"))


def h_lock(service, b, p):
    return service.lock(p["plan_id"])


def h_cancel_plan(service, b, p):
    return service.cancel_plan(p["plan_id"])


def h_apply_replan(service, b, p):
    return service.apply_replan(p["plan_id"], int(b["option_index"]))


def h_mark_enroute(service, b, p):
    return service.mark_enroute(p["item_id"])


def h_complete_item(service, b, p):
    return service.complete(p["item_id"])


def h_checkin(service, b, p):
    return 201, service.checkin(p["item_id"], b["credential_ref"])


def h_item_manifest(service, b, p):
    return service.item_manifest(p["item_id"], p["viewer"])


def h_checkin_summary(service, b, p):
    return service.checkin_summary(p.get("plan_id"))


def h_create_incident(service, b, p):
    return 201, service.register_incident(
        b["type"], at=b.get("at"), delay_minutes=b.get("delay_minutes"),
        competition_id=b.get("competition_id"), fleet_id=b.get("fleet_id"),
        description=b.get("description", ""))


def h_get_incident(service, b, p):
    return service.incident(p["incident_id"])


def h_list_losses(service, b, p):
    return service.list_losses(status=p.get("status"))


def h_confirm_loss(service, b, p):
    return service.confirm_loss(p["loss_id"], b["party"],
                                amount=b.get("amount"), note=b.get("note"))


def h_list_notifications(service, b, p):
    return service.list_notifications(target=p.get("target"))


def h_ack_notification(service, b, p):
    return service.ack_notification(p["nid"], b["party"])


def h_office_overview(service, b, p):
    return service.office_overview()


HANDLERS = {
    "health": h_health,
    "seed": h_seed,
    "create_team": h_create_team,
    "list_teams": h_list_teams,
    "create_member": h_create_member,
    "create_window": h_create_window,
    "build_candidates": h_build_candidates,
    "create_host": h_create_host,
    "list_hosts": h_list_hosts,
    "create_offering": h_create_offering,
    "create_fleet": h_create_fleet,
    "list_offerings": h_list_offerings,
    "list_fleets": h_list_fleets,
    "create_competition": h_create_competition,
    "list_competitions": h_list_competitions,
    "list_plans": h_list_plans,
    "get_plan": h_get_plan,
    "confirm": h_confirm,
    "lock": h_lock,
    "cancel_plan": h_cancel_plan,
    "apply_replan": h_apply_replan,
    "mark_enroute": h_mark_enroute,
    "complete_item": h_complete_item,
    "checkin": h_checkin,
    "item_manifest": h_item_manifest,
    "checkin_summary": h_checkin_summary,
    "create_incident": h_create_incident,
    "get_incident": h_get_incident,
    "list_losses": h_list_losses,
    "confirm_loss": h_confirm_loss,
    "list_notifications": h_list_notifications,
    "ack_notification": h_ack_notification,
    "office_overview": h_office_overview,
}


def build_server(service, port=8000):
    return ThreadingHTTPServer(("0.0.0.0", port), make_handler(service))
