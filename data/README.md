# Training data (2026-08-16)

`training_set.jsonl.gz` — the labeled training set fed to `edge_node/train.py --data`
to produce `source/edge_node/trained_model.py`. Built by:

```
.venv/bin/python scripts/build_training_set.py \
    <board>/pot_a_readings.jsonl <board>/pot_b_readings.jsonl \
    --observations data/observations.json \
    --out training_set.jsonl
gzip -9 training_set.jsonl
```

Source data: real board exports covering `2026-08-14T09:40:47Z` – `2026-08-16T07:56:23Z`
(already pin-swap-corrected, see `scripts/migrate_soil_pin_swap.py`). 171,010 rows,
balanced `stable` (Pot A, 85,509) / `moisture_low` (Pot B, 85,501).

`observations.json` — the ground-truth override windows applied on top of the
seed labels from `edge_node/decision_tree.classify()`:

- **Pot A** (control, watered normally twice daily): overridden to `stable` for
  the whole file. Seed labels were ~98% `waterlogged_risk` for this healthy pot —
  a `clay_loam` RSMI field-capacity miscalibration (`edge_node/texture_table.py`),
  not a real signal.
- **Pot B** (drought trial): overridden to `moisture_low` for the whole file.
  Watering stopped 2026-08-10 09:00 IST; visible wilting observed 2026-08-14
  ~10:00 IST — both predate the earliest sensor sample (2026-08-14 15:10 IST),
  confirmed against the board's `session_log.jsonl` (first-ever `session_start`
  is exactly `2026-08-14T09:40:47Z`, not a truncation from the later pin-swap
  fix). So there's no pre-wilting Pot B baseline in this data at all — the
  whole file already sits after the observed transition.

To decompress: `gunzip -k training_set.jsonl.gz`.
