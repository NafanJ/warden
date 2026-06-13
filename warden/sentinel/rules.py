"""Deterministic anomaly rules over a signal snapshot.

Each rule returns a list of Anomaly. The sentinel opens one incident per
anomaly key (deduplicated against currently-open incidents).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from warden.config import Config


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
    for c in snapshot.get("docker_ps") or []:
        name = c.get("name") or ""
        if name in config.ignored_containers:
            continue
        state = (c.get("state") or "").lower()
        status = c.get("status") or ""
        unhealthy = "unhealthy" in status.lower()
        restarting = state == "restarting"
        down = state in ("exited", "dead", "created", "paused")
        if unhealthy or restarting or down:
            out.append(Anomaly(
                key=f"container_down:{name}",
                category="container_down",
                summary=f"Container {name} is {'unhealthy' if unhealthy else state} ({status})",
                details={"container": name, "state": state, "status": status},
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
