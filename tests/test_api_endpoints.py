import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock, MagicMock
from sqlmodel import SQLModel, create_engine, Session
from sqlmodel.pool import StaticPool
from datetime import datetime


def make_test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    vault_key = tmp_path / ".vault.key"
    vault_file = tmp_path / "nexus.vault"
    vault_key.write_bytes(b"A" * 32)
    vault_file.write_text("{}")
    monkeypatch.chdir(tmp_path)

    test_engine = make_test_engine()

    def override_session():
        with Session(test_engine) as session:
            yield session

    with patch("backend.database.create_db_and_tables"), \
         patch("backend.scheduler.setup_scheduler"), \
         patch("backend.scheduler.scheduler") as sched, \
         patch("backend.agents.memo_watcher.start_watcher", new_callable=AsyncMock), \
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock):
        sched.running = False
        from backend.main import app
        from backend.database import get_session
        app.dependency_overrides[get_session] = override_session
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------

def test_trends_endpoint_empty(app_client, auth_headers):
    """Trend endpoint with no stored snapshots returns empty data list."""
    resp = app_client.get("/api/trends/unraid/storage_used_gb?days=7", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "unraid"
    assert data["metric"] == "storage_used_gb"
    assert data["data"] == []
    assert data["projection"] is None


def test_trends_endpoint_requires_auth(app_client):
    resp = app_client.get("/api/trends/unraid/storage_used_gb")
    assert resp.status_code == 401


def test_trends_endpoint_with_snapshots(app_client, auth_headers):
    """When snapshots exist, projection must be populated."""
    from backend.database import TrendSnapshot
    from sqlmodel import Session

    # Seed some snapshots directly into the test DB by going via the endpoint
    # override — patch the DB query instead for isolation
    from datetime import timedelta

    now = datetime.utcnow()
    snapshots = [
        TrendSnapshot(source="unraid", metric="storage_used_gb",
                      value=float(100 + i * 10),
                      captured_at=now - timedelta(days=7 - i))
        for i in range(8)
    ]

    with patch("backend.api.trends.get_session"):
        with patch("backend.api.trends.select"), \
             patch("sqlmodel.Session") as mock_sess_cls:
            mock_sess = MagicMock()
            mock_sess.__enter__ = MagicMock(return_value=mock_sess)
            mock_sess.__exit__ = MagicMock(return_value=False)
            mock_sess.exec.return_value.all.return_value = snapshots
            mock_sess_cls.return_value = mock_sess

            # Call the endpoint logic directly to verify projection math
            from backend.api.trends import get_trend
            import asyncio

            result = asyncio.get_event_loop().run_until_complete(
                get_trend("unraid", "storage_used_gb", days=7, _=None, session=mock_sess)
            )
            assert result["source"] == "unraid"
            assert len(result["data"]) == 8
            assert result["projection"] is not None
            assert len(result["projection"]) == 14


def test_trends_different_sources(app_client, auth_headers):
    """Different source/metric combinations all return the same response shape."""
    for source, metric in [("channels", "recordings"), ("adguard", "blocked_pct")]:
        resp = app_client.get(f"/api/trends/{source}/{metric}", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == source
        assert body["metric"] == metric


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def test_secrets_list(app_client, auth_headers):
    resp = app_client.get("/api/secrets/list", headers=auth_headers)
    assert resp.status_code == 200
    assert "keys" in resp.json()


def test_secrets_list_unauthorized(app_client):
    resp = app_client.get("/api/secrets/list")
    assert resp.status_code == 401


def test_secrets_set(app_client, auth_headers):
    with patch("backend.secrets.vault.set_secret") as mock_set:
        resp = app_client.post(
            "/api/secrets/set",
            json={"key": "TEST_KEY", "value": "test_value"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_set.assert_called_once_with("TEST_KEY", "test_value")


def test_secrets_set_unauthorized(app_client):
    resp = app_client.post("/api/secrets/set", json={"key": "K", "value": "v"})
    assert resp.status_code == 401


def test_secrets_test_endpoint_success(app_client, auth_headers):
    with patch("backend.api.secrets._run_test", new_callable=AsyncMock) as mock_test:
        mock_test.return_value = (True, None)
        resp = app_client.post("/api/secrets/test/ANTHROPIC_API_KEY", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "latency_ms" in data
        assert data["error"] is None


def test_secrets_test_endpoint_failure(app_client, auth_headers):
    with patch("backend.api.secrets._run_test", new_callable=AsyncMock) as mock_test:
        mock_test.return_value = (False, "bad credentials")
        resp = app_client.post("/api/secrets/test/GITHUB_TOKEN", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["error"] == "bad credentials"


def test_secrets_test_endpoint_exception(app_client, auth_headers):
    with patch("backend.api.secrets._run_test", new_callable=AsyncMock) as mock_test:
        mock_test.side_effect = Exception("vault locked")
        resp = app_client.post("/api/secrets/test/HASS_TOKEN", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "vault locked" in data["error"]


def test_secrets_test_unknown_key_returns_ok(app_client, auth_headers):
    """An unknown secret key has no test function so _run_test returns (True, None)."""
    with patch("backend.api.secrets._run_test", new_callable=AsyncMock) as mock_test:
        mock_test.return_value = (True, None)
        resp = app_client.post("/api/secrets/test/UNKNOWN_KEY", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def test_sources_status_all_healthy(app_client, auth_headers):
    with patch("backend.integrations.homeassistant.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.unifi.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.unraid.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.obsidian.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.github.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.openrouter.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.weather.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.channels_dvr.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.adguard.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.hermes.health_check", new_callable=AsyncMock, return_value=True):
        resp = app_client.get("/api/sources/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        for name in ("homeassistant", "unifi", "unraid", "obsidian", "github",
                     "openrouter", "weather", "channels_dvr", "adguard", "hermes"):
            assert name in data
            assert data[name]["healthy"] is True
            assert "last_checked" in data[name]


def test_sources_status_some_unhealthy(app_client, auth_headers):
    with patch("backend.integrations.homeassistant.health_check", new_callable=AsyncMock, return_value=False), \
         patch("backend.integrations.unifi.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.unraid.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.obsidian.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.github.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.openrouter.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.weather.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.channels_dvr.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.adguard.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.hermes.health_check", new_callable=AsyncMock, return_value=True):
        resp = app_client.get("/api/sources/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["homeassistant"]["healthy"] is False


def test_sources_status_exception_is_unhealthy(app_client, auth_headers):
    """An exception from a health_check is treated as unhealthy (not a 500)."""
    with patch("backend.integrations.homeassistant.health_check", new_callable=AsyncMock, side_effect=Exception("boom")), \
         patch("backend.integrations.unifi.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.unraid.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.obsidian.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.github.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.openrouter.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.weather.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.channels_dvr.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.adguard.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.hermes.health_check", new_callable=AsyncMock, return_value=True):
        resp = app_client.get("/api/sources/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["homeassistant"]["healthy"] is False


def test_sources_status_unauthorized(app_client):
    resp = app_client.get("/api/sources/status")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def test_create_task(app_client, auth_headers):
    with patch("backend.api.tasks.asyncio.create_task"):
        resp = app_client.post("/api/tasks/", json={"prompt": "Test task"}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data["status"] == "running"


def test_list_tasks(app_client, auth_headers):
    resp = app_client.get("/api/tasks/", headers=auth_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_task_not_found(app_client, auth_headers):
    resp = app_client.get("/api/tasks/9999", headers=auth_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Briefing
# ---------------------------------------------------------------------------

def test_list_briefings(app_client, auth_headers):
    resp = app_client.get("/api/briefing/", headers=auth_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# Agent runs
# ---------------------------------------------------------------------------

def test_agent_runs_empty(app_client, auth_headers):
    resp = app_client.get("/api/agents/runs", headers=auth_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_agent_runs_search(app_client, auth_headers):
    resp = app_client.get("/api/agents/runs?q=test", headers=auth_headers)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------

def test_weather_endpoint(app_client):
    with patch("backend.integrations.weather.fetch", new_callable=AsyncMock) as mock_wx:
        from backend.integrations.weather import WeatherData
        mock_wx.return_value = WeatherData(
            condition="Clear", temp_f=72.0, feels_like_f=70.0,
            high_f=78.0, low_f=65.0, precip_chance_pct=10,
            wind_mph=5.0, summary="Clear, 72°F"
        )
        resp = app_client.get("/api/weather")
        assert resp.status_code == 200
        data = resp.json()
        assert data["condition"] == "Clear"


# ---------------------------------------------------------------------------
# AdGuard
# ---------------------------------------------------------------------------

def test_adguard_get(app_client, auth_headers):
    with patch("backend.integrations.adguard.fetch", new_callable=AsyncMock) as mock_fetch:
        from backend.integrations.adguard import AdGuardData
        mock_fetch.return_value = AdGuardData(
            queries_today=1000, blocked_today=234, blocked_pct=23.4, filtering_enabled=True
        )
        resp = app_client.get("/api/adguard/", headers=auth_headers)
        assert resp.status_code == 200


def test_adguard_toggle(app_client, auth_headers):
    with patch("backend.integrations.adguard.set_filtering", new_callable=AsyncMock):
        resp = app_client.post("/api/adguard/filter", json={"enabled": False}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Channels DVR
# ---------------------------------------------------------------------------

def test_channels_get(app_client, auth_headers):
    with patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock) as mock_fetch:
        from backend.integrations.channels_dvr import ChannelsData
        mock_fetch.return_value = ChannelsData()
        resp = app_client.get("/api/channels/", headers=auth_headers)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Unraid API
# ---------------------------------------------------------------------------

def test_unraid_get(app_client, auth_headers):
    with patch("backend.integrations.unraid.fetch", new_callable=AsyncMock) as mock_fetch:
        from backend.integrations.unraid import UnraidData
        mock_fetch.return_value = UnraidData()
        resp = app_client.get("/api/unraid/", headers=auth_headers)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Hermes trigger
# ---------------------------------------------------------------------------

def test_hermes_trigger_briefing(app_client):
    with patch("backend.agents.briefing.run_briefing", new_callable=AsyncMock) as mock_briefing:
        mock_briefing.return_value = "Briefing text"
        resp = app_client.post("/api/trigger", json={"task_name": "briefing", "parameters": {}})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


def test_hermes_trigger_status(app_client):
    with patch("backend.integrations.homeassistant.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.unraid.health_check", new_callable=AsyncMock, return_value=True):
        resp = app_client.post("/api/trigger", json={"task_name": "status", "parameters": {}})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "ha" in body["result"]
        assert "unraid" in body["result"]


def test_hermes_trigger_unknown_task(app_client):
    resp = app_client.post("/api/trigger", json={"task_name": "nonexistent", "parameters": {}})
    assert resp.status_code == 404
