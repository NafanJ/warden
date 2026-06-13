"""LiveBackend: real docker CLI, real HTTP APIs, real filesystem.

Runs on the warden host (blink). All mutating methods are still gated by the
permission tiers in warden.agent.tiers — this class only knows *how* to act,
never *whether* it may.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from warden.config import Config, deletable


def _run(cmd: list[str], timeout: int = 30) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        return f"ERROR (exit {out.returncode}): {out.stderr.strip()[:2000]}"
    return out.stdout


class LiveBackend:
    def __init__(self, config: Config):
        self.config = config
        self._tx_session_id = ""

    # --- docker ---
    def docker_ps(self) -> list[dict[str, Any]]:
        raw = _run(["docker", "ps", "-a", "--format", "{{json .}}"])
        rows = [json.loads(line) for line in raw.splitlines() if line.strip().startswith("{")]
        return [
            {"name": r.get("Names"), "image": r.get("Image"), "status": r.get("Status"), "state": r.get("State")}
            for r in rows
        ]

    def container_logs(self, name: str, lines: int = 100) -> str:
        out = subprocess.run(
            ["docker", "logs", "--tail", str(min(lines, 500)), name],
            capture_output=True, text=True, timeout=30,
        )
        return (out.stdout + out.stderr)[-20000:]

    def container_inspect(self, name: str) -> dict[str, Any]:
        raw = _run(["docker", "inspect", name])
        if raw.startswith("ERROR"):
            return {"error": raw}
        data = json.loads(raw)[0]
        state = data.get("State", {})
        host_cfg = data.get("HostConfig", {})
        return {
            "name": name,
            "state": state,
            "restart_count": data.get("RestartCount"),
            "oom_killed": state.get("OOMKilled"),
            "exit_code": state.get("ExitCode"),
            "memory_limit": host_cfg.get("Memory"),
            "restart_policy": host_cfg.get("RestartPolicy"),
        }

    def container_restart(self, name: str) -> str:
        return _run(["docker", "restart", name], timeout=120) or f"restarted {name}"

    # --- system ---
    def disk_usage(self, paths: list[str]) -> list[dict[str, Any]]:
        results = []
        for p in paths:
            total, used, free = shutil.disk_usage(p)
            results.append({
                "path": p,
                "total_gb": round(total / 1e9, 1),
                "used_gb": round(used / 1e9, 1),
                "free_gb": round(free / 1e9, 1),
                "used_pct": round(used / total * 100, 1),
            })
        return results

    def du_summary(self, path: str, depth: int = 1) -> str:
        return _run(["du", "-h", f"--max-depth={depth}", path], timeout=300)

    def memory(self) -> dict[str, Any]:
        fields = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            fields[key] = rest.strip()
        return {k: fields.get(k) for k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")}

    # --- transmission ---
    def _tx_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        auth = base64.b64encode(
            f"{self.config.transmission_user}:{self.config.transmission_pass}".encode()
        ).decode()
        headers = {"Authorization": f"Basic {auth}"}
        for _ in range(2):
            headers["X-Transmission-Session-Id"] = self._tx_session_id
            resp = httpx.post(self.config.transmission_url, json=payload, headers=headers, timeout=30)
            if resp.status_code == 409:
                self._tx_session_id = resp.headers.get("X-Transmission-Session-Id", "")
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("Transmission session handshake failed")

    def torrents(self) -> list[dict[str, Any]]:
        resp = self._tx_request({
            "method": "torrent-get",
            "arguments": {"fields": [
                "id", "name", "hashString", "percentDone", "peersConnected",
                "activityDate", "addedDate", "errorString", "status",
            ]},
        })
        now = time.time()
        out = []
        for t in resp["arguments"]["torrents"]:
            out.append({
                **t,
                "age_hours": round((now - t["addedDate"]) / 3600, 1),
                "inactive_hours": round((now - t["activityDate"]) / 3600, 1) if t["activityDate"] else None,
            })
        return out

    def remove_torrents(self, ids: list[int], delete_data: bool = False) -> str:
        self._tx_request({
            "method": "torrent-remove",
            "arguments": {"ids": ids, "delete-local-data": delete_data},
        })
        return f"removed torrents {ids} (delete_data={delete_data})"

    # --- sonarr / radarr ---
    def _arr(self, app: str) -> tuple[str, str]:
        if app == "sonarr":
            return self.config.sonarr_url, self.config.sonarr_api_key
        if app == "radarr":
            return self.config.radarr_url, self.config.radarr_api_key
        raise ValueError(f"unknown arr app: {app}")

    def arr_queue(self, app: str) -> list[dict[str, Any]]:
        url, key = self._arr(app)
        resp = httpx.get(f"{url}/api/v3/queue", params={"pageSize": 200},
                         headers={"X-Api-Key": key}, timeout=30)
        resp.raise_for_status()
        records = resp.json().get("records", [])
        return [{
            "id": r.get("id"),
            "title": r.get("title"),
            "download_id": r.get("downloadId"),
            "status": r.get("status"),
            "tracked_status": r.get("trackedDownloadStatus"),
            "error_message": r.get("errorMessage"),
            "size_left": r.get("sizeleft"),
        } for r in records]

    def arr_blocklist_and_research(self, app: str, queue_ids: list[int]) -> str:
        url, key = self._arr(app)
        resp = httpx.request(
            "DELETE", f"{url}/api/v3/queue/bulk",
            params={"removeFromClient": "true", "blocklist": "true"},
            headers={"X-Api-Key": key}, json={"ids": queue_ids}, timeout=30,
        )
        resp.raise_for_status()
        return f"blocklisted {len(queue_ids)} item(s) in {app}; re-search will trigger automatically"

    # --- network ---
    def check_urls(self, urls: list[str]) -> list[dict[str, Any]]:
        results = []
        for url in urls:
            try:
                resp = httpx.get(url, timeout=10, follow_redirects=True)
                results.append({"url": url, "ok": resp.status_code < 500, "status": resp.status_code})
            except Exception as exc:
                results.append({"url": url, "ok": False, "error": str(exc)[:200]})
        return results

    # --- files ---
    def list_dir(self, path: str) -> list[dict[str, Any]]:
        p = Path(path)
        out = []
        for child in sorted(p.iterdir()):
            try:
                stat = child.stat()
                size = stat.st_size
                if child.is_dir():
                    size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
                out.append({
                    "path": str(child),
                    "is_dir": child.is_dir(),
                    "size_gb": round(size / 1e9, 2),
                    "mtime": stat.st_mtime,
                })
            except OSError as exc:
                out.append({"path": str(child), "error": str(exc)})
        return out

    def delete_paths(self, paths: list[str]) -> str:
        deleted = []
        for raw in paths:
            p = Path(raw).resolve()
            if not deletable(str(p), self.config.delete_roots):
                raise PermissionError(f"refusing to delete (outside roots or a root itself): {p}")
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink(missing_ok=True)
            deleted.append(str(p))
        return f"deleted {len(deleted)} path(s): " + ", ".join(deleted)
