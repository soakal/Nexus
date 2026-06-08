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
os.environ.setdefault("OBSIDIAN_HOST", "http://localhost:27123")
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
    "OBSIDIAN_TOKEN": "test-obsidian-token",
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


@pytest.fixture
def api_key():
    return "test-nexus-key"


@pytest.fixture
def auth_headers(api_key):
    return {"Authorization": f"Bearer {api_key}"}
