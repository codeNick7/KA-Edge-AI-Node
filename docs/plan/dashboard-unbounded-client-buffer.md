# Plan — Dashboard client-side unbounded buffer (CPU-pegs-over-time + frozen filters)

**Created:** 2026-08-30
**Status:** Diagnosed, plan approved for implementation.

## Symptom (user report)
- Dashboard fine right after opening; left running for a while, refresh gets
  slow, occasionally CPU pegs at 100%.
- Time-range filters (5m/15m/1h/today/week/overall) stop visibly doing
  anything.

## Investigation
Backend (`source/dashboard/real_source.py`, `app.py`) was already hardened in
the prior perf/OOM pass (see `dashboard-perf-and-tooltips.md`) — bounded tail
caches, streaming decimation, cached "overall" view. Re-read start to finish;
no unbounded server-side growth found.

Root cause is client-side, in `source/dashboard/static/dashboard.js`:

- `_chartData[pot][sensor].byTs` / `_infChartData[pot].byTs` (line ~199, 368)
  are plain objects keyed by reading timestamp. `_mergeByTimestamp` only ever
  *adds* keys — nothing evicts old entries.
- `liveTick()` (raw page: line 341, inference page: line 445) runs every
  `KA.REFRESH_MS` (5s) **forever**, each time merging 60 fresh rows in.
- Every tick also re-renders via `_renderCharts`/`renderInfChart`, which call
  `_sortedSeries` (`Object.keys(byTs).sort()`) and `_filterDataset` (a
  `new Date(...)` parse per entry) over the **entire accumulated buffer**,
  for 12 buffers on the raw page (6 sensors × 2 pots).
- `raw.html`/`inference.html` call `initRawPage`/`initInferencePage` exactly
  once per page load (`<script>KA.initRawPage(...)</script>`), so this state
  — and the cost of processing it — accumulates for as long as the tab is
  left open. At ~1 reading/sec this is tens of thousands of entries per
  buffer after a few hours, re-sorted/re-parsed from scratch on a single JS
  thread every 5 seconds, cost growing with uptime, not bounded by anything.
- This directly explains both symptoms: CPU pegging (large repeated
  sort+date-parse work), and filters appearing dead (a filter-click event
  just queues behind an in-flight multi-hundred-ms-to-seconds render tick on
  the same thread, so nothing visibly changes for a while).

## Fix
1. `dashboard.js`: cap each `byTs` buffer to a bounded number of most-recent
   entries (mirrors the server's own `_TAIL_CACHE_SIZE` bounded-tail
   philosophy) — trim after each merge instead of growing forever.
2. Periodically re-sync the *current* filter's buffer from the server's
   already-scoped+decimated endpoint (reuse existing `loadFilterScopedData`),
   not just on filter-button click — keeps "today"/"week"/"overall" views
   correct over long uptimes instead of slowly narrowing to whatever the
   bounded live tail still holds, and keeps the buffer's shape matching what
   the server considers correct for that range.
3. Validate: `node --check` (or equivalent syntax check) on the edited file;
   manual browser verification of long-running behavior is the user's to do
   (servers not started by me).

## TODO
- [x] Add `KA.MAX_BUFFERED_POINTS` (8000) cap + `_trimByTs` trim-oldest-by-
      timestamp helper, called from `_mergeByTimestamp` (covers both raw and
      inference merge call sites automatically).
- [x] Add periodic re-sync of the active filter: tick-counter inside each
      page's `liveTick`, calling the existing `loadFilterScopedData()` every
      `KA.RESYNC_EVERY_N_TICKS` (12 ticks = 60s) instead of a plain
      merge-append tick.
- [x] Syntax-check (`node --check`) — passes.
- [ ] Report back to user for manual browser verification (not started by
      me). Not committed yet either, per standing rule (validate first,
      commit only after confirmation) — browser verification is the
      remaining validation step here since there's no JS test suite.

## Status: client-buffer fix complete, awaiting user's manual browser
verification (leave a tab open for an extended period and confirm CPU/refresh
speed stay flat, and that filter buttons keep responding).

## 2026-08-30 addendum — "Today" filter genuinely hangs the whole page

User tested on the board after a 3h session: full reload fine, "1h"/"5m"
instant, but "Today" didn't refresh at all -- needed a hard page reload, and
subsequent clicks stayed slow. This is a SEPARATE, real bug from the client
buffer above, root-caused in the backend:

1. **Flask dev server was single-threaded** (`app.run(...)` in `app.py` had
   no `threaded=True`). "Today"/"week" are the only filters that reach
   `_stream_decimated`'s on-disk path (5m/15m/1h are served from the bounded
   in-memory tail cache; overall from a precomputed cache) -- a multi-pass
   full scan of the ever-growing raw JSONL file. On the board's slower ARM
   CPU against a multi-hour file, that one request blocked the ENTIRE
   server -- live-tick polling, status bar, even the page's own next
   request -- until it finished, which read as "the whole dashboard froze."
   Fix: `app.run(..., threaded=True)`.
2. **Wasted full-file pass**: `_stream_decimated`'s today/week cutoff
   calculation re-derived "the most recent timestamp" by scanning the whole
   file to its end, even though the bounded tail cache (seeded at boot, kept
   fresh by every live-tick poll) already has it. Fix: `_stream_decimated`
   takes an optional `known_last_dt`; `get_raw`/`get_inference` pass the
   tail cache's last record's timestamp when available, cutting one of
   today/week's full-file passes entirely.

Validated: `py_compile` on both edited files, full pytest suite (85/85
passed, no regressions). Not yet reverified on the board itself -- that's
the remaining step.
