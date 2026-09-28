# Investigation: 3 reported bugs (inference page empty, chart lines broken, inference-service doubt)

Status: **DONE — all 3 confirmed fixed on the live board** (bugs 1 and 3 turned out to be the same
root cause). Code changes validated by `py_compile` + full pytest suite (105 passed, was 96 —
14 new tests added covering previously-uncovered fix paths and the duplicate-write incident
below). Deployed and verified against production by the user 2026-09-25.

User's report (verbatim, 3 items):
1. Inference logic was rewritten. Now the inference page on the dashboard does not show any data.
2. Disk-space issue: raw data moved to SQLite; archiving old data broke dashboard chart/trend
   smoothness (not smooth chart lines since ~Aug 14).
3. Dashboard app no longer computes inference itself — a separate background program does — user
   isn't sure it's even working.

## Method

Read the two in-flight plan docs (`docs/plan/sqlite-storage-migration.md`,
`docs/plan/nn-inference-upgrade-plan.md`) to understand intended design, then diffed every
uncommitted change (`git diff`), ran `py_compile` on every changed file (clean), ran the full
test suite (`96 passed` — all green, which turned out to be misleading, see Bug 2), and reproduced
`scripts/inference_service.py` end-to-end against a synthetic scratch DB to separate "code is
broken" from "process isn't running."

## Bug 2 — CONFIRMED root cause: tiered archive table is read but never written

`KA-Edge-source/python/board_db.py` (uncommitted) adds two new tables to the schema:
`readings_tiered` / `inference_tiered`, meant to hold a per-point downsampled archive of data
older than the 7-day retention window (`RETENTION_DAYS`).

`source/dashboard/real_source.py`'s `_fetch()` (uncommitted) was rewritten to detect these tables
and, for `week`/`overall` chart ranges, `UNION ALL` tiered rows (older than the live table's
earliest row) with the live `readings`/`inference_results` rows.

**The bug: nothing in the codebase ever inserts a row into `readings_tiered` or
`inference_tiered`.** `board_db.prune_old()` — the only place that deletes old rows — still only
rolls them into the *aggregate* `raw_daily_rollup`/`inference_daily_rollup` tables (per-day
MIN/MAX/MEAN, not per-point) before deleting, exactly as originally designed in
`sqlite-storage-migration.md`. So in production: `readings_tiered` exists (schema created via
`CREATE TABLE IF NOT EXISTS`) but is permanently empty. Once a day's readings age past
`RETENTION_DAYS` (7 days) they are deleted with **no per-point replacement at all** — the
`UNION ALL` just returns 0 rows for that period. `week`/`overall` charts silently lose all
resolution past the 7-day mark, which reads as "broken"/discontinuous lines, not "not smooth" in
the jitter sense, but a hard gap.

Why the test suite didn't catch it: the one new test covering this
(`source/tests/test_real_source.py::test_tiered_history_blended_into_overall_chart`) manually
`INSERT`s synthetic rows directly into `readings_tiered` — it tests that the *reader* blends
correctly if the archive is populated, but nothing tests that the archive actually gets
populated in the first place, because nothing populates it.

### Fix options (need your call — see questions below)
- **A: Implement the missing writer.** Extend `prune_old()` (or a new sibling function) to also
  copy a downsampled subset of each pruned day's rows into `readings_tiered`/`inference_tiered`
  (e.g. one row per N minutes) before deleting, so `week`/`overall` charts stay populated past
  retention.
- **B: Revert the tiered-read complexity for now.** Roll `real_source.py`'s `_fetch()` back to
  the simpler pre-tiered version (live-table-only, matching the sqlite-storage-migration.md
  "Phase 2, deferred" note), so `week`/`overall` just show what's still in the retention window
  — a known, documented limitation — until the writer half is actually built. Simpler, no new
  surface area, but doesn't recover the "smooth from day 1" behavior you're asking for.

## Bug 1 — inference page empty, tied to Bug 3

`app.py`'s `/api/inference` defaults to `n=60` (last 60 rows, no chart-range/tiered logic
involved at all) — a direct `SELECT ... FROM inference_results ... LIMIT 60`. An empty page on
that endpoint means `inference_results` itself has (almost) no rows, not a query/rendering bug.

Verified `scripts/inference_service.py` (the standalone background service that is solely
responsible for populating `inference_results` since the Phase-4 process split) works correctly:
ran it `--once` against a freshly seeded scratch DB (15 synthetic readings per pot) and it wrote
9 inference results per pot with no errors. `trained_predict()` was also stress-tested directly,
including the exact previously-documented edge case (Pot A's sensor pinned at `rsmi=1.0`) — no
exception, valid classification returned. So the newly rewritten transformer model and the
service's own logic are not the problem.

**Conclusion: this is very likely the same root cause as Bug 3** — `inference_service.py --watch`
is either not currently running on the board, or stopped running at some point and nothing
restarted it, so `inference_results` has stopped growing (or never started after the SQLite
migration). This can only be confirmed on the board itself (checking the actual running
processes), which per your standing instructions I don't do myself.

## Bug 3 — inference-service reliability: one real gap found

The service split itself (dashboard reads-only, separate `--watch` process writes) is the
intended, already-validated Phase 4 design from `nn-inference-upgrade-plan.md`, not a bug. But
auditing it for "is it actually reliable enough to trust running unattended" surfaced one real
gap:

`scripts/inference_service.py`'s `_run_once_resilient()` only catches `sqlite3.OperationalError`
around each pot's `run_once()` call. This was added after a real 2026-09-17 incident where an
uncaught `OperationalError` mid-backfill killed the whole `--watch` process for *both* pots with
no supervisor to restart it. But the catch is narrow: **any other exception** (a bad/edge-case
reading, an unexpected value hitting `trained_predict`/feature computation, etc.) would still
propagate uncaught out of the `--watch` loop and silently kill it the same way — same failure
mode, just not the one specific exception type that happened to cause the last incident. There is
still no process supervisor (systemd unit, cron @reboot, etc.) — `nn-inference-upgrade-plan.md`
already flags this as a known "nice-to-have, not required" gap.

### Proposed fix (low-risk, clearly justified, will still ask before applying)
Widen `_run_once_resilient()`'s except clause from `sqlite3.OperationalError` to a broad
`Exception` catch (log + skip that pot's cycle, exactly like the existing handling), so a single
bad row/edge case can no longer take down inference collection for both pots indefinitely. This
directly generalizes a pattern the codebase already committed to once.

## Resolution

### Bug 1 & 3 — same root cause, CONFIRMED on the live board, FIXED

User confirmed `scripts/inference_service.py --watch` IS running on the board (`ps aux` showed
it alive since 07:41, ~30s accumulated CPU). Live diagnostics (`readings` COUNT/MIN/MAX vs
`inference_results` COUNT vs the two `.inference_service_state_{a,b}.json` files) then showed:

- `inference_results` is **completely empty** for both pots.
- The saved resume pointer was `last_reading_id: 2894203`/`2894204` (with a resume window
  last dated 2026-09-22).
- The live `readings` table's actual `MAX(id)` is **59478** — far below the saved pointer.

Root cause: `sensor_data.db` was rebuilt/recreated at some point after the state files were
last written (AUTOINCREMENT restarting from 1), but the two per-pot state files were never
reset alongside it. `PotService._fetch_batch()`'s catch-up query
(`WHERE pot_id = ? AND id > ? ORDER BY id ASC LIMIT ?`) has been matching **zero rows on every
single poll cycle since**, with no error and no crash — the process just silently never made
progress again. This is why the inference page shows nothing (bug 1) and why bug 3's "not sure
it's even working" instinct was correct despite the process being alive.

**Fix applied** (`scripts/inference_service.py`, `PotService._reset_if_stale()`, called from
`__init__` right after loading state): on startup, compare the loaded `last_reading_id` against
the live table's current `MAX(id)` for that pot. If the pointer is beyond what exists, log a
loud WARN and reset to `0` (full backfill) instead of silently stalling forever. Covered by two
new tests in `source/tests/test_inference_service.py`
(`test_stale_resume_pointer_self_heals`, `test_valid_resume_pointer_is_not_reset`) — the first
reproduces the exact board incident (seeds 12 real rows with small ids, plants a state file with
a `last_reading_id` far beyond them, confirms the service resets to 0 and then successfully
backfills all 12).

### Bug 3 (exception-handling gap) — FIXED

`_run_once_resilient()`'s except clause widened from `sqlite3.OperationalError` only to
`Exception`, so a single bad reading/edge case can no longer silently kill the whole `--watch`
loop for both pots with nothing to restart it. Small, additive, same pattern the codebase
already committed to once for the narrower case.

### Bug 2 — FIXED per the confirmed 3-tier compaction design

User supplied the actual intended design (not what this session had assumed):
- **Tier 1** (≤7 days, `RETENTION_DAYS`): live 1Hz per-second readings — `readings`/
  `inference_results`, untouched.
- **Tier 2** (8–20 days, new `DAILY_TIER_MIN_DAYS=20`): hourly, non-outlier points —
  `readings_tiered`/`inference_tiered`, `tier='hourly'`.
- **Tier 3** (>20 days): daily, non-outlier points — same tables, `tier='daily'`.

"Non-outlier" representative = the bucket's real row whose value is closest to that bucket's
median (moisture_pct for readings, rsmi for inference_results) — median-of-real-samples, not a
synthetic average, so a single spike/dropout landing in a bucket is never chosen as its
representative point.

**Implemented** in `KA-Edge-source/python/board_db.py`:
- `_archive_raw_day()` / `_archive_inference_day()` (Tier 1 → Tier 2): called from `prune_old()`
  right before a day's rows are deleted from the live tables — one non-outlier row per hour
  archived first, so no day is ever deleted with zero replacement.
- `compact_hourly_tier_to_daily()` (Tier 2 → Tier 3): called unconditionally on every
  `prune_old()` run (not just when new Tier-1 days get pruned that run — hourly rows keep aging
  in `*_tiered` independent of that), collapses any hourly-tier day older than
  `DAILY_TIER_MIN_DAYS` down to one daily representative and deletes the hourly rows for that
  day.
- `inference_tiered`'s schema gained a `tier` column (the uncommitted schema had it on
  `readings_tiered` only — an asymmetry from before this session; safe to change directly since
  nothing has deployed this schema yet, per the board diagnostics below).
- 5 new tests in `source/tests/test_board_db.py` (new file — no `board_db` tests existed before)
  cover: archiving-before-delete, the "protected/unprocessed day" safety still applies and
  correctly skips archiving too, outlier-rejection in `_pick_non_outlier`, and both directions
  of the Tier 2→3 compaction boundary.

No changes needed in `source/dashboard/real_source.py` — its `_fetch()` already blends
`readings_tiered`/`inference_tiered` with the live table correctly; it was only ever missing a
writer, which now exists.

## Deployment (not done by this session, per standing preference)

Confirmed via live board diagnostics: `sensor_data.db` does **not** currently have the
`readings_tiered`/`inference_tiered` tables at all — the schema/reader changes are entirely
local/uncommitted, never deployed. To pick up all three fixes:

1. Deploy the updated `KA-Edge-source/python/board_db.py` and `scripts/inference_service.py` to
   the board (existing `scripts/deploy_edge_node_to_board.sh` / `deploy_dashboard_to_board.sh`
   copied-file lists already include both).
2. Restart `main.py` (creates the new `readings_tiered`/`inference_tiered` tables on its first
   `board_db.connect()` call, via the existing `CREATE TABLE IF NOT EXISTS` schema DDL — no
   manual migration step needed) and restart `inference_service.py --watch` (picks up the
   stale-pointer self-heal on that restart and does a full backfill from `id=0`, since the
   current live table only holds ~7 days/59478 rows per pot — a full backfill of that size
   should be fast, well under the BATCH_SIZE=2000 chunking's normal cadence).
3. Chart history beyond 7 days won't retroactively reappear for the window that's already been
   silently gap-deleted (before this fix existed) — only future `prune_old()` runs archive
   correctly. If recovering that already-lost historical resolution matters, the original JSONL
   files (kept as a `.migrated` backup per `sqlite-storage-migration.md`) may still hold it —
   flagging as a possible follow-up, not attempted here.

## Live deployment log (2026-09-25, on the board)

- User ran `scripts/migrate_jsonl_to_sqlite.py` for real against the live board (backfilled
  Aug 14 → Sep 18 history directly into `readings`/`inference_results`, per that script's
  existing behavior — it predates this session's tiering work and doesn't write to
  `readings_tiered`/`inference_tiered` itself). `main.py`/dashboard were restarted (new PIDs);
  `prune_old()` ran on that restart and correctly archived the newly-migrated old data into the
  3-tier scheme — confirmed via live query: `daily` tier Aug 14 → Sep 4, `hourly` tier Sep 5 →
  Sep 16, live `readings` Sep 18 → Sep 25. Chart history bug (2) confirmed fixed on real data.
- `inference_service.py --watch`, however, was **not** restarted in that pass (it runs as an
  independent background process, not part of `main.py`/the dashboard) — still the same PID
  from before any fix, still holding the stale `last_reading_id` in memory. `inference_results`
  was still empty.
- Also found: the deployed layout has `inference_service.py` under
  `.../ka-edge-node/dashboard/`, not `.../ka-edge-node/python/` — `deploy_dashboard_to_board.sh`
  already documents this, but the first restart attempt used the wrong directory and failed
  with "file not found."
- After locating and restarting it correctly, `_reset_if_stale()` fired as designed (detected
  `last_reading_id=2894203` was stale, reset to 0) and both pots backfilled successfully —
  `inference_results` populated with real, current data. Bugs 1/3 confirmed fixed.
- **New incident found live**: the restart sequence briefly left two `inference_service.py
  --watch` processes running concurrently against the same db (one from an earlier manual retry,
  one from this session's restart command). Both independently backfilled from the same stale
  pointer and both inserted results — ~35,663 duplicate `inference_results` rows resulted
  (Pot B nearly doubled: 61,665 rows vs Pot A's 35,671), silently, no error, no crash. Caught by
  comparing `inference_results` counts against `readings` counts (should track closely; didn't).
  **Fixed at the schema level**: `KA-Edge-source/python/board_db.py` gained a
  `CREATE UNIQUE INDEX ux_inference_pot_ts ON inference_results(pot_id, timestamp)`, and
  `insert_inference_result()` now uses `INSERT OR IGNORE` — a duplicate write from two
  concurrent processes is now a structurally-enforced no-op instead of a silent data
  corruption, regardless of process-management discipline. 2 new tests
  (`test_insert_inference_result_duplicate_timestamp_is_a_no_op`,
  `test_insert_inference_result_allows_same_timestamp_different_pots`) cover this.
- Existing duplicates cleaned up on the board (`DELETE ... WHERE id NOT IN (SELECT MIN(id) ...
  GROUP BY pot_id, timestamp)` — 35,663 rows removed, both pots rebalanced to ~30,903 each,
  matching `readings` counts). Updated `board_db.py` deployed and restarted afterward (dedup
  must happen *before* the unique index is created, or index creation fails on existing
  violations) — `ux_inference_pot_ts` confirmed present on the live db afterward.
- Final state, confirmed by the user: dashboard inference page shows live data for both pots;
  chart history is continuous across all three tiers; duplicate-write protection is active.
