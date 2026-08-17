# Plan — Wire the Edge Node's inference to a real Karbon Assure ingestion API

**Created:** 2026-08-16
**Status:** Research + design complete (Phase 0 done, all decisions recorded §4,
detailed design §5.1–§5.5). Ready to begin Phase 1 implementation.
**Scope:** Two repos. This one (Contest) is the board/client side. The API lives in
`~/workspace/Karbon-africa/distributed-app` (Karbon Assure's production backend) — a
separate, unfamiliar-to-this-session codebase that already exists and has its own
CLAUDE.md/conventions, which this plan must fit into rather than reinvent.

---

## 1. Why

Execution plan (`docs/karbon-assure-edge-node-execution-plan.md`), 17-18 Aug step:
"Implement the signal schema (flag + confidence + contributing_trend) and wire the
WiFi push to the Karbon Assure ingestion endpoint. Get one full end-to-end cycle
firing correctly: sensor → feature → validity check → flag → cloud → dashboard."

That endpoint doesn't exist yet. This plan is step 1: figure out where it should live
and how, before writing it.

## 2. Research findings (Karbon Assure backend)

Full detail in the research agent's report (not persisted separately — key points below).
Repo: `~/workspace/Karbon-africa/distributed-app`. FastAPI + async SQLAlchemy + Alembic +
Redis (streams) + S3-compatible object store. Module-per-domain layout under `src/modules/`.

### 2.1 The demo projects ("green/orange/red")

Not a literal 3-project set — 5 seeded demo projects across 4 `insurance_colour` tiers
(`demo/tools/seeders.py:2776-2981`):

| key | project_code | name | colour | notes |
|---|---|---|---|---|
| onboarding | demo-onboarding-01 | Kwale Forest Conservation | GREEN | healthy showcase |
| monitoring | demo-monitoring-01 | Mtwara Reforestation Reserve | YELLOW | drought stress |
| wetland | demo-wetland-01 | Coastal Wetland Reserve | YELLOW | salinity stress |
| savanna | demo-savanna-01 | Savanna Carbon Kenya | ORANGE | wildfire risk |
| ops | demo-ops-01 | Cape Coast Dryland Restoration | **RED** | **active sensor-gap incident already seeded** |

**Recommendation: target `ops` (demo-ops-01).** It already has a seeded RED
`sensor_gap` risk signal (`seeders.py:2060-2065`, `2378-2385`) — a real soil sensor
pushing live moisture readings into this project directly resolves that narrative
("the gap gets filled by real data"), which is a strong demo story. No device is
currently registered against it, so there's no fixture collision.

### 2.2 Existing ingestion patterns — two candidates, neither is a perfect fit

**`POST /v1/ingest/iot`** (`src/modules/ingestion/router.py:156-198`) — generic
`measurements` table (project_id UUID, device_id, measurement_type, value, unit,
measured_at, JSONB `metadata`, `data_quality_score`), idempotent (Redis hash dedup),
anti-replay (nonce), writes a raw copy to S3, and publishes to `ingestion.raw`.
**Correction (found in Phase 2 investigation, §5.5): this stream is a dead end for
IoT topics** — `sanitisation`/`pattern_detection` only subscribe to
`raw.satellite_scene`/`clean.vegetation_indices_ready`, so `ingestion.raw` →
sanitisation → pattern-detection → risk does *not* actually happen for IoT data
despite `code-graph.md`'s diagram implying otherwise. The real mechanism that
resolves the sensor-gap signal is a direct `run_assessment()` call (§5.5 point 3),
not this pipeline. Left here as a record of the initial (incorrect) assumption.
Auth: despite the module docstring claiming device auth, the code actually requires a
full admin/project_owner JWT (`router.py:170`) — **contradiction, unresolved**.

**`POST /v1/dmrv/measurements/iot-soil-moisture`** (`src/modules/dmrv/router.py:129-173`)
— richer soil-specific schema (moisture_percentage, soil_temp, soil_ph, sensor_depth_cm,
battery_level, quality_grade, confidence_score, device provenance). Writes to
`iot_soil_moisture_measurements` only — **does not** publish to the risk/alert pipeline,
dashboard-reporting only. Auth: full JWT (`require_farmer_or_admin`).

**Recommendation:** don't force a single choice. Write to a new table shaped like
`iot_soil_moisture_measurements` (so `flag`/`confidence`/`model_version` are first-class
columns, not buried in generic JSONB) **and** publish to `ingestion.raw` like the
`/ingest/iot` path does, so the sensor-gap signal actually reacts. This is the hybrid
the research flagged as architecturally consistent with existing patterns on both sides.

### 2.3 Device registration already exists and is real, not a stub

`IoTDevice` model + full CRUD (`src/modules/system_core/devices/`), and
`device_type` already documents `"soil_sensor"` as a first-class suggested value
(`devices/schemas.py`). Register the board here first (`POST /devices`) before it can
push anything — this is the natural "step 1" on the Karbon Assure side regardless of
which ingestion endpoint gets used.

### 2.4 Auth — no turnkey device-credential model exists

Three patterns coexist, none is a clean fit:
1. User JWT (RS256) — what `/ingest/iot` and `/dmrv/...` actually require today.
2. Heartbeat auth (`device_id` + nonce, no signing secret) — weak, `device_id` itself
   is the bearer credential.
3. HMAC webhook signing (`satellite/webhook.py`, `X-Webhook-Signature` header,
   `SATELLITE_WEBHOOK_SECRET`) — the strongest existing precedent, not yet applied to
   the IoT/device path.

**Decision recorded in §4**: HMAC-signed requests, per-device secret (not shared) —
this determines what the board's push client has to do.

### 2.5 Real inconsistencies found, worth resolving before writing code (not blocking research)

- `project_id` format is **not consistent** across the codebase: `DeviceRegister.project_id`
  expects the `project_code` string (`"demo-ops-01"`), but `measurements.project_id`
  expects the `base_projects.id` UUID (as text). A new endpoint must pick one and
  validate/convert explicitly, or sensor data silently orphans.
- ~~`outbox_events` table exists but `ingestion` publishes directly to Redis
  Streams — unclear which is intended.~~ **Resolved in §5.2**: outbox is the
  documented intended pattern for ingestion specifically; `/ingest/iot` just never
  got migrated to it. The new evidence endpoint uses outbox.
- `/ingest/iot`'s docstring ("device auth for IoT") doesn't match its actual JWT-gated
  implementation — a real gap between intent and code, not just my mis-reading.

## 3. What's already built on the Contest (board) side

Reusable as-is:
- `source/edge_node/trained_model.py` — `trained_predict(rsmi, light_delta,
  gas_index_trend, rsmi_trend) -> (flag, confidence)`, no runtime deps.
- `source/dashboard/real_source.py`'s `_get_inference_locked` — the exact
  feature/validity/inference pipeline per reading, already producing
  `{timestamp, rsmi, light_delta, gas_index_trend, rsmi_trend, flag, confidence,
  contributing_trend, model_version}` per PRD Section 7's evidence schema, just not
  pushed anywhere yet.

Not yet built: anything that POSTs this record off-device. `KA-Edge-source/python/main.py`
currently only writes to local JSONL files.

## 4. Decisions (made 2026-08-16)

- [x] **Target demo project**: `ops` — Cape Coast Dryland Restoration, RED. Resolves
  the seeded `sensor_gap` risk signal with real data.
- [x] **Ingestion pattern — REVISED, see §5.1**: not one hybrid table/endpoint.
  Two separate endpoints/tables matching the PRD's explicit two-path design: a
  continuous evidence/signal stream (publishes to `ingestion.raw`) and a separate
  on-demand/batch raw-data feed (dashboard/audit only, no stream publish). Both get
  exercised for the demo; production intent keeps them at different cadence.
- [x] **Auth model**: HMAC-signed requests, mirroring `satellite/webhook.py`
  (`X-Webhook-Signature: sha256=<hex>` over the raw body, shared secret from a new
  `SOIL_SENSOR_DEVICE_SECRET`-style env var).
- [x] **`project_id` convention**: UUID (`base_projects.id` as text), matching
  `measurements.project_id` — not the `project_code` string `DeviceRegister` uses. The
  board gets this UUID once at setup (config/env), not resolved per-request.

## 5. Proposed phase-wise implementation (board + backend paired per phase, 2026-08-16)

Restructured per your instruction: every phase pairs its Karbon Assure side work
with its Contest board side work, so each phase is independently testable
end-to-end before moving to the next, rather than building the whole backend blind
and only discovering board-integration issues at the very end. Detailed designs are
referenced below rather than repeated: §5.1 (endpoint schemas), §5.2 (Stage 2
outbox/Kafka/blockchain findings), §5.3 (hardening checklist), §5.4
(colour-recomputation platform fix, found during Phase 2 investigation). This
section is the execution order; those are the reference detail.

### Phase 0 — Research (DONE)

Stage 1 (initial research, §2) + Stage 2 (outbox/Kafka/blockchain/delivery
investigation, §5.2).

### Phase 1 — Device identity & auth

The foundation everything else needs — testable in isolation using an endpoint
that already exists, before any new code goes live.

- **Karbon Assure side:**
  - New migration on `iot_devices`: add `secret_encrypted` (§5.3 point 1 — per-device
    auth secret, not one shared global).
  - Register `edge-node-01` as one `IoTDevice` (`device_type="soil_sensor"`)
    against `ops`'s project (`project_code="demo-ops-01"`), generating and storing
    its secret at registration.
- **Board side:**
  - Store the issued `node_id` + secret + project UUID as board config/env, not
    hardcoded.
  - Build the shared HMAC-signing helper (in `push_to_karbon_assure.py`) — reused
    by every later phase's push calls.
- **End-to-end test for this phase:** before building any new endpoint, use the
  *existing* `POST /devices/{id}/heartbeat` (device_id + nonce auth, already built)
  as a first smoke test — confirm the board can authenticate as a real registered
  device and `last_heartbeat_at` updates. De-risks device identity/registration
  before HMAC and new endpoints get layered on top.

### Phase 2 — Evidence/signal pipeline (primary path)

The core of the integration — what should make `ops`'s dashboard actually react to
real data.

- **Karbon Assure side:**
  - Read `f2a1b3c4d5e6_restructure_base_projects_and_arr_projects.py` and
    `iot_soil_moisture_measurements`'s full DDL first (still outstanding, see §7).
  - Migration: `soil_health_evidence` (`sample_index`, `schema_version`,
    `time_uncertain`, `UNIQUE (node_id, pot_id, sample_index)` — §5.1, §5.3).
  - Per-device HMAC auth dependency + server-side bounds validation helper (§5.3
    points 1, 6) — shared with Phase 3's raw endpoint.
  - `POST /v1/ingest/soil-sensor/evidence`: full write path per §5.5 — sanitise
    (inline bounds check), same-transaction insert into `soil_health_evidence` +
    `outbox_events` (mirroring `insurance_risk/service.py`), update
    `iot_devices.last_heartbeat_at` (§5.3 point 7), **call
    `run_assessment(trigger_type="sensor_heartbeat", ...)` directly** (§5.5 point 3
    — this trigger type is defined but never invoked by anything else in the
    current system; we're the first real caller).
  - **Colour-recomputation fix (§5.4)** — a real platform gap found during
    investigation, not scope invented for this feature: `run_assessment()` already
    computes the correct `merged_colour` but never persists it to
    `base_projects.current_insurance_colour`. Fix persists it, correctly merged
    against other risk types' latest signals so a partial-detector-set trigger
    (like `sensor_heartbeat`, which only runs `sensor_gap`) can't wrongly downgrade
    an unrelated active risk.
  - `soil_trend.py` (§5.5 point 4) — mirrors `rolling_ndvi.py`'s shape:
    `fetch_soil_evidence_history()` for the dashboard to chart. No new algorithm —
    the board's per-reading flag/confidence already is the AI inference.
  - Extend `src/modules/blockchain/consumer.py`'s subscribed topics to include the
    new evidence topic; confirm/start the standalone anchoring process rather than
    assuming it's already running.
- **Board side:**
  - Durable local queue (SQLite, architecture doc Section 9) — built here, reused
    by Phase 3.
  - `push_evidence()`, called from `main.py`'s `handle_reading` after the local
    JSONL write, never blocking it.
  - `time_uncertain` sourced from `main.py`'s existing `_ntp_sync_check()` (§5.3
    point 8) — no new clock logic needed.
- **End-to-end test for this phase:** board (or a synthetic replay of real
  `training_set.jsonl` rows at realistic cadence) pushes evidence; confirm the row
  lands in `soil_health_evidence` (sanitisation happened inline, per §5.5 — not via
  the generic satellite-only sanitisation consumer, which our data never touches),
  `last_heartbeat_at` updates, the direct `run_assessment("sensor_heartbeat")` call
  runs the `sensor_gap` detector and `ops`'s dashboard colour actually changes (the
  colour fix, §5.4, is what makes this observable at all — without it the
  assessment would run silently with no visible effect), `fetch_soil_evidence_history()`
  returns a chartable series, the outbox separately publishes to `ingestion.raw`
  (Kafka-ready path, doesn't feed risk directly), and the evidence event reaches
  the blockchain-anchoring queue.

### Phase 3 — Raw data feed (secondary path)

Deliberately built after Phase 2 is proven end-to-end, since it reuses Phase 2's
auth/validation/queue infrastructure rather than duplicating it.

- **Karbon Assure side:**
  - Migration: `soil_sensor_raw_readings` (`UNIQUE (node_id, pot_id, sample_index)`,
    `time_uncertain` — §5.1, §5.3).
  - `POST /v1/ingest/soil-sensor/raw`: same per-device HMAC auth, best-effort
    per-record batch insert with a per-record accepted/duplicate/rejected status
    response (§5.3 point 3), no outbox/stream (no downstream event to guarantee).
- **Board side:**
  - `push_raw_batch()`, reusing Phase 2's durable queue, chunked to a bounded max
    batch size (§5.3 point 4) rather than one unbounded request after a long outage.
- **End-to-end test for this phase:** push a batch including a deliberately
  malformed record, confirm the good records land and the bad one is reported back
  individually rather than blocking the whole batch; confirm a resubmitted batch is
  a no-op via the unique constraint.

### Phase 4 — Docs

Update the abstract/deck's evidence-pipeline sections (currently describe
"Synchronize" with no real backend) once it's real — same spirit as the
trained-model doc updates already done.

## 5.1 Stage 1 design — REVISED after re-reading the PRD/architecture doc (2026-08-16)

The original single-hybrid-table design (below, superseded) missed something the
project's own docs already specify explicitly. Architecture doc Section 5's diagram:

```
Sync manager ─┬─> WiFi/cellular (opportunistic) ─> Karbon Assure ingestion API
              │     ├─ Flags + evidence (primary path)
              │     └─ Raw feed (backup, on demand)
              └─> Cable (ground-team pull) ─> Local export file
```

Section 9: *"Semantic flags (with their evidence metadata) sync first, always; the
raw feed only syncs opportunistically."* This is the edge-AI thesis itself (Section 1:
*"an edge-AI node that reasons on-site is more valuable than a sensor that streams
raw data to the cloud"*) — two genuinely different data products, different cadence,
different purpose, not one table with two write patterns. Revised design:

### Correction: one device, not two

PRD Section 7's schema and architecture Section 8 are both explicit that `pot_id` is
a **field on** each evidence record from one node, not a separate device identity
per pot: *"since this build runs two identical sensor sets (Pot A, Pot B) on one
board, every evidence record needs to declare which pot it came from."* The
previous plan's "register two `IoTDevice`s" call is wrong — reverting to **one**
`IoTDevice` (`device_id = node_id`, e.g. `edge-node-01`), `pot_id` as a field.

### Endpoint 1 — Evidence/signal stream (primary, continuous)

`POST /v1/ingest/soil-sensor/evidence` — small, frequent, per PRD Section 7's schema
(with `model_version` added — architecture Section 8 requires recording it alongside
every inference, PRD's draft schema omitted it), plus `sample_index` and
`time_uncertain` added per the hardening pass (2026-08-16, see §5.3):

```json
{
  "node_id": "edge-node-01",
  "pot_id": "A",
  "sample_index": 50727,
  "timestamp": "2026-08-16T09:20:00Z",
  "time_uncertain": false,
  "flag": "moisture_low",
  "confidence": 0.92,
  "contributing_trend": "moisture_down",
  "model_version": "real-trial-171010rows-seed0",
  "schema_version": "1",
  "raw": { "rsmi": 0.361, "gas_index": 122.49, "temp_c": 26.07, "light_lux": 0.0 }
}
```

New table `soil_health_evidence` (`karbon_assure` schema):
```
evidence_id         UUID PK
project_id          Text            -- base_projects.id as text
node_id             Text
pot_id              Text            -- A | B (open string, not a DB enum — see §5.3)
sample_index        Integer         -- board's local monotonic per-pot counter
schema_version      Text            -- payload shape version, independent of model_version
flag                Text            -- open string, never a hardcoded enum (§5.3 point 5)
confidence          Numeric(5,4)
contributing_trend  Text
model_version       Text
rsmi                Numeric(6,4)
gas_index           Numeric(8,2)
temp_c              Numeric(5,2)
light_lux           Numeric(8,2)
time_uncertain      Boolean         -- true if board's clock wasn't NTP-corrected this session
measured_at         TIMESTAMPTZ
ingested_at         TIMESTAMPTZ
raw_payload         JSONB
```
`UNIQUE (node_id, pot_id, sample_index)`. Indexes: `(project_id, measured_at)`,
`(node_id, pot_id, measured_at)`. No `record_hash`/`prev_hash` columns — superseded
by §5.2's decision to reuse the existing Merkle-batch blockchain-anchoring pipeline
instead of a bespoke per-record hash chain that doesn't exist anywhere else in this
codebase.

**Server-side write path**: see §5.5's updated 6-step version (which supersedes an
earlier 5-step draft that incorrectly assumed the outbox→`ingestion.raw` publish
alone would resolve the sensor-gap signal — it doesn't, since that stream isn't
consumed for IoT topics; the direct `run_assessment()` call is what actually does
it).

### Endpoint 2 — Raw data feed (secondary, on-demand/batch)

`POST /v1/ingest/soil-sensor/raw` — larger, batched, no live-pipeline reaction
(dashboard/audit only, same spirit as the existing `/dmrv/.../iot-soil-moisture`
pattern found in research). Separate table, separate cadence — matches "process it
further... at a later point" and the architecture doc's "ground-team cable pull /
opportunistic sync" framing, rather than continuous per-second upload:

```json
{
  "node_id": "edge-node-01",
  "pot_id": "A",
  "time_uncertain": false,
  "readings": [
    {"sample_index": 1, "millis_ms": 1000, "temp_c": 26.9, "humidity_pct": 76.3,
     "pressure_hpa": 911.0, "gas_kohm": 14.4, "light_lux": 0.0, "moisture_pct": 80.7,
     "received_at": "2026-08-14T09:40:47Z"}
  ]
}
```

New table `soil_sensor_raw_readings`:
```
reading_id     UUID PK
project_id     Text
node_id        Text
pot_id         Text
sample_index   Integer
millis_ms      BigInteger
temp_c, humidity_pct, pressure_hpa, gas_kohm, light_lux, moisture_pct   Numeric
time_uncertain Boolean
received_at    TIMESTAMPTZ
ingested_at    TIMESTAMPTZ
batch_id       UUID            -- groups one sync/upload call
```
`UNIQUE (node_id, pot_id, sample_index)` — `sample_index` is already the board's
local monotonic ID (architecture Section 9), simpler than per-record hashing at this
volume. No stream publish.

**Best-effort batch semantics** (§5.3 point 3) — a batch is not all-or-nothing.
Each reading in the array is validated and inserted independently; the response
reports per-record status:
```json
{
  "batch_id": "...",
  "accepted": 498,
  "duplicate": 1,
  "rejected": [{"sample_index": 3000, "reason": "gas_kohm out of bounds"}]
}
```
A single malformed or already-synced record never blocks the rest of a large
catch-up batch from landing.

### Both endpoints share

- **Auth: per-device secret, not one shared global** (§5.3 point 1). `IoTDevice`
  gets a new `secret_encrypted` column (set at registration, never returned in any
  response), rather than everything trusting one `SOIL_SENSOR_DEVICE_SECRET` env
  var. `X-Webhook-Signature: sha256=<hex>` HMAC over the raw body, keyed by that
  device's own secret — same header shape as `satellite/webhook.py`, different
  (per-device) key source.
- Added to the existing `src/modules/ingestion/` module.

### For the demo specifically

Production intent stays "signals continuous, raw opportunistic/on-demand" — but per
your instruction, **both get exercised for the demo**: the board pushes evidence
records continuously (as the inference loop already produces them) and also pushes
raw batches periodically (e.g. once a minute), rather than waiting for an actual
ground-team cable-pull event. Keeps the two paths architecturally decoupled while
still showing both working end-to-end for the submission.

### Board side (summary — full detail split across Phase 2/3 in §5)

One push module (likely `KA-Edge-source/python/push_to_karbon_assure.py`, called
from `main.py`'s `handle_reading`, never blocking the local JSONL write), covering:

- `push_evidence()` per reading; `push_raw_batch()` on a timer/threshold, **chunked**
  to a bounded max batch size (§5.3 point 4) rather than one unbounded request after
  a long outage — the durable queue (below) tracks per-chunk sync state, not just
  per-file.
- A **durable local queue** (SQLite, per architecture Section 9), folded into this
  stage per your call: every reading gets queued locally first; a record is only
  marked synced after a genuine `2xx`; failures retry with backoff; state survives
  board reboots. Local JSONL logging stays the append-only durable log either way —
  the queue is specifically about *sync state*, not primary durability.
- `time_uncertain` is derived from `main.py`'s existing NTP check
  (`_ntp_sync_check()` already records `ntp_synced`/`ntp_offset_s` per session in
  `session_log.jsonl`, §5.3 point 8) — no new clock logic needed, just plumbing an
  existing signal through into both payloads.
- Config (API base URL, project UUID, `node_id`, this device's own secret) via env
  vars on the board, not hardcoded.

---

<details>
<summary>Superseded: original single-hybrid-table design (kept for reference, not followed)</summary>

**Devices**: register **two** `IoTDevice` rows, not one — `edge-node-pot-a` and
`edge-node-pot-b` (`device_type="soil_sensor"`), both against `ops`'s project UUID.

**New table**: `soil_health_evidence` (new Alembic migration, `karbon_assure` schema),
modeled on `measurements`' indexing (`(project_id, measured_at)`,
`(device_id, measured_at)`) but with the evidence fields as real columns rather than
JSONB — combining derived-feature evidence and raw telemetry into one table/endpoint.

**Endpoint**: `POST /v1/ingest/soil-sensor`, one path for everything.

Superseded because it collapses the PRD's explicit two-path design (signal vs. raw,
different cadence/purpose) into one, and models pot as a device rather than a field.
</details>

## 5.2 Stage 2 findings — outbox, Kafka, blockchain anchoring, delivery guarantees (2026-08-16)

Deep read-only investigation of the actual code (not just docs). Full detail in the
research agent's report (not persisted separately) — key findings and their design
implications below.

**Outbox: real, in use, and the documented intended pattern for ingestion specifically.**
`src/shared/outbox_poller.py` is a live asyncio loop (`SELECT ... FOR UPDATE SKIP
LOCKED`, publish, mark `published_at`, retry pending rows every 1s), started at
app boot (`src/main.py`). Currently only written to by `insurance_risk/service.py`
(same-transaction writes for `insurance.risk.assessment`/`insurance.risk.breach`).
`docs/architecture/SYSTEM_ARCHITECTURE.md §7.5a` gives a worked example that uses
`"raw.sensor"` ingestion as its own sample use case for this pattern — i.e. this is
the documented target for exactly what we're building; `/ingest/iot` just never got
migrated to it (flagged as a real gap by the codebase's own `ARCHITECTURE_REVIEW.md`,
not a deliberate design choice).

**Outbox is explicitly the Kafka migration contract.** Direct quote,
`SYSTEM_ARCHITECTURE.md:1067-1069`: *"When Kafka replaces the internal dispatcher,
the outbox poller is replaced by a Kafka producer that reads from the same outbox
table and publishes to Kafka topics. The outbox table schema is unchanged."* Kafka
itself: confirmed 100% aspirational today — zero dependency, zero infra, zero code
(`pyproject.toml`, all docker-compose files, `src/shared/event_bus.py` all checked
directly). Referenced throughout the docs as "Phase 2," no date, no owner found.
Building the new evidence endpoint on outbox now means it needs no rework when/if
Kafka actually lands.

**Blockchain anchoring exists and works — Merkle-batch, not per-record hash-chain.**
`src/modules/blockchain/anchor_engine.py` batches queued events, SHA-256-leaf-hashes
them into a Merkle tree (`merkle.py`), submits the root to `AnchorRegistry.sol` on a
15-min schedule or immediately for RED/BLACK risk signals (`consumer.py`). Currently
anchors: `insurance.risk_signal`, `alert.generated`, `insurance.project_risk_summary`,
and a filtered subset of `raw.satellite_scene` — **not** IoT/sensor events today. This
is a different, separate mechanism from `/ingest/iot`'s idempotency-check hashing
(confirmed: two different `compute_hash`-style functions, not shared). Our own
architecture doc's "per-record hash chain per pot_id" concept **does not exist
anywhere in the Karbon Assure codebase** — reusing the real Merkle-batch pipeline
(publish evidence events to a topic the blockchain consumer subscribes to) is the
right move rather than building a bespoke parallel hash-chain. Caveat: the anchoring
process is a standalone process the main FastAPI app does not start
(`src/modules/blockchain/main.py`) — needs confirming it's actually deployed/running
before relying on it, not just present as code.

**Delivery guarantee gap — real, and directly relevant to "nothing left behind."**
Redis Streams consumer groups + `XACK` give genuine at-least-once delivery when a
*handler* throws (captured to DLQ, human-replayable via `src/modules/dlq/`). But
there is no `XCLAIM`/`XAUTOCLAIM`/`XPENDING` anywhere in the codebase — if a consumer
process crashes between reading and acking a message, it's stranded in the Pending
Entries List permanently with no automatic recovery. Direct-publish endpoints (like
`/ingest/iot` today) inherit this gap; outbox-based writes don't, since the poller
re-reads pending rows from Postgres every cycle regardless of prior crash state.

### Design decisions from Stage 2

- **Evidence endpoint uses the outbox pattern**, not direct publish — same-transaction
  insert into `soil_health_evidence` + an `outbox_events` row, mirroring
  `insurance_risk/service.py`'s existing pattern exactly rather than inventing a new
  one. This closes the crash-consistency gap and is the documented Kafka on-ramp.
- **Idempotency via a permanent DB unique constraint, not a time-limited cache.**
  `/ingest/iot`'s 24h Redis dedup window would let a duplicate through after a long
  outage. Both new tables get `UNIQUE (node_id, pot_id, sample_index)` — reusing the
  board's own monotonic per-pot counter (already in the raw JSONL schema) as the
  natural idempotency key, matching architecture Section 9's "local monotonically
  increasing ID... sync is idempotent" requirement directly. A resend after any
  outage gets a harmless "already stored" response, not a duplicate row or an error.
  (The raw-feed endpoint doesn't need outbox — it has no downstream event to
  guarantee delivery of, so a plain transactional insert + the same unique
  constraint is sufficient there.)
- **Wire evidence events into the existing blockchain-anchoring pipeline now**
  (decided 2026-08-16), rather than deferring: extend
  `src/modules/blockchain/consumer.py`'s subscribed-topic list to include the new
  evidence topic, and confirm/start the standalone anchoring process as part of this
  work — don't just add the wiring and assume it's live.
- **Board-side durable local queue is folded into the push-client stage** (decided
  2026-08-16), not split into its own stage — they're tightly coupled enough
  (the queue exists only to serve the push client's retry/backoff/reboot-survival
  needs) that building them together is more coherent than a separate review pass.

## 5.3 Hardening checklist (decided 2026-08-16 — do now, not later)

Explicit call: tight fit for the Aug 23 deadline either way, but building these in
now avoids rework/regressions later. All eight are binding requirements across
Phases 1-3 (point 1 → Phase 1; points 2,3,5,6,7,8 → Phase 2; point 4 → Phase 3),
not deferred nice-to-haves. Design detail for each is inline in §5.1 above; this is
the checklist form for tracking during implementation.

1. **Per-device secret, not one shared global.** New `secret_encrypted` column on
   `IoTDevice`, set at registration. A compromised board only compromises itself.
2. **Replay-safety via idempotency-check ordering, not just HMAC.** A signature
   proves integrity, not freshness. The `UNIQUE (node_id, pot_id, sample_index)`
   constraint is what actually makes a replayed request harmless — it must be hit
   before the outbox write commits, not after.
3. **Best-effort per-record batch inserts on the raw endpoint**, not all-or-nothing.
   One malformed record in a large catch-up batch must never block the rest.
   Response reports per-record accepted/duplicate/rejected status.
4. **Chunked catch-up syncs**, bounded max batch size on the board's push client —
   no unbounded single request after a long outage.
5. **`flag` (and `pot_id`) stay open strings everywhere**, never a hardcoded DB enum
   or downstream pattern-match assumption — the flag vocabulary already changed
   once (6-flag literature tree → 2-flag trained model) and will again.
6. **Server-side defense-in-depth validation**, reusing `edge_node/validity.py`'s
   bounds, independent of the board's own validity gate — don't fully trust the
   client in an audit-trail system.
7. **Wire evidence pushes to `iot_devices.last_heartbeat_at`.** Otherwise that field
   stays permanently stale and never actually reflects the live device this
   integration is meant to demonstrate.
8. **`time_uncertain` flag, sourced from the board's existing NTP check.**
   `main.py`'s `_ntp_sync_check()` already records `ntp_synced`/`ntp_offset_s` per
   session — plumb that existing signal into both payloads rather than inventing new
   clock-trust logic. A silently-wrong timestamp is worse than a flagged one in an
   MRV audit context.

## 5.4 Colour-recomputation fix (real platform gap, found during Phase 2 investigation)

Traced the full path from "detector runs" to "dashboard shows a colour":

- `insurance_risk_signals` is **append-only** (`(signal_id, detected_at)` composite
  PK, no `resolved_at`/`is_active`/`expires_at`) — a permanent event log, not
  mutable current state.
- `base_projects.current_insurance_colour` (what the dashboard badge actually
  reads) is a **plain stored column**. Grepped every write to it across the whole
  codebase: the **only** writer is the demo seeder
  (`demo/tools/seeders.py:2385`, a hardcoded `SET current_insurance_colour = 'RED'`
  for `ops`). No code in `src/modules/insurance_risk/` ever updates it.
- But `InsuranceRiskService.run_assessment()` (`service.py:176-260`) **already
  computes the right value** — `merged_colour` — every time it runs. It's just
  never persisted; the function returns it in an `AssessmentResult` that nothing
  writes back to the project row.

**The fix**: persist `merged_colour` to `base_projects.current_insurance_colour`
at the end of `run_assessment()`, in the same transaction as `_persist_signals()`.
One real subtlety, confirmed by reading `_TRIGGER_DETECTOR_SETS`
(`service.py:75-95`): triggers run *partial* detector sets — `"sensor_heartbeat"`
only runs `sensor_gap`. A naive `UPDATE ... SET current_insurance_colour =
:merged_colour` after a partial-detector-set run could wrongly downgrade a project
that has an active risk of a type this trigger didn't evaluate (e.g. an active
wildfire risk from `"firms_update"`, invisible to a `sensor_heartbeat`-triggered
pass). Correct fix: before persisting, merge this pass's `active_scores` with the
most recent non-GREEN signal per risk type *not* covered by this trigger's
detector set (query `insurance_risk_signals` for the latest `detected_at` per
`risk_type` outside `active_set`), then run the existing `merge_threat_scores`/
`_colour_for_score` (`taxonomy/scoring.py`) over the combined set — reusing the
scoring logic that already exists rather than inventing new merge semantics.

This is a real, generically-useful fix to Karbon Assure's platform (the badge
should always have been reactive), not a demo-specific hack — worth landing as
its own reviewable change within Phase 2.

## 5.5 Soil evidence processing pipeline (sanitisation → timeseries → event trigger → trend query)

Answering "what does 'process the data' actually mean" concretely, modeled on the
vegetation/satellite pipeline's real (not aspirational) shape — confirmed by
reading `sanitisation/writer.py` and `pattern_detection/algorithms/rolling_ndvi.py`
directly rather than assuming from the architecture doc's diagram.

**Two things the satellite pipeline's own diagram implies but doesn't actually
have, confirmed by reading the code:**
- `vegetation_indices` is referred to as a "hypertable" in comments/docstrings, but
  there is no `create_hypertable` call anywhere in `migrations/` — it's a plain
  indexed Postgres table. `soil_health_evidence` as already designed (§5.1) already
  matches this real convention, not a gap to fix.
- `/ingest/iot` publishes to topic `raw.iot_measurement` (confirmed via
  `src/modules/replay/tasks.py:44`) — and **nothing consumes that topic**.
  `sanitisation/consumer.py`/`pattern_detection/consumer.py` only subscribe to
  `raw.satellite_scene`/`clean.vegetation_indices_ready`. The "IoT → sanitisation →
  pattern-detection → risk" path in `docs/code-graph.md`'s diagram is real for
  satellite data only — for IoT it's aspirational. Publishing our evidence events
  to that same topic would be a dead end; we need our own consumer.

**Decided pipeline shape for soil evidence** (scope confirmed 2026-08-16 — storage
+ chart-ready query, not a new statistical trend algorithm, since the board's own
per-reading `flag`/`confidence` already *is* the AI inference; duplicating that
cloud-side would be redundant scope):

1. **Sanitisation (simple, inline)** — the server-side bounds validation already
   planned (§5.3 point 6) *is* this stage. No separate async consumer/queue hop for
   it, per your "keep it simple for now" — unlike satellite's sanitisation, which
   is a separate consumer because scene processing is heavier, a bounds check is
   cheap enough to do inline in the request handler before the transactional write.
2. **Timeseries data hold** — the `soil_health_evidence` table (§5.1), written via
   the same outbox-pattern transaction. Confirmed consistent with the real
   (non-hypertable) convention above.
3. **Event trigger** — this is the piece that was actually missing platform-wide,
   not just for our feature: **`run_assessment(trigger_type="sensor_heartbeat",
   ...)` is defined but never invoked by anything in the current system** (the
   daily scheduler only runs `"scheduled_daily"`; the risk consumer only listens
   for `pattern.anomaly_detected`/`raw.firms_update`). The evidence endpoint's
   write path calls it directly after the DB write (via
   `build_sensor_context()` + `InsuranceRiskService.run_assessment()`,
   `data_fetchers/sensors.py`/`service.py` — no new module needed, just an actual
   caller for code that already exists) — this is what makes the `sensor_gap`
   detector run at all, and what makes §5.4's colour fix observable within the
   demo rather than only on the next 06:00 UTC scheduled pass.
4. **Trend query ("AI engine inference to plot the trend")** — a new
   `soil_trend.py`, mirroring `rolling_ndvi.py`'s exact shape: `fetch_soil_evidence_history(db, project_id, pot_id, window_hours)`
   returning oldest-first parallel arrays (timestamps, rsmi, flag, confidence) for
   the dashboard to chart. No new algorithm, no new risk-taxonomy signal type —
   the board's inference is already the AI output; this just makes it queryable as
   a proper time series instead of one-row-at-a-time.

Updated evidence write path (supersedes the shorter 5-step list in §5.1):
1. Verify per-device HMAC signature.
2. Sanitisation: bounds-check against `edge_node/validity.py`'s limits.
3. Same-transaction insert into `soil_health_evidence` + `outbox_events`.
4. Update `iot_devices.last_heartbeat_at`.
5. Call `run_assessment(trigger_type="sensor_heartbeat", ...)` — runs `sensor_gap`,
   persists the recomputed colour (§5.4).
6. Outbox poller publishes to `ingestion.raw` on its next cycle (existing
   Kafka-ready path, unrelated to step 5's direct call).

## 6. Explicit non-goals for this pass

- Not modifying Karbon Assure's existing `/ingest/iot` or `/dmrv/...` endpoints' current
  behavior for their existing callers — additive only.
- Not solving the `project_id` format inconsistency platform-wide — just picking one
  explicitly for the new path.
- Not adding `XCLAIM`/`XAUTOCLAIM`-based PEL reclaim to the shared `event_bus.py`
  consumer-crash gap found in Stage 2 (§5.2) — real, but a platform-wide fix outside
  this feature's scope. The evidence endpoint sidesteps it by using outbox instead of
  direct publish; the underlying gap in `sanitisation`/`pattern_detection`'s consumers
  stays as-is.
- Not building a platform-wide canonical hash utility to de-duplicate the two
  independent `compute_hash`-style functions found in Stage 2 (`ingestion/idempotency.py`
  vs. `blockchain/merkle.py`) — noted as a worthwhile cleanup, not this feature's job.
- **No cloud-side AI/pattern-detection engine for soil evidence.** Per §5.5's scope
  decision, this pass builds storage + a chart-ready history query
  (`fetch_soil_evidence_history()`) only — it does not build a cloud-side model or
  algorithm that independently infers risk from the incoming time series (the kind
  of thing `pattern_detection`'s NDVI rolling-window algorithms do for satellite
  data). The board's own trained decision tree remains the only AI inference in
  this pipeline; the cloud only stores and displays what it already decided. A
  cloud-side engine that re-analyzes the accumulated series for its own
  independent risk signals (e.g. a rolling RSMI trend detector feeding a new
  risk-taxonomy type, mirroring `pattern_detection/algorithms/rolling_ndvi.py`) is
  explicitly future work, not part of this integration.

## 7. Outstanding items to confirm at the start of Phase 1/2 coding

Not blocking the start of Phase 1, but must be checked before the relevant Phase
2 code gets written — flagged rather than assumed:

- Full DDL read of `f2a1b3c4d5e6_restructure_base_projects_and_arr_projects.py`
  and `iot_soil_moisture_measurements` (only grepped for table names so far, not
  read in full) — before writing the `soil_health_evidence`/`soil_sensor_raw_readings`
  migration, so new columns/conventions match current reality exactly.
- Confirm the standalone blockchain-anchoring process
  (`src/modules/blockchain/main.py`) is actually deployed/running somewhere, not
  just present as code — before wiring evidence events into its consumer's
  subscribed topics and assuming they'll get anchored.
- Confirm exactly what `project_id` value `insurance_risk/scheduler.py` and
  `consumer.py` pass into `build_sensor_context(db, project_id, ...)` — traced the
  callee's SQL (`WHERE project_id = :pid` against `iot_devices.project_id`, which
  stores the project_code string) but didn't fully trace the top-level caller's
  own argument to confirm it's project_code and not the UUID. Our direct
  `run_assessment("sensor_heartbeat")` call (§5.5 point 3) needs to pass whichever
  one is actually correct.
