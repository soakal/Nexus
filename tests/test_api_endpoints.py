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
    # Isolate the worker pool's boot-time DB reads (requeue_unfinished) from the
    # real on-disk nexus.db — point the module engine at the in-memory test DB.
    monkeypatch.setattr("backend.database.engine", test_engine)

    def override_session():
        with Session(test_engine) as session:
            yield session

    with patch("backend.database.create_db_and_tables"), \
         patch("backend.scheduler.setup_scheduler"), \
         patch("backend.scheduler.scheduler") as sched, \
         patch("backend.agents.memo_watcher.start_watcher_blocking"), \
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock):
        sched.running = False
        from backend.main import app
        from backend.database import get_session
        app.dependency_overrides[get_session] = override_session
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()


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
    from backend.agents.worker_pool import get_pool
    with patch.object(get_pool(), "enqueue", new_callable=AsyncMock):
        resp = app_client.post("/api/tasks/", json={"prompt": "Test task"}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data["status"] == "pending"


def test_list_tasks(app_client, auth_headers):
    resp = app_client.get("/api/tasks/", headers=auth_headers)
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

def test_weather_endpoint(app_client, auth_headers):
    with patch("backend.integrations.weather.fetch", new_callable=AsyncMock) as mock_wx:
        from backend.integrations.weather import WeatherData
        mock_wx.return_value = WeatherData(
            condition="Clear", temp_f=72.0, feels_like_f=70.0,
            high_f=78.0, low_f=65.0, precip_chance_pct=10,
            wind_mph=5.0, summary="Clear, 72°F"
        )
        resp = app_client.get("/api/weather", headers=auth_headers)
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


def test_proxmox_get(app_client, auth_headers):
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock) as mock_fetch:
        from backend.integrations.proxmox import ProxmoxData
        mock_fetch.return_value = ProxmoxData(
            node="pve", node_status="online", cpu_pct=12.5,
            mem_used_gb=8.0, mem_total_gb=32.0,
            vms=[
                {"vmid": 202, "name": "processforge", "status": "running", "type": "lxc"},
                {"vmid": 203, "name": "glp-calculator", "status": "running", "type": "lxc"},
            ],
            storage_used_gb=100.0, storage_total_gb=500.0,
        )
        resp = app_client.get("/api/proxmox/", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["node"] == "pve"
        assert len(body["vms"]) == 2
        assert body["vms"][0]["name"] == "processforge"


def test_proxmox_get_unauthorized(app_client):
    resp = app_client.get("/api/proxmox/")
    assert resp.status_code in (401, 403)


def test_proxmox_maintenance_both_ok(app_client, auth_headers):
    with patch("backend.integrations.proxmox.fetch_updates", new_callable=AsyncMock) as mock_upd, \
         patch("backend.integrations.proxmox.fetch_backups", new_callable=AsyncMock) as mock_bak:
        mock_upd.return_value = {"node": "pve", "count": 3, "packages": ["a", "b", "c"]}
        mock_bak.return_value = {"node": "pve", "status": "ok", "detail": "OK", "endtime": 100}
        resp = app_client.get("/api/proxmox/maintenance", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["updates"]["count"] == 3
    assert body["backup"]["status"] == "ok"


def test_proxmox_maintenance_both_fail_returns_200_with_nulls(app_client, auth_headers):
    with patch("backend.integrations.proxmox.fetch_updates", new_callable=AsyncMock) as mock_upd, \
         patch("backend.integrations.proxmox.fetch_backups", new_callable=AsyncMock) as mock_bak:
        mock_upd.side_effect = RuntimeError("Proxmox unavailable: down")
        mock_bak.side_effect = RuntimeError("Proxmox unavailable: down")
        resp = app_client.get("/api/proxmox/maintenance", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json() == {"updates": None, "backup": None}


def test_proxmox_maintenance_one_fails_other_still_populated(app_client, auth_headers):
    with patch("backend.integrations.proxmox.fetch_updates", new_callable=AsyncMock) as mock_upd, \
         patch("backend.integrations.proxmox.fetch_backups", new_callable=AsyncMock) as mock_bak:
        mock_upd.side_effect = RuntimeError("apt hiccup")
        mock_bak.return_value = {"node": "pve", "status": "ok", "detail": "OK", "endtime": 100}
        resp = app_client.get("/api/proxmox/maintenance", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["updates"] is None
    assert body["backup"]["status"] == "ok"


def test_proxmox_maintenance_unauthorized(app_client):
    resp = app_client.get("/api/proxmox/maintenance")
    assert resp.status_code in (401, 403)


def test_hermes_actions_execute_happy_path(app_client, auth_headers):
    from backend.safety.broker import ActionResult, Decision, Risk, Reversibility
    with patch("backend.safety.broker.execute_action", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = ActionResult(
            decision=Decision.EXECUTED, risk=Risk.HIGH,
            reversibility=Reversibility.REVERSIBLE_BY_INVERSE, log_id=1,
            result={"response": "rebooting processforge"},
        )
        resp = app_client.post(
            "/api/safety/hermes-actions/execute",
            json={"verb": "vm_action", "args": {"vm": "processforge", "action": "reboot"}},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["decision"] == "executed"
        assert body["response"] == "rebooting processforge"
        mock_exec.assert_awaited_once_with(
            actor="user", kind="hermes_action", target="hermes",
            payload={"verb": "vm_action", "args": {"vm": "processforge", "action": "reboot"}},
        )


def test_hermes_actions_execute_rejects_unknown_verb(app_client, auth_headers):
    resp = app_client.post(
        "/api/safety/hermes-actions/execute",
        json={"verb": "not_a_real_verb", "args": {}},
        headers=auth_headers,
    )
    assert resp.status_code == 400


def test_hermes_actions_execute_rejects_bad_args(app_client, auth_headers):
    resp = app_client.post(
        "/api/safety/hermes-actions/execute",
        json={"verb": "vm_action", "args": {"vm": "processforge", "action": "not_a_real_action"}},
        headers=auth_headers,
    )
    assert resp.status_code == 400


def test_hermes_actions_execute_unauthorized(app_client):
    resp = app_client.post("/api/safety/hermes-actions/execute", json={"verb": "vm_action", "args": {}})
    assert resp.status_code in (401, 403)


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

def test_hermes_trigger_briefing(app_client, auth_headers):
    with patch("backend.agents.briefing.run_briefing", new_callable=AsyncMock) as mock_briefing:
        mock_briefing.return_value = "Briefing text"
        resp = app_client.post("/api/trigger", json={"task_name": "briefing", "parameters": {}}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Today: home-state (passive glance card)
# ---------------------------------------------------------------------------

def test_today_home_state_requires_auth(app_client):
    resp = app_client.get("/api/today/home-state")
    assert resp.status_code == 401


def test_today_home_state_shape(app_client, auth_headers):
    from types import SimpleNamespace
    ha = SimpleNamespace(
        alerts=["porch light on"],
        entities=[
            {"entity_id": "lock.back_door", "state": "locked", "attributes": {"friendly_name": "Back Door"}},
            {"entity_id": "cover.garage_door", "state": "open", "attributes": {"friendly_name": "Garage"}},
        ],
    )
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=ha):
        resp = app_client.get("/api/today/home-state", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["alert_count"] == 1
    assert body["locks"] == ["Back Door=locked"]
    assert body["doors"] == ["Garage=open"]


def test_today_home_state_degrades_quietly_on_ha_failure(app_client, auth_headers):
    """A broken HA integration must never 5xx the card -- it just reports unavailable."""
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, side_effect=Exception("down")):
        resp = app_client.get("/api/today/home-state", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["locks"] == []
    assert body["doors"] == []
    assert body["alert_count"] == 0


def test_hermes_trigger_status(app_client, auth_headers):
    with patch("backend.integrations.homeassistant.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.unraid.health_check", new_callable=AsyncMock, return_value=True):
        resp = app_client.post("/api/trigger", json={"task_name": "status", "parameters": {}}, headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "ha" in body["result"]
        assert "unraid" in body["result"]


def test_hermes_trigger_unknown_task(app_client, auth_headers):
    resp = app_client.post("/api/trigger", json={"task_name": "nonexistent", "parameters": {}}, headers=auth_headers)
    assert resp.status_code == 404


def test_hermes_trigger_requires_auth(app_client):
    """/api/trigger is now Bearer-required (Tier 1.6) — no key -> 401."""
    resp = app_client.post("/api/trigger", json={"task_name": "briefing", "parameters": {}})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Safety: Hermes capabilities (pure read of the structured-verb allowlist)
# ---------------------------------------------------------------------------

def test_hermes_actions_endpoint_requires_auth(app_client):
    resp = app_client.get("/api/safety/hermes-actions")
    assert resp.status_code == 401


def test_hermes_actions_endpoint_lists_verbs(app_client, auth_headers):
    resp = app_client.get("/api/safety/hermes-actions", headers=auth_headers)
    assert resp.status_code == 200
    verbs = resp.json()["verbs"]
    assert len(verbs) > 0
    assert any(v["verb"] == "restart_service" for v in verbs)
