"""Approval reply parsing and execution via the webhook path."""
from warden.backends.replay import ReplayBackend
from warden.webhook.approvals import handle_reply


def queue_delete(store):
    return store.queue_action(
        1, "delete_paths",
        {"paths": ["/mnt/Modi/Kodi/downloads/complete/old"], "reason": "orphaned"},
        2, "Delete 1 path(s)",
    )


def test_yes_executes_action(config, store, channel):
    backend = ReplayBackend({})
    action_id = queue_delete(store)
    reply = handle_reply(f"YES {action_id}", config, backend, store, channel)
    assert "executed" in reply
    assert backend.actions_taken == [{
        "action": "delete_paths",
        "paths": ["/mnt/Modi/Kodi/downloads/complete/old"],
    }]
    assert store.get_action(action_id)["status"] == "executed"


def test_no_rejects_without_executing(config, store, channel):
    backend = ReplayBackend({})
    action_id = queue_delete(store)
    reply = handle_reply(f"no {action_id}", config, backend, store, channel)
    assert "rejected" in reply
    assert backend.actions_taken == []
    assert store.get_action(action_id)["status"] == "denied"


def test_unknown_action_id(config, store, channel):
    reply = handle_reply("YES 999", config, ReplayBackend({}), store, channel)
    assert "not found" in reply


def test_garbage_reply_gets_help_text(config, store, channel):
    reply = handle_reply("what's up", config, ReplayBackend({}), store, channel)
    assert "YES" in reply


def test_double_approval_is_idempotent(config, store, channel):
    backend = ReplayBackend({})
    action_id = queue_delete(store)
    handle_reply(f"YES {action_id}", config, backend, store, channel)
    reply = handle_reply(f"YES {action_id}", config, backend, store, channel)
    assert "no longer pending" in reply
    assert len(backend.actions_taken) == 1
