"""Board dashboard (Flask) - runs locally on the QRB2210 Linux side.

Two parts (per user request):
  1. Raw sensor stream - Pot A and Pot B grouped together (/raw)
  2. Inference charts - Pot A and Pot B separate (/inference)

Backed by MockDataSource (mock_source.py) until the real SQLite evidence
store exists. Run from source/: .venv/bin/python -m dashboard.app
"""

from flask import Flask, jsonify, render_template, request

from .data_source import POTS
from .mock_source import MockDataSource

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # dev/demo: don't let browsers cache static JS/CSS
data_source = MockDataSource()

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
    return jsonify(data_source.get_raw(pot, n))


@app.route("/api/inference")
def api_inference():
    pot = request.args.get("pot", "A")
    n = request.args.get("n", DEFAULT_N, type=int)
    if pot not in POTS:
        return jsonify({"error": f"unknown pot {pot!r}, expected one of {POTS}"}), 400
    return jsonify(data_source.get_inference(pot, n))


if __name__ == "__main__":
    # use_reloader restarts the process on .py changes during this iterative dev/demo
    # phase, without debug=True's interactive debugger -- that combined with
    # host="0.0.0.0" would expose remote code execution to anyone on the LAN.
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=True)
