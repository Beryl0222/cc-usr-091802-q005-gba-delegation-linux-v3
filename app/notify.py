"""通知与回执：改约、取消、扰动等都向相关参与方留一条通知，
接收方的确认以回执形式回收，交流办公室可查看送达与确认状态。
"""

from .errors import Conflict, NotFound
from .timeutil import now_iso


def notify(store, party_id, template, variables, clock, *, about=None, channel=None):
    actor = party_id
    if party_id.startswith("party:"):
        party_id = party_id.split(":", 1)[1]
    party = store.parties.get(party_id)
    notif = {
        "id": store.gen_id("note"),
        "party_id": party_id,
        "actor": actor,
        "channel": channel or (party["contact_channel"] if party else "phone"),
        "template": template,
        "variables": variables,
        "about": about,
        "created_at": now_iso(clock),
        "receipt": None,
    }
    store.notifications[notif["id"]] = notif
    store.record_audit("notified", notif_id=notif["id"], party_id=party_id,
                       template=template, about=about)
    return notif


def notify_many(store, party_ids, template, variables, clock, **kw):
    return [notify(store, p, template, variables, clock, **kw) for p in sorted(set(party_ids))]


def acknowledge(store, notif_id, clock, channel=None):
    notif = store.notifications.get(notif_id)
    if notif is None:
        raise NotFound("通知", notif_id)
    if notif["receipt"] is not None:
        raise Conflict("already_received", "该通知已回执")
    notif["receipt"] = {"at": now_iso(clock), "channel": channel or notif["channel"]}
    store.record_audit("receipt", notif_id=notif_id, party_id=notif["party_id"])
    return notif


def receipts_for(store, about):
    """某事件（扰动/取消）关联通知的送达与回执汇总。"""
    out = []
    for n in store.notifications.values():
        if n.get("about") == about:
            out.append({
                "notif_id": n["id"],
                "party_id": n["party_id"],
                "channel": n["channel"],
                "created_at": n["created_at"],
                "received": n["receipt"] is not None,
                "receipt": n["receipt"],
            })
    return out
