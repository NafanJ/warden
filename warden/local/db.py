"""Short-lived connections and atomic transitions shared by all transports."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from warden.store import Store


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return str(uuid.uuid4())


SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations(
 id TEXT PRIMARY KEY, title TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'auto',
 archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
 run_id TEXT, citations_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(conversation_id, created_at);
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
 prompt TEXT NOT NULL, scope TEXT NOT NULL, origin TEXT NOT NULL DEFAULT 'web', incident_id INTEGER,
 forced_json TEXT, error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(conversation_id)
 WHERE status IN ('queued','running','waiting');
CREATE TABLE IF NOT EXISTS run_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, kind TEXT NOT NULL,
 data_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_run ON run_events(run_id,id);
CREATE TABLE IF NOT EXISTS jobs(
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, target TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', priority INTEGER NOT NULL DEFAULT 10,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_queued_job ON jobs(kind,target)
 WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS local_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts(address TEXT PRIMARY KEY, failures INTEGER NOT NULL, reset_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL,
 collection TEXT NOT NULL DEFAULT 'General', status TEXT NOT NULL DEFAULT 'queued',
 error TEXT, indexed_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL,
 hash TEXT NOT NULL, status TEXT NOT NULL, error TEXT, pages INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chunks(
 id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL, page INTEGER NOT NULL,
 content TEXT NOT NULL, embedding_model TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(content, chunk_id UNINDEXED);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        legacy = Store(self.path)
        legacy.conn.close()
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            columns = {r[1] for r in c.execute("PRAGMA table_info(actions)")}
            for name, definition in {"run_id": "TEXT", "call_id": "TEXT", "verified_json": "TEXT"}.items():
                if name not in columns:
                    c.execute(f"ALTER TABLE actions ADD COLUMN {name} {definition}")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS actions_call ON actions(run_id,call_id) WHERE run_id IS NOT NULL")

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=15000")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def all(self, sql: str, args=()) -> list[dict]:
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def execute(self, sql: str, args=()) -> None:
        with self.connect() as c:
            c.execute(sql, args)

    def event(self, run_id: str, kind: str, data: Any) -> None:
        self.execute("INSERT INTO run_events(run_id,kind,data_json,created_at) VALUES(?,?,?,?)",
                     (run_id, kind, json.dumps(data, default=str), now()))

    def conversation(self, title="New conversation", scope="auto") -> dict:
        identity, stamp = uid(), now()
        self.execute("INSERT INTO conversations(id,title,scope,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (identity, title[:120], scope, stamp, stamp))
        return self.one("SELECT * FROM conversations WHERE id=?", (identity,))

    def submit(self, conversation_id: str, prompt: str, scope="auto", origin="web",
               incident_id=None, forced=None) -> dict:
        identity, stamp = uid(), now()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute("SELECT 1 FROM conversations WHERE id=? AND archived=0", (conversation_id,)).fetchone():
                raise ValueError("Conversation not found")
            c.execute("INSERT INTO runs(id,conversation_id,prompt,scope,origin,incident_id,forced_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (identity, conversation_id, prompt, scope, origin, incident_id,
                       json.dumps(forced) if forced else None, stamp, stamp))
            c.execute("INSERT INTO messages(id,conversation_id,role,content,run_id,created_at) VALUES(?,?,?,?,?,?)",
                      (uid(), conversation_id, "user", prompt, identity, stamp))
            c.execute("UPDATE conversations SET updated_at=?, title=CASE WHEN title='New conversation' THEN ? ELSE title END WHERE id=?",
                      (stamp, prompt[:80], conversation_id))
            c.execute("INSERT INTO jobs(kind,target,priority,created_at,updated_at) VALUES('run',?,?,?,?)",
                      (identity, 10 if origin == "web" else 20, stamp, stamp))
        return self.one("SELECT * FROM runs WHERE id=?", (identity,))

    def enqueue(self, kind: str, target: str, priority=30) -> None:
        self.execute("INSERT OR IGNORE INTO jobs(kind,target,priority,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (kind, target, priority, now(), now()))

    def claim_job(self) -> dict | None:
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY priority,id LIMIT 1").fetchone()
            if row:
                c.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (now(), row["id"]))
            return dict(row) if row else None

    def propose(self, run: dict, call: dict, tier: int, description: str) -> dict:
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO actions(incident_id,tool,args_json,tier,description,created_at,run_id,call_id) VALUES(?,?,?,?,?,?,?,?)",
                      (run.get("incident_id"), call["name"], json.dumps(call["args"], sort_keys=True),
                       tier, description, now(), run["id"], call["id"]))
            return dict(c.execute("SELECT * FROM actions WHERE run_id=? AND call_id=?", (run["id"], call["id"])).fetchone())

    def decide(self, action_id: int, approved: bool) -> dict:
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            action = c.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if not action:
                raise ValueError("Action not found")
            if not action["run_id"]:
                raise ValueError("Legacy approval: use its original notification before cutover")
            if action["status"] == "pending":
                expired = action["created_at"] < (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
                status = "expired" if expired else "approved" if approved else "denied"
                c.execute("UPDATE actions SET status=?,decided_at=? WHERE id=?", (status, now(), action_id))
                c.execute("UPDATE runs SET status='queued',updated_at=? WHERE id=? AND status='waiting'", (now(), action["run_id"]))
                c.execute("INSERT OR IGNORE INTO jobs(kind,target,priority,created_at,updated_at) VALUES('run',?,0,?,?)",
                          (action["run_id"], now(), now()))
            return dict(c.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone())

    def cancel(self, run_id: str) -> None:
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE runs SET cancel_requested=1, status=CASE WHEN status IN ('queued','waiting') THEN 'cancelled' ELSE status END,updated_at=? WHERE id=?",
                      (now(), run_id))
            c.execute("UPDATE actions SET status='denied',decided_at=? WHERE run_id=? AND status IN ('pending','approved')", (now(), run_id))

    def recover(self) -> None:
        # A process can die after the external side effect but before its receipt.
        with self.connect() as c:
            c.execute("UPDATE actions SET status='uncertain',result='Worker stopped during execution; inspect before retrying.' WHERE status='executing' AND run_id IS NOT NULL")
            c.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            c.execute("UPDATE runs SET status='queued' WHERE status='running'")
