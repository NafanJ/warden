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

    def docker_prune(self) -> str:
        return self._record("docker_prune")

    # --- system ---
    def disk_usage(self, paths: list[str]) -> list[dict[str, Any]]:
        return self.snapshot.get("disk_usage", [])

    def du_summary(self, path: str, depth: int = 1) -> str:
        return self.snapshot.get("du_summary", {}).get(path, f"(no du captured for {path})")

    def memory(self) -> dict[str, Any]:
        return self.snapshot.get("memory", {})

    def mount_health(self, paths: list[str]) -> list[dict[str, Any]]:
        return self.snapshot.get("mount_health",
                                 [{"path": p, "mounted": True, "read_only": False,
                                   "accessible": True, "error": None} for p in paths])

    # --- transmission ---
    def torrents(self) -> list[dict[str, Any]]:
        return self.snapshot.get("torrents", [])

    def remove_torrents(self, ids: list[int], delete_data: bool = False) -> str:
        return self._record("remove_torrents", ids=ids, delete_data=delete_data)

    # --- sonarr / radarr ---
    def arr_queue(self, app: str) -> list[dict[str, Any]]:
        return self.snapshot.get("arr_queue", {}).get(app, [])

    def arr_categories(self) -> set[str]:
        return set(self.snapshot.get("arr_categories", []))

    def arr_blocklist_and_research(self, app: str, queue_ids: list[int]) -> str:
        # record under the tool name (arr_blocklist_research), which is the
        # vocabulary the agent and the eval fixtures use — not the backend method name.
        return self._record("arr_blocklist_research", app=app, queue_ids=queue_ids)

    # --- tautulli ---
    def tautulli_activity(self) -> dict[str, Any]:
        return self.snapshot.get("tautulli_activity",
                                 {"stream_count": 0, "transcode_count": 0,
                                  "bandwidth_mbps": 0.0, "sessions": []})

    def tautulli_users(self) -> list[dict[str, Any]]:
        return self.snapshot.get("tautulli_users", [])

    def tautulli_user_stats(self, user_id: int) -> list[dict[str, Any]]:
        return self.snapshot.get("tautulli_user_stats", {}).get(str(user_id), [])

    # --- network ---
    def check_urls(self, urls: list[str]) -> list[dict[str, Any]]:
        return self.snapshot.get("url_checks", [{"url": u, "ok": True, "status": 200} for u in urls])

    # --- files ---
    def list_dir(self, path: str) -> list[dict[str, Any]]:
        return self.snapshot.get("list_dir", {}).get(path, [])

    def delete_paths(self, paths: list[str]) -> str:
        return self._record("delete_paths", paths=paths)
