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
from warden.notifier.discord import fetch_messages
from warden.store import Store
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
            continue  # only the owner may approve
        content = msg.get("content", "")
        if not REPLY.match(content or ""):
            continue  # ignore ordinary chatter — only react to YES/NO <id>
        channel.send(handle_reply(content, config, backend, store, channel))
    return highest


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
        except Exception as exc:  # keep the loop alive across transient API errors
            print(f"poll error: {exc}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
