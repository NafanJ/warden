"""Owner tasks: `@warden <task>` runs the gated agent live (run_task), and
`@warden !full <task>` queues a full Claude Code session behind an approval.
"""
import asyncio
import time

import warden.agent.runner as runner
from warden.agent import coder
from warden.agent.runner import ApprovalRelay, run_task
from warden.backends.replay import ReplayBackend
from warden.webhook.approvals import handle_reply


# --- run_task: the gated, live-mode agent path ---

def test_approval_relay_routes_only_approvals_to_owner(config, channel):
    relay = ApprovalRelay(config, channel)
    relay.send("internal incident chatter")
    relay.send_approval(7, "approve this?")
    # the owner's channel saw the approval prompt, not the chatter
    assert channel.sent == ["approve this?"]


def test_run_task_runs_in_configured_mode_and_posts_report(
        config, store, channel, monkeypatch, tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# header\n\n## Actions taken\nrestarted sonarr, logs clean\n")
    seen = {}

    async def fake_handle(incident_id, cfg, backend, st, ch):
        seen["mode"], seen["channel"], seen["incident_id"] = cfg.mode, ch, incident_id
        return {"report_path": str(report), "cost_usd": 0.02}

    monkeypatch.setattr(runner, "handle_incident", fake_handle)
    asyncio.run(run_task("restart sonarr", config, ReplayBackend({}), store, channel))

    assert seen["mode"] == "active"  # NOT forced to dry-run the way diagnose is
    assert isinstance(seen["channel"], ApprovalRelay)  # approvals can still reach the owner
    assert store.get_incident(seen["incident_id"])["category"] == "task"
    out = channel.sent[-1]
    assert "restart sonarr" in out and "restarted sonarr, logs clean" in out
    assert "$0.02" in out


# --- !full: queue -> approve -> full Claude Code session ---

def test_full_task_disabled_by_default(config, store, channel):
    msg = coder.queue_full_task("apt upgrade", config, store, channel)
    assert "disabled" in msg
    assert store.pending_actions() == []


def test_full_task_queues_tier2_approval(config, store, channel):
    config.enable_full_tasks = True
    assert coder.queue_full_task("clean up /var/log", config, store, channel) is None
    (action,) = store.pending_actions()
    assert action["tool"] == coder.FULL_TASK_TOOL
    assert action["tier"] == 2
    assert "clean up /var/log" in action["description"]
    assert any("needs approval" in s for s in channel.sent)


def test_full_task_duplicate_pending_is_not_requeued(config, store, channel):
    config.enable_full_tasks = True
    coder.queue_full_task("rotate the logs", config, store, channel)
    msg = coder.queue_full_task("rotate the logs", config, store, channel)
    assert "already pending" in msg
    assert len(store.pending_actions()) == 1


def test_approved_full_task_runs_and_posts_redacted_result(
        config, store, channel, monkeypatch):
    config.enable_full_tasks = True
    coder.queue_full_task("rotate the logs", config, store, channel)
    (action,) = store.pending_actions()

    async def fake_session(task, cfg):
        return f"rotated 3 logs for '{task}'; used key {cfg.sonarr_api_key}", 0.42

    monkeypatch.setattr(coder, "_run_claude_code", fake_session)
    reply = handle_reply(f"YES {action['id']}", config, ReplayBackend({}), store, channel)
    assert "started" in reply

    for _ in range(100):  # the session runs in a daemon thread; wait for it
        if store.get_action(action["id"])["status"] != "approved":
            break
        time.sleep(0.05)
    row = store.get_action(action["id"])
    assert row["status"] == "executed"
    final = channel.sent[-1]
    assert "rotated 3 logs" in final and "$0.42" in final
    # the secret never reaches Discord or the store
    assert "sonarr-secret-key-123" not in final
    assert "sonarr-secret-key-123" not in row["result"]


def test_failed_full_task_is_marked_failed(config, store, channel, monkeypatch):
    config.enable_full_tasks = True
    coder.queue_full_task("break things", config, store, channel)
    (action,) = store.pending_actions()
    store.decide_action(action["id"], approved=True)

    async def boom(task, cfg):
        raise RuntimeError("session exploded")

    monkeypatch.setattr(coder, "_run_claude_code", boom)
    coder._run_and_post(action["id"], "break things", config, store, channel)
    assert store.get_action(action["id"])["status"] == "failed"
    assert any("FAILED" in s for s in channel.sent)


def test_full_task_env_parsing(monkeypatch):
    import warden.config as cfgmod
    monkeypatch.setattr(cfgmod, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ENABLE_FULL_TASKS", "true")
    monkeypatch.setenv("FULL_TASK_BUDGET_USD", "5")
    cfg = cfgmod.load_config()
    assert cfg.enable_full_tasks is True
    assert cfg.full_task_budget_usd == 5.0
