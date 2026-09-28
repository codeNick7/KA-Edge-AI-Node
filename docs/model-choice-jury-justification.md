# Why a compact transformer classifier — jury justification

**Written:** 2026-09-15, ahead of the EFY/AIoT Design Challenge submission (deadline 2026-09-30).
**Purpose:** a readable-aloud explanation of the on-device model choice, if a jury member asks
"why this architecture and not X." Every claim below traces to a real decision made this project,
not a hypothetical — see the cross-references.

## The question we're answering

For the earlier Robu submission, we shipped a literature-seeded if/else rule tree
(`edge_node/decision_tree.py`) for live inference — not a trained model. We had already built and
validated a real trained model (`edge_node/trained_model.py`, a scikit-learn decision tree fit on
the first trial's data), but didn't finish wiring it into the live submission path in time.
`docs/pitfalls-and-jury-questions.txt` already anticipated the natural follow-up question a
technical jury would ask: **why a decision tree, and not a lightweight neural model** — is that a
hardware constraint, or a deliberate choice?

It was never a hardware constraint. The board (Arduino UNO Q, Qualcomm QRB2210 quad-core
Cortex-A53, Debian Linux) has real compute headroom for on-device AI — see
`docs/karbon-assure-edge-node-system-architecture.md` Section 2. It was a deliberate simplicity
choice made under the first deadline's time pressure. For this submission, with real trained-NN
inference as an explicit requirement, we're answering that question directly instead of dodging it
again.

## What we chose: a compact transformer encoder, classification head

The model is a small self-attention network — the same core mechanism that powers large language
models, scaled down to a handful of tokens and a few hundred parameters — trained on real sensor
data and run entirely on-device with **zero runtime dependencies** (no scikit-learn, no numpy, no
network call). Each reading's four engineered features (relative soil moisture index, light-delta,
gas-index trend, moisture trend) becomes a token; a learned `[CLS]` token attends over them; the
model outputs a probability distribution over three soil-condition states.

This is genuinely a trained neural network, not a rule table dressed up as one — the weights come
from gradient-based optimization against real trial data, not hand-picked thresholds.

## Why not a full generative small language model (text in, text out)

We considered this seriously — it's the more literal reading of "small language model," and it has
real jury appeal. We decided against it for this submission, for reasons specific to how the model
gets demonstrated, not because it's technically out of reach:

- **The demo is a live video call, not a recorded, editable video.** A classification head either
  gets the answer right or wrong — it cannot produce a malformed, truncated, or off-script output.
  A generative model, even a tiny one, can: decoding failures are a real, visible risk exactly when
  there's no room to redo the take.
- **The underlying problem is a 3-way classification, not language generation.** Generating free
  text to answer a 3-way question is solving an easier problem with a harder tool — the kind of
  "over-engineered to sound trendy" choice this project's own documentation has deliberately
  avoided elsewhere (see `docs/plan/model-training-doc-updates.md`'s framing on not overclaiming
  model capability).
- **Time budget.** With 15 days to the deadline, a generative model adds real, avoidable risk: a
  decoding loop that has to be ported to the board, and a much larger space of things that can go
  wrong in training/eval for a task that doesn't need it.

The architecture we chose keeps the actual language-model mechanism (self-attention over learned
token embeddings) — it's a smaller, safer application of the same idea, not a downgrade to
"not really AI."

## Why not the existing decision tree, retrained on this new data

We do have a working, previously-validated trained decision tree. We're not reusing it for this
round because:

1. It's still a decision tree — it doesn't answer the jury's actual question about neural
   inference, no matter how much new data it's retrained on.
2. It was trained on the *first* trial (2 days, 2 classes: `stable`/`moisture_low`) before the
   corroded soil sensor was identified and swapped (`docs/plan/nn-inference-upgrade-plan.md`,
   Phase 1b). That data quality issue doesn't touch the new architecture decision, but it's a
   reason the old model's numbers specifically shouldn't be re-cited as current.

## Why not a bigger neural network (multi-layer, wider, more tokens)

The training data, while large in row count (~1.58M post-sensor-swap readings across two pots), is
still one controlled two-pot trial — an autocorrelated time series, not independent samples. A
larger network mostly increases overfitting risk without adding capacity where it's actually
needed: a handful of engineered features and three target classes don't require deep capacity to
separate. We size the model to the data, not the other way around, and stress-tested this with a
leave-one-pot-out check (train on Pot A, test on Pot B and vice versa) in addition to the usual
chronological holdout.

**That stress test surfaced something worth stating plainly**: leave-one-pot-out accuracy is poor.
The cause, confirmed by inspection, is not the model — Pot A's `rsmi` (relative soil moisture)
feature is mathematically pinned at its ceiling value for every single post-swap reading, because
the shared `clay_loam` texture calibration clips Pot A's real, higher moisture range to 1.0 on
every row. A model trained only on Pot A literally never observes that feature vary, so it has no
way to have learned what it means anywhere else — the same blind spot the previous decision tree
would have had, just not stress-tested this way before. We're reporting this rather than omitting
it: the model **does** work well evaluated the way it will actually run (chronologically, on this
board, classifying its own future readings — see the accuracy numbers below), and does **not**
generalize to a different pot's calibration without retraining, which is an honest, scoped
limitation of this trial's data, not of the architecture. Fixing the underlying texture
calibration needs a physical jar test this timeline doesn't allow for; it's documented as follow-up
work rather than quietly worked around.

## Why only 3 output classes, not all 6 the sensors could theoretically flag

The validity gate still independently catches `sensor_fault` — deterministic, never learned, same
as before. Of the remaining five condition flags, only `stable`, `waterlogged_risk`, and
`soil_health_degrading` have real, sustained support in the trial data;
`drought_onset`/`moisture_low` occur in a near-zero number of rows (8 and ~3,600 out of over a
million, mostly from a noisy startup window, not sustained conditions) — see
`docs/plan/nn-inference-upgrade-plan.md`'s Phase 1 data audit. We're not force-fitting a model to
classes with no real ground truth; that's documented as a known scope limitation for this trial,
consistent with how this project has handled every other data-coverage gap so far.

## The real numbers

- **Architecture**: 1-block, 1-head transformer encoder ([CLS] + 4 feature tokens,
  `d_model=8`, `d_ff=16`) + linear classification head. **1,155 parameters total.**
- **Training data**: 1,265,876 rows (chronological training portion) from the real
  post-sensor-swap Pot A/B trial, `data-from-board/sept15/`.
- **Training**: hand-written forward and backward pass (no autodiff library), gradient-checked
  against numerical differentiation before training (max relative error ~1e-6 across every
  parameter tensor) — the weights are genuinely learned via gradient descent (Adam), not
  hand-tuned.
- **Accuracy (chronological split — train on the earlier 80%, test on the later 20%,
  the operationally relevant scenario for a rolling deployment)**:
  **98.9% train / 88.9% test accuracy.**
- **Export**: pure Python (`bisect`/`math` standard library only, zero numpy/scipy
  runtime dependency), round-trip-verified to match the in-memory model's predictions
  exactly on 2,000 held-out rows (0 mismatches).
- **Latency**: ~0.11ms per inference call, measured on the dev machine — comfortably under
  the PRD's <50ms target even before accounting for margin on the board's slower ARM CPU.

## The honest summary, one paragraph

We trained a small transformer — the real attention mechanism behind modern language models — on
our own controlled trial data, sized deliberately small to match the data we actually have, and
exported it to run standalone on the board with no runtime dependencies. We chose a classification
head over free-text generation because our demo is live and our problem is genuinely a
classification problem — using the harder tool wouldn't have made the answer more true, just
riskier to show.
