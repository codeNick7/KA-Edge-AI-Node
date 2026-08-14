"""Board dashboard (Flask) — runs locally on the QRB2210 Linux side.

Two views:
  /raw       — Raw sensor stream, Pot A and Pot B in two vertical columns
  /inference — Inference charts, Pot A and Pot B separate

Hardware confirmed 2026-08-14:
  Pot A — BME688 @0x76 (Wire1/Qwiic), Cytron soil on A1
  Pot B — BME688 @0x77 (Wire1/Qwiic), Cytron soil on A0

API endpoints:
  /api/raw?pot=A&n=60     — last N readings (n=0 → full history)
  /api/inference?pot=A&n=60
  /api/status             — session metadata (collecting since, NTP sync, counts)
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
    return jsonify(_source_for(pot).get_raw(pot, n))


@app.route("/api/inference")
def api_inference():
    pot = request.args.get("pot", "A")
    n = request.args.get("n", DEFAULT_N, type=int)
    if pot not in POTS:
        return jsonify({"error": f"unknown pot {pot!r}, expected one of {POTS}"}), 400
    return jsonify(_source_for(pot).get_inference(pot, n))


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

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()

    for pot in pots_to_export:
        # get_raw with n=0 returns full history
        rows = _real_source.get_raw(pot, n=0)
        for row in rows:
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

    filename = f"ka_edge_pot{'_' + pot_param if pot_param else '_all'}_readings.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    # use_reloader restarts the process on .py changes during iterative dev/demo
    # without debug=True's interactive debugger (which + host="0.0.0.0" would
    # expose remote code execution to anyone on the LAN).
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=True)
