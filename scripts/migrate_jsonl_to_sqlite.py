#!/usr/bin/env python3
"""One-time backfill: pot_{a,b}_readings.jsonl / pot_{a,b}_inference_cache.jsonl ->
board_db's SQLite schema (see docs/plan/sqlite-storage-migration.md).

Reads both JSONL files for both pots, batches inserts in transactions (not one INSERT
per row -- at ~1.4M rows/pot that would be very slow), verifies the row counts match
before/after, and does NOT delete or modify the source JSONL files -- it renames them
to a `.migrated` suffix once verification passes, so raw JSONL history is still on
disk (as a safety net) until you're satisfied the SQLite path is working end to end.

All-zero-BME readings rows are skipped during migration (matching the filter
main.py's handle_reading() applies at insert time going forward -- see board_db.py's
module docstring) so the migrated readings table has the same invariant live data
will have. A reading missing light_lux entirely (pre-v6.0 rows) gets
board_db.PLACEHOLDER_LIGHT_LUX, matching board_db.insert_reading's own default.

Usage:
  .venv/bin/python scripts/migrate_jsonl_to_sqlite.py --data-dir /path/to/board/data [--dry-run]

Dry-run against a local copy first, e.g.:
  .venv/bin/python scripts/migrate_jsonl_to_sqlite.py --data-dir data-from-board/sept15/data --dry-run
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (_HERE, os.path.join(_HERE, "..", "KA-Edge-source", "python")):
    if os.path.isfile(os.path.join(_candidate, "board_db.py")):
        sys.path.append(_candidate)
        break
else:
    raise ImportError(f"could not locate board_db.py near {_HERE!r} (checked it and ../KA-Edge-source/python)")

import board_db  # noqa: E402

BATCH_SIZE = 5000


def _is_zero_bme(record: dict) -> bool:
    return (
        record.get("temp_c", 0.0) == 0.0
        and record.get("pressure_hpa", 0.0) == 0.0
        and record.get("gas_kohm", 0.0) == 0.0
    )


def _iter_jsonl(path: str):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # skip a torn line (process killed mid-write) -- same tolerance real_source.py had


def migrate_readings(db_path: str, jsonl_path: str, pot: str) -> tuple[int, int]:
    """Returns (rows_read, rows_inserted) -- rows_read includes all-zero-BME rows
    that get skipped, so rows_inserted <= rows_read is expected, not an error."""
    rows_read = 0
    rows_inserted = 0
    batch = []
    with board_db.connect(db_path) as conn:
        for record in _iter_jsonl(jsonl_path):
            rows_read += 1
            if _is_zero_bme(record):
                continue
            batch.append(record)
            if len(batch) >= BATCH_SIZE:
                for rec in batch:
                    board_db.insert_reading(conn, pot, rec)
                rows_inserted += len(batch)
                conn.commit()
                batch = []
                print(f"  [{pot}] {rows_inserted} readings inserted so far...", flush=True)
        for rec in batch:
            board_db.insert_reading(conn, pot, rec)
        rows_inserted += len(batch)
    return rows_read, rows_inserted


def migrate_inference(db_path: str, jsonl_path: str, pot: str) -> tuple[int, int]:
    """No reading_id linkage for migrated rows (the old sidecar format didn't
    record which raw row produced each result) -- reading_id is left NULL for
    these; only rows inserted by scripts/inference_service.py going forward
    have it populated."""
    rows_read = 0
    rows_inserted = 0
    batch = []
    with board_db.connect(db_path) as conn:
        for record in _iter_jsonl(jsonl_path):
            rows_read += 1
            batch.append(record)
            if len(batch) >= BATCH_SIZE:
                for rec in batch:
                    board_db.insert_inference_result(conn, pot, None, rec)
                rows_inserted += len(batch)
                conn.commit()
                batch = []
                print(f"  [{pot}] {rows_inserted} inference results inserted so far...", flush=True)
        for rec in batch:
            board_db.insert_inference_result(conn, pot, None, rec)
        rows_inserted += len(batch)
    return rows_read, rows_inserted


def bootstrap_inference_service_state(db_path: str, data_dir: str, pot: str) -> None:
    """The old byte-offset state file (.inference_service_state_<pot>.json) is
    incompatible with the new last_reading_id-based format scripts/inference_service.py
    now uses -- left as-is, a fresh PotService would default last_reading_id to 0 and
    reprocess (duplicate) every already-migrated inference_results row.

    Bootstrap the correct starting point instead: the highest readings.id whose
    received_at is at or before the last migrated inference timestamp for this pot
    (i.e. "everything the old sidecar already covered") -- so the live watch program
    resumes exactly where the old sidecar left off, including correctly computing
    inference for any gap where readings kept flowing but the old sidecar had stalled
    (this session found exactly that: a 4-day gap starting 2026-09-11). The trend
    `window` is intentionally left empty -- the first TREND_WINDOW-1 rows after
    resuming warm up the trend features from scratch, the same one-time, small
    accuracy dip any fresh start has, not a correctness bug.

    Only call this for a REAL (non-dry-run) migration, and only after confirming the
    old inference_service.py process has been stopped -- overwriting this file while
    the old process still owns it would corrupt its live state.
    """
    with board_db.connect(db_path) as conn:
        row = conn.execute("SELECT MAX(timestamp) FROM inference_results WHERE pot_id = ?", (pot,)).fetchone()
        last_inference_ts = row[0] if row else None
        last_reading_id = 0
        if last_inference_ts is not None:
            row = conn.execute(
                "SELECT MAX(id) FROM readings WHERE pot_id = ? AND received_at <= ?",
                (pot, last_inference_ts),
            ).fetchone()
            last_reading_id = row[0] if row and row[0] is not None else 0

    state_path = os.path.join(data_dir, f".inference_service_state_{pot.lower()}.json")
    if os.path.exists(state_path):
        backup_path = state_path + ".pre-sqlite-migration.bak"
        os.rename(state_path, backup_path)
        print(f"  [{pot}] old byte-offset state file backed up to {backup_path}")

    with open(state_path, "w") as f:
        json.dump({"last_reading_id": last_reading_id, "window": []}, f)
    print(f"  [{pot}] wrote new state file: last_reading_id={last_reading_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", required=True, help="directory containing pot_{a,b}_readings.jsonl etc.")
    parser.add_argument("--dry-run", action="store_true", help="write to a scratch sensor_data.db.dryrun instead "
                         "of sensor_data.db, and never rename the source JSONL files")
    args = parser.parse_args()

    db_name = "sensor_data.db.dryrun" if args.dry_run else "sensor_data.db"
    db_path = os.path.join(args.data_dir, db_name)
    if os.path.exists(db_path):
        raise SystemExit(f"{db_path} already exists -- refusing to migrate into an existing db "
                          f"(remove it first if you intend to re-run this from scratch)")

    all_ok = True
    for pot in ("A", "B"):
        readings_path = os.path.join(args.data_dir, f"pot_{pot.lower()}_readings.jsonl")
        inference_path = os.path.join(args.data_dir, f"pot_{pot.lower()}_inference_cache.jsonl")

        print(f"=== Pot {pot}: readings ({readings_path}) ===")
        r_read, r_inserted = migrate_readings(db_path, readings_path, pot)
        r_skipped = r_read - r_inserted
        print(f"  read {r_read}, inserted {r_inserted}, skipped {r_skipped} all-zero-BME row(s)")

        print(f"=== Pot {pot}: inference results ({inference_path}) ===")
        i_read, i_inserted = migrate_inference(db_path, inference_path, pot)
        print(f"  read {i_read}, inserted {i_inserted}")
        if i_read != i_inserted:
            print(f"  WARNING: inference read/inserted mismatch for pot {pot} -- investigate before trusting this db")
            all_ok = False

        with board_db.connect(db_path) as conn:
            db_readings_count = conn.execute("SELECT COUNT(*) FROM readings WHERE pot_id = ?", (pot,)).fetchone()[0]
            db_inference_count = conn.execute(
                "SELECT COUNT(*) FROM inference_results WHERE pot_id = ?", (pot,)).fetchone()[0]
        if db_readings_count != r_inserted:
            print(f"  WARNING: pot {pot} readings table has {db_readings_count} rows, expected {r_inserted}")
            all_ok = False
        if db_inference_count != i_inserted:
            print(f"  WARNING: pot {pot} inference_results table has {db_inference_count} rows, expected {i_inserted}")
            all_ok = False

    if not all_ok:
        raise SystemExit("\nVerification found mismatches -- NOT renaming source JSONL files. "
                          f"Inspect {db_path} before retrying.")

    print(f"\nVerification passed. SQLite db written to {db_path}.")
    if args.dry_run:
        print("--dry-run: source JSONL files and state files left untouched.")
        return

    print("\n=== Bootstrapping inference_service.py state files (old format is incompatible) ===")
    for pot in ("A", "B"):
        bootstrap_inference_service_state(db_path, args.data_dir, pot)

    for pot in ("a", "b"):
        for suffix in ("readings", "inference_cache"):
            src = os.path.join(args.data_dir, f"pot_{pot}_{suffix}.jsonl")
            if os.path.exists(src):
                dst = src + ".migrated"
                os.rename(src, dst)
                print(f"Renamed {src} -> {dst}")


if __name__ == "__main__":
    main()
