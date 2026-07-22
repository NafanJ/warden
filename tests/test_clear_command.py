"""`/clear` — closing incidents by hand from Discord.

Covers the three shapes of the command (list, by id, all) plus the store method
underneath, whose contract is "returns the row only if it was actually open".
"""
from warden.notifier.discord_commands import ALIASES, handle_clear


def _open(store, key, summary):
    return store.open_incident(key, "container_down", summary)


def test_bare_clear_lists_open_incidents_with_ids(store):
    a = _open(store, "container:sonarr", "sonarr is down")
    b = _open(store, "disk:/data", "/data at 95%")
    out = handle_clear("", store)
    assert f"#{a} sonarr is down" in out
    assert f"#{b} /data at 95%" in out
    assert "/clear incident:all" in out


def test_bare_clear_with_nothing_open_says_all_clear(store):
    assert "all clear" in handle_clear("", store)


def test_clear_by_id_closes_just_that_incident(store):
    a = _open(store, "container:sonarr", "sonarr is down")
    b = _open(store, "disk:/data", "/data at 95%")
    out = handle_clear(str(a), store)
    assert f"#{a}" in out
    assert store.get_incident(a)["status"] == "resolved"
    assert store.get_incident(b)["status"] == "open"


def test_clear_accepts_hash_prefix_and_several_ids(store):
    a = _open(store, "container:sonarr", "sonarr is down")
    b = _open(store, "disk:/data", "/data at 95%")
    handle_clear(f"#{a} {b}", store)
    assert store.get_incident(a)["status"] == "resolved"
    assert store.get_incident(b)["status"] == "resolved"


def test_clear_reports_ids_that_were_not_open(store):
    a = _open(store, "container:sonarr", "sonarr is down")
    store.close_incident(a, "resolved")
    out = handle_clear(f"{a} 999", store)
    assert f"#{a}" in out and "#999" in out
    assert "Not open" in out


def test_clear_all_closes_every_open_and_escalated_incident(store):
    a = _open(store, "container:sonarr", "sonarr is down")
    b = _open(store, "disk:/data", "/data at 95%")
    store.close_incident(b, "escalated")          # escalated still needs the owner
    c = _open(store, "plex:down", "plex unreachable")
    store.close_incident(c, "resolved")           # already handled — untouched
    out = handle_clear("all", store)
    assert "Cleared 2 incident(s)" in out
    assert store.get_incident(a)["status"] == "resolved"
    assert store.get_incident(b)["status"] == "resolved"
    assert store.get_incident(c)["closed_at"] is not None


def test_clear_rejects_a_non_numeric_argument(store):
    a = _open(store, "container:sonarr", "sonarr is down")
    out = handle_clear("sonarr", store)
    assert "Usage:" in out
    assert store.get_incident(a)["status"] == "open"


def test_clear_leaves_no_older_incident_stranded(store):
    """unresolved_incidents() collapses to the newest per key; clearing must not,
    or an older duplicate would stay open with no way to reach it."""
    old = _open(store, "container:sonarr", "sonarr is down")
    new = _open(store, "container:sonarr", "sonarr is down again")
    handle_clear("all", store)
    assert store.get_incident(old)["status"] == "resolved"
    assert store.get_incident(new)["status"] == "resolved"


def test_clear_incident_returns_none_when_already_closed(store):
    a = _open(store, "container:sonarr", "sonarr is down")
    assert store.clear_incident(a)["id"] == a
    assert store.clear_incident(a) is None
    assert store.clear_incident(4242) is None


def test_clear_is_reachable_on_the_typed_path(store):
    assert ALIASES["clear"] == "clear"
    assert ALIASES["!clear"] == "clear"
