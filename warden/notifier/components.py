"""Discord message components (buttons) for warden's alerts, plus the handler
that runs a button click.

Every alert that needs a decision or invites a follow-up carries buttons:
approval prompts get Approve/Reject, incident alerts get a context-aware set
(Restart for a downed container, Diagnose, Dismiss). A click arrives as a
component interaction over the gateway; its custom_id is `<kind>:<payload>`,
which `handle_component` maps back to the same code paths as the typed commands
and YES/NO replies — so buttons can't drift from the rest of warden.

custom_id is capped at 100 chars by Discord; an incident id or container name
fits comfortably.
"""
from __future__ import annotations

import asyncio

from warden.backends import Backend
from warden.config import Config
from warden.notifier import Channel
from warden.store import Store

# decide_tool / handle_reply / run_diagnose are imported lazily inside the click
# handler: building buttons (used by the agent-free detect-mode sentinel) must not
# pull in the Claude SDK that tiers.py drags along.

# Discord button styles
_PRIMARY, _SECONDARY, _SUCCESS, _DANGER = 1, 2, 3, 4


def _button(label: str, custom_id: str, style: int = _SECONDARY) -> dict:
    return {"type": 2, "style": style, "label": label, "custom_id": custom_id}


def _row(*buttons: dict) -> list[dict]:
    return [{"type": 1, "components": list(buttons)}]


def approval_buttons(action_id: int) -> list[dict]:
    return _row(
        _button("✅ Approve", f"approve:{action_id}", _SUCCESS),
        _button("❌ Reject", f"reject:{action_id}", _DANGER),
    )


def incident_buttons(incident_id: int, category: str, container: str = "") -> list[dict]:
    """Context-aware actions for an incident alert: a one-tap Restart when a
    specific container is down, plus Diagnose and Dismiss on everything."""
    buttons = []
    if category == "container_down" and container:
        buttons.append(_button(f"♻️ Restart {container}"[:80],
                               f"restart:{container}", _PRIMARY))
    buttons.append(_button("🔍 Diagnose", f"diag:{incident_id}"))
    buttons.append(_button("🗙 Dismiss", f"dismiss:{incident_id}"))
    return _row(*buttons)


def handle_component(custom_id: str, config: Config, backend: Backend,
                     store: Store, channel: Channel) -> str:
    """Run a clicked button. Returns the text the message should be edited to.
    Never raises — failures come back as a message string."""
    from warden.webhook.approvals import handle_reply
    kind, _, payload = custom_id.partition(":")
    try:
        if kind == "approve":
            return f"✅ Approved\n{handle_reply(f'YES {payload}', config, backend, store, channel)}"
        if kind == "reject":
            return f"❌ Rejected\n{handle_reply(f'NO {payload}', config, backend, store, channel)}"
        if kind == "restart":
            return _restart(payload, config, backend, store, channel)
        if kind == "diag":
            return _diagnose(int(payload), config, backend, store, channel)
        if kind == "dismiss":
            store.close_incident(int(payload), "resolved")
            return f"🗙 Dismissed incident #{payload} — warden won't nag about it again."
    except Exception as exc:
        return f"warden: button action failed — {exc}"
    return f"warden: unknown action `{kind}`"


def _restart(name: str, config: Config, backend: Backend, store: Store,
             channel: Channel) -> str:
    # Route through the same tier gate as the agent, so it's audited and shows in
    # the daily summary's auto-fix list (and is denied in dry-run mode).
    from warden.agent.tiers import decide_tool
    allowed, msg = decide_tool(config, store, channel, None,
                               "container_restart", {"name": name}, backend)
    if not allowed:
        # denied = dry-run, or escalated to approval (e.g. restart would cut a stream)
        return f"♻️ Restart of {name} not done — {msg}"
    result = backend.container_restart(name)
    return f"♻️ Restarted {name}\n{result}"


def _diagnose(incident_id: int, config: Config, backend: Backend, store: Store,
              channel: Channel) -> str:
    from warden.agent.runner import run_diagnose
    row = store.conn.execute(
        "SELECT summary FROM incidents WHERE id=?", (incident_id,)).fetchone()
    question = (row["summary"] if row else "") or f"incident {incident_id}"
    channel.send(f"🔍 investigating: _{question}_ … (~30s)")
    asyncio.run(run_diagnose(question, config, backend, store, channel))
    return f"🔍 Diagnosed #{incident_id} — findings posted in the channel."
