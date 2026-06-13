"""Sentinel entrypoint: collect signals, apply rules, open incidents, invoke agent.

Run by a systemd timer:  python -m warden.sentinel.run
Green path costs nothing (no LLM call) — just a heartbeat line.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

from warden.backends.live import LiveBackend
from warden.config import load_config
from warden.notifier import get_channel
from warden.notifier.logchannel import LogChannel
from warden.sentinel.collectors import collect_snapshot
from warden.sentinel.reaper import reap_completed_torrents
from warden.sentinel.rules import evaluate
from warden.store import Store


def main() -> int:
    config = load_config()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    backend = LiveBackend(config)
    store = Store(config.state_dir / "warden.db")

    snapshot = collect_snapshot(backend, config)
    anomalies = evaluate(snapshot, config)

    # Routine maintenance (deterministic, no LLM): clear finished torrents the
    # owner doesn't want to keep seeding. Audited, so it shows in the summary.
    reaped = reap_completed_torrents(config, backend, store, LogChannel(config))
    reaped_note = f", reaped {len(reaped)} completed torrent(s)" if reaped else ""

    heartbeat = config.state_dir / "heartbeat.log"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    containers = snapshot.get("docker_ps") or []
    up = sum(1 for c in containers if (c.get("state") or "").lower() == "running")

    if not anomalies:
        with heartbeat.open("a") as f:
            f.write(f"{stamp} OK — {up}/{len(containers)} containers up, "
                    f"no anomalies{reaped_note}\n")
        return 0

    new_incidents = []
    for anomaly in anomalies:
        if store.find_open_incident(anomaly.key):
            continue  # already being handled / awaiting approval
        if store.find_recent_unresolved(anomaly.key, config.incident_cooldown_hours):
            continue  # handled recently but unresolved — don't re-investigate/spam
        incident_id = store.open_incident(anomaly.key, anomaly.category, anomaly.summary)
        incident_file = config.state_dir / "incidents" / f"{incident_id}.json"
        incident_file.parent.mkdir(parents=True, exist_ok=True)
        incident_file.write_text(json.dumps({
            "incident_id": incident_id,
            "key": anomaly.key,
            "category": anomaly.category,
            "summary": anomaly.summary,
            "details": anomaly.details,
            "snapshot": snapshot,
        }, indent=2, default=str))
        new_incidents.append((incident_id, anomaly))

    with heartbeat.open("a") as f:
        f.write(f"{stamp} ANOMALY — {len(anomalies)} anomaly(ies), "
                f"{len(new_incidents)} new incident(s){reaped_note}\n")

    if not new_incidents:
        return 0

    if config.mode == "detect":
        channel = get_channel(config)
        for incident_id, anomaly in new_incidents:
            channel.send(f"⚠️ warden (detect-only) incident #{incident_id}: {anomaly.summary}")
        return 0

    # dry-run / active: hand each new incident to the agent
    from warden.agent.runner import handle_incident  # late import: agent deps not needed in detect mode
    channel = get_channel(config)
    failures = 0
    for incident_id, anomaly in new_incidents:
        try:
            asyncio.run(handle_incident(incident_id, config, backend, store, channel))
        except Exception as exc:
            # A failed agent run must not crash the cycle or leave the incident
            # silently open — a stuck-open incident suppresses all future
            # detection of that problem. Close it (escalated) so it re-opens and
            # retries next cycle once the transient cause clears, and surface it.
            failures += 1
            store.close_incident(incident_id, "escalated")
            try:
                channel.send(f"⚠️ warden incident #{incident_id} ({anomaly.category}) "
                             f"could not be processed: {str(exc)[:300]}")
            except Exception:
                pass
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
