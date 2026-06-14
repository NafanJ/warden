"""Interactive action buttons on warden's Discord alerts: the component schema,
the click handler (shared with the YES/NO + tier paths), and the gateway's
component-interaction flow (owner-gated, deferred-update, buttons stripped after).
"""
import asyncio

import pytest

from warden.backends.replay import ReplayBackend
from warden.notifier import discord as dc
from warden.notifier import discord_gateway as gw
from warden.notifier.components import (approval_buttons, handle_component,
                                        incident_buttons)

OWNER = "owner-111"
OTHER = "stranger-999"


@pytest.fixture
def dconfig(config):
    config.notify_channel = "discord"
    config.discord_bot_token = "bot-token-abc"
    config.discord_channel_id = "chan-42"
    config.discord_owner_id = OWNER
    return config


def _ids(components):
    return [b["custom_id"] for row in components for b in row["components"]]


# --- button schema ---

def test_approval_buttons_carry_action_id():
    assert _ids(approval_buttons(7)) == ["approve:7", "reject:7"]


def test_incident_buttons_offer_restart_only_for_container_down():
    down = _ids(incident_buttons(5, "container_down", "plex"))
    assert down == ["restart:plex", "diag:5", "dismiss:5"]
    disk = _ids(incident_buttons(6, "disk_pressure"))
    assert disk == ["diag:6", "dismiss:6"]            # nothing to one-tap restart


# --- click handler ---

def _queue_delete(store) -> int:
    return store.queue_action(
        1, "delete_paths",
        {"paths": ["/mnt/Modi/Kodi/downloads/complete/old"], "reason": "orphaned"},
        2, "Delete 1 path(s)")


def test_approve_button_executes_the_action(dconfig, store, channel):
    backend = ReplayBackend({})
    aid = _queue_delete(store)
    out = handle_component(f"approve:{aid}", dconfig, backend, store, channel)
    assert "Approved" in out
    assert store.get_action(aid)["status"] == "executed"
    assert backend.actions_taken[0]["action"] == "delete_paths"


def test_reject_button_denies_the_action(dconfig, store, channel):
    aid = _queue_delete(store)
    out = handle_component(f"reject:{aid}", dconfig, ReplayBackend({}), store, channel)
    assert "Rejected" in out
    assert store.get_action(aid)["status"] == "denied"


def test_restart_button_restarts_via_tier_gate(dconfig, store, channel):
    backend = ReplayBackend({})
    out = handle_component("restart:sonarr", dconfig, backend, store, channel)
    assert "Restarted sonarr" in out
    assert backend.actions_taken[0] == {"action": "container_restart", "name": "sonarr"}
    # routed through decide_tool, so it's audited (and shows in the daily summary)
    audited = store.conn.execute(
        "SELECT tool, decision FROM audit WHERE tool='container_restart'").fetchone()
    assert audited["decision"] == "allowed"


def test_dismiss_button_resolves_the_incident(dconfig, store, channel):
    iid = store.open_incident("disk_pressure:/mnt/Modi", "disk_pressure", "/mnt/Modi at 93%")
    out = handle_component(f"dismiss:{iid}", dconfig, ReplayBackend({}), store, channel)
    assert "Dismissed" in out
    assert store.unresolved_incidents() == []


def test_unknown_button_is_safe(dconfig, store, channel):
    assert "unknown" in handle_component("bogus:1", dconfig, ReplayBackend({}), store, channel)


# --- gateway component flow ---

def _component_interaction(custom_id, author=OWNER):
    return {"id": "int-9", "token": "tok-9", "type": 3,
            "data": {"custom_id": custom_id}, "member": {"user": {"id": author}}}


def test_gateway_component_defers_update_then_strips_buttons(dconfig, store, channel, monkeypatch):
    cap = {"deferred_type": None, "reply": None, "components": "unset"}
    monkeypatch.setattr(dc, "interaction_defer",
                        lambda iid, tok, deferred_type=5: cap.update(deferred_type=deferred_type))
    monkeypatch.setattr(dc, "interaction_respond",
                        lambda config, tok, text, components=None: cap.update(reply=text, components=components))
    iid = store.open_incident("disk_pressure:/x", "disk_pressure", "/x full")
    asyncio.run(gw._handle_component(_component_interaction(f"dismiss:{iid}"),
                                     dconfig, ReplayBackend({}), store, channel))
    assert cap["deferred_type"] == dc.DEFERRED_UPDATE   # edits the button's own message
    assert "Dismissed" in cap["reply"]
    assert cap["components"] == []                       # buttons removed so it can't re-fire


def test_gateway_component_rejects_non_owner(dconfig, store, channel, monkeypatch):
    eph = {}
    monkeypatch.setattr(dc, "interaction_reply_ephemeral",
                        lambda iid, tok, text: eph.update(text=text))
    deferred = {"called": False}
    monkeypatch.setattr(dc, "interaction_defer",
                        lambda *a, **k: deferred.update(called=True))
    aid = _queue_delete(store)
    asyncio.run(gw._handle_component(_component_interaction(f"approve:{aid}", author=OTHER),
                                     dconfig, ReplayBackend({}), store, channel))
    assert "owner" in eph["text"].lower()
    assert deferred["called"] is False                   # never deferred / edited the message
    assert store.get_action(aid)["status"] == "pending"  # action untouched
