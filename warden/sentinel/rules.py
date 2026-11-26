"""Deterministic anomaly rules over a signal snapshot.

Each rule returns a list of Anomaly. The sentinel opens one incident per
anomaly key (deduplicated against currently-open incidents).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from warden.config import Config

_EXIT_CODE = re.compile(r"(?:Exited|Restarting) \((\d+)\)")


def _completed_job(c: dict[str, Any]) -> bool:
    """A finished one-shot job, not a downed service: exited with code 0 and a
    restart policy of 'no' (e.g. a compose migration). A real service that's down
    has a restart policy (always/unless-stopped) and is still flagged, even on a
    clean exit, so warden can bring it back."""
    if (c.get("state") or "").lower() != "exited":
        return False
    if (c.get("restart_policy") or "").lower() != "no":
        return False
    m = _EXIT_CODE.search(c.get("status") or "")
    return bool(m) and int(m.group(1)) == 0


def watched(containers: list[dict[str, Any]], config: Config) -> list[dict[str, Any]]:
    """The containers warden actually watches: everything except explicitly
    ignored names and finished one-shot jobs. This is the population every
    up/total count and down-check should be computed over, so a compose
    migration that exits 0 by design never reads as "22/23 up"."""
    return [c for c in containers
            if (c.get("name") or "") not in config.ignored_containers
            and not _completed_job(c)]


@dataclass
class Anomaly:
    key: str          # stable dedup key, e.g. "container_down:plex"
    category: str     # container_down | disk_pressure | disk_unavailable | stalled_download | tunnel_down | arr_queue_error
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


def evaluate(snapshot: dict[str, Any], config: Config) -> list[Anomaly]:
    anomalies: list[Anomaly] = []
    anomalies += _containers(snapshot, config)
    anomalies += _disk(snapshot, config)
    anomalies += _mounts(snapshot)
    anomalies += _stalls(snapshot, config)
    anomalies += _arr_errors(snapshot)
    anomalies += _urls(snapshot)
    return anomalies


def _containers(snapshot: dict[str, Any], config: Config) -> list[Anomaly]:
    out = []
    for c in watched(snapshot.get("docker_ps") or [], config):
        name = c.get("name") or ""
        state = (c.get("state") or "").lower()
        status = c.get("status") or ""
        unhealthy = "unhealthy" in status.lower()
        restarting = state == "restarting"
        down = state in ("exited", "dead", "created", "paused")
        if not (unhealthy or restarting or down):
            continue
        m = _EXIT_CODE.search(status)
        exit_code = int(m.group(1)) if m else None
        restart_count = c.get("restart_count")
        if restarting:
            # Docker is bouncing it via its restart policy: a crash loop. The exit
            # code (e.g. 139 = SIGSEGV) only shows in this live status — a later
            # inspect, once the loop heals to 'running', reports exit 0 — so name it
            # plainly here and carry the code into the incident as ground truth.
            condition = "in a crash loop"
        elif unhealthy:
            condition = "unhealthy"
        else:
            condition = state
        out.append(Anomaly(
            key=f"container_down:{name}",
            category="container_down",
            summary=f"Container {name} is {condition} ({status})",
            details={"container": name, "state": state, "status": status,
                     "exit_code": exit_code, "restart_count": restart_count},
        ))
    return out


def _disk(snapshot: dict[str, Any], config: Config) -> list[Anomaly]:
    out = []
    for d in snapshot.get("disk_usage") or []:
        if d["used_pct"] >= config.disk_threshold_pct:
            out.append(Anomaly(
                key=f"disk_pressure:{d['path']}",
                category="disk_pressure",
                summary=f"{d['path']} at {d['used_pct']}% ({d['free_gb']}GB free)",
                details=d,
            ))
    return out


def _mounts(snapshot: dict[str, Any]) -> list[Anomaly]:
    out = []
    for m in snapshot.get("mount_health") or []:
        if not m.get("mounted"):
            problem = "is NOT MOUNTED (drive dropped off?)"
        elif not m.get("accessible"):
            problem = f"is INACCESSIBLE — {m.get('error') or 'I/O error'}"
        elif m.get("read_only"):
            problem = "went READ-ONLY (filesystem errors?)"
        else:
            continue
        out.append(Anomaly(
            key=f"disk_unavailable:{m['path']}",
            category="disk_unavailable",
            summary=f"Mount {m['path']} {problem}",
            details=m,
        ))
    return out


def _stalls(snapshot: dict[str, Any], config: Config) -> list[Anomaly]:
    out = []
    for t in snapshot.get("torrents") or []:
        if t.get("percentDone", 0) >= 1.0:
            continue
        if (t.get("age_hours") or 0) < config.stall_min_age_hours:
            continue
        inactive = t.get("inactive_hours")
        if inactive is None or inactive < config.stall_threshold_hours:
            continue
        # No piece activity for the threshold window is a stall whether or not
        # peers are connected: 0 peers = dead source, peers-but-idle = stuck.
        peers = t.get("peersConnected", 0)
        descriptor = "0 peers" if peers == 0 else f"{peers} peers but no movement"
        out.append(Anomaly(
            key=f"stalled_download:{t['hashString']}",
            category="stalled_download",
            summary=f"Torrent stalled {inactive}h ({descriptor}): {t['name']}",
            details={k: t.get(k) for k in
                     ("id", "name", "hashString", "percentDone", "inactive_hours",
                      "age_hours", "peersConnected")},
        ))
    return out


def _arr_errors(snapshot: dict[str, Any]) -> list[Anomaly]:
    out = []
    queues = snapshot.get("arr_queue") or {}
    for app, records in queues.items():
        errored = [r for r in records or [] if r.get("error_message")]
        if errored:
            out.append(Anomaly(
                key=f"arr_queue_error:{app}",
                category="arr_queue_error",
                summary=f"{app} queue has {len(errored)} errored item(s)",
                details={"app": app, "errored": errored[:10]},
            ))
    return out


def _urls(snapshot: dict[str, Any]) -> list[Anomaly]:
    out = []
    failing = [u for u in snapshot.get("url_checks") or [] if not u.get("ok")]
    if failing:
        out.append(Anomaly(
            key="tunnel_down",
            category="tunnel_down",
            summary=f"{len(failing)} public URL(s) unreachable: "
                    + ", ".join(u["url"] for u in failing),
            details={"failing": failing},
        ))
    return out
