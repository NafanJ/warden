"""Collect a full signal snapshot from the backend. Pure reads, no judgement."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from warden.backends import Backend
from warden.config import Config
from warden.presence import read_presence


def collect_snapshot(backend: Backend, config: Config) -> dict[str, Any]:
    snapshot: dict[str, Any] = {"collected_at": datetime.now(timezone.utc).isoformat()}

    def safe(key: str, fn):
        try:
            snapshot[key] = fn()
        except Exception as exc:
            snapshot[key] = None
            snapshot.setdefault("collector_errors", {})[key] = str(exc)[:300]

    safe("docker_ps", backend.docker_ps)
    safe("disk_usage", lambda: backend.disk_usage(config.disk_paths))
    safe("mount_health", lambda: backend.mount_health(config.disk_paths))
    safe("memory", backend.memory)
    safe("torrents", backend.torrents)
    safe("arr_queue", lambda: {"sonarr": backend.arr_queue("sonarr"),
                               "radarr": backend.arr_queue("radarr")})
    if config.public_urls:
        safe("url_checks", lambda: backend.check_urls(config.public_urls))
    safe("plex_presence", lambda: read_presence(config))
    return snapshot
