"""Store behaviors that aren't covered via the higher-level tests."""


def test_recent_escalated_incident_suppresses_rerun(store):
    iid = store.open_incident("disk_pressure:/mnt/Modi", "disk_pressure", "full")
    store.close_incident(iid, "escalated")
    # within the window, an escalated (unresolved) incident suppresses a re-raise
    assert store.find_recent_unresolved("disk_pressure:/mnt/Modi", 6.0) is not None
    # ...but not once the window has passed
    assert store.find_recent_unresolved("disk_pressure:/mnt/Modi", 0.0) is None


def test_resolved_incident_does_not_suppress(store):
    iid = store.open_incident("container_down:plex", "container_down", "down")
    store.close_incident(iid, "resolved")
    # a resolved problem should re-raise if it recurs — no suppression
    assert store.find_recent_unresolved("container_down:plex", 6.0) is None


def test_open_incident_found_separately(store):
    store.open_incident("disk_pressure:/", "disk_pressure", "x")
    assert store.find_open_incident("disk_pressure:/") is not None
    assert store.find_open_incident("disk_pressure:/other") is None
