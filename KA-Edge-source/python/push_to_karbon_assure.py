"""Push client for the Karbon Assure soil-sensor ingestion API.

Phase 2/3 (docs/plan/karbon-assure-ingestion-integration.md): push_evidence()
and push_raw_batch() go through the durable local queue (sync_queue.py) --
every record is queued first, only marked synced after a genuine 2xx, and
survives board reboots. Local JSONL logging (main.py) stays the durable
source of truth regardless of cloud-sync state.

Config is read from environment variables, not hardcoded, matching the
Karbon Assure backend's own env-var-first convention (e.g.
SATELLITE_WEBHOOK_SECRET).

Multiple push destinations (e.g. the local LAN demo backend AND the live
demo.karbon-assure.tech instance) are supported simultaneously -- see
load_configs(). Each destination is a separate Karbon Assure backend with
its own database, so it has its own registered device/secret and its own
sync queue (a record synced to one destination is independent of whether
it's synced to the other). Per destination <NAME> (e.g. "local", "cloud"):

  KARBON_ASSURE_<NAME>_API_BASE_URL   e.g. http://localhost:8001
  KARBON_ASSURE_<NAME>_PROJECT_ID     base_projects.id UUID (NOT the
                                       project_code string -- see plan doc
                                       §4's project_id convention decision),
                                       e.g. a166db20-2e3f-5cf4-880a-cac97eb58299
  KARBON_ASSURE_<NAME>_NODE_ID        this board's device_id on that
                                       backend, e.g. edge-node-01
  KARBON_ASSURE_<NAME>_DEVICE_SECRET  the plaintext secret issued once at
                                       registration (POST /v1/devices
                                       response) on that backend -- never
                                       the encrypted column, that only
                                       exists server-side.

Which destination names to look for is DEFAULT_DESTINATIONS ("local",
"cloud") unless overridden by KARBON_ASSURE_DESTINATIONS (comma-separated).
A destination with none of its four vars set is simply not configured yet
and is skipped -- push still works for whichever destinations are.

Optional, per destination -- which pots' data it receives (allowed_pots()):

  KARBON_ASSURE_<NAME>_POTS           comma-separated pot ids, e.g. "B" or
                                       "A,B". Unset falls back to
                                       DEFAULT_POTS: "cloud" gets Pot B
                                       only (the demo project represents
                                       the degrading pot -- 2026-10-06, see
                                       docs/plan/cloud-push-pot-b-only-
                                       2026-10-06.md), any other
                                       destination gets every pot.

Auth: X-Webhook-Signature: sha256=<hex> HMAC-SHA256 over the raw request
body, keyed by KARBON_ASSURE_DEVICE_SECRET -- mirrors
src/modules/ingestion/satellite/webhook.py's verification shape on the
Karbon Assure side (src/modules/system_core/devices/secret_crypto.py's
verify_device_hmac(), same computation).

Usage (stdlib only, no dependencies -- matches scripts/'s convention):
  .venv/bin/python -m push_to_karbon_assure --selftest
"""

import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.request

import sync_queue

logger = logging.getLogger(__name__)

# Bounded chunk size for a single raw-batch POST -- never one unbounded
# request after a long outage (§5.3 point 4).
MAX_RAW_BATCH_SIZE = 200


class ConfigError(Exception):
    pass


DEFAULT_DESTINATIONS = ("local", "cloud")


def load_config(prefix: str = "KARBON_ASSURE_") -> dict:
    """Load a single destination's config from env vars sharing `prefix`."""
    required = {
        "api_base_url": prefix + "API_BASE_URL",
        "project_id": prefix + "PROJECT_ID",
        "node_id": prefix + "NODE_ID",
        "device_secret": prefix + "DEVICE_SECRET",
    }
    config = {}
    missing = []
    for key, env_var in required.items():
        value = os.environ.get(env_var)
        if not value:
            missing.append(env_var)
        config[key] = value
    if missing:
        raise ConfigError(f"missing required env var(s): {', '.join(missing)}")
    return config


def load_configs(names: tuple[str, ...] = DEFAULT_DESTINATIONS) -> dict[str, dict]:
    """Load every configured push destination, keyed by name.

    A destination is only included if at least one of its four vars is
    set; a destination with none of them set is treated as "not configured
    yet", not an error, so e.g. "cloud" can be added later purely via .env.
    A destination with *some* vars set and others missing still raises
    ConfigError (via load_config) -- that's almost certainly a typo.
    """
    requested = os.environ.get("KARBON_ASSURE_DESTINATIONS")
    dest_names = [n.strip() for n in requested.split(",") if n.strip()] if requested else list(names)

    configs = {}
    for name in dest_names:
        prefix = f"KARBON_ASSURE_{name.upper()}_"
        suffixes = ("API_BASE_URL", "PROJECT_ID", "NODE_ID", "DEVICE_SECRET")
        if not any(os.environ.get(prefix + suffix) for suffix in suffixes):
            continue  # this destination isn't configured -- skip, not an error
        configs[name] = load_config(prefix=prefix)
    return configs


# Fallback pot scope per destination when KARBON_ASSURE_<NAME>_POTS is unset.
# "cloud" is pinned to Pot B in code (not only via .env) for the same reason
# main.py pins its destination list: a stale .env or a stray process with old
# env must not silently start sending Pot A to the demo project again.
# Destinations not listed here receive every pot.
DEFAULT_POTS: dict[str, frozenset[str]] = {"cloud": frozenset({"B"})}


def allowed_pots(name: str) -> frozenset[str] | None:
    """Pots whose data destination `name` should receive, or None for all.

    Reads KARBON_ASSURE_<NAME>_POTS (comma-separated, e.g. "B" or "A,B");
    unset or blank falls back to DEFAULT_POTS. Each id must be a single
    uppercase letter -- the same pot_id shape the backend's schema accepts
    (^[A-Z]$) -- otherwise ConfigError, so a typo can't widen the scope.
    """
    env_var = f"KARBON_ASSURE_{name.upper()}_POTS"
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return DEFAULT_POTS.get(name)
    pots = [p.strip() for p in raw.split(",") if p.strip()]
    invalid = [p for p in pots if not (len(p) == 1 and "A" <= p <= "Z")]
    if not pots or invalid:
        raise ConfigError(f"{env_var}={raw!r}: expected comma-separated pot ids like 'B' or 'A,B'")
    return frozenset(pots)


def db_path_for(name: str, db_path: str = sync_queue.DEFAULT_DB_PATH) -> str:
    """Per-destination sync-queue DB path, so 'local' and 'cloud' sync
    state (queued/synced/backoff) are tracked independently -- an outage
    on one destination must not affect retry state for the other."""
    base, ext = os.path.splitext(db_path)
    return f"{base}_{name}{ext}"


def sign_request(body: bytes, device_secret: str) -> str:
    """X-Webhook-Signature header value for a request body.

    Same computation as the Karbon Assure backend's
    devices/secret_crypto.py::verify_device_hmac() performs server-side
    (after decrypting the device's stored secret) -- signing and
    verification must produce byte-identical results for auth to work.
    """
    digest = hmac.new(device_secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def post_json(path: str, payload: dict, config: dict, timeout: float = 10.0) -> tuple[int, dict]:
    """POST a JSON payload with the HMAC signature header.

    Returns (status_code, parsed_json_response). Raises urllib.error.URLError
    on connection failure (caller's responsibility to retry/queue -- Phase 2).
    """
    body = json.dumps(payload).encode("utf-8")
    signature = sign_request(body, config["device_secret"])
    url = config["api_base_url"].rstrip("/") + path

    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _try_send(path: str, payload: dict, config: dict) -> tuple[bool, str]:
    """Attempt one send. Returns (success, error_message). Never raises --
    callers (push_evidence/flush_pending) need this to be a clean boolean
    so a network failure just leaves the record queued for retry."""
    try:
        status, response = post_json(path, payload, config)
    except Exception as exc:  # noqa: BLE001 -- any failure just means "retry later"
        return False, str(exc)

    if 200 <= status < 300:
        return True, ""
    return False, f"HTTP {status}: {response}"


def push_evidence(record: dict, config: dict, db_path: str = sync_queue.DEFAULT_DB_PATH) -> None:
    """Queue an evidence record and attempt to send it immediately.

    record is expected to already match the evidence payload shape
    (node_id, project_id, pot_id, sample_index, timestamp, flag,
    confidence, contributing_trend, model_version, schema_version,
    time_uncertain, raw={rsmi, gas_index, temp_c, light_lux}).
    Never raises -- push failures are queued for retry, not propagated.

    Queue key is the reading's timestamp, not sample_index (2026-10-06):
    sample_index is the firmware's in-memory counter and restarts near zero
    on every board reboot, so a sample_index key collided with rows already
    in the (never-purged) queue from earlier sessions and enqueue() silently
    dropped every new record -- nothing sent, nothing logged. Mirrors the
    backend's own 2026-10-03 re-key of soil_health_evidence onto measured_at.
    """
    key = f"{record['node_id']}|{record['pot_id']}|{record['timestamp']}"
    queue_id = sync_queue.enqueue("evidence", key, record, db_path=db_path)
    if queue_id is None:
        return  # already queued (or already synced and purged) -- no-op

    ok, error = _try_send("/v1/ingest/soil-sensor/evidence", record, config)
    if ok:
        sync_queue.mark_synced(queue_id, db_path=db_path)
    else:
        sync_queue.mark_failed(queue_id, error, db_path=db_path)
        logger.warning("push_evidence failed, queued for retry: %s", error)


def push_raw_batch(node_id: str, project_id: str, pot_id: str, readings: list[dict], config: dict,
                    time_uncertain: bool = False, db_path: str = sync_queue.DEFAULT_DB_PATH) -> None:
    """Queue a batch of raw readings, chunked to MAX_RAW_BATCH_SIZE, and
    attempt to send each chunk immediately. Never raises."""
    for start in range(0, len(readings), MAX_RAW_BATCH_SIZE):
        chunk = readings[start:start + MAX_RAW_BATCH_SIZE]
        # received_at, not sample_index -- same reboot-collision reason as push_evidence's key
        chunk_key = f"{node_id}|{pot_id}|{chunk[0]['received_at']}-{chunk[-1]['received_at']}"
        payload = {
            "node_id": node_id,
            "project_id": project_id,
            "pot_id": pot_id,
            "time_uncertain": time_uncertain,
            "readings": chunk,
        }
        queue_id = sync_queue.enqueue("raw_batch", chunk_key, payload, db_path=db_path)
        if queue_id is None:
            continue

        ok, error = _try_send("/v1/ingest/soil-sensor/raw", payload, config)
        if ok:
            sync_queue.mark_synced(queue_id, db_path=db_path)
        else:
            sync_queue.mark_failed(queue_id, error, db_path=db_path)
            logger.warning("push_raw_batch chunk failed, queued for retry: %s", error)


def flush_pending(config: dict, limit_per_kind: int = 20, db_path: str = sync_queue.DEFAULT_DB_PATH) -> dict:
    """Retry everything past its backoff window. Call this periodically
    (e.g. once per reading cycle) so records queued during an outage get
    synced once connectivity returns -- architecture doc Section 9's
    "sync resumes automatically once connectivity returns" requirement."""
    results = {"evidence": {"sent": 0, "failed": 0}, "raw_batch": {"sent": 0, "failed": 0}}
    for kind, path in (("evidence", "/v1/ingest/soil-sensor/evidence"), ("raw_batch", "/v1/ingest/soil-sensor/raw")):
        for item in sync_queue.get_pending(kind, limit=limit_per_kind, db_path=db_path):
            ok, error = _try_send(path, item["payload"], config)
            if ok:
                sync_queue.mark_synced(item["id"], db_path=db_path)
                results[kind]["sent"] += 1
            else:
                sync_queue.mark_failed(item["id"], error, db_path=db_path)
                results[kind]["failed"] += 1
    return results


def _selftest() -> None:
    """Sanity check: sign a sample payload per configured destination and
    print the header, so it can be diff-checked against a manual
    `hmac.new(...)` computation using the same secret on the Karbon Assure
    side, e.g. verify_device_hmac() in a Python shell there, without
    needing a live network call."""
    configs = load_configs()
    if not configs:
        print("no destinations configured (KARBON_ASSURE_LOCAL_*/_CLOUD_* env vars)")
        return
    for name, config in configs.items():
        sample = {"node_id": config["node_id"], "pot_id": "A", "flag": "stable"}
        body = json.dumps(sample).encode("utf-8")
        print(f"[{name}] body:", body.decode())
        print(f"[{name}] signature:", sign_request(body, config["device_secret"]))


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(__doc__)
