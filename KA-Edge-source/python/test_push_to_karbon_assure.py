import hashlib
import hmac
import os

import pytest

from push_to_karbon_assure import ConfigError, allowed_pots, db_path_for, load_config, load_configs, sign_request

_REQUIRED_ENV = {
    "KARBON_ASSURE_API_BASE_URL": "http://localhost:8001",
    "KARBON_ASSURE_PROJECT_ID": "a166db20-2e3f-5cf4-880a-cac97eb58299",
    "KARBON_ASSURE_NODE_ID": "edge-node-01",
    "KARBON_ASSURE_DEVICE_SECRET": "test-secret-not-real",
}

_LOCAL_ENV = {
    "KARBON_ASSURE_LOCAL_API_BASE_URL": "http://192.168.1.50:8001",
    "KARBON_ASSURE_LOCAL_PROJECT_ID": "a166db20-2e3f-5cf4-880a-cac97eb58299",
    "KARBON_ASSURE_LOCAL_NODE_ID": "edge-node-01",
    "KARBON_ASSURE_LOCAL_DEVICE_SECRET": "local-secret-not-real",
}

_CLOUD_ENV = {
    "KARBON_ASSURE_CLOUD_API_BASE_URL": "https://demo.karbon-assure.tech",
    "KARBON_ASSURE_CLOUD_PROJECT_ID": "b277ec31-3f4g-6df5-991b-dbd08f69f3aa",
    "KARBON_ASSURE_CLOUD_NODE_ID": "edge-node-01",
    "KARBON_ASSURE_CLOUD_DEVICE_SECRET": "cloud-secret-not-real",
}


@pytest.fixture
def clean_env(monkeypatch):
    _POTS_ENV = {"KARBON_ASSURE_LOCAL_POTS": "", "KARBON_ASSURE_CLOUD_POTS": ""}
    for key in {**_REQUIRED_ENV, **_LOCAL_ENV, **_CLOUD_ENV, **_POTS_ENV, "KARBON_ASSURE_DESTINATIONS": ""}:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_load_config_raises_on_missing_vars(clean_env):
    with pytest.raises(ConfigError, match="KARBON_ASSURE_API_BASE_URL"):
        load_config()


def test_load_config_raises_listing_all_missing_vars(clean_env):
    clean_env.setenv("KARBON_ASSURE_NODE_ID", "edge-node-01")
    with pytest.raises(ConfigError) as exc_info:
        load_config()
    msg = str(exc_info.value)
    assert "KARBON_ASSURE_API_BASE_URL" in msg
    assert "KARBON_ASSURE_PROJECT_ID" in msg
    assert "KARBON_ASSURE_DEVICE_SECRET" in msg
    assert "KARBON_ASSURE_NODE_ID" not in msg  # this one was set


def test_load_config_success(clean_env):
    for key, value in _REQUIRED_ENV.items():
        clean_env.setenv(key, value)
    config = load_config()
    assert config == {
        "api_base_url": "http://localhost:8001",
        "project_id": "a166db20-2e3f-5cf4-880a-cac97eb58299",
        "node_id": "edge-node-01",
        "device_secret": "test-secret-not-real",
    }


def test_sign_request_matches_manual_hmac_computation():
    body = b'{"flag": "stable"}'
    secret = "some-device-secret"

    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert sign_request(body, secret) == expected


def test_sign_request_is_deterministic_for_same_inputs():
    body = b'{"flag": "moisture_low"}'
    secret = "abc123"
    assert sign_request(body, secret) == sign_request(body, secret)


def test_sign_request_differs_for_different_secrets():
    body = b'{"flag": "moisture_low"}'
    assert sign_request(body, "secret-a") != sign_request(body, "secret-b")


def test_sign_request_differs_for_different_bodies():
    secret = "abc123"
    assert sign_request(b'{"flag": "stable"}', secret) != sign_request(b'{"flag": "moisture_low"}', secret)


def test_load_configs_empty_when_nothing_set(clean_env):
    assert load_configs() == {}


def test_load_configs_loads_only_configured_destinations(clean_env):
    for key, value in _LOCAL_ENV.items():
        clean_env.setenv(key, value)
    configs = load_configs()
    assert set(configs) == {"local"}
    assert configs["local"] == {
        "api_base_url": "http://192.168.1.50:8001",
        "project_id": "a166db20-2e3f-5cf4-880a-cac97eb58299",
        "node_id": "edge-node-01",
        "device_secret": "local-secret-not-real",
    }


def test_load_configs_loads_both_local_and_cloud_simultaneously(clean_env):
    for env in (_LOCAL_ENV, _CLOUD_ENV):
        for key, value in env.items():
            clean_env.setenv(key, value)
    configs = load_configs()
    assert set(configs) == {"local", "cloud"}
    assert configs["cloud"]["api_base_url"] == "https://demo.karbon-assure.tech"
    assert configs["local"]["api_base_url"] == "http://192.168.1.50:8001"


def test_load_configs_raises_on_partially_configured_destination(clean_env):
    clean_env.setenv("KARBON_ASSURE_LOCAL_API_BASE_URL", "http://192.168.1.50:8001")
    with pytest.raises(ConfigError, match="KARBON_ASSURE_LOCAL_PROJECT_ID"):
        load_configs()


def test_load_configs_respects_destinations_override(clean_env):
    clean_env.setenv("KARBON_ASSURE_DESTINATIONS", "cloud")
    for key, value in {**_LOCAL_ENV, **_CLOUD_ENV}.items():
        clean_env.setenv(key, value)
    assert set(load_configs()) == {"cloud"}


def test_db_path_for_suffixes_each_destination_distinctly():
    local_path = db_path_for("local", db_path="/data/sync_queue.db")
    cloud_path = db_path_for("cloud", db_path="/data/sync_queue.db")
    assert local_path == "/data/sync_queue_local.db"
    assert cloud_path == "/data/sync_queue_cloud.db"
    assert local_path != cloud_path


def test_allowed_pots_cloud_defaults_to_pot_b_only(clean_env):
    assert allowed_pots("cloud") == frozenset({"B"})


def test_allowed_pots_other_destinations_default_to_all(clean_env):
    assert allowed_pots("local") is None


def test_allowed_pots_blank_env_falls_back_to_default(clean_env):
    clean_env.setenv("KARBON_ASSURE_CLOUD_POTS", "   ")
    assert allowed_pots("cloud") == frozenset({"B"})


def test_allowed_pots_env_overrides_default(clean_env):
    clean_env.setenv("KARBON_ASSURE_CLOUD_POTS", "A, B")
    assert allowed_pots("cloud") == frozenset({"A", "B"})
    clean_env.setenv("KARBON_ASSURE_LOCAL_POTS", "B")
    assert allowed_pots("local") == frozenset({"B"})


def test_allowed_pots_ignores_trailing_comma(clean_env):
    clean_env.setenv("KARBON_ASSURE_CLOUD_POTS", "B,")
    assert allowed_pots("cloud") == frozenset({"B"})


@pytest.mark.parametrize("bad", ["b", "Pot B", "AB", ",", "B;A", "1"])
def test_allowed_pots_rejects_malformed_values(clean_env, bad):
    clean_env.setenv("KARBON_ASSURE_CLOUD_POTS", bad)
    with pytest.raises(ConfigError, match="KARBON_ASSURE_CLOUD_POTS"):
        allowed_pots("cloud")


def test_pots_env_does_not_mark_destination_configured(clean_env):
    # *_POTS alone must not make load_configs treat "cloud" as (partially) configured
    clean_env.setenv("KARBON_ASSURE_CLOUD_POTS", "B")
    assert load_configs() == {}


def _evidence(sample_index, timestamp, pot="B"):
    return {"node_id": "edge-node-01", "project_id": "p", "pot_id": pot,
            "sample_index": sample_index, "timestamp": timestamp, "flag": "stable"}


def test_push_evidence_after_reboot_reused_sample_index_is_still_sent(tmp_path, monkeypatch):
    # Board reboot restarts the firmware counter: same sample_index, new timestamp.
    # Keyed on sample_index this second record was silently dropped (2026-10-06 bug).
    import push_to_karbon_assure as pc
    sent = []
    monkeypatch.setattr(pc, "_try_send", lambda path, payload, config: (sent.append(payload) or (True, "")))
    db = str(tmp_path / "q.db")
    pc.push_evidence(_evidence(9, "2026-09-23T14:27:19+00:00"), {}, db_path=db)
    pc.push_evidence(_evidence(9, "2026-10-06T12:16:17+00:00"), {}, db_path=db)
    assert [p["timestamp"] for p in sent] == ["2026-09-23T14:27:19+00:00", "2026-10-06T12:16:17+00:00"]


def test_push_evidence_same_reading_is_deduplicated(tmp_path, monkeypatch):
    import push_to_karbon_assure as pc
    sent = []
    monkeypatch.setattr(pc, "_try_send", lambda path, payload, config: (sent.append(payload) or (True, "")))
    db = str(tmp_path / "q.db")
    pc.push_evidence(_evidence(9, "2026-10-06T12:16:17+00:00"), {}, db_path=db)
    pc.push_evidence(_evidence(9, "2026-10-06T12:16:17+00:00"), {}, db_path=db)
    assert len(sent) == 1
