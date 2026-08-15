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
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock), \
         patch("backend.state_workers.prime_state_workers", new_callable=AsyncMock):
        # prime_state_workers is spawned as a lifespan background task
        # (backend/main.py:135) -- unpatched, its collectors fail against
        # nothing-real and state_store.store_failure() writes durable rows
        # into the session-scoped shared test DB (conftest's reset_caches
        # only clears the in-memory cache, not the DB). A later test's own
        # dashboard-state read can then find a seconds-old failure row and
        # misreport `freshness` as "fresh" instead of "never_observed" --
        # reproduced live 2026-08-14, order-dependent
        # (single_cached_read -> stale_metadata fails, reversed passes).
        sched.running = False
        from backend.main import app
        with TestClient(app) as c:
            yield c


def test_dashboard_state_is_single_cached_read(client, auth_headers):
    """No live integration call is triggered — the endpoint only ever reads
    already-persisted snapshots."""
    state_store.store_success("source.homeassistant", {"healthy": True}, 60)
    state_store.store_success("dashboard.weather", {"temp_f": 72}, 60)
    state_store.store_success(
        "dashboard.today", {"calendar": "3pm meeting", "email": "2 unread"}, 600
    )

    with patch("backend.integrations.homeassistant.health_check", new_callable=AsyncMock) as hc, \
         patch("backend.integrations.calendar.get_today_events", new_callable=AsyncMock) as cal:
        resp = client.get("/api/dashboard/state", headers=auth_headers)
        hc.assert_not_called()
        cal.assert_not_called()

    assert resp.status_code == 200
    body = resp.json()
    assert body["sources"]["homeassistant"]["healthy"] is True
    assert body["sources"]["homeassistant"]["freshness"] == "fresh"
    assert body["weather"]["data"] == {"temp_f": 72}
    assert body["weather"]["freshness"] == "fresh"
    assert body["today"]["data"] == {"calendar": "3pm meeting", "email": "2 unread"}
    assert body["today"]["freshness"] == "fresh"


def test_dashboard_state_exposes_stale_metadata(client, auth_headers):
    """A source that has never been observed reads as never_observed, not a 500."""
    resp = client.get("/api/dashboard/state", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["sources"]["unifi"]["healthy"] is False
    assert body["sources"]["unifi"]["freshness"] == "never_observed"
    assert body["mail"]["data"] is None
    assert body["mail"]["freshness"] == "never_observed"
    assert body["claude_usage"]["data"] is None
    assert body["claude_usage"]["freshness"] == "never_observed"
    assert body["openrouter"]["data"] is None
    assert body["openrouter"]["freshness"] == "never_observed"


def test_dashboard_state_exposes_claude_usage(client, auth_headers):
    state_store.store_success(
        "dashboard.claude_usage",
        {
            "available": True,
            "captured_at": 1785966702.124308,
            "five_hour": {"used_percentage": 45.0, "resets_at": 1785967200},
            "seven_day": {"used_percentage": 7.0, "resets_at": 1786514400},
            "error": None,
        },
        120,
    )
    resp = client.get("/api/dashboard/state", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["claude_usage"]["data"]["five_hour"]["used_percentage"] == 45.0
    assert body["claude_usage"]["data"]["seven_day"]["resets_at"] == 1786514400
    assert body["claude_usage"]["freshness"] == "fresh"


def test_dashboard_state_exposes_openrouter(client, auth_headers):
    state_store.store_success(
        "dashboard.openrouter",
        {
            "available": True,
            "model_count": 400,
            "credit_limit": 5.0,
            "credit_remaining": 0.0,
            "usage": 5.05146565,
            "usage_daily": 0.0,
            "is_free_tier": False,
            "account_total_credits": 30.0,
            "account_total_usage": 17.35,
            "account_balance": 12.65,
        },
        600,
    )
    resp = client.get("/api/dashboard/state", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["openrouter"]["data"]["credit_remaining"] == 0.0
    assert body["openrouter"]["data"]["usage"] == pytest.approx(5.05146565)
    assert body["openrouter"]["data"]["account_balance"] == pytest.approx(12.65)
    assert body["openrouter"]["freshness"] == "fresh"
