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


def _inspect_view(name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Compact, decision-oriented view of `docker inspect` output.

    Crash-loop trap: when a container is bouncing via its restart policy, the
    crash exit code (e.g. 139 = SIGSEGV) only shows while it's momentarily
    'restarting'. Once a start succeeds, State.ExitCode resets to 0 and Health
    reads 'healthy' — so an inspect run *after* the loop heals looks clean and
    hides the crash. restart_count survives, so we surface it prominently and,
    when the container reads healthy but has restarted, attach a note so the
    agent doesn't mistake a recovered crash loop for a false alarm.
    """
    state = data.get("State", {})
    host_cfg = data.get("HostConfig", {})
    status = state.get("Status")              # running | restarting | exited | …
    exit_code = state.get("ExitCode")
    restart_count = data.get("RestartCount")
    health = (state.get("Health") or {}).get("Status")

    note = None
    if restart_count and status == "restarting":
        note = (f"Crash loop: Docker is bouncing this container (restarted "
                f"{restart_count}x). exit_code {exit_code} is the crash signal "
                f"(139 = SIGSEGV, 137 = SIGKILL/OOM).")
    elif restart_count and status == "running" and not exit_code:
        note = (f"Running now, but has restarted {restart_count}x. exit_code/"
                f"health reflect the latest (successful) start, not the crashes "
                f"— a 'Restarting (N)' in the incident snapshot means it was "
                f"crash-looping with exit code N and has since recovered. Treat "
                f"that as a real crash loop, not a false alarm; check logs across "
                f"the restarts for the cause (a silent native SIGSEGV logs nothing).")

    return {
        "name": name,
        "status": status,
        "running": state.get("Running"),
        "health": health,
        "exit_code": exit_code,
        "oom_killed": state.get("OOMKilled"),
        "restart_count": restart_count,
        "started_at": state.get("StartedAt"),
        "finished_at": state.get("FinishedAt"),
        "restart_policy": (host_cfg.get("RestartPolicy") or {}).get("Name"),
        "memory_limit": host_cfg.get("Memory"),
        "note": note,
    }


class LiveBackend:
    def __init__(self, config: Config):
        self.config = config
        self._tx_session_id = ""

    # --- docker ---
    def docker_ps(self) -> list[dict[str, Any]]:
        raw = _run(["docker", "ps", "-a", "--format", "{{json .}}"])
        if raw.startswith("ERROR"):
            raise RuntimeError(raw)
        rows = [json.loads(line) for line in raw.splitlines() if line.strip().startswith("{")]
        out = [
            {"name": r.get("Names"), "image": r.get("Image"), "status": r.get("Status"), "state": r.get("State")}
            for r in rows
        ]
        # Restart policy distinguishes a completed one-shot job (restart=no) from a
        # down service that should be running — the rules use it to avoid paging on
        # finished jobs. RestartCount surfaces a crash loop (a container Docker is
        # bouncing repeatedly) even when this poll catches it momentarily 'running'.
        # One inspect call covers every container.
        names = [c["name"] for c in out if c["name"]]
        if names:
            info = _run(["docker", "inspect", "--format",
                         "{{.Name}}\t{{.HostConfig.RestartPolicy.Name}}\t{{.RestartCount}}",
                         *names])
            extra: dict[str, tuple[str, int | None]] = {}
            for line in info.splitlines():
                parts = line.split("\t")
                if len(parts) >= 3:
                    name = parts[0].lstrip("/").strip()
                    try:
                        count: int | None = int(parts[2].strip())
                    except ValueError:
                        count = None
                    extra[name] = (parts[1].strip(), count)
            for c in out:
                pol, count = extra.get(c["name"], ("", None))
                c["restart_policy"] = pol
                c["restart_count"] = count
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
        return _inspect_view(name, json.loads(raw)[0])

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
                "totalSize",
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

    # --- plex (primary source for user stats) ---
    def _plex_get(self, path: str, base: str | None = None, **params: Any) -> Any:
        resp = httpx.get(f"{base or self.config.plex_url}{path}",
                         params={"X-Plex-Token": self.config.plex_token, **params},
                         headers={"Accept": "application/json"}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def plex_users(self) -> list[dict[str, Any]]:
        """Everyone with access to the server.

        Two sources because neither is complete: /accounts lists people who have
        actually played something (and carries the accountID history is keyed by),
        while a freshly-invited friend exists only on plex.tv until their first play.
        """
        users: list[dict[str, Any]] = []
        seen: set[str] = set()
        container = self._plex_get("/accounts").get("MediaContainer", {})
        for a in container.get("Account", []):
            name = (a.get("name") or "").strip()
            aid = a.get("id")
            # id 0 is the synthetic "all users" row Plex prepends to the list
            if not name or aid in (None, 0, "0"):
                continue
            users.append({"account_id": int(aid), "name": name})
            seen.add(name.lower())
        try:
            friends = self._plex_get("/api/v2/friends", base="https://plex.tv")
        except Exception:
            # plex.tv unreachable — /accounts still covers everyone who has watched,
            # which is everyone with stats to report.
            friends = []
        for f in friends or []:
            name = (f.get("title") or f.get("username") or "").strip()
            if name and name.lower() not in seen:
                users.append({"account_id": int(f.get("id") or 0), "name": name})
                seen.add(name.lower())
        return users

    def _plex_durations(self, rating_keys: list[str]) -> dict[str, int]:
        """Item runtimes in seconds, keyed by ratingKey.

        History rows carry no duration of their own, so runtimes have to be
        joined on from the library. /library/metadata takes comma-separated
        keys, which turns hundreds of lookups into a handful of requests;
        keys that no longer exist are omitted from the response rather than
        failing it, so a deleted item costs only its own runtime.
        """
        out: dict[str, int] = {}
        keys = [k for k in rating_keys if k]
        for i in range(0, len(keys), 100):
            chunk = ",".join(keys[i:i + 100])
            try:
                container = self._plex_get(f"/library/metadata/{chunk}").get("MediaContainer", {})
            except Exception:
                continue
            for m in container.get("Metadata", []):
                out[str(m.get("ratingKey"))] = int(m.get("duration") or 0) // 1000
        return out

    def plex_history(self, account_id: int | None = None) -> list[dict[str, Any]]:
        """Server-side watch history — one row per play, all users included.

        This is what Tautulli can't give us for invitees it never saw stream.
        A row means Plex scrobbled the item as watched, so `seconds` is the
        item's runtime, not measured viewing time: unlike Tautulli it doesn't
        account for partial plays, pauses or re-watches of part of an episode.
        """
        params: dict[str, Any] = {"sort": "viewedAt:desc"}
        if account_id is not None:
            params["accountID"] = account_id
        rows: list[dict[str, Any]] = []
        start, page, cap = 0, 500, 20_000
        while start < cap:
            container = self._plex_get(
                "/status/sessions/history/all", **params,
                **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": page},
            ).get("MediaContainer", {})
            batch = container.get("Metadata", [])
            for m in batch:
                rows.append({
                    "account_id": int(m.get("accountID") or 0),
                    "viewed_at": int(m.get("viewedAt") or 0),
                    "rating_key": str(m.get("ratingKey") or ""),
                    "seconds": 0,
                    "title": m.get("grandparentTitle") or m.get("title"),
                })
            start += page
            if not batch or len(batch) < page or start >= int(container.get("totalSize") or 0):
                break
        durations = self._plex_durations(sorted({r["rating_key"] for r in rows}))
        for r in rows:
            r["seconds"] = durations.get(r["rating_key"], 0)
        return rows

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
