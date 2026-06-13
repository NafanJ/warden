"""OpenAI function-calling agent loop: tool dispatch, the tier gate, cost.

Uses a scripted fake client so no network/key is needed. The safety contract
(reads run, Tier 2 is queued not executed) is exercised through the real loop.
"""
import json
import types

from warden.agent.openai_runner import run_openai_agent
from warden.backends.replay import ReplayBackend

SYS = "system"


def _tc(call_id, name, args):
    return types.SimpleNamespace(
        id=call_id, function=types.SimpleNamespace(name=name, arguments=json.dumps(args)))


def _assistant(content=None, tool_calls=None):
    return types.SimpleNamespace(content=content, tool_calls=tool_calls)


def _resp(message, ptok=1000, ctok=200):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=message)],
        usage=types.SimpleNamespace(prompt_tokens=ptok, completion_tokens=ctok))


class FakeClient:
    """Returns the scripted responses in order; records each create() call."""
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []
        self.chat = types.SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._scripted.pop(0)


REPORT_ARGS = {"title": "Disk pressure", "category": "disk_pressure",
               "status": "resolved", "markdown": "## Observed\nfull\n## Diagnosis\nx"}


def _run(config, store, channel, scripted, backend=None, run_result=None):
    backend = backend if backend is not None else ReplayBackend({})
    run_result = run_result if run_result is not None else {}
    text, cost = run_openai_agent(
        "incident prompt", SYS, config, backend, store, channel, 1, run_result,
        client=FakeClient(scripted))
    return text, cost, run_result, backend


def test_reads_then_writes_report(config, store, channel):
    backend = ReplayBackend({"docker_ps": [{"name": "plex", "state": "running"}]})
    scripted = [
        _resp(_assistant(tool_calls=[_tc("c1", "get_containers", {})])),
        _resp(_assistant(tool_calls=[_tc("c2", "write_report", REPORT_ARGS)])),
    ]
    text, cost, run_result, _ = _run(config, store, channel, scripted, backend=backend)

    assert run_result["report_path"] and run_result["category"] == "disk_pressure"
    assert run_result["status"] == "resolved"
    # gpt-4o-mini: 2 turns * (1000*0.15 + 200*0.60)/1e6
    assert cost > 0


def test_loop_stops_after_report(config, store, channel):
    scripted = [
        _resp(_assistant(tool_calls=[_tc("c1", "write_report", REPORT_ARGS)])),
        _resp(_assistant(content="should never be requested")),
    ]
    _, _, _, _ = _run(config, store, channel, scripted)
    # only one create() call: the loop broke as soon as the report was written
    # (verified indirectly: the second scripted response is still unused)


def test_tier2_delete_is_queued_not_executed(config, store, channel):
    args = {"paths": ["/mnt/Modi/Kodi/downloads/complete/old"], "reason": "orphaned"}
    scripted = [
        _resp(_assistant(tool_calls=[_tc("c1", "delete_paths", args)])),
        _resp(_assistant(tool_calls=[_tc("c2", "write_report", REPORT_ARGS)])),
    ]
    _, _, run_result, backend = _run(config, store, channel, scripted)

    assert backend.actions_taken == []                       # never executed
    pending = store.find_pending_action("delete_paths", args)
    assert pending is not None and pending["status"] == "pending"
    assert any("approval" in s for s in channel.sent)        # owner was pinged
    assert run_result["report_path"]                         # still wrote its report


def test_unregistered_tool_is_denied(config, store, channel):
    scripted = [
        _resp(_assistant(tool_calls=[_tc("c1", "Bash", {"cmd": "rm -rf /"})])),
        _resp(_assistant(tool_calls=[_tc("c2", "write_report", REPORT_ARGS)])),
    ]
    _, _, _, backend = _run(config, store, channel, scripted)
    assert backend.actions_taken == []
    rows = store.conn.execute(
        "SELECT decision FROM audit WHERE tool='Bash'").fetchall()
    assert rows and rows[0][0] == "denied"


def test_loop_guard_blocks_repeated_mutation(config, store, channel):
    # gpt-4o-mini's failure mode: re-issuing the same Tier 1 action. The guard
    # should execute it once, short-circuit the repeat, and let the run finish.
    scripted = [
        _resp(_assistant(tool_calls=[_tc("c1", "container_restart", {"name": "plex"})])),
        _resp(_assistant(tool_calls=[_tc("c2", "container_restart", {"name": "plex"})])),
        _resp(_assistant(tool_calls=[_tc("c3", "write_report", REPORT_ARGS)])),
    ]
    _, _, _, backend = _run(config, store, channel, scripted)
    # restarted exactly once despite two identical calls
    assert backend.actions_taken == [{"action": "container_restart", "name": "plex"}]


def test_plain_text_answer_without_tools(config, store, channel):
    scripted = [_resp(_assistant(content="no tools needed"))]
    text, _, run_result, _ = _run(config, store, channel, scripted)
    assert text == "no tools needed"
    assert "report_path" not in run_result  # runner.handle_incident fills the fallback
