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
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock), \
         patch("backend.state_workers.prime_state_workers", new_callable=AsyncMock):
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
    with patch("backend.secrets.manager.set_secret") as mock_set:
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
         patch("backend.integrations.calendar.health_check", new_callable=AsyncMock, return_value=True):
        resp = app_client.get("/api/sources/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        for name in ("homeassistant", "unifi", "unraid", "obsidian", "github",
                     "openrouter", "weather", "channels_dvr", "adguard", "calendar"):
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
         patch("backend.integrations.calendar.health_check", new_callable=AsyncMock, return_value=True):
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
         patch("backend.integrations.calendar.health_check", new_callable=AsyncMock, return_value=True):
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
# /api/trigger
# ---------------------------------------------------------------------------

def test_trigger_briefing(app_client, auth_headers):
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


def test_trigger_status(app_client, auth_headers):
    with patch("backend.integrations.homeassistant.health_check", new_callable=AsyncMock, return_value=True), \
         patch("backend.integrations.unraid.health_check", new_callable=AsyncMock, return_value=True):
        resp = app_client.post("/api/trigger", json={"task_name": "status", "parameters": {}}, headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "ha" in body["result"]
        assert "unraid" in body["result"]


def test_trigger_unknown_task(app_client, auth_headers):
    resp = app_client.post("/api/trigger", json={"task_name": "nonexistent", "parameters": {}}, headers=auth_headers)
    assert resp.status_code == 404


def test_trigger_requires_auth(app_client):
    """/api/trigger is now Bearer-required (Tier 1.6) — no key -> 401."""
    resp = app_client.post("/api/trigger", json={"task_name": "briefing", "parameters": {}})
    assert resp.status_code == 401


def test_trigger_council_postmortem(app_client, auth_headers):
    """Council-loop's run-loop.ps1 POSTs here at driver exit (Phase 2c hookup)
    -- pins the parameter plumbing that POST depends on: 'since' must reach
    run_postmortem unchanged."""
    with patch("backend.agents.council_postmortem.run_postmortem", new_callable=AsyncMock) as mock_pm:
        mock_pm.return_value = {"ok": True, "findings": []}
        resp = app_client.post(
            "/api/trigger",
            json={"task_name": "council_postmortem", "parameters": {"since": "2026-07-27T00:00:00Z"}},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["result"] == {"ok": True, "findings": []}
        mock_pm.assert_awaited_once_with(since="2026-07-27T00:00:00Z")


# ---------------------------------------------------------------------------
# Outcome flag tracker REST routes (docs/outcome-tracker-spec.md §3.3, AC17)
# ---------------------------------------------------------------------------

def test_flags_endpoints_require_auth(app_client):
    """AC17: all four Outcome Tracker routes are Bearer-gated -- no key -> 401."""
    assert app_client.get("/api/safety/flags").status_code == 401
    assert app_client.get("/api/safety/flags/calibration").status_code == 401
    assert app_client.post("/api/safety/flags", json={"check": "x", "summary": "y"}).status_code == 401
    assert app_client.post("/api/safety/flags/1/resolve", json={"status": "resolved"}).status_code == 401


def test_flags_full_lifecycle(app_client, auth_headers):
    """AC17: POST /flags creates a row and returns its id; GET /flags lists it
    newest-first with the full field shape and honors ?status=; GET
    /flags/calibration returns 200; POST /flags/{id}/resolve maps
    200 (resolved) -> 409 (already_closed) -> 404 (not_found) -> 400
    (invalid_status), matching outcomes.resolve_flag's string-return
    contract."""
    create1 = app_client.post(
        "/api/safety/flags",
        json={"check": "AC17_lifecycle_1", "summary": "first observation"},
        headers=auth_headers,
    )
    assert create1.status_code == 200
    id1 = create1.json()["id"]
    assert id1 is not None

    create2 = app_client.post(
        "/api/safety/flags",
        json={"check": "AC17_lifecycle_2", "summary": "second observation"},
        headers=auth_headers,
    )
    assert create2.status_code == 200
    id2 = create2.json()["id"]
    assert id2 is not None
    assert id2 != id1

    # GET /flags: newest first, full shape, source="manual" from the route.
    listed = app_client.get("/api/safety/flags", headers=auth_headers)
    assert listed.status_code == 200
    rows = listed.json()
    assert isinstance(rows, list)
    assert rows[0]["id"] == id2  # newest first
    row2 = rows[0]
    for key in (
        "id", "source", "check", "fingerprint", "summary", "detail", "severity",
        "status", "resolved_at", "resolved_by", "resolution_note", "deferred_until",
        "action_log_id", "surfaced_count", "last_surfaced_at", "created_at", "updated_at",
    ):
        assert key in row2
    assert row2["source"] == "manual"
    assert row2["check"] == "AC17_lifecycle_2"
    assert row2["status"] == "open"

    # ?status= filter narrows to open rows (both still open at this point).
    filtered = app_client.get("/api/safety/flags?status=open", headers=auth_headers)
    assert filtered.status_code == 200
    assert {r["id"] for r in filtered.json()} >= {id1, id2}

    # GET /flags/calibration
    calib = app_client.get("/api/safety/flags/calibration", headers=auth_headers)
    assert calib.status_code == 200
    assert isinstance(calib.json(), dict)

    # POST /flags/{id}/resolve status-mapping sequence.
    resolved = app_client.post(
        f"/api/safety/flags/{id1}/resolve", json={"status": "resolved"}, headers=auth_headers,
    )
    assert resolved.status_code == 200
    assert resolved.json() == {"id": id1, "status": "resolved"}

    already_closed = app_client.post(
        f"/api/safety/flags/{id1}/resolve", json={"status": "resolved"}, headers=auth_headers,
    )
    assert already_closed.status_code == 409

    not_found = app_client.post(
        "/api/safety/flags/999999/resolve", json={"status": "resolved"}, headers=auth_headers,
    )
    assert not_found.status_code == 404

    invalid_status = app_client.post(
        f"/api/safety/flags/{id1}/resolve", json={"status": "bogus_status"}, headers=auth_headers,
    )
    assert invalid_status.status_code == 400


# ---------------------------------------------------------------------------
# Calibration Loop REST routes (docs/calibration-loop-spec.md §4/§5.2,
# rollout §9.5 step 3 first slice): GET /flags/calibration/hints and
# POST /flags/calibration/{fingerprint}/override.
# ---------------------------------------------------------------------------

def test_calibration_hints_and_override_routes_require_auth(app_client):
    """Both new calibration routes are Bearer-gated -- no key -> 401."""
    assert app_client.get("/api/safety/flags/calibration/hints").status_code == 401
    assert app_client.post(
        "/api/safety/flags/calibration/homelab_watch:garage_open/override",
        json={"active": True},
    ).status_code == 401


def test_calibration_override_lifecycle_and_note_validation(app_client, auth_headers):
    """POST .../override applies a manual suppress (200, status="active"),
    rejects a non-string/over-length note (400, the Security auto-fix), then
    un-suppresses (200, status="overridden_off"); a malformed fingerprint is
    400 and an active=False against a never-suppressed fingerprint is 404.
    GET .../hints then reflects the applied override in its "overridden"
    group."""
    fp = "homelab_watch:AC_calibration_override"

    # Bad note: non-string.
    bad_note_type = app_client.post(
        f"/api/safety/flags/calibration/{fp}/override",
        json={"active": True, "note": 12345},
        headers=auth_headers,
    )
    assert bad_note_type.status_code == 400

    # Bad note: over 1000 chars.
    bad_note_len = app_client.post(
        f"/api/safety/flags/calibration/{fp}/override",
        json={"active": True, "note": "x" * 1001},
        headers=auth_headers,
    )
    assert bad_note_len.status_code == 400

    # Malformed fingerprint (no ':').
    malformed = app_client.post(
        "/api/safety/flags/calibration/no_colon_here/override",
        json={"active": True},
        headers=auth_headers,
    )
    assert malformed.status_code == 400

    # active missing/non-boolean.
    bad_active = app_client.post(
        f"/api/safety/flags/calibration/{fp}/override",
        json={"active": "yes"},
        headers=auth_headers,
    )
    assert bad_active.status_code == 400

    # Valid manual suppress.
    suppress = app_client.post(
        f"/api/safety/flags/calibration/{fp}/override",
        json={"active": True, "note": "manually suppressed for test"},
        headers=auth_headers,
    )
    assert suppress.status_code == 200
    # The route's "status" field echoes set_override's applied/not_found/
    # invalid return contract, not the persisted CalibrationHint.status.
    assert suppress.json() == {"fingerprint": fp, "active": True, "status": "applied"}

    # Un-suppress the same fingerprint.
    unsuppress = app_client.post(
        f"/api/safety/flags/calibration/{fp}/override",
        json={"active": False},
        headers=auth_headers,
    )
    assert unsuppress.status_code == 200
    assert unsuppress.json() == {"fingerprint": fp, "active": False, "status": "applied"}

    # active=False against a fingerprint with no hint row at all -> 404.
    not_found = app_client.post(
        "/api/safety/flags/calibration/homelab_watch:AC_never_suppressed/override",
        json={"active": False},
        headers=auth_headers,
    )
    assert not_found.status_code == 404

    # GET /flags/calibration/hints reflects the override in "overridden".
    hints = app_client.get("/api/safety/flags/calibration/hints", headers=auth_headers)
    assert hints.status_code == 200
    body = hints.json()
    assert fp in {row["fingerprint"] for row in body["overridden"]}


def test_flags_resolve_deferred_valid_defer_days(app_client, auth_headers):
    """POST /flags/{id}/resolve status="deferred" with a valid int defer_days
    resolves 200 and sets OutcomeFlag.deferred_until (outcomes.resolve_flag ->
    now + timedelta(days=defer_days))."""
    create = app_client.post(
        "/api/safety/flags",
        json={"check": "AC17_defer", "summary": "defer me"},
        headers=auth_headers,
    )
    assert create.status_code == 200
    flag_id = create.json()["id"]

    deferred = app_client.post(
        f"/api/safety/flags/{flag_id}/resolve",
        json={"status": "deferred", "defer_days": 7},
        headers=auth_headers,
    )
    assert deferred.status_code == 200
    assert deferred.json() == {"id": flag_id, "status": "deferred"}

    listed = app_client.get("/api/safety/flags?status=deferred", headers=auth_headers)
    assert listed.status_code == 200
    row = next(r for r in listed.json() if r["id"] == flag_id)
    assert row["deferred_until"] is not None


def test_flags_resolve_deferred_rejects_string_defer_days(app_client, auth_headers):
    """POST /flags/{id}/resolve status="deferred" with defer_days as a JSON
    string (e.g. "7") must 400 before it ever reaches outcomes.resolve_flag's
    `now + timedelta(days=defer_days)` call, not 500."""
    create = app_client.post(
        "/api/safety/flags",
        json={"check": "AC17_defer_bad", "summary": "defer me badly"},
        headers=auth_headers,
    )
    assert create.status_code == 200
    flag_id = create.json()["id"]

    bad = app_client.post(
        f"/api/safety/flags/{flag_id}/resolve",
        json={"status": "deferred", "defer_days": "7"},
        headers=auth_headers,
    )
    assert bad.status_code == 400
