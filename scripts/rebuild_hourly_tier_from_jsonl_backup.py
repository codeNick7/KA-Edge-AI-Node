#!/usr/bin/env python3
"""One-off recovery: rebuild hourly-resolution readings_tiered rows for dates that were
already collapsed to tier='daily' by the now-removed Tier 2 -> Tier 3 compaction step
(see docs/plan/rollback-daily-tier-to-hourly-2026-09-28.md).

board_db.py's daily-compaction ran once on the live board (2026-09-25 deployment) and
collapsed Aug 14 -> Sep 4 down to 1 point/day per pot, deleting the hourly-resolution
rows it was built from. That per-point resolution isn't gone, though -- the pre-SQLite
JSONL backup (data-from-board/pre-sqlite-migration/data/data/pot_{a,b}_readings.jsonl.migrated)
holds full per-second readings for 2026-08-14T09:40 -> 2026-09-16T11:31, which covers the
entire collapsed window.

For each (pot, date) that currently has tier='daily' rows in readings_tiered:
  1. Read that date's rows straight out of the JSONL backup.
  2. Re-bucket them to one non-outlier row per hour, using the exact same
     board_db._bucket_and_pick(..., "hour") logic prune_old() uses today.
  3. Delete the existing tier='daily' row(s) for that (pot, date) and insert the
     rebuilt tier='hourly' rows in their place.

Dry-run by default: prints what it would do (dates found, row counts) and writes
nothing. Pass --apply to actually write. Always point --db-path at a local copy first
(e.g. a copy of the board's sensor_data.db pulled down, or a scratch db) -- this script
does not know how to reach the board itself; run it there directly, following the
existing deploy process, only once a local dry-run + --apply looks right.

Usage:
  .venv/bin/python scripts/rebuild_hourly_tier_from_jsonl_backup.py \\
      --db-path /path/to/sensor_data.db \\
      --jsonl-dir data-from-board/pre-sqlite-migration/data/data
  # review the dry-run output, then:
  .venv/bin/python scripts/rebuild_hourly_tier_from_jsonl_backup.py \\
      --db-path /path/to/sensor_data.db \\
      --jsonl-dir data-from-board/pre-sqlite-migration/data/data \\
      --apply
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

_POTS = ("A", "B")


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
                continue  # skip a torn line, same tolerance migrate_jsonl_to_sqlite.py uses


def _rows_for_date(jsonl_path: str, date: str) -> list[tuple]:
    """Rows for `date` (YYYY-MM-DD) from the JSONL backup, shaped as board_db._RAW_TIER_COLS
    tuples so board_db._bucket_and_pick can be reused unchanged."""
    out = []
    for rec in _iter_jsonl(jsonl_path):
        received_at = rec.get("received_at", "")
        if received_at[:10] != date:
            continue
        out.append(tuple(rec.get(col) for col in board_db._RAW_TIER_COLS))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", required=True, help="sensor_data.db to rebuild the hourly tier in")
    parser.add_argument("--jsonl-dir", required=True,
                         help="directory containing pot_{a,b}_readings.jsonl.migrated")
    parser.add_argument("--apply", action="store_true", help="actually write; default is dry-run (prints only)")
    args = parser.parse_args()

    if not os.path.exists(args.db_path):
        raise SystemExit(f"{args.db_path} does not exist")

    for pot in _POTS:
        jsonl_path = os.path.join(args.jsonl_dir, f"pot_{pot.lower()}_readings.jsonl.migrated")
        if not os.path.exists(jsonl_path):
            print(f"[{pot}] SKIP -- no backup file at {jsonl_path}")
            continue

        with board_db.connect(args.db_path) as conn:
            daily_dates = [
                row[0] for row in conn.execute(
                    "SELECT DISTINCT substr(received_at,1,10) FROM readings_tiered "
                    "WHERE pot_id = ? AND tier = 'daily' ORDER BY 1", (pot,)
                ).fetchall()
            ]

        if not daily_dates:
            print(f"[{pot}] no tier='daily' rows found -- nothing to rebuild")
            continue

        print(f"[{pot}] {len(daily_dates)} daily-tier date(s) to rebuild: "
              f"{daily_dates[0]} .. {daily_dates[-1]}")

        for date in daily_dates:
            rows = _rows_for_date(jsonl_path, date)
            if not rows:
                print(f"  [{pot}] {date}: SKIP -- backup has no rows for this date, "
                      f"leaving existing daily-tier row(s) in place")
                continue
            kept = board_db._bucket_and_pick(
                rows, board_db._RAW_TIER_TS_IDX, board_db._RAW_TIER_VALUE_IDX, "hour"
            )
            print(f"  [{pot}] {date}: {len(rows)} backup rows -> {len(kept)} hourly rows "
                  f"(replacing 1 daily row)")

            if not args.apply:
                continue

            with board_db.connect(args.db_path) as conn:
                conn.execute(
                    "DELETE FROM readings_tiered WHERE pot_id = ? AND tier = 'daily' "
                    "AND substr(received_at,1,10) = ?", (pot, date),
                )
                cols = board_db._RAW_TIER_COLS
                conn.executemany(
                    f"INSERT INTO readings_tiered (pot_id, tier, {', '.join(cols)}) "
                    f"VALUES (?, ?, {', '.join('?' * len(cols))})",
                    [(pot, board_db.TIER_HOURLY, *r) for r in kept],
                )
                conn.commit()

    if not args.apply:
        print("\n--dry-run: no changes written. Re-run with --apply once this looks right.")
    else:
        print("\nDone.")


if __name__ == "__main__":
    main()
