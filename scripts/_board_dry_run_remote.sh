#!/usr/bin/env bash
# Executed ON the board by board_dry_run.sh via ssh piping this file to
# "bash -s". Not meant to be run standalone from this repo. REMOTE_DIR and
# ROWS arrive as environment variables set by the caller.
set -euo pipefail

cd "${REMOTE_DIR}"

echo "-- board python/arch --"
python3 --version
uname -m

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

cd source
../.venv/bin/python -m edge_node.train --rows "${ROWS}" --export /tmp/trained_dry_run.py

echo "-- sanity-checking the exported dependency-free module runs standalone --"
../.venv/bin/python ../check_exported_model.py /tmp/trained_dry_run.py
