# Discord approval channel — setup

warden pings you in a Discord channel before running a **Tier 2 (destructive)**
action, and executes it only after you approve — by **tapping the pre-added ✅
reaction** (or ❌ to reject), or by typing `YES <id>` / `NO <id>` as a fallback.
Unlike WhatsApp, there's **no public webhook and no tunnel** — a bot *polls* the
channel over an outbound connection. Setup is ~5 minutes and there's nothing to
expose to the internet.

```
agent queues Tier 2 action ──▶ bot posts alert + pre-adds ✅/❌
                                        │ you tap ✅ (or ❌), or type YES/NO 42
                                        ▼
            discord poller (outbound poll, no inbound port) ──▶ executes action,
                                        then edits the alert to show the outcome
```

## 1. Create the bot

1. <https://discord.com/developers/applications> → **New Application**, name it `warden`.
2. **Bot** tab → **Reset Token** → copy it. This is `DISCORD_BOT_TOKEN`.
3. On the same tab, enable **Message Content Intent** (so the bot can read your replies).
4. **Installation** (or **OAuth2 → URL Generator**): scope **`bot`**, permissions
   **View Channels** + **Send Messages** + **Read Message History**. Open the generated
   URL and add the bot to your server.

## 2. Make a private channel and get the ids

1. Create a channel (e.g. `#warden`) that only you and the bot can see.
2. In Discord, enable **Settings → Advanced → Developer Mode**.
3. Right-click the channel → **Copy Channel ID** → `DISCORD_CHANNEL_ID`.
4. Right-click **your own** username → **Copy User ID** → `DISCORD_OWNER_ID`
   (only this user's replies are accepted as approvals).

## 3. Fill in `.env`

```bash
NOTIFY_CHANNEL=discord
DISCORD_BOT_TOKEN=<bot token from step 1>
DISCORD_CHANNEL_ID=<channel id from step 2>
DISCORD_OWNER_ID=<your user id from step 2>
```

## 4. Run the poller

```bash
sudo systemctl enable --now warden-discord.service   # deploy/warden-discord.service
# it runs: python -m warden.notifier.discord_poller
journalctl -u warden-discord -f                       # watch it
```

## 5. End-to-end test

Queue a harmless no-op Tier 2 action and approve it from Discord:

```bash
/opt/warden/.venv/bin/python - <<'PY'
from warden.config import load_config
from warden.notifier import get_channel
from warden.store import Store
cfg = load_config()
store = Store(cfg.state_dir / "warden.db")
aid = store.queue_action(None, "delete_paths",
    {"paths": ["/mnt/Modi/Kodi/downloads/complete/__warden_selftest__"],
     "reason": "Discord setup self-test (path does not exist; delete is a no-op)"},
    2, "Self-test delete (no-op)")
get_channel(cfg).send(f"🛡️ warden self-test: reply YES {aid} to confirm the loop.")
print("queued action", aid)
PY
```

The path doesn't exist, so approving it is a no-op (and `delete_paths` is hard-limited
to the downloads tree regardless). The bot posts the alert with **✅ and ❌ already
attached** — **tap ✅** and within a few seconds it executes and edits the message to
`✅ Approved — executed`. Tap ❌ (or type `NO <id>`) to reject. Typing `YES <id>` works
too if you'd rather not tap.

## Notes

- **Sharing warden:** an adopter repeats steps 1–4 with their own bot + channel. No Meta
  account, no template review, no public endpoint — that's why this is the recommended
  default over WhatsApp.
- Only messages from `DISCORD_OWNER_ID` are acted on; ordinary chat in the channel is
  ignored (no reply spam).
- Pending actions expire after 12h; a late `YES` returns "no longer pending".
- The bot token is the one secret — treat it like a password; rotate it on the Bot tab if
  leaked. `.env` is gitignored.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| Bot posts nothing | `NOTIFY_CHANNEL` not `discord`, or wrong `DISCORD_CHANNEL_ID`, or bot not added to the server. |
| Typed replies do nothing | Reply came from a user other than `DISCORD_OWNER_ID`, or **Message Content Intent** is off (step 1.3). |
| Tapping ✅/❌ does nothing | `DISCORD_OWNER_ID` not set (reactions need a known owner to attribute the tap), or you tapped a different emoji than the bot pre-added. Reading reactions does **not** require Message Content Intent. |
| `Discord channel requires DISCORD_BOT_TOKEN…` on startup | `NOTIFY_CHANNEL=discord` but token or channel id is blank. |
| `401 Unauthorized` in the poller log | Bad/rotated `DISCORD_BOT_TOKEN`. |
