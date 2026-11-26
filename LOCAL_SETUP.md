# Warden Local

The owner console uses a Python LangGraph worker, a FastAPI API, a React frontend,
SQLite checkpoints, and local Ollama models. Knowledge files, vectors, conversations,
and model processing stay on the server. Existing Discord notifications still use Discord.

## Runtime

- Console: `http://192.168.1.101:8485`
- Existing webhook: `127.0.0.1:8484`
- Ollama: `127.0.0.1:11434`, container `warden-ollama`
- Chat: `qwen3.5:4b-q4_K_M`
- Embeddings: `embeddinggemma:300m-qat-q8_0`, 768 dimensions
- Python 3.12, one worker, three inference threads, 8K context, 256-token batches

Ollama is limited to three CPU cores and 7 GiB memory. It has cloud features disabled,
no GPU passthrough, and no Watchtower auto-update. Its model volume is `warden_ollama`.
Use the pinned `ollama/ollama:0.34.4` image. Recreate the runtime with:

```bash
docker run -d --name warden-ollama --restart unless-stopped \
  --cpus=3 --memory=7g -p 127.0.0.1:11434:11434 \
  -e OLLAMA_NO_CLOUD=1 -e OLLAMA_NUM_PARALLEL=1 \
  -e OLLAMA_MAX_LOADED_MODELS=2 -e OLLAMA_CONTEXT_LENGTH=8192 \
  -e OLLAMA_VULKAN=0 -l com.centurylinklabs.watchtower.enable=false \
  -v warden_ollama:/root/.ollama ollama/ollama:0.34.4
docker exec warden-ollama ollama pull qwen3.5:4b-q4_K_M
docker exec warden-ollama ollama pull embeddinggemma:300m-qat-q8_0
```

## Development

```bash
uv pip install --python .venv/bin/python -e '.[dev]'
cd web
npm ci
npm run build
cd ..
WARDEN_STATE_DIR=.local-state WARDEN_INCIDENTS_DIR=.local-state/incidents .venv/bin/python -m warden.local.admin init
```

Run the API and worker in separate terminals with the same environment:

```bash
export WARDEN_ENGINE=langgraph WARDEN_MODE=dry-run NOTIFY_CHANNEL=log
export WARDEN_STATE_DIR=/home/nathan/Sandbox/warden/.local-state
export WARDEN_INCIDENTS_DIR=$WARDEN_STATE_DIR/incidents
.venv/bin/uvicorn warden.local.api:create_app --factory --host 192.168.1.101 --port 8485
# In the second terminal, after exporting the same variables:
.venv/bin/python -m warden.local.worker
```

The read-only preview uses port `8486` so it can coexist with production on `8485`.
Use `--port 8486` for the preview API and `WARDEN_TEST_URL=http://192.168.1.101:8486`
when running its browser tests. Preview and production use different state and passwords.

Owner initialization writes a random password to `state/owner-password.txt` (mode 0600)
and stores an Argon2 hash in SQLite. The path is relative to `WARDEN_STATE_DIR`.
Use the password to sign in; sessions expire after 24 hours. This release uses HTTP
on the trusted LAN only. It does not configure public access or change Cloudflare routes.

## Knowledge

Uploads accept text PDFs, Markdown and UTF-8 text, up to 25 MB. Scanned PDFs need OCR
outside Warden. Folder sources are limited to `WARDEN_KNOWLEDGE_ROOTS`, a colon-separated
list of absolute directories. The default is `state/knowledge`. Hidden files, credentials
directories, dependency directories and symlinks are excluded. Reports are indexed as a
built-in source. Refresh runs every five minutes; explicit reindexing is available in the UI.

Documents are split into overlapping passages, embedded locally, and searched through
SQLite FTS5 and sqlite-vec with reciprocal-rank fusion. Keyword browsing works while
Ollama is unavailable. Citations keep the passage, page, document identifier and version hash.
Removing a source removes its searchable index, not files in a watched folder.

## Execution and Recovery

The graph is `context -> model -> tools`, with branches to approval, execution,
verification and completion. State is checkpointed per run; conversation messages are
loaded across runs. The job queue serializes inference and prioritizes conversations
over source indexing. There is an eight-tool limit and a five-minute active-run limit.

Every AI-proposed server mutation requires approval, including restarts and queue changes.
The existing configured download throttle and completed-torrent reaper remain automatic.
Their settings can be changed in the console. Claude full tasks and all cloud AI paths are
disabled when `WARDEN_ENGINE=langgraph`. Discord `diagnose` remains read-only.

Actions are linked to a run and tool call. Browser and Discord decisions update the same
record. The worker atomically claims execution; a crash without an execution receipt marks
the action `uncertain`. Such actions are inspected on recovery and never automatically
executed again. Pending approvals expire after 12 hours.

`warden.db` holds application state and vectors; `graph.db` holds LangGraph checkpoints.
Both live under the persistent state directory. Keep them with uploads when backing up.
Raw trace events are local, and secrets are redacted from tool evidence and notifications.

## Deployment

```bash
sudo .venv/bin/python deploy/install_local.py
systemctl status warden-ui warden-worker
journalctl -u warden-worker -n 50 --no-pager
```

The installer requires legacy approvals to be settled, waits for running scheduled jobs,
backs up the database, environment and code, preserves state, installs locked dependencies,
and enables the new services. It retains the sentinel/summary timers and existing webhook
and Discord integrations. Production uses `WARDEN_ENGINE=langgraph`, `LLM_PROVIDER=ollama`
and `ENABLE_FULL_TASKS=false`.

To pause AI processing without affecting deterministic monitoring and maintenance, stop
`warden-worker.service`; new investigations remain queued. Stop `warden-ui.service` to
disable browser access. Do not restore a legacy cloud configuration as an automatic rollback.
Deployment backups are in `/opt/warden-backup-<timestamp>`; restore them deliberately after
stopping writers. Never overwrite a live SQLite database while its services are running.

## Verification

```bash
.venv/bin/python -m pytest -q
cd web
npm run build
npm test
```

Browser tests use the separate development state and never approve a live server mutation.
`scripts/benchmark_local.py` measures real local embeddings, cold/warm replies and tool
selection. `scripts/smoke_local.py` indexes the repository README and runs a cited answer
and a live read-only container investigation; it refuses to run unless `WARDEN_MODE=dry-run`.
Both store their measurement records in the configured state directory.

Measured on this server's N100 CPU: short model replies took 9-11 seconds;
a full cited document answer took 115 seconds and a one-tool server answer took
124 seconds. Multi-step investigations can reach the five-minute limit.
Cancellation also interrupts the wait before the first generated token.
These checks did not simulate active Plex transcoding.

Regenerate shared TypeScript API contracts after changing public response models:

```bash
WARDEN_STATE_DIR=.local-state .venv/bin/python -m warden.local.admin schema
cd web
npm run types
```

The service logos are from the Homarr dashboard-icons project and are bundled locally.
