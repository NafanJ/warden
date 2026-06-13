"""Agent runner: one Claude Agent SDK session per incident."""
from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from warden.agent.report import write_incident_report
from warden.agent.tiers import make_permission_handler
from warden.agent.tools import build_warden_server
from warden.backends import Backend
from warden.config import Config
from warden.notifier import Channel, get_channel
from warden.store import Store

SYSTEM_PROMPT = """You are warden, the on-call operations agent for "blink", a small \
home media server (Ubuntu, Docker). The deterministic sentinel detected an anomaly and \
opened the incident you are given. Your job: investigate, diagnose the root cause, fix \
what you safely can, and write a clear incident report.

The stack: Plex (media), Sonarr/Radarr (TV/movie automation), Prowlarr (indexers), \
Transmission (downloads, /downloads/incomplete -> /downloads/complete -> imported by \
the *arr apps), Overseerr (requests), Cloudflare Tunnel (public access), plus \
supporting containers (Tautulli, Homepage, Uptime Kuma, FileBrowser, Immich, \
AudioBookshelf, Watchtower, Recyclarr). Media lives on the external mount monitored \
as the larger disk; the OS runs on the smaller one.

Operating rules:
- Investigate with read tools BEFORE acting. Logs and inspect output beat guessing.
- Distinguish "restart fixes it" from "restart hides it": check exit codes, OOM flags, \
restart counts, and the last log lines for the real cause.
- Reversible fixes (Tier 1: container restart, blocklist+re-search a stuck download) \
you may take yourself. Destructive actions (Tier 2: deleting files) are queued for \
owner approval automatically when you call them — never retry a queued action.
- If the system denies an action (dry-run mode or pending approval), continue the \
investigation and record it under '## Proposed actions'.
- Stay within the incident's scope. Do not touch unrelated services.
- ALWAYS finish by calling write_report exactly once: title, category, \
status (resolved | escalated | monitoring), and markdown with sections \
'## Observed', '## Diagnosis', '## Actions taken', '## Outcome' \
(plus '## Proposed actions' if anything is pending). Be specific: numbers, \
container names, log lines. The report is public — no secrets, no API keys.
"""


async def handle_incident(incident_id: int, config: Config, backend: Backend,
                          store: Store, channel: Channel | None = None) -> dict[str, Any]:
    channel = channel or get_channel(config)
    incident_file = config.state_dir / "incidents" / f"{incident_id}.json"
    incident = json.loads(incident_file.read_text())

    run_result: dict[str, Any] = {}
    server = build_warden_server(backend, config, store, incident_id, run_result)

    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model=config.model,
        mcp_servers={"warden": server},
        can_use_tool=make_permission_handler(config, store, channel, incident_id),
        max_turns=40,
        max_budget_usd=config.max_budget_usd,
        setting_sources=[],
        cwd=str(config.state_dir),
    )

    prompt = (
        f"Incident #{incident_id} ({incident['category']}): {incident['summary']}\n\n"
        f"Trigger details:\n{json.dumps(incident['details'], indent=2, default=str)}\n\n"
        f"Sentinel snapshot at detection time:\n"
        f"{json.dumps(incident['snapshot'], indent=2, default=str)[:30000]}\n\n"
        f"Mode: {config.mode}. Investigate and handle this incident now."
    )

    result_text, cost = "", None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result_text = message.result or ""
            cost = getattr(message, "total_cost_usd", None)

    if "report_path" not in run_result:
        # agent failed to call write_report — preserve whatever it concluded
        path = write_incident_report(
            config, incident_id, f"Incident #{incident_id}: {incident['summary']}",
            incident["category"], "escalated",
            f"## Agent output (no structured report was written)\n\n{result_text or '(empty)'}",
        )
        run_result.update({"category": incident["category"], "status": "escalated",
                           "report_path": str(path)})
        store.set_report_path(incident_id, str(path))

    run_result["cost_usd"] = cost

    status = run_result.get("status", "escalated")
    if status in ("resolved", "escalated"):
        store.close_incident(incident_id, status, run_result.get("report_path"))
    # 'monitoring' stays open so the sentinel won't re-open a duplicate

    emoji = {"resolved": "✅", "monitoring": "👀"}.get(status, "⚠️")
    channel.send(
        f"{emoji} warden incident #{incident_id} [{status}] "
        f"{run_result.get('title', incident['summary'])}"
        + (f" (cost ${cost:.2f})" if cost else "")
    )
    return run_result
