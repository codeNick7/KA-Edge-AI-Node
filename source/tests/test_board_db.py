"""Tests for KA-Edge-source/python/board_db.py's retention/tiering logic.

Added 2026-09-25 alongside the fix for the "week/overall charts lose all history past
7 days" bug: prune_old() used to delete old rows after rolling them into aggregate-only
daily rollup tables (min/max/mean, no per-point resolution), while
source/dashboard/real_source.py's reader expected a per-point archive
(readings_tiered/inference_tiered) that nothing ever wrote to. This file exercises the
now-implemented compaction (Tier 1 live -> Tier 2 hourly, "non-outlier" = median-closest
real sample) end to end, which previously had zero coverage (the only existing test,
test_real_source.py's test_tiered_history_blended_into_overall_chart, seeds
readings_tiered by hand and only checks the *reader* side).

2026-09-28: rolled back the Tier 2 -> Tier 3 daily-compaction step this file originally
also covered (compact_hourly_tier_to_daily() no longer exists) -- collapsing archived
history to 1 point/day past 20 days flattened out daily trend variation on week/overall
charts. Archived (>7 day) history now stays hourly indefinitely.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "KA-Edge-source", "python"))
import board_db  # noqa: E402

_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    return str(tmp_path / "sensor_data.db")


def _seed_day_of_readings(db_path, pot, date, per_hour=4, moisture_start=40.0, with_inference=False):
    """One day's worth of readings for `pot`, `per_hour` rows spread evenly across each
    of the day's 24 hours, moisture_pct drifting linearly so each hour's bucket has a
    distinguishable, non-tied median. with_inference=True also inserts a matching
    inference_results row (linked via reading_id, as scripts/inference_service.py does)
    for every reading, so prune_old()'s "protect unprocessed days" safety doesn't hold
    the day back."""
    with board_db.connect(db_path) as conn:
        i = 0
        for hour in range(24):
            for slot in range(per_hour):
                i += 1
                ts = date.replace(hour=hour, minute=(60 // per_hour) * slot, second=0, microsecond=0)
                reading_id = board_db.insert_reading(conn, pot, {
                    "sample_index": i, "millis_ms": i * 1000,
                    "temp_c": 24.0, "humidity_pct": 55.0, "pressure_hpa": 1013.0,
                    "gas_kohm": 10.0, "light_lux": 400.0,
                    "moisture_pct": round(moisture_start + hour * 0.1 + slot * 0.01, 3),
                    "received_at": ts.isoformat(),
                })
                if with_inference:
                    board_db.insert_inference_result(conn, pot, reading_id, {
                        "timestamp": ts.isoformat(),
                        "rsmi": round(0.5 + hour * 0.001 + slot * 0.0001, 4),
                        "light_delta": 0.0, "gas_index_trend": 0.0, "rsmi_trend": 0.0,
                        "flag": "stable", "confidence": 0.9,
                        "contributing_trend": "moisture_flat", "model_version": "test-model-v1",
                    })


# ── Tier 1 -> Tier 2 (prune_old archives before deleting) ──────────────────────────

def test_prune_old_archives_pruned_day_into_hourly_tier(tmp_path):
    db_path = _db(tmp_path)
    old_date = _NOW - timedelta(days=10)  # older than RETENTION_DAYS=7
    _seed_day_of_readings(db_path, "A", old_date, per_hour=4, with_inference=True)

    with board_db.connect(db_path) as conn:
        result = board_db.prune_old(conn, retention_days=board_db.RETENTION_DAYS)

    with board_db.connect(db_path) as conn:
        live_count = conn.execute("SELECT COUNT(*) FROM readings WHERE pot_id='A'").fetchone()[0]
        tiered_rows = conn.execute(
            "SELECT tier, moisture_pct, received_at FROM readings_tiered WHERE pot_id='A' ORDER BY received_at"
        ).fetchall()

    assert live_count == 0, "old day should have been deleted from the live table"
    assert len(tiered_rows) == 24, "one representative row per hour (24 hours) should have been archived"
    assert all(tier == "hourly" for tier, _, _ in tiered_rows)


def test_prune_old_protects_unprocessed_day_and_does_not_archive_it(tmp_path):
    """A day whose readings have no matching inference_results row yet must stay live
    (and NOT be archived/deleted) — pre-existing hard_cap safety, still respected by the
    new archiving step (archiving only happens for dates that actually get deleted)."""
    db_path = _db(tmp_path)
    old_date = _NOW - timedelta(days=10)
    _seed_day_of_readings(db_path, "A", old_date, per_hour=4)
    # deliberately do NOT seed inference_results for this day

    with board_db.connect(db_path) as conn:
        result = board_db.prune_old(conn, retention_days=board_db.RETENTION_DAYS)

    assert any(pot == "A" for pot, _date, _n in result["protected_dates"])

    with board_db.connect(db_path) as conn:
        live_count = conn.execute("SELECT COUNT(*) FROM readings WHERE pot_id='A'").fetchone()[0]
        tiered_count = conn.execute("SELECT COUNT(*) FROM readings_tiered WHERE pot_id='A'").fetchone()[0]

    assert live_count > 0, "unprocessed day within the hard cap must not be deleted"
    assert tiered_count == 0, "a day that was never deleted must not have been archived either"


def test_pick_non_outlier_ignores_a_spike(tmp_path):
    rows = [
        (1, 100, 24.0, 55.0, 1013.0, 10.0, 400.0, 40.0, "2026-09-01T00:00:00+00:00"),
        (2, 200, 24.0, 55.0, 1013.0, 10.0, 400.0, 41.0, "2026-09-01T00:15:00+00:00"),
        (3, 300, 24.0, 55.0, 1013.0, 10.0, 400.0, 999.0, "2026-09-01T00:30:00+00:00"),  # spike/outlier
        (4, 400, 24.0, 55.0, 1013.0, 10.0, 400.0, 42.0, "2026-09-01T00:45:00+00:00"),
    ]
    rep = board_db._pick_non_outlier(rows, board_db._RAW_TIER_VALUE_IDX)
    assert rep[board_db._RAW_TIER_VALUE_IDX] != 999.0, "the representative point must not be the outlier"


# ── Hourly tier is never compacted further (2026-09-28 rollback) ───────────────────

def test_prune_old_never_compacts_old_hourly_tier_rows_to_daily(tmp_path):
    """A day archived to the hourly tier long ago (>20 days, the old Tier-3 threshold)
    must stay at 24 hourly rows forever -- prune_old() must not collapse it, and
    compact_hourly_tier_to_daily no longer exists at all."""
    db_path = _db(tmp_path)
    very_old_date = _NOW - timedelta(days=25)
    with board_db.connect(db_path) as conn:
        for hour in range(24):
            ts = very_old_date.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()
            conn.execute(
                "INSERT INTO readings_tiered (pot_id, tier, sample_index, millis_ms, temp_c, "
                "humidity_pct, pressure_hpa, gas_kohm, light_lux, moisture_pct, received_at) "
                "VALUES ('A', 'hourly', ?, ?, 24.0, 55.0, 1013.0, 10.0, 400.0, ?, ?)",
                (hour, hour * 1000, 40.0 + hour * 0.1, ts),
            )

    with board_db.connect(db_path) as conn:
        board_db.prune_old(conn, retention_days=board_db.RETENTION_DAYS)

    with board_db.connect(db_path) as conn:
        rows = conn.execute("SELECT tier FROM readings_tiered WHERE pot_id='A'").fetchall()
    assert len(rows) == 24, "hourly-tier rows must survive prune_old() untouched, regardless of age"
    assert all(tier == "hourly" for (tier,) in rows)
    assert not hasattr(board_db, "compact_hourly_tier_to_daily")


# ── Duplicate-write protection (real 2026-09-25 incident: two inference_service.py
# --watch processes briefly ran concurrently against the same db, both backfilling
# from the same stale resume pointer -- ~30,829 duplicate rows for one pot resulted,
# silently, no error) ────────────────────────────────────────────────────────────

def test_insert_inference_result_duplicate_timestamp_is_a_no_op(tmp_path):
    db_path = _db(tmp_path)
    result = {
        "timestamp": "2026-09-25T09:41:07.670407+00:00", "rsmi": 0.5, "light_delta": 0.0,
        "gas_index_trend": 0.0, "rsmi_trend": 0.0, "flag": "stable", "confidence": 0.9,
        "contributing_trend": "moisture_flat", "model_version": "test-model-v1",
    }
    with board_db.connect(db_path) as conn:
        board_db.insert_inference_result(conn, "A", 1, result)
        # simulate a second, independent process racing to insert the exact same
        # (pot_id, timestamp) -- must not raise, must not create a second row.
        board_db.insert_inference_result(conn, "A", 1, result)

    with board_db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM inference_results WHERE pot_id='A' AND timestamp=?",
            (result["timestamp"],),
        ).fetchone()
    assert rows[0] == 1


def test_insert_inference_result_allows_same_timestamp_different_pots(tmp_path):
    """The uniqueness guard is scoped to (pot_id, timestamp), not timestamp alone --
    both pots legitimately share the same wall-clock reading cycle timestamps."""
    db_path = _db(tmp_path)
    result = {
        "timestamp": "2026-09-25T09:41:07.670407+00:00", "rsmi": 0.5, "light_delta": 0.0,
        "gas_index_trend": 0.0, "rsmi_trend": 0.0, "flag": "stable", "confidence": 0.9,
        "contributing_trend": "moisture_flat", "model_version": "test-model-v1",
    }
    with board_db.connect(db_path) as conn:
        board_db.insert_inference_result(conn, "A", 1, result)
        board_db.insert_inference_result(conn, "B", 1, result)

    with board_db.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM inference_results").fetchone()[0]
    assert count == 2
