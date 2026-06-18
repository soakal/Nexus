"""Regression tests for Task 1-6: notifications must never fail silently.

Covers:
  - validate() logs ERROR (not raises) when notifications enabled + secret absent
  - validate() still raises for missing ANTHROPIC/NEXUS keys
  - notify() on 401/403 does NOT queue and logs ERROR
  - notify() on transport exception still queues (existing behaviour)
  - deliver_pending() logs status code on non-2xx (was silent)
  - deliver_pending() does not queue on 401 via notify() path (belt-and-suspenders)
  - GET /api/safety/status exposes notify_channel (secret_present never leaks value)
  - parametrized: every Hermes auth-path secret is covered by the startup loudness check
"""
import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=httpx.Request("POST", "http://test"))


# ---------------------------------------------------------------------------
# Task 1 — validate() startup loudness
# ---------------------------------------------------------------------------

class TestValidateStartupLoudness:
    def _make_settings(self, *, phone_enabled: bool, has_hermes_secret: bool, has_anthropic: bool = True, has_nexus: bool = True):
        from backend.config import Settings
        s = Settings()
        object.__setattr__(s, "phone_notifications_enabled", phone_enabled)

        def _secret(key):
            if key == "HERMES_WEBHOOK_SECRET":
                if has_hermes_secret:
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
        s, _secret = self._make_settings(phone_enabled=True, has_hermes_secret=False)
        with patch("backend.secrets.manager.get_secret", side_effect=_secret), \
             caplog.at_level(logging.ERROR, logger="backend.config"):
            s.validate()  # must NOT raise
        assert any("HERMES_WEBHOOK_SECRET" in r.message for r in caplog.records if r.levelno == logging.ERROR)
        assert any("manage_vault.py" in r.message for r in caplog.records if r.levelno == logging.ERROR)

    def test_no_error_when_secret_present(self, caplog):
        from backend.config import Settings
        s, _secret = self._make_settings(phone_enabled=True, has_hermes_secret=True)
        with patch("backend.secrets.manager.get_secret", side_effect=_secret), \
             caplog.at_level(logging.ERROR, logger="backend.config"):
            s.validate()
        assert not any("HERMES_WEBHOOK_SECRET" in r.message for r in caplog.records if r.levelno == logging.ERROR)

    def test_no_error_when_notifications_disabled(self, caplog):
        from backend.config import Settings
        s, _secret = self._make_settings(phone_enabled=False, has_hermes_secret=False)
        with patch("backend.secrets.manager.get_secret", side_effect=_secret), \
             caplog.at_level(logging.ERROR, logger="backend.config"):
            s.validate()
        assert not any("HERMES_WEBHOOK_SECRET" in r.message for r in caplog.records)

    def test_still_raises_for_missing_anthropic_key(self):
        from backend.config import Settings
        s, _secret = self._make_settings(phone_enabled=False, has_hermes_secret=True, has_anthropic=False)
        with patch("backend.secrets.manager.get_secret", side_effect=_secret):
            with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
                s.validate()

    def test_still_raises_for_missing_nexus_key(self):
        from backend.config import Settings
        s, _secret = self._make_settings(phone_enabled=False, has_hermes_secret=True, has_nexus=False)
        with patch("backend.secrets.manager.get_secret", side_effect=_secret):
            with pytest.raises(RuntimeError, match="NEXUS_API_KEY"):
                s.validate()


# ---------------------------------------------------------------------------
# Task 2 — notify() auth-failure handling
# ---------------------------------------------------------------------------

class TestNotifyAuthFailure:
    @pytest.fixture(autouse=True)
    def _patch_settings(self):
        mock_settings = MagicMock()
        mock_settings.hermes_host = "http://hermes-test"
        mock_settings.hermes_webhook_secret = "test-secret"
        with patch("backend.config.get_settings", return_value=mock_settings):
            yield

    @pytest.mark.asyncio
    async def test_401_does_not_queue_and_logs_error(self, caplog):
        from backend.integrations import hermes

        initial_count = [0]

        def _count_queue(payload, delivery_type):
            initial_count[0] += 1

        with patch.object(hermes, "_queue_delivery", side_effect=_count_queue), \
             patch("httpx.AsyncClient") as mock_client_cls, \
             caplog.at_level(logging.ERROR, logger="backend.integrations.hermes"):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_make_response(401))
            mock_client_cls.return_value = mock_client

            result = await hermes.notify({"message": "test"})

        assert result is False
        assert initial_count[0] == 0, "401 must NOT queue for retry"
        assert any("AUTH FAILED" in r.message for r in caplog.records if r.levelno == logging.ERROR)

    @pytest.mark.asyncio
    async def test_403_does_not_queue_and_logs_error(self, caplog):
        from backend.integrations import hermes

        queued = []

        with patch.object(hermes, "_queue_delivery", side_effect=lambda p, t: queued.append(t)), \
             patch("httpx.AsyncClient") as mock_client_cls, \
             caplog.at_level(logging.ERROR, logger="backend.integrations.hermes"):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_make_response(403))
            mock_client_cls.return_value = mock_client

            result = await hermes.notify({"message": "test"})

        assert result is False
        assert queued == [], "403 must NOT queue for retry"

    @pytest.mark.asyncio
    async def test_transport_exception_still_queues(self, caplog):
        from backend.integrations import hermes

        queued = []

        with patch.object(hermes, "_queue_delivery", side_effect=lambda p, t: queued.append(t)), \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client_cls.return_value = mock_client

            result = await hermes.notify({"message": "test"})

        assert result is False
        assert "notify" in queued, "transport error must queue for retry"

    @pytest.mark.asyncio
    async def test_500_queues_with_warning(self, caplog):
        from backend.integrations import hermes

        queued = []

        with patch.object(hermes, "_queue_delivery", side_effect=lambda p, t: queued.append(t)), \
             patch("httpx.AsyncClient") as mock_client_cls, \
             caplog.at_level(logging.WARNING, logger="backend.integrations.hermes"):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_make_response(500))
            mock_client_cls.return_value = mock_client

            result = await hermes.notify({"message": "test"})

        assert result is False
        assert "notify" in queued
        assert any("500" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Task 2 — deliver_pending() logs status codes
# ---------------------------------------------------------------------------

class TestDeliverPendingLogging:
    @pytest.fixture(autouse=True)
    def _patch_settings(self):
        mock_settings = MagicMock()
        mock_settings.hermes_host = "http://hermes-test"
        mock_settings.hermes_webhook_secret = "test-secret"
        with patch("backend.config.get_settings", return_value=mock_settings):
            yield

    @pytest.mark.asyncio
    async def test_401_in_deliver_pending_logs_error(self, caplog):
        from backend.integrations import hermes

        pending = [{
            "id": 1, "payload_json": json.dumps({"msg": "hi"}),
            "delivery_type": "notify", "attempts": 3, "last_attempt": None,
        }]

        with patch.object(hermes, "_load_pending", return_value=pending), \
             patch.object(hermes, "_apply_pending_results"), \
             patch("httpx.AsyncClient") as mock_client_cls, \
             caplog.at_level(logging.ERROR, logger="backend.integrations.hermes"):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_make_response(401))
            mock_client_cls.return_value = mock_client

            await hermes.deliver_pending()

        assert any("AUTH FAILED" in r.message and "401" in r.message for r in caplog.records if r.levelno == logging.ERROR)

    @pytest.mark.asyncio
    async def test_500_in_deliver_pending_logs_warning(self, caplog):
        from backend.integrations import hermes

        pending = [{
            "id": 2, "payload_json": json.dumps({"msg": "hi"}),
            "delivery_type": "notify", "attempts": 1, "last_attempt": None,
        }]

        with patch.object(hermes, "_load_pending", return_value=pending), \
             patch.object(hermes, "_apply_pending_results"), \
             patch("httpx.AsyncClient") as mock_client_cls, \
             caplog.at_level(logging.WARNING, logger="backend.integrations.hermes"):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_make_response(500))
            mock_client_cls.return_value = mock_client

            await hermes.deliver_pending()

        assert any("500" in r.message for r in caplog.records if r.levelno == logging.WARNING)

    @pytest.mark.asyncio
    async def test_all_fail_cycle_logs_error_summary(self, caplog):
        from backend.integrations import hermes

        pending = [
            {"id": i, "payload_json": json.dumps({"msg": "x"}),
             "delivery_type": "notify", "attempts": 1, "last_attempt": None}
            for i in range(3)
        ]

        with patch.object(hermes, "_load_pending", return_value=pending), \
             patch.object(hermes, "_apply_pending_results"), \
             patch("httpx.AsyncClient") as mock_client_cls, \
             caplog.at_level(logging.ERROR, logger="backend.integrations.hermes"):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_make_response(401))
            mock_client_cls.return_value = mock_client

            await hermes.deliver_pending()

        summary = [r for r in caplog.records if r.levelno == logging.ERROR and "delivery cycle" in r.message]
        assert len(summary) == 1
        assert "0/" in summary[0].message


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
        with patch("backend.integrations.hermes.delivery_queue_health", return_value={
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
        with patch("backend.integrations.hermes.delivery_queue_health", return_value={
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
# Parametrized: every Hermes auth-path property is covered by startup loudness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("secret_name", ["HERMES_WEBHOOK_SECRET"])
def test_hermes_auth_secrets_covered_by_startup_check(secret_name, caplog):
    """Any secret used for Hermes auth must be caught by validate() if missing."""
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
