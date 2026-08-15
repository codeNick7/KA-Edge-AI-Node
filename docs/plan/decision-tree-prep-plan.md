# Plan — Decision Tree Prep Work (Feature Engineering + Validity Gate)

**Created:** 2026-08-03
**Status:** In progress
**Scope note:** This is *prep* work only, per the execution plan (docs/karbon-assure-edge-node-execution-plan.md).
Real trial data doesn't exist until the 16–17 Aug labeling/training step, so no model training happens here.
What's in scope, per PRD Section 10 steps 3–4 and the architecture doc's Section 11 fault-tier:

1. Feature engineering layer (RSMI conversion, gas-index passthrough, light_delta)
2. Validity gate (bounds + rate-of-change check) — built and unit-tested first, ahead of any trend logic
3. A placeholder decision tree using PRD Section 6/8's literature-seeded thresholds (not yet trained —
   just wired so it produces flags from day one, per PRD Section 5 step 1)
4. Unit tests against synthetic/mock sensor values for all of the above (no real hardware/data yet)

Explicitly out of scope for this pass: real MCU/RPC integration, SQLite evidence store, sync manager,
WiFi push, and anything requiring the real trial data collected 12–16 Aug.

---

## Directory layout

```
source/
  edge_node/
    __init__.py
    texture_table.py     # wilting point / field capacity by soil texture (citations doc item 4)
    features.py          # RSMI, gas_index passthrough, light_delta, trend slope helper
    validity.py          # validity gate (bounds + rate-of-change)
    decision_tree.py     # literature-seeded placeholder decision tree (PRD §6)
    train.py             # timing dry-run: fits DecisionTreeClassifier on synthetic mock data,
                          # optional --export to write a dependency-free plain-Python module
    export.py            # generic sklearn-tree -> plain-Python code generator (no board runtime dep)
  tests/
    __init__.py
    test_texture_table.py
    test_features.py
    test_validity.py
    test_decision_tree.py
    test_export.py       # round-trip: exported module's predictions must exactly match sklearn's
requirements.txt         # pytest, scikit-learn
pytest.ini
scripts/
  board_dry_run.sh              # run from dev machine: scp source+deps to the board, run the
                                 # same timed dry-run there, sanity-check the exported model runs
  _board_dry_run_remote.sh       # executed on the board by board_dry_run.sh (not run standalone)
  check_exported_model.py       # standalone loader for a trained_dry_run.py-style export, used
                                 # both locally and on the board to prove it needs no sklearn/numpy
```

`.venv` created at repo root (`/Users/vadirajdeshpande/workspace/Contest/.venv`), per global CLAUDE.md
instruction to always use `.venv/bin/python`.

---

## TODO

- [x] 1. Create `.venv` at repo root, install `pytest` (and nothing else — no numpy/pandas needed at this scale)
- [x] 2. `source/edge_node/texture_table.py` — texture wilting_point/field_capacity table
- [x] 3. `source/edge_node/features.py` — RSMI, gas_index passthrough, light_delta, trend slope helper
- [x] 4. `source/edge_node/validity.py` — bounds + rate-of-change validity gate
- [x] 5. `source/edge_node/decision_tree.py` — literature-seeded placeholder tree (PRD §6)
- [x] 6. Unit tests — 39 tests across texture_table/features/validity/decision_tree, synthetic values only
- [x] 7. Run `.venv/bin/python -m pytest source/tests -v` — 39/39 passed
- [x] 8. Report back — do not commit until user validates/approves
- [x] 9. Add `train.py` timing dry-run (fit a real `DecisionTreeClassifier` on synthetic mock data,
      since no real trial data exists yet) — measure actual fit time instead of estimating it
- [x] 10. Add `export.py` — generic fitted-tree -> plain-Python code generator, so the board never
      needs a scikit-learn/numpy runtime dependency (avoids version-pinning risk between this
      machine's Python 3.14 and the board's Python 3.13); round-trip-tested against sklearn's own
      `.predict()` output in `test_export.py`
- [x] 11. Script the board benchmark (`scripts/board_dry_run.sh` + helpers) so 16-17 Aug is just
      running one command with `BOARD_HOST`/`BOARD_USER` set, not re-deriving the steps under
      deadline pressure
- [ ] 12. Actually run `scripts/board_dry_run.sh` against the real board once it's reachable over
      SSH, to get real QRB2210 timing numbers (currently only simulated locally — see log below)
- [ ] 13. For the real 16-17 Aug training step: add a time-of-day feature (e.g. `hour_of_day`, or
      raw `light_lux` instead of/alongside `light_delta`) to `train.py`'s `FEATURE_NAMES` and
      `generate_synthetic_rows`, so the trained tree can learn the day/night light pattern from
      real data instead of `compute_light_delta`'s naive two-point delta hand-coding a sunrise/
      sunset heuristic. Context: `compute_light_delta` (features.py) has no diurnal awareness — a
      normal sunrise ramp reads identically to "light rising because soil is drying," which can
      spuriously satisfy `drought_onset`'s light criterion regardless of actual soil moisture.
      Decided against hand-building sunrise/sunset masking in the placeholder tree (2026-08-14
      conversation) since real light data doesn't exist yet (BH1750 not wired until 15 Aug) and
      the placeholder tree is superseded by this training step within ~2 days anyway — not worth
      building/validating day-boundary math against no data just to throw it away.

## Progress log

**2026-08-03** — Built all four modules + full test suite, all green. Mid-build, discovered
`docs/soil-health-node-citations.md` (independently verified citations doc) which **corrects the
PRD's original texture table** — the sandy/loam/clay WP/FC ranges attributed to "Cornell NMSP" don't
match any verifiable source. Reconciled:

- `texture_table.py` rewritten to use the citations doc's interim field-measured values
  (PMC7085626: sandy_loam WP11/FC23, clay_loam WP17/FC35; loam is an interpolated midpoint, not
  itself measured) — explicitly marked pending a jar-test measurement of the actual pot mix
  (60% red soil/20% clay/20% manure) + Saxton & Rawls (2006) pedotransfer calculation from that.
- Web-searched the two thresholds the citations doc didn't cover: found a peer-reviewed grounding
  for `MAX_MOISTURE_JUMP_PCT` (Dominguez-Nino et al. 2020, Sensors 20(18):5100 — capacitive sensor
  CV of 10-16% under constant conditions; threshold raised from an ungrounded 15.0 to 25.0, clearly
  above that noise ceiling) but found **no citable numeric threshold** for `light_delta_bright` or
  the gas-index trend slope scale — documented as a literature search performed, not skipped, in a
  new item 6 appended to the citations doc.
- `decision_tree.py` and `validity.py` docstrings updated to state per-threshold provenance
  explicitly (cited vs. placeholder), rather than a blanket "seeded from literature" claim.

Everything currently in `source/edge_node/` is prep-layer code only — no trend/rolling-baseline
logic, no MCU/RPC, no SQLite evidence store. Ready for real sensor data to be wired in once the
board is up (execution plan, Week 2).

**2026-08-03 (later same day)** — Training-time question: how long would fitting the decision tree
actually take, and can it run on the board. Answered both with measurements, not estimates:

- **Timing** (`train.py`, synthetic data bootstrapped from the current rule-based `classify()`, this
  dev machine): 500 rows -> 2.0ms total, 2,880 rows (the real ~5-day/2-pot collection window size) ->
  5.7ms total, 50,000 rows (17x stress test) -> 111ms total. Fit time is not the bottleneck on
  16-17 Aug; the labeling/data-curation work around it is.
- **Board transfer question**: a fitted `sklearn.tree.DecisionTreeClassifier` is portable data
  (joblib/pickle), not compiled code, so architecture (both this machine and the board are arm64)
  isn't the blocker — but the board runs Python 3.13 vs this machine's 3.14, and unpickling requires
  a matching-enough scikit-learn/numpy install on the board. Decided against shipping that runtime
  dependency onto a device meant to run unattended for years (architecture doc §9); instead built
  `export.py`, which walks the fitted tree's structure and generates a plain-Python
  `trained_predict()` function with zero external imports. Correctness verified in
  `test_export.py` by exec-ing the generated source and asserting it matches `clf.predict()` exactly
  on held-out rows — this caught a real bug (generated code referenced `np.str_(...)` without
  importing numpy, which would have crashed on the board) before it ever reached hardware.
- **Model version**: the exported module's header records `model_version`, generation timestamp,
  the scikit-learn version used to train it, training row count, and feature order — feeds the
  architecture doc's evidence-pipeline requirement (§8) to record model version alongside every
  inference.
- **Board benchmarking scripted**: `scripts/board_dry_run.sh` (run from this machine, needs
  `BOARD_HOST`/`BOARD_USER` env vars once the board has SSH reachable) copies `source/`,
  `requirements.txt`, and `check_exported_model.py` to the board, builds a fresh venv there
  (matching versions via `requirements.txt`, no pickle transferred), runs the same timed dry-run,
  and sanity-checks the exported model loads and predicts with a bare `python3` — no sklearn import
  needed. The remote logic (`_board_dry_run_remote.sh`) was simulated end-to-end locally against a
  scratch directory standing in for the board and passed cleanly (exit 0; see command output: python
  3.14.6/arm64 stand-in run, 2,880 rows -> 13.3ms fit+gen, export + standalone check both succeeded).
  Not yet run against the real board — that's the one remaining open item (#12 above), since the
  board isn't reachable over SSH from this session.
