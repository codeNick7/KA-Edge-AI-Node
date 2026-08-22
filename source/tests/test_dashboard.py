"""Integration tests for the Flask dashboard routes.

Both pots are now served by RealDataSource (real sensor JSONL files).
MockDataSource is still tested in isolation for its own correctness.
"""

import json
from datetime import datetime, timedelta, timezone

import dashboard.app as dashboard_app
from dashboard.app import app
from dashboard.mock_source import MockDataSource
from dashboard.real_source import PLACEHOLDER_LIGHT_LUX, RealDataSource
from edge_node.trained_model import MODEL_VERSION

_FIXTURE_EPOCH = datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc)


def client():
    app.testing = True
    return app.test_client()


def _write_fixture(path, n=15, pot_id="A"):
    """Write n v6.0 JSONL records to path.

    received_at uses real datetime arithmetic so it stays valid for n > 59
    -- see test_real_source.py's identical fixture for the full rationale.
    """
    base_temp = {"A": 24.0, "B": 28.0}[pot_id]
    with open(path, "w") as f:
        for i in range(1, n + 1):
            f.write(
                json.dumps(
                    {
                        "sample_index": i,
                        "millis_ms": i * 1000,
                        "temp_c": round(base_temp + i * 0.1, 1),
                        "humidity_pct": 55.0,
                        "pressure_hpa": 1013.0,
                        "gas_kohm": 10.0 + i,
                        "light_lux": 0.0,     # BH1750 placeholder
                        "moisture_pct": 2.0,
                        "received_at": (_FIXTURE_EPOCH + timedelta(seconds=i)).isoformat(),
                    }
                )
                + "\n"
            )


def test_html_routes_return_200():
    c = client()
    for path in ("/", "/raw", "/inference"):
        resp = c.get(path)
        assert resp.status_code == 200
        assert b"Karbon Assure" in resp.data


def test_api_raw_both_pots_return_n_rows(tmp_path, monkeypatch):
    """Both pots must be served from RealDataSource JSONL relay files."""
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=15, pot_id="A")
    _write_fixture(pot_b, n=15, pot_id="B")
    monkeypatch.setattr(
        dashboard_app,
        "_real_source",
        RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b)),
    )

    c = client()
    for pot in ("A", "B"):
        resp = c.get(f"/api/raw?pot={pot}&n=10")
        assert resp.status_code == 200
        rows = resp.get_json()
        assert len(rows) == 10
        for field in ("timestamp", "moisture_pct", "gas_index", "temp_c",
                      "light_lux", "humidity_pct", "pressure_hpa"):
            assert field in rows[0], f"missing {field!r} in Pot {pot} raw row"
        # light_lux is placeholder (0.0 from fixture); PLACEHOLDER_LIGHT_LUX
        # fallback only kicks in when the key is entirely absent
        assert rows[0]["light_lux"] == 0.0


def test_api_raw_light_lux_fallback_when_key_absent(tmp_path, monkeypatch):
    """Records missing light_lux key should fall back to PLACEHOLDER_LIGHT_LUX."""
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    with open(pot_a, "w") as f:
        f.write(json.dumps({
            "sample_index": 1, "millis_ms": 1000,
            "temp_c": 24.0, "humidity_pct": 55.0, "pressure_hpa": 1013.0,
            "gas_kohm": 11.0, "moisture_pct": 2.0,
            "received_at": "2026-08-13T10:00:01+00:00",
        }) + "\n")
    open(pot_b, "w").close()
    monkeypatch.setattr(
        dashboard_app,
        "_real_source",
        RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b)),
    )

    c = client()
    resp = c.get("/api/raw?pot=A&n=1")
    rows = resp.get_json()
    assert rows[0]["light_lux"] == PLACEHOLDER_LIGHT_LUX


def test_api_inference_both_pots_return_rows(tmp_path, monkeypatch):
    """Inference pipeline must work for both pots."""
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=15, pot_id="A")
    _write_fixture(pot_b, n=15, pot_id="B")
    monkeypatch.setattr(
        dashboard_app,
        "_real_source",
        RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b)),
    )

    c = client()
    for pot in ("A", "B"):
        resp = c.get(f"/api/inference?pot={pot}&n=5")
        assert resp.status_code == 200
        rows = resp.get_json()
        assert len(rows) == 5
        for field in ("timestamp", "rsmi", "light_delta", "gas_index_trend",
                      "rsmi_trend", "flag", "confidence", "contributing_trend", "model_version"):
            assert field in rows[0], f"missing {field!r} in Pot {pot} inference row"
        assert rows[0]["model_version"] == MODEL_VERSION


def test_api_status_reports_sd_card_space(tmp_path, monkeypatch):
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    open(pot_a, "w").close()
    open(pot_b, "w").close()
    sd_status = tmp_path / "sd_status.json"
    sd_status.write_text(json.dumps({
        "sd_used_mb": 12.4, "sd_free_mb": 1987.6, "sd_total_mb": 2000.0,
        "updated_at": "2026-08-15T09:00:00+00:00",
    }))
    monkeypatch.setattr(
        dashboard_app,
        "_real_source",
        RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b), sd_status_path=str(sd_status)),
    )

    c = client()
    resp = c.get("/api/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["sd_used_mb"] == 12.4
    assert body["sd_free_mb"] == 1987.6
    assert body["sd_total_mb"] == 2000.0


def test_api_download_csv_streams_full_history_for_one_pot(tmp_path, monkeypatch):
    """CSV export (2026-08-22 streaming rewrite) must still return every row,
    byte-for-byte the same shape as before, just produced via a generator."""
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=15, pot_id="A")
    open(pot_b, "w").close()
    monkeypatch.setattr(
        dashboard_app,
        "_real_source",
        RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b)),
    )

    c = client()
    resp = c.get("/api/download/csv?pot=A")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    body = resp.get_data(as_text=True)
    lines = [l for l in body.splitlines() if l]
    assert lines[0].startswith("pot,sample_index,received_at")
    assert len(lines) == 1 + 15  # header + 15 rows
    assert all(line.startswith("A,") for line in lines[1:])


def test_api_download_csv_omit_pot_exports_both(tmp_path, monkeypatch):
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=5, pot_id="A")
    _write_fixture(pot_b, n=3, pot_id="B")
    monkeypatch.setattr(
        dashboard_app,
        "_real_source",
        RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b)),
    )

    c = client()
    resp = c.get("/api/download/csv")
    body = resp.get_data(as_text=True)
    lines = [l for l in body.splitlines() if l]
    assert len(lines) == 1 + 5 + 3


def test_api_raw_range_scoped_chart_fetch(tmp_path, monkeypatch):
    """range=overall&max_points=... (dashboard.js's actual fetch shape) must
    work end-to-end through the HTTP layer, not just the Python API."""
    pot_a = tmp_path / "pot_a.jsonl"
    pot_b = tmp_path / "pot_b.jsonl"
    _write_fixture(pot_a, n=500, pot_id="A")
    open(pot_b, "w").close()
    monkeypatch.setattr(
        dashboard_app,
        "_real_source",
        RealDataSource(pot_a_path=str(pot_a), pot_b_path=str(pot_b)),
    )

    c = client()
    resp = c.get("/api/raw?pot=A&n=0&range=overall&max_points=50")
    assert resp.status_code == 200
    rows = resp.get_json()
    assert 0 < len(rows) <= 100


def test_api_rejects_unknown_pot():
    c = client()
    for path in ("/api/raw?pot=Z", "/api/inference?pot=Z"):
        resp = c.get(path)
        assert resp.status_code == 400


def test_mock_data_source_rsmi_and_moisture_are_bounded():
    """MockDataSource still used in isolation to validate its synthetic pipeline."""
    source = MockDataSource()
    for pot in ("A", "B"):
        for row in source.get_raw(pot, 20):
            assert 0.0 <= row["moisture_pct"] <= 100.0
            assert 0.0 <= row["gas_index"] <= 500.0
        for row in source.get_inference(pot, 20):
            assert 0.0 <= row["rsmi"] <= 1.0
            assert 0.5 <= row["confidence"] <= 1.0
