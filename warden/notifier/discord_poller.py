"""Poll a Discord channel for owner approval replies and dispatch them.

Outbound long-poll only — no public endpoint, no tunnel. Run as a service:
    python -m warden.notifier.discord_poller

Reuses warden.webhook.approvals.handle_reply, so the parse -> approve -> execute
logic and the Tier 2 safety model are identical to the WhatsApp path.
"""
from __future__ import annotations

import time

from warden.backends import Backend
from warden.backends.live import LiveBackend
from warden.config import Config, load_config
from warden.notifier import Channel, get_channel
from warden.notifier import discord as dc
from warden.notifier.discord import fetch_messages
from warden.store import Store
from warden.summary import format_summary, gather
from warden.webhook.approvals import REPLY, handle_reply

POLL_SECONDS = 5


def process_batch(messages: list[dict], config: Config, backend: Backend,
                  store: Store, channel: Channel) -> str | None:
    """Dispatch owner approval replies in a batch of Discord messages.

    Returns the highest message id seen so the caller can advance its poll
    cursor — even past messages we ignore, so they're never re-examined.
    """
    highest: str | None = None
    for msg in sorted(messages, key=lambda m: int(m["id"])):
        highest = str(msg["id"])
        author_id = str(msg.get("author", {}).get("id", ""))
        if config.discord_owner_id and author_id != config.discord_owner_id:
            continue  # only the owner may approve / query
        content = (msg.get("content") or "").strip()
        if content.lower() in ("status", "!status"):
            # on-demand live digest (same data as the daily summary, no LLM)
            try:
                channel.send(format_summary(gather(config, backend, store), label="status"))
            except Exception as exc:
                channel.send(f"warden: couldn't build status — {exc}")
            continue
        if not REPLY.match(content):
            continue  # ignore ordinary chatter — only react to YES/NO <id>
        channel.send(handle_reply(content, config, backend, store, channel))
    return highest


def process_reactions(config: Config, backend: Backend, store: Store,
                      channel: Channel) -> None:
    """One-tap approvals: check the ✅/❌ reactions the owner left on each pending
    action's alert message and dispatch them through the same handle_reply path.

    Requires DISCORD_OWNER_ID — without a known owner we can't attribute a
    reaction to an authorized approver, so reactions are ignored (typed YES/NO
    with the owner-id check still works).
    """
    if not config.discord_owner_id:
        return
    for action in store.pending_actions():
        ref = action.get("notify_ref")
        if not ref:
            continue
        approve = config.discord_owner_id in dc.reaction_user_ids(config, ref, dc.APPROVE_EMOJI)
        reject = config.discord_owner_id in dc.reaction_user_ids(config, ref, dc.REJECT_EMOJI)
        if not (approve or reject):
            continue
        verb = "YES" if approve else "NO"  # an explicit approve wins a double-tap
        reply = handle_reply(f"{verb} {action['id']}", config, backend, store, channel)
        outcome = "✅ Approved" if approve else "❌ Rejected"
        try:  # edit the original alert in place so the outcome shows where you tapped
            dc.edit_message(config, ref,
                            f"🛡️ warden action #{action['id']} — {outcome}\n"
                            f"{action.get('description') or ''}\n\n{reply}")
        except Exception as exc:
            print(f"could not edit approval message {ref}: {exc}")


def _starting_cursor(config: Config) -> str | None:
    """Newest existing message id, so we don't act on stale history at boot."""
    messages = fetch_messages(config, limit=1)
    return str(messages[0]["id"]) if messages else None


def main() -> int:
    config = load_config()
    if config.notify_channel != "discord":
        print("NOTIFY_CHANNEL is not 'discord'; nothing to poll.")
        return 1
    backend: Backend = LiveBackend(config)
    store = Store(config.state_dir / "warden.db")
    channel = get_channel(config)

    cursor = _starting_cursor(config)
    print(f"warden discord poller started (channel {config.discord_channel_id}, cursor {cursor}).")
    while True:
        try:
            messages = fetch_messages(config, after=cursor)
            new_cursor = process_batch(messages, config, backend, store, channel)
            if new_cursor:
                cursor = new_cursor
            process_reactions(config, backend, store, channel)  # one-tap ✅/❌
        except Exception as exc:  # keep the loop alive across transient API errors
            print(f"poll error: {exc}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
