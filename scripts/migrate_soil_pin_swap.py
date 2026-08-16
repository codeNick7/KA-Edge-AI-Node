#!/usr/bin/env python3
"""Fix for the Pot A / Pot B soil-pin swap bug (diagnosed 2026-08-15).

Wiring was backwards from the very start of data collection: sketch.ino's
SOIL_PIN_A/SOIL_PIN_B constants assumed Pot A -> A0, Pot B -> A1, but the
real physical wiring is Pot A -> A1, Pot B -> A0. Firmware fixed in
sketch.ino v7.1, but intentionally NOT deployed yet (pins/wiring left as-is
for now) -- so every moisture_pct value written so far, and every value
that will be written until the firmware is eventually redeployed, has the
two pots' soil readings swapped. temp/humidity/pressure/gas are unaffected
(those come from each pot's own BME688 and were never swapped).

This script fixes it after the fact: for each pair of Pot A / Pot B records
sharing the same received_at (both written from the same handle_reading()
call in main.py, so this is a reliable pairing key), it swaps their
moisture_pct values.

Records that exist in only ONE file are left untouched and reported
separately -- these happen when one pot's BME688 read all-zero that cycle
and main.py skipped writing its record entirely (moisture_pct is sampled
independently of the BME688 and would have been fine, but the paired
record was never written, so there's no recoverable correct value; forcing
a guess would be worse than leaving the known-wrong value visible). Decide
what to do with these yourself -- e.g. exclude them from training data --
rather than having this script silently guess.

Re-runnable: since the fix isn't deployed, run this again on freshly
collected data whenever you're about to use it (e.g. before the 16-17 Aug
training step), not just once.

Safe by default: writes to new "<name>.pinfix.jsonl" files rather than
touching the inputs. Pass --in-place to overwrite instead -- this always
makes a timestamped .bak copy first.

Usage (stdlib only, no dependencies):
  python3 migrate_soil_pin_swap.py pot_a_readings.jsonl pot_b_readings.jsonl
  python3 migrate_soil_pin_swap.py --in-place pot_a_readings.jsonl pot_b_readings.jsonl
"""

import argparse
import json
import os
import shutil
from datetime import datetime, timezone


def load_records(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # skip a partially-written line, same tolerance as real_source.py
    return records


def index_by_timestamp(records: list[dict]) -> dict[str, list[int]]:
    """Map received_at -> list of positions in `records` sharing that timestamp.

    Normally one position each; a list only grows past length 1 if the same
    file has a genuine timestamp collision (rare, and flagged as ambiguous
    rather than guessed at).
    """
    index: dict[str, list[int]] = {}
    for i, rec in enumerate(records):
        ts = rec.get("received_at")
        if ts is None:
            continue
        index.setdefault(ts, []).append(i)
    return index


def swap_moisture(pot_a_records: list[dict], pot_b_records: list[dict]) -> dict:
    """Swap moisture_pct between matched (same received_at) A/B record pairs.

    Mutates pot_a_records / pot_b_records in place. Returns a summary dict.
    """
    a_index = index_by_timestamp(pot_a_records)
    b_index = index_by_timestamp(pot_b_records)

    swapped = 0
    ambiguous = 0
    unmatched_a = 0
    unmatched_b = 0

    for ts, a_positions in a_index.items():
        b_positions = b_index.get(ts)
        if b_positions is None:
            unmatched_a += len(a_positions)
            continue
        if len(a_positions) != 1 or len(b_positions) != 1:
            # Same timestamp appears more than once in one of the files --
            # can't tell which A record pairs with which B record. Leave
            # alone rather than guess.
            ambiguous += len(a_positions) + len(b_positions)
            continue

        a_rec = pot_a_records[a_positions[0]]
        b_rec = pot_b_records[b_positions[0]]
        a_rec["moisture_pct"], b_rec["moisture_pct"] = b_rec["moisture_pct"], a_rec["moisture_pct"]
        swapped += 1

    for ts, b_positions in b_index.items():
        if ts not in a_index:
            unmatched_b += len(b_positions)

    return {
        "swapped_pairs": swapped,
        "ambiguous_timestamps_skipped": ambiguous,
        "unmatched_in_a_only": unmatched_a,
        "unmatched_in_b_only": unmatched_b,
    }


def write_records(records: list[dict], path: str) -> None:
    with open(path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Swap moisture_pct between matched Pot A / Pot B records (see module docstring)."
    )
    parser.add_argument("pot_a_path", help="pot_a_readings.jsonl path")
    parser.add_argument("pot_b_path", help="pot_b_readings.jsonl path")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="overwrite both input files (after writing timestamped .bak copies). "
        "Default is to write separate <name>.pinfix.jsonl files instead.",
    )
    args = parser.parse_args()

    pot_a_records = load_records(args.pot_a_path)
    pot_b_records = load_records(args.pot_b_path)
    print(f"{args.pot_a_path}: {len(pot_a_records)} records")
    print(f"{args.pot_b_path}: {len(pot_b_records)} records")

    summary = swap_moisture(pot_a_records, pot_b_records)
    print(f"Swapped moisture_pct for {summary['swapped_pairs']} matched A/B pairs.")
    if summary["ambiguous_timestamps_skipped"]:
        print(
            f"WARNING: {summary['ambiguous_timestamps_skipped']} records skipped "
            "(duplicate received_at within one file made pairing ambiguous) -- "
            "consider running dedup_readings.py first."
        )
    if summary["unmatched_in_a_only"]:
        print(
            f"NOTE: {summary['unmatched_in_a_only']} Pot A records had no matching "
            "Pot B timestamp (Pot B's BME688 likely read all-zero that cycle) -- "
            "left untouched, moisture_pct for these is still the wrong pot's value "
            "with no recoverable correct one. Consider excluding from training data."
        )
    if summary["unmatched_in_b_only"]:
        print(
            f"NOTE: {summary['unmatched_in_b_only']} Pot B records had no matching "
            "Pot A timestamp (Pot A's BME688 likely read all-zero that cycle) -- "
            "left untouched, same caveat as above."
        )

    for path, records in ((args.pot_a_path, pot_a_records), (args.pot_b_path, pot_b_records)):
        if args.in_place:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = f"{path}.{timestamp}.bak"
            shutil.copy2(path, backup_path)
            print(f"  -> backed up {path} to {backup_path}")
            tmp_path = f"{path}.tmp"
            write_records(records, tmp_path)
            os.replace(tmp_path, path)
            print(f"  -> wrote pin-swap-corrected data back to {path}")
        else:
            out_path = f"{os.path.splitext(path)[0]}.pinfix.jsonl"
            write_records(records, out_path)
            print(f"  -> wrote pin-swap-corrected data to {out_path} (original left untouched)")


if __name__ == "__main__":
    main()
