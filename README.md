# Karbon-Assure Edge AI Node

An edge-AI field device for soil-health monitoring and digital MRV (Measurement,
Reporting, and Verification). It senses, reasons, and runs inference entirely
on-device on an Arduino UNO Q, then syncs compact, self-explaining evidence
records to the cloud instead of streaming raw telemetry.

Built for the **Arduino Physical AI Challenge India 2026** (Robu.in x Arduino).
See `demo/Karbon-Assure_Arduino_Physical_AI_Challenge_Report.docx` for the full
contest report.

## How it works

Two potted plants (Pot A control, Pot B drought-stressed) are each instrumented
with a capacitive soil-moisture probe, a BME688 environmental/gas sensor, and a
BH1750 ambient-light sensor. The pipeline runs in four on-device stages:

1. **Sense** — the UNO Q's STM32U585 real-time domain polls all sensors at 1 Hz
   and streams readings to the Linux domain over Bridge RPC, while also logging
   continuously to a microSD card.
2. **Reason** — the QRB2210 Linux domain converts raw readings into soil-type-
   agnostic relative features (RSMI, light delta, gas-index trend).
3. **Detect** — a trained decision tree (scikit-learn, max_depth=5) classifies
   soil-moisture state with a confidence score.
4. **Synchronize** — HMAC-signed evidence records are queued in a durable local
   store and pushed to the Karbon-Assure ingestion API, with automatic retry on
   failure.

See `docs/karbon-assure-edge-node-system-architecture.md` and
`docs/karbon-assure-edge-node-PRD.md` for the full design.

## Repository layout

- `KA-Edge-source/` — board code that runs on the Arduino UNO Q
  - `sketch/sketch.ino` — STM32 real-time firmware (sensor polling, SD logging)
  - `python/main.py` — Linux-side app (feature extraction, inference, evidence sync)
  - `python/edge_node/` — feature computation, validity checks, trained decision tree
  - `python/push_to_karbon_assure.py`, `sync_queue.py` — HMAC-signed sync with a durable local queue
- `source/dashboard/` — Flask dashboard for live/raw sensor views and inference results
- `source/edge_node/` — decision-tree training code (`train.py`) and offline validation
- `source/cloud_mock/` — mock cloud-side demo UI
- `source/tests/` — test suite (pytest)
- `scripts/` — one-off data-migration and deploy scripts (e.g. `deploy_edge_node_to_board.sh`)
- `docs/` — architecture, PRD, BOM, citations, execution plan
- `demo/` — contest report, abstract, and presentation decks

## Running the dashboard

```bash
.venv/bin/python -m flask --app source.dashboard.app run --host 0.0.0.0 --port 8080
```

or directly:

```bash
.venv/bin/python source/dashboard/app.py
```

## Running tests

```bash
.venv/bin/python -m pytest source/tests
.venv/bin/python -m pytest KA-Edge-source/python
```

## License

MIT — see `LICENSE`.
