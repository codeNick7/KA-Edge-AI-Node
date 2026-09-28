#!/usr/bin/env python3
"""Train the tiny transformer classifier (edge_node/tiny_transformer.py) on a
training_set.jsonl built by scripts/build_training_set.py, and export it to a
dependency-free on-device module.

Scope note: training_set.jsonl (built from the literature-seeded classify() seed
labels, per PRD Section 5 step 1) can contain all six flags; this script trains only
on the three with real, sustained support in this trial's data — stable,
waterlogged_risk, soil_health_degrading (see docs/plan/nn-inference-upgrade-plan.md's
Phase 1 data audit). sensor_fault stays the separate deterministic validity gate
(never learned); drought_onset/moisture_low rows (near-zero real support) are dropped
here, counted and reported rather than silently discarded.

Evaluation uses two split strategies, both against real held-out data:
  1. Chronological split (train on the earlier 80%, test on the later 20%) — can the
     model classify readings later than what it trained on.
  2. Leave-one-pot-out (train on Pot A, test on Pot B, and vice versa) — does the model
     generalize across pots, or did it just memorize one pot's specific trajectory.

Usage:
  .venv/bin/python scripts/train_transformer.py training_set.jsonl \\
      --export source/edge_node/trained_model.py
"""

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "source"))

from edge_node.tiny_transformer import (  # noqa: E402
    CLASS_NAMES, FEATURE_NAMES, N_CLASSES, TinyTransformerClassifier,
    compute_bin_edges, quantize,
)
from edge_node.export_transformer import export_model  # noqa: E402

IN_SCOPE_LABELS = set(CLASS_NAMES)
LABEL_TO_IDX = {name: i for i, name in enumerate(CLASS_NAMES)}


def load_rows(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def to_arrays(rows: list[dict], bin_edges: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    X = np.stack(
        [quantize(np.array([r[name] for r in rows]), bin_edges[name]) for name in FEATURE_NAMES],
        axis=1,
    )
    y = np.array([LABEL_TO_IDX[r["label"]] for r in rows])
    return X, y


def evaluate(model: TinyTransformerClassifier, X: np.ndarray, y: np.ndarray) -> float:
    pred, _ = model.predict(X)
    return float(np.mean(pred == y))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("training_set", help="training_set.jsonl from scripts/build_training_set.py")
    parser.add_argument("--n-bins", type=int, default=16, help="quantile bins per feature (default: 16)")
    parser.add_argument("--d-model", type=int, default=8)
    parser.add_argument("--d-ff", type=int, default=16)
    parser.add_argument("--epochs", type=int, default=60)
    parser.add_argument("--batch-size", type=int, default=4096)
    parser.add_argument("--lr", type=float, default=0.05)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--export", help="path to write the dependency-free trained_predict() module")
    args = parser.parse_args()

    all_rows = load_rows(args.training_set)
    print(f"Loaded {len(all_rows)} rows from {args.training_set}")

    in_scope = [r for r in all_rows if r["label"] in IN_SCOPE_LABELS]
    dropped = len(all_rows) - len(in_scope)
    print(f"In-scope (stable/waterlogged_risk/soil_health_degrading): {len(in_scope)}; "
          f"dropped (out of scope for this trial): {dropped}")

    in_scope.sort(key=lambda r: r["timestamp"])  # already sorted by build_training_set.py, defensive

    # ---- bin edges: fit ONLY on the chronological-train portion, to avoid a subtle
    # leak (bin edges derived from data the model is later "tested" on). -------------
    split_idx = int(len(in_scope) * 0.8)
    chrono_train, chrono_test = in_scope[:split_idx], in_scope[split_idx:]
    bin_edges = {
        name: compute_bin_edges(np.array([r[name] for r in chrono_train]), args.n_bins)
        for name in FEATURE_NAMES
    }
    n_bins_actual = max(len(e) for e in bin_edges.values()) + 1  # +1 since searchsorted has n_edges+1 buckets

    X_train, y_train = to_arrays(chrono_train, bin_edges)
    X_test, y_test = to_arrays(chrono_test, bin_edges)
    print(f"\nChronological split: train={len(chrono_train)} test={len(chrono_test)}")
    print(f"Train label distribution: {dict((n, int((y_train == i).sum())) for n, i in LABEL_TO_IDX.items())}")
    print(f"Test label distribution:  {dict((n, int((y_test == i).sum())) for n, i in LABEL_TO_IDX.items())}")

    model = TinyTransformerClassifier(n_bins=n_bins_actual, d_model=args.d_model, d_ff=args.d_ff, seed=args.seed)
    n_params = sum(p.size for p in model.params.values())
    print(f"\nModel: {n_params} parameters, {n_bins_actual} bins/feature")

    t0 = time.time()
    model.fit(X_train, y_train, epochs=args.epochs, batch_size=args.batch_size, lr=args.lr, seed=args.seed)
    fit_seconds = time.time() - t0

    train_acc = evaluate(model, X_train, y_train)
    test_acc = evaluate(model, X_test, y_test)
    print(f"\nFit time: {fit_seconds:.1f}s")
    print(f"Chronological split — train accuracy: {train_acc:.4f}  test accuracy: {test_acc:.4f}")

    # ---- leave-one-pot-out: does it generalize across pots, not just within one ----
    by_pot = {"A": [r for r in in_scope if r["pot"] == "A"], "B": [r for r in in_scope if r["pot"] == "B"]}
    for train_pot, test_pot in (("A", "B"), ("B", "A")):
        if not by_pot[train_pot] or not by_pot[test_pot]:
            print(f"\nLeave-one-pot-out (train={train_pot}, test={test_pot}): skipped, no rows for one side")
            continue
        edges_p = {
            name: compute_bin_edges(np.array([r[name] for r in by_pot[train_pot]]), args.n_bins)
            for name in FEATURE_NAMES
        }
        n_bins_p = max(len(e) for e in edges_p.values()) + 1
        Xp_train, yp_train = to_arrays(by_pot[train_pot], edges_p)
        Xp_test, yp_test = to_arrays(by_pot[test_pot], edges_p)
        pot_model = TinyTransformerClassifier(n_bins=n_bins_p, d_model=args.d_model, d_ff=args.d_ff, seed=args.seed)
        pot_model.fit(Xp_train, yp_train, epochs=args.epochs, batch_size=args.batch_size, lr=args.lr,
                      seed=args.seed, verbose=False)
        acc = evaluate(pot_model, Xp_test, yp_test)
        print(f"Leave-one-pot-out (train={train_pot}, test={test_pot}): accuracy={acc:.4f} "
              f"(n_train={len(by_pot[train_pot])}, n_test={len(by_pot[test_pot])})")

    if args.export:
        export_model(
            model, bin_edges, feature_names=FEATURE_NAMES, class_names=CLASS_NAMES,
            training_rows=len(chrono_train), out_path=args.export,
        )
        print(f"\nExported dependency-free model to {args.export}")
        _verify_round_trip(args.export, model, chrono_test[:2000])


def _verify_round_trip(export_path: str, model: TinyTransformerClassifier, sample_rows: list[dict]) -> None:
    """Load the just-exported pure-Python module and check its trained_predict()
    matches the in-memory model's predict() exactly on a sample of held-out rows --
    same safety net edge_node/export.py's test_export.py already has for the decision
    tree (which caught a real bug there before it ever reached hardware)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_exported_candidate", export_path)
    exported = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exported)

    mismatches = 0
    for row in sample_rows:
        features = {name: row[name] for name in FEATURE_NAMES}
        exported_label, exported_conf = exported.trained_predict(**features)

        X = np.array([[row[name] for name in FEATURE_NAMES]])
        # Re-quantize with the exact same bin edges the model was trained/exported with.
        bin_edges = {name: np.array(exported._BIN_EDGES[name]) for name in FEATURE_NAMES}
        from edge_node.tiny_transformer import quantize
        X_bins = np.stack([quantize(X[:, i], bin_edges[name]) for i, name in enumerate(FEATURE_NAMES)], axis=1)
        pred_idx, conf = model.predict(X_bins)
        model_label = CLASS_NAMES[pred_idx[0]]

        if exported_label != model_label or abs(exported_conf - conf[0]) > 0.01:
            mismatches += 1
            if mismatches <= 3:
                print(f"  MISMATCH: model={model_label}/{conf[0]:.4f} exported={exported_label}/{exported_conf:.4f} row={features}")

    status = "PASS" if mismatches == 0 else "FAIL"
    print(f"Round-trip check ({len(sample_rows)} sample rows): {status} — {mismatches} mismatch(es)")
    if mismatches:
        raise SystemExit(f"Export round-trip check failed: {mismatches}/{len(sample_rows)} rows disagree")


if __name__ == "__main__":
    main()
