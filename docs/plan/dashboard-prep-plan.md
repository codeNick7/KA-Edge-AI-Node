# Plan — Dashboards (Board + Cloud mock)

**Created:** 2026-08-04
**Status:** In progress
**Trigger:** Connectors haven't arrived yet, so this is prep work ahead of real sensor data —
same pattern as `docs/plan/decision-tree-prep-plan.md`: build and unit-test against synthetic/mock
data now, wire in real data once the board/sensors are live.

## Scope (from user request)

1. **Board dashboard** — runs locally on the Arduino UNO Q's QRB2210 Linux side. Two parts:
   - Raw sensor stream: Pot A and Pot B **grouped together** (moisture, gas index, temp, light).
   - Inference charts: Pot A and Pot B **separate** (RSMI trend, flags, confidence, model_version).
2. **Cloud dashboard** — a trend graph of vegetation + other details, meant to eventually be
   integrated into `~/workspace/Karbon-africa/distributed-app` (the real Karbon Assure platform),
   reusing its existing corporate `project-health` dashboard patterns (dependency-free inline-SVG
   `TrendChart`, Card/Badge layout, dark theme).
   - **Decision (user confirmed via AskUserQuestion):** build this first as a standalone mock in
     *this* repo against synthetic data, matching the visual/logic patterns of the real
     `TrendChart` component 1:1 so porting later is mechanical — do NOT touch the live
     `distributed-app` repo yet. That's a separate, deployed production codebase with its own git
     history; real integration is a deliberate later step once real sync data + an agreed API
     contract exist.

## Environment change

Recreated `.venv` at repo root using the **arm64** build of Python 3.13
(`/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13`, a universal x86_64+arm64
binary — verified `platform.machine() == "arm64"` before use) instead of the previous Homebrew
3.14.6. Reason: the Arduino UNO Q board's QRB2210 runs Python 3.13, and the board is arm64, so
matching both the interpreter version and architecture on the dev machine removes a whole class of
"works here, breaks there" risk before the 16–17 Aug crunch week. Verified: `43 passed in 25.68s`
against the existing edge_node test suite after reinstalling `requirements.txt` into the new venv,
so this was a clean swap, no regressions.

## Tech decisions

- **Board dashboard backend: Flask**, not FastAPI. Reasoning: fewer transitive dependencies
  (no starlette/pydantic/uvicorn/anyio) for a device meant to run unattended for years
  (architecture doc §9's longevity framing) — same dependency-conscious reasoning already applied
  to `export.py` avoiding a numpy/sklearn runtime dependency on the board.
- **Board dashboard frontend: plain HTML/CSS/vanilla JS**, inline SVG charts, no Node/React build
  step. The board has no reason to carry a JS toolchain just to render two local pages; the chart
  math is intentionally written as a near-line-for-line JS port of the real
  `TrendChart` component in `distributed-app/web/app/corporate/project-health/page.tsx` so the
  visual language matches Karbon Assure's actual product, not a generic bar/line chart look.
- **Data source abstraction**: `MockDataSource` (synthetic Pot A/B generator, seeded) is the only
  implementation for now. It's built behind a small interface (`get_raw(pot, n)`,
  `get_inference(pot, n)`) so swapping in a real SQLite-evidence-store-backed source later
  (once that store exists — architecture doc §8, not built yet) doesn't touch the Flask routes or
  frontend at all.
- **Cloud mock**: static HTML/CSS/vanilla JS page under `source/cloud_mock/`, with a JS
  `trendChart()` function that mirrors the real TSX `TrendChart` component's math/props
  (`values`, `dates`, `colour`, `unit`, `stressed`) so porting to `.tsx` later is close to
  mechanical translation, not a redesign. Fed by a static mock JSON fixture
  (`vegetation_trend_mock.json`) shaped like what the real dMRV endpoint
  (`/v1/dashboard/projects/{id}/dmrv`) already returns for `satellite_ndvi` / `iot_soil_moisture`,
  plus a soil-node-specific block for RSMI/gas-index trend.

## Theme / branding (user directive)

Both dashboards must carry the same Karbon Assure visual identity as the production
`distributed-app`, not a generic look — borrowed directly from
`~/workspace/Karbon-africa/distributed-app/web`:

- **Logo**: `public/logo.png` (2048x2048 source) copied into this repo and resized for practical
  use — `source/dashboard/static/img/logo.png` (160px, board header, kept small for a
  resource-constrained device) and `source/cloud_mock/img/logo.png` (320px, cloud mock header).
- **Color tokens**, read from `web/app/globals.css` `:root`: background `#090C09`, foreground
  `#EEE9E2`, card `#131009`, border `#2E2318`, accent `#52B788` (emerald — also used for chart
  "up"/healthy trends), brand-secondary `#8A5A47` (dusty terracotta), destructive `#dc2626`.
  Same dark, earthy palette in both the Flask-rendered board pages and the cloud mock, defined as
  plain CSS custom properties (no Tailwind build step available/needed here).
- **Typography**: mirrors the real app's `--font-display` (serif, Fraunces) for headings /
  `--font-sans` (DM Sans) for body — using close system-font fallbacks (`Georgia`/`ui-serif` and
  `ui-sans-serif`) since pulling the actual webfonts isn't worth it for a board-local dashboard.

## Directory layout

```
source/
  dashboard/                    # board dashboard (Flask)
    __init__.py
    app.py                      # Flask app + routes (HTML pages + JSON APIs)
    data_source.py              # DataSource protocol
    mock_source.py              # MockDataSource: synthetic Pot A/B raw + inference generator
    templates/
      base.html
      raw.html                  # Part 1: raw stream, Pot A + Pot B grouped
      inference.html            # Part 2: inference charts, Pot A / Pot B separate
    static/
      dashboard.js              # fetch polling + inline SVG chart rendering
      dashboard.css
  cloud_mock/                   # standalone cloud dashboard mock (not wired into distributed-app)
    index.html
    trend_chart.js              # JS port of distributed-app's TrendChart (see note above)
    vegetation_trend_mock.json  # synthetic data shaped like the real dMRV API response
  tests/
    test_dashboard.py           # Flask test client: routes 200, JSON shape, mock data sane
requirements.txt                 # + flask
```

## TODO

- [x] 1. Recreate `.venv` with arm64 Python 3.13 to match the board; reinstall + re-run existing
      test suite to confirm no regressions
- [x] 2. Write this plan to disk
- [x] 3. Add `flask` to `requirements.txt`, install
- [x] 4. `source/dashboard/data_source.py` + `mock_source.py` — synthetic Pot A/B raw + inference
      generator (reuse `edge_node.features`/`decision_tree` so mock inference flags are produced
      by the real classifier, not hand-faked labels)
- [x] 5. `source/dashboard/app.py` — Flask routes: `/` (nav), `/raw`, `/inference`, plus
      `/api/raw?pot=A|B`, `/api/inference?pot=A|B` JSON endpoints
- [x] 6. Templates + static JS/CSS — raw stream (Pot A+B grouped), inference charts (Pot A / Pot B
      separate), auto-refresh polling
- [x] 7. `source/tests/test_dashboard.py` — Flask test client covers routes + JSON shape
- [x] 8. Cloud mock: `source/cloud_mock/` static page + JS TrendChart port + mock JSON fixture
- [x] 9. Run full test suite, report back before considering this done — 48/48 passed
- [x] 10. User to visually check both dashboards in a browser (server started by user, not by me,
       per standing instruction) and confirm before considering this closed out — confirmed
       working on both `/raw` (Pot A/B column layout) and `/inference`, and the cloud mock served
       via `http.server`. One bug found in the loop: the raw-page column-layout change wasn't
       showing because the already-running Flask process (`debug=False`) doesn't auto-reload
       templates — fixed by adding `TEMPLATES_AUTO_RELOAD`, `SEND_FILE_MAX_AGE_DEFAULT=0`, and
       `use_reloader=True` (kept separate from `debug=True`, since that combined with
       `host="0.0.0.0"` would expose the interactive Werkzeug debugger's RCE to the LAN).
- [ ] 11. Later, deliberate step: port `source/cloud_mock/` into
       `~/workspace/Karbon-africa/distributed-app` as a real route + backend endpoint, once real
       synced soil-node data and an agreed API contract exist (explicitly deferred, not started)

## Progress log

**2026-08-04** — Surveyed `~/workspace/Karbon-africa/distributed-app/web/app/corporate/project-health/page.tsx`
(1161 lines) to understand what to reuse: it's a Next.js/React/shadcn dashboard, auth-gated, backed
by a FastAPI API (`/v1/dashboard/...`), using a dependency-free inline-SVG `TrendChart` component
(lines 249-319) for NDVI/soil-moisture/tree-count trends — no charting library dependency, which is
convenient to port. Confirmed with the user this repo (separate live production codebase) should
not be touched yet; building a same-pattern mock here first. Also confirmed and fixed a real
dev-environment risk: the assumed Python 3.13 was actually a universal x86_64+arm64 binary, and it
was necessary to explicitly verify `platform.machine()` reports arm64 before rebuilding `.venv`
with it, rather than assuming the path implied the architecture.

Built the board dashboard (Flask + vanilla JS/SVG, mock data source running the real
`edge_node.validity`/`features`/`decision_tree` pipeline) and the cloud mock (static HTML/JS,
same TrendChart port, synthetic `vegetation_trend_mock.json` shaped like the real dMRV API
response plus a proposed `soil_node_inference` extension block). Both carry the Karbon Assure
theme (colors pulled from `distributed-app/web/app/globals.css` `:root`, logo copied from
`web/public/logo.png` and resized down — 2048px source was 3.7MB, resized to 160px/40KB for the
board header and 320px/126KB for the cloud mock).

Validated the whole thing runs on the target stack before calling it done: confirmed the
dashboard's own imports are `flask` + stdlib + the already-dependency-free `edge_node` package
(no numpy/sklearn pulled in at dashboard runtime), and that Flask's dependency tree
(werkzeug/jinja2/markupsafe/itsdangerous/click/blinker) are the same kind of common
prebuilt-wheel packages already proven installable on the board's Python 3.13 arm64 via
`board_dry_run.sh`. Exercised every route with Flask's test client (not a running server, per
standing instruction not to start servers) and caught a real bug this way:
`random.Random((seed, bucket))` — a tuple seed — raises `TypeError` on Python 3.13 (worked
differently on older interpreters); fixed by seeding with an f-string instead. All 48 tests
(43 existing + 5 new `test_dashboard.py`) pass on the rebuilt arm64/3.13 venv.
