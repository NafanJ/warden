"""Discord channel: send alerts and receive YES/NO approvals via the bot REST API.

Two-way with no public endpoint. The bot POSTs alerts to one channel, and the
companion poller (warden.notifier.discord_poller) reads that same channel to pick
up the owner's replies. Because the bot *polls* Discord over an outbound HTTPS
connection, there is no webhook, no tunnel, and no inbound port — unlike the
WhatsApp path. Setup is one bot token + one channel id.
"""
from __future__ import annotations

import time
import urllib.parse

import httpx

from warden.config import Config

API = "https://discord.com/api/v10"

APPROVE_EMOJI = "✅"
REJECT_EMOJI = "❌"

_MAX_RETRIES = 3


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bot {token}", "Content-Type": "application/json"}


def _emoji(e: str) -> str:
    return urllib.parse.quote(e)


def _request(method: str, token: str, url: str, **kwargs) -> httpx.Response:
    """One Discord API call that respects 429 rate limits: on a 429 it waits the
    Retry-After the API specifies and retries, instead of raising and dropping
    the message/poll. Discord's per-route limit is small (~5/s)."""
    for attempt in range(_MAX_RETRIES + 1):
        resp = httpx.request(method, url, headers=_headers(token), timeout=30, **kwargs)
        if resp.status_code == 429 and attempt < _MAX_RETRIES:
            retry_after = float(resp.headers.get("retry-after", "1"))
            time.sleep(min(retry_after, 10) + 0.1)
            continue
        resp.raise_for_status()
        return resp
    return resp  # unreachable, but keeps type checkers happy


class DiscordChannel:
    def __init__(self, config: Config):
        if not (config.discord_bot_token and config.discord_channel_id):
            raise ValueError("Discord channel requires DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID")
        self.config = config

    def _post_message(self, text: str) -> dict:
        resp = _request("POST", self.config.discord_bot_token,
                        f"{API}/channels/{self.config.discord_channel_id}/messages",
                        json={"content": text[:2000]})  # Discord caps message bodies at 2000 chars
        return resp.json()

    def send(self, text: str) -> None:
        self._post_message(text)

    def send_approval(self, action_id: int, text: str) -> str | None:
        """Post an approval prompt and pre-add ✅/❌ so the owner can approve with
        a single tap. Returns the message id; the poller reads its reactions."""
        msg = self._post_message(text)
        message_id = str(msg["id"])
        for emoji in (APPROVE_EMOJI, REJECT_EMOJI):
            add_reaction(self.config, message_id, emoji)
        return message_id


def add_reaction(config: Config, message_id: str, emoji: str) -> None:
    _request("PUT", config.discord_bot_token,
             f"{API}/channels/{config.discord_channel_id}/messages/{message_id}"
             f"/reactions/{_emoji(emoji)}/@me")


def reaction_user_ids(config: Config, message_id: str, emoji: str) -> set[str]:
    """User ids that reacted to a message with the given emoji (the bot's own
    seed reaction is included; callers filter to the owner)."""
    resp = _request("GET", config.discord_bot_token,
                    f"{API}/channels/{config.discord_channel_id}/messages/{message_id}"
                    f"/reactions/{_emoji(emoji)}", params={"limit": 100})
    return {str(u["id"]) for u in resp.json()}


def trigger_typing(config: Config) -> None:
    """Show 'warden is typing…' in the channel — clears when the next message is
    sent (or after ~10s). Best-effort feedback while the bot does live reads."""
    _request("POST", config.discord_bot_token,
             f"{API}/channels/{config.discord_channel_id}/typing")


def edit_message(config: Config, message_id: str, text: str) -> None:
    _request("PATCH", config.discord_bot_token,
             f"{API}/channels/{config.discord_channel_id}/messages/{message_id}",
             json={"content": text[:2000]})


def fetch_messages(config: Config, after: str | None = None, limit: int = 50) -> list[dict]:
    """Recent messages in the approval channel. With `after` (a message id /
    snowflake) only messages newer than it are returned — the poll cursor."""
    params: dict[str, object] = {"limit": limit}
    if after:
        params["after"] = after
    resp = _request("GET", config.discord_bot_token,
                    f"{API}/channels/{config.discord_channel_id}/messages", params=params)
    return resp.json()


# --- slash (application) commands ---------------------------------------------
# A bot's application id equals its user id, so /applications/@me resolves it from
# the token alone — no extra config. Commands registered to a guild appear
# instantly; global commands can take up to an hour to propagate.

DEFERRED_RESPONSE = 5  # ACK an interaction now, edit in the real answer later


_APP_ID: str | None = None


def application_id(config: Config) -> str:
    global _APP_ID
    if _APP_ID is None:
        resp = _request("GET", config.discord_bot_token, f"{API}/applications/@me")
        _APP_ID = str(resp.json()["id"])
    return _APP_ID


def register_commands(config: Config, commands: list[dict]) -> str:
    """Bulk-overwrite warden's slash commands (idempotent). Targets the guild in
    DISCORD_GUILD_ID when set (instant); otherwise registers globally. Returns
    the scope used so the caller can log it."""
    app_id = application_id(config)
    if config.discord_guild_id:
        url = f"{API}/applications/{app_id}/guilds/{config.discord_guild_id}/commands"
        scope = f"guild {config.discord_guild_id}"
    else:
        url = f"{API}/applications/{app_id}/commands"
        scope = "global (~1h to appear)"
    _request("PUT", config.discord_bot_token, url, json=commands)
    return scope


def interaction_defer(interaction_id: str, token: str) -> None:
    """ACK an interaction within Discord's 3s window with a 'thinking…' state, so
    a slow command (diagnose) doesn't show 'application did not respond'. The
    callback endpoint is authenticated by the interaction token in the URL, not
    the bot token."""
    httpx.post(f"{API}/interactions/{interaction_id}/{token}/callback",
               json={"type": DEFERRED_RESPONSE}, timeout=30).raise_for_status()


def interaction_respond(config: Config, token: str, text: str) -> None:
    """Fill in (edit) the deferred response with the command's actual output."""
    app_id = application_id(config)
    httpx.patch(f"{API}/webhooks/{app_id}/{token}/messages/@original",
                json={"content": text[:2000]}, timeout=30).raise_for_status()
