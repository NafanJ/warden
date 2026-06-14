"""Active-mode incident handling attaches action buttons to the agent's outcome
message — but only when it escalated or is monitoring, never on an auto-fix.

The agent run itself is stubbed; we're only checking what the owner gets pinged
with at the end of handle_incident.
"""
import json

from warden.agent import runner
from warden.backends.replay import ReplayBackend


class CapChannel:
    """Records (text, components) for each send so we can inspect the buttons."""
    def __init__(self):
        self.calls: list[tuple] = []

    def send(self, text, components=None):
        self.calls.append((text, components))

    def send_approval(self, action_id, text):
        self.send(text)
        return None


def _write_incident(config, iid, category, summary, details):
    d = config.state_dir / "incidents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{iid}.json").write_text(json.dumps(
        {"category": category, "summary": summary, "details": details, "snapshot": {}}))


def _stub_agent(monkeypatch, status):
    async def fake(prompt, config, backend, store, channel, incident_id, run_result):
        run_result.update({"status": status, "title": "result",
                           "category": "container_down", "report_path": "/tmp/r.md"})
        return ("done", 0.0)
    monkeypatch.setattr(runner, "_run_claude_agent", fake)


def _ids(components):
    return [b["custom_id"] for row in components for b in row["components"]]


async def test_escalated_incident_gets_action_buttons(config, store, monkeypatch):
    config.llm_provider = "claude"
    iid = store.open_incident("container_down:plex", "container_down", "Container plex is exited")
    _write_incident(config, iid, "container_down", "Container plex is exited", {"container": "plex"})
    _stub_agent(monkeypatch, "escalated")

    ch = CapChannel()
    await runner.handle_incident(iid, config, ReplayBackend({}), store, ch)

    text, components = ch.calls[-1]
    assert "[escalated]" in text
    assert _ids(components) == ["restart:plex", f"diag:{iid}", f"dismiss:{iid}"]


async def test_resolved_incident_has_no_buttons(config, store, monkeypatch):
    config.llm_provider = "claude"
    iid = store.open_incident("container_down:plex", "container_down", "plex exited")
    _write_incident(config, iid, "container_down", "plex exited", {"container": "plex"})
    _stub_agent(monkeypatch, "resolved")

    ch = CapChannel()
    await runner.handle_incident(iid, config, ReplayBackend({}), store, ch)

    assert ch.calls[-1][1] is None  # warden fixed it — nothing to offer
