"""Tests for GET /api/uptime/summary + GET /api/uptime/speedtest.

Covers:
  - auth is enforced on both endpoints
  - summary groups samples by source, computes uptime %, current_ok, avg latency
  - summary respects the ?days= cutoff window
  - speedtest returns time-ordered rows + the latest sample
  - empty-DB shapes (no sources / null latest)
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401 — registers all table metadata


def make_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


def _seed_uptime(eng, source, ok, latency_ms=None, checked_at=None):
    from backend.database import UptimeSample
    with Session(eng) as s:
        row = UptimeSample(source=source, ok=ok, latency_ms=latency_ms)
        if checked_at is not None:
            row.checked_at = checked_at
        s.add(row)
        s.commit()


def _seed_speedtest(eng, download_mbps, upload_mbps, ping_ms, checked_at=None):
    from backend.database import SpeedtestSample
    with Session(eng) as s:
        row = SpeedtestSample(
            download_mbps=download_mbps, upload_mbps=upload_mbps, ping_ms=ping_ms
        )
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

    test_engine = make_engine()
    monkeypatch.setattr("backend.database.engine", test_engine)

    from backend.database import get_session

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
        app.dependency_overrides[get_session] = override_session
        with TestClient(app) as c:
            c._engine = test_engine
            yield c
        app.dependency_overrides.clear()


def test_uptime_summary_requires_auth(uptime_client):
    assert uptime_client.get("/api/uptime/summary").status_code == 401


def test_speedtest_requires_auth(uptime_client):
    assert uptime_client.get("/api/uptime/speedtest").status_code == 401


def test_uptime_summary_empty(uptime_client, auth_headers):
    resp = uptime_client.get("/api/uptime/summary", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["sources"] == []
    assert "generated_at" in body


def test_uptime_summary_groups_and_computes(uptime_client, auth_headers):
    eng = uptime_client._engine
    now = datetime.utcnow()
    # unraid: 3 samples, 2 ok, latest ok, latencies 10/30 (one None)
    _seed_uptime(eng, "unraid", True, 10, now - timedelta(minutes=4))
    _seed_uptime(eng, "unraid", False, None, now - timedelta(minutes=3))
    _seed_uptime(eng, "unraid", True, 30, now - timedelta(minutes=1))
    # adguard: 1 sample, down
    _seed_uptime(eng, "adguard", False, 5, now - timedelta(minutes=2))

    resp = uptime_client.get("/api/uptime/summary", headers=auth_headers)
    assert resp.status_code == 200
    by = {s["source"]: s for s in resp.json()["sources"]}

    assert by["unraid"]["samples"] == 3
    assert by["unraid"]["uptime_pct"] == round(100 * 2 / 3, 1)
    assert by["unraid"]["current_ok"] is True
    assert by["unraid"]["avg_latency_ms"] == 20  # (10+30)/2

    assert by["adguard"]["samples"] == 1
    assert by["adguard"]["uptime_pct"] == 0.0
    assert by["adguard"]["current_ok"] is False
    assert by["adguard"]["avg_latency_ms"] == 5


def test_uptime_summary_respects_days_cutoff(uptime_client, auth_headers):
    eng = uptime_client._engine
    now = datetime.utcnow()
    _seed_uptime(eng, "unraid", True, 10, now - timedelta(days=30))  # outside 7d
    _seed_uptime(eng, "unraid", True, 10, now - timedelta(days=1))   # inside 7d

    resp = uptime_client.get("/api/uptime/summary", headers=auth_headers, params={"days": 7})
    assert resp.status_code == 200
    sources = resp.json()["sources"]
    assert len(sources) == 1
    assert sources[0]["samples"] == 1


def test_speedtest_empty(uptime_client, auth_headers):
    resp = uptime_client.get("/api/uptime/speedtest", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["latest"] is None


def test_speedtest_returns_ordered_rows_and_latest(uptime_client, auth_headers):
    eng = uptime_client._engine
    now = datetime.utcnow()
    _seed_speedtest(eng, 100.0, 10.0, 12.0, now - timedelta(hours=2))
    _seed_speedtest(eng, 300.0, 20.0, 8.0, now - timedelta(minutes=5))

    resp = uptime_client.get("/api/uptime/speedtest", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    # ordered oldest-first by checked_at
    assert body["data"][0]["download_mbps"] == 100.0
    assert body["data"][1]["download_mbps"] == 300.0
    # latest is the newest row
    assert body["latest"]["download_mbps"] == 300.0
    assert body["latest"]["ping_ms"] == 8.0
