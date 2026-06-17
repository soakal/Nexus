"""Tests for HMAC-SHA256 signature verification on /api/trigger.

Design notes
------------
- The autouse `reset_caches` fixture in conftest.py already calls
  `_reset_rate_limit()` before every test, so the rate limiter never trips here.
- The autouse `mock_secrets` fixture sets HERMES_WEBHOOK_SECRET = "test-hermes-secret".
  We read it via `get_settings().hermes_webhook_secret` so the test signs with the
  same secret the server verifies against.
- Body-bytes parity: we send `content=payload_bytes` (raw bytes) with an explicit
  `Content-Type: application/json` header and sign those SAME bytes. This guarantees
  that the body the server reads (via `await request.body()`) is byte-identical to
  what we signed.
- `trigger_hmac_required` and other settings are toggled via monkeypatch.setattr on
  the cached settings instance (`get_settings()`), matching the pattern used
  elsewhere in this test suite.
"""
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine
from sqlmodel.pool import StaticPool

from backend.api.trigger import compute_trigger_signature
from backend.config import get_settings


# ---------------------------------------------------------------------------
# Shared fixture — a running TestClient with auth, mirrors app_client in
# test_api_endpoints.py but lives here so this file is self-contained.
# ---------------------------------------------------------------------------

def _make_test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def trigger_client(tmp_path, monkeypatch):
    """TestClient wired up identically to app_client in test_api_endpoints.py."""
    vault_key = tmp_path / ".vault.key"
    vault_file = tmp_path / "nexus.vault"
    vault_key.write_bytes(b"A" * 32)
    vault_file.write_text("{}")
    monkeypatch.chdir(tmp_path)

    test_engine = _make_test_engine()
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


# Convenience — the Bearer header every authed call needs.
_AUTH = {"Authorization": "Bearer test-nexus-key"}

# The request body used across tests (must stay consistent).
_BODY_DICT = {"task_name": "briefing", "parameters": {}}
_BODY_BYTES = json.dumps(_BODY_DICT, separators=(",", ":")).encode()


def _signed_headers(secret: str, body: bytes, ts: float | None = None) -> dict:
    """Return headers dict with X-Timestamp + X-Signature (plus Bearer auth)."""
    timestamp = str(ts if ts is not None else time.time())
    sig = compute_trigger_signature(secret, timestamp, body)
    return {
        **_AUTH,
        "X-Timestamp": timestamp,
        "X-Signature": sig,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# 1. Backward compat: hmac NOT required, NO signature headers, valid Bearer → 200
# ---------------------------------------------------------------------------

def test_backward_compat_no_signature_bearer_only(trigger_client, monkeypatch):
    """Critical no-regression test: existing Bearer-only callers keep working when
    trigger_hmac_required is False (the default)."""
    # Ensure the default (not required)
    monkeypatch.setattr(get_settings(), "trigger_hmac_required", False)

    with patch("backend.agents.briefing.run_briefing", new_callable=AsyncMock):
        resp = trigger_client.post(
            "/api/trigger",
            json=_BODY_DICT,
            headers=_AUTH,
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# 2. HMAC required, no signature headers → 401
# ---------------------------------------------------------------------------

def test_hmac_required_no_signature_returns_401(trigger_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "trigger_hmac_required", True)

    resp = trigger_client.post(
        "/api/trigger",
        json=_BODY_DICT,
        headers=_AUTH,
    )
    assert resp.status_code == 401
    assert "Missing trigger signature" in resp.json()["detail"]

    # Restore so other tests don't inherit the flag (monkeypatch cleans up, but
    # be explicit for clarity).
    monkeypatch.setattr(get_settings(), "trigger_hmac_required", False)


# ---------------------------------------------------------------------------
# 3. Valid signature → 200
# ---------------------------------------------------------------------------

def test_valid_signature_returns_200(trigger_client, monkeypatch):
    """Sign the exact bytes sent as the request body; server must accept."""
    monkeypatch.setattr(get_settings(), "trigger_hmac_required", True)

    secret = get_settings().hermes_webhook_secret  # "test-hermes-secret" from conftest

    headers = _signed_headers(secret, _BODY_BYTES)

    with patch("backend.agents.briefing.run_briefing", new_callable=AsyncMock):
        resp = trigger_client.post(
            "/api/trigger",
            content=_BODY_BYTES,       # raw bytes — byte-identical to what we signed
            headers=headers,
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    monkeypatch.setattr(get_settings(), "trigger_hmac_required", False)


# ---------------------------------------------------------------------------
# 4. Bad signature (wrong digest) → 401
# ---------------------------------------------------------------------------

def test_bad_signature_returns_401(trigger_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "trigger_hmac_required", True)

    timestamp = str(time.time())
    bad_sig = "deadbeef" * 8  # 64 hex chars — right length, wrong value

    headers = {
        **_AUTH,
        "X-Timestamp": timestamp,
        "X-Signature": bad_sig,
        "Content-Type": "application/json",
    }
    resp = trigger_client.post(
        "/api/trigger",
        content=_BODY_BYTES,
        headers=headers,
    )
    assert resp.status_code == 401
    assert "Bad trigger signature" in resp.json()["detail"]

    monkeypatch.setattr(get_settings(), "trigger_hmac_required", False)


# ---------------------------------------------------------------------------
# 5. Stale timestamp (outside replay window) → 401
# ---------------------------------------------------------------------------

def test_stale_timestamp_returns_401(trigger_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "trigger_hmac_required", True)
    window = get_settings().trigger_hmac_window_s  # default 300

    secret = get_settings().hermes_webhook_secret
    stale_ts = time.time() - (10 * window)  # well outside the window

    headers = _signed_headers(secret, _BODY_BYTES, ts=stale_ts)

    resp = trigger_client.post(
        "/api/trigger",
        content=_BODY_BYTES,
        headers=headers,
    )
    assert resp.status_code == 401
    assert "outside window" in resp.json()["detail"]

    monkeypatch.setattr(get_settings(), "trigger_hmac_required", False)


# ---------------------------------------------------------------------------
# 6. Tampered body: sign one payload, send a different payload → 401
# ---------------------------------------------------------------------------

def test_tampered_body_returns_401(trigger_client, monkeypatch):
    """Sign a known-good body, but send a different body — digest must not match."""
    monkeypatch.setattr(get_settings(), "trigger_hmac_required", True)

    secret = get_settings().hermes_webhook_secret
    signed_body = _BODY_BYTES  # what we sign
    tampered_body = json.dumps(
        {"task_name": "status", "parameters": {}}, separators=(",", ":")
    ).encode()  # different payload sent over the wire

    # Sign the original body but send the tampered one
    timestamp = str(time.time())
    sig = compute_trigger_signature(secret, timestamp, signed_body)

    headers = {
        **_AUTH,
        "X-Timestamp": timestamp,
        "X-Signature": sig,
        "Content-Type": "application/json",
    }
    resp = trigger_client.post(
        "/api/trigger",
        content=tampered_body,
        headers=headers,
    )
    assert resp.status_code == 401
    assert "Bad trigger signature" in resp.json()["detail"]

    monkeypatch.setattr(get_settings(), "trigger_hmac_required", False)


# ---------------------------------------------------------------------------
# 7. Defense in depth: signature presented + hmac NOT required → still verified
# ---------------------------------------------------------------------------

def test_bad_signature_rejected_even_when_not_required(trigger_client, monkeypatch):
    """Even when trigger_hmac_required=False, a presented (but wrong) signature
    must still be rejected — defense in depth."""
    monkeypatch.setattr(get_settings(), "trigger_hmac_required", False)

    timestamp = str(time.time())
    bad_sig = "cafebabe" * 8

    headers = {
        **_AUTH,
        "X-Timestamp": timestamp,
        "X-Signature": bad_sig,
        "Content-Type": "application/json",
    }
    resp = trigger_client.post(
        "/api/trigger",
        content=_BODY_BYTES,
        headers=headers,
    )
    assert resp.status_code == 401
    assert "Bad trigger signature" in resp.json()["detail"]
