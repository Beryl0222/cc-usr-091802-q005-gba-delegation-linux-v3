"""种子数据装载：支持极简格式（仅队伍/边赛标识）与完整演练格式。"""

from . import registry


def load_seed(store, data, clock="2026-09-12T08:00:00+08:00"):
    """clock 固定种子内授权等记录的时间戳。"""
    registry.bootstrap(store, data)
    counts = {"teams": 0, "games": 0, "parties": 0, "offers": 0, "consents": 0}

    for t in data.get("teams", []):
        if isinstance(t, str):
            registry.register_team(store, {"id": t, "name": t})
        else:
            team = registry.register_team(store, t)
            if t.get("roster"):
                registry.set_roster(store, team["id"], t["roster"])
            if t.get("availability"):
                registry.declare_availability(store, team["id"], t["availability"])
        counts["teams"] += 1

    for g in data.get("games", []):
        registry.register_game(store, g)
        counts["games"] += 1

    for p in data.get("parties", []):
        registry.register_party(store, p)
        counts["parties"] += 1

    for o in data.get("offers", []):
        registry.register_offer(store, o)
        counts["offers"] += 1

    for c in data.get("consents", []):
        registry.grant_consent(store, c, lambda: clock)
        counts["consents"] += 1

    store.record_audit("seed_loaded", **counts)
    return counts
