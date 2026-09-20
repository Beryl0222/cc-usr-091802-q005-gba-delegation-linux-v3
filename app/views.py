"""最小可见视图。

接待方只能看到**完成服务所必需**的信息：

* 场地接待方（校园/企业/市集）：时间、人数、必要的翻译与无障碍需求、
  签到所需的别名与凭证标识；
* 车队：时间、人数、路线与无障碍乘车需求，不接触饮食与影像资料；
* 团体饮食明细与影像公开意愿按用途授权放行（见 :mod:`app.registry`
  的 ``meal`` / ``image`` 授权），授权撤销后立即不可见；
* 交流办公室（office）可见调度全貌。

任何视图都不返回证件图像——系统根本不保存。
"""

from .registry import has_consent

PURPOSE_CATERING = "catering"
PURPOSE_PUBLICITY = "publicity"

SITE_TYPES = {"campus", "industry", "market"}


def _viewer_party(store, viewer):
    if viewer and viewer.startswith("party:"):
        return viewer.split(":", 1)[1]
    return None


def public_roster(store, leg, viewer):
    """按观看者裁剪后的名单。"""
    team = store.teams[leg["team_id"]]
    party_id = _viewer_party(store, viewer)
    is_office = viewer == "office"
    is_self_team = viewer == f"team:{leg['team_id']}"
    serving = party_id == leg["party_id"]
    if not (is_office or is_self_team or serving):
        return None

    offer = store.offers.get(leg["offer_id"])
    is_fleet = bool(offer and offer["type"] == "fleet")

    # 用途授权：缺授权即不可见
    can_see_meal = is_office or is_self_team or (
        serving and not is_fleet
        and has_consent(store, leg["team_id"], "meal", party_id, PURPOSE_CATERING))
    can_see_image = is_office or is_self_team or (
        serving and not is_fleet
        and has_consent(store, leg["team_id"], "image", party_id, PURPOSE_PUBLICITY))

    # 车队只需时间/人数/路线与无障碍乘车汇总，不接触个人标识、饮食与影像
    if is_fleet and serving:
        return []
    # 签到台需要别名+凭证标识来完成核验
    can_identify = is_office or is_self_team or serving

    out = []
    for p in team["roster"]:
        item = {"pid": p["pid"]}
        if can_identify:
            item["alias"] = p["alias"]
            item["badge"] = p["badge"]
        acc = [a for a in p.get("accessibility", [])]
        if acc:
            item["accessibility"] = acc
        if can_see_meal and p.get("meal_need"):
            item["meal_need"] = p["meal_need"]
        if can_see_image:
            item["image"] = p.get("image", {})
        out.append(item)
    return out


def leg_view(store, leg, viewer):
    party_id = _viewer_party(store, viewer)
    is_office = viewer == "office"
    is_self_team = viewer == f"team:{leg['team_id']}"
    serving = party_id == leg["party_id"]
    if not (is_office or is_self_team or serving):
        return None

    offer = store.offers.get(leg["offer_id"], {})
    is_fleet = offer.get("type") == "fleet"
    view = {
        "id": leg["id"],
        "itinerary_id": leg["itinerary_id"],
        "team_id": leg["team_id"],
        "kind": leg["kind"],
        "party_id": leg["party_id"],
        "window": leg["window"],
        "headcount": leg["headcount"],
        "status": leg["status"],
        "locked": leg["locked"],
        "confirmations": _confirmations_view(leg, viewer, is_office),
    }
    if is_office or is_self_team or not is_fleet:
        view["location"] = offer.get("location")
    if is_fleet:
        # 车队只看到完成接驳所必需的路线信息
        view["route"] = offer.get("route")
        view["vehicle_id"] = offer.get("vehicle_id")
    elif is_office or is_self_team:
        view["offer_id"] = leg["offer_id"]
        view["slot_id"] = leg["slot_id"]

    team = store.teams.get(leg["team_id"], {})
    need_acc = sorted({a for p in team.get("roster", [])
                       for a in p.get("accessibility", [])})
    if is_fleet:
        # 车队获得的是乘车所需的汇总，而非个人明细
        counts = {}
        for p in team.get("roster", []):
            for a in p.get("accessibility", []):
                counts[a] = counts.get(a, 0) + 1
        view["accessibility_summary"] = counts
    elif need_acc:
        view["accessibility_needs"] = need_acc
    if not is_fleet:
        view["language_needs"] = team.get("languages", [])

    # 名单仅在服务尚未结束时才是“完成服务所必需”
    if leg["status"] in ("proposed", "proposed-locked", "confirmed",
                         "departed", "in_progress"):
        roster = public_roster(store, leg, viewer)
        if roster is not None:
            view["roster"] = roster
    return view


def _confirmations_view(leg, viewer, is_office):
    """参与方只看到谁已确认，办公室看到时间戳。"""
    if is_office:
        return leg["confirmations"]
    return {actor: True for actor in leg["confirmations"]}


def itinerary_view(store, itinerary, viewer):
    legs = []
    for lid in itinerary["legs"]:
        v = leg_view(store, store.legs[lid], viewer)
        if v is not None:
            legs.append(v)
    if not legs:
        return None
    return {
        "id": itinerary["id"],
        "team_id": itinerary["team_id"],
        "status": itinerary["status"],
        "created_at": itinerary["created_at"],
        "legs": legs,
    }


def visible_itineraries(store, viewer):
    out = []
    for it in store.itineraries.values():
        v = itinerary_view(store, it, viewer)
        if v is not None:
            out.append(v)
    return out
