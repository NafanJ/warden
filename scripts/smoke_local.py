"""Exercise real local retrieval and read-only tools against the running server."""
import argparse
import json
import time
from pathlib import Path

from warden.backends.live import LiveBackend
from warden.config import load_config
from warden.local.db import Database
from warden.local.knowledge import Knowledge
from warden.local.models import LocalModel
from warden.local.settings import LocalSettings
from warden.local.worker import process_run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=["both", "knowledge", "server"], default="both")
    args = parser.parse_args()
    config = load_config()
    assert config.mode == "dry-run", "Smoke test must run in dry-run mode"
    settings = LocalSettings.from_config(config)
    db = Database(settings.state / "warden.db")
    db.initialize()
    backend, model = LiveBackend(config), LocalModel(settings)
    knowledge = Knowledge(db, settings, model)
    source = knowledge.add_source("Warden runbook", "upload", Path("README.md"), "Server")
    started = time.monotonic()
    knowledge.index(source["id"])
    source_status = db.one("SELECT status,error FROM sources WHERE id=?", (source["id"],))
    assert source_status["status"] == "ready", source_status
    print(json.dumps({"index_seconds": round(time.monotonic() - started, 2)}), flush=True)
    report = {"before": backend.docker_ps(), "runs": []}
    if config.tautulli_api_key:
        report["plex_activity"] = {k: v for k, v in backend.tautulli_activity().items() if k in {"stream_count", "transcode_count"}}
    for question, scope in [
        ("According to the Warden runbook, how often does the sentinel run, and what does it do on a healthy cycle? Cite your source and answer in two sentences.", "knowledge"),
        ("Use get_containers once, then report how many containers are running. Use no other tools. Answer in one sentence.", "server"),
    ]:
        if args.scope not in {"both", scope}:
            continue
        conversation = db.conversation("Local acceptance: " + scope, scope)
        run = db.submit(conversation["id"], question, scope)
        start = time.monotonic()
        process_run(db, settings, config, backend, model, knowledge, run["id"])
        result = db.one("SELECT status,error FROM runs WHERE id=?", (run["id"],))
        answer = db.one("SELECT content,citations_json FROM messages WHERE run_id=? AND role='assistant'", (run["id"],))
        entry = {"scope": scope, "seconds": round(time.monotonic() - start, 2), "result": result, "answer": answer}
        report["runs"].append(entry)
        print(json.dumps({**entry, "answer": answer["content"] if answer else None}), flush=True)
        assert result["status"] == "completed", result
        if scope == "knowledge":
            assert answer and json.loads(answer["citations_json"]), "No source citations"
        else:
            assert db.one("SELECT 1 FROM run_events WHERE run_id=? AND kind='tool_result'", (run["id"],)), "No tools were used"
    report["after"] = backend.docker_ps()
    report["actions"] = db.all("SELECT tool,status FROM actions WHERE run_id IS NOT NULL")
    target = settings.state / "acceptance.json"
    target.write_text(json.dumps(report, indent=2))
    print(f"Acceptance record saved to {target}")


if __name__ == "__main__":
    main()
