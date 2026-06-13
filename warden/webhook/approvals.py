"""Parse owner replies and execute approved Tier 2 actions directly against
the backend (no second agent run needed for a pre-described action)."""
from __future__ import annotations

import json
import re
from typing import Any

from warden.backends import Backend
from warden.config import Config
from warden.notifier import Channel
from warden.store import Store

REPLY = re.compile(r"^\s*(yes|no|approve|reject)\s+#?(\d+)\s*$", re.IGNORECASE)

# Tier 2 actions the webhook knows how to execute without re-invoking the agent.
def _execute(backend: Backend, tool: str, args: dict[str, Any]) -> str:
    if tool == "delete_paths":
        return backend.delete_paths(list(args["paths"]))
    if tool == "remove_torrents":
        return backend.remove_torrents(list(args["ids"]), bool(args.get("delete_data", False)))
    raise ValueError(f"no executor for tool: {tool}")


def handle_reply(text: str, config: Config, backend: Backend,
                 store: Store, channel: Channel) -> str:
    match = REPLY.match(text or "")
    if not match:
        return ("warden: reply 'YES <action-id>' to approve or 'NO <action-id>' to "
                "reject a pending action.")

    approved = match.group(1).lower() in ("yes", "approve")
    action_id = int(match.group(2))
    action = store.decide_action(action_id, approved)
    if not action:
        return f"warden: action #{action_id} not found or no longer pending (expired/decided)."

    if not approved:
        return f"warden: action #{action_id} rejected. Nothing was changed."

    try:
        result = _execute(backend, action["tool"], json.loads(action["args_json"]))
        store.mark_executed(action_id, result)
        return f"warden: action #{action_id} executed ✅\n{result[:500]}"
    except Exception as exc:
        store.mark_executed(action_id, str(exc), failed=True)
        return f"warden: action #{action_id} FAILED ❌ — {str(exc)[:300]}"
