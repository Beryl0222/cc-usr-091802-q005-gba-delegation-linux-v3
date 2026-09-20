"""线程安全的内存状态库，可整体序列化/恢复。

状态只保存业务必需信息：参与者仅有别名与凭证标识，不保存证件图像；
饮食与影像资料的可见性由 ``consents`` 中的用途授权决定，存储层不做
放行判断。
"""

import threading


def _new_id(prefix, n):
    return f"{prefix}-{n:04d}"


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.meta = {"event_id": None, "days": []}
        self.teams = {}          # team_id -> {id, name, roster:[{pid,alias,badge,meal_need}]}
        self.games = {}          # game_id -> {id, day, window:{start,end}, teams, status, actual_end}
        self.parties = {}        # party_id -> {id, type, name, contact_channel}
        self.offers = {}         # offer_id -> 可用时段申报
        self.team_windows = {}   # team_id -> [{start,end,source}]
        self.itineraries = {}    # itinerary_id -> 候选/锁定行程
        self.legs = {}           # leg_id -> 行程环节（归属 itinerary_id）
        self.consents = {}       # grant_id -> 用途授权
        self.checkins = []       # [{pid, alias, leg_id, ts, channel}]
        self.losses = {}         # loss_id -> 物料/空驶损失记录
        self.notifications = {}  # notif_id -> 通知与回执
        self.disruptions = {}    # disruption_id -> 加时/故障事件
        self.audit = []          # 关键操作审计
        self._counters = {}

    # -- 标识 ----------------------------------------------------------------

    def gen_id(self, prefix):
        with self.lock:
            n = self._counters.get(prefix, 0) + 1
            self._counters[prefix] = n
            return _new_id(prefix, n)

    # -- 审计 ----------------------------------------------------------------

    def record_audit(self, action, **detail):
        entry = {"action": action, **detail}
        with self.lock:
            self.audit.append(entry)
        return entry

    # -- 序列化 --------------------------------------------------------------

    def to_dict(self):
        with self.lock:
            return {
                "meta": self.meta,
                "teams": self.teams,
                "games": self.games,
                "parties": self.parties,
                "offers": self.offers,
                "team_windows": self.team_windows,
                "itineraries": self.itineraries,
                "legs": self.legs,
                "consents": self.consents,
                "checkins": list(self.checkins),
                "losses": self.losses,
                "notifications": self.notifications,
                "disruptions": self.disruptions,
                "audit": list(self.audit),
                "counters": dict(self._counters),
            }

    def load_dict(self, data):
        with self.lock:
            self.meta = data.get("meta", {"event_id": None, "days": []})
            self.teams = data.get("teams", {})
            self.games = data.get("games", {})
            self.parties = data.get("parties", {})
            self.offers = data.get("offers", {})
            self.team_windows = data.get("team_windows", {})
            self.itineraries = data.get("itineraries", {})
            self.legs = data.get("legs", {})
            self.consents = data.get("consents", {})
            self.checkins = data.get("checkins", [])
            self.losses = data.get("losses", {})
            self.notifications = data.get("notifications", {})
            self.disruptions = data.get("disruptions", {})
            self.audit = data.get("audit", [])
            self._counters = data.get("counters", {})
        return self
