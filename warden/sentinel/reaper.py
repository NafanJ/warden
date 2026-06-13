"""Deterministic torrent reaper — remove completed downloads from Transmission.

The owner doesn't seed: a torrent that's finished has no reason to linger in the
client. This runs every sentinel cycle (no LLM, no incident) — any torrent at
100% that no *arr app is still importing is removed from Transmission, keeping
the local data (`delete_data=False`), so the imported library copy and the
completed files on disk are untouched.

Every removal is routed through the same permission gate as the agent
(`decide_tool`): it's a Tier-1 reversible action, so it's autonomous in active
mode and always audited — which means it also shows up in the daily summary's
auto-fix count.
"""
from __future__ import annotations

from typing import Any

from warden.agent.tiers import decide_tool
from warden.config import Config
from warden.notifier import Channel
from warden.store import Store


def _tracked_hashes(backend: Any) -> set[str] | None:
    """Uppercased torrent hashes the *arr apps are still tracking (downloading or
    mid-import). Returns None if any arr is unreachable — we then can't be sure a
    torrent isn't being imported, so the caller skips reaping this cycle rather
    than risk yanking an in-flight import out from under Sonarr/Radarr."""
    tracked: set[str] = set()
    for app in ("sonarr", "radarr"):
        try:
            for r in backend.arr_queue(app):
                dl = r.get("download_id")
                if dl:
                    tracked.add(dl.upper())
        except Exception:
            return None
    return tracked


def reap_completed_torrents(config: Config, backend: Any, store: Store,
                            channel: Channel) -> list[str]:
    """Remove finished torrents from Transmission (data kept). Returns the names
    removed. A no-op unless enabled and running in active mode."""
    if not config.reap_completed or config.mode != "active":
        return []
    try:
        torrents = backend.torrents()
    except Exception:
        return []
    if not torrents:
        return []

    tracked = _tracked_hashes(backend)
    if tracked is None:
        return []  # an arr is unreachable — don't risk yanking an in-flight import

    removed: list[str] = []
    for t in torrents:
        if (t.get("percentDone") or 0) < 1.0:
            continue
        if (t.get("hashString") or "").upper() in tracked:
            continue  # still being imported by Sonarr/Radarr — leave it alone
        inp = {"ids": [t["id"]], "delete_data": False}
        allowed, _ = decide_tool(config, store, channel, None,
                                 "remove_torrents", inp, backend)
        if not allowed:
            continue
        try:
            backend.remove_torrents([t["id"]], delete_data=False)
            removed.append(t.get("name") or str(t["id"]))
        except Exception:
            pass  # transient RPC error — try again next cycle
    return removed
