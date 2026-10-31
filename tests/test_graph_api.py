from unittest.mock import AsyncMock, patch

import pytest

AUTH = {"Authorization": "Bearer test-nexus-key"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / ".vault.key").write_bytes(b"A" * 32)
    (tmp_path / "nexus.vault").write_text("{}")
    monkeypatch.chdir(tmp_path)
    with patch("backend.database.create_db_and_tables"), \
         patch("backend.scheduler.setup_scheduler"), \
         patch("backend.scheduler.scheduler") as sched, \
         patch("backend.agents.memo_watcher.start_watcher_blocking"), \
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock), \
         patch("backend.state_workers.prime_state_workers", new_callable=AsyncMock):
        sched.running = False
        from fastapi.testclient import TestClient

        from backend.main import app

        with TestClient(app) as c:
            yield c


def test_health_requires_auth(client):
    assert client.get("/api/graph/health").status_code in (401, 403)


def test_health_disabled_by_default(client):
    r = client.get("/api/graph/health", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert "jobs" in body and "nodes" in body
