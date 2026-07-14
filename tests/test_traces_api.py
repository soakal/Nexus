"""Tests for GET /api/traces + GET /api/traces/{id} (council w-observability).

Covers:
  - auth is enforced on both endpoints
  - list shape + newest-first ordering + ?kind= filter
  - single-trace shape with spans ordered by started_at
  - 404 when the trace does not exist
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


def _seed_trace(eng, kind="orchestrator", label="test run", started_at=None, status="ok"):
    from backend.database import AgentTrace
    with Session(eng) as s:
        row = AgentTrace(kind=kind, label=label, status=status)
        if started_at is not None:
            row.started_at = started_at
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def _seed_span(eng, trace_id, span_type="llm_call", name="claude-sonnet-4-6", started_at=None):
    from backend.database import TraceSpan
    with Session(eng) as s:
        row = TraceSpan(trace_id=trace_id, span_type=span_type, name=name)
        if started_at is not None:
            row.started_at = started_at
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


@pytest.fixture
def traces_client(tmp_path, monkeypatch):
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


def test_list_traces_requires_auth(traces_client):
    resp = traces_client.get("/api/traces")
    assert resp.status_code == 401


def test_get_trace_requires_auth(traces_client):
    resp = traces_client.get("/api/traces/1")
    assert resp.status_code == 401


def test_list_traces_newest_first(traces_client, auth_headers):
    now = datetime.utcnow()
    older_id = _seed_trace(traces_client._engine, label="older", started_at=now - timedelta(hours=1))
    newer_id = _seed_trace(traces_client._engine, label="newer", started_at=now)

    resp = traces_client.get("/api/traces", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["id"] == newer_id
    assert body[1]["id"] == older_id
    for key in ("id", "kind", "label", "task_id", "started_at", "ended_at", "status", "error"):
        assert key in body[0]


def test_list_traces_kind_filter(traces_client, auth_headers):
    _seed_trace(traces_client._engine, kind="chat", label="a chat")
    _seed_trace(traces_client._engine, kind="orchestrator", label="an orchestrator run")

    resp = traces_client.get("/api/traces", headers=auth_headers, params={"kind": "chat"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["kind"] == "chat"


def test_list_traces_limit_capped_at_200(traces_client, auth_headers):
    resp = traces_client.get("/api/traces", headers=auth_headers, params={"limit": 5000})
    assert resp.status_code == 200
    # No rows seeded, but confirms the endpoint accepts an oversized limit
    # without erroring (the internal cap is exercised, not directly observable
    # via this response since fewer than 200 rows exist).
    assert resp.json() == []


def test_get_trace_with_spans_ordered_by_started_at(traces_client, auth_headers):
    trace_id = _seed_trace(traces_client._engine, kind="orchestrator", label="run with spans")
    now = datetime.utcnow()
    later_id = _seed_span(traces_client._engine, trace_id, name="second", started_at=now)
    earlier_id = _seed_span(traces_client._engine, trace_id, name="first", started_at=now - timedelta(seconds=30))

    resp = traces_client.get(f"/api/traces/{trace_id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == trace_id
    assert body["kind"] == "orchestrator"
    assert len(body["spans"]) == 2
    assert body["spans"][0]["id"] == earlier_id
    assert body["spans"][1]["id"] == later_id
    for key in (
        "id", "trace_id", "parent_span_id", "span_type", "name", "started_at",
        "ended_at", "duration_ms", "input_summary", "output_summary",
        "tokens_in", "tokens_out", "cost_usd", "error",
    ):
        assert key in body["spans"][0]


def test_get_trace_404_when_absent(traces_client, auth_headers):
    resp = traces_client.get("/api/traces/999999", headers=auth_headers)
    assert resp.status_code == 404
