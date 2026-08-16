import json

from edge_node.train import FEATURE_NAMES, load_real_rows, train_on_real_data


def _write_rows(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _fixture_rows(n_stable=40, n_low=40):
    """Rows shaped like scripts/build_training_set.py's output, chronologically
    sorted: a block of clearly-"stable" feature values followed by a block of
    clearly-"moisture_low" ones, so a shallow tree can separate them perfectly
    and the chronological holdout split still sees both classes.
    """
    rows = []
    for i in range(n_stable):
        rows.append({
            "pot": "A", "timestamp": f"2026-08-14T00:00:{i:02d}+00:00",
            "rsmi": 0.6, "light_delta": 0.0, "gas_index_trend": 0.0, "rsmi_trend": 0.0,
            "seed_label": "stable", "seed_confidence": 0.5,
            "label": "stable", "label_source": "observed", "observation_note": "",
        })
    for i in range(n_low):
        rows.append({
            "pot": "B", "timestamp": f"2026-08-14T01:00:{i:02d}+00:00",
            "rsmi": 0.1, "light_delta": 0.0, "gas_index_trend": 0.0, "rsmi_trend": 0.0,
            "seed_label": "moisture_low", "seed_confidence": 0.5,
            "label": "moisture_low", "label_source": "observed", "observation_note": "",
        })
    return rows


def test_load_real_rows_reads_features_and_label_in_order(tmp_path):
    path = tmp_path / "training_set.jsonl"
    _write_rows(path, _fixture_rows(n_stable=2, n_low=2))

    X, y = load_real_rows(str(path))

    assert len(X) == len(y) == 4
    assert y == ["stable", "stable", "moisture_low", "moisture_low"]
    assert X[0] == [0.6, 0.0, 0.0, 0.0]
    assert len(X[0]) == len(FEATURE_NAMES)


def test_train_on_real_data_fits_and_holds_out_chronologically(tmp_path):
    path = tmp_path / "training_set.jsonl"
    _write_rows(path, _fixture_rows(n_stable=40, n_low=40))

    result = train_on_real_data(str(path), test_frac=0.2, seed=0, max_depth=5)

    assert result["n_rows"] == 80
    assert result["n_train"] == 64
    assert result["n_test"] == 16
    # last 20% chronologically is the tail of the moisture_low block only
    assert result["classes"] == ["moisture_low", "stable"]
    assert result["train_accuracy"] == 1.0
    assert result["test_accuracy"] == 1.0


def test_train_on_real_data_empty_test_frac_reports_no_test_accuracy(tmp_path):
    path = tmp_path / "training_set.jsonl"
    _write_rows(path, _fixture_rows(n_stable=5, n_low=5))

    result = train_on_real_data(str(path), test_frac=0.0, seed=0, max_depth=3)

    assert result["n_test"] == 0
    assert result["test_accuracy"] is None
