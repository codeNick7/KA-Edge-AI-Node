# v3.0 — dual-pot relay with NTP time sync and persistent data (no truncation on restart).
#
# Changes vs v2.0:
#   - Data is APPENDED across restarts (no truncation) so the dashboard can
#     show the full trend from first-ever reading to now.
#   - NTP check at startup: queries pool.ntp.org to verify/log clock offset.
#     Does not require root — just measures and logs the delta.
#   - session_log.jsonl records each startup with timestamp and NTP status.
#
# CSV field order (positions 0–13) — unchanged from v2.0:
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
#  12  moistureA_pct   (Pot A → A1)
#  13  moistureB_pct   (Pot B → A0)
#
# Hardware confirmed 2026-08-14:
#   Pot A: BME688 @0x76, soil A1, Wire1 (Qwiic)
#   Pot B: BME688 @0x77, soil A0, Wire1 (Qwiic)
import json
import os
import socket
import struct
import time
from datetime import datetime, timezone

from arduino.app_utils import App, Bridge

bridge = Bridge()

DATA_DIR        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
POT_A_PATH      = os.path.join(DATA_DIR, "pot_a_readings.jsonl")
POT_B_PATH      = os.path.join(DATA_DIR, "pot_b_readings.jsonl")
SESSION_LOG     = os.path.join(DATA_DIR, "session_log.jsonl")

os.makedirs(DATA_DIR, exist_ok=True)

# ── NTP check ─────────────────────────────────────────────────────────────────
# Queries an NTP server via raw UDP (no external library needed).
# Returns (ntp_utc_timestamp, offset_seconds) or (None, None) on failure.
_NTP_HOST = "pool.ntp.org"
_NTP_PORT = 123
_NTP_EPOCH_DELTA = 2208988800  # seconds between NTP epoch (1900) and Unix epoch (1970)


def _query_ntp(host=_NTP_HOST, port=_NTP_PORT, timeout=5):
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(timeout)
        # Minimal NTP request packet (mode=3 client, version=3)
        packet = b"\x1b" + 47 * b"\x00"
        client.sendto(packet, (host, port))
        data, _ = client.recvfrom(1024)
        client.close()
        # Transmit Timestamp is at byte offset 40 (field index 10 in 4-byte words)
        ntp_time = struct.unpack("!12I", data)[10] - _NTP_EPOCH_DELTA
        offset = ntp_time - time.time()
        return ntp_time, offset
    except Exception as exc:
        return None, None


def _ntp_sync_check():
    """Query NTP, log result, return dict for session log."""
    print("[NTP] Querying pool.ntp.org …", flush=True)
    ntp_time, offset = _query_ntp()
    if ntp_time is not None:
        print(
            f"[NTP] Synced. System clock offset: {offset:+.3f} s "
            f"({'OK' if abs(offset) < 2 else 'WARNING: large offset'})",
            flush=True,
        )
        return {"ntp_synced": True, "ntp_offset_s": round(offset, 3)}
    else:
        print(
            "[NTP] WARNING: could not reach pool.ntp.org — "
            "timestamps will use system clock as-is.",
            flush=True,
        )
        return {"ntp_synced": False, "ntp_offset_s": None}


# ── Session start ─────────────────────────────────────────────────────────────
def _write_session_start(ntp_info: dict):
    entry = {
        "event": "session_start",
        "started_at": datetime.now(timezone.utc).isoformat(),
        **ntp_info,
    }
    with open(SESSION_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[Session] Started at {entry['started_at']}", flush=True)


# Run NTP check and log session start at import time
_ntp_info = _ntp_sync_check()
_write_session_start(_ntp_info)


# ── Bridge handlers ────────────────────────────────────────────────────────────
def handle_status(msg):
    print(f"[SD Bus Status]: {msg}")


def handle_log(msg):
    print(f"[Log]: {msg}")


def handle_reading(msg):
    """Parse a 14-field CSV reading from sketch v7.0 and append two JSONL records.

    Field layout — see module docstring above.
    Data is APPENDED (not overwritten) so history survives restarts.
    """
    try:
        parts = msg.split(",")
        if len(parts) < 14:
            raise ValueError(f"expected 14 fields, got {len(parts)}: {msg!r}")

        received_at = datetime.now(timezone.utc).isoformat()
        sample_index = int(parts[0])
        millis_ms = int(parts[1])

        # Pot A record (BME688 @0x76, soil A1)
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

        # Pot B record (BME688 @0x77, soil A0)
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

    # IMPORTANT: this file must live inside the App Lab project directory.
    # App Lab project: /home/arduino/ArduinoApps/ka-edge-node/
    # Data files are written to: ka-edge-node/python/data/
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

    print(
        f"[Reading #{sample_index}] "
        f"A: temp={rec_a['temp_c']}C hum={rec_a['humidity_pct']}% soil={rec_a['moisture_pct']}% | "
        f"B: temp={rec_b['temp_c']}C hum={rec_b['humidity_pct']}% soil={rec_b['moisture_pct']}%"
    )


bridge.provide("sd_status", handle_status)
bridge.provide("write_log", handle_log)
bridge.provide("reading", handle_reading)

if __name__ == "__main__":
    print("[Python v3.0] Dual-pot sensor relay started — Pot A (0x76/A1) and Pot B (0x77/A0)…")
    App.run()
