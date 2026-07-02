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
         patch("backend.agents.memo_watcher.start_watcher_blocking"), \
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


def test_correct_token_returns_non_401(client):
    # Proves the constant-time compare accepts the real key.
    fake = MagicMock()
    fake.nexus_api_key = "test-key-123"
    with patch("backend.config.get_settings", return_value=fake):
        resp = client.get(
            "/api/secrets/list", headers={"Authorization": "Bearer test-key-123"}
        )
    assert resp.status_code != 401


def test_empty_expected_key_returns_401(client):
    # An unset/empty configured key must reject every bearer, never crash.
    fake = MagicMock()
    fake.nexus_api_key = ""
    with patch("backend.config.get_settings", return_value=fake):
        resp = client.get(
            "/api/secrets/list", headers={"Authorization": "Bearer anything"}
        )
    assert resp.status_code == 401
