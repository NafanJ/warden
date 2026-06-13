"""ReplayBackend: serves a captured incident snapshot for evals and tests.

Reads come from the fixture's `snapshot` section; mutating calls are recorded
in `actions_taken` so the eval scorer can compare them with the fixture's
expected action.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ReplayBackend:
    def __init__(self, snapshot: dict[str, Any]):
        self.snapshot = snapshot
        self.actions_taken: list[dict[str, Any]] = []

    @classmethod
    def from_fixture(cls, path: str | Path) -> "ReplayBackend":
        data = json.loads(Path(path).read_text())
        return cls(data["snapshot"])

    def _record(self, action: str, **kwargs: Any) -> str:
        self.actions_taken.append({"action": action, **kwargs})
        return f"[replay] {action} ok"

    # --- docker ---
    def docker_ps(self) -> list[dict[str, Any]]:
        return self.snapshot.get("docker_ps", [])

    def container_logs(self, name: str, lines: int = 100) -> str:
        return self.snapshot.get("container_logs", {}).get(name, f"(no logs captured for {name})")

    def container_inspect(self, name: str) -> dict[str, Any]:
        return self.snapshot.get("container_inspect", {}).get(name, {"name": name})

    def container_restart(self, name: str) -> str:
        return self._record("container_restart", name=name)

    # --- system ---
    def disk_usage(self, paths: list[str]) -> list[dict[str, Any]]:
        return self.snapshot.get("disk_usage", [])

    def du_summary(self, path: str, depth: int = 1) -> str:
        return self.snapshot.get("du_summary", {}).get(path, f"(no du captured for {path})")

    def memory(self) -> dict[str, Any]:
        return self.snapshot.get("memory", {})

    # --- transmission ---
    def torrents(self) -> list[dict[str, Any]]:
        return self.snapshot.get("torrents", [])

    def remove_torrents(self, ids: list[int], delete_data: bool = False) -> str:
        return self._record("remove_torrents", ids=ids, delete_data=delete_data)

    # --- sonarr / radarr ---
    def arr_queue(self, app: str) -> list[dict[str, Any]]:
        return self.snapshot.get("arr_queue", {}).get(app, [])

    def arr_blocklist_and_research(self, app: str, queue_ids: list[int]) -> str:
        return self._record("arr_blocklist_and_research", app=app, queue_ids=queue_ids)

    # --- network ---
    def check_urls(self, urls: list[str]) -> list[dict[str, Any]]:
        return self.snapshot.get("url_checks", [{"url": u, "ok": True, "status": 200} for u in urls])

    # --- files ---
    def list_dir(self, path: str) -> list[dict[str, Any]]:
        return self.snapshot.get("list_dir", {}).get(path, [])

    def delete_paths(self, paths: list[str]) -> str:
        return self._record("delete_paths", paths=paths)
