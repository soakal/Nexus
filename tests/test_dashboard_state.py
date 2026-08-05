import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock

from backend import state_store


@pytest.fixture
def client(tmp_path, monkeypatch):
    vault_key = tmp_path / ".vault.key"
    vault_file = tmp_path / "nexus.vault"
    vault_key.write_bytes(b"A" * 32)
    vault_file.write_text("{}")
    monkeypatch.chdir(tmp_path)

    with patch("backend.database.create_db_and_tables"), \
         patch("backend.scheduler.setup_scheduler"), \
         patch("backend.scheduler.scheduler") as sched, \
         patch("backend.agents.memo_watcher.start_watcher_blocking"), \
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock):
        sched.running = False
        from backend.main import app
        with TestClient(app) as c:
            yield c


def test_dashboard_state_is_single_cached_read(client, auth_headers):
    """No live integration call is triggered — the endpoint only ever reads
    already-persisted snapshots."""
    state_store.store_success("source.homeassistant", {"healthy": True}, 60)
    state_store.store_success("dashboard.weather", {"temp_f": 72}, 60)

    with patch("backend.integrations.homeassistant.health_check", new_callable=AsyncMock) as hc:
        resp = client.get("/api/dashboard/state", headers=auth_headers)
        hc.assert_not_called()

    assert resp.status_code == 200
    body = resp.json()
    assert body["sources"]["homeassistant"]["healthy"] is True
    assert body["sources"]["homeassistant"]["freshness"] == "fresh"
    assert body["weather"]["data"] == {"temp_f": 72}
    assert body["weather"]["freshness"] == "fresh"


def test_dashboard_state_exposes_stale_metadata(client, auth_headers):
    """A source that has never been observed reads as never_observed, not a 500."""
    resp = client.get("/api/dashboard/state", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["sources"]["unifi"]["healthy"] is False
    assert body["sources"]["unifi"]["freshness"] == "never_observed"
    assert body["mail"]["data"] is None
    assert body["mail"]["freshness"] == "never_observed"
