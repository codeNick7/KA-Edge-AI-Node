#!/usr/bin/env bash
# Clean local Python build artefacts out of KA-Edge-source/python, then copy
# both the Arduino sketch and the board-side Python relay to the board.
#
# Usage (fish):
#   set -x BOARD <ssh-target>
#   set -x DEST <remote-project-root>
#   ./scripts/deploy_edge_node_to_board.sh
#
# BOARD is anything `ssh`/`scp` accept as a target (user@host, a host alias
# from ~/.ssh/config, etc). DEST is the board's shared project root -- the
# same DEST deploy_dashboard_to_board.sh uses (its target,
# $DEST/dashboard, lives alongside this script's $DEST/python and
# $DEST/sketch). After this script, $DEST contains python/ and sketch/
# directly, matching this repo's KA-Edge-source/{python,sketch} layout (see
# real_source.py's _BOARD_DATA_DIR comment for the App Lab project path
# convention this mirrors).
#
# Deliberately does NOT copy .env: that file holds the board's live
# KARBON_ASSURE_DEVICE_SECRET (see push_to_karbon_assure.py), gitignored and
# never meant to leave the board it was issued for -- overwriting it from a
# dev machine's .env (if one even exists locally) would silently break the
# board's push auth or point it at the wrong destination. Python files are
# copied individually (not a wholesale directory copy) specifically so a
# stray local .env can never be swept up by accident. The sketch/ directory
# has no such secret and is copied wholesale (see below), so it never misses
# a file (header, extra .ino tab, etc.) added later without this script
# being updated to know its name.
#
# Requires: SSH access to the board already set up (key or password prompt).
# This script opens one authenticated SSH connection up front (ControlMaster,
# same pattern deploy_dashboard_to_board.sh already uses) and every later
# ssh/scp call below reuses it, so a password (if you're not using key auth)
# is asked for once per run, not once per file/group.

set -euo pipefail

: "${BOARD:?Set BOARD to the board ssh target, e.g. BOARD=arduino@192.168.1.42}"
: "${DEST:?Set DEST to the remote project root, e.g. DEST=/home/arduino/ArduinoApps/ka-edge-node}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_DIR="${REPO_ROOT}/KA-Edge-source/python"
SKETCH_DIR="${REPO_ROOT}/KA-Edge-source/sketch"

CONTROL_PATH="$(mktemp -u /tmp/ka-deploy-ssh-XXXXXX)"
SSH_OPTS=(-o "ControlMaster=auto" -o "ControlPath=${CONTROL_PATH}" -o "ControlPersist=60")
cleanup() { ssh "${SSH_OPTS[@]}" -O exit "${BOARD}" 2>/dev/null || true; }
trap cleanup EXIT

echo "== Opening one SSH connection to ${BOARD} (reused for every call below) =="
ssh "${SSH_OPTS[@]}" "${BOARD}" true

echo "== Cleaning local Python build artefacts under ${PY_DIR} =="
find "${PY_DIR}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "${PY_DIR}" -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
find "${PY_DIR}" -type f -name "*.pyc" -delete

echo "== Copying board-side Python (main.py + friends, no .env) to ${BOARD}:${DEST}/python =="
ssh "${SSH_OPTS[@]}" "${BOARD}" "mkdir -p '${DEST}/python' '${DEST}/sketch'"
scp "${SSH_OPTS[@]}" \
  "${PY_DIR}/main.py" \
  "${PY_DIR}/board_db.py" \
  "${PY_DIR}/push_to_karbon_assure.py" \
  "${PY_DIR}/sync_queue.py" \
  "${PY_DIR}/requirements.txt" \
  "${BOARD}:${DEST}/python/"
scp "${SSH_OPTS[@]}" -r "${PY_DIR}/edge_node" "${BOARD}:${DEST}/python/"

echo "== Copying Arduino sketch (every file under KA-Edge-source/sketch/) to ${BOARD}:${DEST}/sketch =="
scp "${SSH_OPTS[@]}" -r "${SKETCH_DIR}"/* "${BOARD}:${DEST}/sketch/"

echo "== Done. .env was NOT touched -- board's existing KARBON_ASSURE_* config is untouched."
echo "   On the board: cd ${DEST}/python && pip install -r requirements.txt"
