# warden

**An autonomous ops agent that runs my home server so I don't have to.**

warden monitors a 21-container Docker media server (Plex, Sonarr/Radarr, Transmission,
Cloudflare Tunnel, …) on a Beelink N150. When something breaks — a crashed container, a
stalled download, a disk filling up — it investigates the way an on-call engineer would:
reads logs, inspects state, forms a diagnosis. Then it either fixes the problem itself,
or messages me on WhatsApp asking for permission, depending on how dangerous the fix is.

Every incident produces a written post-mortem in [`incidents/`](incidents/). That archive
is the point: not a demo, a production log.

## How it works

```
┌──────────┐  anomaly   ┌─────────────────────┐  report   ┌────────────┐
│ sentinel  │──────────▶│ agent                │──────────▶│ incidents/ │
│ (cron,    │           │ (Claude Agent SDK,   │           └────────────┘
│  no LLM)  │           │  custom tools only)  │
└──────────┘           └──────────┬───────────┘
                            Tier 2 │ approval needed
                                   ▼
                          ┌────────────────┐   YES/NO   ┌──────────┐
                          │ WhatsApp        │◀──────────│ owner    │
                          │ (Cloud API +    │──────────▶│ (me)     │
                          │  webhook)       │   alert    └──────────┘
                          └────────────────┘
```

- **Sentinel** — deterministic Python, runs every 5 minutes via systemd timer. Collects
  signals (container health, disk, download queues, public URL reachability), applies
  threshold rules. Green path costs $0.00 and one heartbeat line. Anomalies open
  incidents and wake the agent.
- **Agent** — one Claude Agent SDK session per incident. It has **no shell and no file
  access** — only 15 purpose-built tools wrapping Docker, the *arr APIs, Transmission
  RPC, and the filesystem, every one of them routed through a permission gate.
- **Webhook** — FastAPI service behind Cloudflare Tunnel receiving WhatsApp replies.
  `YES 42` executes pending action #42; `NO 42` cancels it.

## The safety model

Permissions are enforced **in code** (the SDK's `can_use_tool` callback), not by prompt:

| Tier | What | Policy |
|------|------|--------|
| 0 | reads: logs, inspect, queues, disk | always allowed, audited |
| 1 | reversible: restart container, blocklist + re-search a download | autonomous in `active` mode, denied in `dry-run`, audited |
| 2 | destructive: delete files, remove torrent **with** data | queued in SQLite, owner pinged on WhatsApp, executed only on explicit approval, expires after 12h |
| — | anything else (including all built-in SDK tools) | denied by default |

Other guardrails: file deletion is hard-limited to the downloads tree regardless of
tier; every report passes a redaction pass (secrets, LAN IPs, key-shaped strings)
before being committed to this public repo; per-incident budget cap.

## Evals

Real incidents are captured as fixtures (full signal snapshot + ground truth) and
replayed against the agent with a mock backend:

```
python -m evals.run
```

Scored on: correct root-cause category, correct action choice, and **safety** (no
destructive call may execute during replay — proposing one for approval is the
correct behaviour).

> Results table and incident archive stats land here after the first month in
> production.

## Running it

```bash
cp .env.example .env          # fill in keys; start with WARDEN_MODE=dry-run
pip install -e ".[dev]"
pytest                        # safety contract tests, no API key needed
python -m warden.sentinel.run # one sentinel cycle
sudo bash deploy/install.sh   # install systemd timer + services on the host
```

Modes: `detect` (sentinel only, notifications, no LLM) → `dry-run` (agent
investigates and writes reports, cannot act) → `active` (Tier 1 autonomous,
Tier 2 via approval). Promote when the reports earn your trust.

## Stack

Python · [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview)
(`claude-sonnet-4-6`) · FastAPI · SQLite · WhatsApp Cloud API · systemd ·
Cloudflare Tunnel
