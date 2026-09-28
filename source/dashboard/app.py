"""Board dashboard (Flask) — runs locally on the QRB2210 Linux side.

Two views:
  /raw       — Raw sensor stream, Pot A and Pot B in two vertical columns
  /inference — Inference charts, Pot A and Pot B separate

Soil pin mapping corrected 2026-08-15 (see real_source.py header):
  Pot A — BME688 @0x76 (Wire1/Qwiic), Cytron soil on A1
  Pot B — BME688 @0x77 (Wire1/Qwiic), Cytron soil on A0

API endpoints:
  /api/raw?pot=A&n=60     — last N readings (n=0 → full history)
  /api/inference?pot=A&n=60
    Both also accept range=5m|15m|1h|today|week|overall and max_points=N
    (only applied when n=0) to scope+decimate a full-history chart request
    down to what's actually renderable instead of returning every row ever
    collected — see real_source.py's _slice_by_range/_decimate.
  /api/status             — session metadata (collecting since, NTP sync, counts,
                             SD card used/free/total MB from the MCU's physical card)
  /api/download/csv?pot=A — download JSONL as CSV (pot=A, pot=B, or omit for both)

Run from source/:  .venv/bin/python -m dashboard.app
"""

import csv
import io

from flask import Flask, Response, jsonify, render_template, request

from .data_source import POTS, DataSource
from .real_source import RealDataSource

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # dev/demo: don't cache static JS/CSS

# Both pots served from real sensor JSONL files
_real_source = RealDataSource()


def _source_for(pot: str) -> DataSource:
    return _real_source


DEFAULT_N = 60


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/raw")
def raw_page():
    return render_template("raw.html", pots=POTS)


@app.route("/inference")
def inference_page():
    return render_template("inference.html", pots=POTS)


@app.route("/api/raw")
def api_raw():
    pot = request.args.get("pot", "A")
    # n=0 means "return the full history" (used by initial chart load)
    n = request.args.get("n", DEFAULT_N, type=int)
    if pot not in POTS:
        return jsonify({"error": f"unknown pot {pot!r}, expected one of {POTS}"}), 400
    chart_range = request.args.get("range")
    max_points = request.args.get("max_points", type=int)
    return jsonify(_source_for(pot).get_raw(pot, n, range=chart_range, max_points=max_points))


@app.route("/api/inference")
def api_inference():
    pot = request.args.get("pot", "A")
    n = request.args.get("n", DEFAULT_N, type=int)
    if pot not in POTS:
        return jsonify({"error": f"unknown pot {pot!r}, expected one of {POTS}"}), 400
    chart_range = request.args.get("range")
    max_points = request.args.get("max_points", type=int)
    return jsonify(_source_for(pot).get_inference(pot, n, range=chart_range, max_points=max_points))


@app.route("/api/status")
def api_status():
    """Session metadata: collecting_since, reading counts, NTP sync status."""
    return jsonify(_real_source.get_status())


@app.route("/api/download/csv")
def api_download_csv():
    """Download sensor readings as a CSV file.

    Query params:
      pot=A    — Pot A only
      pot=B    — Pot B only
      (omit)   — both pots interleaved, sorted by timestamp
    """
    pot_param = request.args.get("pot", "").upper()
    pots_to_export = [pot_param] if pot_param in POTS else list(POTS)

    # CSV field order matches the SD card schema
    fieldnames = [
        "pot", "sample_index", "received_at", "millis_ms",
        "temp_c", "humidity_pct", "pressure_hpa", "gas_kohm",
        "light_lux", "moisture_pct",
    ]

    def generate():
        # Streams rows straight from disk via iter_raw_export_rows (2026-08-22
        # OOM fix) instead of materializing the full, ever-growing history as
        # one in-memory list before writing -- never holds more than one row
        # (plus one small StringIO buffer) in memory regardless of file size.
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")

        writer.writeheader()
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)

        for pot in pots_to_export:
            for row in _real_source.iter_raw_export_rows(pot):
                writer.writerow({
                    "pot": pot,
                    "sample_index": "",        # not in _to_raw_row; omit gracefully
                    "received_at": row.get("timestamp", ""),
                    "millis_ms": "",
                    "temp_c": row.get("temp_c", ""),
                    "humidity_pct": row.get("humidity_pct", ""),
                    "pressure_hpa": row.get("pressure_hpa", ""),
                    "gas_kohm": row.get("gas_index", ""),
                    "light_lux": row.get("light_lux", ""),
                    "moisture_pct": row.get("moisture_pct", ""),
                })
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)

    filename = f"ka_edge_pot{'_' + pot_param if pot_param else '_all'}_readings.csv"
    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    # use_reloader restarts the process on .py changes during iterative dev/demo
    # without debug=True's interactive debugger (which + host="0.0.0.0" would
    # expose remote code execution to anyone on the LAN).
    #
    # threaded=True (2026-08-30): without it, Werkzeug's dev server handles
    # ONE request at a time -- a slow request (e.g. /api/raw?range=today,
    # which streams+scans the full growing JSONL file on disk, see
    # real_source.py's _stream_decimated) blocked every other request,
    # including the 5s live-tick poll and even the SAME page's own filter
    # click, until it finished. On the board's slower ARM CPU against a
    # multi-hour readings file this made "Today"/"Week" look like the whole
    # dashboard had frozen, recoverable only by a hard page reload -- while
    # "5m"/"1h"/"overall" (served from bounded in-memory caches, no disk
    # scan) stayed fast and made it look like only those filters "worked".
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=True, threaded=True)
