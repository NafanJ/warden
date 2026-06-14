"""Incident reports must live under state_dir so a deploy (rsync --delete, which
excludes state/) doesn't wipe them — a sibling incidents/ dir got nuked on every
update, losing every post-mortem."""
from pathlib import Path

import warden.config as cfgmod
from warden.config import Config, load_config


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
