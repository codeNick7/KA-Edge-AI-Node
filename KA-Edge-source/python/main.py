# v2.0 — dual-pot: parses 14-field CSV message from sketch v6.0.
# Writes two separate JSONL relay files:
#   data/pot_a_readings.jsonl  — Pot A (BME688 @0x76, soil A0)
#   data/pot_b_readings.jsonl  — Pot B (BME688 @0x77, soil A1)
#
# CSV field order (positions 0–13):
#   0  #               sample index
#   1  millis_ms
#   2  tempA_C
#   3  humA_pct
#   4  pressA_hPa
#   5  gasA_KOhm
#   6  tempB_C
#   7  humB_pct
#   8  pressB_hPa
#   9  gasB_KOhm
#  10  lightA_lux      (placeholder 0.00 until BH1750 is wired)
#  11  lightB_lux      (placeholder 0.00 until BH1750 is wired)
#  12  moistureA_pct
#  13  moistureB_pct
#
# IMPORTANT: this file must live inside the App Lab project directory.
# App Lab sandboxes this script's process; only the project directory itself
# (KA-Edge-source/, deployed at /home/arduino/ArduinoApps/test-sensors/) is
# proven host-visible (a path outside the project dir was written successfully
# from inside the sandbox but never appeared on the host filesystem).
import json
import os
from datetime import datetime, timezone

from arduino.app_utils import App, Bridge

bridge = Bridge()

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
POT_A_PATH = os.path.join(DATA_DIR, "pot_a_readings.jsonl")
POT_B_PATH = os.path.join(DATA_DIR, "pot_b_readings.jsonl")

# Truncate relay files on every App Lab startup so the dashboard shows only
# readings from the current session. Stale records from a previous run would
# otherwise persist in the JSONL and pollute live charts.
os.makedirs(DATA_DIR, exist_ok=True)
open(POT_A_PATH, "w").close()
open(POT_B_PATH, "w").close()


def handle_status(msg):
    print(f"[SD Bus Status]: {msg}")


def handle_log(msg):
    print(f"[Log]: {msg}")


def handle_reading(msg):
    """Parse a 14-field CSV reading from sketch v6.0 and write two JSONL records.

    Field layout — see module docstring above.
    """
    try:
        parts = msg.split(",")
        if len(parts) < 14:
            raise ValueError(f"expected 14 fields, got {len(parts)}: {msg!r}")

        received_at = datetime.now(timezone.utc).isoformat()
        sample_index = int(parts[0])
        millis_ms = int(parts[1])

        # Pot A record (BME688 @0x76, soil A0)
        rec_a = {
            "sample_index": sample_index,
            "millis_ms": millis_ms,
            "temp_c": float(parts[2]),
            "humidity_pct": float(parts[3]),
            "pressure_hpa": float(parts[4]),
            "gas_kohm": float(parts[5]),
            "light_lux": float(parts[10]),
            "moisture_pct": float(parts[12]),
            "received_at": received_at,
        }

        # Pot B record (BME688 @0x77, soil A1)
        rec_b = {
            "sample_index": sample_index,
            "millis_ms": millis_ms,
            "temp_c": float(parts[6]),
            "humidity_pct": float(parts[7]),
            "pressure_hpa": float(parts[8]),
            "gas_kohm": float(parts[9]),
            "light_lux": float(parts[11]),
            "moisture_pct": float(parts[13]),
            "received_at": received_at,
        }

    except (ValueError, IndexError) as e:
        print(f"[Reading] WARN: skipping malformed message {msg!r}: {e}")
        return

    # Skip records where both BME sets are all zeros — indicates both sensors
    # offline (bme.performReading() failed for both pots this cycle).
    all_zero_a = rec_a["temp_c"] == 0.0 and rec_a["pressure_hpa"] == 0.0 and rec_a["gas_kohm"] == 0.0
    all_zero_b = rec_b["temp_c"] == 0.0 and rec_b["pressure_hpa"] == 0.0 and rec_b["gas_kohm"] == 0.0
    if all_zero_a and all_zero_b:
        print(f"[Reading] WARN: sample {sample_index} has all-zero BME fields for both pots — skipping")
        return

    os.makedirs(DATA_DIR, exist_ok=True)

    # Write Pot A only if its BME had a valid reading this cycle.
    if not all_zero_a:
        with open(POT_A_PATH, "a") as f:
            f.write(json.dumps(rec_a) + "\n")

    # Write Pot B only if its BME had a valid reading this cycle.
    if not all_zero_b:
        with open(POT_B_PATH, "a") as f:
            f.write(json.dumps(rec_b) + "\n")

    print(f"[Reading #{sample_index}] A: temp={rec_a['temp_c']}C hum={rec_a['humidity_pct']}% "
          f"soil={rec_a['moisture_pct']}% | "
          f"B: temp={rec_b['temp_c']}C hum={rec_b['humidity_pct']}% soil={rec_b['moisture_pct']}%")


bridge.provide("sd_status", handle_status)
bridge.provide("write_log", handle_log)
bridge.provide("reading", handle_reading)

if __name__ == "__main__":
    print("[Python v2.0] Dual-pot sensor relay started — monitoring Pot A (0x76/A0) and Pot B (0x77/A1)...")
    App.run()
