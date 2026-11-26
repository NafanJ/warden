"""Daily summary: the deterministic gather + format (no LLM, no network)."""
from warden.backends.replay import ReplayBackend
from warden.summary import build_embed, format_status, format_summary, gather, is_notable

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
    assert "Warden daily" in text
    assert "All 2 containers are running" in text and "93.8%" in text and "$0.00" not in text


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
    assert "Needs attention" in text and "/mnt/Modi at 93.8%" in text


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
    assert "3 automatic actions authorised" in text
    assert "outcomes not recorded" in text
    assert "restarted plex" not in text


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


def test_one_shot_job_excluded_from_totals(config, store):
    # the finished migration job must not inflate the denominator: 22/23 → 22/22
    snap = {**SNAP, "docker_ps": [
        {"name": "plex", "state": "running", "status": "Up"},
        {"name": "affine_migration_job", "state": "exited",
         "status": "Exited (0) 2 hours ago", "restart_policy": "no"}]}
    g = gather(config, ReplayBackend(snap), store)
    assert g["containers_up"] == 1 and g["containers_total"] == 1
    assert "All 1 containers are running" in format_summary(g)


def test_embed_quiet_day_is_green_and_compact(config, store):
    g = gather(config, ReplayBackend({**SNAP, "disk_usage": [
        {"path": "/", "used_pct": 10.0, "free_gb": 400, "total_gb": 500, "used_gb": 50}]}), store)
    e = build_embed(g)
    assert e["color"] == 0x3E8E5F                       # green strip
    assert "Warden daily" in e["title"]
    assert "No current alerts or approvals waiting" in e["description"]
    names = [f["name"] for f in e["fields"]]
    assert names == ["Storage"]
    assert "All 2 containers are running" in e["description"]
    assert "Needs you" not in str(names)


def test_embed_down_container_is_red_and_named(config, store):
    snap = {**SNAP, "docker_ps": [{"name": "plex", "state": "exited", "status": "Exited (1)"}]}
    g = gather(config, ReplayBackend(snap), store)
    e = build_embed(g)
    assert e["color"] == 0xB3452E                       # red strip
    assert "plex is exited" in e["fields"][0]["value"]


def test_embed_names_stalled_but_not_seeding(config, store):
    GB = 1024 ** 3
    snap = {**SNAP, "disk_usage": [
        {"path": "/", "used_pct": 10.0, "free_gb": 400, "total_gb": 500, "used_gb": 50}],
        "torrents": [
        {"name": "Seeding.Show.S01", "hashString": "a", "percentDone": 1.0, "status": 6, "totalSize": 6 * GB},
        {"name": "Stuck.Release", "hashString": "d", "percentDone": 0.1, "status": 4, "totalSize": 3 * GB,
         "age_hours": 10, "inactive_hours": 7, "peersConnected": 0},
    ]}
    g = gather(config, ReplayBackend(snap), store)
    e = build_embed(g)
    dl = next(f["value"] for f in e["fields"] if f["name"] == "Needs attention")
    assert "Stuck.Release" in dl and "7h" in dl   # stalled: named
    assert "Seeding.Show.S01" not in dl                  # seeding: count only
    assert e["color"] == 0xC9A227                        # amber: stall needs a look


def test_stale_incidents_do_not_override_live_health(config, store):
    config.disk_threshold_pct = 97
    iid = store.open_incident('container_down:immich-server', 'container_down', 'OLD crash loop')
    store.conn.execute("UPDATE incidents SET opened_at='2026-01-01T00:00:00+00:00' WHERE id=?", (iid,))
    store.conn.commit()
    g = gather(config, ReplayBackend(SNAP), store)
    assert g['unresolved']  # historical record is retained
    e = build_embed(g, note='OLD misleading narrative')
    assert 'OLD' not in str(e)
    assert e['color'] == 0x3E8E5F
    assert 'No current alerts' in e['description']
    assert '200 GB free' in str(e)


def test_unhealthy_running_container_is_not_all_clear(config, store):
    snap = {**SNAP, 'docker_ps': [{'name':'plex','state':'running','status':'Up (unhealthy)'}]}
    e = build_embed(gather(config, ReplayBackend(snap), store))
    assert 'current alert' in e['description']
    assert 'No current alerts' not in e['description']
    assert 'unhealthy' in str(e) and e['color'] == 0xB3452E


def test_collection_failure_never_claims_healthy(config, store):
    g = gather(config, ReplayBackend(SNAP), store)
    g.update(collector_errors={'docker_ps':'secret internal error'}, containers_total=0, containers_up=0,
             anomalies=[], disk=[], unresolved=[])
    e = build_embed(g)
    assert 'could not be confirmed' in e['description']
    assert 'No current alerts' not in e['description']
    assert 'secret internal error' not in str(e)
    assert e['color'] == 0xC9A227


def test_pending_approvals_include_only_unexpired(config, store):
    fresh = store.queue_action(None, 'container_restart', {'name':'plex'}, 2, 'Restart Plex')
    old = store.queue_action(None, 'container_restart', {'name':'sonarr'}, 2, 'OLD approval')
    store.conn.execute("UPDATE actions SET created_at='2026-01-01T00:00:00+00:00' WHERE id=?", (old,))
    store.conn.commit()
    e = build_embed(gather(config, ReplayBackend(SNAP), store))
    assert f'Approval #{fresh}' in str(e)
    assert 'OLD approval' not in str(e)
    assert store.conn.execute('SELECT status FROM actions WHERE id=?',(old,)).fetchone()[0]=='pending'


def test_records_distinguish_success_failure_and_permission(config, store):
    for status,description in [('executed','Completed cleanup'),('failed','Unsuccessful restart'),('approved','Awaiting execution')]:
        aid = store.queue_action(None,'container_restart',{'name':'plex'},2,description)
        store.conn.execute('UPDATE actions SET status=? WHERE id=?',(status,aid))
    store.conn.commit()
    store.audit('container_restart',{'name':'plex'},1,'allowed')
    text = format_summary(gather(config,ReplayBackend(SNAP),store))
    assert 'Recorded complete:' in text and 'Failed:' in text
    assert 'Approved; completion not recorded:' in text
    assert '1 automatic action authorised; outcomes not recorded' in text
    assert 'auto-fix' not in text and 'Fixed itself' not in text


def test_seeding_and_zero_spend_do_not_clutter_daily(config, store):
    snap={**SNAP,'torrents':[{'name':'Routine seeding','percentDone':1.0,'status':6,'totalSize':1024}]}
    text=format_summary(gather(config,ReplayBackend(snap),store))
    assert 'Routine seeding' not in text and 'Downloads' not in text and '$0.00' not in text


def test_discord_limits_and_plaintext_share_facts(config,store):
    g=gather(config,ReplayBackend(SNAP),store)
    g['anomalies']=[{'category':'stalled_download','summary':f'{i} '+('x'*500)} for i in range(40)]
    embed=build_embed(g)
    assert all(len(f['value'])<=1024 for f in embed['fields'])
    assert sum(len(f['name'])+len(f['value']) for f in embed['fields'])+len(embed['description'])+len(embed['title'])+len(embed['footer']['text'])<6000
    assert 'more' in embed['fields'][0]['value']
    text=format_summary(g)
    for f in embed['fields']:assert f['value'] in text
