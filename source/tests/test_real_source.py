"""Tests for RealDataSource (dual-pot, board_db SQLite schema).

2026-09-16: rewritten for the JSONL -> SQLite migration
(docs/plan/sqlite-storage-migration.md). RealDataSource now reads
board_db's readings/inference_results/raw_daily_rollup tables directly via
SQL instead of tailing/streaming two ever-growing JSONL files, so the old
byte-offset tail-cache/partial-line-recovery/out-of-order-reordering/
file-shrink-detection/OOM-bound-cache-size tests no longer apply -- SQL
queries never hold more than the requested result set in memory, and there's
no shared mutable cache left to race on. Behavioral guarantees that still
matter (range filtering, decimation, missing-pot/missing-db handling, status
counts) are re-verified here against a real board_db-created SQLite file.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "KA-Edge-source", "python"))
import board_db  # noqa: E402

from dashboard.real_source import PLACEHOLDER_LIGHT_LUX, RealDataSource  # noqa: E402

_FIXTURE_EPOCH = datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc)
_MODEL_VERSION = "test-model-v1"


def _seed_readings(db_path, pot, n=12, start_ts=None, base_temp=24.0):
    """Insert n readings rows for pot, one per second from start_ts (default
    _FIXTURE_EPOCH). Mirrors the old _write_fixture's per-row shape."""
    start = start_ts or _FIXTURE_EPOCH
    with board_db.connect(db_path) as conn:
        for i in range(1, n + 1):
            board_db.insert_reading(conn, pot, {
                "sample_index": i, "millis_ms": i * 1000,
                "temp_c": round(base_temp + i * 0.1, 1),
                "humidity_pct": 55.0, "pressure_hpa": 1013.0,
                "gas_kohm": 10.0 + i, "light_lux": 0.0, "moisture_pct": 2.0,
                "received_at": (start + timedelta(seconds=i)).isoformat(),
            })


def _seed_readings_at(db_path, pot, timestamps):
    """One reading per given ISO timestamp -- used where exact cutoff
    boundaries matter."""
    with board_db.connect(db_path) as conn:
        for i, ts in enumerate(timestamps, start=1):
            board_db.insert_reading(conn, pot, {
                "sample_index": i, "millis_ms": i * 1000,
                "temp_c": 20.0 + i * 0.01, "humidity_pct": 55.0, "pressure_hpa": 1013.0,
                "gas_kohm": 10.0, "light_lux": 100.0, "moisture_pct": 5.0,
                "received_at": ts,
            })


def _seed_inference(db_path, pot, n=12, start_ts=None):
    """Insert n inference_results rows for pot -- the shape
    scripts/inference_service.py's PotService writes, NOT computed here (this
    file is a pure reader; test_inference_service.py covers computation)."""
    start = start_ts or _FIXTURE_EPOCH
    with board_db.connect(db_path) as conn:
        for i in range(1, n + 1):
            board_db.insert_inference_result(conn, pot, None, {
                "timestamp": (start + timedelta(seconds=i)).isoformat(),
                "rsmi": round(0.5 + i * 0.001, 4), "light_delta": 0.0,
                "gas_index_trend": 0.0, "rsmi_trend": 0.0, "flag": "stable",
                "confidence": 0.9, "contributing_trend": "moisture_flat",
                "model_version": _MODEL_VERSION,
            })


def _db(tmp_path):
    return str(tmp_path / "sensor_data.db")


# ── Pot A / B basics ─────────────────────────────────────────────────────────

def test_get_raw_pot_a_returns_expected_shape(tmp_path):
    db_path = _db(tmp_path)
    _seed_readings(db_path, "A", n=5, base_temp=24.0)
    _seed_readings(db_path, "B", n=5, base_temp=28.0)
    source = RealDataSource(db_path=db_path)

    rows = source.get_raw("A", 3)
    assert len(rows) == 3
    for field in ("timestamp", "moisture_pct", "gas_index", "temp_c", "light_lux",
                  "humidity_pct", "pressure_hpa"):
        assert field in rows[0], f"missing field {field!r} in row"
    assert [row["temp_c"] for row in rows] == [24.3, 24.4, 24.5]


def test_get_raw_pot_b_returns_expected_shape(tmp_path):
    db_path = _db(tmp_path)
    _seed_readings(db_path, "A", n=5, base_temp=24.0)
    _seed_readings(db_path, "B", n=5, base_temp=28.0)
    source = RealDataSource(db_path=db_path)

    rows = source.get_raw("B", 3)
    assert len(rows) == 3
    assert [row["temp_c"] for row in rows] == [28.3, 28.4, 28.5]


def test_get_raw_pot_b_independent_of_pot_a(tmp_path):
    db_path = _db(tmp_path)
    _seed_readings(db_path, "A", n=5, base_temp=24.0)
    _seed_readings(db_path, "B", n=5, base_temp=28.0)
    source = RealDataSource(db_path=db_path)

    rows_a = source.get_raw("A", 1)
    rows_b = source.get_raw("B", 1)
    assert rows_a[0]["temp_c"] != rows_b[0]["temp_c"]


def test_light_lux_placeholder_fallback(tmp_path):
    """light_lux is NOT NULL in the schema -- board_db.insert_reading applies
    PLACEHOLDER_LIGHT_LUX at insert time for a caller that omits it (pre-v6.0
    JSONL rows had no light_lux field at all; migrate_jsonl_to_sqlite.py backfills
    those the same way), so a real_source.py read never needs its own fallback."""
    db_path = _db(tmp_path)
    with board_db.connect(db_path) as conn:
        board_db.insert_reading(conn, "A", {
            "sample_index": 1, "millis_ms": 1000, "temp_c": 24.0, "humidity_pct": 55.0,
            "pressure_hpa": 1013.0, "gas_kohm": 11.0, "moisture_pct": 2.0,
            "received_at": "2026-08-13T10:00:01+00:00",
            # light_lux omitted entirely
        })

    source = RealDataSource(db_path=db_path)
    rows = source.get_raw("A", 1)
    assert rows[0]["light_lux"] == PLACEHOLDER_LIGHT_LUX == board_db.PLACEHOLDER_LIGHT_LUX


# ── Inference (pure reader) ──────────────────────────────────────────────────

def test_get_inference_reads_table_both_pots(tmp_path):
    db_path = _db(tmp_path)
    _seed_readings(db_path, "A", n=12)
    _seed_readings(db_path, "B", n=12)
    _seed_inference(db_path, "A", n=12)
    _seed_inference(db_path, "B", n=12)
    source = RealDataSource(db_path=db_path)

    for pot in ("A", "B"):
        rows = source.get_inference(pot, 5)
        assert len(rows) == 5, f"expected 5 inference rows for Pot {pot}"
        for field in ("timestamp", "rsmi", "light_delta", "gas_index_trend",
                      "rsmi_trend", "flag", "confidence", "contributing_trend", "model_version"):
            assert field in rows[0], f"missing field {field!r} in Pot {pot} inference"
        assert rows[0]["model_version"] == _MODEL_VERSION


def test_get_inference_missing_results_returns_empty(tmp_path):
    """No inference rows yet (inference_service.py hasn't caught up, or hasn't
    started) -- must return [], not raise."""
    db_path = _db(tmp_path)
    _seed_readings(db_path, "A", n=5)
    source = RealDataSource(db_path=db_path)

    assert source.get_inference("A", 5) == []


def test_get_inference_picks_up_rows_inserted_by_another_process(tmp_path):
    """inference_results is written by an independent process
    (scripts/inference_service.py) that may insert between two calls on the
    same RealDataSource instance -- must be visible without needing a new
    RealDataSource (there's no cache left to go stale, but this pins that
    guarantee explicitly)."""
    db_path = _db(tmp_path)
    _seed_readings(db_path, "A", n=12)
    _seed_inference(db_path, "A", n=6)
    source = RealDataSource(db_path=db_path)

    first = source.get_inference("A", 0)
    assert len(first) == 6

    _seed_inference(db_path, "A", n=6, start_ts=_FIXTURE_EPOCH + timedelta(seconds=100))

    second = source.get_inference("A", 0)
    assert len(second) == 12
    assert second[:6] == first


# ── Status / SD card space ─────────────────────────────────────────────────

def test_get_status_without_sd_status_file(tmp_path):
    db_path = _db(tmp_path)
    _seed_readings(db_path, "A", n=2)
    source = RealDataSource(db_path=db_path, sd_status_path=str(tmp_path / "no_sd_status.json"))
    status = source.get_status()
    assert status["sd_used_mb"] is None
    assert status["sd_free_mb"] is None
    assert status["sd_total_mb"] is None


def test_get_status_reads_sd_status_file(tmp_path):
    db_path = _db(tmp_path)
    sd_status = tmp_path / "sd_status.json"
    sd_status.write_text(
        '{"sd_used_mb": 12.4, "sd_free_mb": 1987.6, "sd_total_mb": 2000.0, '
        '"updated_at": "2026-08-15T09:00:00+00:00"}'
    )
    source = RealDataSource(db_path=db_path, sd_status_path=str(sd_status))
    status = source.get_status()
    assert status["sd_used_mb"] == 12.4
    assert status["sd_free_mb"] == 1987.6
    assert status["sd_total_mb"] == 2000.0
    assert status["sd_status_updated_at"] == "2026-08-15T09:00:00+00:00"


def test_get_status_without_board_disk_status_file(tmp_path):
    db_path = _db(tmp_path)
    source = RealDataSource(db_path=db_path, board_disk_status_path=str(tmp_path / "no_disk_status.json"))
    status = source.get_status()
    assert status["board_disk_free_mb"] is None
    assert status["board_disk_low"] is False


def test_get_status_reports_low_disk_warning_under_threshold(tmp_path):
    db_path = _db(tmp_path)
    disk_status = tmp_path / "board_disk_status.json"
    disk_status.write_text(
        '{"board_disk_used_mb": 9500.0, "board_disk_free_mb": 300.0, "board_disk_total_mb": 9800.0, '
        '"updated_at": "2026-09-16T09:00:00+00:00"}'
    )
    source = RealDataSource(db_path=db_path, board_disk_status_path=str(disk_status))
    status = source.get_status()
    assert status["board_disk_free_mb"] == 300.0
    assert status["board_disk_low"] is True


def test_get_status_no_warning_when_disk_above_threshold(tmp_path):
    db_path = _db(tmp_path)
    disk_status = tmp_path / "board_disk_status.json"
    disk_status.write_text(
        '{"board_disk_used_mb": 6700.0, "board_disk_free_mb": 2700.0, "board_disk_total_mb": 9800.0, '
        '"updated_at": "2026-09-16T09:00:00+00:00"}'
    )
    source = RealDataSource(db_path=db_path, board_disk_status_path=str(disk_status))
    status = source.get_status()
    assert status["board_disk_free_mb"] == 2700.0
    assert status["board_disk_low"] is False


def test_get_status_ignores_malformed_sd_status_file(tmp_path):
    db_path = _db(tmp_path)
    sd_status = tmp_path / "sd_status.json"
    sd_status.write_text("{not valid json")
    source = RealDataSource(db_path=db_path, sd_status_path=str(sd_status))
    status = source.get_status()  # must not raise
    assert status["sd_used_mb"] is None


def test_get_status_blends_live_and_rolled_up_rows(tmp_path):
    """Rows older than board_db.RETENTION_DAYS get rolled into
    raw_daily_rollup and deleted from `readings` (see board_db.prune_old) --
    total counts and collecting_since must still reflect the full history,
    not just what's currently live."""
    db_path = _db(tmp_path)
    _seed_readings(db_path, "A", n=10, start_ts=datetime(2026, 9, 1, tzinfo=timezone.utc))
    with board_db.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO raw_daily_rollup (pot_id, date, count, temp_min, temp_max, temp_mean, "
            "moisture_min, moisture_max, moisture_mean, gas_min, gas_max, gas_mean, "
            "light_min, light_max, light_mean, first_ts, last_ts) VALUES "
            "('A', '2026-08-01', 50, 20, 21, 20.5, 30, 30, 30, 10, 10, 10, 400, 400, 400, "
            "'2026-08-01T00:00:00+00:00', '2026-08-01T23:59:00+00:00')"
        )

    source = RealDataSource(db_path=db_path)
    status = source.get_status()
    assert status["total_readings_a"] == 60  # 10 live + 50 rolled-up
    assert status["collecting_since"] == "2026-08-01T00:00:00+00:00"


# ── Edge cases ─────────────────────────────────────────────────────────────

def test_unknown_pot_raises_value_error(tmp_path):
    db_path = _db(tmp_path)
    source = RealDataSource(db_path=db_path)
    for method in (source.get_raw, source.get_inference):
        try:
            method("C", 5)
            assert False, "expected ValueError for pot 'C'"
        except ValueError:
            pass


def test_missing_db_returns_empty(tmp_path):
    """No sensor_data.db yet at all (fresh board, or main.py hasn't run) --
    every read must return empty, and no stray file should get created."""
    db_path = str(tmp_path / "does_not_exist.db")
    source = RealDataSource(db_path=db_path)
    for pot in ("A", "B"):
        assert source.get_raw(pot, 10) == []
        assert source.get_inference(pot, 10) == []
    assert list(source.iter_raw_export_rows("A")) == []
    assert source.get_status()["total_readings_a"] == 0
    assert not os.path.exists(db_path)


# ── Live growth (no cache to go stale, but pin the guarantee) ──────────────

def test_get_raw_picks_up_rows_inserted_after_first_read(tmp_path):
    db_path = _db(tmp_path)
    _seed_readings(db_path, "A", n=3)
    source = RealDataSource(db_path=db_path)

    first = source.get_raw("A", 0)
    assert len(first) == 3

    _seed_readings(db_path, "A", n=3, start_ts=_FIXTURE_EPOCH + timedelta(seconds=100))

    second = source.get_raw("A", 0)
    assert len(second) == 6
    assert second[:3] == first


# ── Range filtering + decimation (2026-08-22 dashboard perf work, still
#    behaviorally required post-migration) ─────────────────────────────────

def test_range_5m_only_includes_last_5_minutes(tmp_path):
    db_path = _db(tmp_path)
    timestamps = [
        "2026-08-20T09:50:00+00:00",  # 10m before last -- excluded
        "2026-08-20T09:56:00+00:00",  # 4m before last -- included
        "2026-08-20T09:58:00+00:00",  # 2m before last -- included
        "2026-08-20T10:00:00+00:00",  # last reading, anchors the cutoff
    ]
    _seed_readings_at(db_path, "A", timestamps)
    source = RealDataSource(db_path=db_path)

    rows = source.get_raw("A", 0, range="5m")
    assert [r["timestamp"] for r in rows] == timestamps[1:]


def test_range_overall_returns_everything(tmp_path):
    db_path = _db(tmp_path)
    _seed_readings(db_path, "A", n=10)
    source = RealDataSource(db_path=db_path)

    assert len(source.get_raw("A", 0, range="overall")) == 10
    assert len(source.get_raw("A", 0)) == 10  # range=None behaves the same


def test_range_falls_back_to_full_series_when_window_too_sparse(tmp_path):
    """A too-narrow window that would leave <2 points must fall back to the
    full series rather than rendering an unusably sparse/empty chart."""
    db_path = _db(tmp_path)
    timestamps = [
        "2026-08-20T09:00:00+00:00",
        "2026-08-20T09:30:00+00:00",
        "2026-08-20T10:00:00+00:00",
    ]
    _seed_readings_at(db_path, "A", timestamps)
    source = RealDataSource(db_path=db_path)

    rows = source.get_raw("A", 0, range="5m")  # only the last row is within 5m
    assert len(rows) == 3


def test_max_points_decimates_large_series_but_keeps_last_row(tmp_path):
    db_path = _db(tmp_path)
    _seed_readings(db_path, "A", n=500)
    source = RealDataSource(db_path=db_path)

    full = source.get_raw("A", 0)
    assert len(full) == 500

    decimated = source.get_raw("A", 0, max_points=50)
    assert 0 < len(decimated) <= 100  # bounded near max_points (stride sampling, not exact)
    assert decimated[-1] == full[-1]  # exact most-recent row always kept


def test_max_points_is_noop_when_series_already_fits(tmp_path):
    db_path = _db(tmp_path)
    _seed_readings(db_path, "A", n=10)
    source = RealDataSource(db_path=db_path)

    assert source.get_raw("A", 0, max_points=1500) == source.get_raw("A", 0)


def test_max_points_and_range_apply_only_to_full_history_requests(tmp_path):
    """n > 0 (the live-tick path) must ignore range/max_points entirely --
    only n=0 (full-history) requests are scoped/decimated."""
    db_path = _db(tmp_path)
    _seed_readings(db_path, "A", n=10)
    source = RealDataSource(db_path=db_path)

    with_extras = source.get_raw("A", 5, range="5m", max_points=1)
    without_extras = source.get_raw("A", 5)
    assert with_extras == without_extras
    assert len(with_extras) == 5


# ── CSV export streaming ─────────────────────────────────────────────────────

def test_iter_raw_export_rows_streams_full_history(tmp_path):
    db_path = _db(tmp_path)
    _seed_readings(db_path, "A", n=50)
    source = RealDataSource(db_path=db_path)

    rows = list(source.iter_raw_export_rows("A"))
    assert len(rows) == 50
    assert rows == source.get_raw("A", 0)


def test_iter_raw_export_rows_unknown_pot_raises(tmp_path):
    db_path = _db(tmp_path)
    source = RealDataSource(db_path=db_path)
    try:
        list(source.iter_raw_export_rows("C"))
        assert False, "expected ValueError for pot 'C'"
    except ValueError:
        pass


def test_tiered_history_blended_into_overall_chart(tmp_path):
    """Tiered historical readings (<7 days) must seamlessly merge with live readings for overall chart."""
    db_path = _db(tmp_path)
    now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
    old_ts_daily = (now - timedelta(days=25)).isoformat()
    old_ts_hourly = (now - timedelta(days=12)).isoformat()
    live_ts = (now - timedelta(days=2)).isoformat()

    with board_db.connect(db_path) as conn:
        # Insert historical tiered readings
        conn.execute("""
        INSERT INTO readings_tiered (pot_id, tier, sample_index, millis_ms, temp_c, humidity_pct, pressure_hpa, gas_kohm, light_lux, moisture_pct, received_at)
        VALUES ('A', 'daily', 1, 1000, 22.0, 50.0, 1013.0, 10.0, 400.0, 5.0, ?),
               ('A', 'hourly', 2, 2000, 24.0, 50.0, 1013.0, 15.0, 400.0, 6.0, ?)
        """, (old_ts_daily, old_ts_hourly))

        # Insert live readings
        board_db.insert_reading(conn, "A", {
            "sample_index": 3, "millis_ms": 3000, "temp_c": 26.0, "humidity_pct": 50.0,
            "pressure_hpa": 1013.0, "gas_kohm": 20.0, "light_lux": 400.0, "moisture_pct": 7.0,
            "received_at": live_ts,
        })

    source = RealDataSource(db_path=db_path)
    rows = source.get_raw("A", 0)
    assert len(rows) == 3
    assert [r["temp_c"] for r in rows] == [22.0, 24.0, 26.0]
    assert [r["timestamp"] for r in rows] == [old_ts_daily, old_ts_hourly, live_ts]

