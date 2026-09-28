#!/usr/bin/env bash
# Quick diagnostic utility to check the running state of the KA-Edge node on the Arduino board over SSH.
#
# Usage:
#   BOARD=arduino@192.168.1.42 ./scripts/board_diagnose.sh
#   or
#   BOARD=<ssh-alias> ./scripts/board_diagnose.sh

set -euo pipefail

: "${BOARD:?Set BOARD to your board ssh target, e.g. BOARD=arduino@192.168.1.42}"

echo "=========================================================="
echo " KA-Edge Node Live Board Diagnostics (${BOARD})"
echo "=========================================================="

echo -e "\n1. Checking running Python processes on the board:"
ssh "${BOARD}" "ps aux | grep -E 'python|applab' | grep -v grep || echo 'No active python processes found.'"

echo -e "\n2. Checking deployed project directory contents:"
ssh "${BOARD}" "ls -la /home/arduino/ArduinoApps/*/python/ 2>/dev/null || echo 'No /home/arduino/ArduinoApps/*/python directory found.'"

echo -e "\n3. Checking version in deployed main.py files:"
ssh "${BOARD}" "grep -H 'PYTHON_VERSION =' /home/arduino/ArduinoApps/*/python/main.py 2>/dev/null || grep -H 'PYTHON_VERSION =' /home/arduino/*/python/main.py 2>/dev/null || echo 'main.py not found.'"

echo -e "\n4. Checking latest session log entries:"
ssh "${BOARD}" "tail -n 5 /home/arduino/ArduinoApps/*/python/data/session_log.jsonl 2>/dev/null || echo 'No session_log.jsonl found.'"

echo -e "\n5. Checking latest SQLite sensor readings (last 5 rows):"
ssh "${BOARD}" "sqlite3 /home/arduino/ArduinoApps/ka-edge-node/python/data/sensor_data.db 'SELECT id, pot_id, sample_index, temp_c, moisture_pct, light_lux, received_at FROM readings ORDER BY id DESC LIMIT 5;' 2>/dev/null || echo 'Could not query sensor_data.db'"

echo -e "\n6. Checking log file if running under crontab / daemon:"
ssh "${BOARD}" "tail -n 10 /home/arduino/logs/ka-edge.log 2>/dev/null || echo 'No /home/arduino/logs/ka-edge.log found.'"

echo -e "\n=========================================================="
echo " Diagnostics complete."
echo "=========================================================="
