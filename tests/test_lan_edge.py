"""
LAN edge hardening tests — CORS allowlist + /ws/logs auth.

Mirrors the fixture pattern from tests/test_api_endpoints.py.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock
from sqlmodel import SQLModel, create_engine, Session
from sqlmodel.pool import StaticPool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def lan_client(tmp_path, monkeypatch):
    """TestClient wired up the same way as app_client in test_api_endpoints.py."""
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
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# WebSocket auth — /ws/logs
# ---------------------------------------------------------------------------

def test_ws_rejects_no_key(lan_client):
    """No key at all: server should close with policy violation before accept."""
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises((WebSocketDisconnect, Exception)):
        with lan_client.websocket_connect("/ws/logs"):
            pass  # should not reach here


def test_ws_rejects_wrong_key(lan_client):
    """Wrong key: server should close with policy violation before accept."""
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises((WebSocketDisconnect, Exception)):
        with lan_client.websocket_connect("/ws/logs?key=WRONG_KEY"):
            pass  # should not reach here


def test_ws_accepts_correct_key(lan_client):
    """Correct key: connection succeeds and the context block is entered."""
    from backend.config import get_settings
    real_key = get_settings().nexus_api_key  # "test-nexus-key" from conftest mock_secrets
    with lan_client.websocket_connect(f"/ws/logs?key={real_key}") as ws:
        # If we get here, the connection was accepted — that's the assertion.
        assert ws is not None


# ---------------------------------------------------------------------------
# CORS allowlist
# ---------------------------------------------------------------------------

def test_cors_allows_private_lan_origin(lan_client):
    """A request from a private LAN origin should get the CORS allow header."""
    resp = lan_client.get(
        "/api/health",
        headers={"Origin": "http://192.168.1.50:3000"},
    )
    # The response must either echo the allowed origin or include it in ACAO.
    assert resp.status_code == 200
    acao = resp.headers.get("access-control-allow-origin", "")
    assert acao == "http://192.168.1.50:3000", (
        f"Expected CORS header to echo private-LAN origin, got: {acao!r}"
    )


def test_cors_allows_localhost_origin(lan_client):
    """localhost is always in the allowlist."""
    resp = lan_client.get(
        "/api/health",
        headers={"Origin": "http://localhost:3000"},
    )
    assert resp.status_code == 200
    acao = resp.headers.get("access-control-allow-origin", "")
    assert acao == "http://localhost:3000", (
        f"Expected CORS header for localhost, got: {acao!r}"
    )


def test_cors_allows_tailscale_origins(lan_client):
    """Tailscale CGNAT (100.64.0.0/10) + *.ts.net MagicDNS must be allowed so remote
    access over Tailscale works (regression: the LAN-edge allowlist must not block it)."""
    for origin in (
        "http://100.101.102.103:3000",   # Tailscale CGNAT IP
        "http://100.64.0.1:8000",
        "http://mypc.tailnet-name.ts.net:3000",  # MagicDNS hostname
    ):
        resp = lan_client.get("/api/health", headers={"Origin": origin})
        assert resp.status_code == 200
        acao = resp.headers.get("access-control-allow-origin", "")
        assert acao == origin, f"Tailscale origin {origin!r} should be CORS-allowed, got {acao!r}"


def test_cors_blocks_non_tailscale_100_range(lan_client):
    """100.x OUTSIDE the 100.64-127 CGNAT band is NOT Tailscale and must stay blocked."""
    resp = lan_client.get("/api/health", headers={"Origin": "http://100.200.0.1:3000"})
    acao = resp.headers.get("access-control-allow-origin", "")
    assert acao != "http://100.200.0.1:3000"


def test_cors_blocks_public_origin(lan_client):
    """A public origin must NOT appear in access-control-allow-origin."""
    resp = lan_client.get(
        "/api/health",
        headers={"Origin": "https://evil.example.com"},
    )
    acao = resp.headers.get("access-control-allow-origin", "")
    assert acao != "https://evil.example.com", (
        f"CORS should have blocked public origin but ACAO was: {acao!r}"
    )
