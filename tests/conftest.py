import pytest
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

# Set test env before any imports
os.environ.setdefault("HASS_HOST", "http://localhost:8123")
os.environ.setdefault("UNIFI_HOST", "https://localhost")
os.environ.setdefault("UNIFI_USERNAME", "admin")
os.environ.setdefault("UNRAID_HOST", "192.168.1.1")
os.environ.setdefault("CHANNELS_HOST", "http://localhost:8089")
os.environ.setdefault("ADGUARD_HOST", "http://localhost:3000")
os.environ.setdefault("ADGUARD_USER", "admin")
os.environ.setdefault("HERMES_HOST", "http://localhost:9000")
os.environ.setdefault("GITHUB_USERNAME", "testuser")

# Mock secrets so vault isn't required in tests
MOCK_SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-test-key",
    "HASS_TOKEN": "test-hass-token",
    "UNIFI_PASSWORD": "test-password",
    "UNRAID_API_KEY": "test-unraid-key",
    "GITHUB_TOKEN": "test-github-token",
    "OPENWEATHER_API_KEY": "test-weather-key",
    "OPENROUTER_API_KEY": "test-openrouter-key",
    "ADGUARD_PASS": "test-adguard-pass",
    "HERMES_WEBHOOK_SECRET": "test-hermes-secret",
    "NEXUS_API_KEY": "test-nexus-key",
}


@pytest.fixture(autouse=True)
def mock_secrets(monkeypatch):
    """Patch get_secret to return test values without vault."""
    def fake_get_secret(key, fallback_env=True):
        if key in MOCK_SECRETS:
            return MOCK_SECRETS[key]
        if fallback_env and key in os.environ:
            return os.environ[key]
        raise KeyError(f"Secret '{key}' not in test mock")

    monkeypatch.setattr("backend.secrets.manager.get_secret", fake_get_secret)
    monkeypatch.setattr("backend.secrets.vault.get_secret", lambda k: MOCK_SECRETS.get(k, (_ for _ in ()).throw(KeyError(k))))


@pytest.fixture(autouse=True)
def reset_caches():
    """Clear all async_ttl_cache state before each test so a cached health_check /
    fetch result from one test can't leak into the next (the caches hold
    module-level state that otherwise persists for the whole session)."""
    from backend.cache import reset_all_caches
    reset_all_caches()
    try:
        from backend.agents.worker_pool import reset_pool
        reset_pool()
    except Exception:
        pass
    try:
        from backend.api.trigger import _reset_rate_limit
        _reset_rate_limit()
    except Exception:
        pass
    yield


@pytest.fixture
def api_key():
    return "test-nexus-key"


@pytest.fixture
def auth_headers(api_key):
    return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture(autouse=True)
def auto_mock_opus_verify(request):
    """Auto-patch _opus_verify in the durable orchestrator path to return a
    permissive success dict by default.

    This prevents the new Opus verifier call (which itself calls run_with_tools)
    from interfering with pre-existing tests that patch run_with_tools and assert
    on its call count. Tests in test_learning_loop.py that need to control the
    verifier's behaviour patch _opus_verify themselves (innermost patch wins).

    Tests that directly unit-test _opus_verify (calling the real function) should
    be marked with @pytest.mark.real_opus_verify to skip this auto-mock so they
    get the actual implementation.
    """
    if request.node.get_closest_marker("real_opus_verify"):
        yield
        return

    _DEFAULT = {
        "verdict": "success",
        "confidence": 1.0,
        "reason": "auto-mocked verifier",
        "grounded": False,
        "evidence": None,
    }
    with patch(
        "backend.agents.orchestrator._opus_verify",
        new_callable=AsyncMock,
        return_value=_DEFAULT,
    ):
        yield
