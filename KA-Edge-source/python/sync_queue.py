"""Durable local sync queue — SQLite-backed, survives board reboots.

Phase 2 (docs/plan/karbon-assure-ingestion-integration.md §5.1 "Board side"):
every record gets queued locally first; a record is only marked synced
after a genuine 2xx from the Karbon Assure API; failures retry with
backoff. This is specifically about *sync state*, not primary durability
-- the local JSONL readings files (written by main.py) remain the durable
source of truth regardless of whether/when cloud sync succeeds (PRD: "a
connectivity gap delays synchronization, not evidence generation").

Stdlib only (sqlite3), matching this project's established convention.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sync_queue.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- 'evidence' | 'raw_batch'
    idempotency_key TEXT NOT NULL,   -- node_id|pot_id|sample_index (or batch equivalent)
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    synced_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_sync_queue_idempotency ON sync_queue(kind, idempotency_key);
CREATE INDEX IF NOT EXISTS ix_sync_queue_pending ON sync_queue(synced_at, next_attempt_at);
"""

# Exponential backoff, capped -- architecture doc Section 9's "exponential
# backoff on failed sync attempts" requirement.
_BACKOFF_BASE_SECONDS = 5
_BACKOFF_CAP_SECONDS = 900  # 15 min


@contextmanager
def _connect(db_path: str):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")  # architecture doc Section 9: "SQLite with WAL mode"
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def enqueue(kind: str, idempotency_key: str, payload: dict, db_path: str = DEFAULT_DB_PATH) -> int | None:
    """Queue a record for sync. Returns the new row's id, or None if this
    idempotency_key is already queued (harmless — the record is already
    durably queued, this is a no-op, not an error)."""
    with _connect(db_path) as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO sync_queue (kind, idempotency_key, payload_json, created_at, next_attempt_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, idempotency_key, json.dumps(payload), time.time(), time.time()),
            )
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            return None


def get_pending(kind: str, limit: int = 50, db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    """Records ready for a sync attempt now (never-tried, or past backoff)."""
    now = time.time()
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, payload_json, attempts FROM sync_queue "
            "WHERE kind = ? AND synced_at IS NULL AND next_attempt_at <= ? "
            "ORDER BY created_at ASC LIMIT ?",
            (kind, now, limit),
        ).fetchall()
    return [{"id": r[0], "payload": json.loads(r[1]), "attempts": r[2]} for r in rows]


def mark_synced(queue_id: int, db_path: str = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute("UPDATE sync_queue SET synced_at = ? WHERE id = ?", (time.time(), queue_id))


def mark_failed(queue_id: int, error: str, db_path: str = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT attempts FROM sync_queue WHERE id = ?", (queue_id,)).fetchone()
        attempts = (row[0] if row else 0) + 1
        backoff = min(_BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)), _BACKOFF_CAP_SECONDS)
        conn.execute(
            "UPDATE sync_queue SET attempts = ?, next_attempt_at = ?, last_error = ? WHERE id = ?",
            (attempts, time.time() + backoff, error, queue_id),
        )


def pending_count(kind: str | None = None, db_path: str = DEFAULT_DB_PATH) -> int:
    with _connect(db_path) as conn:
        if kind:
            row = conn.execute(
                "SELECT COUNT(*) FROM sync_queue WHERE kind = ? AND synced_at IS NULL", (kind,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM sync_queue WHERE synced_at IS NULL").fetchone()
    return row[0] if row else 0
