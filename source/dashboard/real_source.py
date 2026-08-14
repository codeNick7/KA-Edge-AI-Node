"""Real data source for both Pot A and Pot B.

Hardware confirmed 2026-08-14:
  Pot A — BME688 @0x76 (Wire1/Qwiic), Cytron soil sensor on A1
  Pot B — BME688 @0x77 (Wire1/Qwiic), Cytron soil sensor on A0

Readings arrive from sketch v7.0 over Bridge, are parsed by
KA-Edge-source/python/main.py (v3.0), and appended to two JSONL files:
  pot_a_readings.jsonl   (grows across restarts — full history preserved)
  pot_b_readings.jsonl

Each JSONL record has:
  sample_index, millis_ms, temp_c, humidity_pct, pressure_hpa,
  gas_kohm, light_lux, moisture_pct, received_at

Notes:
- gas_kohm is Adafruit_BME680's raw gas_resistance / 1000, NOT Bosch BSEC IAQ.
- light_lux is 0.00 (placeholder) until BH1750 is physically wired.
- moisture_pct is a real Cytron ADC reading for both pots.
- Data is persistent: files are appended on restart so the full trend
  from the first-ever reading is always available.
"""

import json
import os

from edge_node.decision_tree import classify
from edge_node.features import compute_light_delta, compute_rsmi, compute_trend_slope
from edge_node.validity import check_validity

from .mock_source import MODEL_VERSION

# Must match KA-Edge-source/python/main.py's DATA_DIR and filenames.
_BOARD_DATA_DIR = "/home/arduino/ArduinoApps/test-sensors/python/data"

DEFAULT_POT_A_PATH = os.path.join(_BOARD_DATA_DIR, "pot_a_readings.jsonl")
DEFAULT_POT_B_PATH = os.path.join(_BOARD_DATA_DIR, "pot_b_readings.jsonl")
DEFAULT_SESSION_LOG = os.path.join(_BOARD_DATA_DIR, "session_log.jsonl")

# Fallback for JSONL rows written before light_lux existed in the payload.
PLACEHOLDER_LIGHT_LUX = 400.0

# Soil texture assumed for RSMI conversion (loam default).
TEXTURE = "loam"

TREND_WINDOW = 7  # PRD Section 6 trend window


class RealDataSource:
    """Serves real sensor data for both Pot A and Pot B from JSONL relay files."""

    def __init__(
        self,
        pot_a_path: str = DEFAULT_POT_A_PATH,
        pot_b_path: str = DEFAULT_POT_B_PATH,
        session_log_path: str = DEFAULT_SESSION_LOG,
    ):
        self._paths = {"A": pot_a_path, "B": pot_b_path}
        self._session_log = session_log_path

    def _load_records(self, pot: str) -> list[dict]:
        path = self._paths[pot]
        if not os.path.exists(path):
            return []
        records = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # skip a partially-written line mid-append
                # Skip records where all BME fields are zero — offline sensor.
                if (
                    record.get("temp_c", 0.0) == 0.0
                    and record.get("pressure_hpa", 0.0) == 0.0
                    and record.get("gas_kohm", 0.0) == 0.0
                ):
                    continue
                records.append(record)
        records.sort(key=lambda r: r["sample_index"])
        return records

    def _to_raw_row(self, record: dict) -> dict:
        return {
            "timestamp": record["received_at"],
            "temp_c": record["temp_c"],
            "humidity_pct": record.get("humidity_pct", 0.0),
            "pressure_hpa": record.get("pressure_hpa", 0.0),
            "moisture_pct": record.get("moisture_pct", 0.0),
            "gas_index": record["gas_kohm"],
            "light_lux": record.get("light_lux", PLACEHOLDER_LIGHT_LUX),
        }

    def get_raw(self, pot: str, n: int) -> list[dict]:
        """Return the last n raw rows for pot. n=0 returns the full history."""
        if pot not in self._paths:
            raise ValueError(f"RealDataSource: unknown pot {pot!r}")
        records = self._load_records(pot)
        if n and n > 0:
            records = records[-n:]
        return [self._to_raw_row(r) for r in records]

    def get_inference(self, pot: str, n: int) -> list[dict]:
        if pot not in self._paths:
            raise ValueError(f"RealDataSource: unknown pot {pot!r}")
        raw = [self._to_raw_row(r) for r in self._load_records(pot)]

        results = []
        for i in range(TREND_WINDOW - 1, len(raw)):
            history = raw[i - TREND_WINDOW + 1 : i + 1]
            row = raw[i]
            prev_moisture = raw[i - 1]["moisture_pct"] if i > 0 else None

            validity = check_validity(
                row["moisture_pct"], row["gas_index"], row["temp_c"], prev_moisture
            )
            rsmi = compute_rsmi(row["moisture_pct"], TEXTURE)
            rsmi_history = [compute_rsmi(r["moisture_pct"], TEXTURE) for r in history]
            light_history = [r["light_lux"] for r in history]
            gas_history = [r["gas_index"] for r in history]

            light_delta = compute_light_delta(light_history)
            gas_index_trend = compute_trend_slope(gas_history)
            rsmi_trend = compute_trend_slope(rsmi_history)

            flag = classify(validity, rsmi, light_delta, gas_index_trend, rsmi_trend)
            results.append(
                {
                    "timestamp": row["timestamp"],
                    "rsmi": round(rsmi, 4),
                    "light_delta": round(light_delta, 2),
                    "gas_index_trend": round(gas_index_trend, 4),
                    "rsmi_trend": round(rsmi_trend, 4),
                    "flag": flag.flag,
                    "confidence": round(flag.confidence, 3),
                    "contributing_trend": flag.contributing_trend,
                    "model_version": MODEL_VERSION,
                }
            )
        if n and n > 0:
            return results[-n:]
        return results

    def get_status(self) -> dict:
        """Return session and collection metadata for the dashboard status bar."""
        status = {
            "collecting_since": None,
            "last_reading_at": None,
            "total_readings_a": 0,
            "total_readings_b": 0,
            "ntp_synced": None,
            "ntp_offset_s": None,
            "session_started_at": None,
        }

        # Count readings and find first/last timestamps from JSONL files
        for pot, key_total, key_first, key_last in [
            ("A", "total_readings_a", "_first_a", "_last_a"),
            ("B", "total_readings_b", "_first_b", "_last_b"),
        ]:
            records = self._load_records(pot)
            total = len(records)
            status[key_total] = total
            if records:
                first_ts = records[0].get("received_at")
                last_ts = records[-1].get("received_at")
                # collecting_since = earliest timestamp across both pots
                if status["collecting_since"] is None or (first_ts and first_ts < status["collecting_since"]):
                    status["collecting_since"] = first_ts
                # last_reading_at = most recent timestamp across both pots
                if status["last_reading_at"] is None or (last_ts and last_ts > status["last_reading_at"]):
                    status["last_reading_at"] = last_ts

        # Read most recent session_log entry for NTP info
        if os.path.exists(self._session_log):
            try:
                with open(self._session_log) as f:
                    lines = [l.strip() for l in f if l.strip()]
                if lines:
                    latest_session = json.loads(lines[-1])
                    status["ntp_synced"] = latest_session.get("ntp_synced")
                    status["ntp_offset_s"] = latest_session.get("ntp_offset_s")
                    status["session_started_at"] = latest_session.get("started_at")
            except (json.JSONDecodeError, OSError):
                pass

        return status
