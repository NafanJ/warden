"""Discord channel + approval poller.

The poller reuses handle_reply, so these focus on what's Discord-specific:
owner allow-listing by author id, ignoring chatter, cursor advancement, and
that an owner 'YES <id>' drives the same execute path as WhatsApp.
"""
import httpx
import pytest

from warden.backends.replay import ReplayBackend
from warden.notifier import discord as dc
from warden.notifier.discord import API, APPROVE_EMOJI, REJECT_EMOJI, DiscordChannel
from warden.notifier.discord_poller import process_batch, process_reactions

OWNER = "owner-111"
OTHER = "stranger-999"


@pytest.fixture
def dconfig(config):
    config.notify_channel = "discord"
    config.discord_bot_token = "bot-token-abc"
    config.discord_channel_id = "chan-42"
    config.discord_owner_id = OWNER
    return config


def _msg(mid: str, content: str, author: str = OWNER) -> dict:
    return {"id": mid, "content": content, "author": {"id": author}}


def _queue_delete(store) -> int:
    return store.queue_action(
        1, "delete_paths",
        {"paths": ["/mnt/Modi/Kodi/downloads/complete/old"], "reason": "orphaned"},
        2, "Delete 1 path(s)",
    )


# --- channel send ---

def test_send_posts_to_channel_with_bot_auth(dconfig, monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, json=None, timeout=None, **kw):
        captured.update(method=method, url=url, headers=headers, json=json)
        return httpx.Response(200, json={"id": "m-0"}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)
    DiscordChannel(dconfig).send("hello")
    assert captured["method"] == "POST"
    assert captured["url"] == f"{API}/channels/chan-42/messages"
    assert captured["headers"]["Authorization"] == "Bot bot-token-abc"
    assert captured["json"] == {"content": "hello"}


def test_request_retries_on_429_then_succeeds(dconfig, monkeypatch):
    import warden.notifier.discord as dmod
    monkeypatch.setattr(dmod.time, "sleep", lambda *_: None)  # don't actually wait
    calls = {"n": 0}

    def fake_request(method, url, headers=None, timeout=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0.2"},
                                  json={"message": "rate limited"},
                                  request=httpx.Request(method, url))
        return httpx.Response(200, json=[], request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)
    msgs = dmod.fetch_messages(dconfig)        # should retry past the 429
    assert msgs == [] and calls["n"] == 2


def test_send_requires_credentials(config):
    config.discord_bot_token = ""
    with pytest.raises(ValueError):
        DiscordChannel(config)


# --- poller dispatch ---

def test_owner_yes_executes(dconfig, store, channel):
    backend = ReplayBackend({})
    aid = _queue_delete(store)
    cursor = process_batch([_msg("1001", f"YES {aid}")], dconfig, backend, store, channel)
    assert store.get_action(aid)["status"] == "executed"
    assert backend.actions_taken == [
        {"action": "delete_paths", "paths": ["/mnt/Modi/Kodi/downloads/complete/old"]}
    ]
    assert cursor == "1001"
    assert any("executed" in s for s in channel.sent)


def test_non_owner_reply_ignored_but_cursor_advances(dconfig, store, channel):
    backend = ReplayBackend({})
    aid = _queue_delete(store)
    cursor = process_batch([_msg("2002", f"YES {aid}", author=OTHER)],
                           dconfig, backend, store, channel)
    assert store.get_action(aid)["status"] == "pending"
    assert backend.actions_taken == []
    assert cursor == "2002"  # still advance past it so it's not re-read


def test_chatter_is_ignored_no_reply_spam(dconfig, store, channel):
    backend = ReplayBackend({})
    aid = _queue_delete(store)
    cursor = process_batch([_msg("3003", "lol nice"), _msg("3004", "what's warden?")],
                           dconfig, backend, store, channel)
    assert store.get_action(aid)["status"] == "pending"
    assert channel.sent == []          # we did not reply to non-approval messages
    assert cursor == "3004"


def test_batch_processed_in_id_order_and_returns_highest(dconfig, store, channel):
    backend = ReplayBackend({})
    aid = _queue_delete(store)
    # deliberately out of order; YES should still execute and cursor be the max id
    cursor = process_batch(
        [_msg("5005", "hi"), _msg("4004", f"YES {aid}")],
        dconfig, backend, store, channel,
    )
    assert store.get_action(aid)["status"] == "executed"
    assert cursor == "5005"


def test_owner_no_rejects(dconfig, store, channel):
    backend = ReplayBackend({})
    aid = _queue_delete(store)
    process_batch([_msg("6006", f"NO {aid}")], dconfig, backend, store, channel)
    assert store.get_action(aid)["status"] == "denied"
    assert backend.actions_taken == []


# --- on-demand status command ---

def test_status_command_replies_with_realtime_health(dconfig, store, channel):
    backend = ReplayBackend({
        "docker_ps": [{"name": "plex", "state": "running", "status": "Up"}],
        "disk_usage": [{"path": "/mnt/Modi", "used_pct": 93.8, "free_gb": 200,
                        "total_gb": 4000, "used_gb": 3800}],
        "torrents": [],
    })
    process_batch([_msg("9001", "status")], dconfig, backend, store, channel)
    reply = channel.sent[0]
    assert "warden status" in reply
    # real-time: the live disk pressure shows up as an *active issue right now*,
    # not as a 24h rollup
    assert "Active issues" in reply and "93.8%" in reply
    assert "Agent cost" not in reply  # that's daily-summary material, not status


def test_status_command_ignored_from_non_owner(dconfig, store, channel):
    process_batch([_msg("9002", "status", author=OTHER)], dconfig, ReplayBackend({}), store, channel)
    assert channel.sent == []


def test_diagnose_command_dispatches_to_agent(dconfig, store, channel, monkeypatch):
    import warden.notifier.discord_commands as cmds
    called = {}

    async def fake_diag(question, config, backend, store, channel):
        called["q"] = question

    monkeypatch.setattr(cmds, "run_diagnose", fake_diag)
    process_batch([_msg("9300", "diagnose why is plex slow")],
                  dconfig, ReplayBackend({}), store, channel)
    assert called.get("q") == "why is plex slow"
    assert any("investigating" in s for s in channel.sent)


def test_diagnose_without_question_shows_usage(dconfig, store, channel, monkeypatch):
    import warden.notifier.discord_commands as cmds
    monkeypatch.setattr(cmds, "run_diagnose", lambda *a, **k: None)
    process_batch([_msg("9301", "diagnose")], dconfig, ReplayBackend({}), store, channel)
    assert any("Usage" in s for s in channel.sent)


def test_status_shows_typing_indicator(dconfig, store, channel, monkeypatch):
    typed = []
    monkeypatch.setattr(dc, "trigger_typing", lambda config: typed.append(True))
    backend = ReplayBackend({"docker_ps": [], "disk_usage": [], "torrents": []})
    process_batch([_msg("9101", "status")], dconfig, backend, store, channel)
    assert typed  # 'warden is typing…' was triggered before the reply


# --- send_approval seeds reactions ---

def test_send_approval_posts_and_seeds_both_reactions(dconfig, monkeypatch):
    calls = {"reactions": []}

    def fake_request(method, url, headers=None, json=None, timeout=None, **kw):
        if method == "POST":
            return httpx.Response(200, json={"id": "msg-77"}, request=httpx.Request(method, url))
        if method == "PUT":
            calls["reactions"].append(url)
            return httpx.Response(204, request=httpx.Request(method, url))
        return httpx.Response(200, json={}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)
    ref = DiscordChannel(dconfig).send_approval(7, "approve?")
    assert ref == "msg-77"
    # both ✅ and ❌ seeded on the posted message
    assert len(calls["reactions"]) == 2
    assert all("/messages/msg-77/reactions/" in u for u in calls["reactions"])


# --- reaction-based approval (one tap) ---

def _stub_reactions(monkeypatch, approve_users=(), reject_users=()):
    def fake(config, message_id, emoji):
        return set(approve_users) if emoji == APPROVE_EMOJI else set(reject_users)
    monkeypatch.setattr(dc, "reaction_user_ids", fake)
    edits = []
    monkeypatch.setattr(dc, "edit_message",
                        lambda config, mid, text: edits.append((mid, text)))
    return edits


def test_owner_check_reaction_executes(dconfig, store, channel, monkeypatch):
    backend = ReplayBackend({})
    aid = _queue_delete(store)
    store.set_action_notify_ref(aid, "m-1")
    edits = _stub_reactions(monkeypatch, approve_users=[OWNER])

    process_reactions(dconfig, backend, store, channel)

    assert store.get_action(aid)["status"] == "executed"
    assert backend.actions_taken[0]["action"] == "delete_paths"
    assert edits and edits[0][0] == "m-1" and "Approved" in edits[0][1]


def test_owner_cross_reaction_rejects(dconfig, store, channel, monkeypatch):
    backend = ReplayBackend({})
    aid = _queue_delete(store)
    store.set_action_notify_ref(aid, "m-2")
    edits = _stub_reactions(monkeypatch, reject_users=[OWNER])

    process_reactions(dconfig, backend, store, channel)

    assert store.get_action(aid)["status"] == "denied"
    assert backend.actions_taken == []
    assert edits and "Rejected" in edits[0][1]


def test_non_owner_reaction_ignored(dconfig, store, channel, monkeypatch):
    backend = ReplayBackend({})
    aid = _queue_delete(store)
    store.set_action_notify_ref(aid, "m-3")
    _stub_reactions(monkeypatch, approve_users=[OTHER])  # a stranger tapped ✅

    process_reactions(dconfig, backend, store, channel)

    assert store.get_action(aid)["status"] == "pending"
    assert backend.actions_taken == []


def test_no_reaction_leaves_pending(dconfig, store, channel, monkeypatch):
    backend = ReplayBackend({})
    aid = _queue_delete(store)
    store.set_action_notify_ref(aid, "m-4")
    _stub_reactions(monkeypatch)  # nobody reacted yet (besides the bot's seed)

    process_reactions(dconfig, backend, store, channel)

    assert store.get_action(aid)["status"] == "pending"
