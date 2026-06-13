"""The safety contract: tiers are enforced by code, not by prompt."""
import dataclasses

import pytest

from warden.agent.tiers import make_permission_handler, tier_of


def handler_for(config, store, channel, incident_id=1):
    return make_permission_handler(config, store, channel, incident_id)


async def test_reads_always_allowed(config, store, channel):
    handler = handler_for(config, store, channel)
    result = await handler("mcp__warden__container_logs", {"name": "plex"}, None)
    assert result.behavior == "allow"


async def test_unknown_tools_denied(config, store, channel):
    handler = handler_for(config, store, channel)
    result = await handler("Bash", {"command": "rm -rf /"}, None)
    assert result.behavior == "deny"


async def test_tier1_allowed_in_active_mode(config, store, channel):
    handler = handler_for(config, store, channel)
    result = await handler("mcp__warden__container_restart", {"name": "plex"}, None)
    assert result.behavior == "allow"


async def test_tier1_denied_in_dry_run(config, store, channel):
    dry = dataclasses.replace(config, mode="dry-run")
    handler = handler_for(dry, store, channel)
    result = await handler("mcp__warden__container_restart", {"name": "plex"}, None)
    assert result.behavior == "deny"
    assert "dry-run" in result.message.lower()


async def test_tier2_queued_not_executed(config, store, channel):
    handler = handler_for(config, store, channel)
    args = {"paths": ["/mnt/Modi/Kodi/downloads/complete/old"], "reason": "orphaned"}
    result = await handler("mcp__warden__delete_paths", args, None)
    assert result.behavior == "deny"
    action = store.find_pending_action("delete_paths", args)
    assert action is not None and action["status"] == "pending"
    assert len(channel.sent) == 1 and "approval" in channel.sent[0]


async def test_tier2_duplicate_not_requeued(config, store, channel):
    handler = handler_for(config, store, channel)
    args = {"paths": ["/mnt/Modi/Kodi/downloads/complete/old"], "reason": "orphaned"}
    await handler("mcp__warden__delete_paths", args, None)
    result = await handler("mcp__warden__delete_paths", args, None)
    assert result.behavior == "deny"
    assert len(channel.sent) == 1  # no second notification
    count = store.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    assert count == 1


async def test_tier2_allowed_after_approval(config, store, channel):
    handler = handler_for(config, store, channel)
    args = {"paths": ["/mnt/Modi/Kodi/downloads/complete/old"], "reason": "orphaned"}
    await handler("mcp__warden__delete_paths", args, None)
    pending = store.find_pending_action("delete_paths", args)
    store.decide_action(pending["id"], approved=True)
    result = await handler("mcp__warden__delete_paths", args, None)
    assert result.behavior == "allow"


def test_remove_torrents_with_data_is_tier2():
    assert tier_of("mcp__warden__remove_torrents", {"ids": [1], "delete_data": False}) == 1
    assert tier_of("mcp__warden__remove_torrents", {"ids": [1], "delete_data": True}) == 2


def test_everything_has_a_tier_or_is_denied():
    assert tier_of("mcp__warden__write_report", {}) == 0
    assert tier_of("mcp__warden__nonexistent", {}) is None
    assert tier_of("WebFetch", {}) is None
