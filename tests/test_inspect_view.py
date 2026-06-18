"""_inspect_view turns raw `docker inspect` JSON into the agent-facing view.

The case that matters: a container that crash-looped but has since recovered
reads `running` / exit 0 / healthy at inspect time. The view must still flag the
restart history so the agent doesn't call a recovered SIGSEGV loop a false alarm.
"""
from warden.backends.live import _inspect_view


def _raw(status, exit_code, restart_count, health="healthy", oom=False):
    return {
        "RestartCount": restart_count,
        "State": {
            "Status": status, "Running": status == "running",
            "ExitCode": exit_code, "OOMKilled": oom,
            "StartedAt": "2026-06-17T23:03:39Z", "FinishedAt": "2026-06-17T23:03:39Z",
            "Health": {"Status": health},
        },
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}, "Memory": 0},
    }


def test_recovered_crash_loop_is_flagged_despite_healthy_read():
    v = _inspect_view("immich-server", _raw("running", 0, 13))
    assert v["restart_count"] == 13
    assert v["exit_code"] == 0 and v["health"] == "healthy"
    assert v["note"] and "crash" in v["note"].lower()
    assert "13" in v["note"]


def test_active_restart_loop_surfaces_exit_code_in_note():
    v = _inspect_view("immich-server", _raw("restarting", 139, 13, health="starting"))
    assert v["note"] and "139" in v["note"] and "SIGSEGV" in v["note"]


def test_genuinely_healthy_container_has_no_note():
    v = _inspect_view("plex", _raw("running", 0, 0))
    assert v["note"] is None


def test_clean_stopped_container_has_no_crash_note():
    # exited cleanly, never restarted — nothing to warn about
    v = _inspect_view("job", _raw("exited", 0, 0, health=None))
    assert v["note"] is None
    assert v["status"] == "exited"
