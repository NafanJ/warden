# WhatsApp approval channel — setup

warden pings you on WhatsApp when it wants to run a **Tier 2 (destructive)** action,
and executes it only after you reply `YES <id>`. This is the one-time setup to wire
that up. Everything here is external account configuration — the code is already done
and tested (`tests/test_webhook.py`).

The loop:

```
agent queues Tier 2 action ──▶ WhatsApp message to you ("Reply YES 42 …")
                                        │ you reply "YES 42"
                                        ▼
Meta Cloud API ──POST──▶ Cloudflare Tunnel ──▶ webhook (127.0.0.1:8484) ──▶ executes action
```

---

## 1. Create a Meta WhatsApp Business app

1. Go to <https://developers.facebook.com/apps> → **Create app** → type **Business**.
2. Add the **WhatsApp** product to the app.
3. In **WhatsApp → API Setup** you get a test phone number and a temporary token.
   Note the **Phone number ID** (a long number, *not* the phone number itself).
4. Add **your own** WhatsApp number as a recipient (test numbers can only message
   numbers you've verified) and send a message to the business number once so the
   24-hour messaging window is open for first testing.

For anything beyond testing, generate a **permanent token**: create a System User in
**Business Settings → Users → System users**, assign the app, and generate a token with
`whatsapp_business_messaging` + `whatsapp_business_management` scopes.

## 2. Create the `incident_alert` message template

Free-form text only works inside the 24h window after you last messaged the number.
Outside it, Meta requires a pre-approved template — warden falls back to one named
**`incident_alert`** (see `warden/notifier/whatsapp.py`).

In **WhatsApp → Message templates → Create template**:

| Field | Value |
|-------|-------|
| Name | `incident_alert` |
| Category | **Utility** |
| Language | English (`en`) |
| Body | `{{1}}` (a single body parameter — warden passes the alert text as `{{1}}`) |

Submit and wait for approval (usually minutes to a few hours). Until it's approved,
notifications still work *inside* the 24h window.

> The body must contain exactly one parameter `{{1}}`. Meta sometimes rejects a body
> that is *only* `{{1}}`; if so, prefix fixed text, e.g. `warden alert: {{1}}`.

## 3. Fill in `.env`

On the host, set these in `/opt/warden/.env` (or your repo `.env`):

```bash
NOTIFY_CHANNEL=whatsapp

WA_TOKEN=<permanent or temporary access token>
WA_PHONE_NUMBER_ID=<Phone number ID from step 1>
WA_TO=+<your number in E.164, e.g. +447700900123>
WA_VERIFY_TOKEN=<a random string you invent — used only in step 5>
WA_APP_SECRET=<App secret from Meta: App settings → Basic → App secret>
```

- `WA_VERIFY_TOKEN` — any secret you choose; it must match what you enter in the Meta
  dashboard in step 5. warden checks it during the verification handshake.
- `WA_APP_SECRET` — Meta signs every inbound POST with this (`X-Hub-Signature-256`).
  warden rejects any request whose signature doesn't match, so this must be exact.
- `WA_TO` — only messages **from this number** are accepted as approvals; everything
  else is ignored. Store it with the leading `+`.

## 4. Run the webhook and expose it via Cloudflare Tunnel

The webhook listens on loopback only — never open a port; the tunnel provides public
access.

```bash
# start the service (deploy/warden-webhook.service does this under systemd):
sudo systemctl enable --now warden-webhook.service
# it runs: uvicorn warden.webhook.app:app --host 127.0.0.1 --port 8484

# local sanity check (should print {"status":"ok"}):
curl -s http://127.0.0.1:8484/healthz
```

Add a public hostname to your existing tunnel pointing at the webhook. In
`~/.cloudflared/config.yml` (or the Zero Trust dashboard):

```yaml
ingress:
  - hostname: warden.<your-domain>
    service: http://127.0.0.1:8484
  # … your existing rules …
  - service: http_status:404
```

Reload cloudflared. Your **Callback URL** for Meta is then:
`https://warden.<your-domain>/webhook`

## 5. Register the webhook with Meta

In **WhatsApp → Configuration → Webhook → Edit**:

| Field | Value |
|-------|-------|
| Callback URL | `https://warden.<your-domain>/webhook` |
| Verify token | the same string you put in `WA_VERIFY_TOKEN` |

Click **Verify and save**. Meta sends a `GET /webhook?hub.mode=subscribe&...`; warden
echoes the challenge if the token matches.

You can reproduce that handshake yourself before involving Meta (replace the token):

```bash
curl -s "https://warden.<your-domain>/webhook?hub.mode=subscribe&hub.verify_token=YOUR_VERIFY_TOKEN&hub.challenge=ping123"
# expected output: ping123    (a 403 means the token doesn't match WA_VERIFY_TOKEN)
```

Finally, under **Webhook fields**, **Subscribe** to the **`messages`** field. Without
this, Meta won't forward your replies.

## 6. End-to-end test

1. Make sure `WARDEN_MODE=active` (Tier 2 queueing only happens outside dry-run).
2. Trigger a Tier 2 action. Easiest controlled way — queue one and send the ping
   yourself, then reply on your phone:

   ```bash
   /opt/warden/.venv/bin/python - <<'PY'
   from warden.config import load_config
   from warden.notifier import get_channel
   from warden.store import Store
   cfg = load_config()
   store = Store(cfg.state_dir / "warden.db")
   aid = store.queue_action(None, "delete_paths",
       {"paths": ["/mnt/Modi/Kodi/downloads/complete/__warden_selftest__"],
        "reason": "WhatsApp setup self-test (path does not exist; delete is a no-op)"},
       2, "Self-test delete (no-op)")
   get_channel(cfg).send(f"🛡️ warden self-test: reply YES {aid} to confirm the loop.")
   print("queued action", aid)
   PY
   ```

   The path doesn't exist, so approving it is a harmless no-op (and `delete_paths` is
   hard-limited to the downloads tree regardless).
3. You should get the WhatsApp message. Reply `YES <id>`. Within a second you should get
   `warden: action #<id> executed ✅`. Reply `NO <id>` instead to see it rejected.

If the message never arrives, check `journalctl -u warden-webhook -f` and
`tail -f /opt/warden/state/notifications.log`.

---

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| Meta "Verify and save" fails | `WA_VERIFY_TOKEN` mismatch, or tunnel not routing `/webhook`. Test with the step-5 `curl`. |
| Replies do nothing | Not subscribed to the `messages` webhook field (step 5), or the reply came from a number other than `WA_TO`. |
| Inbound returns 403 | `X-Hub-Signature-256` mismatch → wrong `WA_APP_SECRET`. |
| No outbound message | Outside the 24h window and `incident_alert` template not yet approved; or wrong `WA_PHONE_NUMBER_ID`. |
| `WhatsApp channel requires WA_TOKEN…` on startup | `NOTIFY_CHANNEL=whatsapp` but one of `WA_TOKEN` / `WA_PHONE_NUMBER_ID` / `WA_TO` is blank. |

Pending actions **expire after 12h** (`APPROVAL_TTL_HOURS`); a late `YES` returns
"no longer pending".

> Host note: `deploy/install.sh` builds the venv with `python3 -m venv` + `pip`. On the
> blink host the default `python3` is 3.14 with no `pip`, which breaks; build
> `/opt/warden/.venv` with `uv venv --python 3.12` + `uv pip install -e .` instead.
