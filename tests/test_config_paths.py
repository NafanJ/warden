"""Incident reports must live under state_dir so a deploy (rsync --delete, which
excludes state/) doesn't wipe them — a sibling incidents/ dir got nuked on every
update, losing every post-mortem."""
from pathlib import Path

import warden.config as cfgmod
from warden.config import Config, _kv_hours, load_config


def test_dataclass_default_incidents_dir_under_state():
    assert Config().incidents_dir == Path("state/incidents")


def test_incidents_dir_nests_under_state_dir_by_default(monkeypatch):
    monkeypatch.setattr(cfgmod, "load_dotenv", lambda *a, **k: None)  # ignore the real .env
    monkeypatch.setenv("WARDEN_STATE_DIR", "/srv/warden/state")
    monkeypatch.delenv("WARDEN_INCIDENTS_DIR", raising=False)
    assert load_config().incidents_dir == Path("/srv/warden/state/incidents")


def test_incidents_dir_still_overridable(monkeypatch):
    monkeypatch.setattr(cfgmod, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("WARDEN_INCIDENTS_DIR", "/var/reports")
    assert load_config().incidents_dir == Path("/var/reports")


def test_kv_hours_splits_on_last_equals_and_drops_junk():
    # anomaly keys carry a ':' so only the final '=' separates the hours; bad
    # entries (no '=', non-numeric) are dropped rather than raising.
    parsed = _kv_hours("container_down:immich-server=168, nope, bad:k=abc, d:/m=48")
    assert parsed == {"container_down:immich-server": 168.0, "d:/m": 48.0}


def test_cooldown_override_takes_precedence_over_global(monkeypatch):
    monkeypatch.setattr(cfgmod, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("INCIDENT_COOLDOWN_HOURS", "6")
    monkeypatch.setenv("INCIDENT_COOLDOWN_OVERRIDES", "container_down:immich-server=168")
    cfg = load_config()
    # the per-key window wins for the named key; everything else falls back to global
    assert cfg.incident_cooldown_overrides.get("container_down:immich-server") == 168.0
    assert cfg.incident_cooldown_overrides.get("container_down:plex", cfg.incident_cooldown_hours) == 6.0
