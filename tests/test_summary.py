"""Daily summary: the deterministic gather + format (no LLM, no network)."""
from warden.backends.replay import ReplayBackend
from warden.summary import format_status, format_summary, gather, is_notable

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


def test_disk_glyph_follows_configured_threshold(config, store):
    config.disk_threshold_pct = 97          # raise the bar
    g = gather(config, ReplayBackend(SNAP), store)
    text = format_summary(g)
    assert "93.8%" in text and "93.8% ⚠️" not in text  # under 97 → no warning glyph


def test_notable_day_with_cost_and_needs_you(config, store):
    iid = store.open_incident("disk_pressure:/mnt/Modi", "disk_pressure", "/mnt/Modi at 93.8%")
    store.set_incident_cost(iid, 0.01)
    store.close_incident(iid, "escalated")  # unresolved
    g = gather(config, ReplayBackend(SNAP), store)
    assert is_notable(g)                       # an incident happened today
    assert g["cost"] == 0.01
    text = format_summary(g)
    assert "Needs you" in text and "/mnt/Modi at 93.8%" in text


def test_status_shows_active_issues_not_rollup(config, store):
    # a disk over threshold + a down container should appear as live issues
    snap = {**SNAP, "docker_ps": [{"name": "plex", "state": "exited", "status": "Exited (1)"}]}
    g = gather(config, ReplayBackend(snap), store)
    text = format_status(g)
    assert "warden status" in text
    assert "down: plex" in text                         # live container state
    assert "Active issues" in text and "93.8%" in text  # what the sentinel flags now
    assert "Agent cost" not in text and "auto-fix" not in text  # no 24h rollup


def test_status_shows_plex_activity_when_configured(config, store):
    config.tautulli_api_key = "set"
    snap = {**SNAP, "tautulli_activity": {
        "stream_count": 2, "transcode_count": 1, "bandwidth_mbps": 14.0,
        "sessions": [{"user": "Tom", "title": "Dune", "state": "playing", "transcode": True}]}}
    g = gather(config, ReplayBackend(snap), store)
    text = format_status(g)
    assert "Plex:" in text and "2 stream" in text and "Tom: Dune" in text


def test_status_omits_plex_when_unconfigured(config, store):
    g = gather(config, ReplayBackend(SNAP), store)  # no tautulli_api_key
    assert "Plex:" not in format_status(g)


def test_status_all_clear_when_healthy(config, store):
    snap = {"docker_ps": [{"name": "plex", "state": "running", "status": "Up"}],
            "disk_usage": [{"path": "/", "used_pct": 10.0, "free_gb": 400, "total_gb": 500, "used_gb": 50}],
            "torrents": []}
    g = gather(config, ReplayBackend(snap), store)
    assert "all clear ✅" in format_status(g)


def test_completed_one_shot_job_not_reported_down(config, store):
    # a migration job that exited 0 with restart policy 'no' is finished, not down
    snap = {**SNAP, "docker_ps": [
        {"name": "plex", "state": "running", "status": "Up"},
        {"name": "affine_migration_job", "state": "exited",
         "status": "Exited (0) 2 hours ago", "restart_policy": "no"}]}
    g = gather(config, ReplayBackend(snap), store)
    text = format_status(g)
    assert "affine_migration_job" not in text     # finished job, not flagged down
    assert "down:" not in text


def test_downloads_broken_out_by_state_with_sizes(config, store):
    GB = 1024 ** 3
    snap = {**SNAP, "torrents": [
        {"name": "Seeding.Show.S01", "hashString": "a", "percentDone": 1.0, "status": 6, "totalSize": 6 * GB},
        {"name": "Downloading.Movie", "hashString": "b", "percentDone": 0.45, "status": 4, "totalSize": 4 * GB,
         "age_hours": 2, "inactive_hours": 0.1, "peersConnected": 5},
        {"name": "Queued.Thing", "hashString": "c", "percentDone": 0.0, "status": 3, "totalSize": 2 * GB},
        {"name": "Stuck.Release", "hashString": "d", "percentDone": 0.1, "status": 4, "totalSize": 3 * GB,
         "age_hours": 10, "inactive_hours": 7, "peersConnected": 0},
    ]}
    g = gather(config, ReplayBackend(snap), store)
    text = format_status(g)
    assert "Downloads:   4 torrent(s)" in text
    assert "🌱 seeding (1 · 6.0 GB)" in text and "Seeding.Show.S01" in text
    assert "⬇️ downloading (1 · 4.0 GB)" in text and "Downloading.Movie — 4.0 GB · 45%" in text
    assert "⏳ todo (1 · 2.0 GB)" in text
    assert "⚠️ stalled (1 · 3.0 GB)" in text and "7h idle" in text


def test_summary_lists_auto_fixes(config, store):
    store.audit("container_restart", {"name": "plex"}, 1, "allowed")
    store.audit("remove_torrents", {"ids": [1], "delete_data": False}, 1, "allowed")
    store.audit("remove_torrents", {"ids": [2], "delete_data": False}, 1, "allowed")
    g = gather(config, ReplayBackend(SNAP), store)
    text = format_summary(g)
    assert "Auto-fixes:" in text
    assert "restarted plex" in text
    assert "cleared a completed torrent ×2" in text   # identical fixes collapse


def test_summary_omits_auto_fixes_when_none(config, store):
    g = gather(config, ReplayBackend(SNAP), store)
    assert "Auto-fixes:" not in format_summary(g)


def test_resolved_incident_not_in_needs_you(config, store):
    iid = store.open_incident("container_down:plex", "container_down", "plex down")
    store.close_incident(iid, "resolved")
    g = gather(config, ReplayBackend(SNAP), store)
    assert is_notable(g)                        # it still counts as today's activity
    assert g["unresolved"] == []                # ...but resolved, so not "needs you"
    assert "Needs you" not in format_summary(g)
