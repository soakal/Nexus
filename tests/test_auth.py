import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock, AsyncMock

PROTECTED_ENDPOINTS = [
    ("GET", "/api/tasks/"),
    ("GET", "/api/sources/status"),
    ("GET", "/api/agents/runs"),
    ("GET", "/api/adguard/"),
    ("GET", "/api/channels/"),
    ("GET", "/api/secrets/list"),
    ("GET", "/api/unraid/"),
]


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
         patch("backend.agents.memo_watcher.start_watcher", new_callable=AsyncMock), \
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock):
        sched.running = False
        from backend.main import app
        with TestClient(app) as c:
            yield c


@pytest.mark.parametrize("method,path", PROTECTED_ENDPOINTS)
def test_no_token_returns_401(client, method, path):
    resp = client.request(method, path)
    assert resp.status_code == 401, f"{method} {path} should return 401 without token"


@pytest.mark.parametrize("method,path", PROTECTED_ENDPOINTS)
def test_wrong_token_returns_401(client, method, path):
    resp = client.request(method, path, headers={"Authorization": "Bearer wrong-key"})
    assert resp.status_code == 401
