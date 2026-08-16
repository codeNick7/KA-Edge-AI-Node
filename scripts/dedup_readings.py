#!/usr/bin/env python3
"""One-time cleanup for the pot_a/pot_b_readings.jsonl duplication bug.

Root cause (diagnosed 2026-08-15): stopping the App Lab project without
using its Stop control left the previous main.py process running. The
Bridge "reading" channel broadcasts to every subscribed process, so each
leftover process independently appended its own copy of the same reading
to the same JSONL files -- confirmed via exact byte-identical duplicate
records (same received_at down to the microsecond, same every field).
Fixed at the source around 2026-08-15T02:09:37Z once the extra processes
were cleared; data collected before that timestamp has the duplicates
baked in and needs a one-time cleanup before the 16-17 Aug training step.

This dedupes on exact full-record equality (every field must match) --
deliberately NOT just received_at, so it only ever removes true replay
duplicates and can never merge two genuinely different readings that
happen to share a timestamp. Order-preserving: first occurrence of each
record is kept. Malformed/partial lines are skipped, same tolerance
real_source.py's _load_records already applies.

Safe by default: writes to a new "<name>.dedup.jsonl" file rather than
touching the input. Pass --in-place to overwrite the original instead --
this always makes a timestamped .bak copy first.

Usage (run on the board, stdlib only, no dependencies):
  python3 dedup_readings.py pot_a_readings.jsonl pot_b_readings.jsonl
  python3 dedup_readings.py --in-place pot_a_readings.jsonl pot_b_readings.jsonl
"""

import argparse
import json
import os
import shutil
from datetime import datetime, timezone


def dedup_lines(lines: list[str]) -> tuple[list[str], int, int]:
    """Return (kept_lines, malformed_skipped, duplicates_removed)."""
    seen = set()
    kept = []
    malformed = 0
    duplicates = 0

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue

        # Canonical form so key order never affects duplicate detection.
        key = json.dumps(record, sort_keys=True)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        kept.append(line)

    return kept, malformed, duplicates


def process_file(path: str, in_place: bool) -> None:
    if not os.path.exists(path):
        print(f"SKIP {path}: file not found")
        return

    with open(path) as f:
        lines = f.readlines()

    kept, malformed, duplicates = dedup_lines(lines)

    print(
        f"{path}: {len(lines)} lines read, {duplicates} exact duplicates removed, "
        f"{malformed} malformed lines skipped, {len(kept)} unique records kept"
    )

    if duplicates == 0 and malformed == 0:
        print(f"  -> no changes needed for {path}")
        return

    if in_place:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = f"{path}.{timestamp}.bak"
        shutil.copy2(path, backup_path)
        print(f"  -> backed up original to {backup_path}")

        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w") as f:
            for line in kept:
                f.write(line + "\n")
        os.replace(tmp_path, path)
        print(f"  -> wrote deduped data back to {path}")
    else:
        out_path = f"{os.path.splitext(path)[0]}.dedup.jsonl"
        with open(out_path, "w") as f:
            for line in kept:
                f.write(line + "\n")
        print(f"  -> wrote deduped data to {out_path} (original left untouched)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove exact-duplicate JSONL reading records (see module docstring)."
    )
    parser.add_argument("paths", nargs="+", help="pot_a_readings.jsonl / pot_b_readings.jsonl paths")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="overwrite the input file (after writing a timestamped .bak copy). "
        "Default is to write a separate <name>.dedup.jsonl file instead.",
    )
    args = parser.parse_args()

    for path in args.paths:
        process_file(path, args.in_place)


if __name__ == "__main__":
    main()
