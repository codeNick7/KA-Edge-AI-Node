# Doc updates needed — real trained model (2026-08-16)

Both `demo/karbon_assure_edge_node_demo_1.key` and
`docs/Karbon-Assure_Edge_Node_Revised_Abstract_v3.docx` still describe the
literature-seeded placeholder tree (`edge_node/decision_tree.py`) as if it's
what's running. As of 2026-08-16 the live dashboard runs a real trained model
(`edge_node/trained_model.py`, see `data/README.md` for provenance). These are
binary formats (Keynote/.docx) — apply by hand rather than programmatic edit,
to avoid corrupting formatting.

## The one fact that matters most

**The trained model can only ever emit `stable` or `moisture_low`** (plus
`sensor_fault` from the unchanged validity gate, which runs independently
before the model). It has never seen a labeled example of `drought_onset`,
`waterlogged_risk`, or `soil_health_degrading` — this two-pot trial never
produced ground truth for any of those. Both documents currently present all
six flags as the model's live output space; that's the placeholder tree's
design space, not the trained model's actual, validated capability.

This is directly the risk `docs/pitfalls-and-jury-questions.txt` (line 23)
flagged in advance: "Don't over-fit the decision tree to two data points."
Owning it now, explicitly, is a stronger jury answer than downplaying it —
consistent with the project's existing non-claim / transparency framing.

## Real numbers to cite (replace placeholders/estimates with these)

- Training set: 171,010 labeled rows from the real Pot A/B trial
  (2026-08-14 09:40 UTC – 2026-08-16 07:56 UTC), built by
  `scripts/build_training_set.py`.
- Labels: hybrid per PRD Section 5 — `edge_node.decision_tree.classify()`
  seed labels, overridden by observed ground truth
  (`data/observations.json`) where known: Pot A (control, watered normally)
  → `stable` for the whole file; Pot B (drought trial, watering stopped
  2026-08-10, visible wilting observed 2026-08-14) → `moisture_low` for the
  whole file.
- Split: chronological (not random) — train on the earlier 80% of the trial,
  test on the later 20%, so the reported accuracy reflects "can the model
  classify readings later than what it trained on," not interpolation
  between near-duplicate adjacent-second readings.
- **Train accuracy: 92.83%. Test accuracy: 100.00%.** (`edge_node/train.py --data`)
  The train accuracy being *lower* than test is real and worth explaining if
  asked: the training portion covers early trial hours when Pot B hadn't
  dried out much yet, but the whole-file observation override still labels
  it `moisture_low` from the start (no pre-wilting Pot B baseline exists to
  label separately — see `data/README.md`). By the test period, Pot B is
  fully dried (~47% moisture vs. Pot A's ~82%), so the classes separate
  cleanly on `rsmi` alone.
- Fit time: 99ms for the full 171k-row tree (`max_depth=5`) — reinforces the
  existing "decision tree is trivially light on this hardware" talking point.

## Slide deck edits (`demo/karbon_assure_edge_node_demo_1.key`)

**Slide "The Model: Local + Scientific Learning"** — the right-hand panel
("Decision Tree — Edge Inference") still shows the placeholder's 6-flag rule
table. Replace with something like:

> **Trained Decision Tree — Edge Inference**
> Fit on 171,010 real readings from the controlled trial (2026-08-14 to
> 2026-08-16), chronological holdout: **92.8% train / 100% test accuracy**.
>
> Reading invalid → `sensor_fault` (validity gate, runs before the model)
> Pot A signature (well-watered) → `stable`
> Pot B signature (drought-stressed) → `moisture_low`
>
> *This trial validated two conditions. `drought_onset`, `waterlogged_risk`,
> and `soil_health_degrading` remain defined in the literature-seeded
> fallback tree but weren't exercised by ground truth this round — next
> trial iteration.*

Left panel ("Scientific Literature" / "Controlled Trial Data" cards) can stay
close to as-is, but change "supply real, labeled examples of healthy vs.
degrading trajectories" → "supplied the real, labeled training set the
decision tree was actually fit on" (past tense — this happened, it's not
aspirational anymore).

**Slide "Where We Go From Here"** — the italic caption currently says: "Open
items before submission: RSMI texture-table calibration, threshold
validation against trial data, and documenting BME688 placement." The
threshold-validation item is done — replace with something like: "Open items
before submission: RSMI texture-table calibration via jar test (interim
`clay_loam` values still provisional), and a second trial run to capture
`drought_onset`/`waterlogged_risk`/`soil_health_degrading` ground truth."

**Slide "From Readings to Trend: The Dashboard"** — the illustrative flag
history example (`moisture_low` → `drought_onset` → `soil_health_degrading`)
uses flags the trained model can't currently produce. Either swap it for a
real possible sequence (`stable` → `moisture_low`, matching what the trained
model actually does), or keep it explicitly labeled illustrative/placeholder
if you want to preserve the fuller-taxonomy aspiration — but don't let it
read as a real captured trace.

## Abstract edits (`docs/Karbon-Assure_Edge_Node_Revised_Abstract_v3.docx`)

**Section 2 (Core Architecture), step 3 "Detect"** currently reads: "executes
a lightweight, explainable decision tree to flag conditions such as drought
onset, waterlogging, degrading microbial activity, or sensor faults." Narrow
to what's validated: "...to flag soil-moisture state (`stable` vs.
`moisture_low` in the current trained model; `drought_onset`,
`waterlogged_risk`, and `soil_health_degrading` remain defined in a
literature-seeded fallback tree pending ground truth from a future trial),
each with a confidence score."

**Section 6 (Validation Plan)** currently reads: "The decision tree's
drought-onset flag is evaluated against the controlled two-pot trial's known
ground truth..., targeting correct flag timing within one observation cycle
of the imposed watering change, with precision/recall reported against the
trial's labeled data in the final submission." This is now stale — it reads
as a future plan for something already completed with different results.
Replace with the real numbers section above, and be explicit that
`drought_onset` itself was never the flag evaluated (Pot B's actual
ground-truth label was `moisture_low`, not `drought_onset` — the trial never
produced a `drought_onset` example, since that leaf requires bright light
+ low RSMI and no light data was collected). Suggested replacement:

> The trained decision tree (`edge_node/trained_model.py`) was fit on
> 171,010 real readings from the controlled two-pot trial, using the known
> ground truth (Pot A watered normally → `stable`; Pot B's watering stopped
> 2026-08-10, visible wilting observed 2026-08-14 → `moisture_low`) as
> labels. A chronological holdout split (train on the trial's earlier 80%,
> test on the later 20%) gives 92.8% train accuracy and 100% test accuracy.
> This validates the model's ability to distinguish a well-watered from a
> drought-stressed pot; it does not yet cover waterlogging or independent
> light-driven drought detection, since this trial didn't produce ground
> truth for either — noted as a specific target for the next trial
> iteration rather than left implicit.

## Not edited

I did not touch `docs/karbon-assure-edge-node-PRD.md`,
`docs/karbon-assure-edge-node-execution-plan.md`, or
`docs/pitfalls-and-jury-questions.txt` — the PRD's Section 5/6 describe the
*plan*, which is still accurate as a plan; the execution plan's 16-17 Aug
step is now genuinely complete (worth a status note if you want one); and
the pitfalls doc's line 23/47 warnings are still exactly the right jury
answers, now with real numbers to back them instead of a hypothetical.
