#!/usr/bin/env bash
# Copies everything the board needs to run the dashboard + the standalone background
# inference service, from a clean local build. Does NOT delete anything on the board
# first -- the board's data directory (sensor_data.db, session_log.jsonl, sd_status.json
# -- see docs/plan/sqlite-storage-migration.md for the 2026-09-16 move off
# pot_{a,b}_readings.jsonl/pot_{a,b}_inference_cache.jsonl) lives under this same code
# tree (python/data -- see real_source.py's _BOARD_DATA_DIR), so an automatic `rm -rf`
# here would risk destroying live sensor history along with the code. If you want a
# clean slate, delete the board-side code files yourself first (selectively, leaving
# python/data alone), then run this script. Every file the board needs is listed
# explicitly below, nothing is assumed to already be there from a previous copy.
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
# edge_node/, inference_service.py, requirements.txt -- app.py's own docstring says
# "Run from source/: .venv/bin/python -m dashboard.app", so the equivalent on the
# board is `cd $DEST/dashboard && python -m dashboard.app`, and
# `cd $DEST/dashboard && python inference_service.py --data-dir <dir-containing-sensor_data.db> --watch`
# for the background routine that keeps inference_results current (see
# docs/plan/nn-inference-upgrade-plan.md's Phase 4 note -- the dashboard computes
# nothing itself, it only reads what that routine writes -- and
# docs/plan/sqlite-storage-migration.md for the board_db.py-backed schema both of
# them share, which is why board_db.py is copied here too, not just to $DEST/python).
#
# edge_node/ is copied SELECTIVELY, not wholesale: only the files the dashboard and
# inference_service.py actually import at runtime (features/validity/texture_table/
# decision_tree/trained_model.py, all dependency-free). train.py, export.py,
# tiny_transformer.py, export_transformer.py are dev-machine-only training/export
# tooling that needs numpy/scipy/scikit-learn -- deliberately left off the board, per
# the architecture doc's "dependency-free, years of unattended operation" design
# (Section 9). requirements.txt copied is source/requirements-board.txt (just flask),
# not the top-level dev requirements.txt.
#
# Requires: SSH access to the board already set up (key or password prompt). This
# script makes several separate ssh/scp calls (one per logical group of files, not
# one giant transfer) -- to avoid re-prompting for a password on every single one, it
# opens one authenticated connection up front (SSH ControlMaster) and every later
# ssh/scp call in this script reuses it, so a password (if you're not using key auth)
# is asked for exactly once, not once per file/group.

set -euo pipefail

: "${BOARD:?Set BOARD to the board ssh target, e.g. BOARD=arduino@192.168.1.42}"
: "${DEST:?Set DEST to the remote project root, e.g. DEST=/home/arduino/ka-edge-node}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="${REPO_ROOT}/source"
REMOTE_DASHBOARD_DIR="${DEST}/dashboard"

EDGE_NODE_RUNTIME_FILES=(
  "__init__.py"
  "features.py"
  "texture_table.py"
  "validity.py"
  "decision_tree.py"
  "trained_model.py"
)

CONTROL_PATH="$(mktemp -u /tmp/ka-deploy-ssh-XXXXXX)"
SSH_OPTS=(-o "ControlMaster=auto" -o "ControlPath=${CONTROL_PATH}" -o "ControlPersist=60")
cleanup() { ssh "${SSH_OPTS[@]}" -O exit "${BOARD}" 2>/dev/null || true; }
trap cleanup EXIT

echo "== Opening one SSH connection to ${BOARD} (reused for every call below) =="
ssh "${SSH_OPTS[@]}" "${BOARD}" true

echo "== Cleaning local Python build artefacts under ${SRC_DIR} =="
find "${SRC_DIR}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "${SRC_DIR}" -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
find "${SRC_DIR}" -type f -name "*.pyc" -delete
find "${SRC_DIR}" -type f -name ".DS_Store" -delete

echo "== Ensuring ${BOARD}:${REMOTE_DASHBOARD_DIR} exists (not wiped -- see header comment) =="
ssh "${SSH_OPTS[@]}" "${BOARD}" "mkdir -p '${REMOTE_DASHBOARD_DIR}/edge_node'"

echo "== Copying dashboard/ (app + templates/static) =="
scp "${SSH_OPTS[@]}" -r "${SRC_DIR}/dashboard" "${BOARD}:${REMOTE_DASHBOARD_DIR}/"

echo "== Copying edge_node/ runtime files only (no training/export tooling) =="
edge_node_paths=()
for f in "${EDGE_NODE_RUNTIME_FILES[@]}"; do
  edge_node_paths+=("${SRC_DIR}/edge_node/${f}")
done
scp "${SSH_OPTS[@]}" "${edge_node_paths[@]}" "${BOARD}:${REMOTE_DASHBOARD_DIR}/edge_node/"

echo "== Copying the standalone background inference service + its board_db.py dependency =="
scp "${SSH_OPTS[@]}" \
  "${REPO_ROOT}/scripts/inference_service.py" \
  "${REPO_ROOT}/KA-Edge-source/python/board_db.py" \
  "${BOARD}:${REMOTE_DASHBOARD_DIR}/"

echo "== Copying board-minimal requirements.txt (flask only) =="
scp "${SSH_OPTS[@]}" "${SRC_DIR}/requirements-board.txt" "${BOARD}:${REMOTE_DASHBOARD_DIR}/requirements.txt"

echo "== Done. On the board:"
echo "     cd ${REMOTE_DASHBOARD_DIR} && pip install -r requirements.txt"
echo "     python inference_service.py --data-dir <dir-containing-sensor_data.db> --watch &"
echo "     python -m dashboard.app"
