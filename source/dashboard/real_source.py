"""Real data source for both Pot A and Pot B.

Hardware confirmed 2026-08-14:
  Pot A — BME688 @0x76 (Wire1/Qwiic), Cytron soil sensor on A0
  Pot B — BME688 @0x77 (Wire1/Qwiic), Cytron soil sensor on A1

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
# App Lab project: /home/arduino/ArduinoApps/ka-edge-node/
_BOARD_DATA_DIR = "/home/arduino/ArduinoApps/ka-edge-node/python/data"

DEFAULT_POT_A_PATH = os.path.join(_BOARD_DATA_DIR, "pot_a_readings.jsonl")
DEFAULT_POT_B_PATH = os.path.join(_BOARD_DATA_DIR, "pot_b_readings.jsonl")
DEFAULT_SESSION_LOG = os.path.join(_BOARD_DATA_DIR, "session_log.jsonl")
# Written by main.py's handle_status() from sketch.ino's periodic SD_SPACE
# report — the physical SD card on the MCU side, not the QRB2210's own disk.
DEFAULT_SD_STATUS_PATH = os.path.join(_BOARD_DATA_DIR, "sd_status.json")

# Fallback for JSONL rows written before light_lux existed in the payload.
PLACEHOLDER_LIGHT_LUX = 400.0

# Soil texture assumed for RSMI conversion.
# Interim: clay_loam (WP 34 / FC 70 on the sensor's own 0-100 scale, see
# texture_table.py's 2026-08-14 unit-fix note), not loam — the pot mix (60%
# red soil / 20% clay / 20% manure) is closer to clay_loam than to plain
# loam, and clay_loam is a real cited table entry rather than loam's
# interpolated midpoint. Still provisional: replace with a jar-test +
# Saxton-Rawls (2006) calculated value once the sedimentation test is done —
# manure/organic matter will likely push field capacity higher still.
TEXTURE = "clay_loam"


TREND_WINDOW = 7  # PRD Section 6 trend window


class RealDataSource:
    """Serves real sensor data for both Pot A and Pot B from JSONL relay files."""

    def __init__(
        self,
        pot_a_path: str = DEFAULT_POT_A_PATH,
        pot_b_path: str = DEFAULT_POT_B_PATH,
        session_log_path: str = DEFAULT_SESSION_LOG,
        sd_status_path: str = DEFAULT_SD_STATUS_PATH,
    ):
        self._paths = {"A": pot_a_path, "B": pot_b_path}
        self._session_log = session_log_path
        self._sd_status_path = sd_status_path
        # Per-pot incremental read state: byte offset already consumed, plus
        # the file identity (mtime, size) that offset is valid against.
        # Avoids re-reading + re-parsing the whole (ever-growing) JSONL file
        # on every request — see 2026-08-15 dashboard perf conversation.
        self._record_cache = {
            pot: {"mtime": None, "size": 0, "offset": 0, "records": []}
            for pot in self._paths
        }
        # Per-pot incremental inference state: how many raw rows (post
        # dedup/filter) have already been classified, so a live 5s poll only
        # runs the RSMI/trend/classify pipeline on newly-arrived rows instead
        # of the entire history every time.
        self._inference_cache = {pot: {"raw_len": 0, "results": []} for pot in self._paths}

    def _load_records(self, pot: str) -> list[dict]:
        path = self._paths[pot]
        cache = self._record_cache[pot]

        if not os.path.exists(path):
            cache["mtime"] = None
            cache["size"] = 0
            cache["offset"] = 0
            cache["records"] = []
            return []

        stat = os.stat(path)
        if cache["mtime"] == stat.st_mtime and cache["size"] == stat.st_size:
            return cache["records"]  # unchanged since last call — no disk I/O

        if stat.st_size < cache["offset"]:
            # File shrank (truncated/rotated) — invalidate and re-read fully.
            cache["offset"] = 0
            cache["records"] = []

        new_records = []
        with open(path, "rb") as f:
            f.seek(cache["offset"])
            chunk = f.read()

        # Only consume complete (newline-terminated) lines. A trailing partial
        # line (mid-append by the relay process) is left unconsumed so it's
        # re-read in full next call, rather than being permanently skipped —
        # a plain per-call full-file reread never had this failure mode since
        # it always restarted from byte 0.
        lines = chunk.split(b"\n")
        remainder = lines[-1]
        complete_lines = lines[:-1]
        cache["offset"] += len(chunk) - len(remainder)

        for raw_line in complete_lines:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Skip records where all BME fields are zero — offline sensor.
            if (
                record.get("temp_c", 0.0) == 0.0
                and record.get("pressure_hpa", 0.0) == 0.0
                and record.get("gas_kohm", 0.0) == 0.0
            ):
                continue
            new_records.append(record)

        if new_records:
            prev_last = cache["records"][-1].get("received_at", "") if cache["records"] else ""
            already_ordered = new_records[0].get("received_at", "") >= prev_last and all(
                new_records[i].get("received_at", "") <= new_records[i + 1].get("received_at", "")
                for i in range(len(new_records) - 1)
            )
            cache["records"].extend(new_records)
            if not already_ordered:
                # Rare (Bridge relay reordering) — full resort as a fallback,
                # same guarantee the old always-resort behavior gave.
                cache["records"].sort(key=lambda r: r.get("received_at", ""))

        cache["mtime"] = stat.st_mtime
        cache["size"] = stat.st_size
        return cache["records"]


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

        cache = self._inference_cache[pot]
        if cache["raw_len"] > len(raw):
            # Raw history shrank (file truncated/rotated) — recompute clean.
            cache["raw_len"] = 0
            cache["results"] = []
        start = max(cache["raw_len"], TREND_WINDOW - 1)

        results = cache["results"]
        for i in range(start, len(raw)):
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
        cache["raw_len"] = len(raw)
        if n and n > 0:
            return results[-n:]
        return list(results)

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
            "sd_used_mb": None,
            "sd_free_mb": None,
            "sd_total_mb": None,
            "sd_status_updated_at": None,
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

        # Read latest SD-card free-space report (physical card on the MCU
        # side — see sketch.ino's reportSdSpace()). Overwritten in place by
        # main.py, so this is always just the single latest reading; may be
        # absent entirely on firmware that predates this feature, or for the
        # first ~60s after board boot before the first report arrives.
        if os.path.exists(self._sd_status_path):
            try:
                with open(self._sd_status_path) as f:
                    sd_entry = json.load(f)
                status["sd_used_mb"] = sd_entry.get("sd_used_mb")
                status["sd_free_mb"] = sd_entry.get("sd_free_mb")
                status["sd_total_mb"] = sd_entry.get("sd_total_mb")
                status["sd_status_updated_at"] = sd_entry.get("updated_at")
            except (json.JSONDecodeError, OSError):
                pass

        return status
