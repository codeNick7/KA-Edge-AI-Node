# Cloud push: send Pot B evidence only (Pot A stays board-local)

Date: 2026-10-06
Status: analysis done, strategy proposed — awaiting user decisions (see "Open decisions"). No code changed yet.
Repos: `~/workspace/Contest` (edge board), `~/workspace/karbon-africa/distributed-app` (demo cloud)

## Goal

demo.karbon-assure.tech's demo project must represent Pot B only (the soil-health-degrading
pot). Pot A evidence must stop leaving the board. Pot A must still be logged, inferred
and shown on the board's own local dashboard.

## Findings (verified in code)

### Edge — Contest
- The screenshot ("Inference — Pot A and Pot B, tracked separately") is the board's LOCAL
  dashboard (`source/dashboard/templates/inference.html`), not the cloud. Showing both pots
  there is correct and must not change.
- `KA-Edge-source/python/main.py` `handle_reading()` (~L497-503) calls
  `_push_evidence_if_ready("A", ...)` AND `_push_evidence_if_ready("B", ...)` unconditionally.
  `_push_evidence_if_ready` (~L227-279) loops over every destination in `_push_configs`
  (currently just `"cloud"`, pinned at ~L194). So Pot A evidence goes to the cloud.
- Raw batches are already excluded from `"cloud"` (`_push_raw_if_batch_full`, `name == "cloud"`
  skip, commit 0961bdf). Only evidence is affected.
- `push_to_karbon_assure.load_config()` has no per-destination pot setting.
- Board sync queue (`sync_queue_cloud.db`, table `sync_queue`, `kind='evidence'`) stores the full
  payload in `payload_json` incl. `pot_id`. Any queued, not-yet-synced Pot A rows will keep
  flushing via `_flush_pending()` even after the push-side fix, so they need purging separately.
- Uncommitted change in the working tree: a `[DEBUG] evidence payload` print in
  `_push_evidence_if_ready` (~L278). It prints one line per pot per reading. Decide: keep or drop.
- `backfill_historical_evidence.py` already has `--pot {A,B,both}` (default `both`). That default
  is how Pot A history got backfilled to the cloud before.

### Cloud — distributed-app
- `src/modules/ingestion/soil_sensor/receiver.py` accepts any `pot_id` matching `^[A-Z]$`
  (schemas.py), inserts into `karbon_assure.soil_health_evidence`, and writes an outbox event
  `soil_sensor.evidence_recorded` in the same transaction.
- `src/modules/blockchain/consumer.py` consumes `soil_sensor.evidence_recorded`, so Pot A
  evidence is being ANCHORED as well as stored. Anchors cannot be removed afterwards.
- Only one reader filters by pot: `dashboard_api/views/dmrv_detail.py` hardcodes `pot_id = 'B'`
  (commit 189ae2e). Farmer project page + corporate project-health read through that view, so
  the visible dMRV panels already show Pot B. The leak is in storage, anchoring and the
  `/soil-sensor/evidence/history?pot_id=` endpoint, not in those two pages.
- `scripts/delete_evidence_for_pot.py` already exists (dry-run by default, `--execute` to delete).
  It was written for this same "only Pot B represents this demo project" cleanup after an earlier
  backfill.

## Proposed strategy

1. **Edge (root-cause fix):** per-destination pot allowlist.
   - `push_to_karbon_assure.load_config()`: read optional `<PREFIX>POTS` (e.g.
     `KARBON_ASSURE_CLOUD_POTS=B`) into `config["pots"]`.
   - `_push_evidence_if_ready`: keep appending to `_pot_history` for both pots (unchanged), but
     skip any destination whose `pots` doesn't include this pot. Apply the same filter to
     `_push_raw_if_batch_full` for consistency.
   - Fallback when the var is unset: see decision D1.
2. **Edge (queue cleanup, one-off, user-run on board):** delete unsynced Pot A evidence from
   `sync_queue_cloud.db` (dry-run count first):
   `SELECT COUNT(*) FROM sync_queue WHERE kind='evidence' AND synced_at IS NULL AND json_extract(payload_json,'$.pot_id')='A';`
3. **Cloud (defense in depth, optional — D2):** reject evidence whose pot isn't allowed for the
   device/project. Returns 4xx and stores/anchors nothing, so a stray board config can't leak Pot A again.
4. **Cloud (data cleanup, user-run on VPS — D3):** `delete_evidence_for_pot.py --node-id <id> --pot-id A`
   dry-run, then `--execute`. Check the related `outbox_events` rows; on-chain anchors are permanent.
5. **Backfill guard:** change `backfill_historical_evidence.py --pot` default from `both` to `B`, or
   require it explicitly, so a future backfill can't re-leak Pot A (D4).

## Open decisions (user)
- D1: Default when `KARBON_ASSURE_CLOUD_POTS` is unset: all pots (backward compatible), or
  hardcode cloud=B in main.py like the existing `names=("cloud",)` pin (deterministic regardless of .env)?
- D2: Add the cloud-side reject (step 3) now, or edge-only?
- D3: Delete existing Pot A cloud rows, or leave them (dmrv view already filters)?
- D4: Backfill default → `B`, or make `--pot` required?
- D5: Keep or drop the uncommitted `[DEBUG] evidence payload` print?

## Decisions (user, 2026-10-06)
- D1: env var `KARBON_ASSURE_CLOUD_POTS`, cloud falls back to `B` when unset
- D2: edge-only for now (no cloud reject)
- D3: leave existing Pot A cloud rows
- D4: not yet asked
- D5: keep the `[DEBUG] evidence payload` print

## New evidence: cloud panel screenshot (Cape Coast Dryland Restoration = demo-ops-01)
"Soil Health (Edge Node)" panel: current 0.565 RSMI, 90-day trend 23 Aug → **2 Oct**, rows
2 Oct 0.565 stable, 29 Sep 0.349 stable, 4 Sep 0.399 / 4 Sep 0.416 moisture_low.
- The latest point is 2 Oct, but today is 6 Oct and the board's local dashboard has data up to
  6 Oct 03:56. **The cloud panel is 4 days stale**, which may be a separate problem from the Pot A leak.
- Values 0.35–0.565 fall in Pot B's local range (0.33–0.63); Pot A is flat at 1.000. So the panel is
  most likely already Pot B, but this is unverified (the UI doesn't show pot_id).
- Two "4 Sep" rows: probably UTC-day grouping rendered in IST. Unverified.
- Gap 4 Sep → 29 Sep matches the documented 13–29 Sep device-404 outage.
- Candidate causes for staleness (none verified yet): 63b27cc (idempotency re-key, 3 Oct) not
  deployed, so post-reboot pushes silently no-op; device 404/401 again; board queue backed up.
- Next: user runs the read-only checks (row counts by pot, deployed commit, backend log status codes).

### Backend log check (user-run, 2026-10-06 05:54 UTC)
- `POST /v1/ingest/soil-sensor/evidence` returning **201**, in pairs about 2s apart (Pot A + Pot B).
- router.py:265-284: 201 = row inserted; 200 = duplicate; 422 = rejected. So fresh rows ARE being
  inserted today for both pots. Board → cloud ingestion is working. The 2 Oct cutoff on the panel is
  on the READ side (wrong project_id, deployed dmrv_detail differs, measured_at values off, or frontend cache).
- VPS has no git (code is deployed via scp), so check file versions by checksum. Local sha256:
  - dmrv_detail.py `d626572fa3ab8df1b4f7c658af36cd431ffcb2f062b245cd9b18f5eebd80bcec`
  - receiver.py    `cfbfb6730668c0f7ea1619bdf44072ff14be0e79654d3bb98918df4daeb598b7`
  - expected alembic head includes `b4e8f1a9c3d6` (soil_evidence_key_on_measured_at)
- VPS result: both checksums MATCH local; alembic current = `b4e8f1a9c3d6 (head)`. Deployed code is current.
- DB (native PG, port 5656, db `karbon_assure_demo`), demo-ops-01 = `a166db20-2e3f-5cf4-880a-cac97eb58299`:
  Pot A 159,443 rows, Pot B 160,077 rows, both latest measured/ingested 2026-10-06 05:59 UTC.
  → Pot A leak into cloud storage confirmed. Pot B data is current, so the panel's 2 Oct cutoff is NOT missing data.
- Read path: the dmrv route has no server cache; the project-health page renders API order as-is
  (`iot_soil_moisture[0]` = "Current"). So the API's query must be dropping recent days.
- Prime suspect (unverified): `dmrv_detail.py` filter `sample_index < 900000000`. Contest's backfill
  writes `900_000_000 + day_index` (historical) and `950_000_000 + seconds` (`--latest`), so that filter
  also drops REAL backfilled history, not only the synthetic seeds its comment describes. Whether recent
  live rows also carry >= 900M indices is the open question → per-day sample_index query.
- Per-day query result: hidden_by_filter = 0 for every recent day (only the single 22 Aug seed row).
  Suspect DISPROVED. Pot B has rows on 5 Oct (1,338) and 6 Oct (9,515). No Pot B rows 5–28 Sep (the device-404 outage; never backfilled).
- **RESOLVED (06:02 UTC):** a fresh browser load of `GET https://demo-api.karbon-assure.tech/v1/dashboard/projects/a166db20-…/dmrv`
  returns `iot_soil_moisture[0]` = 2026-10-06T06:02:52Z, Pot B, rsmi 0.6392, flag `soil_health_degrading`;
  [1] = 2026-10-05T22:26Z. Every row is pot_id B. API and panel are correct. The earlier screenshot
  showing "2 Oct" was most likely from a page loaded before the 5 Oct 22:26Z rows arrived (page fetches once on mount).
  Not a bug; no cloud code change needed for staleness.
- Follow-up: "the plot trend isn't going down". Why: the panel plots RSMI (moisture LEVEL), one reading per UTC
  day (DISTINCT ON), while `soil_health_degrading` = gas_index slope < 0 AND rsmi slope < 0 over the last
  7 readings (~10–15 s) (decision_tree.py:90-94). The badge = last − first point.
- Pot B daily averages (excl. seeds):
  avg_gas 129.8, 124.0, 90.7, 81.5, 58.7, 79.3, 93.9, 88.7, 77.4 (Aug 22–Sep 4) … 36.0, 67.3 (Oct 5–6) → declining, noisy.
  avg_rsmi 0.41–0.55 throughout → no trend (watering-driven).
  pct_degrading 0% through Sep 4, ~18% Oct 5–6. CONFOUNDED: Aug–Sep rows came from model `real-trial-171010rows-seed0`,
  Sep 29+ from `tiny-transformer-1265876rows-d8`, so the jump partly reflects the model switch.
  Sep 29 and Oct 2 have n=1 (not representative).
  → The only real long-run decline in the data is the gas index. Chart change options pending user decision.
- Remaining scope = the Pot A leak only (159,443 Pot A rows stored and anchored); edge fix per D1/D2/D3/D5.

## TODO
- [x] Analyse edge push path, cloud receiver/consumers, existing cleanup tooling
- [x] User decisions D1, D2, D3, D5 (D4 backfill default still open)
- [x] Edge code change: `push_to_karbon_assure.allowed_pots()` + `DEFAULT_POTS={"cloud": {"B"}}`;
      main.py v3.6: `_push_pots`, `_pot_allowed()`, filter in evidence + raw loops, history still appended for all pots
- [x] Tests: 8 new in test_push_to_karbon_assure.py; clean_env clears *_POTS
- [x] Validate: py_compile OK (main.py, push client, tests); pytest 31 passed (push + sync_queue);
      main.py push logic checked in isolation (ast-extracted functions + stubs, 4 scenarios) — scratchpad only
- [x] Board (user, 2026-10-06 ~12:16 UTC): deleted 631 queued unsynced Pot A evidence rows from
      sync_queue_cloud.db; deployed v3.6; console shows `[DEBUG] evidence payload` for pot_id B only
      while Pot A readings are still logged locally
- [ ] VPS confirm: Pot A latest_ingested frozen, Pot B advancing
- [ ] D4: backfill_historical_evidence.py bypasses the scope (`--pot both` default, pushes straight to cloud)
- [ ] Chart change (user chose: daily avg gas + RSMI) — distributed-app, own plan doc
- [ ] (if D2) cloud receiver change + tests in distributed-app
- [ ] User runs: board .env update + main.py restart; queue purge (step 2)
- [ ] User runs: cloud cleanup (step 4) if D3 = delete
- [ ] Verify: cloud row counts by pot over ~10 min show only B growing
- [ ] Commit only after user review/approval (no push)

## Pot B stopped landing after the v3.6 deploy (2026-10-06 ~12:16 UTC)
- VPS: both pots' latest_ingested froze at 09:37:37 UTC. Backend now returns 422 about once a second.
- Board queue `data/sync_queue_cloud.db` (live file; the NFS path is the same file): newest created_at = 2026-10-03 11:11:10,
  i.e. nothing new enqueued since then. Keys `edge-node-01|B|9` / `|B|10` exist from 2026-09-23.
- ROOT CAUSE (confirmed): the board queue key = node|pot|sample_index. The firmware counter restarts on reboot, and the queue is
  never purged, so enqueue() hits IntegrityError → returns None → push silently skipped (the DEBUG print fires
  before the queue check). Same bug class as the backend's 63b27cc.
- The 422s: 643 old unsynced rows (29 Sep) with gas_index 102400.0 (BME688 maximum-value glitch), rejected permanently
  by the backend sanitiser and retried forever (attempts=571).
- Unexplained: the cloud received live Pot B rows 5–6 Oct even though the queue got nothing after 3 Oct. Most likely the board
  ran code that differed from the repo until today's scp. Unverified; the user didn't confirm.
- inference_service.py checked: no network code, local SQLite only. The only pushers are main.py and backfill_historical_evidence.py.
- FIX: push_evidence key → node|pot|timestamp; push_raw_batch chunk key → received_at range.
  +2 tests (reboot reuse of sample_index is still sent; identical reading is deduped). 33 passed.
  The regression test FAILS against the old key (verified on a scratch copy).
- Board steps: stop main.py; move sync_queue_cloud.db* aside (backup, also drops the 643 poison rows); scp
  push_to_karbon_assure.py; start; verify 201s + Pot B latest_ingested advancing, Pot A frozen.
- Follow-up (not done): stop retrying permanent 4xx rejects (status "rejected") instead of retrying forever.

## Verified end-to-end (2026-10-06 13:08 UTC)
- Board: old queue moved aside at 13:01:19 (`*.bak-20261006`); main.py restarted at 13:02:48; push client md5 13f2f8f6…
  New queue: 135 rows, 0 unsynced at 13:07:41.
- VPS: evidence POSTs return 201, one about every 2 s (Pot B only, no longer in pairs). Pot B count 166,460 → 166,612 (+152),
  latest_ingested 13:08:16. Pot A frozen at 165,825 / 09:37:37.
- DONE: Pot A leak stopped; Pot B sync restored.

## Follow-ups (not started)
1. Queue: stop retrying permanent rejects (HTTP 422 status "rejected") forever; decide how sensor_fault evidence with an
   out-of-range gas_index is sent (currently rejected wholesale by the cloud sanitiser).
2. Cloud Pot B gaps: 2026-10-06 09:37–13:02 UTC and 2026-09-05..09-28. Backfill from board_db. backfill_historical_evidence.py
   still reads pot_*_readings.jsonl (stale since the 2026-09-16 SQLite migration) and needs updating first.
3. Chart (user chose): daily avg gas + daily avg RSMI in the dmrv panel (distributed-app, separate plan doc).
4. D4: backfill script pot scope.
5. Board housekeeping: data/sync_queue_cloud.db.bak-20261006 (209 MB) → move off the board.

## Probe degradation analysis (2026-10-06, after the commit)
User report: Pot B probe coating gone along its full length up to the electronics; Pot A partially exposed. Replacement: generic
"Capacitive Soil Moisture Sensor v1.2" (Robocraze, Rs 55). The vendor says it is qualitative only; edges/electronics unsealed.
Source: data-from-board/Oct05/data/sensor_data.db (read-only; partly corrupt: 1 unreadable 2000-id chunk in readings;
inference_results unreadable). Daily rollup covers 08-14..09-27, raw readings 09-18..10-05.
- Pot B moisture: daily mean 47–54% from 08-16 to 10-05, low daily sd (0.7–2.4), no clipping, no drying trend over 7 weeks.
  No clear failure signature, but the flatness could also mean an insensitive probe. Needs the dry-air / in-water test.
- Pot A moisture: crept 80 → 90 → 100% (08-14..09-07), pinned at exactly 100.0 (sd 0.00) since 10-02. Consistent with a
  saturated/shorting probe (or genuinely waterlogged soil); not distinguishable without the test.
- Both pots show a discontinuity on 09-10 (folder "sept09-post-sensor-swap"). What was swapped is unknown → ask the user.
- Gas: Pot B 143 → ~126–130 (08-16..22) → ~72–80 (late Aug) → 55–107 since; Pot A 203–237 → 160–175 → 135–200.
  BOTH pots decline. So part of the "Pot B gas decline" is common-mode (BME688 ageing/burn-in, ambient VOC, humidity:
  A ~82% vs B ~59% RH), not soil-specific. The B/A gas ratio went from ~0.55–0.7 (mid-Aug) to ~0.4–0.5: a modest, noisy
  differential decline. CORRECTION to the earlier "gas is the clean evidence" claim.
- Exclude days with n < 500 (09-18..09-23: board mostly down; gas 14–38 kOhm = post-power-on warm-up).
