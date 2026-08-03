#!/usr/bin/env bash
# Run the same synthetic training/export dry-run ON the Arduino UNO Q board
# (QRB2210, Debian Linux, arm64, Python 3.13) for a real-hardware timing
# comparison against dev-machine numbers, and to sanity-check that the
# exported dependency-free model runs cleanly on the board Python.
#
# This dev machine and the board are both arm64, but the board runs Python
# 3.13 vs this machine 3.14 -- a fresh venv on the board avoids any
# pickle/binary compatibility question entirely, since this script only
# ever transfers source (.py) files, never a serialized model.
#
# Usage:
#   BOARD_HOST=<board-ip-or-hostname> BOARD_USER=<ssh-user> ./scripts/board_dry_run.sh
#
# Requires: SSH access to the board already set up (key or password prompt),
# and the board reachable on the network. This script does not attempt to
# discover the board address -- pass it explicitly via BOARD_HOST.

set -euo pipefail

: "${BOARD_HOST:?Set BOARD_HOST to the board IP or hostname, e.g. BOARD_HOST=192.168.1.42}"
: "${BOARD_USER:?Set BOARD_USER to the SSH user on the board, e.g. BOARD_USER=arduino}"

ROWS="${ROWS:-2880}"
REMOTE_DIR="${REMOTE_DIR:-karbon-assure-dry-run}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== Copying source + requirements.txt to ${BOARD_USER}@${BOARD_HOST}:${REMOTE_DIR} =="
ssh "${BOARD_USER}@${BOARD_HOST}" "mkdir -p ${REMOTE_DIR}"
scp -r \
  "${REPO_ROOT}/source" \
  "${REPO_ROOT}/requirements.txt" \
  "${REPO_ROOT}/scripts/check_exported_model.py" \
  "${BOARD_USER}@${BOARD_HOST}:${REMOTE_DIR}/"

echo "== Setting up a fresh venv on the board and running the timed dry-run =="
ssh "${BOARD_USER}@${BOARD_HOST}" "REMOTE_DIR=${REMOTE_DIR} ROWS=${ROWS} bash -s" < "${REPO_ROOT}/scripts/_board_dry_run_remote.sh"

echo "== Done. Board timing numbers are in the output above. =="
