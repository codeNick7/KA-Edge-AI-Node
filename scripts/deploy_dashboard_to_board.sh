#!/usr/bin/env bash
# Clean local Python build artefacts out of source/ (dashboard + edge_node),
# then copy it to the board so the dashboard there is running the same code
# as this repo -- no cache/build cruft carried along for the ride.
#
# Usage (fish):
#   set -x BOARD <ssh-target>
#   set -x DEST <remote-project-root>
#   ./scripts/deploy_dashboard_to_board.sh
#
# BOARD is anything `ssh`/`scp` accept as a target (user@host, a host alias
# from ~/.ssh/config, etc). DEST is the board's shared project root -- the
# same DEST used by deploy_edge_node_to_board.sh, whose own remote paths
# ($DEST/python, $DEST/sketch) live alongside this script's target,
# $DEST/dashboard. Inside $DEST/dashboard this script lays out dashboard/,
# edge_node/, requirements.txt exactly as they sit under this repo's
# source/ -- app.py's own docstring says "Run from source/:
# .venv/bin/python -m dashboard.app", so the equivalent on the board is
# `cd $DEST/dashboard && python -m dashboard.app`.
#
# Requires: SSH access to the board already set up (key or password prompt).

set -euo pipefail

: "${BOARD:?Set BOARD to the board ssh target, e.g. BOARD=arduino@192.168.1.42}"
: "${DEST:?Set DEST to the remote project root, e.g. DEST=/home/arduino/ka-edge-node}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="${REPO_ROOT}/source"
REMOTE_DASHBOARD_DIR="${DEST}/dashboard"

echo "== Cleaning local Python build artefacts under ${SRC_DIR} =="
find "${SRC_DIR}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "${SRC_DIR}" -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
find "${SRC_DIR}" -type f -name "*.pyc" -delete
find "${SRC_DIR}" -type f -name ".DS_Store" -delete

echo "== Copying dashboard/ + edge_node/ + requirements.txt to ${BOARD}:${REMOTE_DASHBOARD_DIR} =="
ssh "${BOARD}" "mkdir -p '${REMOTE_DASHBOARD_DIR}'"
scp -r \
  "${SRC_DIR}/dashboard" \
  "${SRC_DIR}/edge_node" \
  "${REPO_ROOT}/requirements.txt" \
  "${BOARD}:${REMOTE_DASHBOARD_DIR}/"

echo "== Done. On the board: cd ${REMOTE_DASHBOARD_DIR} && pip install -r requirements.txt && python -m dashboard.app =="
