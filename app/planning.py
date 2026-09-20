"""候选行程规划。

输入：队伍的可用窗口（含赛后派生窗口）、正式比赛的不可移动时段、
各接待方申报的容量/翻译/无障碍/最晚确认点。

输出：每条需求的可行候选与被拒原因。候选**不**预占资源；容量在参与方
确认后才锁定（见 :mod:`app.lifecycle`）。
"""

from datetime import time as dtime

from .errors import DomainError, NotFound
from .registry import slot_remaining
from .timeutil import iso, mins, overlap, parse_ts

RECOVERY_MIN = 15          # 赛后恢复缓冲
POSTGAME_HORIZON_MIN = 240
DAY_CEILING = dtime(21, 0)

KINDS = ("campus", "industry", "market", "fleet")


# --------------------------------------------------------------------------
# 队伍时间约束
# --------------------------------------------------------------------------

def game_block(store, game, recovery=True):
    """比赛对参赛队形成的不可占用区间：开球到（可能加时后的）实际结束，
    另含赛后恢复缓冲。"""
    start = parse_ts(game["window"]["start"])
    end = parse_ts(game["actual_end"] or game["window"]["end"])
    if recovery:
        end = end + mins(RECOVERY_MIN)
    return start, end


def _day_ceiling(dt):
    ceil = dt.replace(hour=DAY_CEILING.hour, minute=0, second=0, microsecond=0)
    if dt >= ceil:
        ceil += mins(1440)
    return ceil


def availability_intervals(store, team_id):
    """返回 (可用区间列表, 比赛封锁区间列表)。

    可用 = 申报窗口 ∪ 赛后派生窗口（实际结束优先，含恢复缓冲），
    候选还须躲开全部比赛封锁区间。
    """
    if team_id not in store.teams:
        raise NotFound("队伍", team_id)
    raw = []
    for w in store.team_windows.get(team_id, []):
        raw.append((parse_ts(w["start"]), parse_ts(w["end"]), w.get("source", "declared")))

    blocks = []
    for game in store.games.values():
        if team_id in game["teams"]:
            bstart, bend = game_block(store, game)  # bend 已含恢复缓冲
            blocks.append((bstart, bend))
            post_start = bend
            post_end = min(post_start + mins(POSTGAME_HORIZON_MIN), _day_ceiling(post_start))
            if post_end > post_start:
                raw.append((post_start, post_end, f"post-game:{game['id']}"))

    # 合并重叠的可用窗口
    raw.sort(key=lambda x: x[0])
    merged = []
    for start, end, source in raw:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end, merged[-1][2] + "+" + source)
        else:
            merged.append([start, end, source])

    free = []
    blocks_sorted = sorted(blocks)
    for start, end, source in merged:
        pieces = [(start, end)]
        for bs, be in blocks_sorted:
            next_pieces = []
            for ps, pe in pieces:
                if not overlap(ps, pe, bs, be):
                    next_pieces.append((ps, pe))
                else:
                    if ps < bs:
                        next_pieces.append((ps, bs))
                    if be < pe:
                        next_pieces.append((be, pe))
            pieces = next_pieces
        for ps, pe in pieces:
            if pe > ps:
                free.append((ps, pe, source))
    return free, blocks


# --------------------------------------------------------------------------
# 单条需求的候选
# --------------------------------------------------------------------------

def _covers(offer_features, need):
    missing_lang = [l for l in need.get("languages", [])
                    if l not in offer_features["languages"]]
    missing_acc = [a for a in need.get("accessibility", [])
                   if a not in offer_features["accessibility"]]
    return missing_lang, missing_acc


def options_for_leg(store, team_id, req, clock, exclude_leg_id=None):
    kind = req["kind"]
    if kind not in KINDS:
        raise DomainError("bad_kind", f"未知环节类型: {kind}")
    headcount = int(req["headcount"])
    duration_min = int(req.get("duration_min", 0))
    free, blocks = availability_intervals(store, team_id)
    now = parse_ts(clock() if callable(clock) else clock)

    options, rejected = [], []
    for offer in store.offers.values():
        if offer["type"] != kind or offer["status"] != "active":
            continue
        m_lang, m_acc = _covers(offer["features"], req)
        route_ok = True
        if kind == "fleet":
            route = offer["route"]
            route_ok = bool(route and route.get("from") == req.get("from")
                            and route.get("to") == req.get("to"))
            if route_ok and duration_min == 0:
                duration_min = route["duration_min"]

        for slot in offer["slots"]:
            reasons = []
            s_start, s_end = parse_ts(slot["start"]), parse_ts(slot["end"])
            remaining = slot_remaining(store, slot)
            if exclude_leg_id:
                for lock in slot["locks"]:
                    if lock["leg_id"] == exclude_leg_id:
                        remaining += lock["qty"]
            if remaining < headcount:
                reasons.append(f"容量不足: 余 {remaining} / 需 {headcount}")
            if m_lang:
                reasons.append("缺少翻译: " + ",".join(m_lang))
            if m_acc:
                reasons.append("缺少无障碍条件: " + ",".join(m_acc))
            if kind == "fleet" and not route_ok:
                reasons.append(f"路线不符: 需 {req.get('from')}→{req.get('to')}")

            # 最晚确认点：过点不消灭候选，但显著标记，提示办公室必须再确认
            confirm_by_passed = bool(
                offer["confirm_by"] and now > parse_ts(offer["confirm_by"]))

            # 与可用窗口求交，扣除比赛封锁
            isect = None
            for f_start, f_end, source in free:
                lo, hi = max(s_start, f_start), min(s_end, f_end)
                if hi > lo:
                    # 再剔除比赛封锁（理论上 free 已剔除，双重保险）
                    for bs, be in blocks:
                        if overlap(lo, hi, bs, be):
                            lo = max(lo, be)
                    if hi > lo:
                        isect = (lo, hi, source)
                        break
            if isect is None:
                reasons.append("时段落在比赛或不可用窗口内")
            elif duration_min and (isect[1] - isect[0]) < mins(duration_min):
                reasons.append(f"可用时长不足: 需 {duration_min} 分钟")

            if reasons:
                rejected.append({
                    "offer_id": offer["id"], "slot_id": slot["id"],
                    "party_id": offer["party_id"], "reasons": reasons,
                })
                continue

            win_start = isect[0]
            win_end = win_start + mins(duration_min) if duration_min else isect[1]
            options.append({
                "offer_id": offer["id"],
                "slot_id": slot["id"],
                "party_id": offer["party_id"],
                "kind": kind,
                "start": iso(win_start),
                "end": iso(win_end),
                "headcount": headcount,
                "remaining_capacity": remaining,
                "confirm_by": offer["confirm_by"],
                "confirm_by_passed": confirm_by_passed,
                "within_availability": isect[2],
                "location": offer["location"],
                "route": offer["route"] if kind == "fleet" else None,
            })

    options.sort(key=lambda o: o["start"])
    return {"kind": kind, "options": options, "rejected": rejected}


def build_candidates(store, team_id, payload, clock):
    if team_id not in store.teams:
        raise NotFound("队伍", team_id)
    legs = payload.get("legs", [])
    if not legs:
        raise DomainError("empty_request", "至少给出一条环节需求")
    results = [options_for_leg(store, team_id, req, clock) for req in legs]
    feasible = all(r["options"] for r in results)
    return {
        "team_id": team_id,
        "feasible": feasible,
        "availability": [
            {"start": iso(s), "end": iso(e), "source": src}
            for s, e, src in availability_intervals(store, team_id)[0]
        ],
        "legs": results,
    }
