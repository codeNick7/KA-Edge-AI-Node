# Plan — Replace decision-tree inference with a real trained NN, on-device (contest deadline: ~20 days)

**Created:** 2026-09-09
**Updated:** 2026-09-10 — Phase 1 data audit done, surfaced a likely sensor-calibration
issue; blocked again pending physical sensor check.
**Updated:** 2026-09-15 — Unblocked: `data-from-board/sept15/` has ~4.5 days of clean
post-swap data (more than the "2 more days" target). Target board confirmed as the
**Arduino UNO Q** (Qualcomm QRB2210, Debian Linux side) — matches this doc's original
QRB2210 assumption. Architecture decision changed from "MLP (leading candidate)" to a
**compact transformer encoder + classification head**: the contest track is EFY/AIoT
(deadline 2026-09-30), which explicitly requires real trained-NN inference; user wants
to market this as a small trainable SLM, and a classification head (not free-text
generation) was chosen for live-demo safety — see `docs/model-choice-jury-justification.md`
for the full reasoning. Phase 2/3/4 below updated to reflect what was actually built.
**New finding (Phase 2, 2026-09-15):** leave-one-pot-out training surfaced a real
calibration issue, not a model defect — see Phase 2's results below.
**Why now:** DigiKey contest rules require the UNO Q board specifically AND require AI
inference actually running on the board. The current `edge_node/trained_model.py` is a
real sklearn `DecisionTreeClassifier` fit on real trial data, but exported as a
dependency-free nested if/else for on-device deployment — defensible, but an obvious
"is that really AI" target (see `docs/pitfalls-and-jury-questions.txt`, Qualcomm-panel
question: "why a decision tree and not a lightweight neural model — hardware constraint
or deliberate choice?"). Board (QRB2210, quad-core Cortex-A53, 2GB RAM, Debian Linux) is
NOT the constraint — it was a deliberate simplicity choice, per
`docs/karbon-assure-edge-node-system-architecture.md` Section 2/13.

## Decisions already made (with user, 2026-09-09)
- Data handoff: user will copy the board's current (Jul 30–now) growing
  `pot_a_readings.jsonl` / `pot_b_readings.jsonl` to this Mac themselves (not pulled by me).
  The `data-from-board/` snapshot currently in the repo only covers Aug 14–16 (~171k rows,
  gitignored) and is stale — that's what the existing tree was trained on.
- Model architecture: NOT decided yet — hold off until the fuller dataset is in hand and
  audited (class balance, real trend/signal diversity, row count). Leading candidate is a
  small MLP (1-2 hidden layers, numpy forward pass) over engineered features, chosen for:
  genuinely being a trained NN, small enough to avoid compounding the overfitting risk the
  pitfalls doc already flags for a 2-pot trial (more rows ≠ more independent trials — still
  autocorrelated single-pot time series), and cheap enough not to fight the dashboard for
  CPU. A sequence/attention model over the raw trailing window is the fallback option if the
  data supports it, but higher risk (more overfitting surface, more new code/dependency
  risk in 20 days). Revisit this decision explicitly once Phase 1 data audit is done —
  do not silently default to MLP without checking back in.
- Process architecture: inference moves OUT of the Flask request path (today it's computed
  inline in `real_source.py`'s catch-up logic on-demand per request — the likely cause of
  the CPU bursts on initial load / filter changes / refresh) and into an independent
  long-running background service on the board, given higher OS scheduling priority
  (nice / cgroup cpu.weight) than the Flask dashboard process, so dashboard load can never
  delay or starve inference. Dashboard becomes purely a *reader* of the inference sidecar
  files the service produces — same `pot_{a,b}_inference_cache.jsonl` sidecar shape
  `real_source.py` already reads today, just written by a different process.
- Label scope (confirmed 2026-09-10, after Phase 1 audit): 3 classes only —
  `stable` / `waterlogged_risk` / `soil_health_degrading`. `sensor_fault` remains the
  separate deterministic validity gate. `drought_onset`/`moisture_low` dropped from model
  scope — see Phase 1 findings, near-zero real support in this trial's data.
- Labels: distill the existing literature-seeded rule tree
  (`edge_node/decision_tree.classify()`, already multi-class: sensor_fault / drought_onset /
  moisture_low / waterlogged_risk / soil_health_degrading / stable) as the pseudo-labeler for
  the fuller dataset, per user's own framing ("combine sensor readings to get close to
  these"). `scripts/build_training_set.py` already supports exactly this (seed_label from
  classify(), optional observed-label override windows). Extend classify()'s inputs with
  temp/humidity signals if the data audit shows they carry real separating signal beyond
  the current rsmi/light_delta/gas_index_trend/rsmi_trend features — TBD in Phase 1.

## Known related work already in flight
- `docs/plan/dashboard-unbounded-client-buffer.md`: client-side buffer growth + Flask
  single-threaded blocking (today/week filters) already diagnosed and largely fixed
  (`threaded=True`, bounded client buffers, periodic resync). Not yet reverified on the
  board. This addresses *some* of the CPU-burst symptom but not the "inference computed
  inline in the request path" issue — that's this plan's job.
- `docs/plan/decision-tree-prep-plan.md`, `docs/plan/model-training-doc-updates.md`: prior
  planning docs for the existing tree; read before duplicating any of that groundwork.

## Phases

### Phase 0 — blocked, waiting on user
- [ ] User copies fresh board data (pot_a/pot_b readings, full Jul 30–now history) to this
      Mac. Until this lands, no training/model work proceeds — do not build against the
      stale Aug 14–16 snapshot as a substitute without saying so explicitly.

### Phase 1 — data audit (DONE 2026-09-10, against `data-from-board/sept09/data/`)
- [x] Row counts: 1,168,081 (Pot A) / 1,168,032 (Pot B) rows, Aug 14 → Sep 9. 13 gaps >2min
      (matches `session_log.jsonl` restarts, largest ~8.7h Aug 24-25) — normal WiFi/reboot
      gaps, not data corruption. 4 malformed JSONL lines total, skipped (partial-write
      artifacts, same tolerance pattern as `real_source.py`/`build_training_set.py`).
- [x] Re-ran `scripts/build_training_set.py` (seed labels via `decision_tree.classify()`)
      over the full pull → label distribution: waterlogged_risk 1,199,363 (51%), stable
      897,829 (38%), soil_health_degrading 235,272 (10%), moisture_low 3,604 (0.15%),
      drought_onset 8, sensor_fault 4.
- [x] **Finding — likely sensor calibration/fault issue, not organic soil behavior**:
      Pot A's moisture climbed from ~80% (Aug 14) to sitting at an exact, zero-jitter
      100.00% for entire days (Sep 7, Sep 9); 133,474 of its 1.17M rows (~11%) are precisely
      `100.0`. A real capacitive sensor should show noise even near saturation — this reads
      as the sensor pinned at its ADC ceiling, not necessarily true soil state. Since RSMI
      clips to 1.0 once this happens, every one of those rows auto-labels
      `waterlogged_risk`, which is why that class dominates the whole dataset.
      **User's read (2026-09-10)**: Pot A has been watered regularly but is not
      over-watered, no standing water above the soil observed — suspects the sensor surface
      itself may be stuck/reading a fixed high value regardless of actual soil moisture.
      Plans to wipe both sensors dry and re-seat them to observe behavior after.
- [x] **Finding — Pot B never reached "dry"**: moisture stayed in a 42–83% band the whole
      trial (mostly 47-50% after the first 2 days) — under clay_loam RSMI bounds (WP=34/
      FC=70) this never drops below RSMI≈0.22, just above the 0.20 drought/moisture_low
      cutoff. That's why those two classes are ~unpopulated (8 and 3,604 rows, mostly from
      the noisy Aug 14-15 startup window before Pot B settled, not sustained dry
      conditions). **User's read**: top of Pot B's soil is visibly dry; suspects the
      clay/manure mix may be holding moisture at depth where the probe sits, masking the
      dry-at-surface condition from the sensor. Also re-checking this sensor as part of the
      same physical inspection.
- [x] Only 3 classes have real, sustained support: `stable`, `waterlogged_risk`,
      `soil_health_degrading`. Decision (user, 2026-09-10): **scope the model to these 3
      classes**; `sensor_fault` stays the existing deterministic validity gate (never
      learned); `drought_onset`/`moisture_low` are out of scope for this trial's data —
      document as a known limitation rather than force-fit a model to 8-and-3,604-row
      classes.

### Phase 1b — physical sensor re-check (DONE 2026-09-10)
- [x] Root cause found: Pot A's capacitive sensor was severely rusted/corroded, outer
      coating wiped off exposing bare copper. Pot B's sensor was clean. User swapped the
      two physical sensor *units* between pots at **2026-09-10 ~13:53 IST = 08:21-08:23
      UTC** (wiring/pins unchanged — pot_a_readings.jsonl / pot_b_readings.jsonl still
      correctly correspond to physical location A/B, no pin-swap-style remapping needed
      this time).
- [x] Verified against `data-from-board/sept09-post-sensor-swap/data/` (fresh pull,
      1,207,559 / 1,207,500 rows, now runs to 2026-09-10T12:19 UTC):
  - Pot A: pinned at exact `100.0` continuously right up to **2026-09-10T08:21:11 UTC**
    (last occurrence in the whole file), then transitions through a brief handling-noise
    bucket (~08:20-08:30 UTC, expected — sensors were physically out of the soil), then
    settles into a real, slowly-declining trend: 91.8% → 89.9% over the next ~4h. Zero
    `100.0` readings anywhere after the swap. This confirms the flatline was the corroded
    sensor, not real soil state, and the swap fixed it for Pot A.
  - Pot B: was steady ~49% before the swap, now steady ~52-53% after (i.e. now running the
    formerly-rusted unit) — no pinning or obviously erratic behavior yet in this ~4h
    window. Caveat: corrosion effects can be progressive (drift/failure over time, not
    necessarily immediate) — worth continuing to watch this pot's sensor specifically over
    the next few days rather than assuming it's fine because the first 4h looked normal.
  - Calibration-offset risk (SOIL_DRY_ADC=1020/SOIL_WET_ADC=350 in `sketch.ino:73-74` are
    global constants, not per-unit calibrated) is still live and unverified — the ~3pt
    shift in Pot B's baseline (49%→52%) after the swap is consistent with this, though too
    small to distinguish from a real explanation on 4h of data.
- [x] Training-set boundary confirmed: **exclude all Pot A rows before
      2026-09-10T08:23:00 UTC** from training (corroded-sensor artifact, not real soil
      signal) — matches the finding, not just a suspicion anymore.
- [ ] **Still open**: only ~4h of clean post-swap data exists so far. Need a longer
      post-swap collection window before this data is trustworthy enough to retrain
      against — a few hours isn't enough to see a real wet/dry cycle or catch slow sensor
      drift (especially on Pot B's now-corroded unit). Recommend waiting at least a few
      days of continued collection before finalizing Phase 2's training pull; re-check Pot
      B's readings for pinning/erratic behavior each time new data comes in during that
      window, not just once.

### Phase 2 — model prototyping (DONE 2026-09-15, against `data-from-board/sept15/`)
- [x] Built `training_set.jsonl` via existing `scripts/build_training_set.py`, Pot A
      pre-filtered to exclude corroded-sensor rows before 2026-09-10T08:23:00 UTC (Phase 1b
      boundary). 1,582,346 in-scope rows (stable/waterlogged_risk/soil_health_degrading).
- [x] Trained `TinyTransformerClassifier` (`source/edge_node/tiny_transformer.py`) —
      1-block/1-head transformer encoder ([CLS] + 4 feature tokens) + linear classification
      head, 1,155 parameters. Hand-written forward/backward pass (no autodiff library),
      gradient-checked against numerical differentiation (max relative error ~1e-6 across
      every parameter tensor) before training.
- [x] Chronological split (train earlier 80%, test later 20%): **97.9% train / 88.4% test
      accuracy** after adding loss-checkpointing (Adam with a fixed step size was
      overshooting late in training — kept the lowest-training-loss epoch's weights instead
      of always the last epoch's).
- [x] **Leave-one-pot-out finding**: train=A/test=B and train=B/test=A both perform poorly
      (near-zero). Root cause confirmed NOT a model defect: Pot A's `rsmi` feature is exactly
      `1.000` for literally every post-swap row (and `rsmi_trend` exactly `0.0`) — the
      `clay_loam` RSMI texture bounds (WP=34/FC=70, `texture_table.py`) clip Pot A's real
      89-100% moisture range to the ceiling on every single reading, so a model trained only
      on Pot A never sees those features vary at all. Pre-existing calibration issue
      (`texture_table.py` already flagged `clay_loam` as provisional), surfaced by this
      stress test, not caused by it. **Decision (user, 2026-09-15): ship using the
      chronological split as the operationally relevant metric** (this board classifies its
      own pots' future readings, not a foreign pot's) — documented as a known limitation
      rather than silently ignored. See `docs/model-choice-jury-justification.md`.

### Phase 3 — dependency-free export for the board (DONE 2026-09-15)
- [x] `source/edge_node/export_transformer.py` — pure-Python (stdlib `bisect`/`math` only,
      no numpy/scipy) re-implementation of the fitted transformer's forward pass, round-trip
      tested against the in-memory model's predictions. Sidesteps the "is numpy safe on the
      board" question below entirely — it's no longer needed on-device.
- [x] ~~Confirm numpy is safe/available on the board~~ — moot, superseded by the pure-Python
      export above.

### Phase 4 — on-device service split (DONE 2026-09-15, revised scope)
- [x] Extracted inference computation out of `real_source.py`'s catch-up path into a new
      standalone `scripts/inference_service.py` (`PotService`, byte-offset + rolling-window
      state persisted to a small JSON file per pot) — same sidecar schema, so nothing
      downstream changed shape.
- [x] `real_source.py` is now a pure reader of `pot_{a,b}_inference_cache.jsonl` — all
      `edge_node.features`/`validity`/`trained_model` imports and computation removed, same
      tail-cache/streaming-decimation pattern as the raw-readings read path.
- [ ] Standing deployment mechanism on the board: plain background process for now
      (`nohup .venv/bin/python scripts/inference_service.py --watch &`) — a real systemd unit
      (with nice/cgroup priority above Flask) is a nice-to-have, not required, since the
      dashboard now only runs on-demand (trend checks / the live demo), not continuously, so
      CPU contention between the two is far less likely than the original Phase 4 concern
      assumed.

### Phase 5 — validation
- [x] `.venv/bin/python -m pytest source/tests` — 99/99 passed (includes new
      `test_tiny_transformer.py`, `test_export_transformer.py`, `test_inference_service.py`,
      and the `test_real_source.py`/`test_dashboard.py` tests rewritten for the pure-reader
      split). `py_compile` clean on all new/edited files.
- [x] Accuracy: **98.9% train / 88.9% test** (chronological split) — see
      `docs/model-choice-jury-justification.md` for the full number set and the
      leave-one-pot-out finding. Old decision tree's number (100% test) isn't a fair
      comparison — that tree was fit on the pre-swap 2-day/2-class trial, this model on the
      post-swap 3-class trial; not the same task.
- [x] Latency: **~0.11ms/call** on the dev machine (pure-Python `trained_predict()`),
      comfortably under the <50ms PRD target. Not yet confirmed on the real QRB2210 — dev
      machine is a reasonable proxy but board-side confirmation is still a follow-up
      (`scripts/board_dry_run.sh` exists for the decision tree; would need a small variant
      for this model, not yet built — flagged, not silently skipped).
- [ ] Confirm the priority mechanism actually protects inference under dashboard load —
      superseded by the revised Phase 4 scope: dashboard now runs on-demand only, so this is
      lower-priority than originally planned; revisit only if contention is actually observed.

### Phase 6 — docs
- [ ] Update PRD Section 6 / architecture doc Section 13 (already names this exact upgrade
      as a roadmap item — "GenAI / local SLM upgrade path" — update to reflect what was
      actually built, not oversell it as more than it is).
- [ ] Update `docs/pitfalls-and-jury-questions.txt` framing for the "why not a NN" question
      — it flips to "why NOT a bigger model" now; keep the same honesty about trial size
      that document already models.
- [ ] Record model_version / provenance the same way `trained_model.py`'s header does today.

## Non-goals (unless user asks explicitly)
- No pretrained Hugging Face model — no off-the-shelf model fits tabular soil-sensor
  features; would need to be trained from scratch on this project's own data regardless.
- No literal reuse of `tiny_transformer.py` (character-level text generation, architecturally
  unrelated) — it's a reference for attention mechanics only, not directly portable.
