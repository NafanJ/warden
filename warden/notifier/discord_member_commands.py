"""Member-only helpers; never invoke Warden's agent, host tools or operational status."""
from __future__ import annotations

import threading
import time
import httpx

from warden.config import Config

MEMBER_COMMANDS = frozenset({"request", "status", "help"})
_lock = threading.Lock()
_cache: dict[str, tuple[float, str]] = {}


def member_allowed(interaction: dict, config: Config) -> bool:
    if not config.discord_guild_id or interaction.get("guild_id") != config.discord_guild_id:
        return False
    member = interaction.get("member") or {}
    author = str((member.get("user") or {}).get("id", ""))
    return bool((config.discord_owner_id and author == config.discord_owner_id) or (
        config.discord_member_role_id and config.discord_member_role_id in member.get("roles", [])))


def _reachable(url: str) -> str:
    try:
        response = httpx.get(url, timeout=3, follow_redirects=False)
        return "Responding" if response.status_code == 200 else "Currently unavailable"
    except httpx.HTTPError:
        return "Could not confirm availability"


def member_reply(name: str, config: Config) -> str:
    if name == "request":
        return (f"**Request a film or show:** {config.discord_requests_url}\n"
                "Sign in with Plex, find the title, and submit your request. "
                "Updates appear in #media-requests; availability can vary.")
    if name == "help":
        return ("**Watch:** use the Plex app or https://app.plex.tv with your shared account.\n"
                f"**Request:** `/request` opens {config.discord_requests_url}.\n"
                "**Service check:** `/status` checks Plex and requests.\n"
                "**Problems:** post in #help with the title/episode, device and error.\n"
                "Missing access? Ask Nathan to check your Plex invite. "
                "Command replies are visible only to you.")
    if name != "status":
        raise ValueError("Unknown member command")
    # Bound repeated probes and return only a coarse service health summary.
    with _lock:
        key = config.plex_url
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < 20:
            return cached[1]
        plex = _reachable(config.plex_url.rstrip('/') + '/identity')
        requests = _reachable('http://127.0.0.1:5055/api/v1/status')
        reply = (f"**Plex:** {plex}\n**Requests:** {requests}\n\n"
                 "Service checks refresh every 20 seconds. They don't verify remote playback "
                 "or whether a particular title is available. If you have trouble, use #help.")
        _cache[key] = (time.monotonic(), reply)
        return reply
