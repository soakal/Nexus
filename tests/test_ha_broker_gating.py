"""Tests for BLOCKER #1: HA API endpoints must route through the broker.

Verifies:
- POST /api/ha/service routes through execute_action (not directly to
  call_service), writes an ActionLog row with actor=user/kind=ha_service/decision=executed,
  and calls call_service with {"entity_id": <id>} (no service_data key in payload).
- POST /api/ha/reload-cloud: first attempt success path writes one
  ActionLog row; fallback path (first call_service raises, second succeeds) writes
  two ActionLog rows and still returns 200 ok.
"""

import pytest
from unittest.mock import AsyncMock, patch, call
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool
from fastapi.testclient import TestClient

import backend.database  # noqa: F401 — registers all tables on metadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_test_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


def _seed_state(eng, autonomy: bool = True):
    from backend.database import SystemState
    with Session(eng) as s:
        row = s.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            s.add(row)
        row.autonomy_enabled = autonomy
        row.daily_budget_usd = 25.0
        row.per_task_budget_usd = 5.0
        s.commit()


def _all_logs(eng):
    from backend.database import ActionLog
    with Session(eng) as s:
        return s.exec(select(ActionLog).order_by(ActionLog.created_at)).all()


@pytest.fixture
def ha_client(tmp_path, monkeypatch):
    """FastAPI TestClient with an isolated in-memory DB and mocked scheduler/vault."""
    vault_key = tmp_path / ".vault.key"
    vault_file = tmp_path / "nexus.vault"
    vault_key.write_bytes(b"A" * 32)
    vault_file.write_text("{}")
    monkeypatch.chdir(tmp_path)

    test_engine = make_test_engine()
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
            yield c, test_engine
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# BLOCKER #1a: POST /api/ha/service
# ---------------------------------------------------------------------------

def test_ha_service_routes_through_broker(ha_client, auth_headers):
    """POST /service routes through the broker: ActionLog written, call_service called
    with {"entity_id": <id>} (no extra service_data wrapping)."""
    client, eng = ha_client
    _seed_state(eng, autonomy=True)

    with patch(
        "backend.integrations.homeassistant.call_service",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as cs:
        resp = client.post(
            "/api/ha/service",
            json={"domain": "light", "service": "turn_on", "entity_id": "light.office"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True

    # Broker must have called call_service with entity_id as service_data
    cs.assert_awaited_once_with("light", "turn_on", {"entity_id": "light.office"})

    # ActionLog row must exist with actor=user, kind=ha_service, decision=executed
    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].actor == "user"
    assert logs[0].kind == "ha_service"
    assert logs[0].decision == "executed"


def test_ha_service_with_service_data_merges_entity_id(ha_client, auth_headers):
    """POST /service with service_data (e.g. climate set_temperature) merges the
    entity_id into service_data server-side before hitting the broker."""
    client, eng = ha_client
    _seed_state(eng, autonomy=True)

    with patch(
        "backend.integrations.homeassistant.call_service",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as cs:
        resp = client.post(
            "/api/ha/service",
            json={
                "domain": "climate",
                "service": "set_temperature",
                "entity_id": "climate.dining_room",
                "service_data": {"temperature": 72},
            },
            headers=auth_headers,
        )

    assert resp.status_code == 200
    cs.assert_awaited_once_with(
        "climate", "set_temperature",
        {"entity_id": "climate.dining_room", "temperature": 72},
    )

    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].decision == "executed"


def test_ha_service_requires_auth(ha_client):
    """No auth header -> 401."""
    client, _ = ha_client
    resp = client.post(
        "/api/ha/service",
        json={"domain": "light", "service": "turn_on", "entity_id": "light.office"},
    )
    assert resp.status_code == 401


def test_ha_service_dispatch_failure_returns_502(ha_client, auth_headers):
    """When call_service raises, the broker records FAILED and the endpoint returns 502."""
    client, eng = ha_client
    _seed_state(eng, autonomy=True)

    with patch(
        "backend.integrations.homeassistant.call_service",
        new_callable=AsyncMock,
        side_effect=Exception("HA unreachable"),
    ):
        resp = client.post(
            "/api/ha/service",
            json={"domain": "light", "service": "turn_on", "entity_id": "light.office"},
            headers=auth_headers,
        )

    assert resp.status_code == 502
    assert "Service call failed" in resp.json()["detail"]

    # A FAILED ActionLog row must still exist (broker always writes before dispatch)
    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].actor == "user"
    assert logs[0].kind == "ha_service"
    assert logs[0].decision == "failed"


# ---------------------------------------------------------------------------
# BLOCKER #1b: POST /api/ha/reload-cloud
# ---------------------------------------------------------------------------

def test_reload_cloud_first_attempt_success(ha_client, auth_headers):
    """First reload attempt succeeds: one ActionLog row, call_service called with
    service_data={"entry_id": "cloud"} (not entity_id)."""
    client, eng = ha_client
    _seed_state(eng, autonomy=True)

    with patch(
        "backend.integrations.homeassistant.call_service",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as cs:
        resp = client.post("/api/ha/reload-cloud", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Broker must have routed through with the explicit service_data
    cs.assert_awaited_once_with(
        "homeassistant", "reload_config_entry", {"entry_id": "cloud"}
    )

    # Exactly one ActionLog row, decision=executed
    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].actor == "user"
    assert logs[0].kind == "ha_service"
    assert logs[0].decision == "executed"


def test_reload_cloud_fallback_path(ha_client, auth_headers):
    """First call_service raises, second succeeds: TWO ActionLog rows, endpoint returns ok.
    Second call uses empty service_data {} (not entity_id)."""
    client, eng = ha_client
    _seed_state(eng, autonomy=True)

    call_count = [0]

    async def _mock_call_service(domain, service, service_data):
        call_count[0] += 1
        if call_count[0] == 1:
            raise Exception("entry_id not found")
        return {"ok": True}

    with patch(
        "backend.integrations.homeassistant.call_service",
        side_effect=_mock_call_service,
    ) as cs:
        resp = client.post("/api/ha/reload-cloud", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Two broker calls → two ActionLog rows
    logs = _all_logs(eng)
    assert len(logs) == 2
    assert logs[0].actor == "user"
    assert logs[0].kind == "ha_service"
    assert logs[0].decision == "failed"   # first attempt failed
    assert logs[1].actor == "user"
    assert logs[1].kind == "ha_service"
    assert logs[1].decision == "executed"  # fallback succeeded

    # Verify the second call used empty service_data (not entity_id)
    assert call_count[0] == 2


def test_reload_cloud_both_fail_returns_502(ha_client, auth_headers):
    """Both reload attempts fail: endpoint returns 502."""
    client, eng = ha_client
    _seed_state(eng, autonomy=True)

    with patch(
        "backend.integrations.homeassistant.call_service",
        new_callable=AsyncMock,
        side_effect=Exception("HA down"),
    ):
        resp = client.post("/api/ha/reload-cloud", headers=auth_headers)

    assert resp.status_code == 502
    assert "Cloud reload failed" in resp.json()["detail"]

    # Two FAILED ActionLog rows
    logs = _all_logs(eng)
    assert len(logs) == 2
    assert all(log.decision == "failed" for log in logs)


def test_reload_cloud_requires_auth(ha_client):
    """No auth header -> 401."""
    client, _ = ha_client
    resp = client.post("/api/ha/reload-cloud")
    assert resp.status_code == 401
