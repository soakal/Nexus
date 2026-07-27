"""The unauthenticated first-run setup gate, and the docs surface it lives next to.

`POST /api/setup/complete` writes ANTHROPIC_API_KEY + a fresh NEXUS_API_KEY with
no credential of any kind, so `_needs_setup()` is the ONLY thing standing between
a LAN/tailnet caller and a takeover of a provisioned install. It must fail closed.
"""
import pytest

from backend.api import setup as setup_api
from tests.test_api import client  # noqa: F401 — FastAPI TestClient fixture


def test_needs_setup_true_when_key_absent(monkeypatch):
    def missing(key, fallback_env=True):
        raise KeyError(key)

    monkeypatch.setattr("backend.secrets.manager.get_secret", missing)
    assert setup_api._needs_setup() is True


def test_needs_setup_false_when_key_present(monkeypatch):
    monkeypatch.setattr("backend.secrets.manager.get_secret", lambda k, fallback_env=True: "live-key")
    assert setup_api._needs_setup() is False


@pytest.mark.parametrize("exc", [RuntimeError("vault unreadable"), ValueError("corrupt json"), OSError("store down")])
def test_needs_setup_fails_closed_on_store_error(monkeypatch, exc):
    """A store that errors is NOT proof the install is unprovisioned."""
    def boom(key, fallback_env=True):
        raise exc

    monkeypatch.setattr("backend.secrets.manager.get_secret", boom)
    assert setup_api._needs_setup() is False


def test_setup_complete_rejected_when_configured(client):  # noqa: F811 — imported fixture
    resp = client.post(
        "/api/setup/complete",
        json={"anthropic_api_key": "sk-ant-attacker"},
    )
    assert resp.status_code == 409


def test_openapi_docs_disabled_by_default(client):  # noqa: F811 — imported fixture
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
