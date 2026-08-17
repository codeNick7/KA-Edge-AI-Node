"""Push client for the Karbon Assure soil-sensor ingestion API — Phase 1.

Phase 1 scope only (per docs/plan/karbon-assure-ingestion-integration.md):
config loading + the shared HMAC-signing helper every later phase's push
calls reuse. push_evidence()/push_raw_batch() and the durable local queue
are Phase 2/3 work, not built yet.

Config is read from environment variables, not hardcoded, matching the
Karbon Assure backend's own env-var-first convention (e.g.
SATELLITE_WEBHOOK_SECRET):

  KARBON_ASSURE_API_BASE_URL   e.g. http://localhost:8001
  KARBON_ASSURE_PROJECT_ID     base_projects.id UUID (NOT the project_code
                                string -- see plan doc §4's project_id
                                convention decision), e.g.
                                a166db20-2e3f-5cf4-880a-cac97eb58299
  KARBON_ASSURE_NODE_ID        this board's device_id, e.g. edge-node-01
  KARBON_ASSURE_DEVICE_SECRET  the plaintext secret issued once at
                                registration (POST /v1/devices response) --
                                never the encrypted column, that only
                                exists server-side.

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
import os
import urllib.error
import urllib.request


class ConfigError(Exception):
    pass


def load_config() -> dict:
    required = {
        "api_base_url": "KARBON_ASSURE_API_BASE_URL",
        "project_id": "KARBON_ASSURE_PROJECT_ID",
        "node_id": "KARBON_ASSURE_NODE_ID",
        "device_secret": "KARBON_ASSURE_DEVICE_SECRET",
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


def _selftest() -> None:
    """Sanity check: sign a sample payload and print the header, so it can
    be diff-checked against a manual `hmac.new(...)` computation using the
    same secret on the Karbon Assure side, e.g. verify_device_hmac() in a
    Python shell there, without needing a live network call."""
    config = load_config()
    sample = {"node_id": config["node_id"], "pot_id": "A", "flag": "stable"}
    body = json.dumps(sample).encode("utf-8")
    print("body:", body.decode())
    print("signature:", sign_request(body, config["device_secret"]))


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(__doc__)
