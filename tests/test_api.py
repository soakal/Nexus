import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock, MagicMock
from pathlib import Path
from sqlmodel import SQLModel, create_engine, Session
from sqlmodel.pool import StaticPool


def make_test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def client(tmp_path, monkeypatch):
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


def test_health_ok(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_no_auth_required(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200


def test_protected_endpoint_without_token(client):
    resp = client.get("/api/tasks/")
    assert resp.status_code == 401


def test_protected_endpoint_with_token(client, auth_headers):
    resp = client.get("/api/tasks/", headers=auth_headers)
    assert resp.status_code == 200


def test_sources_status_unauthorized(client):
    resp = client.get("/api/sources/status")
    assert resp.status_code == 401


def test_briefing_latest_no_auth(client):
    resp = client.get("/api/briefing/latest")
    assert resp.status_code in (200, 404)  # No auth required
