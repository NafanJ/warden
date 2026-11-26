from __future__ import annotations

import fcntl
import logging
import os
import time

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from warden.backends.live import LiveBackend
from warden.config import load_config
from warden.local.db import Database, now
from warden.local.graph import Investigation
from warden.local.knowledge import IndexPaused, Knowledge
from warden.local.models import LocalModel
from warden.local.settings import LocalSettings

log = logging.getLogger("warden.worker")


def process_run(db, settings, config, backend, model, knowledge, run_id):
    run = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    if not run or run["status"] in {"completed", "cancelled"}:
        return
    db.execute("UPDATE runs SET status='running',error=NULL,updated_at=? WHERE id=?", (now(), run_id))
    investigation = Investigation(db, settings, config, backend, model, knowledge, run)
    try:
        with SqliteSaver.from_conn_string(str(settings.state / "graph.db")) as saver:
            graph = investigation.build(saver)
            options = {"configurable": {"thread_id": run_id}, "recursion_limit": 80}
            snapshot = graph.get_state(options)
            interrupted = any(task.interrupts for task in snapshot.tasks)
            data = Command(resume=True) if interrupted else None if snapshot.values else {}
            result = graph.invoke(data, options)
            if result.get("__interrupt__"):
                db.execute("UPDATE runs SET status='waiting',updated_at=? WHERE id=?", (now(), run_id))
                db.event(run_id, "approval", {"action_id": result.get("action_id")})
    except Exception as exc:
        status = "cancelled" if isinstance(exc, InterruptedError) else "failed"
        db.execute("UPDATE actions SET status='uncertain',result=? WHERE run_id=? AND status='executing'",
                   ("Execution ended without a receipt. Inspect before retrying.", run_id))
        db.execute("UPDATE runs SET status=?,error=?,updated_at=? WHERE id=?", (status, str(exc)[:1000], now(), run_id))
        db.event(run_id, "error", {"message": str(exc)[:1000], "status": status})
        log.exception("Run %s failed", run_id)


def main():
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    settings = LocalSettings.from_config(config)
    settings.state.mkdir(parents=True, exist_ok=True)
    lock = (settings.state / "worker.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    db = Database(settings.state / "warden.db")
    db.initialize()
    db.recover()
    model = LocalModel(settings)
    knowledge = Knowledge(db, settings, model)
    backend = LiveBackend(config)
    last_scan = 0.0

    def indexing_checkpoint():
        if db.one("SELECT 1 FROM jobs WHERE kind='run' AND status='queued'"):
            raise IndexPaused()

    while True:
        db.execute("INSERT OR REPLACE INTO local_settings(key,value) VALUES('worker_heartbeat',?)", (now(),))
        for action in db.all("SELECT id FROM actions WHERE run_id IS NOT NULL AND status='pending' AND datetime(created_at) < datetime('now','-12 hours')"):
            db.decide(action["id"], False)
        for row in db.all("SELECT DISTINCT r.id FROM runs r JOIN actions a ON a.run_id=r.id WHERE r.status='waiting' AND a.status IN ('approved','denied','expired','uncertain')"):
            db.execute("UPDATE runs SET status='queued' WHERE id=?", (row["id"],))
            db.enqueue("run", row["id"], 0)
        if time.monotonic() - last_scan > 300:
            for source in db.all("SELECT id FROM sources WHERE kind IN ('folder','reports')"):
                db.enqueue("index", source["id"], 40)
            last_scan = time.monotonic()
        job = db.claim_job()
        if not job:
            time.sleep(1)
            continue
        try:
            if job["kind"] == "run":
                process_run(db, settings, config, backend, model, knowledge, job["target"])
            elif job["kind"] == "index":
                knowledge.index(job["target"], indexing_checkpoint)
            db.execute("UPDATE jobs SET status='done',updated_at=? WHERE id=?", (now(), job["id"]))
            if job["kind"] == "run" and db.one("SELECT 1 FROM runs WHERE id=? AND status='queued'", (job["target"],)):
                db.enqueue("run", job["target"], 0)
        except IndexPaused:
            db.execute("UPDATE jobs SET status='done' WHERE id=?", (job["id"],))
            db.enqueue("index", job["target"], 40)
        except Exception:
            log.exception("Job %s failed", job["id"])
            db.execute("UPDATE jobs SET status='failed',updated_at=? WHERE id=?", (now(), job["id"]))


if __name__ == "__main__":
    main()
