"""End-to-end tests for the FastAPI WhatsApp webhook.

Covers the security boundary that unit tests of handle_reply don't reach:
the Meta verification handshake, X-Hub-Signature-256 validation, owner
allow-listing, and a realistic inbound payload driving the full
reply -> approve -> execute loop. Uses a ReplayBackend so no real files
are touched.
"""
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import warden.webhook.app as app_module
from warden.backends.replay import ReplayBackend

OWNER = "15551234567"
APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"


@pytest.fixture
def webhook(config, store, channel, monkeypatch):
    # The webhook reads these module globals at request time, so injecting
    # test doubles is enough — no need to rebuild the app.
    config.wa_app_secret = APP_SECRET
    config.wa_verify_token = VERIFY_TOKEN
    config.wa_to = "+" + OWNER
    backend = ReplayBackend({})
    monkeypatch.setattr(app_module, "config", config)
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "backend", backend)
    monkeypatch.setattr(app_module, "channel", channel)
    return TestClient(app_module.app), store, backend, channel


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payload(text: str, sender: str = OWNER, msg_type: str = "text") -> dict:
    message = {"from": sender, "type": msg_type}
    if msg_type == "text":
        message["text"] = {"body": text}
    return {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}


def _post(client, payload: dict, secret: str = APP_SECRET):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhook",
        content=body,  # exact bytes we signed; the handler re-reads them raw
        headers={"X-Hub-Signature-256": _sign(body, secret),
                 "Content-Type": "application/json"},
    )


def _queue_delete(store) -> int:
    return store.queue_action(
        1, "delete_paths",
        {"paths": ["/mnt/Modi/Kodi/downloads/complete/old"], "reason": "orphaned"},
        2, "Delete 1 path(s)",
    )


# --- verification handshake (GET) ---

def test_verify_handshake_echoes_challenge(webhook):
    client, *_ = webhook
    r = client.get("/webhook", params={"hub.mode": "subscribe",
                                        "hub.verify_token": VERIFY_TOKEN,
                                        "hub.challenge": "challenge-42"})
    assert r.status_code == 200
    assert r.text == "challenge-42"


def test_verify_handshake_rejects_wrong_token(webhook):
    client, *_ = webhook
    r = client.get("/webhook", params={"hub.mode": "subscribe",
                                        "hub.verify_token": "wrong",
                                        "hub.challenge": "x"})
    assert r.status_code == 403


# --- inbound replies (POST) ---

def test_owner_yes_approves_and_executes(webhook):
    client, store, backend, _ = webhook
    aid = _queue_delete(store)
    r = _post(client, _payload(f"YES {aid}"))
    assert r.status_code == 200
    assert store.get_action(aid)["status"] == "executed"
    assert backend.actions_taken == [
        {"action": "delete_paths", "paths": ["/mnt/Modi/Kodi/downloads/complete/old"]}
    ]


def test_owner_no_rejects_without_executing(webhook):
    client, store, backend, _ = webhook
    aid = _queue_delete(store)
    r = _post(client, _payload(f"NO {aid}"))
    assert r.status_code == 200
    assert store.get_action(aid)["status"] == "denied"
    assert backend.actions_taken == []


def test_bad_signature_rejected_and_nothing_executes(webhook):
    client, store, backend, _ = webhook
    aid = _queue_delete(store)
    r = _post(client, _payload(f"YES {aid}"), secret="wrong-secret")
    assert r.status_code == 403
    assert store.get_action(aid)["status"] == "pending"
    assert backend.actions_taken == []


def test_non_owner_sender_ignored(webhook):
    client, store, backend, _ = webhook
    aid = _queue_delete(store)
    r = _post(client, _payload(f"YES {aid}", sender="19999999999"))
    assert r.status_code == 200
    assert store.get_action(aid)["status"] == "pending"
    assert backend.actions_taken == []


def test_non_text_message_ignored(webhook):
    client, store, backend, _ = webhook
    aid = _queue_delete(store)
    r = _post(client, _payload("", msg_type="image"))
    assert r.status_code == 200
    assert store.get_action(aid)["status"] == "pending"
    assert backend.actions_taken == []
