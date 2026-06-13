"""SQLite persistence: incidents, Tier 2 pending actions, audit trail."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    category TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',          -- open | resolved | escalated
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    report_path TEXT
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER REFERENCES incidents(id),
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    tier INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',       -- pending | approved | denied | executed | failed | expired
    description TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    result TEXT,
    notify_ref TEXT                               -- e.g. the Discord message id the owner reacts to
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    incident_id INTEGER,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    tier INTEGER NOT NULL,
    decision TEXT NOT NULL                        -- allowed | denied | queued
);
"""

APPROVAL_TTL_HOURS = 12


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: str | Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the webhook's connection is created at import
        # on the main thread but read/written from request-handler threads. Access
        # is effectively serialized (low volume), so sharing the connection is safe.
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive migrations for DBs created before a column existed."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(actions)")}
        if "notify_ref" not in cols:
            self.conn.execute("ALTER TABLE actions ADD COLUMN notify_ref TEXT")

    # --- incidents ---
    def open_incident(self, key: str, category: str, summary: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO incidents (key, category, summary, opened_at) VALUES (?,?,?,?)",
            (key, category, summary, _now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def find_open_incident(self, key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM incidents WHERE key=? AND status='open' ORDER BY id DESC LIMIT 1", (key,)
        ).fetchone()
        return dict(row) if row else None

    def find_recent_unresolved(self, key: str, cooldown_hours: float) -> dict[str, Any] | None:
        """Most recent incident for `key` that was closed as 'escalated' within the
        cooldown window — i.e. handled recently but not actually resolved. The
        sentinel uses this to avoid re-raising a persistent unfixable condition
        every cycle. A 'resolved' incident does not suppress (a recurrence is news)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)).isoformat()
        row = self.conn.execute(
            "SELECT * FROM incidents WHERE key=? AND status='escalated' AND opened_at >= ? "
            "ORDER BY id DESC LIMIT 1", (key, cutoff)
        ).fetchone()
        return dict(row) if row else None

    def close_incident(self, incident_id: int, status: str = "resolved",
                       report_path: str | None = None) -> None:
        self.conn.execute(
            "UPDATE incidents SET status=?, closed_at=?, report_path=COALESCE(?, report_path) WHERE id=?",
            (status, _now(), report_path, incident_id),
        )
        self.conn.commit()

    def set_report_path(self, incident_id: int, report_path: str) -> None:
        self.conn.execute("UPDATE incidents SET report_path=? WHERE id=?", (report_path, incident_id))
        self.conn.commit()

    def get_incident(self, incident_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        return dict(row) if row else None

    # --- tier 2 pending actions ---
    def queue_action(self, incident_id: int | None, tool: str, args: dict[str, Any],
                     tier: int, description: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO actions (incident_id, tool, args_json, tier, description, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (incident_id, tool, json.dumps(args), tier, description, _now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def set_action_notify_ref(self, action_id: int, ref: str) -> None:
        """Record the notification message (e.g. Discord message id) whose
        reactions stand in for an approval reply."""
        self.conn.execute("UPDATE actions SET notify_ref=? WHERE id=?", (ref, action_id))
        self.conn.commit()

    def pending_actions(self) -> list[dict[str, Any]]:
        self._expire_stale()
        rows = self.conn.execute(
            "SELECT * FROM actions WHERE status='pending' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_action(self, action_id: int) -> dict[str, Any] | None:
        self._expire_stale()
        row = self.conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        return dict(row) if row else None

    def decide_action(self, action_id: int, approved: bool) -> dict[str, Any] | None:
        """Mark a pending action approved/denied. Returns the action row, or None
        if it doesn't exist or is no longer pending."""
        self._expire_stale()
        row = self.conn.execute(
            "SELECT * FROM actions WHERE id=? AND status='pending'", (action_id,)
        ).fetchone()
        if not row:
            return None
        status = "approved" if approved else "denied"
        self.conn.execute("UPDATE actions SET status=?, decided_at=? WHERE id=?",
                          (status, _now(), action_id))
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone())

    def mark_executed(self, action_id: int, result: str, failed: bool = False) -> None:
        self.conn.execute("UPDATE actions SET status=?, result=? WHERE id=?",
                          ("failed" if failed else "executed", result[:5000], action_id))
        self.conn.commit()

    def find_pending_action(self, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """An identical action already queued (avoids duplicate WhatsApp pings)."""
        self._expire_stale()
        row = self.conn.execute(
            "SELECT * FROM actions WHERE tool=? AND args_json=? AND status='pending'",
            (tool, json.dumps(args)),
        ).fetchone()
        return dict(row) if row else None

    def find_approved_action(self, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        self._expire_stale()
        row = self.conn.execute(
            "SELECT * FROM actions WHERE tool=? AND args_json=? AND status='approved'",
            (tool, json.dumps(args)),
        ).fetchone()
        return dict(row) if row else None

    def _expire_stale(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=APPROVAL_TTL_HOURS)).isoformat()
        self.conn.execute(
            "UPDATE actions SET status='expired' WHERE status='pending' AND created_at < ?", (cutoff,)
        )
        self.conn.commit()

    # --- audit ---
    def audit(self, tool: str, args: dict[str, Any], tier: int, decision: str,
              incident_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO audit (ts, incident_id, tool, args_json, tier, decision) VALUES (?,?,?,?,?,?)",
            (_now(), incident_id, tool, json.dumps(args), tier, decision),
        )
        self.conn.commit()
