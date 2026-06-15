"""LiveBackend: real docker CLI, real HTTP APIs, real filesystem.

Runs on the warden host (blink). All mutating methods are still gated by the
permission tiers in warden.agent.tiers — this class only knows *how* to act,
never *whether* it may.
"""
from __future__ import annotations

import base64
import json
import os
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
        out = [
            {"name": r.get("Names"), "image": r.get("Image"), "status": r.get("Status"), "state": r.get("State")}
            for r in rows
        ]
        # Restart policy distinguishes a completed one-shot job (restart=no) from a
        # down service that should be running — the rules use it to avoid paging on
        # finished jobs. One inspect call covers every container.
        names = [c["name"] for c in out if c["name"]]
        if names:
            info = _run(["docker", "inspect", "--format",
                         "{{.Name}}\t{{.HostConfig.RestartPolicy.Name}}", *names])
            policy = {}
            for line in info.splitlines():
                if "\t" in line:
                    n, _, pol = line.partition("\t")
                    policy[n.lstrip("/").strip()] = pol.strip()
            for c in out:
                c["restart_policy"] = policy.get(c["name"], "")
        return out

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

    def docker_prune(self) -> str:
        # dangling images + build cache only — never -a, so nothing a container
        # (running or stopped) references is touched. Pure reclaim.
        images = _run(["docker", "image", "prune", "-f"], timeout=120)
        cache = _run(["docker", "builder", "prune", "-f"], timeout=120)
        return f"docker image prune:\n{images.strip()}\n\ndocker builder prune:\n{cache.strip()}"

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

    def mount_health(self, paths: list[str]) -> list[dict[str, Any]]:
        """Per-mount health: is it mounted, accessible, and writable. Catches a
        USB drive that dropped off or a filesystem the kernel flipped read-only
        after I/O errors — no root or smartctl needed."""
        opts: dict[str, list[str]] = {}
        try:
            for line in Path("/proc/mounts").read_text().splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    opts[parts[1].replace("\\040", " ")] = parts[3].split(",")
        except OSError:
            pass
        out = []
        for p in paths:
            rec: dict[str, Any] = {
                "path": p,
                "mounted": p in opts or Path(p).is_mount(),
                "read_only": "ro" in opts.get(p, []),
                "accessible": True,
                "error": None,
            }
            try:
                os.statvfs(p)  # raises on a stale / dropped mount
            except OSError as exc:
                rec["accessible"] = False
                rec["error"] = str(exc)[:120]
            out.append(rec)
        return out

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
                "activityDate", "addedDate", "errorString", "status", "downloadDir",
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

    def set_download_throttle(self, on: bool) -> str:
        """Toggle Transmission's alt-speed ('turtle') mode — a reversible global
        bandwidth cap. Used to protect Plex playback while someone is streaming."""
        self._tx_request({
            "method": "session-set",
            "arguments": {"alt-speed-enabled": bool(on)},
        })
        return f"alt-speed {'enabled' if on else 'disabled'}"

    def download_throttled(self) -> bool:
        resp = self._tx_request({
            "method": "session-get",
            "arguments": {"fields": ["alt-speed-enabled"]},
        })
        return bool(resp["arguments"]["alt-speed-enabled"])

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

    def arr_categories(self) -> set[str]:
        """Download-client categories the *arr apps assign (e.g. 'tv-sonarr',
        'radarr'). A torrent landing in such a category subdir is *arr-managed —
        Sonarr/Radarr own its import + cleanup, so the reaper leaves it alone."""
        cats: set[str] = set()
        for app in ("sonarr", "radarr"):
            url, key = self._arr(app)
            resp = httpx.get(f"{url}/api/v3/downloadclient",
                             headers={"X-Api-Key": key}, timeout=30)
            resp.raise_for_status()
            for client in resp.json():
                for f in client.get("fields", []):
                    if str(f.get("name", "")).lower().endswith("category") and f.get("value"):
                        cats.add(str(f["value"]))
        return cats

    def arr_blocklist_and_research(self, app: str, queue_ids: list[int]) -> str:
        url, key = self._arr(app)
        resp = httpx.request(
            "DELETE", f"{url}/api/v3/queue/bulk",
            params={"removeFromClient": "true", "blocklist": "true"},
            headers={"X-Api-Key": key}, json={"ids": queue_ids}, timeout=30,
        )
        resp.raise_for_status()
        return f"blocklisted {len(queue_ids)} item(s) in {app}; re-search will trigger automatically"

    # --- tautulli (plex activity + user stats) ---
    def _tautulli(self, cmd: str, **params: Any) -> Any:
        resp = httpx.get(f"{self.config.tautulli_url}/api/v2",
                         params={"apikey": self.config.tautulli_api_key, "cmd": cmd, **params},
                         timeout=30)
        resp.raise_for_status()
        return resp.json()["response"]["data"]

    def tautulli_activity(self) -> dict[str, Any]:
        d = self._tautulli("get_activity")
        return {
            "stream_count": int(d.get("stream_count") or 0),
            "transcode_count": int(d.get("stream_count_transcode") or 0),
            "bandwidth_mbps": round((int(d.get("total_bandwidth") or 0)) / 1000, 1),
            "sessions": [{
                "user": s.get("friendly_name"),
                "title": s.get("full_title"),
                "state": s.get("state"),
                "transcode": s.get("transcode_decision") == "transcode",
            } for s in d.get("sessions", [])],
        }

    def tautulli_users(self) -> list[dict[str, Any]]:
        d = self._tautulli("get_users_table", order_column="plays", order_dir="desc", length=100)
        return [{
            "user_id": u.get("user_id"),
            "name": u.get("friendly_name"),
            "plays": u.get("plays") or 0,
            "duration_seconds": u.get("duration") or 0,
            "last_seen": u.get("last_seen"),
        } for u in d.get("data", []) if u.get("friendly_name") and u.get("user_id")]

    def tautulli_user_stats(self, user_id: int) -> list[dict[str, Any]]:
        rows = self._tautulli("get_user_watch_time_stats", user_id=user_id)
        return [{"days": r.get("query_days"), "plays": r.get("total_plays") or 0,
                 "seconds": r.get("total_time") or 0} for r in rows]

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
