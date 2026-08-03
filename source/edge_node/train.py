"""Training dry-run script — timing only, not a real model yet.

There is no real trial data as of 2026-08-03 (pots not yet split, execution
plan Week 2). This script exists to measure actual decision-tree fit time
against synthetic, realistically-sized mock data, so the 16-17 Aug training
step (execution plan) has a known time budget instead of a guess. Labels
are bootstrapped from the existing rule-based edge_node.decision_tree.classify
so the synthetic set is at least self-consistent with the current threshold
logic — it is NOT a substitute for real labeled trial data.

Run: .venv/bin/python -m edge_node.train --rows 2880
"""

import argparse
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Time a decision-tree fit on synthetic data")
    parser.add_argument("--rows", type=int, default=2880, help="synthetic row count")
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
        "(defaults to 'synthetic-dry-run-<rows>rows-seed<seed>')",
    )
    args = parser.parse_args()

    result = time_training_run(args.rows, seed=args.seed)
    print(
        f"rows={result['n_rows']} "
        f"classes_seen={result['n_classes_seen']} "
        f"data_gen={result['data_generation_seconds']*1000:.2f}ms "
        f"fit={result['fit_seconds']*1000:.2f}ms "
        f"total={result['total_seconds']*1000:.2f}ms"
    )

    if args.export:
        model_version = args.model_version or f"synthetic-dry-run-{args.rows}rows-seed{args.seed}"
        export_start = time.perf_counter()
        export_model(
            result["clf"],
            FEATURE_NAMES,
            args.export,
            model_version,
            n_training_rows=args.rows,
        )
        export_elapsed = time.perf_counter() - export_start
        print(f"exported to {args.export} (model_version={model_version!r}) in {export_elapsed*1000:.2f}ms")


if __name__ == "__main__":
    main()
