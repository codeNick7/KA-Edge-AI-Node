"""Real data source for both Pot A and Pot B.

Soil pin mapping corrected 2026-08-15 (the 2026-08-14 "confirmed" mapping
below was backwards from the very start of data collection — see
sketch.ino's header and scripts/migrate_soil_pin_swap.py for the
historical-data fix this forced):
  Pot A — BME688 @0x76 (Wire1/Qwiic), Cytron soil sensor on A1
  Pot B — BME688 @0x77 (Wire1/Qwiic), Cytron soil sensor on A0

Readings arrive from sketch v7.0 over Bridge, are parsed by
KA-Edge-source/python/main.py (v3.0), and inserted into board_db's SQLite
schema (KA-Edge-source/python/board_db.py: readings / inference_results /
raw_daily_rollup / inference_daily_rollup tables).

Each `readings` row has: pot_id, sample_index, millis_ms, temp_c,
humidity_pct, pressure_hpa, gas_kohm, light_lux, moisture_pct, received_at.

Notes:
- gas_kohm is Adafruit_BME680's raw gas_resistance / 1000, NOT Bosch BSEC IAQ.
- light_lux is 0.00 (placeholder) until BH1750 is physically wired.
- moisture_pct is a real Cytron ADC reading for both pots.
- All-zero-BME rows (offline sensor) are filtered at insert time by
  main.py, not read time -- unlike the old JSONL version, `readings` never
  contains one, so there's nothing to skip here.

Storage migration (2026-09-16, see docs/plan/sqlite-storage-migration.md): this file
used to hand-roll a byte-offset tail cache + streaming decimation over two
ever-growing JSONL files (~40MB/day combined, no retention, was heading toward
filling the board's disk). That's replaced by plain SQL over board_db's indexed
tables: range filters become `WHERE received_at >= ?`, decimation becomes a
`ROW_NUMBER() OVER (...)` stride query. This is a pure reader -- inference is
computed and inserted by scripts/inference_service.py, exactly as before.
board_db.RETENTION_DAYS-old rows are rolled into the permanent
raw_daily_rollup/inference_daily_rollup tables and deleted from the live tables;
get_status() blends both so total counts / collecting_since stay correct even
past that cutoff. Chart ranges (overall/week) beyond the retention window losing
raw-resolution detail on the board's own dashboard is a known, deferred
limitation (see that plan doc's Phase 2) -- CSV export and the live charts here
only ever see what's still in `readings`/`inference_results`.

No more in-memory tail cache or per-pot lock: every call opens a short-lived
SQLite connection and closes it; SQLite handles concurrent readers natively, so
there's no shared mutable cache left to race on (the old OOM/duplication bugs
this file used to guard against don't exist in this design).
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

# Must match KA-Edge-source/python/board_db.py's DB location and
# KA-Edge-source/python/main.py's DATA_DIR/session-log/sd-status paths.
# App Lab project: /home/arduino/ArduinoApps/ka-edge-node/
_BOARD_DATA_DIR = "/home/arduino/ArduinoApps/ka-edge-node/python/data"

DEFAULT_DB_PATH = os.path.join(_BOARD_DATA_DIR, "sensor_data.db")
DEFAULT_SESSION_LOG = os.path.join(_BOARD_DATA_DIR, "session_log.jsonl")
# Written by main.py's handle_status() from sketch.ino's periodic SD_SPACE
# report — the physical SD card on the MCU side, not the QRB2210's own disk.
DEFAULT_SD_STATUS_PATH = os.path.join(_BOARD_DATA_DIR, "sd_status.json")
# Written by main.py's _maybe_report_disk_status() -- the QRB2210's own eMMC/disk
# free space (distinct from DEFAULT_SD_STATUS_PATH's MCU-side physical SD card).
# See docs/plan/board-disk-backup-routine.md's original low-disk-indicator ask.
DEFAULT_BOARD_DISK_STATUS_PATH = os.path.join(_BOARD_DATA_DIR, "board_disk_status.json")

# Dashboard shows a low-disk warning once free space drops under this floor --
# matches the 500MB manual-backup-cue threshold from board-disk-backup-routine.md.
LOW_DISK_FREE_MB_THRESHOLD = 500

# Fallback for rows written before light_lux existed in the payload.
PLACEHOLDER_LIGHT_LUX = 400.0

_POTS = ("A", "B")

# Time-range filter keys, mirrored from dashboard.js's _filterDataset so the
# server is the single source of truth once range-scoped fetches replace the
# old flat n=0 full-history fetch (2026-08-22 dashboard perf work).
RANGE_KEYS = ("5m", "15m", "1h", "today", "week", "overall")

# Range keys that need a "most recent timestamp" anchor computed from the data.
_CUTOFF_RANGES = ("5m", "15m", "1h", "today", "week")

# Must match dashboard.js's KA.CHART_MAX_POINTS -- the "overall" decimated view
# is what a page load asks for by default.
_DEFAULT_CHART_MAX_POINTS = 1500


def _cutoff_for_range(range_key: str, last_dt: datetime) -> datetime | None:
    """Cutoff timestamp for a range filter, anchored to the latest reading
    (not wall-clock now) -- same anchoring dashboard.js's _filterDataset used
    client-side. None means "no cutoff" (overall/unrecognized).

    "today" uses UTC midnight of last_dt's date, not the browser's local
    timezone dashboard.js used to compute it -- a known simplification since
    the server has no client timezone; readings are stored with explicit UTC
    offsets, so this is a stable, well-defined boundary, just not always
    identical to what "today" meant to a specific browser locally.
    """
    if range_key == "5m":
        return last_dt - timedelta(minutes=5)
    if range_key == "15m":
        return last_dt - timedelta(minutes=15)
    if range_key == "1h":
        return last_dt - timedelta(hours=1)
    if range_key == "today":
        return last_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_key == "week":
        return last_dt - timedelta(days=7)
    return None  # "overall" or unrecognized -- no cutoff


def _parse_iso(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _to_raw_row(row: sqlite3.Row) -> dict:
    return {
        "timestamp": row["received_at"],
        "temp_c": row["temp_c"],
        "humidity_pct": row["humidity_pct"],
        "pressure_hpa": row["pressure_hpa"],
        "moisture_pct": row["moisture_pct"],
        "gas_index": row["gas_kohm"],
        "light_lux": row["light_lux"],  # NOT NULL in the schema -- board_db.insert_reading
                                         # already applies PLACEHOLDER_LIGHT_LUX for a missing value.
    }


def _to_inference_row(row: sqlite3.Row) -> dict:
    return {
        "timestamp": row["timestamp"],
        "rsmi": row["rsmi"],
        "light_delta": row["light_delta"],
        "gas_index_trend": row["gas_index_trend"],
        "rsmi_trend": row["rsmi_trend"],
        "flag": row["flag"],
        "confidence": row["confidence"],
        "contributing_trend": row["contributing_trend"],
        "model_version": row["model_version"],
    }


class RealDataSource:
    """Serves real sensor data for both Pot A and Pot B from board_db's SQLite tables."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        session_log_path: str = DEFAULT_SESSION_LOG,
        sd_status_path: str = DEFAULT_SD_STATUS_PATH,
        board_disk_status_path: str = DEFAULT_BOARD_DISK_STATUS_PATH,
    ):
        self._db_path = db_path
        self._session_log = session_log_path
        self._sd_status_path = sd_status_path
        self._board_disk_status_path = board_disk_status_path

    def _connect(self) -> sqlite3.Connection | None:
        """None means "no DB yet" (e.g. main.py hasn't run on this board/test
        fixture yet) -- callers treat that the same as "empty", matching the
        old JSONL behavior of a missing file. Deliberately NOT sqlite3.connect
        unconditionally first, since that would silently create an empty,
        schema-less file when the real one is simply missing."""
        if not os.path.exists(self._db_path):
            return None
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _check_pot(pot: str) -> None:
        if pot not in _POTS:
            raise ValueError(f"RealDataSource: unknown pot {pot!r}")

    def _fetch(self, pot: str, table: str, ts_col: str, transform, n: int,
               range: str | None, max_points: int | None) -> list[dict]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            if n and n > 0:
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE pot_id = ? ORDER BY {ts_col} DESC LIMIT ?", (pot, n)
                ).fetchall()
                return [transform(r) for r in reversed(rows)]

            # n == 0: full-history mode, optionally range-filtered + decimated.
            cutoff_iso = None
            if range in _CUTOFF_RANGES:
                last_row = conn.execute(
                    f"SELECT {ts_col} FROM {table} WHERE pot_id = ? ORDER BY {ts_col} DESC LIMIT 1", (pot,)
                ).fetchone()
                last_dt = _parse_iso(last_row[0]) if last_row else None
                if last_dt is not None:
                    cutoff = _cutoff_for_range(range, last_dt)
                    cutoff_iso = cutoff.isoformat() if cutoff else None

            where, params = "pot_id = ?", [pot]
            if cutoff_iso is not None:
                where += f" AND {ts_col} >= ?"
                params.append(cutoff_iso)

            count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
            if cutoff_iso is not None:
                if count == 0:
                    return []
                if count < 2:
                    # Same "not enough points" fallback the old client-side
                    # _filterDataset used -- a too-narrow window shouldn't
                    # degrade to an unusably sparse/empty chart.
                    where, params = "pot_id = ?", [pot]
                    count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
            if count == 0:
                return []

            if not max_points or count <= max_points:
                rows = conn.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY {ts_col} ASC", params).fetchall()
                return [transform(r) for r in rows]

            stride = -(-count // max_points)  # ceil division
            query = (
                f"SELECT * FROM (SELECT *, ROW_NUMBER() OVER (ORDER BY {ts_col} ASC) AS rn "
                f"FROM {table} WHERE {where}) WHERE (rn - 1) % ? = 0 OR rn = ?"
            )
            rows = conn.execute(query, params + [stride, count]).fetchall()
            return [transform(r) for r in rows]
        finally:
            conn.close()

    def get_raw(self, pot: str, n: int, range: str | None = None, max_points: int | None = None) -> list[dict]:
        """Return the last n raw rows for pot. n=0 returns the full (retained) history,
        optionally scoped by range and decimated to max_points -- see module docstring."""
        self._check_pot(pot)
        return self._fetch(pot, "readings", "received_at", _to_raw_row, n, range, max_points)

    def get_inference(self, pot: str, n: int, range: str | None = None, max_points: int | None = None) -> list[dict]:
        """Same read pattern as get_raw, over inference_results instead of readings.
        This class computes nothing -- scripts/inference_service.py is solely
        responsible for keeping inference_results current; this just queries it."""
        self._check_pot(pot)
        return self._fetch(pot, "inference_results", "timestamp", _to_inference_row, n, range, max_points)

    def iter_raw_export_rows(self, pot: str):
        """Stream every retained raw row for pot, oldest first, one at a time --
        for CSV export. Only sees rows still in `readings` (see module docstring's
        note on the retention-window/rollup trade-off)."""
        self._check_pot(pot)
        conn = self._connect()
        if conn is None:
            return
        try:
            cursor = conn.execute("SELECT * FROM readings WHERE pot_id = ? ORDER BY received_at ASC", (pot,))
            for row in cursor:
                yield _to_raw_row(row)
        finally:
            conn.close()

    def get_status(self) -> dict:
        """Return session and collection metadata for the dashboard status bar.

        total_readings_*/collecting_since blend the live `readings` table with the
        permanent `raw_daily_rollup` table, so these stay correct even for rows
        already rolled up and deleted past the retention window.
        """
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
            "board_disk_used_mb": None,
            "board_disk_free_mb": None,
            "board_disk_total_mb": None,
            "board_disk_status_updated_at": None,
            "board_disk_low": False,
        }

        conn = self._connect()
        if conn is not None:
            try:
                for pot, key_total in (("A", "total_readings_a"), ("B", "total_readings_b")):
                    live_count = conn.execute(
                        "SELECT COUNT(*) FROM readings WHERE pot_id = ?", (pot,)).fetchone()[0]
                    rollup_count = conn.execute(
                        "SELECT COALESCE(SUM(count), 0) FROM raw_daily_rollup WHERE pot_id = ?", (pot,)
                    ).fetchone()[0]
                    status[key_total] = live_count + rollup_count

                    first_candidates = [
                        row[0] for row in conn.execute(
                            "SELECT MIN(received_at) FROM readings WHERE pot_id = ? "
                            "UNION ALL SELECT MIN(first_ts) FROM raw_daily_rollup WHERE pot_id = ?",
                            (pot, pot),
                        ).fetchall() if row[0] is not None
                    ]
                    if first_candidates:
                        first_ts = min(first_candidates)
                        if status["collecting_since"] is None or first_ts < status["collecting_since"]:
                            status["collecting_since"] = first_ts

                    last_candidates = [
                        row[0] for row in conn.execute(
                            "SELECT MAX(received_at) FROM readings WHERE pot_id = ? "
                            "UNION ALL SELECT MAX(last_ts) FROM raw_daily_rollup WHERE pot_id = ?",
                            (pot, pot),
                        ).fetchall() if row[0] is not None
                    ]
                    if last_candidates:
                        last_ts = max(last_candidates)
                        if status["last_reading_at"] is None or last_ts > status["last_reading_at"]:
                            status["last_reading_at"] = last_ts
            finally:
                conn.close()

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

        # Read latest QRB2210 disk-space report (the board's own eMMC/disk,
        # not the MCU's physical SD card above). Overwritten in place by
        # main.py, same "always just the single latest reading" shape as
        # sd_status.json; may be absent on older code or in the first
        # _DISK_STATUS_INTERVAL_S after boot.
        if os.path.exists(self._board_disk_status_path):
            try:
                with open(self._board_disk_status_path) as f:
                    disk_entry = json.load(f)
                status["board_disk_used_mb"] = disk_entry.get("board_disk_used_mb")
                status["board_disk_free_mb"] = disk_entry.get("board_disk_free_mb")
                status["board_disk_total_mb"] = disk_entry.get("board_disk_total_mb")
                status["board_disk_status_updated_at"] = disk_entry.get("updated_at")
                free_mb = status["board_disk_free_mb"]
                status["board_disk_low"] = free_mb is not None and free_mb < LOW_DISK_FREE_MB_THRESHOLD
            except (json.JSONDecodeError, OSError):
                pass

        return status
