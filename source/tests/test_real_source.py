"""Tests for RealDataSource (dual-pot, sketch v6.0 schema).

Both Pot A (BME688 @0x76, soil A0) and Pot B (BME688 @0x77, soil A1) are
now backed by separate JSONL relay files. RealDataSource accepts two paths
and serves both pots from real data.
"""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

from dashboard.real_source import PLACEHOLDER_LIGHT_LUX, RealDataSource, _TAIL_CACHE_SIZE
from edge_node.trained_model import MODEL_VERSION

_FIXTURE_EPOCH = datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc)


def _write_fixture(path, n=12, pot_id="A"):
    """Write n JSONL records to path.

    Uses v6.0 field schema: sample_index, millis_ms, temp_c, humidity_pct,
    pressure_hpa, gas_kohm, light_lux, moisture_pct, received_at.
    moisture_pct is always present (sketch v6.0 always emits it).

    received_at uses real datetime arithmetic (not an f-string seconds
    field) so it stays a valid, monotonically-sortable ISO timestamp for any
    n -- for n <= 59 this produces byte-identical strings to the old
    "...T10:00:{i:02d}+00:00" format (still "2026-08-13T10:00:01+00:00" for
    i=1, etc.), so existing hardcoded-string assertions are unaffected; for
    n > 59 it correctly rolls into minutes/hours instead of producing an
    invalid/lexicographically-broken seconds field like "10:00:99".
    """
    base_temp = {"A": 24.0, "B": 28.0}[pot_id]
    with open(path, "w") as f:
        for i in range(1, n + 1):
            record = {
                "sample_index": i,
                "millis_ms": i * 1000,
                "temp_c": round(base_temp + i * 0.1, 1),
                "humidity_pct": 55.0,
                "pressure_hpa": 1013.0,
                "gas_kohm": 10.0 + i,
                "light_lux": 0.0,        # BH1750 placeholder
                "moisture_pct": 2.0,     # dry desk reading
                "received_at": (_FIXTURE_EPOCH + timedelta(seconds=i)).isoformat(),
            }
            f.write(json.dumps(record) + "\n")


# ── Pot A tests ────────────────────────────────────────────────────────────

def test_get_raw_pot_a_returns_expected_shape(tmp_path):
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=5, pot_id="A")
    _write_fixture(pot_b, n=5, pot_id="B")
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    rows = source.get_raw("A", 3)
    assert len(rows) == 3
    for field in ("timestamp", "moisture_pct", "gas_index", "temp_c", "light_lux",
                  "humidity_pct", "pressure_hpa"):
        assert field in rows[0], f"missing field {field!r} in row"
    # ordered oldest-first, most recent 3 of 5 rows → temp_c 24.3, 24.4, 24.5
    assert [row["temp_c"] for row in rows] == [24.3, 24.4, 24.5]


# ── Pot B tests ────────────────────────────────────────────────────────────

def test_get_raw_pot_b_returns_expected_shape(tmp_path):
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=5, pot_id="A")
    _write_fixture(pot_b, n=5, pot_id="B")
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    rows = source.get_raw("B", 3)
    assert len(rows) == 3
    for field in ("timestamp", "moisture_pct", "gas_index", "temp_c", "light_lux",
                  "humidity_pct", "pressure_hpa"):
        assert field in rows[0], f"missing field {field!r} in row"
    # Pot B base temp is 28.0, most recent 3 of 5 → 28.3, 28.4, 28.5
    assert [row["temp_c"] for row in rows] == [28.3, 28.4, 28.5]


def test_get_raw_pot_b_independent_of_pot_a(tmp_path):
    """Pot A and Pot B must be served from their own separate files."""
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=5, pot_id="A")
    _write_fixture(pot_b, n=5, pot_id="B")
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    rows_a = source.get_raw("A", 1)
    rows_b = source.get_raw("B", 1)
    # The two pots have different base temperatures so they must differ.
    assert rows_a[0]["temp_c"] != rows_b[0]["temp_c"]


# ── Light-lux placeholder ──────────────────────────────────────────────────

def test_light_lux_placeholder_fallback(tmp_path):
    """Records without light_lux should fall back to PLACEHOLDER_LIGHT_LUX."""
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    # Write a record without light_lux (pre-v6.0 compat)
    with open(pot_a, "w") as f:
        f.write(json.dumps({
            "sample_index": 1, "millis_ms": 1000,
            "temp_c": 24.0, "humidity_pct": 55.0, "pressure_hpa": 1013.0,
            "gas_kohm": 11.0, "moisture_pct": 2.0,
            "received_at": "2026-08-13T10:00:01+00:00",
        }) + "\n")
    open(pot_b, "w").close()  # empty
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    rows = source.get_raw("A", 1)
    assert rows[0]["light_lux"] == PLACEHOLDER_LIGHT_LUX


# ── Inference ─────────────────────────────────────────────────────────────

def test_get_inference_both_pots(tmp_path):
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=12, pot_id="A")
    _write_fixture(pot_b, n=12, pot_id="B")
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    for pot in ("A", "B"):
        rows = source.get_inference(pot, 5)
        assert len(rows) == 5, f"expected 5 inference rows for Pot {pot}"
        for field in ("timestamp", "rsmi", "light_delta", "gas_index_trend",
                      "rsmi_trend", "flag", "confidence", "contributing_trend", "model_version"):
            assert field in rows[0], f"missing field {field!r} in Pot {pot} inference"
        assert rows[0]["model_version"] == MODEL_VERSION


def test_get_inference_dry_reading_not_waterlogged(tmp_path):
    """moisture_pct=2% (dry desk) must never produce waterlogged_risk for either pot."""
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=12, pot_id="A")
    _write_fixture(pot_b, n=12, pot_id="B")
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    for pot in ("A", "B"):
        rows = source.get_inference(pot, 5)
        for row in rows:
            assert row["flag"] != "waterlogged_risk", (
                f"Pot {pot} dry reading wrongly classified as waterlogged_risk: {row}"
            )


# ── Concurrent cold-start access ────────────────────────────────────────────

def test_concurrent_cold_start_does_not_duplicate_history(tmp_path, monkeypatch):
    """Diagnosed 2026-08-15 against real board data: the dashboard fires
    several concurrent requests touching the same pot on page load
    (/api/status, /api/raw, /api/inference — each an un-awaited fetch()).
    Without a lock, concurrent callers hitting a cold (empty) cache could
    each independently read the whole file from byte 0 and each append
    their own copy, producing an exact N-times duplication of the entire
    history in memory (confirmed: every row before the board process's
    cold-start race window resolved was tripled; every row after was
    clean). This exercises that exact pattern under real thread
    contention — widening the race window with an injected delay, since
    local disk I/O is normally too fast to reliably interleave in a test."""
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=200, pot_id="A")
    open(pot_b, "w").close()
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    orig = source._load_records_locked

    def slow_load_records_locked(pot, path, cache):
        time.sleep(0.02)  # widen the race window, like real disk I/O latency
        return orig(pot, path, cache)

    monkeypatch.setattr(source, "_load_records_locked", slow_load_records_locked)

    results = []

    def worker():
        results.append(len(source.get_raw("A", 0)))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [200] * 8, f"a concurrent caller saw a duplicated/partial view: {results}"
    assert len(source.get_raw("A", 0)) == 200


# ── Status / SD card space ─────────────────────────────────────────────────

def test_get_status_without_sd_status_file(tmp_path):
    """No sd_status.json (older firmware, or before the first ~60s report) —
    sd_* fields must be None, not raise."""
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=2, pot_id="A")
    open(pot_b, "w").close()
    source = RealDataSource(
        pot_a_path=str(pot_a), pot_b_path=str(pot_b),
        sd_status_path=str(tmp_path / "no_sd_status.json"),
    )
    status = source.get_status()
    assert status["sd_used_mb"] is None
    assert status["sd_free_mb"] is None
    assert status["sd_total_mb"] is None


def test_get_status_reads_sd_status_file(tmp_path):
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    open(pot_a, "w").close()
    open(pot_b, "w").close()
    sd_status = tmp_path / "sd_status.json"
    sd_status.write_text(json.dumps({
        "sd_used_mb": 12.4, "sd_free_mb": 1987.6, "sd_total_mb": 2000.0,
        "updated_at": "2026-08-15T09:00:00+00:00",
    }))
    source = RealDataSource(
        pot_a_path=str(pot_a), pot_b_path=str(pot_b), sd_status_path=str(sd_status),
    )
    status = source.get_status()
    assert status["sd_used_mb"] == 12.4
    assert status["sd_free_mb"] == 1987.6
    assert status["sd_total_mb"] == 2000.0
    assert status["sd_status_updated_at"] == "2026-08-15T09:00:00+00:00"


def test_get_status_ignores_malformed_sd_status_file(tmp_path):
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    open(pot_a, "w").close()
    open(pot_b, "w").close()
    sd_status = tmp_path / "sd_status.json"
    sd_status.write_text("{not valid json")
    source = RealDataSource(
        pot_a_path=str(pot_a), pot_b_path=str(pot_b), sd_status_path=str(sd_status),
    )
    status = source.get_status()  # must not raise
    assert status["sd_used_mb"] is None


# ── Edge cases ─────────────────────────────────────────────────────────────

def test_unknown_pot_raises_value_error(tmp_path):
    pot_a = tmp_path / "a.jsonl"
    pot_b = tmp_path / "b.jsonl"
    open(pot_a, "w").close()
    open(pot_b, "w").close()
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))
    for method in (source.get_raw, source.get_inference):
        try:
            method("C", 5)
            assert False, "expected ValueError for pot 'C'"
        except ValueError:
            pass


def test_missing_data_files_return_empty(tmp_path):
    source = RealDataSource(
        pot_a_path=str(tmp_path / "no_a.jsonl"),
        pot_b_path=str(tmp_path / "no_b.jsonl"),
    )
    for pot in ("A", "B"):
        assert source.get_raw(pot, 10) == []
        assert source.get_inference(pot, 10) == []


# ── Incremental read cache ─────────────────────────────────────────────────

def test_load_records_picks_up_appended_data(tmp_path):
    """Live growth: appending to the file after the first read must be
    visible on a later call against the same RealDataSource instance
    (exercises the tail-read cache instead of a fresh full reparse)."""
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=3, pot_id="A")
    open(pot_b, "w").close()
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    first = source.get_raw("A", 0)
    assert len(first) == 3

    with open(pot_a, "a") as f:
        for i in range(4, 7):
            f.write(json.dumps({
                "sample_index": i, "millis_ms": i * 1000,
                "temp_c": round(24.0 + i * 0.1, 1), "humidity_pct": 55.0,
                "pressure_hpa": 1013.0, "gas_kohm": 10.0 + i,
                "light_lux": 0.0, "moisture_pct": 2.0,
                "received_at": f"2026-08-13T10:00:{i:02d}+00:00",
            }) + "\n")

    second = source.get_raw("A", 0)
    assert len(second) == 6
    assert second[:3] == first  # previously-read rows unchanged


def test_load_records_recovers_partial_line_written_mid_read(tmp_path):
    """A line still being written (no trailing newline yet) must not be
    silently lost once it's completed on a later call — only get skipped
    on the call where it was still incomplete."""
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    complete_record = {
        "sample_index": 1, "millis_ms": 1000, "temp_c": 24.1,
        "humidity_pct": 55.0, "pressure_hpa": 1013.0, "gas_kohm": 11.0,
        "light_lux": 0.0, "moisture_pct": 2.0,
        "received_at": "2026-08-13T10:00:01+00:00",
    }
    partial_record = {
        "sample_index": 2, "millis_ms": 2000, "temp_c": 24.2,
        "humidity_pct": 55.0, "pressure_hpa": 1013.0, "gas_kohm": 12.0,
        "light_lux": 0.0, "moisture_pct": 2.0,
        "received_at": "2026-08-13T10:00:02+00:00",
    }
    with open(pot_a, "w") as f:
        f.write(json.dumps(complete_record) + "\n")
        # Simulate a write in progress: partial JSON, no trailing newline.
        f.write(json.dumps(partial_record)[:20])
    open(pot_b, "w").close()
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    mid_write = source.get_raw("A", 0)
    assert len(mid_write) == 1  # partial line correctly not parsed yet

    # Writer finishes the line.
    with open(pot_a, "a") as f:
        f.write(json.dumps(partial_record)[20:] + "\n")

    completed = source.get_raw("A", 0)
    assert len(completed) == 2  # the previously-partial row is now recovered
    assert completed[1]["temp_c"] == 24.2


def test_load_records_reorders_late_out_of_order_append(tmp_path):
    """A relay hiccup can append an older-timestamped row after newer ones
    already read; the merged result must still be chronologically sorted."""
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    with open(pot_a, "w") as f:
        f.write(json.dumps({
            "sample_index": 2, "millis_ms": 2000, "temp_c": 24.2,
            "humidity_pct": 55.0, "pressure_hpa": 1013.0, "gas_kohm": 12.0,
            "light_lux": 0.0, "moisture_pct": 2.0,
            "received_at": "2026-08-13T10:00:05+00:00",
        }) + "\n")
    open(pot_b, "w").close()
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))
    source.get_raw("A", 0)  # prime the cache

    with open(pot_a, "a") as f:
        # Arrives later but timestamped earlier than the row already cached.
        f.write(json.dumps({
            "sample_index": 1, "millis_ms": 1000, "temp_c": 24.1,
            "humidity_pct": 55.0, "pressure_hpa": 1013.0, "gas_kohm": 11.0,
            "light_lux": 0.0, "moisture_pct": 2.0,
            "received_at": "2026-08-13T10:00:01+00:00",
        }) + "\n")

    rows = source.get_raw("A", 0)
    assert [r["temp_c"] for r in rows] == [24.1, 24.2]


# ── Incremental inference cache ────────────────────────────────────────────

def test_get_inference_incremental_matches_full_recompute(tmp_path):
    """Reading inference in two increments (simulating live polling) must
    produce the same final result as reading the full history in one shot —
    proves the incremental cache's math matches a from-scratch recompute."""
    pot_a_full = tmp_path / "full" / "pot_a.jsonl"
    pot_b_full = tmp_path / "full" / "pot_b.jsonl"
    (tmp_path / "full").mkdir()
    _write_fixture(pot_a_full, n=12, pot_id="A")
    open(pot_b_full, "w").close()
    baseline = RealDataSource(pot_a_path=str(pot_a_full), pot_b_path=str(pot_b_full))
    expected = baseline.get_inference("A", 0)

    pot_a_inc = tmp_path / "inc" / "pot_a.jsonl"
    pot_b_inc = tmp_path / "inc" / "pot_b.jsonl"
    (tmp_path / "inc").mkdir()
    _write_fixture(pot_a_inc, n=6, pot_id="A")
    open(pot_b_inc, "w").close()
    incremental = RealDataSource(pot_a_path=str(pot_a_inc), pot_b_path=str(pot_b_inc))
    incremental.get_inference("A", 0)  # first poll, primes the cache

    with open(pot_a_full) as f:
        all_lines = f.readlines()
    with open(pot_a_inc, "a") as f:
        f.writelines(all_lines[6:12])

    actual = incremental.get_inference("A", 0)
    assert actual == expected


def test_all_zero_bme_rows_are_skipped(tmp_path):
    """Records with all-zero BME fields (offline sensor) must be filtered out."""
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    with open(pot_a, "w") as f:
        # Zero row (should be skipped)
        f.write(json.dumps({
            "sample_index": 1, "millis_ms": 1000,
            "temp_c": 0.0, "humidity_pct": 0.0, "pressure_hpa": 0.0,
            "gas_kohm": 0.0, "light_lux": 0.0, "moisture_pct": 5.0,
            "received_at": "2026-08-13T10:00:01+00:00",
        }) + "\n")
        # Valid row
        f.write(json.dumps({
            "sample_index": 2, "millis_ms": 2000,
            "temp_c": 24.1, "humidity_pct": 55.0, "pressure_hpa": 1013.0,
            "gas_kohm": 11.0, "light_lux": 0.0, "moisture_pct": 5.0,
            "received_at": "2026-08-13T10:00:02+00:00",
        }) + "\n")
    open(pot_b, "w").close()
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    rows = source.get_raw("A", 10)
    assert len(rows) == 1
    assert rows[0]["temp_c"] == 24.1


# ── Persisted inference cache (2026-08-22 dashboard perf work) ─────────────

def test_inference_cache_persists_across_restart(tmp_path, monkeypatch):
    """A fresh RealDataSource on the same paths (simulating a process
    restart) must load the previous run's inference results from the
    on-disk sidecar instead of recomputing them — the whole point of
    persisting it is to avoid repaying the full O(history) recompute."""
    import dashboard.real_source as real_source_module

    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=12, pot_id="A")
    open(pot_b, "w").close()

    first = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))
    expected = first.get_inference("A", 0)
    assert expected  # sanity: TREND_WINDOW=7, n=12 -> 6 results

    cache_path = tmp_path / "pot_a_inference_cache.jsonl"
    assert cache_path.exists()
    with open(cache_path) as f:
        assert len(f.readlines()) == len(expected)

    call_count = {"n": 0}
    real_trained_predict = real_source_module.trained_predict

    def _counting_trained_predict(*args, **kwargs):
        call_count["n"] += 1
        return real_trained_predict(*args, **kwargs)

    monkeypatch.setattr(real_source_module, "trained_predict", _counting_trained_predict)

    second = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))
    actual = second.get_inference("A", 0)

    assert actual == expected
    assert call_count["n"] == 0  # nothing recomputed -- loaded straight from disk


def test_inference_cache_only_computes_newly_arrived_rows_after_restart(tmp_path, monkeypatch):
    import dashboard.real_source as real_source_module

    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=12, pot_id="A")
    open(pot_b, "w").close()

    first = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))
    first.get_inference("A", 0)

    _write_fixture(pot_a, n=15, pot_id="A")  # 3 new rows arrive after "restart"

    call_count = {"n": 0}
    real_trained_predict = real_source_module.trained_predict

    def _counting_trained_predict(*args, **kwargs):
        call_count["n"] += 1
        return real_trained_predict(*args, **kwargs)

    monkeypatch.setattr(real_source_module, "trained_predict", _counting_trained_predict)

    second = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))
    actual = second.get_inference("A", 0)

    assert len(actual) == 15 - 6  # TREND_WINDOW - 1
    assert call_count["n"] == 3  # only the 3 newly-arrived rows recomputed


def test_inference_cache_invalidated_when_file_shrinks(tmp_path):
    """Truncation/rotation must drop the stale persisted cache, not just the
    in-memory one -- otherwise a later append would duplicate ahead of it."""
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=12, pot_id="A")
    open(pot_b, "w").close()

    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))
    source.get_inference("A", 0)

    _write_fixture(pot_a, n=8, pot_id="A")  # shrink
    results = source.get_inference("A", 0)
    assert len(results) == 8 - 6

    cache_path = tmp_path / "pot_a_inference_cache.jsonl"
    with open(cache_path) as f:
        assert len(f.readlines()) == len(results)


# ── Range filtering + decimation (2026-08-22 dashboard perf work) ─────────

def _write_spread_fixture(path, timestamps):
    """One row per ISO timestamp given, otherwise identical shape to
    _write_fixture -- used where exact cutoff boundaries matter."""
    with open(path, "w") as f:
        for i, ts in enumerate(timestamps, start=1):
            f.write(json.dumps({
                "sample_index": i, "millis_ms": i * 1000,
                "temp_c": 20.0 + i * 0.01, "humidity_pct": 55.0, "pressure_hpa": 1013.0,
                "gas_kohm": 10.0, "light_lux": 100.0, "moisture_pct": 5.0,
                "received_at": ts,
            }) + "\n")


def test_range_5m_only_includes_last_5_minutes(tmp_path):
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    timestamps = [
        "2026-08-20T09:50:00+00:00",  # 10m before last -- excluded
        "2026-08-20T09:56:00+00:00",  # 4m before last -- included
        "2026-08-20T09:58:00+00:00",  # 2m before last -- included
        "2026-08-20T10:00:00+00:00",  # last reading, anchors the cutoff
    ]
    _write_spread_fixture(pot_a, timestamps)
    open(pot_b, "w").close()
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    rows = source.get_raw("A", 0, range="5m")
    assert [r["timestamp"] for r in rows] == timestamps[1:]


def test_range_overall_returns_everything(tmp_path):
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=10, pot_id="A")
    open(pot_b, "w").close()
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    assert len(source.get_raw("A", 0, range="overall")) == 10
    assert len(source.get_raw("A", 0)) == 10  # range=None behaves the same


def test_range_falls_back_to_full_series_when_window_too_sparse(tmp_path):
    """A too-narrow window that would leave <2 points must fall back to the
    full series rather than rendering an unusably sparse/empty chart."""
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    timestamps = [
        "2026-08-20T09:00:00+00:00",
        "2026-08-20T09:30:00+00:00",
        "2026-08-20T10:00:00+00:00",
    ]
    _write_spread_fixture(pot_a, timestamps)
    open(pot_b, "w").close()
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    rows = source.get_raw("A", 0, range="5m")  # only the last row is within 5m
    assert len(rows) == 3


def test_max_points_decimates_large_series_but_keeps_last_row(tmp_path):
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=500, pot_id="A")
    open(pot_b, "w").close()
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    full = source.get_raw("A", 0)
    assert len(full) == 500

    decimated = source.get_raw("A", 0, max_points=50)
    assert 0 < len(decimated) <= 100  # bounded near max_points (stride sampling, not exact)
    assert decimated[-1] == full[-1]  # exact most-recent row always kept


def test_max_points_is_noop_when_series_already_fits(tmp_path):
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=10, pot_id="A")
    open(pot_b, "w").close()
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    assert source.get_raw("A", 0, max_points=1500) == source.get_raw("A", 0)


def test_max_points_and_range_apply_only_to_full_history_requests(tmp_path):
    """n > 0 (the live-tick path) must ignore range/max_points entirely --
    only n=0 (full-history) requests are scoped/decimated."""
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=10, pot_id="A")
    open(pot_b, "w").close()
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    with_extras = source.get_raw("A", 5, range="5m", max_points=1)
    without_extras = source.get_raw("A", 5)
    assert with_extras == without_extras
    assert len(with_extras) == 5


# ── Bounded memory (2026-08-22 OOM fix) ────────────────────────────────────

def test_record_cache_stays_bounded_regardless_of_history_size(tmp_path):
    """The in-memory tail cache must never grow past _TAIL_CACHE_SIZE, even
    when the underlying file has far more rows -- this is the actual fix for
    the board's OOM kill (dmesg-confirmed: anon-rss ~1GB / 1.7GB total)."""
    n = _TAIL_CACHE_SIZE + 1000
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=n, pot_id="A")
    open(pot_b, "w").close()
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    assert len(source._record_cache["A"]["records"]) <= _TAIL_CACHE_SIZE
    assert source._record_cache["A"]["total_raw_seen"] == n

    # Full history must still be reachable (via disk streaming), just not
    # all resident in memory at once.
    full = source.get_raw("A", 0)
    assert len(full) == n

    status = source.get_status()
    assert status["total_readings_a"] == n


def test_inference_results_cache_stays_bounded_and_sidecar_stays_complete(tmp_path):
    n = _TAIL_CACHE_SIZE + 500
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=n, pot_id="A")
    open(pot_b, "w").close()
    source = RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b))

    full = source.get_inference("A", 0)
    assert len(full) == n - 6  # TREND_WINDOW - 1
    assert len(source._inference_cache["A"]["results"]) <= _TAIL_CACHE_SIZE

    cache_path = tmp_path / "pot_a_inference_cache.jsonl"
    with open(cache_path) as f:
        assert len(f.readlines()) == len(full)
