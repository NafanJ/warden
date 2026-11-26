"""Full-access owner tasks: `@warden !full <task>` spawns a real Claude Code
session on the host — shell, files, the lot — via the Claude Agent SDK.

This deliberately steps outside warden's 18-tool sandbox, so it is fenced in
four ways:
- disabled unless ENABLE_FULL_TASKS=true (an approved Discord message becomes
  arbitrary command execution on the host — that's an explicit opt-in),
- reachable only through the owner-gated mention/typed path,
- never run directly: every request is queued as a Tier 2 action and starts
  only after the owner taps Approve — same store, buttons, and audit trail as
  a destructive delete,
- budget-capped per task, and the result is redacted before being posted.

The session itself runs in a daemon thread so a multi-minute task doesn't
stall the poller; the result posts to the channel when it finishes.
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from warden.agent.report import redact
from warden.config import Config
from warden.notifier import Channel
from warden.store import Store

FULL_TASK_TOOL = "claude_code_task"

SYSTEM_PROMPT = """You are warden's full-access mode, running directly on "blink", \
a small Ubuntu home media server (Docker stack: Plex, Sonarr/Radarr, Prowlarr, \
Transmission, Overseerr, Cloudflare Tunnel, Tautulli, Immich, and friends; media \
on the external mount, OS on the internal disk). The owner explicitly approved \
this task over Discord and is not watching a terminal — work autonomously, verify \
what you change, and don't ask questions you can answer from the machine itself.

Rules:
- Do the task you were given; don't refactor or "improve" things beyond it.
- Prefer reversible steps, and note anything you change so it can be undone.
- Never include secrets (API keys, tokens, passwords) in your final answer.
- Finish with a short plain-language summary of what you did and what, if
  anything, still needs the owner. It is posted to Discord (2000-char cap),
  so keep it tight."""


def queue_full_task(task: str, config: Config, store: Store,
                    channel: Channel) -> str | None:
    """Queue a full-access task for owner approval. Returns a message for the
    owner, or None when the approval prompt itself is the visible feedback."""
    if config.engine == "langgraph":
        return "Warden: cloud full tasks are disabled in local-only mode."
    if not config.enable_full_tasks:
        return ("⛔ Full tasks are disabled. Set ENABLE_FULL_TASKS=true in .env first — "
                "be aware this lets an approved Discord message run shell commands "
                "on the host.")
    pending = store.find_pending_action(FULL_TASK_TOOL, {"task": task})
    if pending:
        return (f"That exact task is already pending approval (#{pending['id']}) — "
                f"tap ✅ on it or reply YES {pending['id']}.")

    description = (f"Run a FULL Claude Code session on the host — shell and file "
                   f"access, budget ${config.full_task_budget_usd:.2f}:\n“{task}”")
    action_id = store.queue_action(None, FULL_TASK_TOOL, {"task": task}, 2, description)
    store.audit(FULL_TASK_TOOL, {"task": task}, 2, "queued", None)
    ref = channel.send_approval(
        action_id,
        f"🛡️ warden needs approval (action #{action_id}):\n{description}\n\n"
        f"Tap ✅ to approve or ❌ to reject (or reply YES {action_id} / NO {action_id}).")
    if ref:
        store.set_action_notify_ref(action_id, ref)
    return None


def start_full_task(action: dict[str, Any], config: Config, store: Store,
                    channel: Channel) -> str:
    """Kick off an approved full task in a background thread (a session can run
    for minutes; the poller must keep polling). Returns the immediate reply."""
    if config.engine == "langgraph":
        return "Warden: cloud full tasks are disabled in local-only mode."
    task = json.loads(action["args_json"])["task"]
    threading.Thread(target=_run_and_post,
                     args=(int(action["id"]), task, config, store, channel),
                     name=f"full-task-{action['id']}", daemon=True).start()
    return (f"warden: full task #{action['id']} started 🚀 — running with shell "
            "access; results will post here when it finishes.")


def _run_and_post(action_id: int, task: str, config: Config, store: Store,
                  channel: Channel) -> None:
    try:
        summary, cost = asyncio.run(_run_claude_code(task, config))
        summary = redact(summary or "(the session produced no summary)", config.secrets())
        store.mark_executed(action_id, summary)
        cost_note = f"\n\n_(ran for ${cost:.2f})_" if cost else ""
        channel.send(f"🤖 full task #{action_id} finished\n"
                     f"**{task[:150]}**\n\n{summary[:1600]}{cost_note}")
    except Exception as exc:
        store.mark_executed(action_id, str(exc), failed=True)
        try:
            channel.send(f"🤖 full task #{action_id} FAILED ❌ — {str(exc)[:300]}")
        except Exception:
            pass  # the failure is recorded in the store either way


async def _run_claude_code(task: str, config: Config) -> tuple[str, float | None]:
    """One unrestricted Claude Code session. bypassPermissions is the point:
    the human approval already happened, on the Discord prompt."""
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model=config.model,
        permission_mode="bypassPermissions",
        cwd=config.full_task_cwd or str(Path.home()),
        max_turns=100,
        max_budget_usd=config.full_task_budget_usd,
        setting_sources=[],
    )
    result_text, cost = "", None
    async for message in query(prompt=task, options=options):
        if isinstance(message, ResultMessage):
            result_text = message.result or ""
            cost = getattr(message, "total_cost_usd", None)
    return result_text, cost
