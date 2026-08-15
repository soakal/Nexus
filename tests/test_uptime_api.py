"""Tests for GET /api/uptime/summary — decommissioned-source exclusion."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine


def make_engine(db_path):
    eng = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(eng)
    return eng


def _seed_sample(eng, source, ok=True, checked_at=None):
    from backend.database import UptimeSample
    with Session(eng) as s:
        row = UptimeSample(source=source, ok=ok)
        if checked_at is not None:
            row.checked_at = checked_at
        s.add(row)
        s.commit()


@pytest.fixture
def uptime_client(tmp_path, monkeypatch):
    vault_key = tmp_path / ".vault.key"
    vault_file = tmp_path / "nexus.vault"
    vault_key.write_bytes(b"A" * 32)
    vault_file.write_text("{}")
    monkeypatch.chdir(tmp_path)

    import backend.database  # noqa: F401 — registers all table metadata
    test_engine = make_engine(tmp_path / "test_uptime_api.db")
    monkeypatch.setattr("backend.database.engine", test_engine)

    from backend.database import get_session

    def override_session():
        with Session(test_engine) as session:
            yield session

    with patch("backend.database.create_db_and_tables"), \
         patch("backend.scheduler.setup_scheduler"), \
         patch("backend.scheduler.scheduler") as sched, \
         patch("backend.agents.memo_watcher.start_watcher_blocking"), \
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock), \
         patch("backend.state_workers.prime_state_workers", new_callable=AsyncMock):
        sched.running = False
        from backend.main import app
        app.dependency_overrides[get_session] = override_session
        with TestClient(app) as c:
            c._engine = test_engine
            yield c
        app.dependency_overrides.clear()


def test_uptime_summary_excludes_decommissioned_hermes(uptime_client, auth_headers):
    """Hermes, retired 2026-08-09, must never appear in the live summary --
    even though its historical rows are deliberately never deleted (they age
    out of the query window naturally), a currently-unmonitored source
    showing "up" for days is worse than a source that's simply absent."""
    now = datetime.utcnow()
    _seed_sample(uptime_client._engine, "hermes", ok=True, checked_at=now)
    _seed_sample(uptime_client._engine, "homeassistant", ok=True, checked_at=now)

    resp = uptime_client.get("/api/uptime/summary", headers=auth_headers)
    assert resp.status_code == 200
    sources = {s["source"] for s in resp.json()["sources"]}
    assert "hermes" not in sources
    assert "homeassistant" in sources


def test_uptime_summary_old_hermes_rows_outside_window_also_excluded(uptime_client, auth_headers):
    stale = datetime.utcnow() - timedelta(days=30)
    _seed_sample(uptime_client._engine, "hermes", ok=True, checked_at=stale)

    resp = uptime_client.get("/api/uptime/summary?days=7", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["sources"] == []
