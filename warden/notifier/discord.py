"""Discord channel: send alerts and receive YES/NO approvals via the bot REST API.

Two-way with no public endpoint. The bot POSTs alerts to one channel, and the
companion poller (warden.notifier.discord_poller) reads that same channel to pick
up the owner's replies. Because the bot *polls* Discord over an outbound HTTPS
connection, there is no webhook, no tunnel, and no inbound port — unlike the
WhatsApp path. Setup is one bot token + one channel id.
"""
from __future__ import annotations

import httpx

from warden.config import Config

API = "https://discord.com/api/v10"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bot {token}", "Content-Type": "application/json"}


class DiscordChannel:
    def __init__(self, config: Config):
        if not (config.discord_bot_token and config.discord_channel_id):
            raise ValueError("Discord channel requires DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID")
        self.config = config

    def send(self, text: str) -> None:
        resp = httpx.post(
            f"{API}/channels/{self.config.discord_channel_id}/messages",
            headers=_headers(self.config.discord_bot_token),
            json={"content": text[:2000]},  # Discord hard-limits message bodies to 2000 chars
            timeout=30,
        )
        resp.raise_for_status()


def fetch_messages(config: Config, after: str | None = None, limit: int = 50) -> list[dict]:
    """Recent messages in the approval channel. With `after` (a message id /
    snowflake) only messages newer than it are returned — the poll cursor."""
    params: dict[str, object] = {"limit": limit}
    if after:
        params["after"] = after
    resp = httpx.get(
        f"{API}/channels/{config.discord_channel_id}/messages",
        headers=_headers(config.discord_bot_token),
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
