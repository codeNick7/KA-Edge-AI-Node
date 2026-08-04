from dashboard.app import app
from dashboard.mock_source import MODEL_VERSION, MockDataSource


def client():
    app.testing = True
    return app.test_client()


def test_html_routes_return_200():
    c = client()
    for path in ("/", "/raw", "/inference"):
        resp = c.get(path)
        assert resp.status_code == 200
        assert b"Karbon Assure" in resp.data


def test_api_raw_returns_n_rows_with_expected_fields():
    c = client()
    resp = c.get("/api/raw?pot=A&n=10")
    assert resp.status_code == 200
    rows = resp.get_json()
    assert len(rows) == 10
    for field in ("timestamp", "moisture_pct", "gas_index", "temp_c", "light_lux"):
        assert field in rows[0]


def test_api_inference_returns_n_rows_with_expected_fields():
    c = client()
    resp = c.get("/api/inference?pot=B&n=5")
    assert resp.status_code == 200
    rows = resp.get_json()
    assert len(rows) == 5
    for field in (
        "timestamp",
        "rsmi",
        "light_delta",
        "gas_index_trend",
        "rsmi_trend",
        "flag",
        "confidence",
        "contributing_trend",
        "model_version",
    ):
        assert field in rows[0]
    assert rows[0]["model_version"] == MODEL_VERSION


def test_api_rejects_unknown_pot():
    c = client()
    for path in ("/api/raw?pot=Z", "/api/inference?pot=Z"):
        resp = c.get(path)
        assert resp.status_code == 400


def test_mock_data_source_rsmi_and_moisture_are_bounded():
    source = MockDataSource()
    for pot in ("A", "B"):
        for row in source.get_raw(pot, 20):
            assert 0.0 <= row["moisture_pct"] <= 100.0
            assert 0.0 <= row["gas_index"] <= 500.0
        for row in source.get_inference(pot, 20):
            assert 0.0 <= row["rsmi"] <= 1.0
            assert 0.5 <= row["confidence"] <= 1.0
