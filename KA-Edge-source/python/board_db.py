"""Shared SQLite schema/connection helper for board sensor data.

Replaces pot_{a,b}_readings.jsonl / pot_{a,b}_inference_cache.jsonl (see
docs/plan/sqlite-storage-migration.md for the why: unbounded JSONL growth,
~40MB/day combined, 16GB eMMC with only a 9.8G /home partition and 2.7G free
as of 2026-09-16). Every reader/writer (main.py, scripts/inference_service.py,
source/dashboard/real_source.py, scripts/build_training_set.py,
scripts/migrate_jsonl_to_sqlite.py) goes through this module so the schema and
connection setup (WAL mode, matching sync_queue.py's existing convention for
durability) live in exactly one place.

readings/inference_results hold a rolling window (see prune_old()); the two
_daily_rollup tables are never pruned -- they back collecting_since/total-count/
coarse long-range chart context after old raw rows are gone (architecture doc
Section 9's "keep all flags/anomalies indefinitely, downsample or summarize old
raw readings rather than deleting them outright").
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sensor_data.db")

# Historical placeholder for rows with no real light_lux value (pre-BH1750-wiring
# JSONL rows had no light_lux field at all; matches
# source/dashboard/real_source.py's PLACEHOLDER_LIGHT_LUX). light_lux is NOT NULL
# in the schema below, so this is the value actually stored, not a read-time
# fallback -- main.py's live rec_a/rec_b always supply a real light_lux, so this
# only matters for migrate_jsonl_to_sqlite.py backfilling old pre-v6.0 rows.
PLACEHOLDER_LIGHT_LUX = 400.0

# Days of full-resolution readings/inference_results rows kept on the board before
# they're rolled into the permanent daily rollup tables and deleted -- see
# docs/plan/sqlite-storage-migration.md's "Resolved 2026-09-16" retention decision.
RETENTION_DAYS = 7

_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pot_id TEXT NOT NULL,
    sample_index INTEGER NOT NULL,
    millis_ms INTEGER NOT NULL,
    temp_c REAL NOT NULL,
    humidity_pct REAL NOT NULL,
    pressure_hpa REAL NOT NULL,
    gas_kohm REAL NOT NULL,
    light_lux REAL NOT NULL,
    moisture_pct REAL NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_readings_pot_ts ON readings(pot_id, received_at);

CREATE TABLE IF NOT EXISTS inference_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pot_id TEXT NOT NULL,
    reading_id INTEGER REFERENCES readings(id),
    timestamp TEXT NOT NULL,
    rsmi REAL NOT NULL,
    light_delta REAL NOT NULL,
    gas_index_trend REAL NOT NULL,
    rsmi_trend REAL NOT NULL,
    flag TEXT NOT NULL,
    confidence REAL NOT NULL,
    contributing_trend TEXT NOT NULL,
    model_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_inference_pot_ts ON inference_results(pot_id, timestamp);

CREATE TABLE IF NOT EXISTS raw_daily_rollup (
    pot_id TEXT NOT NULL, date TEXT NOT NULL,
    count INTEGER NOT NULL,
    temp_min REAL, temp_max REAL, temp_mean REAL,
    moisture_min REAL, moisture_max REAL, moisture_mean REAL,
    gas_min REAL, gas_max REAL, gas_mean REAL,
    light_min REAL, light_max REAL, light_mean REAL,
    first_ts TEXT, last_ts TEXT,
    PRIMARY KEY (pot_id, date)
);

CREATE TABLE IF NOT EXISTS inference_daily_rollup (
    pot_id TEXT NOT NULL, date TEXT NOT NULL,
    count INTEGER NOT NULL,
    flag_counts_json TEXT NOT NULL,
    rsmi_min REAL, rsmi_max REAL, rsmi_mean REAL,
    confidence_mean REAL,
    model_versions_json TEXT NOT NULL,
    first_ts TEXT, last_ts TEXT,
    PRIMARY KEY (pot_id, date)
);
"""


_SCHEMA_READY: set[str] = set()  # db_path values whose schema DDL has already run this process

_CONNECT_TIMEOUT_S = 30  # generous busy-timeout: main.py and inference_service.py are two
                          # independent processes writing to the same file concurrently --
                          # SQLite only ever allows one writer at a time (WAL mode helps
                          # reader/writer concurrency, not writer/writer), so a competing
                          # writer occasionally has to wait, not fail (2026-09-17 incident:
                          # a "database is locked" OperationalError killed inference_service.py
                          # mid-backfill because the old 10s timeout + running executescript()
                          # on every single connect from both processes made a collision more
                          # likely, and nothing caught the error).


@contextmanager
def connect(db_path: str = DEFAULT_DB_PATH):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=_CONNECT_TIMEOUT_S)
    conn.execute(f"PRAGMA busy_timeout = {_CONNECT_TIMEOUT_S * 1000}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    try:
        # Only run the schema DDL once per process's first connection to this file's
        # path, not on every single connect() call -- CREATE TABLE/INDEX IF NOT EXISTS
        # is a no-op once the schema exists, but still takes a brief write lock every
        # time it runs, and main.py/inference_service.py both call connect() far more
        # often than "once" (every reading, every batch). Cutting that out removes most
        # of the avoidable writer/writer contention between the two processes.
        if db_path not in _SCHEMA_READY:
            conn.executescript(_SCHEMA)
            _SCHEMA_READY.add(db_path)
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_reading(conn: sqlite3.Connection, pot_id: str, rec: dict) -> int:
    """rec: {sample_index, millis_ms, temp_c, humidity_pct, pressure_hpa, gas_kohm,
    light_lux, moisture_pct, received_at} -- same shape main.py already builds."""
    cursor = conn.execute(
        "INSERT INTO readings (pot_id, sample_index, millis_ms, temp_c, humidity_pct, "
        "pressure_hpa, gas_kohm, light_lux, moisture_pct, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            pot_id, rec["sample_index"], rec["millis_ms"], rec["temp_c"], rec["humidity_pct"],
            rec["pressure_hpa"], rec["gas_kohm"], rec.get("light_lux", PLACEHOLDER_LIGHT_LUX), rec["moisture_pct"],
            rec["received_at"],
        ),
    )
    return cursor.lastrowid


def insert_inference_result(conn: sqlite3.Connection, pot_id: str, reading_id: int | None, result: dict) -> int:
    """result: {timestamp, rsmi, light_delta, gas_index_trend, rsmi_trend, flag,
    confidence, contributing_trend, model_version} -- same shape
    scripts/inference_service.py's _compute_inference_row already produces."""
    cursor = conn.execute(
        "INSERT INTO inference_results (pot_id, reading_id, timestamp, rsmi, light_delta, "
        "gas_index_trend, rsmi_trend, flag, confidence, contributing_trend, model_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            pot_id, reading_id, result["timestamp"], result["rsmi"], result["light_delta"],
            result["gas_index_trend"], result["rsmi_trend"], result["flag"], result["confidence"],
            result["contributing_trend"], result["model_version"],
        ),
    )
    return cursor.lastrowid


def _row_to_reading(row: sqlite3.Row) -> dict:
    """Shape matches what main.py/real_source.py/build_training_set.py already expect
    from a parsed pot_{a,b}_readings.jsonl line -- callers migrating off JSONL don't
    need to change their downstream field names."""
    return {
        "sample_index": row["sample_index"], "millis_ms": row["millis_ms"], "temp_c": row["temp_c"],
        "humidity_pct": row["humidity_pct"], "pressure_hpa": row["pressure_hpa"], "gas_kohm": row["gas_kohm"],
        "light_lux": row["light_lux"], "moisture_pct": row["moisture_pct"], "received_at": row["received_at"],
    }


def _row_to_inference(row: sqlite3.Row) -> dict:
    return {
        "timestamp": row["timestamp"], "rsmi": row["rsmi"], "light_delta": row["light_delta"],
        "gas_index_trend": row["gas_index_trend"], "rsmi_trend": row["rsmi_trend"], "flag": row["flag"],
        "confidence": row["confidence"], "contributing_trend": row["contributing_trend"],
        "model_version": row["model_version"],
    }


def fetch_readings(conn: sqlite3.Connection, pot_id: str, since_ts: str | None = None,
                    limit: int | None = None) -> list[dict]:
    conn.row_factory = sqlite3.Row
    query = "SELECT * FROM readings WHERE pot_id = ?"
    params: list = [pot_id]
    if since_ts is not None:
        query += " AND received_at >= ?"
        params.append(since_ts)
    query += " ORDER BY received_at ASC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [_row_to_reading(r) for r in rows]


def fetch_inference_results(conn: sqlite3.Connection, pot_id: str, since_ts: str | None = None,
                             limit: int | None = None) -> list[dict]:
    conn.row_factory = sqlite3.Row
    query = "SELECT * FROM inference_results WHERE pot_id = ?"
    params: list = [pot_id]
    if since_ts is not None:
        query += " AND timestamp >= ?"
        params.append(since_ts)
    query += " ORDER BY timestamp ASC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [_row_to_inference(r) for r in rows]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _rollup_raw_day(conn: sqlite3.Connection, pot_id: str, date: str) -> None:
    rows = conn.execute(
        "SELECT temp_c, moisture_pct, gas_kohm, light_lux, received_at FROM readings "
        "WHERE pot_id = ? AND substr(received_at, 1, 10) = ? ORDER BY received_at",
        (pot_id, date),
    ).fetchall()
    if not rows:
        return
    temps, moist, gas, light = [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows]
    conn.execute(
        "INSERT OR REPLACE INTO raw_daily_rollup (pot_id, date, count, temp_min, temp_max, temp_mean, "
        "moisture_min, moisture_max, moisture_mean, gas_min, gas_max, gas_mean, "
        "light_min, light_max, light_mean, first_ts, last_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            pot_id, date, len(rows), min(temps), max(temps), _mean(temps),
            min(moist), max(moist), _mean(moist), min(gas), max(gas), _mean(gas),
            min(light), max(light), _mean(light), rows[0][4], rows[-1][4],
        ),
    )


def _rollup_inference_day(conn: sqlite3.Connection, pot_id: str, date: str) -> None:
    rows = conn.execute(
        "SELECT rsmi, confidence, flag, model_version, timestamp FROM inference_results "
        "WHERE pot_id = ? AND substr(timestamp, 1, 10) = ? ORDER BY timestamp",
        (pot_id, date),
    ).fetchall()
    if not rows:
        return
    rsmi_vals = [r[0] for r in rows]
    conf_vals = [r[1] for r in rows]
    flag_counts = dict(Counter(r[2] for r in rows))
    model_versions = sorted({r[3] for r in rows})
    conn.execute(
        "INSERT OR REPLACE INTO inference_daily_rollup (pot_id, date, count, flag_counts_json, "
        "rsmi_min, rsmi_max, rsmi_mean, confidence_mean, model_versions_json, first_ts, last_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            pot_id, date, len(rows), json.dumps(flag_counts),
            min(rsmi_vals), max(rsmi_vals), _mean(rsmi_vals), _mean(conf_vals),
            json.dumps(model_versions), rows[0][4], rows[-1][4],
        ),
    )


def prune_old(conn: sqlite3.Connection, retention_days: int = RETENTION_DAYS,
              hard_cap_days: int | None = None) -> dict:
    """Roll every full day older than retention_days into the permanent rollup
    tables, then delete those rows and reclaim space. Idempotent: re-running with
    nothing new to prune is a no-op (INSERT OR REPLACE + DELETE on an empty range).
    Call this once per day (not per reading) -- e.g. from a lightweight timer in
    main.py or a standalone cron-style invocation, not on the hot ingestion path.

    Safety (2026-09-16, motivated by a real incident: scripts/inference_service.py's
    --watch process silently stalled for 4 days while main.py kept ingesting readings):
    a day's readings are only deleted once every row in it already has a matching
    inference_results row -- i.e. the inference watch program has actually caught up
    on that day -- OR the day is older than hard_cap_days (default 3x retention_days),
    in which case it's deleted anyway to guarantee bounded growth even if the watch
    program is permanently dead, and reported back via `protected_dates` so a caller
    can log/alert on it instead of silently losing inference coverage.
    """
    hard_cap_days = hard_cap_days if hard_cap_days is not None else retention_days * 3
    now = datetime.now(timezone.utc)
    cutoff_date = (now - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    hard_cutoff_date = (now - timedelta(days=hard_cap_days)).strftime("%Y-%m-%d")

    pots = [r[0] for r in conn.execute("SELECT DISTINCT pot_id FROM readings WHERE substr(received_at,1,10) < ?",
                                        (cutoff_date,)).fetchall()]
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(received_at,1,10) FROM readings WHERE substr(received_at,1,10) < ?",
        (cutoff_date,)).fetchall()]
    for pot_id in pots:
        for date in dates:
            _rollup_raw_day(conn, pot_id, date)

    inf_pots = [r[0] for r in conn.execute(
        "SELECT DISTINCT pot_id FROM inference_results WHERE substr(timestamp,1,10) < ?", (cutoff_date,)).fetchall()]
    inf_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(timestamp,1,10) FROM inference_results WHERE substr(timestamp,1,10) < ?",
        (cutoff_date,)).fetchall()]
    for pot_id in inf_pots:
        for date in inf_dates:
            _rollup_inference_day(conn, pot_id, date)

    deleted_readings = 0
    protected_dates = []
    for pot_id in pots:
        for date in dates:
            unprocessed = conn.execute(
                "SELECT COUNT(*) FROM readings r WHERE r.pot_id = ? AND substr(r.received_at,1,10) = ? "
                "AND NOT EXISTS (SELECT 1 FROM inference_results i WHERE i.reading_id = r.id)",
                (pot_id, date),
            ).fetchone()[0]
            if unprocessed and date >= hard_cutoff_date:
                protected_dates.append((pot_id, date, unprocessed))
                continue
            deleted_readings += conn.execute(
                "DELETE FROM readings WHERE pot_id = ? AND substr(received_at,1,10) = ?", (pot_id, date)
            ).rowcount

    deleted_inference = conn.execute(
        "DELETE FROM inference_results WHERE substr(timestamp,1,10) < ?", (cutoff_date,)).rowcount
    conn.commit()
    conn.execute("PRAGMA incremental_vacuum")
    return {
        "cutoff_date": cutoff_date, "deleted_readings": deleted_readings, "deleted_inference": deleted_inference,
        "protected_dates": protected_dates,
    }
