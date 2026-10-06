# v3.7 (2026-10-06) — Pot B evidence carries Pot A's same-sample gas reading as
# raw.ref_gas_index/raw.ref_pot_id (reference for common-mode BME688 drift);
# Pot A is still not pushed as its own evidence stream.
#
# v3.6 (2026-10-06) — Per-destination pot scope: the "cloud" destination now
# receives Pot B only (KARBON_ASSURE_CLOUD_POTS, falls back to B when unset).
# Pot A is still stored, inferred and shown locally; it just no longer leaves
# the board. See docs/plan/cloud-push-pot-b-only-2026-10-06.md.
#
# v3.5 (2026-09-22) — Unbuffered stdout streaming with line_buffering=True;
# immediate console raw sensor streaming output in handle_reading and handle_log;
# registered both write_log and diag channels; robust exception safety so DB/cloud
# errors never suppress console output.
#
# v3.4 — Immediate console raw sensor streaming output in handle_reading and handle_log.
#
# v3.3 — [Reading #] console line now also prints light_lux, so real BH1750
# values are visible without checking the dashboard.
#
# Changes vs v3.2: push evidence/raw readings to multiple Karbon Assure
# destinations simultaneously (local LAN demo backend + live
# demo.karbon-assure.tech), each with its own device/secret and its own
# independent sync queue.
#
# Changes vs v3.1:
#   - push_client.load_config() -> load_configs(): reads KARBON_ASSURE_LOCAL_*
#     and KARBON_ASSURE_CLOUD_* (see push_to_karbon_assure.py's docstring)
#     instead of a single unprefixed KARBON_ASSURE_* set. A destination with
#     no vars set is skipped, not an error -- "cloud" can be added later via
#     .env alone.
#   - Each destination pushes to its own sync_queue*.db (push_client.db_path_for)
#     so a "cloud" outage doesn't block or get blocked by "local" sync state.
#
# Changes vs v3.0:
#   - Added python-dotenv dependency (requirements.txt); load_dotenv() runs
#     first thing, before push_to_karbon_assure.load_configs() reads os.environ.
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
#  12  moistureA_pct   (Pot A → A1 as of sketch.ino v7.1 / 2026-08-15 fix —
#                        NOT yet deployed as of this comment; readings until
#                        the firmware is redeployed still carry the old,
#                        backwards A0 mapping. See scripts/migrate_soil_pin_swap.py)
#  13  moistureB_pct   (Pot B → A0, same caveat as above)
#
# Soil pin mapping corrected 2026-08-15 (undeployed — pins/wiring
# intentionally left as-is for now, only historical data was migrated):
#   Pot A: BME688 @0x76, soil A1, Wire1 (Qwiic)
#   Pot B: BME688 @0x77, soil A0, Wire1 (Qwiic)
import json
import os
import shutil
import socket
import sqlite3
import struct
import sys
import time
from collections import deque
from datetime import datetime, timezone

# Ensure standard output and error flush immediately without block buffering in App Lab
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)

from dotenv import load_dotenv

PYTHON_VERSION = "v3.7 (2026-10-06)"

# Load KARBON_ASSURE_* config from a local .env (gitignored, never committed —
# see push_to_karbon_assure.py's load_config()) before anything reads os.environ.
# Explicit path, not the default CWD-search, since App Lab may not launch
# main.py with this directory as the working directory.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from arduino.app_utils import App, Bridge

import board_db
import push_to_karbon_assure as push_client
from edge_node.features import compute_light_delta, compute_rsmi, compute_trend_slope
from edge_node.trained_model import MODEL_VERSION, trained_predict
from edge_node.validity import check_validity

bridge = Bridge()

DATA_DIR        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SESSION_LOG     = os.path.join(DATA_DIR, "session_log.jsonl")
# Latest-value only (overwritten each update, not appended) — unlike the
# readings files this has no history value, and appending would just be
# another ever-growing file for no benefit.
SD_STATUS_PATH  = os.path.join(DATA_DIR, "sd_status.json")
# Same "overwritten, not appended" shape as SD_STATUS_PATH, but for the QRB2210's
# own eMMC/disk (docs/plan/board-disk-backup-routine.md's original ask -- distinct
# from SD_STATUS_PATH, which reports the MCU side's physical SD card).
BOARD_DISK_STATUS_PATH = os.path.join(DATA_DIR, "board_disk_status.json")

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


# ── Karbon Assure cloud push (docs/plan/karbon-assure-ingestion-integration.md) ─
# Config comes from env vars (KARBON_ASSURE_LOCAL_*/KARBON_ASSURE_CLOUD_*,
# see push_to_karbon_assure.py's docstring) set on the board, not hardcoded.
# Pushes go to every configured destination simultaneously (local LAN demo
# backend + live demo.karbon-assure.tech), each independently. Missing
# config for a destination just skips it; missing config for ALL
# destinations disables push entirely rather than crashing the read loop --
# the board must keep working fully offline (local JSONL logging is the
# durable source of truth regardless of cloud-sync state).
#
# "local" disabled here in code (2026-09-11): the local LAN demo backend
# hasn't been running, so every push to it failed and retried forever --
# sync_queue_local.db grew to 296MB (of 296k queued evidence rows, only 8.6%
# ever synced) and filled the board's disk (see docs/plan/nn-inference-
# upgrade-plan.md). Relying on unsetting KARBON_ASSURE_LOCAL_* in .env alone
# wasn't reliable (a stray/duplicate main.py process kept running with the
# old env and recreated the queue file after deletion) -- pinning the
# destination list here is deterministic regardless of .env state or which
# process instance is running. Re-add "local" to the tuple below (or pass
# push_client.DEFAULT_DESTINATIONS) once the local demo backend is actually
# back in use.
#
# Pot scope per destination (2026-10-06): the demo cloud project represents
# Pot B only (the degrading pot), but both pots were being pushed. _push_pots
# maps each destination to the pots it receives (None = all) -- see
# push_client.allowed_pots(): KARBON_ASSURE_<NAME>_POTS, "cloud" falls back to
# B when unset. A malformed value disables push entirely (same as any other
# ConfigError here) rather than risk widening the scope.
try:
    # _push_configs = push_client.load_configs()  # commented out 2026-09-11, see note above
    _push_configs = push_client.load_configs(names=("cloud",))
    _push_pots = {name: push_client.allowed_pots(name) for name in _push_configs}
except push_client.ConfigError as e:
    print(f"[Push] WARN: {e} — evidence/raw push disabled, logging locally only.", flush=True)
    _push_configs = {}
    _push_pots = {}

if _push_configs:
    print(f"[Push] Karbon Assure destinations enabled: {', '.join(_push_configs)}.", flush=True)
    for _name, _pots in _push_pots.items():
        print(f"[Push]   {_name}: pots {'all' if _pots is None else ','.join(sorted(_pots))}", flush=True)
else:
    print("[Push] No Karbon Assure destinations configured — logging locally only.", flush=True)

TEXTURE = "clay_loam"  # must match source/dashboard/real_source.py's TEXTURE
TREND_WINDOW = 7  # must match source/dashboard/real_source.py's TREND_WINDOW

# Per-pot rolling history for feature/trend computation -- same window the
# dashboard's RealDataSource._get_inference_locked uses, kept in memory here
# so evidence can be computed and pushed per-reading, live, instead of only
# ever being computed by a separate downstream dashboard process.
_pot_history = {"A": deque(maxlen=TREND_WINDOW), "B": deque(maxlen=TREND_WINDOW)}

# Per-pot raw-reading accumulation for the batch/raw path -- flushed once it
# reaches MAX_RAW_BATCH_SIZE, matching the "chunked catch-up syncs" hardening
# requirement (plan §5.3 point 4) rather than one push per reading.
_raw_batch_buffer = {"A": [], "B": []}


def _contributing_trend(rsmi_trend: float) -> str:
    if rsmi_trend > 1e-4:
        return "moisture_up"
    if rsmi_trend < -1e-4:
        return "moisture_down"
    return "moisture_flat"


def _pot_allowed(name: str, pot: str) -> bool:
    """Whether destination `name` receives `pot`'s data (see _push_pots)."""
    pots = _push_pots.get(name)
    return pots is None or pot in pots


def _push_evidence_if_ready(pot: str, rec: dict, sample_index: int,
                            ref_gas: float | None = None, ref_pot: str | None = None):
    """Append rec to pot's rolling history; once there's a full trend
    window, compute rsmi/flag/confidence exactly like the dashboard's
    RealDataSource does (same edge_node functions) and push it to every
    configured destination whose pot scope includes this pot.

    ref_gas/ref_pot (2026-10-06): the reference pot's gas reading from the
    same sample, carried as raw.ref_gas_index/raw.ref_pot_id. Both pots' BME688
    gas drifts together (sensor ageing, ambient VOCs), so the soil signal is
    the gap between them; the cloud only receives Pot B, so the reference
    rides along inside Pot B's record instead of as its own evidence stream.
    See distributed-app docs/plan/edge-node-gas-reference-chart.md."""
    if not _push_configs:
        return

    history = _pot_history[pot]
    history.append(rec)  # always, so the window stays continuous if scope changes
    if len(history) < TREND_WINDOW:
        return  # not enough history yet for a trend-window computation
    if not any(_pot_allowed(name, pot) for name in _push_configs):
        return  # no destination receives this pot -- skip the inference work

    hist_list = list(history)
    rsmi = compute_rsmi(rec["moisture_pct"], TEXTURE)
    rsmi_history = [compute_rsmi(r["moisture_pct"], TEXTURE) for r in hist_list]
    light_history = [r["light_lux"] for r in hist_list]
    gas_history = [r["gas_kohm"] for r in hist_list]

    light_delta = compute_light_delta(light_history)
    gas_index_trend = compute_trend_slope(gas_history)
    rsmi_trend = compute_trend_slope(rsmi_history)
    prev_moisture = hist_list[-2]["moisture_pct"] if len(hist_list) > 1 else None

    validity = check_validity(rec["moisture_pct"], rec["gas_kohm"], rec["temp_c"], prev_moisture)
    if validity.is_valid:
        flag, confidence = trained_predict(rsmi, light_delta, gas_index_trend, rsmi_trend)
    else:
        flag, confidence = "sensor_fault", validity.validity_score

    base_record = {
        "pot_id": pot,
        "sample_index": sample_index,
        "timestamp": rec["received_at"],
        "time_uncertain": not _ntp_info["ntp_synced"],
        "flag": flag,
        "confidence": round(confidence, 3),
        "contributing_trend": _contributing_trend(rsmi_trend),
        "model_version": MODEL_VERSION,
        "schema_version": "1",
        "raw": {
            "rsmi": round(rsmi, 4),
            "gas_index": rec["gas_kohm"],
            "temp_c": rec["temp_c"],
            "light_lux": rec["light_lux"],
        },
    }
    if ref_gas is not None:
        base_record["raw"]["ref_gas_index"] = ref_gas
        base_record["raw"]["ref_pot_id"] = ref_pot
    # node_id/project_id are per-destination -- each backend has its own
    # device registration and its own base_projects.id UUID for this project.
    for name, config in _push_configs.items():
        if not _pot_allowed(name, pot):
            continue
        record = {"node_id": config["node_id"], "project_id": config["project_id"], **base_record}
        print(f"[DEBUG] evidence payload: {record}", flush=True)
        push_client.push_evidence(record, config, db_path=push_client.db_path_for(name))


_DISK_STATUS_INTERVAL_S = 60  # disk usage doesn't move fast enough to need every reading cycle
_last_disk_status_at = 0.0


def _maybe_report_disk_status():
    """Overwrite BOARD_DISK_STATUS_PATH with the QRB2210's own live free/used/
    total disk space, at most once every _DISK_STATUS_INTERVAL_S -- the
    dashboard reads this (real_source.py's get_status()) to show a low-disk
    warning, the manual cue to run a backup/rotation pass (board-disk-backup-
    routine.md's original ask)."""
    global _last_disk_status_at
    now = time.time()
    if now - _last_disk_status_at < _DISK_STATUS_INTERVAL_S:
        return
    _last_disk_status_at = now
    try:
        usage = shutil.disk_usage(DATA_DIR)
    except OSError as e:
        print(f"[Disk] WARN: could not read board disk usage: {e}", flush=True)
        return
    entry = {
        "board_disk_used_mb": round(usage.used / (1024 * 1024), 1),
        "board_disk_free_mb": round(usage.free / (1024 * 1024), 1),
        "board_disk_total_mb": round(usage.total / (1024 * 1024), 1),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp_path = BOARD_DISK_STATUS_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(entry, f)
    os.replace(tmp_path, BOARD_DISK_STATUS_PATH)


_last_prune_date = None  # UTC date string ("YYYY-MM-DD") prune_old() last ran for


def _maybe_prune_old():
    """Run board_db.prune_old() at most once per UTC calendar day -- it's a
    rollup+delete+vacuum pass, not something to run on every reading (see its
    own docstring). A plain date-string comparison, checked on the already-
    per-reading handle_reading() call, is enough: worst case it runs once extra
    on a restart that lands on a new date, which is harmless (idempotent)."""
    global _last_prune_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today == _last_prune_date:
        return
    _last_prune_date = today
    with board_db.connect() as conn:
        result = board_db.prune_old(conn)
    if result["deleted_readings"] or result["deleted_inference"]:
        print(f"[Prune] rolled up + removed rows older than {result['cutoff_date']}: "
              f"{result['deleted_readings']} readings, {result['deleted_inference']} inference results",
              flush=True)
    if result["protected_dates"]:
        print(f"[Prune] WARNING: kept {len(result['protected_dates'])} pot/day pair(s) past retention "
              f"because inference hasn't caught up yet -- is scripts/inference_service.py --watch running? "
              f"{result['protected_dates']}", flush=True)


def _flush_pending():
    """Retry whatever's due for retry in each destination's sync queue --
    push_to_karbon_assure.flush_pending()'s own docstring calls for this to
    run "once per reading cycle" so records queued during an outage sync
    once connectivity returns, but nothing was actually calling it (bug
    found 2026-09-16: every queued row had attempts=1 and sat forever past
    its backoff window). limit_per_kind=20 per call plus get_pending's own
    next_attempt_at filter keeps this cheap and naturally rate-limited."""
    if not _push_configs:
        return
    for name, config in _push_configs.items():
        push_client.flush_pending(config, db_path=push_client.db_path_for(name))


def _push_raw_if_batch_full(pot: str, rec: dict, sample_index: int):
    if not _push_configs:
        return

    buf = _raw_batch_buffer[pot]
    buf.append({
        "sample_index": sample_index,
        "millis_ms": rec["millis_ms"],
        "temp_c": rec["temp_c"],
        "humidity_pct": rec["humidity_pct"],
        "pressure_hpa": rec["pressure_hpa"],
        "gas_kohm": rec["gas_kohm"],
        "light_lux": rec["light_lux"],
        "moisture_pct": rec["moisture_pct"],
        "received_at": rec["received_at"],
    })
    if len(buf) < push_client.MAX_RAW_BATCH_SIZE:
        return

    readings, _raw_batch_buffer[pot] = buf, []
    for name, config in _push_configs.items():
        # Raw readings are local-diagnostic data, not part of the PRD's cloud
        # contract (only inference/flag/confidence/trend -- see
        # _push_evidence_if_ready -- is meant to leave the board). Pushing raw
        # to "cloud" too was an oversight from before "local" got disabled
        # (2026-09-11, see the _push_configs comment above): with only
        # "cloud" configured, this loop was sending raw batches there by
        # default. Board-local storage (board_db/JSONL) remains the durable
        # raw source of truth regardless.
        if name == "cloud":
            continue
        if not _pot_allowed(name, pot):
            continue
        push_client.push_raw_batch(
            config["node_id"], config["project_id"], pot, readings,
            config, time_uncertain=not _ntp_info["ntp_synced"],
            db_path=push_client.db_path_for(name),
        )


# ── Bridge handlers ────────────────────────────────────────────────────────────
def handle_status(msg):
    """Reports on the sd_status channel: init diagnostics (printed only) and
    the periodic "SD_SPACE used=X.XMB free=X.XMB total=X.XMB" line from
    sketch.ino's reportSdSpace(), which is persisted for the dashboard.
    """
    print(f"[SD Bus Status]: {msg}", flush=True)
    if not msg.startswith("SD_SPACE"):
        return
    try:
        values = {}
        for token in msg.split()[1:]:
            key, raw_val = token.split("=", 1)
            values[key] = float(raw_val.rstrip("MB"))
        entry = {
            "sd_used_mb": values["used"],
            "sd_free_mb": values["free"],
            "sd_total_mb": values["total"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(SD_STATUS_PATH, "w") as f:
            json.dump(entry, f)
    except (ValueError, IndexError, KeyError) as e:
        print(f"[SD Status] WARN: could not parse {msg!r}: {e}", flush=True)


def handle_log(msg):
    print(f"[Log]: {msg}", flush=True)


def handle_reading(msg):
    """Parse a 14-field CSV reading from sketch and process storage/sync.
    Prints the 1-second raw sensor values immediately on arrival.
    """
    try:
        parts = msg.split(",")
        if len(parts) < 14:
            print(f"[Reading] WARN: skipping malformed message (expected 14 fields): {msg!r}", flush=True)
            return

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

        # Immediately print raw sensor line for both pots every 1 second
        print(
            f"[Reading #{sample_index}] "
            f"A: temp={rec_a['temp_c']}C hum={rec_a['humidity_pct']}% press={rec_a['pressure_hpa']}hPa gas={rec_a['gas_kohm']}kΩ soil={rec_a['moisture_pct']}% light={rec_a['light_lux']}lx | "
            f"B: temp={rec_b['temp_c']}C hum={rec_b['humidity_pct']}% press={rec_b['pressure_hpa']}hPa gas={rec_b['gas_kohm']}kΩ soil={rec_b['moisture_pct']}% light={rec_b['light_lux']}lx",
            flush=True,
        )

    except (ValueError, IndexError) as e:
        print(f"[Reading] WARN: skipping malformed message {msg!r}: {e}", flush=True)
        return

    # Skip DB and push if both BME sets are all zero
    all_zero_a = rec_a["temp_c"] == 0.0 and rec_a["pressure_hpa"] == 0.0 and rec_a["gas_kohm"] == 0.0
    all_zero_b = rec_b["temp_c"] == 0.0 and rec_b["pressure_hpa"] == 0.0 and rec_b["gas_kohm"] == 0.0
    if all_zero_a and all_zero_b:
        return

    os.makedirs(DATA_DIR, exist_ok=True)

    try:
        with board_db.connect() as conn:
            if not all_zero_a:
                board_db.insert_reading(conn, "A", rec_a)
            if not all_zero_b:
                board_db.insert_reading(conn, "B", rec_b)
        _maybe_prune_old()
        _maybe_report_disk_status()
    except sqlite3.OperationalError as e:
        print(f"[Reading] WARN: sample {sample_index} DROPPED (db write failed: {e}) -- "
              f"local storage contended, will retry on next reading", flush=True)
    except Exception as e:
        print(f"[Reading] WARN: db write failed for sample {sample_index}: {e}", flush=True)

    # Cloud push (never blocks/undoes local storage)
    try:
        if not all_zero_a:
            _push_evidence_if_ready("A", rec_a, sample_index)
            _push_raw_if_batch_full("A", rec_a, sample_index)
        if not all_zero_b:
            # Pot A as Pot B's gas reference -- same sample, None if Pot A's BME read all-zero
            _push_evidence_if_ready("B", rec_b, sample_index,
                                    ref_gas=None if all_zero_a else rec_a["gas_kohm"], ref_pot="A")
            _push_raw_if_batch_full("B", rec_b, sample_index)
        _flush_pending()
    except Exception as e:  # noqa: BLE001 — a push-path bug must never take down the read loop
        print(f"[Push] WARN: cloud push failed for sample {sample_index}: {e}", flush=True)


bridge.provide("sd_status", handle_status)
bridge.provide("write_log", handle_log)
bridge.provide("diag", handle_log)
bridge.provide("reading", handle_reading)

if __name__ == "__main__":
    print(f"[{PYTHON_VERSION}] Dual-pot sensor relay started — Pot A (0x76/A1) and Pot B (0x77/A0)…", flush=True)
    App.run()
