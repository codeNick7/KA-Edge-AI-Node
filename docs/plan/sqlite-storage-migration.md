# Storage migration: JSONL → SQLite (board + training pipeline)

Status: DRAFT — plan only, no implementation yet. Written 2026-09-16.
Supersedes the JSONL-rolling-window part of `docs/plan/board-disk-backup-routine.md`
(that doc's evidence/root-cause sections and the `sync_queue_cloud.db`/`flush_pending()`
findings still stand — only the "keep truncating JSONL + scp backups" mechanism is
replaced by this migration).

## Why this instead of just truncating JSONL

Same underlying disk-pressure problem (see `board-disk-backup-routine.md`'s evidence
table: ~40MB/day combined growth across 4 JSONL files, 16GB eMMC total, no existing
retention). Decided 2026-09-16 to replace the storage format itself rather than bolt a
rotation policy onto flat files:
- SQLite rows are meaningfully more compact than JSONL text (no repeated key names/
  braces/quotes per row).
- Native SQL aggregation (`MIN`/`MAX`/`AVG`/`GROUP BY date`) replaces the hand-rolled
  rollup logic Phase 1 would have needed to write from scratch.
- Native indexing/range queries (`WHERE ts > ?`) replace `real_source.py`'s hand-built
  byte-offset tail-cache and `_stream_decimated` machinery — that file's complexity
  (see its own "OOM fix"/"Memory model" docstring) existed specifically to work around
  JSONL having no indexing; a lot of it goes away with SQL doing the same job natively.

**This does NOT remove the need for a retention policy.** Unless old rows are pruned
(`DELETE ... WHERE ts < cutoff`) and the file is periodically compacted
(`VACUUM` or `PRAGMA incremental_vacuum`), a SQLite table grows unbounded exactly like
`sync_queue_cloud.db` already did in this same codebase (272MB, 302,891 rows, never
pruned — see `board-disk-backup-routine.md`). Retention is designed into the schema
from day one here, not deferred.

**Live disk numbers**: still pending — user is running `ssh $BOARD df -h $DEST`
directly. Retention window / how urgent this is gets finalized once that's in hand.

## Decisions so far (2026-09-16)

1. **Full migration**: backfill existing JSONL history into SQLite (not just
   new-data-forward) — single source of truth, existing history stays queryable.
2. **Training pipeline included**: `build_training_set.py` / `train_transformer.py` /
   `tiny_transformer.py` / `export_transformer.py` get updated to read SQLite natively,
   not just the live board path (`main.py`, `inference_service.py`, `real_source.py`).
3. `sync_queue_cloud.db` stays out of scope (separate bug, separate fix already landed
   for `flush_pending()`; device-registration 404 still needs investigating separately).

## Proposed schema (needs sign-off before implementation)

One shared SQLite file per board, e.g. `python/data/sensor_data.db`, one shared helper
module (new file, e.g. `KA-Edge-source/python/board_db.py`) used by every reader/writer
below so the schema/connection setup lives in exactly one place.

```sql
CREATE TABLE readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pot_id TEXT NOT NULL,              -- 'A' | 'B'
    sample_index INTEGER NOT NULL,
    millis_ms INTEGER NOT NULL,
    temp_c REAL NOT NULL,
    humidity_pct REAL NOT NULL,
    pressure_hpa REAL NOT NULL,
    gas_kohm REAL NOT NULL,
    light_lux REAL NOT NULL,
    moisture_pct REAL NOT NULL,
    received_at TEXT NOT NULL          -- ISO8601, same format as today's JSONL
);
CREATE INDEX ix_readings_pot_ts ON readings(pot_id, received_at);

CREATE TABLE inference_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pot_id TEXT NOT NULL,
    reading_id INTEGER REFERENCES readings(id),
    timestamp TEXT NOT NULL,
    rsmi REAL NOT NULL,
    light_delta REAL NOT NULL,
    gas_index_trend REAL NOT NULL,
    rsmi_trend REAL NOT NULL,
    flag TEXT NOT NULL,
    confidence REAL NOT NULL,
    contributing_trend TEXT NOT NULL,
    model_version TEXT NOT NULL
);
CREATE INDEX ix_inference_pot_ts ON inference_results(pot_id, timestamp);

-- Permanent (never pruned) daily aggregates -- survives raw-row deletion, backs
-- collecting_since / total-count / coarse long-range chart context.
CREATE TABLE raw_daily_rollup (
    pot_id TEXT NOT NULL, date TEXT NOT NULL,   -- UTC date, 'YYYY-MM-DD'
    count INTEGER NOT NULL,
    temp_min REAL, temp_max REAL, temp_mean REAL,
    moisture_min REAL, moisture_max REAL, moisture_mean REAL,
    gas_min REAL, gas_max REAL, gas_mean REAL,
    light_min REAL, light_max REAL, light_mean REAL,
    first_ts TEXT, last_ts TEXT,
    PRIMARY KEY (pot_id, date)
);

CREATE TABLE inference_daily_rollup (
    pot_id TEXT NOT NULL, date TEXT NOT NULL,
    count INTEGER NOT NULL,
    flag_counts_json TEXT NOT NULL,     -- {"stable": 41000, "sensor_fault": 12, ...}
    rsmi_min REAL, rsmi_max REAL, rsmi_mean REAL,
    confidence_mean REAL,
    model_versions_json TEXT NOT NULL,  -- distinct model_version values seen that day
    first_ts TEXT, last_ts TEXT,
    PRIMARY KEY (pot_id, date)
);
```

Retention: a periodic (daily, board-local — no scp round-trip needed for this part)
job rolls yesterday's-and-older `readings`/`inference_results` rows into the two
rollup tables, then `DELETE`s them, then runs `PRAGMA incremental_vacuum`. Exact
window (how many days of full-resolution rows stay queryable) still pending the live
disk-space numbers.

**Resolved 2026-09-16** (live board numbers now in hand): `df -h /home` shows 9.8G
total, 6.7G used, **2.7G avail (72% used)** — smaller than the 16GB the architecture
doc assumed (that's the whole eMMC; `/home` is one partition on it). At the measured
~53MB/day combined growth rate, that's ~42 days to the 500MB floor — not an
immediate emergency, but confirms unbounded growth is real and will matter well
before "years of unattended operation." Decision: keep the **7-day full-resolution
retention window** (already agreed before the SQLite pivot) with permanent rollup —
cheap to build in now, matches the architecture doc's own §9 policy, and avoids
re-litigating this once the migration is live. The original JSONL files stay on the
board as a `.migrated`-renamed backup until the SQLite path is verified end to end
(not deleted immediately even after migration), so raw resolution beyond 7 days isn't
actually lost during the initial validation period.

## Files this touches

**Board runtime:**
- New `KA-Edge-source/python/board_db.py` — connection/schema helper (WAL mode,
  matching `sync_queue.py`'s existing convention for durability).
- `KA-Edge-source/python/main.py` — `handle_reading()` inserts into `readings`
  instead of appending to `pot_{a,b}_readings.jsonl`.
- `scripts/inference_service.py` — `PotService` reads un-inferenced rows from
  `readings` (e.g. tracking last-processed `id` instead of a byte offset) and inserts
  into `inference_results` instead of appending to the JSONL sidecar. The
  `.inference_service_state_<pot>.json` offset files go away.
- `source/dashboard/real_source.py` — `_load_records`/`_stream_decimated`/tail-cache
  machinery replaced by SQL queries (range filters become `WHERE received_at >= ?`,
  decimation becomes a stride `WHERE id % ? = 0` or a window-function query, "overall"
  before the retention cutoff comes from the rollup tables per the open question
  above). This is the single biggest rewrite in scope — `real_source.py` is ~760
  lines built specifically around JSONL's lack of indexing; expect most of it to be
  replaced rather than patched.
- `source/dashboard/app.py` / templates / `dashboard.js` — CSV export and any other
  direct file access need to point at the DB instead.

**Training pipeline** (narrower than first thought — confirmed by reading all four
files 2026-09-16):
- `scripts/build_training_set.py`: only `load_records()` (lines 76-90) touches raw
  sensor JSONL — swap for a SQLite query (`SELECT * FROM readings WHERE pot_id=?
  ORDER BY received_at`). Everything else in the file (label windows, feature
  computation, `training_set.jsonl` output) is unaffected.
- `scripts/train_transformer.py`: reads only the *intermediate* `training_set.jsonl`
  (a small one-time labeled-dataset artifact `build_training_set.py` produces) — no
  raw-sensor-data I/O at all. **No change needed.**
- `source/edge_node/tiny_transformer.py`, `source/edge_node/export_transformer.py`:
  pure model code (quantization, bin edges, classifier, export-to-Python-module).
  Zero JSONL/file I/O touching sensor data. **No change needed.**

**One-time migration:**
- New `scripts/migrate_jsonl_to_sqlite.py` — backfills the ~1.39M rows/pot of existing
  `pot_{a,b}_readings.jsonl` and ~1.23M rows/pot of `pot_{a,b}_inference_cache.jsonl`
  into the new schema, batched transactions (not one INSERT per row — 1.4M individual
  commits would be very slow), verifies row counts match pre/post, and does NOT delete
  the source JSONL files (rename to `.migrated` or move under `data-from-board/`-style
  archive) until verification passes.
- Must be dry-run against a copy of `data-from-board/sept15/data/` (already-local,
  safe to experiment on) before ever running against live board data.

**Deploy scripts:** `scripts/deploy_dashboard_to_board.sh` /
`scripts/deploy_edge_node_to_board.sh` need the new `board_db.py` added to their
copied-file lists; the old per-pot state JSON files and JSONL paths referenced in
their comments need updating.

## Not yet done / explicitly blocked on

- Live `df -h` output from the board (user is fetching this).
- Reading `build_training_set.py`/`train_transformer.py`/`tiny_transformer.py`/
  `export_transformer.py` to know their actual current JSONL-reading code before
  planning their specific changes (only referenced by name so far, not inspected).
- Sign-off on the schema above and the pruning-vs-keep-forever open question.
- Given the size of this change touching the live inference path 14 days before a
  contest submission: a rollback plan (keep the old JSONL-based code path available,
  e.g. behind a flag or just on a branch, until the SQLite path is validated end to
  end) should be part of implementation, not an afterthought.

## Status (2026-09-16): Phase 1 implementation complete and validated, except deploy scripts

- [x] Live board `df` output (2.7G avail / 9.8G, 72% used) — confirmed ~42 days runway at
      measured growth rate, not an emergency; retention window set to 7 days as planned.
- [x] Read training-pipeline files — only `build_training_set.py::load_records()` needed
      changing; `train_transformer.py`/`tiny_transformer.py`/`export_transformer.py` have
      no raw-sensor-data I/O at all.
- [x] Schema implemented as designed, plus a safety addition not in the original plan:
      `prune_old()` now protects a day's readings from deletion until every row in it has
      a matching `inference_results` row (or a 21-day hard cap is hit) — directly motivated
      by the real 4-day inference-watcher stall this session found. Verified with a
      dedicated test (`board_db.py` smoke test: unprocessed day survives past retention,
      ancient unprocessed day still gets deleted at the hard cap).
- [x] `board_db.py` implemented (`KA-Edge-source/python/board_db.py`) — schema, insert/fetch
      helpers, daily rollup, retention-safe pruning. Smoke-tested standalone.
- [x] `main.py` write path: `handle_reading()` now inserts into `readings` via `board_db`;
      added `_maybe_prune_old()` (once/UTC-day) and `_flush_pending()` (the separate
      `sync_queue_cloud.db` dead-retry-code fix, landed the same session — see
      `board-disk-backup-routine.md`).
- [x] `scripts/inference_service.py` rewritten: reads `readings`, writes
      `inference_results`, state tracks `last_reading_id` instead of a byte offset.
      Functionally verified against a scratch db (idempotent catch-up confirmed).
- [x] `source/dashboard/real_source.py` rewritten: SQL range filters + a
      `ROW_NUMBER() OVER (...)` stride query for decimation, replacing the old hand-rolled
      byte-offset tail cache. `get_status()` blends live `readings` +
      `raw_daily_rollup` so total counts/`collecting_since` stay correct past the
      retention cutoff (this happens to already cover part of what the original plan
      deferred to "Phase 2" — full chart-range history blending for `overall`/`week`
      beyond the retention window is still deferred, only the status-bar numbers got
      fixed for free).
- [x] `test_real_source.py`, `test_dashboard.py`, `test_inference_service.py` fully
      rewritten for the new schema (JSONL-specific cases like byte-offset tail caching,
      partial-line recovery, and file-shrink detection no longer apply and were dropped,
      not just adapted). Full suite: **92 passed**.
- [x] Found and removed two unrelated stale, gitignored, shadow-package directories
      (`source/tests/edge_node/`, `source/tests/dashboard/`, both dated 2026-08-14) that
      were breaking test collection for 5 files this session didn't touch, plus a
      shadowing hazard in my own new `sys.path` handling (fixed: append, not insert(0)).
- [x] `scripts/build_training_set.py::load_records()` updated to read `board_db` instead
      of JSONL.
- [x] `scripts/migrate_jsonl_to_sqlite.py` implemented and validated **at full production
      scale** against a scratch copy of `data-from-board/sept15/data/` (not the tracked
      copy): ~2.6M readings + ~2.5M inference rows migrated in ~16s, verification passed,
      `RealDataSource` spot-checked against the result (status/raw/inference/decimation
      all correct). Compaction: 1.2GB JSONL → 833MB SQLite (~31% smaller — real, but more
      modest than the "2-3x" hoped for in this doc's earlier draft; the bigger long-term
      win is retention/pruning, not raw format compactness).

## Additional fixes found during deployment planning (2026-09-16)

- **`migrate_jsonl_to_sqlite.py` state-file bootstrap**: the old byte-offset
  `.inference_service_state_{a,b}.json` files are incompatible with the new
  `last_reading_id`-based format. Left as-is, a fresh `PotService` would default to
  `last_reading_id=0` and duplicate every already-migrated `inference_results` row.
  Fixed: the migration script now computes the correct resume point (highest
  `readings.id` at/before the last migrated inference timestamp) and writes a fresh
  state file per pot, backing up the old one to `.pre-sqlite-migration.bak`. Verified
  with a synthetic replay of the real Sept-11 stall: 6 pre-migration rows + 8
  post-resume rows = 14 total, zero duplicates, correct one-time
  `TREND_WINDOW`-warm-up gap before the first new result.
- Deploy scripts (`deploy_edge_node_to_board.sh`, `deploy_dashboard_to_board.sh`)
  updated to also ship `board_db.py`; `bash -n` + shellcheck clean (pre-existing
  SC2029 info-level notes only, unrelated to this change).
- Low-disk dashboard indicator implemented: `main.py` writes
  `board_disk_status.json` (QRB2210's own eMMC, distinct from the existing MCU-SD-card
  `sd_status.json`) at most once/60s via `shutil.disk_usage`; `real_source.py` blends
  it into `get_status()` (`board_disk_free_mb`, `board_disk_low` at the 500MB floor);
  `dashboard.js`/`dashboard.css` render a warning-styled status-bar segment when low.
  Verified functionally (throttling + correct values) by stubbing the board-only
  `arduino.app_utils` import to load `main.py` locally.

## Remaining

- [ ] **Deploy scripts** (`scripts/deploy_dashboard_to_board.sh`,
      `scripts/deploy_edge_node_to_board.sh`): need `board_db.py` added to their
      copied-file lists; comments referencing the old JSONL paths need updating. Not
      started.
- [ ] **Low-disk dashboard indicator** — the original ask (`docs/plan/board-disk-backup-routine.md`'s
      Phase 1): report the QRB2210's own live free disk space (distinct from the existing
      MCU-SD-card `sd_status.json`) and surface a banner when it drops under 500MB, as the
      manual cue to run a backup/migration pass. Not started — this predates the SQLite
      pivot and is still independently useful regardless of storage format.
- [ ] Running `migrate_jsonl_to_sqlite.py` for real (non-dry-run) against the live board,
      and deploying the new code — deliberately not done automatically; user runs board
      operations themselves per standing preference.
- [ ] `HTTP 404: Unknown device 'edge-node-01'` cloud-sync registration issue — flagged
      earlier this session, user is handling separately.
