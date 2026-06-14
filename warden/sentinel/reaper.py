"""Deterministic torrent reaper — clear *unmanaged* finished downloads.

Sonarr/Radarr already own the lifecycle of the torrents they grab: with
completed-download-handling + hardlink imports, a finished managed download is
imported into the library (sharing disk blocks, so it costs ~nothing extra) and
the *arr removes it when its seeding goal is met. warden must NOT step on that.

What nothing cleans up is a *manual* grab the *arr never tracked — it seeds
forever as a standalone copy. So this reaper, run each sentinel cycle (no LLM,
no incident), removes only completed torrents that are (a) not in any *arr queue
and (b) not sitting in an *arr download category. Data is kept on disk
(`delete_data=False`); only the seed entry goes.

Every removal is routed through the same permission gate as the agent
(`decide_tool`): a Tier-1 reversible action, autonomous in active mode and
always audited (so it shows in the daily summary's auto-fix count).
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from warden.agent.tiers import decide_tool
from warden.config import Config
from warden.notifier import Channel
from warden.store import Store


def _arr_managed(backend: Any) -> tuple[set[str], set[str]] | None:
    """(tracked-hashes, category-names) the *arr apps own — torrents either
    mid-import or filed under a Sonarr/Radarr download category. Returns None if
    any *arr is unreachable: we then can't tell what's managed, so the caller
    skips reaping this cycle rather than risk yanking an *arr's download."""
    tracked: set[str] = set()
    try:
        for app in ("sonarr", "radarr"):
            for r in backend.arr_queue(app):
                dl = r.get("download_id")
                if dl:
                    tracked.add(dl.upper())
        categories = backend.arr_categories()
    except Exception:
        return None
    return tracked, categories


def reap_completed_torrents(config: Config, backend: Any, store: Store,
                            channel: Channel) -> list[str]:
    """Remove finished, unmanaged torrents from Transmission (data kept). Returns
    the names removed. A no-op unless enabled and running in active mode."""
    if not config.reap_completed or config.mode != "active":
        return []
    try:
        torrents = backend.torrents()
    except Exception:
        return []
    if not torrents:
        return []

    managed = _arr_managed(backend)
    if managed is None:
        return []  # an *arr is unreachable — don't risk reaping a managed download
    tracked, categories = managed

    removed: list[str] = []
    for t in torrents:
        if (t.get("percentDone") or 0) < 1.0:
            continue
        if (t.get("hashString") or "").upper() in tracked:
            continue  # still being imported by Sonarr/Radarr
        if PurePosixPath(t.get("downloadDir") or "").name in categories:
            continue  # filed under an *arr category — the *arr owns its cleanup
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
