"""FastAPI webhook for WhatsApp Cloud API.

GET  /webhook — Meta's verification handshake (hub.challenge echo).
POST /webhook — inbound messages, validated with X-Hub-Signature-256.

Run:  uvicorn warden.webhook.app:app --host 127.0.0.1 --port 8484
Expose via a Cloudflare Tunnel public hostname; never open a port.
"""
from __future__ import annotations

import hashlib
import hmac

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from warden.backends.live import LiveBackend
from warden.config import load_config
from warden.notifier import get_channel
from warden.store import Store
from warden.webhook.approvals import handle_reply

app = FastAPI(title="warden webhook")

config = load_config()
backend = LiveBackend(config)
store = Store(config.state_dir / "warden.db")
channel = get_channel(config)


@app.get("/webhook")
async def verify(
    hub_mode: str = Query(alias="hub.mode", default=""),
    hub_verify_token: str = Query(alias="hub.verify_token", default=""),
    hub_challenge: str = Query(alias="hub.challenge", default=""),
):
    if hub_mode == "subscribe" and hub_verify_token == config.wa_verify_token:
        return PlainTextResponse(hub_challenge)
    raise HTTPException(status_code=403, detail="verification failed")


@app.post("/webhook")
async def receive(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(
        config.wa_app_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=403, detail="bad signature")

    payload = await request.json()
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for message in change.get("value", {}).get("messages", []):
                if message.get("type") != "text":
                    continue
                sender = message.get("from", "")
                if config.wa_to and sender != config.wa_to.lstrip("+"):
                    continue  # only the owner may approve actions
                reply = handle_reply(message["text"]["body"], config, backend, store, channel)
                channel.send(reply)
    return {"status": "ok"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
