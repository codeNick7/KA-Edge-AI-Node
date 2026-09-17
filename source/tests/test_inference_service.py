"""Tests for scripts/inference_service.py's PotService — the background routine that
owns everything source/dashboard/real_source.py used to compute inline (see
test_real_source.py's "pure reader" tests and docs/plan/nn-inference-upgrade-plan.md's
Phase 4 note).

2026-09-16: rewritten for the JSONL -> SQLite migration
(docs/plan/sqlite-storage-migration.md). PotService now reads board_db's `readings`
table and inserts into `inference_results` instead of tailing/appending JSONL files,
and its state tracks the last-processed readings.id instead of a byte offset -- so
the old raw-file-shrink/partial-line-mid-write tests (JSONL-specific failure modes)
no longer apply, and the old zero-BME-row-skipping test doesn't either: main.py now
filters those out before a row ever reaches `readings` (see board_db.insert_reading's
callers), not at inference time.
"""

import importlib.util
import os
import sys
import sqlite3
from datetime import datetime, timedelta, timezone

_FIXTURE_EPOCH = datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc)

# scripts/ isn't a package (no __init__.py, by design — see scripts/build_training_set.py
# and friends, all standalone CLI entry points) so it's loaded by file path, same pattern
# every other scripts/*.py test in this repo would need.
_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts")
_spec = importlib.util.spec_from_file_location("inference_service", os.path.join(_SCRIPTS_DIR, "inference_service.py"))
inference_service = importlib.util.module_from_spec(_spec)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "source"))
_spec.loader.exec_module(inference_service)

import board_db  # noqa: E402 -- inference_service's own module-exec above already put
                  # KA-Edge-source/python on sys.path (appended, not inserted -- see that
                  # file's sys.path search loop).
from edge_node.trained_model import MODEL_VERSION  # noqa: E402

TREND_WINDOW = inference_service.TREND_WINDOW


def _seed_readings(db_path, pot, n=12, start_index=1, start_ts=None):
    """Insert readings with sample_index/gas_kohm/temp_c/timestamp continuing
    from start_index -- so seeding in two calls (start_index=1..6, then
    start_index=7..12) produces byte-identical rows to one call (1..12),
    which test_incremental_matches_full_recompute relies on."""
    base_temp = {"A": 24.0, "B": 28.0}[pot]
    start = start_ts if start_ts is not None else _FIXTURE_EPOCH
    with board_db.connect(db_path) as conn:
        for offset in range(n):
            i = start_index + offset
            board_db.insert_reading(conn, pot, {
                "sample_index": i, "millis_ms": i * 1000,
                "temp_c": round(base_temp + i * 0.1, 1), "humidity_pct": 55.0,
                "pressure_hpa": 1013.0, "gas_kohm": 10.0 + i,
                "light_lux": 0.0, "moisture_pct": 2.0,  # dry desk reading
                "received_at": (start + timedelta(seconds=i)).isoformat(),
            })


def _fetch_inference_rows(db_path, pot):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM inference_results WHERE pot_id = ? ORDER BY id", (pot,)
        ).fetchall()
    return [dict(r) for r in rows]


def _make_service(tmp_path, pot="A", db_name="sensor_data.db"):
    db_path = str(tmp_path / db_name)
    state = tmp_path / f".inference_service_state_{pot.lower()}.json"
    return inference_service.PotService(pot, db_path, str(state)), db_path, state


def test_run_once_writes_expected_shape(tmp_path):
    svc, db_path, _ = _make_service(tmp_path)
    _seed_readings(db_path, "A", n=12)

    n_written = svc.run_once()
    assert n_written == 12 - (TREND_WINDOW - 1)

    rows = _fetch_inference_rows(db_path, "A")
    assert len(rows) == n_written
    for field in ("timestamp", "rsmi", "light_delta", "gas_index_trend",
                  "rsmi_trend", "flag", "confidence", "contributing_trend", "model_version"):
        assert field in rows[0]
    assert rows[0]["model_version"] == MODEL_VERSION


def test_dry_reading_not_waterlogged(tmp_path):
    """moisture_pct=2% (dry desk) must never produce waterlogged_risk."""
    svc, db_path, _ = _make_service(tmp_path)
    _seed_readings(db_path, "A", n=12)
    svc.run_once()

    rows = _fetch_inference_rows(db_path, "A")
    for row in rows:
        assert row["flag"] != "waterlogged_risk", f"dry reading wrongly classified: {row}"


def test_incremental_matches_full_recompute(tmp_path):
    """Processing in two run_once() calls (simulating a live poll loop) must
    produce the same final inference_results as one call over the full backlog."""
    baseline_svc, baseline_db, _ = _make_service(tmp_path / "baseline", db_name="sensor_data.db")
    _seed_readings(baseline_db, "A", n=12)
    baseline_svc.run_once()
    expected = _fetch_inference_rows(baseline_db, "A")

    inc_svc, inc_db, _ = _make_service(tmp_path / "incremental", db_name="sensor_data.db")
    _seed_readings(inc_db, "A", n=6, start_index=1)
    inc_svc.run_once()  # first poll
    _seed_readings(inc_db, "A", n=6, start_index=7)
    inc_svc.run_once()  # second poll picks up the rest

    actual = _fetch_inference_rows(inc_db, "A")
    # Compare everything except the autoincrement id/reading_id, which legitimately
    # differ between the two independent databases.
    strip = lambda rows: [{k: v for k, v in r.items() if k not in ("id", "reading_id")} for r in rows]
    assert strip(actual) == strip(expected)


def test_state_persists_across_restart(tmp_path, monkeypatch):
    """A fresh PotService on the same db/state paths (simulating a process
    restart) must resume from the persisted state file instead of reprocessing
    rows already in inference_results."""
    svc, db_path, state = _make_service(tmp_path)
    _seed_readings(db_path, "A", n=12)
    n_first = svc.run_once()
    assert n_first == 12 - (TREND_WINDOW - 1)
    assert state.exists()

    call_count = {"n": 0}
    real_predict = inference_service.trained_predict

    def _counting_predict(*args, **kwargs):
        call_count["n"] += 1
        return real_predict(*args, **kwargs)

    monkeypatch.setattr(inference_service, "trained_predict", _counting_predict)

    restarted, _, _ = _make_service(tmp_path)  # new instance, same paths -- loads state from disk
    n_second = restarted.run_once()
    assert n_second == 0  # nothing new since the state file already covers everything written
    assert call_count["n"] == 0


def test_only_new_rows_processed_after_restart(tmp_path, monkeypatch):
    svc, db_path, _ = _make_service(tmp_path)
    _seed_readings(db_path, "A", n=12)
    svc.run_once()

    _seed_readings(db_path, "A", n=3, start_index=13)  # 3 new rows

    call_count = {"n": 0}
    real_predict = inference_service.trained_predict

    def _counting_predict(*args, **kwargs):
        call_count["n"] += 1
        return real_predict(*args, **kwargs)

    monkeypatch.setattr(inference_service, "trained_predict", _counting_predict)

    restarted, _, _ = _make_service(tmp_path)
    n_new = restarted.run_once()
    assert n_new == 3
    assert call_count["n"] == 3


def test_run_once_loops_across_multiple_batches(tmp_path, monkeypatch):
    """A backlog larger than one BATCH_SIZE must still be fully processed (and
    checkpointed) within a single run_once() call, matching the old
    CHECKPOINT_EVERY behavior's guarantee for a large first backfill."""
    monkeypatch.setattr(inference_service, "BATCH_SIZE", 3)
    svc, db_path, _ = _make_service(tmp_path)
    _seed_readings(db_path, "A", n=20)

    n_written = svc.run_once()
    assert n_written == 20 - (TREND_WINDOW - 1)
    assert len(_fetch_inference_rows(db_path, "A")) == n_written

    # A second call with nothing new must be a no-op, proving the multi-batch
    # loop persisted last_reading_id correctly at every checkpoint, not just at
    # the very end.
    assert svc.run_once() == 0


def test_pots_are_independent_in_shared_db(tmp_path):
    """Both pots share one sensor_data.db (pot_id column) -- a PotService for
    one pot must never process or count the other pot's rows."""
    db_path = str(tmp_path / "sensor_data.db")
    _seed_readings(db_path, "A", n=12)
    _seed_readings(db_path, "B", n=12)
    svc_a, _, _ = _make_service(tmp_path, pot="A")
    n_a = svc_a.run_once()
    assert n_a == 12 - (TREND_WINDOW - 1)
    assert _fetch_inference_rows(db_path, "B") == []  # B untouched by A's service
