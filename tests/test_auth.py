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


@pytest.mark.asyncio
async def test_non_ascii_token_returns_401_not_500():
    # compare_digest on str raises TypeError for non-ASCII — must be a clean
    # 401, never an unhandled 500. Call the dependency directly (the HTTP test
    # client refuses to even send a non-ASCII header).
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials
    from backend.auth import require_api_key

    fake = MagicMock()
    fake.nexus_api_key = "test-key-123"
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="tokené")
    with patch("backend.config.get_settings", return_value=fake):
        with pytest.raises(HTTPException) as exc:
            await require_api_key(creds)
    assert exc.value.status_code == 401


def test_empty_expected_key_returns_401(client):
    # An unset/empty configured key must reject every bearer, never crash.
    fake = MagicMock()
    fake.nexus_api_key = ""
    with patch("backend.config.get_settings", return_value=fake):
        resp = client.get(
            "/api/secrets/list", headers={"Authorization": "Bearer anything"}
        )
    assert resp.status_code == 401


def test_401_records_an_auth_failure(client):
    from backend.safety import authfail
    resp = client.get("/api/sources/status")
    assert resp.status_code == 401
    stats = authfail.recent(600)
    assert any(v["count"] >= 1 for v in stats.values())
    assert any("/api/sources/status" in [p for p, _ in v["paths"]] for v in stats.values())


def test_success_records_nothing(client):
    from backend.safety import authfail
    fake = MagicMock()
    fake.nexus_api_key = "test-key-123"
    with patch("backend.config.get_settings", return_value=fake):
        resp = client.get(
            "/api/secrets/list", headers={"Authorization": "Bearer test-key-123"}
        )
    assert resp.status_code != 401
    assert authfail.recent(600) == {}


@pytest.mark.asyncio
async def test_direct_call_signature_still_works():
    # Guards the request=None default: calling require_api_key with a single
    # positional arg (no Request) must keep working, matching the call shape
    # used elsewhere in this file (test_non_ascii_token_returns_401_not_500).
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials
    from backend.auth import require_api_key

    fake = MagicMock()
    fake.nexus_api_key = "test-key-123"
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong")
    with patch("backend.config.get_settings", return_value=fake):
        with pytest.raises(HTTPException) as exc:
            await require_api_key(creds)
    assert exc.value.status_code == 401


def test_counter_failure_does_not_break_401(client):
    with patch("backend.safety.authfail.record_failure", side_effect=RuntimeError("boom")):
        resp = client.get("/api/sources/status")
    assert resp.status_code == 401


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    """Minimal stand-in for FastAPI's Request — _client_source/_client_path
    only touch .client.host, .headers, and .url.path, so a real TestClient
    round-trip (which can't set a custom peer address in this Starlette
    version) isn't needed to exercise them directly."""

    def __init__(self, *, peer=None, headers=None, path="/x"):
        self.client = _FakeClient(peer) if peer else None
        self.headers = headers or {}

        class _URL:
            pass
        self.url = _URL()
        self.url.path = path


def test_forwarded_for_is_preferred_over_peer():
    from backend.auth import _client_source
    req = _FakeRequest(
        peer="127.0.0.1",  # loopback, i.e. arrived via a trusted local proxy
        headers={"x-forwarded-for": "10.1.2.3, 127.0.0.1"},
    )
    assert _client_source(req) == "10.1.2.3"


def test_forwarded_for_ignored_when_peer_not_loopback():
    # The actual fix for Bug 2: XFF must NOT be trusted from an arbitrary
    # (non-loopback) peer, or any caller could spoof it to evade detection,
    # evict a real offender from the bounded table, or misattribute the alert.
    from backend.auth import _client_source
    req = _FakeRequest(
        peer="198.51.100.5",  # RFC 5737 TEST-NET-2, not a real host
        headers={"x-forwarded-for": "1.2.3.4"},
    )
    assert _client_source(req) == "198.51.100.5"


def test_source_is_charset_sanitised():
    from backend.auth import _client_source
    req = _FakeRequest(peer="127.0.0.1", headers={"x-forwarded-for": "<b>evil</b>"})
    source = _client_source(req)
    assert "<" not in source and ">" not in source
    assert len(source) <= 45


def test_client_path_is_html_escaped():
    # Bug 1 fix: an attacker-controlled path renders into an HTML-parse-mode
    # Telegram message (via the auth-burst watchdog alert) — unescaped, a
    # crafted path could inject a live link, or malformed HTML could fail the
    # send outright and suppress the alert.
    from backend.auth import _client_path
    req = _FakeRequest(path='/<a href="http://evil">pwn</a>')
    escaped = _client_path(req)
    assert "<a" not in escaped
    assert "&lt;a" in escaped
