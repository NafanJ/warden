"""Agent runner: one agent session per incident (and per owner `diagnose`)."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from warden.agent.report import write_incident_report
from warden.agent.tiers import make_permission_handler
from warden.agent.tools import build_warden_server
from warden.backends import Backend
from warden.config import Config
from warden.notifier import Channel, get_channel
from warden.notifier.logchannel import LogChannel
from warden.sentinel.collectors import collect_snapshot
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
- You may ONLY delete files inside the downloads tree (Transmission's complete/ and \
incomplete/ folders). For space used elsewhere — orphaned recovery files, the media \
library, system dirs — do NOT call delete_paths; record it as a manual recommendation \
under '## Proposed actions' instead. Calling delete_paths outside that tree is refused.
- For disk pressure, reclaim space with the safe Tier 1 levers first: docker_prune \
(dead image/build layers — often the easiest win), then removing fully-seeded torrents \
already imported by the *arr apps, then deleting completed downloads inside the \
downloads tree. Only escalate to a Tier 2 delete when those aren't enough.
- If the system denies an action (dry-run mode or pending approval), continue the \
investigation and record it under '## Proposed actions'.
- Before restarting Plex, check tautulli_activity — if people are streaming, a \
restart will be held for owner approval (it interrupts viewers), so prefer it only \
when Plex is actually broken, and say who's affected.
- Stay within the incident's scope. Do not touch unrelated services.
- ALWAYS finish by calling write_report exactly once: title, category, \
status (resolved | escalated | monitoring), and markdown with sections \
'## Observed', '## Diagnosis', '## Actions taken', '## Outcome' \
(plus '## Proposed actions' if anything is pending). Be specific: numbers, \
container names, log lines. The report is public — no secrets, no API keys.
"""


async def _run_claude_agent(prompt_text: str, config: Config, backend: Backend,
                            store: Store, channel: Channel, incident_id: int | None,
                            run_result: dict[str, Any]) -> tuple[str, float | None]:
    """Claude Agent SDK path: tools as an in-process MCP server, the permission
    gate as can_use_tool."""
    server = build_warden_server(backend, config, store, incident_id, run_result)
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model=config.model,
        mcp_servers={"warden": server},
        can_use_tool=make_permission_handler(config, store, channel, incident_id, backend),
        max_turns=40,
        max_budget_usd=config.max_budget_usd,
        setting_sources=[],
        cwd=str(config.state_dir),
    )

    async def prompt_stream():
        # can_use_tool requires streaming-mode input (an async iterable), not a string.
        yield {"type": "user", "message": {"role": "user", "content": prompt_text}}

    result_text, cost = "", None
    async for message in query(prompt=prompt_stream(), options=options):
        if isinstance(message, ResultMessage):
            result_text = message.result or ""
            cost = getattr(message, "total_cost_usd", None)
    return result_text, cost


async def handle_incident(incident_id: int, config: Config, backend: Backend,
                          store: Store, channel: Channel | None = None) -> dict[str, Any]:
    channel = channel or get_channel(config)
    incident_file = config.state_dir / "incidents" / f"{incident_id}.json"
    incident = json.loads(incident_file.read_text())

    run_result: dict[str, Any] = {}

    prompt_text = (
        f"Incident #{incident_id} ({incident['category']}): {incident['summary']}\n\n"
        f"Trigger details:\n{json.dumps(incident['details'], indent=2, default=str)}\n\n"
        f"Sentinel snapshot at detection time:\n"
        f"{json.dumps(incident['snapshot'], indent=2, default=str)[:30000]}\n\n"
        f"Mode: {config.mode}. Investigate and handle this incident now."
    )

    if config.llm_provider == "openai":
        from warden.agent.openai_runner import run_openai_agent
        result_text, cost = run_openai_agent(
            prompt_text, SYSTEM_PROMPT, config, backend, store, channel, incident_id, run_result)
    else:
        result_text, cost = await _run_claude_agent(
            prompt_text, config, backend, store, channel, incident_id, run_result)

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
    store.set_incident_cost(incident_id, cost)

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


async def run_diagnose(question: str, config: Config, backend: Backend,
                       store: Store, channel: Channel) -> dict[str, Any]:
    """Owner-initiated, read-only investigation of a question (the `diagnose`
    command). Runs the agent in dry-run — it investigates and answers but never
    takes autonomous action — then posts its findings to the channel."""
    snapshot = collect_snapshot(backend, config)
    key = f"diagnose:{datetime.now(timezone.utc).timestamp()}"
    incident_id = store.open_incident(key, "query", question[:300])
    incident_file = config.state_dir / "incidents" / f"{incident_id}.json"
    incident_file.parent.mkdir(parents=True, exist_ok=True)
    incident_file.write_text(json.dumps({
        "incident_id": incident_id, "key": key, "category": "query",
        "summary": f"Owner question: {question}",
        "details": {"owner_question": question},
        "snapshot": snapshot,
    }, default=str))

    # dry-run: answer + propose only, never auto-act. Internal notifications go to
    # a LogChannel so the owner gets one clean answer, not warden's own chatter.
    dry = dataclasses.replace(config, mode="dry-run")
    result = await handle_incident(incident_id, dry, backend, store, LogChannel(config))
    store.close_incident(incident_id, "resolved")  # a query shouldn't linger as an incident

    body = "(no findings)"
    path = result.get("report_path")
    if path and Path(path).exists():
        md = Path(path).read_text()
        idx = md.find("## ")  # skip the report header/metadata, keep the sections
        body = (md[idx:] if idx >= 0 else md).strip()
        # if the model answered in prose, the fallback wraps it — drop the wrapper
        body = body.replace("## Agent output (no structured report was written)\n\n", "")
    cost = result.get("cost_usd")
    cost_note = ""
    if cost:
        cost_note = f"\n\n_(diagnosed for {'<$0.01' if cost < 0.01 else f'${cost:.2f}'})_"
    channel.send(f"🔍 **{question}**\n\n{body[:1700]}{cost_note}")
    return result
