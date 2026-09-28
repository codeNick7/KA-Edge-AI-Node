"""Round-trip test for edge_node/export_transformer.py: the generated pure-Python
module's trained_predict() must match the in-memory TinyTransformerClassifier's
predict() exactly (within floating-point rounding) on held-out rows -- same pattern
as edge_node/tests/test_export.py already uses for the decision-tree exporter, which
caught a real bug (a missing numpy import in generated code) before it reached
hardware. scripts/train_transformer.py runs this same check against the actual
trained model at export time; this test proves the exporter logic itself is correct
on a small, fast, from-scratch model.
"""

import importlib.util
import os

import numpy as np
import pytest

from edge_node.export_transformer import export_model
from edge_node.tiny_transformer import (
    CLASS_NAMES, FEATURE_NAMES, N_CLASSES, TinyTransformerClassifier,
    compute_bin_edges, quantize,
)


@pytest.fixture
def fitted_model_and_bins():
    rng = np.random.default_rng(0)
    n = 400
    raw = {name: rng.normal(size=n) for name in FEATURE_NAMES}
    bin_edges = {name: compute_bin_edges(raw[name], n_bins=10) for name in FEATURE_NAMES}
    n_bins_actual = max(len(e) for e in bin_edges.values()) + 1

    X_bins = np.stack([quantize(raw[name], bin_edges[name]) for name in FEATURE_NAMES], axis=1)
    y = rng.integers(0, N_CLASSES, size=n)

    model = TinyTransformerClassifier(n_bins=n_bins_actual, d_model=6, d_ff=12, seed=0)
    model.fit(X_bins, y, epochs=10, batch_size=64, lr=0.05, verbose=False)
    return model, bin_edges, raw


def test_exported_module_matches_in_memory_predictions(tmp_path, fitted_model_and_bins):
    model, bin_edges, raw = fitted_model_and_bins
    out_path = tmp_path / "exported_model.py"
    export_model(
        model, bin_edges, feature_names=FEATURE_NAMES, class_names=CLASS_NAMES,
        training_rows=len(raw[FEATURE_NAMES[0]]), out_path=str(out_path),
    )

    spec = importlib.util.spec_from_file_location("exported_candidate", str(out_path))
    exported = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exported)

    assert exported.FEATURE_NAMES == FEATURE_NAMES
    assert exported.CLASS_NAMES == CLASS_NAMES

    n = len(raw[FEATURE_NAMES[0]])
    X_bins = np.stack([quantize(raw[name], bin_edges[name]) for name in FEATURE_NAMES], axis=1)
    pred_idx, confidence = model.predict(X_bins)

    for i in range(n):
        features = {name: float(raw[name][i]) for name in FEATURE_NAMES}
        exported_label, exported_conf = exported.trained_predict(**features)
        assert exported_label == CLASS_NAMES[pred_idx[i]], f"row {i}: label mismatch"
        assert abs(exported_conf - confidence[i]) < 0.01, f"row {i}: confidence mismatch"


def test_exported_module_has_no_numpy_import(tmp_path, fitted_model_and_bins):
    """The whole point of the export -- must run standalone on the board with
    no numpy/scipy runtime dependency (architecture doc Section 9)."""
    model, bin_edges, raw = fitted_model_and_bins
    out_path = tmp_path / "exported_model.py"
    export_model(
        model, bin_edges, feature_names=FEATURE_NAMES, class_names=CLASS_NAMES,
        training_rows=1, out_path=str(out_path),
    )
    source = out_path.read_text()
    assert "import numpy" not in source
    assert "import scipy" not in source
