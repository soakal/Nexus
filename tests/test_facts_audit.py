"""Tests for the Fact audit/recall API + soft-dismiss (spec: council w33gixx93).

Covers:
  1. dismissed_at column: a Fact with dismissed_at can be created/read.
  2. _db_active_facts excludes dismissed facts; facts_recall skips them.
  3. list_facts_for_audit returns effective_confidence + above_floor correctly.
  4. dismiss_fact soft-sets dismissed_at (no DELETE); returns False for unknown id.
  5. API: GET /api/facts/ lists active; GET /api/facts/recall returns {query,result};
     POST /api/facts/{id}/dismiss → 200 then excluded from GET; unknown id → 404.
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch, AsyncMock

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select


# ---------------------------------------------------------------------------
# Helpers shared across this module
# ---------------------------------------------------------------------------

def _make_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


def _seed_fact(engine, *, subject, predicate, value,
               confidence=0.8, source="manual", created_at=None, dismissed_at=None):
    """Insert a Fact row directly for test seeding. Returns the fact id."""
    from backend.database import Fact
    now = datetime.utcnow()
    f = Fact(
        subject=subject,
        predicate=predicate,
        value=value,
        confidence=confidence,
        source=source,
        created_at=created_at or now,
        updated_at=now,
        last_seen_at=now,
        dismissed_at=dismissed_at,
    )
    with Session(engine) as s:
        s.add(f)
        s.commit()
        s.refresh(f)
        return f.id


def _get_fact(engine, fact_id):
    from backend.database import Fact
    with Session(engine) as s:
        return s.get(Fact, fact_id)


# ---------------------------------------------------------------------------
# 1. dismissed_at column — model round-trip
# ---------------------------------------------------------------------------

def test_fact_dismissed_at_field_exists():
    """Fact model must have a dismissed_at field that round-trips through SQLite."""
    from backend.database import Fact
    eng = _make_engine()
    now = datetime.utcnow()
    f = Fact(
        subject="test", predicate="has", value="value",
        confidence=0.7, source="manual",
        created_at=now, updated_at=now, last_seen_at=now,
        dismissed_at=now,
    )
    with Session(eng) as s:
        s.add(f)
        s.commit()
        s.refresh(f)
        assert f.dismissed_at is not None
        # Compare at second precision (SQLite TIMESTAMP may drop sub-second)
        assert abs((f.dismissed_at - now).total_seconds()) < 2


def test_fact_dismissed_at_defaults_to_none():
    """A new Fact without dismissed_at must have dismissed_at=None."""
    from backend.database import Fact
    eng = _make_engine()
    now = datetime.utcnow()
    f = Fact(subject="u", predicate="p", value="v", created_at=now, updated_at=now, last_seen_at=now)
    with Session(eng) as s:
        s.add(f)
        s.commit()
        s.refresh(f)
        assert f.dismissed_at is None


# ---------------------------------------------------------------------------
# 2. _db_active_facts excludes dismissed; facts_recall skips dismissed
# ---------------------------------------------------------------------------

def test_db_active_facts_excludes_dismissed(monkeypatch):
    """A dismissed fact must not appear in _db_active_facts."""
    from backend.agents.facts import _db_active_facts

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    active_id   = _seed_fact(eng, subject="user", predicate="name", value="Brian")
    dismissed_id = _seed_fact(eng, subject="user", predicate="city", value="OldCity",
                              dismissed_at=datetime.utcnow())

    active = _db_active_facts()
    ids = [f["id"] for f in active]
    assert active_id in ids, "Active fact must appear in _db_active_facts"
    assert dismissed_id not in ids, "Dismissed fact must NOT appear in _db_active_facts"


@pytest.mark.asyncio
async def test_facts_recall_skips_dismissed(monkeypatch):
    """facts_recall must not surface a dismissed fact."""
    from backend.agents.facts import facts_recall

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    # Active fact with strong signal
    _seed_fact(eng, subject="user", predicate="name", value="Brian", confidence=0.9)
    # Dismissed fact that would otherwise match the query keyword
    _seed_fact(eng, subject="user", predicate="city", value="SecretCity", confidence=0.9,
               dismissed_at=datetime.utcnow())

    result = await facts_recall("SecretCity user location")
    assert "SecretCity" not in result, "Dismissed fact value must not appear in recall result"


# ---------------------------------------------------------------------------
# 3. list_facts_for_audit — effective_confidence + above_floor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_facts_for_audit_fresh_fact_above_floor(monkeypatch):
    """A fresh high-confidence fact must have above_floor=True."""
    from backend.agents.facts import list_facts_for_audit

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    _seed_fact(eng, subject="user", predicate="name", value="Brian", confidence=0.9)

    facts = await list_facts_for_audit()
    assert len(facts) == 1
    f = facts[0]
    assert f["above_floor"] is True
    assert f["effective_confidence"] > 0.2
    assert "subject" in f
    assert "predicate" in f
    assert "value" in f
    assert "created_at" in f
    assert "last_seen_at" in f


@pytest.mark.asyncio
async def test_list_facts_for_audit_old_fact_below_floor(monkeypatch):
    """A very old low-confidence fact must have above_floor=False (still listed)."""
    from backend.agents.facts import list_facts_for_audit, EFFECTIVE_FLOOR

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    # conf=0.5, age=200 days: eff = 0.5 * 0.5^(200/30) ≈ 0.5 * 0.0099 ≈ 0.005 < 0.2
    old_ts = datetime.utcnow() - timedelta(days=200)
    _seed_fact(eng, subject="server", predicate="name", value="OldServer",
               confidence=0.5, created_at=old_ts)

    facts = await list_facts_for_audit()
    assert len(facts) == 1
    f = facts[0]
    assert f["above_floor"] is False
    assert f["effective_confidence"] < EFFECTIVE_FLOOR


@pytest.mark.asyncio
async def test_list_facts_for_audit_excludes_dismissed(monkeypatch):
    """Dismissed facts must not appear in list_facts_for_audit."""
    from backend.agents.facts import list_facts_for_audit

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    _seed_fact(eng, subject="user", predicate="name", value="Brian", confidence=0.9)
    _seed_fact(eng, subject="user", predicate="city", value="Gone",
               confidence=0.9, dismissed_at=datetime.utcnow())

    facts = await list_facts_for_audit()
    assert len(facts) == 1
    assert facts[0]["value"] == "Brian"


@pytest.mark.asyncio
async def test_list_facts_for_audit_empty(monkeypatch):
    """Empty table must return an empty list, not raise."""
    from backend.agents.facts import list_facts_for_audit

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    facts = await list_facts_for_audit()
    assert facts == []


# ---------------------------------------------------------------------------
# 4. dismiss_fact — soft sets dismissed_at; returns False for unknown id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dismiss_fact_sets_dismissed_at(monkeypatch):
    """dismiss_fact must set dismissed_at and return True."""
    from backend.agents.facts import dismiss_fact

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    fid = _seed_fact(eng, subject="user", predicate="pref", value="dark")
    before = _get_fact(eng, fid)
    assert before.dismissed_at is None

    result = await dismiss_fact(fid)
    assert result is True

    after = _get_fact(eng, fid)
    assert after is not None, "Row must be preserved (soft delete)"
    assert after.dismissed_at is not None


@pytest.mark.asyncio
async def test_dismiss_fact_row_preserved(monkeypatch):
    """After dismiss_fact, the row still exists with its original fields."""
    from backend.agents.facts import dismiss_fact

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    fid = _seed_fact(eng, subject="server", predicate="ip", value="192.168.1.1")
    await dismiss_fact(fid)

    row = _get_fact(eng, fid)
    assert row.value == "192.168.1.1"   # original data preserved
    assert row.superseded_by is None    # not superseded, just dismissed


@pytest.mark.asyncio
async def test_dismiss_fact_unknown_id_returns_false(monkeypatch):
    """dismiss_fact with an id that doesn't exist must return False."""
    from backend.agents.facts import dismiss_fact

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    result = await dismiss_fact(99999)
    assert result is False


# ---------------------------------------------------------------------------
# 5. API endpoints via TestClient
# ---------------------------------------------------------------------------

@pytest.fixture
def facts_client(tmp_path, monkeypatch):
    """TestClient fixture wired to an in-memory DB, same pattern as test_api_endpoints."""
    vault_key = tmp_path / ".vault.key"
    vault_file = tmp_path / "nexus.vault"
    vault_key.write_bytes(b"A" * 32)
    vault_file.write_text("{}")
    monkeypatch.chdir(tmp_path)

    from sqlmodel import Session as _Sess
    from sqlalchemy.pool import StaticPool as _SP
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=_SP,
    )
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr("backend.database.engine", test_engine)

    def override_session():
        with _Sess(test_engine) as s:
            yield s

    with patch("backend.database.create_db_and_tables"), \
         patch("backend.scheduler.setup_scheduler"), \
         patch("backend.scheduler.scheduler") as sched, \
         patch("backend.agents.memo_watcher.start_watcher_blocking"), \
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock):
        sched.running = False
        from fastapi.testclient import TestClient
        from backend.main import app
        from backend.database import get_session
        app.dependency_overrides[get_session] = override_session
        with TestClient(app) as c:
            yield c, test_engine
        app.dependency_overrides.clear()


AUTH = {"Authorization": "Bearer test-nexus-key"}


def test_api_facts_list_empty(facts_client):
    client, _ = facts_client
    resp = client.get("/api/facts/", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == []


def test_api_facts_list_requires_auth(facts_client):
    client, _ = facts_client
    resp = client.get("/api/facts/")
    assert resp.status_code == 401


def test_api_facts_list_returns_active_facts(facts_client):
    client, eng = facts_client
    _seed_fact(eng, subject="user", predicate="name", value="Brian", confidence=0.9)
    resp = client.get("/api/facts/", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    f = data[0]
    assert f["subject"] == "user"
    assert f["predicate"] == "name"
    assert f["value"] == "Brian"
    assert "effective_confidence" in f
    assert "above_floor" in f


def test_api_facts_recall_returns_query_and_result(facts_client):
    client, eng = facts_client
    _seed_fact(eng, subject="user", predicate="name", value="Brian", confidence=0.9)
    resp = client.get("/api/facts/recall?query=user+name", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert "query" in data
    assert "result" in data
    assert data["query"] == "user name"


def test_api_facts_recall_requires_auth(facts_client):
    client, _ = facts_client
    resp = client.get("/api/facts/recall?query=test")
    assert resp.status_code == 401


def test_api_facts_dismiss_removes_from_list(facts_client):
    client, eng = facts_client
    fid = _seed_fact(eng, subject="user", predicate="pref", value="dark mode", confidence=0.9)

    # Confirm it's listed
    resp = client.get("/api/facts/", headers=AUTH)
    assert resp.status_code == 200
    assert any(f["id"] == fid for f in resp.json())

    # Dismiss it
    resp2 = client.post(f"/api/facts/{fid}/dismiss", headers=AUTH)
    assert resp2.status_code == 200
    assert resp2.json() == {"id": fid, "dismissed": True}

    # No longer in list
    resp3 = client.get("/api/facts/", headers=AUTH)
    assert resp3.status_code == 200
    assert not any(f["id"] == fid for f in resp3.json())


def test_api_facts_dismiss_unknown_id_returns_404(facts_client):
    client, _ = facts_client
    resp = client.post("/api/facts/99999/dismiss", headers=AUTH)
    assert resp.status_code == 404


def test_api_facts_dismiss_requires_auth(facts_client):
    client, eng = facts_client
    fid = _seed_fact(eng, subject="x", predicate="y", value="z")
    resp = client.post(f"/api/facts/{fid}/dismiss")
    assert resp.status_code == 401


def test_api_facts_row_preserved_after_dismiss(facts_client):
    """After API dismiss the row still exists in the DB (soft delete)."""
    client, eng = facts_client
    fid = _seed_fact(eng, subject="server", predicate="ip", value="10.0.0.1", confidence=0.9)
    client.post(f"/api/facts/{fid}/dismiss", headers=AUTH)
    row = _get_fact(eng, fid)
    assert row is not None
    assert row.value == "10.0.0.1"
    assert row.dismissed_at is not None
