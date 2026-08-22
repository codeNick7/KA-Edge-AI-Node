# Plan — Dashboard performance (persisted inference cache + range-scoped downsampling) + chart tooltips

**Created:** 2026-08-22
**Status:** Approved, implementing.

Full design rationale in this session's plan-mode approval (see conversation) —
summary + TODO tracked here per project convention.

## Root causes
1. `RealDataSource._inference_cache` (source/dashboard/real_source.py) is in-memory
   only — every process restart re-runs inference over the entire history from
   scratch (likely the ">1 min" cost reported).
2. `/api/raw?n=0` / `/api/inference?n=0` return every row ever collected; the
   hand-rolled SVG chart (trend_chart.js) draws one polyline vertex per row with no
   downsampling — hundreds of thousands of points for "Overall".

## TODO
- [x] `real_source.py`: persist inference cache to a JSONL sidecar per pot, loaded on
      construction, appended incrementally.
- [x] `real_source.py`: `_cutoff_for_range()` (server-side port of dashboard.js's
      `_filterDataset` cutoff math) + `_decimate()` (stride sampling, keep last row),
      threaded through `get_raw`/`get_inference` via new optional `range`/`max_points`
      params (default None = unchanged behavior).
- [x] `app.py`: `range`/`max_points` query params on `/api/raw`, `/api/inference`. CSV
      export path untouched (stays full-resolution).
- [x] `dashboard.js`: filter-switch handlers and initial load fetch
      `range=<filter>&max_points=1500` instead of `n=0`; live tick unchanged.
- [x] `trend_chart.js`: hover tooltip (value + timestamp) + hover-dot + point-to-point
      trend arrow, mouse + basic touch support. Also fixed a latent values/dates
      index-misalignment bug (independent filtering) while wiring this up.
- [x] `dashboard.css`: `.ka-trend-tooltip` styling.
- [x] `source/tests/test_real_source.py`: 9 new tests — persistence across restart
      (+ incremental-only-recompute, + shrink invalidation), range filtering
      (5m/overall/sparse-fallback), decimation (bounded + last-row-exact + no-op when
      already small + n>0 ignores range/max_points).
- [x] Validate: py_compile + pytest — 79/79 passed, no regressions. Manual browser
      verification (cold-start load time, all filters, tooltip behavior, CSV row count)
      still needed from the user — servers not started by me.

## Status: implementation complete, awaiting user's manual browser verification.
Not yet committed (per standing rule: validate first, commit only after confirmation).

## 2026-08-22 addendum — OOM fix

The persisted-cache + range/decimation work above caused (or rather, finally
surfaced) an OOM kill on the board: `dmesg` confirmed `anon-rss ~1GB` against
1.7GB total RAM (readings files 75MB/pot, inference sidecars 86-89MB/pot).
Root cause: the in-memory cache retained the *entire* history forever
regardless of any of the above optimizations — see the separate plan file
this session used (`~/.claude/plans/reflective-riding-hejlsberg.md`,
"Fix dashboard OOM kill") for full design rationale.

Fix implemented in `real_source.py`:
- Bounded per-pot tail cache (`deque(maxlen=_TAIL_CACHE_SIZE=5000)`) for raw
  records and inference results, replacing unbounded lists. Serves live-tick
  (`n>0`) and narrow ranges (5m/15m/1h).
- Wide ranges (today/week/overall) and full-history/no-range requests stream
  directly from disk (`_stream_decimated`, two/three-pass count+decimate),
  never materializing more than `~max_points` rows in memory.
- `_catchup_inference_streaming`: when the inference backlog exceeds what the
  bounded tail can supply (fresh deployment, long outage), streams the whole
  raw file with a small `TREND_WINDOW`-sized rolling window instead of
  relying on the bounded tail — fixes a real correctness gap caught by a new
  test (`test_inference_results_cache_stays_bounded_and_sidecar_stays_complete`)
  where rows older than `_TAIL_CACHE_SIZE` were silently never computed.
- CSV export (`app.py`) rewritten as a true streaming `Response` (generator),
  via new `RealDataSource.iter_raw_export_rows`.
- Fixed a test-fixture bug along the way: `_write_fixture`'s `{i:02d}`
  seconds-only timestamp format was invalid/lexicographically broken for
  n > 59, exposed by the new code's need to sort small decimated result sets.

Validated: 84/84 tests pass; manual 50k-row sanity check — startup seed 0.07s,
full inference compute 0.57s, peak process RSS ~92MB (vs. unbounded growth
before), both caches confirmed bounded to exactly 5000 regardless of dataset
size. Not yet verified against the board itself — user to restart and confirm
no OOM kill (dmesg clean) across repeated page loads and all time-range filters.

## 2026-08-22 addendum #2 — board testing found 3 more real issues

Board no longer OOMs, but board logs showed genuinely slow per-request
latency (raw "overall" ~76s, inference "overall" ~68s on a second run).
Root-caused and fixed three separate things, in order found:

1. **`_stream_decimated` did 3 full parse passes for "overall"**, including
   a wasted "find the cutoff" pass that "overall" never even uses (only
   today/week need a cutoff). Fixed: skip that pass unless `range_key in
   ("today","week")`; use a cheap newline count instead of a parse-count
   pass when no cutoff filtering applies. ~500k-row synthetic test:
   3-pass cost down to a single pass (~0.79s locally).

2. **User caught a design regression**: catch-up was lazy (first request to
   need it paid the cost), not eager at boot like originally intended
   ("first boot reads everything and pre-computes; subsequent loads just
   reuse + append"). Fixed: `RealDataSource.__init__` now eagerly calls
   `_catch_up_inference` for every pot before returning, so the app never
   starts serving with stale/incomplete inference. Extracted the backlog
   dispatch logic out of `_get_inference_locked` into a reusable
   `_catch_up_inference` for this.

3. **`_tail_lines` had an O(n²) bug**: rebuilt the whole accumulated buffer
   via `buffer = new_chunk + buffer` every chunk instead of joining once at
   the end. Caught by testing the "no pre-existing sidecar, backlog ==
   nearly the whole file" case (needed for #2 to be safe) -- spiked peak
   RSS to ~7GB before the fix, ~32MB after. Also added a size guard
   (`_catchup_inference_streaming`: if the backlog needed is bigger than
   `_TAIL_CACHE_SIZE * 4`, skip the tail-read-and-materialize approach
   entirely and go straight to the genuinely O(1)-memory rolling-window
   full scan) so a huge fresh-deployment backlog can never re-materialize
   the whole file as parsed dicts at once, regardless of `_tail_lines`.

4. Even after #1-3, the board's ARM CPU is just much slower than a dev
   machine at a single JSON-parse pass over 500k+ lines (~35s observed vs
   ~0.79s locally -- a genuine hardware difference, not a further bug).
   Added the caching layer the user asked for from the start: the "overall"
   decimated view (raw + inference, the default filter hit on every page
   load) is precomputed once at boot (`_refresh_chart_cache`) and served
   from memory (`_cached_overall_raw`/`_cached_overall_inference`)
   thereafter, refreshing only after `_CHART_CACHE_REFRESH_EVERY=1000` new
   rows accumulate -- not on every request. Only applies when the request
   matches exactly what the frontend actually sends
   (`range in (None,"overall")` and `max_points == _DEFAULT_CHART_MAX_POINTS
   == 1500`, matching `dashboard.js`'s `KA.CHART_MAX_POINTS`); anything else
   (tests, custom max_points) still streams fresh, unaffected.

Verified end-to-end on a 540k-row synthetic dataset: startup (eager
inference catch-up + chart precompute) ~7s, every subsequent
`range=overall` request for both raw and inference ~0ms. 84/84 tests still
pass. Not yet reverified against the board itself.
