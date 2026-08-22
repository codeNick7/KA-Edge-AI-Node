"""Real data source for both Pot A and Pot B.

Soil pin mapping corrected 2026-08-15 (the 2026-08-14 "confirmed" mapping
below was backwards from the very start of data collection — see
sketch.ino's header and scripts/migrate_soil_pin_swap.py for the
historical-data fix this forced):
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

Model (2026-08-16): inference now runs edge_node.trained_model.trained_predict(),
a decision tree fit on the real Pot A/B trial (scripts/build_training_set.py +
edge_node/train.py --data, PRD Section 5 steps 3-4) -- not the literature-seeded
placeholder (edge_node.decision_tree.classify(), still used by mock_source.py's
synthetic demo data and by train.py's synthetic timing dry-run). The validity
gate (edge_node.validity.check_validity) still runs first and independently --
"sensor_fault" is a data-quality signal the trained model was never asked to
learn (its training set only ever contains "stable"/"moisture_low" labels, so
the flags "drought_onset"/"waterlogged_risk"/"soil_health_degrading" the old
placeholder tree could emit are no longer reachable here at all).

Memory model (2026-08-22, OOM fix): earlier versions of this file kept the
*entire* history of both pots resident in memory forever (one Python dict per
row) -- fine at first, but with the board's readings files now tens of MB
and growing daily, this OOM-killed the dashboard process (confirmed via
dmesg: anon-rss ~1GB against the board's 1.7GB total). Now:
  - A small bounded per-pot "tail" cache (deque, maxlen=_TAIL_CACHE_SIZE)
    serves live-tick polling (N_LIVE) and the narrow time-range filters
    (5m/15m/1h), which never need more than recent history.
  - Wide-range/full-history requests (today/week/overall, and CSV export)
    stream directly from disk instead, bounded to ~max_points in memory
    regardless of how large the underlying file is.
See _TAIL_CACHE_SIZE, _stream_decimated, iter_raw_export_rows.
"""

import json
import os
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

from edge_node.features import compute_light_delta, compute_rsmi, compute_trend_slope
from edge_node.trained_model import MODEL_VERSION, trained_predict
from edge_node.validity import check_validity

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

# Bounded per-pot in-memory tail (records + inference results, separately).
# Comfortably covers N_LIVE (60) live-tick polling and the "5m"/"15m"/"1h"
# range filters at typical ~1Hz sampling, at negligible memory cost -- see
# module docstring's "Memory model" note. Wide ranges (today/week/overall)
# and CSV export stream from disk instead, never bounded by this constant.
_TAIL_CACHE_SIZE = 5000

# Time-range filter keys, mirrored from dashboard.js's _filterDataset so the
# server is the single source of truth once range-scoped fetches replace the
# old flat n=0 full-history fetch (2026-08-22 dashboard perf work).
RANGE_KEYS = ("5m", "15m", "1h", "today", "week", "overall")

# Range keys narrow enough to always be answerable from the bounded tail
# cache at typical sampling rates -- everything else streams from disk.
_TAIL_SERVABLE_RANGES = ("5m", "15m", "1h")

# Must match dashboard.js's KA.CHART_MAX_POINTS -- the "overall" decimated
# view (default filter, hit on every page load) is precomputed once at boot
# and cached in memory rather than re-scanning the readings/sidecar files on
# every request (2026-08-22: a single full parse pass over 500k+ lines is
# fast on a dev machine but genuinely slow on the board's ARM CPU -- ~35s
# observed for "overall" on real hardware even after the earlier streaming
# fix). Only requests using exactly this max_points (or none, treated as
# "use the default") benefit from the cache; anything else still streams
# fresh, so tests/callers passing a custom max_points are unaffected.
_DEFAULT_CHART_MAX_POINTS = 1500
# Recompute the cached "overall" view once this many new raw rows have
# arrived since it was last computed -- keeps it reasonably fresh without
# paying the full recompute cost on every single request.
_CHART_CACHE_REFRESH_EVERY = 1000


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


def _is_zero_bme(record: dict) -> bool:
    """Records where all BME fields are zero — offline sensor."""
    return (
        record.get("temp_c", 0.0) == 0.0
        and record.get("pressure_hpa", 0.0) == 0.0
        and record.get("gas_kohm", 0.0) == 0.0
    )


def _tail_lines(path: str, n: int) -> list[str]:
    """Last n non-empty lines of a text file, oldest-first, WITHOUT reading
    the whole file — reverse chunked read from the end. Used to seed the
    bounded in-memory tail cache at startup so a restart doesn't need to
    read from byte 0 of a large file (see RealDataSource.__init__)."""
    if n <= 0 or not os.path.exists(path):
        return []
    chunk_size = 65536
    # Accumulate chunks in a list (most-recent-first) and join ONCE at the
    # end -- rebuilding the whole buffer via `buffer = chunk + buffer` on
    # every iteration is O(n^2) in total bytes processed for a file needing
    # many chunks, and caused a real multi-GB memory/CPU blowup in practice
    # (2026-08-22: a 540k-line file with no pre-existing sidecar, so this
    # was asked for nearly the whole file in one call, spiked peak RSS to
    # ~7GB before this fix).
    chunks: list[bytes] = []
    lines_found = 0
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        while pos > 0 and lines_found <= n:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            chunks.append(chunk)
            lines_found += chunk.count(b"\n")
    buffer = b"".join(reversed(chunks))
    lines = buffer.split(b"\n")
    text_lines = [line.decode("utf-8", errors="replace") for line in lines if line.strip()]
    return text_lines[-n:]


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
        self._inference_cache_paths = {
            pot: os.path.join(os.path.dirname(path) or ".", f"pot_{pot.lower()}_inference_cache.jsonl")
            for pot, path in self._paths.items()
        }
        self._locks = {pot: threading.RLock() for pot in self._paths}

        # Per-pot bounded tail cache: byte offset already consumed (so a
        # restart doesn't re-read from byte 0 of a large file), plus a
        # deque(maxlen=_TAIL_CACHE_SIZE) of the most recent parsed raw
        # records — NOT the full history (see module docstring). Seeded at
        # startup from the file's tail so live-tick/narrow-range queries are
        # correct immediately, without paying for a full-file read.
        self._record_cache = {}
        for pot, path in self._paths.items():
            self._record_cache[pot] = {
                "mtime": None, "size": 0, "offset": 0,
                "records": deque(maxlen=_TAIL_CACHE_SIZE),
                "total_raw_seen": 0, "first_ts": None,
            }
            self._seed_record_cache(pot, path)

        # Per-pot inference bookkeeping: total_processed is how many valid
        # raw rows have been classified so far (an ever-increasing counter,
        # NOT a list length — see module docstring), seeded from the
        # persisted sidecar's line count. results is a bounded tail of
        # recent inference results, seeded from the sidecar's last lines —
        # same rationale as the record cache above. Full inference history
        # for wide-range requests comes from streaming the sidecar file
        # directly (_stream_decimated), not from this bounded cache.
        self._inference_cache = {}
        for pot in self._paths:
            self._inference_cache[pot] = {
                "total_processed": self._count_lines(self._inference_cache_paths[pot]),
                "results": deque(
                    self._parse_jsonl_tolerant(_tail_lines(self._inference_cache_paths[pot], _TAIL_CACHE_SIZE)),
                    maxlen=_TAIL_CACHE_SIZE,
                ),
            }

        # Eager catch-up, once, at boot: bring the persisted sidecar fully
        # up to date with the raw readings file *before* the app starts
        # serving requests, per the intended design ("first boot reads
        # everything and pre-computes what's needed; subsequent page loads
        # just reuse it and append") -- doing this lazily on whichever
        # request happened to need it first turned a routine page load into
        # an unpredictable multi-second/minute stall (2026-08-22 report).
        # This can take a while on a genuinely large first-ever backlog
        # (same one-time cost the original design always accepted), but it
        # happens once, here, not on a user-facing request.
        for pot in self._paths:
            with self._locks[pot]:
                self._catch_up_inference(pot)

        # Precompute the "overall" decimated chart view too (raw + inference)
        # -- same rationale as the inference catch-up above, and the same
        # "read everything once at boot, then just reuse + append" design.
        # See _DEFAULT_CHART_MAX_POINTS.
        self._chart_cache = {
            pot: {"raw": None, "raw_at": -1, "inference": None, "inference_at": -1}
            for pot in self._paths
        }
        for pot in self._paths:
            with self._locks[pot]:
                self._refresh_chart_cache(pot)

    @staticmethod
    def _count_lines(path: str) -> int:
        if not os.path.exists(path):
            return 0
        with open(path, "rb") as f:
            return sum(1 for line in f if line.strip())

    @staticmethod
    def _parse_jsonl_tolerant(lines):
        """Yield json.loads(line) for each line, silently skipping any that
        don't parse -- e.g. a torn line from a process killed mid-write.
        Every other JSONL read in this file already has this tolerance
        (see the try/except around each other json.loads call); this was
        the one gap, in the __init__ tail-seed of the results cache."""
        for line in lines:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

    def _seed_record_cache(self, pot: str, path: str) -> None:
        """Startup seed: count valid rows + capture the first-ever timestamp
        via one parse-and-discard pass (no retention, so this is fast and
        memory-flat even for a huge file), then tail-seed the bounded deque
        from the file's last lines. Sets offset to the file's current size
        so the next incremental read only picks up genuinely new bytes."""
        cache = self._record_cache[pot]
        if not os.path.exists(path):
            return

        count = 0
        first_ts = None
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if _is_zero_bme(record):
                    continue
                count += 1
                if first_ts is None:
                    first_ts = record.get("received_at")
        cache["total_raw_seen"] = count
        cache["first_ts"] = first_ts

        for line in _tail_lines(path, _TAIL_CACHE_SIZE):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _is_zero_bme(record):
                continue
            cache["records"].append(record)

        stat = os.stat(path)
        cache["mtime"] = stat.st_mtime
        cache["size"] = stat.st_size
        cache["offset"] = stat.st_size

    def _append_inference_cache(self, pot: str, new_results: list[dict]) -> None:
        if not new_results:
            return
        with open(self._inference_cache_paths[pot], "a") as f:
            for result in new_results:
                f.write(json.dumps(result) + "\n")

    def _load_records(self, pot: str) -> list[dict]:
        path = self._paths[pot]
        cache = self._record_cache[pot]

        with self._locks[pot]:
            return self._load_records_locked(pot, path, cache)

    def _load_records_locked(self, pot: str, path: str, cache: dict) -> list[dict]:
        if not os.path.exists(path):
            cache["mtime"] = None
            cache["size"] = 0
            cache["offset"] = 0
            cache["records"].clear()
            cache["total_raw_seen"] = 0
            cache["first_ts"] = None
            return []

        stat = os.stat(path)
        if cache["mtime"] == stat.st_mtime and cache["size"] == stat.st_size:
            return list(cache["records"])  # unchanged since last call — no disk I/O

        if stat.st_size < cache["offset"]:
            # File shrank (truncated/rotated) — invalidate and re-seed fully.
            cache["offset"] = 0
            cache["records"].clear()
            cache["total_raw_seen"] = 0
            cache["first_ts"] = None
            self._seed_record_cache(pot, path)
            return list(cache["records"])

        new_records = []
        with open(path, "rb") as f:
            f.seek(cache["offset"])
            chunk = f.read()

        # Only consume complete (newline-terminated) lines. A trailing partial
        # line (mid-append by the relay process) is left unconsumed so it's
        # re-read in full next call, rather than being permanently skipped.
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
            if _is_zero_bme(record):
                continue
            new_records.append(record)

        if new_records:
            prev_last = cache["records"][-1].get("received_at", "") if cache["records"] else ""
            already_ordered = new_records[0].get("received_at", "") >= prev_last and all(
                new_records[i].get("received_at", "") <= new_records[i + 1].get("received_at", "")
                for i in range(len(new_records) - 1)
            )
            if already_ordered:
                cache["records"].extend(new_records)  # bounded deque auto-trims oldest
            else:
                # Rare (Bridge relay reordering) — full resort as a fallback,
                # same guarantee the old always-resort behavior gave. deque
                # has no in-place sort, so rebuild it.
                merged = sorted(list(cache["records"]) + new_records, key=lambda r: r.get("received_at", ""))
                cache["records"] = deque(merged[-_TAIL_CACHE_SIZE:], maxlen=_TAIL_CACHE_SIZE)
            cache["total_raw_seen"] += len(new_records)
            if cache["first_ts"] is None:
                cache["first_ts"] = new_records[0].get("received_at")

        cache["mtime"] = stat.st_mtime
        cache["size"] = stat.st_size
        return list(cache["records"])

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

    @staticmethod
    def _filter_and_decimate_tail(rows: list[dict], range_key: str | None, max_points: int | None) -> list[dict]:
        """Apply a range cutoff + stride decimation to an already-in-memory
        list of {"timestamp": ...} rows (the bounded tail cache) -- shared by
        get_raw/get_inference's tail-servable branch (5m/15m/1h)."""
        if not rows:
            return rows
        last_dt = _parse_iso(rows[-1]["timestamp"])
        cutoff = _cutoff_for_range(range_key, last_dt) if last_dt and range_key else None
        if cutoff is not None:
            filtered = [r for r in rows if (dt := _parse_iso(r["timestamp"])) and dt >= cutoff]
            # Same "not enough points" fallback the old client-side
            # _filterDataset used -- a too-narrow window shouldn't degrade to
            # an unusably sparse/empty chart.
            if len(filtered) >= 2 or len(rows) < 2:
                rows = filtered
        if not max_points or len(rows) <= max_points:
            return rows
        stride = -(-len(rows) // max_points)  # ceil division
        decimated = rows[::stride]
        if decimated[-1] is not rows[-1]:
            decimated.append(rows[-1])
        return decimated

    def _stream_decimated(self, path: str, range_key: str | None, max_points: int | None, is_raw: bool) -> list[dict]:
        """Two/three-pass streaming read of a JSONL file: never retains more
        than one record at a time except for the small final result list
        (bounded to ~max_points) — see module docstring's "Memory model".
        Used for wide-range/full-history requests (today/week/overall, and
        no-range "give me everything decimated" requests), where the bounded
        in-memory tail cache may not reach far back enough.

        is_raw selects the BME-zero skip filter + _to_raw_row transform
        (readings file) vs. pass-through (already-clean inference sidecar).
        """
        if not os.path.exists(path):
            return []

        def _iter_valid():
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if is_raw and _is_zero_bme(record):
                        continue
                    yield record

        # Only "today"/"week" ever produce an actual cutoff (see
        # _cutoff_for_range) -- "overall"/no-range never do, so skip the
        # "find the last record" pass entirely for those instead of paying
        # for a full parse just to learn we don't need it. "overall" is the
        # default filter (every page load), so this matters a lot.
        cutoff = None
        if range_key in ("today", "week"):
            last_record = None
            for record in _iter_valid():
                last_record = record
            if last_record is not None:
                last_dt = _parse_iso(last_record.get("received_at" if is_raw else "timestamp", ""))
                if last_dt is not None:
                    cutoff = _cutoff_for_range(range_key, last_dt)

        def _in_range(record: dict) -> bool:
            if cutoff is None:
                return True
            dt = _parse_iso(record.get("received_at" if is_raw else "timestamp", ""))
            return dt is not None and dt >= cutoff

        if cutoff is None:
            # No filtering to apply -- a plain newline count is enough to
            # size the decimation stride (approximate for is_raw, since it
            # doesn't apply the BME-zero skip filter, but stride only needs
            # to be roughly right, not exact) instead of a full JSON-parse
            # counting pass.
            count = self._count_lines(path)
        else:
            count = sum(1 for record in _iter_valid() if _in_range(record))
            if count == 0:
                return []
            if count < 2:
                # Same "not enough points" fallback the old client-side
                # _filterDataset used -- a too-narrow window shouldn't
                # degrade to an unusably sparse/empty chart.
                cutoff = None
                count = self._count_lines(path)
        if count == 0:
            return []

        stride = -(-count // max_points) if max_points and count > max_points else 1
        transform = self._to_raw_row if is_raw else (lambda r: r)
        kept = []
        last_kept_row = None
        idx = 0
        for record in _iter_valid():
            if not _in_range(record):
                continue
            row = transform(record)
            last_kept_row = row
            # count may be an approximate (unfiltered-line) upper bound when
            # cutoff is None, so "idx == count - 1" can't reliably detect
            # the true last matching row here -- last_kept_row (updated
            # every iteration) always ends up holding it instead, appended
            # below regardless of whether the modulo already kept it.
            if idx % stride == 0:
                kept.append(row)
            idx += 1
        if last_kept_row is not None and (not kept or kept[-1] is not last_kept_row):
            kept.append(last_kept_row)
        # kept is small (bounded by max_points/stride) regardless of file
        # size, so a final sort here is cheap -- guards against the rare
        # out-of-order relay append the in-memory cache's own resort logic
        # (_load_records_locked) handles for the bounded-tail path; a
        # straight streaming read has no equivalent chance to notice one.
        # kept entries are already post-transform, so both raw and
        # inference rows use the "timestamp" key here.
        kept.sort(key=lambda r: r.get("timestamp", ""))
        return kept

    def iter_raw_export_rows(self, pot: str):
        """Stream every raw row for pot, oldest first, one at a time --
        for CSV export, which needs the exact/lossless full history but
        must not hold it all in memory at once (see module docstring)."""
        if pot not in self._paths:
            raise ValueError(f"RealDataSource: unknown pot {pot!r}")
        path = self._paths[pot]
        if not os.path.exists(path):
            return
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if _is_zero_bme(record):
                    continue
                yield self._to_raw_row(record)

    def _refresh_chart_cache(self, pot: str) -> None:
        """(Re)compute the cached "overall" decimated raw + inference views
        for pot via one streaming pass each, and record the raw-row/result
        counts they were computed at (used to decide when a cache is stale
        enough to be worth refreshing -- see _CHART_CACHE_REFRESH_EVERY)."""
        cache = self._chart_cache[pot]
        cache["raw"] = self._stream_decimated(self._paths[pot], None, _DEFAULT_CHART_MAX_POINTS, is_raw=True)
        cache["raw_at"] = self._record_cache[pot]["total_raw_seen"]
        cache["inference"] = self._stream_decimated(
            self._inference_cache_paths[pot], None, _DEFAULT_CHART_MAX_POINTS, is_raw=False
        )
        cache["inference_at"] = self._inference_cache[pot]["total_processed"]

    def _cached_overall_raw(self, pot: str) -> list[dict]:
        cache = self._chart_cache[pot]
        total_raw_seen = self._record_cache[pot]["total_raw_seen"]
        if cache["raw"] is None or total_raw_seen - cache["raw_at"] >= _CHART_CACHE_REFRESH_EVERY:
            cache["raw"] = self._stream_decimated(self._paths[pot], None, _DEFAULT_CHART_MAX_POINTS, is_raw=True)
            cache["raw_at"] = total_raw_seen
        return cache["raw"]

    def _cached_overall_inference(self, pot: str) -> list[dict]:
        cache = self._chart_cache[pot]
        total_processed = self._inference_cache[pot]["total_processed"]
        if cache["inference"] is None or total_processed - cache["inference_at"] >= _CHART_CACHE_REFRESH_EVERY:
            cache["inference"] = self._stream_decimated(
                self._inference_cache_paths[pot], None, _DEFAULT_CHART_MAX_POINTS, is_raw=False
            )
            cache["inference_at"] = total_processed
        return cache["inference"]

    def get_raw(self, pot: str, n: int, range: str | None = None, max_points: int | None = None) -> list[dict]:
        """Return the last n raw rows for pot. n=0 returns the full history.

        range/max_points (2026-08-22 dashboard perf work) only apply when
        n=0 -- they scope+decimate a full-history request down to what a
        chart actually needs to render. Callers that want the exact,
        lossless history (CSV export) use iter_raw_export_rows instead.
        """
        if pot not in self._paths:
            raise ValueError(f"RealDataSource: unknown pot {pot!r}")
        with self._locks[pot]:
            if n and n > 0:
                records = self._load_records(pot)[-n:]
                return [self._to_raw_row(r) for r in records]

            if range in _TAIL_SERVABLE_RANGES:
                rows = [self._to_raw_row(r) for r in self._load_records(pot)]
                return self._filter_and_decimate_tail(rows, range, max_points)

            if range in (None, "overall") and max_points == _DEFAULT_CHART_MAX_POINTS:
                return self._cached_overall_raw(pot)

            return self._stream_decimated(self._paths[pot], range, max_points, is_raw=True)

    def get_inference(self, pot: str, n: int, range: str | None = None, max_points: int | None = None) -> list[dict]:
        if pot not in self._paths:
            raise ValueError(f"RealDataSource: unknown pot {pot!r}")
        with self._locks[pot]:
            tail = self._get_inference_locked(pot)  # always runs catch-up as a side effect

            if n and n > 0:
                return tail[-n:]

            if range in _TAIL_SERVABLE_RANGES:
                return self._filter_and_decimate_tail(tail, range, max_points)

            if range in (None, "overall") and max_points == _DEFAULT_CHART_MAX_POINTS:
                return self._cached_overall_inference(pot)

            return self._stream_decimated(self._inference_cache_paths[pot], range, max_points, is_raw=False)

    @staticmethod
    def _compute_inference_row(row: dict, history: list[dict], prev_moisture: float | None) -> dict:
        """One row's inference result, given its TREND_WINDOW-sized trailing
        history (row included, oldest first). Pure function so it's usable
        both by the bounded-tail loop and the streaming catch-up path below
        without duplicating the validity/rsmi/trend/model logic."""
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

        # Validity gate runs before the trained model, same as the old
        # placeholder tree — sensor_fault is a data-quality signal, not
        # something the model was ever trained to recognize (the real
        # trial's training_set.jsonl only ever contains "stable" and
        # "moisture_low", see scripts/build_training_set.py).
        if validity.is_valid:
            flag_label, flag_confidence = trained_predict(rsmi, light_delta, gas_index_trend, rsmi_trend)
        else:
            flag_label, flag_confidence = "sensor_fault", validity.validity_score

        # trained_predict() only returns (label, confidence) -- the fitted
        # tree has no notion of "contributing_trend" the way the old
        # rule-based leaves did, so this is derived directly from
        # rsmi_trend's sign for display purposes only.
        if rsmi_trend > 1e-4:
            contributing_trend = "moisture_up"
        elif rsmi_trend < -1e-4:
            contributing_trend = "moisture_down"
        else:
            contributing_trend = "moisture_flat"

        return {
            "timestamp": row["timestamp"],
            "rsmi": round(rsmi, 4),
            "light_delta": round(light_delta, 2),
            "gas_index_trend": round(gas_index_trend, 4),
            "rsmi_trend": round(rsmi_trend, 4),
            "flag": flag_label,
            "confidence": round(flag_confidence, 3),
            "contributing_trend": contributing_trend,
            "model_version": MODEL_VERSION,
        }

    def _catchup_inference_streaming(self, pot: str, already_processed_raw_rows: int, backlog: int) -> tuple[int, list[dict]]:
        """Compute inference for a backlog larger than the bounded raw tail
        can supply, WITHOUT reading the whole raw file from byte 0 -- tail-
        reads just enough lines from the END of the file to cover the
        backlog plus TREND_WINDOW-1 rows of leading context (reusing
        _tail_lines, the same utility __init__ uses to seed the bounded
        caches at startup), so cost is O(backlog), not O(total file size).
        Only falls back to a full from-the-start scan in the pathological
        case where the tail-read doesn't turn out to contain enough VALID
        (non-zero-BME) rows even after a generous over-fetch.

        Persists each new result to the sidecar as computed. Returns (total
        new results, bounded tail of the newest ones, for seeding the
        in-memory results cache)."""
        path = self._paths[pot]
        needed = backlog + TREND_WINDOW - 1

        # A tail-read materializes `needed` rows as parsed dicts at once --
        # fine for a modest gap (the intended case: a few thousand rows
        # since the last run), but for a backlog this large (e.g. no
        # pre-existing sidecar at all -- needed ~= the entire file) that's
        # exactly the unbounded-memory pattern this whole rewrite exists to
        # avoid (confirmed: an un-guarded tail-read of a 540k-row file with
        # no sidecar peaked at ~700MB). Go straight to the genuinely O(1)
        # rolling-window full scan instead once the backlog gets this big.
        if needed > _TAIL_CACHE_SIZE * 4:
            return self._catchup_inference_streaming_full_scan(pot, already_processed_raw_rows)

        valid_rows = None
        for attempt_lines in (needed + 1000, needed + 20000):
            raw_lines = _tail_lines(path, attempt_lines)
            candidate = []
            for line in raw_lines:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if _is_zero_bme(record):
                    continue
                candidate.append(self._to_raw_row(record))
            if len(candidate) >= needed or len(raw_lines) < attempt_lines:
                # Either enough valid rows, or we've already hit the start
                # of the file (fewer lines available than requested) --
                # either way, a bigger tail-read wouldn't help further.
                valid_rows = candidate
                break
        if valid_rows is None or len(valid_rows) < needed:
            # Pathological: even a large tail-read didn't cover the backlog
            # (e.g. very dense zero-BME runs). Correctness over speed here.
            return self._catchup_inference_streaming_full_scan(pot, already_processed_raw_rows)

        # valid_rows may have more than `needed` rows (over-fetch); take
        # exactly the trailing `needed`, oldest-first: the first
        # TREND_WINDOW-1 of those are context only (already processed in an
        # earlier run), the rest are the actual backlog to compute+persist.
        trailing = valid_rows[-needed:]
        context = trailing[: TREND_WINDOW - 1]
        new_rows = trailing[TREND_WINDOW - 1:]
        window: deque[dict] = deque(context, maxlen=TREND_WINDOW)

        new_count = 0
        tail_buffer: deque[dict] = deque(maxlen=_TAIL_CACHE_SIZE)
        with open(self._inference_cache_paths[pot], "a") as sidecar:
            for row in new_rows:
                window.append(row)
                prev_moisture = window[-2]["moisture_pct"] if len(window) > 1 else None
                result = self._compute_inference_row(row, list(window), prev_moisture)
                sidecar.write(json.dumps(result) + "\n")
                tail_buffer.append(result)
                new_count += 1

        return new_count, list(tail_buffer)

    def _catchup_inference_streaming_full_scan(self, pot: str, already_processed_raw_rows: int) -> tuple[int, list[dict]]:
        """Fallback for _catchup_inference_streaming's pathological case:
        compute sequentially over the ENTIRE raw readings file from byte 0,
        maintaining only a TREND_WINDOW-sized rolling window (same pattern
        KA-Edge-source/python/main.py's board-side _pot_history deque uses).
        O(TREND_WINDOW + _TAIL_CACHE_SIZE) memory, but O(total file size)
        time -- only reached when a tail-read genuinely can't supply enough
        valid rows."""
        path = self._paths[pot]
        window: deque[dict] = deque(maxlen=TREND_WINDOW)
        tail_buffer: deque[dict] = deque(maxlen=_TAIL_CACHE_SIZE)
        valid_idx = -1
        new_count = 0
        start_at = max(already_processed_raw_rows, TREND_WINDOW - 1)

        with open(path) as f, open(self._inference_cache_paths[pot], "a") as sidecar:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if _is_zero_bme(record):
                    continue
                valid_idx += 1
                row = self._to_raw_row(record)
                window.append(row)
                if valid_idx < start_at:
                    continue
                prev_moisture = window[-2]["moisture_pct"] if len(window) > 1 else None
                result = self._compute_inference_row(row, list(window), prev_moisture)
                sidecar.write(json.dumps(result) + "\n")
                tail_buffer.append(result)
                new_count += 1

        return new_count, list(tail_buffer)

    def _catch_up_inference(self, pot: str) -> None:
        """Bring the persisted sidecar + in-memory results cache fully up
        to date with whatever's currently in the raw readings file.

        Called eagerly for every pot at __init__ (process boot) time, per
        the intended design: pre-compute everything once at startup so
        subsequent page loads just reuse what's already there and append
        newly-arrived readings — not lazily on whichever request happens to
        first need it (that turned a routine page load into an unpredictable
        multi-second/minute stall, see 2026-08-22 slow-inference-page-load
        report). Also called per-request from get_inference to pick up the
        small deltas that arrive between polls; after the eager startup
        catch-up, that's normally 0-a few rows and stays cheap.
        """
        record_cache = self._record_cache[pot]
        self._load_records(pot)  # refreshes record_cache as a side effect
        total_raw_seen = record_cache["total_raw_seen"]

        inf_cache = self._inference_cache[pot]
        # total_processed counts RESULTS (1 per sidecar line), which is
        # offset from raw-row count by TREND_WINDOW - 1 (results only start
        # once raw reaches TREND_WINDOW - 1 rows). Convert to "how many raw
        # rows have already been accounted for" so it's comparable to
        # total_raw_seen (a raw-row count).
        already_processed_raw_rows = (
            inf_cache["total_processed"] + TREND_WINDOW - 1 if inf_cache["total_processed"] > 0 else 0
        )
        if already_processed_raw_rows > total_raw_seen:
            # Raw history shrank (file truncated/rotated) — recompute clean,
            # and drop the now-stale persisted cache so it doesn't get
            # duplicated ahead of the freshly recomputed rows on next append.
            inf_cache["total_processed"] = 0
            inf_cache["results"].clear()
            already_processed_raw_rows = 0
            cache_path = self._inference_cache_paths[pot]
            if os.path.exists(cache_path):
                os.remove(cache_path)

        backlog = max(total_raw_seen - already_processed_raw_rows, 0)
        if backlog == 0:
            return
        raw_tail = [self._to_raw_row(r) for r in record_cache["records"]]

        if backlog > len(raw_tail):
            # Bounded tail can't reach back far enough -- tail-read from disk.
            new_count, tail_of_new = self._catchup_inference_streaming(pot, already_processed_raw_rows, backlog)
            inf_cache["results"].extend(tail_of_new)  # bounded deque auto-trims oldest
            inf_cache["total_processed"] += new_count
        else:
            start = max(len(raw_tail) - backlog, TREND_WINDOW - 1)
            new_results = []
            for i in range(start, len(raw_tail)):
                history = raw_tail[i - TREND_WINDOW + 1 : i + 1]
                row = raw_tail[i]
                prev_moisture = raw_tail[i - 1]["moisture_pct"] if i > 0 else None
                new_results.append(self._compute_inference_row(row, history, prev_moisture))
            inf_cache["results"].extend(new_results)  # bounded deque auto-trims oldest
            inf_cache["total_processed"] += len(new_results)
            self._append_inference_cache(pot, new_results)

    def _get_inference_locked(self, pot: str) -> list[dict]:
        """Pick up any raw rows newer than what's already processed (in
        steady state, after the __init__ eager catch-up, this is normally a
        handful of rows or none) and return the bounded in-memory results
        tail (for n>0 / narrow-range callers)."""
        self._catch_up_inference(pot)
        return list(self._inference_cache[pot]["results"])

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
        for pot, key_total in [("A", "total_readings_a"), ("B", "total_readings_b")]:
            records = self._load_records(pot)
            cache = self._record_cache[pot]
            status[key_total] = cache["total_raw_seen"]
            if records:
                first_ts = cache["first_ts"]
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
