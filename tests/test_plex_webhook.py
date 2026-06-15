"""Tests for the Plex playback webhook → download-throttle reconcile.

Drives the FastAPI /plex endpoint with realistic multipart payloads and a
ReplayBackend whose tautulli_activity reflects how many streams are live, then
asserts warden toggles Transmission's alt-speed and records presence. Also
covers the standalone reconcile() the sentinel calls.
"""
import json

import pytest
from fastapi.testclient import TestClient

import warden.webhook.app as app_module
from warden.backends.replay import ReplayBackend
from warden.presence import read_presence
from warden.webhook.plex import reconcile

TOKEN = "plex-secret"


def _backend(stream_count: int, throttled: bool = False) -> ReplayBackend:
    return ReplayBackend({
        "tautulli_activity": {
            "stream_count": stream_count,
            "transcode_count": 1 if stream_count else 0,
            "bandwidth_mbps": 8.0 if stream_count else 0.0,
            "sessions": [{"user": "Benn", "title": "Supernatural", "state": "playing",
                          "transcode": True}] * stream_count,
        },
        "downloads_throttled": throttled,
    })


@pytest.fixture
def client(config, store, channel, monkeypatch):
    config.plex_webhook_token = ""
    config.plex_throttle_downloads = True
    monkeypatch.setattr(app_module, "config", config)
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "channel", channel)

    def use(backend):
        monkeypatch.setattr(app_module, "backend", backend)
        return TestClient(app_module.app)

    return use, config


def _post(client, event: str, token: str | None = None):
    url = "/plex" + (f"?token={token}" if token else "")
    return client.post(url, data={"payload": json.dumps({"event": event})})


# --- throttle on/off via the HTTP endpoint ---

def test_play_throttles_downloads(client):
    use, config = client
    backend = _backend(stream_count=1, throttled=False)
    r = _post(use(backend), "media.play")
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "throttled" and body["downloads_throttled"] is True
    assert backend.actions_taken == [{"action": "set_download_throttle", "on": True}]
    assert read_presence(config)["downloads_throttled"] is True


def test_stop_restores_full_speed(client):
    use, config = client
    backend = _backend(stream_count=0, throttled=True)
    r = _post(use(backend), "media.stop")
    assert r.status_code == 200
    assert r.json()["action"] == "unthrottled"
    assert backend.actions_taken == [{"action": "set_download_throttle", "on": False}]
    assert read_presence(config)["downloads_throttled"] is False


def test_idempotent_when_already_in_desired_state(client):
    use, _ = client
    backend = _backend(stream_count=1, throttled=True)  # already throttled
    r = _post(use(backend), "media.resume")
    assert r.status_code == 200
    assert r.json()["action"] == "none"
    assert backend.actions_taken == []  # no redundant RPC


def test_non_playback_event_ignored(client):
    use, _ = client
    backend = _backend(stream_count=1, throttled=False)
    r = _post(use(backend), "library.new")
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"
    assert backend.actions_taken == []


def test_throttle_disabled_records_presence_only(client):
    use, config = client
    config.plex_throttle_downloads = False
    backend = _backend(stream_count=2, throttled=False)
    r = _post(use(backend), "media.play")
    assert r.status_code == 200
    assert backend.actions_taken == []          # never touches Transmission
    assert read_presence(config)["stream_count"] == 2  # but still tracks presence


# --- token guard ---

def test_token_required_when_configured(client):
    use, config = client
    config.plex_webhook_token = TOKEN
    backend = _backend(stream_count=1)
    assert _post(use(backend), "media.play").status_code == 403          # missing
    assert _post(use(backend), "media.play", token="wrong").status_code == 403
    assert _post(use(backend), "media.play", token=TOKEN).status_code == 200


# --- the reconcile() the sentinel calls directly ---

def test_reconcile_corrects_stuck_throttle(config):
    # No stream live, but a dropped "stop" left Transmission throttled — the
    # sentinel reconcile must flip it back.
    config.plex_throttle_downloads = True
    backend = _backend(stream_count=0, throttled=True)
    out = reconcile(config, backend)
    assert out["action"] == "unthrottled"
    assert backend.download_throttled() is False
