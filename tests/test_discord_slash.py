"""Native Discord slash commands: the registration schema, interaction parsing,
and the gateway's interaction -> dispatch flow (owner-gated, deferred, answered).

No real websocket or network: _handle_interaction is driven directly, with the
defer/respond REST helpers stubbed to capture what warden would send back.
"""
import asyncio

import pytest

from warden.backends.replay import ReplayBackend
from warden.notifier import discord as dc
from warden.notifier import discord_gateway as gw
from warden.notifier.discord_commands import COMMANDS, dispatch, parse_interaction

OWNER = "owner-111"
OTHER = "stranger-999"


@pytest.fixture
def dconfig(config):
    config.notify_channel = "discord"
    config.discord_bot_token = "bot-token-abc"
    config.discord_channel_id = "chan-42"
    config.discord_owner_id = OWNER
    return config


def _interaction(name, options=None, author=OWNER):
    return {
        "id": "int-1", "token": "tok-xyz", "type": 2,
        "data": {"name": name, "options": options or []},
        "member": {"user": {"id": author}},
    }


# --- registration schema (what the / picker shows) ---

def test_commands_schema_matches_picker_expectations():
    by_name = {c["name"]: c for c in COMMANDS}
    assert set(by_name) == {"status", "diagnose", "user-stats"}
    assert "options" not in by_name["status"]                 # no params
    q = by_name["diagnose"]["options"][0]
    assert q["name"] == "question" and q["required"] is True  # required param
    u = by_name["user-stats"]["options"][0]
    assert u["name"] == "user" and u["required"] is False     # optional param


def test_register_commands_targets_guild_when_set(dconfig, monkeypatch):
    monkeypatch.setattr(dc, "application_id", lambda c: "app-9")
    sent = {}
    monkeypatch.setattr(dc, "_request",
                        lambda method, token, url, **kw: sent.update(method=method, url=url, json=kw.get("json")) or _Resp())
    dconfig.discord_guild_id = "guild-7"
    scope = dc.register_commands(dconfig, COMMANDS)
    assert "guild guild-7" in scope
    assert sent["method"] == "PUT" and "/guilds/guild-7/commands" in sent["url"]
    assert sent["json"] == COMMANDS


class _Resp:
    def json(self): return {}
    def raise_for_status(self): return None


# --- interaction parsing ---

def test_parse_interaction_extracts_name_arg_author():
    it = _interaction("diagnose", [{"name": "question", "value": "why is plex buffering"}])
    assert parse_interaction(it) == ("diagnose", "why is plex buffering", OWNER)


def test_parse_interaction_no_options_gives_empty_arg():
    assert parse_interaction(_interaction("status")) == ("status", "", OWNER)


def test_parse_interaction_ignores_non_command():
    assert parse_interaction({"type": 3, "data": {}}) is None  # a component, not a command


# --- gateway interaction -> dispatch flow ---

def _stub_responses(monkeypatch):
    """Capture defer + final-edit instead of hitting Discord."""
    captured = {"deferred": False, "reply": None}
    monkeypatch.setattr(dc, "interaction_defer",
                        lambda iid, tok: captured.update(deferred=True))
    monkeypatch.setattr(dc, "interaction_respond",
                        lambda config, tok, text: captured.update(reply=text))
    return captured


def test_gateway_status_defers_then_answers(dconfig, store, channel, monkeypatch):
    captured = _stub_responses(monkeypatch)
    backend = ReplayBackend({"docker_ps": [{"name": "plex", "state": "running", "status": "Up"}],
                             "disk_usage": [], "torrents": []})
    asyncio.run(gw._handle_command(_interaction("status"), dconfig, backend, store, channel))
    assert captured["deferred"] is True                 # ACKed within the 3s window
    assert "warden status" in captured["reply"]         # then the real health digest


def test_gateway_rejects_non_owner(dconfig, store, channel, monkeypatch):
    captured = _stub_responses(monkeypatch)
    backend = ReplayBackend({"docker_ps": [], "disk_usage": [], "torrents": []})
    asyncio.run(gw._handle_command(_interaction("status", author=OTHER),
                                       dconfig, backend, store, channel))
    assert captured["deferred"] is True
    assert "owner" in captured["reply"].lower()
    assert channel.sent == []                           # command never ran


def test_gateway_diagnose_acks_to_channel(dconfig, store, channel, monkeypatch):
    captured = _stub_responses(monkeypatch)
    import warden.notifier.discord_commands as cmds

    async def fake_diag(q, *a, **k):
        return None
    monkeypatch.setattr(cmds, "run_diagnose", fake_diag)

    it = _interaction("diagnose", [{"name": "question", "value": "why is plex slow"}])
    asyncio.run(gw._handle_command(it, dconfig, ReplayBackend({}), store, channel))
    # diagnose streams to the channel, so the interaction reply points there
    assert "chan-42" in captured["reply"]
    assert any("investigating" in s for s in channel.sent)


# --- shared dispatch (used by both message + gateway paths) ---

def test_dispatch_unknown_command_returns_none(dconfig, store, channel):
    assert dispatch("bogus", "", dconfig, ReplayBackend({}), store, channel) is None
