# Cloud-sync fix: device registration + raw-data-to-cloud bug

Date: 2026-09-29
Status: diagnosed, awaiting VPS-side action + confirmation on code fix scope

## Symptom

Board pushes to `https://demo-api.karbon-assure.tech` have been 404ing since
at least 2026-09-13, still reproducing live as of 2026-09-29 15:04 UTC (see
`docker logs karbon_demo_backend`).

## Root cause (confirmed, not assumed)

Not a routing/deploy problem — ruled out:
- Route exists in `distributed-app/src/modules/ingestion/router.py:265`
  since commit `0cfd22f` (2026-08-17), mounted via `app.include_router()`.
- `karbon_demo_backend` container image build is current (recreated ~2026-09-15,
  after the route was added).
- `nginx/karbon.conf` proxies cleanly to `demo-api.karbon-assure.tech` →
  127.0.0.1:7878, no path allowlist/rewrite.
- Manual `curl -X POST .../v1/ingest/soil-sensor/evidence -d '{}'` returns
  **422** (Pydantic validation error on empty body) — proves the route
  itself resolves fine.

Actual cause: `distributed-app/src/modules/ingestion/soil_sensor/auth.py`'s
`authenticate_device()` raises `HTTPException(404, "Unknown device '<id>'")`
when `db.get(IoTDevice, claimed_node_id)` finds no row — and this runs
*after* FastAPI's body validation succeeds, so a well-formed board request
(unlike the empty-body curl test) reaches it and gets a **device-not-found
404**, indistinguishable in uvicorn's access log from a route-not-found 404.

The board's currently-configured `KARBON_ASSURE_CLOUD_NODE_ID` has no
matching `IoTDevice` row in the demo backend's current database — almost
certainly because a DB/env reset wiped the `iot_devices` table (matches the
user's account of "multiple env resets"). This confirms the user's original
instinct: a fresh device registration is genuinely required, not a
misdiagnosis — just via a different mechanism (wiped DB row, not a rotated
encryption key or bad ID).

Also confirmed while investigating: `register_device()` refuses to
re-register an existing `device_id` (`DeviceAlreadyExists`), and there is no
rotate-secret script (`register_device_direct.py`'s own error message
references one that doesn't exist). So if `edge-node-01` still exists as a
row (just unreachable/orphaned), reusing the same ID isn't an option anyway —
registering under a new `device_id` is the correct path either way.

## Second issue (user-flagged, confirmed in code)

`KA-Edge-source/python/main.py`'s `handle_reading()` calls both
`_push_evidence_if_ready()` and `_push_raw_if_batch_full()` unconditionally
for every entry in `_push_configs` (currently just `"cloud"`, since `"local"`
was hardcoded off on 2026-09-11 — see the comment at main.py:181). Nothing
scopes raw-batch pushes to a "local only" destination, so raw sensor batches
go to the cloud alongside evidence, contrary to the PRD intent of sending
only inference/trend data to the cloud destination.

## TODO

- [ ] VPS: register a new device (new `device_id`, e.g. `edge-node-02`) against
      the demo backend for project `demo-ops-01`, capture the plaintext secret.
- [ ] Board: update `.env` — `KARBON_ASSURE_CLOUD_NODE_ID`,
      `KARBON_ASSURE_CLOUD_DEVICE_SECRET` (and confirm `PROJECT_ID`/`API_BASE_URL`
      are still correct) to the newly issued values.
- [ ] Board: restart `main.py` (App Lab "Always On" / crontab) so the new env
      is picked up — no sketch.ino change needed, this is Python-side config only.
- [ ] Verify: watch `docker logs karbon_demo_backend` for a 201 (or the
      `{"status": "rejected"/"duplicate"}` 200/422 JSON body) instead of 404.
- [ ] Code: stop pushing raw batches to the `"cloud"` destination — scope
      `_push_raw_if_batch_full` to a raw-eligible destination set (excludes
      `"cloud"`), pending user confirmation on exact policy.
- [ ] Validate: `.venv/bin/python -m py_compile` on edited files;
      `.venv/bin/python -m pytest KA-Edge-source/python/test_push_to_karbon_assure.py`.
- [ ] Do not commit until user reviews and approves the diff.
