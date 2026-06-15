"""Live Plex playback presence, persisted to a small JSON file in state/.

Written by the Plex webhook and the sentinel reconcile so two consumers can read
it without each hitting Tautulli:
  * the throttle logic — is anyone streaming right now?
  * collect_snapshot — so incident post-mortems know who was watching when a
    stall/spike happened.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from warden.config import Config

FILENAME = "plex_presence.json"


def _path(config: Config) -> Path:
    return config.state_dir / FILENAME


def write_presence(config: Config, activity: dict[str, Any], throttled: bool,
                   event: str) -> dict[str, Any]:
    record = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_event": event,
        "stream_count": activity.get("stream_count", 0),
        "transcode_count": activity.get("transcode_count", 0),
        "bandwidth_mbps": activity.get("bandwidth_mbps", 0),
        "sessions": activity.get("sessions", []),
        "downloads_throttled": throttled,
    }
    p = _path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(record, indent=2, default=str))
    return record


def read_presence(config: Config) -> dict[str, Any] | None:
    p = _path(config)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
