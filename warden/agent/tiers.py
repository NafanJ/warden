"""Permission tiers, enforced in code via the SDK's can_use_tool callback.

Tier 0 — reads: always allowed.
Tier 1 — reversible actions (restart, blocklist+re-search): autonomous in
         active mode, denied in dry-run. Always audited.
Tier 2 — destructive actions (delete data): never autonomous. Queued in the
         store, owner notified over WhatsApp, executed only after approval.
Anything not in the registry is denied (default-deny), including all of the
SDK's built-in tools — the agent can only touch the system through warden's
own backend-wrapped tools.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from warden.config import Config, path_within
from warden.notifier import Channel
from warden.store import Store

TIER0 = {
    "get_containers", "container_logs", "container_inspect", "disk_usage",
    "du_summary", "memory", "list_torrents", "arr_queue", "check_urls",
    "list_dir", "write_report",
}
TIER1 = {"container_restart", "arr_blocklist_research", "remove_torrents"}
TIER2 = {"delete_paths"}


def bare_name(tool_name: str) -> str:
    """mcp__warden__container_logs -> container_logs"""
    return tool_name.split("__")[-1]


def tier_of(tool_name: str, input_data: dict[str, Any]) -> int | None:
    name = bare_name(tool_name)
    if name in TIER0:
        return 0
    if name in TIER1:
        # removing a torrent *with its data* is destructive, not reversible
        if name == "remove_torrents" and input_data.get("delete_data"):
            return 2
        return 1
    if name in TIER2:
        return 2
    return None


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _path_size(path: str) -> int | None:
    """On-disk size of a file or directory tree, or None if it can't be read."""
    p = Path(path)
    try:
        if p.is_dir():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return p.stat().st_size
    except OSError:
        return None


def describe_action(tool_name: str, input_data: dict[str, Any]) -> str:
    name = bare_name(tool_name)
    if name == "delete_paths":
        paths = input_data.get("paths", [])
        reason = input_data.get("reason", "")
        lines, total = [], 0
        for path in paths[:5]:
            size = _path_size(path)
            if size is None:
                lines.append(f"• {path} (not found on disk)")
            else:
                total += size
                lines.append(f"• {path} ({_human_size(size)})")
        if len(paths) > 5:
            lines.append(f"• …and {len(paths) - 5} more")
        return (f"Delete {len(paths)} path(s) — {_human_size(total)} total:\n"
                + "\n".join(lines) + f"\nReason: {reason}")
    return f"{name} {json.dumps(input_data)[:300]}"


def decide_tool(config: Config, store: Store, channel: Channel,
                incident_id: int | None, tool_name: str,
                input_data: dict[str, Any]) -> tuple[bool, str]:
    """The safety gate, provider-agnostic. Returns (allowed, message): when
    denied, `message` is the explanation fed back to the model. Used directly by
    the OpenAI loop and wrapped by make_permission_handler for the Claude SDK."""
    tier = tier_of(tool_name, input_data)

    if tier is None:
        store.audit(tool_name, input_data, -1, "denied", incident_id)
        return False, "Tool not in warden's registry. Use only the warden tools provided."

    if tier == 0:
        store.audit(tool_name, input_data, 0, "allowed", incident_id)
        return True, ""

    # Delete-scope policy: warden may only delete within its configured roots.
    # Catch out-of-bounds deletes here, before queuing, so the owner is never
    # pinged about a deletion that could never execute — and tell the agent to
    # recommend it in the report instead.
    if bare_name(tool_name) == "delete_paths":
        outside = [p for p in input_data.get("paths", [])
                   if not path_within(p, config.delete_roots)]
        if outside:
            store.audit(tool_name, input_data, tier, "denied", incident_id)
            return False, (
                f"warden may only delete within {', '.join(config.delete_roots)}. "
                f"These paths are outside that and will NOT be deleted: {', '.join(outside)}. "
                "Do not call delete_paths on them — instead list them as manual "
                "recommendations under '## Proposed actions' in your report.")

    if config.mode == "dry-run":
        store.audit(tool_name, input_data, tier, "denied", incident_id)
        return False, ("Dry-run mode: action not executed. Record what you *would* do "
                       "in your report under 'Proposed actions'.")

    if tier == 1:
        store.audit(tool_name, input_data, 1, "allowed", incident_id)
        return True, ""

    # Tier 2: only allowed if this exact action was already approved
    approved = store.find_approved_action(bare_name(tool_name), input_data)
    if approved:
        store.audit(tool_name, input_data, 2, "allowed", incident_id)
        store.mark_executed(approved["id"], "executed by agent after approval")
        return True, ""

    pending = store.find_pending_action(bare_name(tool_name), input_data)
    if pending:
        store.audit(tool_name, input_data, 2, "denied", incident_id)
        return False, (f"Identical action already pending approval (#{pending['id']}). "
                       "Do not retry; note it in your report.")

    description = describe_action(tool_name, input_data)
    action_id = store.queue_action(incident_id, bare_name(tool_name), input_data, 2, description)
    store.audit(tool_name, input_data, 2, "queued", incident_id)
    ref = channel.send_approval(
        action_id,
        f"🛡️ warden needs approval (action #{action_id}, incident #{incident_id}):\n"
        f"{description}\n\nTap ✅ to approve or ❌ to reject "
        f"(or reply YES {action_id} / NO {action_id})."
    )
    if ref:
        store.set_action_notify_ref(action_id, ref)
    return False, (f"Destructive action queued for owner approval as action #{action_id}. "
                   "Do not retry it. Finish your investigation and note the pending "
                   "action in your report.")


def make_permission_handler(config: Config, store: Store, channel: Channel,
                            incident_id: int | None):
    """Wrap decide_tool as the Claude Agent SDK's can_use_tool callback."""

    async def handler(tool_name: str, input_data: dict[str, Any], context: Any):
        allowed, message = decide_tool(config, store, channel, incident_id, tool_name, input_data)
        if allowed:
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message=message)

    return handler
