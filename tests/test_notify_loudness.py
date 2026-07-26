"""Regression tests: notifications must never fail silently.

Covers:
  - validate() logs ERROR (not raises) when notifications enabled + secret absent
  - validate() still raises for missing ANTHROPIC/NEXUS keys
  - GET /api/safety/status exposes notify_channel (secret_present never leaks value)
  - parametrized: every Telegram auth-path secret is covered by the startup loudness check

notify()/deliver_pending() failure-classification tests (401/403/404/400 don't
queue, 429/5xx/transport errors do) live in test_telegram_notify.py, since that
logic moved from backend.integrations.hermes to backend.integrations.telegram
(Phase 1 Hermes decoupling).
"""
import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Task 1 — validate() startup loudness
# ---------------------------------------------------------------------------

class TestValidateStartupLoudness:
    def _make_settings(self, *, phone_enabled: bool, has_telegram_secrets: bool, has_anthropic: bool = True, has_nexus: bool = True):
        from backend.config import Settings
        s = Settings()
        object.__setattr__(s, "phone_notifications_enabled", phone_enabled)

        def _secret(key):
            if key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
                if has_telegram_secrets:
                    return "real-secret"
                raise KeyError(key)
            if key == "ANTHROPIC_API_KEY":
                if has_anthropic:
                    return "sk-ant-real"
                raise KeyError(key)
            if key == "NEXUS_API_KEY":
                if has_nexus:
                    return "nexus-real"
                raise KeyError(key)
            raise KeyError(key)

        return s, _secret

    def test_logs_error_when_notify_enabled_and_secret_absent(self, caplog):
        from backend.config import Settings
        s, _secret = self._make_settings(phone_enabled=True, has_telegram_secrets=False)
        with patch("backend.secrets.manager.get_secret", side_effect=_secret), \
             caplog.at_level(logging.ERROR, logger="backend.config"):
            s.validate()  # must NOT raise
        assert any("TELEGRAM_BOT_TOKEN" in r.message for r in caplog.records if r.levelno == logging.ERROR)
        assert any("TELEGRAM_CHAT_ID" in r.message for r in caplog.records if r.levelno == logging.ERROR)

    def test_no_error_when_secret_present(self, caplog):
        from backend.config import Settings
        s, _secret = self._make_settings(phone_enabled=True, has_telegram_secrets=True)
        with patch("backend.secrets.manager.get_secret", side_effect=_secret), \
             caplog.at_level(logging.ERROR, logger="backend.config"):
            s.validate()
        assert not any("TELEGRAM_BOT_TOKEN" in r.message for r in caplog.records if r.levelno == logging.ERROR)

    def test_no_error_when_notifications_disabled(self, caplog):
        from backend.config import Settings
        s, _secret = self._make_settings(phone_enabled=False, has_telegram_secrets=False)
        with patch("backend.secrets.manager.get_secret", side_effect=_secret), \
             caplog.at_level(logging.ERROR, logger="backend.config"):
            s.validate()
        assert not any("TELEGRAM_BOT_TOKEN" in r.message for r in caplog.records)

    def test_still_raises_for_missing_anthropic_key(self):
        from backend.config import Settings
        s, _secret = self._make_settings(phone_enabled=False, has_telegram_secrets=True, has_anthropic=False)
        with patch("backend.secrets.manager.get_secret", side_effect=_secret):
            with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
                s.validate()

    def test_still_raises_for_missing_nexus_key(self):
        from backend.config import Settings
        s, _secret = self._make_settings(phone_enabled=False, has_telegram_secrets=True, has_nexus=False)
        with patch("backend.secrets.manager.get_secret", side_effect=_secret):
            with pytest.raises(RuntimeError, match="NEXUS_API_KEY"):
                s.validate()


# ---------------------------------------------------------------------------
# Task 4 — GET /api/safety/status exposes notify_channel without leaking secret
# ---------------------------------------------------------------------------

class TestSafetyStatusNotifyChannel:
    @pytest.fixture()
    def client(self):
        from backend.main import app
        from backend.auth import require_api_key
        app.dependency_overrides[require_api_key] = lambda: None
        yield TestClient(app, raise_server_exceptions=False)
        app.dependency_overrides.clear()

    def test_notify_channel_present_in_status(self, client):
        with patch("backend.integrations.telegram.delivery_queue_health", return_value={
            "pending_count": 2,
            "oldest_age_seconds": 300,
            "dead_lettered_count": 1,
            "secret_present": False,
        }):
            resp = client.get("/api/safety/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "notify_channel" in body
        nc = body["notify_channel"]
        assert "pending_count" in nc
        assert "secret_present" in nc
        assert "enabled" in nc
        # Must never contain the actual secret value
        assert nc.get("secret_present") in (True, False)

    def test_notify_channel_no_secret_value_leaked(self, client):
        with patch("backend.integrations.telegram.delivery_queue_health", return_value={
            "pending_count": 0, "oldest_age_seconds": None,
            "dead_lettered_count": 0, "secret_present": True,
        }):
            resp = client.get("/api/safety/status")
        body = resp.json()
        nc = body.get("notify_channel", {})
        # Ensure no string value that looks like a secret is in the response
        for v in nc.values():
            assert not isinstance(v, str) or len(v) < 20, f"Possible secret value leaked: {v!r}"


# ---------------------------------------------------------------------------
# Parametrized: every Telegram auth-path property is covered by startup loudness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("secret_name", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])
def test_hermes_auth_secrets_covered_by_startup_check(secret_name, caplog):
    """Any secret used for Telegram notify auth must be caught by validate() if missing."""
    from backend.config import Settings
    s = Settings()
    object.__setattr__(s, "phone_notifications_enabled", True)

    def _secret(key):
        if key == secret_name:
            raise KeyError(key)
        if key in ("ANTHROPIC_API_KEY", "NEXUS_API_KEY"):
            return "present"
        raise KeyError(key)

    with patch("backend.secrets.manager.get_secret", side_effect=_secret), \
         caplog.at_level(logging.ERROR, logger="backend.config"):
        s.validate()

    assert any(secret_name in r.message for r in caplog.records if r.levelno == logging.ERROR), \
        f"{secret_name} not surfaced by validate() — add it to the startup loudness check"
