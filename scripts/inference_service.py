#!/usr/bin/env python3
"""Standalone background inference service.

This is the "system routine that runs all the time" — it owns computing inference
(validity gate + feature engineering + trained_predict) from the board_db.readings
table and inserting results into board_db.inference_results.
The dashboard (source/dashboard/real_source.py) is now a pure reader of that table --
it no longer computes anything itself (see docs/plan/nn-inference-upgrade-plan.md's
Phase 4 note and this session's follow-up decision to keep the service split).

2026-09-16: migrated off pot_{a,b}_readings.jsonl / pot_{a,b}_inference_cache.jsonl
onto board_db's SQLite schema (see docs/plan/sqlite-storage-migration.md) -- unbounded
JSONL growth was filling the board's disk. Reads/writes now go through board_db
instead of tailing/appending flat files; state tracks the last processed readings.id
instead of a raw-file byte offset.

Two modes:
  --once   One backfill pass (looping internally until caught up), then exit. Used
           for local dev/testing (e.g. against a data-from-board/sept15/ copy with
           its own sensor_data.db) and to bootstrap inference_results on first deploy.
  --watch  Runs forever, polling for new rows every --interval seconds. This is the
           actual standing routine -- start it with e.g.
           `nohup .venv/bin/python scripts/inference_service.py --watch &` (or a systemd
           unit / cron @reboot line, once the board's init system is confirmed) and leave
           it running independently of the dashboard process.

State (a small JSON file per pot, `.inference_service_state_<pot>.json` next to the
db) tracks the last-processed readings.id and the trailing TREND_WINDOW-1 rows needed
for the next trend computation -- so a restart resumes from where it left off instead
of either reprocessing from scratch or re-scanning the whole table for its max id.

Usage:
  .venv/bin/python scripts/inference_service.py --data-dir data-from-board/sept15/data --once
  .venv/bin/python scripts/inference_service.py --data-dir /path/to/board/data --watch --interval 5
"""

import argparse
import json
import os
import sqlite3
import sys
import time

# Locate the directories containing edge_node/ and board_db.py relative to this file.
# Layouts supported: the repo's own (scripts/inference_service.py, edge_node under
# ../source, board_db.py under ../KA-Edge-source/python) and the board's deployed
# layout (scripts/deploy_dashboard_to_board.sh places this file directly alongside
# both edge_node/ and board_db.py, as siblings -- see that script's comments).
_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (_HERE, os.path.join(_HERE, "..", "source")):
    if os.path.isdir(os.path.join(_candidate, "edge_node")):
        sys.path.insert(0, _candidate)
        break
else:
    raise ImportError(f"could not locate edge_node/ near {_HERE!r} (checked it and ../source)")

for _candidate in (_HERE, os.path.join(_HERE, "..", "KA-Edge-source", "python")):
    if os.path.isfile(os.path.join(_candidate, "board_db.py")):
        # append, not insert(0): that directory's own edge_node/ (the board-deployed
        # runtime subset -- see deploy_dashboard_to_board.sh) would otherwise shadow
        # the fuller source/edge_node/ package just resolved above.
        sys.path.append(_candidate)
        break
else:
    raise ImportError(f"could not locate board_db.py near {_HERE!r} (checked it and ../KA-Edge-source/python)")

import board_db  # noqa: E402
from edge_node.features import compute_light_delta, compute_rsmi, compute_trend_slope  # noqa: E402
from edge_node.trained_model import MODEL_VERSION, trained_predict  # noqa: E402
from edge_node.validity import check_validity  # noqa: E402

TEXTURE = "clay_loam"  # must match dashboard/real_source.py's historical constant
TREND_WINDOW = 7  # must match build_training_set.py / the trained model's training pipeline

# Rows fetched/processed per batch within a single run_once() call, and the interval
# between state-file checkpoints -- bounds how much a crash/power-loss mid-backlog can
# lose or duplicate, and gives visible progress on a long first backfill instead of
# silence (same rationale the old byte-offset version had for CHECKPOINT_EVERY).
BATCH_SIZE = 2000

PLACEHOLDER_LIGHT_LUX = 400.0


def _to_row(record: dict) -> dict:
    return {
        "timestamp": record["received_at"],
        "temp_c": record["temp_c"],
        "moisture_pct": record.get("moisture_pct", 0.0),
        "gas_index": record["gas_kohm"],
        "light_lux": record.get("light_lux", PLACEHOLDER_LIGHT_LUX),
    }


def _compute_inference_row(row: dict, history: list[dict], prev_moisture: float | None) -> dict:
    """Identical math to the inline version real_source.py used to run per-request —
    relocated, not rewritten, so inference_results' schema/semantics don't change
    under the dashboard."""
    validity = check_validity(row["moisture_pct"], row["gas_index"], row["temp_c"], prev_moisture)
    rsmi = compute_rsmi(row["moisture_pct"], TEXTURE)
    rsmi_history = [compute_rsmi(r["moisture_pct"], TEXTURE) for r in history]
    light_history = [r["light_lux"] for r in history]
    gas_history = [r["gas_index"] for r in history]

    light_delta = compute_light_delta(light_history)
    gas_index_trend = compute_trend_slope(gas_history)
    rsmi_trend = compute_trend_slope(rsmi_history)

    if validity.is_valid:
        flag_label, flag_confidence = trained_predict(rsmi, light_delta, gas_index_trend, rsmi_trend)
    else:
        flag_label, flag_confidence = "sensor_fault", validity.validity_score

    if rsmi_trend > 1e-4:
        contributing_trend = "moisture_up"
    elif rsmi_trend < -1e-4:
        contributing_trend = "moisture_down"
    else:
        contributing_trend = "moisture_flat"

    return {
        "timestamp": row["timestamp"],
        "rsmi": round(rsmi, 4),
        "light_delta": round(light_delta, 2),
        "gas_index_trend": round(gas_index_trend, 4),
        "rsmi_trend": round(rsmi_trend, 4),
        "flag": flag_label,
        "confidence": round(flag_confidence, 3),
        "contributing_trend": contributing_trend,
        "model_version": MODEL_VERSION,
    }


class PotService:
    def __init__(self, pot: str, db_path: str, state_path: str):
        self.pot = pot  # "A" | "B" -- matches board_db's pot_id values
        self.db_path = db_path
        self.state_path = state_path
        self.last_reading_id, self.window = self._load_state()
        self._reset_if_stale()

    def _load_state(self) -> tuple[int, list[dict]]:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path) as f:
                    state = json.load(f)
                return state.get("last_reading_id", 0), state.get("window", [])
            except (json.JSONDecodeError, OSError):
                pass
        return 0, []

    def _reset_if_stale(self) -> None:
        """Guard against a resume pointer that no longer refers to any row in
        `readings` -- e.g. sensor_data.db got rebuilt/recreated (AUTOINCREMENT
        restarting from 1) while this pot's state file was left pointing at the
        old, now-nonexistent id range from before the rebuild.

        Real incident (2026-09-25): exactly this happened -- state file had
        last_reading_id=2894204 from a prior db incarnation, the live db's
        current MAX(id) was 59478, so `_fetch_batch()`'s `WHERE id > ?` matched
        zero rows on every single poll cycle, forever, with no error and no
        crash -- inference_results silently stayed empty indefinitely, with
        nothing in the process's own behavior (still "running", still using
        some CPU) to indicate anything was wrong. If the pointer is beyond what
        currently exists, there is nothing valid to resume from -- rewind to 0
        (full backfill from scratch, same as a first-ever run) rather than
        silently never making progress again.
        """
        if self.last_reading_id <= 0:
            return
        try:
            with board_db.connect(self.db_path) as conn:
                max_id = conn.execute(
                    "SELECT MAX(id) FROM readings WHERE pot_id = ?", (self.pot,)
                ).fetchone()[0]
        except sqlite3.OperationalError:
            return  # db not reachable yet -- let the normal poll-cycle retry path handle it
        if max_id is not None and self.last_reading_id > max_id:
            print(
                f"[{self.pot}] WARN: resume pointer last_reading_id={self.last_reading_id} is "
                f"beyond the live table's MAX(id)={max_id} -- sensor_data.db was likely rebuilt "
                f"since this state file was last written. Resetting to 0 (full backfill) instead "
                f"of silently never catching up.",
                flush=True,
            )
            self.last_reading_id, self.window = 0, []
            self._save_state()

    def _save_state(self) -> None:
        tmp_path = self.state_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump({"last_reading_id": self.last_reading_id, "window": self.window}, f)
        os.replace(tmp_path, self.state_path)  # atomic — a crash mid-write can't corrupt the real state file

    def _fetch_batch(self) -> list[sqlite3.Row]:
        with board_db.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM readings WHERE pot_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                (self.pot, self.last_reading_id, BATCH_SIZE),
            ).fetchall()

    def _insert_results(self, results: list[tuple[int, dict]]) -> None:
        if not results:
            return
        with board_db.connect(self.db_path) as conn:
            for reading_id, result in results:
                board_db.insert_inference_result(conn, self.pot, reading_id, result)

    def run_once(self) -> int:
        """Process every not-yet-inferred reading for this pot, looping internally
        in BATCH_SIZE chunks until caught up. Checkpoints (inserts results + persists
        last_reading_id/window state) after every batch -- a single call can cover a
        very large backlog (e.g. a fresh state file against days of accumulated
        readings), and without incremental checkpointing, any failure partway through
        loses ALL progress from this call. Returns the total number of new inference
        results written."""
        window = list(self.window)
        n_written = 0

        while True:
            rows = self._fetch_batch()
            if not rows:
                break

            batch_results = []
            for row in rows:
                rec = dict(row)
                row_for_calc = _to_row(rec)
                prev_moisture = window[-1]["moisture_pct"] if window else None
                window.append(row_for_calc)
                if len(window) > TREND_WINDOW:
                    window = window[-TREND_WINDOW:]
                if len(window) >= TREND_WINDOW:
                    result = _compute_inference_row(row_for_calc, window, prev_moisture)
                    batch_results.append((row["id"], result))
                self.last_reading_id = row["id"]

            self._insert_results(batch_results)
            n_written += len(batch_results)
            self.window = window[-(TREND_WINDOW - 1):]
            self._save_state()
            if len(rows) == BATCH_SIZE:
                print(f"[{self.pot}] checkpoint: {n_written} result(s) so far this run", flush=True)
            if len(rows) < BATCH_SIZE:
                break

        return n_written


def build_services(data_dir: str) -> list[PotService]:
    db_path = os.path.join(data_dir, "sensor_data.db")
    services = []
    for pot in ("A", "B"):
        state_path = os.path.join(data_dir, f".inference_service_state_{pot.lower()}.json")
        services.append(PotService(pot, db_path, state_path))
    return services


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", required=True, help="directory containing sensor_data.db")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="one backfill pass, then exit")
    mode.add_argument("--watch", action="store_true", help="run forever, polling every --interval seconds")
    parser.add_argument("--interval", type=float, default=5.0, help="poll interval in seconds for --watch (default: 5)")
    args = parser.parse_args()

    services = build_services(args.data_dir)

    if args.once:
        for svc in services:
            n = _run_once_resilient(svc)
            print(f"[{svc.pot}] wrote {n} new inference result(s)")
        return

    print(f"[inference_service] watching {args.data_dir} every {args.interval}s (Ctrl+C to stop)")
    try:
        while True:
            for svc in services:
                n = _run_once_resilient(svc)
                if n:
                    print(f"[{svc.pot}] wrote {n} new inference result(s)")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[inference_service] stopped")


def _run_once_resilient(svc: "PotService") -> int:
    """svc.run_once(), but any exception (not just sqlite3.OperationalError -- see
    2026-09-25 widening below) is logged and swallowed instead of killing the whole
    watch process for every pot.

    2026-09-17 incident: an uncaught sqlite3.OperationalError ("database is locked"
    -- main.py and this script are two independent processes writing to the same
    file concurrently, see board_db.py's _CONNECT_TIMEOUT_S comment) crashed
    inference_service.py mid-backfill on pot A -- pot B never got a single
    run_once() call afterward because the process had already exited. Originally
    fixed by catching only that specific exception type.

    2026-09-25 widening: that narrow catch still leaves the exact same failure mode
    open for ANY other exception (a bad/edge-case reading, an unexpected value
    reaching trained_predict/feature computation, etc.) -- it would just as
    silently kill the whole --watch loop for both pots, with no supervisor process
    to restart it (see nn-inference-upgrade-plan.md's "not yet required" note on a
    real process supervisor). PotService.run_once()'s own per-batch checkpointing
    (state saved only after a batch's insert fully commits) already makes a failed
    batch safe to simply retry on the next poll cycle -- nothing here needs its own
    retry loop, just not dying, for any exception, not just the one type that
    happened to cause the last incident.
    """
    try:
        return svc.run_once()
    except Exception as e:  # noqa: BLE001 -- a bug in one pot's batch must never take down the other
        print(f"[{svc.pot}] WARN: run_once() failed ({type(e).__name__}: {e}) -- safe to retry "
              f"(--watch will retry next poll cycle automatically; --once needs to be re-run)", flush=True)
        return 0


if __name__ == "__main__":
    main()
