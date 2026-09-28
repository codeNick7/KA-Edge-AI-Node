# Rollback: drop Tier 3 (daily) compaction, keep archived history hourly forever

Status: **CODE DONE, LOCALLY VALIDATED — awaiting user deploy/run on the board**

## Ask (verbatim intent)
User: "lets rollback the current 1 day sample plot for all historical charts on the dashboard
but make them 1 hour ticks. so We can see the trends vary daily.."

Confirmed via AskUserQuestion:
1. Remove Tier 3 (the >20-day daily-compaction step) entirely, not just push its threshold out.
2. The Aug 14 -> Sep 4 window that's already been collapsed to 1 point/day on the live board
   should be restored to hourly resolution, using the local `data-from-board/pre-sqlite-migration/`
   JSONL backups (`pot_a_readings.jsonl.migrated` / `pot_b_readings.jsonl.migrated`, per-second,
   covering 2026-08-14T09:40 -> 2026-09-16T11:31 for both pots) rather than accepting the loss.

## Background
`docs/plan/three-dashboard-bugs-2026-09-25.md` (Bug 2 fix, deployed 2026-09-25) introduced a
3-tier chart-history scheme in `KA-Edge-source/python/board_db.py`:
- Tier 1 (<=7 days, RETENTION_DAYS): live 1Hz readings/inference_results, untouched.
- Tier 2 (8-20 days, DAILY_TIER_MIN_DAYS=20): hourly non-outlier points, `readings_tiered`/
  `inference_tiered`, tier='hourly'. Written by `_archive_raw_day`/`_archive_inference_day`,
  called from `prune_old()` right before a day's live rows are deleted.
- Tier 3 (>20 days): collapsed further to 1 non-outlier point/day, tier='daily'. Written by
  `compact_hourly_tier_to_daily()`, called unconditionally on every `prune_old()` run.

Per the live deployment log in that doc, the board already ran this once: `daily` tier
Aug 14 -> Sep 4, `hourly` tier Sep 5 -> Sep 16, live `readings` Sep 18 -> Sep 25. So any
week/overall chart reaching back past ~20 days currently renders that window at 1 point/day --
the "1 day sample plot" the user wants rolled back.

## Plan

### 1. Code: remove Tier 3 from `board_db.py`
- Delete `compact_hourly_tier_to_daily()`, its call site in `prune_old()`, and the
  now-unused `DAILY_TIER_MIN_DAYS` / `TIER_DAILY` constants.
- Update the tier-scheme comment block above `DAILY_TIER_MIN_DAYS` to describe the new
  2-tier scheme (live + hourly-forever).
- `TIER_HOURLY` / `_archive_raw_day` / `_archive_inference_day` stay as-is -- Tier 2 is
  unchanged, it just never gets compacted further now.
- Existing `tier='daily'` rows already on the board are inert data, not a code path --
  `real_source.py`'s reader unions the whole tiered table regardless of `tier` value, so old
  daily rows still render, just at their existing (coarser) resolution unless replaced by step 3.

### 2. Tests: `source/tests/test_board_db.py`
- Remove the two `compact_hourly_tier_to_daily` tests (function no longer exists).
- Add/keep coverage that `prune_old()` no longer touches/creates any `tier='daily'` rows,
  and that hourly-tier rows from 25+ days ago are left alone (previously they'd have been
  compacted away).

### 3. Data recovery: rebuild hourly tier for the already-compacted Aug 14 -> Sep 4 window
- New one-off script `scripts/rebuild_hourly_tier_from_jsonl_backup.py`:
  - Reads `data-from-board/pre-sqlite-migration/data/data/pot_{a,b}_readings.jsonl.migrated`.
  - Reuses `board_db._bucket_and_pick(..., "hour")` to pick one non-outlier row per
    (pot, hour) for dates strictly before the live board's current hourly-tier floor
    (i.e. the dates presently sitting at tier='daily').
  - For each such date: delete the existing tier='daily' rows for that (pot, date) from
    `readings_tiered`, insert the rebuilt tier='hourly' rows instead.
  - Dry-run by default (prints counts/date range, no writes); `--apply` to actually write;
    operates on a `--db-path` the caller supplies (a local copy first, board's real
    `sensor_data.db` only when the user runs it there themselves per standing deploy process).
  - No inference-side rebuild: the JSONL backup's inference cache reflects the old
    (pre-rewrite) inference logic, not today's model, so backfilling `inference_tiered` from
    it would mix model versions on one chart -- flagged as out of scope, not attempted.

### 4. Validation
- `py_compile` on `board_db.py` and the new script.
- `.venv/bin/python -m pytest source/tests/test_board_db.py -q` (and the full suite) green.
- Dry-run the rebuild script against a copy of the repo's own scratch/test db first, not
  the real board db, before the user runs `--apply` anywhere real.

### 5. Deployment (not done by this session, per standing preference)
- User deploys updated `board_db.py` to the board (existing deploy script's file list
  already includes it) and restarts `main.py` so future `prune_old()` runs stop compacting
  to daily.
- User runs the rebuild script against the board's actual `sensor_data.db` (or wherever they
  choose) to backfill the Aug 14 -> Sep 4 hourly rows, after reviewing the dry-run output.

## Progress
- [x] Investigated current tier scheme, confirmed root cause of "1 day" ticks.
- [x] Located local JSONL backup covering the affected window.
- [x] Confirmed rollback scope and data-recovery approach with user.
- [x] Remove Tier 3 from `board_db.py` (`compact_hourly_tier_to_daily`, `DAILY_TIER_MIN_DAYS`,
      `TIER_DAILY`, and the call site in `prune_old()` all removed; comment block updated to
      describe the resulting 2-tier scheme).
- [x] Update `test_board_db.py` (removed the two Tier 2->3 tests, added
      `test_prune_old_never_compacts_old_hourly_tier_rows_to_daily`).
- [x] Write `scripts/rebuild_hourly_tier_from_jsonl_backup.py`.
- [x] Validate: `py_compile` clean on both changed/new files; full `pytest source/tests/` —
      104 passed; script dry-run and `--apply` both exercised against a scratch db seeded with
      a synthetic `tier='daily'` row for 2026-08-15, confirmed it correctly replaced that 1 row
      with 24 real hourly rows rebuilt from
      `data-from-board/pre-sqlite-migration/data/data/pot_a_readings.jsonl.migrated`.

## Addendum: Raw Sensor Stream page charts also switched to hourly ticks

User follow-up: apply the same 1-point-per-hour scale to the Raw Sensor Stream page's own
charts (`source/dashboard/templates/raw.html`, `raw-filter-group`) across **all six** time
filters (5m/15m/1h/today/week/overall) -- confirmed explicitly, including the tradeoff that
5m/15m/1h will render at most 1 point (often 0) since those windows are shorter than an hour.
Scope confirmed as the Raw Sensor Stream page only, not the Inference page.

Implemented client-side in `source/dashboard/static/dashboard.js` (no backend change needed --
this is a rendering concern, and `_renderCharts` is the single chart-render path for every
filter and for both the initial range-scoped fetch and the live 5s tail-poll merge):
- New `_bucketToHourly(values, dates)`: collapses a series to <=1 point per UTC hour, keeping
  the last reading in each hour. Deliberately truncates via `setUTCMinutes(0,0,0)`, not
  `setMinutes()` -- local-time truncation would put the hour boundary at the wrong instant for
  any non-whole-hour UTC offset (e.g. UTC+5:30) and would vary per viewer's browser timezone.
  Caught by testing the function standalone with `node` before wiring it in (initial
  local-time version visibly mis-bucketed under TZ offset -330).
- Wired into `_renderCharts()` right after `_filterDataset()`: the chart and its trend badge
  now render from the hourly-bucketed series. `updateRawCard`'s "Current: X" value is left
  untouched (still the true latest raw reading, unaffected by any bucketing) -- only the chart
  line and its trend delta are bucketed.
- Inference page (`renderInfChart`) untouched, per confirmed scope.

Validated: `node --check dashboard.js` (syntax), and `_bucketToHourly` exercised standalone
under `node` for same-hour collapse, multi-hour spans, empty/single-point input, out-of-order
input, and explicit-offset timestamps -- all correct after the UTC fix.

## Remaining (user-side, per standing "I deploy/run things myself" preference)
- Deploy updated `KA-Edge-source/python/board_db.py` to the board, restart `main.py` so future
  `prune_old()` runs stop compacting to daily.
- Run `scripts/rebuild_hourly_tier_from_jsonl_backup.py --db-path <board's sensor_data.db>
  --jsonl-dir data-from-board/pre-sqlite-migration/data/data` (dry-run first, review output,
  then `--apply`) to backfill the Aug 14 -> Sep 4 window back to hourly resolution. Only Pot A's
  dates were dry-run-tested above (Pot B's backup file is also present and the script loops
  both pots automatically); Pot B's own `tier='daily'` dates will be picked up the same way once
  run against the real board db, which actually has both pots' collapsed history.
