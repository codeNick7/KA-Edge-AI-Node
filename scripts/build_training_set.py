#!/usr/bin/env python3
"""Build a labeled training set from real Pot A/B sensor history (PRD Section 5, steps 3-4).

Section 5 step 1 ("seed labels from published thresholds") has been running live since day
one -- that's edge_node.decision_tree.classify(), already producing a flag for every dashboard
reading. What's been missing is steps 3-4: turning the *real* collected pot_a/pot_b readings
into an actual (features, label) training set, and recording where your own observed ground
truth (e.g. "visible wilting started here") should override the uncalibrated seed label rather
than just noting it in a doc afterward.

This script:
  1. Re-runs the exact same feature pipeline dashboard/real_source.py uses live
     (compute_rsmi / compute_trend_slope / compute_light_delta / check_validity / classify,
     same TREND_WINDOW=7 and TEXTURE="clay_loam") over the real pot_a_readings.jsonl /
     pot_b_readings.jsonl files, producing a seed_label per row.
  2. Optionally applies an --observations file of timestamp-range overrides, so rows in a
     window you've marked ("Pot B was visibly wilting from this timestamp on") get your
     observed label instead of the seed one.
  3. Writes one combined, chronologically-sorted training_set.jsonl with both seed_label and
     the final label kept side by side -- label provenance (Section 5 step 4) is then a
     per-row fact, not a documentation footnote.

Observations file (--observations, JSON): a list of override windows, e.g.

  [
    {
      "pot": "B",
      "start": "2026-08-15T14:00:00+00:00",
      "end": null,
      "label": "moisture_low",
      "note": "visible wilting first observed"
    }
  ]

`end: null` means "from start onward" (until a later window for the same pot takes effect, if
any -- the window with the latest `start` <= a row's timestamp wins). Valid `label` values are
decision_tree.classify()'s leaf flags: sensor_fault, drought_onset, moisture_low,
waterlogged_risk, soil_health_degrading, stable.

Usage (stdlib + the edge_node package only, no dashboard/Flask dependency):
  .venv/bin/python scripts/build_training_set.py \\
      pot_a_readings.jsonl pot_b_readings.jsonl \\
      --observations observations.json \\
      --out training_set.jsonl
"""

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "source"))

from edge_node.decision_tree import classify  # noqa: E402
from edge_node.features import compute_light_delta, compute_rsmi, compute_trend_slope  # noqa: E402
from edge_node.validity import check_validity  # noqa: E402

# Must match dashboard/real_source.py's TREND_WINDOW / TEXTURE exactly -- these feed
# classify() the same way the live dashboard does, so seed labels here match what's already
# been shown on screen rather than silently diverging from it.
TREND_WINDOW = 7
TEXTURE = "clay_loam"
PLACEHOLDER_LIGHT_LUX = 400.0

VALID_LABELS = {
    "sensor_fault",
    "drought_onset",
    "moisture_low",
    "waterlogged_risk",
    "soil_health_degrading",
    "stable",
}


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
    records.sort(key=lambda r: r.get("received_at", ""))
    return records


def load_observations(path: str | None) -> list[dict]:
    if not path:
        return []
    with open(path) as f:
        raw = json.load(f)
    windows = []
    for i, entry in enumerate(raw):
        pot = entry.get("pot")
        start = entry.get("start")
        label = entry.get("label")
        if pot not in ("A", "B"):
            raise ValueError(f"observations[{i}]: 'pot' must be 'A' or 'B', got {pot!r}")
        if not start:
            raise ValueError(f"observations[{i}]: 'start' is required")
        if label not in VALID_LABELS:
            raise ValueError(f"observations[{i}]: label {label!r} not in {sorted(VALID_LABELS)}")
        end = entry.get("end")
        if end is not None and end <= start:
            raise ValueError(f"observations[{i}]: 'end' ({end}) must be after 'start' ({start})")
        windows.append({"pot": pot, "start": start, "end": end, "label": label, "note": entry.get("note", "")})
    return windows


def observed_label(pot: str, timestamp: str, windows: list[dict]) -> tuple[str | None, str | None]:
    """Return (label, note) from the latest-starting matching window, or (None, None)."""
    match = None
    for w in windows:
        if w["pot"] != pot:
            continue
        if timestamp < w["start"]:
            continue
        if w["end"] is not None and timestamp >= w["end"]:
            continue
        if match is None or w["start"] > match["start"]:
            match = w
    if match is None:
        return None, None
    return match["label"], match["note"]


def build_rows(pot: str, records: list[dict], windows: list[dict]) -> list[dict]:
    rows = []
    for i in range(TREND_WINDOW - 1, len(records)):
        history = records[i - TREND_WINDOW + 1 : i + 1]
        rec = records[i]
        prev_moisture = records[i - 1].get("moisture_pct") if i > 0 else None

        moisture = rec.get("moisture_pct", 0.0)
        gas = rec.get("gas_kohm", 0.0)
        temp = rec.get("temp_c", 0.0)

        validity = check_validity(moisture, gas, temp, prev_moisture)
        rsmi = compute_rsmi(moisture, TEXTURE)
        rsmi_history = [compute_rsmi(r.get("moisture_pct", 0.0), TEXTURE) for r in history]
        light_history = [r.get("light_lux", PLACEHOLDER_LIGHT_LUX) for r in history]
        gas_history = [r.get("gas_kohm", 0.0) for r in history]

        light_delta = compute_light_delta(light_history)
        gas_index_trend = compute_trend_slope(gas_history)
        rsmi_trend = compute_trend_slope(rsmi_history)

        flag = classify(validity, rsmi, light_delta, gas_index_trend, rsmi_trend)

        ts = rec.get("received_at", "")
        obs_label, obs_note = observed_label(pot, ts, windows)

        rows.append(
            {
                "pot": pot,
                "timestamp": ts,
                "rsmi": round(rsmi, 4),
                "light_delta": round(light_delta, 2),
                "gas_index_trend": round(gas_index_trend, 4),
                "rsmi_trend": round(rsmi_trend, 4),
                "seed_label": flag.flag,
                "seed_confidence": round(flag.confidence, 3),
                "label": obs_label if obs_label is not None else flag.flag,
                "label_source": "observed" if obs_label is not None else "seed",
                "observation_note": obs_note or "",
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a labeled training set from real Pot A/B sensor history (PRD Section 5 steps 3-4)."
    )
    parser.add_argument("pot_a_path", help="pot_a_readings.jsonl path")
    parser.add_argument("pot_b_path", help="pot_b_readings.jsonl path")
    parser.add_argument("--observations", help="JSON file of observed-label override windows")
    parser.add_argument("--out", default="training_set.jsonl", help="output path (default: training_set.jsonl)")
    args = parser.parse_args()

    windows = load_observations(args.observations)
    if args.observations:
        print(f"{args.observations}: {len(windows)} observation window(s)")

    all_rows = []
    for pot, path in (("A", args.pot_a_path), ("B", args.pot_b_path)):
        records = load_records(path)
        print(f"{path}: {len(records)} records")
        if len(records) < TREND_WINDOW:
            print(f"  -> fewer than TREND_WINDOW={TREND_WINDOW} records, no rows produced")
            continue
        rows = build_rows(pot, records, windows)
        n_observed = sum(1 for r in rows if r["label_source"] == "observed")
        print(f"  -> {len(rows)} labeled rows ({n_observed} from observations, {len(rows) - n_observed} seed-only)")
        all_rows.extend(rows)

    all_rows.sort(key=lambda r: r["timestamp"])

    with open(args.out, "w") as f:
        for row in all_rows:
            f.write(json.dumps(row) + "\n")
    print(f"Wrote {len(all_rows)} labeled rows to {args.out}")

    print("Final label distribution:", dict(Counter(r["label"] for r in all_rows)))
    print("Seed label distribution:", dict(Counter(r["seed_label"] for r in all_rows)))


if __name__ == "__main__":
    main()
