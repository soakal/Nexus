"""Tests for the HA unavailable-entity triage API
(backend/api/homeassistant.py):

- GET  /api/ha/unavailable        -- age-bucketed report + per-item resolved_name
- POST /api/ha/unavailable/{id}/suppress    -- "known-dead, stop counting"
- POST /api/ha/unavailable/{id}/unsuppress  -- undo

Follows tests/test_ha_broker_gating.py's `ha_client` fixture pattern (isolated
in-memory DB + TestClient), since this endpoint lives in the same router.
"""
from datetime import datetime, timedelta

import pytest
from unittest.mock import AsyncMock, patch
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool
from fastapi.testclient import TestClient

import backend.database  # noqa: F401 — registers all tables on metadata


def make_test_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def ha_client(tmp_path, monkeypatch):
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
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock), \
         patch("backend.state_workers.prime_state_workers", new_callable=AsyncMock):
        sched.running = False
        from backend.main import app
        from backend.database import get_session
        app.dependency_overrides[get_session] = override_session
        with TestClient(app) as c:
            yield c, test_engine
        app.dependency_overrides.clear()


def _seed_unavailable(eng, entity_id, days_ago=0, hours_ago=0, minutes_ago=0, suppressed=False):
    from backend.database import HaEntityUnavailable
    now = datetime.utcnow()
    with Session(eng) as s:
        s.add(HaEntityUnavailable(
            entity_id=entity_id,
            first_seen_unavailable_at=now - timedelta(days=days_ago, hours=hours_ago, minutes=minutes_ago),
            last_checked_at=now,
            suppressed=suppressed,
        ))
        s.commit()


def test_get_unavailable_reports_buckets_and_items(ha_client, auth_headers):
    client, eng = ha_client
    _seed_unavailable(eng, "sensor.persistent_dead", days_ago=30)
    _seed_unavailable(eng, "light.just_restarted", minutes_ago=2)

    with patch(
        "backend.integrations.homeassistant.fetch",
        new_callable=AsyncMock,
        return_value=type("HAData", (), {"entities": []})(),
    ):
        resp = client.get("/api/ha/unavailable", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["persistent"] == 1
    assert body["recent"] == 1
    ids = {i["entity_id"] for i in body["items"]}
    assert ids == {"sensor.persistent_dead", "light.just_restarted"}


def test_get_unavailable_unauthorized(ha_client):
    client, _eng = ha_client
    resp = client.get("/api/ha/unavailable")
    assert resp.status_code == 401 or resp.status_code == 403


def test_suppress_then_unsuppress_round_trip(ha_client, auth_headers):
    client, eng = ha_client
    _seed_unavailable(eng, "sensor.orphan_zigbee", days_ago=60)

    resp = client.post("/api/ha/unavailable/sensor.orphan_zigbee/suppress", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    from backend.database import HaEntityUnavailable
    with Session(eng) as s:
        row = s.get(HaEntityUnavailable, "sensor.orphan_zigbee")
        assert row.suppressed is True

    resp2 = client.post("/api/ha/unavailable/sensor.orphan_zigbee/unsuppress", headers=auth_headers)
    assert resp2.status_code == 200
    with Session(eng) as s:
        row = s.get(HaEntityUnavailable, "sensor.orphan_zigbee")
        assert row.suppressed is False


def test_suppress_unknown_entity_404s(ha_client, auth_headers):
    client, _eng = ha_client
    resp = client.post("/api/ha/unavailable/sensor.never_seen/suppress", headers=auth_headers)
    assert resp.status_code == 404


def test_get_unavailable_resolves_device_tracker_name_from_known_device(ha_client, auth_headers):
    """Bonus MAC cross-check: a device_tracker.* entity whose entity_id
    embeds a MAC already known to UniFi's KnownDevice table gets a
    resolved_name in the report."""
    client, eng = ha_client
    _seed_unavailable(eng, "device_tracker.aa_bb_cc_dd_ee_ff", hours_ago=2)

    from backend.database import KnownDevice
    with Session(eng) as s:
        s.add(KnownDevice(mac="aa:bb:cc:dd:ee:ff", hostname="brians-phone"))
        s.commit()

    ha_data = type("HAData", (), {"entities": [
        {"entity_id": "device_tracker.aa_bb_cc_dd_ee_ff", "state": "unavailable", "attributes": {}},
    ]})()
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=ha_data):
        resp = client.get("/api/ha/unavailable", headers=auth_headers)

    assert resp.status_code == 200
    items = {i["entity_id"]: i for i in resp.json()["items"]}
    assert items["device_tracker.aa_bb_cc_dd_ee_ff"]["resolved_name"] == "brians-phone"


def test_get_unavailable_resolved_name_none_when_mac_unknown(ha_client, auth_headers):
    client, eng = ha_client
    _seed_unavailable(eng, "device_tracker.11_22_33_44_55_66", hours_ago=1)

    ha_data = type("HAData", (), {"entities": [
        {"entity_id": "device_tracker.11_22_33_44_55_66", "state": "unavailable", "attributes": {}},
    ]})()
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=ha_data):
        resp = client.get("/api/ha/unavailable", headers=auth_headers)

    items = {i["entity_id"]: i for i in resp.json()["items"]}
    assert items["device_tracker.11_22_33_44_55_66"]["resolved_name"] is None


def test_get_unavailable_non_device_tracker_never_resolved(ha_client, auth_headers):
    client, eng = ha_client
    _seed_unavailable(eng, "sensor.some_zigbee_thing", hours_ago=1)

    ha_data = type("HAData", (), {"entities": []})()
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=ha_data):
        resp = client.get("/api/ha/unavailable", headers=auth_headers)

    items = {i["entity_id"]: i for i in resp.json()["items"]}
    assert items["sensor.some_zigbee_thing"]["resolved_name"] is None
