"""Training script — real trial data (2026-08-16) plus the original synthetic dry-run.

The synthetic dry-run mode (default, no --data) measures decision-tree fit time
against synthetic, realistically-sized mock data — it predates real trial data
(2026-08-03) and its labels are bootstrapped from the existing rule-based
edge_node.decision_tree.classify, so it is NOT a substitute for real labeled
trial data. Kept as-is for the timing-budget record in docs/plan/decision-tree-prep-plan.md.

--data mode is the real thing: fits against a training_set.jsonl built by
scripts/build_training_set.py (real Pot A/B sensor history, features from the
same pipeline dashboard/real_source.py uses live, labels from observation
overrides where available and decision_tree.classify() seed labels otherwise —
see PRD Section 5 steps 3-4). Rows in that file are pre-sorted chronologically
by scripts/build_training_set.py, so the holdout split here is a time-based
cut (train on the earlier portion, test on the later) rather than a random
shuffle — the honest evaluation for a rolling sensor deployment: can a tree
fit on earlier readings classify what comes later, not just interpolate
between neighbouring seconds' near-duplicate feature values.

Run (synthetic dry-run): .venv/bin/python -m edge_node.train --rows 2880
Run (real data):         .venv/bin/python -m edge_node.train --data training_set.jsonl --export trained_model.py
"""

import argparse
import json
import random
import time

from sklearn.tree import DecisionTreeClassifier

from .decision_tree import classify
from .export import export_model
from .validity import ValidityResult

VALID = ValidityResult(is_valid=True, validity_score=1.0, reason=None)

FEATURE_NAMES = ["rsmi", "light_delta", "gas_index_trend", "rsmi_trend"]


def generate_synthetic_rows(n_rows: int, seed: int = 0) -> tuple[list[list[float]], list[str]]:
    """Synthetic (features, label) pairs shaped like real readings.

    Labels come from the current rule-based classify() — this only tells us
    "can a tree of this shape be fit quickly," not whether the resulting
    tree is accurate. Accuracy requires real trial data.
    """
    rng = random.Random(seed)
    X: list[list[float]] = []
    y: list[str] = []
    for _ in range(n_rows):
        rsmi = rng.uniform(0.0, 1.0)
        light_delta = rng.uniform(-100.0, 100.0)
        gas_index_trend = rng.uniform(-5.0, 5.0)
        rsmi_trend = rng.uniform(-0.05, 0.05)
        flag = classify(VALID, rsmi, light_delta, gas_index_trend, rsmi_trend)
        X.append([rsmi, light_delta, gas_index_trend, rsmi_trend])
        y.append(flag.flag)
    return X, y


def time_training_run(n_rows: int, seed: int = 0) -> dict:
    gen_start = time.perf_counter()
    X, y = generate_synthetic_rows(n_rows, seed=seed)
    gen_elapsed = time.perf_counter() - gen_start

    clf = DecisionTreeClassifier(max_depth=5, random_state=seed)
    fit_start = time.perf_counter()
    clf.fit(X, y)
    fit_elapsed = time.perf_counter() - fit_start

    return {
        "n_rows": n_rows,
        "data_generation_seconds": gen_elapsed,
        "fit_seconds": fit_elapsed,
        "total_seconds": gen_elapsed + fit_elapsed,
        "n_classes_seen": len(set(y)),
        "clf": clf,
    }


def load_real_rows(path: str) -> tuple[list[list[float]], list[str]]:
    """Load a (features, label) training set built by scripts/build_training_set.py.

    Rows there are already sorted chronologically across both pots, so this
    preserves that order rather than shuffling -- train_on_real_data relies
    on it for a time-based holdout split.
    """
    X: list[list[float]] = []
    y: list[str] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            X.append([row[name] for name in FEATURE_NAMES])
            y.append(row["label"])
    return X, y


def train_on_real_data(path: str, test_frac: float = 0.2, seed: int = 0, max_depth: int = 5) -> dict:
    X, y = load_real_rows(path)
    n = len(X)
    split = int(n * (1 - test_frac))
    X_train, y_train = X[:split], y[:split]
    X_test, y_test = X[split:], y[split:]

    fit_start = time.perf_counter()
    clf = DecisionTreeClassifier(max_depth=max_depth, random_state=seed)
    clf.fit(X_train, y_train)
    fit_elapsed = time.perf_counter() - fit_start

    return {
        "n_rows": n,
        "n_train": len(X_train),
        "n_test": len(X_test),
        "fit_seconds": fit_elapsed,
        "train_accuracy": clf.score(X_train, y_train),
        "test_accuracy": clf.score(X_test, y_test) if X_test else None,
        "classes": sorted(set(y)),
        "clf": clf,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fit a decision tree — real trial data (--data) or a synthetic timing dry-run")
    parser.add_argument("--data", metavar="PATH", help="training_set.jsonl built by scripts/build_training_set.py (real data mode)")
    parser.add_argument("--test-frac", type=float, default=0.2, help="fraction held out as a chronological test set (--data mode only, default 0.2)")
    parser.add_argument("--max-depth", type=int, default=5, help="decision tree max_depth (--data mode only, default 5)")
    parser.add_argument("--rows", type=int, default=2880, help="synthetic row count (no --data)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--export",
        metavar="PATH",
        help="write the fitted tree as a dependency-free Python module to PATH",
    )
    parser.add_argument(
        "--model-version",
        default=None,
        help="model version string recorded in the exported module's header "
        "(defaults to 'synthetic-dry-run-<rows>rows-seed<seed>', or 'real-trial-<n>rows-seed<seed>' in --data mode)",
    )
    args = parser.parse_args()

    if args.data:
        result = train_on_real_data(args.data, test_frac=args.test_frac, seed=args.seed, max_depth=args.max_depth)
        test_acc_str = f"{result['test_accuracy']*100:.2f}%" if result["test_accuracy"] is not None else "n/a (no test rows)"
        print(
            f"rows={result['n_rows']} (train={result['n_train']}, test={result['n_test']}) "
            f"classes={result['classes']} "
            f"fit={result['fit_seconds']*1000:.2f}ms "
            f"train_accuracy={result['train_accuracy']*100:.2f}% "
            f"test_accuracy={test_acc_str}"
        )
        n_training_rows = result["n_train"]
        default_model_version = f"real-trial-{result['n_rows']}rows-seed{args.seed}"
    else:
        result = time_training_run(args.rows, seed=args.seed)
        print(
            f"rows={result['n_rows']} "
            f"classes_seen={result['n_classes_seen']} "
            f"data_gen={result['data_generation_seconds']*1000:.2f}ms "
            f"fit={result['fit_seconds']*1000:.2f}ms "
            f"total={result['total_seconds']*1000:.2f}ms"
        )
        n_training_rows = args.rows
        default_model_version = f"synthetic-dry-run-{args.rows}rows-seed{args.seed}"

    if args.export:
        model_version = args.model_version or default_model_version
        export_start = time.perf_counter()
        export_model(
            result["clf"],
            FEATURE_NAMES,
            args.export,
            model_version,
            n_training_rows=n_training_rows,
        )
        export_elapsed = time.perf_counter() - export_start
        print(f"exported to {args.export} (model_version={model_version!r}) in {export_elapsed*1000:.2f}ms")


if __name__ == "__main__":
    main()
