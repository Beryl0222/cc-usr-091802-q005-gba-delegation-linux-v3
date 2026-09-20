"""把 fixtures 里的申报数据装载进服务，便于演示与联调。"""

import json
from pathlib import Path

from . import timeutil

DEFAULT_SEED = Path(__file__).resolve().parent.parent / "fixtures" / "sample.json"


def load_seed(service, path=None):
    """根据种子 JSON 完成基础申报。返回装载的记录条数。

    种子文件是幂等覆盖式的：同一 ID 重复注册会更新而非报错。
    """
    seed_path = Path(path) if path else DEFAULT_SEED
    data = json.loads(seed_path.read_text(encoding="utf-8"))
    count = 0

    if data.get("now"):
        service.set_clock(lambda: timeutil.parse_ts(data["now"]))

    for leg in data.get("travel_matrix", []):
        a, b, minutes = leg["from_zone"], leg["to_zone"], int(leg["minutes"])
        service.travel_matrix[(a, b)] = minutes
        count += 1

    for t in data.get("team_details", []):
        service.register_team(t["team_id"], t["name"], t["home_region"])
        count += 1

    for m in data.get("members", []):
        service.register_member(
            m["team_id"], m["alias"], m["credential_ref"],
            member_id=m.get("member_id"),
            dietary_tags=m.get("dietary_tags", []),
            accessibility_needs=m.get("accessibility_needs", []),
            consents=m.get("consents", m.get("photo_consents", [])))
        count += 1

    for c in data.get("competitions", []):
        service.register_competition(
            c["competition_id"], c["label"], c["team_ids"],
            c["start"], c["end"], source=c.get("source", "fixture"))
        count += 1

    for w in data.get("team_windows", []):
        service.register_team_window(w["team_id"], w["start"], w["end"],
                                     w.get("note", ""))
        count += 1

    for h in data.get("hosts", []):
        service.register_host(h["host_id"], h["name"], h["kind"],
                              contact=h.get("contact"))
        count += 1

    for o in data.get("offerings", []):
        service.register_offering(
            o["host_id"], o["kind"], o["title"], o["location"], o["zone"],
            o["windows"], o["capacity"], o.get("languages", []),
            o.get("accessibility", []), o["confirm_deadline"],
            provides_meal=o.get("provides_meal", False),
            media_requested=o.get("media_requested", False),
            material_cost_per_seat=o.get("material_cost_per_seat"),
            offering_id=o.get("offering_id"))
        count += 1

    for f in data.get("fleets", []):
        service.register_fleet(
            f["host_id"], f["name"], f["windows"], f["seats_per_vehicle"],
            f["vehicle_count"], f.get("wheelchair", False),
            f.get("zones", []), f["confirm_deadline"],
            deadhead_fee=f.get("deadhead_fee"), fleet_id=f.get("fleet_id"))
        count += 1

    return count
