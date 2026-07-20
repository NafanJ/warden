"""Plex per-user watch stats for the Discord `user-stats` command.

`user-stats` (or `user-stats all`) → everyone's totals; `user-stats <name>` →
one person's 24h / 7d / 30d / all-time breakdown.

Plex is the primary source: libraries are shared by invite, and Tautulli only
knows users it has watched stream, so invitees are missing or stale there. Plex
knows the full roster and holds server-side history for everyone. Tautulli is
kept as a supplement — it can retain history from before the current Plex
retention window, and it still knows users whose invite was later revoked.
"""
from __future__ import annotations

from collections import defaultdict
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


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


_DAY_LABELS = {1: "Last 24h", 7: "Last 7d", 30: "Last 30d", 0: "All time"}
_WINDOWS = (1, 7, 30, 0)  # days; 0 = all time


def bucket_history(history: list[dict[str, Any]], now: float | None = None) -> list[dict[str, Any]]:
    """Roll one account's play rows up into the 24h / 7d / 30d / all-time windows.

    Plex history is a flat list of plays, so unlike Tautulli there are no
    pre-aggregated buckets to read — we count them here.
    """
    now = _now() if now is None else now
    totals = {d: {"days": d, "plays": 0, "seconds": 0} for d in _WINDOWS}
    for row in history:
        age_days = (now - (row.get("viewed_at") or 0)) / 86400
        for d in _WINDOWS:
            if d == 0 or age_days <= d:
                totals[d]["plays"] += 1
                totals[d]["seconds"] += row.get("seconds") or 0
    return [totals[d] for d in _WINDOWS]


def merge_users(plex_users: list[dict[str, Any]], plex_history: list[dict[str, Any]],
                tautulli_users: list[dict[str, Any]],
                aliases: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Fold both sources into one roster, keyed on name (the only shared field).

    `aliases` maps a lowercased source name onto the display name it should
    count towards, for people the two systems disagree about.
    """
    aliases = aliases or {}

    def canon(name: str | None) -> str:
        name = (name or "?").strip()
        return aliases.get(name.lower(), name)

    by_account: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in plex_history:
        by_account[row.get("account_id")].append(row)

    taut = {canon(u.get("name")).lower(): u for u in tautulli_users if u.get("name")}
    merged: list[dict[str, Any]] = []
    claimed: set[str] = set()

    # One person can hold several Plex accounts under the same name — re-inviting
    # someone mints a fresh accountID while the old one lingers, with history
    # split across both. Collapse them so they read as the one human they are.
    accounts_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for u in plex_users:
        accounts_by_name[canon(u.get("name")).lower()].append(u)

    for accounts in accounts_by_name.values():
        name = canon(accounts[0].get("name"))
        rows = [r for a in accounts for r in by_account.get(a.get("account_id"), [])]
        plays = len(rows)
        seconds = sum(r.get("seconds") or 0 for r in rows)
        last_seen = max((r.get("viewed_at") or 0 for r in rows), default=0)
        t = taut.get(name.lower())
        if t:
            claimed.add(name.lower())
            # Neither source is a superset: Plex may have aged history out, and
            # Tautulli may never have seen this user. Take whichever saw more
            # rather than summing, which would double-count overlapping plays.
            plays = max(plays, t.get("plays") or 0)
            seconds = max(seconds, t.get("duration_seconds") or 0)
            last_seen = max(last_seen, t.get("last_seen") or 0)
        merged.append({
            "account_id": accounts[0].get("account_id"),
            "account_ids": [a.get("account_id") for a in accounts],
            "user_id": (t or {}).get("user_id"),
            "name": name,
            "plays": plays,
            "duration_seconds": seconds,
            "last_seen": last_seen or None,
            "history": rows,
        })

    # Anyone Tautulli remembers that Plex no longer lists — e.g. a revoked invite.
    for key, t in taut.items():
        if key in claimed:
            continue
        merged.append({
            "account_id": None,
            "account_ids": [],
            "user_id": t.get("user_id"),
            "name": canon(t.get("name")),
            "plays": t.get("plays") or 0,
            "duration_seconds": t.get("duration_seconds") or 0,
            "last_seen": t.get("last_seen") or None,
            "history": [],
        })

    merged.sort(key=lambda u: (-u["plays"], (u["name"] or "").lower()))
    return merged


def format_users_table(users: list[dict[str, Any]]) -> str:
    if not users:
        return "📺 No Plex users found."
    rows = [f"{(u['name'] or '?'):<16}{u['plays']:>4} plays  {_hours(u['duration_seconds']):>7}"
            f"  last {_ago(u['last_seen'])}" for u in users[:25]]
    return ("📺 **Plex — all users** (by plays)\n```\n" + "\n".join(rows) + "\n```\n"
            "Sources: Plex (primary) + Tautulli. "
            "Reply `user-stats <name>` for one person.")


def format_user_detail(user: dict[str, Any], watch_stats: list[dict[str, Any]]) -> str:
    by_days = {s.get("days"): s for s in watch_stats}
    rows = []
    for days in _WINDOWS:
        s = by_days.get(days)
        if s:
            rows.append(f"{_DAY_LABELS[days]:<9}{s['plays']:>3} plays  {_hours(s['seconds'])}")
    body = "\n".join(rows) or "no recorded plays"
    return (f"📺 **Plex — {user['name']}**\n```\n{body}\n```\n"
            f"Last seen: {_ago(user.get('last_seen'))}")


def _user_detail_stats(user: dict[str, Any], backend: Any) -> list[dict[str, Any]]:
    """Per-window breakdown from Plex, falling back to Tautulli's own buckets."""
    if user.get("history"):
        return bucket_history(user["history"])
    if user.get("user_id") is not None:
        return backend.tautulli_user_stats(user["user_id"])
    return []


def _roster(backend: Any) -> list[dict[str, Any]]:
    """Plex first; a Plex outage degrades to Tautulli rather than answering nothing."""
    try:
        plex_users, plex_history = backend.plex_users(), backend.plex_history()
    except Exception:
        plex_users, plex_history = [], []
    try:
        tautulli_users = backend.tautulli_users()
    except Exception:
        tautulli_users = []
    aliases = getattr(getattr(backend, "config", None), "plex_user_aliases", None)
    return merge_users(plex_users, plex_history, tautulli_users, aliases)


def handle_userstats(arg: str, backend: Any) -> str:
    """Dispatch the command: everyone (bare / 'all') vs a single named user."""
    users = _roster(backend)
    arg = (arg or "").strip().lower()
    if arg and arg != "all":
        match = next((u for u in users if arg in (u["name"] or "").lower()), None)
        if not match:
            known = ", ".join(u["name"] for u in users[:20])
            return f"📺 No Plex user matching '{arg}'. Known: {known or '(none)'}"
        return format_user_detail(match, _user_detail_stats(match, backend))
    return format_users_table(users)
