"""Tests for edge_node/tiny_transformer.py."""

import numpy as np

from edge_node.tiny_transformer import (
    CLASS_NAMES, FEATURE_NAMES, N_CLASSES, TinyTransformerClassifier,
    check_gradients, compute_bin_edges, quantize,
)


def test_gradients_match_numerical_check():
    """The hand-written backward() must agree with finite-difference gradients
    for every parameter tensor -- the standard correctness check for
    hand-rolled backprop (see tiny_transformer.py's module docstring)."""
    rng = np.random.default_rng(0)
    model = TinyTransformerClassifier(n_bins=10, d_model=6, d_ff=12, seed=1)
    X = rng.integers(0, 10, size=(12, len(FEATURE_NAMES)))
    y = rng.integers(0, N_CLASSES, size=12)

    errs = check_gradients(model, X, y)
    worst_name, worst_err = max(errs.items(), key=lambda kv: kv[1])
    assert worst_err < 1e-4, f"gradient check failed for {worst_name!r}: {worst_err:.2e}"


def test_forward_output_shape():
    model = TinyTransformerClassifier(n_bins=8, d_model=6, d_ff=12, seed=0)
    X = np.zeros((5, len(FEATURE_NAMES)), dtype=np.int64)
    logits, _ = model.forward(X)
    assert logits.shape == (5, N_CLASSES)


def test_predict_returns_valid_class_and_confidence():
    model = TinyTransformerClassifier(n_bins=8, d_model=6, d_ff=12, seed=0)
    X = np.zeros((3, len(FEATURE_NAMES)), dtype=np.int64)
    pred_idx, confidence = model.predict(X)
    assert all(0 <= i < N_CLASSES for i in pred_idx)
    assert all(0.0 <= c <= 1.0 for c in confidence)


def test_fit_reduces_loss_on_separable_toy_data():
    """Not a real accuracy target -- just proves the training loop (forward +
    backward + Adam) actually reduces loss on a trivially learnable toy
    problem, catching a wiring bug (e.g. a gradient never reaching a
    parameter) that check_gradients alone wouldn't catch in fit()'s loop."""
    rng = np.random.default_rng(0)
    n_bins = 5
    n = 300
    # Class is fully determined by feature 0's bin, trivially separable.
    X = rng.integers(0, n_bins, size=(n, len(FEATURE_NAMES)))
    y = X[:, 0] % N_CLASSES

    model = TinyTransformerClassifier(n_bins=n_bins, d_model=6, d_ff=12, seed=0)
    history = model.fit(X, y, epochs=30, batch_size=64, lr=0.05, verbose=False)
    assert history[-1] < history[0] * 0.5, "loss did not meaningfully decrease during training"

    pred_idx, _ = model.predict(X)
    accuracy = float(np.mean(pred_idx == y))
    assert accuracy > 0.9, f"failed to learn a trivially separable toy task, accuracy={accuracy:.2f}"


def test_compute_bin_edges_and_quantize_roundtrip():
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
    edges = compute_bin_edges(values, n_bins=4)
    bins = quantize(values, edges)
    assert bins.min() >= 0
    assert bins.max() < 4 + 1  # searchsorted can return len(edges), i.e. up to n_bins - 1 for unique edges
    # Monotonic values must produce monotonic (non-decreasing) bin ids.
    assert all(b1 <= b2 for b1, b2 in zip(bins, bins[1:]))
