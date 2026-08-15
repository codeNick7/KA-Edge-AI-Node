"""Tests for RealDataSource (dual-pot, sketch v6.0 schema).

Both Pot A (BME688 @0x76, soil A0) and Pot B (BME688 @0x77, soil A1) are
now backed by separate JSONL relay files. RealDataSource accepts two paths
and serves both pots from real data.
"""

import json

from dashboard.mock_source import MODEL_VERSION
from dashboard.real_source import PLACEHOLDER_LIGHT_LUX, RealDataSource


def _write_fixture(path, n=12, pot_id="A"):
    """Write n JSONL records to path.

    Uses v6.0 field schema: sample_index, millis_ms, temp_c, humidity_pct,
    pressure_hpa, gas_kohm, light_lux, moisture_pct, received_at.
    moisture_pct is always present (sketch v6.0 always emits it).
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
                "received_at": f"2026-08-13T10:00:{i:02d}+00:00",
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
