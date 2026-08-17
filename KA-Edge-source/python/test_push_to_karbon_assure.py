import hashlib
import hmac
import os

import pytest

from push_to_karbon_assure import ConfigError, load_config, sign_request

_REQUIRED_ENV = {
    "KARBON_ASSURE_API_BASE_URL": "http://localhost:8001",
    "KARBON_ASSURE_PROJECT_ID": "a166db20-2e3f-5cf4-880a-cac97eb58299",
    "KARBON_ASSURE_NODE_ID": "edge-node-01",
    "KARBON_ASSURE_DEVICE_SECRET": "test-secret-not-real",
}


@pytest.fixture
def clean_env(monkeypatch):
    for key in _REQUIRED_ENV:
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
