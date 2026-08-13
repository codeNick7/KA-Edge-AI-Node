"""Board dashboard (Flask) — runs locally on the QRB2210 Linux side.

Two views:
  /raw       — Raw sensor stream, Pot A and Pot B in two vertical columns
  /inference — Inference charts, Pot A and Pot B separate

Both pots now backed by RealDataSource (real_source.py) fed by
KA-Edge-source/python/main.py via JSONL relay files:
  Pot A — BME688 @0x76, Cytron soil A0
  Pot B — BME688 @0x77, Cytron soil A1

Run from source/:  .venv/bin/python -m dashboard.app
"""

from flask import Flask, jsonify, render_template, request

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


if __name__ == "__main__":
    # use_reloader restarts the process on .py changes during iterative dev/demo
    # without debug=True's interactive debugger (which + host="0.0.0.0" would
    # expose remote code execution to anyone on the LAN).
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=True)
