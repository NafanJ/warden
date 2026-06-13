"""Plex per-user watch stats for the Discord `user-stats` command.

`user-stats` (or `user-stats all`) → everyone's totals; `user-stats <name>` →
one person's 24h / 7d / 30d / all-time breakdown. Data comes from Tautulli.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _hours(seconds: int | None) -> str:
    h = (seconds or 0) / 3600
    return f"{h:.1f}h" if h < 100 else f"{h:.0f}h"


def _ago(epoch: int | None) -> str:
    if not epoch:
        return "never"
    days = (datetime.now(timezone.utc).timestamp() - epoch) / 86400
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    return f"{days:.0f}d ago"


def format_users_table(users: list[dict[str, Any]]) -> str:
    if not users:
        return "📺 No Plex users found."
    rows = [f"{(u['name'] or '?'):<16}{u['plays']:>4} plays  {_hours(u['duration_seconds']):>7}"
            f"  last {_ago(u['last_seen'])}" for u in users[:25]]
    return ("📺 **Plex — all users** (by plays)\n```\n" + "\n".join(rows) + "\n```\n"
            "Reply `user-stats <name>` for one person.")


_DAY_LABELS = {1: "Last 24h", 7: "Last 7d", 30: "Last 30d", 0: "All time"}


def format_user_detail(user: dict[str, Any], watch_stats: list[dict[str, Any]]) -> str:
    by_days = {s.get("days"): s for s in watch_stats}
    rows = []
    for days in (1, 7, 30, 0):
        s = by_days.get(days)
        if s:
            rows.append(f"{_DAY_LABELS[days]:<9}{s['plays']:>3} plays  {_hours(s['seconds'])}")
    body = "\n".join(rows) or "no recorded plays"
    return (f"📺 **Plex — {user['name']}**\n```\n{body}\n```\n"
            f"Last seen: {_ago(user.get('last_seen'))}")


def handle_userstats(arg: str, backend: Any) -> str:
    """Dispatch the command: everyone (bare / 'all') vs a single named user."""
    users = backend.tautulli_users()
    arg = (arg or "").strip().lower()
    if arg and arg != "all":
        match = next((u for u in users if arg in (u["name"] or "").lower()), None)
        if not match:
            known = ", ".join(u["name"] for u in users[:20])
            return f"📺 No Plex user matching '{arg}'. Known: {known or '(none)'}"
        return format_user_detail(match, backend.tautulli_user_stats(match["user_id"]))
    return format_users_table(users)
