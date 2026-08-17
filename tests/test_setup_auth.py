import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A genuinely-pending first-run setup: no .vault.key/nexus.vault on disk
    (vault_ok False, matching the real first-run state before
    /var/lib/nexus/.vault.key is seeded — see docs/lxc-migration-spec.md
    Phase 1.1-1.4), so the
    lifespan's `if vault_ok:` branch (settings.validate(), scheduler, memo
    watcher, worker pool, telegram poller) never runs — only
    ensure_bootstrap_token() (unconditional) and the router registrations do.

    The autouse `mock_secrets` fixture in tests/conftest.py makes NEXUS_API_KEY
    resolve successfully for every test by default, which would make
    _needs_setup() always False and defeat this file's entire premise —
    overridden here with a real, mutable in-memory store scoped to this
    fixture (innermost patch wins, same pattern conftest.py documents),
    starting EMPTY so setup is genuinely pending. setup.py's own get_secret/
    set_secret calls read and write into it for real.
    """
    monkeypatch.chdir(tmp_path)

    store: dict[str, str] = {}

    def _fake_get_secret(key, fallback_env=True):
        if key in store:
            return store[key]
        raise KeyError(f"Secret '{key}' not configured yet")

    def _fake_set_secret(key, value):
        store[key] = value

    monkeypatch.setattr("backend.secrets.manager.get_secret", _fake_get_secret)
    monkeypatch.setattr("backend.secrets.manager.set_secret", _fake_set_secret)

    with patch("backend.database.create_db_and_tables"):
        from backend.main import app
        with TestClient(app) as c:
            yield c


def _body(anthropic_key="sk-ant-test-key"):
    return {"anthropic_api_key": anthropic_key, "secrets": {}}


def test_no_header_returns_401_and_leaves_setup_pending(client):
    resp = client.post("/api/setup/complete", json=_body())
    assert resp.status_code == 401

    status_resp = client.get("/api/setup/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["needs_setup"] is True


def test_wrong_token_returns_401(client):
    resp = client.post(
        "/api/setup/complete",
        headers={"Authorization": "Bearer wrong-token"},
        json=_body(),
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_non_ascii_token_returns_401_not_500():
    # compare_digest on str raises TypeError for non-ASCII — must be a clean
    # 401, never an unhandled 500. Call the dependency directly (the HTTP
    # test client refuses to even send a non-ASCII header).
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials
    import backend.api.setup as setup_mod

    setup_mod._bootstrap_token = "real-token-123"
    try:
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="tokené")
        with pytest.raises(HTTPException) as exc:
            await setup_mod.require_setup_token(creds)
        assert exc.value.status_code == 401
    finally:
        setup_mod._bootstrap_token = None


def test_correct_token_returns_200_with_nexus_api_key(client):
    import backend.api.setup as setup_mod

    token = setup_mod._bootstrap_token
    assert token  # minted by ensure_bootstrap_token() at lifespan startup

    resp = client.post(
        "/api/setup/complete",
        headers={"Authorization": f"Bearer {token}"},
        json=_body(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["nexus_api_key"]


def test_token_file_written_then_destroyed(client, tmp_path):
    import backend.api.setup as setup_mod

    token_path = tmp_path / ".nexus-setup-token"
    assert token_path.exists()
    assert token_path.read_text(encoding="utf-8").strip() == setup_mod._bootstrap_token

    resp = client.post(
        "/api/setup/complete",
        headers={"Authorization": f"Bearer {setup_mod._bootstrap_token}"},
        json=_body(),
    )
    assert resp.status_code == 200
    assert not token_path.exists()
    assert setup_mod._bootstrap_token is None


def test_token_file_is_acl_hardened(client, monkeypatch):
    import backend.api.setup as setup_mod

    calls = []
    monkeypatch.setattr(
        setup_mod, "secure_key_file", lambda path=None: calls.append(path)
    )
    # Setup is still pending in this fixture's fresh state — re-running the
    # generator exercises the hardening call in isolation from the fixture's
    # own (already-happened) startup mint.
    setup_mod.ensure_bootstrap_token()
    assert calls
    assert calls[-1] == setup_mod._BOOTSTRAP_TOKEN_PATH


def test_token_single_use_replay_returns_401(client):
    import backend.api.setup as setup_mod

    token = setup_mod._bootstrap_token
    resp1 = client.post(
        "/api/setup/complete",
        headers={"Authorization": f"Bearer {token}"},
        json=_body(),
    )
    assert resp1.status_code == 200

    resp2 = client.post(
        "/api/setup/complete",
        headers={"Authorization": f"Bearer {token}"},
        json=_body(),
    )
    assert resp2.status_code == 401


def test_double_submit_already_configured_returns_409(client):
    # Simulates the race the lock closes: a second request arrives holding a
    # still-valid bootstrap token (not yet cleared) after another request has
    # already completed setup — must 409 inside the lock, not mint a second key.
    import backend.api.setup as setup_mod
    from backend.secrets import manager

    token = setup_mod._bootstrap_token
    assert token
    manager.set_secret("NEXUS_API_KEY", "already-set-by-another-request")

    resp = client.post(
        "/api/setup/complete",
        headers={"Authorization": f"Bearer {token}"},
        json=_body(),
    )
    assert resp.status_code == 409


def test_status_endpoint_stays_public(client):
    resp = client.get("/api/setup/status")
    assert resp.status_code == 200
    assert "needs_setup" in resp.json()


def test_ensure_bootstrap_token_noop_when_already_configured(client, tmp_path):
    import backend.api.setup as setup_mod
    from backend.secrets import manager

    manager.set_secret("NEXUS_API_KEY", "already-configured")
    setup_mod.ensure_bootstrap_token()

    assert setup_mod._bootstrap_token is None
    assert not (tmp_path / ".nexus-setup-token").exists()


def test_rejected_attempt_recorded_in_authfail(client):
    from backend.safety import authfail

    resp = client.post(
        "/api/setup/complete",
        headers={"Authorization": "Bearer wrong-token"},
        json=_body(),
    )
    assert resp.status_code == 401

    stats = authfail.recent(600)
    assert any(v["count"] >= 1 for v in stats.values())
    assert any(
        "/api/setup/complete" in [p for p, _ in v["paths"]] for v in stats.values()
    )
