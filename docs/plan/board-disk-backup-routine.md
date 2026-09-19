# Board disk backup / rotation routine

Status: DRAFT — plan only, no implementation yet. Written 2026-09-16.

## Why (evidence, from data-from-board/sept15/data — most recent snapshot)

Three independent unbounded-growth files on the board's `python/data/` dir:

| File | 2026-08-14 → 2026-09-15 | Rows | Size | Rate |
|---|---|---|---|---|
| `pot_a_readings.jsonl` | full range | 1,391,188 | 298M | ~9.4MB/day |
| `pot_b_readings.jsonl` | full range | 1,391,142 | 297M | ~9.4MB/day |
| `pot_a_inference_cache.jsonl` | stalled at 09-11 (see below) | 1,228,190 | 300M | ~10.6MB/day while running |
| `pot_b_inference_cache.jsonl` | stalled at 09-11 | 1,228,187 | 313M | ~10.6MB/day while running |
| `sync_queue_cloud.db` | since 08-24 | 302,891 rows, 0 synced | 272M | out of scope (see below) |

Board is an Arduino UNO Q, 16GB eMMC total (`docs/karbon-assure-edge-node-system-architecture.md` §7). At ~40MB/day combined for the 4 JSONL files alone, unbounded growth is a multi-month-not-multi-year problem, contrary to that doc's original (much lower sampling-rate) estimate.

`sync_queue_cloud.db` is a separate bug (flush_pending() was dead code, now wired up in `KA-Edge-source/python/main.py`'s `_flush_pending()`, `2026-09-16`) and a device-registration issue (`HTTP 404: Unknown device 'edge-node-01'`) — **explicitly out of scope for this routine** per 2026-09-16 decision: don't touch unsynced rows until cloud sync is confirmed working again.

Architecture doc's own §9 "Rotation policy" (already written, never implemented): *"keep all flags/anomalies indefinitely, downsample or summarize old raw readings rather than deleting them outright."* This plan implements that.

## Decisions already made (2026-09-16 conversation)

1. **Retention shape**: rolling window of full-resolution raw+inference JSONL on the board (recent N days), PLUS a compact SQLite daily-rollup table kept forever on the board so `collecting_since`/total-count/coarse-trend context survives even after old raw rows are archived off.
2. **Backup direction**: routine runs FROM this machine (not the board), using `scp`/`ssh` against `$BOARD`/`$DEST` (same convention as `scripts/deploy_edge_node_to_board.sh` / `scripts/deploy_dashboard_to_board.sh`), pulling files to this machine, then truncating on the board.
3. **Trigger model**: NOT a cron/automatic job. The board reports live free disk space; when it drops under **500MB free**, the dashboard shows an indicator; that's the human cue to run the backup routine manually from this machine.
4. **Backup destination**: `data-from-board/<timestamp>/` at repo root — same layout convention as existing `data-from-board/sept09*/`, `sept15/` snapshots already in this repo.
5. **sync_queue_cloud.db**: excluded from this routine for now.

## Open items needing a decision before implementation (flagging, not guessing)

- **Retention window size (N days)**: proposed default **3 days** full-resolution on board (~4 files × ~10MB/day × 3 days ≈ 120MB combined at current rate — comfortably comparable to leaving 500MB+ headroom versus 16GB, and small enough that the routine, run occasionally, doesn't have to move huge amounts each time). Needs your confirmation or a different number.
- **Live disk numbers unknown**: I only have historical copies (`data-from-board/*/data/`), not the board's actual current total/used/free disk. The routine's first step must query this live (`ssh $BOARD df $DEST`) rather than trust anything computed here — 500MB-free triggering logic must be based on that live read, not the snapshot sizes above.
- **Daily rollup schema** (proposed, needs sign-off):
  - `raw_daily_rollup(pot, date, count, temp_min/max/mean, moisture_min/max/mean, gas_min/max/mean, light_min/max/mean, first_ts, last_ts)`
  - `inference_daily_rollup(pot, date, count, flag_counts_json, rsmi_min/max/mean, confidence_mean, model_versions_json, first_ts, last_ts)`
  - Computed once per day-being-archived-away, from the rows about to be removed, appended to a small SQLite file (e.g. `python/data/rollup.db`) that is NEVER pruned.
- **Dashboard still won't show full "overall" history after truncation** — `real_source.py`'s `_stream_decimated`/`_seed_record_cache` read the raw JSONL directly; once old rows are gone from disk, "overall"/"week" charts on the board's own dashboard will only show the retained window, not full project history. Teaching the dashboard to blend `rollup.db` into those chart ranges is a **separate, later phase** (Phase 2 below) — not blocking the immediate disk-safety fix. Confirm this phased split is acceptable.
- **CSV export** (`iter_raw_export_rows`) also only sees what's still on the board post-truncation — full-history export would need to come from this machine's archive instead, not the board's dashboard, once truncation is live.

## Phase 1 — disk safety (this phase's implementation scope)

1. **Board-side: live disk-free reporting.**
   - New small write, alongside the existing `sd_status.json` pattern in `main.py` (that one is for the MCU's physical SD card — see `real_source.py:63-64` — this is a *new*, separate file for the QRB2210's own eMMC, e.g. `board_disk_status.json`), written periodically (e.g. once per reading cycle or on a light timer) via `shutil.disk_usage(DATA_DIR)`.
   - `real_source.py.get_status()` reads it and exposes `board_disk_free_mb` etc., same way it already exposes `sd_free_mb`.
   - Dashboard UI: banner/indicator when `board_disk_free_mb < 500` (exact wording/placement TBD — small UI change in `app.py`/templates/`dashboard.js`).

2. **Local (this-machine) backup script**, `scripts/backup_board_data.sh` (bash, matching the existing deploy scripts' style/SSH-ControlMaster pattern):
   - Reads `$BOARD`/`$DEST` like the deploy scripts.
   - `ssh` to check live `df` on `$DEST` (report actual used/free before doing anything).
   - `scp` down the 4 JSONL files (both pots × raw + inference) into `data-from-board/<UTC-timestamp>/data/`.
   - Verify each copy (byte count / checksum match against the source before touching the board).
   - Invoke a small board-side Python helper (new file, e.g. `KA-Edge-source/python/rotate_data.py`, deployed alongside `main.py`) over SSH that:
     - Computes/appends the daily rollup rows for everything older than the retention window into `rollup.db`.
     - Atomically rewrites each JSONL file to keep only the retention window (temp file + `os.replace`, same pattern `inference_service.py`/`sync_queue.py` already use for atomic writes).
   - Never deletes anything locally-backed-up until the board-side truncate step has been confirmed to succeed.

3. **Validation before commit** (per your standing rule): compile-check the new Python, shellcheck/`bash -n` the new shell script, and a dry-run against one of the `data-from-board/sept15/data/` snapshots copied to a scratch dir (safe, since it's already a local copy, not live board data) before ever running it against the real board.

## Phase 2 — dashboard long-history blending (separate, later)

Teach `real_source.py`'s `overall`/`week` (and possibly `today`, if the window shrinks below a day) code paths to merge `rollup.db` daily aggregates for the archived-away period with the fresh JSONL window for recent data, so the board's own dashboard doesn't lose historical context after Phase 1 starts truncating. Not started until Phase 1 is working and the schema above is validated against real usage.

## Durability gap found 2026-09-18 (separate from disk-space; not yet fully closed)

While debugging why the physical SD card couldn't be mounted on the Linux/QRB2210
side (blocked by a hardware issue -- see below), a real question came up: if the
board's eMMC fails, is the raw sensor history actually recoverable from anywhere
else? Investigation found:

- **The MCU-side SD card (`KA-Edge-source/sketch/sketch.ino`) was never a working
  historical backup**, despite writing every reading to `sensor_log.csv` in
  parallel with the Bridge/Linux path. `doInit()` did `sd.remove(LOG_FILENAME)`
  unconditionally on every sketch restart ("fresh log each run") -- with dozens of
  restarts across the real collection period (`session_log.jsonl`), that file only
  ever held "since the last restart," not full history. **Fixed 2026-09-18**:
  `doInit()` now only writes the header if the log doesn't already exist, so it
  appends across restarts like the Python-side files already do. Not yet
  hardware-tested (no arduino-cli/board core available in this environment to
  compile-check `.ino` files) -- verify it compiles and flashes via Arduino App Lab
  before trusting it. Known residual limitation, not fixed: rows carry only
  `millis()`-since-boot, no wall-clock timestamp -- cross-reference against
  `session_log.jsonl`'s session_start times to anchor a stretch of rows to a real
  date.
- **Mounting that same SD card on the Linux side (as extra `board_db.py` storage or
  an independent backup target) is blocked by hardware, not software**: the QRB2210
  requires the single USB-C port to be in Host mode (default is Device mode --
  confirmed bug in the `dwc3-qcom` driver when booted without a cable already
  attached; live debugfs workaround: `echo host > /sys/kernel/debug/usb/4e00000.usb/mode`,
  not yet made persistent via a systemd unit) AND that port's VBUS is only sourced
  from `5V_SYS`, which is unavailable when the board itself is powered *through*
  that same USB-C port (per the ABX00162/173 datasheet). The user's current dongle
  is also a dual-USB-C-prong hub built for adjacent-port MacBooks and physically
  cannot deliver USB data to this single-port board regardless of any of the above.
  Needs a single-connector powered USB-C hub/dongle (Arduino's own datasheet lists
  this as the supported accessory) before revisiting.
- **Cloud sync (the architecture's originally-intended off-board durability path)
  is still broken** (`HTTP 404: Unknown device 'edge-node-01'`, 0% synced) --
  separate issue, flagged earlier, not addressed.
- **The recurring "backup raw data to this laptop" routine from this session's
  original ask was never built** -- superseded by the SQLite migration, which
  fixed disk-space but not this durability question. `migrate_jsonl_to_sqlite.py`
  is a one-time format-migration tool, not a repeatable backup.

Net effect as of 2026-09-18: if the board's eMMC fails today, the only recovery
point is the one-time `data-from-board/pre-sqlite-migration/` snapshot on this
laptop (Sept 16) -- nothing collected since then is backed up anywhere else. User
chose to prioritize the SD-card sketch fix first; the recurring-laptop-backup and
cloud-sync-registration gaps remain open.

**2026-09-19 decision: mounting the SD card on the Linux side is paused, not
abandoned.** Extensive back-and-forth (host-mode debugfs fix, VBUS-sourcing
mechanism, dongle compatibility, VIN/5V_SYS power-input options, CCTV SMPS/PD-
trigger/battery options for powering VIN) confirmed the approach is technically
sound, but every viable power source for `VIN` assumes mains access near the
board, which the user does not have at the current install location. Decision:
keep relying on the MCU-side SD card (sketch.ino, restart-wipe bug fixed this
session, not yet hardware-verified) as the local backup for now, and return focus
to data collection/analysis. Revisit Linux-side SD mounting once either mains
access changes or a portable `VIN` power solution is found.

**Explicitly deferred, to revisit later per user request**: the `HTTP 404: Unknown
device 'edge-node-01'` cloud-sync registration issue (see
docs/plan/sqlite-storage-migration.md) -- flagged repeatedly this session, still
unfixed, user wants to come back to it.

**Explicitly dropped per user request**: the recurring backup-to-laptop routine
from this session's original ask. User backs up manually when needed; not
building an automated version.

## TODO

- [ ] Confirm retention window default (3 days?) and rollup schema above.
- [ ] Implement `board_disk_status.json` writer in `main.py` + `real_source.py` read + dashboard indicator.
- [ ] Implement `KA-Edge-source/python/rotate_data.py` (rollup + atomic truncate).
- [ ] Implement `scripts/backup_board_data.sh`.
- [ ] Dry-run against a `data-from-board/sept15/data/` copy in scratch space.
- [ ] Deploy + test against real board (user-run, not automatic).
