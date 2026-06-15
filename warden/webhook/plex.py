"""Plex webhook handling: react to playback events by throttling downloads.

Plex Pass servers POST a multipart/form-data body with a JSON `payload` field on
events such as media.play / media.resume / media.pause / media.stop. Rather than
trust per-event bookkeeping (Plex can drop events), every playback event is just
a trigger to RECONCILE against ground truth from Tautulli: if anyone is
streaming, slow Transmission down (alt-speed / "turtle" mode); when the last
stream ends, restore full speed. The identical reconcile also runs from the
sentinel timer, so a missed "stop" can never leave downloads throttled forever —
self-healing, the same way the rest of warden works.

The handler is deliberately framework-free (no FastAPI imports) so the sentinel
can call reconcile() without pulling in the web stack.
"""
from __future__ import annotations

from typing import Any

from warden.backends import Backend
from warden.config import Config
from warden.presence import write_presence

# Plex events that change who-is-watching. Everything else (library.new, etc.)
# is ignored.
PLAYBACK_EVENTS = {
    "media.play", "media.resume", "media.pause", "media.stop", "media.scrobble",
}


def reconcile(config: Config, backend: Backend, event: str = "reconcile") -> dict[str, Any]:
    """Bring Transmission's throttle state in line with live Plex activity and
    refresh the presence record. Returns the presence record plus an 'action'
    note ('throttled' / 'unthrottled' / 'none')."""
    activity = backend.tautulli_activity()
    streaming = (activity.get("stream_count") or 0) > 0
    desired = bool(streaming and config.plex_throttle_downloads)

    action = "none"
    if config.plex_throttle_downloads:
        try:
            current = backend.download_throttled()
        except Exception:
            current = None  # unknown — fall through and assert desired state
        if current is None or current != desired:
            backend.set_download_throttle(desired)
            action = "throttled" if desired else "unthrottled"

    record = write_presence(config, activity, throttled=desired, event=event)
    record["action"] = action
    return record


def handle_plex_event(payload: dict[str, Any], config: Config, backend: Backend) -> dict[str, Any]:
    event = str(payload.get("event") or "")
    if event not in PLAYBACK_EVENTS:
        return {"status": "ignored", "event": event}
    record = reconcile(config, backend, event=event)
    return {
        "status": "ok",
        "event": event,
        "action": record["action"],
        "stream_count": record["stream_count"],
        "downloads_throttled": record["downloads_throttled"],
    }
