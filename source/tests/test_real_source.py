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
