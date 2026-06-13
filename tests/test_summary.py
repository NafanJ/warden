"""Daily summary: the deterministic gather + format (no LLM, no network)."""
from warden.backends.replay import ReplayBackend
from warden.summary import format_summary, gather, is_notable

SNAP = {
    "docker_ps": [{"name": "plex", "state": "running", "status": "Up"},
                  {"name": "sonarr", "state": "running", "status": "Up"}],
    "disk_usage": [{"path": "/", "used_pct": 10.0, "free_gb": 400, "total_gb": 500, "used_gb": 50},
                   {"path": "/mnt/Modi", "used_pct": 93.8, "free_gb": 200, "total_gb": 4000, "used_gb": 3800}],
    "torrents": [],
}


def test_quiet_day_is_not_notable(config, store):
    g = gather(config, ReplayBackend(SNAP), store)
    assert g["containers_up"] == 2 and g["containers_total"] == 2
    assert g["incidents"] == [] and not is_notable(g)
    text = format_summary(g)
    assert "warden daily" in text
    assert "2/2 up" in text and "93.8% ⚠️" in text and "$0.00" in text


def test_notable_day_with_cost_and_needs_you(config, store):
    iid = store.open_incident("disk_pressure:/mnt/Modi", "disk_pressure", "/mnt/Modi at 93.8%")
    store.set_incident_cost(iid, 0.01)
    store.close_incident(iid, "escalated")  # unresolved
    g = gather(config, ReplayBackend(SNAP), store)
    assert is_notable(g)                       # an incident happened today
    assert g["cost"] == 0.01
    text = format_summary(g)
    assert "Needs you" in text and "/mnt/Modi at 93.8%" in text


def test_resolved_incident_not_in_needs_you(config, store):
    iid = store.open_incident("container_down:plex", "container_down", "plex down")
    store.close_incident(iid, "resolved")
    g = gather(config, ReplayBackend(SNAP), store)
    assert is_notable(g)                        # it still counts as today's activity
    assert g["unresolved"] == []                # ...but resolved, so not "needs you"
    assert "Needs you" not in format_summary(g)
