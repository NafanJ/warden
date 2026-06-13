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
from warden.sentinel.collectors import collect_snapshot
from warden.sentinel.rules import evaluate
from warden.store import Store


def main() -> int:
    config = load_config()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    backend = LiveBackend(config)
    store = Store(config.state_dir / "warden.db")

    snapshot = collect_snapshot(backend, config)
    anomalies = evaluate(snapshot, config)

    heartbeat = config.state_dir / "heartbeat.log"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    containers = snapshot.get("docker_ps") or []
    up = sum(1 for c in containers if (c.get("state") or "").lower() == "running")

    if not anomalies:
        with heartbeat.open("a") as f:
            f.write(f"{stamp} OK — {up}/{len(containers)} containers up, no anomalies\n")
        return 0

    new_incidents = []
    for anomaly in anomalies:
        if store.find_open_incident(anomaly.key):
            continue  # already being handled / awaiting approval
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
                f"{len(new_incidents)} new incident(s)\n")

    if not new_incidents:
        return 0

    if config.mode == "detect":
        channel = get_channel(config)
        for incident_id, anomaly in new_incidents:
            channel.send(f"⚠️ warden (detect-only) incident #{incident_id}: {anomaly.summary}")
        return 0

    # dry-run / active: hand each new incident to the agent
    from warden.agent.runner import handle_incident  # late import: agent deps not needed in detect mode
    for incident_id, anomaly in new_incidents:
        asyncio.run(handle_incident(incident_id, config, backend, store))
    return 0


if __name__ == "__main__":
    sys.exit(main())
